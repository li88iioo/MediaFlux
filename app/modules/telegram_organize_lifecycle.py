"""光鸭整理与后续 STRM/媒体库刷新的单消息事务投影。"""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Sequence

from app.modules.telegram_media_projection import (
    attach_bounded_media_details,
    build_media_detail_blocks,
)
from app.modules.telegram_notification_center import (
    NotificationPublishResult,
    get_notification_thread_snapshot,
    publish_notification_thread,
    deserialize_notification_event,
)
from app.logger import get_logger
from app.repositories.telegram_notifications import list_recovered_organize_refresh_notifications
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import NotificationEvent, safe_int
from app.modules.organize_results import organize_strm_error

_PENDING_STRM_STATES = frozenset({
    "已排队",
    "等待后处理",
    "排队中",
    "运行中",
    "同步中",
    "已触发",
    "后台规格补全进行中",
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
_refresh_recovery_after_id = 0
_refresh_recovery_lock = threading.Lock()


def _field_value(event: NotificationEvent, label: str) -> str:
    return next((
        str(value or "").strip()
        for current_label, value in event.fields
        if str(current_label or "").strip() == label
    ), "")


def organize_lifecycle_downstream_settled(event: NotificationEvent) -> bool:
    """按持久机器状态判断终态；仅为升级前 outbox 保留文案兼容。"""
    state = (event.state or "").strip().lower()
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
    thread_key = f"organize:{(task_id or '').strip()}"
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


def _counts(stats: Mapping[str, Any]) -> dict[str, int]:
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


def _scan_incomplete(stats: Mapping[str, Any]) -> bool:
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
    stats: Mapping[str, Any],
) -> dict[str, Any]:
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
    if event.lifecycle.get("refresh"):
        return True
    if "downstream" in event.lifecycle:
        downstream = event.lifecycle["downstream"]
        return bool(downstream.get("partial") or downstream.get("error"))
    values = (
        _field_value(event, "STRM"),
        _field_value(event, "媒体库"),
    )
    return (
        str(event.title).startswith("⚠️ 光鸭整理链路")
        or any(
            marker in value
            for value in values
            for marker in ("失败", "错误", "未完成")
        )
    )


def _join_inventory_issues(issues: tuple[str, ...]) -> str:
    if len(issues) <= 1:
        return f"{issues[0]}项" if issues else ""
    return f"{'、'.join(issues[:-1])}或{issues[-1]}项"


def _confirmation_inventory_status(
    counts: Mapping[str, int],
    *,
    stopped: bool,
    scan_incomplete: bool,
    expired: int,
) -> str:
    """根据确认后的真实剩余问题重算媒体详情状态。"""
    issues = tuple(
        label
        for active, label in (
            (safe_int(counts.get("skipped"), 0, minimum=0), "跳过"),
            (safe_int(counts.get("confirm"), 0, minimum=0), "待确认"),
            (safe_int(counts.get("failed"), 0, minimum=0), "失败"),
            (safe_int(expired, 0, minimum=0), "过期未处理"),
            (scan_incomplete, "扫描未完成"),
        )
        if active
    )
    remaining = _join_inventory_issues(issues)
    if stopped:
        prefix = "本次整理已停止"
        if remaining:
            prefix += f"，且仍有{remaining}"
        return f"{prefix}，暂不生成最终缺集结论"
    if remaining:
        return f"本次整理仍有{remaining}，暂不生成最终缺集结论"
    return ""


def _confirmation_media_lines(
    lines: Sequence[object], *, inventory_status: str,
) -> tuple[str, ...]:
    """刷新媒体详情中的整理状态，移除已失效的待确认原因。

    原汇总只持久化了已入库媒体的有界文本投影，无法安全重算整个季库存；
    因此这里只按剩余问题改写或删除状态行，不猜测缺集和其它库存统计。
    """
    settled: list[str] = []
    for block in lines:
        updated_lines: list[str] = []
        for line in str(block).splitlines():
            if _INCOMPLETE_MEDIA_STATUS_MARKER not in line:
                updated_lines.append(line)
                continue
            if not inventory_status:
                continue
            label, separator, _old_status = line.partition("：")
            updated_lines.append(
                f"{label}{separator}{inventory_status}"
                if separator else inventory_status
            )
        filtered = "\n".join(updated_lines)
        if filtered.strip():
            settled.append(filtered)
    return tuple(settled)


def _replace_field(
    fields: Sequence[tuple[object, object]], label: str, value: str,
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
    stats: Mapping[str, Any],
    *,
    source_name: str,
    strm_status: str = "已排队",
    media_refresh: str = "等待 STRM 完成",
) -> NotificationEvent:
    counts = _counts(stats)
    stopped = bool(stats.get("stopped"))
    scan_incomplete = _scan_incomplete(stats)
    non_confirmation_attention = bool(
        stopped or scan_incomplete or counts["skipped"] or counts["failed"] or organize_strm_error(stats)
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
    if stats.get('resumed_download_ids'):
        fields.append(("后台续接", f"{len(stats['resumed_download_ids'])} 个下载整理任务，等待重新核验文件落稳"))
        footer = (footer + "\n已移动的下载目录将由后台继续整理，无需重复下达指令；当前不代表已经入库。").strip()
    fields.extend((("STRM", strm_status), ("媒体库", media_refresh)))
    if stopped:
        lifecycle_state = "stopped"
    elif (strm_status or "").strip() in _PENDING_STRM_STATES:
        lifecycle_state = "queued"
    elif counts["failed"] and (strm_status or "").strip() == "未执行":
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
        lifecycle={"parent": {
            "title": title, "footer": footer,
            "state": lifecycle_state if lifecycle_state in {"stopped", "failed"} else
                     "partial" if attention else "completed",
            "importance": _importance(stats).value,
        }},
    )
    return attach_bounded_media_details(
        event,
        build_media_detail_blocks(
            tuple(stats.get("media_items") or ()),
            inventory_final=not (attention or stopped),
        ),
    )


def update_organize_lifecycle_confirmations(
    task_id: str,
    *,
    chat_id: str = "",
    baseline: Mapping[str, Any],
    outcomes: Mapping[str, Any],
    topic_enabled: bool = True,
) -> NotificationPublishResult:
    """全部 Telegram 候选收口后，一次性回写原整理汇总。"""
    thread_key = f"organize:{task_id!s}"
    snapshot = get_notification_thread_snapshot(
        thread_key, topic=NotificationTopic.ORGANIZE, chat_id=chat_id,
    )
    if snapshot is None:
        return NotificationPublishResult(False, status="missing_thread")
    previous = snapshot.event

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
    confirmation_parts = [f"已处理 {handled_groups} / {expected_groups} 组"]
    for key, label in (
        ("moved", "入库"),
        ("skipped", "跳过"),
        ("failed", "失败"),
        ("expired", "过期"),
    ):
        value = safe_int(outcomes.get(key), 0, minimum=0)
        if value:
            confirmation_parts.append(f"{label} {value} 个视频")
    if counts["confirm"]:
        confirmation_parts.append(f"Web 待处理 {counts['confirm']} 个视频")

    fields = _replace_field(
        previous.fields, "整理", _summary_label(counts, expired=expired),
    )
    fields = _replace_field(fields, "人工确认", " · ".join(confirmation_parts))
    stopped = bool(baseline.get("stopped"))
    scan_incomplete = bool(baseline.get("scan_incomplete"))
    downstream_failed = _event_downstream_failed(previous)
    parent_attention = bool(
        stopped
        or scan_incomplete
        or counts["confirm"]
        or counts["skipped"]
        or counts["failed"]
        or expired
    )
    attention = parent_attention or downstream_failed
    if stopped:
        title = "⏹️ 光鸭整理已停止"
    elif attention:
        title = "⚠️ 光鸭整理部分完成"
    else:
        title = "✅ 光鸭整理完成"

    if counts["confirm"]:
        footer = (
            f"Telegram 候选已处理；仍有 {counts['confirm']} 个视频保留在 Web "
            "待确认队列。"
        )
    elif expired:
        footer = (
            f"{expired} 个视频超过 24 小时未确认，文件保持原位；"
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
    inventory_status = _confirmation_inventory_status(
        counts,
        stopped=stopped,
        scan_incomplete=scan_incomplete,
        expired=expired,
    )
    event = NotificationEvent(
        title,
        fields=fields,
        lines=_confirmation_media_lines(
            previous.lines, inventory_status=inventory_status,
        ),
        footer=footer,
        actions=previous.actions,
        layout=previous.layout,
        field_emojis=previous.field_emojis,
        state=lifecycle_state,
        lifecycle={**previous.lifecycle, "downstream": previous.lifecycle.get("downstream", {
            "partial": downstream_failed,
            "error": str(previous.footer) if downstream_failed else "",
        }), "parent": {
            "title": ("⏹️ 光鸭整理已停止" if stopped else
                      "⚠️ 光鸭整理部分完成" if parent_attention else "✅ 光鸭整理完成"),
            "footer": footer,
            "state": "stopped" if stopped else "partial" if parent_attention else "completed",
            "importance": "error" if parent_attention else "result",
        }},
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
        expected_revision=snapshot.revision,
    )


def _importance(stats: Mapping[str, Any]) -> NotificationImportance:
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
    stats: Mapping[str, Any],
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
        delivery_route=stats.get("delivery_route"),
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
    refresh_handoff: dict[str, Any] | None = None,
) -> NotificationPublishResult:
    thread_key = f"organize:{task_id!s}"
    for _ in range(3):
        snapshot = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.ORGANIZE, chat_id=chat_id,
        )
        if snapshot is None:
            return NotificationPublishResult(False, status="missing_thread")
        previous = snapshot.event
        downstream = previous.lifecycle.get("downstream", {})
        receipt = previous.lifecycle.get("refresh")
        if refresh_handoff:
            entries = set(tuple(item) for item in (receipt or {}).get("entries", []))
            entries.update((path, refresh_handoff["allow_emby"]) for path in refresh_handoff["paths"])
            receipt = {"entries": sorted(entries), "pending_label": media_refresh, "error": error}
        # 刷新错误单独结算；后续单批成功不能清掉前批非刷新错误。
        preserve_failure = bool(refresh_handoff) or not (partial or error)
        event, importance = _project_downstream(
            previous, strm_status=strm_status, media_refresh=media_refresh,
            partial=bool(downstream.get("partial")) if preserve_failure else partial,
            error=str(downstream.get("error") or "") if preserve_failure else error,
            refresh_handoff=receipt,
        )
        result = publish_notification_thread(
            thread_key, event, topic=NotificationTopic.ORGANIZE,
            importance=importance, chat_id=chat_id, topic_enabled=topic_enabled,
            expected_revision=snapshot.revision,
        )
        if result or result.status != "invalid":
            return result
        # 确认汇总/刷新恢复抢先提交：重新读取后合并，不回写旧父统计和旧 marker。
    return result


def _project_downstream(
    previous: NotificationEvent, *, strm_status: str, media_refresh: str,
    partial: bool = False, error: str = "", refresh_handoff: dict[str, Any] | None = None,
) -> tuple[NotificationEvent, str]:
    # 老消息没有分层事实，保留原结论；不能把无法归因的警告推断成成功。
    parent = previous.lifecycle.get("parent") or {
        "title": str(previous.title), "footer": str(previous.footer),
        "state": (previous.state if previous.state in {"stopped", "failed"} else
                  "stopped" if str(previous.title).startswith("⏹️") else
                  "partial" if str(previous.title).startswith(("⚠️", "⏳")) else "completed"),
        "importance": "action" if str(previous.title).startswith("⏳") else "result",
    }
    failed = bool(partial or error or refresh_handoff)
    state = (parent["state"] if parent["state"] in {"stopped", "failed"} else
             "queued" if strm_status.strip() in _PENDING_STRM_STATES else
             "partial" if failed else parent["state"])
    fields = _replace_field(previous.fields, "STRM", strm_status)
    if refresh_handoff:
        media_refresh = refresh_handoff["pending_label"]
    fields = _replace_field(fields, "媒体库", media_refresh)
    lifecycle = {"parent": parent, "downstream": {"partial": partial, "error": error}}
    if refresh_handoff:
        lifecycle["refresh"] = refresh_handoff
    downstream_error = error or (refresh_handoff or {}).get("error", "")
    event = replace(
        previous,
        title="⚠️ 光鸭整理链路部分完成" if failed and state != "stopped" else parent["title"],
        footer=downstream_error[:260] if downstream_error else parent["footer"], fields=fields,
        state=state, lifecycle=lifecycle,
    )
    return event, "error" if failed else parent["importance"]


def reconcile_organize_refresh_notifications() -> int:
    """刷新 ACK 后（也包括进程重启），通过原通知 outbox 幂等收敛同一消息。"""
    global _refresh_recovery_after_id
    from app.modules.organize_probe_notifications import probe_downstream_state

    updated = 0
    try:
        with _refresh_recovery_lock:
            rows = list_recovered_organize_refresh_notifications(after_id=_refresh_recovery_after_id)
            if not rows and _refresh_recovery_after_id:
                rows = list_recovered_organize_refresh_notifications()
            # 无论本页写入成功、CAS 冲突或载荷损坏都推进；到尾后绕回重试。
            _refresh_recovery_after_id = int(rows[-1]["id"]) if rows else 0
    except Exception:
        get_logger(__name__).exception("读取整理刷新通知失败，下轮重试")
        return 0
    for row in rows:
        try:
            previous = deserialize_notification_event(row["event_json"])
            downstream = previous.lifecycle.get("downstream", {})
            status, partial, error = probe_downstream_state(
                {"topic": "organize", "thread_key": row["thread_key"], "chat_id": row["chat_id"]},
                strm_status=_field_value(previous, "STRM"),
                partial=bool(downstream.get("partial")), error=str(downstream.get("error") or ""),
            )
            event, importance = _project_downstream(
                # ACK 证明刷新队列已接管；配置可能已变，不猜测具体 provider 或扫描终态。
                previous, strm_status=status, media_refresh="已排队",
                partial=partial, error=error,
            )
            result = publish_notification_thread(
                row["thread_key"], event, topic=NotificationTopic.ORGANIZE,
                importance=importance, chat_id=row["chat_id"], expected_revision=row["revision"],
                update_existing_only=True,
            )
            updated += bool(result)
        except Exception:
            # 保留原通知中的刷新依据，下轮重试；不能使成功 ACK 反向失败。
            get_logger(__name__).exception("恢复整理刷新通知失败 event=%s", row["event_key"])
    return updated
