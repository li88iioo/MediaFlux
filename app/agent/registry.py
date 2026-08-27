"""受控工具注册表：显式 schema、风险门与统一错误。"""
from __future__ import annotations

import re
import secrets
from time import monotonic
from typing import Any

from app.agent.models import (
    ConfirmedToolHandler,
    ConfirmationPreparer,
    ContextualConfirmationPreparer,
    ContextualConfirmedToolHandler,
    ContextualToolHandler,
    LLMToolDisposition,
    PostWriteVerifier,
    RiskLevel,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
)
from app.agent.metrics import agent_metrics
from app.agent.tool_semantics import (
    default_result_presentation,
    default_stages_resource_candidates,
)
from app.agent.observability import safe_exception_summary
from app.logger import get_logger

logger = get_logger(__name__)
_NATIVE_ALIAS_RE = re.compile(r"^mf_[A-Za-z0-9_-]{1,61}$")
_LLM_EXAMPLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LLM_SEMANTIC_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")



class AgentToolError(ValueError):
    """可安全映射到 API 的工具调用错误。"""

    def __init__(self, message: str, *, code: str = "invalid_tool_call"):
        super().__init__(message)
        self.safe_message = str(message)
        self.code = code


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        name = str(spec.name or "").strip()
        if not name or name in self._tools:
            raise ValueError(f"invalid or duplicate tool name: {name}")
        if spec.requires_confirmation and spec.risk is RiskLevel.READ:
            raise ValueError(f"read-only tool cannot require confirmation: {name}")
        if spec.confirmed_handler is not None and not spec.requires_confirmation:
            raise ValueError(f"confirmed handler requires confirmation: {name}")
        if spec.confirmation_state_cleaner is not None and not spec.requires_confirmation:
            raise ValueError(f"confirmation state cleaner requires confirmation: {name}")
        if spec.confirmation_preparer is not None and not spec.requires_confirmation:
            raise ValueError(f"confirmation preparer requires confirmation: {name}")
        if spec.context_confirmation_preparer is not None and not spec.requires_confirmation:
            raise ValueError(f"context confirmation preparer requires confirmation: {name}")
        if spec.context_confirmed_handler is not None and not spec.requires_confirmation:
            raise ValueError(f"context confirmed handler requires confirmation: {name}")
        if spec.confirmation_preparer is not None and spec.context_confirmation_preparer is not None:
            raise ValueError(f"duplicate confirmation preparer modes: {name}")
        if spec.confirmed_handler is not None and spec.context_confirmed_handler is not None:
            raise ValueError(f"duplicate confirmed handler modes: {name}")
        if spec.llm_read and (spec.risk is not RiskLevel.READ or spec.requires_confirmation):
            raise ValueError(f"LLM read tool must be confirmation-free READ: {name}")
        if spec.llm_read_plan and not spec.llm_read:
            raise ValueError(f"LLM plan tool must also be LLM readable: {name}")
        if spec.result_presentation not in {"narrative", "resource_candidates"}:
            raise ValueError(f"invalid tool result presentation: {name}")
        if (
            len(spec.llm_domains) > 8
            or any(
                not isinstance(domain, str)
                or not _LLM_SEMANTIC_LABEL_RE.fullmatch(domain.strip())
                for domain in spec.llm_domains
            )
        ):
            raise ValueError(f"invalid LLM capability domains: {name}")
        if not _LLM_SEMANTIC_LABEL_RE.fullmatch(
            str(spec.llm_source_kind or "").strip()
        ):
            raise ValueError(f"invalid LLM capability source: {name}")
        if spec.llm_evidence_role not in {"primary", "supporting"}:
            raise ValueError(f"invalid LLM capability evidence role: {name}")
        if spec.llm_freshness not in {
            "realtime", "live", "snapshot", "cached", "derived", "historical"
        }:
            raise ValueError(f"invalid LLM capability freshness: {name}")
        if not isinstance(spec.llm_parallel_safe, bool):
            raise ValueError(f"invalid LLM capability parallel flag: {name}")
        if spec.llm_confirmation and (
            spec.risk is RiskLevel.READ
            or not spec.requires_confirmation
            or (
                spec.preview_handler is None
                and spec.confirmation_preparer is None
                and spec.context_confirmation_preparer is None
            )
        ):
            raise ValueError(
                f"LLM confirmation tool must be a confirmation-gated non-READ tool: {name}"
            )
        native_exposed = spec.llm_read or spec.llm_confirmation
        alias = self._native_alias_for_spec(spec) if native_exposed else ""
        if spec.native_alias and not native_exposed:
            raise ValueError(
                f"native alias requires LLM orchestration exposure: {name}"
            )
        if alias and not _NATIVE_ALIAS_RE.fullmatch(alias):
            raise ValueError(f"invalid native tool alias: {alias}")
        if alias and any(
            self._native_alias_for_spec(item) == alias for item in self._tools.values()
            if item.llm_read or item.llm_confirmation
        ):
            raise ValueError(f"duplicate native tool alias: {alias}")
        if (
            len(spec.llm_examples) > 6
            or any(
                not isinstance(example, str)
                or not example.strip()
                or len(example.strip()) > 160
                or _LLM_EXAMPLE_CONTROL_RE.search(example)
                for example in spec.llm_examples
            )
        ):
            raise ValueError(f"invalid LLM routing examples: {name}")
        self._tools[name] = spec

    def capabilities(self) -> list[dict[str, Any]]:
        return [self._tools[name].public_dict() for name in sorted(self._tools)]

    def llm_read_capabilities(self) -> list[dict[str, Any]]:
        return [
            self._tools[name].llm_capability_dict()
            for name in sorted(self._tools)
            if self._tools[name].llm_read
        ]

    def llm_read_plan_capabilities(self) -> list[dict[str, Any]]:
        return [
            self._tools[name].llm_capability_dict()
            for name in sorted(self._tools)
            if self._tools[name].llm_read_plan
        ]

    def llm_confirmation_capabilities(self) -> list[dict[str, Any]]:
        return [
            self._tools[name].llm_capability_dict()
            for name in sorted(self._tools)
            if self._tools[name].llm_confirmation
        ]

    def llm_orchestration_capabilities(
        self, *, include_confirmations: bool = True
    ) -> list[dict[str, Any]]:
        """返回统一语义路由可见工具；匿名调用方只能获得只读能力。"""
        return [
            self._tools[name].llm_capability_dict()
            for name in sorted(self._tools)
            if self._tools[name].llm_read
            or (include_confirmations and self._tools[name].llm_confirmation)
        ]

    def llm_disposition_for(self, name: str) -> LLMToolDisposition:
        """由注册表而非模型决定工具可执行还是只能创建确认票据。"""
        spec = self._get_spec(name)
        if spec.llm_read and spec.risk is RiskLevel.READ and not spec.requires_confirmation:
            return LLMToolDisposition.EXECUTE_READ
        if (
            spec.llm_confirmation
            and spec.risk is not RiskLevel.READ
            and spec.requires_confirmation
        ):
            return LLMToolDisposition.PREPARE_CONFIRMATION
        raise AgentToolError("该工具未开放给 Agent 编排", code="tool_not_exposed")

    def validate_llm_orchestration_call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> tuple[LLMToolDisposition, dict[str, Any]]:
        """统一校验模型选择，且绝不在此执行任何 handler。"""
        spec = self._get_spec(name)
        disposition = self.llm_disposition_for(spec.name)
        return disposition, self._normalize_arguments(spec, arguments)

    @staticmethod
    def _native_alias_for_spec(spec: ToolSpec) -> str:
        return str(spec.native_alias or f"mf_{spec.name.replace('.', '_')}").strip()

    def native_alias_for(self, name: str) -> str:
        spec = self._get_spec(name)
        return (
            self._native_alias_for_spec(spec)
            if spec.llm_read or spec.llm_confirmation
            else ""
        )

    def native_aliases(self) -> frozenset[str]:
        return frozenset(
            self._native_alias_for_spec(spec)
            for spec in self._tools.values()
            if spec.llm_read or spec.llm_confirmation
        )

    def native_tool_name(self, alias: str) -> str | None:
        target = str(alias or "").strip()
        for name, spec in self._tools.items():
            if (
                (spec.llm_read or spec.llm_confirmation)
                and self._native_alias_for_spec(spec) == target
            ):
                return name
        return None

    def has(self, name: str) -> bool:
        return str(name or "").strip() in self._tools

    def risk_for(self, name: str) -> RiskLevel:
        """返回已注册工具的风险等级，不暴露内部 ToolSpec。"""
        return self._get_spec(name).risk

    def result_presentation_for(self, name: str) -> str:
        """返回工具声明的结果展示语义，并兼容旧 ToolSpec。"""
        spec = self._get_spec(name)
        if spec.result_presentation != "narrative":
            return spec.result_presentation
        return default_result_presentation(spec.name)

    def stages_resource_candidates_for(self, name: str) -> bool:
        """返回工具是否产生可供本轮认知链使用的候选证据。"""
        spec = self._get_spec(name)
        return bool(
            spec.stages_resource_candidates
            or default_stages_resource_candidates(spec.name)
        )

    def llm_parallel_safe_for(self, name: str) -> bool:
        """返回只读能力是否允许与本轮其他只读工具并行。"""
        return bool(self._get_spec(name).llm_parallel_safe)

    def validate_read_call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """仅校验只读调用，不执行 handler；用于原子预检复合计划。"""
        spec = self._get_spec(name)
        if spec.risk is not RiskLevel.READ or spec.requires_confirmation:
            raise AgentToolError(
                "该工具不能进入只读计划", code="confirmation_required"
            )
        return self._normalize_arguments(spec, arguments)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> tuple[ToolResult, int]:
        spec = self._get_spec(name)
        if spec.risk is not RiskLevel.READ:
            raise AgentToolError("该工具需要确认，不能直接执行", code="confirmation_required")
        normalized = self._normalize_arguments(spec, arguments)
        if spec.context_handler is not None:
            return self._call_context(
                spec.name,
                spec.context_handler,
                normalized,
                context or ToolContext(),
            )
        return self._call(spec.name, spec.handler, normalized, context or ToolContext())

    def prepare_confirmation(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> tuple[ToolSpec, dict[str, Any], str, ToolResult, int]:
        spec = self._get_spec(name)
        self._clear_confirmation_state(spec)
        if (
            spec.risk is RiskLevel.READ
            or not spec.requires_confirmation
            or (
                spec.preview_handler is None
                and spec.confirmation_preparer is None
                and spec.context_confirmation_preparer is None
            )
        ):
            raise AgentToolError("该工具不支持确认执行", code="confirmation_not_supported")
        normalized = self._normalize_arguments(spec, arguments)
        if spec.context_confirmation_preparer is not None:
            result, context_fingerprint, elapsed_ms = self._call_context_preparer(
                spec.name,
                spec.context_confirmation_preparer,
                normalized,
                context or ToolContext(),
            )
        elif spec.confirmation_preparer is not None:
            result, context_fingerprint, elapsed_ms = self._call_preparer(
                spec.name,
                spec.confirmation_preparer,
                normalized,
                context or ToolContext(),
            )
        else:
            result, elapsed_ms = self._call(
                spec.name, spec.preview_handler, normalized, context or ToolContext()
            )
            context_fingerprint = ""
            if spec.confirmation_context is not None:
                try:
                    context_fingerprint = str(spec.confirmation_context(dict(normalized)) or "")
                except Exception as exc:
                    self._clear_confirmation_state(spec)
                    logger.warning(
                        "Agent 确认上下文生成失败 tool=%s type=%s",
                        spec.name,
                        type(exc).__name__,
                    )
                    raise AgentToolError(
                        "暂时无法创建确认请求",
                        code="confirmation_unavailable",
                    ) from exc
        if not result.ok:
            self._clear_confirmation_state(spec)
            raise AgentToolError(result.error or result.summary or "动作预检未通过", code="precondition_failed")
        self._clear_confirmation_state(spec)
        return spec, normalized, context_fingerprint, result, elapsed_ms

    def execute_confirmed(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_context: str = "",
        context: ToolContext | None = None,
    ) -> tuple[ToolResult, int]:
        spec = self._get_spec(name)
        self._clear_confirmation_state(spec)
        if spec.risk is RiskLevel.READ or not spec.requires_confirmation:
            raise AgentToolError("该工具不支持确认执行", code="confirmation_not_supported")
        normalized = self._normalize_arguments(spec, arguments)
        if spec.confirmation_context is not None:
            try:
                current_context = str(spec.confirmation_context(dict(normalized)) or "")
            except Exception as exc:
                logger.warning("Agent 确认上下文校验失败 tool=%s type=%s", spec.name, type(exc).__name__)
                raise AgentToolError("确认请求已失效，请重新预检", code="confirmation_stale") from exc
            if not secrets.compare_digest(current_context, str(expected_context or "")):
                self._clear_confirmation_state(spec)
                raise AgentToolError("相关配置已变化，请重新预检", code="confirmation_stale")
        try:
            if spec.context_confirmed_handler is not None:
                result, elapsed_ms = self._call_context_confirmed(
                    spec.name,
                    spec.context_confirmed_handler,
                    normalized,
                    str(expected_context or ""),
                    context or ToolContext(),
                )
            elif spec.confirmed_handler is not None:
                result, elapsed_ms = self._call_confirmed(
                    spec.name,
                    spec.confirmed_handler,
                    normalized,
                    str(expected_context or ""),
                    context or ToolContext(),
                )
            else:
                result, elapsed_ms = self._call(
                    spec.name, spec.handler, normalized, context or ToolContext()
                )
        finally:
            self._clear_confirmation_state(spec)
        if result.ok and spec.post_write_verifier is not None:
            result, verify_elapsed_ms = self._call_post_write_verifier(
                spec.name,
                spec.post_write_verifier,
                normalized,
                result,
            )
            elapsed_ms += verify_elapsed_ms
        return result, elapsed_ms

    @staticmethod
    def _clear_confirmation_state(spec: ToolSpec) -> None:
        cleaner = spec.confirmation_state_cleaner
        if cleaner is None:
            return
        try:
            cleaner()
        except Exception as exc:
            logger.warning(
                "Agent 确认临时状态清理失败 tool=%s type=%s",
                spec.name,
                type(exc).__name__,
            )

    def _get_spec(self, name: str) -> ToolSpec:
        tool_name = str(name or "").strip()
        spec = self._tools.get(tool_name)
        if spec is None:
            raise AgentToolError("未知 Agent 工具", code="tool_not_found")
        return spec

    @staticmethod
    def _normalize_arguments(spec: ToolSpec, arguments: dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise AgentToolError("工具参数必须是 JSON 对象")
        try:
            return spec.validator(dict(arguments))
        except AgentToolError:
            raise
        except KeyError as exc:
            key = str(exc.args[0] if exc.args else "").strip()
            message = f"缺少必需参数：{key}" if key else "缺少必需参数"
            raise AgentToolError(message) from exc
        except (TypeError, ValueError) as exc:
            raise AgentToolError(str(exc) or "工具参数无效") from exc

    @staticmethod
    def _verification_pending(result: ToolResult) -> ToolResult:
        data = dict(result.data)
        data["verification_state"] = "pending"
        suggestions = list(result.suggestions)
        message = "配置已提交，但服务端回读验证暂时不可用；请刷新设置页确认最终状态。"
        if message not in suggestions:
            suggestions.append(message)
        return ToolResult(
            ok=True,
            status=result.status,
            summary=result.summary,
            data=data,
            evidence=list(result.evidence),
            suggestions=suggestions,
            error="",
        )

    @classmethod
    def _call_post_write_verifier(
        cls,
        tool_name: str,
        verifier: PostWriteVerifier,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> tuple[ToolResult, int]:
        started = monotonic()
        try:
            verified = verifier(dict(arguments), result)
            if not isinstance(verified, ToolResult) or not verified.ok:
                logger.warning("Agent 写后验证返回无效结果 tool=%s", tool_name)
                verified = cls._verification_pending(result)
        except Exception as exc:
            # 写动作已经成功，回读失败不能伪装成“写入失败”。只标记为待复核，
            # 且日志不记录配置内容、路径或异常正文。
            logger.warning(
                "Agent 写后验证暂时不可用 tool=%s type=%s",
                tool_name,
                type(exc).__name__,
            )
            verified = cls._verification_pending(result)
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        return verified, elapsed_ms

    @staticmethod
    def _call_context_preparer(
        tool_name: str,
        handler: ContextualConfirmationPreparer,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, str, int]:
        started = monotonic()
        try:
            result, fingerprint = handler(arguments, context)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            logger.warning(
                "Agent 上下文确认预检失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            raise AgentToolError(
                "暂时无法创建确认请求", code="confirmation_unavailable"
            ) from exc
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, str(fingerprint or ""), elapsed_ms

    @staticmethod
    def _call_context_confirmed(
        tool_name: str,
        handler: ContextualConfirmedToolHandler,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        started = monotonic()
        try:
            result = handler(arguments, expected_context, context)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            logger.warning(
                "Agent 上下文确认工具执行失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            result = ToolResult(
                ok=False,
                status="unavailable",
                summary="工具暂时不可用",
                error="上游数据源暂时不可用，请稍后重试。",
            )
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, elapsed_ms

    @staticmethod
    def _call_preparer(
        tool_name: str,
        handler: ConfirmationPreparer,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, str, int]:
        started = monotonic()
        try:
            result, fingerprint = handler(arguments)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            logger.warning(
                "Agent 确认预检失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            raise AgentToolError(
                "暂时无法创建确认请求",
                code="confirmation_unavailable",
            ) from exc
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, str(fingerprint or ""), elapsed_ms

    @staticmethod
    def _call_confirmed(
        tool_name: str,
        handler: ConfirmedToolHandler,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        started = monotonic()
        try:
            result = handler(arguments, expected_context)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            logger.warning(
                "Agent 确认工具执行失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            result = ToolResult(
                ok=False,
                status="unavailable",
                summary="工具暂时不可用",
                error="上游数据源暂时不可用，请稍后重试。",
            )
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, elapsed_ms

    @staticmethod
    def _call_context(
        tool_name: str,
        handler: ContextualToolHandler,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        started = monotonic()
        try:
            result = handler(arguments, context)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            logger.warning(
                "Agent 上下文工具执行失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            result = ToolResult(
                ok=False,
                status="unavailable",
                summary="工具暂时不可用",
                error="上游数据源暂时不可用，请稍后重试。",
            )
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, elapsed_ms

    @staticmethod
    def _call(
        tool_name: str,
        handler: ToolHandler,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        started = monotonic()
        try:
            result = handler(arguments)
        except AgentToolError:
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=False)
            raise
        except Exception as exc:
            logger.warning(
                "Agent 工具执行失败 tool=%s request_id=%s session_id=%s error=%s",
                tool_name, context.request_id, context.session_id,
                safe_exception_summary(exc),
            )
            result = ToolResult(
                ok=False,
                status="unavailable",
                summary="工具暂时不可用",
                error="上游数据源暂时不可用，请稍后重试。",
            )
        elapsed_ms = max(0, int((monotonic() - started) * 1000))
        agent_metrics.record_tool(tool_name, elapsed_ms=elapsed_ms, ok=result.ok)
        return result, elapsed_ms
