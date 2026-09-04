"""唯一的 MODEL -> TOOL -> MODEL Agent 决策循环。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.sensitive_data import contains_sensitive_credential

from .capabilities import CapabilityRetriever, ToolCatalog, ToolEffect
from .events import AgentEvent, AgentEventType, EventFactory
from .model import (
    ModelAdapter,
    ModelEventType,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
)
from .pipeline import ToolCallContext, ToolPipeline, ToolPipelineError
from .state import (
    AgentInput,
    CancellationToken,
    PublicationLease,
    SessionBusyError,
    SessionState,
    SessionStateStore,
    StalePublicationError,
    StateUpdate,
    TurnCoordinator,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是 MediaFlux Media Agent，一名可操作当前 MediaFlux 项目的家庭媒体助手。

职责边界：
- 你是自然语言理解与多步规划的唯一权威。直接理解口语、上下文和省略表达，不要求用户记工具名。
- 云盘、媒体库、下载、订阅、资源、TMDB 与项目状态等事实必须来自本轮工具结果；不得凭记忆编造当前状态。
- 在本轮候选原子工具中自主执行 MODEL -> TOOL -> MODEL 循环。工具失败时先阅读安全错误，能修正参数或改用候选能力就自行重试。
- 一次请求可以连续组合多个 READ 工具；最终直接回答，不调用第二个模型做 presentation。
- 短追问必须继承最近会话中的媒体对象、工具事实与用户约束；只要本轮候选中存在相关能力，就先调用验证，不能未经尝试便声称“未挂载”或“接口未开放”。

副作用规则：
- READ 工具可直接调用。
- WRITE/DANGER 工具永远只会生成冻结 EffectPlan，不会立即写入。看到 approval_required 后，清楚概括对象、动作、影响与不可逆性，然后停止；绝不能声称已经执行。
- 用户确认由系统独立执行，不经过你。不得猜测、修改或伪造 plan_id。
- 只使用工具返回的安全 opaque ref；不要猜数据库主键、Provider 对象 ID、绝对路径、URL、令牌或内部句柄。

领域判断：
- “查看/列出/搜索云盘目录”先用通用光鸭文件查询；“创建目录、改名、移动、回收站”是在查询结果上生成文件变更计划。
- 用户用自然片名描述父目录下的对象时，不要先猜一个同名绝对路径；先列出或递归观察父目录。若同一作品散落在多个发布组目录中，应观察父目录并汇总全部匹配文件，不能只处理第一个目录。
- 用户要求先整理混乱发布组文件、按 TMDB 集序重命名、再方便后续识别入库时，这是云盘文件规整，不等同于刮削、媒体名称垃圾清理或立即执行媒体整理。先列出共同父目录确认真实发布组名称，再用 paths 一次合并这些目录并以 kinds=["video"]、max_depth=0 聚合正片；只有不便枚举目录时才从共同父目录 search，并用 max_depth 限制花絮子目录。有更多页时沿同一 observation_ref 分页，再一次生成批量文件变更预览。
- 同一 observation_ref 的全部分页合计已覆盖用户指定的对象数量且未截断时，视为观察完成；直接使用这份快照生成变更预览，不要再创建新的搜索快照或重复核对，否则先前 object_ref 会失效。
- 大批量剧集需要统一移动并按集号改名时，使用一项 batch_relocate，把每个 object_ref 与真实集号完整列入 items；不要只提交一个示例文件。用户要求全局 1-N/TMDB 顺序时使用 naming="absolute"，按季编号时使用 naming="season_episode"。若目标目录尚不存在，可在同一 operations 中加入 create_directory（可直接传完整 path），并让 batch_relocate.target_path 指向该新目录。
- 媒体服务器实时统计、媒体总数、qBittorrent 实时任务/速度/进度应先读取 Provider 能力，再执行 Provider 实时查询；媒体总数使用能力清单中的 media.items.counts，不用本地历史记录或巡检快照冒充实时状态。
- 资源搜索结果会给出 `reference_arguments.resource_candidates_ref`。同轮继续提交或用户用“这个/4K版/第几个/推送”等短句续接时，必须把该引用原样传给资源检查/提交工具，再生成确认计划；不能遗漏引用、重复搜索，或因为当前短句没重复“云盘”就声称提交能力未挂载。
- 直链或光鸭分享检查会给出 `reference_arguments.ingest_snapshot_ref`。后续提交必须原样传入该引用；不得把原始链接重新塞进写工具，也不得依赖另一个标签页的“最近一次”内存状态。
- “最近/今年/定档/新剧”若本地探索数据不能证明时，结合联网公开信息并标明来源时效。
- RSS 规则、媒体追更订阅和下载请求是不同对象；创建、修改、删除必须展示准确预览并等待确认，不能仅凭模型回答宣称创建成功。

回答要求：
- 先给结论，再给必要明细；明确区分实时结果、缓存结果、部分完成和未执行。
- 不重复同一错误，不输出内部链路、凭据、完整路径或无意义的“请稍后重试”。
- 若确实缺少必要对象，说明已检查什么以及只缺哪一个信息。"""


class EventJournal(Protocol):
    async def append(self, event: AgentEvent, *, owner: str) -> None: ...


class TurnAdmissionPolicy(Protocol):
    async def begin(self, agent_input: AgentInput) -> Any: ...

    async def is_current(self, token: Any, agent_input: AgentInput) -> bool: ...


class AllowAllTurnAdmission:
    async def begin(self, agent_input: AgentInput) -> None:
        del agent_input

    async def is_current(self, token: Any, agent_input: AgentInput) -> bool:
        del token, agent_input
        return True


@dataclass(frozen=True, slots=True)
class SessionLimits:
    # 复杂的只读观察 -> 批量计划通常需要 5 轮以上；预算仍有硬上限，
    # 但不能让分页本身把正常任务挤成 model_round_budget_exceeded。
    max_model_rounds: int = 12
    max_tool_calls: int = 16
    max_output_tokens: int = 6_000
    context_window_tokens: int = 128_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_model_rounds <= 12:
            raise ValueError("max_model_rounds out of range")
        if not 1 <= self.max_tool_calls <= 32:
            raise ValueError("max_tool_calls out of range")
        if not 128 <= self.max_output_tokens <= 16_000:
            raise ValueError("max_output_tokens out of range")
        if not 16_384 <= self.context_window_tokens <= 2_000_000:
            raise ValueError("context_window_tokens out of range")

    @property
    def effective_output_tokens(self) -> int:
        return min(
            self.max_output_tokens,
            max(1_024, self.context_window_tokens // 4),
        )


class AgentSession:
    """领域无关、事件驱动、可暂停确认的 Agent Kernel。"""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        catalog: ToolCatalog,
        retriever: CapabilityRetriever,
        pipeline: ToolPipeline,
        state_store: SessionStateStore,
        coordinator: TurnCoordinator | None = None,
        journal: EventJournal | None = None,
        limits: SessionLimits | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        turn_admission: TurnAdmissionPolicy | None = None,
    ) -> None:
        if pipeline.catalog is not catalog:
            raise ValueError("AgentSession and ToolPipeline must share one catalog")
        if pipeline.state_store is not state_store:
            raise ValueError("AgentSession and ToolPipeline must share one state store")
        self.model = model
        self.catalog = catalog
        self.retriever = retriever
        self.pipeline = pipeline
        self.state_store = state_store
        self.coordinator = coordinator or TurnCoordinator()
        self.journal = journal
        self.limits = limits or SessionLimits()
        self.system_prompt = str(system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
        self.turn_admission = turn_admission or AllowAllTurnAdmission()
        # 只串行化极短的“取得 generation + 注册 active turn”窗口；
        # 已确认写操作一旦开始就不会被后续聊天抢占。
        self._start_lock = asyncio.Lock()

    async def run(self, agent_input: AgentInput) -> AsyncIterator[AgentEvent]:
        """运行一轮并实时产生事实事件；消费者断开时取消当前回合。"""
        async for event in self._run_background(
            lambda queue: self._drive(agent_input, queue)
        ):
            yield event

    async def confirm(
        self,
        *,
        owner: str,
        session_id: str,
        plan_id: str,
        request_id: str = "",
        channel: str = "api",
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._run_background(
            lambda queue: self._drive_confirmation(
                owner=str(owner or "").strip(),
                session_id=str(session_id or "").strip(),
                plan_id=str(plan_id or "").strip(),
                request_id=str(request_id or "").strip() or secrets.token_urlsafe(12),
                channel=str(channel or "api").strip().lower(),
                queue=queue,
            )
        ):
            yield event

    async def cancel(self, *, owner: str, session_id: str) -> bool:
        return await self.coordinator.cancel(
            owner=str(owner or "").strip(),
            session_id=str(session_id or "").strip(),
            reason="user_cancelled",
        )

    async def cancel_effect(
        self,
        *,
        owner: str,
        session_id: str,
        plan_id: str,
        request_id: str = "",
    ) -> bool:
        state = await self.state_store.load(owner=owner, session_id=session_id)
        if state.generation <= 0:
            return False
        lease = PublicationLease(
            owner=owner,
            session_id=session_id,
            generation=state.generation,
            turn_id=secrets.token_urlsafe(12),
            request_id=request_id or secrets.token_urlsafe(12),
        )
        token = CancellationToken()

        async def ignore_progress(_payload: Mapping[str, Any]) -> None:
            return None

        context = ToolCallContext(
            owner=owner,
            session_id=session_id,
            request_id=lease.request_id,
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=token,
            report_progress=ignore_progress,
        )
        return await self.pipeline.cancel_effect(plan_id, context=context)

    async def _run_background(
        self,
        producer: Callable[[asyncio.Queue[AgentEvent | None]], Awaitable[None]],
    ) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        task = asyncio.create_task(producer(queue))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _drive(
        self,
        agent_input: AgentInput,
        queue: asyncio.Queue[AgentEvent | None],
    ) -> None:
        lease: PublicationLease | None = None
        token: CancellationToken | None = None
        factory: EventFactory | None = None
        try:
            admission_token = await self.turn_admission.begin(agent_input)
            async with self._start_lock:
                if await self.coordinator.has_protected_turn(
                    owner=agent_input.owner,
                    session_id=agent_input.session_id,
                ):
                    raise SessionBusyError("confirmed effect is executing")
                lease, state = await self.state_store.begin_turn(
                    owner=agent_input.owner,
                    session_id=agent_input.session_id,
                    request_id=agent_input.request_id,
                )
                token = await self.coordinator.begin(lease)
            factory = EventFactory(
                session_id=agent_input.session_id,
                turn_id=lease.turn_id,
                request_id=agent_input.request_id,
            )

            async def publish(
                event_type: AgentEventType,
                payload: Mapping[str, Any] | None = None,
                **extra: Any,
            ) -> None:
                token.raise_if_cancelled()
                if not await self.coordinator.is_current(lease, token):
                    raise asyncio.CancelledError("superseded")
                if not await self.state_store.is_current(lease):
                    raise asyncio.CancelledError("stale_generation")
                if not await self.turn_admission.is_current(
                    admission_token, agent_input
                ):
                    raise asyncio.CancelledError("runtime_changed")
                event = factory.create(event_type, payload, **extra)
                if self.journal is not None:
                    await self.journal.append(event, owner=agent_input.owner)
                await queue.put(event)

            await publish(
                AgentEventType.TURN_STARTED,
                {
                    "channel": agent_input.channel,
                    "generation": lease.generation,
                },
            )
            contextual_message = self._contextual_message(agent_input)
            if contains_sensitive_credential(contextual_message):
                await publish(
                    AgentEventType.TURN_FAILED,
                    {
                        "code": "sensitive_input",
                        "message": "消息包含疑似凭据，未发送给模型。",
                    },
                )
                return

            selection = self.retriever.retrieve(
                contextual_message,
                self.catalog,
                context={
                    "owner": agent_input.owner,
                    "session_id": agent_input.session_id,
                    "channel": agent_input.channel,
                    "reference_kinds": tuple(state.ref_kinds),
                    **self._capability_retrieval_context(state),
                },
            )
            await publish(
                AgentEventType.CAPABILITIES_SELECTED,
                {
                    "tools": list(selection.names),
                    "count": len(selection.tools),
                },
            )
            messages = self._restore_messages(state)
            current_user_index = len(messages)
            messages.append(ModelMessage(role="user", content=contextual_message))
            selected_names = set(selection.names)
            selected_model_names = set(selection.model_names)
            tool_definitions = tuple(
                tool.model_definition() for tool in selection.tools
            )
            total_tool_calls = 0
            total_usage: dict[str, int] = {}

            async def progress(payload: Mapping[str, Any]) -> None:
                await publish(AgentEventType.TOOL_PROGRESS, payload)

            tool_context = ToolCallContext(
                owner=agent_input.owner,
                session_id=agent_input.session_id,
                request_id=agent_input.request_id,
                turn_id=lease.turn_id,
                lease=lease,
                cancellation=token,
                report_progress=progress,
            )
            # 新的自然语言回合会明确取代尚未确认的旧计划。若只提升
            # generation 而不撤销票据，历史卡片会永久显示“待确认”，
            # 但点击时又只能得到 stale plan，形成确认死状态。
            if state.pending_effect_plan_id:
                await self.pipeline.cancel_effect(
                    state.pending_effect_plan_id,
                    context=tool_context,
                )

            for round_index in range(self.limits.max_model_rounds):
                token.raise_if_cancelled()
                await publish(AgentEventType.MODEL_STARTED, {"round": round_index + 1})
                text_parts: list[str] = []
                calls: list[ModelToolCall] = []
                finish_reason = ""
                request = ModelRequest(
                    system_prompt=self.system_prompt,
                    messages=self._bounded_model_messages(
                        messages,
                        history_end=current_user_index,
                        tool_definitions=tool_definitions,
                    ),
                    tools=tool_definitions,
                    max_output_tokens=self.limits.effective_output_tokens,
                    round_index=round_index,
                )
                async for model_event in self.model.stream(request, cancellation=token):
                    token.raise_if_cancelled()
                    if model_event.type is ModelEventType.TEXT_DELTA:
                        if model_event.text:
                            text_parts.append(model_event.text)
                            await publish(
                                AgentEventType.MODEL_DELTA,
                                {"delta": model_event.text, "round": round_index + 1},
                            )
                    elif model_event.type is ModelEventType.TOOL_CALL_COMPLETED:
                        call = model_event.tool_call
                        if call is not None:
                            calls.append(call)
                            try:
                                public_tool_name = self.catalog.get(call.name).name
                            except KeyError:
                                public_tool_name = call.name
                            await publish(
                                AgentEventType.MODEL_TOOL_CALL,
                                {
                                    "call_id": call.call_id,
                                    "tool": public_tool_name,
                                    "argument_keys": sorted(
                                        str(key)[:80] for key in call.arguments
                                    )[:50],
                                    "round": round_index + 1,
                                },
                            )
                    elif model_event.type is ModelEventType.USAGE:
                        for key, value in model_event.usage.items():
                            try:
                                total_usage[key] = total_usage.get(key, 0) + max(
                                    0, int(value)
                                )
                            except (TypeError, ValueError):
                                continue
                    elif model_event.type is ModelEventType.FINISH:
                        finish_reason = model_event.finish_reason

                assistant_text = "".join(text_parts).strip()
                if calls:
                    messages.append(
                        ModelMessage(
                            role="assistant",
                            content=assistant_text,
                            tool_calls=tuple(calls),
                        )
                    )
                    for call in calls:
                        total_tool_calls += 1
                        if total_tool_calls > self.limits.max_tool_calls:
                            raise ToolPipelineError(
                                "本轮工具调用次数超过安全上限",
                                code="tool_budget_exceeded",
                            )
                        if (
                            call.name not in selected_names
                            and call.name not in selected_model_names
                        ):
                            error = ToolPipelineError(
                                "该工具不在本轮候选能力中",
                                code="tool_not_available",
                            )
                            await publish(
                                AgentEventType.TOOL_FAILED,
                                {
                                    "call_id": call.call_id,
                                    "tool": call.name,
                                    "code": error.code,
                                    "message": str(error),
                                },
                            )
                            messages.append(self._tool_error_message(call, error))
                            continue
                        tool = self.catalog.get(call.name)
                        canonical_call = ModelToolCall(
                            call_id=call.call_id,
                            name=tool.name,
                            arguments=call.arguments,
                        )
                        if tool.effect is not ToolEffect.READ:
                            await publish(
                                AgentEventType.EFFECT_PREVIEW_STARTED,
                                {"call_id": call.call_id, "tool": tool.name},
                            )
                        await publish(
                            AgentEventType.TOOL_STARTED,
                            {
                                "call_id": call.call_id,
                                "tool": tool.name,
                                "effect": tool.effect.value,
                            },
                        )
                        try:
                            result = await self.pipeline.execute(
                                canonical_call.name,
                                canonical_call.arguments,
                                context=tool_context,
                            )
                        except ToolPipelineError as exc:
                            await publish(
                                AgentEventType.TOOL_FAILED,
                                {
                                    "call_id": call.call_id,
                                    "tool": tool.name,
                                    "code": exc.code,
                                    "message": str(exc),
                                },
                            )
                            messages.append(self._tool_error_message(call, exc))
                            continue
                        if result.effect_plan is not None:
                            plan = result.effect_plan
                            await publish(
                                AgentEventType.EFFECT_APPROVAL_REQUIRED,
                                {
                                    "call_id": call.call_id,
                                    "tool": tool.name,
                                    "plan": plan.public_dict(),
                                    "result": dict(result.outcome.public_content),
                                },
                            )
                            messages.append(
                                ModelMessage(
                                    role="tool",
                                    content=result.outcome.model_message(),
                                    tool_call_id=call.call_id,
                                    tool_name=call.name,
                                )
                            )
                            await self.state_store.commit(
                                lease,
                                conversation=self._persisted_conversation(
                                    messages,
                                    current_user_index=current_user_index,
                                    original_message=agent_input.message,
                                ),
                            )
                            await publish(
                                AgentEventType.TURN_COMPLETED,
                                {
                                    "status": "approval_required",
                                    "plan_id": plan.plan_id,
                                    "usage": total_usage,
                                    "model_calls": round_index + 1,
                                    "tool_calls": total_tool_calls,
                                },
                            )
                            return
                        await publish(
                            AgentEventType.TOOL_COMPLETED,
                            {
                                "call_id": call.call_id,
                                "tool": tool.name,
                                "elapsed_ms": result.elapsed_ms,
                                "result": dict(result.outcome.public_content),
                            },
                        )
                        messages.append(
                            ModelMessage(
                                role="tool",
                                content=result.outcome.model_message(),
                                tool_call_id=call.call_id,
                                tool_name=call.name,
                            )
                        )
                    continue

                final_text = assistant_text
                if not final_text:
                    raise ToolPipelineError(
                        "模型没有返回回答或工具调用",
                        code="empty_model_response",
                    )
                messages.append(ModelMessage(role="assistant", content=final_text))
                await self.state_store.commit(
                    lease,
                    conversation=self._persisted_conversation(
                        messages,
                        current_user_index=current_user_index,
                        original_message=agent_input.message,
                    ),
                )
                await publish(
                    AgentEventType.TURN_COMPLETED,
                    {
                        "status": "success",
                        "answer": final_text,
                        "finish_reason": finish_reason or "stop",
                        "usage": total_usage,
                        "model_calls": round_index + 1,
                        "tool_calls": total_tool_calls,
                    },
                )
                return

            raise ToolPipelineError(
                "模型在调用轮次上限内未完成任务",
                code="model_round_budget_exceeded",
            )
        except (asyncio.CancelledError, StalePublicationError) as exc:
            if factory is not None:
                event = factory.create(
                    AgentEventType.TURN_CANCELLED,
                    {"reason": str(exc) or (token.reason if token else "cancelled")},
                )
                if self.journal is not None:
                    await self.journal.append(event, owner=agent_input.owner)
                await queue.put(event)
        except SessionBusyError:
            busy_factory = factory or EventFactory(
                session_id=agent_input.session_id,
                turn_id=secrets.token_urlsafe(12),
                request_id=agent_input.request_id,
            )
            event = busy_factory.create(
                AgentEventType.TURN_FAILED,
                {
                    "code": "effect_in_progress",
                    "message": "已确认的写操作正在执行，请等待完成后再继续。",
                },
            )
            if self.journal is not None:
                await self.journal.append(event, owner=agent_input.owner)
            await queue.put(event)
        except ToolPipelineError as exc:
            if factory is not None:
                event = factory.create(
                    AgentEventType.TURN_FAILED,
                    {"code": exc.code, "message": str(exc)},
                )
                if self.journal is not None:
                    await self.journal.append(event, owner=agent_input.owner)
                await queue.put(event)
        except Exception as exc:  # noqa: BLE001 - final turn fault boundary
            logger.error("Agent turn failed type=%s", type(exc).__name__)
            if factory is not None:
                event = factory.create(
                    AgentEventType.TURN_FAILED,
                    {"code": "internal_error", "message": "Agent 运行失败"},
                )
                if self.journal is not None:
                    await self.journal.append(event, owner=agent_input.owner)
                await queue.put(event)
        finally:
            if lease is not None and token is not None:
                await self.coordinator.finish(lease, token)
            await queue.put(None)

    async def _drive_confirmation(
        self,
        *,
        owner: str,
        session_id: str,
        plan_id: str,
        request_id: str,
        channel: str,
        queue: asyncio.Queue[AgentEvent | None],
    ) -> None:
        if not owner or not session_id or not plan_id:
            await queue.put(None)
            return
        try:
            async with self._start_lock:
                state = await self.state_store.load(owner=owner, session_id=session_id)
                lease = PublicationLease(
                    owner=owner,
                    session_id=session_id,
                    generation=state.generation,
                    turn_id=secrets.token_urlsafe(12),
                    request_id=request_id,
                )
                token = await self.coordinator.begin(lease, protected=True)
        except SessionBusyError:
            factory = EventFactory(
                session_id=session_id,
                turn_id=secrets.token_urlsafe(12),
                request_id=request_id,
            )
            event = factory.create(
                AgentEventType.TURN_FAILED,
                {
                    "code": "effect_in_progress",
                    "message": "另一项已确认写操作正在执行。",
                },
            )
            if self.journal is not None:
                await self.journal.append(event, owner=owner)
            await queue.put(event)
            await queue.put(None)
            return
        factory = EventFactory(
            session_id=session_id, turn_id=lease.turn_id, request_id=request_id
        )

        async def publish(
            event_type: AgentEventType, payload: Mapping[str, Any] | None = None
        ) -> None:
            token.raise_if_cancelled()
            event = factory.create(event_type, payload)
            if self.journal is not None:
                await self.journal.append(event, owner=owner)
            await queue.put(event)

        async def progress(payload: Mapping[str, Any]) -> None:
            await publish(AgentEventType.TOOL_PROGRESS, payload)

        context = ToolCallContext(
            owner=owner,
            session_id=session_id,
            request_id=request_id,
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=token,
            report_progress=progress,
        )

        async def remember_result(
            *,
            tool_name: str,
            content: str,
        ) -> None:
            """把确定性确认终态写回会话，供下一轮续问直接引用。"""
            safe_content = str(content or "").strip()
            if not safe_content:
                return
            conversation = [dict(item) for item in state.conversation]
            conversation.append(
                ModelMessage(
                    role="assistant",
                    content=(
                        "已确认操作的可信系统结果（不是待执行计划）：\n"
                        + safe_content
                    ),
                    tool_name=tool_name,
                ).to_dict()
            )
            try:
                await self.state_store.commit(
                    lease,
                    conversation=conversation,
                    updates=(StateUpdate("pending_effect_plan_id", ""),),
                )
            except StalePublicationError:
                raise
            except Exception as exc:  # noqa: BLE001 - 已执行副作用不得被状态写回遮蔽
                logger.warning(
                    "Agent 确认结果写回会话失败 type=%s", type(exc).__name__
                )
        try:
            await publish(
                AgentEventType.TURN_STARTED,
                {
                    "channel": channel,
                    "generation": lease.generation,
                    "kind": "confirmation",
                },
            )
            await publish(
                AgentEventType.TOOL_STARTED,
                {"plan_id": plan_id, "kind": "confirmed_effect"},
            )
            result = await self.pipeline.execute_confirmed(plan_id, context=context)
            public_result = dict(result.outcome.public_content)
            await remember_result(
                tool_name=result.tool.name,
                content=result.outcome.model_message(),
            )
            if public_result.get("ok") is False:
                code = str(public_result.get("status") or "effect_failed")[:80]
                await publish(
                    AgentEventType.EFFECT_FAILED,
                    {
                        "plan_id": plan_id,
                        "tool": result.tool.name,
                        "code": code,
                        "message": str(
                            public_result.get("error")
                            or public_result.get("summary")
                            or "执行未完成"
                        )[:500],
                        "elapsed_ms": result.elapsed_ms,
                        "result": public_result,
                    },
                )
                await publish(
                    AgentEventType.TURN_FAILED,
                    {"code": code, "message": "已确认操作未能完成"},
                )
            else:
                await publish(
                    AgentEventType.EFFECT_COMPLETED,
                    {
                        "plan_id": plan_id,
                        "tool": result.tool.name,
                        "elapsed_ms": result.elapsed_ms,
                        "result": public_result,
                    },
                )
                await publish(
                    AgentEventType.TURN_COMPLETED,
                    {"status": "effect_completed", "plan_id": plan_id},
                )
        except (asyncio.CancelledError, StalePublicationError) as exc:
            event = factory.create(
                AgentEventType.TURN_CANCELLED,
                {"reason": str(exc) or token.reason},
            )
            if self.journal is not None:
                await self.journal.append(event, owner=owner)
            await queue.put(event)
        except ToolPipelineError as exc:
            await remember_result(
                tool_name="confirmed_effect",
                content=f"执行失败：{str(exc)[:500]}（错误码：{exc.code[:80]}）",
            )
            await publish(
                AgentEventType.EFFECT_FAILED,
                {"plan_id": plan_id, "code": exc.code, "message": str(exc)},
            )
            await publish(
                AgentEventType.TURN_FAILED,
                {"code": exc.code, "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - confirmed-effect fault boundary
            logger.error("Agent confirmed effect failed type=%s", type(exc).__name__)
            await remember_result(
                tool_name="confirmed_effect",
                content=(
                    "执行状态未知：确认执行发生内部错误"
                    "（错误码：internal_error），请先查询真实业务状态再决定是否重试。"
                ),
            )
            await publish(
                AgentEventType.EFFECT_FAILED,
                {
                    "plan_id": plan_id,
                    "code": "internal_error",
                    "message": "确认执行失败",
                },
            )
            await publish(
                AgentEventType.TURN_FAILED,
                {"code": "internal_error", "message": "确认执行失败"},
            )
        finally:
            await self.coordinator.finish(lease, token)
            await queue.put(None)

    @staticmethod
    def _estimated_tokens(value: object) -> int:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), default=str
                )
            except (TypeError, ValueError):
                text = str(value)
        ascii_chars = sum(1 for char in text if ord(char) < 128)
        wide_chars = len(text) - ascii_chars
        return max(1, (ascii_chars + 3) // 4 + wide_chars)

    def _bounded_model_messages(
        self,
        messages: Sequence[ModelMessage],
        *,
        history_end: int,
        tool_definitions: Sequence[Mapping[str, Any]],
    ) -> tuple[ModelMessage, ...]:
        """裁剪旧回合并压缩当前工具结果，绝不把超预算请求交给 Provider。"""
        split_at = max(0, min(int(history_end), len(messages)))
        history = list(messages[:split_at])
        current = list(messages[split_at:])
        fixed_tokens = (
            self._estimated_tokens(self.system_prompt)
            + self._estimated_tokens(tool_definitions)
            + self.limits.effective_output_tokens
            + 512
        )
        message_budget = max(0, self.limits.context_window_tokens - fixed_tokens)

        def message_cost(items: Sequence[ModelMessage]) -> int:
            return sum(8 + self._estimated_tokens(item.to_dict()) for item in items)

        current_cost = message_cost(current)
        if current_cost > message_budget:
            current = self._compact_current_chain(current, max_tool_chars=1_200)
            current_cost = message_cost(current)
        if current_cost > message_budget:
            current = self._compact_current_chain(current, max_tool_chars=400)
            current_cost = message_cost(current)
        if current_cost > message_budget:
            raise ToolPipelineError(
                "当前工具链超过模型上下文上限，请缩小查询范围后重试",
                code="context_budget_exceeded",
            )
        remaining = max(0, message_budget - current_cost)
        if message_cost(history) <= remaining:
            return tuple(history + current)

        groups: list[list[ModelMessage]] = []
        for message in history:
            if message.role == "user" or not groups:
                groups.append([])
            groups[-1].append(message)

        kept: list[list[ModelMessage]] = []
        for group in reversed(groups):
            cost = message_cost(group)
            if cost > remaining:
                break
            kept.append(group)
            remaining -= cost
        bounded_history = [message for group in reversed(kept) for message in group]
        return tuple(bounded_history + current)

    @classmethod
    def _compact_current_chain(
        cls,
        messages: Sequence[ModelMessage],
        *,
        max_tool_chars: int,
    ) -> list[ModelMessage]:
        result: list[ModelMessage] = []
        for message in messages:
            if message.role == "tool":
                result.append(
                    replace(
                        message,
                        content=cls._compact_tool_content(
                            message.content,
                            maximum=max_tool_chars,
                        ),
                    )
                )
            elif message.role == "assistant" and message.tool_calls and message.content:
                result.append(replace(message, content=""))
            else:
                result.append(message)
        return result

    @staticmethod
    def _compact_tool_content(content: str, *, maximum: int) -> str:
        text = str(content or "")
        json_text, _separator, suffix = text.partition("\nopaque_refs=")
        try:
            payload = json.loads(json_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        compact = {
            "ok": bool(payload.get("ok", True)) if isinstance(payload, dict) else True,
            "status": str(payload.get("status") or "success")[:80]
            if isinstance(payload, dict)
            else "success",
            "summary": str(payload.get("summary") or "工具执行完成")
            if isinstance(payload, dict)
            else "工具执行完成",
            "truncated": True,
        }
        reference_suffix = f"\nopaque_refs={suffix}" if suffix else ""
        budget = max(80, int(maximum) - len(reference_suffix) - 80)
        compact["summary"] = compact["summary"][:budget]
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        return encoded + reference_suffix

    @staticmethod
    def _capability_retrieval_context(
        state: SessionState,
    ) -> dict[str, tuple[str, ...]]:
        """提取有界跨轮线索供本地召回使用，不替模型裁决用户意图。"""
        recent_users: list[str] = []
        recent_tools: list[str] = []
        seen_tools: set[str] = set()
        for item in reversed(state.conversation[-24:]):
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role == "user" and len(recent_users) < 3:
                text = str(item.get("content") or "").strip()
                if text:
                    recent_users.append(text[:600])
            tool_name = str(item.get("tool_name") or "").strip()
            if tool_name and tool_name not in seen_tools and len(recent_tools) < 6:
                recent_tools.append(tool_name)
                seen_tools.add(tool_name)
            calls = item.get("tool_calls")
            if isinstance(calls, Sequence) and not isinstance(
                calls, (str, bytes, bytearray)
            ):
                for call in reversed(calls):
                    if not isinstance(call, Mapping):
                        continue
                    name = str(call.get("name") or "").strip()
                    if name and name not in seen_tools and len(recent_tools) < 6:
                        recent_tools.append(name)
                        seen_tools.add(name)
        return {
            "recent_user_messages": tuple(recent_users),
            "recent_tool_names": tuple(recent_tools),
        }

    @staticmethod
    def _contextual_message(agent_input: AgentInput) -> str:
        reply = agent_input.reply_context
        reply_text = (
            str(reply.get("text") or "").strip() if isinstance(reply, Mapping) else ""
        )
        if not reply_text:
            return agent_input.message
        return (
            f"{agent_input.message}\n\n"
            "<reply_context purpose=reference_resolution>\n"
            f"{reply_text[:2_000]}\n"
            "</reply_context>"
        )

    @staticmethod
    def _persisted_conversation(
        messages: Sequence[ModelMessage],
        *,
        current_user_index: int,
        original_message: str,
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        for message in messages:
            item = message.to_dict()
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list):
                item["tool_calls"] = [
                    {**dict(call), "arguments": {}}
                    for call in tool_calls
                    if isinstance(call, Mapping)
                ]
            stored.append(item)
        if 0 <= current_user_index < len(stored):
            stored[current_user_index] = {
                **stored[current_user_index],
                "content": original_message,
            }
        return stored

    @staticmethod
    def _restore_messages(state: SessionState) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for item in state.conversation[-60:]:
            if not isinstance(item, Mapping):
                continue
            try:
                message = ModelMessage.from_dict(item)
            except Exception as exc:  # noqa: BLE001 - isolate malformed persisted rows
                logger.warning("忽略无效 Agent 会话消息 type=%s", type(exc).__name__)
                continue
            if message.role in {"user", "assistant", "tool"}:
                messages.append(message)
        return messages

    @staticmethod
    def _tool_error_message(
        call: ModelToolCall, error: ToolPipelineError
    ) -> ModelMessage:
        content = json.dumps(
            {
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
