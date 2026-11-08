"""资源站点配置的共享白名单与规范化逻辑。"""
from __future__ import annotations

import hashlib
import json
import secrets
from base64 import b64decode
from collections.abc import Iterable
from contextlib import nullcontext
from typing import Any

from app import config


# 内置地址仅作Base64轻度混淆；运行时白名单和TLS策略不变。
def _decode(encoded: str) -> str:
    return b64decode(encoded, validate=True).decode("ascii")


NYAA_URL = _decode("aHR0cHM6Ly9ueWFhLnNpLw==")
NYAA_MIRROR_URL = _decode("aHR0cHM6Ly9ueWFhLm5ldC8=")
SUKEBEI_URL = _decode("aHR0cHM6Ly9zdWtlYmVpLm55YWEuc2kv")
MIKAN_URL = _decode("aHR0cHM6Ly9taWthbmFuaS5tZS8=")
MIKAN_MIRROR_URL = _decode("aHR0cHM6Ly9taWthbmltZS50di8=")
BTBTLA_URL = _decode("aHR0cHM6Ly93d3cuYnRidGxiLmNvbS8=")
BTBTLA_MIRROR_URL = _decode("aHR0cHM6Ly9idGJ0bGIuY29tLw==")
AIPAN_URL = _decode("aHR0cHM6Ly93d3cuYWlwYW4ubWUv")
AIPAN_SEARCH_URL = _decode("aHR0cHM6Ly93d3cuYWlwYW4ubWUvYXBpL21vdmllcy9zZWFyY2g=")
DYGANG_URL = _decode("aHR0cHM6Ly93d3cuZHlnYW5nLnR2Lw==")
YS5266_URL = _decode("aHR0cHM6Ly93d3cuNTI2NnlzLm5ldC8=")
TPB_URL = _decode("aHR0cHM6Ly90aGVwaXJhdGViYXkub3JnLw==")
TPB_API_URL = _decode("aHR0cHM6Ly9hcGliYXkub3JnL3EucGhw")

INDEXER_SITE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("nyaa", "Nyaa"),
    ("mikan", "Mikan"),
    ("btbtla", "综合"),
    ("aipan", "爱盼"),
    ("dygang", "电影港"),
    ("ys5266", "5266影视"),
    ("kpkuang", "看片狂人"),
    ("tpb", "The Pirate Bay"),
    ("sukebei", "Sukebei"),
)
INDEXER_SITE_ORDER = tuple(site_id for site_id, _label in INDEXER_SITE_DEFINITIONS)
INDEXER_SITE_LABELS = dict(INDEXER_SITE_DEFINITIONS)
AGENT_ONLY_INDEXER_SITE_IDS = frozenset({"aipan", "dygang", "ys5266", "kpkuang"})
SENSITIVE_INDEXER_SITE_IDS = frozenset({"sukebei"})
MANUAL_INDEXER_SITE_ORDER = tuple(
    site_id for site_id in INDEXER_SITE_ORDER
    if site_id not in AGENT_ONLY_INDEXER_SITE_IDS
)
DEFAULT_INDEXER_SITE_IDS = ("nyaa", "mikan", "btbtla", "tpb")
INDEXER_SITE_CONFIG_VERSION_KEY = "INDEXER_SITE_CONFIG_VERSION"
INDEXER_SITE_CONFIG_VERSION = "3"
RETIRED_INDEXER_SITE_IDS = frozenset({"animetosho", "1lou"})


def normalize_indexer_site_ids(value: Any) -> tuple[str, ...]:
    """严格规范化固定站点 ID，去重并按产品顺序返回。"""
    return _normalize_indexer_site_ids(value)


def normalize_persisted_indexer_site_ids(value: Any) -> tuple[str, ...]:
    """读取旧配置时丢弃已下线与已移出手动集合的站点。"""
    return _normalize_indexer_site_ids(
        value,
        ignored=RETIRED_INDEXER_SITE_IDS | AGENT_ONLY_INDEXER_SITE_IDS,
    )


def _normalize_indexer_site_ids(
    value: Any,
    *,
    ignored: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value
        if "\r" in raw or "\n" in raw or len(raw) > 256:
            raise ValueError("INDEXER_ENABLED_SITES 格式无效")
        items: Iterable[Any] = raw.split(",")
    elif isinstance(value, (list, tuple)):
        if len(value) > (len(MANUAL_INDEXER_SITE_ORDER) + len(ignored)) * 2:
            raise ValueError("资源站点数量超出允许范围")
        items = value
    else:
        raise ValueError("资源站点必须是字符串列表")

    requested: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError("资源站点 ID 必须是字符串")
        site_id = item.strip().lower()
        if not site_id:
            continue
        if len(site_id) > 32 or "\r" in site_id or "\n" in site_id:
            raise ValueError("资源站点 ID 格式无效")
        requested.add(site_id)

    allowed = set(MANUAL_INDEXER_SITE_ORDER)
    unknown = sorted(requested - allowed - ignored)
    if unknown:
        raise ValueError(f"未知资源站点: {', '.join(unknown)}")
    return tuple(site_id for site_id in MANUAL_INDEXER_SITE_ORDER if site_id in requested)


def encode_indexer_site_ids(value: Any) -> str:
    return ",".join(normalize_indexer_site_ids(value))


def build_indexer_site_updates(value: Any) -> dict[str, str]:
    site_ids = normalize_indexer_site_ids(value)
    return {
        "INDEXER_ENABLED_SITES": ",".join(site_ids),
        INDEXER_SITE_CONFIG_VERSION_KEY: INDEXER_SITE_CONFIG_VERSION,
        "INDEXER_SUKEBEI_ENABLED": "1" if "sukebei" in site_ids else "0",
    }


# ===== 按媒体语义的站点路由 =====
#
# 对仍可用的站点保留 fail-open 回退；不重新启用已下线或成人站点，
# 用户在订阅上显式配置的站点列表始终优先于自动路由。
_ANIME_JA_PREFERRED = ("mikan", "nyaa", "btbtla")
_ANIME_JA_DROPPED = frozenset({"tpb"})
# 国漫主要在中文站；Mikan 只收录日本番组。
_ANIME_ZH_PREFERRED = ("btbtla", "nyaa")
_ANIME_ZH_DROPPED = frozenset({"mikan", "tpb"})
# 真人影视不会出现在动漫专站。
_LIVE_ACTION_PREFERRED = ("btbtla", "tpb")
_LIVE_ACTION_DROPPED = frozenset({"mikan", "nyaa"})


def plan_media_site_route(
    available: Iterable[str],
    *,
    is_animation: bool,
    original_language: str = "",
) -> tuple[str, ...]:
    """按动漫/真人与原产语言收敛站点集合并排定优先序。

    ``available`` 应为当前启用的站点；成人站点永不参与自动路由。
    路由结果为空时原样返回可用集合，保证任何情况下都有站点可搜。
    """
    ordered = [
        site_id.strip().lower()
        for site_id in available
        if site_id.strip() and site_id.strip().lower() not in RETIRED_INDEXER_SITE_IDS | {"sukebei"}
    ]
    if is_animation:
        if (original_language or "").strip().lower().startswith("zh"):
            preferred, dropped = _ANIME_ZH_PREFERRED, _ANIME_ZH_DROPPED
        else:
            preferred, dropped = _ANIME_JA_PREFERRED, _ANIME_JA_DROPPED
    else:
        preferred, dropped = _LIVE_ACTION_PREFERRED, _LIVE_ACTION_DROPPED
    routed = [site_id for site_id in ordered if site_id not in dropped]
    routed.sort(
        key=lambda site_id: (
            preferred.index(site_id) if site_id in preferred else len(preferred)
        )
    )
    return tuple(routed) if routed else tuple(ordered)


def tmdb_detail_is_animation(detail: Any) -> bool:
    """从 TMDB 详情 genres 判断是否动画；结构异常一律按非动画处理。"""
    if not isinstance(detail, dict):
        return False
    for genre in detail.get("genres") or []:
        if not isinstance(genre, dict):
            continue
        try:
            if int(genre.get("id") or 0) == 16:
                return True
        except (TypeError, ValueError):
            pass
        if str(genre.get("name") or "").strip() in {"Animation", "动画"}:
            return True
    return False


class SiteConfigurationChanged(ValueError):
    """确认的资源站点配置已变化，未开始写入。"""


_SITE_CONFIG_KEYS = ("INDEXER_SEARCH_ENABLED", "INDEXER_ENABLED_SITES",
                     INDEXER_SITE_CONFIG_VERSION_KEY, "INDEXER_SUKEBEI_ENABLED")


def current_indexer_site_ids() -> tuple[str, ...]:
    raw = config.get("INDEXER_ENABLED_SITES", ",".join(DEFAULT_INDEXER_SITE_IDS)) or ""
    try:
        requested = set(normalize_persisted_indexer_site_ids(raw))
    except ValueError:
        requested = {part.strip().lower() for part in raw.split(",")
                     if part.strip().lower() in MANUAL_INDEXER_SITE_ORDER}
    if config.get_bool("INDEXER_SUKEBEI_ENABLED", False):
        requested.add("sukebei")
    return tuple(site for site in MANUAL_INDEXER_SITE_ORDER if site in requested)


def site_configuration() -> dict:
    """当前手动站点选择；与Agent自动附加的检索源区分，不探测网络。"""
    sites = current_indexer_site_ids()
    return {"site_ids": list(sites), "site_count": len(sites),
            "sites": [{"site_id": site, "label": INDEXER_SITE_LABELS[site]} for site in sites],
            "search_enabled": config.get_bool("INDEXER_SEARCH_ENABLED"),
            "managed_by_environment": any(config.has_external_override(key) for key in _SITE_CONFIG_KEYS)}


def normalize_site_selection(site_ids: list[str], enable_search: bool | None = None) -> dict:
    if not isinstance(site_ids, list) or not 1 <= len(site_ids) <= len(MANUAL_INDEXER_SITE_ORDER):
        raise ValueError("site_ids必须是非空站点ID数组，数量不能超过可选站点数")
    sites = normalize_indexer_site_ids(site_ids)
    if len(sites) != len(site_ids):
        raise ValueError("site_ids不允许重复或空白项")
    if enable_search is not None and type(enable_search) is not bool:
        raise ValueError("enable_search必须是布尔值")
    return {"site_ids": list(sites), **({"enable_search": enable_search} if enable_search is not None else {})}


def _site_configuration_state(arguments):
    raw, _ = config.read_env_snapshot(config.ENV_FILE)
    current = site_configuration()
    fingerprint = hashlib.sha256(json.dumps({"file_present": raw is not None,
        "file_hash": hashlib.sha256(raw or b"").hexdigest(), "current": current,
        "arguments": arguments}, sort_keys=True).encode()).hexdigest()
    return raw, current, fingerprint


def prepare_site_configuration(*, site_ids: list[str], enable_search: bool | None = None) -> tuple[dict, str]:
    args = normalize_site_selection(site_ids, enable_search)
    _, current, fingerprint = _site_configuration_state(args)
    if current["managed_by_environment"]:
        return {"ok": False, "status": "environment_override", "summary": "资源站点由部署环境管理",
                "error": "请在部署环境调整站点配置。"}, fingerprint
    if current["site_ids"] == args["site_ids"] and (enable_search is None or current["search_enabled"] == enable_search):
        return {"ok": True, "status": "no_changes", "summary": "资源站点已经是目标选择"}, fingerprint
    effects = ["保存站点选择并刷新当前进程Web与Telegram资源搜索服务"]
    if "sukebei" in args["site_ids"] and "sukebei" not in current["site_ids"]:
        effects.insert(0, "将启用成人内容站点Sukebei")
    return {"ok": True, "status": "confirmation_required", "summary": f"确认后保存{len(args['site_ids'])}个资源站点",
            "data": {"current_count": current["site_count"], "requested_count": len(args["site_ids"]),
                     "current_sites": current["sites"],
                     "requested_sites": [{"site_id": site, "label": INDEXER_SITE_LABELS[site]} for site in args["site_ids"]],
                     "search_enabled": current["search_enabled"] if enable_search is None else enable_search,
                     "requested_enable_search": enable_search, "effects": effects}}, fingerprint


def apply_site_configuration(*, site_ids: list[str], expected_fingerprint: str, enable_search: bool | None = None, admission=nullcontext) -> dict:
    args = normalize_site_selection(site_ids, enable_search)
    with admission():
        raw, current, fingerprint = _site_configuration_state(args)
        if not secrets.compare_digest(fingerprint, expected_fingerprint):
            raise SiteConfigurationChanged("配置已变化，请重新预检")
        if current["managed_by_environment"]:
            return {"ok": False, "status": "environment_override", "summary": "配置由部署环境管理，未修改"}
        if current["site_ids"] == args["site_ids"] and (enable_search is None or current["search_enabled"] == enable_search):
            return {"ok": True, "status": "no_changes", "summary": "资源站点已经是目标选择"}
        updates = build_indexer_site_updates(args["site_ids"])
        if enable_search is not None:
            updates["INDEXER_SEARCH_ENABLED"] = "1" if enable_search else "0"
        try:
            config.update_runtime_env_file(config.ENV_FILE, updates, expected=raw)
        except config.ConcurrentConfigUpdateError:
            return {"ok": False, "status": "conflict", "summary": "配置已被其他操作修改，请重新预检"}
        except config.ExternalConfigOverrideError:
            return {"ok": False, "status": "environment_override", "summary": "配置由部署环境管理，未修改"}
    # 发布或回读异常交给执行器保留未知结果，不能误报未执行后重试。
    from app.indexers.runtime import refresh_indexer_configuration
    runtime = refresh_indexer_configuration()
    _, persisted = config.read_env_snapshot(config.ENV_FILE)
    verified = all(persisted.get(key) == value for key, value in updates.items())
    summary = f"已保存{len(args['site_ids'])}个资源站点" if verified else "已提交但最终状态待核验，请先查看配置而非直接重试"
    return {"ok": verified, "status": "completed" if verified else "outcome_unknown", "summary": summary,
            "error": "" if verified else summary,
            "data": {**site_configuration(), "runtime_refresh": runtime,
                     "runtime_refreshed": all(runtime.values()), "runtime_scope": "current_process",
                     "verification_state": "verified" if verified else "pending"},
            "suggestions": [f"{name}索引器配置已保存，但未能热更新；重启后生效" for name, ok in runtime.items() if not ok]}
