"""待确认媒体识别的短生命周期 Agent 只读复核。

该模块复用生产 Agent Kernel 的唯一 MODEL -> TOOL 循环，但使用独立的内存
SessionState、受限只读工具目录和单次会话。模型只能提出候选选择；真正的文件
操作仍由 ``organize_confirmations`` 的冻结快照执行器完成。
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any

from app import config
from app.agent.kernel import (
    AgentEventType,
    AgentInput,
    AgentSession,
    CapabilityRetriever,
    InMemorySessionStateStore,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
    ToolPipeline,
)
from app.agent.kernel.pipeline import ToolPipelineError
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import SessionLimits
from app.logger import get_logger
from app.modules.scraper import TMDBScraper
from app.sensitive_data import contains_sensitive_credential

logger = get_logger(__name__)

_MIN_APPROVAL_CONFIDENCE = 0.92
_MAX_FILES = 80
_MAX_CANDIDATES = 3

_SYSTEM_PROMPT = """
你是 MediaFlux 内部媒体识别复核器。你只处理当前冻结的待确认案例，不与用户聊天。

必须遵守：
1. 先调用 recognition.inspect_case；不得凭文件名印象直接决定。
2. 对可能选择的候选调用 recognition.inspect_candidate。
3. 剧集必须再按案例中出现的每个季调用 recognition.inspect_season，并核对每个集号真实存在。
4. 只能从冻结候选中选择；不得新建候选、修改季集号、猜测缺失集号或绕过规则。
5. 只有标题/别名、年份、媒体类型和全部季集边界形成一致证据时才 approve；否则 abstain。
6. 最后必须且只能调用一次 recognition.propose_review_decision。该工具只记录建议，不执行写操作。
7. 不输出思维过程；工具接受决定后只用一句短句结束。
""".strip()


@dataclass(frozen=True, slots=True)
class RecognitionReviewDecision:
    status: str
    candidate_index: int | None = None
    confidence: float = 0.0
    reason_code: str = ""
    summary: str = ""
    model: str = ""
    tool_calls: int = 0
    duration_ms: int = 0
    failure_code: str = ""

    @property
    def approved(self) -> bool:
        return self.status == "approved" and self.candidate_index is not None

    def audit_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("status", None)
        # 模型自由文本只在本次短生命周期内使用；审计只保存结构化结果，
        # 避免把解释、文件名或潜在思维过程带入长期数据库。
        payload.pop("summary", None)
        return payload


class RecognitionReviewUnavailable(RuntimeError):
    """当前模型配置不具备主动复核条件。"""


def recognition_review_enabled() -> bool:
    return bool(
        config.get_bool("AGENT_ENABLED", False)
        and config.get_bool("AGENT_LLM_ENABLED", False)
        and config.get_bool("AGENT_RECOGNITION_REVIEW_ENABLED", False)
        and str(config.get("AGENT_LLM_API_URL", "") or "").strip()
        and str(config.get("AGENT_LLM_MODEL", "") or "").strip()
    )


def _safe_leaf(value: object, fallback: str = "") -> str:
    text = str(value or "").strip().replace("\\", "/")
    return PurePath(text).name[:500] if text else fallback


def _candidate_provider(candidate: dict[str, Any]) -> str:
    provider = str(candidate.get("provider") or "").strip().lower()
    if not provider and str(candidate.get("tmdb_id") or "").strip():
        return "tmdb"
    return provider


def _candidate_summary(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    try:
        score = max(0.0, min(float(candidate.get("score", 0.0) or 0.0), 1.0))
    except (TypeError, ValueError, OverflowError):
        score = 0.0
    return {
        "index": index,
        "title": str(candidate.get("title") or "").strip()[:300],
        "year": str(candidate.get("year") or "").strip()[:8],
        "media_type": str(candidate.get("media_type") or "").strip().lower(),
        "provider": _candidate_provider(candidate),
        "tmdb_id": str(candidate.get("tmdb_id") or "").strip()[:32],
        "score": round(score, 4),
        "support": max(0, int(candidate.get("support") or 0)),
        "genre_ids": [
            int(item) for item in list(candidate.get("genre_ids") or [])[:20]
            if str(item).isdigit()
        ],
    }


def _detail_projection(detail: dict[str, Any], *, media_type: str) -> dict[str, Any]:
    genres = []
    for item in list(detail.get("genres") or [])[:12]:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            genres.append(str(item.get("name") or "").strip()[:80])
    return {
        "found": bool(detail),
        "id": str(detail.get("id") or ""),
        "media_type": media_type,
        "title": str(detail.get("name") or detail.get("title") or "").strip()[:300],
        "original_title": str(
            detail.get("original_name") or detail.get("original_title") or ""
        ).strip()[:300],
        "date": str(
            detail.get("first_air_date") or detail.get("release_date") or ""
        ).strip()[:16],
        "status": str(detail.get("status") or "").strip()[:80],
        "origin_country": [str(item)[:8] for item in list(detail.get("origin_country") or [])[:8]],
        "genres": genres,
        "number_of_seasons": detail.get("number_of_seasons"),
        "number_of_episodes": detail.get("number_of_episodes"),
        "overview": str(detail.get("overview") or "").strip()[:800],
    }


def _season_projection(detail: dict[str, Any], *, season: int) -> dict[str, Any]:
    episodes: list[int] = []
    for item in list(detail.get("episodes") or [])[:500]:
        if not isinstance(item, dict):
            continue
        number = item.get("episode_number")
        if isinstance(number, bool):
            continue
        try:
            normalized = int(number)
        except (TypeError, ValueError, OverflowError):
            continue
        if normalized not in episodes:
            episodes.append(normalized)
    return {
        "found": bool(detail),
        "season": season,
        "name": str(detail.get("name") or "").strip()[:200],
        "episode_count": len(episodes),
        "episode_numbers": episodes,
    }


def _normalized_title(value: object) -> str:
    return "".join(
        character.casefold()
        for character in str(value or "").strip()
        if character.isalnum()
    )


def _normalized_reason_code(value: object) -> str:
    normalized = re.sub(
        r"[^a-z0-9_.:-]+", "_", str(value or "").strip().casefold()
    ).strip("_.:-")
    return normalized[:80] or "unspecified"


def _validate_index(value: object, candidates: list[dict[str, Any]]) -> int:
    if isinstance(value, bool):
        raise TypeError("候选序号无效")
    index = int(value)
    if index < 0 or index >= len(candidates):
        raise ValueError("候选序号不在冻结范围内")
    return index


def _model_visible_payload_is_safe(
    payload: dict[str, Any], candidates: list[dict[str, Any]]
) -> bool:
    """仅扫描将发给外部模型的文字，内部 ID 和绝对路径不会外发。"""
    values: list[object] = [
        payload.get("identity"),
        payload.get("reason"),
        _safe_leaf(payload.get("directory")),
    ]
    values.extend(
        _safe_leaf(item.get("name"))
        for item in list(payload.get("files") or [])[:_MAX_FILES]
        if isinstance(item, dict)
    )
    for candidate in candidates:
        values.extend(
            (
                candidate.get("title"),
                candidate.get("year"),
                candidate.get("provider"),
            )
        )
    return not any(
        contains_sensitive_credential(value)
        for value in values
        if str(value or "").strip()
    )


async def _review_async(payload: dict[str, Any]) -> RecognitionReviewDecision:
    started = time.monotonic()
    candidates = [
        dict(item) for item in list(payload.get("candidates") or [])[:_MAX_CANDIDATES]
        if isinstance(item, dict)
    ]
    if not candidates:
        return RecognitionReviewDecision(
            status="abstained",
            reason_code="no_candidate",
            summary="冻结案例没有可选择候选",
        )
    if not _model_visible_payload_is_safe(payload, candidates):
        return RecognitionReviewDecision(
            status="abstained",
            reason_code="unsafe_input",
            summary="识别材料疑似包含凭据，已保留人工确认",
        )

    settings = ProviderSettings.from_config()
    scraper = TMDBScraper()
    proposed: dict[str, Any] = {}
    case_inspected = False
    inspected_candidates: dict[int, bool] = {}
    inspected_details: dict[int, dict[str, Any]] = {}
    inspected_seasons: dict[tuple[int, int], set[int]] = {}
    parsed_positions: list[tuple[int | None, int | None]] = []

    def case_files() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        parent = str(payload.get("directory") or "")
        for item in list(payload.get("files") or [])[:_MAX_FILES]:
            if not isinstance(item, dict):
                continue
            name = _safe_leaf(item.get("name"), "未命名文件")
            season = item.get("season")
            episode = item.get("episode")
            try:
                season = None if season in (None, "") else int(season)
            except (TypeError, ValueError, OverflowError):
                season = None
            try:
                episode = None if episode in (None, "") else int(episode)
            except (TypeError, ValueError, OverflowError):
                episode = None
            if season is None or episode is None:
                try:
                    parsed_season, parsed_episode = scraper.parse_source_position(
                        name, parent
                    )
                except (AttributeError, TypeError, ValueError):
                    parsed_season = parsed_episode = None
                season = season if season is not None else parsed_season
                episode = episode if episode is not None else parsed_episode
            parsed_positions.append((season, episode))
            rows.append({
                "name": name,
                "season": season,
                "episode": episode,
                "size": max(0, int(item.get("size") or 0)),
            })
        return rows

    frozen_files = case_files()

    def inspect_case(_arguments: dict[str, Any], _context) -> dict[str, Any]:
        nonlocal case_inspected
        if proposed:
            raise ToolPipelineError(
                "复核决定提交后不能继续读取证据", code="decision_already_proposed"
            )
        case_inspected = True
        return {
            "ok": True,
            "status": "success",
            "summary": "已读取冻结识别案例",
            "data": {
                "kind": str(payload.get("kind") or "guangya"),
                "identity": str(payload.get("identity") or "").strip()[:300],
                "reason": str(payload.get("reason") or "").strip()[:800],
                "directory": _safe_leaf(payload.get("directory"), "根目录"),
                "file_count": len(frozen_files),
                "files": frozen_files,
                "candidates": [
                    _candidate_summary(item, index)
                    for index, item in enumerate(candidates)
                ],
                "constraints": {
                    "candidate_is_frozen": True,
                    "may_change_position": False,
                    "write_allowed": False,
                },
            },
        }

    def inspect_candidate(arguments: dict[str, Any], _context) -> dict[str, Any]:
        if proposed:
            raise ToolPipelineError(
                "复核决定提交后不能继续读取证据", code="decision_already_proposed"
            )
        index = _validate_index(arguments.get("candidate_index"), candidates)
        candidate = candidates[index]
        provider = _candidate_provider(candidate)
        media_type = str(candidate.get("media_type") or "").strip().lower()
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        if provider != "tmdb" or media_type not in {"movie", "tv"} or not tmdb_id:
            inspected_candidates[index] = False
            return {
                "ok": False,
                "status": "unsupported_candidate",
                "summary": "当前主动复核只自动确认可由 TMDB 再验证的候选",
                "data": {"candidate_index": index, "provider": provider},
            }
        detail = scraper.get_detail(tmdb_id, media_type)
        projected = _detail_projection(detail, media_type=media_type)
        inspected_candidates[index] = bool(
            projected["found"] and str(projected["id"]) == tmdb_id
        )
        inspected_details[index] = projected
        return {
            "ok": inspected_candidates[index],
            "status": "success" if inspected_candidates[index] else "not_found",
            "summary": "候选详情已核对" if inspected_candidates[index] else "候选详情不可用",
            "data": {"candidate_index": index, **projected},
        }

    def inspect_season(arguments: dict[str, Any], _context) -> dict[str, Any]:
        if proposed:
            raise ToolPipelineError(
                "复核决定提交后不能继续读取证据", code="decision_already_proposed"
            )
        index = _validate_index(arguments.get("candidate_index"), candidates)
        season = int(arguments.get("season"))
        if not 0 <= season <= 99:
            raise ValueError("季号超出允许范围")
        candidate = candidates[index]
        if (
            _candidate_provider(candidate) != "tmdb"
            or str(candidate.get("media_type") or "").strip().lower() != "tv"
        ):
            return {
                "ok": False,
                "status": "invalid_media_type",
                "summary": "该候选不是可核对季集的 TMDB 剧集",
                "data": {"candidate_index": index, "season": season},
            }
        detail = scraper.get_tv_season_detail(
            str(candidate.get("tmdb_id") or "").strip(), season
        )
        projected = _season_projection(detail, season=season)
        inspected_seasons[(index, season)] = {
            int(item) for item in projected["episode_numbers"]
        }
        return {
            "ok": bool(projected["found"]),
            "status": "success" if projected["found"] else "not_found",
            "summary": f"第 {season} 季边界已核对" if projected["found"] else f"第 {season} 季详情不可用",
            "data": {"candidate_index": index, **projected},
        }

    def propose_decision(arguments: dict[str, Any], _context) -> dict[str, Any]:
        if proposed:
            raise ToolPipelineError("复核决定已提交", code="decision_already_proposed")
        decision = str(arguments.get("decision") or "").strip().lower()
        candidate_index = int(arguments.get("candidate_index", -1))
        confidence = max(0.0, min(float(arguments.get("confidence") or 0.0), 1.0))
        reason_code = _normalized_reason_code(arguments.get("reason_code"))
        summary = str(arguments.get("summary") or "").strip()[:500]
        if decision not in {"approve", "abstain"}:
            raise ValueError("复核决定无效")
        if decision == "approve":
            _validate_index(candidate_index, candidates)
        else:
            candidate_index = -1
        proposed.update({
            "decision": decision,
            "candidate_index": candidate_index,
            "confidence": confidence,
            "reason_code": reason_code,
            "summary": summary or ("证据一致" if decision == "approve" else "证据不足"),
        })
        return {
            "ok": True,
            "status": "accepted",
            "summary": "复核建议已记录；尚未执行任何写操作",
            "data": {"decision": decision},
        }

    object_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    index_schema = {
        "type": "object",
        "properties": {"candidate_index": {"type": "integer", "minimum": 0, "maximum": 2}},
        "required": ["candidate_index"],
        "additionalProperties": False,
    }
    season_schema = {
        "type": "object",
        "properties": {
            "candidate_index": {"type": "integer", "minimum": 0, "maximum": 2},
            "season": {"type": "integer", "minimum": 0, "maximum": 99},
        },
        "required": ["candidate_index", "season"],
        "additionalProperties": False,
    }
    decision_schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "abstain"]},
            "candidate_index": {"type": "integer", "minimum": -1, "maximum": 2},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason_code": {"type": "string", "minLength": 2, "maxLength": 80},
            "summary": {"type": "string", "minLength": 2, "maxLength": 500},
        },
        "required": ["decision", "candidate_index", "confidence", "reason_code", "summary"],
        "additionalProperties": False,
    }
    specs = (
        KernelToolSpec(
            name="recognition.inspect_case",
            domain="recognition",
            description="读取当前冻结待确认案例的文件名、已解析季集和候选列表。复核必须先调用。",
            input_schema=object_schema,
            effect=ToolEffect.READ,
            examples=("查看待确认案例", "核对文件和冻结候选"),
            read=inspect_case,
        ),
        KernelToolSpec(
            name="recognition.inspect_candidate",
            domain="recognition",
            description="从 TMDB 实时读取一个冻结候选的标题、年份、类型和基本详情。",
            input_schema=index_schema,
            effect=ToolEffect.READ,
            examples=("核对候选 0", "读取候选详情"),
            read=inspect_candidate,
        ),
        KernelToolSpec(
            name="recognition.inspect_season",
            domain="recognition",
            description="读取冻结 TMDB 剧集候选某一季的逐集边界，用于证明源文件集号真实存在。",
            input_schema=season_schema,
            effect=ToolEffect.READ,
            examples=("核对候选 0 的第 1 季", "检查剧集边界"),
            read=inspect_season,
        ),
        KernelToolSpec(
            name="recognition.propose_review_decision",
            domain="recognition",
            description="提交一次结构化复核建议。approve 仅选择冻结候选；abstain 保留人工确认。不会写文件。",
            input_schema=decision_schema,
            effect=ToolEffect.READ,
            examples=("提交复核决定", "证据不足时放弃自动确认"),
            read=propose_decision,
        ),
    )
    state_store = InMemorySessionStateStore()
    catalog = ToolCatalog(specs)
    pipeline = ToolPipeline(
        catalog=catalog,
        state_store=state_store,
        projector=DefaultProjector(max_model_chars=12_000),
    )
    session = AgentSession(
        model=OpenAICompatibleModelAdapter(settings),
        catalog=catalog,
        retriever=CapabilityRetriever(minimum=4, maximum=4),
        pipeline=pipeline,
        state_store=state_store,
        journal=None,
        limits=SessionLimits(
            max_model_rounds=6,
            max_tool_calls=8,
            max_output_tokens=512,
            context_window_tokens=16_384,
        ),
        system_prompt=_SYSTEM_PROMPT,
    )
    tool_calls = 0
    failure_code = ""
    try:
        agent_input = AgentInput(
            message=(
                "复核当前待确认媒体。请按协议调用工具核对全部证据，"
                "最后提交 approve 或 abstain。"
            ),
            owner="internal-recognition-review",
            session_id=f"review-{secrets.token_urlsafe(10)}",
            channel="internal",
        )
        timeout = max(20, min(90, int(settings.timeout_seconds) * 2 + 10))

        async def consume() -> None:
            nonlocal tool_calls, failure_code
            async for event in session.run(agent_input):
                if event.type is AgentEventType.TOOL_STARTED:
                    tool_calls += 1
                elif event.type is AgentEventType.TURN_FAILED:
                    failure_code = str(event.payload.get("code") or "turn_failed")[:80]
                elif event.type is AgentEventType.TURN_CANCELLED:
                    failure_code = "cancelled"

        await asyncio.wait_for(consume(), timeout=timeout)
    finally:
        scraper.close()

    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    model_name = str(settings.model or "")[:200]
    if not proposed:
        return RecognitionReviewDecision(
            status="failed",
            reason_code="model_no_decision",
            summary="模型未提交结构化复核决定",
            model=model_name,
            tool_calls=tool_calls,
            duration_ms=elapsed_ms,
            failure_code=failure_code or "missing_decision",
        )
    if proposed["decision"] != "approve":
        return RecognitionReviewDecision(
            status="abstained",
            candidate_index=None,
            confidence=float(proposed["confidence"]),
            reason_code=str(proposed["reason_code"]),
            summary=str(proposed["summary"]),
            model=model_name,
            tool_calls=tool_calls,
            duration_ms=elapsed_ms,
        )

    index = int(proposed["candidate_index"])
    candidate = candidates[index]
    confidence = float(proposed["confidence"])
    guard_failure = ""
    if not case_inspected:
        guard_failure = "case_not_inspected"
    elif confidence < _MIN_APPROVAL_CONFIDENCE:
        guard_failure = "confidence_below_gate"
    elif _candidate_provider(candidate) != "tmdb":
        guard_failure = "provider_not_revalidated"
    elif not inspected_candidates.get(index, False):
        guard_failure = "candidate_not_revalidated"
    else:
        detail = inspected_details.get(index) or {}
        candidate_title = _normalized_title(candidate.get("title"))
        verified_titles = {
            _normalized_title(detail.get("title")),
            _normalized_title(detail.get("original_title")),
        } - {""}
        candidate_year = str(candidate.get("year") or "").strip()[:4]
        detail_year = str(detail.get("date") or "").strip()[:4]
        if not candidate_title or candidate_title not in verified_titles:
            guard_failure = "candidate_title_mismatch"
        elif candidate_year and detail_year and candidate_year != detail_year:
            guard_failure = "candidate_year_mismatch"
    if (
        not guard_failure
        and str(candidate.get("media_type") or "").strip().lower() == "tv"
    ):
        if not parsed_positions or any(
            season is None or episode is None
            for season, episode in parsed_positions
        ):
            guard_failure = "episode_position_incomplete"
        else:
            for season, episode in parsed_positions:
                assert season is not None and episode is not None
                if episode not in inspected_seasons.get((index, season), set()):
                    guard_failure = "episode_boundary_unverified"
                    break
    if guard_failure:
        return RecognitionReviewDecision(
            status="abstained",
            candidate_index=None,
            confidence=confidence,
            reason_code=guard_failure,
            summary="Agent 建议未通过确定性复核门，已保留人工确认",
            model=model_name,
            tool_calls=tool_calls,
            duration_ms=elapsed_ms,
        )
    return RecognitionReviewDecision(
        status="approved",
        candidate_index=index,
        confidence=confidence,
        reason_code=str(proposed["reason_code"]),
        summary=str(proposed["summary"]),
        model=model_name,
        tool_calls=tool_calls,
        duration_ms=elapsed_ms,
    )


def review_confirmation_payload(payload: dict[str, Any]) -> RecognitionReviewDecision:
    """同步入口，供持久化后台 Worker 调用。异常一律收敛为人工回退。"""
    if not recognition_review_enabled():
        raise RecognitionReviewUnavailable("Agent 主动复核未启用或模型未配置")
    try:
        return asyncio.run(_review_async(dict(payload)))
    except asyncio.TimeoutError:
        return RecognitionReviewDecision(
            status="failed",
            reason_code="timeout",
            summary="Agent 复核超时，已保留人工确认",
            failure_code="timeout",
        )
    except Exception as exc:  # 背景复核必须失败关闭
        logger.warning(
            "Agent 主动识别复核失败 type=%s", type(exc).__name__, exc_info=True
        )
        return RecognitionReviewDecision(
            status="failed",
            reason_code="runtime_error",
            summary="Agent 复核不可用，已保留人工确认",
            failure_code=type(exc).__name__,
        )
