"""下载、整理、STRM 与媒体库复核的一条 Telegram 事务时间线。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
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
from app.repositories.download_requests import download_display_title, usable_download_title

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
    "cancelled": "已停止跟踪",
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
    downloads = {_status(_value(row, key)) for key in ("qb_status", "gy_status")} - {""}
    if downloads and downloads.issubset({"failed", "manual_review", "cancelled", "resubmitted"}):
        return "自动整理", "未启动（需核对下载状态）" if "manual_review" in downloads else "未启动（下载已停止）"
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
    archive_states = {
        _status(_value(row, "local_import_status")),
        _status(_value(row, "organize_status")),
    } - {""}
    archive_completed = archive_states.intersection({"completed", "success"})
    if "skipped" in archive_states and not archive_completed:
        return "downloaded"
    downstream = archive_states | {
        _status(_value(row, "strm_status")),
    } - {""}
    if downstream and downstream.issubset({"completed", "success", "skipped"}):
        return "completed"
    if _status(_value(row, "status")) == "cancelled":
        return "cancelled"
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


def _load_probe_progress(row) -> dict[str, int]:
    """只投影本请求、当前收件人的可见补全状态，不改变任何业务终态。"""
    from app.repositories.organize_probe import get_organize_probe_notification_progress

    request_id = int(_value(row, "id", 0) or 0)
    chat_id = _chat_id(row) or str(config.get("TG_CHAT_ID", "") or "").strip()
    if request_id <= 0 or not chat_id:
        return {}
    return get_organize_probe_notification_progress(
        topic=NotificationTopic.DOWNLOAD,
        thread_key=f"download:{request_id}",
        chat_id=chat_id,
        notification_enabled_only=True,
    )


def _probe_needs_attention(progress: Mapping[str, int]) -> bool:
    return any(int(progress.get(key, 0) or 0) > 0 for key in ("failed", "cancelled", "strm_failed"))


def _merge_probe_progress(
    event: NotificationEvent, row, progress: Mapping[str, int], *, verification_status: str,
) -> NotificationEvent:
    if not progress.get("total") and not progress.get("strm_pending"):
        return event
    needs_attention = _probe_needs_attention(progress)
    pending = bool(progress.get("pending") or progress.get("strm_pending"))
    label = "需要复核" if needs_attention else "进行中" if pending else "完成"
    fields = tuple(event.fields) + (("后台规格补全", label),)
    base_state = _overall_state(row, verification_status=verification_status)
    if needs_attention:
        # ACTION 优先级与原业务异常保留；不能把后台失败写回 download_requests
        # 让已成功下载/整理被调度器重新执行。异常细节不跨通知范围外泄。
        title = event.title if base_state in {"attention", "error"} else "⚠️ 下载入库链路部分完成"
        note = "后台规格补全或后续 STRM 同步尚有异常，请在 Web 运行记录中复核。"
        footer = "\n".join(value for value in (event.footer, note) if value)
        return replace(event, title=title, fields=fields, footer=footer, state="partial")
    if pending and base_state not in {"attention", "error"}:
        return replace(
            event, title="⏳ 下载与入库处理中", fields=fields, state="processing",
            footer=event.footer or "后续阶段会更新本条消息，无需重复提交。",
        )
    return replace(event, fields=fields)


def build_download_lifecycle_event(
    row,
    *,
    stats: Mapping[str, object] | None = None,
    media_refresh: str = "",
    verification_status: str = "",
    verification_result: str = "",
    probe_progress: Mapping[str, int] | None = None,
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
        "downloaded": "✅ 下载完成（自动入库已跳过）",
        "processing": "⏳ 下载与入库处理中",
        "cancelled": "⏹️ 下载跟踪已停止",
    }[state]
    archive_name, archive_value = _archive_label(row)
    media_title = notification_payload.get("title")
    if not usable_download_title(media_title):
        media_title = download_display_title(row)
    fields: list[tuple[object, object]] = [
        ("媒体", str(media_title)[:160]),
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
        states = {
            _status(_value(row, key))
            for key in (
                "status", "qb_status", "gy_status", "local_import_status",
                "organize_status", "strm_status",
            )
        }
        if "requires_manual" in states:
            footer = "整理候选卡会单独发送；若未收到，可在 Web 待确认队列继续处理。"
        elif _status(verification_status) == "attention":
            footer = "请在 Agent 中查询最近下载状态，或前往 Web 查看入库复核详情。"
        else:
            footer = "请前往 Web 下载任务核对当前状态；为避免重复提交，请勿直接重试。"
        if _status(_value(row, "qb_status")) == "manual_review" and _value(row, "qb_task_missing_since"):
            fields.append(("发现 qB 缺失", str(_value(row, "qb_task_missing_since"))))
            if _value(row, "created_at"):
                fields.append(("请求创建", str(_value(row, "created_at"))))
            footer = (
                "qB 任务持续未找到，无法仅凭缺失判断是否主动删除。"
                "若已主动移除，可在 Web 下载任务的待处理区移出记录；"
                "否则请核对下载器。为避免重复提交，请勿直接重试。"
            )
    elif state == "error":
        footer = errors[0] if errors else "本次链路存在未完成阶段，请查看 Web 运行记录。"
    elif state == "downloaded":
        footer = errors[0] if errors else "下载已完成，但本次没有执行自动入库。"
    elif state == "processing":
        footer = "后续阶段会更新本条消息，无需重复提交。"
    event = NotificationEvent(
        title,
        fields=tuple(fields),
        footer=footer,
        layout="relaxed",
    )
    event = _merge_probe_progress(
        event, row, _load_probe_progress(row) if probe_progress is None else probe_progress,
        verification_status=verification_status,
    )
    return attach_bounded_media_details(event, lines)


def download_notification_obsolescence(thread_key: str, event: NotificationEvent) -> str:
    """投递前仅核对撤销后的下载字段；重建仍回到唯一生命周期生产者。"""
    prefix = "download:"
    if not str(thread_key).startswith(prefix):
        return ""
    raw_id = str(thread_key)[len(prefix):]
    if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 19:
        return ""
    if not 0 < int(raw_id) < 2**63:
        return ""
    row = db.get_download_request(int(raw_id))
    # 历史纯通知未必绑定请求，不凭记录缺失推断其业务已撤销。
    if row is None or _status(_value(row, "qb_status")) != "cancelled":
        return ""
    if _status(_value(row, "status")) in {"cancelled", "resubmitted"}:
        return "cancelled"
    if _previous_field(event, "下载") == _download_label(row):
        return ""
    from app.repositories.download_requests import request_download_notification_refresh

    request_download_notification_refresh(int(raw_id))
    return "stale"


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
    probe_progress = _load_probe_progress(row)
    event = build_download_lifecycle_event(
        row,
        stats=stats,
        media_refresh=media_refresh,
        verification_status=verification_status,
        verification_result=verification_result,
        probe_progress=probe_progress,
    )
    importance = _importance(row, verification_status=verification_status)
    if importance == NotificationImportance.RESULT and _probe_needs_attention(probe_progress):
        importance = NotificationImportance.ERROR
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
