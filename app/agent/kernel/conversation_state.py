"""会话历史的保留、来源校验和完成回执合并；不取得发布或执行权限。"""
from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any, Protocol

from app.agent.errors import ToolPipelineError
from app.agent.model_context_budget import compact_tool_content

from .conversation_codec import PendingText, conversation_cost, json_text
from .state_types import HistoryCapacityError, StateUpdate, _ExecutionCapacity


class ConversationState(Protocol):
    metadata: dict[str, Any]
    summary: str
    conversation: list[dict[str, Any]]


def retain_conversation(
    conversation: Sequence[Mapping[str, Any]], *, maximum_bytes: int = 160 * 1024,
    _required_from: int | None = None,
    _receipt_plan_id: str = "",
) -> list[dict[str, Any]]:
    """按实际存储预算保留完整用户回合，不因80条短消息删除原始条件。"""
    cost = conversation_cost

    original_bytes = cost(conversation)
    if original_bytes > maximum_bytes and any(row.get("activities") for row in conversation):
        # 展示过程是可丢弃的附加信息，不能挤掉原始请求、工具结果或执行回执。
        conversation = [{key: value for key, value in row.items() if key != "activities"}
                        for row in conversation]
        original_bytes = cost(conversation)
    if original_bytes <= maximum_bytes:
        return [dict(row) for row in conversation]
    ratio = maximum_bytes / original_bytes * 0.75
    required_end = len(conversation)
    if _receipt_plan_id:
        receipt_indices = [i for i, row in enumerate(conversation)
                           if row.get("completion_receipt_id") == _receipt_plan_id]
        if not receipt_indices:
            raise HistoryCapacityError("confirmed execution receipt is unavailable")
        required_end = receipt_indices[-1] + 1
    turns: list[list[dict[str, Any]]] = []
    required_rows: set[int] = set()
    for index, item in enumerate(conversation):
        row = deepcopy(dict(item))
        if _required_from is not None and _required_from <= index < required_end and (
            row.get("tool_calls") or row.get("role") == "tool" or row.get("completion_receipt_id")
            or row.get("reply_context", {}).get("reference_handle")
        ):
            required_rows.add(id(row))
        if _receipt_plan_id and (
            row.get("completion_receipt_id") == _receipt_plan_id
            or (row.get("role") == "tool" and row.get("effect_plan_id") == _receipt_plan_id)
            or isinstance(row.get("content"), PendingText)
        ):
            required_rows.add(id(row))  # 目标回执及未结束调用的配对不能作为诊断淘汰。
        if row.get("role") == "tool" and not isinstance(row.get("content"), PendingText):
            content = str(row.get("content") or "")
            try:
                row["content"] = compact_tool_content(content, maximum=max(512, int(len(content) * ratio)), tool_name=row.get("tool_name", ""), trusted=True)
            except ToolPipelineError as exc:
                if exc.code != "context_budget_exceeded" or index < required_end:
                    raise
                # 不可压缩的已完成诊断交给整组保留规则淘汰，不遮蔽之前的写入回执。
        elif row.get("role") == "assistant" and not row.get("completion_receipt_id") and len(str(row.get("content") or "")) > 8000:
            row["content"] = str(row["content"])[:8000] + "\n[历史长文本已截断]"
        if row.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(row)
    kept: list[dict[str, Any]] = []
    for turn in reversed(turns):
        if cost(turn + kept) <= maximum_bytes:
            kept = turn + kept
            continue
        if not kept:
            # 单次长任务也必须保留用户请求和已执行回执；工具批次不可拆开。
            blocks: list[list[dict[str, Any]]] = []
            pending: set[str] = set()
            for row in turn:
                if row.get("role") == "tool" and row.get("tool_call_id") in pending:
                    blocks[-1].append(row)
                    pending.discard(row["tool_call_id"])
                else:
                    blocks.append([row])
                    pending = {call.get("call_id") for call in row.get("tool_calls") or ()}
            order = sorted(range(len(blocks)), key=lambda i: (
                any(row.get("role") == "user" for row in blocks[i]),
                any(row.get("effect_plan_id") or row.get("completion_receipt_id") for row in blocks[i]), i,
            ), reverse=True)
            chosen = []
            used = 256
            for index in order:
                size = cost(blocks[index])
                if used + size <= maximum_bytes:
                    chosen.append(index)
                    used += size
            kept = [row for index in sorted(chosen) for row in blocks[index]]
        if required_rows - {id(row) for row in kept}:
            raise HistoryCapacityError("current execution group exceeds conversation budget")
        if kept:
            kept[0] = {**kept[0], "history_before_truncated": True}
        break
    return kept


def history_memory(state: ConversationState) -> dict[str, Any]:
    """只有 store 在裁剪事务中提取的原始 user 行可以成为历史任务来源。"""
    if state.metadata.get("history_memory_version") != 1:
        return {"requests": [], "observations": [], "legacy_note": state.summary}
    try:
        memory = json.loads(state.summary)
        if (not isinstance(memory, dict) or not isinstance(memory["requests"], list)
                or not isinstance(memory["observations"], list)
                or not isinstance(memory.get("legacy_note", ""), str)
                or any(not isinstance(row, dict) or not isinstance(row.get("sha256"), str)
                       for row in memory["observations"])):
            raise ValueError("invalid history memory")
        for row in memory["requests"]:
            if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
                    or row.get("sha256") != sha256(row["text"].encode()).hexdigest()):
                raise ValueError("invalid historical user source")
        return memory
    except (ValueError, KeyError, TypeError) as exc:
        raise HistoryCapacityError("historical user sources cannot be verified") from exc


def historical_user_sources(state: ConversationState) -> tuple[str, ...]:
    return tuple(dict.fromkeys([
        *(row["text"] for row in history_memory(state)["requests"]),
        *(row["content"] for row in state.conversation
          if row.get("role") == "user" and isinstance(row.get("content"), str)),
    ]))


def _user_event_order(
    state: ConversationState, original: Sequence[Mapping[str, Any]], memory: Mapping[str, Any],
) -> dict[str, int]:
    """沿用已落盘的事件序号；新事件只按完整原始 user 行顺序分配，早于任何裁剪。"""
    previous = [*memory["requests"], *(row for row in state.conversation if row.get("role") == "user")]
    order = {row["history_id"]: row["history_order"] for row in previous
             if row.get("history_id") and type(row.get("history_order")) is int and row["history_order"] >= 0}
    next_order = max(order.values(), default=-1) + 1
    for row in original:
        event_id = row.get("history_id")
        if row.get("role") == "user" and event_id and event_id not in order:
            order[event_id] = next_order
            next_order += 1
    return order


def preserve_history(
    state: ConversationState, original: Sequence[Mapping[str, Any]], retained: Sequence[Mapping[str, Any]],
    *, memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """落盘与模型窗口共用的来源收敛：不生成目标、授权或完成结论。"""
    memory = deepcopy(dict(memory)) if memory is not None else history_memory(state)
    event_order = _user_event_order(state, original, memory)
    known_events = {row.get("history_id") for row in memory["requests"]}
    kept_users = {row.get("history_id") for row in retained if row.get("role") == "user"}
    kept_tools = {json_text(row) for row in retained if row.get("role") == "tool"}
    observations = {row["sha256"]: row for row in memory.setdefault("observations", [])}
    for row in original:
        content = row.get("content")
        event_id = row.get("history_id")
        if row.get("role") == "user" and isinstance(content, str) and event_id not in kept_users and event_id not in known_events:
            memory["requests"].append({"text": content, "sha256": sha256(content.encode()).hexdigest(), "history_id": event_id})
            known_events.add(event_id)
        elif row.get("role") == "tool" and json_text(row) not in kept_tools:
            digest = sha256(json_text(row).encode()).hexdigest()
            # 每次按原历史顺序刷新遗漏观察；超过保留上限后，不能让
            # 被淘汰的旧观察反复挤掉下一条已见观察，造成窗口永不收敛。
            previous = observations.pop(digest, None)
            if previous is not None:
                observations[digest] = previous
                continue
            try:
                result = json.loads(content) if isinstance(content, str) else {}
            except ValueError:
                result = {}
            result = result if isinstance(result, dict) else {}
            observations[digest] = {
                "sha256": digest, "tool": str(row.get("tool_name") or "")[:200],
                "call_id": str(row.get("tool_call_id") or "")[:200],
                "status": str(result.get("status") or "unknown")[:80],
                "summary": str(result.get("summary") or result.get("error") or "")[:512],
                "result_handle": str(result.get("result_handle") or "")[:200],
                "details_omitted": True,
            }
    for row in memory["requests"]:
        if row.get("history_id") in event_order:
            row["history_order"] = event_order[row["history_id"]]
    # 无 ID 的旧来源不推断时序；已知事件按原始序号排列，与淘汰先后、正文无关。
    memory["requests"].sort(key=lambda row: event_order.get(row.get("history_id"), -1))
    memory["observations"] = list(observations.values())[-12:]
    if len(observations) > 12:
        memory["observations_omitted"] = True
    if any(row.get("history_before_truncated") for row in original) and not state.metadata.get("history_memory_version"):
        memory["legacy_history_incomplete"] = True
    return memory


def commit_conversation(
    state: ConversationState, conversation: Sequence[Mapping[str, Any]],
    updates: Sequence[StateUpdate] = (), *, receipt_metadata: Mapping[str, Any] | None = None,
) -> None:
    # Resolve the executor's positional boundary, not equal call IDs in old turns.
    execution = next((u for u in updates if type(u) is _ExecutionCapacity), None)
    start = execution.current_start if execution else None
    merged = merge_effect_receipts(conversation, state.metadata if receipt_metadata is None else receipt_metadata)
    for row in merged:
        if row.get("role") == "user" and not row.get("history_id"):
            row["history_id"] = secrets.token_hex(8)
    event_order = _user_event_order(state, merged, history_memory(state))
    for row in merged:
        if row.get("role") == "user":
            row["history_order"] = event_order[row["history_id"]]
    # 取消按钮可在同一generation的只读期间到达；迟到检查点不能复活旧预览。
    cancelled = {(row.get("effect_plan_id"), row.get("tool_call_id")): row["content"]
                 for row in state.conversation if row.get("role") == "tool" and row.get("effect_cancelled") is True}
    for row in merged:
        key = (row.get("effect_plan_id"), row.get("tool_call_id"))
        if row.get("role") == "tool" and key in cancelled:
            row.update(content=cancelled[key], effect_cancelled=True)
    if start is not None:
        user_ordinal = sum(row.get("role") == "user" for row in conversation[:start])
        users = [i for i, row in enumerate(merged) if row.get("role") == "user"]
        if start >= len(conversation) or conversation[start].get("role") != "user" or user_ordinal >= len(users):
            raise HistoryCapacityError("current execution boundary is unavailable")
        start = users[user_ordinal]
    retained = retain_conversation(merged, _required_from=start,
                                   _receipt_plan_id=execution.receipt_plan_id if execution else "")
    if retained != merged:
        memory = preserve_history(state, merged, retained)
        state.summary = json_text(memory)
        state.metadata["history_memory_version"] = 1
    state.conversation = retained


def merge_effect_receipts(
    conversation: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """按 plan ID 将已完成回执并入会话，不做文本近似匹配。"""
    merged = [deepcopy(dict(message)) for message in conversation]
    waits = metadata.get("effect_waits")
    if not isinstance(waits, Mapping):
        return merged

    for plan_id, record in waits.items():
        if not isinstance(plan_id, str) or not plan_id or not isinstance(record, Mapping):
            continue
        receipt = record.get("receipt_message")
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("completion_receipt_id") != plan_id
        ):
            continue

        final_result = record.get("final_result")
        if isinstance(final_result, Mapping):
            # The original native call and its late receipt are one observation.
            # Keeping accepted/running in the tool row while appending completed as
            # an assistant row gives the next model two contradictory authorities.
            for message in reversed(merged):
                if message.get("role") == "tool" and message.get("effect_plan_id") == plan_id:
                    message["content"] = json.dumps(dict(final_result), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    break

        matches = [
            index for index, message in enumerate(merged)
            if message.get("completion_receipt_id") == plan_id
        ]
        replacement = deepcopy(dict(receipt))
        if matches:
            # 确认后的模型答复可能已概述“仍在执行”。晚到终态必须位于它
            # 之后，否则公开投影会把更新后的系统回执当中间结果折叠掉。
            has_later_answer = any(
                message.get("role") == "user" or (
                    message.get("role") == "assistant"
                    and message.get("effect_plan_id") == plan_id
                    and not message.get("tool_name")
                )
                for message in merged[matches[0] + 1:]
            )
            if has_later_answer:
                for index in reversed(matches):
                    del merged[index]
                merged.append(replacement)
            else:
                merged[matches[0]] = replacement
                for index in reversed(matches[1:]):
                    del merged[index]
        else:
            merged.append(replacement)
    return merged
