"""光鸭目录/文件刮削的 owner-bound inspect/search/preview/confirmed run。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import re
import threading
import time
from typing import Any, Callable

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.clients.guangya import GuangYaClient
from app.modules.guangya_workspace import resolve_workspace_path
from app.agent.result_projection import sanitize_public_text
from app.agent.session_context import (
    AgentContextWriteGuard,
    AgentSessionContextRepository,
)
from app.modules.directory_scrape import get_directory_scrape_service
from app.repositories.organize_operation_jobs import organize_operation_public_ref
from app.modules.directory_scrape_errors import (
    DirectoryScrapeGoneError,
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
    scope_type: str = ""
    scope_id: str = ""
    inspection_id: str = ""
    candidates: list[dict[str, Any]] | None = None
    preview_id: str = ""
    preview_safe: dict[str, Any] | None = None
    selected_candidate: dict[str, Any] | None = None
    preview_arguments: dict[str, Any] | None = None
    updated_at: float = 0.0
    generation: int = 0
    revision: int = 0


_lock = threading.RLock()
_flows: dict[str, _Flow] = {}
_CONTEXT_TYPE = "directory_scrape"
_repository: AgentSessionContextRepository | None = None
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_directory_scrape_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    """配置跨 Worker 的目录刮削短期上下文仓储。"""
    global _repository
    _repository = repository


def reset_directory_scrape_context_for_tests() -> None:
    global _repository
    with _lock:
        _flows.clear()
    _repository = None


def clear_directory_scrape_context(
    *, owner: str, delete_persisted: bool = True,
) -> bool:
    owner_key = str(owner or "").strip()
    if not owner_key:
        return False
    with _lock:
        removed = _flows.pop(owner_key, None) is not None
    if delete_persisted and _repository is not None:
        try:
            removed = bool(_repository.delete_latest(
                owner=owner_key, context_type=_CONTEXT_TYPE,
            )) or removed
        except Exception as exc:
            logger.warning(
                "Agent 光鸭刮削上下文清理失败 type=%s", type(exc).__name__
            )
    return removed


def _candidate_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    number = value.get("candidate_number")
    provider = str(value.get("provider") or "").strip().casefold()
    external_id = str(value.get("external_id") or "").strip()
    tmdb_id = str(value.get("tmdb_id") or "").strip()
    media_type = str(value.get("media_type") or "").strip().casefold()
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or not 1 <= number <= 10
        or provider not in {"tmdb", "metatube"}
        or not _ID_RE.fullmatch(external_id)
        or media_type not in {"movie", "tv"}
        or (provider == "tmdb" and not (tmdb_id or external_id).isdigit())
    ):
        return None
    try:
        score = float(value.get("score") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= score <= 1:
        return None
    return {
        "candidate_number": number,
        "provider": provider,
        "external_id": external_id,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": sanitize_public_text(value.get("title"), limit=160) or "未命名候选",
        "year": sanitize_public_text(value.get("year"), limit=12),
        "score": round(score, 3),
    }


def _preview_safe_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    candidate_number = value.get("candidate_number")
    if isinstance(candidate_number, bool) or not isinstance(candidate_number, int):
        return None
    result = {
        "candidate_number": candidate_number,
        "candidate_title": sanitize_public_text(value.get("candidate_title"), limit=160),
        "candidate_year": sanitize_public_text(value.get("candidate_year"), limit=12),
        "plan_count": max(0, int(value.get("plan_count") or 0)),
        "companion_count": max(0, int(value.get("companion_count") or 0)),
        "conflict_count": max(0, int(value.get("conflict_count") or 0)),
        "cloud_write": False,
    }
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, dict):
        return None
    result["actions"] = {
        str(action): max(0, int(count or 0))
        for action, count in raw_actions.items()
        if str(action) in _SAFE_PLAN_ACTIONS | {"other"}
        and isinstance(count, int) and not isinstance(count, bool)
    }
    return result


def _candidate_execution_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in (
            "candidate_number", "provider", "external_id", "tmdb_id", "media_type"
        )
    }


def _preview_execution_guard(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "candidate_number": value.get("candidate_number"),
        "plan_count": value.get("plan_count"),
        "companion_count": value.get("companion_count"),
        "conflict_count": value.get("conflict_count"),
        "actions": dict(value.get("actions") or {}),
    }


def _durable_flow_payload(item: _Flow) -> dict[str, Any]:
    """仅持久化重建写计划所需字段，不保存搜索列表、标题或短期 store ID。"""
    return {
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "selected_candidate": _candidate_execution_payload(item.selected_candidate),
        "preview_arguments": dict(item.preview_arguments) if item.preview_arguments else None,
        "preview_guard": _preview_execution_guard(item.preview_safe),
    }


def _durable_flow_from_payload(owner: str, payload: dict[str, Any]) -> _Flow | None:
    scope_type = str(payload.get("scope_type") or "").strip().casefold()
    scope_id = str(payload.get("scope_id") or "").strip()
    if (
        scope_type not in {"directory", "file"}
        or not _ID_RE.fullmatch(scope_id)
        or (scope_type == "directory" and scope_id == "0")
    ):
        return None
    raw_candidate = payload.get("selected_candidate")
    candidate = _candidate_payload(raw_candidate)
    if candidate is None:
        return None
    try:
        arguments = directory_scrape_preview_arguments(
            dict(payload.get("preview_arguments") or {})
        )
    except AgentToolError:
        return None
    raw_guard = payload.get("preview_guard")
    if not isinstance(raw_guard, dict):
        return None
    synthetic_safe = {
        "candidate_number": raw_guard.get("candidate_number"),
        "candidate_title": "",
        "candidate_year": "",
        "plan_count": raw_guard.get("plan_count"),
        "companion_count": raw_guard.get("companion_count"),
        "conflict_count": raw_guard.get("conflict_count"),
        "cloud_write": False,
        "actions": raw_guard.get("actions"),
    }
    try:
        preview_safe = _preview_safe_payload(synthetic_safe)
    except (TypeError, ValueError, OverflowError):
        return None
    if preview_safe is None:
        return None
    return _Flow(
        owner=owner,
        scope_type=scope_type,
        scope_id=scope_id,
        candidates=[],
        selected_candidate=candidate,
        preview_arguments=arguments,
        preview_safe=preview_safe,
    )


def _flow_payload(item: _Flow) -> dict[str, Any]:
    return {
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "inspection_id": item.inspection_id,
        "candidates": [dict(candidate) for candidate in (item.candidates or [])[:10]],
        "preview_id": item.preview_id,
        "preview_safe": dict(item.preview_safe) if item.preview_safe else None,
        "selected_candidate": dict(item.selected_candidate) if item.selected_candidate else None,
        "preview_arguments": dict(item.preview_arguments) if item.preview_arguments else None,
    }


def _flow_from_payload(
    owner: str,
    payload: dict[str, Any],
    *,
    generation: int = 0,
    revision: int = 0,
) -> _Flow | None:
    scope_type = str(payload.get("scope_type") or "").strip().casefold()
    scope_id = str(payload.get("scope_id") or "").strip()
    if (
        scope_type not in {"directory", "file"}
        or not _ID_RE.fullmatch(scope_id)
        or (scope_type == "directory" and scope_id == "0")
    ):
        return None
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates[:10]:
        candidate = _candidate_payload(raw)
        if candidate is None or candidate["candidate_number"] != len(candidates) + 1:
            return None
        candidates.append(candidate)
    selected = None
    if payload.get("selected_candidate") is not None:
        selected = _candidate_payload(payload.get("selected_candidate"))
        if selected is None:
            return None
    preview_arguments = None
    if payload.get("preview_arguments") is not None:
        try:
            preview_arguments = directory_scrape_preview_arguments(
                dict(payload.get("preview_arguments") or {})
            )
        except AgentToolError:
            return None
    preview_safe = None
    if payload.get("preview_safe") is not None:
        try:
            preview_safe = _preview_safe_payload(payload.get("preview_safe"))
        except (TypeError, ValueError, OverflowError):
            return None
        if preview_safe is None:
            return None
    inspection_id = str(payload.get("inspection_id") or "").strip()
    preview_id = str(payload.get("preview_id") or "").strip()
    if (inspection_id and not _ID_RE.fullmatch(inspection_id)) or (
        preview_id and not _ID_RE.fullmatch(preview_id)
    ):
        return None
    return _Flow(
        owner=str(owner), scope_type=scope_type, scope_id=scope_id,
        inspection_id=inspection_id, candidates=candidates, preview_id=preview_id,
        preview_safe=preview_safe, selected_candidate=selected,
        preview_arguments=preview_arguments, updated_at=time.monotonic(),
        generation=max(0, int(generation or 0)),
        revision=max(0, int(revision or 0)),
    )


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
            logger.warning(
                "Agent 光鸭刮削上下文恢复失败 type=%s", type(exc).__name__
            )
            with _lock:
                _flows.pop(owner_key, None)
            return None
        if persisted is None:
            with _lock:
                _flows.pop(owner_key, None)
            return None
        if callable(getattr(_repository, "replace_latest_guarded", None)) and (
            persisted.generation <= 0 or persisted.revision <= 0
        ):
            with _lock:
                _flows.pop(owner_key, None)
            return None
        restored = _flow_from_payload(
            owner_key, persisted.payload,
            generation=persisted.generation, revision=persisted.revision,
        )
        if restored is None:
            with _lock:
                _flows.pop(owner_key, None)
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


def _begin_flow(owner: str) -> AgentContextWriteGuard:
    if _repository is None:
        return AgentContextWriteGuard(generation=0, revision=0)
    begin = getattr(_repository, "begin_context", None)
    if not callable(begin):
        return AgentContextWriteGuard(generation=0, revision=0)
    return begin(owner=owner, context_type=_CONTEXT_TYPE)


def _save(item: _Flow) -> bool:
    item.updated_at = time.monotonic()
    if _repository is not None:
        try:
            guarded = getattr(_repository, "replace_latest_guarded", None)
            if callable(guarded):
                if item.generation <= 0:
                    with _lock:
                        _flows.pop(item.owner, None)
                    return False
                persisted = guarded(
                    owner=item.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(item),
                    expires_at=time.time() + _TTL_SECONDS,
                    guard=AgentContextWriteGuard(
                        generation=item.generation, revision=item.revision,
                    ),
                )
                if persisted is None:
                    with _lock:
                        _flows.pop(item.owner, None)
                    return False
                item.generation = persisted.generation
                item.revision = persisted.revision
            else:
                _repository.replace_latest(
                    owner=item.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(item),
                    expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:
            logger.warning(
                "Agent 光鸭刮削上下文持久化失败 type=%s", type(exc).__name__
            )
            with _lock:
                _flows.pop(item.owner, None)
            return False
    with _lock:
        expired = [owner for owner, row in _flows.items() if time.monotonic() - row.updated_at > _TTL_SECONDS]
        for owner in expired:
            _flows.pop(owner, None)
        _flows[item.owner] = item
    return True


def _consume_flow(item: _Flow) -> bool:
    if _repository is not None:
        consume = getattr(_repository, "consume_latest_guarded", None)
        try:
            if callable(consume) and item.generation > 0 and item.revision > 0:
                consumed = bool(consume(
                    owner=item.owner,
                    context_type=_CONTEXT_TYPE,
                    guard=AgentContextWriteGuard(
                        generation=item.generation, revision=item.revision,
                    ),
                ))
            elif not callable(consume):
                consumed = bool(_repository.delete_latest(
                    owner=item.owner, context_type=_CONTEXT_TYPE,
                ))
            else:
                consumed = False
        except Exception as exc:
            logger.warning(
                "Agent 光鸭刮削上下文消费失败 type=%s", type(exc).__name__
            )
            return False
        if not consumed:
            return False
    with _lock:
        _flows.pop(item.owner, None)
    return True


def _restore_consumed_flow(item: _Flow) -> bool:
    """仅在队列明确拒绝且世代未变化时恢复可重试 flow。"""
    item.revision = 0
    return _save(item)


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, DirectoryScrapePublicError):
        return AgentToolError(public_error_message(exc), code="precondition_failed")
    return AgentToolError("光鸭目录刮削当前不可用", code="precondition_failed")


def _ensure_inspection(service: Any, owner: str, flow: _Flow) -> str:
    if flow.inspection_id:
        try:
            service.store.get_inspection(owner, flow.inspection_id)
            return flow.inspection_id
        except DirectoryScrapeGoneError:
            pass
    if not flow.scope_id or flow.scope_type not in {"directory", "file"}:
        raise AgentToolError("最近刮削检查无法恢复，请重新检查", code="precondition_failed")
    payload = (
        service.inspect(owner, flow.scope_id)
        if flow.scope_type == "directory"
        else service.inspect_file(owner, flow.scope_id)
    )
    inspection_id = str(payload.get("inspection_id") or "").strip()
    if not inspection_id:
        raise AgentToolError("最近刮削检查无法恢复，请重新检查", code="precondition_failed")
    flow.inspection_id = inspection_id
    if not _save(flow):
        raise AgentToolError("刮削流程已被更新请求取代，请重新检查", code="precondition_failed")
    return inspection_id


def _preview_payload(
    service: Any,
    owner: str,
    flow: _Flow,
    candidate: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    inspection_id = _ensure_inspection(service, owner, flow)
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
    return service.preview(
        owner,
        inspection_id,
        tmdb_id,
        candidate["media_type"],
        **kwargs,
    )


def _safe_preview(
    payload: dict[str, Any], candidate: dict[str, Any],
) -> dict[str, Any]:
    plans = payload.get("plans") if isinstance(payload.get("plans"), list) else []
    companions = (
        payload.get("companion_plans")
        if isinstance(payload.get("companion_plans"), list)
        else []
    )
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
    return {
        "candidate_number": candidate["candidate_number"],
        "candidate_title": candidate["title"],
        "candidate_year": candidate["year"],
        "plan_count": len(plans),
        "companion_count": len(companions),
        "conflict_count": conflicts,
        "actions": actions,
        "cloud_write": False,
    }


def directory_scrape_inspect_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    keys = set(arguments)
    if keys not in ({"directory_id"}, {"file_id"}, {"path"}):
        raise AgentToolError("必须且只能提供 path、directory_id 或 file_id 其中一个")
    if "path" in arguments:
        path = str(arguments.get("path") or "").strip().replace("\\", "/")
        if not path.startswith("/") or len(path) > 2048:
            raise AgentToolError("path 必须是光鸭绝对路径")
        parts = [part for part in path.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise AgentToolError("path 不能是根目录或相对路径")
        return {"path": "/" + "/".join(parts)}
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
    try:
        guard = _begin_flow(context.owner)
        service = get_directory_scrape_service()
        resolved_arguments = dict(arguments)
        if "path" in resolved_arguments:
            client = GuangYaClient()
            try:
                target = resolve_workspace_path(
                    client, resolved_arguments.pop("path")
                )
            finally:
                client.close()
            resolved_arguments = {
                "directory_id" if target.is_dir else "file_id": str(target.file_id)
            }
        payload = (
            service.inspect(context.owner, resolved_arguments["directory_id"])
            if "directory_id" in resolved_arguments
            else service.inspect_file(context.owner, resolved_arguments["file_id"])
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    flow = _Flow(
        owner=context.owner,
        scope_type="directory" if "directory_id" in resolved_arguments else "file",
        scope_id=str(
            resolved_arguments.get("directory_id")
            or resolved_arguments.get("file_id")
            or ""
        ),
        inspection_id=str(payload.get("inspection_id") or ""),
        candidates=[],
        updated_at=time.monotonic(),
        generation=guard.generation,
        revision=guard.revision,
    )
    if not _save(flow):
        raise AgentToolError("本次刮削检查已被更新请求取代，请重新检查", code="precondition_failed")
    query = sanitize_public_text(payload.get("suggested_query"), limit=120)
    return ToolResult(
        True,
        "attention" if payload.get("requires_manual_match") else "completed",
        "光鸭刮削检查完成，可继续搜索匹配" if query else "光鸭刮削检查完成，需要手动提供搜索词",
        data={
            "scope_type": "directory" if "directory_id" in resolved_arguments else "file",
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
        inspection_id = _ensure_inspection(service, context.owner, flow)
        raw = service.search(
            context.owner,
            inspection_id,
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
        candidate = _candidate_payload({
            "candidate_number": len(internal) + 1,
            "provider": provider,
            "external_id": internal_id,
            "tmdb_id": str(item.get("tmdb_id") or ""),
            "media_type": str(item.get("media_type") or "movie"),
            "title": title,
            "year": sanitize_public_text(item.get("year"), limit=12),
            "score": score,
        })
        if candidate is None:
            continue
        internal.append(candidate)
        public.append({key: value for key, value in candidate.items() if key not in {"external_id", "tmdb_id"}})
    flow.candidates = internal
    flow.preview_id = ""
    flow.preview_safe = None
    flow.selected_candidate = None
    flow.preview_arguments = None
    if not _save(flow):
        raise AgentToolError("本次匹配搜索已被更新请求取代，请重新搜索", code="precondition_failed")
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
    try:
        payload = _preview_payload(
            get_directory_scrape_service(), context.owner, flow, candidate, arguments,
        )
    except Exception as exc:
        raise _public_error(exc) from exc
    safe = _safe_preview(payload, candidate)
    flow.preview_id = str(payload.get("preview_id") or "")
    flow.preview_safe = safe
    flow.selected_candidate = dict(candidate)
    flow.preview_arguments = dict(arguments)
    if not _save(flow):
        raise AgentToolError("本次刮削预览已被更新请求取代，请重新生成", code="precondition_failed")
    return ToolResult(
        True,
        "completed",
        f"刮削预览已生成：{safe['plan_count']} 个视频计划，{safe['companion_count']} 个伴随文件计划",
        data={**safe, "continuation_available": bool(flow.preview_id), "limits": {"ttl_seconds": int(_TTL_SECONDS)}},
        evidence=[Evidence(
            "guangya_preview",
            "仅生成 dry-run 预览；未返回文件名、路径、云盘对象 ID 或目标目录 ID，未执行云盘写入。",
            _now(),
        )],
        suggestions=["如确认预览无误，可请求：执行刚才的刮削预览。"],
    )


def _ensure_preview_for_run(service: Any, owner: str, flow: _Flow) -> Any:
    if flow.preview_id:
        try:
            return service.store.get_preview(owner, flow.preview_id)
        except DirectoryScrapeGoneError:
            pass
    if not flow.selected_candidate or not flow.preview_arguments or not flow.preview_safe:
        raise AgentToolError("刮削预览已过期，请重新生成", code="confirmation_stale")
    payload = _preview_payload(
        service, owner, flow, flow.selected_candidate, flow.preview_arguments,
    )
    rebuilt_safe = _safe_preview(payload, flow.selected_candidate)
    if rebuilt_safe != flow.preview_safe:
        raise AgentToolError("刮削预览已变化，请重新预检", code="confirmation_stale")
    flow.preview_id = str(payload.get("preview_id") or "").strip()
    if not flow.preview_id:
        raise AgentToolError("刮削预览无法恢复，请重新生成", code="confirmation_stale")
    if not _save(flow):
        raise AgentToolError("刮削预览已变化，请重新生成", code="confirmation_stale")
    return service.store.get_preview(owner, flow.preview_id)


def _run_fingerprint(flow: _Flow, record: Any) -> str:
    if bool(record.claimed):
        raise AgentToolError("刮削预览正在执行或已被领取", code="confirmation_stale")
    inspection = getattr(record, "inspection", None)
    rules = getattr(record, "rules", None)
    return confirmation_context_fingerprint(
        {
            "scope": [flow.scope_type, flow.scope_id],
            "inspection_fingerprint": str(
                getattr(inspection, "fingerprint", "") or ""
            ),
            "signature": repr(record.signature),
            "target_snapshot": repr(getattr(record, "target_snapshot", ())),
            "rules": asdict(rules) if rules is not None else {},
            "candidate": _candidate_execution_payload(flow.selected_candidate),
            "preview_arguments": flow.preview_arguments,
            "preview_safe": _preview_execution_guard(flow.preview_safe),
        },
        domain="guangya-directory-scrape-run",
    )


def execute_durable_directory_scrape_job(
    payload: dict[str, Any], *, cancel_check: Callable[[], None] | None = None
) -> dict[str, Any]:
    """从持久化确认快照重建预览并执行；不依赖原进程内存。"""
    if not isinstance(payload, dict) or set(payload) != {
        "version", "execution_owner", "expected_context", "flow",
    }:
        raise ValueError("持久化刮削任务参数无效")
    if payload.get("version") != 1:
        raise ValueError("持久化刮削任务版本无效")
    execution_owner = str(payload.get("execution_owner") or "").strip().casefold()
    expected_context = str(payload.get("expected_context") or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", execution_owner) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_context
    ):
        raise ValueError("持久化刮削任务身份无效")
    raw_flow = payload.get("flow")
    if not isinstance(raw_flow, dict):
        raise ValueError("持久化刮削任务上下文无效")
    flow = _durable_flow_from_payload(execution_owner, raw_flow)
    if (
        flow is None
        or not flow.selected_candidate
        or not flow.preview_arguments
        or not flow.preview_safe
    ):
        raise ValueError("持久化刮削任务上下文无效")

    # inspection/preview ID 只在创建它们的进程内有效；重启后必须根据
    # 已确认的 scope、候选与参数重新生成，并再次核对完整 fingerprint。
    flow.inspection_id = ""
    flow.preview_id = ""
    if cancel_check is not None:
        cancel_check()
    service = get_directory_scrape_service()
    inspection = (
        service.inspect(execution_owner, flow.scope_id)
        if flow.scope_type == "directory"
        else service.inspect_file(execution_owner, flow.scope_id)
    )
    flow.inspection_id = str(inspection.get("inspection_id") or "").strip()
    if not flow.inspection_id:
        raise ValueError("持久化刮削检查无法恢复")
    candidate = flow.selected_candidate
    arguments = flow.preview_arguments
    preview_kwargs: dict[str, Any] = {
        "season": arguments.get("season"),
        "episode": arguments.get("episode"),
        "numbering_mode": arguments["numbering_mode"],
    }
    if candidate["provider"] == "metatube":
        preview_kwargs.update(
            provider="metatube", external_id=candidate["external_id"]
        )
        tmdb_id = ""
    else:
        tmdb_id = candidate["tmdb_id"] or candidate["external_id"]
    if cancel_check is not None:
        cancel_check()
    preview = service.preview(
        execution_owner, flow.inspection_id, tmdb_id, candidate["media_type"],
        **preview_kwargs,
    )
    rebuilt_safe = _safe_preview(preview, flow.selected_candidate)
    if _preview_execution_guard(rebuilt_safe) != _preview_execution_guard(flow.preview_safe):
        raise AgentToolError("刮削预览已变化，请重新预检", code="confirmation_stale")
    flow.preview_safe = rebuilt_safe
    flow.preview_id = str(preview.get("preview_id") or "").strip()
    record = service.store.get_preview(execution_owner, flow.preview_id)
    if _run_fingerprint(flow, record) != expected_context:
        raise AgentToolError("刮削预览已变化，请重新预检", code="confirmation_stale")
    if cancel_check is not None:
        cancel_check()
    if cancel_check is None:
        return service.execute_preview(execution_owner, flow.preview_id)
    return service.execute_preview(
        execution_owner, flow.preview_id, cancel_check=cancel_check
    )


def prepare_run_directory_scrape(
    _arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    flow = _flow(context.owner)
    if flow is None or not flow.preview_id or not flow.preview_safe:
        raise AgentToolError("最近刮削预览不存在或已过期，请重新生成预览", code="precondition_failed")
    try:
        service = get_directory_scrape_service()
        record = _ensure_preview_for_run(service, context.owner, flow)
        fingerprint = _run_fingerprint(flow, record)
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


def run_directory_scrape_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None or not flow.preview_id:
        raise AgentToolError("刮削预览已过期，请重新生成", code="confirmation_stale")
    try:
        service = get_directory_scrape_service()
        record = _ensure_preview_for_run(service, context.owner, flow)
        if _run_fingerprint(flow, record) != str(expected_context or ""):
            raise AgentToolError("刮削预览已变化，请重新预检", code="confirmation_stale")
        # durable queue 不持久化目录名称；公开状态仅显示固定操作标签。
        reference = "已确认目录"
        if not _consume_flow(flow):
            raise AgentToolError("刮削预览已变化或已被消费，请重新生成", code="confirmation_stale")
        from app.modules.organize_tasks import get_organize_manager

        execution_owner = confirmation_context_fingerprint(
            {"owner": context.owner}, domain="guangya-directory-scrape-job-owner",
        )
        task = get_organize_manager().start_durable_operation(
            "目录刮削",
            reference,
            job_kind="agent_directory_scrape",
            owner=context.owner,
            payload={
                "version": 1,
                "execution_owner": execution_owner,
                "expected_context": str(expected_context or "").strip().casefold(),
                "flow": _durable_flow_payload(flow),
            },
            dedupe_key=f"agent-directory-scrape:{context.owner}:{expected_context}",
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    if not task.get("ok"):
        restored = _restore_consumed_flow(flow)
        message = (
            "光鸭整理队列当前不可用，预览已保留，请稍后重新确认"
            if restored
            else "光鸭整理队列当前不可用，请重新生成预览"
        )
        raise AgentToolError(message, code="precondition_failed")
    queued = bool(task.get("queued"))
    internal_task_id = str(task.get("task_id") or "").strip()
    operation_ref = (
        organize_operation_public_ref(internal_task_id)
        if re.fullmatch(r"[0-9a-f]{32}", internal_task_id) else ""
    )
    return ToolResult(
        True,
        "accepted",
        "目录刮削已排队" if queued else "目录刮削任务已启动",
        data={
            "queued": queued,
            "queue_position": max(0, int(task.get("queue_position") or 0)),
            "replayed": bool(task.get("replayed")),
            "operation_ref": operation_ref,
            "plan_count": int((flow.preview_safe or {}).get("plan_count") or 0),
        },
        evidence=[Evidence("organize_queue", "已提交到可恢复的光鸭整理互斥队列；仅返回可查询的操作 ID，不返回文件名或路径。", _now())],
        suggestions=["可稍后查看光鸭整理任务状态。"],
    )
