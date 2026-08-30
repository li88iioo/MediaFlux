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
_CONFIRMATION_ROLLUP_VERSION = 1
_INCOMPLETE_MEDIA_STATUS_MARKER = "暂不生成最终缺集结论"


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


def _scan_incomplete(stats: Mapping[str, object]) -> bool:
    return bool(
        stats.get("scan_errors")
        or stats.get("scan_limited")
        or stats.get("scan_complete") is False
    )


def _summary_label(counts: Mapping[str, int], *, expired: int = 0) -> str:
    summary = [f"视频 {safe_int(counts.get('total'), 0, minimum=0)}"]
    labels = (
        ("moved", "入库"),
        ("metadata", "元数据"),
        ("confirm", "待确认"),
        ("skipped", "跳过"),
        ("failed", "失败"),
    )
    for key, label in labels:
        value = safe_int(counts.get(key), 0, minimum=0)
        if value:
            summary.append(f"{label} {value}")
    if expired:
        summary.append(f"过期未处理 {safe_int(expired, 0, minimum=0)}")
    return " · ".join(summary)


def build_organize_confirmation_rollup(
    stats: Mapping[str, object],
) -> dict[str, object]:
    """生成可随候选卡持久化的最小任务基线，不复制媒体详情。"""
    counts = _counts(stats)
    return {
        "version": _CONFIRMATION_ROLLUP_VERSION,
        **counts,
        "actionable_files": safe_int(
            stats.get("notification_actionable_confirmation_files"),
            0,
            minimum=0,
        ),
        "actionable_groups": safe_int(
            stats.get("notification_actionable_confirmation_groups"),
            0,
            minimum=0,
        ),
        "stopped": bool(stats.get("stopped")),
        "scan_incomplete": _scan_incomplete(stats),
    }


def _event_downstream_failed(event: NotificationEvent) -> bool:
    values = (
        _field_value(event, "STRM"),
        _field_value(event, "媒体库"),
    )
    return bool(
        event.title.startswith("⚠️ 光鸭整理链路")
        or any(
            marker in value
            for value in values
            for marker in ("失败", "错误", "未完成")
        )
    )


def _confirmation_media_lines(
    lines: tuple[str, ...], *, inventory_final: bool,
) -> tuple[str, ...]:
    """确认全部成功后移除已失效的“仍待确认”说明。

    原汇总只持久化了已入库媒体的有界文本投影，无法安全重算整个季库存；
    因此这里只删除已经确定不成立的状态行，不猜测缺集或改写其它统计。
    """
    if not inventory_final:
        return lines
    settled: list[str] = []
    for block in lines:
        filtered = "\n".join(
            line for line in str(block).splitlines()
            if _INCOMPLETE_MEDIA_STATUS_MARKER not in line
        )
        if filtered.strip():
            settled.append(filtered)
    return tuple(settled)


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
    scan_incomplete = _scan_incomplete(stats)
    non_confirmation_attention = bool(
        stopped or scan_incomplete or counts["skipped"] or counts["failed"]
    )
    attention = bool(
        non_confirmation_attention or counts["confirm"]
    )
    title = (
        "⏹️ 光鸭整理已停止"
        if stopped
        else (
            "⏳ 光鸭整理等待人工确认"
            if counts["confirm"] and not non_confirmation_attention
            else ("⚠️ 光鸭整理部分完成" if attention else "✅ 光鸭整理完成")
        )
    )
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
        ("整理", _summary_label(counts)),
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


def update_organize_lifecycle_confirmations(
    task_id: str,
    *,
    chat_id: str = "",
    baseline: Mapping[str, object],
    outcomes: Mapping[str, object],
    topic_enabled: bool = True,
) -> NotificationPublishResult:
    """全部 Telegram 候选收口后，一次性回写原整理汇总。"""
    thread_key = f"organize:{task_id!s}"
    previous = get_notification_thread_event(
        thread_key, topic=NotificationTopic.ORGANIZE, chat_id=chat_id,
    )
    if previous is None:
        return NotificationPublishResult(False, status="missing_thread")

    base = {
        key: safe_int(baseline.get(key), 0, minimum=0)
        for key in ("total", "moved", "metadata", "confirm", "skipped", "failed")
    }
    resolved_files = min(
        base["confirm"],
        safe_int(outcomes.get("resolved_files"), 0, minimum=0),
    )
    counts = {
        "total": base["total"],
        "moved": base["moved"] + safe_int(
            outcomes.get("moved"), 0, minimum=0,
        ),
        "metadata": base["metadata"] + safe_int(
            outcomes.get("metadata"), 0, minimum=0,
        ),
        "confirm": max(0, base["confirm"] - resolved_files),
        "skipped": base["skipped"] + safe_int(
            outcomes.get("skipped"), 0, minimum=0,
        ),
        "failed": base["failed"] + safe_int(
            outcomes.get("failed"), 0, minimum=0,
        ),
    }
    expired = safe_int(outcomes.get("expired"), 0, minimum=0)
    handled_groups = safe_int(outcomes.get("groups"), 0, minimum=0)
    expected_groups = max(
        handled_groups,
        safe_int(baseline.get("actionable_groups"), 0, minimum=0),
    )
    confirmation_parts = [f"已处理 {handled_groups} / {expected_groups}"]
    for key, label in (
        ("moved", "入库"),
        ("skipped", "跳过"),
        ("failed", "失败"),
        ("expired", "过期"),
    ):
        value = safe_int(outcomes.get(key), 0, minimum=0)
        if value:
            confirmation_parts.append(f"{label} {value}")
    if counts["confirm"]:
        confirmation_parts.append(f"Web 待处理 {counts['confirm']}")

    fields = _replace_field(
        previous.fields, "整理", _summary_label(counts, expired=expired),
    )
    fields = _replace_field(fields, "人工确认", " · ".join(confirmation_parts))
    stopped = bool(baseline.get("stopped"))
    scan_incomplete = bool(baseline.get("scan_incomplete"))
    downstream_failed = _event_downstream_failed(previous)
    attention = bool(
        stopped
        or scan_incomplete
        or counts["confirm"]
        or counts["skipped"]
        or counts["failed"]
        or expired
        or downstream_failed
    )
    if stopped:
        title = "⏹️ 光鸭整理已停止"
    elif attention:
        title = "⚠️ 光鸭整理部分完成"
    else:
        title = "✅ 光鸭整理完成"

    if counts["confirm"]:
        footer = (
            f"Telegram 候选已处理；仍有 {counts['confirm']} 个项目保留在 Web "
            "待确认队列。"
        )
    elif expired:
        footer = (
            f"{expired} 个候选超过 24 小时未确认，文件保持原位；"
            "重新执行整理可再次识别。"
        )
    elif counts["skipped"] or counts["failed"]:
        footer = "人工确认已结束；跳过或失败项目可在 Web 整理日志中查看。"
    else:
        footer = "人工确认已全部完成；任务汇总已更新。"

    strm_status = _field_value(previous, "STRM")
    if stopped:
        lifecycle_state = "stopped"
    elif strm_status in _PENDING_STRM_STATES:
        lifecycle_state = "queued"
    elif attention:
        lifecycle_state = "partial"
    else:
        lifecycle_state = "completed"
    event = NotificationEvent(
        title,
        fields=fields,
        lines=_confirmation_media_lines(
            previous.lines, inventory_final=not attention,
        ),
        footer=footer,
        actions=previous.actions,
        layout=previous.layout,
        field_emojis=previous.field_emojis,
        state=lifecycle_state,
    )
    return publish_notification_thread(
        thread_key,
        event,
        topic=NotificationTopic.ORGANIZE,
        importance=(
            NotificationImportance.ERROR if attention
            else NotificationImportance.RESULT
        ),
        chat_id=chat_id,
        topic_enabled=topic_enabled,
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
    elif title.startswith("⏳"):
        importance = NotificationImportance.ACTION
    if title.startswith("⏹️"):
        lifecycle_state = "stopped"
    elif str(strm_status or "").strip() in _PENDING_STRM_STATES:
        lifecycle_state = "queued"
    elif partial or error or title.startswith(("⚠️", "⏳")):
        lifecycle_state = "partial"
    else:
        lifecycle_state = "completed"
    event = NotificationEvent(
        title,
        fields=fields,
        lines=previous.lines,
        footer=footer,
        actions=previous.actions,
        layout=previous.layout,
        field_emojis=previous.field_emojis,
        state=lifecycle_state,
    )
    return publish_notification_thread(
        thread_key,
        event,
        topic=NotificationTopic.ORGANIZE,
        importance=importance,
        chat_id=chat_id,
        topic_enabled=topic_enabled,
    )
