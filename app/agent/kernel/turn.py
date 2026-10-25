"""一次输入、确认或恢复的准入、发布与终态收束。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
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
from app.agent.public_safety import public_tool_label
from app.agent.public_view import (
    format_partial_progress,
    format_public_result,
    public_result_state,
    sanitize_confirmed_answer,
)
from app.sensitive_data import contains_sensitive_credential

from . import telegram_reference
from .adapters import TurnViewBuilder
from .capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from .continuation import _ContinuationAction, continuation_record
from .conversation_context import (
    capability_retrieval_context,
    contextual_message,
    restore_messages,
    tool_error_message,
)
from .discovery import CapabilityDiscovery
from .effects import EffectPlan
from .events import TERMINAL_EVENT_TYPES, AgentEvent, AgentEventType, EventFactory
from .model import (
    BudgetedModelAdapter,
    ModelMessage,
    ModelToolCall,
    ProviderState,
)
from .provider_model import ModelProviderError, UncommittableModelTurn
from .session_guard import foreground_execution_guard, session_scope_guard
from .task_state import task_projection

if TYPE_CHECKING:
    from .session import AgentSession
from .model_rounds import model_loop
from .turn_history import persist_conversation, preserve_checkpoint, remember_result

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
        self.unresolved_errors: dict[tuple[str, int, str], ToolPipelineError] = {}
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
                        self.contextual_message += "\n已校验的回复批次，仅可从该引用解析候选编号：" + str(self.candidate_context.guard.ref)
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
            wait_for_completion=True,
            channel=self.agent_input.channel,
            continuation_binding=self.session.continuation_binding,
            delivery_route=self.agent_input.delivery_route,
            continuation_policy={"model_policy": self.selected_model,
                "model_limit": self.session.limits.max_model_rounds, "tool_limit": self.session.limits.max_tool_calls},
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
                # 确认前的规划成本不属于新领取的 effect 续行。
                self._budget_task_id = ""
                self._budget_paid_models = self._budget_model_attempts
                self._budget_paid_tools = self._budget_tool_attempts
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
        reference_kinds = tuple(self.state.ref_kinds | ({"telegram_reference"} if self.agent_input.reply_context.get("reference_handle") or any(row.get("reply_context", {}).get("reference_handle") for row in self.state.conversation) else set()))
        history = capability_retrieval_context(
            self.state, current_turn=self.current_user_index is not None and not self.sensitive_input,
        )
        context = {
            **history,
            "owner": self.agent_input.owner, "session_id": self.agent_input.session_id,
            "request_id": self.agent_input.request_id, "channel": self.agent_input.channel,
            "generation": lease.generation, "turn_id": lease.turn_id,
            "has_task": bool(self.state.metadata.get("task")),
            "reference_kinds": reference_kinds,
            "has_current_reference": bool(self.agent_input.reply_context.get("text")),
        }
        selection = None if self.explaining or self.agent_input.initial_read_tool else self.session.retriever.retrieve(
            self.contextual_message, self.session.catalog, context=context,
        )
        self.discovery = CapabilityDiscovery(
            self.session.catalog,
            context=context,
            maximum=self.session.limits.capability_window,
        )
        # 接力的整个回合只开放 READ，能力发现 callback 也不能加载写工具。
        initial_tool = None
        self.pending_initial_read = None
        if self.agent_input.initial_read_tool:
            self.discovery.catalog = ToolCatalog(
                tool for tool in self.session.catalog.visible(self.discovery.context)
                if tool.effect is ToolEffect.READ
            )
            initial_tool = self.session.catalog.get(self.agent_input.initial_read_tool)
            if initial_tool.effect is not ToolEffect.READ:
                raise ToolPipelineError("工作区行动必须是只读能力。", code="read_handoff_invalid")
        source = self.tool_context.reobservation_source
        source_read = source.get("read") if isinstance(source, Mapping) and source.get("available") is True else None
        source_name = source_read.get("tool") if isinstance(source_read, Mapping) else None
        source_spec = self.session.catalog.get(source_name) if isinstance(source_name, str) and self.session.catalog.has(source_name) else None
        if not (self.diagnosing_effect and source_spec is not None and source_spec.effect is ToolEffect.READ
                and "reobserve" in source_spec.input_schema.get("properties", {}).get("operation", {}).get("enum", ())):
            source_spec = None
        # 真实确认结果的只读邻接优先用于核验；仍受同一窗口、可见性与授权过滤。
        # 失败重观察保留最高优先级；加载不等于执行，更不为后续写入授予权限。
        effect = self.tool_context.confirmed_effect
        readbacks = tuple(self.session.catalog.get(name) for name in (
            effect.tool.metadata.get("related_tools", ()) if effect else ()
        ) if self.session.catalog.has(name) and self.session.catalog.get(name).effect is ToolEffect.READ)
        # 历史调用仍在模型上下文中，其只读 Schema 按剩余容量续用，避免换话题
        # 后同一能力突然不可调用。当前查询优先；仍由唯一窗口重查可见性与授权。
        recent_reads = tuple(self.session.catalog.get(name) for name in history["recent_tool_names"]
            if self.session.catalog.has(name) and self.session.catalog.get(name).effect is ToolEffect.READ)
        self.selected_tools = () if self.explaining else self.discovery.window(
            (selection.tools if selection else ()) + recent_reads,
            ((initial_tool,) if initial_tool else ()) + ((source_spec,) if source_spec else ()) + readbacks)
        if initial_tool is not None:
            if initial_tool not in self.selected_tools:
                raise ToolPipelineError("工作区行动当前不可用。", code="read_handoff_invalid")
            self.pending_initial_read = ModelToolCall(secrets.token_urlsafe(12), initial_tool.model_name, {})
        self.pending_resume_call = None
        if self.resume_context is not None:
            from .continuation import history_call
            record = continuation_record(self.state, lease.continuation_id, lease.continuation_kind)
            if record.get("anchor"):
                self.pending_resume_call = history_call(self.state, record)
                self.resumed_tool = self.session.catalog.get(record["anchor"]["tool"])
                if self.resumed_tool.effect is not ToolEffect.READ or self.resumed_tool.name.startswith("agent."):
                    raise ToolPipelineError("等待能力不可恢复。", code="read_resume_invalid")
                self.selected_tools = self.discovery.window(self.selected_tools, (self.resumed_tool,))
        if self.validated_selection is not None:
            assert self.selection_tool is not None
            self.selected_tools = (self.selection_tool,)
        await self.publish(
            AgentEventType.CAPABILITIES_SELECTED,
            {
                "tools": [tool.name for tool in self.selected_tools],
                "count": len(self.selected_tools),
            },
        )
        self.available_names = {name for tool in self.selected_tools for name in (tool.name, tool.model_name)}
        if (self.pending_resume_call is None and self.tool_context.diagnostic_only
                and source_spec is not None and source_spec.name in self.available_names):
            # 可信旧计划已保存原观察范围；重读不是新写入，不要求模型再征求许可。
            # 共用READ的准入、预算、检查点和取消链，不重放旧写操作。
            self.resumed_tool = source_spec
            self.pending_resume_call = {"arguments": {"operation": "reobserve"}}
        self.tool_definitions = tuple(
            tool.model_definition() for tool in self.selected_tools
        )
        self.total_tool_calls = 0
        self.business_tool_calls = 0
        self.write_tool_calls = 0
        self.discovery_tool_calls = 0
        self.tool_budget_blocked = False
        self.total_usage: dict[str, int] = {}
        self.observed_reads: set[tuple[str, str]] = set()
        self.tool_context = replace(self.tool_context, capability_search=self.discovery.search)

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
        await persist_conversation(self)
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
                current_errors=self.unresolved_errors.values(), analysis_text=analysis_text,
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
        public_name = tool_name or call.name
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
                            *, response_state: ProviderState | None = None) -> None:
        """正常回答与预算收尾共用一次持久化/终态发布，不丢失工具调用协议。"""
        if self.unresolved_errors:
            # 预算收尾仍以真实阻塞为准；语法错误不能掩盖尚未恢复的服务错误。
            pending_error = next((error for error in self.unresolved_errors.values()
                                  if error.code != "invalid_arguments"),
                                 next(iter(self.unresolved_errors.values())))
            message = f"仍有工具调用受到频率限制：{pending_error}" if pending_error.code == "rate_limited" else str(pending_error)
            await self.failure(pending_error.code, message, analysis_text=answer)
            return
        answer = answer or format_partial_progress(
            "本轮预算已用完，已保留已取得的结果；可回复继续。", self.progress_results,
            confirmed_result=self.confirmed_result, write_attempted=self.attempted_write,
            current_errors=self.unresolved_errors.values(),
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
            "discovery_tool_calls": self.discovery_tool_calls,
            "successful_read_queries": len(self.observed_reads),
        })

    @property
    def uses_session_task(self) -> bool:
        """显式新目标不自动继承旧任务；结构化任务选择和续行仍使用原任务。"""
        return bool(self.tool_context.lease.continuation_id or self._budget_task_id) or not (
            self.agent_input.initial_read_tool or self.validated_selection is not None
            or self.explaining or self.plan_id is not None
        )

    @property
    def task_budget(self) -> dict | None:
        lease = self.tool_context.lease
        if lease.continuation_id:
            return continuation_record(self.state, lease.continuation_id, lease.continuation_kind)["budget"]
        if self._budget_task_id:
            record = self.state.metadata.get("read_wait") or {}
            if record.get("task_id") != self._budget_task_id:
                raise StalePublicationError("selected task budget changed")
            return record["budget"]
        return None

    async def reserve_budget(self, *, model: int = 0, tools: int = 0,
                             tool: KernelToolSpec | None = None,
                             arguments: Mapping[str, Any] | None = None) -> None:
        self._budget_model_attempts += model
        self._budget_tool_attempts += tools
        if self.session.continuation_binding is None:
            return
        lease = self.tool_context.lease
        operation = (arguments or {}).get("operation") if tool is not None and tool.name == "agent.task" else None
        if not lease.continuation_id and (operation in ("create", "cancel")
                                         or (tool is not None and tool.name == "agent.pending_plan")):
            return  # 耗尽后仍可停止/选择新任务；新建时在原事务内归账。
        if tool is not None and not lease.continuation_id and not self._budget_task_id:
            task = self.state.metadata.get("task")
            record = self.state.metadata.get("read_wait") or {}
            continuing = (operation in ("checkpoint", "revise") or (
                self.uses_session_task and task and task["status"] in {"partial", "blocked"}
                and tool.name != "agent.capabilities"
                and (not tool.metadata.get("kernel_utility") or tool.effect is not ToolEffect.READ
                     or tool.name in {"agent.read_result", "agent.repair"})))
            if continuing and task and record.get("task_id") == task["task_id"]:
                self._budget_task_id = task["task_id"]
        if self.task_budget is not None:
            try:
                self.state = await self.session.state_store.commit(lease, updates=(_ContinuationAction("", {
                    "operation": "reserve", "task_id": self._budget_task_id,
                    "model_spent": self._budget_model_attempts - self._budget_paid_models,
                    "tool_spent": self._budget_tool_attempts - self._budget_paid_tools,
                    "model_limit": self.session.limits.max_model_rounds, "tool_limit": self.session.limits.max_tool_calls,
                }, binding=self.session.continuation_binding),))
            except ValueError as exc:
                raise ToolPipelineError("任务累计预算已用完，未继续调用。", code="task_budget_exceeded") from exc
            self._budget_paid_models = self._budget_model_attempts
            self._budget_paid_tools = self._budget_tool_attempts
