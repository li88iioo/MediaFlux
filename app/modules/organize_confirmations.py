"""Telegram 整理候选确认与安全重跑。

通知按钮只携带短 token；源文件快照、候选和整理规则均持久化在 SQLite。
用户确认后仍走 Organizer 的计划、冲突、日志、STRM 与媒体库刷新链路。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import asdict
from datetime import datetime, timedelta

from app import database as db
from app.clients.guangya import GuangYaClient
from app.config import get
from app.logger import get_logger
from app.modules.directory_scrape import FixedMatchScraper, ScopedGuangYaClient
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.organize import OrganizeRules, Organizer, enforce_fixed_organize_rules
from app.modules.scraper import TMDBScraper
from app.notifier import NotificationAction, NotificationEvent, edit_event, safe_int, send_event

logger = get_logger(__name__)
_CONFIRMATION_TTL_HOURS = 24
_MAX_CANDIDATES = 3
_DISPATCH_POLL_SECONDS = 1.0
_DELIVERY_LEASE_SECONDS = 120
_DELIVERY_RETRY_SECONDS = (2, 8, 30, 120, 600)
_dispatch_guard = threading.Lock()
_dispatch_stop = threading.Event()
_dispatch_wakeup = threading.Event()
_dispatch_thread: threading.Thread | None = None
_dispatch_accepting = False


class ConfirmationRetryableError(RuntimeError):
    """外部服务瞬时失败；保留同一快照并允许用户显式重试。"""


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def semantic_candidate_category(candidate: dict) -> str:
    """把 TMDB 大类与题材合并成对用户有意义的候选身份。"""
    media_type = str(candidate.get("media_type") or "").strip().lower()
    genre_ids = {
        int(value) for value in (candidate.get("genre_ids") or [])
        if str(value).isdigit()
    }
    if media_type == "movie":
        if 16 in genre_ids:
            return "电影 · 动画"
        if 99 in genre_ids:
            return "电影 · 纪录片"
        return "电影"
    if media_type == "tv":
        if 16 in genre_ids:
            return "剧集 · 动漫"
        if 99 in genre_ids:
            return "剧集 · 纪录片"
        if genre_ids.intersection({10763, 10764, 10767}):
            return "剧集 · 综艺"
        return "剧集"
    return "未知类型"


def _safe_label(candidate: dict, index: int) -> str:
    title = str(candidate.get("title") or f"候选 {index + 1}").strip()
    if len(title) > 18:
        title = f"{title[:17].rstrip()}…"
    tmdb_id = str(candidate.get("tmdb_id") or "").strip()
    suffix = f" · TMDB {tmdb_id}" if tmdb_id else ""
    return f"{index + 1}  {title}{suffix}"


def _candidate_summary_lines(group: dict) -> tuple[str, ...]:
    lines: list[str] = []
    for index, candidate in enumerate((group.get("candidates") or [])[:_MAX_CANDIDATES]):
        title = str(candidate.get("title") or f"候选 {index + 1}").strip()
        year = str(candidate.get("year") or "").strip()
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        score = max(0.0, min(float(candidate.get("score") or 0.0), 1.0))
        support = max(0, int(candidate.get("support") or 0))
        heading = f"{index + 1}. {title}" + (f" ({year})" if year else "")
        identity = f"TMDB {tmdb_id} · {semantic_candidate_category(candidate)} · 匹配 {score:.0%}"
        if support:
            identity += f" · 支持 {support} 个文件"
        lines.append(f"{heading}\n{identity}")
    return tuple(lines)


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_confirmation_actions(
    group: dict,
    rules: OrganizeRules,
    *,
    source_name: str = "",
    chat_id: str = "",
) -> tuple[NotificationAction, ...]:
    """持久化候选组并返回 Telegram 按钮；无有效候选时返回空。"""
    candidates = [
        dict(item) for item in (group.get("candidates") or [])[:_MAX_CANDIDATES]
        if str((item or {}).get("tmdb_id") or "").strip()
        and str((item or {}).get("media_type") or "") in {"movie", "tv"}
    ]
    files = [dict(item) for item in (group.get("files") or [])]
    if not candidates or not files:
        return ()
    resolved_chat = str(chat_id or get("TG_CHAT_ID", "") or "").strip()
    payload = {
        "version": 1,
        "source_dir_id": str(group.get("source_dir_id") or ""),
        "source_name": str(source_name or group.get("source_name") or ""),
        "directory": str(group.get("directory") or "/"),
        "source_parent_id": str(group.get("source_parent_id") or "0"),
        "identity": str(group.get("identity") or ""),
        "reason": str(group.get("reason") or ""),
        "files": files,
        "companions": [dict(item) for item in (group.get("companions") or [])],
        "candidates": candidates,
        "rules": asdict(rules),
    }
    token = secrets.token_urlsafe(12)
    db.create_organize_confirmation(
        token=token,
        fingerprint=_fingerprint(payload),
        chat_id=resolved_chat,
        source_name=payload["source_name"],
        directory_path=payload["directory"],
        payload=payload,
        expires_at=_timestamp(datetime.now() + timedelta(hours=_CONFIRMATION_TTL_HOURS)),
    )
    actions = [
        NotificationAction(_safe_label(candidate, index), f"orgc:{token}:{index}")
        for index, candidate in enumerate(candidates)
    ]
    actions.append(NotificationAction("暂不处理", f"orgc:{token}:cancel"))
    return tuple(actions)


def _decode_row(row) -> dict:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("确认任务数据损坏，请重新执行整理") from exc
    if not isinstance(payload, dict):
        raise ValueError("确认任务数据损坏，请重新执行整理")
    return payload


def cancel_confirmation(
    token: str, *, chat_id: str, message_id: int | str | None = None
) -> dict:
    current = db.get_organize_confirmation(token)
    if current is None:
        raise ValueError("确认操作不存在或已失效")
    directory = str(current["directory_path"] or "/")
    try:
        resolved_message_id = int(message_id or 0)
    except (TypeError, ValueError):
        resolved_message_id = 0
    terminal_event = NotificationEvent(
        "⏸️ 已暂不处理",
        fields=(("目录", directory),),
        footer="文件保持原位；需要时可重新执行整理生成新候选。",
        layout="relaxed",
    )
    db.cancel_organize_confirmation(
        token,
        chat_id=chat_id,
        event_json=_serialize_notification_event(terminal_event),
        message_id=resolved_message_id or None,
    )
    if not _deliver_persisted_confirmation_terminal(token):
        logger.warning(
            "Telegram 取消确认回执暂未送达，已进入重试队列 token=%s",
            str(token)[:6],
        )
    return {"cancelled": True, "directory": directory}


def _confirmation_result(row, payload: dict, candidate: dict, *, status: str) -> dict:
    queue_position = (
        db.get_organize_confirmation_queue_position(int(row["id"]))
        if status == "queued" else 0
    )
    return {
        "task_id": str(row["task_id"] or f"queue-{int(row['id']):06d}"),
        "candidate": candidate,
        "directory": str(
            payload.get("directory") or payload.get("source_name") or "待确认媒体"
        ),
        "file_count": len(payload.get("files") or []),
        "scope_summary": Organizer._confirmation_scope_summary(payload),
        "source_name": str(payload.get("source_name") or ""),
        "media_type": str(candidate.get("media_type") or ""),
        "status": status,
        "queue_position": queue_position,
    }


def _selected_candidate(row) -> tuple[dict, dict, int]:
    payload = _decode_row(row)
    candidates = list(payload.get("candidates") or [])
    selected_index = int(row["selected_index"] if row["selected_index"] is not None else -1)
    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("候选参数无效")
    return payload, dict(candidates[selected_index]), selected_index


def _dispatch_confirmation_token(token: str) -> dict:
    """尝试领取并启动一个排队任务；统一写锁繁忙时原样放回队列。"""
    row = db.claim_queued_organize_confirmation(token)
    if row is None:
        return {"ok": False, "claimed": False}
    try:
        payload, candidate, selected_index = _selected_candidate(row)
    except Exception as exc:
        message = "确认任务数据损坏，请重新执行整理"
        failure_event = NotificationEvent(
            "❌ Telegram 确认整理失败",
            fields=(("目录", str(row["directory_path"] or "/")),),
            footer=f"{message}生成新候选。",
            layout="relaxed",
        )
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=_serialize_notification_event(failure_event),
            chat_id=str(row["chat_id"] or ""),
            message_id=None,
            retryable=False,
        )
        if not _deliver_persisted_confirmation_terminal(token):
            logger.warning(
                "Telegram 损坏确认任务回执暂未送达，已进入重试队列 token=%s",
                str(token)[:6],
            )
        logger.warning(
            "Telegram 排队整理数据损坏 token=%s type=%s",
            str(token)[:6],
            type(exc).__name__,
        )
        return {"ok": False, "claimed": True, "terminal": True, "error": message}

    from app.modules.organize_tasks import get_organize_manager

    reference = str(
        payload.get("directory") or payload.get("source_name") or "待确认媒体"
    )
    chat_id = str(row["chat_id"] or "")
    try:
        task = get_organize_manager().start_operation(
            "Telegram 确认整理",
            reference,
            lambda: _execute_confirmation(
                token, payload, candidate,
                selected_index=selected_index, chat_id=chat_id,
            ),
        )
    except Exception as exc:
        db.requeue_organize_confirmation(token, str(exc or "整理任务提交失败"))
        logger.warning(
            "Telegram 排队整理提交异常 token=%s type=%s",
            str(token)[:6],
            type(exc).__name__,
        )
        return {"ok": False, "claimed": True, "busy": True, "error": str(exc)}

    if not task.get("ok"):
        error = str(task.get("error") or "统一整理队列暂时繁忙")
        db.requeue_organize_confirmation(token, error)
        return {"ok": False, "claimed": True, "busy": True, "error": error}

    task_id = str(row["task_id"] or task.get("task_id") or "")
    db.update_organize_confirmation(token, error="")
    return {
        "ok": True,
        "claimed": True,
        "task_id": task_id,
        "worker_task_id": str(task.get("task_id") or ""),
    }


def _dispatch_next_queued_confirmation() -> dict:
    row = db.get_next_queued_organize_confirmation()
    if row is None:
        return {"ok": False, "idle": True}
    return _dispatch_confirmation_token(str(row["token"] or ""))


def _serialize_notification_event(event: NotificationEvent) -> str:
    return json.dumps(
        {
            "title": str(event.title or ""),
            "fields": [[str(key or ""), str(value or "")] for key, value in event.fields],
            "lines": [str(line or "") for line in event.lines],
            "image_url": str(event.image_url or ""),
            "footer": str(event.footer or ""),
            "actions": [
                {"label": str(action.label or ""), "callback_data": str(action.callback_data or "")}
                for action in event.actions
            ],
            "layout": str(event.layout or "default"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_notification_event(raw: object) -> NotificationEvent:
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram 整理回执数据损坏") from exc
    if not isinstance(data, dict):
        raise ValueError("Telegram 整理回执数据损坏")
    raw_fields = data.get("fields") or []
    raw_actions = data.get("actions") or []
    fields = tuple(
        (str(item[0] or ""), str(item[1] or ""))
        for item in raw_fields
        if isinstance(item, (list, tuple)) and len(item) == 2
    )
    actions = tuple(
        NotificationAction(
            str(item.get("label") or ""),
            str(item.get("callback_data") or ""),
        )
        for item in raw_actions
        if isinstance(item, dict) and str(item.get("callback_data") or "").strip()
    )
    return NotificationEvent(
        str(data.get("title") or "Telegram 整理结果"),
        fields=fields,
        lines=tuple(str(line or "") for line in (data.get("lines") or [])),
        image_url=str(data.get("image_url") or ""),
        footer=str(data.get("footer") or ""),
        actions=actions,
        layout=str(data.get("layout") or "default"),
    )


def _delivery_timestamp(delay_seconds: int = 0) -> str:
    return _timestamp(datetime.now() + timedelta(seconds=int(delay_seconds)))


def _dispatch_due_confirmation_delivery(token: str = "") -> bool:
    current = _delivery_timestamp()
    stale_before = _delivery_timestamp(-_DELIVERY_LEASE_SECONDS)
    item = db.claim_due_organize_confirmation_delivery(
        current_time=current, stale_before=stale_before, token=token
    )
    if item is None:
        return False

    delivery_id = int(item["id"])
    generation = int(item["lease_generation"])
    try:
        event = _deserialize_notification_event(item["event_json"])
    except ValueError:
        event = NotificationEvent(
            "❌ Telegram 整理结果回执异常",
            footer="回执内容读取失败，请在日志记录中核对本次整理结果。",
            layout="relaxed",
        )
    chat_id = str(item["chat_id"] or "")
    message_id = item["message_id"]
    delivered = False
    try:
        if message_id:
            delivered = edit_event(event, chat_id=chat_id, message_id=message_id)
        if not delivered:
            delivered = send_event(event, chat_id=chat_id or None)
    except Exception as exc:
        logger.warning(
            "Telegram 整理回执投递异常 token=%s type=%s",
            str(item["confirmation_token"] or "")[:6],
            type(exc).__name__,
        )
    if delivered:
        completed = db.complete_organize_confirmation_delivery(
            delivery_id, expected_lease_generation=generation, sent_at=current
        )
        if not completed:
            logger.info(
                "Telegram 整理回执已送达，但投递租约已变化 token=%s",
                str(item["confirmation_token"] or "")[:6],
            )
        return True

    attempts = max(0, int(item["attempts"] or 0))
    delay = _DELIVERY_RETRY_SECONDS[min(attempts, len(_DELIVERY_RETRY_SECONDS) - 1)]
    db.retry_organize_confirmation_delivery(
        delivery_id,
        expected_lease_generation=generation,
        next_attempt_at=_delivery_timestamp(delay),
        error="DeliveryFailed",
    )
    return True


def _confirmation_dispatch_loop() -> None:
    while not _dispatch_stop.is_set():
        try:
            # stop() 可能在 while 条件检查后立刻触发；查询前再次确认，
            # 避免测试库/应用资源已经开始释放时仍访问 SQLite。
            if _dispatch_stop.is_set():
                break
            if _dispatch_due_confirmation_delivery():
                continue
            row = db.get_next_queued_organize_confirmation()
            if row is None:
                _dispatch_wakeup.wait(2.0)
                _dispatch_wakeup.clear()
                continue

            if _dispatch_stop.is_set():
                break
            token = str(row["token"] or "")
            result = _dispatch_confirmation_token(token)
            if result.get("ok"):
                while not _dispatch_stop.is_set():
                    current = db.get_organize_confirmation(token)
                    if current is None or str(current["status"] or "") != "running":
                        break
                    _dispatch_due_confirmation_delivery()
                    _dispatch_wakeup.wait(_DISPATCH_POLL_SECONDS)
                    _dispatch_wakeup.clear()
                continue
        except Exception as exc:
            logger.error(
                "Telegram 整理确认队列调度异常 type=%s",
                type(exc).__name__,
                exc_info=True,
            )

        _dispatch_wakeup.wait(_DISPATCH_POLL_SECONDS)
        _dispatch_wakeup.clear()


def start_confirmation_dispatcher() -> None:
    """在应用启动阶段启用持久化确认队列消费者；重复调用安全。"""
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_guard:
        _dispatch_accepting = True
        _dispatch_stop.clear()
        if _dispatch_thread is None or not _dispatch_thread.is_alive():
            _dispatch_thread = threading.Thread(
                target=_confirmation_dispatch_loop,
                name="telegram-organize-confirmations",
                daemon=True,
            )
            _dispatch_thread.start()
    _dispatch_wakeup.set()


def wake_confirmation_dispatcher() -> bool:
    """唤醒已启用的消费者；关机期间不会清除停止信号或重建线程。"""
    with _dispatch_guard:
        if not _dispatch_accepting or _dispatch_stop.is_set():
            return False
        thread = _dispatch_thread
        if thread is None or not thread.is_alive():
            return False
        _dispatch_wakeup.set()
        return True


def stop_confirmation_dispatcher(timeout: float = 2.0) -> None:
    """停止队列消费者；已持久化的 queued 项会在下次启动继续执行。"""
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_guard:
        _dispatch_accepting = False
        _dispatch_stop.set()
        _dispatch_wakeup.set()
        thread = _dispatch_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(max(0.0, float(timeout)))
    with _dispatch_guard:
        if _dispatch_thread is thread and (thread is None or not thread.is_alive()):
            _dispatch_thread = None


def start_confirmation(
    token: str,
    selected_index: int,
    *,
    chat_id: str,
) -> dict:
    """持久化用户选择；空闲时立即执行，繁忙时按 FIFO 自动排队。"""
    preview = db.get_organize_confirmation(token)
    if preview is None:
        raise ValueError("确认操作不存在或已失效")
    expected_chat = str(preview["chat_id"] or "")
    if expected_chat and expected_chat != str(chat_id or ""):
        raise ValueError("确认操作不存在或已失效")

    payload = _decode_row(preview)
    candidates = list(payload.get("candidates") or [])
    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("候选参数无效")
    candidate = dict(candidates[selected_index])
    status = str(preview["status"] or "pending")
    if status == "queued" and str(preview["expires_at"] or "") <= db.now():
        db.expire_queued_organize_confirmations()
        raise ValueError("确认操作已过期，请重新执行整理")

    if status in {"queued", "running", "completed"}:
        if int(preview["selected_index"] if preview["selected_index"] is not None else -1) != selected_index:
            raise ValueError("该媒体已选择其他候选，不能重复修改")
        return _confirmation_result(
            preview, payload, candidate, status=status
        ) | {"replayed": True}
    if status != "pending":
        raise ValueError("该确认操作已处理")

    try:
        row = db.claim_organize_confirmation(
            token, chat_id=chat_id, selected_index=selected_index
        )
    except ValueError:
        # 两个相同回调可同时读到 pending；数据库只允许一个认领成功。
        # 失败方重新读取真实状态，同候选按幂等重放处理，不误报“已处理”。
        current = db.get_organize_confirmation(token)
        if current is not None and str(current["status"] or "") in {"queued", "running", "completed"}:
            current_index = int(
                current["selected_index"]
                if current["selected_index"] is not None else -1
            )
            if current_index == selected_index:
                current_payload = _decode_row(current)
                current_candidate = dict(
                    list(current_payload.get("candidates") or [])[selected_index]
                )
                return _confirmation_result(
                    current,
                    current_payload,
                    current_candidate,
                    status=str(current["status"] or "queued"),
                ) | {"replayed": True}
            if str(current["status"] or "") in {"queued", "running"}:
                raise ValueError("该媒体已选择其他候选，不能重复修改")
        raise
    queue_id = f"queue-{int(row['id']):06d}"
    db.update_organize_confirmation(token, task_id=queue_id, error="")
    row = db.get_organize_confirmation(token)

    # 只调度队首，避免后来点击的消息绕过已经排队的确认任务。
    _dispatch_next_queued_confirmation()
    current = db.get_organize_confirmation(token) or row
    current_status = str(current["status"] or "queued")
    if current_status == "running":
        return _confirmation_result(
            current, payload, candidate, status="running"
        )

    wake_confirmation_dispatcher()
    # 后台消费者可能已在上一步与当前线程竞争成功，返回前再读取一次真实状态。
    current = db.get_organize_confirmation(token) or current
    current_status = str(current["status"] or "queued")
    return _confirmation_result(
        current,
        payload,
        candidate,
        status="running" if current_status == "running" else "queued",
    )


def _validate_snapshot(client: GuangYaClient, item: dict, *, role: str) -> None:
    file_id = str(item.get("file_id") or "").strip()
    current = client.file_info(file_id) if file_id else None
    if current is None or current.is_dir:
        raise DirectoryScrapeConflictError(f"{role}已不存在，请重新执行整理")
    mismatches = []
    if str(current.name or "") != str(item.get("name") or ""):
        mismatches.append("文件名")
    expected_parent = str(item.get("parent_id") or "")
    if expected_parent and str(current.parent_id or "") != expected_parent:
        mismatches.append("所在目录")
    if int(current.size or 0) != int(item.get("size") or 0):
        mismatches.append("文件大小")
    expected_etag = str(item.get("etag") or "")
    if expected_etag and str(current.etag or "") != expected_etag:
        mismatches.append("ETag")
    if mismatches:
        raise DirectoryScrapeConflictError(
            f"{role}在通知后发生变化（{'、'.join(mismatches)}），请重新执行整理"
        )


def _record_confirmation_learning(
    scraper: TMDBScraper,
    payload: dict,
    candidate: dict,
    match,
) -> list[str]:
    """Best-effort persist an explicit Telegram choice without affecting cloud writes."""
    warnings: list[str] = []
    selected_tmdb_id = str(candidate.get("tmdb_id") or match.tmdb_id or "").strip()
    rejected_tmdb_ids = list(dict.fromkeys(
        str(item.get("tmdb_id") or "").strip()
        for item in (payload.get("candidates") or [])
        if str(item.get("tmdb_id") or "").strip()
        and str(item.get("tmdb_id") or "").strip() != selected_tmdb_id
    ))
    parent_path = str(payload.get("directory") or "").strip()
    seen_names: set[str] = set()
    for item in payload.get("files") or []:
        raw_name = str((item or {}).get("name") or "").strip()
        if not raw_name or raw_name in seen_names:
            continue
        seen_names.add(raw_name)
        try:
            scraper.confirm(
                raw_name,
                selected_tmdb_id,
                str(match.title or candidate.get("title") or "").strip(),
                str(match.year or candidate.get("year") or "").strip(),
                str(match.media_type or candidate.get("media_type") or "").strip(),
                parent_path=parent_path,
                rejected_tmdb_ids=rejected_tmdb_ids,
            )
        except Exception as exc:
            warnings.append(f"人工确认识别知识保存失败: {raw_name}")
            logger.warning(
                "Telegram 人工确认识别知识保存失败 parent=%s type=%s",
                parent_path or "根目录",
                type(exc).__name__,
            )
    return warnings


def _confirmation_result_event(
    payload: dict, candidate: dict, stats: dict
) -> NotificationEvent:
    moved = safe_int(stats.get("moved"), 0, minimum=0)
    metadata = safe_int(stats.get("metadata_moved"), 0, minimum=0)
    skipped = safe_int(stats.get("skipped"), 0, minimum=0)
    failed = safe_int(stats.get("failed"), 0, minimum=0)
    return NotificationEvent(
        "✅ 人工确认整理完成",
        fields=(
            ("媒体", candidate.get("title") or candidate.get("tmdb_id") or "待确认媒体"),
            ("目录", payload.get("directory") or payload.get("source_name") or "/"),
            ("结果", f"已移动 {moved} · 元数据 {metadata} · 跳过 {skipped} · 失败 {failed}"),
        ),
        layout="relaxed",
    )


def _confirmation_message_id(payload: dict) -> int | None:
    try:
        message_id = int(payload.get("_telegram_message_id") or 0)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def _deliver_persisted_confirmation_terminal(token: str) -> bool:
    try:
        delivered_or_attempted = _dispatch_due_confirmation_delivery(token)
        if not delivered_or_attempted:
            wake_confirmation_dispatcher()
        delivery = db.get_organize_confirmation_delivery(token)
        return bool(
            delivery is not None and str(delivery["status"] or "") == "sent"
        )
    except Exception as exc:
        logger.warning(
            "Telegram 整理回执调度失败 token=%s type=%s",
            str(token or "")[:6],
            type(exc).__name__,
        )
        wake_confirmation_dispatcher()
        return False


def _execute_confirmation(
    token: str, payload: dict, candidate: dict, *, selected_index: int, chat_id: str
) -> dict:
    # running 状态由 claim_queued_organize_confirmation 原子授予；worker 不得
    # 无条件重新取得所有权，否则重启恢复后的 failed 终态会被迟到执行覆盖。
    try:
        stored_rules = enforce_fixed_organize_rules(
            OrganizeRules(**dict(payload.get("rules") or {}))
        )
        current_rules = OrganizeRules.from_config()
        if asdict(stored_rules) != asdict(current_rules):
            raise DirectoryScrapeConflictError("整理规则已变化，请重新执行整理后再确认")

        files = [dict(item) for item in (payload.get("files") or [])]
        companions = [dict(item) for item in (payload.get("companions") or [])]
        parent_id = str(payload.get("source_parent_id") or "0")
        if not files or any(str(item.get("parent_id") or "0") != parent_id for item in files):
            raise DirectoryScrapeConflictError("待确认文件作用域无效，请重新执行整理")

        client = GuangYaClient()
        for item in files:
            _validate_snapshot(client, item, role="待确认视频")
        for item in companions:
            _validate_snapshot(client, item, role="伴随文件")

        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        media_type = str(candidate.get("media_type") or "").strip()
        if not tmdb_id or media_type not in {"movie", "tv"}:
            raise ValueError("候选媒体参数无效")
        scraper = TMDBScraper()
        try:
            detail = scraper.get_detail_with_credits(tmdb_id, media_type)
            match = scraper.match_from_tmdb(tmdb_id, media_type)
        except Exception as exc:
            raise ConfirmationRetryableError(
                "TMDB 服务暂时不可用，请稍后重试"
            ) from exc
        if not detail or not match.tmdb_id or match.need_confirm:
            raise ConfirmationRetryableError(
                "TMDB 候选暂时无法确认，请稍后重试"
            )
        match.locked = True
        match.need_confirm = False
        match.matched_by = "telegram_confirmation"

        position_overrides = {
            str(item.get("name") or ""): (item.get("season"), item.get("episode"))
            for item in files
        }
        allowed_ids = {
            str(item.get("file_id") or "") for item in (*files, *companions)
            if str(item.get("file_id") or "")
        }
        scoped = ScopedGuangYaClient(client, parent_id, allowed_ids)
        organizer = Organizer(
            client=scoped,
            scraper=FixedMatchScraper(
                scraper,
                match,
                detail,
                preserve_specials=True,
                position_overrides=position_overrides,
            ),
        )
        organizer._validate_target_outside_source(parent_id, current_rules.target_dir_id)
        scoped.begin_source_scan()
        plans, _preview_stats = organizer.organize(
            parent_id,
            current_rules,
            dry_run=True,
            post_actions=False,
            source_name=str(payload.get("directory") or payload.get("source_name") or ""),
            require_complete_scan=True,
            # 预览在线探测并预热缓存，保证随后只读缓存的执行阶段命名一致。
            media_probe_cache_only=False,
        )
        planned_ids = {str(plan.file_id) for plan in plans}
        expected_ids = {str(item.get("file_id") or "") for item in files}
        if planned_ids != expected_ids:
            raise DirectoryScrapeConflictError("待确认文件集合已变化，请重新执行整理")

        scoped.begin_source_scan()
        _plans, stats = organizer.organize(
            parent_id,
            current_rules,
            dry_run=False,
            post_actions=False,
            source_name=str(payload.get("directory") or payload.get("source_name") or ""),
            require_complete_scan=True,
            # 执行阶段只读缓存，保持与确认预览一致；缓存由预览阶段预热。
            media_probe_cache_only=True,
        )
        learning_warnings = _record_confirmation_learning(
            scraper, payload, candidate, match
        )
        if learning_warnings:
            stats.setdefault("warnings", []).extend(learning_warnings)
        scope_name = str(payload.get("directory") or payload.get("source_name") or "TG 人工确认")
        Organizer.notify_directory_results(
            stats, current_rules, source_name=scope_name, chat_id=chat_id
        )
        Organizer.trigger_post_actions(
            stats, current_rules, source_name=scope_name, chat_id=chat_id
        )
        terminal_event = _confirmation_result_event(payload, candidate, stats)
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json=json.dumps(stats, ensure_ascii=False, default=str),
            event_json=_serialize_notification_event(terminal_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
        )
        if not _deliver_persisted_confirmation_terminal(token):
            logger.warning(
                "Telegram 确认整理完成回执暂未送达，已进入重试队列 token=%s", token[:6]
            )
        return {"candidate": candidate, "stats": stats}
    except Exception as exc:
        message = str(exc or "Telegram 确认整理失败").strip() or "Telegram 确认整理失败"
        retryable = not isinstance(exc, (DirectoryScrapeConflictError, ValueError))
        actions: tuple[NotificationAction, ...] = ()
        if retryable:
            actions = (NotificationAction(
                f"重新尝试 · {_safe_label(candidate, selected_index)}",
                f"orgc:{token}:{selected_index}",
            ),)
        logger.warning(
            "Telegram 确认整理失败 token=%s type=%s retryable=%s",
            token[:6],
            type(exc).__name__,
            retryable,
        )
        failure_event = NotificationEvent(
            "❌ Telegram 确认整理失败",
            fields=(
                ("目录", payload.get("directory") or "/"),
                ("候选", candidate.get("title") or candidate.get("tmdb_id") or ""),
            ),
            footer=(message + (
                "\n\n可点击下方按钮重试。"
                if retryable else "\n\n请重新执行整理生成新候选。"
            )),
            actions=actions,
            layout="relaxed",
        )
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=_serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            retryable=retryable,
        )
        if not _deliver_persisted_confirmation_terminal(token):
            logger.warning(
                "Telegram 确认整理失败回执暂未送达，已进入重试队列 token=%s", token[:6]
            )
        raise


def confirmation_event(
    title: str,
    fields: dict,
    group: dict,
    rules: OrganizeRules,
    *,
    source_name: str,
    chat_id: str,
) -> NotificationEvent:
    actions = create_confirmation_actions(
        group, rules, source_name=source_name, chat_id=chat_id
    )
    reason = str(group.get("reason") or "匹配结果需要人工确认").strip()
    footer = f"{reason}\n\n请选择下方候选继续整理。"
    return NotificationEvent(
        title=title,
        fields=tuple(fields.items()),
        lines=_candidate_summary_lines(group),
        footer=footer,
        actions=actions,
        layout="relaxed",
    )
