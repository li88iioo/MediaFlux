"""Telegram 整理批次的单消息分页、人工选择与冻结建议确认。"""
from __future__ import annotations

import json
import hashlib
import re
from collections import Counter
from hmac import compare_digest
from typing import NamedTuple

from app.repositories import organize_confirmations as organize_confirmations_repo
from app.agent import feature_gate
from app.modules.telegram_notification_center import (
    get_notification_thread_snapshot,
    publish_notification_thread,
)
from app.modules.telegram_notification_policy import NotificationImportance, NotificationTopic
from app.notifier import (
    NotificationAction,
    NotificationEvent,
    notification_target_chat_id,
    safe_int,
    truncate_telegram_text,
)

_PAGE_SIZE = 5
_BATCH_ID = re.compile(r"[0-9a-f]{16,32}\Z")
_CALLBACK = re.compile(r"orgb:([A-Za-z0-9_-]{1,32}):([pdra]):([0-9a-f]{16}|[0-9]{1,8})\Z")
_STATUSES = {
    "pending": "待确认", "queued": "已排队", "running": "整理中",
    "completed": "整理完成", "failed": "失败", "cancelled": "已结束", "expired": "已过期",
}
_ISSUE_STATES = {"partial", "waiting"}


class _Receipt(NamedTuple):
    state: str
    label: str


class _GroupStatus(NamedTuple):
    state: str
    organize_issue: str
    strm: _Receipt
    refresh: tuple[_Receipt, ...]


def _thread_key(batch_id: str) -> str:
    return f"confirmation-batch:{batch_id}"


def _batch_items(batch_id: str, chat_id: str) -> dict:
    from app.modules.organize_confirmation_batches import list_confirmations

    return list_confirmations(
        batch_id=batch_id, status="all", chat_id=chat_id, page=1, page_size=1000,
    )


def _receipt(value: object, *, media_refresh: bool = False) -> _Receipt:
    label = str(value or "").strip() or "等待回执"
    provider, state = "", label.lower()
    if media_refresh:
        parts = label.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in {"jellyfin", "emby", "媒体库"}:
            provider, state = f"{parts[0]} ", parts[1].lower()
    if state in {"完成", "已完成", "成功", "success", "completed"} or (
        media_refresh and state in {"已刷新", "请求已受理"}
    ):
        return _Receipt("completed", f"{provider}请求已受理" if media_refresh else "已完成")
    if media_refresh and state in {"已排队", "queued", "待后台提交"}:
        return _Receipt("submitted", f"{provider}待后台提交")
    if any(word in state for word in ("失败", "异常", "错误", "部分完成", "已停止", "failed", "error")):
        return _Receipt("partial", label)
    if any(word in state for word in (
        "等待", "排队", "进行中", "执行中", "未完成", "待重试", "pending", "queued", "running",
    )):
        return _Receipt("waiting", label)
    if any(word in state for word in (
        "完成", "成功", "已刷新", "请求已受理", "跳过", "未启用", "未触发", "无变更", "关闭", "无目标",
        "completed", "success", "skipped", "disabled", "not_required",
    )):
        return _Receipt("completed", label)
    return _Receipt("waiting", label)


def _organize_issue(item: dict) -> str:
    if item.get("error"):
        return str(item["error"])
    execution = item.get("execution") or {}
    issues = []
    cleanup_failed = False
    for key, label in (("failed", "失败"), ("rename_failed", "改名失败"),
                       ("need_confirm", "文件待确认"), ("duplicate_cleanup_failed", "同版本回收失败"),
                       ("source_dir_cleanup_failed", "源目录清理失败"), ("empty_dir_cleanup_failed", "空目录清理失败")):
        count = safe_int(execution.get(key), minimum=0)
        if count:
            issues.append(f"{label} {count}")
            cleanup_failed |= key in {"source_dir_cleanup_failed", "empty_dir_cleanup_failed"}
    if cleanup_failed:
        reason = next((text for text in execution.get("empty_dir_cleanup_reasons", []) if "失败" in text), "")
        if reason:
            issues.append(reason)
    warnings = execution.get("warnings") or execution.get("scan_errors") or []
    return " · ".join([*issues, *warnings[:1]])


def _group_status(item: dict) -> _GroupStatus:
    downstream = item.get("downstream") or {}
    organize_issue = _organize_issue(item)
    strm = _receipt(downstream.get("strm"))
    refresh = tuple(_receipt(part, media_refresh=True) for part in str(downstream.get("media_refresh") or "").split("/"))
    states = {strm.state, *(receipt.state for receipt in refresh)}
    status = item.get("status")
    if downstream.get("partial") or downstream.get("error") or status == "failed":
        state = "partial"
    elif status != "completed":
        state = ""
    elif organize_issue or "partial" in states:
        state = "partial"
    elif "waiting" in states:
        state = "waiting"
    elif "submitted" in states:
        # STRM 已完成才可将媒体刷新交给后台接管，并等待实际提交回执。
        state = "submitted" if strm.label == "已完成" else "waiting"
    else:
        state = "completed"
    return _GroupStatus(state, organize_issue, strm, refresh)


def _downstream_issues(item: dict, receipts: _GroupStatus) -> list[str]:
    downstream = item.get("downstream") or {}
    organized = item.get("status") == "completed"
    issues = []
    if (organized or downstream.get("strm")) and receipts.strm.state in _ISSUE_STATES:
        issues.append(f"STRM：{truncate_telegram_text(receipts.strm.label, 100)}")
    if organized or downstream.get("media_refresh"):
        refresh = " / ".join(receipt.label for receipt in receipts.refresh if receipt.state in _ISSUE_STATES or (
            receipts.state == "waiting" and receipt.state == "submitted"
        ))
        if refresh:
            issues.append(f"媒体库刷新：{truncate_telegram_text(refresh, 160)}")
    if downstream.get("error"):
        issues.append(f"后续处理：{truncate_telegram_text(downstream['error'], 140)}")
    elif downstream.get("partial") and not issues:
        issues.append("后续处理：部分完成")
    return issues


def _candidate(item: dict, index: object) -> dict | None:
    if type(index) is not int or index < 0:
        return None
    return next((candidate for candidate in item.get("candidates", [])
                 if candidate.get("index") == index), None)


def _candidate_label(candidate: dict) -> str:
    title = truncate_telegram_text(candidate.get("title") or "未命名候选", 80)
    year = str(candidate.get("year") or "")
    provider = str(candidate.get("provider") or "TMDB")
    external_id = str(candidate.get("external_id") or candidate.get("tmdb_id") or "")
    return f"{title}{f' ({year})' if year else ''} · {provider} {external_id}".strip()


def _suggestion_preview(items: list[dict], chat_id: str) -> dict | None:
    """预览整批已通过服务器复核门或人工保存的建议；单组核验失败时剔除该组。"""
    from app.modules.organize_confirmation_batches import preview_choices

    candidates = []
    for item in items:
        suggestion = item.get("suggestion")
        if item.get("status") != "pending" or not isinstance(suggestion, dict):
            continue
        index = suggestion.get("candidate_index")
        if _candidate(item, index) is not None:
            candidates.append((item, {"confirmation_id": item["confirmation_id"], "candidate_index": index}))
    if not candidates:
        return None
    entries, skipped = [entry for _item, entry in candidates], []
    try:
        preview = preview_choices(entries, chat_id=chat_id)
    except ValueError:
        # 整批失败时才逐组定位原因，正常路径只预览一次。
        entries = []
        for item, entry in candidates:
            try:
                preview_choices([entry], chat_id=chat_id)
            except ValueError as exc:
                skipped.append((item, str(exc)))
            else:
                entries.append(entry)
        if not entries:
            return {"entries": [], "digest": "", "items": [], "mapping_items": [], "scope_digest": "",
                    "skipped": skipped}
        preview = preview_choices(entries, chat_id=chat_id)
    scope_digest = preview["digest"]
    mapped = [item for item in preview["items"] if item.get("episode_mappings")]
    if mapped:
        mapped_tokens = {item["confirmation_id"] for item in mapped}
        ordinary = [entry for entry in preview["entries"] if entry["confirmation_id"] not in mapped_tokens]
        preview = preview_choices(ordinary, chat_id=chat_id) if ordinary else {"entries": [], "digest": "", "items": []}
    return {**preview, "mapping_items": mapped, "scope_digest": scope_digest, "skipped": skipped}


def _skipped_notice(item: dict, error: str) -> str:
    identity = truncate_telegram_text(item.get("identity") or item.get("directory") or "待确认媒体", 60)
    return f"{identity} 暂不可批量确认：{truncate_telegram_text(error, 120)}"


def _mapping_notice(item: dict) -> str:
    mappings = item["episode_mappings"]
    examples = "、".join(
        f"S{mapping['source_season']:02d}E{mapping['source_episode']:02d} → "
        f"S{mapping['target_season']:02d}E{mapping['target_episode']:02d}"
        for mapping in mappings[:2]
    )
    identity = truncate_telegram_text(item.get("identity") or item.get("directory") or "待确认媒体", 60)
    return f"{identity}：{examples}（共 {len(mappings)} 个季集映射），请在 Web 预览后确认。"


def _button(label: str, token: str, action: str, value: object) -> NotificationAction:
    data = f"orgb:{token}:{action}:{value}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("批次按钮参数过长")
    return NotificationAction(label, data)


def _batch_event(
    batch_id: str, items: list[dict], receipts: list[_GroupStatus], *, chat_id: str, page: int,
    detail_token: str, counts: Counter[str], downstream_counts: dict[str, int],
    truncated: bool, downstream_digest: str,
) -> NotificationEvent:
    attention = [(item, receipt) for item, receipt in zip(items, receipts)
                 if item.get("status") in {"pending", "queued", "running", "failed", "expired"}
                 or receipt.state in _ISSUE_STATES]
    pages = max(1, (len(attention) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, pages))
    anchor = str(items[0]["confirmation_id"])
    active = bool(counts["pending"] or counts["queued"] or counts["running"] or downstream_counts["waiting"] or truncated)
    status_text = " · ".join(f"{label} {counts[status]}" for status, label in _STATUSES.items() if counts[status])
    metadata: dict = {
        "batch_id": batch_id, "page": page, "detail_token": detail_token,
        "progress": dict(counts), "downstream": downstream_counts, "truncated": truncated,
        "downstream_digest": downstream_digest,
    }
    detail_index = None
    if detail_token and (active or attention):
        detail_index = next((index for index, item in enumerate(items) if item["confirmation_id"] == detail_token), None)
    lines: list[str] = []
    actions: list[NotificationAction] = []
    if detail_index is not None:
        detail, detail_receipts = items[detail_index], receipts[detail_index]
        status = str(detail.get("status") or "pending")
        fields = (
            ("媒体", str(detail.get("identity") or "待确认媒体")),
            ("来源", f"{detail.get('source') or ''}/{detail.get('directory') or ''}"),
            ("状态", _STATUSES.get(status, status)),
            ("有效期", str(detail.get("expires_at") or "")),
        )
        if detail.get("reason"):
            lines.append(truncate_telegram_text(detail["reason"], 240))
        suggestion = detail.get("suggestion")
        mapping_items = []
        if isinstance(suggestion, dict):
            candidate = _candidate(detail, suggestion.get("candidate_index"))
            if candidate:
                label = "人工选择" if suggestion.get("source") == "human" else "Agent 建议"
                lines.append(f"{label}：{_candidate_label(candidate)}")
            if suggestion.get("summary"):
                lines.append(truncate_telegram_text(suggestion["summary"], 300))
            if candidate and status == "pending":
                try:
                    preview = _suggestion_preview([detail], chat_id)
                    mapping_items = preview.get("mapping_items", []) if preview else []
                except ValueError:
                    pass  # 普通候选仍可由原单组链路重新核验。
                lines.extend(_mapping_notice(item) for item in mapping_items)
        elif detail.get("review_summary"):
            lines.append(f"复核结果：{truncate_telegram_text(detail['review_summary'], 240)}")
        for candidate in detail.get("candidates", []):
            index = candidate["index"]
            label = _candidate_label(candidate)
            lines.append(f"{index + 1}. {label}")
            if status == "pending" and not mapping_items:
                actions.append(NotificationAction(
                    f"选择 {index + 1} · {label}", f"orgc:{detail_token}:{index}",
                ))
        if detail_receipts.organize_issue:
            lines.append(truncate_telegram_text(detail_receipts.organize_issue, 240))
        downstream = detail.get("downstream") or {}
        fields += (("STRM 状态", detail_receipts.strm.label),
                   ("媒体库刷新", " / ".join(receipt.label for receipt in detail_receipts.refresh)))
        if downstream.get("partial"):
            lines.append("后续处理部分完成，请查看 STRM 与媒体库状态。")
        if downstream.get("error"):
            lines.append(truncate_telegram_text(downstream["error"], 240))
        if status == "pending":
            actions.append(NotificationAction("跳过此组", f"orgc:{detail_token}:skip"))
        actions.append(_button("返回批次摘要", anchor, "p", page))
        title = "整理确认 · 媒体详情"
        footer = "选择候选后进入原整理队列；跳过时源文件保持原位。"
        if mapping_items:
            footer = "此组涉及季集映射，请到 Web 查看完整映射后确认；TG 仅保留跳过操作。"
    else:
        metadata["detail_token"] = ""
        title = (
            "⚠️ 整理待确认批次" if counts["pending"] else
            "⚠️ 整理批次部分完成" if downstream_counts["partial"] else
            "⏳ 整理批次处理中" if active else
            "✅ 整理批次已完成" if counts["completed"] else "整理批次已结束"
        )
        # 分母是整批进入过复核的组；已确认或已处理的组算完成，不能移出分母让进度倒退。
        reviewing = [item for item in items if item.get("review_status")]
        review_count = sum(item.get("status") == "pending" and item["review_status"] in {"waiting", "pending", "running"}
                           for item in reviewing)
        fields = (("媒体组", f"{len(items)}{'+' if truncated else ''}"), ("进度", status_text))
        if pages > 1:
            fields += (("分页", f"{page}/{pages}"),)
        if review_count:
            # 复核逐组完成时不重发消息，避免刷屏；进度在刷新或最后一组完成时更新。
            fields += (("Agent 复核", f"已完成 {len(reviewing) - review_count}/{len(reviewing)} 组，点「刷新摘要」查看最新建议"),)
        start = (page - 1) * _PAGE_SIZE
        visible = attention[start:start + _PAGE_SIZE]
        for offset, (item, item_receipts) in enumerate(visible, start + 1):
            identity = truncate_telegram_text(item.get("identity") or item.get("directory") or "待确认媒体", 70)
            status = str(item.get("status") or "pending")
            label = _STATUSES.get(status, status)
            if status == "failed":
                label = "整理失败"
            elif status == "completed" and item_receipts.organize_issue:
                label = "整理部分完成"
            lines.append(f"{offset}. {identity} · {label}")
            if item_receipts.organize_issue:
                lines.append(f"整理：{truncate_telegram_text(item_receipts.organize_issue, 140)}")
            lines.extend(_downstream_issues(item, item_receipts))
            suggestion = item.get("suggestion")
            candidate = _candidate(item, suggestion.get("candidate_index")) if isinstance(suggestion, dict) else None
            if candidate and status == "pending":
                assert isinstance(suggestion, dict)
                lines.append(f"建议：{_candidate_label(candidate)}\n{truncate_telegram_text(suggestion.get('summary') or '请查看候选详情', 140)}")
            elif item.get("review_summary") and status == "pending":
                lines.append(f"复核：{truncate_telegram_text(item['review_summary'], 140)}")
            actions.append(_button(f"查看 {offset} · {identity}", item["confirmation_id"], "d", page))
        if page > 1:
            actions.append(_button("上一页", anchor, "p", page - 1))
        if page < pages:
            actions.append(_button("下一页", anchor, "p", page + 1))
        footer = ("打开媒体详情可手动选择候选。没有建议的媒体不会自动选择。" if counts["pending"] else
                  "后续处理仍在进行，可刷新摘要查看状态。" if active else
                  "请在 Web 整理日志查看处理明细。")
        if downstream_counts["submitted"]:
            footer += "\n收到后台刷新回执后，会更新这条消息。"
        preview = None
        if truncated:
            footer = "此批次超过 1000 组，仅展示前 1000 组；请在 Web 分批审阅、复核和确认。"
        else:
            try:
                # 建议只来自通过确定性复核门的 Agent 结果或人工保存的选择。
                preview = _suggestion_preview(items, chat_id)
            except ValueError as exc:
                lines.append(f"建议暂不可提交：{truncate_telegram_text(str(exc), 160)}")
        visible_tokens = {item["confirmation_id"] for item, _receipt in visible}
        lines.extend(_skipped_notice(item, error) for item, error in (preview or {}).get("skipped", [])
                     if item["confirmation_id"] in visible_tokens)
        mapped = preview.get("mapping_items", []) if preview else []
        if mapped:
            lines.extend(_mapping_notice(item) for item in mapped if item["confirmation_id"] in visible_tokens)
            footer += f"\n{len(mapped)} 组涉及季集映射，请在 Web 查看完整映射；TG 批量仅确认其余普通建议。"
        if preview and preview.get("entries"):
            metadata["digest"] = preview["digest"]
            metadata["scope_digest"] = preview["scope_digest"]
            actions.append(_button(
                f"{'确认已核验的普通建议' if mapped else '确认全部已核验建议'}（{len(preview['entries'])} 组）",
                anchor, "a", str(preview["digest"])[:16],
            ))
            footer += ("\n一键确认只包含 Agent 已通过服务器核验或你已保存的选择，可翻页逐组查看；"
                       "没有建议的组需逐组选择，建议变化时需刷新后再次确认。")
            if preview.get("note"):
                footer += f"\n{preview['note']}"
        if not truncated and counts["pending"] and feature_gate.recognition_review_enabled():
            actions.append(_button("请求 Agent 只读复核", anchor, "r", page))
        if active:
            actions.append(_button("刷新摘要", anchor, "p", page))
    return NotificationEvent(
        title=title, fields=fields, lines=tuple(lines), footer=footer,
        actions=tuple(actions), layout="relaxed", state="confirmation_batch",
        lifecycle={"confirmation_batch": metadata},
    )


def _publish_batch_view(
    batch_id: str, *, chat_id: str = "", message_id: int | None = None,
    deliver_now: bool = False, expected_confirmation_delivery: tuple[int, int] | None = None,
    expected_revision: int | None = None, page: int | None = None, detail_token: str | None = None,
) -> bool:
    if not _BATCH_ID.fullmatch(batch_id):
        return False
    target = notification_target_chat_id(chat_id)
    if not target:
        return False
    for _ in range(3):
        snapshot = get_notification_thread_snapshot(
            _thread_key(batch_id), topic=NotificationTopic.CONFIRMATION, chat_id=target,
        )
        previous = snapshot.event.lifecycle.get("confirmation_batch", {}) if snapshot else {}
        result = _batch_items(batch_id, target)
        items = result["items"]
        truncated = bool(result.get("selection_truncated") or result["total"] > len(items))
        if not items:
            return False
        counts = Counter(item["status"] for item in items)
        receipts = [_group_status(item) for item in items]
        downstream_counts = dict.fromkeys(("partial", "waiting", "submitted", "completed"), 0)
        downstream_counts.update(Counter(receipt.state for receipt in receipts if receipt.state))
        downstream_digest = hashlib.sha256(json.dumps([
            (item["confirmation_id"], item.get("downstream_run_id", 0), item.get("downstream") or {}, receipt.organize_issue)
            for item, receipt in zip(items, receipts)
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # 只读复核逐组落盘时先保留已展示的冻结按钮，最后一组完成再统一更新。
        # 手动刷新与执行状态回执始终走 outbox；重启后仍可从持久状态重建。
        if (not deliver_now and expected_confirmation_delivery is None and expected_revision is None
                and snapshot and not previous.get("detail_token")
                and previous.get("progress") == dict(counts)
                and previous.get("downstream") == downstream_counts
                and previous.get("downstream_digest") == downstream_digest
                and previous.get("truncated") == truncated
                and any(item.get("status") == "pending" and item.get("review_status") in {"waiting", "pending", "running"} for item in items)):
            return True
        event = _batch_event(
            batch_id, items, receipts, chat_id=target,
            page=page if page is not None else int(previous.get("page") or 1),
            detail_token=detail_token if detail_token is not None else str(previous.get("detail_token") or ""),
            counts=counts, downstream_counts=downstream_counts, truncated=truncated, downstream_digest=downstream_digest,
        )
        published = publish_notification_thread(
            _thread_key(batch_id), event, topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.ERROR if downstream_counts["partial"] else NotificationImportance.ACTION if counts["pending"] else NotificationImportance.RESULT,
            chat_id=target, preferred_message_id=message_id or 0, deliver_now=deliver_now,
            expected_confirmation_delivery=expected_confirmation_delivery,
            expected_revision=expected_revision if expected_revision is not None else snapshot.revision if snapshot else 0,
        )
        if published or expected_revision is not None or published.status != "invalid":
            return bool(published)
    return False


def publish_batch_summary(
    batch_id: str, *, chat_id: str = "", message_id: int | None = None,
    deliver_now: bool = False, expected_confirmation_delivery: tuple[int, int] | None = None,
    expected_revision: int | None = None,
) -> bool:
    """沿通知中心 outbox 原位更新摘要，保留用户当前页或媒体详情。"""
    return _publish_batch_view(
        batch_id, chat_id=chat_id, message_id=message_id, deliver_now=deliver_now,
        expected_confirmation_delivery=expected_confirmation_delivery, expected_revision=expected_revision,
    )


def handle_batch_callback(data: str, *, chat_id: str, message_id: int) -> str:
    """回调只读复核或提交冻结选择；实际文件操作仍由原确认队列执行。"""
    from app.modules.organize_confirmation_batches import request_reviews, submit_choices

    match = _CALLBACK.fullmatch(data)
    if not match or len(data.encode("utf-8")) > 64:
        raise ValueError("批次确认参数无效")
    token, action, value = match.groups()
    row = organize_confirmations_repo.get_organize_confirmation(token)
    if row is None or not chat_id or str(row["chat_id"] or "") != chat_id:
        raise ValueError("确认操作不存在或已失效")
    payload = json.loads(row["payload_json"])
    batch_id = str(payload.get("_confirmation_batch") or "")
    if not _BATCH_ID.fullmatch(batch_id):
        raise ValueError("确认操作不存在或已失效")
    snapshot = get_notification_thread_snapshot(
        _thread_key(batch_id), topic=NotificationTopic.CONFIRMATION, chat_id=chat_id,
    )
    if snapshot is None or message_id <= 0 or snapshot.message_id != message_id:
        raise ValueError("批次消息已失效，请打开原批次摘要")
    previous = snapshot.event.lifecycle.get("confirmation_batch", {})
    page = int(previous.get("page") or 1)
    notice = "已刷新批次摘要"
    if action in {"p", "d", "r"}:
        if not value.isdigit() or int(value) < 1:
            raise ValueError("批次分页参数无效")
        page = int(value)
    if action == "r":
        if not feature_gate.recognition_review_enabled():
            raise ValueError("Agent 复核未启用，可直接手动选择候选")
        result = _batch_items(batch_id, chat_id)
        if result.get("selection_truncated") or result["total"] > len(result["items"]):
            raise ValueError("此批次超过 1000 组，请在 Web 分批请求复核")
        tokens = [item["confirmation_id"] for item in result["items"] if item.get("status") == "pending"]
        if tokens:
            counts = request_reviews(tokens, chat_id=chat_id)
            notice = f"只读复核：新请求 {counts['requested']} 组，保留 {counts['unchanged']} 组，不可用 {counts['unavailable']} 组；仍需人工确认"
        else:
            notice = "此批次已无待复核项"
    elif action == "a":
        frozen_digest = str(previous.get("digest") or "")
        result = _batch_items(batch_id, chat_id)
        if result.get("selection_truncated") or result["total"] > len(result["items"]):
            raise ValueError("此批次超过 1000 组，请在 Web 分批确认")
        preview = _suggestion_preview(result["items"], chat_id)
        if (not frozen_digest or not compare_digest(frozen_digest[:16], value)
                or not preview or not compare_digest(str(preview["digest"]), frozen_digest)
                or not compare_digest(str(preview["scope_digest"]), str(previous.get("scope_digest") or ""))):
            _publish_batch_view(batch_id, chat_id=chat_id, message_id=message_id,
                                page=page, detail_token="", deliver_now=True)
            raise ValueError("建议已变化，摘要已刷新，请查看后重新确认")
        result = submit_choices(preview["entries"], preview["digest"], chat_id=chat_id)
        notice = f"已接纳 {result['accepted']} 组，未提交 {result['failed']} 组"
        failed = next((item for item in result["items"] if not item.get("accepted") and item.get("error")), None)
        if failed:
            notice += f"；{truncate_telegram_text(failed['error'], 90)}"
    if not _publish_batch_view(
        batch_id, chat_id=chat_id, message_id=message_id, deliver_now=True,
        page=page, detail_token=token if action == "d" else "",
    ):
        raise ValueError(f"{notice}；消息更新待重试，请稍后刷新")
    return "已打开媒体详情" if action == "d" else notice
