"""统一工具生命周期与副作用闸门。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.agent.public_safety import sanitize_public_text

from .capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from .effects import (
    ConfirmationEffectPlanStore,
    EffectPlan,
    EffectPlanStore,
    PreparedEffect,
)
from .projection import DefaultProjector, ReferenceValue, ToolOutcome
from .references import InMemoryReferenceStore, ReferenceStore
from .state import (
    CancellationToken,
    PublicationLease,
    SessionStateStore,
    StalePublicationError,
    StateUpdate,
)


class ToolPipelineError(RuntimeError):
    def __init__(self, message: str, *, code: str = "tool_failed") -> None:
        super().__init__(message)
        self.code = code


ProgressSink = Callable[[Mapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    owner: str
    session_id: str
    request_id: str
    turn_id: str
    lease: PublicationLease
    cancellation: CancellationToken
    report_progress: ProgressSink

    def policy_context(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "generation": self.lease.generation,
        }


@dataclass(frozen=True, slots=True)
class PipelineResult:
    tool: KernelToolSpec
    arguments: Mapping[str, Any]
    outcome: ToolOutcome
    effect_plan: EffectPlan | None = None
    elapsed_ms: int = 0


class AuthorizationPolicy(Protocol):
    async def authorize(
        self,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> None: ...


class RateLimiter(Protocol):
    async def acquire(self, *, owner: str, tool_name: str, cost: float) -> None: ...


class EffectLifecycle(Protocol):
    """已领取 EffectPlan 的确定性审计与运行代次生命周期。"""

    def completed(self, *, plan: EffectPlan, value: Any, elapsed_ms: int) -> None: ...

    def failed(self, *, plan: EffectPlan, code: str, elapsed_ms: int) -> None: ...

    def interrupted(self, *, plan: EffectPlan) -> None: ...


class NoopEffectLifecycle:
    def completed(self, *, plan: EffectPlan, value: Any, elapsed_ms: int) -> None:
        del plan, value, elapsed_ms

    def failed(self, *, plan: EffectPlan, code: str, elapsed_ms: int) -> None:
        del plan, code, elapsed_ms

    def interrupted(self, *, plan: EffectPlan) -> None:
        del plan


class DefaultAuthorizationPolicy:
    async def authorize(
        self,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> None:
        if tool.effect is not ToolEffect.READ and not context.owner:
            raise ToolPipelineError(
                "写操作需要已登录身份", code="authorization_required"
            )
        if tool.authorize is not None:
            try:
                allowed = bool(
                    tool.authorize(
                        {
                            **context.policy_context(),
                            "arguments": dict(arguments),
                            "effect": tool.effect.value,
                        }
                    )
                )
            except Exception as exc:
                raise ToolPipelineError(
                    "工具授权检查失败", code="authorization_failed"
                ) from exc
            if not allowed:
                raise ToolPipelineError(
                    "当前身份无权使用该工具", code="authorization_denied"
                )


class InMemoryRateLimiter:
    """按 owner/tool 的轻量滑动窗口限流；生产可替换而不改变 Pipeline。"""

    def __init__(self, *, limit: int = 30, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._lock = asyncio.Lock()
        self._events: dict[tuple[str, str], list[tuple[float, float]]] = {}

    async def acquire(self, *, owner: str, tool_name: str, cost: float) -> None:
        now = time.monotonic()
        key = (owner, tool_name)
        weighted_cost = max(0.1, float(cost or 1.0))
        async with self._lock:
            cutoff = now - self.window_seconds
            events = [
                (stamp, weight)
                for stamp, weight in self._events.get(key, ())
                if stamp > cutoff
            ]
            if sum(weight for _stamp, weight in events) + weighted_cost > self.limit:
                raise ToolPipelineError(
                    "工具调用过于频繁，请稍后重试", code="rate_limited"
                )
            events.append((now, weighted_cost))
            self._events[key] = events


_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _validate_json_schema(
    value: Any, schema: Mapping[str, Any], *, path: str = "arguments"
) -> None:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        expected = _TYPE_CHECKS.get(schema_type)
        if expected is not None:
            if schema_type in {"integer", "number"} and isinstance(value, bool):
                raise ToolPipelineError(f"{path} 类型无效", code="invalid_arguments")
            if not isinstance(value, expected):
                raise ToolPipelineError(f"{path} 类型无效", code="invalid_arguments")
    if "enum" in schema and value not in schema.get("enum", ()):
        raise ToolPipelineError(f"{path} 不在允许范围内", code="invalid_arguments")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0) or 0):
            raise ToolPipelineError(f"{path} 太短", code="invalid_arguments")
        maximum = schema.get("maxLength")
        if maximum is not None and len(value) > int(maximum):
            raise ToolPipelineError(f"{path} 太长", code="invalid_arguments")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            raise ToolPipelineError(f"{path} 格式无效", code="invalid_arguments")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise ToolPipelineError(f"{path} 小于允许值", code="invalid_arguments")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise ToolPipelineError(f"{path} 超过允许值", code="invalid_arguments")
    if isinstance(value, dict):
        required = schema.get("required") or ()
        for key in required:
            if key not in value:
                raise ToolPipelineError(
                    f"缺少必需参数：{key}", code="invalid_arguments"
                )
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unexpected = set(value).difference(properties)
            if unexpected:
                raise ToolPipelineError(
                    f"包含未知参数：{min(unexpected)}", code="invalid_arguments"
                )
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate_json_schema(item, child, path=f"{path}.{key}")
    if isinstance(value, (list, tuple)) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            _validate_json_schema(item, schema["items"], path=f"{path}[{index}]")


async def _invoke(handler: Callable[..., Any], *args: Any) -> Any:
    try:
        if inspect.iscoroutinefunction(handler):
            return await handler(*args)
        value = await asyncio.to_thread(handler, *args)
        if inspect.isawaitable(value):
            return await value
        return value
    except ToolPipelineError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ToolPipelineError("工具执行失败", code="tool_execution_failed") from exc


class ToolPipeline:
    """所有 READ、WRITE 与 DANGER 工具唯一允许经过的生命周期。"""

    def __init__(
        self,
        *,
        catalog: ToolCatalog,
        state_store: SessionStateStore,
        reference_store: ReferenceStore | None = None,
        effect_store: EffectPlanStore | None = None,
        projector: DefaultProjector | None = None,
        authorization: AuthorizationPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        effect_lifecycle: EffectLifecycle | None = None,
    ) -> None:
        self.catalog = catalog
        self.state_store = state_store
        self.reference_store = reference_store or InMemoryReferenceStore()
        self.effect_store = effect_store or ConfirmationEffectPlanStore()
        self.projector = projector or DefaultProjector()
        self.authorization = authorization or DefaultAuthorizationPolicy()
        self.rate_limiter = rate_limiter or InMemoryRateLimiter()
        self.effect_lifecycle = effect_lifecycle or NoopEffectLifecycle()

    def _effect_completed(self, plan: EffectPlan, value: Any, elapsed_ms: int) -> None:
        try:
            self.effect_lifecycle.completed(
                plan=plan,
                value=value,
                elapsed_ms=elapsed_ms,
            )
        except Exception:  # noqa: BLE001 - audit hooks may not change effect outcome
            return

    def _effect_failed(self, plan: EffectPlan, code: str, elapsed_ms: int) -> None:
        try:
            self.effect_lifecycle.failed(
                plan=plan,
                code=code,
                elapsed_ms=elapsed_ms,
            )
        except Exception:  # noqa: BLE001 - audit hooks may not mask domain failure
            return

    def _effect_interrupted(self, plan: EffectPlan) -> None:
        try:
            self.effect_lifecycle.interrupted(plan=plan)
        except Exception:  # noqa: BLE001 - audit hooks may not mask interruption
            return

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: ToolCallContext,
    ) -> PipelineResult:
        started = time.monotonic()
        context.cancellation.raise_if_cancelled()
        try:
            tool = self.catalog.get(name)
        except KeyError as exc:
            raise ToolPipelineError("未知工具", code="tool_not_found") from exc
        raw_arguments = dict(arguments or {})
        _validate_json_schema(raw_arguments, tool.input_schema)
        try:
            normalized = tool.validator(raw_arguments)
        except ToolPipelineError:
            raise
        except Exception as exc:
            safe_message = sanitize_public_text(
                getattr(exc, "safe_message", ""),
                limit=500,
            )
            raise ToolPipelineError(
                safe_message or "工具参数无效",
                code="invalid_arguments",
            ) from exc
        if not isinstance(normalized, dict):
            raise ToolPipelineError(
                "工具参数校验器返回无效结果", code="invalid_arguments"
            )
        resolved = await self._resolve_references(normalized, context=context)
        await self.authorization.authorize(tool, resolved, context)
        await self.rate_limiter.acquire(
            owner=context.owner,
            tool_name=tool.name,
            cost=tool.cost,
        )
        context.cancellation.raise_if_cancelled()

        if tool.effect is ToolEffect.READ:
            if tool.read is None:  # pragma: no cover - ToolSpec 已校验
                raise ToolPipelineError("工具不可执行", code="tool_not_executable")
            value = await _invoke(tool.read, resolved, context)
            outcome = await self._materialize_refs(
                self.projector.project(value), context=context
            )
            await self._commit_updates(context.lease, outcome.state_updates)
            return PipelineResult(
                tool=tool,
                arguments=normalized,
                outcome=outcome,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )

        if tool.prepare is None:  # pragma: no cover - ToolSpec 已校验
            raise ToolPipelineError("工具不支持预检", code="confirmation_not_supported")
        prepared_value = await _invoke(tool.prepare, resolved, context)
        if not isinstance(prepared_value, PreparedEffect):
            raise ToolPipelineError(
                "工具预检返回无效结果", code="invalid_effect_preview"
            )
        if not prepared_value.snapshot_fingerprint:
            raise ToolPipelineError(
                "工具预检缺少快照指纹", code="invalid_effect_preview"
            )
        if not await self.state_store.is_current(context.lease):
            raise StalePublicationError("turn lost publication authority")
        plan = await asyncio.to_thread(
            self.effect_store.freeze,
            owner=context.owner,
            session_id=context.session_id,
            generation=context.lease.generation,
            tool_name=tool.name,
            effect=tool.effect,
            arguments=normalized,
            prepared=prepared_value,
        )
        preview_outcome = await self._materialize_refs(
            self.projector.project(prepared_value.preview), context=context
        )
        updates = tuple(preview_outcome.state_updates) + (
            StateUpdate("pending_effect_plan_id", plan.plan_id),
        )
        outcome = replace(preview_outcome, state_updates=updates, effect_plan=plan)
        await self._commit_updates(context.lease, updates)
        return PipelineResult(
            tool=tool,
            arguments=normalized,
            outcome=outcome,
            effect_plan=plan,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    async def execute_confirmed(
        self,
        plan_id: str,
        *,
        context: ToolCallContext,
    ) -> PipelineResult:
        started = time.monotonic()
        context.cancellation.raise_if_cancelled()
        if not await self.state_store.is_current(context.lease):
            raise StalePublicationError("turn lost publication authority")
        try:
            plan = await asyncio.to_thread(
                self.effect_store.claim,
                owner=context.owner,
                session_id=context.session_id,
                generation=context.lease.generation,
                plan_id=plan_id,
            )
        except ToolPipelineError:
            raise
        except Exception as exc:
            raise ToolPipelineError(
                "确认计划无效、已过期或已被使用",
                code="confirmation_invalid",
            ) from exc

        try:
            try:
                tool = self.catalog.get(plan.tool_name)
            except KeyError as exc:
                raise ToolPipelineError(
                    "确认计划对应工具不存在", code="tool_not_found"
                ) from exc
            if tool.effect is ToolEffect.READ or tool.effect is not plan.effect:
                raise ToolPipelineError(
                    "确认计划风险类型不匹配", code="confirmation_invalid"
                )
            await self.authorization.authorize(tool, plan.arguments, context)
            await self.rate_limiter.acquire(
                owner=context.owner,
                tool_name=f"confirm:{tool.name}",
                cost=max(1.0, tool.cost),
            )
            context.cancellation.raise_if_cancelled()
            if tool.execute_confirmed is None:  # pragma: no cover - ToolSpec 已校验
                raise ToolPipelineError(
                    "工具不支持确认执行", code="confirmation_not_supported"
                )
            value = await _invoke(
                tool.execute_confirmed,
                dict(plan.arguments),
                plan.snapshot_fingerprint,
                context,
            )
            if tool.verify is not None:
                verified = await _invoke(
                    tool.verify,
                    dict(plan.arguments),
                    value,
                    context,
                )
                if verified is False:
                    raise ToolPipelineError(
                        "写后验证失败", code="post_write_verification_failed"
                    )
                if verified is not True and verified is not None:
                    value = verified
        except ToolPipelineError as exc:
            self._effect_failed(
                plan,
                exc.code,
                max(0, int((time.monotonic() - started) * 1000)),
            )
            raise
        except BaseException:
            self._effect_interrupted(plan)
            raise

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        # 写操作与写后验证已取得可信终态。先收束审计并推进全局运行代次；
        # 后续 DTO 投影、引用或会话提交失败，不得把已发生的副作用伪装成未执行。
        self._effect_completed(plan, value, elapsed_ms)
        outcome = await self._materialize_refs(
            self.projector.project(value), context=context
        )
        updates = tuple(outcome.state_updates) + (
            StateUpdate("pending_effect_plan_id", ""),
        )
        outcome = replace(outcome, state_updates=updates, effect_plan=plan)
        await self._commit_updates(context.lease, updates)
        return PipelineResult(
            tool=tool,
            arguments=dict(plan.arguments),
            outcome=outcome,
            effect_plan=plan,
            elapsed_ms=elapsed_ms,
        )

    async def cancel_effect(
        self,
        plan_id: str,
        *,
        context: ToolCallContext,
    ) -> bool:
        cancelled = await asyncio.to_thread(
            self.effect_store.cancel,
            owner=context.owner,
            session_id=context.session_id,
            plan_id=plan_id,
        )
        # 即使票据已过期或被抢先消费，当前会话也不能继续保留一个
        # 永远无法执行的 pending_effect_plan_id。
        await self._commit_updates(
            context.lease,
            (StateUpdate("pending_effect_plan_id", ""),),
        )
        return bool(cancelled)

    async def _resolve_references(
        self,
        value: Any,
        *,
        context: ToolCallContext,
        key: str = "",
    ) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                if (
                    child_key.endswith("_ref")
                    and isinstance(child_value, str)
                    and child_value.startswith("ref_")
                ):
                    expected_kind = child_key[:-4].strip("_").casefold()
                    result[
                        child_key[:-4] or child_key
                    ] = await self.reference_store.resolve(
                        child_value,
                        owner=context.owner,
                        session_id=context.session_id,
                        expected_kind=expected_kind,
                    )
                else:
                    result[child_key] = await self._resolve_references(
                        child_value, context=context, key=child_key
                    )
            return result
        if isinstance(value, list):
            return [
                await self._resolve_references(item, context=context, key=key)
                for item in value
            ]
        return value

    async def _materialize_refs(
        self,
        outcome: ToolOutcome,
        *,
        context: ToolCallContext,
    ) -> ToolOutcome:
        if not outcome.refs:
            return outcome
        exposed: list[dict[str, str]] = []
        kinds: list[str] = []
        ids: list[str] = []
        for item in outcome.refs:
            if not isinstance(item, ReferenceValue):
                raise ToolPipelineError(
                    "工具返回了无效引用", code="invalid_tool_result"
                )
            reference = await self.reference_store.put(
                owner=context.owner,
                session_id=context.session_id,
                kind=item.kind,
                value=item.value,
                ttl_seconds=item.ttl_seconds,
            )
            exposed.append({"ref": reference.ref, "kind": reference.kind})
            ids.append(reference.ref)
            kinds.append(reference.kind)
        public = dict(outcome.public_content)
        public["refs"] = exposed
        model_content = (
            outcome.model_content.rstrip()
            + "\nopaque_refs="
            + json.dumps(exposed, ensure_ascii=False, separators=(",", ":"))
        )
        updates = tuple(outcome.state_updates) + (
            StateUpdate("recent_refs", ids, mode="append"),
            StateUpdate("ref_kinds", kinds, mode="append"),
        )
        return replace(
            outcome,
            public_content=public,
            model_content=model_content,
            state_updates=updates,
        )

    async def _commit_updates(
        self,
        lease: PublicationLease,
        updates: Sequence[StateUpdate],
    ) -> None:
        if not updates:
            if not await self.state_store.is_current(lease):
                raise StalePublicationError("turn lost publication authority")
            return
        await self.state_store.commit(lease, updates=updates)
