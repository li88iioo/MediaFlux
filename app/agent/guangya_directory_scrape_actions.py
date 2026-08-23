"""光鸭目录/文件刮削的 owner-bound inspect/search/preview/confirmed run。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import threading
import time
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.modules.directory_scrape import get_directory_scrape_service
from app.modules.directory_scrape_errors import (
    DirectoryScrapePublicError,
    public_error_message,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_ALLOWED_TYPES = {"auto", "movie", "tv"}
_ALLOWED_NUMBERING = {"auto", "absolute", "season", "merged_cour"}
_TTL_SECONDS = 600.0
_SAFE_PLAN_ACTIONS = {"move", "skip", "conflict"}


@dataclass
class _Flow:
    owner: str
    inspection_id: str = ""
    candidates: list[dict[str, Any]] | None = None
    preview_id: str = ""
    preview_safe: dict[str, Any] | None = None
    updated_at: float = 0.0


_lock = threading.RLock()
_flows: dict[str, _Flow] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _flow(owner: str) -> _Flow | None:
    with _lock:
        item = _flows.get(str(owner or ""))
        if item is None or time.monotonic() - item.updated_at > _TTL_SECONDS:
            _flows.pop(str(owner or ""), None)
            return None
        return item


def _save(item: _Flow) -> None:
    item.updated_at = time.monotonic()
    with _lock:
        expired = [owner for owner, row in _flows.items() if time.monotonic() - row.updated_at > _TTL_SECONDS]
        for owner in expired:
            _flows.pop(owner, None)
        _flows[item.owner] = item


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, DirectoryScrapePublicError):
        return AgentToolError(public_error_message(exc), code="precondition_failed")
    return AgentToolError("光鸭目录刮削当前不可用", code="precondition_failed")


def directory_scrape_inspect_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    keys = set(arguments)
    if keys not in ({"directory_id"}, {"file_id"}):
        raise AgentToolError("必须且只能提供 directory_id 或 file_id 其中一个")
    key = "directory_id" if "directory_id" in arguments else "file_id"
    value = str(arguments[key] or "").strip()
    if not _ID_RE.fullmatch(value) or (key == "directory_id" and value == "0"):
        raise AgentToolError(f"{key} 格式无效")
    return {key: value}


def directory_scrape_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"query", "media_type", "year"}:
        raise AgentToolError("只接受 query、media_type 和 year")
    query = sanitize_public_text(arguments.get("query"), limit=120)
    media_type = str(arguments.get("media_type") or "auto").strip().casefold()
    year = str(arguments.get("year") or "").strip()
    if media_type not in _ALLOWED_TYPES:
        raise AgentToolError("media_type 仅支持 auto、movie 或 tv")
    if year and (not year.isdigit() or len(year) != 4):
        raise AgentToolError("year 必须是 4 位年份")
    result: dict[str, Any] = {"media_type": media_type}
    if query:
        result["query"] = query
    if year:
        result["year"] = year
    return result


def directory_scrape_preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"candidate_number", "season", "episode", "numbering_mode"}:
        raise AgentToolError("只接受 candidate_number、season、episode 和 numbering_mode")
    candidate = arguments.get("candidate_number")
    if isinstance(candidate, bool) or not isinstance(candidate, int) or not 1 <= candidate <= 10:
        raise AgentToolError("candidate_number 必须是 1 到 10 的整数")
    result: dict[str, Any] = {"candidate_number": int(candidate)}
    if "season" in arguments:
        season = arguments["season"]
        if isinstance(season, bool) or not isinstance(season, int) or not 0 <= season <= 99:
            raise AgentToolError("season 必须是 0 到 99 的整数")
        result["season"] = int(season)
    if "episode" in arguments:
        episode = arguments["episode"]
        if isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 999:
            raise AgentToolError("episode 必须是 1 到 999 的整数")
        result["episode"] = int(episode)
    mode = str(arguments.get("numbering_mode") or "auto").strip().casefold()
    if mode not in _ALLOWED_NUMBERING:
        raise AgentToolError("numbering_mode 无效")
    result["numbering_mode"] = mode
    return result


def directory_scrape_run_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("guangya.directory_scrape.run 不接受参数")
    return {}


def inspect_directory_scrape(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.owner:
        raise AgentToolError("目录刮削需要已登录会话", code="precondition_failed")
    service = get_directory_scrape_service()
    try:
        payload = (
            service.inspect(context.owner, arguments["directory_id"])
            if "directory_id" in arguments
            else service.inspect_file(context.owner, arguments["file_id"])
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    flow = _Flow(owner=context.owner, inspection_id=str(payload.get("inspection_id") or ""), candidates=[], updated_at=time.monotonic())
    _save(flow)
    query = sanitize_public_text(payload.get("suggested_query"), limit=120)
    return ToolResult(
        True,
        "attention" if payload.get("requires_manual_match") else "completed",
        "光鸭刮削检查完成，可继续搜索匹配" if query else "光鸭刮削检查完成，需要手动提供搜索词",
        data={
            "scope_type": "directory" if "directory_id" in arguments else "file",
            "media_type": str(payload.get("media_type") or "unknown"),
            "suggested_query": query,
            "season": payload.get("season"),
            "episode": payload.get("episode"),
            "requires_manual_match": bool(payload.get("requires_manual_match")),
            "manual_match_reason": sanitize_public_text(payload.get("manual_match_reason"), limit=160),
            "counts": {str(key): max(0, int(value or 0)) for key, value in counts.items() if isinstance(value, (int, float)) and not isinstance(value, bool)},
            "continuation_available": True,
            "limits": {"ttl_seconds": int(_TTL_SECONDS)},
        },
        evidence=[Evidence(
            "guangya_inspection",
            "已按现有整理规则只读检查目标；未返回云盘对象 ID、文件名或路径，未移动、重命名或删除文件。",
            _now(),
        )],
        suggestions=["可继续说：搜索匹配。"],
    )


def search_directory_scrape(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None or not flow.inspection_id:
        raise AgentToolError("最近刮削检查不存在或已过期，请先检查目录或文件", code="precondition_failed")
    service = get_directory_scrape_service()
    try:
        raw = service.search(
            context.owner,
            flow.inspection_id,
            str(arguments.get("query") or ""),
            arguments["media_type"],
            str(arguments.get("year") or ""),
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    internal: list[dict[str, Any]] = []
    public: list[dict[str, Any]] = []
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "tmdb").strip().casefold()
        internal_id = str(item.get("external_id") or item.get("tmdb_id") or "").strip()
        title = sanitize_public_text(item.get("title"), limit=160) or "未命名候选"
        if provider not in {"tmdb", "metatube"} or not internal_id:
            continue
        try:
            score = round(max(0.0, min(float(item.get("score") or 0), 1.0)), 3)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        candidate = {
            "candidate_number": len(internal) + 1,
            "provider": provider,
            "external_id": internal_id,
            "tmdb_id": str(item.get("tmdb_id") or ""),
            "media_type": str(item.get("media_type") or "movie"),
            "title": title,
            "year": sanitize_public_text(item.get("year"), limit=12),
            "score": score,
        }
        internal.append(candidate)
        public.append({key: value for key, value in candidate.items() if key not in {"external_id", "tmdb_id"}})
    flow.candidates = internal
    flow.preview_id = ""
    flow.preview_safe = None
    _save(flow)
    return ToolResult(
        True,
        "completed" if public else "empty",
        f"找到 {len(public)} 个刮削匹配候选" if public else "没有找到可用的刮削匹配候选",
        data={"candidates": public, "candidate_count": len(public), "limits": {"max_candidates": 10, "ttl_seconds": int(_TTL_SECONDS)}},
        evidence=[Evidence(
            "metadata_search",
            "候选查询不会写入映射或云盘；候选内部 ID 仅保存在当前会话短期上下文。",
            _now(),
        )],
        suggestions=["可回复：预览第 1 个。"] if public else [],
    )


def preview_directory_scrape(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None or not flow.inspection_id or not flow.candidates:
        raise AgentToolError("最近刮削候选不存在或已过期，请先搜索匹配", code="precondition_failed")
    candidate = next((item for item in flow.candidates if item["candidate_number"] == arguments["candidate_number"]), None)
    if candidate is None:
        raise AgentToolError("刮削候选序号不存在", code="precondition_failed")
    kwargs: dict[str, Any] = {
        "season": arguments.get("season"),
        "episode": arguments.get("episode"),
        "numbering_mode": arguments["numbering_mode"],
    }
    if candidate["provider"] == "metatube":
        kwargs.update(provider="metatube", external_id=candidate["external_id"])
        tmdb_id = ""
    else:
        tmdb_id = candidate["tmdb_id"] or candidate["external_id"]
    try:
        payload = get_directory_scrape_service().preview(
            context.owner,
            flow.inspection_id,
            tmdb_id,
            candidate["media_type"],
            **kwargs,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    plans = payload.get("plans") if isinstance(payload.get("plans"), list) else []
    companions = payload.get("companion_plans") if isinstance(payload.get("companion_plans"), list) else []
    actions: dict[str, int] = {}
    conflicts = 0
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        raw_action = str(plan.get("action") or "").strip().casefold()
        action = raw_action if raw_action in _SAFE_PLAN_ACTIONS else "other"
        actions[action] = actions.get(action, 0) + 1
        if str(plan.get("conflict_decision") or "") not in {"", "none", "keep"}:
            conflicts += 1
    safe = {
        "candidate_number": candidate["candidate_number"],
        "candidate_title": candidate["title"],
        "candidate_year": candidate["year"],
        "plan_count": len(plans),
        "companion_count": len(companions),
        "conflict_count": conflicts,
        "actions": actions,
        "cloud_write": False,
    }
    flow.preview_id = str(payload.get("preview_id") or "")
    flow.preview_safe = safe
    _save(flow)
    return ToolResult(
        True,
        "completed",
        f"刮削预览已生成：{len(plans)} 个视频计划，{len(companions)} 个伴随文件计划",
        data={**safe, "continuation_available": bool(flow.preview_id), "limits": {"ttl_seconds": int(_TTL_SECONDS)}},
        evidence=[Evidence(
            "guangya_preview",
            "仅生成 dry-run 预览；未返回文件名、路径、云盘对象 ID 或目标目录 ID，未执行云盘写入。",
            _now(),
        )],
        suggestions=["如确认预览无误，可请求：执行刚才的刮削预览。"],
    )


def _run_fingerprint(owner: str, preview_id: str) -> str:
    service = get_directory_scrape_service()
    record = service.store.get_preview(owner, preview_id)
    return confirmation_context_fingerprint(
        {
            "preview_id": preview_id,
            "created_at": record.created_at,
            "signature": repr(record.signature),
            "claimed": bool(record.claimed),
        },
        domain="guangya-directory-scrape-run",
    )


def prepare_run_directory_scrape(
    _arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    flow = _flow(context.owner)
    if flow is None or not flow.preview_id or not flow.preview_safe:
        raise AgentToolError("最近刮削预览不存在或已过期，请重新生成预览", code="precondition_failed")
    try:
        fingerprint = _run_fingerprint(context.owner, flow.preview_id)
    except Exception as exc:
        raise _public_error(exc) from exc
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将执行刚才的刮削预览，共 {flow.preview_safe['plan_count']} 个视频计划",
        data={**flow.preview_safe, "effects": [
            "会按确认过的固定计划移动和重命名选中范围内的云盘文件。",
            "执行前会再次核对整理规则、来源内容、目标内容和计划签名；变化时拒绝执行。",
            "任务会进入现有光鸭整理队列，不在确认请求中同步完成。",
        ]},
        evidence=[Evidence("guangya_preview", "已核对当前会话的短期预览仍然存在且未被领取。", _now())],
    ), fingerprint


def run_directory_scrape(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("目录刮削执行必须先预检并确认", code="confirmation_required")


def run_directory_scrape_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None or not flow.preview_id:
        raise AgentToolError("刮削预览已过期，请重新生成", code="confirmation_stale")
    try:
        if _run_fingerprint(context.owner, flow.preview_id) != str(expected_context or ""):
            raise AgentToolError("刮削预览已变化，请重新预检", code="confirmation_stale")
        service = get_directory_scrape_service()
        reference = service.preview_reference(context.owner, flow.preview_id)
        from app.modules.organize_tasks import get_organize_manager

        task = get_organize_manager().start_operation(
            "目录刮削",
            reference,
            lambda: service.execute_preview(context.owner, flow.preview_id),
            queue_if_busy=True,
            dedupe_key=f"agent-directory-scrape:{context.owner}:{flow.preview_id}",
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    if not task.get("ok"):
        raise AgentToolError("光鸭整理队列当前不可用，请稍后重试", code="precondition_failed")
    with _lock:
        _flows.pop(context.owner, None)
    queued = bool(task.get("queued"))
    return ToolResult(
        True,
        "accepted",
        "目录刮削已排队" if queued else "目录刮削任务已启动",
        data={
            "queued": queued,
            "queue_position": max(0, int(task.get("queue_position") or 0)),
            "replayed": bool(task.get("replayed")),
            "plan_count": int((flow.preview_safe or {}).get("plan_count") or 0),
        },
        evidence=[Evidence("organize_queue", "已提交到现有光鸭整理互斥队列；响应不包含任务 ID、文件名或路径。", _now())],
        suggestions=["可稍后查看光鸭整理任务状态。"],
    )
