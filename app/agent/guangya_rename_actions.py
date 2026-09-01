"""Agent 光鸭重命名：只读冻结计划、owner-bound 确认与持久执行。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
import threading
import time
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.agent.session_context import AgentContextWriteGuard, AgentSessionContextRepository
from app.clients.guangya import GuangYaClient
from app.modules.guangya_media_hygiene import build_media_hygiene_plan
from app.modules.guangya_rename import (
    GuangYaRenamePlanError,
    GuangYaRenamePlanStale,
    build_rename_plan,
    confirm_rename_plan,
    discard_rename_plan,
    execute_rename_plan,
    load_rename_plan,
)
from app.repositories.organize_operation_jobs import (
    organize_operation_owner_digest,
    organize_operation_public_ref,
)

logger = logging.getLogger(__name__)
_CONTEXT_TYPE = "guangya_rename"
_TTL_SECONDS = 15 * 60.0
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_RENAME_MODES = {"remove_bitrate", "replace_text"}
_PERSISTED_MODES = {*_PUBLIC_RENAME_MODES, "media_hygiene"}
_PREVIEW_COUNT_KEYS = (
    "scope_count", "scanned_items", "scanned_dirs", "matched",
    "rename_count", "conflict_count", "no_change_count",
    "identified_video_count", "unidentified_video_count",
    "video_rename_count", "companion_rename_count",
    "directory_rename_count", "metadata_enriched_count",
)


@dataclass
class _Flow:
    owner: str
    plan_id: str
    fingerprint: str
    preview_safe: dict[str, Any]
    updated_at: float = 0.0
    generation: int = 0
    revision: int = 0


_lock = threading.RLock()
_flows: dict[str, _Flow] = {}
_repository: AgentSessionContextRepository | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_guangya_rename_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    global _repository
    _repository = repository


def reset_guangya_rename_context_for_tests() -> None:
    global _repository
    with _lock:
        _flows.clear()
    _repository = None


def clear_guangya_rename_context(*, owner: str) -> None:
    """清除 owner 的内存缓存；SQLite epoch 是唯一有效性权威。"""
    owner_key = str(owner or "").strip()
    if not owner_key:
        return
    with _lock:
        _flows.pop(owner_key, None)


def _safe_preview(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mode = str(value.get("mode") or "").strip().casefold()
    if mode not in _PERSISTED_MODES:
        return None
    try:
        counts = {
            key: max(0, int(value.get(key) or 0))
            for key in _PREVIEW_COUNT_KEYS
        }
    except (TypeError, ValueError, OverflowError):
        return None
    samples: list[str] = []
    raw_samples = value.get("sample_changes")
    if not isinstance(raw_samples, list):
        return None
    for raw in raw_samples[:5]:
        sample = sanitize_public_text(raw, limit=520)
        if sample:
            samples.append(sample)
    return {
        **counts,
        "mode": mode,
        "recursive": bool(value.get("recursive")),
        "sample_changes": samples,
        "rollback_available": bool(value.get("rollback_available")),
        "trigger_strm": bool(value.get("trigger_strm")),
        "cloud_write": False,
    }


def _flow_payload(flow: _Flow) -> dict[str, Any]:
    return {
        "plan_id": flow.plan_id,
        "fingerprint": flow.fingerprint,
        "preview_safe": dict(flow.preview_safe),
    }


def _flow_from_payload(
    owner: str,
    payload: dict[str, Any],
    *,
    generation: int = 0,
    revision: int = 0,
) -> _Flow | None:
    plan_id = str(payload.get("plan_id") or "").strip()
    fingerprint = str(payload.get("fingerprint") or "").strip()
    preview_safe = _safe_preview(payload.get("preview_safe"))
    if (
        not _PLAN_ID_RE.fullmatch(plan_id)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or preview_safe is None
    ):
        return None
    return _Flow(
        owner=str(owner), plan_id=plan_id, fingerprint=fingerprint,
        preview_safe=preview_safe, updated_at=time.monotonic(),
        generation=max(0, int(generation or 0)), revision=max(0, int(revision or 0)),
    )


def _begin_flow(owner: str) -> AgentContextWriteGuard:
    if _repository is None:
        return AgentContextWriteGuard(generation=0, revision=0)
    begin = getattr(_repository, "begin_context", None)
    if not callable(begin):
        return AgentContextWriteGuard(generation=0, revision=0)
    return begin(owner=owner, context_type=_CONTEXT_TYPE)


def _begin_flow_update(
    owner: str,
) -> tuple[_Flow | None, AgentContextWriteGuard]:
    if _repository is not None:
        begin_update = getattr(_repository, "begin_context_update", None)
        if callable(begin_update):
            persisted, guard = begin_update(
                owner=owner, context_type=_CONTEXT_TYPE,
            )
            previous = None
            if persisted is not None:
                previous = _flow_from_payload(
                    owner, persisted.payload,
                    generation=persisted.generation,
                    revision=persisted.revision,
                )
                if previous is not None:
                    with _lock:
                        _flows[owner] = previous
            return previous, guard
    return _flow(owner), _begin_flow(owner)


def _discard_replaced_plan(previous: _Flow | None, plan_id: str) -> None:
    if previous is not None and previous.plan_id != plan_id:
        discard_rename_plan(previous.plan_id)


def _save(flow: _Flow) -> bool:
    flow.updated_at = time.monotonic()
    if _repository is not None:
        try:
            guarded = getattr(_repository, "replace_latest_guarded", None)
            if callable(guarded):
                if flow.generation <= 0:
                    return False
                persisted = guarded(
                    owner=flow.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(flow),
                    expires_at=time.time() + _TTL_SECONDS,
                    guard=AgentContextWriteGuard(
                        generation=flow.generation, revision=flow.revision,
                    ),
                )
                if persisted is None:
                    return False
                flow.generation = persisted.generation
                flow.revision = persisted.revision
            else:
                _repository.replace_latest(
                    owner=flow.owner, context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(flow), expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:
            logger.warning("Agent 光鸭重命名上下文保存失败 type=%s", type(exc).__name__)
            return False
    with _lock:
        expired = [
            owner for owner, item in _flows.items()
            if time.monotonic() - item.updated_at > _TTL_SECONDS
        ]
        for owner in expired:
            _flows.pop(owner, None)
        _flows[flow.owner] = flow
    return True


def _flow(owner: str) -> _Flow | None:
    owner_key = str(owner or "").strip()
    if not owner_key:
        return None
    if _repository is not None:
        try:
            persisted = _repository.get_latest(
                owner=owner_key, context_type=_CONTEXT_TYPE, now=time.time(),
            )
        except Exception as exc:
            logger.warning("Agent 光鸭重命名上下文恢复失败 type=%s", type(exc).__name__)
            return None
        if persisted is None:
            return None
        restored = _flow_from_payload(
            owner_key, persisted.payload,
            generation=persisted.generation, revision=persisted.revision,
        )
        if restored is None:
            return None
        with _lock:
            _flows[owner_key] = restored
        return restored
    with _lock:
        item = _flows.get(owner_key)
        if item is None or time.monotonic() - item.updated_at > _TTL_SECONDS:
            _flows.pop(owner_key, None)
            return None
        return item


def _consume(flow: _Flow) -> bool:
    if _repository is not None:
        consume = getattr(_repository, "consume_latest_guarded", None)
        try:
            if callable(consume) and flow.generation > 0 and flow.revision > 0:
                consumed = bool(consume(
                    owner=flow.owner, context_type=_CONTEXT_TYPE,
                    guard=AgentContextWriteGuard(
                        generation=flow.generation, revision=flow.revision,
                    ),
                ))
            elif not callable(consume):
                consumed = bool(_repository.delete_latest(
                    owner=flow.owner, context_type=_CONTEXT_TYPE,
                ))
            else:
                consumed = False
        except Exception as exc:
            logger.warning("Agent 光鸭重命名上下文消费失败 type=%s", type(exc).__name__)
            return False
        if not consumed:
            return False
    with _lock:
        _flows.pop(flow.owner, None)
    return True


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, GuangYaRenamePlanStale):
        return AgentToolError(str(exc), code="confirmation_stale")
    if isinstance(exc, GuangYaRenamePlanError):
        return AgentToolError(str(exc), code="precondition_failed")
    logger.warning("Agent 光鸭重命名失败 type=%s", type(exc).__name__)
    return AgentToolError("光鸭重命名当前不可用", code="precondition_failed")


def guangya_rename_preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    allowed = {"paths", "mode", "recursive", "limit", "find", "replace"}
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_paths = arguments.get("paths")
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 4:
        raise AgentToolError("paths 必须包含 1 到 4 个精确光鸭路径")
    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise AgentToolError("光鸭路径必须是字符串")
        path = raw.strip()
        if not path.startswith("/") or len(path) > 2048:
            raise AgentToolError("光鸭路径必须是绝对路径")
        paths.append(path)
    mode = str(arguments.get("mode") or "").strip().casefold()
    if mode not in _PUBLIC_RENAME_MODES:
        raise AgentToolError("mode 仅支持 remove_bitrate 或 replace_text")
    recursive = arguments.get("recursive", False)
    if type(recursive) is not bool:
        raise AgentToolError("recursive 必须是布尔值")
    limit = arguments.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise AgentToolError("limit 必须是 1 到 10000 的整数")
    normalized: dict[str, Any] = {
        "paths": paths, "mode": mode, "recursive": recursive, "limit": limit,
    }
    if mode == "replace_text":
        find_text = arguments.get("find")
        replace_text = arguments.get("replace", "")
        if not isinstance(find_text, str) or not find_text:
            raise AgentToolError("文本替换必须提供非空 find")
        if not isinstance(replace_text, str):
            raise AgentToolError("replace 必须是字符串")
        normalized.update({"find_text": find_text, "replace_text": replace_text})
    elif set(arguments) & {"find", "replace"}:
        raise AgentToolError("去除码率模式不接受文本替换参数")
    return normalized


def guangya_media_hygiene_preview_arguments(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("媒体名称清理参数必须是对象")
    allowed = {"path", "recursive", "limit", "enrich_metadata"}
    if set(arguments) - allowed:
        raise AgentToolError("媒体名称清理包含不支持的参数")
    path = str(arguments.get("path") or "").strip().replace("\\", "/")
    if path and not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/") or len(path) > 2048:
        raise AgentToolError("请提供一个精确的光鸭目录路径")
    if any(part in {".", ".."} for part in path.split("/") if part):
        raise AgentToolError("光鸭目录路径不能包含相对路径组件")
    recursive = arguments.get("recursive", True)
    enrich_metadata = arguments.get("enrich_metadata", True)
    if not isinstance(recursive, bool) or not isinstance(enrich_metadata, bool):
        raise AgentToolError("recursive 和 enrich_metadata 必须是布尔值")
    try:
        limit = int(arguments.get("limit", 1000))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentToolError("limit 必须是整数") from exc
    if not 1 <= limit <= 10_000:
        raise AgentToolError("limit 必须在 1 到 10000 之间")
    return {
        "path": path,
        "recursive": recursive,
        "limit": limit,
        "enrich_metadata": enrich_metadata,
    }


def guangya_rename_execute_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("guangya.rename.execute 不接受参数")
    return {}


def _preview_projection(plan: dict[str, Any]) -> dict[str, Any]:
    stats = plan.get("stats") if isinstance(plan.get("stats"), dict) else {}
    sample_changes: list[str] = []
    for item in list(plan.get("samples") or [])[:5]:
        if not isinstance(item, dict):
            continue
        before = sanitize_public_text(item.get("before"), limit=255)
        after = sanitize_public_text(item.get("after"), limit=255)
        sample = sanitize_public_text(
            f"{before} → {after}" if before and after else "", limit=520,
        )
        if sample:
            sample_changes.append(sample)
    rename_count = max(0, int(stats.get("rename_count") or 0))
    return {
        "scope_count": len(plan.get("targets") or []),
        "scanned_items": max(0, int(stats.get("scanned_items") or 0)),
        "scanned_dirs": max(0, int(stats.get("scanned_dirs") or 0)),
        "matched": max(0, int(stats.get("matched") or 0)),
        "rename_count": rename_count,
        "conflict_count": max(0, int(stats.get("conflict_count") or 0)),
        "no_change_count": max(0, int(stats.get("no_change_count") or 0)),
        "identified_video_count": max(0, int(stats.get("identified_video_count") or 0)),
        "unidentified_video_count": max(0, int(stats.get("unidentified_video_count") or 0)),
        "video_rename_count": max(0, int(stats.get("video_rename_count") or 0)),
        "companion_rename_count": max(0, int(stats.get("companion_rename_count") or 0)),
        "directory_rename_count": max(0, int(stats.get("directory_rename_count") or 0)),
        "metadata_enriched_count": max(0, int(stats.get("metadata_enriched_count") or 0)),
        "mode": str(plan.get("mode") or ""),
        "recursive": bool(plan.get("recursive")),
        "sample_changes": sample_changes,
        "rollback_available": rename_count > 0,
        "trigger_strm": (
            str((plan.get("transform") or {}).get("trigger_strm") or "").strip() == "1"
            or str(plan.get("mode") or "") == "media_hygiene"
        ),
        "cloud_write": False,
    }


def preview_guangya_rename(
    arguments: dict[str, Any], context: ToolContext,
) -> ToolResult:
    if not context.owner:
        raise AgentToolError("光鸭重命名需要已登录会话", code="precondition_failed")
    previous, guard = _begin_flow_update(context.owner)
    client = GuangYaClient()
    try:
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        plan_arguments = dict(arguments)
        plan_arguments["targets"] = plan_arguments.pop("paths")
        plan = build_rename_plan(client, owner=context.owner, **plan_arguments)
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        client.close()
    preview = _preview_projection(plan)
    flow = _Flow(
        owner=context.owner, plan_id=str(plan["plan_id"]),
        fingerprint=str(plan["fingerprint"]), preview_safe=preview,
        generation=guard.generation, revision=guard.revision,
    )
    if not _save(flow):
        discard_rename_plan(flow.plan_id)
        raise AgentToolError("重命名预览已被更新请求取代，请重新生成", code="precondition_failed")
    _discard_replaced_plan(previous, flow.plan_id)
    count = int(preview["rename_count"])
    conflicts = int(preview["conflict_count"])
    summary = (
        f"找到 {count} 个可安全重命名对象"
        + (f"，另有 {conflicts} 个名称冲突已排除" if conflicts else "")
        if count else "没有找到可安全执行的名称变更"
    )
    suggestions = (
        ["如预览无误，可以确认执行刚才的光鸭重命名计划。"]
        if count else ["请调整路径、重命名方式或匹配文本后重新预览。"]
    )
    return ToolResult(
        ok=True,
        status="ready" if count else "no_changes",
        summary=summary,
        data=preview,
        evidence=[Evidence(
            "guangya_snapshot",
            "已按 file_id、父目录、名称、大小和内容标识冻结远端快照；未执行云端写入。",
            _now(),
        )],
        suggestions=suggestions,
    )


def preview_guangya_media_hygiene(
    arguments: dict[str, Any], context: ToolContext,
) -> ToolResult:
    if not context.owner:
        raise AgentToolError("媒体名称清理需要已登录会话", code="precondition_failed")
    previous, guard = _begin_flow_update(context.owner)
    client = GuangYaClient()
    try:
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        plan = build_media_hygiene_plan(client, owner=context.owner, **arguments)
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        client.close()
    preview = _preview_projection(plan)
    flow = _Flow(
        owner=context.owner, plan_id=str(plan["plan_id"]),
        fingerprint=str(plan["fingerprint"]), preview_safe=preview,
        generation=guard.generation, revision=guard.revision,
    )
    if not _save(flow):
        discard_rename_plan(flow.plan_id)
        raise AgentToolError("名称清理预览已被更新请求取代，请重新生成", code="precondition_failed")
    _discard_replaced_plan(previous, flow.plan_id)
    count = int(preview["rename_count"])
    conflicts = int(preview["conflict_count"])
    summary = (
        f"找到 {count} 个可安全清理名称的光鸭对象"
        + (f"，另有 {conflicts} 个重名冲突已排除" if conflicts else "")
        if count else "没有发现可安全清理的域名污染媒体名称"
    )
    return ToolResult(
        ok=True,
        status="ready" if count else "no_changes",
        summary=summary,
        data=preview,
        evidence=[Evidence(
            "guangya_snapshot",
            "已提取高置信番号并冻结目录、视频和唯一关联伴随文件的名称映射；未执行云端写入。",
            _now(),
        )],
        suggestions=(
            ["如预览无误，可以确认执行；成功后会自动触发 STRM 全量核对。"]
            if count else ["未识别到高置信番号或域名污染时会保留原名称，可缩小到更精确的目录后重试。"]
        ),
    )


def _confirmation_fingerprint(flow: _Flow, plan: dict[str, Any]) -> str:
    return confirmation_context_fingerprint({
        "owner": flow.owner,
        "plan_id": flow.plan_id,
        "plan_fingerprint": flow.fingerprint,
        "credential_generation": int(plan.get("credential_generation") or 0),
        "rename_count": int(flow.preview_safe.get("rename_count") or 0),
        "mode": str(flow.preview_safe.get("mode") or ""),
        "trigger_strm": bool(flow.preview_safe.get("trigger_strm")),
    }, domain="guangya-rename-confirmation")


def prepare_guangya_rename_confirmation(
    _arguments: dict[str, Any], context: ToolContext,
) -> tuple[ToolResult, str]:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError("最近重命名预览不存在或已过期，请重新生成", code="precondition_failed")
    try:
        plan = load_rename_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint,
        )
        client = GuangYaClient()
        try:
            if not client.logged_in or int(client.credential_generation) != int(plan["credential_generation"]):
                raise GuangYaRenamePlanStale("光鸭登录凭据已变化，请重新预览")
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc) from exc
    count = int(flow.preview_safe.get("rename_count") or 0)
    if count <= 0:
        raise AgentToolError("最近预览没有可执行的名称变更", code="precondition_failed")
    mode = str(flow.preview_safe.get("mode") or "")
    media_hygiene = mode == "media_hygiene"
    strm_linked = bool(flow.preview_safe.get("trigger_strm"))
    return ToolResult(
        True,
        "confirmation_required",
        (
            f"确认后将清理 {count} 个光鸭媒体名称"
            if media_hygiene else f"确认后将批量转换 {count} 个光鸭对象名称"
        ),
        data={**flow.preview_safe, "effects": [
            "只执行刚才冻结并排除冲突后的名称映射，不会扩大扫描范围。",
            "执行前会重新核对登录凭据、文件快照与目标名称占用情况。",
            "每次写入后都会按 file_id 读取真实名称，HTTP 200 不直接视为成功。",
            "旧名称、新名称与写入结果会保存在私有回滚清单中。",
            *(
                ["至少一个名称成功变更后会自动触发 STRM 全量核对。"]
                if strm_linked else []
            ),
        ]},
        evidence=[Evidence(
            "guangya_rename_plan",
            "已核对当前会话的冻结计划、凭据世代和计划签名。",
            _now(),
        )],
    ), _confirmation_fingerprint(flow, plan)


def execute_guangya_rename_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext,
) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError("重命名预览已过期，请重新生成", code="confirmation_stale")
    try:
        plan = load_rename_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint,
        )
        if _confirmation_fingerprint(flow, plan) != str(expected_context or ""):
            raise AgentToolError("重命名预览已变化，请重新预检", code="confirmation_stale")
        confirmed = confirm_rename_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint,
        )
        from app.modules.organize_tasks import get_organize_manager

        confirmed_mode = str(confirmed.get("mode") or "")
        media_hygiene = confirmed_mode == "media_hygiene"
        operation = "光鸭媒体名称清理" if media_hygiene else "光鸭批量名称转换"
        task = get_organize_manager().start_durable_operation(
            operation,
            "已确认媒体名称清理计划" if media_hygiene else "已确认批量名称转换计划",
            job_kind="agent_guangya_rename",
            owner=context.owner,
            payload={
                "version": 1,
                "plan_id": flow.plan_id,
                "plan_fingerprint": flow.fingerprint,
                "owner_digest": organize_operation_owner_digest(context.owner),
                "credential_generation": int(confirmed["credential_generation"]),
            },
            dedupe_key=f"agent-guangya-rename:{flow.plan_id}:{flow.fingerprint}",
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    if not task.get("ok"):
        raise AgentToolError(
            "光鸭整理队列当前不可用，重命名预览已保留，请稍后重新确认",
            code="precondition_failed",
        )
    context_consumed = _consume(flow)
    internal_id = str(task.get("task_id") or "")
    operation_ref = organize_operation_public_ref(internal_id) if re.fullmatch(r"[0-9a-f]{32}", internal_id) else ""
    queued = bool(task.get("queued"))
    return ToolResult(
        True,
        "accepted",
        f"{operation}已排队" if queued else f"{operation}任务已启动",
        data={
            "queued": queued,
            "queue_position": max(0, int(task.get("queue_position") or 0)),
            "replayed": bool(task.get("replayed")),
            "operation_ref": operation_ref,
            "rename_count": int(flow.preview_safe.get("rename_count") or 0),
            "requires_manual": not context_consumed,
        },
        evidence=[Evidence(
            "organize_queue",
            "冻结计划已提交到可恢复的光鸭写入队列；公开结果不包含文件 ID、路径或清单位置。",
            _now(),
        )],
        suggestions=[
            "可以稍后查询光鸭整理状态查看完成、部分完成或失败数量。",
            *(
                ["会话状态未能可靠消费，请勿重复提交同一计划。"]
                if not context_consumed else []
            ),
        ],
    )


def execute_durable_guangya_rename_job(
    payload: dict[str, Any], *, cancel_check=None,
) -> dict[str, Any]:
    plan = load_rename_plan(
        str(payload.get("plan_id") or ""),
        expected_fingerprint=str(payload.get("plan_fingerprint") or ""),
        require_confirmed=True,
    )
    result = execute_rename_plan(payload, cancel_check=cancel_check)
    stats = result.setdefault("stats", {})
    plan_mode = str(plan.get("mode") or "")
    transform = plan.get("transform") if isinstance(plan.get("transform"), dict) else {}
    # 仅为升级前已经确认并入队的旧声明式计划保留执行兼容；
    # 注册层、参数校验和新计划构建入口均已移除。
    legacy_strm_linked = bool(
        plan_mode == "declarative"
        and str(transform.get("trigger_strm") or "") == "1"
    )
    should_trigger_strm = plan_mode == "media_hygiene" or legacy_strm_linked
    if should_trigger_strm and int(stats.get("renamed") or 0) > 0:
        try:
            from app.modules.scheduler import get_scheduler

            triggered = get_scheduler().trigger(
                "organize", force_full=True, sync_mode="full"
            )
        except Exception as exc:
            logger.warning(
                "光鸭改名后 STRM 联动失败 type=%s", type(exc).__name__
            )
            triggered = {"ok": False}
        if bool(triggered.get("ok")):
            stats["strm_triggered"] = 1
        else:
            stats["strm_trigger_failed"] = 1
            result["partial"] = True
    return result
