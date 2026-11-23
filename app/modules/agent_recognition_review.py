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
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import PurePath
from typing import Any

from app import config
from app.agent import feature_gate
from app.agent.errors import ToolPipelineError
from app.agent.kernel import (
    AgentEventType,
    AgentInput,
    AgentSession,
    InMemorySessionStateStore,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
    ToolPipeline,
)
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
    network_idle_timeout_seconds,
)
from app.agent.kernel.session import SessionLimits
from app.logger import get_logger
from app.modules.media_identity import normalize_tmdb_id
from app.modules.nsfw import (
    MetaTubeClient,
    MetaTubeError,
    clean_nsfw_release_text,
    extract_nsfw_identifier,
    normalize_code,
)
from app.modules.nsfw_clean_review import (
    inspect_nsfw_clean_candidate,
    nsfw_clean_review_enabled,
)
from app.modules.organize_identity import _GENERIC_FILENAME_IDENTITY_HINTS
from app.modules.recognition.resolver import _resolve_explicit_tmdb_marker
from app.modules.scraper import (
    Candidate,
    TMDBScraper,
    _candidate_aliases,
    _low_information_query,
    extract_recognition_context,
    parse_release_position,
)
from app.modules.special_media import title_hint_from_path
from app.repositories.organize_confirmations import MAX_REVIEW_FILES as _MAX_FILES
from app.repositories.organize_confirmations import can_discover_candidates
from app.sensitive_data import contains_sensitive_credential

logger = get_logger(__name__)

_MIN_APPROVAL_CONFIDENCE = 0.92
_MAX_CANDIDATES = 3
_DISCOVERY_MISS_REASONS = (
    "episode_boundary_unverified", "discovery_tmdb_marker_conflict", "candidate_year_mismatch",
    "candidate_title_mismatch", "no_candidate",
)
_DISCOVERY_FALLBACK_REASONS = {"no_candidate", "candidate_title_mismatch", "candidate_year_mismatch"}
_DISCOVERY_SUMMARIES = {
    "unsafe_input": "识别材料疑似包含凭据，请移除后重试。",
    "discovery_media_type_conflict": "这组文件同时出现电影和剧集线索，请分别核对作品。",
    "discovery_year_conflict": "文件名或目录记载的年份不一致，请核对作品版本。",
    "discovery_source_title_missing": "文件名和目录缺少可用片名，请手动核对作品。",
    "discovery_source_title_conflict": "文件名指向多个作品，请分别核对后再选择。",
    "discovery_episode_position_conflict": "文件名和待确认记录中的季集不一致，请核对文件后重新整理。",
    "discovery_multi_episode_file": "这组包含多集合并文件，请手动核对并处理整段集号。",
    "discovery_tmdb_marker_conflict": "文件或目录中的 TMDB 标记与候选不一致，请核对标记和作品。",
    "episode_position_incomplete": "文件名和目录缺少明确的季集，请核对季号和集号。",
    "no_candidate": "TMDB 未查到匹配作品，请核对片名或手动选择。",
    "discovery_search_not_configured": "请检查 TMDB 搜索配置后重试。",
    "discovery_search_unavailable": "TMDB 搜索暂不可用，请稍后重试。",
    "candidate_not_revalidated": "暂时无法核验 TMDB 候选详情，请稍后重试或手动核对。",
    "candidate_title_mismatch": "TMDB 详情的片名和别名无法对应来源作品，请手动核对片名。",
    "candidate_year_mismatch": "TMDB 候选年份与来源证据不一致，请核对作品版本。",
    "discovery_ambiguous": "有多个同名 TMDB 候选符合来源证据，请手动选定作品。",
}

_SYSTEM_PROMPT = """
你是 MediaFlux 内部媒体识别复核器。你只处理当前冻结的待确认案例，不与用户聊天。

必须遵守：
1. 先调用 recognition.inspect_case；不得凭文件名印象直接决定。
2. 对可能选择的候选调用 recognition.inspect_candidate。TMDB候选核对真实详情；MetaTube必须读取所有冻结冲突候选的真实详情，比较provider:external_id、精确番号、年份/日期、演员、厂牌等；clean_title仅核对服务器清洗证据，不代表外部元数据匹配。
3. 剧集必须再按案例中出现的每个季调用 recognition.inspect_season，并核对每个集号真实存在。
4. 只能从冻结候选中选择；不得新建候选、修改季集号、猜测缺失集号或绕过规则。
5. TMDB必须有标题/别名、年份、类型及季集的一致证据；MetaTube必须核对源文件和详情的同一精确番号、冻结身份及年份，且有源文件年份/日期/演员等证据唯一排除冲突候选。同番号不同年份不能直接视为等价，不能默认选择候选0；证据不足、详情缺失或冲突未消解必须abstain。clean_title必须有已授权的光鸭成人来源、同一明确番号、无歧义分段以及保留身份的清洗变换才approve。不得把score=1当成外部查证，不能补造演员/剧情/年份；否则abstain。
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
    entry_mode: str = ""
    episode_research_receipt: dict[str, Any] | None = None
    # 仅保留 MetaTube 的结构化详情，不收模型解释或上游原始载荷。
    evidence: list[dict[str, Any]] = field(default_factory=list)
    discovered_candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.status == "approved" and self.candidate_index is not None

    def audit_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("status", None)
        payload.pop("discovered_candidates", None)
        # 默认审计不收模型自由文本；公共摘要由调用方单独脱敏、截断后保存。
        payload.pop("summary", None)
        if not self.entry_mode:
            payload.pop("entry_mode", None)
        if self.episode_research_receipt is None:
            payload.pop("episode_research_receipt", None)
        if not self.evidence:
            payload.pop("evidence", None)
        return payload


class RecognitionReviewUnavailable(RuntimeError):
    """当前模型配置不具备主动复核条件。"""


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
    result: dict[str, Any] = {
        "index": index,
        "title": str(candidate.get("title") or "").strip()[:300],
        "year": str(candidate.get("year") or "").strip()[:8],
        "media_type": str(candidate.get("media_type") or "").strip().lower(),
        "provider": _candidate_provider(candidate),
        "external_id": str(candidate.get("external_id") or "").strip()[:300],
        "tmdb_id": str(candidate.get("tmdb_id") or "").strip()[:32],
        "score": round(score, 4),
        "support": max(0, int(candidate.get("support") or 0)),
        "genre_ids": [
            int(item) for item in list(candidate.get("genre_ids") or [])[:20]
            if str(item).isdigit()
        ],
    }
    if _candidate_provider(candidate) == "metatube":
        result["evidence"] = _metatube_projection(candidate.get("metadata"))
    return result


def _metatube_projection(metadata: object) -> dict[str, Any]:
    """只投影可比对字段；不外发原始响应、图片 URL 或服务配置。"""
    raw = metadata if isinstance(metadata, dict) else {}

    def text(key: str, limit: int = 300) -> str:
        value = raw.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    release_date = ""
    try:
        release_date = date.fromisoformat(text("release_date")[:10]).isoformat()
    except ValueError:
        pass
    provider, item_id = text("provider"), text("id")
    result: dict[str, Any] = {
        "source_provider": provider,
        "external_id": f"{provider}:{item_id}" if provider and item_id else "",
        # number 必须由上游明确返回，不能采用客户端以 id 补号的回退。
        "number": normalize_code(text("number")),
        "title": text("title"),
        "release_date": release_date,
        "year": release_date[:4],
    }
    for key in ("actors", "genres"):
        values = raw.get(key) or []
        values = values if isinstance(values, (list, tuple)) else [values]
        result[key] = [value.strip()[:100] for value in values[:20]
                       if isinstance(value, str) and value.strip()]
        if key == "actors":
            result["actors_truncated"] = len(values) > 20
    for key in ("maker", "label", "director", "series"):
        result[key] = text(key)
    return result


def _metatube_source_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    rules = payload.get("rules")
    strip_domains = str(rules.get("nsfw_strip_domains") or "") if isinstance(rules, dict) else ""
    names = [_safe_leaf(payload.get("directory"))]
    names.extend(_safe_leaf(item.get("name")) for item in payload.get("files") or []
                 if isinstance(item, dict))
    codes: list[set[str]] = []
    texts: list[str] = []
    for name in names:
        remaining = clean_nsfw_release_text(name, strip_domains)
        found: set[str] = set()
        for _ in range(12):
            identifier = extract_nsfw_identifier(remaining)
            if identifier is None:
                break
            found.add(normalize_code(identifier.code))
            if not identifier.matched_text or identifier.matched_text not in remaining:
                found.add("ambiguous")
                break
            remaining = remaining.replace(identifier.matched_text, " ", 1)
        else:
            found.add("ambiguous")
        codes.append(found)
        # 移除番号后再提取年份，避免把 T38-1999 等番号的一部分当作年份。
        texts.append(remaining)
    numbers = set().union(*codes)
    consistent = len(numbers) == 1 and len(codes) > 1 and all(
        item == numbers or (not item and codes[0] == numbers) for item in codes[1:]
    )
    text = "\n".join(texts)
    dates: set[str] = set()
    for value in re.findall(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)", text):
        try:
            dates.add(date.fromisoformat(value).isoformat())
        except ValueError:
            pass
    return {
        "number": next(iter(numbers)) if consistent else "",
        "years": sorted(set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text))),
        "dates": sorted(dates),
        "text": text,
    }


def _metatube_approval_failure(
    candidates: list[dict[str, Any]], index: int,
    details: dict[int, dict[str, Any]], source: dict[str, Any],
) -> str:
    """模型只能建议；冻结身份、精确番号和冲突消歧由服务器再次校验。"""
    if not source["number"]:
        return "metatube_source_number_ambiguous"
    if len(source["years"]) > 1 or len(source["dates"]) > 1:
        return "metatube_source_date_ambiguous"
    for position, candidate in enumerate(candidates):
        if _candidate_provider(candidate) != "metatube":
            return "metatube_candidate_scope_conflict"
        provider, separator, item_id = str(candidate.get("external_id") or "").strip().partition(":")
        if (not separator or not provider or not item_id
                or str(candidate.get("media_type") or "").strip().lower() != "movie"):
            return "provider_not_revalidated"
        detail = details.get(position) or {}
        if not detail.get("found"):
            return "metatube_details_unavailable"
        if detail["external_id"] != f"{provider}:{item_id}":
            return "metatube_identity_mismatch"
        if not detail["number"]:
            return "metatube_number_unverified"
        frozen = _metatube_projection(candidate.get("metadata"))
        identifier = extract_nsfw_identifier(str(candidate.get("title") or ""))
        frozen_number = frozen["number"] or (identifier.code if identifier else "")
        frozen_year = str(candidate.get("year") or "").strip()
        if ((frozen_number and normalize_code(frozen_number) != detail["number"])
                or (frozen_year and frozen_year != detail["year"])
                or (frozen["release_date"] and frozen["release_date"] != detail["release_date"])
                or (frozen["external_id"] and frozen["external_id"] != detail["external_id"])):
            return "metatube_snapshot_mismatch"

    source_actors = {
        actor.casefold() for detail in details.values() for actor in detail.get("actors", [])
        if len(actor) >= 2 and re.search(
            r"(?<!\w)" + re.escape(actor) + r"(?!\w)", source["text"], re.IGNORECASE
        )
    }
    eligible = []
    for position, detail in details.items():
        if (detail["number"] == source["number"]
                and (not source["years"] or not detail["year"] or source["years"] == [detail["year"]])
                and (not source["dates"] or not detail["release_date"] or source["dates"] == [detail["release_date"]])
                and (not detail["actors"] or detail["actors_truncated"]
                     or source_actors.issubset({actor.casefold() for actor in detail["actors"]}))):
            eligible.append(position)
    if eligible != [index]:
        return "metatube_conflict_unresolved"
    selected = details[index]
    if (not selected["title"] or not (selected["year"] or source_actors)
            or (source["years"] and source["years"] != [selected["year"]])
            or (source["dates"] and source["dates"] != [selected["release_date"]])
            or not source_actors.issubset({actor.casefold() for actor in selected["actors"]})):
        return "metatube_evidence_insufficient"
    return ""


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
        number: Any = item.get("episode_number")
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


def _validate_index(value: Any, candidates: list[dict[str, Any]]) -> int:
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
                candidate.get("external_id"),
            )
        )
        if _candidate_provider(candidate) == "metatube":
            values.append(_metatube_projection(candidate.get("metadata")))
    return not any(
        contains_sensitive_credential(value)
        for value in values
        if str(value or "").strip()
    )


async def _review_async(payload: dict[str, Any]) -> RecognitionReviewDecision:
    started = time.monotonic()
    files = list(payload.get("files") or [])
    if len(files) > _MAX_FILES:
        return RecognitionReviewDecision(
            status="abstained",
            reason_code="case_file_limit_exceeded",
            summary=f"冻结案例包含 {len(files)} 个文件，超过自动复核上限 {_MAX_FILES}",
        )
    frozen_candidates = list(payload.get("candidates") or [])
    if len(frozen_candidates) > _MAX_CANDIDATES or any(
        not isinstance(item, dict) for item in frozen_candidates
    ):
        return RecognitionReviewDecision(
            status="abstained", reason_code="invalid_frozen_candidates",
            summary="冻结候选范围无效，已保留人工确认",
        )
    candidates = [dict(item) for item in frozen_candidates]
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

    if all(_candidate_provider(item) == "clean_title" for item in candidates):
        if not nsfw_clean_review_enabled():
            return RecognitionReviewDecision(
                status="abstained", reason_code="nsfw_clean_not_authorized",
                summary="未授权光鸭 NSFW 清洗入库，已保留人工确认",
            )
        if not any(inspect_nsfw_clean_candidate(payload, item)["ok"] for item in candidates):
            return RecognitionReviewDecision(
                status="abstained", reason_code="clean_evidence_invalid",
                summary="清洗候选证据不足，已保留人工确认",
            )
    settings = ProviderSettings.from_config()
    scraper = TMDBScraper() if any(
        _candidate_provider(item) == "tmdb" for item in candidates
    ) else None
    proposed: dict[str, Any] = {}
    case_inspected = False
    inspected_candidates: dict[int, bool] = {}
    inspected_details: dict[int, dict[str, Any]] = {}
    inspected_seasons: dict[tuple[int, int], set[int]] = {}
    parsed_positions: list[tuple[int | None, int | None]] = []
    metatube_client: MetaTubeClient | None = None
    metatube_source = _metatube_source_evidence(payload) if any(
        _candidate_provider(item) == "metatube" for item in candidates
    ) else {}

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
            if scraper is not None and (season is None or episode is None):
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
                "metatube_source": {
                    key: value for key, value in metatube_source.items() if key != "text"
                },
                "constraints": {
                    "candidate_is_frozen": True,
                    "may_change_position": False,
                    "write_allowed": False,
                },
            },
        }

    def inspect_candidate(arguments: dict[str, Any], _context) -> dict[str, Any]:
        nonlocal metatube_client
        if proposed:
            raise ToolPipelineError(
                "复核决定提交后不能继续读取证据", code="decision_already_proposed"
            )
        index = _validate_index(arguments.get("candidate_index"), candidates)
        candidate = candidates[index]
        provider = _candidate_provider(candidate)
        media_type = str(candidate.get("media_type") or "").strip().lower()
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        if provider == "clean_title":
            if not nsfw_clean_review_enabled():
                inspected_candidates[index] = False
                return {"ok": False, "status": "not_authorized",
                        "summary": "未授权光鸭 NSFW 清洗入库，保留人工确认"}
            evidence = inspect_nsfw_clean_candidate(payload, candidate)
            inspected_candidates[index] = bool(evidence["ok"])
            return evidence
        if provider == "metatube":
            source_provider, separator, item_id = str(
                candidate.get("external_id") or ""
            ).strip().partition(":")
            if media_type != "movie" or not separator or not source_provider or not item_id:
                return {"ok": False, "status": "unsupported_candidate",
                        "summary": "MetaTube 冻结身份不完整，保留人工确认"}
            if index not in inspected_details:
                projected: dict[str, Any] = {"found": False}
                try:
                    if metatube_client is None:
                        endpoint = (
                            config.get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT", "") or ""
                        ).strip()
                        rules = payload.get("rules")
                        frozen_endpoint = (
                            str(rules.get("nsfw_metatube_endpoint") or "").strip()
                            if isinstance(rules, dict) else ""
                        )
                        if frozen_endpoint and frozen_endpoint.rstrip("/") != endpoint.rstrip("/"):
                            raise MetaTubeError("MetaTube 配置已变化")
                        metatube_client = MetaTubeClient(
                            endpoint,
                            config.get("GY_ORGANIZE_NSFW_METATUBE_TOKEN", "") or "",
                            timeout=config.get_int("GY_ORGANIZE_NSFW_TIMEOUT_SECONDS", 8),
                        )
                    detail = metatube_client.get_movie(source_provider, item_id)
                    evidence = _metatube_projection(detail.raw)
                    if not contains_sensitive_credential(evidence):
                        projected = {"found": True, **evidence}
                except (MetaTubeError, ValueError):
                    # 不把上游异常文本、服务地址或认证字段送给模型。
                    pass
                inspected_details[index] = projected
            projected = inspected_details[index]
            inspected_candidates[index] = bool(
                projected["found"]
                and projected.get("external_id") == f"{source_provider}:{item_id}"
                and projected.get("number")
            )
            return {
                "ok": inspected_candidates[index],
                "status": "success" if inspected_candidates[index] else "not_found",
                "summary": (
                    "已读取 MetaTube 冻结候选详情；仍需核对源文件并排除冲突"
                    if inspected_candidates[index] else "MetaTube 详情不足或身份不符，保留人工确认"
                ),
                "data": {"candidate_index": index, **projected},
            }
        if provider != "tmdb" or media_type not in {"movie", "tv"} or not tmdb_id:
            inspected_candidates[index] = False
            return {
                "ok": False,
                "status": "unsupported_candidate",
                "summary": "该候选不属于已支持的 TMDB、MetaTube 核验或光鸭清洗范围",
                "data": {"candidate_index": index, "provider": provider},
            }
        assert scraper is not None
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
        season_value: Any = arguments.get("season")
        season = int(season_value)
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
        assert scraper is not None
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
            description="核验冻结候选：TMDB读取实时详情；MetaTube仅按冻结provider:external_id读取番号、日期、演员和厂牌等结构化详情，所有冲突候选均须读取；已授权的光鸭clean_title仅核对服务器清洗证据。",
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
        model_tools=tuple(tool.name for tool in specs),
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
        # 至少容纳两次实际网络空闲等待（含 30 秒下限），整次复核最长 90 秒。
        timeout = min(90, network_idle_timeout_seconds(settings.timeout_seconds) * 2 + 10)

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
        if scraper is not None:
            scraper.close()
        if metatube_client is not None:
            metatube_client.close()

    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    model_name = (settings.model or "")[:200]
    review_evidence = [
        {
            "candidate_index": position,
            "frozen_identity_matches": detail["external_id"] == str(
                candidates[position].get("external_id") or ""
            ).strip(),
            "source_number_matches": bool(
                metatube_source.get("number") == detail["number"] and detail["number"]
            ),
            **{key: detail[key] for key in (
                "source_provider", "external_id", "number", "year", "release_date",
                "actors", "actors_truncated", "maker", "label", "director", "series",
            )},
        }
        for position, detail in sorted(inspected_details.items())
        if _candidate_provider(candidates[position]) == "metatube" and detail.get("found")
    ]
    if not proposed:
        return RecognitionReviewDecision(
            status="failed",
            reason_code="model_no_decision",
            summary="模型未提交结构化复核决定",
            model=model_name,
            tool_calls=tool_calls,
            duration_ms=elapsed_ms,
            failure_code=failure_code or "missing_decision",
            evidence=review_evidence,
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
            evidence=review_evidence,
        )

    index = int(proposed["candidate_index"])
    candidate = candidates[index]
    confidence = float(proposed["confidence"])
    guard_failure = ""
    if not case_inspected:
        guard_failure = "case_not_inspected"
    elif confidence < _MIN_APPROVAL_CONFIDENCE:
        guard_failure = "confidence_below_gate"
    elif _candidate_provider(candidate) == "clean_title":
        if not nsfw_clean_review_enabled():
            guard_failure = "nsfw_clean_not_authorized"
        elif not inspected_candidates.get(index, False):
            guard_failure = "candidate_not_revalidated"
        else:
            evidence = inspect_nsfw_clean_candidate(payload, candidate)
            if not evidence["ok"]:
                guard_failure = evidence["reason_code"]
    elif _candidate_provider(candidate) == "metatube":
        guard_failure = _metatube_approval_failure(
            candidates, index, inspected_details, metatube_source
        )
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
            evidence=review_evidence,
        )
    return RecognitionReviewDecision(
        status="approved",
        entry_mode="clean_title" if _candidate_provider(candidate) == "clean_title" else "",
        candidate_index=index,
        confidence=confidence,
        reason_code=str(proposed["reason_code"]),
        summary=str(proposed["summary"]),
        model=model_name,
        tool_calls=tool_calls,
        duration_ms=elapsed_ms,
        evidence=review_evidence,
    )


def _discovery_abstention(reason_code: str, summary: str = "") -> RecognitionReviewDecision:
    return RecognitionReviewDecision(
        status="abstained", reason_code=reason_code, summary=summary or _DISCOVERY_SUMMARIES[reason_code],
    )


def _discovery_title_key(title: str) -> str:
    key = _normalized_title(title)
    if (key.isdigit() or key in _GENERIC_FILENAME_IDENTITY_HINTS
            or _low_information_query(title) or not title_hint_from_path(title)):
        return ""
    return key


def _verify_discovered_candidates(
    scraper: TMDBScraper, candidates: list[Candidate], *, media_type: str, year: str,
    required_titles: set[str], episodes_by_season: dict[int, set[int]], file_count: int, explicit_tmdb_id: str,
) -> RecognitionReviewDecision:
    title_field, original_field, date_field = (
        ("name", "original_name", "first_air_date") if media_type == "tv"
        else ("title", "original_title", "release_date")
    )
    selected: dict[str, Any] | None = None
    seen_ids: set[str] = set()
    misses = {"no_candidate": ""}
    for candidate in candidates[:_MAX_CANDIDATES]:
        if candidate.provider != "tmdb" or candidate.media_type != media_type:
            continue
        if year and candidate.year and candidate.year != year:
            misses["candidate_year_mismatch"] = ""
            continue
        try:
            tmdb_id = normalize_tmdb_id(candidate.tmdb_id)
        except ValueError:
            continue
        if explicit_tmdb_id and tmdb_id != explicit_tmdb_id:
            misses["discovery_tmdb_marker_conflict"] = ""
            continue
        if tmdb_id in seen_ids:
            continue
        seen_ids.add(tmdb_id)

        # 只传请求身份，让缓存中的真实详情独立证明标题、别名和年份。
        detail, verified = scraper._enrich_candidate_for_scoring({"id": tmdb_id}, media_type)
        title = str(detail.get(title_field) or "").strip()
        if not verified or not title:
            return _discovery_abstention("candidate_not_revalidated")
        aliases = {
            _normalized_title(value)
            for value in (title, detail.get(original_field), *_candidate_aliases(detail))
        }
        if not required_titles <= aliases:
            misses["candidate_title_mismatch"] = ""
            continue
        try:
            detail_year = date.fromisoformat(str(detail.get(date_field) or "")).isoformat()[:4]
        except ValueError:
            return _discovery_abstention("candidate_not_revalidated")
        if any(expected and detail_year != expected for expected in (year, candidate.year)):
            misses["candidate_year_mismatch"] = ""
            continue

        # 逐季读取真实集号，全组检查成员资格，即使季集映射关闭也必须通过。
        for season, episodes in episodes_by_season.items():
            season_detail = scraper.get_tv_season_detail(tmdb_id, season)
            rows = season_detail.get("episodes")
            if (season_detail.get("season_number") != season or not isinstance(rows, list)
                    or any(not isinstance(row, dict) or type(row.get("episode_number")) is not int
                           or row.get("season_number", season) != season for row in rows)):
                return _discovery_abstention(
                    "discovery_season_unavailable", f"无法核验 TMDB 的 S{season:02d} 逐集详情，请稍后重试。",
                )
            missing = episodes.difference(_season_projection(season_detail, season=season)["episode_numbers"])
            if missing:
                label = f"S{season:02d}E{min(missing):02d}"
                misses["episode_boundary_unverified"] = f"TMDB 季详情未记录 {label}，请手动核对该集。"
                break
        else:
            if selected is not None:
                return _discovery_abstention("discovery_ambiguous")
            selected = {
                "provider": "tmdb", "external_id": tmdb_id, "tmdb_id": tmdb_id,
                "media_type": media_type, "title": title, "year": detail_year,
                "score": candidate.score, "support": file_count,
                "genre_ids": list(candidate.metadata.get("genre_ids") or []),
            }

    if selected is None:
        reason = next(reason for reason in _DISCOVERY_MISS_REASONS if reason in misses)
        return _discovery_abstention(reason, misses[reason])
    return RecognitionReviewDecision(
        status="approved", candidate_index=0, confidence=1.0,
        reason_code="tmdb_discovery_verified", summary="已补搜并核验全组 TMDB 候选，等待人工确认",
        discovered_candidates=[selected],
    )


def _discover_tmdb_candidate(
    payload: dict[str, Any], discovery_scraper: TMDBScraper | None,
) -> RecognitionReviewDecision:
    files = payload["files"]
    directory = str(payload.get("directory") or "")
    names = [item["name"].strip() for item in files]
    if (not _model_visible_payload_is_safe(payload, [])
            or any(contains_sensitive_credential(value) for value in (directory, *names))):
        return _discovery_abstention("unsafe_input")

    if any(parse_release_position(name).get("episode_end") is not None for name in names):
        return _discovery_abstention("discovery_multi_episode_file")
    explicit_ids: set[str] = set()
    for value in (directory, *names):
        marker_id, conflict = _resolve_explicit_tmdb_marker(value, nearest_first=True)
        if conflict:
            return _discovery_abstention("discovery_tmdb_marker_conflict")
        if marker_id:
            try:
                explicit_ids.add(normalize_tmdb_id(marker_id))
            except ValueError:
                return _discovery_abstention("discovery_tmdb_marker_conflict")
    if len(explicit_ids) > 1:
        return _discovery_abstention("discovery_tmdb_marker_conflict")
    explicit_tmdb_id = next(iter(explicit_ids), "")

    contexts = [extract_recognition_context(name, directory) for name in names]
    media_type = contexts[0].media_type
    if any(context.media_type != media_type for context in contexts):
        return _discovery_abstention("discovery_media_type_conflict")
    years = {year for context in contexts for year in (context.filename_year, context.folder_year) if year}
    if len(years) > 1:
        return _discovery_abstention("discovery_year_conflict")
    year = next(iter(years), "")

    filename_titles: dict[str, str] = {}
    folder_titles: dict[str, str] = {}
    required_titles: set[str] = set()
    episodes_by_season: dict[int, set[int]] = {}
    for item, context in zip(files, contexts, strict=True):
        title = context.filename_title
        key = _discovery_title_key(title)
        folder_key = _discovery_title_key(context.folder_title)
        if folder_key:
            folder_titles.setdefault(folder_key, context.folder_title)
        if key:
            filename_titles.setdefault(key, title)
        if not (key or folder_key):
            return _discovery_abstention("discovery_source_title_missing")
        required_titles.add(key or folder_key)

        season, episode = context.season, context.episode
        for field_name, parsed in (("season", season), ("episode", episode)):
            frozen = item.get(field_name)
            if frozen not in (None, "") and (not str(frozen).isdecimal() or int(frozen) != parsed):
                return _discovery_abstention("discovery_episode_position_conflict")
        if media_type == "tv":
            if season is None or not 0 <= season <= 99 or episode is None or episode < 1:
                return _discovery_abstention("episode_position_incomplete")
            episodes_by_season.setdefault(season, set()).add(episode)

    # 同目录的标题可以给无标题文件补证据，但不能掩盖两个不同的文件名标题。
    if len(filename_titles) > 1 or len(folder_titles) > 1:
        return _discovery_abstention("discovery_source_title_conflict")
    scraper = discovery_scraper if discovery_scraper is not None else TMDBScraper()
    try:
        best_miss = _discovery_abstention("no_candidate")
        searched: set[str] = set()
        for source_key, title in (*filename_titles.items(), *folder_titles.items()):
            query = scraper.clean_title(title)
            query_key = _discovery_title_key(query)
            if not query_key or query_key in searched:
                continue
            if contains_sensitive_credential(query):
                return _discovery_abstention("unsafe_input")
            searched.add(query_key)
            # 与手动选片共用搜索缓存和真实候选格式，并保留网络失败状态。
            outcome = scraper._search_with_outcome(query, year, media_type)
            if outcome.status == "no_result":
                continue
            if outcome.status != "matched":
                return _discovery_abstention(
                    "discovery_search_not_configured" if outcome.status == "config_error"
                    else "discovery_search_unavailable",
                )
            candidates = scraper._pick_best(query, year, media_type, list(outcome.results)).candidates
            decision = _verify_discovered_candidates(
                scraper, candidates, media_type=media_type, year=year,
                required_titles=required_titles | {source_key}, episodes_by_season=episodes_by_season,
                file_count=len(files), explicit_tmdb_id=explicit_tmdb_id,
            )
            if decision.approved:
                if not _model_visible_payload_is_safe(payload, decision.discovered_candidates):
                    return _discovery_abstention("unsafe_input")
                return decision
            if decision.reason_code not in _DISCOVERY_FALLBACK_REASONS:
                return decision
            if _DISCOVERY_MISS_REASONS.index(decision.reason_code) < _DISCOVERY_MISS_REASONS.index(best_miss.reason_code):
                best_miss = decision
        return best_miss
    finally:
        if discovery_scraper is None:
            scraper.close()


def _should_research_episodes(payload: dict, decision: RecognitionReviewDecision | None = None) -> bool:
    if not payload.get("candidates"):
        return False
    from app.modules.episode_research_service import episode_research_enabled
    if not episode_research_enabled() or str(payload.get("kind") or "guangya") != "guangya":
        return False
    if decision is not None and (decision.approved or decision.status != "abstained"):
        return False
    if decision is not None and decision.reason_code in {"episode_boundary_unverified", "episode_position_incomplete"}:
        return True
    # 多个同名候选中可能有一个能直接满足标准季集：先保留原复核选择它的机会。
    # 只有单一候选的明确位置硬冲突才跳过不能改号的旧模型回合。
    tv_candidates = [item for item in payload.get("candidates", [])
                     if isinstance(item, dict) and item.get("media_type") == "tv"
                     and _candidate_provider(item) == "tmdb"]
    reason = str(payload.get("reason") or "")
    return len(tv_candidates) == 1 and any(marker in reason for marker in (
        "文件季号在 TMDB 中不存在", "文件集号超出 TMDB 记录范围",
    ))


def _research_episode_decision(payload: dict) -> RecognitionReviewDecision:
    from app.modules.episode_research_service import research_confirmation_episodes
    result = research_confirmation_episodes(payload)
    verified = result.get("status") == "verified"
    receipt = result.get("receipt") if verified else None
    return RecognitionReviewDecision(
        status="approved" if verified else "abstained",
        entry_mode="episode_research",
        candidate_index=receipt["candidate_index"] if isinstance(receipt, dict) else None,
        confidence=1.0 if verified else 0.0,
        reason_code=str(result.get("reason_code") or "episode_research_abstained"),
        summary="已验证唯一完整的季集组对应关系" if verified else "研究证据不足或不可用，保留人工确认",
        model=str(result.get("model") or ""),
        tool_calls=int(result.get("tool_calls") or 0), duration_ms=int(result.get("duration_ms") or 0),
        episode_research_receipt=receipt,
    )


def review_confirmation_payload(
    payload: dict[str, Any], *, discovery_scraper: TMDBScraper | None = None,
) -> RecognitionReviewDecision:
    """同步入口，供持久化后台 Worker 调用。异常一律收敛为人工回退。"""
    if not feature_gate.recognition_review_enabled():
        raise RecognitionReviewUnavailable("Agent 主动复核未启用或模型未配置")
    try:
        if (payload.get("review_only") is True and payload.get("discover_candidates") is True
                and can_discover_candidates(payload)):
            return _discover_tmdb_candidate(payload, discovery_scraper)
        if _should_research_episodes(payload):
            return _research_episode_decision(payload)
        decision = asyncio.run(_review_async(dict(payload)))
        if _should_research_episodes(payload, decision):
            return _research_episode_decision(payload)
        return decision
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
