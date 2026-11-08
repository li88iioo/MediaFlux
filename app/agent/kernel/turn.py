"""一次输入、确认或恢复的准入、发布与终态收束。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Mapping
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel.conversation_state import historical_user_sources
from app.agent.kernel.session_state import (
    CandidateSelectionGuard,
    PendingEffectGuard,
    SessionState,
)
from app.agent.kernel.state_types import (
    AgentInput,
    CancellationToken,
    PublicationLease,
    SelectionInvalidError,
    SessionBusyError,
    StalePublicationError,
    _CancelledReadInline,
    _PublicationFinished,
    _PublicationUnprotected,
)
from app.agent.kernel.tool_calls import PipelineResult, ToolCallContext
from app.agent.public_safety import public_tool_label, sanitize_public_text
from app.agent.public_view import (
    format_partial_progress,
    format_public_result,
    public_activity,
    public_result_state,
    sanitize_confirmed_answer,
)
from app.sensitive_data import contains_sensitive_credential

from . import telegram_reference
from .adapters import TurnViewBuilder
from .capabilities import KernelToolSpec, ToolEffect
from .continuation import (
    _ContinuationAction, active_remaining, continuation_record, record_budget, reserve_budget_values,
)
from .conversation_context import (
    contextual_message,
    restore_messages,
    tool_error_message,
)
from .tool_calls import EffectPlan
from .events import TERMINAL_EVENT_TYPES, AgentEvent, AgentEventType, EventFactory
from .model import (
    BudgetedModelAdapter,
    ModelMessage,
    ModelToolCall,
    ProviderState,
)
from .provider_model import ModelProviderError, UncommittableModelTurn
from .session_guard import foreground_execution_guard, session_scope_guard
from .task_state import TaskPatch, task_projection

if TYPE_CHECKING:
    from .session import AgentSession
from .model_rounds import model_loop
from .turn_history import persist_conversation, preserve_checkpoint, record_result, remember_result

logger = logging.getLogger(__name__)

def _provider_failure_message(exc: ModelProviderError) -> str:
    """把 Provider 内部故障归一为不泄露配置的用户提示。"""

    if isinstance(exc, UncommittableModelTurn) and exc.reason == "unreplayable_arguments":
        return "模型工具参数无法解析，本次生成的调用未执行；请稍后重试。"
    diagnostic = exc.failure
    if diagnostic is not None:
        if diagnostic.emitted and diagnostic.kind in {
            "connect_timeout", "read_timeout", "write_timeout", "pool_timeout",
            "connection_error", "transport_error", "deadline",
        }:
            return "模型回复传输中断，本轮未完整生成；请稍后重试。"
        if diagnostic.kind in {"read_timeout", "write_timeout", "pool_timeout", "deadline"}:
            return "模型服务响应超时，请稍后重试。"
        if diagnostic.kind in {"connect_timeout", "connection_error"}:
            return "无法连接模型服务，请检查网络或模型路由后重试。"
    reason = str(exc or "")
    if "完成事件前中断" in reason or "未完整结束" in reason:
        return "模型回复未完整生成，请重试；不要把截断内容视为完成结果。"
    if "超时" in reason:
        return "模型服务响应超时，请稍后重试。"
    if "HTTP 429" in reason:
        return "模型服务请求过于频繁，请稍后重试。"
    if "HTTP 401" in reason or "HTTP 403" in reason:
        return "模型服务鉴权失败，请检查模型路由配置。"
    return "模型服务暂时不可用，请稍后重试。"


class AgentTurn:
    """一次用户输入/确认/恢复的执行器；状态不挂在共享AgentSession上。"""

    state: SessionState
    messages: list[ModelMessage]
    last_user: dict[str, Any] | None
    tool_context: ToolCallContext

    def __init__(self, session: AgentSession, agent_input: AgentInput,
                 queue: asyncio.Queue[AgentEvent | None], *, plan_id: str | None = None,
                 explain_plan_id: str | None = None,
                 resume_context: tuple[PublicationLease, SessionState, Mapping[str, Any]] | None = None) -> None:
        self.session = session
        self.agent_input = agent_input
        self.request_time = datetime.now().astimezone().isoformat(timespec="seconds")
        self.queue = queue
        self.plan_id = plan_id
        self.explain_plan_id = explain_plan_id
        self.resume_context = resume_context
        self.lease: PublicationLease | None = None
        self.token: CancellationToken | None = None
        self.factory: EventFactory | None = None
        self.checkpoint_ready = False
        self.scope = ExitStack()
        self.execution_scope = ExitStack()
        self.confirming = self.plan_id is not None
        self.explaining = self.explain_plan_id is not None
        self.explanation_plan: EffectPlan | None = None
        self.selected_tools: tuple[KernelToolSpec, ...] = ()
        self.selection_tool: KernelToolSpec | None = None
        self.model_calls = 0
        self.round_index = 0
        self._budget_task_id = ""
        self._budget_model_attempts = self._budget_tool_attempts = 0
        self._budget_paid_models = self._budget_paid_tools = 0
        self._budget_token_attempts = self._budget_paid_tokens = 0
        self._local_budget = session.limits.budget()
        self.confirmed_result: dict[str, Any] | None = None
        self.diagnosing_effect = False
        self.reobservation_source = None
        self.active_calls: tuple[ModelToolCall, ...] = ()
        self.completed_call_ids: set[str] = set()
        self.started_call_ids: dict[str, None] = {}
        self.returned_reads: dict[str, _CancelledReadInline] = {}
        self.parallel_call_ids: set[str] = set()
        self.publication_lock = asyncio.Lock()
        self.progress_results: list[tuple[str, dict[str, Any]]] = []
        self.activities: dict[str, dict[str, str]] = {}
        self.unresolved_results: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.user_memory = ""
        self.last_business_tool = ""
        self.context_revision = 0
        self.has_tool_result = False
        self.attempted_write = self.confirming
        self.resume_read_done = False
        self.is_effect_resume = self.resume_context is not None and self.resume_context[0].continuation_kind == "effect"
        self.batch_resuming = bool(self.resume_context is not None and self.is_effect_resume
                                   and self.resume_context[2]["origin"].get("batch_plan"))
        self.is_read_resume = self.resume_context is not None and not self.is_effect_resume
        self.continuation_view = TurnViewBuilder()

    async def run(self) -> None:
        with self.session.metrics.measure(
            channel=self.agent_input.channel, request_id=self.agent_input.request_id,
        ) as self.measurement:
            try:
                await self.admit()
                assert self.lease is not None
                await self.publish(AgentEventType.TURN_STARTED, {
                    "channel": self.agent_input.channel,
                    "generation": self.lease.generation,
                    "stream_display_enabled": (self.session.stream_display()) if self.session.stream_display else True,
                    **({"kind": "confirmation"} if self.plan_id is not None else {}),
                })
                if self.sensitive_input:
                    await self.publish(AgentEventType.TURN_FAILED, {
                        "code": "sensitive_input", "message": "消息包含疑似凭据，未发送给模型。",
                    })
                    return
                self.prepare_context()
                if not self.confirming:
                    await self.reserve_budget()
                if self.is_effect_resume and not await self.resume_effect():
                    return
                if self.plan_id is not None and not await self.confirm_effect():
                    return
                await self.prepare_tools()
                if self.validated_selection is not None:
                    await self.preview_selection()
                    return
                self.prepare_model()
                await model_loop(self)
            except (asyncio.CancelledError, StalePublicationError) as exc:
                await preserve_checkpoint(self)
                settlement = {}
                completed_reads = []
                for call_id, receipt in self.returned_reads.items():
                    if receipt._content is None:
                        continue
                    if call_id in self.parallel_call_ids:
                        # 前面的并发结果可能已按顺序落盘；只迁移同一调用尚未结算的槽，
                        # 绑定的是本执行器最后一次成功CAS的检查点，不读新用户的历史。
                        receipt = receipt.at_checkpoint(self.state.conversation)
                        if receipt is None:
                            continue
                    elif call_id != next(reversed(self.started_call_ids), None):
                        continue
                    completed_reads.append(receipt)
                if completed_reads:
                    try:
                        settled = await self.session.state_store.settle_cancelled_reads(tuple(completed_reads))
                    except Exception as settlement_error:  # noqa: BLE001 - failed transaction leaves outcome unknown
                        logger.warning("Cancelled READ settlement rejected type=%s", type(settlement_error).__name__)
                        settled = False
                    settlement = {"read_settlement": "settled" if settled else "rejected"}
                if self.factory is None:
                    # 准入被撤销也属于取消；尚无发布权，不伪造会话事件。
                    raise asyncio.CancelledError(str(exc)) from exc
                event = self.factory.create(
                    AgentEventType.TURN_CANCELLED,
                    {"reason": str(exc) or (self.token.reason if self.token else "cancelled"), **settlement, **await self.terminal_task("cancelled")},
                )
                assert self.lease is not None
                if self.session.journal is not None:
                    try:
                        await self.session.journal.append_for_turn(event, self.lease)
                    except StalePublicationError:
                        pass  # 旧消费者仍收取消终态，但不得写入新会话日志。
                if self.lease is not None and self.lease.continuation_id:
                    try:
                        await self.record_continuation_event(event)
                    except (StalePublicationError, SessionBusyError):
                        pass  # A newer user owns the session; never publish an old delivery into it.
                self.emit(event)
            except SelectionInvalidError as exc:
                failure_factory = EventFactory(
                    session_id=self.agent_input.session_id,
                    turn_id=secrets.token_urlsafe(12), request_id=self.agent_input.request_id,
                )
                # 拒绝发生在 begin_turn 前；不记会话事件，不污染现有确认/历史。
                self.emit(failure_factory.create(
                    AgentEventType.TURN_FAILED,
                    {"code": "selection_invalid", "message": str(exc), "turn_status": "failed", "task": None},
                ))
            except SessionBusyError:
                await self.failure("effect_in_progress", "已确认的写操作正在执行，请等待完成后再继续。")
            except ToolPipelineError as exc:
                await self.failure(exc.code, str(exc))
            except ModelProviderError as exc:
                logger.warning("Agent model provider failed type=%s diagnostic=%s", type(exc).__name__, exc.failure)
                await self.failure("model_provider_error", _provider_failure_message(exc))
            except Exception as exc:  # noqa: BLE001 - final turn fault boundary
                logger.error("Agent turn failed type=%s", type(exc).__name__)
                await self.failure("internal_error", "Agent 运行失败")
            finally:
                try:
                    self.scope.close()
                    if self.lease is not None and self.token is not None:
                        try:
                            if hasattr(self, "tool_context"):
                                await self.reserve_budget(stop=True)
                            if (not self.lease.continuation_id
                                    or continuation_record(self.state, self.lease.continuation_id, self.lease.continuation_kind).get("state") in {"claimed", "running"}):
                                update = _ContinuationAction("", {
                                    "operation": "finish", "reason": "turn_ended",
                                }, binding=self.session.continuation_binding) if self.lease.continuation_id else _PublicationFinished("", None)
                                await self.session.state_store.commit(self.lease, updates=(update,))
                        except (StalePublicationError, SessionBusyError):
                            pass
                        finally:
                            await self.session.coordinator.finish(self.lease, self.token)
                finally:
                    self.execution_scope.close()

    async def admit(self) -> None:
        self.validated_selection = None
        self.candidate_context = None
        self.selection_tool = None
        selection = self.session.pipeline.selection
        if selection is not None:
            self.validated_selection, self.candidate_context, self.selection_tool = await selection.admit(
                self.agent_input, state_store=self.session.state_store,
                reference_store=self.session.pipeline.reference_store, catalog=self.session.catalog)
        elif "selection" in self.agent_input.metadata or self.agent_input.metadata.get("candidate_context"):
            raise ToolPipelineError("候选预览能力暂不可用", code="selection_unavailable")
        self.contextual_message = contextual_message(self.agent_input)
        self.sensitive_input = contains_sensitive_credential(self.agent_input.message) or contains_sensitive_credential(str(self.agent_input.reply_context.get("text") or ""))
        self.admission_token = await self.session.turn_admission.begin(self.agent_input)
        if self.resume_context is not None:
            self.lease, self.state, _ = self.resume_context
            if not await self.session.state_store.is_current(self.lease):
                raise StalePublicationError("read wait revoked before start")
            if self.batch_resuming:
                self.scope.enter_context(session_scope_guard(self.agent_input.owner, self.agent_input.session_id, kind="effect"))
            self.token = await self.session.coordinator.begin(self.lease, protected=self.batch_resuming, task=asyncio.current_task())
            self.messages = restore_messages(self.state)
            self.current_user_index = None
            self.checkpoint_ready = True
        elif self.plan_id is not None:
            self.scope.enter_context(session_scope_guard(self.agent_input.owner, self.agent_input.session_id, kind="effect"))
            async with self.session._start_lock:
                self.state = await self.session.state_store.load(owner=self.agent_input.owner, session_id=self.agent_input.session_id)
                # 无效/重复点击没有回合所有权，不能先抢占正在运行的续行再拒绝。
                if not self.plan_id or self.state.pending_effect_plan_id != self.plan_id:
                    raise ConfirmationClaimError()
                self.lease = PublicationLease(self.agent_input.owner, self.agent_input.session_id, self.state.generation,
                                         secrets.token_urlsafe(12), self.agent_input.request_id)
                pending_plan = await self.session.pipeline.get_confirmation_evidence(
                    owner=self.lease.owner, session_id=self.lease.session_id, scoped=True,
                )
                if pending_plan is None:
                    await self.session.pipeline._cancel_effect_scoped(self.plan_id, lease=self.lease)
                    raise ConfirmationClaimError()
                if not self.execution_scope.enter_context(foreground_execution_guard(
                    self.lease.owner, self.lease.session_id, self.state.lifecycle_id, self.lease.generation,
                    turn_id=self.lease.turn_id,
                )):
                    raise SessionBusyError("confirmation publication is already executing")
                self.reobservation_source = pending_plan.metadata.get("reobservation_source")
                self.token = await self.session.coordinator.begin(self.lease, protected=True, task=asyncio.current_task())
            self.messages = restore_messages(self.state)
            plan_start = next((i for i, row in enumerate(self.state.conversation) if row.get("effect_plan_id") == self.plan_id), len(self.state.conversation))
            self.last_user = next((row for row in reversed(self.state.conversation[:plan_start]) if row.get("role") == "user"), None)
            if "pending_effect_goal" in self.state.metadata:
                self.last_user = self.state.metadata["pending_effect_goal"] or None
            if self.last_user:
                self.agent_input = replace(self.agent_input, message=str(self.last_user.get("content") or self.agent_input.message),
                                      reply_context=self.last_user.get("reply_context") or {})
                self.contextual_message = contextual_message(self.agent_input)
            self.current_user_index = None
        else:
            with session_scope_guard(self.agent_input.owner, self.agent_input.session_id):
                async with self.session._start_lock:
                    if await self.session.coordinator.has_protected_turn(
                        owner=self.agent_input.owner,
                        session_id=self.agent_input.session_id,
                    ):
                        raise SessionBusyError("confirmed effect is executing")
                    pending_guard: PendingEffectGuard | None = None
                    selection_guard: CandidateSelectionGuard | None = None
                    pending_state = await self.session.state_store.load(owner=self.agent_input.owner, session_id=self.agent_input.session_id)
                    if self.validated_selection is not None:
                        self.validated_selection.guard.check(pending_state)
                    elif self.candidate_context is not None:
                        self.candidate_context.guard.check(pending_state)
                    pending_plan = await self.session.pipeline.get_pending_plan(
                        owner=self.agent_input.owner, session_id=self.agent_input.session_id,
                    ) if pending_state.pending_effect_plan_id else None
                    if self.explaining:
                        if pending_plan is None or pending_plan.plan_id != self.explain_plan_id:
                            raise ConfirmationClaimError()
                        self.explanation_plan = pending_plan
                    if pending_plan is not None:
                        pending_guard = self.session.pipeline.pending_guard(pending_state, pending_plan)
                    elif pending_state.pending_effect_plan_id:
                        await self.session.pipeline._cancel_effect_scoped(
                            pending_state.pending_effect_plan_id,
                            lease=PublicationLease(self.agent_input.owner, self.agent_input.session_id,
                                pending_state.generation, secrets.token_urlsafe(12), self.agent_input.request_id),
                        )
                    if self.validated_selection is not None:
                        selection_guard = self.validated_selection.guard
                    elif self.candidate_context is not None:
                        selection_guard = self.candidate_context.guard
                    self.lease, self.state = await self.session.state_store.begin_turn(
                        owner=self.agent_input.owner,
                        session_id=self.agent_input.session_id,
                        request_id=self.agent_input.request_id,
                        pending_guard=pending_guard,
                        selection_guard=selection_guard,
                    )
                    if not self.execution_scope.enter_context(foreground_execution_guard(
                        self.lease.owner, self.lease.session_id, self.state.lifecycle_id, self.lease.generation,
                    )):
                        raise SessionBusyError("foreground publication is already executing")
                    self.token = await self.session.coordinator.begin(self.lease, task=asyncio.current_task())
                    if not self.sensitive_input and self.agent_input.reply_context:
                        self.agent_input = replace(self.agent_input, reply_context=await telegram_reference.prepare(
                            self.agent_input.reply_context, self.lease, self.session.pipeline.reference_store,
                        ))
                        self.contextual_message = contextual_message(self.agent_input)
                    if self.candidate_context is not None:
                        self.contextual_message += "\n已校验的回复候选批次，仅可选择这些真实 result_ids：" + json.dumps(
                            [item["result_id"] for item in (self.state.metadata.get("ux_candidate_view") or {}).get("items", [])], ensure_ascii=False)
                    self.messages = restore_messages(self.state)
                    self.current_user_index = len(self.messages)
                    self.messages.append(ModelMessage(role="user", content=self.contextual_message, history_id=secrets.token_hex(8)))
                    # 输入接纳和持久化必须位于同一个 start window，不能等事件发布：
                    # journal/observer 可能挂起，此时下一条追问已取得新 generation。
                    # 这里只写通过凭据检测的用户输入，迟到的模型/工具仍不得提交。
                    if not self.sensitive_input:
                        self.checkpoint_ready = True
                        try:
                            await persist_conversation(self, preserve_capacity=True)
                        except ValueError as exc:
                            if self.agent_input.reply_context.get("reference_handle"):
                                raise ToolPipelineError("引用超过会话持久化容量，未发送给模型。", code="reference_capacity_exceeded") from exc
                            raise
        self.factory = EventFactory(
            session_id=self.agent_input.session_id,
            turn_id=self.lease.turn_id,
            request_id=self.agent_input.request_id,
        )

    def prepare_context(self) -> None:
        assert self.lease is not None and self.token is not None
        self.selected_model = (
            self.session.model_selector(self.agent_input, self.session.model)
            if self.session.model_selector is not None else ""
        )
        self.tool_context = ToolCallContext(
            owner=self.agent_input.owner,
            session_id=self.agent_input.session_id,
            request_id=self.agent_input.request_id,
            turn_id=self.lease.turn_id,
            lease=self.lease,
            cancellation=self.token,
            report_progress=self.report_progress,
            task_control=self.control_task, task_budget=self.adopt_task_budget,
            wait_for_completion=True,
            read_only=bool(self.agent_input.initial_read_tool),
            channel=self.agent_input.channel,
            continuation_binding=self.session.continuation_binding,
            delivery_route=self.agent_input.delivery_route,
            continuation_policy={"model_policy": self.selected_model,
                "model_limit": self.session.limits.max_model_rounds, "tool_limit": self.session.limits.max_tool_calls,
                "budget": self.task_budget or self._local_budget},
            selection_arguments=self.validated_selection.arguments if self.validated_selection else None,
            resource_candidate_ref=self.candidate_context.guard.ref if self.candidate_context else "",
            # Capture the admitted/restored task before either stale refresh or
            # the normal tool loop; later explanation rows are not task goals.
            user_source=self.agent_input.message,
            user_history=historical_user_sources(self.state),
            user_reply_context=telegram_reference.context_copy(self.agent_input.reply_context),
        )

    async def resume_effect(self) -> bool:
        """恢复已确认后台副作用；停止或回执失败就在本阶段终结。"""
        from .continuation import confirmed_result as restore_confirmed
        lease = self.tool_context.lease
        result = restore_confirmed(self.session.catalog, self.state, continuation_record(self.state, lease.continuation_id, lease.continuation_kind))
        if self.batch_resuming:
            from .batch_effects import resume_batch
            progress = self.state.metadata["effect_waits"][lease.continuation_id].get("batch_progress")
            result = await resume_batch(self.session.pipeline, result, self.tool_context, progress)
            plan = result.effect_plan
            assert plan is not None
            self.state = await self.session.state_store.load(owner=lease.owner, session_id=lease.session_id)
            saved = await remember_result(self, tool_name=result.tool.name, content=result.outcome.model_message(),
                public_content=format_public_result(result.outcome.public_content), receipt_plan_id=plan.plan_id)
            await self.publish(AgentEventType.EFFECT_COMPLETED if result.outcome.public_content.get("ok") else AgentEventType.EFFECT_FAILED,
                {"plan_id": plan.plan_id, "tool": result.tool.name,
                 "result": result.outcome.audit_result(), "receipt": format_public_result(result.outcome.public_content)})
            self.state = await self.session.state_store.commit(lease, updates=(_PublicationUnprotected("", None),))
            self.scope.close()
            await self.session.coordinator.unprotect(lease, self.tool_context.cancellation)
            if not saved:
                await self.failure("receipt_unavailable", "已确认组合的结果保存失败，请核对原操作，勿重复执行。")
                return False
            stop_state = await self.session.state_store.load(owner=lease.owner, session_id=lease.session_id)
            if stop_state.metadata.get("stop_requested_generation") == lease.generation:
                await self.publish(AgentEventType.TURN_CANCELLED, {"reason": "已停止后续步骤；已完成项以原回执为准。"})
                return False
        self.confirmed_result = dict(result.outcome.public_content)
        self.diagnosing_effect = public_result_state(self.confirmed_result) in {"failed", "warning", "pending"}
        self.tool_context = replace(self.tool_context, confirmed_effect=result,
            diagnostic_only=self.diagnosing_effect,
            reobservation_source=(result.effect_plan.metadata.get("reobservation_source")
                if self.diagnosing_effect and result.effect_plan is not None else None))
        return True

    async def confirm_effect(self, *, scoped_trust: bool = False) -> bool:
        """确认只执行一次，保存真实回执后再进入只读诊断/后续规划。"""
        plan_id = self.plan_id
        assert plan_id is not None
        lease = self.lease
        assert lease is not None
        plan = await self.session.pipeline.get_confirmation_evidence(
            owner=self.agent_input.owner, session_id=self.agent_input.session_id, scoped=True,
        )
        if plan is not None and plan.plan_id == plan_id and (frozen := plan.metadata.get("continuation_budget")):
            if not lease.continuation_id:
                self._budget_task_id = frozen.get("task_id", "")
                self._local_budget = deepcopy(frozen["budget"])
            self._budget_paid_models = self._budget_model_attempts
            self._budget_paid_tools = self._budget_tool_attempts
            self._budget_paid_tokens = self._budget_token_attempts
        await self.reserve_budget()
        public_result: dict[str, Any]
        result = None
        try:
            if scoped_trust:
                result = await self.session.pipeline.execute_confirmed(plan_id, context=self.tool_context, scoped_trust=True)
            else:
                result = await self.session.pipeline.execute_confirmed(plan_id, context=self.tool_context)
            public_result = dict(result.outcome.public_content)
        except (asyncio.CancelledError, StalePublicationError):
            raise
        except ConfirmationClaimError as exc:
            await self.failure(exc.code, str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - 写后未知状态必须落盘，禁止伪装成未执行
            public_result = {"ok": False, "status": exc.code if isinstance(exc, ToolPipelineError) else "internal_error",
                             "summary": str(exc) if isinstance(exc, ToolPipelineError) else "确认执行状态未知，请先查询真实业务状态再决定是否重试。"}
            if isinstance(exc, ToolPipelineError) and exc.execution_started is not None:
                public_result["data"] = {"execution_started": exc.execution_started}
        receipt_saved = await remember_result(self,
            tool_name=result.tool.name if result else "confirmed_effect",
            content=result.outcome.model_message() if result else json.dumps(public_result, ensure_ascii=False),
            public_content=format_public_result(public_result),
            candidate_result=self.session.pipeline.selection.confirmed_result(result, public_result)
                if self.session.pipeline.selection is not None else None,
        )
        failed = public_result.get("ok") is False
        stopped = public_result.get("status") in {"stopped", "cancelled"}
        await self.publish(AgentEventType.EFFECT_FAILED if failed else AgentEventType.EFFECT_COMPLETED, {
            "plan_id": self.plan_id, "tool": result.tool.name if result else "confirmed_effect", "result": result.outcome.audit_result() if result else public_result,
            "receipt": format_public_result(public_result),
            "code": str(public_result.get("status") or "effect_failed") if failed else "",
            "message": str(public_result.get("error") or public_result.get("summary") or "执行未完成") if failed else "",
            "elapsed_ms": result.elapsed_ms if result else 0,
            "scoped_trust": self.session.pipeline.scoped_trust.view(self.state),
        })
        # Consume the foreground continuation while the confirmation still owns
        # the session guard; a completion poll must not start a competing model loop.
        if receipt_saved and self.last_user:
            from .continuation import adopt_foreground
            adopted = await adopt_foreground(self.session.state_store, self.tool_context.lease, plan_id)
            if adopted is not None:
                self.lease = adopted
                self.state = await self.session.state_store.load(owner=adopted.owner, session_id=adopted.session_id)
                self.tool_context = replace(self.tool_context, lease=adopted)
                # 新租约已继承冻结预算；本地已入账尝试只防止重复扣减，不清零原账。
                self._budget_task_id = ""
                self._budget_paid_models = self._budget_model_attempts
                self._budget_paid_tools = self._budget_tool_attempts
                self._budget_paid_tokens = self._budget_token_attempts
                self.sync_budget_context()
                if result is None:
                    from .continuation import confirmed_result as restore_confirmed
                    result = restore_confirmed(self.session.catalog, self.state, continuation_record(self.state, adopted.continuation_id, adopted.continuation_kind))
        # 写入及回执已完成：先解除保护，再读停止标记，关闭两者之间的竞态窗口。
        lease = self.tool_context.lease
        self.state = await self.session.state_store.commit(lease, updates=(_PublicationUnprotected("", None),))
        self.scope.close()
        await self.session.coordinator.unprotect(lease, self.tool_context.cancellation)
        stop_state = await self.session.state_store.load(owner=lease.owner, session_id=lease.session_id)
        if stop_state.metadata.get("stop_requested_generation") == lease.generation:
            await self.publish(AgentEventType.TURN_CANCELLED, {
                "reason": "已停止后续会话；已提交操作以执行回执和后台任务状态为准。",
            })
            return False
        if failed and (not receipt_saved or not self.last_user or stopped or public_result.get("status") in {
                "authorization_denied", "agent_disabled", "confirmation_invalid",
                "confirmation_not_supported", "tool_not_found"}):
            if result is not None:
                self.confirmed_result = public_result
                self.checkpoint_ready = receipt_saved
            await self.failure(str(public_result.get("status") or "effect_failed"), str(public_result.get("summary") or "执行未完成"))
            return False
        if not receipt_saved:
            self.confirmed_result = public_result
            await self.failure("receipt_unavailable", "执行结果已取得，但会话记录保存失败，未继续后续步骤；请先核对任务状态。")
            return False
        if plan is not None and plan.plan_id == plan_id:
            # 执行回执已有专门的事件与正文位置，不再混入后续查询重复展示。
            # 计划参数可被 validator/预检扩充；结清准入拒绝必须回到该计划的原调用。
            arguments = plan.arguments
            for index, message in enumerate(self.messages):
                if message.role == "tool" and message.effect_plan_id == plan_id:
                    original = next((call for previous in reversed(self.messages[:index])
                                     for call in previous.tool_calls if call.call_id == message.tool_call_id), None)
                    if original is not None and original.arguments is not None:
                        arguments = original.arguments
                    break
            record_result(self, plan.tool_name, arguments, public_result, progress=False)
        background = (public_result.get("data") or {}).get("background_job") or {}
        if background.get("followup_pending") and str(background.get("status") or "") not in {
                "completed", "partial", "failed", "cancelled", "manual_review", "stopped"}:
            await self.publish(AgentEventType.TURN_COMPLETED, {
                "status": "waiting", "answer": format_public_result(public_result),
                "finish_reason": "effect_pending", "usage": {}, "model_calls": 0, "tool_calls": 0,
            })
            return False
        result_state = public_result_state(public_result)
        if not self.last_user or stopped:
            await self.publish(AgentEventType.TURN_COMPLETED, {
                "status": "partial" if not stopped and result_state in {"failed", "warning"} else "success",
                "answer": format_public_result(public_result),
                "finish_reason": f"effect_{result_state}",
                "usage": {},
                "model_calls": 0,
                "tool_calls": 0,
            })
            return False
        self.confirmed_result = public_result
        self.diagnosing_effect = result_state in {"failed", "warning", "pending"}
        self.tool_context = replace(self.tool_context, lease=lease, confirmed_effect=result,
            diagnostic_only=self.diagnosing_effect,
            reobservation_source=self.reobservation_source if self.diagnosing_effect else None)
        self.confirming = False
        self.admission_token = await self.session.turn_admission.begin(self.agent_input)
        self.checkpoint_ready = True
        return True

    async def prepare_tools(self) -> None:
        lease = self.tool_context.lease
        if self.session.user_memory is not None and self.agent_input.owner:
            try:
                self.user_memory = (await asyncio.to_thread(self.session.user_memory, self.agent_input.owner)).strip()
            except Exception as exc:  # noqa: BLE001 - 记忆不可用不能阻断回合
                logger.warning("用户长期记忆读取失败 type=%s", type(exc).__name__)
        reference_kinds = tuple(self.state.ref_kinds | ({"telegram_reference"} if self.agent_input.reply_context.get("reference_handle") or any(row.get("reply_context", {}).get("reference_handle") for row in self.state.conversation) else set()))
        self.model_context = {
            "owner": self.agent_input.owner, "session_id": self.agent_input.session_id,
            "request_id": self.agent_input.request_id, "channel": self.agent_input.channel,
            "generation": lease.generation, "turn_id": lease.turn_id,
            "has_task": bool(self.state.metadata.get("task")),
            "has_pending_effect": bool(self.state.pending_effect_plan_id),
            "reference_kinds": reference_kinds,
        }
        self.refresh_model_tools()
        self.pending_initial_read = None
        if self.agent_input.initial_read_tool:
            initial_tool = self.session.catalog.get(self.agent_input.initial_read_tool)
            if initial_tool.effect is not ToolEffect.READ:
                raise ToolPipelineError("工作区行动必须是只读能力。", code="read_handoff_invalid")
            self.pending_initial_read = ModelToolCall(secrets.token_urlsafe(12), initial_tool.model_name,
                                                     dict(self.agent_input.initial_read_arguments))
        source = self.tool_context.reobservation_source
        source_read = source.get("read") if isinstance(source, Mapping) and source.get("available") is True else None
        source_name = source_read.get("tool") if isinstance(source_read, Mapping) else None
        source_spec = self.session.catalog.get(source_name) if isinstance(source_name, str) and self.session.catalog.has(source_name) else None
        if not (self.diagnosing_effect and source_spec is not None and source_spec.effect is ToolEffect.READ
                and "reobserve" in source_spec.input_schema.get("properties", {}).get("operation", {}).get("enum", ())):
            source_spec = None
        self.pending_resume_call = None
        if self.resume_context is not None:
            from .continuation import history_call
            record = continuation_record(self.state, lease.continuation_id, lease.continuation_kind)
            if record.get("anchor"):
                self.pending_resume_call = history_call(self.state, record)
                self.resumed_tool = self.session.catalog.get(record["anchor"]["tool"])
                if self.resumed_tool.effect is not ToolEffect.READ or self.resumed_tool.name.startswith("agent."):
                    raise ToolPipelineError("等待能力不可恢复。", code="read_resume_invalid")
        await self.publish(
            AgentEventType.CAPABILITIES_SELECTED,
            {
                "tools": [tool.name for tool in self.selected_tools],
                "count": len(self.selected_tools),
            },
        )
        if (self.pending_resume_call is None and self.tool_context.diagnostic_only
                and source_spec is not None):
            # 可信旧计划已保存原观察范围；重读不是新写入，不要求模型再征求许可。
            # 共用READ的准入、预算、检查点和取消链，不重放旧写操作。
            self.resumed_tool = source_spec
            self.pending_resume_call = {"arguments": {"operation": "reobserve"}}
        self.total_tool_calls = 0
        self.business_tool_calls = 0
        self.write_tool_calls = 0
        self.total_usage: dict[str, int] = {}
        self.observed_reads: set[tuple[str, str]] = set()

    def refresh_model_tools(self) -> None:
        self.model_context.update(reference_kinds=tuple(self.state.ref_kinds),
                                  has_task=bool(self.state.metadata.get("task")),
                                  has_pending_effect=bool(self.state.pending_effect_plan_id))
        tools = () if self.explaining else self.session.visible_model_tools(self.model_context)
        self.selected_tools = tuple(tool for tool in tools if not self.agent_input.initial_read_tool or tool.effect is ToolEffect.READ)
        self.available_names = {name for tool in self.selected_tools for name in (tool.name, tool.model_name)}
        self.tool_definitions = tuple(tool.model_definition() for tool in self.selected_tools)

    async def preview_selection(self) -> None:
        selection, tool = self.validated_selection, self.selection_tool
        assert selection is not None and tool is not None
        call = ModelToolCall(
            call_id=f"selection_{self.tool_context.lease.turn_id}", name=tool.name,
            arguments=dict(selection.arguments),
        )
        await self.publish_tool(
            AgentEventType.TOOL_STARTED, call, effect=tool.effect.value,
        )
        result = await self.session.pipeline.execute(call.name, call.arguments, context=self.tool_context)
        self.messages.append(ModelMessage(role="assistant", tool_calls=(call,)))
        self.append_tool_result(call, result)
        plan = result.effect_plan
        await self.publish_tool_result(call, result)
        answer = "" if plan else format_public_result(dict(result.outcome.public_content))
        if answer:
            self.messages.append(ModelMessage(role="assistant", content=answer))
        await persist_conversation(self, preserve_capacity=plan is not None)
        await self.publish(AgentEventType.TURN_COMPLETED, {
            "status": "approval_required" if plan else "success", "answer": answer,
            "plan_id": plan.plan_id if plan else "", "usage": {}, "model_calls": 0, "tool_calls": 1,
        })

    def prepare_model(self) -> None:
        self.history_end = self.current_user_index if self.current_user_index is not None else next(
            (i for i in range(len(self.messages) - 1, -1, -1) if self.messages[i].role == "user"), len(self.messages))
        if self.resume_context is not None:
            lease = self.tool_context.lease
            record = continuation_record(self.state, lease.continuation_id, lease.continuation_kind)
            # Legacy non-TG waits used only the fence-bound default route.
            # A missing TG selection has no historical authority to infer.
            issued_model = record.get('model_policy', None if self.agent_input.channel == 'telegram' else "")
            if issued_model != self.selected_model:
                raise ToolPipelineError("等待任务的模型策略已改变，未继续调用。", code="model_policy_changed")
        self.context_window = self.session.limits.context_window_tokens
        self.output_tokens = self.session.limits.effective_output_tokens
        self.conservative_budget = False
        if isinstance(self.session.model, BudgetedModelAdapter):
            self.context_window, self.output_tokens, self.conservative_budget = self.session.model.budget_for_model(
                self.selected_model, self.context_window, self.output_tokens,
            )
        self.recovery_round: int | None = None
        self.recovery_reason = "incomplete"
        self.history_snapshot: tuple[str, str] = ("", "")

    async def record_continuation_event(self, event: AgentEvent, *, reason: str | None = None) -> None:
        self.continuation_view.apply(event)
        if self.lease is None or not self.lease.continuation_id or event.type not in TERMINAL_EVENT_TYPES:
            return
        view = self.continuation_view.build()
        if view.status == "waiting":
            return  # A trusted quota park retains its goal and cumulative budget.
        self.state = await self.session.state_store.commit(self.lease, updates=(_ContinuationAction("", {
            "operation": "finish", "reason": reason or (
                ("completed" if self.resume_read_done else "incomplete") if self.is_read_resume else view.status),
            **({"delivery_view": view.to_dict()} if self.resume_context is not None and self.agent_input.channel == "telegram" and self.agent_input.delivery_route else {}),
        }, binding=self.session.continuation_binding),))


    async def failure(self, code: str, message: str, *, analysis_text: str = "") -> None:

        failure_factory = self.factory or EventFactory(
            session_id=self.agent_input.session_id, turn_id=secrets.token_urlsafe(12),
            request_id=self.agent_input.request_id,
        )
        measurements = {"successful_read_queries": len(getattr(self, "observed_reads", ()))}
        answer: str | None = None
        if self.confirmed_result is not None or self.has_tool_result:
            answer = format_partial_progress(
                message, self.progress_results, confirmed_result=self.confirmed_result, write_attempted=self.attempted_write,
                current_errors=(result.get("error") or result.get("summary") for result in self.unresolved_results.values()),
                analysis_text=analysis_text,
            )
            self.messages.append(ModelMessage(role="assistant", content=answer, effect_plan_id=self.plan_id or ""))
        await preserve_checkpoint(self)
        if answer is not None:
            event = failure_factory.create(AgentEventType.TURN_COMPLETED, {"status": "partial", "answer": answer, "finish_reason": code, **measurements, **await self.terminal_task("partial")})
        else:
            event = failure_factory.create(AgentEventType.TURN_FAILED, {"code": code, "message": message, **measurements, **await self.terminal_task("failed")})
        if self.lease is not None and not await self.session.state_store.is_current(self.lease):
            latest = await self.session.state_store.load(owner=self.lease.owner, session_id=self.lease.session_id)
            marker = latest.metadata.get("active_publication") or {}
            if self.resume_context is not None or (marker.get("status") == "cancelled" and marker.get("request_id") == self.lease.request_id):
                return
        try:
            if self.session.journal is not None:
                if self.lease is not None and self.token is not None and not self.confirming:
                    await self.session.journal.append_for_turn(event, self.lease)
                else:
                    # Early admission/confirmation rejection has no executor
                    # ownership, but must still return its failure display.
                    await self.session.journal.append(event, owner=self.agent_input.owner)
            if (self.lease is not None and self.token is not None and not self.confirming
                    and not await self.session.state_store.is_current(self.lease)):
                return
            # finish 会关闭取消窗口；只能在 journal I/O 后核实发布权再登记。
            await self.record_continuation_event(event, reason=code)
        except StalePublicationError:
            # Cancellation may win during the journal transaction itself.
            # Do not replace the rejected failure with another late event.
            return
        self.emit(event)

    async def terminal_task(self, status: str) -> dict[str, Any]:
        task = None
        if (self.lease is not None and self.checkpoint_ready and self.state.metadata.get("task")
                and await self.session.state_store.is_current(self.lease)
                and await self.session.turn_admission.is_current(self.admission_token, self.agent_input)):
            task = task_projection(self.state.metadata["task"])
        return {"turn_status": status, "task": task}

    def emit(self, event: AgentEvent) -> None:
        self.measurement.observe(event)
        self.queue.put_nowait(event)

    async def publish(self,
        event_type: AgentEventType,
        payload: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        assert self.lease is not None and self.token is not None and self.factory is not None
        self.token.raise_if_cancelled()
        if not await self.session.coordinator.is_current(self.lease, self.token):
            raise asyncio.CancelledError("superseded")
        if not self.confirming and not await self.session.state_store.is_current(self.lease):
            raise asyncio.CancelledError("stale_generation")
        if not self.confirming and not await self.session.turn_admission.is_current(
            self.admission_token, self.agent_input
        ):
            raise asyncio.CancelledError("runtime_changed")
        if event_type in {AgentEventType.TURN_COMPLETED, AgentEventType.TURN_FAILED, AgentEventType.TURN_CANCELLED}:
            payload = {**(payload or {}), "turn_status": (payload or {}).get("status", "failed" if event_type is AgentEventType.TURN_FAILED else "cancelled"),
                       "task": task_projection(self.state.metadata.get("task"))}
        event = self.factory.create(event_type, payload, **extra)
        if self.session.journal is not None:
            if not self.confirming:
                await self.session.journal.append_for_turn(event, self.lease)
            else:
                await self.session.journal.append(event, owner=self.agent_input.owner)
        if (not self.confirming and event_type is not AgentEventType.TURN_STARTED
                and not await self.session.state_store.is_current(self.lease)):
            raise StalePublicationError("event revoked during journal I/O")
        await self.record_continuation_event(event)
        activity = public_activity(event.payload.get("activity"))
        if event.type.value == "model.reasoning_summary.delta":
            identity = f"reasoning:{event.payload.get('round', 1)}:{event.payload.get('summary_id', '')}"
            previous = self.activities.get(identity, {}).get("text", "")
            activity = {"id": identity, "kind": "reasoning",
                        "text": (previous + str(event.payload.get("delta", "")))[:8000]}
        if activity:
            self.activities[activity["id"]] = activity
            if len(self.activities) > 80:
                del self.activities[next(iter(self.activities))]
        self.emit(event)

    async def report_progress(self, payload: Mapping[str, Any]) -> None:
        await self.publish(AgentEventType.TOOL_STARTED if payload.get("kind") == "confirmed_effect" else AgentEventType.TOOL_PROGRESS, payload)

    async def publish_tool(self,
        event_type: AgentEventType,
        call: ModelToolCall,
        *,
        tool_name: str | None = None,
        **payload: Any,
    ) -> None:
        assert self.factory is not None
        public_name = tool_name or call.name
        if not public_name.startswith("agent."):
            status = {AgentEventType.TOOL_STARTED: "running", AgentEventType.TOOL_COMPLETED: "success",
                      AgentEventType.TOOL_FAILED: "failed", AgentEventType.EFFECT_APPROVAL_REQUIRED: "approval_required"}.get(event_type)
            if status:
                result = payload.get("result") or {}
                payload["activity"] = public_activity({"id": f"tool:{self.factory.turn_id}:{self.round_index}:{call.call_id}", "kind": "tool",
                    "method": public_name, "label": public_tool_label(public_name),
                    "status": "failed" if result.get("ok") is False else "partial" if result.get("status") == "partial" else status,
                    "summary": payload.get("message") or result.get("summary") or ""})
        await self.publish(event_type, {
            "call_id": call.call_id,
            "tool": public_name,
            "label": public_tool_label(public_name),
            **payload,
        })

    def append_tool_result(self, call: ModelToolCall, result: PipelineResult) -> None:
        self.messages.append(ModelMessage(
            role="tool",
            content=result.outcome.model_message(),
            tool_call_id=call.call_id,
            tool_name=call.name,
            effect_plan_id=result.effect_plan.plan_id if result.effect_plan else "",
        ))

    async def publish_tool_result(self,
        call: ModelToolCall, result: PipelineResult,
    ) -> None:
        plan = result.effect_plan
        event_type = (
            AgentEventType.EFFECT_APPROVAL_REQUIRED
            if plan else AgentEventType.TOOL_COMPLETED
        )
        if plan is not None:
            await self.publish_tool(event_type, call, result=result.outcome.audit_result(), plan=plan.public_dict())
        else:
            await self.publish_tool(event_type, call, result=result.outcome.audit_result(), elapsed_ms=result.elapsed_ms)

    async def close_failed_call(self,
        call: ModelToolCall,
        error: ToolPipelineError,
        *,
        tool_name: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
    ) -> None:
        self.messages.append(
            tool_error_message(call, error, input_schema=input_schema)
        )
        self.completed_call_ids.add(call.call_id)
        await self.publish_tool(
            AgentEventType.TOOL_FAILED,
            call,
            tool_name=tool_name,
            code=error.code,
            message=str(error),
        )

    async def finish_answer(self, answer: str, status: str, reason: str, model_calls: int,
                            *, response_state: ProviderState | None = None, interrupted: bool = False) -> None:
        """正常回答与预算收尾共用一次持久化/终态发布，不丢失工具调用协议。"""
        if self.unresolved_results and (interrupted or self.attempted_write or self.confirmed_result is not None):
            # 预算收尾仍以真实阻塞为准；语法错误不能掩盖尚未恢复的服务错误。
            pending = next((result for result in self.unresolved_results.values()
                            if result.get("status") != "invalid_arguments"),
                           next(iter(self.unresolved_results.values())))
            await self.failure(str(pending.get("status") or "tool_failed"),
                               sanitize_public_text(pending.get("error") or pending.get("summary")) or "工具调用未取得完整结果。",
                               analysis_text=answer)
            return
        if self.unresolved_results:
            status = "partial"
        answer = answer or format_partial_progress(
            "本轮预算已用完，已保留已取得的结果；可回复继续。", self.progress_results,
            confirmed_result=self.confirmed_result, write_attempted=self.attempted_write,
            current_errors=(result.get("error") or result.get("summary") for result in self.unresolved_results.values()),
        )
        public_answer = sanitize_confirmed_answer(
            answer, self.confirmed_result, results=self.progress_results,
        ) if self.confirmed_result is not None else answer
        self.messages.append(ModelMessage(role="assistant", content=public_answer, effect_plan_id=self.plan_id or "", provider_state=response_state))
        await persist_conversation(self)
        await self.publish(AgentEventType.TURN_COMPLETED, {
            "status": status, "answer": public_answer, "finish_reason": reason,
            "usage": self.total_usage, "model_calls": model_calls, "tool_calls": self.total_tool_calls,
            "business_tool_calls": self.business_tool_calls,
            "discovery_tool_calls": 0,
            "successful_read_queries": len(self.observed_reads),
        })

    async def control_task(self, patch: TaskPatch | None) -> dict | None:
        """原TaskPatch发布事务的原生入口；不新建任务存储或另一套收据。"""
        self.tool_context.cancellation.raise_if_cancelled()
        if not await self.session.state_store.is_current(self.tool_context.lease):
            raise StalePublicationError("turn lost publication authority")
        if patch is not None:
            if patch.operation != "observe":
                await self.reserve_budget(task_operation=patch.operation)
            try:
                await persist_conversation(self, task_patch=patch, preserve_capacity=True)
            except ValueError as exc:
                raise ToolPipelineError(str(exc), code="task_conflict") from exc
            self.tool_context = replace(self.tool_context, current_task=self.state.metadata.get("task"))
        return deepcopy(self.state.metadata.get("task"))

    async def adopt_task_budget(self) -> None:
        # 每段代码首次业务调用选取一次原累计预算，不按每个RPC重复记账。
        await self.reserve_budget(task_operation="use")

    @property
    def uses_session_task(self) -> bool:
        """显式新目标不自动继承旧任务；结构化任务选择和续行仍使用原任务。"""
        return bool(self.tool_context.lease.continuation_id or self._budget_task_id) or not (
            self.agent_input.initial_read_tool or self.validated_selection is not None
            or self.explaining or self.plan_id is not None
        )

    @property
    def task_budget(self) -> dict | None:
        lease = self.lease
        if lease is not None and lease.continuation_id:
            return record_budget(self.state, continuation_record(self.state, lease.continuation_id, lease.continuation_kind))
        if self._budget_task_id:
            record = self.state.metadata.get("read_wait") or {}
            if record.get("task_id") != self._budget_task_id:
                raise StalePublicationError("selected task budget changed")
            return record["budget"]
        return None

    def sync_budget_context(self) -> None:
        budget = self.task_budget or self._local_budget
        policy = self.tool_context.continuation_policy
        lease = self.lease
        assert policy is not None and lease is not None
        policy["budget"] = budget
        record = continuation_record(self.state, lease.continuation_id, lease.continuation_kind) if lease.continuation_id else {}
        policy["budget_task_id"] = record.get("budget_task_id", "") or self._budget_task_id
        remaining = active_remaining(budget, self.session.state_store._clock())
        policy["deadline"] = None if remaining is None else asyncio.get_running_loop().time() + remaining

    def seed_budget(self) -> dict:
        budget = deepcopy(self._local_budget) if self.task_budget is None else {
                **self.session.limits.budget(),
                "model_spent": self._budget_model_attempts - self._budget_paid_models,
                "tool_spent": self._budget_tool_attempts - self._budget_paid_tools,
                "token_spent": self._budget_token_attempts - self._budget_paid_tokens,
                "active_since": self.session.state_store._clock()}
        # task预算一直计全部尝试；前台业务调用的发现折算不能扩大持久续行额度。
        budget.pop("discovery_spent", None)
        return budget

    async def reserve_budget(self, *, model: int = 0, tools: int = 0, tokens: int = 0,
                             tool: KernelToolSpec | None = None,
                             arguments: Mapping[str, Any] | None = None, stop: bool = False,
                             task_operation: str | None = None) -> None:
        self._budget_model_attempts += model
        self._budget_tool_attempts += tools
        self._budget_token_attempts += tokens
        lease = self.tool_context.lease
        operation = task_operation
        control = not lease.continuation_id and operation in ("create", "cancel")
        selected = False
        if (self.session.continuation_binding is not None and (tool is not None or operation is not None)
                and not lease.continuation_id and not self._budget_task_id and not control):
            task = self.state.metadata.get("task")
            record = self.state.metadata.get("read_wait") or {}
            continuing = (operation in ("checkpoint", "revise")
                or (operation == "use" and self.uses_session_task and task and task["status"] in {"partial", "blocked"}) or (
                self.uses_session_task and task and task["status"] in {"partial", "blocked"}
                and tool is not None and tool.name != "agent.run_code"
                and (not tool.metadata.get("kernel_utility") or tool.effect is not ToolEffect.READ)))
            if continuing and task and record.get("task_id") == task["task_id"]:
                self._budget_task_id = task["task_id"]
                selected = True
        budget = self.task_budget
        value = {key: value for key, value in self.session.limits.budget().items() if key.endswith("_limit")}
        value.update(stop=stop)
        value["discovery_spent"] = 0
        try:
            if budget is not None and not control:
                value.update(model_spent=0 if stop else self._budget_model_attempts - self._budget_paid_models,
                             tool_spent=0 if stop else self._budget_tool_attempts - self._budget_paid_tools,
                             token_spent=0 if stop else self._budget_token_attempts - self._budget_paid_tokens)
                if selected:
                    reserve_budget_values(self._local_budget, {"stop": True}, self.session.state_store._clock())
                    value["active_spent"] = self._local_budget["active_spent"]
                    value["discovery_spent"] += self._local_budget.get("discovery_spent", 0)
                self.state = await self.session.state_store.commit(lease, updates=(_ContinuationAction("", {
                    **value, "operation": "reserve", "task_id": self._budget_task_id,
                }, binding=self.session.continuation_binding),))
                self._budget_paid_models = self._budget_model_attempts
                self._budget_paid_tools = self._budget_tool_attempts
                self._budget_paid_tokens = self._budget_token_attempts
            elif not control or budget is None:
                reserve_budget_values(self._local_budget, {**value, "model_spent": model,
                    "tool_spent": tools, "token_spent": tokens}, self.session.state_store._clock())
        except ValueError as exc:
            if selected:
                self._budget_task_id = ""  # 未取得原任务预算，不能在收尾时改写它。
            reason = str(exc)
            code = (reason if reason in {"token_budget_exceeded", "active_time_budget_exceeded"}
                    else "task_budget_exceeded" if budget is not None
                    else "model_round_budget_exceeded" if model else "tool_budget_exceeded")
            raise ToolPipelineError("累计执行预算已用完，未继续调用；估算token额度不是实际账单。", code=code) from exc
        self.sync_budget_context()
