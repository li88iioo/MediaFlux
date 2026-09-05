"""媒体质量与用户状态原子能力；复用 Provider 冻结计划，不新增执行入口。"""

from __future__ import annotations

from typing import Any

from app.agent.errors import AgentToolError
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.provider_actions import get_provider_gateway
from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_operations import media_management_specs
from app.agent.provider_policy import validate_provider_arguments


def _handler(operation, schema):
    def validate(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_provider_arguments(schema, arguments)
        except ProviderGatewayError as exc:
            raise AgentToolError(exc.safe_message, code=exc.code) from exc

    def query(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        values = dict(arguments)
        profile = values.pop("profile_ref")
        try:
            return get_provider_gateway().query(
                profile_ref=profile,
                operation=operation,
                arguments=values,
                context=context,
            )
        except ProviderGatewayError as exc:
            raise AgentToolError(exc.safe_message, code=exc.code) from exc

    def prepare(
        arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        values = dict(arguments)
        profile = values.pop("profile_ref")
        gateway = get_provider_gateway()
        try:
            preview = gateway.preview_change(
                profile_ref=profile,
                operation=operation,
                arguments=values,
                context=context,
            )
            plan_ref = str(preview.data["plan_ref"])
            result, fingerprint = gateway.prepare_change_execution(
                plan_ref=plan_ref, context=context
            )
            # 冻结 token 只含持久化计划引用与校验摘要，确认时不再调用模型或重新选目标。
            return result, f"{plan_ref}:{fingerprint}"
        except ProviderGatewayError as exc:
            raise AgentToolError(exc.safe_message, code=exc.code) from exc

    def confirmed(
        _arguments: dict[str, Any], expected: str, context: ToolContext
    ) -> ToolResult:
        plan_ref, separator, fingerprint = expected.partition(":")
        if not separator or not plan_ref.startswith("PP-") or not fingerprint:
            raise AgentToolError(
                "媒体操作计划已失效，请重新预检", code="confirmation_stale"
            )
        try:
            return get_provider_gateway().execute_change(
                plan_ref=plan_ref, expected_context=fingerprint, context=context
            )
        except ProviderGatewayError as exc:
            raise AgentToolError(exc.safe_message, code=exc.code) from exc

    return validate, query, prepare, confirmed


def register_specs(
    registry, *, resource_store=None, active_ingest_store=None, ingest_actions=None
) -> None:
    neighbors = {
        "media.library.quality": ("provider.capabilities", "provider.query"),
        "media.user.inspect": ("provider.capabilities", "provider.query"),
        "media.playlists.list": ("provider.capabilities", "media.playlist.inspect"),
        "media.playlist.inspect": ("provider.capabilities", "media.playlists.list"),
    }
    for operation in media_management_specs():
        schema = {
            **operation.parameters,
            "properties": {
                "profile_ref": {"type": "string", "minLength": 1, "maxLength": 80},
                **operation.parameters["properties"],
            },
            "required": ["profile_ref", *operation.parameters.get("required", [])],
        }
        validate, query, prepare, confirmed = _handler(operation.operation_id, schema)
        spec_args = {
            "name": operation.operation_id,
            "description": operation.description,
            "risk": operation.risk,
            "parameters": schema,
            "validator": validate,
            "domains": operation.domains,
            "examples": operation.examples,
            "source_kind": "provider_api",
            "freshness": "live",
            "related_tools": neighbors.get(
                operation.operation_id, ("provider.capabilities", "provider.query")
            ),
        }
        if operation.risk is RiskLevel.READ:
            spec_args["context_handler"] = query
        else:
            spec_args.update(
                requires_confirmation=True,
                context_confirmation_preparer=prepare,
                context_confirmed_handler=confirmed,
            )
        registry.register(ToolSpec(**spec_args))
