"""媒体用户状态与播放列表；使用真实 ID，预检快照由调用方持久冻结。"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from app.modules.media_library import (
    MediaLibraryError,
    _client,
    _id,
    _item,
    playback_scope,
    playback_user,
)

_PLAYLIST_LIMIT = 250
_STATES = {"set_played": "Played", "set_favorite": "IsFavorite"}
_VIDEO_TYPES = {"Movie", "Episode", "MusicVideo", "Video"}


class _BoundedMediaClient:
    """一次读取、预检或执行共享20秒预算；取消在每次请求边界核对。"""
    def __init__(self, client, check_cancelled=None):
        self.client = client
        self.deadline = time.monotonic() + 20
        self.check_cancelled = check_cancelled
        self.execution_started = False

    def check(self):
        if self.check_cancelled:
            self.check_cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MediaLibraryError("媒体检查预算已用尽，请缩小条目范围", code="upstream_timeout")
        return min(float(self.client.timeout), remaining)

    def _request(self, path, params=None):
        result = self.client._request(path, params=params, timeout=self.check())
        self.check()
        return result

    def write(self, method, path, *, params=None, body=None):
        timeout = self.check()
        self.execution_started = True
        response = self.client._session.request(method, self.client.url + path,
            headers=self.client._headers(), params=params, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json() if response.content else None


def _ids(values, name):
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise MediaLibraryError(f"{name} 必须是1–20项的ID数组", code="invalid_arguments")
    result = [_id(value) for value in values]
    if len(set(result)) != len(result):
        raise MediaLibraryError(f"{name} 不得包含重复ID", code="invalid_arguments")
    return result


def _page(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 250:
        raise MediaLibraryError("offset必须为0–1000000的整数；limit必须为1–250的整数", code="invalid_arguments")


def _videos(client, user_id, ids):
    rows = []
    for item_id in ids:
        row = _item(client, user_id, item_id)
        if row.get("Type") not in _VIDEO_TYPES:
            raise MediaLibraryError("播放列表只支持单个视频，不能隐式展开整个剧集或目录", code="precondition_failed")
        rows.append(row)
    return rows


def _state_snapshot(row, user_id) -> dict[str, Any]:
    data = row.get("UserData")
    data = data if isinstance(data, dict) else {}
    return {"id": _id(row.get("Id")), "user": user_id, "name": str(row["Name"]),
            "type": str(row.get("Type") or ""), "state": {key: data.get(key) for key in
                ("Played", "PlayCount", "PlaybackPositionTicks", "LastPlayedDate", "IsFavorite")}}


def user_state(item_id: str, server="auto", *, check_cancelled=None) -> dict:
    item_id = _id(item_id)
    with _client(server) as (profile, raw):
        user_id, selection = playback_user(profile, raw)
        snapshot = _state_snapshot(_item(_BoundedMediaClient(raw, check_cancelled), user_id, item_id), user_id)
        return {**playback_scope(profile, user_id, selection), "status": "success", "item": {
            **snapshot, "played": snapshot["state"]["Played"], "favorite": snapshot["state"]["IsFavorite"],
            "progress_ticks": snapshot["state"]["PlaybackPositionTicks"]}}


def _playlist_page(client, user_id, playlist_id, *, start, limit):
    raw = client._request(f"/Playlists/{_id(playlist_id)}/Items", params={
        "UserId": _id(user_id), "StartIndex": start, "Limit": limit,
        "EnableImages": "false", "EnableTotalRecordCount": "true"})
    if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
        raise MediaLibraryError("播放列表返回无效", code="invalid_response")
    total, rows = raw.get("TotalRecordCount"), raw["Items"]
    if type(total) is not int or total < 0:
        raise MediaLibraryError("播放列表未返回可靠总数，无法冻结修改范围", code="invalid_response")
    if (len(rows) > limit or rows and start + len(rows) > total or not rows and start < total
            or any(not isinstance(row, dict) or not row.get("Id") for row in rows)):
        raise MediaLibraryError("播放列表分页不一致，请重新读取", code="provider_snapshot_changed")
    return raw


def _members(rows):
    members = [{"item_id": _id(row.get("Id")), "entry_id": _id(row.get("PlaylistItemId")),
                "name": str(row.get("Name") or "未命名条目")} for row in rows]
    if len({row["entry_id"] for row in members}) != len(members):
        raise MediaLibraryError("播放列表成员ID重复，无法安全修改", code="invalid_response")
    return members


def _playlist_snapshot(client, user_id, playlist_id, *, start=0, limit=_PLAYLIST_LIMIT + 1, allow_partial=False) -> dict[str, Any]:
    item = _item(client, user_id, playlist_id)
    if item.get("Type") != "Playlist":
        raise MediaLibraryError("目标不是播放列表", code="confirmation_stale")
    raw = _playlist_page(client, user_id, playlist_id, start=start, limit=limit)
    complete = start == 0 and raw["TotalRecordCount"] <= _PLAYLIST_LIMIT and len(raw["Items"]) == raw["TotalRecordCount"]
    if not complete and not allow_partial:
        raise MediaLibraryError("播放列表超过250项或分页不完整；请缩小范围后修改", code="provider_scope_too_large")
    return {"id": playlist_id, "user": user_id, "name": str(item["Name"]),
            "members": _members(raw["Items"]), "complete": complete, "total": raw["TotalRecordCount"],
            "is_public": item.get("IsPublic"), "owner_user_id": item.get("OwnerUserId")}


def playlists(server="auto", *, offset=0, limit=20, check_cancelled=None) -> dict:
    _page(offset, limit)
    with _client(server) as (profile, raw):
        user_id, selection = playback_user(profile, raw)
        result = _BoundedMediaClient(raw, check_cancelled)._request("/Items", params={
            "UserId": _id(user_id), "IncludeItemTypes": "Playlist", "Recursive": "true",
            "StartIndex": offset, "Limit": limit, "EnableImages": "false",
            "EnableTotalRecordCount": "true", "SortBy": "SortName", "SortOrder": "Ascending"})
        if (not isinstance(result, dict) or not isinstance(result.get("Items"), list)
                or len(result["Items"]) > limit or any(not isinstance(row, dict) or not row.get("Id") for row in result["Items"])):
            raise MediaLibraryError("播放列表查询格式无效", code="invalid_response")
        total = result.get("TotalRecordCount")
        total = total if type(total) is int and total >= 0 else None
        items = [{"id": _id(row.get("Id")), "name": str(row.get("Name") or "未命名播放列表"),
                  "is_public": row.get("IsPublic"), "owner_user_id": row.get("OwnerUserId")} for row in result["Items"]]
        if total is not None and (offset + len(items) > total or not items and offset < total):
            raise MediaLibraryError("播放列表分页已变化，请重新读取", code="provider_snapshot_changed")
        more = offset + len(items) < total if total is not None else len(items) == limit
        return {**playback_scope(profile, user_id, selection), "status": "success" if items else "empty",
                "items": items, "count": len(items), "returned": len(items), "total": total,
                "has_more": more, "offset": offset, "limit": limit,
                "next_offset": offset + len(items) if more else None,
                "complete": offset == 0 and total is not None and not more}


def playlist(playlist_id: str, server="auto", *, offset=0, limit=250, check_cancelled=None) -> dict:
    playlist_id = _id(playlist_id)
    _page(offset, limit)
    with _client(server) as (profile, raw):
        user_id, selection = playback_user(profile, raw)
        client = _BoundedMediaClient(raw, check_cancelled)
        snapshot = _playlist_snapshot(client, user_id, playlist_id, start=offset, limit=limit, allow_partial=True)
        members = snapshot["members"]
        more = offset + len(members) < snapshot["total"]
        return {**playback_scope(profile, user_id, selection), "status": "success" if members else "empty",
                "playlist": {key: value for key, value in snapshot.items() if key not in {"members", "user"}},
                "can_modify": snapshot["complete"], "items": members, "returned": len(members),
                "total": snapshot["total"], "has_more": more, "offset": offset, "limit": limit,
                "next_offset": offset + len(members) if more else None, "delete_media_files": False}


def _check_change(client, user_id, operation, arguments, server_type) -> dict[str, Any]:
    if operation in _STATES:
        snapshot = _state_snapshot(_item(client, user_id, arguments["item_id"]), user_id)
        field, desired = _STATES[operation], arguments["value"]
        if not isinstance(snapshot["state"][field], bool):
            raise MediaLibraryError("媒体服务器缺少用户状态，无法安全修改", code="invalid_response")
        if field == "Played" and snapshot["type"] not in _VIDEO_TYPES | {"Audio"}:
            raise MediaLibraryError("已看状态仅允许单个可播放条目；不批量改变剧集或文件夹的子项", code="precondition_failed")
        keys = ("IsFavorite",) if field == "IsFavorite" else ("Played", "PlayCount", "PlaybackPositionTicks", "LastPlayedDate")
        baseline = {**snapshot, "state": {key: snapshot["state"][key] for key in keys}}
        return {"baseline": baseline, "name": snapshot["name"], "field": field, "desired": desired,
                "before": snapshot["state"][field], "action": "标记已看" if field == "Played" and desired else
                    "标记未看" if field == "Played" else "收藏" if desired else "取消收藏"}
    checked: dict[str, Any]
    if operation == "create_playlist":
        if server_type != "jellyfin":
            raise MediaLibraryError("当前Emby API凭据不能可靠绑定新播放列表所有者；暂不开放创建，可修改已有列表", code="provider_version_unsupported")
        items = _videos(client, user_id, arguments["item_ids"])
        existing = client._request("/Items", params={"UserId": user_id, "IncludeItemTypes": "Playlist",
            "Recursive": "true", "SearchTerm": arguments["name"], "Limit": 100,
            "EnableImages": "false", "EnableTotalRecordCount": "true"})
        if (not isinstance(existing, dict) or not isinstance(existing.get("Items"), list)
                or existing.get("TotalRecordCount") != len(existing["Items"])):
            raise MediaLibraryError("无法完整核对同名播放列表，未创建", code="invalid_response")
        if any(str(row.get("Name") or "").strip().casefold() == arguments["name"].casefold()
               for row in existing["Items"] if isinstance(row, dict)):
            raise MediaLibraryError("已有同名播放列表，请查询后向现有列表添加", code="provider_conflict")
        baseline = {"items": [{"id": row["Id"], "name": row["Name"], "type": row.get("Type")} for row in items]}
        checked = {"baseline": baseline, "name": arguments["name"], "ids": arguments["item_ids"],
                   "action": "创建私有播放列表"}
    else:
        snapshot = _playlist_snapshot(client, user_id, arguments["playlist_id"])
        checked = {"baseline": snapshot, "name": snapshot["name"], "members": snapshot["members"]}
        if operation == "add_to_playlist":
            current_ids = {row["item_id"] for row in snapshot["members"]}
            ids = [item_id for item_id in arguments["item_ids"] if item_id not in current_ids]
            items = _videos(client, user_id, ids)
            if len(snapshot["members"]) + len(ids) > _PLAYLIST_LIMIT:
                raise MediaLibraryError("修改后的列表将超过有界校验范围250项", code="provider_scope_too_large")
            checked.update(ids=ids, action="加入播放列表")
            checked["selected"] = [{"id": row["Id"], "name": row["Name"], "type": row.get("Type")} for row in items]
        else:
            members = {row["entry_id"]: row for row in snapshot["members"]}
            if any(entry not in members for entry in arguments["entry_ids"]):
                raise MediaLibraryError("选中成员不属于此播放列表或已变化", code="confirmation_stale")
            checked.update(entries=arguments["entry_ids"], names=[members[key]["name"] for key in arguments["entry_ids"]],
                           action="移出播放列表（保留媒体文件）")
            return checked
    checked["names"] = [str(row["Name"]) for row in items]
    return checked


def prepare_change(operation: str, *, server="auto", item_id="", value=None, name="", item_ids=None,
                   playlist_id="", entry_ids=None, check_cancelled=None) -> dict:
    arguments: dict[str, Any]
    if operation in _STATES:
        arguments = {"item_id": _id(item_id), "value": value}
        if type(value) is not bool:
            raise MediaLibraryError("状态值必须为布尔值", code="invalid_arguments")
    elif operation in {"create_playlist", "add_to_playlist", "remove_from_playlist"}:
        arguments = {"playlist_id": _id(playlist_id)} if operation != "create_playlist" else {}
        if operation == "remove_from_playlist":
            arguments["entry_ids"] = _ids(entry_ids, "entry_ids")
        else:
            arguments["item_ids"] = _ids(item_ids, "item_ids")
        if operation == "create_playlist":
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
                raise MediaLibraryError("name必须为1–120个可见字符", code="invalid_arguments")
            arguments["name"] = name.strip()
    else:
        raise MediaLibraryError("媒体用户操作未开放", code="invalid_arguments")
    with _client(server) as (profile, raw):
        if operation == "create_playlist" and not profile.user_id:
            raise MediaLibraryError("创建播放列表前须明确配置媒体服务器用户", code="provider_user_required")
        user_id, selection = playback_user(profile, raw)
        checked = _check_change(_BoundedMediaClient(raw, check_cancelled), user_id, operation, arguments, profile.server_type)
        scope = playback_scope(profile, user_id, selection)
        data = {**scope, "name": checked["name"], "action": checked["action"], "delete_media_files": False}
        if operation in _STATES:
            data.update(before=checked["before"], after=checked["desired"], state=checked["field"], item_id=arguments["item_id"])
            if operation == "set_played":
                data.update(previous_progress_ticks=checked["baseline"]["state"]["PlaybackPositionTicks"],
                            previous_play_count=checked["baseline"]["state"]["PlayCount"],
                            note="修改已看状态可能调整播放计数或进度，不自动恢复历史播放记录。")
        else:
            data.update(affected=len(checked.get("ids", checked.get("entries", []))), items=checked.get("names", []),
                        may_be_shared=operation != "create_playlist")
        snapshot = {"operation": operation, "server": profile.server_type, "profile": profile.source,
                    "profile_revision": profile.fingerprint(), "user_id": user_id, "arguments": arguments, "checked": checked}
        return {"snapshot": snapshot, "server": profile.server_type,
                "preview": {"ok": True, "status": "confirmation_required",
                            "summary": f"将{checked['action']}：《{checked['name']}》", "data": data}}


def execute_change(snapshot: dict, *, check_cancelled=None) -> dict:
    operation, arguments = snapshot["operation"], snapshot["arguments"]
    with _client(snapshot["server"]) as (profile, raw):
        if profile.source != snapshot["profile"] or profile.fingerprint() != snapshot["profile_revision"]:
            raise MediaLibraryError("媒体服务器配置已变化，请重新预检", code="confirmation_stale")
        user_id, selection = playback_user(profile, raw)
        if user_id != snapshot["user_id"]:
            raise MediaLibraryError("媒体用户已变化，请重新预检", code="confirmation_stale")
        client = _BoundedMediaClient(raw, check_cancelled)
        checked = _check_change(client, user_id, operation, arguments, profile.server_type)
        if checked != snapshot["checked"]:
            raise MediaLibraryError("媒体用户状态、条目或播放列表成员或名称已变化，请重新预检", code="confirmation_stale")
        scope = playback_scope(profile, user_id, selection)
        playlist_id = arguments.get("playlist_id", "")
        try:
            if operation in _STATES:
                field, desired = checked["field"], checked["desired"]
                if checked["before"] != desired:
                    endpoint = "PlayedItems" if field == "Played" else "FavoriteItems"
                    path = f"/User{endpoint}/{arguments['item_id']}" if profile.server_type == "jellyfin" else f"/Users/{user_id}/{endpoint}/{arguments['item_id']}"
                    client.write("POST" if desired else "DELETE", path,
                                 params={"UserId": user_id} if profile.server_type == "jellyfin" else None)
                current = _state_snapshot(_item(client, user_id, arguments["item_id"]), user_id)
                if current["state"][field] is not desired:
                    raise ValueError("user state verification failed")
                data = {**scope, "item": current, "item_id": arguments["item_id"], "name": checked["name"],
                        "state": field, "value": desired, "affected": int(client.execution_started)}
            else:
                if operation == "create_playlist":
                    response = client.write("POST", "/Playlists", body={"Name": checked["name"], "Ids": checked["ids"],
                        "UserId": user_id, "MediaType": "Video", "IsPublic": False})
                    playlist_id = _id(response.get("Id") if isinstance(response, dict) else None)
                    privacy = client._request(f"/Playlists/{playlist_id}")
                    if (not isinstance(privacy, dict) or privacy.get("OpenAccess") is not False
                            or privacy.get("OwnerUserId", user_id) != user_id):
                        raise ValueError("new playlist privacy verification failed")
                elif operation == "add_to_playlist" and checked["ids"]:
                    client.write("POST", f"/Playlists/{playlist_id}/Items", params={"UserId": user_id, "Ids": ",".join(checked["ids"])})
                elif operation == "remove_from_playlist" and checked["entries"]:
                    client.write("DELETE", f"/Playlists/{playlist_id}/Items", params={"EntryIds": ",".join(checked["entries"])})
                after = _playlist_snapshot(client, user_id, playlist_id)
                previous = checked.get("members", [])
                expected_ids = (checked["ids"] if operation == "create_playlist" else
                    [row["item_id"] for row in previous] + checked["ids"] if operation == "add_to_playlist" else
                    [row["item_id"] for row in previous if row["entry_id"] not in checked["entries"]])
                if Counter(row["item_id"] for row in after["members"]) != Counter(expected_ids) or after["name"] != checked["name"]:
                    raise ValueError("playlist verification failed")
                if operation == "remove_from_playlist" and Counter(
                    (row["item_id"], row["entry_id"]) for row in after["members"]
                ) != Counter((row["item_id"], row["entry_id"]) for row in previous if row["entry_id"] not in checked["entries"]):
                    raise ValueError("playlist entry verification failed")
                data = {**scope, "playlist_id": playlist_id, "name": checked["name"], "action": checked["action"],
                        "member_count": len(after["members"]), "items": after["members"],
                        "affected": len(checked.get("ids", checked.get("entries", []))) if client.execution_started else 0,
                        "may_be_shared": operation != "create_playlist", "delete_media_files": False}
            return {"ok": True, "status": "completed", "summary": f"已{checked['action']}：《{checked['name']}》" if client.execution_started
                    else f"《{checked['name']}》已是目标状态，无需修改",
                    "data": {**data, "accepted": True, "verification": "已回读验证"}}
        except (Exception, asyncio.CancelledError):
            if not client.execution_started:
                raise
            return {"ok": False, "status": "outcome_unknown", "summary": "媒体写请求已发出，结果待核对",
                    "data": {**scope, "item_id": arguments.get("item_id"), "playlist_id": playlist_id or None,
                             "execution_started": True, "delete_media_files": False},
                    "error": "写后核验未完成，请先读取真实状态，勿重复提交。"}
