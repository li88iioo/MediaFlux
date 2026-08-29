"""光鸭整理与后续 STRM/媒体库刷新的单消息事务投影。"""
from __future__ import annotations

from collections.abc import Mapping

from app.modules.telegram_notification_center import (
    NotificationPublishResult,
    get_notification_thread_event,
    publish_notification_thread,
)
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import NotificationEvent, safe_int


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


def _media_lines(stats: Mapping[str, object]) -> tuple[str, ...]:
    rows = [item for item in (stats.get("media_items") or []) if isinstance(item, Mapping)]
    grouped: dict[tuple[str, str, str, int], set[int]] = {}
    for item in rows:
        title = str(item.get("title") or "未识别媒体").strip()
        year = str(item.get("year") or "").strip()
        media_type = str(item.get("media_type") or "").strip().lower()
        season = safe_int(item.get("season"), 0, minimum=0)
        episode = safe_int(item.get("episode"), 0, minimum=0)
        grouped.setdefault((title, year, media_type, season), set())
        if episode:
            grouped[(title, year, media_type, season)].add(episode)
    lines: list[str] = []
    for (title, year, media_type, season), episodes in list(grouped.items())[:6]:
        suffix = f" ({year})" if year else ""
        if media_type == "tv" and season:
            suffix += f" · S{season:02d}"
            if episodes:
                ordered = sorted(episodes)
                if len(ordered) <= 4:
                    suffix += " · " + ", ".join(f"E{value:02d}" for value in ordered)
                else:
                    suffix += f" · {len(ordered)} 集"
        lines.append(f"• {title}{suffix}")
    if len(grouped) > len(lines):
        lines.append(f"…另有 {len(grouped) - len(lines)} 项媒体")
    return tuple(lines)


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
        unresolved_files = max(0, counts["confirm"] - actionable_files)
        confirmation_parts: list[str] = []
        if actionable_groups:
            confirmation_parts.append(
                f"{actionable_files} 个文件 / {actionable_groups} 组按钮卡"
            )
        if unresolved_files:
            confirmation_parts.append(f"{unresolved_files} 个暂无候选")
        confirmation_label = " · ".join(confirmation_parts) or "暂无可用候选"
        if actionable_groups and unresolved_files:
            footer = (
                "可操作候选会以独立按钮卡发送；暂无候选的项目仍保留在 Web "
                "待确认队列。"
            )
        elif actionable_groups:
            footer = "候选已按媒体合并为独立按钮卡；任务快照同时保留在 Web。"
        else:
            footer = "本轮暂无可用 TMDB 候选，请在 Web 待确认队列手动识别。"
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
    return NotificationEvent(
        title,
        fields=tuple(fields),
        lines=_media_lines(stats),
        footer=footer,
        layout="relaxed",
        field_emojis=False,
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
        f"organize:{str(task_id)}",
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
    thread_key = f"organize:{str(task_id)}"
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
    )
    return publish_notification_thread(
        thread_key,
        event,
        topic=NotificationTopic.ORGANIZE,
        importance=importance,
        chat_id=chat_id,
        topic_enabled=topic_enabled,
    )
