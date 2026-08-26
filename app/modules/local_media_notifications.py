"""本地媒体整理的结构化 Telegram 通知。只展示文件名，不泄露本地绝对路径。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app import config
from app import database as db
from app.logger import get_logger
from app.notifier import NotificationEvent, send_event

logger = get_logger(__name__)
send = send_event

_TRIGGER_LABELS = {
    "qb_completed": "qB 下载完成",
    "scan": "定时扫描",
    "manual": "手动整理",
}


def _value(data: Mapping[str, Any] | None, key: str, default: Any = "") -> Any:
    return data.get(key, default) if isinstance(data, Mapping) else default


def _basename(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1] if text else ""


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
    fields: list[tuple[object, object]] = [
        ("任务", f"#{task.id}"),
        ("触发方式", _TRIGGER_LABELS.get(task.trigger, task.trigger)),
        ("来源", source.name if source else "已删除来源"),
        ("文件", _basename(task.content_path)),
    ]
    lines: list[str] = []

    if status == "completed":
        moved = list(_value(payload, "moved", []) or [])
        deleted = list(_value(payload, "deleted_junk", []) or [])
        warnings = list(_value(payload, "warnings", []) or [])
        if not warnings and not result:
            warnings = [item for item in str(getattr(task, "warning", "") or "").split("；") if item]
        refresh_status = str(_value(payload, "media_refresh_status", "") or "").strip().lower()
        if not refresh_status:
            refresh_status = "failed" if any("刷新失败" in item or "未刷新媒体库" in item for item in warnings) else "completed"
        refresh_label = {
            "completed": "完成",
            "failed": "失败（需处理）",
            "skipped": "未启用",
        }.get(refresh_status, "状态未知")
        fields.extend((
            ("已移动", f"{len(moved)} 个文件"),
            ("清理", f"{len(deleted)} 个确认垃圾文件"),
            ("媒体库刷新", refresh_label),
        ))
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
        return NotificationEvent("✅ 本地媒体整理完成", fields=tuple(fields), lines=tuple(lines))

    preview = _value(payload, "preview", {})
    if status == "requires_manual":
        reason = str(_value(preview, "reason", "") or getattr(task, "error", "") or "TMDB 结果需要人工确认")
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
        return NotificationEvent("ℹ️ 本地媒体预览完成", fields=tuple(fields))

    fields.append(("错误", "执行失败，请到本地媒体任务详情查看"))
    return NotificationEvent("❌ 本地媒体整理失败", fields=tuple(fields))


def notify_local_media_task(
    task_id: int,
    result: Mapping[str, Any] | None = None,
    *,
    owner: str = "admin",
    error: str = "",
    chat_id: str = "",
) -> bool:
    """读取任务上下文并发送通知；任务不存在时静默跳过。"""
    if (not config.get_bool("GY_ORGANIZE_NOTIFY_ENABLED", True)
            or not config.get_bool("GY_ORGANIZE_LIBRARY_NOTIFY", True)):
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
    if chat_id:
        return bool(send(event, chat_id=chat_id))
    return bool(send(event))
