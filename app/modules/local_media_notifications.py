"""本地媒体整理的结构化 Telegram 通知。只展示文件名，不泄露本地绝对路径。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app import config
from app import database as db
from app.agent.public_safety import sanitize_public_text
from app.logger import get_logger
from app.notifier import NOTIFICATION_SECTION_BREAK, NotificationEvent
from app.sensitive_data import redact_sensitive_text

logger = get_logger(__name__)

_TRIGGER_LABELS = {
    "qb_completed": "qB 下载完成",
    "scan": "手动扫描",
    "manual": "手动整理",
}


def _value(data: Mapping[str, Any] | None, key: str, default: Any = "") -> Any:
    return data.get(key, default) if isinstance(data, Mapping) else default


def _basename(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1] if text else ""


def _safe_error_summary(value: object) -> str:
    raw = str(value or "").strip()
    normalized = raw.casefold()
    mappings = (
        (
            ("目标目录不存在", "归档目录不存在"),
            "目标归档目录不存在或不可访问，请检查媒体来源的归档路径。",
        ),
        (
            ("permission denied", "权限不足", "不可写"),
            "目标目录权限不足，请检查容器挂载与读写权限。",
        ),
        (
            ("源文件不存在", "no such file", "文件已不存在"),
            "源文件已不存在或已被其他任务移动，请重新扫描来源。",
        ),
        (
            ("目标已存在", "移动冲突", "发生变化"),
            "文件状态或目标位置已变化，请重新预览后再整理。",
        ),
        (("tmdb",), "TMDB 识别或查询失败，请稍后重试或手动确认。"),
        (
            ("媒体库刷新", "jellyfin", "emby"),
            "媒体文件已处理，但媒体库刷新未完成，请检查媒体服务器连接。",
        ),
    )
    for markers, message in mappings:
        if any(marker in normalized for marker in markers):
            return message
    safe = sanitize_public_text(redact_sensitive_text(raw), limit=180)
    return safe or "执行失败，请到本地媒体任务详情查看。"


def build_local_media_event(
    task,
    source,
    result: Mapping[str, Any] | None = None,
    *,
    error: str = "",
    chat_id: str = "",
) -> NotificationEvent:
    """根据任务终态生成一条可安全发送的通知。"""
    payload = result or {}
    status = str(_value(payload, "status", getattr(task, "status", "")) or "")
    target_name = _basename(task.content_path) or "未命名条目"
    fields: list[tuple[object, object]] = [
        ("任务编号", f"#{task.id}"),
        ("触发方式", _TRIGGER_LABELS.get(task.trigger, task.trigger)),
        ("存储来源", source.name if source else "已删除来源"),
        NOTIFICATION_SECTION_BREAK,
        ("目标文件", target_name),
    ]
    lines: list[str] = []

    if status == "completed":
        moved = list(_value(payload, "moved", []) or [])
        deleted = list(_value(payload, "deleted_junk", []) or [])
        warnings = list(_value(payload, "warnings", []) or [])
        if not warnings and not result:
            warnings = [
                item
                for item in str(getattr(task, "warning", "") or "").split("；")
                if item
            ]
        refresh_status = (
            str(_value(payload, "media_refresh_status", "") or "").strip().lower()
        )
        if not refresh_status:
            refresh_status = (
                "failed"
                if any(
                    "刷新失败" in item or "未刷新媒体库" in item for item in warnings
                )
                else "completed"
            )
        refresh_label = {
            "completed": "刷新完成 🎯",
            "queued": "已排队（合并刷新） ⏳",
            "failed": "刷新失败（需处理） ❌",
            "skipped": "未启用",
        }.get(refresh_status, "状态未知")
        fields.extend(
            (
                (
                    "执行结果",
                    f"已移动 {len(moved)} · "
                    f"清理 {len(deleted)} 个确认垃圾文件 · 警告 {len(warnings)}",
                ),
                ("媒体库刷新", refresh_label),
            )
        )
        media = list(_value(payload, "media", []) or [])
        for item in media[:8]:
            title = str(_value(item, "title", "") or "未识别媒体")
            year = str(_value(item, "year", "") or "")
            tmdb_id = str(_value(item, "tmdb_id", "") or "")
            label = f"🎬 {title}{f' ({year})' if year else ''}"
            if tmdb_id:
                label += f" · TMDB {tmdb_id}"
            lines.append(label)
            target_name = _basename(_value(item, "target_name", ""))
            if target_name:
                lines.append(f"└── 📄 {target_name}")
        if len(media) > 8:
            lines.append(f"…另有 {len(media) - 8} 个媒体未展示")
        if warnings:
            lines.append(f"⚠️ 共有 {len(warnings)} 条警告，请到本地媒体任务详情查看。")
        title = "✅ 本地媒体整理完成"
        if refresh_status == "failed":
            title = "⚠️ 本地媒体整理部分完成"
        return NotificationEvent(
            title,
            fields=tuple(fields),
            lines=tuple(lines),
            layout="relaxed",
        )

    preview = _value(payload, "preview", {})
    if status == "requires_manual":
        reason = str(
            _value(preview, "reason", "")
            or getattr(task, "error", "")
            or "TMDB 结果需要人工确认"
        )
        candidate = _value(preview, "candidate", {})
        title = str(_value(candidate, "title", "") or "")
        year = str(_value(candidate, "year", "") or "")
        tmdb_id = str(_value(candidate, "tmdb_id", "") or "")
        confidence = _value(candidate, "confidence", "")
        fields.append(("待确认原因", reason))
        if title:
            fields.append(("候选", f"{title}{f' ({year})' if year else ''}"))
        if tmdb_id:
            fields.append(("TMDB", tmdb_id))
        if confidence not in (None, ""):
            try:
                fields.append(("置信度", f"{float(confidence) * 100:.0f}%"))
            except (TypeError, ValueError):
                pass
        from app.modules.organize_confirmations import (
            create_local_media_confirmation_actions,
        )

        try:
            actions = create_local_media_confirmation_actions(
                task,
                source,
                dict(preview) if isinstance(preview, Mapping) else {},
                owner=str(getattr(task, "owner", "admin") or "admin"),
                chat_id=chat_id,
            )
        except Exception as exc:
            actions = ()
            logger.warning(
                "本地媒体 TG 确认按钮生成失败 task=%s type=%s",
                getattr(task, "id", ""),
                type(exc).__name__,
            )
        footer = (
            "请选择下方候选继续整理。"
            if actions
            else "当前信息不足以安全直接确认，请前往 Web 的本地媒体待确认页处理。"
        )
        return NotificationEvent(
            "⚠️ 本地媒体待确认",
            fields=tuple(fields),
            footer=footer,
            actions=actions,
            layout="relaxed",
        )

    if status == "planned":
        fields.append(("处理状态", "预览已生成，尚未执行文件操作"))
        return NotificationEvent(
            "ℹ️ 本地媒体预览完成",
            fields=tuple(fields),
            layout="relaxed",
        )

    fields.append(
        (
            "错误原因",
            _safe_error_summary(error or getattr(task, "error", "")),
        )
    )
    return NotificationEvent(
        "❌ 本地媒体整理失败",
        fields=tuple(fields),
        layout="relaxed",
    )


def schedule_local_media_task_review(
    task_id: int,
    result: Mapping[str, Any] | None,
    *,
    owner: str = "admin",
    chat_id: str = "",
) -> bool:
    """无通知或静默任务也可进入同一冻结 Agent 复核队列。"""
    preview = _value(result, "preview", {})
    if not isinstance(preview, Mapping):
        return False
    task = db.get_local_media_task(task_id, owner=owner)
    if task is None or str(getattr(task, "status", "")) != "requires_manual":
        return False
    source = db.get_local_media_source(task.source_id, owner=owner)
    if source is None:
        return False
    try:
        from app.modules.organize_confirmations import (
            schedule_local_media_recognition_review,
        )

        return schedule_local_media_recognition_review(
            task,
            source,
            dict(preview),
            owner=owner,
            chat_id=chat_id,
        )
    except Exception as exc:  # noqa: BLE001 - 自动复核不可阻断人工链路
        logger.warning(
            "本地媒体 Agent 复核调度失败 task=%s type=%s",
            task_id,
            type(exc).__name__,
        )
        return False


def notify_local_media_task(
    task_id: int,
    result: Mapping[str, Any] | None = None,
    *,
    owner: str = "admin",
    error: str = "",
    chat_id: str = "",
) -> bool:
    """读取任务上下文并发送通知；任务不存在时静默跳过。"""
    notifications_enabled = config.get_bool(
        "GY_ORGANIZE_NOTIFY_ENABLED", True
    ) and config.get_bool("GY_ORGANIZE_LIBRARY_NOTIFY", True)
    if not notifications_enabled:
        schedule_local_media_task_review(
            task_id, result, owner=owner, chat_id=chat_id
        )
        return False
    task = db.get_local_media_task(task_id, owner=owner)
    if task is None:
        return False
    source = db.get_local_media_source(task.source_id, owner=owner)
    event = build_local_media_event(
        task,
        source,
        result,
        error=error,
        chat_id=chat_id,
    )
    linked_requests = db.list_download_requests_for_local_media_task(task_id)
    if linked_requests:
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle

        accepted = False
        for request in linked_requests:
            outcome = publish_download_lifecycle(
                int(request["id"]),
                stats=result,
            )
            accepted = bool(outcome) or accepted
        # 人工候选按钮必须独立保留；其余结果已合并到下载事务消息。
        if event.actions:
            from app.modules.organize_confirmations import publish_confirmation_event

            accepted = publish_confirmation_event(event, chat_id=chat_id) or accepted
        return accepted

    if event.actions:
        from app.modules.organize_confirmations import publish_confirmation_event

        return publish_confirmation_event(event, chat_id=chat_id)

    from app.modules.telegram_notification_center import publish_notification_thread
    from app.modules.telegram_notification_policy import (
        NotificationImportance,
        NotificationTopic,
    )

    status = str((result or {}).get("status") or getattr(task, "status", ""))
    importance = (
        NotificationImportance.ERROR
        if event.title.startswith(("❌", "⚠️"))
        else NotificationImportance.ACTION
        if status == "requires_manual"
        else NotificationImportance.RESULT
    )
    return bool(
        publish_notification_thread(
            f"local-media:{int(task_id)}",
            event,
            topic=NotificationTopic.LOCAL_MEDIA,
            importance=importance,
            chat_id=chat_id,
            topic_enabled=(
                config.get_bool("GY_ORGANIZE_NOTIFY_ENABLED", True)
                and config.get_bool("GY_ORGANIZE_LIBRARY_NOTIFY", True)
            ),
        )
    )
