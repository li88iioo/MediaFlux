"""Agent 光鸭整理残留清理：冻结预览、确认与持久执行。"""
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
from app.agent.organize_actions import _configured_sources
from app.clients.guangya import GuangYaClient
from app.modules.offline import OfflineRules
from app.modules.guangya_residual_cleanup import (
    GuangYaCleanupPlanError,
    GuangYaCleanupPlanStale,
    build_cleanup_plan,
    confirm_cleanup_plan,
    discard_cleanup_plan,
    execute_cleanup_plan,
    load_cleanup_plan,
)
from app.repositories.organize_operation_jobs import (
    organize_operation_owner_digest,
    organize_operation_public_ref,
)

logger = logging.getLogger(__name__)
_CONTEXT_TYPE = "guangya_cleanup"
_TTL_SECONDS = 15 * 60.0
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_KEYS = (
    "source_count", "scanned_items", "scanned_dirs", "empty_dir_count",
    "residual_dir_count", "quarantine_file_count", "preserved_dir_count",
    "unsupported_empty_dir_count",
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


def configure_guangya_cleanup_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    global _repository
    _repository = repository


def reset_guangya_cleanup_context_for_tests() -> None:
    global _repository
    with _lock:
        _flows.clear()
    _repository = None


def _safe_preview(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        counts = {key: max(0, int(value.get(key) or 0)) for key in _PREVIEW_KEYS}
    except (TypeError, ValueError, OverflowError):
        return None
    samples = []
    if not isinstance(value.get("sample_directories"), list):
        return None
    for raw in value["sample_directories"][:5]:
        sample = sanitize_public_text(raw, limit=255)
        if sample:
            samples.append(sample)
    return {
        **counts,
        "sample_directories": samples,
        "cloud_write": False,
        "quarantine_instead_of_delete": True,
    }


def _payload(flow: _Flow) -> dict[str, Any]:
    return {
        "plan_id": flow.plan_id,
        "fingerprint": flow.fingerprint,
        "preview_safe": dict(flow.preview_safe),
    }


def _from_payload(
    owner: str,
    payload: dict[str, Any],
    *,
    generation: int = 0,
    revision: int = 0,
) -> _Flow | None:
    plan_id = str(payload.get("plan_id") or "")
    fingerprint = str(payload.get("fingerprint") or "")
    preview = _safe_preview(payload.get("preview_safe"))
    if (
        not _PLAN_ID_RE.fullmatch(plan_id)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or preview is None
    ):
        return None
    return _Flow(
        owner=owner, plan_id=plan_id, fingerprint=fingerprint,
        preview_safe=preview, updated_at=time.monotonic(),
        generation=max(0, int(generation or 0)), revision=max(0, int(revision or 0)),
    )


def _begin(owner: str) -> AgentContextWriteGuard:
    if _repository is None:
        return AgentContextWriteGuard(generation=0, revision=0)
    begin = getattr(_repository, "begin_context", None)
    if not callable(begin):
        return AgentContextWriteGuard(generation=0, revision=0)
    return begin(owner=owner, context_type=_CONTEXT_TYPE)


def _begin_update(
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
                previous = _from_payload(
                    owner, persisted.payload,
                    generation=persisted.generation,
                    revision=persisted.revision,
                )
                if previous is not None:
                    with _lock:
                        _flows[owner] = previous
            return previous, guard
    return _flow(owner), _begin(owner)


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
                    payload=_payload(flow),
                    expires_at=time.time() + _TTL_SECONDS,
                    guard=AgentContextWriteGuard(flow.generation, flow.revision),
                )
                if persisted is None:
                    return False
                flow.generation = persisted.generation
                flow.revision = persisted.revision
            else:
                _repository.replace_latest(
                    owner=flow.owner, context_type=_CONTEXT_TYPE,
                    payload=_payload(flow), expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:
            logger.warning("Agent 光鸭残留清理上下文保存失败 type=%s", type(exc).__name__)
            return False
    with _lock:
        for owner in [
            owner for owner, item in _flows.items()
            if time.monotonic() - item.updated_at > _TTL_SECONDS
        ]:
            _flows.pop(owner, None)
        _flows[flow.owner] = flow
    return True


def _flow(owner: str) -> _Flow | None:
    owner = str(owner or "").strip()
    if not owner:
        return None
    if _repository is not None:
        try:
            persisted = _repository.get_latest(
                owner=owner, context_type=_CONTEXT_TYPE, now=time.time()
            )
        except Exception as exc:
            logger.warning("Agent 光鸭残留清理上下文恢复失败 type=%s", type(exc).__name__)
            return None
        if persisted is None:
            return None
        restored = _from_payload(
            owner, persisted.payload,
            generation=persisted.generation, revision=persisted.revision,
        )
        if restored is None:
            return None
        with _lock:
            _flows[owner] = restored
        return restored
    with _lock:
        current = _flows.get(owner)
        if current is None or time.monotonic() - current.updated_at > _TTL_SECONDS:
            _flows.pop(owner, None)
            return None
        return current


def _consume(flow: _Flow) -> bool:
    if _repository is not None:
        try:
            consume = getattr(_repository, "consume_latest_guarded", None)
            if callable(consume) and flow.generation > 0 and flow.revision > 0:
                consumed = bool(consume(
                    owner=flow.owner, context_type=_CONTEXT_TYPE,
                    guard=AgentContextWriteGuard(flow.generation, flow.revision),
                ))
            elif not callable(consume):
                consumed = bool(_repository.delete_latest(
                    owner=flow.owner, context_type=_CONTEXT_TYPE
                ))
            else:
                consumed = False
        except Exception as exc:
            logger.warning("Agent 光鸭残留清理上下文消费失败 type=%s", type(exc).__name__)
            return False
        if not consumed:
            return False
    with _lock:
        _flows.pop(flow.owner, None)
    return True


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, GuangYaCleanupPlanStale):
        return AgentToolError(str(exc), code="confirmation_stale")
    if isinstance(exc, GuangYaCleanupPlanError):
        return AgentToolError(str(exc), code="precondition_failed")
    logger.warning("光鸭残留清理失败 type=%s", type(exc).__name__)
    return AgentToolError("暂时无法检查光鸭整理残留，请稍后重试", code="unavailable")


def _configured_cleanup_sources() -> list[dict[str, str]]:
    sources = list(_configured_sources())
    rules = OfflineRules.from_config()
    extras = [(rules.target_dir_id, rules.target_dir_name or "光鸭离线执行目录")]
    if rules.secondary_enabled:
        extras.append((rules.secondary_dir_id, rules.secondary_dir_name or "光鸭二次分流目录"))
    for source_id, name in extras:
        normalized = str(source_id or "").strip()
        if normalized and normalized != "0" and all(
            str(item.get("id") or "") != normalized for item in sources
        ):
            sources.append({"id": normalized, "name": str(name or "光鸭执行目录")})
    return sources


def guangya_cleanup_preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("残留清理参数必须是对象")
    if set(arguments) - {"max_candidates"}:
        raise AgentToolError("残留清理包含不支持的参数")
    try:
        maximum = int(arguments.get("max_candidates", 200))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentToolError("max_candidates 必须是整数") from exc
    if not 1 <= maximum <= 500:
        raise AgentToolError("max_candidates 必须在 1 到 500 之间")
    return {"max_candidates": maximum}


def guangya_cleanup_execute_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("guangya.organize.cleanup.execute 不接受参数")
    return {}


def _projection(plan: dict[str, Any]) -> dict[str, Any]:
    stats = plan.get("stats") if isinstance(plan.get("stats"), dict) else {}
    samples = [
        sample for sample in (
            sanitize_public_text(item, limit=255)
            for item in list(plan.get("samples") or [])[:5]
        ) if sample
    ]
    return {
        **{key: max(0, int(stats.get(key) or 0)) for key in _PREVIEW_KEYS},
        "sample_directories": samples,
        "cloud_write": False,
        "quarantine_instead_of_delete": True,
    }


def preview_guangya_cleanup(
    arguments: dict[str, Any], context: ToolContext,
) -> ToolResult:
    if not context.owner:
        raise AgentToolError("残留清理需要已登录会话", code="precondition_failed")
    previous, guard = _begin_update(context.owner)
    sources = _configured_cleanup_sources()
    client = GuangYaClient()
    try:
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        plan = build_cleanup_plan(client, owner=context.owner, sources=sources, **arguments)
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        client.close()
    preview = _projection(plan)
    flow = _Flow(
        owner=context.owner, plan_id=str(plan["plan_id"]),
        fingerprint=str(plan["fingerprint"]), preview_safe=preview,
        generation=guard.generation, revision=guard.revision,
    )
    if not _save(flow):
        discard_cleanup_plan(flow.plan_id)
        raise AgentToolError("残留清理预览已被更新请求取代，请重新生成", code="precondition_failed")
    if previous is not None and previous.plan_id != flow.plan_id:
        discard_cleanup_plan(previous.plan_id)
    empty_count = int(preview["empty_dir_count"])
    residual_count = int(preview["residual_dir_count"])
    total = empty_count + residual_count
    return ToolResult(
        True,
        "ready" if total else "no_changes",
        (
            f"发现 {empty_count} 个真空目录、{residual_count} 个可隔离垃圾残留目录"
            if total else "整理来源中没有可安全清理的空目录或垃圾残留目录"
        ),
        data=preview,
        evidence=[Evidence(
            "guangya_snapshot",
            "已保护整理来源根；空目录要求可验证版本，非空目录仅接受小型、无视频且全部属于严格垃圾允许集的目录。",
            _now(),
        )],
        suggestions=(
            ["如预览无误，可以确认执行：空目录进入回收站，非空垃圾残留整体移入隔离区。"]
            if total else ["包含海报、NFO、字幕、压缩包、种子或未知文件的目录会被保留。"]
        ),
    )


def _confirmation_fingerprint(flow: _Flow, plan: dict[str, Any]) -> str:
    return confirmation_context_fingerprint({
        "owner": flow.owner,
        "plan_id": flow.plan_id,
        "plan_fingerprint": flow.fingerprint,
        "credential_generation": int(plan.get("credential_generation") or 0),
        "empty_dir_count": int(flow.preview_safe.get("empty_dir_count") or 0),
        "residual_dir_count": int(flow.preview_safe.get("residual_dir_count") or 0),
    }, domain="guangya-cleanup-confirmation")


def prepare_guangya_cleanup_confirmation(
    _arguments: dict[str, Any], context: ToolContext,
) -> tuple[ToolResult, str]:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError("最近残留清理预览不存在或已过期", code="precondition_failed")
    try:
        plan = load_cleanup_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint
        )
        client = GuangYaClient()
        try:
            if (
                not client.logged_in
                or int(client.credential_generation) != int(plan["credential_generation"])
            ):
                raise GuangYaCleanupPlanStale("光鸭登录凭据已变化，请重新预览")
        finally:
            client.close()
    except Exception as exc:
        raise _public_error(exc) from exc
    empty_count = int(flow.preview_safe.get("empty_dir_count") or 0)
    residual_count = int(flow.preview_safe.get("residual_dir_count") or 0)
    if empty_count + residual_count <= 0:
        raise AgentToolError("最近预览没有可执行的清理对象", code="precondition_failed")
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将处理 {empty_count + residual_count} 个光鸭整理残留目录",
        data={**flow.preview_safe, "effects": [
            "整理来源根目录永远不会被删除或移动。",
            "真空目录会在版本与空目录双重复核后移入光鸭回收站。",
            "非空垃圾残留不会永久删除，而是整体移入 /MediaFlux隔离/整理残留 的独立批次。",
            "任何视频、海报/NFO/字幕/压缩包/种子、未知文件或快照变化都会阻止对应目录进入计划。",
            "执行明细保存在私有审计清单中，公开状态只返回聚合计数。",
        ]},
        evidence=[Evidence(
            "guangya_cleanup_plan", "已核对冻结计划、当前凭据世代和计划签名。", _now()
        )],
    ), _confirmation_fingerprint(flow, plan)


def execute_guangya_cleanup(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("光鸭残留清理必须先预览并确认", code="confirmation_required")


def execute_guangya_cleanup_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext,
) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError("残留清理预览已过期，请重新生成", code="confirmation_stale")
    try:
        plan = load_cleanup_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint
        )
        if _confirmation_fingerprint(flow, plan) != str(expected_context or ""):
            raise AgentToolError("残留清理预览已变化，请重新预检", code="confirmation_stale")
        confirmed = confirm_cleanup_plan(
            flow.plan_id, owner=context.owner, expected_fingerprint=flow.fingerprint
        )
        from app.modules.organize_tasks import get_organize_manager

        task = get_organize_manager().start_durable_operation(
            "光鸭整理残留清理",
            "已确认空目录与垃圾残留清理计划",
            job_kind="agent_guangya_cleanup",
            owner=context.owner,
            payload={
                "version": 1,
                "plan_id": flow.plan_id,
                "plan_fingerprint": flow.fingerprint,
                "owner_digest": organize_operation_owner_digest(context.owner),
                "credential_generation": int(confirmed["credential_generation"]),
            },
            dedupe_key=f"agent-guangya-cleanup:{flow.plan_id}:{flow.fingerprint}",
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    if not task.get("ok"):
        raise AgentToolError(
            "光鸭整理队列当前不可用，清理预览已保留，请稍后重新确认",
            code="precondition_failed",
        )
    consumed = _consume(flow)
    task_id = str(task.get("task_id") or "")
    reference = (
        organize_operation_public_ref(task_id)
        if re.fullmatch(r"[0-9a-f]{32}", task_id) else ""
    )
    queued = bool(task.get("queued"))
    return ToolResult(
        True,
        "accepted",
        "光鸭整理残留清理已排队" if queued else "光鸭整理残留清理任务已启动",
        data={
            "queued": queued,
            "queue_position": max(0, int(task.get("queue_position") or 0)),
            "replayed": bool(task.get("replayed")),
            "operation_ref": reference,
            "empty_dir_count": int(flow.preview_safe.get("empty_dir_count") or 0),
            "residual_dir_count": int(flow.preview_safe.get("residual_dir_count") or 0),
            "requires_manual": not consumed,
        },
        evidence=[Evidence(
            "organize_queue",
            "冻结计划已提交到可恢复的光鸭写入队列；公开结果不包含文件 ID、来源路径或隔离批次位置。",
            _now(),
        )],
        suggestions=[
            "可以稍后查询光鸭整理状态查看回收、隔离和失败数量。",
            *(["会话状态未能可靠消费，请勿重复提交同一计划。"] if not consumed else []),
        ],
    )


def execute_durable_guangya_cleanup_job(
    payload: dict[str, Any], *, cancel_check=None,
) -> dict[str, Any]:
    return execute_cleanup_plan(payload, cancel_check=cancel_check)
