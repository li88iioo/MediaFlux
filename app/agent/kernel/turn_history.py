"""回合历史检查点与完整事实保存。"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.agent.errors import ToolPipelineError
from app.agent.kernel.state_types import (
    HistoryCapacityError,
    StalePublicationError,
    StateUpdate,
    _ExecutionCapacity,
)
from app.agent.public_safety import public_tool_label
from app.agent.public_view import public_result_state
from app.agent.schema import normalize_read_arguments

from .capabilities import ToolEffect
from .continuation import _ContinuationAction
from .conversation_codec import pending_content
from .conversation_context import persisted_conversation, restore_messages
from .model import (
    ModelMessage,
)
from .task_state import TaskPatch

if TYPE_CHECKING:
    from .turn import AgentTurn

logger = logging.getLogger(__name__)

def record_result(turn: AgentTurn,
    name: str, arguments: Mapping[str, Any], result: Mapping[str, Any],
    *, error: ToolPipelineError | None = None, rejected_call: str = "", progress: bool = True,
) -> None:
    tool = turn.session.catalog.get(name) if turn.session.catalog.has(name) else None
    if tool is not None:
        name = tool.name
        if tool.effect is ToolEffect.READ:
            # 恢复身份与执行共用只读归一化；不改模型原始调用及其协议历史。
            arguments = normalize_read_arguments(dict(arguments), tool.input_schema)
    # 显式参数重试按对象归并；无参工具依赖隐式上下文，切换后不能冒充同一对象恢复。
    if name != turn.last_business_tool:
        turn.context_revision += 1
    turn.last_business_tool = name
    attempt = (name, 0 if arguments else turn.context_revision,
               "unparsed:" + rejected_call if rejected_call else json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str))
    # 观察保留原 DTO；部分来源或失败不改造成回合异常。
    turn.has_tool_result = turn.has_tool_result or (error is None and result.get("ok") is not False)
    if error is None:
        # 取得业务回执说明本次参数已被接纳；旧语法拒绝不掩盖真实执行结果。
        # 已经开始执行的失败仍按对象保留，不因另一笔合法调用而消失。
        for previous, outcome in list(turn.unresolved_results.items()):
            if (previous[0] == name and outcome.get("status") == "invalid_arguments"
                    and outcome.get("execution_started") is not True):
                turn.unresolved_results.pop(previous)
    incomplete = error is not None or result.get("status") == "rate_limited" or public_result_state(result) in {"failed", "warning", "pending"}
    if progress and (error is None or error.code != "invalid_arguments" or error.execution_started is True):
        turn.progress_results.append((public_tool_label(name), {
            **result, "_tool_name": name, "_recovery_tracked": incomplete,
        }))
    if error is not None:
        turn.unresolved_results[attempt] = {**result, "status": error.code,
                                          "error": str(error), "execution_started": error.execution_started}
    elif incomplete:
        turn.unresolved_results[attempt] = {**result,
                                          "execution_started": (result.get("data") or {}).get("execution_started", True)}
    else:
        turn.unresolved_results.pop(attempt, None)


async def persist_conversation(turn: AgentTurn,
    *, close_pending: bool = False, preserve_capacity: bool = False,
    task_patch: TaskPatch | None = None,
    staged_messages: list[ModelMessage] | None = None,
    staged_completed: set[str] | None = None,
    wait_action: _ContinuationAction | None = None,
) -> None:
    lease = turn.lease
    assert lease is not None
    async with turn.publication_lock:
        checkpoint_messages = list(turn.messages if staged_messages is None else staged_messages)
        completed = turn.completed_call_ids if staged_completed is None else staged_completed
        if close_pending:
            for call in turn.active_calls:
                if call.call_id in completed:
                    continue
                started = call.call_id in turn.started_call_ids
                checkpoint_messages.append(
                    ModelMessage(
                        role="tool", content=pending_content(started),
                        tool_call_id=call.call_id, tool_name=call.name,
                    )
                )
        stored = persisted_conversation(
            checkpoint_messages,
            current_user_index=turn.current_user_index,
            original_message=turn.agent_input.message,
            reply_context=turn.agent_input.reply_context,
            prior_conversation=turn.state.conversation,
        )
        if turn.activities and stored and not stored[-1].get("tool_calls") and (
            stored[-1].get("role") == "assistant" or stored[-1].get("effect_plan_id")
        ):
            from app.agent.public_view import public_activities
            stored[-1]["activities"] = public_activities(list(turn.activities.values()))
        # The store enforces retention after receipt merge in its transaction.
        start = turn.current_user_index if turn.current_user_index is not None else next(
            (i for i in range(len(stored) - 1, -1, -1) if stored[i].get("role") == "user"), len(stored),
        )
        receipt_plan_id = ""
        if turn.diagnosing_effect:
            receipt_index = next((i for i in range(len(stored) - 1, -1, -1)
                                  if stored[i].get("completion_receipt_id")), None)
            if receipt_index is not None and all(
                turn.session.catalog.has(call["name"])
                and turn.session.catalog.get(call["name"]).effect is ToolEffect.READ
                for row in stored[receipt_index + 1:] for call in row.get("tool_calls", ())
            ):
                receipt_plan_id = stored[receipt_index]["completion_receipt_id"]
        try:
            extra_updates = ()
            if wait_action is not None:
                extra_updates = (wait_action,)
            elif task_patch is not None and task_patch.operation == "create" and turn.session.continuation_binding is not None:
                extra_updates = (_ContinuationAction("", {
                    "operation": "seed", "budget": turn.seed_budget(),
                }, binding=turn.session.continuation_binding),)
            # 失败确认后的诊断仍属于同一执行组，不能在续接前撤掉已有的有界容量。
            # 下一用户回合仍按正常规则释放；保留此阶段并不会提高容量上限。
            turn.state = await turn.session.state_store.commit(
                lease,
                updates=(_ExecutionCapacity(
                    "", turn.state.execution_capacity if preserve_capacity or turn.diagnosing_effect else close_pending and any(
                        call.call_id not in completed for call in turn.active_calls
                    ),
                    current_start=start if any(row.get("tool_calls") or row.get("reply_context", {}).get("reference_handle") for row in stored[start:]) else None,
                    receipt_plan_id=receipt_plan_id,
                ),) + extra_updates,
                conversation=stored,
                **({"task_patch": task_patch} if task_patch is not None else {}),
            )
            if task_patch is not None and not lease.continuation_id:
                if task_patch.operation == "create" and turn.session.continuation_binding is not None:
                    turn._budget_task_id = turn.state.metadata["task"]["task_id"]
                    turn._budget_paid_models = turn._budget_model_attempts
                    turn._budget_paid_tools = turn._budget_tool_attempts
                    turn._budget_paid_tokens = turn._budget_token_attempts
                    turn.sync_budget_context()
                elif task_patch.operation == "cancel":
                    turn._budget_task_id = ""
                    turn._local_budget = turn.seed_budget()
                    turn.sync_budget_context()
        except HistoryCapacityError as exc:
            raise ToolPipelineError(
                "当前结果超过历史容量，已保留此前检查点；已开始的调用结果可能未知，请勿重放写操作。",
                code="history_capacity_exceeded",
            ) from exc


async def remember_result(turn: AgentTurn,
    *,
    tool_name: str,
    content: str,
    public_content: str,
    candidate_result: Mapping[str, Any] | None = None,
    receipt_plan_id: str | None = None,
) -> bool:
    """先持久化真实执行结果，再沿原始任务继续，不能靠模型改写事实。"""

    lease = turn.tool_context.lease
    receipt_plan_id = receipt_plan_id or turn.plan_id
    safe_content = (content or "").strip()
    if not safe_content:
        return False
    conversation = [dict(item) for item in turn.state.conversation]
    # 完成原调用的结果，而非仅追加一条模型自述；旧预览仍保留在事件审计中。
    # 不新增虚构调用或孤立tool消息，保持各Provider的一问一答配对。
    for row in reversed(conversation):
        if row.get("role") == "tool" and row.get("effect_plan_id") == receipt_plan_id:
            row["content"] = safe_content
            break
    item = ModelMessage(
        role="assistant",
        content=(
            "已确认操作的可信系统结果（不是待执行计划）：\n"
            + safe_content
        ),
        tool_name=tool_name,
        effect_plan_id=receipt_plan_id or "",
        completion_receipt_id=receipt_plan_id or "",
    ).to_dict()
    safe_public_content = (public_content or "").strip()
    if safe_public_content:
        item["public_content"] = safe_public_content
    updates: tuple[StateUpdate, ...] = (
        _ExecutionCapacity("", turn.state.execution_capacity, receipt_plan_id=receipt_plan_id or ""),
    )
    if candidate_result:
        item["candidate_result_ref"] = candidate_result["ref"]
        updates += (
            StateUpdate("metadata.ux_candidate_result", dict(candidate_result)),
        )
    conversation.append(item)
    try:
        from app.agent.kernel.completion_receipts import (
            acknowledge_effect_receipt,
            remember_effect_receipt,
        )

        await remember_effect_receipt(turn.session.state_store, owner=lease.owner,
            session_id=lease.session_id, plan_id=receipt_plan_id, message=item)
        turn.state = await turn.session.state_store.commit(
            lease,
            conversation=conversation,
            updates=updates,
        )
        await acknowledge_effect_receipt(turn.session.state_store, owner=lease.owner,
            session_id=lease.session_id, plan_id=receipt_plan_id)
        turn.messages = restore_messages(turn.state)
        return True
    except StalePublicationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 已执行副作用不得被状态写回遮蔽
        logger.warning(
            "Agent 确认结果写回会话失败 type=%s", type(exc).__name__
        )
        return False


async def preserve_checkpoint(turn: AgentTurn) -> None:
    if not turn.checkpoint_ready:
        return
    try:
        await persist_conversation(turn, close_pending=True)
    except StalePublicationError:
        return
    except Exception as exc:  # noqa: BLE001 - 故障收尾不能遮蔽原始错误
        logger.warning(
            "Agent 故障检查点写回失败 type=%s", type(exc).__name__
        )
