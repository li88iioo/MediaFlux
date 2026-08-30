"""Telegram 整理候选确认与安全重跑。

通知按钮只携带短 token；源文件快照、候选和整理规则均持久化在 SQLite。
用户确认后仍走 Organizer 的计划、冲突、日志、STRM 与媒体库刷新链路。
"""
from __future__ import annotations

import hashlib
import json
import re
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
from app.modules.nsfw import (
    MetaTubeError, NsfwRecognizer, build_clean_title_candidate,
    extract_nsfw_identifier, normalize_code,
)
from app.modules.organize import (
    OrganizeRules,
    Organizer,
    enforce_fixed_organize_rules,
    organize_rules_snapshot,
    organize_rules_snapshot_matches,
    restore_organize_rules_snapshot,
)
from app.modules.scraper import MatchResult, TMDBScraper
from app.notifier import (
    NOTIFICATION_SECTION_BREAK,
    NotificationAction,
    NotificationEvent,
    safe_int,
)

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


def _candidate_provider(candidate: dict) -> str:
    provider = str(candidate.get("provider") or "").strip().lower()
    if not provider and str(candidate.get("tmdb_id") or "").strip():
        provider = "tmdb"
    return provider


def _candidate_external_id(candidate: dict) -> str:
    return str(
        candidate.get("external_id") or candidate.get("tmdb_id") or ""
    ).strip()


def _valid_confirmation_candidate(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    media_type = str(candidate.get("media_type") or "").strip().lower()
    if provider == "tmdb":
        return bool(str(candidate.get("tmdb_id") or "").strip() and media_type in {"movie", "tv"})
    if provider in {"metatube", "clean_title"}:
        return bool(external_id and media_type == "movie")
    return False


def semantic_candidate_category(candidate: dict) -> str:
    """把 provider、媒体大类与题材合并成用户可理解的候选身份。"""
    if _candidate_provider(candidate) in {"metatube", "clean_title"}:
        return "成人内容"
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


def _candidate_identity_label(candidate: dict) -> str:
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    if provider == "metatube":
        return f"MetaTube {external_id}" if external_id else "MetaTube"
    if provider == "clean_title":
        return f"清洗标题 {external_id}" if external_id else "清洗标题"
    if provider == "tmdb":
        return f"TMDB {external_id}" if external_id else "TMDB"
    return external_id or "未知来源"


def _candidate_display_name(candidate: dict, fallback: str = "待确认媒体") -> str:
    return str(
        candidate.get("title") or _candidate_external_id(candidate) or fallback
    ).strip()


def _safe_label(candidate: dict, index: int) -> str:
    if _candidate_provider(candidate) == "clean_title":
        code = _candidate_external_id(candidate)
        return "清洗标题后入库" + (f" · {code}" if code else "")
    title = _candidate_display_name(candidate, f"候选 {index + 1}")
    if len(title) > 18:
        title = f"{title[:17].rstrip()}…"
    return f"{index + 1}  {title} · {_candidate_identity_label(candidate)}"


def _candidate_summary_lines(group: dict) -> tuple[str, ...]:
    lines: list[str] = []
    for index, candidate in enumerate((group.get("candidates") or [])[:_MAX_CANDIDATES]):
        title = _candidate_display_name(candidate, f"候选 {index + 1}")
        year = str(candidate.get("year") or "").strip()
        score = max(0.0, min(float(candidate.get("score") or 0.0), 1.0))
        support = max(0, int(candidate.get("support") or 0))
        heading = f"{index + 1}. {title}" + (f" ({year})" if year else "")
        identity = (
            f"{_candidate_identity_label(candidate)} · "
            f"{semantic_candidate_category(candidate)} · 匹配 {score:.0%}"
        )
        if support:
            identity += f" · 支持 {support} 个文件"
        lines.append(f"{heading}\n{identity}")
    return tuple(lines)

def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _confirmation_kind(payload: dict) -> str:
    """旧记录没有 kind；缺省值必须永久保持为光鸭整理。"""
    kind = str(payload.get("kind") or "guangya").strip().lower()
    if kind not in {"guangya", "local_media"}:
        raise ValueError("确认任务类型无效，请重新执行整理")
    return kind


def _persist_confirmation_actions(
    payload: dict, *, chat_id: str = ""
) -> tuple[NotificationAction, ...]:
    candidates = [dict(item) for item in (payload.get("candidates") or [])]
    files = list(payload.get("files") or [])
    allow_skip_terminal = bool(payload.get("allow_skip_terminal"))
    if not files or (not candidates and not allow_skip_terminal):
        return ()
    resolved_chat = str(chat_id or get("TG_CHAT_ID", "") or "").strip()
    token = secrets.token_urlsafe(12)
    db.create_organize_confirmation(
        token=token,
        fingerprint=_fingerprint(payload),
        chat_id=resolved_chat,
        source_name=str(payload.get("source_name") or ""),
        directory_path=str(payload.get("directory") or "/"),
        payload=payload,
        expires_at=_timestamp(datetime.now() + timedelta(hours=_CONFIRMATION_TTL_HOURS)),
    )
    actions = [
        NotificationAction(_safe_label(candidate, index), f"orgc:{token}:{index}")
        for index, candidate in enumerate(candidates)
    ]
    if allow_skip_terminal:
        actions.append(NotificationAction("跳过此组", f"orgc:{token}:skip"))
    else:
        actions.append(NotificationAction("暂不处理", f"orgc:{token}:cancel"))
    return tuple(actions)




def confirmation_token_from_event(event: NotificationEvent) -> str:
    """从任一人工确认按钮提取持久化 token。"""
    for action in event.actions:
        parts = str(action.callback_data or "").split(":", 2)
        if len(parts) == 3 and parts[0] == "orgc" and parts[1]:
            return parts[1]
    return ""


def publish_confirmation_event(
    event: NotificationEvent,
    *,
    chat_id: str = "",
    token: str = "",
    message_id: int | None = None,
    terminal: bool = False,
    error: bool = False,
) -> bool:
    """把候选卡及其终态写入同一个可靠 Telegram 消息线程。"""
    from app.modules.telegram_notification_center import publish_notification_thread
    from app.modules.telegram_notification_policy import (
        NotificationImportance, NotificationTopic,
    )

    resolved_token = str(token or confirmation_token_from_event(event)).strip()
    if not resolved_token:
        return False
    importance = (
        NotificationImportance.ERROR if error else
        NotificationImportance.RESULT if terminal else
        NotificationImportance.ACTION
    )
    result = publish_notification_thread(
        f"confirmation:{resolved_token}",
        event,
        topic=NotificationTopic.CONFIRMATION,
        importance=importance,
        chat_id=chat_id,
        preferred_message_id=int(message_id or 0),
    )
    return bool(result)


def _terminal_status_label(value: object, *, media_library: bool = False) -> str:
    """为确认卡的后续状态补充稳定、不过度重复的终态提示。"""
    text = str(value or "").strip()
    if not text or any(marker in text for marker in ("✅", "❌", "⚠️", "⏳", "⏭️", "🎯")):
        return text
    if any(marker in text for marker in ("失败", "错误", "未完成")):
        return f"{text} ❌"
    if any(marker in text for marker in ("部分", "警告", "需处理")):
        return f"{text} ⚠️"
    if any(marker in text for marker in ("排队", "等待", "运行中", "同步中")):
        return f"{text} ⏳"
    if "跳过" in text:
        return f"{text} ⏭️"
    if any(marker in text for marker in ("完成", "成功", "已刷新")):
        return f"{text} {'🎯' if media_library else '✅'}"
    return text


def update_confirmation_lifecycle_downstream(
    token: str,
    *,
    chat_id: str = "",
    strm_status: str,
    media_refresh: str,
    partial: bool = False,
    error: str = "",
) -> bool:
    """在人工确认卡的同一消息上补齐 STRM 与媒体库终态。"""
    from app.modules.telegram_notification_center import get_notification_thread_event
    from app.modules.telegram_notification_policy import NotificationTopic

    thread_key = f"confirmation:{str(token or '').strip()}"
    previous = get_notification_thread_event(
        thread_key, topic=NotificationTopic.CONFIRMATION, chat_id=chat_id,
    )
    if previous is None:
        return False
    fields = list(previous.fields)
    updates = (
        (("STRM 状态", "STRM"), "STRM 状态", _terminal_status_label(strm_status)),
        (("媒体库刷新", "媒体库"), "媒体库刷新", _terminal_status_label(
            media_refresh, media_library=True,
        )),
    )
    for aliases, label, value in updates:
        replaced = False
        for index, (current_label, _current_value) in enumerate(fields):
            if str(current_label) in aliases:
                fields[index] = (label, value)
                replaced = True
                break
        if not replaced:
            fields.append((label, value))
    row = db.get_organize_confirmation(token)
    keep_actions = bool(row is not None and str(row["status"] or "") == "pending")
    title = "⚠️ 人工确认整理链路部分完成" if partial or error else previous.title
    footer = str(error or previous.footer)[:300]
    event = NotificationEvent(
        title, fields=tuple(fields), lines=previous.lines, footer=footer,
        actions=previous.actions if keep_actions else (),
        layout=previous.layout, field_emojis=previous.field_emojis,
    )
    return publish_confirmation_event(
        event, chat_id=chat_id, token=token, terminal=True,
        error=bool(partial or error),
    )


def create_confirmation_actions(
    group: dict,
    rules: OrganizeRules,
    *,
    source_name: str = "",
    chat_id: str = "",
) -> tuple[NotificationAction, ...]:
    """持久化候选组；无元数据时仍返回可终结待确认状态的跳过按钮。"""
    candidates = [
        dict(item) for item in (group.get("candidates") or [])[:_MAX_CANDIDATES]
        if _valid_confirmation_candidate(item)
    ]
    files = [dict(item) for item in (group.get("files") or [])]
    if not files:
        return ()
    group_rules = group.get("rules")
    if isinstance(group_rules, dict):
        effective_rules = restore_organize_rules_snapshot(
            group_rules, trusted_rules=rules,
        )
    else:
        effective_rules = enforce_fixed_organize_rules(OrganizeRules(**asdict(rules)))
    payload = {
        "version": 2,
        "allow_skip_terminal": True,
        "source_dir_id": str(group.get("source_dir_id") or ""),
        "source_name": str(source_name or group.get("source_name") or ""),
        "directory": str(group.get("directory") or "/"),
        "source_parent_id": str(group.get("source_parent_id") or "0"),
        "identity": str(group.get("identity") or ""),
        "reason": str(group.get("reason") or ""),
        "multipart_strategy": str(group.get("multipart_strategy") or ""),
        "files": files,
        "companions": [dict(item) for item in (group.get("companions") or [])],
        "candidates": candidates,
        "rules": organize_rules_snapshot(effective_rules),
    }
    return _persist_confirmation_actions(payload, chat_id=chat_id)


def create_local_media_confirmation_actions(
    task,
    source,
    preview: dict,
    *,
    owner: str = "admin",
    chat_id: str = "",
) -> tuple[NotificationAction, ...]:
    """为本地待确认任务生成与光鸭相同协议的 TG 候选按钮。"""
    if task is None or source is None or str(getattr(task, "status", "")) != "requires_manual":
        return ()
    reason = str(preview.get("reason") or getattr(task, "error", "") or "").strip()
    raw_candidates = list(preview.get("candidates") or [])
    if not raw_candidates and isinstance(preview.get("candidate"), dict):
        raw_candidates = [preview["candidate"]]
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        media_type = str(candidate.get("media_type") or "").strip().lower()
        provider = str(candidate.get("provider") or "tmdb").strip().lower()
        key = (tmdb_id, media_type)
        if (
            not tmdb_id
            or media_type not in {"movie", "tv"}
            or provider != "tmdb"
            or key in seen
        ):
            continue
        seen.add(key)
        candidate["tmdb_id"] = tmdb_id
        candidate["media_type"] = media_type
        try:
            candidate["score"] = float(
                candidate.get("score", candidate.get("confidence", 0.0)) or 0.0
            )
        except (TypeError, ValueError):
            candidate["score"] = 0.0
        candidates.append(candidate)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    if not candidates:
        return ()
    if (
        candidates[0].get("media_type") == "tv"
        and "缺少集数" in reason
        and getattr(task, "episode_override", None) is None
    ):
        return ()

    def safe_name(value: object) -> str:
        text = str(value or "").strip().replace("\\", "/")
        return text.rsplit("/", 1)[-1] if text else ""

    files = []
    for item in list(preview.get("files") or []):
        if not isinstance(item, dict):
            continue
        name = safe_name(item.get("name"))
        if name:
            file_item = {"name": name}
            if getattr(task, "season_override", None) is not None:
                file_item["season"] = task.season_override
            if getattr(task, "episode_override", None) is not None:
                file_item["episode"] = task.episode_override
            files.append(file_item)
    if not files:
        name = safe_name(getattr(task, "content_path", ""))
        if name:
            file_item = {"name": name}
            if getattr(task, "season_override", None) is not None:
                file_item["season"] = task.season_override
            if getattr(task, "episode_override", None) is not None:
                file_item["episode"] = task.episode_override
            files.append(file_item)
    if not files:
        return ()
    expected_digest = str(
        preview.get("snapshot_digest") or getattr(task, "snapshot_digest", "") or ""
    ).strip()
    rules_snapshot = str(
        preview.get("rules_snapshot") or getattr(task, "rules_snapshot", "") or ""
    ).strip()
    if not expected_digest or not rules_snapshot:
        return ()
    payload = {
        "version": 1,
        "kind": "local_media",
        "owner": str(owner or "admin"),
        "local_task_id": int(task.id),
        "local_task_version": int(task.version),
        "local_source_id": int(task.source_id),
        "source_name": str(getattr(source, "name", "") or "本地媒体"),
        "directory": safe_name(getattr(task, "content_path", "")) or "本地媒体",
        "reason": reason,
        "files": files,
        "candidates": candidates,
        "rules_snapshot": rules_snapshot,
        "snapshot_digest": expected_digest,
        "season_override": getattr(task, "season_override", None),
        "episode_override": getattr(task, "episode_override", None),
        "numbering_mode": str(getattr(task, "numbering_mode", "auto") or "auto"),
    }
    return _persist_confirmation_actions(payload, chat_id=chat_id)


def _decode_row(row) -> dict:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("确认任务数据损坏，请重新执行整理") from exc
    if not isinstance(payload, dict):
        raise ValueError("确认任务数据损坏，请重新执行整理")
    return payload


def _finalize_guangya_manual_logs(
    payload: dict, *, status: str, error: str,
) -> None:
    """同步确认终态到光鸭整理时间线；本地媒体使用自身任务状态机。"""
    try:
        if _confirmation_kind(payload) != "guangya":
            return
        file_ids = [
            str(item.get("file_id") or "").strip()
            for item in (payload.get("files") or [])
            if isinstance(item, dict) and str(item.get("file_id") or "").strip()
        ]
        if file_ids:
            db.finalize_pending_organize_logs(
                "guangya", file_ids, status=status, error=error,
            )
    except Exception as exc:
        # 确认操作的终态已经持久化，日志投影同步失败只能降级告警，
        # 不能让用户收到“取消/失败处理失败”的错误回执。
        logger.warning(
            "同步人工确认日志终态失败 status=%s type=%s",
            status,
            type(exc).__name__,
        )


def cancel_confirmation(
    token: str, *, chat_id: str, message_id: int | str | None = None
) -> dict:
    current = db.get_organize_confirmation(token)
    if current is None:
        raise ValueError("确认操作不存在或已失效")
    payload = _decode_row(current)
    directory = str(current["directory_path"] or "/")
    try:
        resolved_message_id = int(message_id or 0)
    except (TypeError, ValueError):
        resolved_message_id = 0
    terminal_event = NotificationEvent(
        "⏸️ 已暂不处理",
        fields=(
            ("所在目录", directory),
            NOTIFICATION_SECTION_BREAK,
            ("处理状态", "文件保持原位（本次待确认状态已结束）"),
            ("附带说明", "需要时可重新执行整理生成新候选。"),
        ),
        layout="relaxed",
    )
    db.cancel_organize_confirmation(
        token,
        chat_id=chat_id,
        event_json=_serialize_notification_event(terminal_event),
        message_id=resolved_message_id or None,
        enqueue_delivery=False,
    )
    publish_confirmation_event(
        terminal_event, chat_id=chat_id, token=token,
        message_id=resolved_message_id or None, terminal=True,
    )
    _finalize_guangya_manual_logs(
        payload, status="skipped", error="用户选择暂不处理",
    )
    return {"cancelled": True, "directory": directory}


def skip_confirmation(
    token: str, *, chat_id: str, message_id: int | str | None = None
) -> dict:
    """显式跳过无可用元数据的光鸭待确认组，并同步结束日志状态。"""
    current = db.get_organize_confirmation(token)
    if current is None:
        raise ValueError("确认操作不存在或已失效")
    payload = _decode_row(current)
    if _confirmation_kind(payload) != "guangya" or not bool(
        payload.get("allow_skip_terminal")
    ):
        raise ValueError("该确认操作不支持跳过")
    directory = str(current["directory_path"] or "/")
    try:
        resolved_message_id = int(message_id or 0)
    except (TypeError, ValueError):
        resolved_message_id = 0
    terminal_event = NotificationEvent(
        "⏭️ 跳过待确认项",
        fields=(
            ("目标媒体", payload.get("identity") or "未识别媒体"),
            ("所在目录", directory),
            NOTIFICATION_SECTION_BREAK,
            ("涉及文件", f"{len(payload.get('files') or [])} 个视频"),
            ("处理状态", "文件保持原位（本次待确认状态已结束）"),
            ("附带说明", "以后重新执行整理时仍会再次尝试识别。"),
        ),
        layout="relaxed",
    )
    db.cancel_organize_confirmation(
        token,
        chat_id=chat_id,
        event_json=_serialize_notification_event(terminal_event),
        message_id=resolved_message_id or None,
        enqueue_delivery=False,
    )
    reason = "用户选择跳过：暂无可用元数据"
    _finalize_guangya_manual_logs(payload, status="skipped", error=reason)
    if not publish_confirmation_event(
        terminal_event,
        chat_id=chat_id,
        token=token,
        message_id=resolved_message_id or None,
        terminal=True,
    ):
        logger.warning(
            "Telegram 跳过确认终态未被统一通知中心接纳 token=%s",
            str(token)[:6],
        )
    return {"skipped": True, "directory": directory}


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
            fields=(
                ("所在目录", str(row["directory_path"] or "/")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer="请重新执行整理生成新候选。",
            layout="relaxed",
        )
        chat_id = str(row["chat_id"] or "")
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=_serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=None,
            retryable=False,
            enqueue_delivery=False,
        )
        if not publish_confirmation_event(
            failure_event,
            chat_id=chat_id,
            token=token,
            terminal=True,
            error=True,
        ):
            logger.warning(
                "Telegram 损坏确认任务回执未被统一通知中心接纳 token=%s",
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
            "field_emojis": bool(event.field_emojis),
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
        field_emojis=bool(data.get("field_emojis", True)),
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
    token = str(item["confirmation_token"] or "").strip()
    try:
        # 兼容升级前已写入的旧回执：这里只负责一次性交给统一通知中心，
        # 后续发送、编辑、重试与 message_id 维护全部由统一 outbox 接管。
        accepted = publish_confirmation_event(
            event,
            chat_id=chat_id,
            token=token,
            message_id=item["message_id"],
            terminal=True,
            error=event.title.startswith(("❌", "⚠️")),
        )
    except Exception as exc:
        accepted = False
        logger.warning(
            "Telegram 旧整理回执迁移失败 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
    if accepted:
        completed = db.complete_organize_confirmation_delivery(
            delivery_id, expected_lease_generation=generation, sent_at=current
        )
        if not completed:
            logger.info(
                "Telegram 旧整理回执已移交，但投递租约已变化 token=%s",
                token[:6],
            )
        return True

    attempts = max(0, int(item["attempts"] or 0))
    delay = _DELIVERY_RETRY_SECONDS[min(attempts, len(_DELIVERY_RETRY_SECONDS) - 1)]
    db.retry_organize_confirmation_delivery(
        delivery_id,
        expected_lease_generation=generation,
        next_attempt_at=_delivery_timestamp(delay),
        error="UnifiedNotificationHandoffFailed",
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


def stop_confirmation_dispatcher(timeout: float = 2.0) -> bool:
    """停止队列消费者；返回是否已退出，queued 项留待下次启动。"""
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_guard:
        _dispatch_accepting = False
        _dispatch_stop.set()
        _dispatch_wakeup.set()
        thread = _dispatch_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(max(0.0, float(timeout)))
    stopped = thread is None or not thread.is_alive()
    with _dispatch_guard:
        if _dispatch_thread is thread and stopped:
            _dispatch_thread = None
    return stopped


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


def _validate_metatube_confirmation_identity(
    payload: dict, detail: dict, rules: OrganizeRules,
) -> None:
    source_codes: set[str] = set()
    source_values = [str(payload.get("directory") or "")]
    source_values.extend(
        str(item.get("name") or "")
        for item in (payload.get("files") or [])
        if isinstance(item, dict)
    )
    for value in source_values:
        identifier = extract_nsfw_identifier(value, rules.nsfw_strip_domains)
        if identifier is not None:
            source_codes.add(normalize_code(identifier.code))
    resolved_code = normalize_code(str(detail.get("number") or ""))
    if not source_codes:
        raise ValueError("待确认文件未提取到可校验番号，不能套用 MetaTube 元数据")
    if not resolved_code or resolved_code not in source_codes:
        raise ValueError("MetaTube 候选番号与待确认文件不一致")


def _resolve_guangya_confirmation_candidate(
    payload: dict, candidate: dict, rules: OrganizeRules,
) -> tuple[TMDBScraper, object, dict, str]:
    provider = _candidate_provider(candidate)
    external_id = _candidate_external_id(candidate)
    media_type = str(candidate.get("media_type") or "").strip().lower()
    scraper = TMDBScraper()
    if provider == "tmdb":
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        if not tmdb_id or media_type not in {"movie", "tv"}:
            raise ValueError("TMDB 候选媒体参数无效")
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
    elif provider == "metatube":
        if media_type != "movie" or not external_id:
            raise ValueError("MetaTube 候选媒体参数无效")
        if not rules.nsfw_exclusive:
            raise ValueError("当前来源不是成人专用来源，已拒绝 MetaTube 候选")
        if not str(rules.nsfw_metatube_endpoint or "").strip():
            raise ValueError("MetaTube 服务地址未配置")
        try:
            recognizer = NsfwRecognizer(
                rules.nsfw_metatube_endpoint,
                rules.nsfw_metatube_token,
                strip_domains=rules.nsfw_strip_domains,
                timeout=rules.nsfw_timeout_seconds,
            )
            match, detail = recognizer.resolve(external_id)
        except MetaTubeError as exc:
            raise ConfirmationRetryableError(
                "MetaTube 服务暂时不可用，请稍后重试"
            ) from exc
        _validate_metatube_confirmation_identity(payload, detail, rules)
    elif provider == "clean_title":
        if media_type != "movie" or not external_id:
            raise ValueError("清洗标题候选参数无效")
        if not rules.nsfw_exclusive:
            raise ValueError("当前来源不是成人专用来源，已拒绝清洗标题入库")
        seed = next((
            str(item.get("name") or "")
            for item in (payload.get("files") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ), str(payload.get("directory") or ""))
        fallback = build_clean_title_candidate(seed, rules.nsfw_strip_domains)
        if fallback is None:
            raise ValueError("待确认文件未提取到有效番号，不能清洗标题入库")
        resolved_id = str(fallback.get("external_id") or "").strip()
        if normalize_code(external_id) != normalize_code(resolved_id):
            raise ValueError("清洗标题候选番号与待确认文件不一致")
        # 标题由服务端根据原始文件重新生成，不能信任回传候选中的可修改文本。
        title = str(fallback.get("title") or resolved_id).strip()
        detail = {
            **dict(fallback.get("metadata") or {}),
            "number": resolved_id,
            "title": title,
            "fallback": True,
        }
        match = MatchResult(
            title=title,
            media_type="movie",
            confidence=1.0,
            provider="clean_title",
            external_id=resolved_id,
            metadata=detail,
            status="matched",
        )
    else:
        raise ValueError("候选媒体来源无效")
    match.locked = True
    match.need_confirm = False
    match.matched_by = "telegram_confirmation"
    return scraper, match, detail, provider


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
    warnings = len(list(stats.get("warnings") or []))
    strm = stats.get("strm") if isinstance(stats.get("strm"), dict) else {}
    if strm.get("ok"):
        strm_label, refresh_label = "已排队", "等待 STRM 完成"
    elif strm.get("skipped"):
        strm_label, refresh_label = "已跳过", "未触发"
    elif strm:
        strm_label, refresh_label = "启动失败", "未触发"
    else:
        strm_label, refresh_label = "未启用或无变更", "未触发"
    partial = bool(failed or warnings or (strm and not strm.get("ok") and not strm.get("skipped")))
    return NotificationEvent(
        "⚠️ 人工确认整理部分完成" if partial else "✅ 人工确认整理完成",
        fields=(
            ("目标媒体", _candidate_display_name(candidate)),
            ("源文件目录", payload.get("directory") or payload.get("source_name") or "/"),
            NOTIFICATION_SECTION_BREAK,
            ("执行结果", f"已移动 {moved} · 元数据 {metadata} · 跳过 {skipped} · 失败 {failed}"),
            ("STRM 状态", _terminal_status_label(strm_label)),
            ("媒体库刷新", _terminal_status_label(
                refresh_label, media_library=True,
            )),
        ),
        layout="relaxed",
    )


def _confirmation_message_id(payload: dict) -> int | None:
    try:
        message_id = int(payload.get("_telegram_message_id") or 0)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def _local_confirmation_result_event(
    payload: dict, candidate: dict, result: dict
) -> NotificationEvent:
    moved = len(list(result.get("moved") or []))
    deleted = len(list(result.get("deleted_junk") or []))
    warnings = len(list(result.get("warnings") or []))
    refresh_status = str(result.get("media_refresh_status") or "")
    partial = bool(warnings or refresh_status == "failed")
    return NotificationEvent(
        "⚠️ 本地媒体确认整理部分完成" if partial else "✅ 本地媒体确认整理完成",
        fields=(
            ("目标媒体", _candidate_display_name(candidate)),
            ("存储来源", payload.get("source_name") or "本地媒体"),
            NOTIFICATION_SECTION_BREAK,
            ("执行结果", f"已移动 {moved} · 清理 {deleted} · 警告 {warnings}"),
            ("媒体库刷新", _terminal_status_label({
                "completed": "已刷新", "queued": "已排队",
                "failed": "刷新失败", "skipped": "未启用",
            }.get(refresh_status, "已处理"), media_library=True)),
        ),
        layout="relaxed",
    )


def _execute_local_media_confirmation(
    token: str, payload: dict, candidate: dict, *, selected_index: int, chat_id: str
) -> dict:
    claimed_task = False
    task_id = safe_int(payload.get("local_task_id"), 0, minimum=0)
    try:
        if task_id <= 0:
            raise ValueError("本地媒体确认任务无效，请前往 Web 重新处理")
        owner = str(payload.get("owner") or "admin").strip() or "admin"
        expected_version = safe_int(payload.get("local_task_version"), 0, minimum=0)
        expected_source_id = safe_int(payload.get("local_source_id"), 0, minimum=0)
        expected_digest = str(payload.get("snapshot_digest") or "").strip()
        rules_snapshot = str(payload.get("rules_snapshot") or "").strip()
        tmdb_id = str(candidate.get("tmdb_id") or "").strip()
        media_type = str(candidate.get("media_type") or "").strip().lower()
        if not tmdb_id or media_type not in {"movie", "tv"}:
            raise ValueError("候选媒体参数无效")
        if expected_version <= 0 or expected_source_id <= 0 or not expected_digest or not rules_snapshot:
            raise ValueError("本地媒体确认快照无效，请前往 Web 重新处理")

        task = db.get_local_media_task(task_id, owner=owner)
        if task is None or task.status != "requires_manual":
            raise ValueError("本地媒体任务已变化，请前往 Web 查看最新状态")
        if task.version != expected_version or task.source_id != expected_source_id:
            raise ValueError("本地媒体任务已更新，请前往 Web 重新确认")
        source = db.get_local_media_source(task.source_id, owner=owner)
        if source is None:
            raise ValueError("本地媒体来源已删除，请前往 Web 重新配置")

        from app.modules.local_media_scheduler import get_local_media_scheduler

        scheduler = get_local_media_scheduler()
        inspection = scheduler.service.inspect_source(owner, task.source_id, task.content_path)
        if str(inspection.get("digest") or "") != expected_digest:
            raise ValueError("源文件在通知后发生变化，请前往 Web 重新检查")
        if not db.claim_local_media_confirmation_task(
            task_id,
            owner=owner,
            expected_version=expected_version,
            expected_snapshot_digest=str(task.snapshot_digest or ""),
            tmdb_id=tmdb_id,
            media_type=media_type,
            rules_snapshot=rules_snapshot,
            season_override=payload.get("season_override"),
            episode_override=payload.get("episode_override"),
            numbering_mode=str(payload.get("numbering_mode") or "auto"),
            title=str(candidate.get("title") or ""),
            year=str(candidate.get("year") or ""),
        ):
            raise ValueError("本地媒体任务已被其他操作认领，请前往 Web 查看最新状态")
        claimed_task = True
        current = db.get_local_media_task(task_id, owner=owner)
        if current is None:
            raise ValueError("本地媒体任务不存在")
        qb_client = scheduler.qb_factory() if current.qb_hash else None
        result = scheduler.service.execute_task(owner, task_id, qb_client=qb_client)
        if str(result.get("status") or "") != "completed":
            reason = str(
                (result.get("preview") or {}).get("reason")
                or "本地媒体仍需补充季集等信息"
            )
            raise ValueError(f"{reason}；请前往 Web 继续处理")
        try:
            db.update_download_request_for_local_media_task(
                task_id, "completed", resolve_manual=True
            )
        except Exception as exc:
            logger.warning(
                "本地媒体确认完成状态回写失败 task=%s type=%s",
                task_id,
                type(exc).__name__,
            )
        terminal_event = _local_confirmation_result_event(payload, candidate, result)
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            event_json=_serialize_notification_event(terminal_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            enqueue_delivery=False,
        )
        publish_confirmation_event(
            terminal_event, chat_id=chat_id, token=token,
            message_id=_confirmation_message_id(payload), terminal=True,
            error=terminal_event.title.startswith("⚠️"),
        )
        return {"candidate": candidate, "stats": result, "local_task_id": task_id}
    except Exception as exc:
        message = str(exc or "本地媒体确认整理失败").strip() or "本地媒体确认整理失败"
        if claimed_task and task_id > 0:
            try:
                current = db.get_local_media_task(
                    task_id, owner=str(payload.get("owner") or "admin")
                )
                if current is not None and current.status == "recognizing":
                    db.update_local_media_task(
                        task_id,
                        owner=current.owner,
                        status="failed",
                        error=message,
                    )
                    current = db.get_local_media_task(task_id, owner=current.owner)
                if current is not None and current.status == "failed":
                    db.update_download_request_for_local_media_task(
                        task_id,
                        "failed",
                        error=message,
                        resolve_manual=True,
                    )
            except Exception:
                logger.warning(
                    "本地媒体确认失败状态保存异常 task=%s", task_id, exc_info=True
                )
        logger.warning(
            "本地媒体确认整理失败 token=%s type=%s",
            token[:6],
            type(exc).__name__,
        )
        failure_event = NotificationEvent(
            "❌ 本地媒体确认整理失败",
            fields=(
                ("目标文件", payload.get("directory") or "本地媒体"),
                ("候选媒体", _candidate_display_name(candidate, "")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer="请前往 Web 的本地媒体待确认页继续处理。",
            layout="relaxed",
        )
        db.fail_organize_confirmation_with_delivery(
            token,
            error=message,
            event_json=_serialize_notification_event(failure_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            retryable=False,
            enqueue_delivery=False,
        )
        publish_confirmation_event(
            failure_event, chat_id=chat_id, token=token,
            message_id=_confirmation_message_id(payload), terminal=True, error=True,
        )
        raise


def _execute_confirmation(
    token: str, payload: dict, candidate: dict, *, selected_index: int, chat_id: str
) -> dict:
    if _confirmation_kind(payload) == "local_media":
        return _execute_local_media_confirmation(
            token,
            payload,
            candidate,
            selected_index=selected_index,
            chat_id=chat_id,
        )
    return _execute_guangya_confirmation(
        token,
        payload,
        candidate,
        selected_index=selected_index,
        chat_id=chat_id,
    )


def _natural_sort_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value or ""))
    )


def _confirmed_multipart_overrides(payload: dict, files: list[dict]) -> dict[object, int]:
    if str(payload.get("multipart_strategy") or "") != "sequence":
        return {}
    ordered = sorted(
        files, key=lambda item: _natural_sort_key(str(item.get("name") or ""))
    )
    directory = str(payload.get("directory") or "")
    overrides: dict[object, int] = {}
    for index, item in enumerate(ordered, 1):
        name = str(item.get("name") or "")
        overrides[name] = index
        overrides[(directory, name)] = index
    return overrides


def _execute_guangya_confirmation(
    token: str, payload: dict, candidate: dict, *, selected_index: int, chat_id: str
) -> dict:
    # running 状态由 claim_queued_organize_confirmation 原子授予；worker 不得
    # 无条件重新取得所有权，否则重启恢复后的 failed 终态会被迟到执行覆盖。
    try:
        source_dir_id = str(payload.get("source_dir_id") or "").strip()
        current_rules = OrganizeRules.from_config().for_source(source_dir_id)
        if not organize_rules_snapshot_matches(payload.get("rules"), current_rules):
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

        scraper, match, detail, provider = _resolve_guangya_confirmation_candidate(
            payload, candidate, current_rules,
        )

        position_overrides = {
            str(item.get("name") or ""): (item.get("season"), item.get("episode"))
            for item in files
        }
        multipart_overrides = _confirmed_multipart_overrides(payload, files)
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
                multipart_overrides=multipart_overrides,
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
        if provider == "tmdb":
            learning_warnings = _record_confirmation_learning(
                scraper, payload, candidate, match
            )
            if learning_warnings:
                stats.setdefault("warnings", []).extend(learning_warnings)
        scope_name = str(payload.get("directory") or payload.get("source_name") or "TG 人工确认")
        try:
            confirm_debounce = max(0, min(int(float(
                get("GY_ORGANIZE_CONFIRM_STRM_DEBOUNCE_SECONDS", "8") or 8
            )), 30))
        except (TypeError, ValueError, OverflowError):
            confirm_debounce = 8
        terminal_event = _confirmation_result_event(payload, candidate, stats)
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json=json.dumps(stats, ensure_ascii=False, default=str),
            event_json=_serialize_notification_event(terminal_event),
            chat_id=chat_id,
            message_id=_confirmation_message_id(payload),
            enqueue_delivery=False,
        )
        # 先把候选卡收敛为整理终态，再排队 STRM；即使 debounce=0，
        # 后续刷新也只会在同一条终态消息上补字段，不会被较旧内容覆盖。
        publish_confirmation_event(
            terminal_event, chat_id=chat_id, token=token,
            message_id=_confirmation_message_id(payload), terminal=True,
            error=terminal_event.title.startswith("⚠️"),
        )
        try:
            Organizer.trigger_post_actions(
                stats,
                current_rules,
                source_name=scope_name,
                chat_id=chat_id,
                notify_result=False,
                strm_debounce_seconds=confirm_debounce,
                notification_threads=[{
                    "topic": "confirmation",
                    "thread_key": f"confirmation:{token}",
                    "token": token,
                    "chat_id": str(chat_id or ""),
                    "topic_enabled": True,
                }],
            )
        except Exception as post_exc:
            warning = f"STRM 后处理启动失败：{post_exc}"
            stats.setdefault("warnings", []).append(warning)
            db.update_organize_confirmation(
                token,
                result_json=json.dumps(stats, ensure_ascii=False, default=str),
            )
            update_confirmation_lifecycle_downstream(
                token,
                chat_id=chat_id,
                strm_status="启动失败",
                media_refresh="未触发",
                partial=True,
                error=warning,
            )
            logger.warning(
                "人工确认整理已完成但后处理启动失败 token=%s type=%s",
                token[:6],
                type(post_exc).__name__,
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
                ("所在目录", payload.get("directory") or "/"),
                ("候选媒体", _candidate_display_name(candidate, "")),
                NOTIFICATION_SECTION_BREAK,
                ("错误原因", message),
            ),
            footer=(
                "可点击下方按钮重试。"
                if retryable else "请重新执行整理生成新候选。"
            ),
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
            enqueue_delivery=False,
        )
        publish_confirmation_event(
            failure_event, chat_id=chat_id, token=token,
            message_id=_confirmation_message_id(payload), terminal=True, error=True,
        )
        if not retryable:
            _finalize_guangya_manual_logs(
                payload, status="failed", error=message,
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
    if list(group.get("candidates") or []):
        footer = f"{reason}\n\n请选择候选继续整理，或跳过此组。"
    else:
        footer = (
            f"{reason}\n\n当前没有可用元数据。可跳过此组；文件保持原位，"
            "本次待确认状态会结束。"
        )
    return NotificationEvent(
        title=title,
        fields=tuple(fields.items()),
        lines=_candidate_summary_lines(group),
        footer=footer,
        actions=actions,
        layout="relaxed",
    )
