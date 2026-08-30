"""光鸭整理与后续 STRM/媒体库刷新的单消息事务投影。"""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping

from app.modules.telegram_media_projection import (
    attach_bounded_media_details,
    build_media_detail_blocks,
)
from app.modules.telegram_notification_center import (
    NotificationPublishResult,
    get_notification_thread_event,
    get_notification_thread_snapshot,
    publish_notification_thread,
)
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import NotificationEvent, safe_int

_PENDING_STRM_STATES = frozenset({
    "已排队",
    "等待后处理",
    "排队中",
    "运行中",
    "同步中",
    "已触发",
})
_TERMINAL_DELIVERY_FAILURES = frozenset({
    "failed",
    "outcome_unknown",
    "suppressed",
})
_TERMINAL_LIFECYCLE_STATES = frozenset({
    "completed",
    "partial",
    "failed",
    "stopped",
})


def _field_value(event: NotificationEvent, label: str) -> str:
    return next((
        str(value or "").strip()
        for current_label, value in event.fields
        if str(current_label or "").strip() == label
    ), "")


def organize_lifecycle_downstream_settled(event: NotificationEvent) -> bool:
    """按持久机器状态判断终态；仅为升级前 outbox 保留文案兼容。"""
    state = str(event.state or "").strip().lower()
    if state:
        return state in _TERMINAL_LIFECYCLE_STATES
    # 兼容升级前已经持久化、尚未送达且没有 state 字段的事件。新事件不得
    # 依赖展示文案驱动状态机。
    strm_status = _field_value(event, "STRM")
    return bool(strm_status and strm_status not in _PENDING_STRM_STATES)


def wait_for_organize_lifecycle_delivery(
    task_id: str,
    *,
    chat_id: str = "",
    timeout_seconds: float = 30 * 60,
    poll_seconds: float = 0.5,
    cancel_event: threading.Event | None = None,
) -> bool:
    """等待整理链路终态的最新 revision 真正送达 Telegram。

    整理完成只代表 STRM 已排队，不能据此提前清除 ``typing``。只有同一
    生命周期卡片写入 STRM/媒体库终态，且 outbox 确认最新 revision 已投递，
    才允许调用方结束输入状态。
    """
    thread_key = f"organize:{str(task_id or '').strip()}"
    if thread_key == "organize:":
        return False
    timeout = max(0.0, float(timeout_seconds or 0.0))
    interval = max(0.05, min(float(poll_seconds or 0.5), 5.0))
    deadline = time.monotonic() + timeout
    while True:
        snapshot = get_notification_thread_snapshot(
            thread_key,
            topic=NotificationTopic.ORGANIZE,
            chat_id=chat_id,
        )
        if snapshot is not None:
            settled = organize_lifecycle_downstream_settled(snapshot.event)
            if settled and snapshot.current_revision_delivered:
                return True
            if settled and snapshot.status in _TERMINAL_DELIVERY_FAILURES:
                return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        wait_seconds = min(interval, remaining)
        if cancel_event is not None:
            if cancel_event.wait(wait_seconds):
                return False
        else:
            time.sleep(wait_seconds)


def _counts(stats: Mapping[str, object]) -> dict[str, int]:
    keys = {
        "total": "total",
        "moved": "moved",
        "metadata": "metadata_moved",
        "confirm": "need_confirm",
        "skipped": "skipped",
        "failed": "failed",
    }
    counts = {
        label: safe_int(stats.get(source), 0, minimum=0)
        for label, source in keys.items()
    }
    directories = stats.get("directories")
    if isinstance(directories, Mapping):
        for label, source in keys.items():
            if counts[label]:
                continue
            counts[label] = sum(
                safe_int(item.get(source), 0, minimum=0)
                for item in directories.values()
                if isinstance(item, Mapping)
            )
    return counts



def _replace_field(
    fields: tuple[tuple[object, object], ...], label: str, value: str,
) -> tuple[tuple[object, object], ...]:
    result: list[tuple[object, object]] = []
    replaced = False
    for current_label, current_value in fields:
        if str(current_label) == label:
            if value:
                result.append((label, value))
            replaced = True
        else:
            result.append((current_label, current_value))
    if not replaced and value:
        result.append((label, value))
    return tuple(result)


def build_organize_lifecycle_event(
    stats: Mapping[str, object],
    *,
    source_name: str,
    strm_status: str = "已排队",
    media_refresh: str = "等待 STRM 完成",
) -> NotificationEvent:
    counts = _counts(stats)
    stopped = bool(stats.get("stopped"))
    scan_incomplete = bool(
        stats.get("scan_errors")
        or stats.get("scan_limited")
        or stats.get("scan_complete") is False
    )
    attention = bool(
        stopped or scan_incomplete or counts["confirm"]
        or counts["skipped"] or counts["failed"]
    )
    title = (
        "⏹️ 光鸭整理已停止"
        if stopped else ("⚠️ 光鸭整理部分完成" if attention else "✅ 光鸭整理完成")
    )
    summary = [f"视频 {counts['total']}"]
    if counts["moved"]:
        summary.append(f"入库 {counts['moved']}")
    if counts["metadata"]:
        summary.append(f"元数据 {counts['metadata']}")
    if counts["confirm"]:
        summary.append(f"待确认 {counts['confirm']}")
    if counts["skipped"]:
        summary.append(f"跳过 {counts['skipped']}")
    if counts["failed"]:
        summary.append(f"失败 {counts['failed']}")
    footer = ""
    confirmation_label = ""
    if counts["confirm"]:
        actionable_files = safe_int(
            stats.get("notification_actionable_confirmation_files"),
            0,
            minimum=0,
        )
        actionable_groups = safe_int(
            stats.get("notification_actionable_confirmation_groups"),
            0,
            minimum=0,
        )
        candidate_groups = min(
            actionable_groups,
            safe_int(
                stats.get("notification_candidate_confirmation_groups"),
                actionable_groups,
                minimum=0,
            ),
        )
        skip_groups = min(
            max(0, actionable_groups - candidate_groups),
            safe_int(
                stats.get("notification_skip_confirmation_groups"),
                max(0, actionable_groups - candidate_groups),
                minimum=0,
            ),
        )
        unresolved_files = max(0, counts["confirm"] - actionable_files)
        confirmation_parts: list[str] = []
        card_parts: list[str] = []
        if candidate_groups:
            card_parts.append(f"{candidate_groups} 组候选卡")
        if skip_groups:
            card_parts.append(f"{skip_groups} 组跳过卡")
        if actionable_groups:
            card_label = " + ".join(card_parts) or f"{actionable_groups} 组按钮卡"
            confirmation_parts.append(f"{actionable_files} 个文件 / {card_label}")
        if unresolved_files:
            confirmation_parts.append(f"{unresolved_files} 个暂无候选")
        confirmation_label = " · ".join(confirmation_parts) or "暂无可用候选"
        if actionable_groups and unresolved_files:
            footer = (
                "可操作项目会以独立按钮卡发送；缺少安全快照的项目仍保留在 Web "
                "待确认队列。"
            )
        elif candidate_groups and skip_groups:
            footer = "候选卡用于选择识别结果；暂无元数据的项目可在跳过卡直接结束。"
        elif candidate_groups:
            footer = "候选已按媒体合并为独立按钮卡；任务快照同时保留在 Web。"
        elif skip_groups:
            footer = "本轮暂无可用元数据；可在独立跳过卡结束待确认，文件保持原位。"
        else:
            footer = "本轮暂无可用候选，请在 Web 待确认队列手动识别。"
    elif scan_incomplete:
        footer = (
            "目录扫描未完整结束，本次结果不是最终缺集结论；"
            "请查看 Web 整理日志。"
        )
    elif attention:
        footer = "本次存在跳过或失败项目，请查看 Web 整理日志。"
    fields: list[tuple[object, object]] = [
        ("来源", source_name),
        ("整理", " · ".join(summary)),
    ]
    if confirmation_label:
        fields.append(("人工确认", confirmation_label))
    fields.extend((("STRM", strm_status), ("媒体库", media_refresh)))
    if stopped:
        lifecycle_state = "stopped"
    elif str(strm_status or "").strip() in _PENDING_STRM_STATES:
        lifecycle_state = "queued"
    elif counts["failed"] and str(strm_status or "").strip() == "未执行":
        lifecycle_state = "failed"
    elif attention:
        lifecycle_state = "partial"
    else:
        lifecycle_state = "completed"
    event = NotificationEvent(
        title,
        fields=tuple(fields),
        footer=footer,
        layout="relaxed",
        state=lifecycle_state,
    )
    return attach_bounded_media_details(
        event,
        build_media_detail_blocks(
            tuple(stats.get("media_items") or ()),
            inventory_final=not bool(attention or stopped),
        ),
    )


def _importance(stats: Mapping[str, object]) -> NotificationImportance:
    counts = _counts(stats)
    if counts["confirm"]:
        return NotificationImportance.ACTION
    if (
        bool(stats.get("stopped"))
        or counts["failed"]
        or stats.get("scan_errors")
        or stats.get("scan_limited")
        or stats.get("scan_complete") is False
    ):
        return NotificationImportance.ERROR
    return NotificationImportance.RESULT


def publish_organize_lifecycle(
    task_id: str,
    stats: Mapping[str, object],
    *,
    source_name: str,
    chat_id: str = "",
    topic_enabled: bool = True,
    strm_status: str = "已排队",
    media_refresh: str = "等待 STRM 完成",
) -> NotificationPublishResult:
    return publish_notification_thread(
        f"organize:{task_id!s}",
        build_organize_lifecycle_event(
            stats,
            source_name=source_name,
            strm_status=strm_status,
            media_refresh=media_refresh,
        ),
        topic=NotificationTopic.ORGANIZE,
        importance=_importance(stats),
        chat_id=chat_id,
        topic_enabled=topic_enabled,
    )


def update_organize_lifecycle_downstream(
    task_id: str,
    *,
    chat_id: str = "",
    strm_status: str,
    media_refresh: str,
    partial: bool = False,
    error: str = "",
    topic_enabled: bool = True,
) -> NotificationPublishResult:
    thread_key = f"organize:{task_id!s}"
    previous = get_notification_thread_event(
        thread_key, topic=NotificationTopic.ORGANIZE, chat_id=chat_id,
    )
    if previous is None:
        return NotificationPublishResult(False, status="missing_thread")
    fields = _replace_field(previous.fields, "STRM", str(strm_status or ""))
    fields = _replace_field(fields, "媒体库", str(media_refresh or ""))
    title = previous.title
    footer = previous.footer
    importance = NotificationImportance.RESULT
    if partial or error:
        title = "⚠️ 光鸭整理链路部分完成"
        importance = NotificationImportance.ERROR
        if error:
            footer = str(error)[:260]
    event = NotificationEvent(
        title,
        fields=fields,
        lines=previous.lines,
        footer=footer,
        actions=previous.actions,
        layout=previous.layout,
        field_emojis=previous.field_emojis,
        state="partial" if partial or error else "completed",
    )
    return publish_notification_thread(
        thread_key,
        event,
        topic=NotificationTopic.ORGANIZE,
        importance=importance,
        chat_id=chat_id,
        topic_enabled=topic_enabled,
    )
