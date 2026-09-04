"""光鸭回收站读取、恢复与清空的 Effect-safe 领域动作。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_untrusted_filename
from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected

logger = logging.getLogger(__name__)
_MAX_RECYCLE_ITEMS = 20_000
_MAX_SELECTION = 200


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _bounded_int(value: object, *, maximum: int = 1 << 63) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError, OverflowError):
        return 0


def _snapshot(item: GuangYaFile) -> dict[str, Any]:
    return {
        "file_id": str(item.file_id),
        "name": str(item.name),
        "is_dir": bool(item.is_dir),
        "size": _bounded_int(item.size),
        "etag": str(item.etag or ""),
        "parent_id": str(item.parent_id or "0"),
        "updated_at": _bounded_int(item.updated_at),
    }


def _matches(current: GuangYaFile | None, expected: dict[str, Any]) -> bool:
    if current is None:
        return False
    if (
        str(current.file_id) != str(expected.get("file_id") or "")
        or str(current.name) != str(expected.get("name") or "")
        or bool(current.is_dir) != bool(expected.get("is_dir"))
        or _bounded_int(current.size) != _bounded_int(expected.get("size"))
    ):
        return False
    expected_etag = str(expected.get("etag") or "")
    return not expected_etag or str(current.etag or "") in {"", expected_etag}


def _open_client() -> GuangYaClient:
    client = GuangYaClient()
    if not client.logged_in:
        client.close()
        raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
    return client


def _public_error(exc: Exception, *, fallback: str) -> AgentToolError:
    if isinstance(exc, AgentToolError):
        return exc
    if isinstance(exc, GuangYaWriteRejected):
        return AgentToolError(
            exc.public_message or fallback,
            code="provider_rejected",
        )
    if isinstance(exc, (ValueError, RuntimeError)):
        message = str(exc).strip()
        if message and len(message) <= 240:
            return AgentToolError(message, code="precondition_failed")
    logger.warning("Agent 光鸭回收站操作失败 type=%s", type(exc).__name__)
    return AgentToolError(fallback, code="unavailable")


def guangya_recycle_list_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"page", "page_size"}:
        raise AgentToolError("光鸭回收站查询参数无效")
    page = arguments.get("page", 1)
    page_size = arguments.get("page_size", 50)
    if type(page) is not int or not 1 <= page <= 400:
        raise AgentToolError("page 必须在 1 到 400 之间")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise AgentToolError("page_size 必须在 1 到 100 之间")
    return {"page": page, "page_size": page_size}


def list_guangya_recycle(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        items = client.list_recycle(max_items=_MAX_RECYCLE_ITEMS)
        generation = int(client.credential_generation)
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭回收站当前不可用") from exc
    finally:
        if client is not None:
            client.close()

    page = int(arguments["page"])
    page_size = int(arguments["page_size"])
    start = (page - 1) * page_size
    selected = items[start : start + page_size]
    snapshots = [_snapshot(item) for item in selected]
    public_items = [
        {
            "index": start + index,
            "name": sanitize_untrusted_filename(item.name) or "未命名对象",
            "kind": "directory" if item.is_dir else "file",
            "size": _bounded_int(item.size),
        }
        for index, item in enumerate(selected, 1)
    ]
    collection = {
        "credential_generation": generation,
        "page": page,
        "page_size": page_size,
        "items": snapshots,
    }
    return ToolResult(
        True,
        "found" if selected else "empty",
        f"光鸭回收站共有 {len(items)} 个对象，本页显示 {len(selected)} 个",
        data={
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "has_more": start + len(selected) < len(items),
            "items": public_items,
        },
        model_data={
            "page": page,
            "total": len(items),
            "has_more": start + len(selected) < len(items),
            "items": public_items,
        },
        references=[
            ToolReference(
                kind="guangya_recycle_items",
                value=collection,
                ttl_seconds=15 * 60,
            )
        ] if selected else [],
        evidence=[
            Evidence(
                "guangya_recycle_bin",
                "返回的是当前账号回收站的脱敏快照和会话绑定引用；未公开 Provider 文件 ID。",
                _now(),
            )
        ],
        suggestions=[
            "恢复对象需要使用本次返回的回收站引用和本页 index，并经过人工确认。",
            "清空回收站会永久删除全部对象，必须单独生成高风险确认计划。",
        ],
    )


def guangya_recycle_restore_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {
        "guangya_recycle_items_ref",
        "indices",
    }:
        raise AgentToolError("恢复回收站对象需要引用和 indices")
    ref = str(arguments.get("guangya_recycle_items_ref") or "").strip()
    if not ref.startswith("ref_") or len(ref) > 200:
        raise AgentToolError("回收站引用无效")
    raw_indices = arguments.get("indices")
    if not isinstance(raw_indices, list) or not 1 <= len(raw_indices) <= _MAX_SELECTION:
        raise AgentToolError(f"indices 必须包含 1 到 {_MAX_SELECTION} 个序号")
    indices: list[int] = []
    for value in raw_indices:
        if type(value) is not int or value < 1 or value > _MAX_RECYCLE_ITEMS:
            raise AgentToolError("回收站序号无效")
        if value not in indices:
            indices.append(value)
    return {"guangya_recycle_items_ref": ref, "indices": indices}


def _selected_recycle_snapshots(arguments: dict[str, Any]) -> tuple[dict, list[dict]]:
    collection = arguments.get("guangya_recycle_items")
    if not isinstance(collection, dict) or not isinstance(collection.get("items"), list):
        raise AgentToolError("回收站引用已失效，请重新查询", code="confirmation_stale")
    page = _bounded_int(collection.get("page"))
    page_size = _bounded_int(collection.get("page_size"))
    snapshots = [item for item in collection["items"] if isinstance(item, dict)]
    by_index = {
        (page - 1) * page_size + offset: item
        for offset, item in enumerate(snapshots, 1)
    }
    selected: list[dict] = []
    for index in arguments.get("indices") or ():
        item = by_index.get(int(index))
        if item is None:
            raise AgentToolError(
                "选择的回收站序号不属于当前快照，请重新查询",
                code="confirmation_stale",
            )
        selected.append(item)
    return collection, selected


def _restore_snapshot(
    arguments: dict[str, Any], *, verify_live: bool
) -> tuple[dict[str, Any], str]:
    collection, selected = _selected_recycle_snapshots(arguments)
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        if int(client.credential_generation) != _bounded_int(
            collection.get("credential_generation")
        ):
            raise AgentToolError(
                "光鸭登录凭据已变化，请重新查询回收站",
                code="confirmation_stale",
            )
        if verify_live:
            current = {
                str(item.file_id): item
                for item in client.list_recycle(max_items=_MAX_RECYCLE_ITEMS)
            }
            if any(
                not _matches(current.get(str(item.get("file_id") or "")), item)
                for item in selected
            ):
                raise AgentToolError(
                    "回收站内容已变化，请重新查询后再确认",
                    code="confirmation_stale",
                )
    finally:
        if client is not None:
            client.close()
    safe = {
        "count": len(selected),
        "total_size": sum(_bounded_int(item.get("size")) for item in selected),
        "samples": [
            sanitize_untrusted_filename(item.get("name")) or "未命名对象"
            for item in selected[:6]
        ],
    }
    fingerprint = confirmation_context_fingerprint(
        {
            "credential_generation": _bounded_int(
                collection.get("credential_generation")
            ),
            "items": selected,
        },
        domain="guangya-recycle-restore",
    )
    return safe, fingerprint


def prepare_restore_guangya_recycle(
    arguments: dict[str, Any], _context: ToolContext
) -> tuple[ToolResult, str]:
    try:
        safe, fingerprint = _restore_snapshot(arguments, verify_live=True)
    except Exception as exc:
        raise _public_error(exc, fallback="回收站恢复预检失败") from exc
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将从光鸭回收站恢复 {safe['count']} 个对象",
        data={
            **safe,
            "effects": [
                "只恢复本次回收站快照中选定的对象。",
                "确认时会重新核对账号凭据与回收站对象快照。",
                "恢复可能在原目录形成同名冲突，Provider 拒绝时不会扩大操作范围。",
            ],
        },
        evidence=[
            Evidence(
                "guangya_recycle_restore_preview",
                "已冻结对象数量、体积和对象快照；未公开内部文件 ID。",
                _now(),
            )
        ],
    ), fingerprint


def execute_restore_guangya_recycle(
    arguments: dict[str, Any], expected_context: str, _context: ToolContext
) -> ToolResult:
    try:
        safe, fingerprint = _restore_snapshot(arguments, verify_live=True)
        if fingerprint != str(expected_context or ""):
            raise AgentToolError(
                "回收站恢复计划已变化，请重新预检",
                code="confirmation_stale",
            )
        _collection, selected = _selected_recycle_snapshots(arguments)
        client = _open_client()
        try:
            task_id = client.restore_from_recycle(
                [str(item.get("file_id") or "") for item in selected]
            )
            remaining = {str(item.get("file_id") or "") for item in selected}
            for _attempt in range(20):
                current_ids = {
                    str(item.file_id)
                    for item in client.list_recycle(max_items=_MAX_RECYCLE_ITEMS)
                }
                remaining &= current_ids
                if not remaining:
                    break
                time.sleep(0.5)
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭回收站恢复失败") from exc

    references = [
        ToolReference(
            kind="guangya_task",
            value={"task_id": task_id, "operation": "recycle_restore"},
            ttl_seconds=24 * 60 * 60,
        )
    ] if task_id else []
    verified = not remaining
    return ToolResult(
        True,
        "completed" if verified else "accepted",
        (
            f"已从光鸭回收站恢复 {safe['count']} 个对象"
            if verified
            else f"光鸭已受理 {safe['count']} 个对象的恢复请求"
        ),
        data={**safe, "verified": verified, "verification_pending": not verified},
        model_data={
            "count": safe["count"],
            "verified": verified,
            "verification_pending": not verified,
        },
        references=references,
        evidence=[
            Evidence(
                "guangya_recycle_restore",
                "恢复请求已经 Provider 接受；同步可见时已再次读取回收站验证。",
                _now(),
            )
        ],
        suggestions=[
            *(["可使用返回的光鸭任务引用继续查询异步状态。"] if references else []),
        ],
    )


def guangya_recycle_clear_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("清空光鸭回收站不接受参数")
    return {}


def _clear_snapshot(*, verify_nonempty: bool = True) -> tuple[dict[str, Any], str]:
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        items = client.list_recycle(max_items=_MAX_RECYCLE_ITEMS)
        generation = int(client.credential_generation)
    finally:
        if client is not None:
            client.close()
    if verify_nonempty and not items:
        raise AgentToolError("光鸭回收站已经为空", code="precondition_failed")
    # Provider 未承诺回收站列表顺序稳定。冻结“集合”而不是当前返回顺序，
    # 避免仅因分页/排序变化让同一批对象的确认票据无故失效。
    ordered = sorted(items, key=lambda item: str(item.file_id))
    snapshots = [_snapshot(item) for item in ordered]
    safe = {
        "count": len(ordered),
        "total_size": sum(_bounded_int(item.size) for item in ordered),
        "samples": [
            sanitize_untrusted_filename(item.name) or "未命名对象"
            for item in ordered[:6]
        ],
        "irreversible": True,
    }
    fingerprint = confirmation_context_fingerprint(
        {"credential_generation": generation, "items": snapshots},
        domain="guangya-recycle-clear",
    )
    return safe, fingerprint


def prepare_clear_guangya_recycle(
    _arguments: dict[str, Any], _context: ToolContext
) -> tuple[ToolResult, str]:
    try:
        safe, fingerprint = _clear_snapshot()
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭回收站清空预检失败") from exc
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将永久删除光鸭回收站中的 {safe['count']} 个对象",
        data={
            **safe,
            "effects": [
                "操作范围是确认时仍与当前快照完全一致的整个回收站。",
                "回收站任一对象发生变化都会让确认票据失效。",
                "清空后无法通过 MediaFlux 或光鸭回收站恢复。",
            ],
        },
        evidence=[
            Evidence(
                "guangya_recycle_clear_preview",
                "已完整读取并冻结当前回收站计数、体积和私有对象快照。",
                _now(),
            )
        ],
    ), fingerprint


def execute_clear_guangya_recycle(
    _arguments: dict[str, Any], expected_context: str, _context: ToolContext
) -> ToolResult:
    try:
        safe, fingerprint = _clear_snapshot()
        if fingerprint != str(expected_context or ""):
            raise AgentToolError(
                "光鸭回收站内容已变化，请重新预检",
                code="confirmation_stale",
            )
        client = _open_client()
        try:
            task_id = client.clear_recycle_bin()
            remaining = safe["count"]
            for _attempt in range(20):
                remaining = len(client.list_recycle(max_items=_MAX_RECYCLE_ITEMS))
                if remaining == 0:
                    break
                time.sleep(0.5)
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭回收站清空失败") from exc

    references = [
        ToolReference(
            kind="guangya_task",
            value={"task_id": task_id, "operation": "recycle_clear"},
            ttl_seconds=24 * 60 * 60,
        )
    ] if task_id else []
    verified = remaining == 0
    return ToolResult(
        True,
        "completed" if verified else "accepted",
        (
            f"光鸭回收站已清空，共永久删除 {safe['count']} 个对象"
            if verified
            else f"光鸭已受理清空回收站请求，涉及 {safe['count']} 个对象"
        ),
        data={
            "count": safe["count"],
            "total_size": safe["total_size"],
            "verified": verified,
            "verification_pending": not verified,
            "irreversible": True,
        },
        model_data={
            "count": safe["count"],
            "verified": verified,
            "verification_pending": not verified,
        },
        references=references,
        evidence=[
            Evidence(
                "guangya_recycle_clear",
                "Provider 已接受不可逆清空请求；同步可见时已再次读取回收站验证。",
                _now(),
            )
        ],
        suggestions=[
            *(["可使用返回的光鸭任务引用继续查询异步状态。"] if references else []),
        ],
    )


def guangya_task_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"guangya_task_ref"}:
        raise AgentToolError("查询光鸭任务需要 guangya_task_ref")
    ref = str(arguments.get("guangya_task_ref") or "").strip()
    if not ref.startswith("ref_") or len(ref) > 200:
        raise AgentToolError("光鸭任务引用无效")
    return {"guangya_task_ref": ref}


def query_guangya_task_status(
    arguments: dict[str, Any], _context: ToolContext
) -> ToolResult:
    task = arguments.get("guangya_task")
    if not isinstance(task, dict) or not str(task.get("task_id") or "").strip():
        raise AgentToolError("光鸭任务引用已失效", code="reference_invalid")
    client: GuangYaClient | None = None
    try:
        client = _open_client()
        raw = client.task_status(str(task["task_id"]))
    except Exception as exc:
        raise _public_error(exc, fallback="光鸭任务状态当前不可用") from exc
    finally:
        if client is not None:
            client.close()

    payload = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    status = str(
        payload.get("status")
        or payload.get("taskStatus")
        or payload.get("state")
        or "unknown"
    ).strip().casefold()
    progress = payload.get("progress")
    if progress is None:
        progress = payload.get("percent")
    try:
        progress_value = float(progress or 0)
        if progress_value > 1:
            progress_value /= 100
    except (TypeError, ValueError, OverflowError):
        progress_value = 0.0
    completed = status in {"2", "done", "completed", "success", "succeeded", "finished"}
    failed = status in {"3", "failed", "error", "cancelled", "canceled"}
    public_status = "completed" if completed else "failed" if failed else "running"
    return ToolResult(
        not failed,
        public_status,
        "光鸭任务已完成" if completed else "光鸭任务执行失败" if failed else "光鸭任务仍在处理中",
        data={
            "operation": str(task.get("operation") or "operation"),
            "status": public_status,
            "progress": max(0.0, min(progress_value, 1.0)),
        },
        evidence=[
            Evidence(
                "guangya_provider_task",
                "通过会话绑定任务引用读取 Provider 脱敏状态；未公开原始任务 ID 或响应。",
                _now(),
            )
        ],
    )
