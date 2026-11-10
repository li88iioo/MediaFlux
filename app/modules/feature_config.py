"""白名单功能事实、配置 CAS 与 Web 共用的运行时刷新。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
import hashlib
import json
import time
from typing import Any

from app import config
from app.defaults import DEFAULT_DISCOVERY_ENABLED, DEFAULT_INDEXER_SEARCH_ENABLED
from app.indexers.config import current_indexer_site_ids
from app.logger import get_logger

logger = get_logger(__name__)

FEATURES = {
    "discovery": ("DISCOVERY_ENABLED", "媒体探索", DEFAULT_DISCOVERY_ENABLED),
    "douban": ("DISCOVERY_DOUBAN_ENABLED", "豆瓣探索", True),
    "resource_results": ("DISCOVERY_RESOURCE_RESULTS_ENABLED", "探索页站点资源结果", True),
    "indexer_search": ("INDEXER_SEARCH_ENABLED", "多站资源搜索", DEFAULT_INDEXER_SEARCH_ENABLED),
    "web_search": ("WEB_SEARCH_ENABLED", "Tavily 联网搜索", False),
    "web_read": ("WEB_READ_ENABLED", "本地网页读取", True),
    "offline_magnet": ("OFFLINE_MAGNET_ENABLED", "光鸭磁力链接离线转存", True),
    "offline_ed2k": ("OFFLINE_ED2K_ENABLED", "光鸭 ED2K 离线转存", True),
    "offline_http": ("OFFLINE_HTTP_ENABLED", "光鸭 HTTP 链接离线转存", False),
    "strm_metadata": ("STRM_METADATA_ENABLED", "STRM 伴随元数据同步", False),
    "download_verification_notify": ("AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED", "下载后入库复核通知", True),
}
BOOLEAN_VALUES = {**dict.fromkeys(("1", "true", "yes", "on", "y"), "1"),
                  **dict.fromkeys(("0", "false", "no", "off", "n"), "0")}
DISCOVERY_RUNTIME_KEYS = frozenset({
    "DISCOVERY_ENABLED", "DISCOVERY_CACHE_TTL_SECONDS", "DISCOVERY_STALE_TTL_SECONDS",
    "DISCOVERY_RESOURCE_RESULTS_ENABLED", "DISCOVERY_DOUBAN_ENABLED", "DOUBAN_DBCL2",
    "DOUBAN_CACHE_TTL_SECONDS", "BANGUMI_USER_AGENT", "ORGANIZE_DOUBAN_HINTS_ENABLED",
    "ORGANIZE_BANGUMI_HINTS_ENABLED", "TMDB_API_KEY", "TMDB_API_URL", "TMDB_MATCH_MODE", "PROXY_URL",
})

WEB_CACHE_KEYS = frozenset({"TAVILY_SEARCH_DEPTH", "TAVILY_MAX_RESULTS", "TAVILY_TIMEOUT_SECONDS",
    "TAVILY_CACHE_TTL_SECONDS", "WEB_READ_TIMEOUT_SECONDS", "WEB_READ_CACHE_TTL_SECONDS"})


class FeatureConflict(ValueError):
    """确认事实或配置文件 CAS 已变化，未提交功能写入。"""


def normalize_boolean(key: str, value: Any) -> str:
    """沿用设置页接受的布尔文本；SDK 写入另要求真实 bool。"""
    normalized = BOOLEAN_VALUES.get(str(value).strip().lower())
    if normalized is None:
        raise ValueError(f"{key} 必须是布尔值（0/1、true/false）")
    return normalized


def _facts() -> dict[str, Any]:
    enabled = {name: config.get_bool(key, default) for name, (key, _, default) in FEATURES.items()}
    sites = current_indexer_site_ids()
    tavily = bool(config.get("TAVILY_API_KEY", "").strip())
    items = []
    for name, (key, label, _) in FEATURES.items():
        reasons = []
        if not enabled[name]:
            reasons.append("feature_disabled")
        else:
            if name in {"douban", "resource_results"} and not enabled["discovery"]:
                reasons.append("parent_disabled")
            if name == "resource_results" and not enabled["indexer_search"]:
                reasons.append("search_disabled")
            if name in {"resource_results", "indexer_search"} and not sites:
                reasons.append("no_enabled_sites")
            if name == "web_search" and not tavily:
                reasons.append("provider_not_configured")
        items.append({"feature": name, "label": label, "enabled": enabled[name],
                      "availability": "disabled" if not enabled[name] else "blocked" if reasons else "available",
                      "reason_codes": reasons, "managed_by_environment": config.has_external_override(key)})
    available = sum(row["availability"] == "available" for row in items)
    disabled = sum(not row["enabled"] for row in items)
    blocked = len(items) - available - disabled
    return {"features": items, "feature_count": len(items), "enabled_count": len(items) - disabled,
            "available_count": available, "disabled_count": disabled, "attention_count": blocked,
            "status": "attention" if blocked else "ready" if available else "disabled",
            "indexer_site_count": len(sites), "tavily_configured": tavily}


def features() -> dict[str, Any]:
    """读取当前文件和部署覆盖的完整白名单状态，不回显配置值、站点或凭据。"""
    with config.runtime_config_read_guard(), config.use_runtime_config_snapshot(config.capture_runtime_config_snapshot()):
        return _facts()


def _prepare(feature: str, enabled: bool) -> tuple[bytes | None, dict[str, Any]]:
    if feature not in FEATURES or type(enabled) is not bool:
        raise ValueError("feature 必须是白名单功能名称，enabled 必须是布尔值")
    raw, _ = config.read_env_snapshot(config.ENV_FILE)
    facts = features()
    item = next(row for row in facts["features"] if row["feature"] == feature)
    label = item["label"]
    status, ok, message = "confirmation_required", True, f"确认后将{'开启' if enabled else '关闭'}{label}"
    if item["managed_by_environment"]:
        status, ok, message = "environment_override", False, f"{label}由运行环境管理，不能覆盖部署配置"
    elif item["enabled"] == enabled:
        status, message = "no_changes", f"{label}已经处于目标状态"
    elif enabled and feature in {"resource_results", "indexer_search"} and not facts["indexer_site_count"]:
        status, ok, message = "not_configured", False, "当前没有可用资源站点，请先在设置页选择"
    elif enabled and feature == "web_search" and not facts["tavily_configured"]:
        status, ok, message = "not_configured", False, "联网搜索供应商尚未配置，请先在设置页配置 Tavily API Key"
    fingerprint = hashlib.sha256(json.dumps({"feature": feature, "enabled": enabled, "facts": facts,
        "file_present": raw is not None, "file_sha256": hashlib.sha256(raw or b"").hexdigest()},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    effects = ["仅改变这一项白名单功能，后续请求使用新状态；不会启动下载、搜索或整理任务。",
               "关闭不会取消已发出的请求；STRM 元数据从下一次同步生效；链接转存不重跑历史消息。"]
    suggestions = []
    if enabled and feature in {"douban", "resource_results"}:
        parent = next(row for row in facts["features"] if row["feature"] == "discovery")
        if not parent["enabled"]:
            suggestions.append("媒体探索总开关关闭；本设置会保存，开启总开关后才生效。")
    if enabled and feature == "resource_results":
        search = next(row for row in facts["features"] if row["feature"] == "indexer_search")
        if not search["enabled"]:
            suggestions.append("探索页资源结果需同时开启多站资源搜索。")
    return raw, {"fingerprint": fingerprint, "preview": {"ok": ok, "status": status, "summary": message,
        "data": {"feature": feature, "label": label, "current_enabled": item["enabled"],
                 "requested_enabled": enabled, "effects": effects}, "suggestions": suggestions,
        **({"error": message} if not ok else {})}}


def prepare_feature(feature: str, enabled: bool) -> dict[str, Any]:
    with config.runtime_config_read_guard():
        return _prepare(feature, enabled)[1]


AGENT_EPISODE_RESEARCH_KEYS = frozenset({
    "AGENT_EPISODE_RESEARCH_ENABLED",
    "AGENT_EPISODE_RESEARCH_DAILY_LIMIT",
})
MEDIA_SERVER_PROFILE_KEYS = frozenset({
    "JELLYFIN_ENABLED", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER_ID",
    "EMBY_ENABLED", "EMBY_URL", "EMBY_TOKEN", "EMBY_USER_ID",
})
NSFW_ORGANIZE_KEYS = frozenset({
    "GY_ORGANIZE_NSFW_ENABLED",
    "GY_ORGANIZE_NSFW_SOURCE_IDS",
    "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT",
    "GY_ORGANIZE_NSFW_METATUBE_TOKEN",
    "GY_ORGANIZE_NSFW_CATEGORY_NAME",
    "GY_ORGANIZE_NSFW_STRIP_DOMAINS",
    "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS",
})
AI_RECOGNITION_KEYS = frozenset({
    "AI_RECOGNITION_ENABLED",
    "AI_RECOGNITION_CONFIDENCE_THRESHOLD",
    "AI_RECOGNITION_REQUESTS_PER_MINUTE",
    "AI_RECOGNITION_DAILY_REQUEST_LIMIT",
    "AI_RECOGNITION_MAX_CONCURRENCY",
    "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS",
})
ORGANIZE_TAVILY_KEYS = frozenset({
    "ORGANIZE_TAVILY_HINTS_ENABLED",
    "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT",
})
_RECOGNITION_WEB_RUNTIME_KEYS = ORGANIZE_TAVILY_KEYS | {
    "TAVILY_API_KEY", "TAVILY_TIMEOUT_SECONDS", "TAVILY_CACHE_TTL_SECONDS",
}
_REVIEW_DISPATCHER_KEYS = AGENT_EPISODE_RESEARCH_KEYS | {
    "AGENT_RECOGNITION_REVIEW_ENABLED", "AGENT_NSFW_CLEAN_REVIEW_ENABLED",
}


class _Reload:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.restart = False

    def warn(self, message: str, *, restart: bool = True) -> None:
        self.warnings.append(message)
        self.restart = self.restart or restart


def reload_runtime(
    changed_keys: Iterable[str], *, background_services: bool,
    retired_source_ids: Iterable[str] = (), retry_keys: Iterable[str] = (),
) -> tuple[list[str], bool]:
    """配置落盘后的唯一运行时刷新入口；Web 保存与 Agent 写入共用同一规则。

    ``retry_keys`` 是值未变但已提交的键，只用于重试本地媒体运行时切换。
    """
    keys = set(changed_keys)
    outcome = _Reload()
    _refresh_recognition_caches(keys)
    _refresh_discovery_and_web(keys, outcome)
    _refresh_local_media(keys | set(retry_keys), outcome)
    _refresh_bot_and_agent(keys, outcome, background_services=background_services)
    _refresh_media_servers(keys, outcome, background_services=background_services)
    _refresh_schedulers(keys, outcome, background_services=background_services,
                        retired_source_ids=tuple(retired_source_ids))
    return outcome.warnings, outcome.restart


def _refresh_recognition_caches(keys: set[str]) -> None:
    if AI_RECOGNITION_KEYS & keys:
        try:
            from app.modules.ai_recognition_governance import clear_ai_recognition_governance

            clear_ai_recognition_governance()
        except Exception as exc:
            logger.warning("AI 识别治理状态重置失败 type=%s", type(exc).__name__)
    if _RECOGNITION_WEB_RUNTIME_KEYS & keys:
        try:
            from app.modules.recognition_web_hints import clear_recognition_web_hint_cache

            clear_recognition_web_hint_cache()
        except Exception as exc:
            logger.warning("整理标题线索缓存重置失败 type=%s", type(exc).__name__)


def _refresh_discovery_and_web(keys: set[str], outcome: _Reload) -> None:
    if DISCOVERY_RUNTIME_KEYS & keys:
        try:
            from app.discovery.search import shutdown_discovery_search_service
            from app.discovery.service import shutdown_discovery_service
            from app.modules.recognition_hints import clear_recognition_hint_cache

            results = (shutdown_discovery_service(), shutdown_discovery_search_service())
            clear_recognition_hint_cache()
            if False in results:
                outcome.warn("发现页配置已保存，但运行中实例未能热更新；重启后生效")
        except Exception as exc:
            logger.warning("发现页运行时配置热更新失败 type=%s", type(exc).__name__)
            outcome.warn("发现页配置已保存，但运行中实例未能热更新；重启后生效")
    if any(key.startswith("INDEXER_") for key in keys):
        from app.indexers.runtime import refresh_indexer_configuration

        runtime = refresh_indexer_configuration()
        for name, label in (("web", "Web"), ("telegram", "Telegram")):
            if not runtime[name]:
                outcome.warn(f"{label} 索引器配置已保存，但运行中实例未能热更新；重启后生效")
    if {"LOGIN_WALLPAPER_MODE", "TMDB_API_KEY"} & keys:
        try:
            from app.modules.login_wallpaper import schedule_login_wallpaper_refresh
            # False 也表示默认壁纸或正在单飞刷新，沿用 Web 的正常跳过语义。
            schedule_login_wallpaper_refresh(force=True)
        except Exception as exc:  # noqa: BLE001 -- 已保存配置，刷新异常须与发布结果区分
            logger.warning("登录壁纸后台刷新失败 type=%s", type(exc).__name__)
            outcome.warn("壁纸配置已保存，后台刷新失败")
    if WEB_CACHE_KEYS & keys:
        try:
            from app.modules.web_evidence import clear_web_cache
            clear_web_cache()
        except Exception as exc:  # noqa: BLE001 -- 保存已完成，必须记录刷新失败而不是误报未写
            logger.warning("网页缓存重置失败 type=%s", type(exc).__name__)
            outcome.warn("网页策略已保存，但缓存未完整刷新；重启后生效")


def _refresh_local_media(keys: set[str], outcome: _Reload) -> None:
    """正式发布和原值重试共用同一切换结果，不能用 no-op 掩盖待切换状态。"""
    from app.modules.local_media_service import LOCAL_MEDIA_RUNTIME_KEYS

    if not set(LOCAL_MEDIA_RUNTIME_KEYS) & keys:
        return
    try:
        from app.modules.local_media_scheduler import reload_local_media_scheduler_config
        from app.modules.local_media_service import reload_local_media_service

        web_updated = reload_local_media_service()
        scheduler_updated = reload_local_media_scheduler_config()
        if not (web_updated and scheduler_updated):
            outcome.warn(
                "本地媒体配置已保存，运行时切换尚未完成；在途操作不受影响，新操作会重试切换，请稍后重试",
                restart=False,
            )
    except Exception as exc:  # noqa: BLE001 - 已落盘配置的切换边界必须报告 warning，不能伪报成功。
        logger.warning("本地媒体配置热更新失败 type=%s", type(exc).__name__)
        outcome.warn("本地媒体配置已保存，但运行时切换失败；请重试或重启服务")


def _refresh_bot_and_agent(keys: set[str], outcome: _Reload, *, background_services: bool) -> None:
    if {"TG_BOT_TOKEN", "TG_CHAT_ID"} & keys:
        started = time.perf_counter()
        try:
            if background_services:
                from app.bot import restart_bot

                if not restart_bot():
                    outcome.warn("Telegram Bot 配置已保存，但旧连接未及时退出；请稍后重试或重启 MediaFlux 服务")
                    logger.warning("Telegram Bot 配置热更新未完成：旧 polling 仍在退出")
            else:
                from app.notifier import reset

                reset()
        except Exception as exc:
            outcome.warn("Telegram Bot 配置已保存，但运行中实例热更新失败；请稍后重试或重启 MediaFlux 服务")
            logger.warning("Telegram Bot 配置热更新失败 type=%s", type(exc).__name__)
        finally:
            logger.info("Telegram Bot 配置热更新 ms=%s", max(1, round((time.perf_counter() - started) * 1000)))
    elif background_services and {"AGENT_ENABLED", "TG_AGENT_ENABLED"} & keys:
        try:
            from app.bot.handlers import request_command_menu_refresh

            request_command_menu_refresh()
        except Exception as exc:
            logger.warning("Telegram Bot 命令菜单后台刷新失败 type=%s", type(exc).__name__)
    if "AGENT_ENABLED" in keys and background_services:
        try:
            from app.modules.agent_runtime import request_agent_runtime_reconcile

            request_agent_runtime_reconcile()
        except Exception as exc:
            logger.warning("Agent 总开关后台热更新启动失败 type=%s", type(exc).__name__)


def _refresh_media_servers(keys: set[str], outcome: _Reload, *, background_services: bool) -> None:
    if not MEDIA_SERVER_PROFILE_KEYS & keys:
        return
    from app.services import clear_dashboard_cache

    clear_dashboard_cache()
    if not background_services:
        return
    from app.modules.media_proxy import get_media_proxy_manager

    try:
        if not get_media_proxy_manager().request_reconcile():
            outcome.warn("媒体反代配置已保存，但运行中实例未接受热更新；重启后生效")
    except Exception as exc:
        outcome.warn("媒体反代配置已保存，但运行中实例未能热更新；重启后生效")
        logger.warning("媒体反代配置热加载失败 type=%s", type(exc).__name__)


def _refresh_schedulers(
    keys: set[str], outcome: _Reload, *, background_services: bool, retired_source_ids: tuple[str, ...],
) -> None:
    if any(key.startswith(("STRM_", "GY_STRM_")) for key in keys):
        try:
            from app.modules.scheduler import get_scheduler

            scheduler = get_scheduler()
            scheduler.reload()
            if retired_source_ids and background_services:
                result = scheduler.trigger("config-retirement")
                if not result.get("ok"):
                    logger.info("STRM 退役来源已入队，等待下次同步处理: %s", result.get("error", "任务繁忙"))
        except Exception as exc:
            outcome.warn("STRM 调度配置已保存，但运行中调度器未能热更新；重启后生效")
            logger.warning("STRM 调度配置热加载失败: %s", exc)
    if any(key.startswith("GY_ORGANIZE_") for key in keys):
        try:
            from app.modules.organize_scheduler import get_organize_scheduler

            get_organize_scheduler().reload()
        except Exception as exc:
            outcome.warn("整理调度配置已保存，但运行中调度器未能热更新；重启后生效")
            logger.warning("整理调度配置热加载失败: %s", exc)
        if NSFW_ORGANIZE_KEYS & keys:
            try:
                from app.modules.nsfw import clear_nsfw_cache

                clear_nsfw_cache()
            except Exception as exc:
                logger.warning("MetaTube 识别缓存重置失败: %s", exc)
    if any(key.startswith("AGENT_LIBRARY_PATROL_") for key in keys):
        started = time.perf_counter()
        try:
            from app.modules.agent_library_patrol_scheduler import get_agent_library_patrol_scheduler

            get_agent_library_patrol_scheduler().reload(immediate=False)
        except Exception as exc:
            outcome.warn("Agent 巡检配置已保存，但运行中调度器未能热更新；重启后生效")
            logger.warning("Agent 全库缺集巡检配置热加载失败 type=%s", type(exc).__name__)
        finally:
            logger.info("Agent 巡检配置热更新 ms=%s", max(1, round((time.perf_counter() - started) * 1000)))
    if _REVIEW_DISPATCHER_KEYS & keys:
        try:
            from app.modules.organize_confirmations import wake_recognition_review_dispatcher

            wake_recognition_review_dispatcher()
        except Exception as exc:
            logger.warning("Agent 主动识别复核配置热加载失败 type=%s", type(exc).__name__)
    if any(key.startswith("AGENT_DOWNLOAD_VERIFICATION_") for key in keys):
        try:
            from app.modules.agent_download_verification_scheduler import (
                get_download_library_verification_scheduler,
            )

            get_download_library_verification_scheduler().reload()
        except Exception as exc:
            if background_services:
                outcome.warn("下载后复核配置已保存，但运行中调度器未能热更新；重启后生效")
                logger.warning("Agent 下载后媒体库复核配置热加载失败 type=%s", type(exc).__name__)
            else:
                logger.debug("后台服务未启动，跳过下载后媒体库复核调度器热加载结果 type=%s", type(exc).__name__)
    if "DOWNLOAD_TORRENT_RETENTION_DAYS" in keys:
        try:
            from app.modules.download_tracker import get_download_tracker

            get_download_tracker().reload(reset_torrent_cleanup=True)
        except Exception as exc:
            outcome.warn("种子保留期配置已保存，但下载跟踪器未能热更新；重启后生效")
            logger.warning("原始种子保留期配置热加载失败 type=%s", type(exc).__name__)


def set_feature(feature: str, enabled: bool, *, expected_fingerprint: str,
                check_write: Callable[[], None] | None = None,
                admission: Callable[[], AbstractContextManager] = nullcontext) -> dict[str, Any]:
    """锁内重算稳定事实，再使用原配置发布器的文件 CAS，不保存 user.env 到确认卡。"""
    with admission(), config.runtime_config_read_guard():
        raw, prepared = _prepare(feature, enabled)
        if prepared["fingerprint"] != expected_fingerprint:
            raise FeatureConflict("配置已变化，请重新预检")
        preview = prepared["preview"]
        if preview["status"] != "confirmation_required":
            return preview
        key, label, _ = FEATURES[feature]
        if check_write:
            check_write()
        try:
            config.update_runtime_env_file(config.ENV_FILE, {key: normalize_boolean(key, enabled)}, expected=raw)
        except (config.ConcurrentConfigUpdateError, config.ExternalConfigOverrideError) as exc:
            raise FeatureConflict("配置或部署覆盖已变化，请重新预检") from exc
    # Agent 写入只发生在运行中的服务内。
    warnings, restart = reload_runtime({key}, background_services=True)
    _, persisted = config.read_env_snapshot(config.ENV_FILE)
    verified = persisted.get(key) == ("1" if enabled else "0")
    ok = verified and not restart
    return {"ok": ok, "status": "completed" if ok else "outcome_unknown",
                "summary": f"{label}配置已{'开启' if enabled else '关闭'}" if ok else "功能配置已提交，最终状态或运行时待核验；请先读取状态，不要重放",
                "data": {"feature": feature, "label": label, "enabled": enabled, "saved": verified,
                         "runtime_refreshed": not restart, "runtime_scope": "current_process",
                         "verification_state": "verified" if verified else "pending"},
                "suggestions": warnings, **({"error": "请先核对配置与运行时状态，不要直接重试旧确认。"} if not ok else {})}
