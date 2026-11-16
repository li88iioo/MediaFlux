"""Telegram 整理批次的单消息分页、人工选择与冻结建议确认。"""
from __future__ import annotations

import json
import re
from collections import Counter
from hmac import compare_digest

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
    truncate_telegram_text,
)

_PAGE_SIZE = 5
_BATCH_ID = re.compile(r"[0-9a-f]{16,32}\Z")
_CALLBACK = re.compile(r"orgb:([A-Za-z0-9_-]{1,32}):([pdra]):([0-9a-f]{16}|[0-9]{1,8})\Z")
_STATUSES = {
    "pending": "待确认", "queued": "已排队", "running": "整理中",
    "completed": "整理完成", "failed": "失败", "cancelled": "已结束", "expired": "已过期",
}


def _thread_key(batch_id: str) -> str:
    return f"confirmation-batch:{batch_id}"


def _batch_items(batch_id: str, chat_id: str) -> dict:
    from app.modules.organize_confirmation_batches import list_confirmations

    return list_confirmations(
        batch_id=batch_id, status="all", chat_id=chat_id, page=1, page_size=1000,
    )


def _downstream_counts(items: list[dict]) -> dict[str, int]:
    counts = {"partial": 0, "waiting": 0, "submitted": 0, "completed": 0}
    for item in items:
        downstream = item.get("downstream") or {}
        if downstream.get("partial") or downstream.get("error") or item.get("status") == "failed":
            counts["partial"] += 1
        elif item.get("status") == "completed":
            states = [str(downstream.get(key) or "").lower() for key in ("strm", "media_refresh")]
            if any(any(word in state for word in ("失败", "异常", "failed", "error")) for state in states):
                counts["partial"] += 1
            elif states[0] in {"完成", "已完成", "success", "completed"} and all(
                re.search(r"(?:^|\s)(?:已排队|queued|(?:已)?完成|成功|已跳过|未启用(?:或无目标)?|无目标|skipped|disabled|success|completed)$", part.strip())
                for part in states[1].split("/")
            ) and ("已排队" in states[1] or "queued" in states[1]):
                # 队列接管是持久回执，不是缺回执；也不能据此声称媒体库扫描完成。
                counts["submitted"] += 1
            elif any(not state or any(word in state for word in (
                "等待", "排队", "进行中", "执行中", "未完成", "pending", "queued", "running",
            )) for state in states):
                counts["waiting"] += 1
            elif all(any(word in state for word in (
                "完成", "成功", "跳过", "未启用", "未触发", "无变更", "关闭",
                "completed", "success", "skipped", "disabled", "not_required",
            )) for state in states):
                counts["completed"] += 1
            else:
                counts["waiting"] += 1
    return counts


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
    batch_id: str, items: list[dict], *, chat_id: str, page: int, detail_token: str,
    truncated: bool = False,
) -> NotificationEvent:
    pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, pages))
    anchor = str(items[0]["confirmation_id"])
    counts = Counter(str(item.get("status") or "pending") for item in items)
    downstream_counts = _downstream_counts(items)
    active = bool(counts["pending"] or counts["queued"] or counts["running"] or downstream_counts["waiting"] or truncated)
    settled = not active and not downstream_counts["partial"]
    status_text = " · ".join(f"{label} {counts[status]}" for status, label in _STATUSES.items() if counts[status])
    metadata: dict = {
        "batch_id": batch_id, "page": page, "detail_token": detail_token,
        "progress": dict(counts), "downstream": downstream_counts, "truncated": truncated,
    }
    detail = next((item for item in items if item["confirmation_id"] == detail_token), None) if not settled else None
    lines: list[str] = []
    actions: list[NotificationAction] = []
    if detail is not None:
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
        if detail.get("error"):
            lines.append(truncate_telegram_text(detail["error"], 240))
        downstream = detail.get("downstream") or {}
        fields += (
            ("STRM 状态", str(downstream.get("strm") or "等待后续处理回执")),
            ("媒体库刷新", str(downstream.get("media_refresh") or "等待后续处理回执")),
        )
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
        fields = (("媒体组", f"{len(items)}{'+' if truncated else ''}"), ("进度", status_text), ("分页", f"{page}/{pages}"))
        if any(downstream_counts.values()):
            fields += (("后续处理", " · ".join(f"{label} {downstream_counts[key]}" for key, label in (
                ("partial", "部分完成"), ("waiting", "等待回执"),
                ("submitted", "已交接后台刷新"), ("completed", "已完成"),
            ) if downstream_counts[key])),)
        if review_count:
            # 复核逐组完成时不重发消息，避免刷屏；进度在刷新或最后一组完成时更新。
            fields += (("Agent 复核", f"已完成 {len(reviewing) - review_count}/{len(reviewing)} 组，点「刷新摘要」查看最新建议"),)
        for offset, item in enumerate(items[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE], (page - 1) * _PAGE_SIZE + 1):
            identity = truncate_telegram_text(item.get("identity") or item.get("directory") or "待确认媒体", 70)
            status = str(item.get("status") or "pending")
            lines.append(f"{offset}. {identity} · {_STATUSES.get(status, status)}")
            suggestion = item.get("suggestion")
            candidate = _candidate(item, suggestion.get("candidate_index")) if isinstance(suggestion, dict) else None
            if candidate and status == "pending":
                assert isinstance(suggestion, dict)
                lines.append(f"建议：{_candidate_label(candidate)}\n{truncate_telegram_text(suggestion.get('summary') or '请查看候选详情', 140)}")
            elif item.get("review_summary") and status == "pending":
                lines.append(f"复核：{truncate_telegram_text(item['review_summary'], 140)}")
            item_downstream = _downstream_counts([item])
            if status in {"pending", "queued", "running", "failed"} or item_downstream["partial"] or item_downstream["waiting"]:
                actions.append(_button(f"查看 {offset} · {identity}", item["confirmation_id"], "d", page))
        if page > 1 and not settled:
            actions.append(_button("上一页", anchor, "p", page - 1))
        if page < pages and not settled:
            actions.append(_button("下一页", anchor, "p", page + 1))
        footer = ("可分页查看并手动选择候选；没有建议的媒体不会自动选择。" if counts["pending"] else
                  "后续处理仍在进行，可刷新摘要查看状态。" if active else
                  "请在 Web 整理日志查看处理明细。")
        if downstream_counts["submitted"]:
            footer += "\nSTRM 已完成，刷新已交给后台；不代表媒体库已扫描完成。"
        preview = None
        if truncated:
            footer = "此批次超过 1000 组，仅展示前 1000 组；请在 Web 分批审阅、复核和确认。"
        else:
            try:
                # 建议只来自通过确定性复核门的 Agent 结果或人工保存的选择。
                preview = _suggestion_preview(items, chat_id)
            except ValueError as exc:
                lines.append(f"建议暂不可提交：{truncate_telegram_text(str(exc), 160)}")
        lines.extend(_skipped_notice(item, error) for item, error in (preview or {}).get("skipped", [])[:3])
        mapped = preview.get("mapping_items", []) if preview else []
        if mapped:
            lines.extend(_mapping_notice(item) for item in mapped[:3])
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
    snapshot = get_notification_thread_snapshot(
        _thread_key(batch_id), topic=NotificationTopic.CONFIRMATION, chat_id=target,
    )
    previous = snapshot.event.lifecycle.get("confirmation_batch", {}) if snapshot else {}
    result = _batch_items(batch_id, target)
    items = result["items"]
    truncated = bool(result.get("selection_truncated") or result["total"] > len(items))
    if not items:
        return False
    # 只读复核逐组落盘时先保留已展示的冻结按钮，最后一组完成再统一更新。
    # 手动刷新与执行状态回执始终走 outbox；重启后仍可从持久状态重建。
    if (not deliver_now and expected_confirmation_delivery is None and expected_revision is None
            and snapshot and not previous.get("detail_token")
            and previous.get("progress") == dict(Counter(item["status"] for item in items))
            and previous.get("downstream") == _downstream_counts(items)
            and previous.get("truncated") == truncated
            and any(item.get("status") == "pending" and item.get("review_status") in {"waiting", "pending", "running"} for item in items)):
        return True
    event = _batch_event(
        batch_id, items, chat_id=target,
        page=page if page is not None else int(previous.get("page") or 1),
        detail_token=detail_token if detail_token is not None else str(previous.get("detail_token") or ""),
        truncated=truncated,
    )
    return bool(publish_notification_thread(
        _thread_key(batch_id), event, topic=NotificationTopic.CONFIRMATION,
        importance=NotificationImportance.ERROR if _downstream_counts(items)["partial"] else NotificationImportance.ACTION if any(item.get("status") == "pending" for item in items) else NotificationImportance.RESULT,
        chat_id=target, preferred_message_id=message_id or 0, deliver_now=deliver_now,
        expected_confirmation_delivery=expected_confirmation_delivery,
        expected_revision=expected_revision if expected_revision is not None else snapshot.revision if snapshot else 0,
    ))


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
