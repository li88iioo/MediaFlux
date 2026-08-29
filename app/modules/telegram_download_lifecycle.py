"""下载、整理、STRM 与媒体库复核的一条 Telegram 事务时间线。"""
from __future__ import annotations

from collections.abc import Mapping
import json

from app import config, database as db
from app.modules.telegram_notification_center import (
    NotificationPublishResult,
    get_notification_thread_event,
    publish_notification_thread,
)
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.modules.telegram_media_projection import (
    attach_bounded_media_details,
    build_media_detail_blocks,
)
from app.notifier import NotificationEvent, safe_int

_STATUS_LABELS = {
    "": "—",
    "pending": "等待中",
    "submitted": "已提交",
    "downloading": "下载中",
    "outcome_unknown": "结果待核对",
    "completed": "完成",
    "complete": "完成",
    "success": "完成",
    "succeeded": "完成",
    "running": "进行中",
    "queued": "已排队",
    "settling": "等待文件落稳",
    "planned": "已生成预览",
    "requires_manual": "需要确认",
    "manual_review": "需要人工核对",
    "partial": "部分完成",
    "stopped": "已停止",
    "skipped": "已跳过",
    "failed": "失败",
}
_ATTENTION_STATES = {"manual_review", "requires_manual"}
_ERROR_STATES = {"failed", "partial", "stopped", "outcome_unknown"}
_PROCESSING_STATES = {"pending", "submitted", "downloading", "running", "queued", "settling"}


def _value(row, key: str, default: object = "") -> object:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _status(value: object) -> str:
    return str(value or "").strip().lower()


def _notification_payload(row) -> dict[str, object]:
    raw = str(_value(row, "notification_payload_json", "") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _chat_id(row) -> str:
    payload = _notification_payload(row)
    return str(payload.get("chat_id") or _value(row, "chat_id", "") or "").strip()


def _label(value: object, *, empty: str = "—") -> str:
    normalized = _status(value)
    return _STATUS_LABELS.get(normalized, normalized or empty)


def _download_label(row) -> str:
    parts: list[str] = []
    qb = _status(_value(row, "qb_status"))
    gy = _status(_value(row, "gy_status"))
    if qb:
        parts.append(f"qB {_label(qb)}")
    if gy:
        parts.append(f"光鸭 {_label(gy)}")
    return " · ".join(parts) or _label(_value(row, "status"), empty="等待开始")


def _archive_label(row) -> tuple[str, str]:
    local_status = _status(_value(row, "local_import_status"))
    organize_status = _status(_value(row, "organize_status"))
    if local_status:
        return "本地整理", _label(local_status)
    if organize_status:
        return "光鸭整理", _label(organize_status)
    return "自动整理", "等待下载完成"


def _importance(row, *, verification_status: str = "") -> NotificationImportance:
    states = {
        _status(_value(row, key))
        for key in (
            "status", "qb_status", "gy_status", "local_import_status",
            "organize_status", "strm_status",
        )
    }
    verification = _status(verification_status)
    if states.intersection(_ATTENTION_STATES) or verification == "attention":
        return NotificationImportance.ACTION
    if states.intersection(_ERROR_STATES):
        return NotificationImportance.ERROR
    return NotificationImportance.RESULT


def _overall_state(row, *, verification_status: str = "") -> str:
    states = {
        _status(_value(row, key))
        for key in (
            "status", "qb_status", "gy_status", "local_import_status",
            "organize_status", "strm_status",
        )
        if _status(_value(row, key))
    }
    verification = _status(verification_status)
    if states.intersection(_ATTENTION_STATES) or verification == "attention":
        return "attention"
    if states.intersection(_ERROR_STATES):
        return "error"
    if states.intersection(_PROCESSING_STATES):
        return "processing"
    downstream = {
        _status(_value(row, "local_import_status")),
        _status(_value(row, "organize_status")),
        _status(_value(row, "strm_status")),
    } - {""}
    if downstream and downstream.issubset({"completed", "success", "skipped"}):
        return "completed"
    if _status(_value(row, "status")) in {"completed", "success"}:
        return "completed"
    return "processing"



def _previous_field(event: NotificationEvent | None, label: str) -> str:
    if event is None:
        return ""
    for current_label, value in event.fields:
        if str(current_label) == label:
            return str(value or "")
    return ""


def build_download_lifecycle_event(
    row,
    *,
    stats: Mapping[str, object] | None = None,
    media_refresh: str = "",
    verification_status: str = "",
    verification_result: str = "",
) -> NotificationEvent:
    request_id = int(_value(row, "id", 0) or 0)
    chat_id = _chat_id(row)
    notification_payload = _notification_payload(row)
    previous = get_notification_thread_event(
        f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id=chat_id,
    )
    state = _overall_state(row, verification_status=verification_status)
    title = {
        "attention": "⚠️ 下载入库需要处理",
        "error": "⚠️ 下载入库部分完成",
        "completed": "✅ 下载与入库完成",
        "processing": "⏳ 下载与入库处理中",
    }[state]
    archive_name, archive_value = _archive_label(row)
    fields: list[tuple[object, object]] = [
        ("媒体", str(
            notification_payload.get("title")
            or _value(row, "title", "")
            or "未命名任务"
        )[:160]),
        ("下载", _download_label(row)),
        (archive_name, archive_value),
    ]
    strm_status = _status(_value(row, "strm_status"))
    if strm_status:
        fields.append(("STRM", _label(strm_status)))
    refresh_value = str(media_refresh or "").strip() or _previous_field(previous, "媒体库")
    if refresh_value:
        fields.append(("媒体库", refresh_value))
    if verification_status:
        fields.append((
            "入库复核",
            str(verification_result or _label(verification_status)).strip(),
        ))

    media_blocks = build_media_detail_blocks(
        tuple((stats or {}).get("media_items") or ()),
        inventory_final=not bool(
            stats
            and (
                stats.get("stopped")
                or stats.get("scan_errors")
                or stats.get("scan_limited")
                or stats.get("scan_complete") is False
                or safe_int(stats.get("need_confirm"), 0, minimum=0)
                or safe_int(stats.get("skipped"), 0, minimum=0)
                or safe_int(stats.get("failed"), 0, minimum=0)
            )
        ),
    )
    lines = media_blocks or (previous.lines if previous is not None else ())
    errors: list[str] = []
    for key in ("error", "local_import_error", "organize_error", "strm_error"):
        value = str(_value(row, key, "") or "").strip()
        if value and value not in errors:
            errors.append(value[:220])
    footer = ""
    if state == "attention":
        footer = "请按候选按钮继续处理；若按钮未送达，可在 Web 待确认队列操作。"
    elif state == "error":
        footer = errors[0] if errors else "本次链路存在未完成阶段，请查看 Web 运行记录。"
    elif state == "processing":
        footer = "后续阶段会更新本条消息，无需重复提交。"
    event = NotificationEvent(
        title,
        fields=tuple(fields),
        footer=footer,
        layout="relaxed",
        field_emojis=False,
    )
    return attach_bounded_media_details(event, lines)


def publish_download_lifecycle(
    request_id: int,
    *,
    stats: Mapping[str, object] | None = None,
    media_refresh: str = "",
    verification_status: str = "",
    verification_result: str = "",
    deliver_now: bool = True,
) -> NotificationPublishResult:
    row = db.get_download_request(int(request_id))
    if row is None:
        return NotificationPublishResult(False, status="missing_request")
    event = build_download_lifecycle_event(
        row,
        stats=stats,
        media_refresh=media_refresh,
        verification_status=verification_status,
        verification_result=verification_result,
    )
    importance = _importance(row, verification_status=verification_status)
    topic_enabled = config.get_bool("GY_ORGANIZE_NOTIFY_ENABLED", True)
    # 下载异常和人工处理不应被“整理成功通知”开关吞掉；全局通知总开关仍生效。
    if importance in {NotificationImportance.ACTION, NotificationImportance.ERROR}:
        topic_enabled = True
    return publish_notification_thread(
        f"download:{int(request_id)}",
        event,
        topic=NotificationTopic.DOWNLOAD,
        importance=importance,
        chat_id=_chat_id(row),
        topic_enabled=topic_enabled,
        deliver_now=deliver_now,
    )
