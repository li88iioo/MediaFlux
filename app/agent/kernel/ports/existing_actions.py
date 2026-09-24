"""把已验证的 MediaFlux 领域 action 映射为 Kernel 原子工具。

此层只复用领域能力，不承担自然语言编排；每个 handler 都由 ToolPipeline
统一完成校验、权限、Effect Gate、投影与提交。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.agent.confirmation_contract import build_confirmation_contract
from app.agent.domain_tool_metadata import (
    DOMAIN_ALIASES,
    PREFIX_RETRIEVAL_TERMS,
    TOOL_RETRIEVAL_TERMS,
)
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_admission,
    current_agent_runtime_generation,
)
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.owner_routes import telegram_owner_route_is_currently_authorized
from app.agent.public_safety import sanitize_public_text

from ..capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from ..effects import PreparedEffect
from ..pipeline import ToolCallContext, ToolPipelineError
from ..session_guard import guarded_state_call

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


def _production_owner_kind(owner: object) -> str:
    value = str(owner or "")
    if value.startswith("webk:v1:"):
        return "web"
    if value.startswith("tg:v1:"):
        return "telegram"
    return ""


def _bind_runtime_snapshot(fingerprint: str, context: ToolCallContext) -> str:
    if not _production_owner_kind(context.owner):
        return fingerprint
    return f"mfkr1:{current_agent_runtime_generation()}:{fingerprint}"


def _split_runtime_snapshot(
    fingerprint: str,
    context: ToolCallContext,
) -> tuple[int | None, str]:
    if not _production_owner_kind(context.owner):
        return None, fingerprint
    prefix, separator, remainder = str(fingerprint or "").partition(":")
    generation_text, separator2, domain_fingerprint = remainder.partition(":")
    if prefix != "mfkr1" or not separator or not separator2:
        raise ToolPipelineError("确认计划缺少运行态快照", code="confirmation_invalid")
    try:
        generation = int(generation_text)
    except (TypeError, ValueError) as exc:
        raise ToolPipelineError(
            "确认计划运行态快照无效", code="confirmation_invalid"
        ) from exc
    return generation, domain_fingerprint


def _kernel_context(context: ToolCallContext) -> ToolContext:
    return ToolContext(
        owner=context.owner,
        session_id=context.session_id,
        request_id=context.request_id,
        confirmation_bootstrap=False,
        capability_search=context.capability_search,
        cancelled=lambda: context.cancellation.cancelled,
    )


def _public_error_message(value: object, *, fallback: str) -> str:
    return sanitize_public_text(value, limit=500) or fallback


def _safe_error(exc: Exception, *, fallback_code: str) -> ToolPipelineError:
    # 只有领域显式声明的 safe_message 才可跨越 ToolPipeline 边界；
    # 未预期异常的 str(exc) 可能包含绝对路径、URL 或凭据。
    message = _public_error_message(
        getattr(exc, "safe_message", ""),
        fallback="领域能力暂时不可用",
    )
    raw_code = str(getattr(exc, "code", "") or fallback_code).strip().casefold()
    code = raw_code if _ERROR_CODE_RE.fullmatch(raw_code) else fallback_code
    return ToolPipelineError(message, code=code)


def _assert_success(result: ToolResult, *, code: str) -> ToolResult:
    if not isinstance(result, ToolResult):
        raise ToolPipelineError("领域能力返回无效结果", code="invalid_tool_result")
    if not result.ok:
        message = _public_error_message(
            result.error or result.summary,
            fallback="领域能力未能完成请求",
        )
        domain_code = str(result.status or "").strip().casefold()
        # 领域服务已经把 conflict / busy / stale / outcome_unknown 等
        # 用户可处理状态标准化；Kernel 只校验其机器码形状，不应再次抹平。
        safe_code = domain_code if _ERROR_CODE_RE.fullmatch(domain_code) else code
        raise ToolPipelineError(message, code=safe_code)
    return result


def _domain(spec: ToolSpec) -> str:
    prefix = spec.name.partition(".")[0].casefold()
    candidate = spec.domains[0] if spec.domains else DOMAIN_ALIASES.get(prefix, prefix)
    candidate = re.sub(r"[^a-z0-9_.-]+", "_", str(candidate).casefold()).strip("_.-")
    return candidate or "system"


def _effect(spec: ToolSpec) -> ToolEffect:
    if spec.risk is RiskLevel.READ and not spec.requires_confirmation:
        return ToolEffect.READ
    if spec.risk is RiskLevel.DANGER:
        return ToolEffect.DANGER
    return ToolEffect.WRITE


def _retrieval_terms(spec: ToolSpec) -> tuple[str, ...]:
    prefix = spec.name.partition(".")[0].casefold()
    values = (
        *spec.domains,
        spec.source_kind,
        *PREFIX_RETRIEVAL_TERMS.get(prefix, ()),
        *TOOL_RETRIEVAL_TERMS.get(spec.name, ()),
    )
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _related_tools(spec: ToolSpec) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(item).strip() for item in spec.related_tools if str(item).strip()
        )
    )


def adapt_tool_spec(spec: ToolSpec) -> KernelToolSpec:
    effect = _effect(spec)

    if effect is ToolEffect.READ:

        def read(arguments: dict[str, Any], context: ToolCallContext) -> ToolResult:
            try:
                if spec.context_handler is not None:
                    result = spec.context_handler(arguments, _kernel_context(context))
                elif spec.handler is not None:
                    result = spec.handler(arguments)
                else:  # pragma: no cover - 领域 ToolSpec 声明已保证
                    raise ToolPipelineError(
                        "领域能力不可执行", code="tool_not_executable"
                    )
            except ToolPipelineError:
                raise
            except Exception as exc:
                raise _safe_error(exc, fallback_code="tool_execution_failed") from exc
            if not isinstance(result, ToolResult):
                raise ToolPipelineError(
                    "领域能力返回无效结果", code="invalid_tool_result"
                )
            return result

        return KernelToolSpec(
            name=spec.name,
            model_name=spec.model_name,
            domain=_domain(spec),
            description=spec.description,
            examples=spec.examples,
            input_schema=spec.parameters,
            effect=effect,
            validator=spec.validator,
            read=read,
            runtime_status=spec.runtime_status,
            cost=1.0 if spec.freshness != "live" else 1.25,
            metadata={
                "source_kind": spec.source_kind,
                "freshness": spec.freshness,
                "workflow": spec.workflow,
                "workflow_stage": spec.workflow_stage,
                "related_tools": _related_tools(spec),
                "retrieval_terms": _retrieval_terms(spec),
                "risk": spec.risk.value,
            },
        )

    def prepare(arguments: dict[str, Any], context: ToolCallContext) -> PreparedEffect:
        handler = spec.context_confirmation_preparer
        if handler is None:  # pragma: no cover - 领域 ToolSpec 声明已保证
            raise ToolPipelineError(
                "领域能力不支持预检", code="confirmation_not_supported"
            )
        try:
            result, fingerprint = handler(arguments, _kernel_context(context))
        except ToolPipelineError:
            raise
        except Exception as exc:
            raise _safe_error(exc, fallback_code="precondition_failed") from exc
        checked = _assert_success(result, code="precondition_failed")
        effect_metadata = (
            dict(checked.effect_metadata)
            if isinstance(checked.effect_metadata, dict)
            else {}
        )
        return PreparedEffect(
            preview=checked.to_dict(),
            snapshot_fingerprint=_bind_runtime_snapshot(
                str(fingerprint or ""), context
            ),
            metadata={
                **effect_metadata,
                "source_kind": spec.source_kind,
                "risk": spec.risk.value,
                "confirmation": build_confirmation_contract(
                    tool_name=spec.name,
                    risk=spec.risk,
                    preview=checked,
                ),
            },
        )

    def execute_confirmed(
        arguments: dict[str, Any],
        expected_snapshot: str,
        context: ToolCallContext,
    ) -> ToolResult:
        handler = spec.context_confirmed_handler
        if handler is None:  # pragma: no cover - 领域 ToolSpec 声明已保证
            raise ToolPipelineError(
                "领域能力不支持确认执行", code="confirmation_not_supported"
            )
        runtime_generation, domain_snapshot = _split_runtime_snapshot(
            expected_snapshot, context
        )
        owner_kind = _production_owner_kind(context.owner)
        try:
            if owner_kind:
                with agent_runtime_admission(
                    require_telegram=owner_kind == "telegram",
                    expected_generation=runtime_generation,
                ):
                    if (
                        owner_kind == "telegram"
                        and not telegram_owner_route_is_currently_authorized(
                            context.owner
                        )
                    ):
                        raise AgentRuntimeDisabled("Telegram Agent 授权已变化")
                    result = handler(
                        arguments, domain_snapshot, _kernel_context(context)
                    )
            else:
                result = handler(arguments, domain_snapshot, _kernel_context(context))
        except ToolPipelineError:
            raise
        except AgentRuntimeDisabled as exc:
            raise ToolPipelineError(
                "Media Agent 授权或运行状态已变化", code="agent_disabled"
            ) from exc
        except Exception as exc:
            raise _safe_error(exc, fallback_code="tool_execution_failed") from exc
        if not isinstance(result, ToolResult):
            raise ToolPipelineError("领域能力返回无效结果", code="invalid_tool_result")
        # 已确认写操作允许返回 conflict / busy / outcome_unknown 等可信终态。
        # Pipeline 会保留完整安全 DTO、完成审计，并由入口发布 effect.failed；
        # 只有异常或写后验证失败才进入 ToolPipelineError。
        return result

    async def verify(
        arguments: dict[str, Any], value: Any, context: ToolCallContext
    ) -> Any:
        if not isinstance(value, ToolResult):
            raise ToolPipelineError(
                "写后验证输入无效", code="post_write_verification_failed"
            )
        verified = value
        post_write_verifier = spec.post_write_verifier
        if post_write_verifier is not None:
            try:
                verified = await guarded_state_call(
                    context.owner,
                    context.session_id,
                    post_write_verifier,
                    arguments,
                    value,
                    kind="effect",
                    complete_on_cancel=True,
                )
            except Exception as exc:
                raise _safe_error(
                    exc, fallback_code="post_write_verification_failed"
                ) from exc
            if not isinstance(verified, ToolResult):
                raise ToolPipelineError(
                    "领域能力返回无效结果", code="invalid_tool_result"
                )
        if not context.wait_for_completion:
            return verified
        return await wait_for_effect_completion(
            verified,
            tool=spec.name,
            context=_kernel_context(context),
            report_progress=context.report_progress,
        )

    return KernelToolSpec(
        name=spec.name,
        model_name=spec.model_name,
        domain=_domain(spec),
        description=spec.description,
        examples=spec.examples,
        input_schema=spec.parameters,
        effect=effect,
        validator=spec.validator,
        runtime_status=spec.runtime_status,
        prepare=prepare,
        execute_confirmed=execute_confirmed,
        verify=verify,
        cost=2.0 if effect is ToolEffect.DANGER else 1.5,
        metadata={
            "source_kind": spec.source_kind,
            "freshness": spec.freshness,
            "workflow": spec.workflow,
            "workflow_stage": spec.workflow_stage,
            "related_tools": _related_tools(spec),
            "retrieval_terms": _retrieval_terms(spec),
            "risk": spec.risk.value,
        },
    )


def catalog_from_tool_specs(specs: Iterable[ToolSpec]) -> ToolCatalog:
    return ToolCatalog(adapt_tool_spec(spec) for spec in specs)
