"""Telegram 资源批选的薄 UI：短句柄、原位键盘、持久化 CAS；不执行下载。"""
from __future__ import annotations

import asyncio
import html
import re
import time
from copy import deepcopy
from typing import Any

from app.agent.kernel.references import ReferenceError
from app.agent.kernel.state import PublicationLease, SelectionInvalidError, StateUpdate
from app.agent.kernel.ux_selection import current_candidate_view

CALLBACK_RE = re.compile(
    r"^agk:s:(?:(?P<route>[A-Za-z0-9_-]{8,16}):)?"
    r"(?P<handle>ref_[A-Za-z0-9_-]{24}):"
    r"(?P<action>i(?:[1-9]|1[0-2])|t(?:qb|guangya|both)|[epr])$"
)
_DRAFT_KIND = "ux_telegram_candidate"


def _short(item: dict[str, Any]) -> str:
    coverage = item.get("coverage")
    title = str(item.get("title") or "资源")[:48]
    if isinstance(coverage, list) and len(coverage) == 3:
        season, start, end = coverage
        title = f"{'S' + str(season) + ' · ' if season else ''}{start:02}–{end:02} 集"
    tags = item.get("tags") or {}
    specs = " / ".join(str(tags[key]) for key in ("resolution", "effect", "media") if tags.get(key) and str(tags[key]).casefold() not in title.casefold())
    return f"#{item['position']} {title}{' · ' + specs if specs else ''}"


async def save_draft(runtime: Any, *, owner: str, session_id: str, view: dict, draft: dict, expected: str | None) -> dict:
    state = await runtime.store.load(owner=owner, session_id=session_id)
    current = await current_candidate_view(state=state, store=runtime.store)
    if not current or current["ref"] != view["ref"]:
        raise SelectionInvalidError()
    handle = await runtime.store.put(
        owner=owner, session_id=session_id, kind=_DRAFT_KIND,
        value={"candidate_ref": view["ref"], "selection_ref": view["selection_ref"]},
        ttl_seconds=max(1, int(view["expires_at"] - time.time())),
    )
    updated = {**deepcopy(draft), "handle": handle.ref, "candidate_ref": view["ref"], "selection_ref": view["selection_ref"]}
    lease = PublicationLease(owner, session_id, state.generation, "candidate-ui", "candidate-ui")
    await runtime.store.commit(lease, updates=(StateUpdate(
        "metadata.ux_candidate_draft", {"expected": expected, "next": updated}, mode="compare_candidate",
    ),))
    return updated


async def start_draft(runtime: Any, *, owner: str, session_id: str, view: dict) -> dict:
    return await save_draft(runtime, owner=owner, session_id=session_id, view=view, expected=None, draft={
        "positions": list(view.get("recommended_positions") or []), "target": view.get("target", "guangya"),
        "expanded": False, "phase": "select", "plan_id": "", "message_id": None,
    })


async def load_draft(runtime: Any, *, owner: str, session_id: str, handle: str, message_id: int) -> tuple[dict, dict]:
    bound = await runtime.store.resolve(handle, owner=owner, session_id=session_id, expected_kind=_DRAFT_KIND)
    state = await runtime.store.load(owner=owner, session_id=session_id)
    view = await current_candidate_view(state=state, store=runtime.store)
    draft = state.metadata.get("ux_candidate_draft")
    if (
        not view or not isinstance(draft, dict) or draft.get("handle") != handle
        or bound.get("candidate_ref") != view["ref"] or bound.get("selection_ref") != view["selection_ref"]
        or draft.get("message_id") not in (None, message_id)
    ):
        raise SelectionInvalidError("候选或按钮状态已更新，请使用当前消息中的按钮。")
    return view, deepcopy(draft)


def render(
    telebot: Any,
    view: dict,
    draft: dict,
    *,
    owner: str = "",
    session_id: str = "",
    message_thread_id: object = None,
) -> tuple[str, Any]:
    if draft.get("phase") == "result":
        return str(draft.get("result_html") or "本次处理已结束，请核对实际下载状态。"), None
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    handle = draft["handle"]
    if owner and session_id and message_thread_id is not None:
        from app.modules.telegram_topic_routing import create_session_callback_route

        route = create_session_callback_route(owner, session_id, message_thread_id)
        callback_prefix = f"agk:s:{route}:{handle}"
    else:
        callback_prefix = f"agk:s:{handle}"

    def button(label: str, action: str):
        return telebot.types.InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{action}")

    selected = set(draft["positions"])
    recommended = view.get("recommended_positions") or []
    lines = ["<b>资源推荐与批选</b>" if recommended else "<b>资源搜索与批选</b>"]
    if not recommended:
        lines.append("请选择需要的版本，再预览下载。")
    items = view["items"] if draft["expanded"] else [item for item in view["items"] if item["position"] in selected]
    for item in items:
        description = _short(item)
        extra = " · ".join(str(item.get(key) or "") for key in ("size_text", "site_name") if item.get(key))
        lines.append(html.escape(description + (" · " + extra if extra else "")))
    if not items:
        lines.append("尚未选择资源；请展开版本列表。")
    if draft["expanded"]:
        for item in view["items"]:
            markup.add(button(f"{'✓' if item['position'] in selected else '□'} {_short(item)[:45]}", f"i{item['position']}"))
    controls = [button("收起版本" if draft["expanded"] else "挑选版本", "e")]
    if recommended:
        controls.append(button("使用推荐组合", "r"))
    markup.add(*controls)
    for target in view.get("targets", []):
        label = f"{'✓ ' if target['value'] == draft['target'] else ''}{target['label']}{'' if target['available'] else '（未就绪）'}"
        markup.add(button(label, f"t{target['value']}"))
    markup.add(button(f"预览下载 {len(selected)} 项", "p"))
    labels = {target["value"]: target["label"] for target in view.get("targets", [])}
    lines += [f"\n已选 {len(selected)} 项 · 目标：{html.escape(labels.get(draft['target'], draft['target']))}",
              "选择不会下载，预检后仍需确认一次。也可回复：把 1 和 3 下载到光鸭。"]
    return "\n".join(lines), markup


async def reply_selection_ref(runtime: Any, *, owner: str, session_id: str, message: Any) -> str:
    """只信服务端签发的回复键盘句柄，不从聊天文本猜测候选编号所属批次。"""
    replied = getattr(message, "reply_to_message", None)
    markup = getattr(replied, "reply_markup", None)
    for row in getattr(markup, "keyboard", None) or getattr(markup, "inline_keyboard", None) or []:
        for button in row:
            value = button.get("callback_data") if isinstance(button, dict) else getattr(button, "callback_data", "")
            match = CALLBACK_RE.fullmatch(str(value or ""))
            if not match:
                continue
            try:
                bound = await runtime.store.resolve(match.group("handle"), owner=owner, session_id=session_id, expected_kind=_DRAFT_KIND)
            except ReferenceError as exc:
                raise SelectionInvalidError("回复的候选已过期，请重新搜索后选择。") from exc
            state = await runtime.store.load(owner=owner, session_id=session_id)
            view = await current_candidate_view(state=state, store=runtime.store)
            if not view or view["ref"] != bound["candidate_ref"]:
                raise SelectionInvalidError("回复的搜索批次已过期或被新搜索替代，请基于当前候选选择。")
            return view["selection_ref"]
    return ""


def handle_callback(
    bot: Any,
    call: Any,
    telebot: Any,
    *,
    owner: str,
    session_id: str,
    message_thread_id: object = None,
) -> None:
    from app.agent.kernel.bootstrap import get_agent_kernel_runtime
    from app.agent.kernel.transports import QueryEnvelope
    from app.bot.agent_adapter import (
        _approval_markup,
        _edit_final,
        _preview_lines,
        _render_turn,
        AGENT_CANCELLATION,
    )

    match = CALLBACK_RE.fullmatch(str(call.data or ""))
    if not match:
        bot.answer_callback_query(call.id, "按钮无效，请重新搜索。", show_alert=True)
        return
    runtime = get_agent_kernel_runtime()
    try:
        view, draft = asyncio.run(load_draft(runtime, owner=owner, session_id=session_id, handle=match.group("handle"), message_id=call.message.message_id))
        action = match.group("action")
        if draft["phase"] != "select":
            raise SelectionInvalidError("正在处理预检或确认，请使用当前按钮。")
        draft["message_id"] = call.message.message_id
        selected = set(draft["positions"])
        if action.startswith("i"):
            position = int(action[1:])
            if position not in {item["position"] for item in view["items"]}:
                raise SelectionInvalidError()
            selected.symmetric_difference_update({position})
        elif action.startswith("t"):
            target = action[1:]
            if not any(item["value"] == target and item["available"] for item in view["targets"]):
                raise SelectionInvalidError("该下载目标尚未配置或登录。")
            draft["target"] = target
        elif action == "e":
            draft["expanded"] = not draft["expanded"]
        elif action == "r":
            if not view["recommended_positions"]:
                raise SelectionInvalidError("当前没有推荐组合，请展开版本手动选择。")
            selected = set(view["recommended_positions"])
        elif action == "p":
            if not selected:
                raise SelectionInvalidError("请至少选择一个资源。")
            if not any(item["value"] == draft["target"] and item["available"] for item in view["targets"]):
                raise SelectionInvalidError("当前目标尚未就绪，请先切换目标。")
            draft["phase"] = "previewing"
        draft["positions"] = sorted(selected)
        draft = asyncio.run(save_draft(runtime, owner=owner, session_id=session_id, view=view, draft=draft, expected=match.group("handle")))
    except Exception as exc:  # noqa: BLE001 - 安全失败，不删除另一并发点击刚更新的键盘
        text = str(exc) if isinstance(exc, SelectionInvalidError) else "候选已失效，请重新搜索后选择。"
        bot.answer_callback_query(call.id, text, show_alert=True)
        return
    bot.answer_callback_query(call.id, "正在预检，尚未下载" if action == "p" else "选择已更新")
    if action != "p":
        body, markup = render(
            telebot, view, draft,
            owner=owner, session_id=session_id,
            message_thread_id=message_thread_id,
        )
        _edit_final(bot, call.message, body, reply_markup=markup, rendered_html=True)
        return
    try:
        result = asyncio.run(runtime.telegram.query(QueryEnvelope(
            owner=owner, session_id=session_id,
            message=f"预览候选 {','.join(str(pos) for pos in draft['positions'])}，目标 {draft['target']}。",
            selection={"ref": view["selection_ref"], "positions": draft["positions"], "target": draft["target"]},
            request_id=f"tgsel_{call.id}"[:150], channel="telegram",
        ), cancellation=AGENT_CANCELLATION.get()))
        if result.approval:
            draft.update(phase="approval", plan_id=result.approval.plan_id)
            asyncio.run(save_draft(runtime, owner=owner, session_id=session_id, view=view, draft=draft, expected=draft["handle"]))
            _edit_final(
                bot,
                call.message,
                "\n".join(_preview_lines(result.approval)),
                reply_markup=_approval_markup(
                    telebot,
                    result.approval,
                    owner=owner,
                    session_id=session_id,
                    message_thread_id=message_thread_id,
                ),
                rendered_html=True,
            )
        else:
            draft.update(phase="result", result_html=_render_turn(result))
            draft = asyncio.run(save_draft(runtime, owner=owner, session_id=session_id, view=view, draft=draft, expected=draft["handle"]))
            body, markup = render(
                telebot, view, draft,
                owner=owner, session_id=session_id,
                message_thread_id=message_thread_id,
            )
            _edit_final(bot, call.message, body, reply_markup=markup, rendered_html=True)
    except Exception:  # noqa: BLE001 - 预检或投递中断，不把未知状态伪造成执行成功
        _edit_final(bot, call.message, "预检响应中断；尚未确认下载。请查询当前计划或重新发起选择。")


async def settle_draft(runtime: Any, *, owner: str, session_id: str, plan_id: str, result_html: str, next_plan_id: str = "") -> None:
    state = await runtime.store.load(owner=owner, session_id=session_id)
    draft = state.metadata.get("ux_candidate_draft")
    view = await current_candidate_view(state=state, store=runtime.store)
    if not view or not isinstance(draft, dict) or draft.get("plan_id") != plan_id or draft.get("candidate_ref") != view["ref"]:
        return None
    await save_draft(runtime, owner=owner, session_id=session_id, view=view, expected=draft["handle"], draft={
        **draft, "phase": "approval" if next_plan_id else "result", "plan_id": next_plan_id,
        "result_html": "" if next_plan_id else result_html,
        "positions": draft["positions"] if next_plan_id else [],
    })
