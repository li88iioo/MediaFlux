"""媒体订阅事务 outbox 到统一 Telegram 通知中心的可靠移交。"""

from __future__ import annotations

from app.agent.public_safety import sanitize_public_text
from app.logger import get_logger
from app.notifier import NOTIFICATION_SECTION_BREAK, NotificationEvent
from app.repositories.media_experience import (
    claim_due_notifications,
    mark_notification_sent,
    recover_notifications,
    retry_notification,
)

logger = get_logger(__name__)


def _event(item: dict) -> NotificationEvent:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    title = sanitize_public_text(payload.get("title"), limit=120) or "媒体订阅"
    event_type = str(item.get("event_type") or "")
    if event_type == "missing":
        missing = max(0, int(payload.get("missing_count") or 0))
        candidates = max(0, int(payload.get("candidate_count") or 0))
        submitted = max(0, int(payload.get("auto_submitted") or 0))
        action = str(payload.get("action") or "notify").strip().lower()
        fields = [
            ("目标媒体", title),
            NOTIFICATION_SECTION_BREAK,
            ("待补内容", f"{missing} 项"),
        ]
        if candidates:
            fields.append(("候选", f"{candidates} 个"))
        if submitted:
            fields.append(("已提交", f"{submitted} 项"))
            return NotificationEvent(
                "✅ 追更已自动提交下载",
                fields=tuple(fields),
                footer="下载与后续入库状态会继续由对应任务通知更新。",
                layout="relaxed",
            )
        if action == "confirm" and candidates:
            footer = "已找到候选，请在媒体追更中确认后再提交下载。"
            heading = "⚠️ 追更候选待确认"
        elif candidates:
            footer = "已找到可用候选，请在媒体追更中查看。"
            heading = "📺 追更发现待补内容"
        else:
            footer = "本轮未找到可用候选，可稍后重试或调整资源检索条件。"
            heading = "⚠️ 追更暂无可用资源"
        return NotificationEvent(
            heading,
            fields=tuple(fields),
            footer=footer,
            layout="relaxed",
        )
    if event_type == "satisfied":
        return NotificationEvent(
            "✅ 追更已满足",
            fields=(
                ("目标媒体", title),
                NOTIFICATION_SECTION_BREAK,
                ("状态", "当前没有检测到缺失内容"),
            ),
            layout="relaxed",
        )
    if event_type == "inconclusive":
        reason = sanitize_public_text(payload.get("summary"), limit=220)
        return NotificationEvent(
            "⚠️ 追更检查无法得出结论",
            fields=(("目标媒体", title),),
            footer=reason or "请检查媒体服务器连接、媒体库映射与 TMDB 数据后重试。",
            layout="relaxed",
        )
    return NotificationEvent(
        "⚠️ 追更检查异常",
        fields=(("目标媒体", title),),
        footer="本轮检查未能完成，可稍后重试。",
        layout="relaxed",
    )


def drain_media_subscription_notifications(*, limit: int = 20) -> bool:
    """移交订阅事务事件；领域记录只在统一通知中心接纳后确认。"""
    # 主动摘要复用同一周期唤醒与统一通知 outbox，不建立 Agent 专用线程。
    from app.modules.media_automation_rules import drain_automation_rules

    try:
        drain_automation_rules(limit=limit)
    except Exception as exc:
        logger.exception("主动媒体摘要调度失败 type=%s", type(exc).__name__)
    recover_notifications()
    claimed = claim_due_notifications(limit=limit)
    if not claimed:
        return True
    delivered = True
    from app.modules.telegram_notification_center import publish_notification_event
    from app.modules.telegram_notification_policy import (
        NotificationImportance,
        NotificationTopic,
        notifications_enabled,
    )

    for item in claimed:
        generation = int(item["lease_generation"])
        event_type = str(item.get("event_type") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        missing_actionable = bool(
            event_type == "missing" and int(payload.get("auto_submitted") or 0) <= 0
        )
        outcome = publish_notification_event(
            f"media-subscription:{item['id']}:{event_type}",
            _event(item),
            topic=NotificationTopic.MEDIA_SUBSCRIPTION,
            importance=(
                NotificationImportance.ERROR
                if event_type not in {"missing", "satisfied"}
                else NotificationImportance.ACTION
                if missing_actionable
                else NotificationImportance.RESULT
            ),
        )
        # 全局开关或通知等级主动抑制都属于已执行的消费策略；事务 outbox
        # 不应把同一条被策略拒绝的结果反复移交和重试。
        accepted = (
            bool(outcome)
            or str(outcome.status or "") in {"disabled", "suppressed"}
            or not notifications_enabled()
        )
        if accepted:
            if not mark_notification_sent(item["id"], lease_generation=generation):
                delivered = False
            continue
        delivered = False
        retry_notification(
            item["id"],
            lease_generation=generation,
            error=str(outcome.status or "telegram_unavailable"),
        )
    return delivered


def recover_media_subscription_notifications() -> int:
    recovered = recover_notifications()
    try:
        drain_media_subscription_notifications()
    except Exception as exc:
        logger.exception("媒体订阅通知恢复投递失败 type=%s", type(exc).__name__)
    return recovered
