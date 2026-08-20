"""RSS 订阅的人类名称解析，仅在服务端转换为内部编号。"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from app import database as db


_BULK_TARGETS = frozenset({
    "全部", "所有", "全部订阅", "所有订阅", "all", "everything",
})
_EDGE_NOISE = " \t\r\n'\"“”‘’《》<>【】[]()（）.。!！?？,，:：;；"


@dataclass(frozen=True)
class RSSSubscriptionResolution:
    """内部名称解析结果；候选只保留公开可引用的订阅编号。"""

    status: str
    subscription_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


def normalize_rss_subscription_name(value: str) -> str:
    """统一名称比较口径，不改写数据库中的原始名称。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip(_EDGE_NOISE)
    normalized = re.sub(r"\s+", " ", normalized).casefold().strip()
    return normalized


def resolve_rss_subscription_name(value: str) -> RSSSubscriptionResolution:
    """按名称精确解析订阅；不会模糊猜测或泄露 URL、过滤词与路径。"""
    target = normalize_rss_subscription_name(value)
    if not target or target in _BULK_TARGETS:
        return RSSSubscriptionResolution(status="invalid")
    rows = db.find_rss_subscriptions_by_normalized_name(target, limit=3)
    candidate_ids = tuple(int(row["id"]) for row in rows)
    if not candidate_ids:
        return RSSSubscriptionResolution(status="not_found")
    if len(candidate_ids) > 1:
        return RSSSubscriptionResolution(
            status="ambiguous",
            candidate_ids=candidate_ids,
        )
    return RSSSubscriptionResolution(
        status="resolved",
        subscription_id=candidate_ids[0],
        candidate_ids=candidate_ids,
    )
