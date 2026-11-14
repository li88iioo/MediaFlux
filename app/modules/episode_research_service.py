"""季集研究的默认关闭入口、预算、证据缓存及写前重新核验。"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import threading
import time
from typing import Any

from app import config
from app.concurrency import ensure_sync_bridge_available
from app.concurrency import KeyedSingleFlight
from app.modules.episode_research import (
    POLICY_VERSION,
    EpisodeEvidenceReader,
    EpisodeResearchError,
    normalize_case,
)

_TOTAL_TIMEOUT_SECONDS = 95.0

_flights = KeyedSingleFlight(max_entries=64)
_running = threading.BoundedSemaphore(2)
_RECEIPT_KEYS = frozenset({"version", "cache_key", "candidate_index", "tmdb_id", "group_id", "group_fingerprint", "mapping_digest"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _remaining(deadline_at: float) -> float:
    # 浮点减法可能比总预算多出极小尾差，传给等待器前硬性钳制上限。
    remaining = min(_TOTAL_TIMEOUT_SECONDS, deadline_at - time.monotonic())
    if remaining <= 0:
        raise EpisodeResearchError("research_timeout")
    return remaining


def _reader_with_deadline(case: dict, factory, deadline_at: float):
    _remaining(deadline_at)
    reader = (factory or EpisodeEvidenceReader)(case)
    try:
        reader.limit_deadline(deadline_at)
    except Exception:
        reader.close()
        raise
    return reader


def episode_research_enabled(*, explicit_approval: bool = False) -> bool:
    """显式批准仅免除主动复核开关；复杂研究及 Agent/LLM 依赖仍须启用。"""
    return bool(
        config.get_bool("AGENT_EPISODE_RESEARCH_ENABLED", False)
        and (explicit_approval is True or config.get_bool("AGENT_RECOGNITION_REVIEW_ENABLED", False))
        and config.get_bool("AGENT_ENABLED", False)
        and config.get_bool("AGENT_LLM_ENABLED", False)
        and (config.get("AGENT_LLM_API_URL", "") or "").strip()
        and (config.get("AGENT_LLM_MODEL", "") or "").strip()
    )


def _outcome(reason: str, *, status: str = "abstained") -> dict[str, Any]:
    return {"status": status, "proposal": None, "reason_code": reason, "tool_calls": 0, "duration_ms": 0, "model": ""}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def proposal_receipt(proposal: object) -> dict:
    if not isinstance(proposal, dict) or type(proposal.get("version")) is not int or proposal.get("version") != POLICY_VERSION or proposal.get("status") != "verified":
        raise EpisodeResearchError("invalid_research_proposal")
    result = {"version": POLICY_VERSION, "cache_key": proposal.get("case_key"),
              "candidate_index": proposal.get("candidate_index"), "tmdb_id": proposal.get("tmdb_id"),
              "group_id": proposal.get("group_id"), "group_fingerprint": proposal.get("group_fingerprint"),
              "mapping_digest": _digest(proposal.get("mappings"))}
    _validate_receipt_shape(result)
    return result


def _validate_receipt_shape(receipt: object) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise EpisodeResearchError("invalid_research_receipt")
    if type(receipt["version"]) is not int or receipt["version"] != POLICY_VERSION:
        raise EpisodeResearchError("research_policy_changed")
    if type(receipt["candidate_index"]) is not int or not 0 <= receipt["candidate_index"] < 3:
        raise EpisodeResearchError("invalid_research_receipt")
    for key in ("cache_key", "group_fingerprint", "mapping_digest"):
        if not isinstance(receipt[key], str) or not _HEX.fullmatch(receipt[key]):
            raise EpisodeResearchError("invalid_research_receipt")
    if not isinstance(receipt["tmdb_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,11}", receipt["tmdb_id"]):
        raise EpisodeResearchError("invalid_research_receipt")
    if not isinstance(receipt["group_id"], str) or not re.fullmatch(r"[0-9a-fA-F]{24}", receipt["group_id"]):
        raise EpisodeResearchError("invalid_research_receipt")


def _check_scope(payload: dict, *, explicit_approval: bool = False) -> None:
    if str(payload.get("kind") or "guangya").lower() != "guangya":
        raise EpisodeResearchError("unsupported_research_scope", "当前季集研究只用于光鸭冻结待确认案例")
    if not episode_research_enabled(explicit_approval=explicit_approval):
        raise EpisodeResearchError("episode_research_disabled", "复杂季集研究或主动复核已关闭")


def revalidate_episode_research_receipt(
    payload: dict, receipt: dict, *, expected_candidate_index: int | None = None,
    explicit_approval: bool = False, reader_factory=None, deadline_at: float | None = None,
) -> dict:
    """重新核验缓存、原始文件、所选身份和当前TMDB证据；receipt 本身不授写权。

    explicit_approval 仅由已批准的宿主执行链传入，允许主动复核关闭后重验原映射，
    不绕过复杂研究开关、其它运行依赖或任何证据与时限校验。
    """
    from app.repositories.episode_research import (
        get_episode_research_cache,
        invalidate_episode_research_cache,
    )
    deadline_at = time.monotonic() + _TOTAL_TIMEOUT_SECONDS if deadline_at is None else deadline_at
    _remaining(deadline_at)
    _check_scope(payload, explicit_approval=explicit_approval)
    _validate_receipt_shape(receipt)
    case = normalize_case(payload)
    if case["case_key"] != receipt["cache_key"]:
        raise EpisodeResearchError("research_case_changed")
    if expected_candidate_index is not None and receipt["candidate_index"] != expected_candidate_index:
        raise EpisodeResearchError("research_candidate_changed")
    stored = get_episode_research_cache(case["case_key"], policy_version=POLICY_VERSION)
    if stored is None or stored["status"] != "verified":
        raise EpisodeResearchError("research_evidence_expired")
    prior = stored["payload"].get("proposal")
    if not isinstance(prior, dict) or proposal_receipt(prior) != receipt:
        raise EpisodeResearchError("research_receipt_mismatch")
    reader = _reader_with_deadline(case, reader_factory, deadline_at)
    try:
        index, group_id = receipt["candidate_index"], receipt["group_id"]
        reader.inspect_candidate(index)
        reader.list_groups(index)
        reader.read_group(index, group_id)
        current = reader.validate(index, group_id)
        if proposal_receipt(current) != receipt:
            invalidate_episode_research_cache(case["case_key"])
            raise EpisodeResearchError("research_evidence_changed", "季集顺序或目标元数据已变化，需重新研究")
        _check_scope(payload, explicit_approval=explicit_approval)
        _remaining(deadline_at)
        return current
    finally:
        reader.close()


def _cached_outcome(payload: dict, case: dict, *, reader_factory=None, deadline_at: float) -> dict | None:
    from app.repositories.episode_research import (
        get_episode_research_cache,
        invalidate_episode_research_cache,
    )
    stored = get_episode_research_cache(case["case_key"], policy_version=POLICY_VERSION)
    if stored is None:
        return None
    if stored["status"] == "negative":
        reason = stored["payload"].get("reason_code")
        return _outcome(reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,80}", reason) else "research_cached_abstention") | {"cached": True}
    if stored["status"] != "verified":
        return None
    try:
        receipt = proposal_receipt(stored["payload"].get("proposal"))
        current = revalidate_episode_research_receipt(payload, receipt, reader_factory=reader_factory, deadline_at=deadline_at)
        return {"status": "verified", "proposal": current, "receipt": receipt, "reason_code": "episode_group_proven", "cached": True, "tool_calls": 0, "duration_ms": 0, "model": ""}
    except EpisodeResearchError as exc:
        if exc.code in {"research_timeout", "tmdb_unavailable", "episode_research_disabled"}:
            return _outcome(exc.code, status="failed" if exc.code == "research_timeout" else "abstained")
        invalidate_episode_research_cache(case["case_key"])
        return None


def _cache_failed_attempt(case_key: str, reason: str) -> None:
    # 在模型调用失败后仍可能已计费；短时抑制同包重试，不退款、不保存异常原文。
    if not episode_research_enabled():
        return
    try:
        from app.repositories.episode_research import put_episode_research_cache
        put_episode_research_cache(case_key, {"reason_code": reason}, status="negative", ttl_seconds=60, policy_version=POLICY_VERSION)
    except Exception:
        pass  # 数据库故障仍失败关闭；不可为了错误缓存掩盖原失败。


def research_confirmation_episodes(payload: dict, *, runner=None, reader_factory=None) -> dict:
    """后台同步入口；同包合并、每日案例预算和并发上限都不能被模型绕过。"""
    deadline_at = time.monotonic() + _TOTAL_TIMEOUT_SECONDS
    from app.repositories import agent_web_search as agent_web_search_repo
    from app.repositories.episode_research import put_episode_research_cache
    try:
        _check_scope(payload)
        ensure_sync_bridge_available()
        case = normalize_case(payload)
    except EpisodeResearchError as exc:
        return _outcome(exc.code)
    except Exception:
        return _outcome("research_unavailable", status="failed")
    lease = _flights.reserve(case["case_key"])
    try:
        if not lease.tracked:
            return _outcome("research_capacity")
        if not lease.owner and not _flights.wait(lease, timeout=_remaining(deadline_at)):
            return _outcome("research_wait_timeout")
        cached = _cached_outcome(payload, case, reader_factory=reader_factory, deadline_at=deadline_at)
        if cached is not None:
            return cached
        if not lease.owner:
            return _outcome("research_owner_failed")
        if not _running.acquire(blocking=False):
            return _outcome("research_capacity")
        try:
            _check_scope(payload)
            try:
                limit = int(config.get("AGENT_EPISODE_RESEARCH_DAILY_LIMIT", "10"))
            except (TypeError, ValueError):
                limit = 10
            limit = min(100, max(1, limit))
            if not agent_web_search_repo.reserve_agent_web_search_credits(provider="episode_research", usage_date=agent_web_search_repo.current_agent_web_search_usage_date(), cost=1, daily_limit=limit):
                return _outcome("research_daily_budget")
            if runner is None:
                from app.modules.agent_episode_research import (
                    research_episode_case_async,
                )
                runner = research_episode_case_async
            reader = _reader_with_deadline(case, reader_factory, deadline_at)
            handed_off = False
            try:
                async def run_owned():
                    nonlocal handed_off
                    operation = runner(payload, reader=reader)
                    if not inspect.isawaitable(operation):
                        raise EpisodeResearchError("invalid_research_runner")
                    handed_off = True
                    # runner/其串行工作线程接管reader，含取消后的延后关闭。
                    return await operation
                async def run():
                    return await asyncio.wait_for(run_owned(), timeout=_remaining(deadline_at))
                result = asyncio.run(run())
            finally:
                if not handed_off:
                    reader.close()
            _check_scope(payload)
            _remaining(deadline_at)
            if not isinstance(result, dict):
                raise EpisodeResearchError("invalid_research_result")
            if result.get("status") == "verified":
                proposal = result.get("proposal")
                receipt = proposal_receipt(proposal)
                if receipt["cache_key"] != case["case_key"]:
                    raise EpisodeResearchError("research_case_changed")
                put_episode_research_cache(case["case_key"], {"proposal": proposal}, status="verified", ttl_seconds=86400, policy_version=POLICY_VERSION)
                return {**result, "receipt": receipt, "cached": False}
            reason = result.get("reason_code")
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
                reason = "research_abstained"
            put_episode_research_cache(case["case_key"], {"reason_code": reason}, status="negative", ttl_seconds=300, policy_version=POLICY_VERSION)
            return {**_outcome(reason), "tool_calls": result.get("tool_calls", 0), "duration_ms": result.get("duration_ms", 0), "model": result.get("model", "")}
        finally:
            _running.release()
    except EpisodeResearchError as exc:
        if lease.owner:
            _cache_failed_attempt(case["case_key"], exc.code)
        return _outcome(exc.code)
    except TimeoutError:
        if lease.owner:
            _cache_failed_attempt(case["case_key"], "research_timeout")
        return _outcome("research_timeout", status="failed")
    except Exception:
        # 不把网络异常字符串、模型原文或可能带凭据的URL写入持久审计。
        if lease.owner:
            _cache_failed_attempt(case["case_key"], "research_runtime_error")
        return _outcome("research_runtime_error", status="failed")
    finally:
        _flights.finish(lease)
