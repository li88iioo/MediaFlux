"""光鸭自有分享的安全查询、创建与撤销动作。"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.errors import AgentToolError
from app.agent.guangya_workspace_actions import latest_guangya_observation_ref
from app.agent.models import Evidence, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text, sanitize_untrusted_filename
from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected
from app.modules.guangya_workspace import (
    GuangYaWorkspaceError,
    GuangYaWorkspaceStale,
    load_directory_observation,
    observation_entry_map,
    valid_object_handle,
    valid_observation_ref,
)

logger = logging.getLogger(__name__)
_MAX_SHARES = 2_000
_MAX_SHARE_OBJECTS = 100
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,16}$")
_SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _bounded_int(value: object, *, maximum: int = 1 << 63) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(float(value or 0)), maximum))
    except (TypeError, ValueError, OverflowError):
        return 0


def _open_client() -> GuangYaClient:
    client = GuangYaClient()
    if not client.logged_in:
        client.close()
        raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
    return client


def _public_error(exc: Exception, *, fallback: str) -> AgentToolError:
    if isinstance(exc, AgentToolError):
        return exc
    if isinstance(exc, (GuangYaWorkspaceStale,)):
        return AgentToolError(str(exc), code="confirmation_stale")
    if isinstance(exc, (GuangYaWorkspaceError, ValueError)):
        return AgentToolError(str(exc), code="precondition_failed")
    if isinstance(exc, GuangYaWriteRejected):
        return AgentToolError(
            exc.public_message or fallback,
            code="provider_rejected",
        )
    logger.warning("Agent 光鸭分享操作失败 type=%s", type(exc).__name__)
    return AgentToolError(fallback, code="unavailable")


def _first(raw: dict[str, Any], *keys: str) -> object:
    payloads = [raw]
    data = raw.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
    return ""


def _share_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    share_id = str(_first(raw, "shareId", "shareID", "share_id", "id") or "").strip()
    return {
        "share_id": share_id,
        "title": str(_first(raw, "title", "shareName", "name") or "").strip(),
        "status": str(_first(raw, "status", "shareStatus", "state") or "").strip(),
        "created_at": str(
            _first(raw, "createTime", "createdAt", "created_at", "shareTime") or ""
        ).strip(),
        "expires_at": str(
            _first(raw, "expireTime", "expiresAt", "expiredAt", "invalidTime") or ""
        ).strip(),
        "file_count": _bounded_int(
            _first(raw, "fileCount", "resCount", "count", "resourceCount"),
            maximum=1_000_000,
        ),
        "updated_at": str(
            _first(raw, "updateTime", "updatedAt", "updated_at") or ""
        ).strip(),
    }


def _public_share(item: dict[str, Any], *, index: int) -> dict[str, Any]:
    title = sanitize_public_text(item.get("title"), limit=160)
    return {
        "index": index,
        "title": title or "未命名分享",
        "status": sanitize_public_text(item.get("status"), limit=40) or "unknown",
        "created_at": sanitize_public_text(item.get("created_at"), limit=60),
        "expires_at": sanitize_public_text(item.get("expires_at"), limit=60),
        "file_count": _bounded_int(item.get("file_count"), maximum=1_000_000),
    }


def _load_share_snapshots(client: GuangYaClient) -> list[dict[str, Any]]:
    snapshots = [
        _share_snapshot(item)
        for item in client.list_user_shares(max_items=_MAX_SHARES)
        if isinstance(item, dict)
    ]
    return [item for item in snapshots if item["share_id"]]


def guangya_share_list_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"page", "page_size"}:
        raise AgentToolError("光鸭分享列表参数无效")
    page = arguments.get("page", 1)
    page_size = arguments.get("page_size", 50)
    if type(page) is not int or not 1 <= page <= 200:
        raise AgentToolError("page 必须在 1 到 200 之间")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise AgentToolError("page_size 必须在 1 到 100 之间")
    return {"page": page, "page_size": page_size}


def list_guangya_user_shares(
    arguments: dict[str, Any], _context: ToolContext
) -> ToolResult:
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        snapshots = _load_share_snapshots(client)
        generation = int(client.credential_generation)
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭分享列表当前不可用") from exc
    finally:
        if client is not None:
            client.close()

    page = int(arguments["page"])
    page_size = int(arguments["page_size"])
    start = (page - 1) * page_size
    selected = snapshots[start : start + page_size]
    public_items = [
        _public_share(item, index=start + index)
        for index, item in enumerate(selected, 1)
    ]
    return ToolResult(
        True,
        "found" if selected else "empty",
        f"当前账号共有 {len(snapshots)} 条光鸭分享，本页显示 {len(selected)} 条",
        data={
            "page": page,
            "page_size": page_size,
            "total": len(snapshots),
            "has_more": start + len(selected) < len(snapshots),
            "items": public_items,
        },
        model_data={
            "page": page,
            "total": len(snapshots),
            "has_more": start + len(selected) < len(snapshots),
            "items": public_items,
        },
        references=[
            ToolReference(
                kind="guangya_shares",
                value={
                    "credential_generation": generation,
                    "page": page,
                    "page_size": page_size,
                    "items": selected,
                },
                ttl_seconds=15 * 60,
            )
        ] if selected else [],
        evidence=[
            Evidence(
                "guangya_share_list",
                "仅返回分享标题、状态和时间摘要；分享 ID、访问码及底层响应保存在会话私有引用中。",
                _now(),
            )
        ],
        suggestions=["撤销分享需使用本次返回的分享引用和 index，并经过人工确认。"],
    )


def guangya_share_create_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("创建光鸭分享参数必须是对象")
    allowed = {
        "observation_ref",
        "object_refs",
        "title",
        "expires_days",
        "access_code",
        "auto_access_code",
        "max_restore_count",
        "allow_download",
    }
    if set(arguments) - allowed:
        raise AgentToolError("创建光鸭分享包含不支持的参数")
    observation_ref = str(arguments.get("observation_ref") or "").strip().upper()
    if observation_ref and not valid_observation_ref(observation_ref):
        raise AgentToolError("observation_ref 格式无效")
    raw_refs = arguments.get("object_refs")
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= _MAX_SHARE_OBJECTS:
        raise AgentToolError(f"object_refs 必须包含 1 到 {_MAX_SHARE_OBJECTS} 个对象")
    object_refs: list[str] = []
    for value in raw_refs:
        ref = str(value or "").strip().upper()
        if not valid_object_handle(ref):
            raise AgentToolError("object_refs 包含无效对象引用")
        if ref not in object_refs:
            object_refs.append(ref)
    title = str(arguments.get("title") or "").strip()
    if len(title) > 180:
        raise AgentToolError("分享标题不能超过 180 个字符")
    expires_days = arguments.get("expires_days", 0)
    if type(expires_days) is not int or not 0 <= expires_days <= 3650:
        raise AgentToolError("expires_days 必须在 0 到 3650 之间")
    access_code = str(arguments.get("access_code") or "").strip()
    if access_code and not _CODE_RE.fullmatch(access_code):
        raise AgentToolError("access_code 必须是 4 到 16 位字母或数字")
    auto_access_code = arguments.get("auto_access_code", not bool(access_code))
    if type(auto_access_code) is not bool:
        raise AgentToolError("auto_access_code 必须是布尔值")
    if access_code:
        auto_access_code = False
    max_restore_count = arguments.get("max_restore_count", 0)
    if type(max_restore_count) is not int or not 0 <= max_restore_count <= 1_000_000:
        raise AgentToolError("max_restore_count 必须在 0 到 1000000 之间")
    allow_download = arguments.get("allow_download", True)
    if type(allow_download) is not bool:
        raise AgentToolError("allow_download 必须是布尔值")
    return {
        "observation_ref": observation_ref,
        "object_refs": object_refs,
        "title": title,
        "expires_days": expires_days,
        "access_code": access_code,
        "auto_access_code": auto_access_code,
        "max_restore_count": max_restore_count,
        "allow_download": allow_download,
    }


def _workspace_selection(
    arguments: dict[str, Any], *, owner: str, verify_live: bool
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    observation_ref = str(arguments.get("observation_ref") or "").strip().upper()
    if not observation_ref:
        observation_ref = latest_guangya_observation_ref(owner)
    if not observation_ref:
        raise AgentToolError("请先读取要分享的光鸭目录", code="precondition_failed")
    observation = load_directory_observation(observation_ref, owner=owner)
    entries = observation_entry_map(observation)
    selected: list[dict[str, Any]] = []
    for ref in arguments.get("object_refs") or ():
        item = entries.get(str(ref).strip().upper())
        if item is None:
            raise AgentToolError(
                "分享对象不属于当前目录快照，请重新查询",
                code="confirmation_stale",
            )
        selected.append(item)
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        expected_generation = _bounded_int(observation.get("credential_generation"))
        if int(client.credential_generation) != expected_generation:
            raise AgentToolError(
                "光鸭登录凭据已变化，请重新读取目录",
                code="confirmation_stale",
            )
        if verify_live:
            for expected in selected:
                current = client.file_info(str(expected.get("file_id") or ""))
                if not _file_matches(current, expected):
                    raise AgentToolError(
                        "分享对象已变化，请重新读取目录",
                        code="confirmation_stale",
                    )
    finally:
        if client is not None:
            client.close()
    return observation, selected, {
        "observation_ref": observation_ref,
        "credential_generation": _bounded_int(observation.get("credential_generation")),
    }


def _file_matches(current: GuangYaFile | None, expected: dict[str, Any]) -> bool:
    if current is None:
        return False
    if (
        str(current.file_id) != str(expected.get("file_id") or "")
        or str(current.name) != str(expected.get("name") or "")
        or bool(current.is_dir) != bool(expected.get("is_dir"))
        or _bounded_int(current.size) != _bounded_int(expected.get("size"))
    ):
        return False
    etag = str(expected.get("etag") or "")
    return not etag or str(current.etag or "") in {"", etag}


def _create_snapshot(
    arguments: dict[str, Any], *, owner: str, verify_live: bool
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    _observation, selected, identity = _workspace_selection(
        arguments, owner=owner, verify_live=verify_live
    )
    title = str(arguments.get("title") or "").strip()
    if not title:
        title = str(selected[0].get("name") or "MediaFlux 分享")
        if len(selected) > 1:
            title = f"{title} 等 {len(selected)} 项"
    title = sanitize_untrusted_filename(title, limit=180) or "MediaFlux 分享"
    safe = {
        "count": len(selected),
        "title": title,
        "expires_days": int(arguments.get("expires_days") or 0),
        "access_policy": (
            "custom_code"
            if arguments.get("access_code")
            else "auto_code"
            if arguments.get("auto_access_code")
            else "no_code"
        ),
        "max_restore_count": int(arguments.get("max_restore_count") or 0),
        "allow_download": bool(arguments.get("allow_download")),
        "samples": [
            sanitize_untrusted_filename(item.get("name")) or "未命名对象"
            for item in selected[:6]
        ],
    }
    fingerprint = confirmation_context_fingerprint(
        {
            **identity,
            "objects": [
                {
                    key: item.get(key)
                    for key in (
                        "file_id",
                        "parent_id",
                        "name",
                        "is_dir",
                        "size",
                        "etag",
                        "updated_at",
                    )
                }
                for item in selected
            ],
            "policy": {
                "title": title,
                "expires_days": safe["expires_days"],
                "access_code": str(arguments.get("access_code") or ""),
                "auto_access_code": bool(arguments.get("auto_access_code")),
                "max_restore_count": safe["max_restore_count"],
                "allow_download": safe["allow_download"],
            },
        },
        domain="guangya-share-create",
    )
    return safe, fingerprint, selected


def prepare_create_guangya_share(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    try:
        safe, fingerprint, _selected = _create_snapshot(
            arguments, owner=context.owner, verify_live=True
        )
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭分享创建预检失败") from exc
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将为 {safe['count']} 个光鸭对象创建分享",
        data={
            **safe,
            "effects": [
                "只分享本次目录快照中选定的对象。",
                "确认时会重新校验对象、登录凭据和分享策略。",
                "创建后会向当前登录用户返回分享链接；链接不会写入模型上下文。",
            ],
        },
        evidence=[
            Evidence(
                "guangya_share_create_preview",
                "已冻结分享对象和策略；访问码与 Provider 对象 ID 不进入公开预览。",
                _now(),
            )
        ],
    ), fingerprint


def _extract_created_share(raw: dict[str, Any]) -> dict[str, str]:
    candidate_id = str(
        _first(raw, "shareId", "shareID", "share_id", "id") or ""
    ).strip()
    share_id = candidate_id if _SHARE_ID_RE.fullmatch(candidate_id) else ""
    candidate_url = str(
        _first(raw, "shareUrl", "shareURL", "share_url", "url", "link") or ""
    ).strip()
    # 顶层 ``code`` 常是 HTTP 业务成功码（0/200），不能误当成分享访问码。
    candidate_code = str(
        _first(
            raw,
            "shareCode",
            "share_code",
            "accessCode",
            "access_code",
            "extractCode",
            "extract_code",
            "pwd",
        )
        or ""
    ).strip()
    code = candidate_code if _CODE_RE.fullmatch(candidate_code) else ""
    share_url = ""
    if candidate_url and len(candidate_url) <= 2048:
        try:
            parsed = urlsplit(candidate_url)
            hostname = str(parsed.hostname or "").casefold()
            if (
                parsed.scheme.casefold() == "https"
                and not parsed.username
                and not parsed.password
                and (
                    hostname == "guangyapan.com"
                    or hostname.endswith(".guangyapan.com")
                )
                and parsed.path.startswith("/s/")
            ):
                share_url = urlunsplit(parsed)
        except ValueError:
            share_url = ""
    if not share_url and share_id:
        share_url = f"https://www.guangyapan.com/s/{share_id}#/share"
    return {"share_id": share_id, "share_url": share_url, "access_code": code}


def execute_create_guangya_share(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    try:
        safe, fingerprint, selected = _create_snapshot(
            arguments, owner=context.owner, verify_live=True
        )
        if fingerprint != str(expected_context or ""):
            raise AgentToolError("光鸭分享计划已变化，请重新预检", code="confirmation_stale")
        client = _open_client()
        try:
            raw = client.create_user_share(
                [str(item.get("file_id") or "") for item in selected],
                title=safe["title"],
                validate_duration=safe["expires_days"],
                code=str(arguments.get("access_code") or ""),
                auto_fill_code=bool(arguments.get("auto_access_code")),
                max_restore_count=safe["max_restore_count"],
                allow_download=safe["allow_download"],
            )
            created = _extract_created_share(raw)
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc, fallback="创建光鸭分享失败") from exc

    verified = bool(created["share_url"])
    public_data = {
        "title": safe["title"],
        "count": safe["count"],
        "expires_days": safe["expires_days"],
        "allow_download": safe["allow_download"],
        "share_url": created["share_url"],
        "access_code": created["access_code"],
        "verified": verified,
        "verification_pending": not verified,
    }
    return ToolResult(
        True,
        "completed" if verified else "accepted",
        (
            f"光鸭分享已创建，共包含 {safe['count']} 个对象"
            if verified
            else f"光鸭已受理分享创建请求，共包含 {safe['count']} 个对象"
        ),
        data=public_data,
        model_data={
            "title": safe["title"],
            "count": safe["count"],
            "expires_days": safe["expires_days"],
            "share_created": verified,
            "verification_pending": not verified,
            "share_link_available_to_user": bool(created["share_url"]),
            "access_code_available_to_user": bool(created["access_code"]),
        },
        evidence=[
            Evidence(
                "guangya_share_create",
                (
                    "Provider 已返回可验证的光鸭分享链接；链接和访问码仅出现在当前用户公开结果中。"
                    if verified
                    else "Provider 已受理创建请求，但未返回可验证的分享标识；未将不可信链接或访问码公开。"
                ),
                _now(),
            )
        ],
        suggestions=(
            []
            if verified
            else ["可稍后查询光鸭分享列表，核对新分享是否已可见。"]
        ),
    )


def guangya_share_revoke_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {
        "guangya_shares_ref",
        "indices",
    }:
        raise AgentToolError("撤销光鸭分享需要分享引用和 indices")
    ref = str(arguments.get("guangya_shares_ref") or "").strip()
    if not ref.startswith("ref_") or len(ref) > 200:
        raise AgentToolError("分享引用无效")
    raw_indices = arguments.get("indices")
    if not isinstance(raw_indices, list) or not 1 <= len(raw_indices) <= 100:
        raise AgentToolError("indices 必须包含 1 到 100 个序号")
    indices: list[int] = []
    for value in raw_indices:
        if type(value) is not int or not 1 <= value <= _MAX_SHARES:
            raise AgentToolError("分享序号无效")
        if value not in indices:
            indices.append(value)
    return {"guangya_shares_ref": ref, "indices": indices}


def _selected_shares(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    collection = arguments.get("guangya_shares")
    if not isinstance(collection, dict) or not isinstance(collection.get("items"), list):
        raise AgentToolError("分享引用已失效，请重新查询", code="confirmation_stale")
    page = _bounded_int(collection.get("page"))
    page_size = _bounded_int(collection.get("page_size"))
    by_index = {
        (page - 1) * page_size + offset: item
        for offset, item in enumerate(collection["items"], 1)
        if isinstance(item, dict)
    }
    selected: list[dict[str, Any]] = []
    for index in arguments.get("indices") or ():
        item = by_index.get(int(index))
        if item is None:
            raise AgentToolError(
                "选择的分享序号不属于当前快照，请重新查询",
                code="confirmation_stale",
            )
        selected.append(item)
    return collection, selected


def _revoke_snapshot(
    arguments: dict[str, Any], *, verify_live: bool
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    collection, selected = _selected_shares(arguments)
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        if int(client.credential_generation) != _bounded_int(
            collection.get("credential_generation")
        ):
            raise AgentToolError(
                "光鸭登录凭据已变化，请重新查询分享列表",
                code="confirmation_stale",
            )
        if verify_live:
            current = {
                item["share_id"]: item for item in _load_share_snapshots(client)
            }
            for item in selected:
                live = current.get(str(item.get("share_id") or ""))
                if live is None or any(
                    str(live.get(key) or "") != str(item.get(key) or "")
                    for key in ("share_id", "title", "status", "updated_at")
                ):
                    raise AgentToolError(
                        "分享列表已变化，请重新查询后再确认",
                        code="confirmation_stale",
                    )
    finally:
        if client is not None:
            client.close()
    safe = {
        "count": len(selected),
        "samples": [
            sanitize_public_text(item.get("title"), limit=160) or "未命名分享"
            for item in selected[:6]
        ],
    }
    fingerprint = confirmation_context_fingerprint(
        {
            "credential_generation": _bounded_int(
                collection.get("credential_generation")
            ),
            "shares": selected,
        },
        domain="guangya-share-revoke",
    )
    return safe, fingerprint, selected


def prepare_revoke_guangya_shares(
    arguments: dict[str, Any], _context: ToolContext
) -> tuple[ToolResult, str]:
    try:
        safe, fingerprint, _selected = _revoke_snapshot(arguments, verify_live=True)
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭分享撤销预检失败") from exc
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将撤销 {safe['count']} 条光鸭分享",
        data={
            **safe,
            "effects": [
                "只撤销本次分享列表快照中选定的分享。",
                "撤销后原分享链接将不可继续访问。",
                "分享中的原始云盘文件不会被删除或移动。",
            ],
        },
        evidence=[
            Evidence(
                "guangya_share_revoke_preview",
                "已冻结分享列表快照；未公开分享 ID 或访问凭据。",
                _now(),
            )
        ],
    ), fingerprint


def execute_revoke_guangya_shares(
    arguments: dict[str, Any], expected_context: str, _context: ToolContext
) -> ToolResult:
    try:
        safe, fingerprint, selected = _revoke_snapshot(arguments, verify_live=True)
        if fingerprint != str(expected_context or ""):
            raise AgentToolError("光鸭分享撤销计划已变化，请重新预检", code="confirmation_stale")
        client = _open_client()
        try:
            client.delete_user_shares(
                [str(item.get("share_id") or "") for item in selected]
            )
            remaining = {str(item.get("share_id") or "") for item in selected}
            for _attempt in range(20):
                current = {item["share_id"] for item in _load_share_snapshots(client)}
                remaining &= current
                if not remaining:
                    break
                time.sleep(0.5)
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc, fallback="撤销光鸭分享失败") from exc

    verified = not remaining
    return ToolResult(
        True,
        "completed" if verified else "accepted",
        (
            f"已撤销 {safe['count']} 条光鸭分享"
            if verified
            else f"光鸭已受理 {safe['count']} 条分享撤销请求"
        ),
        data={**safe, "verified": verified, "verification_pending": not verified},
        model_data={
            "count": safe["count"],
            "verified": verified,
            "verification_pending": not verified,
        },
        evidence=[
            Evidence(
                "guangya_share_revoke",
                "Provider 已接受撤销请求；同步可见时已重新读取分享列表验证。",
                _now(),
            )
        ],
    )
