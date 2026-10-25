"""整理补全的服务端通知上下文与 STRM 通知 scope。

身份不属于规则快照：入口在任何入队之前冻结父任务和收件人。probe 交接
把该上下文附在既有持久 changes 中，避免成功 ACK 后重启丢失路由。
旧任务不猜测关联；只允许脱敏异常，永不补发逐文件成功消息。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Iterable

from app.config import get
from app.logger import get_logger
from app.repositories import organize_probe as probe_repository

logger = get_logger(__name__)
PROBE_CONTEXT_KEY = "_probe_notification_context"


def build_notification_context(
    *, task_id: str = "", operation_token: str = "", confirmation_token: str = "",
    chat_id: str = "", download_request_ids: list[int] | None = None,
    notification_threads: list[dict] | None = None,
    notify_enabled: bool = True, topic_enabled: bool = True,
) -> dict:
    """仅由服务端入口调用；不从 rules、API payload 或未知旧任务推断身份。

    默认 chat 也在此刻解析并冻结；冻结为空时禁止以后借用新默认 chat。
    下载任务读取已持久化 request 的路由，不把整个多源任务广播到一个 chat。
    """
    chat = (chat_id or get("TG_CHAT_ID", "") or "").strip()
    task = (task_id or (operation_token if not confirmation_token else "") or "").strip()
    refs = list(notification_threads or [])
    ids = list(download_request_ids or [])
    if ids and not chat_id and not confirmation_token:
        # 已知下载 owner 的路由读取失败时，不得把默认管理员当作替代收件人。
        # 成功读取到的每个 request 仍在 refs 中保留其明确冻结的接收范围。
        chat = ""
    if task and not ids:
        refs.append({"topic": "organize", "thread_key": f"organize:{task}", "task_id": task,
                     "chat_id": chat, "topic_enabled": topic_enabled})
    if confirmation_token:
        refs.append({"topic": "confirmation", "thread_key": f"confirmation:{confirmation_token}",
                     "token": confirmation_token, "chat_id": chat, "topic_enabled": topic_enabled})
    if ids:
        from app import database as db
        from app.modules.telegram_download_lifecycle import _chat_id
        for request_id in ids:
            try:
                row = db.get_download_request(request_id)
                if row is None:
                    continue
                recipient = (_chat_id(row) or get("TG_CHAT_ID", "") or "").strip()
            except Exception as exc:  # noqa: BLE001 -- 失败路由静默保留，不能扩大接收范围或反驱动业务。
                logger.warning("冻结下载补全通知路由失败 type=%s", type(exc).__name__)
                continue
            refs.append({"topic": "download", "thread_key": f"download:{request_id}",
                         "request_id": str(request_id), "chat_id": recipient,
                         "topic_enabled": topic_enabled})
    return probe_repository.normalize_notification_context({
        "version": 1, "task_id": task, "chat_id": chat,
        "notify_enabled": notify_enabled, "topic_enabled": topic_enabled,
        "notification_threads": refs, "download_request_ids": ids,
    })


def apply_notification_context(stats: dict, context: object) -> None:
    context = probe_repository.normalize_notification_context(context)
    if context:
        stats["notification_context"] = context
        if context["task_id"]:
            stats["task_id"] = context["task_id"]


def tag_probe_changes(changes: list[dict], context: object, *, notify_enabled: bool) -> list[dict]:
    context = probe_repository.normalize_notification_context(context)
    return [{**item, PROBE_CONTEXT_KEY: context, "_probe_notify_enabled": notify_enabled} for item in changes]


def merge_notification_scopes(*groups) -> list[dict]:
    merged = {}
    for group in groups:
        for item in group or []:
            if isinstance(item, dict):
                merged[json.dumps(item, sort_keys=True, ensure_ascii=False)] = dict(item)
    return list(merged.values())


def probe_scopes_from_changes(changes: Iterable[object] | None) -> list[dict]:
    return merge_notification_scopes([
        {"probe": True, "context": probe_repository.normalize_notification_context(item[PROBE_CONTEXT_KEY]),
         "notify_override": item.get("_probe_notify_enabled", True) is True}
        for item in changes or [] if isinstance(item, dict) and PROBE_CONTEXT_KEY in item
    ])


def notification_scopes(options: dict) -> list[dict]:
    """按触发请求保存通知范围；合并同步不意味着合并接收者和通知策略。"""
    if "notification_scopes" in options:
        return list(options["notification_scopes"])
    changes = list(options.get("organize_changes") or [])
    scopes = probe_scopes_from_changes(changes)
    if not changes or any(PROBE_CONTEXT_KEY not in item for item in changes):
        scopes.insert(0, {key: options.get(key) for key in (
            "notify_override", "detail_notify_override", "download_request_ids", "notification_threads",
            "chat_ids", "uses_default_notification_scope", "has_silent_notification_scope",
        )})
    return scopes


def probe_downstream_state(ref: dict, *, strm_status: str, partial: bool, error: str) -> tuple[str, bool, str]:
    """稳定的批次状态，不把首个完成对象或仅入 STRM 队列当作全批完成。"""
    progress = probe_repository.get_organize_probe_notification_progress(
        topic=str(ref.get("topic") or ""), thread_key=str(ref.get("thread_key") or ""),
        chat_id=str(ref.get("chat_id") or ""), notification_enabled_only=True,
    )
    if partial or error:
        return strm_status, partial, error
    if progress["failed"] or progress["cancelled"] or progress.get("strm_failed", 0):
        return "后台规格补全需复核", True, "后台规格补全有失败或取消项，请在 Web 运行记录中查看。"
    if progress["pending"] or progress["strm_pending"]:
        return "后台规格补全进行中", False, ""
    return strm_status, partial, error


def publish_probe_acknowledged(job: dict) -> None:
    """补齐最后一项 STRM 先完成、probe 后 ACK 的通知竞态；不改变业务结果。"""
    context = probe_repository.normalize_notification_context(job.get("notification_context_json"))
    if not context:
        return
    if not probe_repository.probe_rules_notify_enabled(job.get("rules_json", "{}")):
        return
    ready_refs = []
    for ref in context["notification_threads"]:
        progress = probe_repository.get_organize_probe_notification_progress(
            topic=ref["topic"], thread_key=ref["thread_key"], chat_id=ref["chat_id"],
            notification_enabled_only=True,
        )
        if not progress["pending"] and not progress["strm_pending"]:
            ready_refs.append(ref)
    if not ready_refs:
        return
    ready_context = {**context, "notification_threads": ready_refs}
    publish_probe_scope(
        {"probe": True, "context": ready_context, "notify_override": context["notify_enabled"]},
        strm_status="完成", media_refresh="",
    )


def publish_probe_scope(scope: dict, *, strm_status: str, media_refresh: str,
                        partial: bool = False, error: str = "") -> bool:
    """复用已存在父消息；成功仅更新稳定状态，不按 file/run 新建消息。

    错误不被随后单文件成功覆盖，未知初次发送不盲重试。缺父线程时只发
    一条按父/接收范围隔离的脱敏异常线程，legacy 只进默认管理通知范围。
    """
    from app.modules.telegram_notification_center import (
        get_notification_thread_snapshot,
        publish_notification_thread,
    )
    from app.modules.telegram_notification_policy import (
        NotificationImportance,
        NotificationTopic,
    )
    from app.notifier import NotificationEvent

    context = probe_repository.normalize_notification_context(scope.get("context"))
    if not scope.get("notify_override", True) or (context and not context["notify_enabled"]):
        return False
    if context and not context["topic_enabled"]:
        return False
    failed = bool(partial or error)
    handled = False
    missing_chats = set()
    refs = context.get("notification_threads", [])
    for ref in refs:
        chat = ref["chat_id"]
        if not chat or not ref["topic_enabled"]:
            continue
        try:
            previous = get_notification_thread_snapshot(ref["thread_key"], topic=ref["topic"], chat_id=chat)
            if previous is None:
                missing_chats.add(chat)
                continue
            scoped_status, scoped_partial, scoped_error = probe_downstream_state(
                ref, strm_status=strm_status, partial=partial, error=error,
            )
            scoped_failed = bool(scoped_partial or scoped_error)
            # unknown 可以保存新期望内容；通知中心保持 unknown，不重新发送。
            # 分层整理消息可更新已结算的 probe 状态，同时保留 parent 警告。
            # 未归因的旧消息或仍存在其他下游错误时，单项成功不能清掉告警。
            from app.modules.telegram_organize_lifecycle import _event_downstream_failed

            can_project_parent = (
                ref["topic"] == "organize" and previous.event.lifecycle.get("parent")
                and not _event_downstream_failed(previous.event)
            )
            if not failed and not can_project_parent and (previous.event.state == "partial" or str(previous.event.title).startswith(("⚠️", "⏳"))):
                handled = True
                continue
            safe_error = "后台规格补全后的 STRM 同步异常，请在 Web 运行记录中查看。" if scoped_failed else ""
            scoped_refresh = media_refresh or next((str(value) for label, value in previous.event.fields
                                                   if label in {"媒体库", "媒体库刷新"}), "")
            if ref["topic"] == "organize":
                from app.modules.telegram_organize_lifecycle import (
                    update_organize_lifecycle_downstream,
                )
                result = update_organize_lifecycle_downstream(
                    ref["task_id"], chat_id=chat, strm_status=scoped_status, media_refresh=scoped_refresh,
                    partial=scoped_failed, error=safe_error, topic_enabled=ref["topic_enabled"],
                )
            elif ref["topic"] == "confirmation":
                from app.modules.organize_confirmations import (
                    update_confirmation_lifecycle_downstream,
                )
                result = update_confirmation_lifecycle_downstream(
                    ref["token"], chat_id=chat, strm_status=scoped_status, media_refresh=scoped_refresh,
                    partial=scoped_failed, error=safe_error,
                )
            else:
                # 下载补全不重写 request 的业务终态，只原位更新其通知下游字段。
                fields = tuple((label, scoped_status if label == "STRM" else scoped_refresh if label == "媒体库" else value)
                               for label, value in previous.event.fields)
                event = replace(previous.event, fields=fields)
                if scoped_failed:
                    event = replace(event, title="⚠️ 下载入库链路部分完成", footer=safe_error, state="partial")
                result = publish_notification_thread(
                    ref["thread_key"], event, topic=NotificationTopic.DOWNLOAD, chat_id=chat,
                    importance=NotificationImportance.ERROR if scoped_failed else NotificationImportance.RESULT,
                    topic_enabled=ref["topic_enabled"],
                )
            # 缺父时绝不把成功降级为独立汇总；ERROR 的兜底仍使用固定逻辑键。
            if getattr(result, "status", "") == "missing_thread":
                missing_chats.add(chat)
            else:
                handled = True
        except Exception as exc:  # noqa: BLE001 -- 通知侧异常必须与已提交的媒体写入隔离。
            logger.warning("规格补全父通知更新失败 type=%s", type(exc).__name__)
            missing_chats.add(chat)
    if failed:
        if not refs:
            if not context:
                # 无来源/私聊证据的旧任务，仅允许默认管理范围的脱敏异常。
                missing_chats.add((get("TG_CHAT_ID", "") or "").strip())
            elif context["chat_id"]:
                missing_chats.add(context["chat_id"])
        identity = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:24]
        for chat in sorted(missing_chats):
            if not chat:
                continue
            result = publish_notification_thread(
                f"probe-error:{identity}",
                NotificationEvent("⚠️ 后台规格补全同步异常", fields=(("说明", "精准 STRM 同步尚未完成，请在 Web 运行记录中查看。"),), state="partial"),
                topic=NotificationTopic.STRM, importance=NotificationImportance.ERROR, chat_id=chat,
            )
            handled = bool(result) or handled
    return handled
