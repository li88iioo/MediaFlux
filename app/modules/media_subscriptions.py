"""媒体订阅巡检、候选资源和安全下载准入。

订阅中心只在确认的 TMDB 身份上工作。媒体服务器审计不完整时仅展示状态，
不会搜索或自动提交；自动模式还要求明确季集匹配与原子下载准入。
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app import config, database as db
from app.agent.resource_recommendation import rank_episode_search
from app.clients.tmdb import TMDBClient
from app.discovery.models import ProviderError, ProviderNotConfigured
from app.indexers.config import tmdb_detail_is_animation
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import get_indexer_service, run_indexer_awaitable
from app.logger import get_logger
from app.modules.indexer_download import download_indexer_result_public
from app.modules.media_identity import build_media_key
from app.sensitive_data import redact_sensitive_text
from app.services import inspect_media_identity_sources, inspect_series_episode_sources

logger = get_logger(__name__)

_MAX_SEARCH_EPISODES = 12
_MAX_CANDIDATES_PER_MEDIA = 8
_PREVIEW_MAX_SEARCH_EPISODES = 3
_PREVIEW_MAX_CANDIDATES_PER_MEDIA = 3
_AUTO_RELEVANCE_THRESHOLD = 90
_ALLOWED_MEDIA_TYPES = frozenset({"movie", "tv"})
_ALLOWED_MONITOR_MODES = frozenset({"missing", "future", "selected"})
_ALLOWED_ACTIONS = frozenset({"notify", "confirm", "auto"})
_ALLOWED_TARGETS = frozenset({"qb", "guangya", "both"})
_ALLOWED_PROVIDERS = frozenset({"tmdb", "douban", "bangumi"})
_ALLOWED_CHECK_INTERVALS = frozenset({4320, 10080})
_ACTIVE_CHECK_RUN_ID: ContextVar[int | None] = ContextVar(
    "media_subscription_active_check_run_id", default=None
)


class MediaSubscriptionError(ValueError):
    """可安全返回给 Web 的订阅业务错误。"""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)


@dataclass(frozen=True)
class _ExpectedMedia:
    media_key: str
    season: int | None = None
    episode: int | None = None
    air_date: str = ""

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"media_key": self.media_key}
        if self.season is not None:
            payload["season"] = self.season
        if self.episode is not None:
            payload["episode"] = self.episode
            payload["label"] = f"S{self.season:02d}E{self.episode:02d}"
        if self.air_date:
            payload["air_date"] = self.air_date
        return payload


def _loads(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _numeric_tmdb_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized.isascii() or not normalized.isdigit() or not 1 <= len(normalized) <= 10:
        raise MediaSubscriptionError("TMDB ID 必须是 1 到 10 位数字")
    normalized = str(int(normalized))
    if normalized == "0":
        raise MediaSubscriptionError("TMDB ID 无效")
    return normalized


def _choice(value: Any, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or default).strip().lower()
    if normalized not in allowed:
        raise MediaSubscriptionError(f"不支持的选项：{normalized}")
    return normalized


def _strict_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise MediaSubscriptionError("布尔参数必须为 true 或 false")


def _provider_identity(provider: Any, external_id: Any, tmdb_id: str) -> tuple[str, str]:
    normalized_provider = _choice(provider, _ALLOWED_PROVIDERS, "tmdb")
    normalized_external = str(external_id or tmdb_id).strip()
    if not normalized_external or len(normalized_external) > 128:
        raise MediaSubscriptionError("外部媒体 ID 无效")
    if any(char.isspace() for char in normalized_external):
        raise MediaSubscriptionError("外部媒体 ID 不得包含空白字符")
    if normalized_provider == "tmdb":
        normalized_external = _numeric_tmdb_id(normalized_external)
        if normalized_external != tmdb_id:
            raise MediaSubscriptionError("TMDB 外部 ID 与订阅 TMDB ID 不一致")
    return normalized_provider, normalized_external


def _bounded_interval(value: Any) -> int:
    try:
        interval = int(value or 4320)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaSubscriptionError("检查间隔必须是整数") from exc
    if interval not in _ALLOWED_CHECK_INTERVALS:
        raise MediaSubscriptionError("检查间隔仅支持每 3 天或每 7 天")
    return interval


def _normalize_seasons(values: Any) -> list[int]:
    if values in (None, ""):
        return []
    if not isinstance(values, (list, tuple, set)):
        raise MediaSubscriptionError("季度必须是数组")
    seasons: list[int] = []
    for raw in values:
        if isinstance(raw, bool):
            raise MediaSubscriptionError("季度包含无效值")
        try:
            season = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaSubscriptionError("季度包含无效值") from exc
        if not 0 <= season <= 100:
            raise MediaSubscriptionError("季度必须在 0 到 100 之间")
        if season not in seasons:
            seasons.append(season)
    return sorted(seasons)


def _normalize_sites(values: Any) -> list[str]:
    if values in (None, ""):
        return []
    if not isinstance(values, (list, tuple, set)):
        raise MediaSubscriptionError("资源站必须是数组")
    sites: list[str] = []
    for value in values:
        site = str(value or "").strip().lower()
        if not site or len(site) > 32 or not site.replace("-", "").replace("_", "").isalnum():
            raise MediaSubscriptionError("资源站包含无效值")
        if site not in sites:
            sites.append(site)
    return sites[:16]


def _search_rotation_from_row(row: Any, revision: int) -> dict[str, int] | None:
    """读取上次成功巡检的内部搜索游标；配置版本变化后自动失效。"""
    stored = _loads(row["result_json"], {})
    raw = stored.get("_search_rotation") if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        cursor_revision = int(raw.get("revision"))
        season = int(raw.get("season"))
        episode = int(raw.get("episode"))
    except (TypeError, ValueError, OverflowError):
        return None
    if cursor_revision != int(revision) or season < 0 or episode <= 0:
        return None
    return {"revision": cursor_revision, "season": season, "episode": episode}


def _rotated_missing_targets(
    missing: Iterable[_ExpectedMedia],
    cursor: dict[str, int] | None,
    *,
    revision: int,
    limit: int = _MAX_SEARCH_EPISODES,
) -> tuple[list[_ExpectedMedia], dict[str, int] | None]:
    """公平选择本轮缺集，避免长季永久只搜索最前面的固定批次。"""
    ordered = sorted(
        (
            item for item in missing
            if item.season is not None and item.episode is not None
        ),
        key=lambda item: (int(item.season or 0), int(item.episode or 0), item.media_key),
    )
    if not ordered or int(limit) <= 0:
        return [], None

    start = 0
    if cursor and int(cursor.get("revision", 0)) == int(revision):
        position = (int(cursor.get("season", 0)), int(cursor.get("episode", 0)))
        start = next(
            (
                index for index, item in enumerate(ordered)
                if (int(item.season or 0), int(item.episode or 0)) > position
            ),
            0,
        )

    count = min(len(ordered), max(0, int(limit)))
    selected = [ordered[(start + offset) % len(ordered)] for offset in range(count)]
    last = selected[-1]
    return selected, {
        "revision": int(revision),
        "season": int(last.season or 0),
        "episode": int(last.episode or 0),
    }


def _parse_air_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _metadata_from_detail(detail: dict[str, Any], media_type: str) -> dict[str, str]:
    title = str(detail.get("name" if media_type == "tv" else "title") or "").strip()
    original = str(detail.get("original_name" if media_type == "tv" else "original_title") or "").strip()
    release = str(detail.get("first_air_date" if media_type == "tv" else "release_date") or "").strip()
    year = release[:4] if len(release) >= 4 and release[:4].isdigit() else ""
    poster = str(detail.get("poster_path") or "").strip()
    if not title:
        raise MediaSubscriptionError("TMDB 详情缺少标题", status_code=502, code="invalid_response")
    return {"title": title, "original_title": original, "year": year, "poster_key": poster}


def _subscription_workflow(row: Any, aggregate: dict[str, Any] | None = None) -> dict[str, Any]:
    values = aggregate or {}
    workflow = {
        "available_candidate_count": int(values.get("available_candidate_count") or 0),
        "submitted_candidate_count": int(values.get("submitted_candidate_count") or 0),
        "max_relevance_score": values.get("max_relevance_score"),
        "submitted_count": int(values.get("submitted_count") or 0),
        "downloading_count": int(values.get("downloading_count") or 0),
        "processing_count": int(values.get("processing_count") or 0),
        "manual_review_count": int(values.get("manual_review_count") or 0),
        "failed_count": int(values.get("failed_count") or 0),
    }
    active_count = sum(
        workflow[key]
        for key in (
            "submitted_count", "downloading_count", "processing_count", "manual_review_count"
        )
    )
    missing_count = int(row["missing_count"] or 0)
    status = str(row["status"] or "new")
    action = str(row["action"] or "confirm")
    if workflow["manual_review_count"]:
        primary = "manual_review"
    elif workflow["processing_count"]:
        primary = "processing"
    elif workflow["downloading_count"]:
        primary = "downloading"
    elif workflow["submitted_count"]:
        primary = "submitted"
    elif missing_count and workflow["failed_count"]:
        primary = "delivery_failed"
    elif not bool(row["enabled"]):
        primary = "paused"
    elif status in {"new", "checking", "error", "inconclusive"}:
        primary = status
    elif missing_count and workflow["available_candidate_count"]:
        primary = {
            "auto": "candidate_waiting_auto",
            "confirm": "candidate_waiting_confirm",
        }.get(action, "candidate_available")
    elif missing_count:
        primary = "missing_no_candidate"
    else:
        primary = status or "satisfied"
    workflow["primary"] = primary
    workflow["active_count"] = active_count
    workflow["candidate_count"] = (
        workflow["available_candidate_count"] + workflow["submitted_candidate_count"]
    )
    return workflow


def _public_subscription(
    row: Any, *, workflow: dict[str, Any] | None = None, candidates: int | None = None
) -> dict[str, Any]:
    payload = _row_dict(row)
    if not payload:
        return {}
    payload["enabled"] = bool(payload.get("enabled"))
    payload["include_specials"] = bool(payload.get("include_specials"))
    payload["seasons"] = _loads(payload.pop("seasons_json", "[]"), [])
    payload["sites"] = _loads(payload.pop("sites_json", "[]"), [])
    payload["missing"] = _loads(payload.pop("missing_json", "[]"), [])
    payload["result"] = _loads(payload.pop("result_json", "{}"), {})
    if isinstance(payload["result"], dict):
        payload["result"].pop("_search_rotation", None)
    payload["workflow"] = _subscription_workflow(row, workflow)
    payload["candidate_count"] = int(payload["workflow"]["candidate_count"] or 0)
    if candidates is not None:
        payload["candidate_count"] = max(payload["candidate_count"], int(candidates))
    return payload


def _public_candidate(row: Any) -> dict[str, Any]:
    payload = _row_dict(row)
    payload["match_reasons"] = _loads(payload.pop("match_reasons_json", "[]"), [])
    delivery_keys = {
        "admission_id": "delivery_admission_id",
        "status": "delivery_status",
        "error": "delivery_error",
        "request_id": "delivery_request_id",
        "request_status": "delivery_request_status",
        "qb_status": "delivery_qb_status",
        "gy_status": "delivery_gy_status",
        "organize_status": "delivery_organize_status",
        "local_import_status": "delivery_local_import_status",
        "strm_status": "delivery_strm_status",
        "request_error": "delivery_request_error",
    }
    delivery = {name: payload.pop(column, None) for name, column in delivery_keys.items()}
    if any(value not in (None, "", 0) for value in delivery.values()):
        delivery["error"] = redact_sensitive_text(
            str(delivery.get("error") or delivery.get("request_error") or "")
        )[:500]
        delivery.pop("request_error", None)
        payload["delivery"] = delivery
    return payload


def _public_run(row: Any) -> dict[str, Any]:
    payload = _row_dict(row)
    payload["payload"] = _loads(payload.pop("payload_json", "{}"), {})
    return payload


def _resolve_search_sites(
    configured: list | None, detail: dict[str, Any], service: Any,
) -> list | None:
    """订阅显式配置的站点优先；否则按动漫/真人与原产语言自动路由。"""
    if configured:
        return list(configured)
    try:
        routed = service.media_site_route(
            is_animation=tmdb_detail_is_animation(detail),
            original_language=str(detail.get("original_language") or ""),
        )
    except Exception:
        logger.debug("站点自动路由失败，回退全部启用站点", exc_info=True)
        return None
    return list(routed) or None


class MediaSubscriptionService:
    """按 TMDB 身份巡检媒体库，并在安全边界内生成/提交候选资源。"""

    async def create_subscription(
        self,
        data: dict[str, Any],
        *,
        identity_confirmed: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise MediaSubscriptionError("订阅参数必须是 JSON 对象")
        media_type = _choice(data.get("media_type"), _ALLOWED_MEDIA_TYPES, "tv")
        tmdb_id = _numeric_tmdb_id(data.get("tmdb_id"))
        monitor_mode = _choice(data.get("monitor_mode"), _ALLOWED_MONITOR_MODES, "missing")
        action = _choice(data.get("action"), _ALLOWED_ACTIONS, "confirm")
        target = _choice(data.get("download_target"), _ALLOWED_TARGETS, "guangya")
        interval = _bounded_interval(data.get("check_interval_minutes", 4320))
        seasons = _normalize_seasons(data.get("seasons"))
        sites = _normalize_sites(data.get("sites"))
        include_specials = _strict_bool(data.get("include_specials"), default=False)
        enabled = _strict_bool(data.get("enabled"), default=True)
        provider, external_id = _provider_identity(
            data.get("provider"), data.get("external_id"), tmdb_id
        )
        if provider != "tmdb" and not identity_confirmed:
            raise MediaSubscriptionError(
                "非 TMDB 媒体需先确认映射后再创建订阅",
                status_code=409,
                code="mapping_required",
            )
        if media_type == "movie" and (seasons or include_specials or monitor_mode == "selected"):
            raise MediaSubscriptionError("电影订阅不支持季度或特典筛选")
        if monitor_mode == "selected" and not seasons:
            raise MediaSubscriptionError("按季度订阅时至少选择一个季度")

        try:
            detail = await asyncio.to_thread(TMDBClient().detail, tmdb_id, media_type)
        except ProviderNotConfigured as exc:
            raise MediaSubscriptionError("请先配置 TMDB API Key", status_code=503, code="not_configured") from exc
        except ProviderError as exc:
            raise MediaSubscriptionError(exc.safe_message, status_code=exc.status_code, code=exc.code) from exc
        metadata = _metadata_from_detail(detail, media_type)
        subscription_id, created = db.upsert_media_subscription(
            provider=provider,
            external_id=external_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=metadata["title"],
            original_title=metadata["original_title"],
            year=metadata["year"],
            poster_key=metadata["poster_key"],
            enabled=enabled,
            monitor_mode=monitor_mode,
            seasons=seasons,
            include_specials=include_specials,
            action=action,
            download_target=target,
            sites=sites,
            check_interval_minutes=interval,
        )
        return {"created": created, "subscription": self.get_subscription(subscription_id)}

    def get_subscription(self, subscription_id: int) -> dict[str, Any]:
        row = db.get_media_subscription(int(subscription_id))
        if row is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        workflows = db.list_media_subscription_workflows([int(subscription_id)])
        return _public_subscription(row, workflow=workflows.get(int(subscription_id)))

    def list_subscriptions(self, *, status: str = "", enabled: bool | None = None) -> list[dict[str, Any]]:
        rows = db.list_media_subscriptions(status=status, enabled=enabled)
        workflows = db.list_media_subscription_workflows(row["id"] for row in rows)
        return [
            _public_subscription(row, workflow=workflows.get(int(row["id"])))
            for row in rows
        ]

    def list_runs(self, *, subscription_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [_public_run(row) for row in db.list_media_subscription_runs(
            subscription_id=subscription_id, limit=limit
        )]

    def list_candidates(self, subscription_id: int) -> list[dict[str, Any]]:
        if db.get_media_subscription(int(subscription_id)) is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        rows = db.list_media_subscription_candidates(int(subscription_id), status="", limit=500)
        return [
            _public_candidate(row) for row in rows
            if str(row["status"] or "") in {"available", "submitted"}
        ]

    async def preview_subscription_updates(
        self,
        subscription_id: int,
        *,
        max_search_episodes: int = 1,
        limit_per_media: int = _PREVIEW_MAX_CANDIDATES_PER_MEDIA,
    ) -> dict[str, Any]:
        """实时核对单条订阅，但不持久化候选、不改调度、也不提交下载。"""
        subscription_id = int(subscription_id)
        max_search_episodes = max(
            0,
            min(int(max_search_episodes), _PREVIEW_MAX_SEARCH_EPISODES),
        )
        limit_per_media = max(
            1,
            min(int(limit_per_media), _PREVIEW_MAX_CANDIDATES_PER_MEDIA),
        )
        row = db.get_media_subscription(subscription_id)
        if row is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")

        revision = int(row["revision"] or 1)
        if not bool(row["enabled"]):
            missing = _loads(row["missing_json"], [])
            return {
                "subscription_number": subscription_id,
                "title": str(row["title"] or ""),
                "media_type": str(row["media_type"] or ""),
                "enabled": False,
                "status": "paused",
                "summary": "订阅已暂停，本次未访问媒体库或资源站",
                "expected_count": max(0, int(row["expected_count"] or 0)),
                "local_count": max(0, int(row["local_count"] or 0)),
                "missing_count": max(0, int(row["missing_count"] or 0)),
                "missing": list(missing)[:12] if isinstance(missing, list) else [],
                "inventory_complete": False,
                "sources": [],
                "resource_search": self._empty_preview_search("not_run"),
                "delivery": self._preview_delivery(row, missing_count=0, resources=[]),
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }

        if str(row["media_type"] or "") == "tv":
            result = await self._preview_tv(
                row,
                max_search_episodes=max_search_episodes,
                limit_per_media=limit_per_media,
            )
        else:
            result = await self._preview_movie(row, limit_per_media=limit_per_media)

        current = db.get_media_subscription(subscription_id)
        if (
            current is None
            or int(current["revision"] or 1) != revision
            or not bool(current["enabled"])
        ):
            return {
                **result,
                "status": "inconclusive",
                "summary": "订阅配置在检查期间发生变化，本次结果未用于后续操作",
                "delivery": self._preview_delivery(row, missing_count=0, resources=[]),
            }
        return result

    def update_subscription(self, subscription_id: int, data: dict[str, Any]) -> dict[str, Any]:
        current = db.get_media_subscription(int(subscription_id))
        if current is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        if not isinstance(data, dict):
            raise MediaSubscriptionError("订阅参数必须是 JSON 对象")
        allowed = {
            "enabled", "monitor_mode", "seasons", "include_specials", "action",
            "download_target", "sites", "check_interval_minutes",
        }
        unknown = set(data) - allowed
        if unknown:
            raise MediaSubscriptionError(f"包含不支持的订阅参数：{', '.join(sorted(unknown))}")
        fields: dict[str, Any] = {}
        requested_enabled = bool(current["enabled"])
        if "enabled" in data:
            requested_enabled = _strict_bool(data["enabled"], default=True)
            fields["enabled"] = int(requested_enabled)
        if "monitor_mode" in data:
            fields["monitor_mode"] = _choice(data["monitor_mode"], _ALLOWED_MONITOR_MODES, "missing")
        if "seasons" in data:
            fields["seasons_json"] = _dumps(_normalize_seasons(data["seasons"]))
        if "include_specials" in data:
            fields["include_specials"] = int(_strict_bool(data["include_specials"], default=False))
        if "action" in data:
            fields["action"] = _choice(data["action"], _ALLOWED_ACTIONS, "confirm")
        if "download_target" in data:
            fields["download_target"] = _choice(data["download_target"], _ALLOWED_TARGETS, "guangya")
        if "sites" in data:
            fields["sites_json"] = _dumps(_normalize_sites(data["sites"]))
        if "check_interval_minutes" in data:
            fields["check_interval_minutes"] = _bounded_interval(data["check_interval_minutes"])
        media_type = str(current["media_type"])
        mode = str(fields.get("monitor_mode", current["monitor_mode"]))
        seasons = _loads(fields.get("seasons_json", current["seasons_json"]), [])
        specials = bool(fields.get("include_specials", current["include_specials"]))
        if media_type == "movie" and (seasons or specials or mode == "selected"):
            raise MediaSubscriptionError("电影订阅不支持季度或特典筛选")
        if mode == "selected" and not seasons:
            raise MediaSubscriptionError("按季度订阅时至少选择一个季度")
        fields["status"] = "new" if requested_enabled else "paused"
        fields["next_check_at"] = db.now()
        fields["last_error"] = ""
        if not db.update_media_subscription_config(int(subscription_id), **fields):
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        return self.get_subscription(int(subscription_id))

    def delete_subscription(self, subscription_id: int) -> bool:
        return db.delete_media_subscription(int(subscription_id))

    async def check_subscription(
        self,
        subscription_id: int,
        *,
        trigger: str = "manual",
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        subscription_id = int(subscription_id)
        if db.get_media_subscription(subscription_id) is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        run_id = db.claim_media_subscription_check_run(subscription_id, trigger)
        if run_id is None:
            raise MediaSubscriptionError("该媒体订阅正在检查或已暂停", status_code=409, code="busy")
        row = db.get_media_subscription(subscription_id)
        assert row is not None
        interval = int(row["check_interval_minutes"] or 4320)
        revision = int(row["revision"] or 1)
        run_token = _ACTIVE_CHECK_RUN_ID.set(int(run_id))
        try:
            self._ensure_active_check(subscription_id, revision, cancel_event)
            if str(row["media_type"]) == "tv":
                result = await self._check_tv(row, cancel_event=cancel_event)
            else:
                result = await self._check_movie(row, cancel_event=cancel_event)
            stored_result = dict(result)
            public_result = dict(result)
            public_result.pop("_search_rotation", None)
            status = str(public_result["status"])
            committed = db.finalize_media_subscription_check(
                subscription_id,
                run_id,
                status=status,
                run_status={
                    "satisfied": "satisfied", "missing": "missing",
                    "inconclusive": "inconclusive",
                }.get(status, "failed"),
                summary=str(public_result.get("summary") or ""),
                payload=public_result,
                interval_minutes=interval,
                expected_count=int(public_result.get("expected_count", 0)),
                local_count=int(public_result.get("local_count", 0)),
                missing_count=int(public_result.get("missing_count", 0)),
                missing_json=_dumps(public_result.get("missing", [])),
                result_json=_dumps(stored_result),
                subscription_revision=revision,
            )
            if not committed:
                raise MediaSubscriptionError(
                    "订阅在检查期间被暂停或配置已变更",
                    status_code=409,
                    code="cancelled",
                )
            try:
                from app.modules.media_subscription_notifications import (
                    drain_media_subscription_notifications,
                )
                drain_media_subscription_notifications(limit=5)
            except Exception as exc:
                logger.warning(
                    "媒体订阅通知投递失败，已保留 outbox type=%s",
                    type(exc).__name__,
                )
            return {"subscription": self.get_subscription(subscription_id), "result": public_result}
        except asyncio.CancelledError:
            db.cancel_media_subscription_run(
                run_id,
                subscription_id=subscription_id,
                subscription_revision=revision,
                reason="应用正在停止，订阅检查已取消",
            )
            raise
        except MediaSubscriptionError as exc:
            if exc.code == "cancelled":
                db.cancel_media_subscription_run(
                    run_id,
                    subscription_id=subscription_id,
                    subscription_revision=revision,
                    reason=str(exc),
                )
            else:
                self._finish_failed_check(subscription_id, run_id, interval, revision, str(exc))
            raise
        except Exception as exc:
            logger.exception("媒体订阅巡检异常 subscription=%s type=%s", subscription_id, type(exc).__name__)
            self._finish_failed_check(
                subscription_id, run_id, interval, revision, "媒体订阅巡检失败"
            )
            raise MediaSubscriptionError("媒体订阅巡检失败", status_code=500, code="failed") from exc
        finally:
            _ACTIVE_CHECK_RUN_ID.reset(run_token)

    def _finish_failed_check(
        self, subscription_id: int, run_id: int, interval: int, revision: int, error: str
    ) -> None:
        committed = db.fail_media_subscription_check(
            subscription_id,
            run_id,
            interval_minutes=interval,
            error=error,
            subscription_revision=revision,
        )
        if committed:
            try:
                from app.modules.media_subscription_notifications import (
                    drain_media_subscription_notifications,
                )
                drain_media_subscription_notifications(limit=5)
            except Exception as exc:
                logger.warning(
                    "媒体订阅异常通知投递失败，已保留 outbox type=%s",
                    type(exc).__name__,
                )

    @staticmethod
    def _ensure_active_check(
        subscription_id: int,
        subscription_revision: int,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise MediaSubscriptionError(
                "应用正在停止，订阅检查已取消", status_code=409, code="cancelled"
            )
        if not db.media_subscription_check_is_active(
            int(subscription_id),
            int(subscription_revision),
            run_id=_ACTIVE_CHECK_RUN_ID.get(),
        ):
            raise MediaSubscriptionError(
                "订阅已暂停或检查状态已变化", status_code=409, code="cancelled"
            )

    async def _check_tv(
        self, row: Any, *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        tmdb_id = str(row["tmdb_id"])
        title = str(row["title"])
        include_specials = bool(row["include_specials"])
        revision = int(row["revision"] or 1)
        search_rotation = _search_rotation_from_row(row, revision)
        try:
            detail = await asyncio.to_thread(TMDBClient().detail, tmdb_id, "tv")
            self._ensure_active_check(int(row["id"]), revision, cancel_event)
            expected, future_count, unknown_dates = await self._expected_tv(
                row, detail, cancel_event=cancel_event
            )
        except ProviderNotConfigured as exc:
            raise MediaSubscriptionError("请先配置 TMDB API Key", status_code=503, code="not_configured") from exc
        except ProviderError as exc:
            raise MediaSubscriptionError(exc.safe_message, status_code=exc.status_code, code=exc.code) from exc

        sources = await asyncio.to_thread(
            inspect_series_episode_sources,
            title,
            tmdb_id=tmdb_id,
            max_episodes=2000,
            include_specials=include_specials,
        )
        self._ensure_active_check(int(row["id"]), revision, cancel_event)
        inventory_complete, inventory_reason = self._inventory_complete(sources)
        local_positions: set[tuple[int, int]] = set()
        for source in sources:
            if source.get("status") == "ready":
                local_positions.update(
                    (int(item[0]), int(item[1]))
                    for item in source.get("episodes", [])
                    if isinstance(item, (list, tuple)) and len(item) == 2
                )
        local_keys = {build_media_key(tmdb_id, "tv", season, episode) for season, episode in local_positions}
        await self._sync_admissions(row, local_keys)
        active_keys = {
            str(item["media_key"])
            for item in db.list_active_media_download_admissions(int(row["id"]))
        }
        missing_items = [
            item for item in expected
            if (item.season, item.episode) not in local_positions and item.media_key not in active_keys
        ]
        missing = [item.public_dict() for item in missing_items]
        expected_keys = {item.media_key for item in expected}
        base = {
            "media_type": "tv",
            "tmdb_id": tmdb_id,
            "title": title,
            "expected_count": len(expected),
            "local_count": sum(
                build_media_key(tmdb_id, "tv", *position) in expected_keys
                for position in local_positions
            ),
            "missing_count": len(missing),
            "missing": missing,
            "inflight_count": len(active_keys),
            "future_count": future_count,
            "unknown_air_date_count": unknown_dates,
            "inventory_complete": inventory_complete,
            "inventory_reason": inventory_reason,
            "sources": self._public_sources(sources),
            "candidate_count": 0,
            "auto_submitted": 0,
        }
        if not inventory_complete:
            return {
                **base,
                "status": "inconclusive",
                "summary": inventory_reason,
                "_search_rotation": search_rotation,
            }
        if not missing_items:
            return {
                **base,
                "status": "satisfied",
                "summary": "已播剧集均已收录或正在下载",
                "_search_rotation": None,
            }
        candidates = 0
        auto_submitted = 0
        if str(row["action"]) != "notify":
            candidates, auto_submitted, search_rotation = await self._search_missing_tv(
                row,
                detail,
                missing_items,
                search_rotation=search_rotation,
                cancel_event=cancel_event,
            )
        return {
            **base,
            "status": "missing",
            "summary": f"发现 {len(missing_items)} 集已播但尚未收录",
            "candidate_count": candidates,
            "auto_submitted": auto_submitted,
            "_search_rotation": search_rotation,
        }

    async def _expected_tv(
        self,
        row: Any,
        detail: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        require_active_check: bool = True,
    ) -> tuple[list[_ExpectedMedia], int, int]:
        raw_seasons = detail.get("seasons", [])
        if not isinstance(raw_seasons, list):
            raise MediaSubscriptionError("TMDB 季度数据无效", status_code=502, code="invalid_response")
        available = sorted({
            int(item.get("season_number"))
            for item in raw_seasons
            if isinstance(item, dict)
            and str(item.get("season_number", "")).lstrip("-").isdigit()
            and int(item.get("season_number")) >= 0
        })
        include_specials = bool(row["include_specials"])
        if not include_specials:
            available = [season for season in available if season > 0]
        mode = str(row["monitor_mode"] or "missing")
        selected = [int(value) for value in _loads(row["seasons_json"], [])]
        if mode == "selected":
            available = [season for season in available if season in selected]
        today = date.today()
        created_day = _parse_air_date(str(row["created_at"] or "")[:10]) or today
        expected: list[_ExpectedMedia] = []
        future_count = 0
        unknown_dates = 0
        client = TMDBClient()
        for season in available[:30]:
            payload = await asyncio.to_thread(client.tv_season_detail, str(row["tmdb_id"]), season)
            # 已开始的同步 HTTP 请求无法被 asyncio 取消；每季返回后立即复核，
            # 避免停机/配置变更后继续请求其余最多 29 季。
            if require_active_check:
                self._ensure_active_check(
                    int(row["id"]), int(row["revision"] or 1), cancel_event
                )
            episodes = payload.get("episodes", [])
            if not isinstance(episodes, list):
                raise MediaSubscriptionError("TMDB 集数数据无效", status_code=502, code="invalid_response")
            for item in episodes:
                if not isinstance(item, dict):
                    continue
                try:
                    episode = int(item.get("episode_number"))
                except (TypeError, ValueError):
                    continue
                if episode <= 0:
                    continue
                aired = _parse_air_date(item.get("air_date"))
                if aired is None:
                    unknown_dates += 1
                    continue
                if aired > today:
                    future_count += 1
                    continue
                if mode == "future" and aired < created_day:
                    continue
                expected.append(_ExpectedMedia(
                    media_key=build_media_key(str(row["tmdb_id"]), "tv", season, episode),
                    season=season,
                    episode=episode,
                    air_date=aired.isoformat(),
                ))
        return expected, future_count, unknown_dates

    async def _preview_tv(
        self,
        row: Any,
        *,
        max_search_episodes: int,
        limit_per_media: int,
    ) -> dict[str, Any]:
        tmdb_id = str(row["tmdb_id"])
        title = str(row["title"])
        include_specials = bool(row["include_specials"])
        try:
            detail = await asyncio.to_thread(TMDBClient().detail, tmdb_id, "tv")
            expected, future_count, unknown_dates = await self._expected_tv(
                row,
                detail,
                require_active_check=False,
            )
        except ProviderNotConfigured as exc:
            raise MediaSubscriptionError(
                "请先配置 TMDB API Key", status_code=503, code="not_configured"
            ) from exc
        except ProviderError as exc:
            raise MediaSubscriptionError(
                exc.safe_message, status_code=exc.status_code, code=exc.code
            ) from exc

        sources = await asyncio.to_thread(
            inspect_series_episode_sources,
            title,
            tmdb_id=tmdb_id,
            max_episodes=2000,
            include_specials=include_specials,
        )
        inventory_complete, inventory_reason = self._inventory_complete(sources)
        local_positions: set[tuple[int, int]] = set()
        for source in sources:
            if source.get("status") != "ready":
                continue
            local_positions.update(
                (int(item[0]), int(item[1]))
                for item in source.get("episodes", [])
                if isinstance(item, (list, tuple)) and len(item) == 2
            )
        local_keys = {
            build_media_key(tmdb_id, "tv", season, episode)
            for season, episode in local_positions
        }
        active_keys = {
            str(item["media_key"])
            for item in db.list_active_media_download_admissions(int(row["id"]))
            if str(item["media_key"] or "") not in local_keys
        }
        missing_items = [
            item
            for item in expected
            if (item.season, item.episode) not in local_positions
            and item.media_key not in active_keys
        ]
        missing = [item.public_dict() for item in missing_items]
        expected_keys = {item.media_key for item in expected}
        local_count = sum(
            build_media_key(tmdb_id, "tv", *position) in expected_keys
            for position in local_positions
        )
        search = self._empty_preview_search("not_run")
        if inventory_complete and missing_items and max_search_episodes > 0:
            search = await self._preview_search_missing_tv(
                row,
                detail,
                missing_items,
                max_search_episodes=max_search_episodes,
                limit_per_media=limit_per_media,
            )
        if not inventory_complete:
            status = "inconclusive"
            summary = inventory_reason
        elif missing_items:
            status = "missing"
            summary = f"发现 {len(missing_items)} 集已播但尚未收录"
        else:
            status = "satisfied"
            summary = "已播剧集均已收录或正在下载"
        resources = self._preview_resource_items(search)
        return {
            "subscription_number": int(row["id"]),
            "title": title,
            "media_type": "tv",
            "enabled": True,
            "status": status,
            "summary": summary,
            "expected_count": len(expected),
            "local_count": int(local_count),
            "missing_count": len(missing),
            "missing": missing[:12],
            "missing_truncated": len(missing) > 12,
            "inflight_count": len(active_keys),
            "future_count": future_count,
            "unknown_air_date_count": unknown_dates,
            "inventory_complete": inventory_complete,
            "inventory_reason": inventory_reason,
            "sources": self._public_sources(sources),
            "resource_search": search,
            "delivery": self._preview_delivery(
                row,
                missing_count=len(missing),
                resources=resources,
                media_type="tv",
                search_status=str(search.get("status") or ""),
            ),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    async def _preview_movie(
        self,
        row: Any,
        *,
        limit_per_media: int,
    ) -> dict[str, Any]:
        tmdb_id = str(row["tmdb_id"])
        try:
            detail = await asyncio.to_thread(TMDBClient().detail, tmdb_id, "movie")
        except ProviderNotConfigured as exc:
            raise MediaSubscriptionError(
                "请先配置 TMDB API Key", status_code=503, code="not_configured"
            ) from exc
        except ProviderError as exc:
            raise MediaSubscriptionError(
                exc.safe_message, status_code=exc.status_code, code=exc.code
            ) from exc

        sources = await asyncio.to_thread(inspect_media_identity_sources, tmdb_id, "movie")
        inventory_complete, inventory_reason = self._inventory_complete(sources, identity=True)
        present = any(
            bool(source.get("present"))
            for source in sources
            if source.get("status") == "ready"
        )
        media_key = build_media_key(tmdb_id, "movie")
        active = any(
            str(item["media_key"] or "") == media_key
            for item in db.list_active_media_download_admissions(int(row["id"]))
        ) and not present
        release = _parse_air_date(detail.get("release_date"))
        release_known = release is not None
        released = bool(release and release <= date.today())
        missing_count = int(bool(inventory_complete and released and not present and not active))
        search = self._empty_preview_search("not_run")
        if missing_count:
            search = await self._preview_search_movie(
                row,
                detail,
                limit_per_media=limit_per_media,
            )
        if not inventory_complete:
            status = "inconclusive"
            summary = inventory_reason
        elif not release_known and not present and not active:
            status = "inconclusive"
            summary = "TMDB 未提供上映日期，本次未搜索资源站"
        elif missing_count:
            status = "missing"
            summary = "电影已上映但媒体库尚未收录"
        else:
            status = "satisfied"
            summary = "电影尚未上映" if not released else "媒体已收录或正在下载"
        resources = self._preview_resource_items(search)
        return {
            "subscription_number": int(row["id"]),
            "title": str(row["title"]),
            "media_type": "movie",
            "enabled": True,
            "status": status,
            "summary": summary,
            "expected_count": int(released),
            "local_count": int(present),
            "missing_count": missing_count,
            "missing": ([{"label": "电影"}] if missing_count else []),
            "inflight_count": int(active),
            "future_count": int(release_known and not released),
            "unknown_air_date_count": int(not release_known),
            "inventory_complete": inventory_complete,
            "inventory_reason": inventory_reason,
            "sources": self._public_sources(sources),
            "resource_search": search,
            "delivery": self._preview_delivery(
                row,
                missing_count=missing_count,
                resources=resources,
                media_type="movie",
                search_status=str(search.get("status") or ""),
            ),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    async def _check_movie(
        self, row: Any, *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        tmdb_id = str(row["tmdb_id"])
        revision = int(row["revision"] or 1)
        try:
            detail = await asyncio.to_thread(TMDBClient().detail, tmdb_id, "movie")
            self._ensure_active_check(int(row["id"]), revision, cancel_event)
        except ProviderNotConfigured as exc:
            raise MediaSubscriptionError("请先配置 TMDB API Key", status_code=503, code="not_configured") from exc
        except ProviderError as exc:
            raise MediaSubscriptionError(exc.safe_message, status_code=exc.status_code, code=exc.code) from exc
        sources = await asyncio.to_thread(inspect_media_identity_sources, tmdb_id, "movie")
        self._ensure_active_check(int(row["id"]), revision, cancel_event)
        complete, reason = self._inventory_complete(sources, identity=True)
        present = any(bool(source.get("present")) for source in sources if source.get("status") == "ready")
        key = build_media_key(tmdb_id, "movie")
        await self._sync_admissions(row, {key} if present else set())
        active = any(
            str(item["media_key"]) == key
            for item in db.list_active_media_download_admissions(int(row["id"]))
        )
        release = _parse_air_date(detail.get("release_date"))
        release_known = release is not None
        released = bool(release and release <= date.today())
        expected_count = 1 if released else 0
        missing = [] if present or active or not released else [_ExpectedMedia(key).public_dict()]
        base = {
            "media_type": "movie", "tmdb_id": tmdb_id, "title": str(row["title"]),
            "expected_count": expected_count, "local_count": int(present),
            "missing_count": len(missing), "missing": missing,
            "inflight_count": int(active), "future_count": int(not released),
            "unknown_air_date_count": int(release is None),
            "inventory_complete": complete, "inventory_reason": reason,
            "sources": self._public_sources(sources), "candidate_count": 0, "auto_submitted": 0,
        }
        if not complete:
            return {**base, "status": "inconclusive", "summary": reason}
        if not release_known and not present and not active:
            return {
                **base,
                "status": "inconclusive",
                "summary": "TMDB 未提供上映日期，已停止自动搜索",
            }
        if not missing:
            summary = "电影尚未上映" if not released else "媒体已收录或正在下载"
            return {**base, "status": "satisfied", "summary": summary}
        candidates = 0
        auto_submitted = 0
        if str(row["action"]) != "notify":
            candidates, auto_submitted = await self._search_movie(
                row, detail, key, cancel_event=cancel_event
            )
        return {
            **base, "status": "missing", "summary": "电影已上映但媒体库尚未收录",
            "candidate_count": candidates, "auto_submitted": auto_submitted,
        }

    @staticmethod
    def _inventory_complete(sources: list[dict[str, Any]], *, identity: bool = False) -> tuple[bool, str]:
        if not sources:
            return False, "未配置 Jellyfin 或 Emby，无法可靠判断是否已收录"
        statuses = {str(source.get("status") or "unavailable") for source in sources}
        if "unavailable" in statuses:
            return False, "部分媒体服务器不可用，本次未执行资源搜索"
        if not identity and ({"ambiguous", "unmapped"} & statuses):
            return False, "媒体服务器映射不完整，本次未执行资源搜索"
        if any(bool(source.get("truncated")) for source in sources):
            return False, "媒体服务器集数清单被截断，本次未执行资源搜索"
        if not statuses <= {"ready", "not_found"}:
            return False, "媒体服务器返回了无法确认的状态"
        return True, "媒体服务器审计完整"

    @staticmethod
    def _public_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for source in sources:
            result.append({
                "server_type": str(source.get("server_type") or ""),
                "server_name": str(source.get("server_name") or "媒体服务器"),
                "status": str(source.get("status") or "unavailable"),
                "present": bool(source.get("present")),
                "local_count": len(source.get("episodes") or []),
                "truncated": bool(source.get("truncated")),
                "error": str(source.get("error") or ""),
            })
        return result

    @staticmethod
    def _empty_preview_search(status: str) -> dict[str, Any]:
        return {
            "status": str(status or "not_run"),
            "attempted_count": 0,
            "candidate_count": 0,
            "truncated": False,
            "items": [],
        }

    @staticmethod
    def _preview_candidate(
        item: dict[str, Any],
        *,
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_quality = quality if isinstance(quality, dict) else {}
        return {
            "result_id": str(item.get("result_id") or ""),
            "site_id": str(item.get("site_id") or "")[:32],
            "site_name": str(item.get("site_name") or "")[:80],
            "title": " ".join(str(item.get("title") or "").split())[:300],
            "size_text": str(item.get("size_text") or "")[:32],
            "seeders": item.get("seeders"),
            "published_at": str(item.get("published_at") or "")[:40],
            "download_state": str(item.get("download_state") or "")[:24],
            "download_kinds": [
                kind
                for kind in item.get("download_kinds", [])
                if kind in {"magnet", "torrent"}
            ][:2],
            "relevance_score": item.get("relevance_score"),
            "rank": safe_quality.get("rank"),
            "score": safe_quality.get("score"),
            "confidence": str(safe_quality.get("confidence") or ""),
            "match": str(safe_quality.get("match") or ""),
            "eligible": bool(safe_quality.get("eligible", True)),
        }

    @staticmethod
    def _preview_resource_items(search: dict[str, Any]) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for entry in search.get("items", []):
            if not isinstance(entry, dict):
                continue
            for candidate in entry.get("candidates", []):
                if isinstance(candidate, dict):
                    resources.append(candidate)
        return resources

    @staticmethod
    def _preview_delivery(
        row: Any,
        *,
        missing_count: int,
        resources: list[dict[str, Any]],
        media_type: str = "tv",
        search_status: str = "",
    ) -> dict[str, Any]:
        mode = str(row["action"] or "confirm")
        target = str(row["download_target"] or "guangya")
        mode_label = {
            "notify": "仅通知",
            "confirm": "确认后提交",
            "auto": "自动提交",
        }.get(mode, "确认后提交")
        target_label = {
            "qb": "qBittorrent",
            "guangya": "光鸭",
            "both": "qBittorrent + 光鸭",
        }.get(target, "光鸭")

        search_partial = str(search_status or "").strip().lower() == "partial"
        if missing_count <= 0:
            state = "no_action"
            summary = "当前不需要提交下载"
        elif not resources and search_partial:
            state = "partial_unavailable"
            summary = "已确认缺失，但资源站核对未完整完成，暂不能判定没有候选"
        elif not resources:
            state = "no_candidate"
            summary = "已确认缺失，但本次没有可提交候选"
        elif mode == "notify":
            state = "notify_only"
            summary = f"当前策略只通知，不会自动推送到{target_label}"
        elif mode == "confirm":
            state = "confirmation_required"
            summary = f"找到候选；提交到{target_label}前需要确认"
        else:
            def _auto_eligible(candidate: dict[str, Any]) -> bool:
                relevance = int(candidate.get("relevance_score") or 0)
                if media_type == "movie":
                    return relevance >= _AUTO_RELEVANCE_THRESHOLD
                return bool(
                    candidate.get("match") == "exact_episode"
                    and candidate.get("confidence") == "high"
                    and relevance >= _AUTO_RELEVANCE_THRESHOLD
                    and candidate.get("download_state") in {"ready", "resolvable"}
                )

            if any(_auto_eligible(candidate) for candidate in resources):
                state = "auto_eligible"
                summary = f"存在满足自动准入条件的候选；实时查询本身不会推送到{target_label}"
            else:
                state = "review_required"
                summary = f"找到候选，但质量不足以自动推送到{target_label}"
        return {
            "mode": mode,
            "mode_label": mode_label,
            "target": target,
            "target_label": target_label,
            "state": state,
            "summary": summary,
            "partial": search_partial,
        }

    async def _preview_search_missing_tv(
        self,
        row: Any,
        detail: dict[str, Any],
        missing: list[_ExpectedMedia],
        *,
        max_search_episodes: int,
        limit_per_media: int,
    ) -> dict[str, Any]:
        if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
            return self._empty_preview_search("disabled")
        service = get_indexer_service()
        sites = _resolve_search_sites(_loads(row["sites_json"], []), detail, service)
        original = str(detail.get("original_name") or row["original_title"] or "")
        aliases = [value for value in (str(row["title"]), original) if value]
        targets = sorted(
            (
                item
                for item in missing
                if item.season is not None and item.episode is not None
            ),
            key=lambda item: (int(item.season or 0), int(item.episode or 0)),
        )[:max_search_episodes]
        items: list[dict[str, Any]] = []
        partial = False
        candidate_total = 0
        for target in targets:
            assert target.season is not None and target.episode is not None
            request = IndexerMediaSearchRequest.create(
                title=str(row["title"]),
                original_title=original,
                aliases=aliases,
                year=row["year"],
                media_type="tv",
                sort_mode="published_desc",
                season=target.season,
                episode=target.episode,
            )
            try:
                aggregated = await run_indexer_awaitable(
                    service.search_media(request, sites or None)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                partial = True
                logger.warning(
                    "媒体订阅实时资源搜索失败 subscription=%s episode=S%02dE%02d type=%s",
                    row["id"], target.season, target.episode, type(exc).__name__,
                )
                items.append({
                    "season": target.season,
                    "episode": target.episode,
                    "label": f"S{target.season:02d}E{target.episode:02d}",
                    "status": "unavailable",
                    "candidate_count": 0,
                    "candidates": [],
                })
                continue
            public = {"items": [item.to_public_dict() for item in aggregated.items]}
            ranked = rank_episode_search(
                public, season=target.season, episode=target.episode
            )
            candidates: list[dict[str, Any]] = []
            for item in ranked.get("items", []):
                quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
                if not quality.get("eligible") or quality.get("match") == "conflict":
                    continue
                candidates.append(self._preview_candidate(item, quality=quality))
                if len(candidates) >= limit_per_media:
                    break
            candidate_total += len(candidates)
            partial = partial or bool(aggregated.partial)
            items.append({
                "season": target.season,
                "episode": target.episode,
                "label": f"S{target.season:02d}E{target.episode:02d}",
                "status": "success" if candidates else "empty",
                "candidate_count": len(candidates),
                "candidates": candidates,
            })
        status = "partial" if partial else ("success" if candidate_total else "empty")
        return {
            "status": status,
            "attempted_count": len(targets),
            "candidate_count": candidate_total,
            "truncated": len(missing) > len(targets),
            "items": items,
        }

    async def _preview_search_movie(
        self,
        row: Any,
        detail: dict[str, Any],
        *,
        limit_per_media: int,
    ) -> dict[str, Any]:
        if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
            return self._empty_preview_search("disabled")
        service = get_indexer_service()
        request = IndexerMediaSearchRequest.create(
            title=str(row["title"]),
            original_title=str(detail.get("original_title") or row["original_title"] or ""),
            year=row["year"],
            media_type="movie",
        )
        try:
            aggregated = await run_indexer_awaitable(
                service.search_media(
                    request,
                    _resolve_search_sites(_loads(row["sites_json"], []), detail, service),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "媒体订阅实时电影资源搜索失败 subscription=%s type=%s",
                row["id"], type(exc).__name__,
            )
            return self._empty_preview_search("unavailable")
        candidates = [
            self._preview_candidate(item.to_public_dict())
            for item in aggregated.items
            if item.download_state in {"ready", "resolvable"}
            and int(item.relevance_score or 0) >= 60
        ][:limit_per_media]
        return {
            "status": "partial" if aggregated.partial else ("success" if candidates else "empty"),
            "attempted_count": 1,
            "candidate_count": len(candidates),
            "truncated": len(aggregated.items) > len(candidates),
            "items": [{
                "label": "电影",
                "status": "success" if candidates else "empty",
                "candidate_count": len(candidates),
                "candidates": candidates,
            }],
        }

    async def _sync_admissions(self, row: Any, local_keys: set[str]) -> None:
        # 该同步段原先按 admission 逐条开连接查询/更新；仓储现在在一个连接中
        # 完成 LEFT JOIN 与状态归并，并整体卸载出 ASGI event loop。
        await asyncio.to_thread(
            db.reconcile_media_download_admissions,
            int(row["id"]),
            set(local_keys),
            expected_revision=int(row["revision"] or 1),
        )

    @staticmethod
    def _available_candidate_pairs(
        candidate_ids: list[int],
        candidates: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        """按持久化状态筛选候选，避免刷新后重试 submitted/dismissed 行。"""
        rows = db.list_media_subscription_candidates_by_ids(candidate_ids)
        available = {
            int(row["id"]): str(row["result_id"] or "")
            for row in rows
            if str(row["status"] or "") == "available"
        }
        return [
            (int(candidate_id), candidate)
            for candidate_id, candidate in zip(candidate_ids, candidates)
            if available.get(int(candidate_id)) == str(candidate.get("result_id") or "")
        ]


    async def _search_missing_tv(
        self,
        row: Any,
        detail: dict[str, Any],
        missing: list[_ExpectedMedia],
        *,
        search_rotation: dict[str, int] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int, dict[str, int] | None]:
        if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
            return 0, 0, search_rotation
        original = str(detail.get("original_name") or row["original_title"] or "")
        aliases = [value for value in (str(row["title"]), original) if value]
        revision = int(row["revision"] or 1)
        self._ensure_active_check(int(row["id"]), revision, cancel_event)
        service = get_indexer_service()
        sites = _resolve_search_sites(_loads(row["sites_json"], []), detail, service)
        candidate_total = 0
        auto_submitted = 0
        targets, next_rotation = _rotated_missing_targets(
            missing,
            search_rotation,
            revision=revision,
            limit=_MAX_SEARCH_EPISODES,
        )
        for target in targets:
            self._ensure_active_check(int(row["id"]), revision, cancel_event)
            assert target.season is not None and target.episode is not None
            request = IndexerMediaSearchRequest.create(
                title=str(row["title"]),
                original_title=original,
                aliases=aliases,
                year=row["year"],
                media_type="tv",
                sort_mode="published_desc",
                season=target.season,
                episode=target.episode,
            )
            try:
                aggregated = await run_indexer_awaitable(
                    service.search_media(request, sites or None)
                )
                self._ensure_active_check(int(row["id"]), revision, cancel_event)
            except (asyncio.CancelledError, MediaSubscriptionError):
                raise
            except Exception as exc:
                logger.warning(
                    "媒体订阅资源搜索失败 subscription=%s episode=S%02dE%02d type=%s",
                    row["id"], target.season, target.episode, type(exc).__name__,
                )
                continue
            public = {"items": [item.to_public_dict() for item in aggregated.items]}
            ranked = rank_episode_search(public, season=target.season, episode=target.episode)
            candidates = []
            for item in ranked.get("items", []):
                quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
                if not quality.get("eligible") or quality.get("match") == "conflict":
                    continue
                candidates.append({
                    **item,
                    "relevance_score": item.get("relevance_score"),
                    "match_reasons": list(quality.get("reasons") or []) + [
                        f"季集匹配：{quality.get('match', 'unknown')}"
                    ],
                    "_quality": quality,
                })
                if len(candidates) >= _MAX_CANDIDATES_PER_MEDIA:
                    break
            ids = db.replace_media_subscription_candidates(
                int(row["id"]), target.media_key,
                season=target.season, episode=target.episode,
                candidates=candidates, expires_at=self._candidate_expiry(),
            )
            candidate_total += len(ids)
            if str(row["action"]) == "auto" and ids and candidates:
                for candidate_id, candidate in self._available_candidate_pairs(ids, candidates):
                    quality = candidate.get("_quality", {})
                    relevance = int(candidate.get("relevance_score") or 0)
                    if not (
                        quality.get("match") == "exact_episode"
                        and quality.get("confidence") == "high"
                        and relevance >= _AUTO_RELEVANCE_THRESHOLD
                        and candidate.get("download_state") in {"ready", "resolvable"}
                    ):
                        continue
                    try:
                        result = await self.download_candidate(
                            candidate_id,
                            str(row["download_target"]),
                            require_active_check=True,
                            cancel_event=cancel_event,
                        )
                    except MediaSubscriptionError as exc:
                        if exc.code == "unavailable":
                            continue
                        raise
                    auto_submitted += int(bool(result.get("ok") or result.get("duplicate")))
                    break
        return candidate_total, auto_submitted, next_rotation

    async def _search_movie(
        self,
        row: Any,
        detail: dict[str, Any],
        media_key: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        if not config.get_bool("INDEXER_SEARCH_ENABLED", True):
            return 0, 0
        revision = int(row["revision"] or 1)
        self._ensure_active_check(int(row["id"]), revision, cancel_event)
        service = get_indexer_service()
        request = IndexerMediaSearchRequest.create(
            title=str(row["title"]),
            original_title=str(detail.get("original_title") or row["original_title"] or ""),
            year=row["year"],
            media_type="movie",
        )
        try:
            aggregated = await run_indexer_awaitable(
                service.search_media(
                    request,
                    _resolve_search_sites(_loads(row["sites_json"], []), detail, service),
                )
            )
            self._ensure_active_check(int(row["id"]), revision, cancel_event)
        except (asyncio.CancelledError, MediaSubscriptionError):
            raise
        except Exception as exc:
            logger.warning("媒体订阅电影搜索失败 subscription=%s type=%s", row["id"], type(exc).__name__)
            return 0, 0
        candidates = [
            item.to_public_dict() for item in aggregated.items
            if item.download_state in {"ready", "resolvable"}
            and int(item.relevance_score or 0) >= 60
        ][:_MAX_CANDIDATES_PER_MEDIA]
        ids = db.replace_media_subscription_candidates(
            int(row["id"]), media_key, season=None, episode=None,
            candidates=candidates, expires_at=self._candidate_expiry(),
        )
        auto_submitted = 0
        if str(row["action"]) == "auto" and ids and candidates:
            for candidate_id, candidate in self._available_candidate_pairs(ids, candidates):
                if int(candidate.get("relevance_score") or 0) < _AUTO_RELEVANCE_THRESHOLD:
                    continue
                try:
                    result = await self.download_candidate(
                        candidate_id,
                        str(row["download_target"]),
                        require_active_check=True,
                        cancel_event=cancel_event,
                    )
                except MediaSubscriptionError as exc:
                    if exc.code == "unavailable":
                        continue
                    raise
                auto_submitted = int(bool(result.get("ok") or result.get("duplicate")))
                break
        return len(ids), auto_submitted

    @staticmethod
    def _candidate_expiry() -> str:
        ttl = max(60, min(config.get_int("INDEXER_RESULT_TTL_SECONDS", 600), 3600))
        safe_ttl = max(45, ttl - 15)
        return (datetime.now() + timedelta(seconds=safe_ttl)).strftime("%Y-%m-%d %H:%M:%S")

    async def _candidate_still_missing(self, subscription: Any, candidate: Any) -> bool:
        """在产生外部下载副作用前，以当前媒体库库存复核候选。"""
        media_type = str(subscription["media_type"] or "").strip().lower()
        tmdb_id = str(subscription["tmdb_id"] or "").strip()
        if media_type == "movie":
            sources = await asyncio.to_thread(
                inspect_media_identity_sources, tmdb_id, "movie"
            )
            if not sources:
                # 未配置媒体服务器时维持原有手动下载能力；只有存在库存来源时
                # 才执行提交前二次校验，避免把“无法校验”误当成系统故障。
                return True
            complete, reason = self._inventory_complete(sources, identity=True)
            if not complete:
                raise MediaSubscriptionError(
                    reason or "媒体库库存暂时无法确认",
                    status_code=503,
                    code="inventory_unavailable",
                )
            return not any(
                bool(source.get("present"))
                for source in sources
                if source.get("status") == "ready"
            )

        if media_type != "tv":
            raise MediaSubscriptionError(
                "媒体订阅类型无效", status_code=409, code="unavailable"
            )
        try:
            season = int(candidate["season"])
            episode = int(candidate["episode"])
        except (TypeError, ValueError) as exc:
            raise MediaSubscriptionError(
                "候选集号无效", status_code=409, code="unavailable"
            ) from exc
        sources = await asyncio.to_thread(
            inspect_series_episode_sources,
            str(subscription["title"] or ""),
            tmdb_id=tmdb_id,
            max_episodes=2000,
            include_specials=bool(subscription["include_specials"]),
        )
        if not sources:
            return True
        complete, reason = self._inventory_complete(sources)
        if not complete:
            raise MediaSubscriptionError(
                reason or "媒体库库存暂时无法确认",
                status_code=503,
                code="inventory_unavailable",
            )
        present = any(
            (season, episode) in {
                (int(item[0]), int(item[1]))
                for item in source.get("episodes", [])
                if isinstance(item, (list, tuple)) and len(item) == 2
            }
            for source in sources
            if source.get("status") == "ready"
        )
        return not present

    async def download_candidate(
        self,
        candidate_id: int,
        target: str = "",
        *,
        require_active_check: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        candidate = db.get_media_subscription_candidate(int(candidate_id))
        if candidate is None:
            raise MediaSubscriptionError("候选资源不存在", status_code=404, code="not_found")
        if str(candidate["status"]) != "available":
            raise MediaSubscriptionError("候选资源已失效或已提交", status_code=409, code="unavailable")
        subscription = db.get_media_subscription(int(candidate["subscription_id"]))
        if subscription is None:
            raise MediaSubscriptionError("媒体订阅不存在", status_code=404, code="not_found")
        if not bool(subscription["enabled"]):
            raise MediaSubscriptionError("媒体订阅已暂停", status_code=409, code="paused")
        revision = int(subscription["revision"] or 1)
        normalized_target = _choice(target or subscription["download_target"], _ALLOWED_TARGETS, "guangya")
        if require_active_check:
            self._ensure_active_check(int(subscription["id"]), revision, cancel_event)
        elif cancel_event is not None and cancel_event.is_set():
            raise MediaSubscriptionError(
                "应用正在停止，下载提交已取消", status_code=409, code="cancelled"
            )
        if not await self._candidate_still_missing(subscription, candidate):
            db.update_media_subscription_candidate(int(candidate["id"]), status="expired")
            raise MediaSubscriptionError(
                "该媒体已入库，候选已失效",
                status_code=409,
                code="already_present",
            )
        admission_id = db.claim_media_download_admission(
            media_key=str(candidate["media_key"]), tmdb_id=str(subscription["tmdb_id"]),
            media_type=str(subscription["media_type"]), subscription_id=int(subscription["id"]),
            candidate_id=int(candidate["id"]), season=candidate["season"], episode=candidate["episode"],
            subscription_revision=revision,
            require_active_check=require_active_check,
            check_run_id=_ACTIVE_CHECK_RUN_ID.get() if require_active_check else None,
        )
        if admission_id == 0:
            raise MediaSubscriptionError(
                "订阅已暂停或检查状态已变化", status_code=409, code="cancelled"
            )
        if admission_id is None:
            return {
                "ok": False, "duplicate": True, "status": "duplicate",
                "error": "该媒体已在下载或入库处理中",
            }
        if cancel_event is not None and cancel_event.is_set():
            db.update_media_download_admission(
                admission_id,
                expected_statuses=("claimed",),
                status="cancelled",
                error="应用正在停止，下载提交已取消",
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            raise MediaSubscriptionError(
                "应用正在停止，下载提交已取消", status_code=409, code="cancelled"
            )
        if not db.begin_media_download_dispatch(
            admission_id,
            subscription_id=int(subscription["id"]),
            subscription_revision=revision,
            require_active_check=require_active_check,
            check_run_id=_ACTIVE_CHECK_RUN_ID.get() if require_active_check else None,
        ):
            raise MediaSubscriptionError(
                "订阅已暂停、删除或配置已变更", status_code=409, code="cancelled"
            )
        if cancel_event is not None and cancel_event.is_set():
            db.update_media_download_admission(
                admission_id,
                expected_statuses=("dispatching",),
                status="cancelled",
                error="应用正在停止，下载提交已取消",
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            raise MediaSubscriptionError(
                "应用正在停止，下载提交已取消", status_code=409, code="cancelled"
            )
        try:
            result = await run_indexer_awaitable(
                download_indexer_result_public(
                    get_indexer_service(),
                    str(candidate["result_id"]),
                    normalized_target,
                    admission_id=admission_id,
                )
            )
        except Exception as exc:
            db.fail_unbound_media_download_admission(admission_id, "下载提交异常")
            raise MediaSubscriptionError(
                "下载提交失败", status_code=502, code="download_failed"
            ) from exc
        request_id = int(result.get("request_id") or 0)
        manual_review = str(result.get("status") or "") == "manual_review"
        if result.get("ok") or result.get("duplicate") or manual_review:
            db.update_media_subscription_candidate(
                int(candidate["id"]), status="submitted", request_id=request_id or None
            )
            request_row = db.get_download_request(request_id) if request_id else None
            request_status = str(request_row["status"] or "") if request_row else ""
            admission_status = {
                "downloading": "downloading",
                "completed": "processing",
                "manual_review": "processing",
            }.get(request_status, "processing" if manual_review else "submitted")
            db.update_media_download_admission(
                admission_id,
                expected_statuses=(
                    "claimed", "dispatching", "submitted", "downloading", "processing",
                ),
                status=admission_status, request_id=request_id or None,
                error=(
                    str(request_row["error"] or "")[:500]
                    if request_status == "manual_review" and request_row
                    else str(result.get("error") or "")[:500] if manual_review
                    else ""
                ),
            )
        else:
            message = str(result.get("error") or "下载提交失败")[:500]
            if request_id and message in {"下载提交失败", "下载处理失败"}:
                request_row = db.get_download_request(request_id)
                dispatch_error = str(request_row["error"] or "").strip() if request_row else ""
                if dispatch_error:
                    message = redact_sensitive_text(dispatch_error)[:500]
            if request_id:
                request_row = db.get_download_request(request_id)
                request_status = str(request_row["status"] or "") if request_row else ""
                admission_status = {
                    "failed": "failed",
                    "submitted": "submitted",
                    "downloading": "downloading",
                    "completed": "processing",
                    "manual_review": "processing",
                }.get(request_status, "processing")
                db.update_media_download_admission(
                    admission_id,
                    expected_statuses=(
                        "claimed", "dispatching", "submitted", "downloading", "processing",
                    ),
                    status=admission_status, request_id=request_id, error=message,
                    completed_at=(db.now() if admission_status == "failed" else None),
                )
            else:
                db.update_media_download_admission(
                    admission_id,
                    expected_statuses=("claimed", "dispatching"),
                    status="failed", request_id=None, error=message,
                    completed_at=db.now(),
                )
            raise MediaSubscriptionError(
                message, status_code=502, code="download_failed"
            )
        return {**result, "candidate_id": int(candidate["id"]), "admission_id": admission_id}

    def stats(self) -> dict[str, int]:
        return db.get_media_subscription_stats()


_service = MediaSubscriptionService()


def get_media_subscription_service() -> MediaSubscriptionService:
    return _service
