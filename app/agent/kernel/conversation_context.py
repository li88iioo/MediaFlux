"""会话消息编解码、回复引用和能力召回线索。"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.errors import ToolPipelineError
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state_types import AgentInput

from . import telegram_reference
from .model import (
    ModelMessage,
    ModelToolCall,
)
from .projection import _arguments_safe

logger = logging.getLogger(__name__)

def contextual_message(agent_input: AgentInput) -> str:
    return with_reply_context(agent_input.message, agent_input.reply_context)


def with_reply_context(message: str, reply: object) -> str:
    reply_text = str(reply.get("text") or "").strip() if isinstance(reply, Mapping) else ""
    if not reply_text:
        return message
    return (
        f"{message}\n\n"
        "<reply_context purpose=reference_resolution>\n"
        f"{telegram_reference.preview(reply)}\n"
        "</reply_context>"
    )


def persisted_conversation(
    messages: Sequence[ModelMessage],
    *,
    current_user_index: int | None,
    original_message: str,
    reply_context: Mapping[str, Any] | None = None,
    prior_conversation: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    public_history: dict[tuple, list[dict[str, Any]]] = {}
    for prior in prior_conversation:
        if not isinstance(prior, Mapping):
            continue
        restored = dict(prior)
        if prior.get("role") == "user":
            restored["content"] = with_reply_context(str(prior.get("content") or ""), prior.get("reply_context"))
        if prior.get("history_before_truncated"):
            restored["content"] = "[更早的历史因存储预算已省略；缺少旧证据不代表之前未执行，必要时重新核验]\n" + str(restored.get("content") or "")
        key = tuple(str(restored.get(field) or "") for field in ("role", "content", "tool_call_id", "tool_name"))
        preserved = {key: prior[key] for key in ("content", "public_content", "candidate_result_ref") if isinstance(prior.get(key), str)}
        if prior.get("history_before_truncated"):
            preserved["history_before_truncated"] = True
        if isinstance(prior.get("activities"), list):
            preserved["activities"] = prior["activities"]
        if isinstance(prior.get("reply_context"), Mapping):
            preserved["reply_context"] = telegram_reference.context_copy(prior["reply_context"])
        public_history.setdefault(key, []).append(preserved)
    for index, message in enumerate(messages):
        item = message.to_dict()
        key = tuple(str(item.get(field) or "") for field in ("role", "content", "tool_call_id", "tool_name"))
        prior = public_history.get(key)
        if (current_user_index is None or index < current_user_index) and prior:
            item.update(prior.pop(0))
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            item["tool_calls"] = [
                dict(call) if call.get("argument_error") == "invalid_json" else
                {**dict(call), "arguments": _arguments_safe(call.get("arguments") or {})}
                for call in tool_calls
                if isinstance(call, Mapping)
            ]
        stored.append(item)
    if current_user_index is not None and 0 <= current_user_index < len(stored):
        stored[current_user_index] = {
            **stored[current_user_index],
            "content": original_message,
        }
        if reply_context and str(reply_context.get("text") or "").strip():
            stored[current_user_index]["reply_context"] = telegram_reference.context_copy(reply_context)
    return stored


def restore_messages(state: SessionState) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for item in state.conversation:
        if not isinstance(item, Mapping):
            continue
        try:
            restored = dict(item)
            if item.get("role") == "user":
                restored["content"] = with_reply_context(str(item.get("content") or ""), item.get("reply_context"))
                restored["history_id"] = item.get("history_id") or secrets.token_hex(8)
            if item.get("history_before_truncated"):
                restored["content"] = "[更早的历史因存储预算已省略；缺少旧证据不代表之前未执行，必要时重新核验]\n" + str(restored.get("content") or "")
            message = ModelMessage.from_dict(restored)
        except Exception as exc:  # noqa: BLE001 - isolate malformed persisted rows
            logger.warning("忽略无效 Agent 会话消息 type=%s", type(exc).__name__)
            continue
        if message.role in {"user", "assistant", "tool"}:
            messages.append(message)
    return messages


def tool_error_message(
    call: ModelToolCall, error: ToolPipelineError, *, input_schema: Mapping[str, Any] | None = None,
) -> ModelMessage:
    # 重述静态工具/已登记 operation 的真实契约，不回显调用参数，也不进入用户事件。
    schema = input_schema
    if schema is not None and error.argument_schemas:
        properties = schema.get("properties", {})
        schema = ({**schema, "properties": {**properties, **error.argument_schemas}}
                  if error.argument_schemas.keys() <= properties.keys() else None)
    feedback = ({"expected_input_schema": dict(schema)}
                if error.code == "invalid_arguments" and schema is not None else {})
    content = json.dumps(
        {
            **feedback,
            "ok": False,
            "status": "error",
            "code": error.code,
            "error": str(error),
            "instruction": "请根据错误修正参数、换用本轮其他工具，或向用户明确说明限制。",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ModelMessage(
        role="tool",
        content=content,
        tool_call_id=call.call_id,
        tool_name=call.name,
    )
