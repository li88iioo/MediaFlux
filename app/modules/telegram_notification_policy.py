"""Telegram 主动通知的统一开关、等级与领域语义。"""
from __future__ import annotations

from enum import StrEnum

from app import config


class NotificationTopic(StrEnum):
    DOWNLOAD = "download"
    ORGANIZE = "organize"
    STRM = "strm"
    LOCAL_MEDIA = "local_media"
    CONFIRMATION = "confirmation"
    RSS = "rss"
    MEDIA_SUBSCRIPTION = "media_subscription"
    AGENT = "agent"
    GCID = "gcid"
    SYSTEM = "system"


class NotificationImportance(StrEnum):
    ACTION = "action"
    ERROR = "error"
    RESULT = "result"
    DETAIL = "detail"


_LEVEL_RANK = {
    "essential": 0,
    "standard": 1,
    "detailed": 2,
}
_IMPORTANCE_RANK = {
    NotificationImportance.ACTION: 0,
    NotificationImportance.ERROR: 0,
    NotificationImportance.RESULT: 1,
    NotificationImportance.DETAIL: 2,
}


def notification_level() -> str:
    value = str(config.get("TG_NOTIFICATION_LEVEL", "standard") or "standard").strip().lower()
    return value if value in _LEVEL_RANK else "standard"


def notifications_enabled() -> bool:
    return config.get_bool("TG_NOTIFICATION_ENABLED", True)


def allows_notification(
    importance: NotificationImportance | str,
    *,
    topic_enabled: bool = True,
) -> bool:
    """判断一条主动通知是否允许发送。

    命令交互回复不经过本函数。待确认与错误仍受全局总开关约束，但不受
    ``standard/detailed`` 成功信息等级限制。
    """
    if not notifications_enabled() or not bool(topic_enabled):
        return False
    try:
        normalized = NotificationImportance(str(importance))
    except ValueError:
        normalized = NotificationImportance.RESULT
    return _LEVEL_RANK[notification_level()] >= _IMPORTANCE_RANK[normalized]


def notification_policy_snapshot() -> dict[str, object]:
    return {
        "enabled": notifications_enabled(),
        "level": notification_level(),
    }
