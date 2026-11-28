"""下载、整理、STRM 与媒体库复核的一条 Telegram 事务时间线。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from app import config
from app.database import get_task_run
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.repositories import agent_download_verification as agent_download_verification_repo
from app.modules.agent_download_verification_notifications import (
    _RESULT_LABELS as _VERIFICATION_RESULT_LABELS,
    VERIFICATION_HISTORY_WINDOW,
)
from app.modules.local_media_outcomes import (
    LOCAL_REFRESH_LABELS,
    QB_CLEANUP_WARNING,
    local_media_archive_stage,
    local_media_qb_cleanup_pending,
    local_media_refresh_status,
    local_media_task_outcome,
)
from app.modules.organize_results import read_organize_result
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
from app.notifier import NotificationEvent, notification_target_chat_id, safe_int
from app.repositories.download_requests import download_postprocess_issues
from app.repositories.download_requests import (
    download_display_title,
    safe_download_text,
    usable_download_title,
)

_STATUS_LABELS = {
    "": "—",
    "pending": "等待中",
    "submitted": "已提交",
    "submitting": "提交中",
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
_PROCESSING_STATES = {"pending", "submitting", "submitted", "downloading", "running", "queued", "settling", "planned"}


def _value(row, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _status(value: object) -> str:
    return str(value or "").strip().lower()


def _notification_payload(row) -> dict[str, Any]:
    raw = str(_value(row, "notification_payload_json", "") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
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


def _archive_stage(row, task, downloads: set[str]) -> tuple[str, str, str]:
    """一次读取本地文件事实，阶段文案、总状态及投递级别共同使用。"""
    local_status = _status(_value(row, "local_import_status"))
    organize_status = _status(_value(row, "organize_status"))
    if local_status == "completed" and task is not None:
        outcome = local_media_task_outcome(task, local_media_repo.list_local_media_task_items(task.id))
        status, detail = local_media_archive_stage(outcome)
        return "本地整理", status, detail
    if local_status:
        return "本地整理", local_status, _label(local_status)
    if organize_status:
        return "光鸭整理", organize_status, _label(organize_status)
    if downloads and downloads.issubset({"failed", "manual_review", "cancelled", "resubmitted"}):
        return "自动整理", "", "未启动（需核对下载状态）" if "manual_review" in downloads else "未启动（下载已停止）"
    detail = "尚无入库完成记录" if _status(_value(row, "status")) in {"completed", "success"} else "等待下载完成"
    return "自动整理", "", detail


def _current_organize_source_stats(row) -> dict[str, Any]:
    task_id = _value(row, "organize_task_id")
    run_id = _value(row, "organize_run_id")
    source_id = _value(row, "gy_target_dir")
    finished_at = _value(row, "organize_finished_at")
    if (
        not task_id or not finished_at or source_id in (None, "", "0")
        or type(run_id) is not int or run_id <= 0
    ):
        return {}
    run = get_task_run(run_id)
    if run is None or run["task_name"] != "guangya_organize" or run["status"] not in {"success", "partial"}:
        return {}
    # 人工重新整理可能更新请求完成时间，同时保留旧的运行关联。
    if not run["started_at"] <= finished_at <= (run["finished_at"] or ""):
        return {}
    try:
        result = read_organize_result(json.loads(run["result"] or "{}"))
    except (TypeError, ValueError, OverflowError):
        return {}
    sources = result.get("sources")
    if result.get("task_id") != task_id or not isinstance(sources, list):
        return {}
    matches = [
        source.get("stats") for source in sources
        if isinstance(source, dict) and source.get("id") == source_id
    ]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else {}


def _overall_state(row, *, archive_status: str, verification_status: str = "", local_issue: bool = False) -> str:
    states = {
        _status(_value(row, key))
        for key in (
            "status", "qb_status", "gy_status", "organize_status", "strm_status",
        )
        if _status(_value(row, key))
    } | {archive_status}
    states.update(item["status"] for item in download_postprocess_issues(row))
    verification = _status(verification_status)
    if states.intersection(_ATTENTION_STATES) or verification == "attention":
        return "attention"
    if states.intersection(_ERROR_STATES) or local_issue:
        return "error"
    if states.intersection(_PROCESSING_STATES) or verification in {"pending", "running", "retry_wait"}:
        return "processing"
    archive_states = {
        archive_status, _status(_value(row, "organize_status")),
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
        return "downloaded"
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
    chat_id = _chat_id(row) or (config.get("TG_CHAT_ID", "") or "").strip()
    if request_id <= 0 or not chat_id:
        return {}
    return get_organize_probe_notification_progress(
        topic=NotificationTopic.DOWNLOAD,
        thread_key=f"download:{request_id}",
        chat_id=chat_id,
        notification_enabled_only=True,
    )


def _probe_needs_attention(progress: Mapping[str, int]) -> bool:
    return any((progress.get(key, 0) or 0) > 0 for key in ("failed", "cancelled", "strm_failed"))


def _merge_probe_progress(
    event: NotificationEvent, progress: Mapping[str, int],
) -> NotificationEvent:
    if not progress.get("total") and not progress.get("strm_pending"):
        return event
    needs_attention = _probe_needs_attention(progress)
    pending = bool(progress.get("pending") or progress.get("strm_pending"))
    label = "需要复核" if needs_attention else "进行中" if pending else "完成"
    fields = tuple(event.fields) + (("后台规格补全", label),)
    base_state = event.state
    if needs_attention:
        # ACTION 优先级与原业务异常保留；不能把后台失败写回 download_requests
        # 让已成功下载/整理被调度器重新执行。异常细节不跨通知范围外泄。
        title = event.title if base_state in {"attention", "error"} else "⚠️ 下载入库链路部分完成"
        note = "后台规格补全或后续 STRM 同步尚有异常，请在 Web 运行记录中复核。"
        footer = "\n".join(str(value) for value in (event.footer, note) if value)
        return replace(event, title=title, fields=fields, footer=footer,
                       state="attention" if base_state == "attention" else "partial")
    if pending and base_state not in {"attention", "error"}:
        return replace(
            event, title="⏳ 下载与入库处理中", fields=fields, state="processing",
            footer=event.footer or "后续阶段会更新本条消息，无需重复提交。",
        )
    return replace(event, fields=fields)


def build_download_lifecycle_event(
    row,
    *,
    stats: Mapping[str, Any] | None = None,
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
    reference = str(_value(row, "local_import_target"))
    suffix = reference.removeprefix("local-media-task:")
    task_id = int(suffix) if reference.startswith("local-media-task:") and len(suffix) <= 19 and suffix.isascii() and suffix.isdigit() else 0
    task = local_media_repo.get_local_media_task(task_id) if 0 < task_id < 2**63 else None
    downloads = {_status(_value(row, key)) for key in ("qb_status", "gy_status")} - {""}
    downloads_failed = downloads == {"failed"} or (not downloads and _status(_value(row, "status")) == "failed")
    archive_name, archive_status, archive_value = _archive_stage(row, task, downloads)
    refresh_status = local_media_refresh_status(task, stats, archive_status=archive_status) if task is not None else ""
    qb_cleanup_pending = task is not None and local_media_qb_cleanup_pending(task, stats)
    state = "error" if downloads_failed else _overall_state(
        row, archive_status=archive_status, verification_status=verification_status,
        local_issue=qb_cleanup_pending or refresh_status == "failed",
    )
    strm_status = _status(_value(row, "strm_status"))
    strm_skip_reason = ""
    if strm_status == "skipped":
        strm_skip_reason = safe_download_text(_value(row, "strm_error"), limit=80, fallback="")
        if strm_skip_reason == "整理未产生需要同步的媒体变更":
            strm_skip_reason = "无变更"
    title = {
        "attention": "⚠️ 下载入库需要处理",
        "error": "❌ 下载失败" if downloads_failed else "⚠️ 下载入库部分完成",
        "completed": "✅ 下载与入库完成",
        "downloaded": "✅ 下载完成（自动入库已跳过）" if archive_status == "skipped" else "✅ 下载完成",
        "processing": "⏳ 下载与入库处理中",
        "cancelled": "⏹️ 下载跟踪已停止",
    }[state]
    if task is not None and state == "completed":
        # 本地文件归档完成不代表异步媒体库已经可见，不能把刷新入队当作入库验证。
        title = "✅ 下载与归档完成"
    elif (
        state == "completed"
        and archive_name == "光鸭整理"
        and archive_status in {"completed", "success"}
        and strm_skip_reason == "无变更"
    ):
        source_stats = _current_organize_source_stats(row)
        moved = source_stats.get("moved")
        if type(moved) is not int or moved <= 0:
            title = "✅ 下载与整理完成"
        recycled = source_stats.get("duplicate_recycled")
        if type(recycled) is int and recycled > 0:
            archive_value += f"（本轮该来源回收 {recycled} 项同版本媒体，保留已有版本）"
    media_title = notification_payload.get("title")
    if not usable_download_title(media_title):
        media_title = download_display_title(row)
    fields: list[tuple[object, object]] = [
        ("媒体", str(media_title)[:160]),
        ("下载", _download_label(row)),
        (archive_name, archive_value),
    ]
    if qb_cleanup_pending:
        fields.append(("qB 收尾", QB_CLEANUP_WARNING))
    if strm_status:
        strm_label = _label(strm_status)
        if strm_skip_reason:
            strm_label += f"（{strm_skip_reason}）"
        fields.append(("STRM", strm_label))
    previous_refresh = _previous_field(previous, "媒体库")
    local_refresh = LOCAL_REFRESH_LABELS.get(refresh_status, "")
    if refresh_status == "skipped" and strm_status and previous_refresh not in LOCAL_REFRESH_LABELS.values():
        # 本地未入队时保留同请求 STRM 已发布的刷新回执，淘汰旧本地入队文案。
        local_refresh = previous_refresh or local_refresh
    refresh_value = (media_refresh or "").strip() or local_refresh or previous_refresh
    if refresh_status == "failed":
        # 同请求另一后端的刷新成功不能掩盖本地入队失败。
        refresh_value = "本地媒体库刷新失败（需处理） ❌"
    if refresh_value:
        fields.append(("媒体库", refresh_value))
    if verification_status:
        fields.append((
            "入库复核",
            (verification_result or _label(verification_status)).strip(),
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
        footer = errors[0] if errors else "下载已完成，暂无本次新增归档的记录。"
    elif state == "processing":
        footer = "后续阶段会更新本条消息，无需重复提交。"
    event = NotificationEvent(
        title,
        fields=tuple(fields),
        footer=footer,
        layout="relaxed",
        state=state,
    )
    event = _merge_probe_progress(
        event, _load_probe_progress(row) if probe_progress is None else probe_progress,
    )
    return attach_bounded_media_details(
        event, media_blocks or (previous.lines if previous is not None else ()),
    )


def _historical_settlement(row) -> bool:
    # 复核可能晚于实际入库数月；按最后一次业务结算区分刚入库的长下载。
    settled_at = max(row[field] or "" for field in (
        "completed_at", "organize_finished_at", "strm_finished_at", "local_import_completed_at",
    )) or row["updated_at"] or row["created_at"]
    cutoff = (datetime.now() - VERIFICATION_HISTORY_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    return bool(settled_at) and settled_at < cutoff


def download_notification_obsolescence(thread_key: str, event: NotificationEvent) -> str:
    """用请求台账核对待投递的生命周期卡，包括重启前已排队的旧卡。"""
    prefix = "download:"
    if not thread_key.startswith(prefix):
        return ""
    raw_id = thread_key[len(prefix):]
    if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 19:
        return ""
    if not 0 < int(raw_id) < 2**63:
        return ""
    row = download_requests_repo.get_download_request(int(raw_id))
    # 历史纯通知未必绑定请求，不凭记录缺失推断其业务已撤销。
    if row is None:
        return ""
    if _status(_value(row, "qb_status")) == "cancelled":
        if _status(_value(row, "status")) in {"cancelled", "resubmitted"}:
            return "cancelled"
        if _previous_field(event, "下载") != _download_label(row):
            from app.repositories.download_requests import request_download_notification_refresh

            request_download_notification_refresh(int(raw_id))
            return "stale"
    if _previous_field(event, "入库复核") in _VERIFICATION_RESULT_LABELS.values() and _historical_settlement(row):
        return "edit_only"
    return ""


def publish_download_lifecycle(
    request_id: int,
    *,
    stats: Mapping[str, Any] | None = None,
    media_refresh: str = "",
    deliver_now: bool = True,
    expected_chat_id: str | None = None,
) -> NotificationPublishResult:
    thread_key = f"download:{request_id}"
    result = NotificationPublishResult(False, status="invalid")
    for attempt in range(3):
        row = download_requests_repo.get_download_request(request_id)
        if row is None:
            return NotificationPublishResult(False, status="missing_request")
        chat_id = notification_target_chat_id(_chat_id(row))
        if expected_chat_id is not None and chat_id != expected_chat_id:
            return NotificationPublishResult(False, status="InvalidRoute")
        snapshot = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.DOWNLOAD, chat_id=chat_id,
        )
        verification_status = ""
        verification_result = ""
        verification = agent_download_verification_repo.get_agent_download_verification(request_id)
        if verification is not None and verification["chat_id"] == chat_id:
            # 复用旧请求的复核不能进入另一会话的下载消息；原请求路由不变。
            # 旧通知载荷、STRM 回调和 tracker 重试都不能覆盖当前复核代际。
            verification_status = _status(verification["status"])
            if verification_status in {"visible", "attention"} and _historical_settlement(row):
                # 已排队的首卡需要更新为当前复核结果，交给投递器按原消息 ID 处理。
                if snapshot is None or (
                    not snapshot.message_id and snapshot.status not in {"pending", "retry_wait", "sending"}
                ):
                    return NotificationPublishResult(True, status="HistoricalVerification")
            verification_result = (
                _VERIFICATION_RESULT_LABELS.get(
                    _status(verification["result"]), _VERIFICATION_RESULT_LABELS["inconclusive"],
                )
                if verification_status in {"visible", "attention"}
                else {
                    "pending": "等待复核", "running": "复核中", "retry_wait": "等待复核重试",
                }.get(verification_status, _label(verification_status))
            )
        # 冲突后仅重投影当前事实，不能继续套用迟到回调的阶段提示。
        event = build_download_lifecycle_event(
            row,
            stats=stats if attempt == 0 else None,
            media_refresh=media_refresh if attempt == 0 else "",
            verification_status=verification_status,
            verification_result=verification_result,
            probe_progress=_load_probe_progress(row),
        )
        event = replace(event, lifecycle=snapshot.event.lifecycle if snapshot is not None else {})
        importance = {
            "attention": NotificationImportance.ACTION,
            "error": NotificationImportance.ERROR,
            "partial": NotificationImportance.ERROR,
        }.get(event.state, NotificationImportance.RESULT)
        topic_enabled = config.get_bool("GY_ORGANIZE_NOTIFY_ENABLED")
        # 下载异常和人工处理不应被“整理成功通知”开关吞掉；全局通知总开关仍生效。
        if importance in {NotificationImportance.ACTION, NotificationImportance.ERROR}:
            topic_enabled = True
        result = publish_notification_thread(
            thread_key,
            event,
            topic=NotificationTopic.DOWNLOAD,
            importance=importance,
            chat_id=chat_id,
            topic_enabled=topic_enabled,
            deliver_now=deliver_now,
            expected_revision=snapshot.revision if snapshot is not None else 0,
            # 同内容发布不递增 revision；业务快照也必须在原 outbox 写事务内匹配。
            expected_download_state=(dict(row), dict(verification) if verification is not None else None),
        )
        if result or result.status != "invalid":
            return result
    return result
