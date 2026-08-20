"""资源站点配置的共享白名单与规范化逻辑。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

INDEXER_SITE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("nyaa", "Nyaa"),
    ("mikan", "Mikan"),
    ("btbtla", "BTBTLA"),
    ("1lou", "1LOU"),
    ("animetosho", "AnimeTosho"),
    ("tpb", "The Pirate Bay"),
    ("sukebei", "Sukebei"),
)
INDEXER_SITE_ORDER = tuple(site_id for site_id, _label in INDEXER_SITE_DEFINITIONS)
INDEXER_SITE_LABELS = dict(INDEXER_SITE_DEFINITIONS)
DEFAULT_INDEXER_SITE_IDS = INDEXER_SITE_ORDER[:6]


def normalize_indexer_site_ids(value: Any) -> tuple[str, ...]:
    """严格规范化固定站点 ID，去重并按产品顺序返回。"""
    if isinstance(value, str):
        raw = value
        if "\r" in raw or "\n" in raw or len(raw) > 256:
            raise ValueError("INDEXER_ENABLED_SITES 格式无效")
        items: Iterable[Any] = raw.split(",")
    elif isinstance(value, (list, tuple)):
        if len(value) > len(INDEXER_SITE_ORDER) * 2:
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

    unknown = sorted(requested - set(INDEXER_SITE_ORDER))
    if unknown:
        raise ValueError(f"未知资源站点: {', '.join(unknown)}")
    return tuple(site_id for site_id in INDEXER_SITE_ORDER if site_id in requested)


def encode_indexer_site_ids(value: Any) -> str:
    return ",".join(normalize_indexer_site_ids(value))


def build_indexer_site_updates(value: Any) -> dict[str, str]:
    site_ids = normalize_indexer_site_ids(value)
    return {
        "INDEXER_ENABLED_SITES": ",".join(site_ids),
        "INDEXER_SUKEBEI_ENABLED": "1" if "sukebei" in site_ids else "0",
    }


# ===== 按媒体语义的站点路由 =====
#
# 只剔除确定无关的站点，永不返回空集合（fail-open 回退全量），
# 用户在订阅上显式配置的站点列表始终优先于自动路由。
_ANIME_JA_PREFERRED = ("mikan", "nyaa", "animetosho", "1lou", "btbtla")
_ANIME_JA_DROPPED = frozenset({"tpb"})
# 国漫主要在中文站；Mikan 只收录日本番组。
_ANIME_ZH_PREFERRED = ("1lou", "btbtla", "nyaa", "animetosho")
_ANIME_ZH_DROPPED = frozenset({"mikan", "tpb"})
# 真人影视不会出现在动漫专站。
_LIVE_ACTION_PREFERRED = ("1lou", "btbtla", "tpb")
_LIVE_ACTION_DROPPED = frozenset({"mikan", "animetosho", "nyaa"})


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
        str(site_id).strip().lower()
        for site_id in available
        if str(site_id).strip() and str(site_id).strip().lower() != "sukebei"
    ]
    if is_animation:
        if str(original_language or "").strip().lower().startswith("zh"):
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
