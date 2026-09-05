"""媒体用户状态与播放列表领域操作；所有写入由 Provider 冻结计划确认后调用。

协议依据：Jellyfin 官方 OpenAPI（含用户、私有列表及 EntryIds 契约）
https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json
Emby 复用官方 UserLibraryService / PlaystateService / PlaylistService：
https://dev.emby.media/reference/RestAPI/PlaylistService.html
Emby API-key 创建接口不保证明确用户归属，因此不静默降级为公开列表。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.provider_models import ProviderGatewayError, ProviderPayload

_STATE_WRITES = {
    "media.user.mark_played": ("Played", True, "标记已看"),
    "media.user.mark_unplayed": ("Played", False, "标记未看"),
    "media.user.favorite": ("IsFavorite", True, "收藏"),
    "media.user.unfavorite": ("IsFavorite", False, "取消收藏"),
}
_PLAYLIST_WRITES = {
    "media.playlist.create",
    "media.playlist.add_items",
    "media.playlist.remove_items",
}
USER_READS = frozenset(
    {"media.user.inspect", "media.playlists.list", "media.playlist.inspect"}
)
USER_WRITES = frozenset(_STATE_WRITES) | _PLAYLIST_WRITES
_PLAYLIST_LIMIT = 250


class _BoundedMediaClient:
    """一项领域操作共享20秒预算，避免多条目预检叠加为数分钟。"""

    def __init__(self, client: Any):
        self.client = client
        self.deadline = time.monotonic() + 20

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    @property
    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderGatewayError(
                "媒体检查预算已用尽，请缩小条目范围", code="upstream_timeout"
            )
        return min(float(self.client.timeout), remaining)

    def _request(self, path: str, params=None):
        return self.client._request(path, params=params, timeout=self.timeout)


def _id(value: object) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProviderGatewayError(
            "媒体对象标识已失效，请重新读取", code="confirmation_stale"
        )
    return value


def _freeze(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _thaw(value: object, user_id: str) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
        if not isinstance(result, dict) or result.get("user") != user_id:
            raise ValueError
        _id(result.get("id"))
        return result
    except (ValueError, TypeError) as exc:
        raise ProviderGatewayError(
            "媒体用户或快照已失效，请重新检查", code="confirmation_stale"
        ) from exc


def _item(client: Any, user_id: str, item_id: str) -> dict[str, Any]:
    result = client._request(f"/Users/{_id(user_id)}/Items/{_id(item_id)}")
    if (
        not isinstance(result, dict)
        or str(result.get("Id")) != item_id
        or not result.get("Name")
    ):
        raise ProviderGatewayError(
            "媒体条目已不存在或返回无效", code="confirmation_stale"
        )
    return result


def _state_snapshot(item: dict[str, Any], user_id: str) -> dict[str, Any]:
    data = item.get("UserData")
    data = data if isinstance(data, dict) else {}
    return {
        "id": _id(item.get("Id")),
        "user": user_id,
        "name": str(item["Name"]),
        "type": str(item.get("Type") or ""),
        "state": {
            key: data.get(key)
            for key in (
                "Played",
                "PlayCount",
                "PlaybackPositionTicks",
                "LastPlayedDate",
                "IsFavorite",
            )
        },
    }


def _playlist_page(
    client: Any, user_id: str, playlist_id: str, *, start: int, limit: int
) -> dict[str, Any]:
    raw = client._request(
        f"/Playlists/{_id(playlist_id)}/Items",
        params={
            "UserId": _id(user_id),
            "StartIndex": start,
            "Limit": limit,
            "EnableImages": "false",
            "EnableTotalRecordCount": "true",
        },
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
        raise ProviderGatewayError("播放列表返回无效", code="invalid_response")
    total = raw.get("TotalRecordCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ProviderGatewayError(
            "播放列表未返回可靠总数，无法冻结修改范围", code="invalid_response"
        )
    rows = raw["Items"]
    if (
        len(rows) > limit
        or start + len(rows) > total
        or not rows
        and start < total
        or any(not isinstance(row, dict) or not row.get("Id") for row in rows)
    ):
        raise ProviderGatewayError(
            "播放列表分页不一致，请重新读取", code="provider_snapshot_changed"
        )
    return raw


def _playlist_snapshot(
    client: Any, user_id: str, playlist_id: str, *, allow_partial: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item = _item(client, user_id, playlist_id)
    if item.get("Type") != "Playlist":
        raise ProviderGatewayError("目标不是播放列表", code="confirmation_stale")
    raw = _playlist_page(
        client, user_id, playlist_id, start=0, limit=_PLAYLIST_LIMIT + 1
    )
    rows = raw["Items"]
    complete = (
        raw["TotalRecordCount"] <= _PLAYLIST_LIMIT
        and len(rows) == raw["TotalRecordCount"]
    )
    if not complete and not allow_partial:
        raise ProviderGatewayError(
            "播放列表超过 250 项或分页不完整；请缩小范围后修改",
            code="provider_scope_too_large",
        )
    members = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderGatewayError("播放列表条目格式无效", code="invalid_response")
        members.append(
            {
                "id": _id(row.get("Id")),
                "entry": _id(row.get("PlaylistItemId")),
                "name": str(row.get("Name") or "未命名条目"),
            }
        )
    if len({member["entry"] for member in members}) != len(members):
        raise ProviderGatewayError(
            "播放列表成员引用重复，无法安全修改", code="invalid_response"
        )
    return {
        "id": playlist_id,
        "user": user_id,
        "name": str(item["Name"]),
        "members": members,
        "complete": complete,
        "total": raw["TotalRecordCount"],
    }, rows


def _playlist_digest(snapshot: dict[str, Any]) -> str:
    return confirmation_context_fingerprint(
        snapshot, domain="media-playlist-membership"
    )


def _write(
    client: Any,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """仅接受本模块构造的固定端点；不向模型暴露 HTTP 或凭据。"""
    try:
        response = client._session.request(
            method,
            f"{client.url}{path}",
            headers=client._headers(),
            params=params,
            json=body,
            timeout=client.timeout,
        )
        response.raise_for_status()
        return response.json() if response.content else None
    except Exception as exc:
        raise ProviderGatewayError(
            "媒体服务器写请求未获可靠结果，请先核对状态，不要盲目重试",
            code="provider_write_outcome_unknown",
            external_write_possible=True,
        ) from exc


def read_media_user(
    client: Any, user_id: str, operation: str, arguments: dict[str, Any], *, source: str
) -> ProviderPayload:
    client = _BoundedMediaClient(client)
    if operation == "media.user.inspect":
        raw = _item(client, user_id, _id(arguments["item_ref"]))
        snapshot = _state_snapshot(raw, user_id)
        return ProviderPayload(
            summary=f"已读取《{snapshot['name']}》的用户状态",
            data={
                "item": {
                    "__object_id": _freeze(snapshot),
                    "__object_kind": "media_user_item",
                    "name": snapshot["name"],
                    "type": snapshot["type"],
                    "played": snapshot["state"]["Played"],
                    "favorite": snapshot["state"]["IsFavorite"],
                    "progress_ticks": snapshot["state"]["PlaybackPositionTicks"],
                }
            },
            source=source,
        )
    if operation == "media.playlists.list":
        start, limit = (
            int(arguments.get("start_index", 0)),
            int(arguments.get("limit", 20)),
        )
        raw = client._request(
            "/Items",
            params={
                "UserId": _id(user_id),
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "StartIndex": start,
                "Limit": limit,
                "EnableImages": "false",
                "EnableTotalRecordCount": "true",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
            },
        )
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("Items"), list)
            or len(raw["Items"]) > limit
            or any(
                not isinstance(row, dict) or not row.get("Id") for row in raw["Items"]
            )
        ):
            raise ProviderGatewayError("播放列表查询格式无效", code="invalid_response")
        total = raw.get("TotalRecordCount")
        total = (
            total
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0
            else None
        )
        items = [
            {
                "__object_id": _id(row.get("Id")),
                "__object_kind": "media_playlist",
                "name": str(row.get("Name") or "未命名播放列表"),
            }
            for row in raw["Items"]
            if isinstance(row, dict)
        ]
        if total is not None and (
            start + len(items) > total or not items and start < total
        ):
            raise ProviderGatewayError(
                "播放列表分页已变化，请重新读取", code="provider_snapshot_changed"
            )
        more = start + len(items) < total if total is not None else len(items) == limit
        return ProviderPayload(
            summary=f"已读取本页 {len(items)} 个播放列表",
            data={
                "items": items,
                "reported_total": total,
                "has_more": more,
                "next_start_index": start + len(items) if more else None,
                "complete": start == 0 and total is not None and not more,
            },
            source=source,
        )
    if operation == "media.playlist.inspect":
        snapshot, _rows = _playlist_snapshot(
            client, user_id, _id(arguments["playlist_ref"]), allow_partial=True
        )
        start, limit = (
            int(arguments.get("start_index", 0)),
            int(arguments.get("limit", 20)),
        )
        members = snapshot["members"]
        if snapshot["complete"]:
            page = members[start : start + limit]
        else:
            raw = _playlist_page(
                client, user_id, snapshot["id"], start=start, limit=limit
            )
            if raw["TotalRecordCount"] != snapshot["total"]:
                raise ProviderGatewayError(
                    "播放列表在读取期间已变化，请重试", code="provider_snapshot_changed"
                )
            page = [
                {
                    "id": _id(row.get("Id")),
                    "entry": _id(row.get("PlaylistItemId")),
                    "name": str(row.get("Name") or "未命名条目"),
                }
                for row in raw["Items"]
            ]
        items = [
            {
                "__object_id": _freeze(
                    {
                        "id": member["entry"],
                        "user": user_id,
                        "playlist": snapshot["id"],
                        "media": member["id"],
                    }
                ),
                "__object_kind": "media_playlist_entry",
                "name": member["name"],
            }
            for member in page
        ]
        frozen = {key: value for key, value in snapshot.items() if key != "members"}
        frozen["digest"] = _playlist_digest(snapshot)
        total = snapshot["total"]
        more = start + len(items) < total
        return ProviderPayload(
            summary=f"播放列表《{snapshot['name']}》共 {total} 项；"
            + (
                "已读取完整成员快照"
                if snapshot["complete"]
                else "仅分页读取，超过安全校验范围，不允许据此修改"
            ),
            data={
                "playlist": {
                    "__object_id": _freeze(frozen)
                    if snapshot["complete"]
                    else snapshot["id"],
                    "__object_kind": "media_playlist_snapshot"
                    if snapshot["complete"]
                    else "media_playlist",
                    "name": snapshot["name"],
                    "member_count": total,
                    "can_modify": snapshot["complete"],
                },
                "items": items,
                "has_more": more,
                "next_start_index": start + len(items) if more else None,
                "scope": "仅播放列表成员，不删除媒体文件",
            },
            source=source,
        )
    raise ProviderGatewayError("媒体用户读取操作未开放", code="operation_not_allowed")


def _validate_change(
    client: Any,
    user_id: str,
    operation: str,
    arguments: dict[str, Any],
    *,
    server_type: str,
) -> dict[str, Any]:
    if operation in _STATE_WRITES:
        expected = _thaw(arguments["item_ref"], user_id)
        current = _state_snapshot(_item(client, user_id, expected["id"]), user_id)
        field, desired, action = _STATE_WRITES[operation]
        expected_state = expected.get("state")
        if (
            not isinstance(expected_state, dict)
            or not isinstance(expected_state.get(field), bool)
            or not isinstance(current["state"].get(field), bool)
        ):
            raise ProviderGatewayError(
                "媒体服务器缺少用户状态，无法安全修改", code="invalid_response"
            )
        keys = (
            ("IsFavorite",)
            if field == "IsFavorite"
            else ("Played", "PlayCount", "PlaybackPositionTicks", "LastPlayedDate")
        )
        if any(
            expected_state.get(key) != current["state"].get(key) for key in keys
        ) or any(expected.get(key) != current.get(key) for key in ("name", "type")):
            raise ProviderGatewayError(
                "媒体用户状态已变化，请重新检查后确认", code="confirmation_stale"
            )
        if (
            current["type"] not in {"Movie", "Episode", "Audio", "MusicVideo", "Video"}
            and field == "Played"
        ):
            raise ProviderGatewayError(
                "已看状态仅允许单个可播放条目；不批量改变剧集或文件夹的子项",
                code="precondition_failed",
            )
        return {
            "item_id": expected["id"],
            "name": current["name"],
            "field": field,
            "desired": desired,
            "action": action,
            "before": current["state"][field],
            "previous_progress_ticks": current["state"].get("PlaybackPositionTicks"),
            "previous_play_count": current["state"].get("PlayCount"),
        }
    if operation == "media.playlist.create":
        if server_type != "jellyfin":
            raise ProviderGatewayError(
                "当前 Emby API 凭据不能可靠绑定新播放列表所有者；暂不开放创建，可修改已有播放列表",
                code="provider_version_unsupported",
            )
        name = str(arguments["name"]).strip()
        if not name:
            raise ProviderGatewayError("播放列表名称不能为空", code="invalid_arguments")
        ids = list(
            dict.fromkeys(_id(value) for value in arguments.get("item_refs", []))
        )
        names = []
        for item_id in ids:
            row = _item(client, user_id, item_id)
            if row.get("Type") not in {"Movie", "Episode", "MusicVideo", "Video"}:
                raise ProviderGatewayError(
                    "播放列表仅支持明确选中的单个视频；剧集需先选择具体单集",
                    code="precondition_failed",
                )
            names.append(str(row["Name"]))
        existing = client._request(
            "/Items",
            params={
                "UserId": user_id,
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "SearchTerm": name,
                "Limit": 100,
                "EnableImages": "false",
                "EnableTotalRecordCount": "true",
            },
        )
        if (
            not isinstance(existing, dict)
            or not isinstance(existing.get("Items"), list)
            or existing.get("TotalRecordCount") != len(existing["Items"])
        ):
            raise ProviderGatewayError(
                "无法完整核对同名播放列表，未创建", code="invalid_response"
            )
        if any(
            str(row.get("Name") or "").strip().casefold() == name.casefold()
            for row in existing["Items"]
            if isinstance(row, dict)
        ):
            raise ProviderGatewayError(
                "已有同名播放列表，请查询后向现有列表添加", code="provider_conflict"
            )
        return {"name": name, "ids": ids, "names": names, "action": "创建私有播放列表"}
    expected = _thaw(arguments["playlist_ref"], user_id)
    snapshot, _rows = _playlist_snapshot(client, user_id, expected["id"])
    if expected.get("digest") != _playlist_digest(snapshot):
        raise ProviderGatewayError(
            "播放列表成员或名称已变化，请重新读取后确认", code="confirmation_stale"
        )
    result = {
        "playlist_id": expected["id"],
        "name": snapshot["name"],
        "members": snapshot["members"],
    }
    if operation == "media.playlist.add_items":
        current_ids = {member["id"] for member in snapshot["members"]}
        ids = list(dict.fromkeys(_id(value) for value in arguments["item_refs"]))
        ids = [value for value in ids if value not in current_ids]
        names = []
        for value in ids:
            row = _item(client, user_id, value)
            if row.get("Type") not in {"Movie", "Episode", "MusicVideo", "Video"}:
                raise ProviderGatewayError(
                    "只能加入单个视频，不能隐式展开整个剧集或目录",
                    code="precondition_failed",
                )
            names.append(str(row["Name"]))
        if len(snapshot["members"]) + len(ids) > _PLAYLIST_LIMIT:
            raise ProviderGatewayError(
                "修改后的列表将超过有界校验范围 250 项", code="provider_scope_too_large"
            )
        result.update(ids=ids, names=names, action="加入播放列表")
    elif operation == "media.playlist.remove_items":
        entries = {
            _thaw(value, user_id)["id"]: _thaw(value, user_id)
            for value in arguments["entry_refs"]
        }
        members = {member["entry"]: member for member in snapshot["members"]}
        if any(
            entry.get("playlist") != expected["id"]
            or key not in members
            or entry.get("media") != members[key]["id"]
            for key, entry in entries.items()
        ):
            raise ProviderGatewayError(
                "选中成员不属于此播放列表或已变化", code="confirmation_stale"
            )
        result.update(
            entries=list(entries),
            names=[members[key]["name"] for key in entries],
            action="移出播放列表（保留媒体文件）",
        )
    else:
        raise ProviderGatewayError("媒体用户写操作未开放", code="operation_not_allowed")
    return result


def preview_media_user(
    client: Any,
    user_id: str,
    operation: str,
    arguments: dict[str, Any],
    *,
    server_type: str,
    source: str,
) -> ProviderPayload:
    client = _BoundedMediaClient(client)
    checked = _validate_change(
        client, user_id, operation, arguments, server_type=server_type
    )
    data = {
        "name": checked["name"],
        "action": checked["action"],
        "delete_media_files": False,
    }
    if operation in _STATE_WRITES:
        data.update(
            before=checked["before"],
            after=checked["desired"],
            state=checked["field"],
            scope="当前用户的单个媒体条目",
        )
        if checked["field"] == "Played":
            data.update(
                previous_progress_ticks=checked["previous_progress_ticks"],
                previous_play_count=checked["previous_play_count"],
                note="修改已看状态可能同时调整播放计数或进度，不会自动恢复历史播放记录。",
            )
    else:
        data.update(
            affected=len(checked.get("ids", checked.get("entries", []))),
            items=checked.get("names", []),
            scope="播放列表成员关系",
            may_be_shared=operation != "media.playlist.create",
        )
    return ProviderPayload(
        summary=f"将{checked['action']}：《{checked['name']}》",
        data=data,
        source=source,
    )


def execute_media_user(
    client: Any,
    user_id: str,
    operation: str,
    arguments: dict[str, Any],
    *,
    server_type: str,
    source: str,
) -> ProviderPayload:
    client = _BoundedMediaClient(client)
    checked = _validate_change(
        client, user_id, operation, arguments, server_type=server_type
    )
    changed = False
    try:
        if operation in _STATE_WRITES:
            field, desired = checked["field"], checked["desired"]
            if checked["before"] != desired:
                endpoint = "PlayedItems" if field == "Played" else "FavoriteItems"
                path = (
                    f"/User{endpoint}/{checked['item_id']}"
                    if server_type == "jellyfin"
                    else f"/Users/{user_id}/{endpoint}/{checked['item_id']}"
                )
                changed = True
                _write(
                    client,
                    "POST" if desired else "DELETE",
                    path,
                    params={"UserId": user_id} if server_type == "jellyfin" else None,
                )
            current = _item(client, user_id, checked["item_id"])
            state = current.get("UserData")
            if not isinstance(state, dict) or state.get(field) is not desired:
                raise ValueError("user state verification failed")
            data = {
                "name": checked["name"],
                "state": field,
                "value": desired,
                "affected": int(changed),
                "verification": "已回读验证",
            }
        else:
            playlist_id = checked.get("playlist_id", "")
            if operation == "media.playlist.create":
                changed = True
                response = _write(
                    client,
                    "POST",
                    "/Playlists",
                    body={
                        "Name": checked["name"],
                        "Ids": checked["ids"],
                        "UserId": user_id,
                        "MediaType": "Video",
                        "IsPublic": False,
                    },
                )
                playlist_id = _id(
                    response.get("Id") if isinstance(response, dict) else ""
                )
                privacy = client._request(f"/Playlists/{playlist_id}")
                if (
                    not isinstance(privacy, dict)
                    or privacy.get("OpenAccess") is not False
                ):
                    raise ValueError("new playlist privacy verification failed")
            elif operation == "media.playlist.add_items" and checked["ids"]:
                changed = True
                _write(
                    client,
                    "POST",
                    f"/Playlists/{playlist_id}/Items",
                    params={"UserId": user_id, "Ids": ",".join(checked["ids"])},
                )
            elif operation == "media.playlist.remove_items" and checked["entries"]:
                changed = True
                _write(
                    client,
                    "DELETE",
                    f"/Playlists/{playlist_id}/Items",
                    params={"EntryIds": ",".join(checked["entries"])},
                )
            after, _rows = _playlist_snapshot(client, user_id, playlist_id)
            previous = list(checked.get("members", []))
            if operation == "media.playlist.create":
                expected_ids = Counter(checked["ids"])
            elif operation == "media.playlist.add_items":
                expected_ids = Counter(
                    [member["id"] for member in previous] + checked["ids"]
                )
            else:
                expected_ids = Counter(
                    member["id"]
                    for member in previous
                    if member["entry"] not in checked["entries"]
                )
            if (
                Counter(member["id"] for member in after["members"]) != expected_ids
                or after["name"] != checked["name"]
            ):
                raise ValueError("playlist verification failed")
            data = {
                "name": checked["name"],
                "action": checked["action"],
                "member_count": len(after["members"]),
                "affected": len(checked.get("ids", checked.get("entries", [])))
                if changed
                else 0,
                "verification": "已回读验证",
                "delete_media_files": False,
            }
        return ProviderPayload(
            summary=f"已{checked['action']}：《{checked['name']}》"
            if changed
            else f"《{checked['name']}》已是目标状态，无需修改",
            data={**data, "accepted": True},
            source=source,
        )
    except ProviderGatewayError as exc:
        if changed and not exc.external_write_possible:
            raise ProviderGatewayError(
                "媒体写请求已发出，但写后核对未完成，请先查看真实状态",
                code="provider_write_outcome_unknown",
                external_write_possible=True,
            ) from exc
        raise
    except Exception as exc:
        raise ProviderGatewayError(
            "媒体操作写后核验未通过，请先核对真实状态",
            code="provider_write_outcome_unknown"
            if changed
            else "provider_unavailable",
            external_write_possible=changed,
        ) from exc
