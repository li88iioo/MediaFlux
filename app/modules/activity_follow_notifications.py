"""单任务持久跟踪的确定性通知投影；不调用模型，不创建新的通知发送器。"""

from datetime import datetime, timedelta

from app.agent.activity_actions import timeline_snapshot
from app.agent.public_safety import sanitize_public_text
from app.modules.media_automation_rules import RuleDelivery
from app.notifier import NotificationEvent


def deliver_activity_follow(rule: dict, now: datetime) -> RuleDelivery:
    settings = rule["settings"]
    timeline = timeline_snapshot(settings["target"])
    expires = datetime.fromisoformat(settings["expires_at"])
    expired = now >= expires
    data = timeline.data
    attention = bool(data.get("needs_attention"))
    terminal = not timeline.ok or attention or bool(data.get("terminal")) or expired
    event = None
    if terminal:
        reason = "活动记录已不存在，跟踪结束。" if not timeline.ok else timeline.summary
        if expired and not attention and not data.get("terminal"):
            reason = "跟踪已到期，原任务尚未确认结束；未修改或取消原任务。"
        fields = [
            ("活动", sanitize_public_text(settings.get("title"), limit=120)),
            ("状态", sanitize_public_text(reason, limit=500)),
        ]
        gaps = data.get("gaps", [])
        if gaps:
            fields.append(
                ("核对范围", sanitize_public_text("；".join(gaps), limit=500))
            )
        event = NotificationEvent(
            title="活动跟踪需要关注" if attention else "活动跟踪结束",
            lines=("依据系统已持久化的处理记录；下载完成不等于已入库。",),
            fields=fields,
        )
    return RuleDelivery(
        event,
        f"{rule['id']}:{rule['revision']}:terminal",
        (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        importance="error" if attention else "result",
        terminal=terminal,
    )
