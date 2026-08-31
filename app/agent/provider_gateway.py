"""Agent 统一 Provider Gateway。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.agent.models import Evidence, RiskLevel, ToolContext, ToolResult
from app.agent.provider_artifacts import ProviderArtifactStore
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.provider_policy import validate_provider_arguments


class ProviderTransport(Protocol):
    provider: str

    def profiles(self) -> list[ProviderProfileView]: ...

    def execute_read(
        self, profile_ref: str, operation: str, arguments: dict[str, Any]
    ) -> ProviderPayload: ...


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ProviderGateway:
    def __init__(
        self,
        *,
        catalog: ProviderCatalog,
        transports: list[ProviderTransport],
        artifacts: ProviderArtifactStore | None = None,
    ) -> None:
        self.catalog = catalog
        self.transports = {transport.provider: transport for transport in transports}
        self.artifacts = artifacts or ProviderArtifactStore()

    def profiles(self, provider: str = "") -> list[ProviderProfileView]:
        normalized = str(provider or "").strip()
        views: list[ProviderProfileView] = []
        for name, transport in sorted(self.transports.items()):
            if normalized and name != normalized:
                continue
            views.extend(transport.profiles())
        return views

    def capabilities(
        self, *, provider: str = "", intent: str = "", limit: int = 16
    ) -> ToolResult:
        operations = self.catalog.list(
            provider=provider,
            intent=intent,
            limit=limit,
        )
        profiles = self.profiles(provider)
        return ToolResult(
            ok=True,
            status="success",
            summary=f"已读取 {len(operations)} 项 Provider 能力",
            data={
                "profiles": [item.public_dict() for item in profiles],
                "operations": [item.public_dict() for item in operations],
                "rules": {
                    "reads_execute_automatically": True,
                    "writes_require_preview_and_confirmation": True,
                    "arbitrary_http_allowed": False,
                },
            },
            evidence=[Evidence(
                "provider_catalog",
                "读取服务端静态操作目录与非敏感配置状态；未连接 Provider。",
                _now(),
            )],
            suggestions=["先选择已启用 profile，再调用对应只读 operation。"],
        )

    def query(
        self,
        *,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        spec = self.catalog.get(operation)
        if spec.risk is not RiskLevel.READ:
            raise ProviderGatewayError(
                "写操作不能通过 Provider 查询入口执行",
                code="operation_not_allowed",
            )
        normalized = validate_provider_arguments(spec.parameters, arguments)
        transport = self.transports.get(spec.provider)
        if transport is None:
            raise ProviderGatewayError(
                "Provider 当前未接入", code="provider_not_configured"
            )
        profiles = {item.profile_ref: item for item in transport.profiles()}
        profile = profiles.get(profile_ref)
        if profile is None:
            raise ProviderGatewayError(
                "Provider profile 不存在", code="provider_not_configured"
            )
        if profile.state != "online":
            raise ProviderGatewayError(
                "Provider 尚未启用或配置不完整", code="provider_not_configured"
            )
        resolved = dict(normalized)
        for argument, expected_kind in spec.reference_arguments.items():
            raw_id, _snapshot = self.artifacts.resolve_object(
                owner=context.owner,
                session_id=context.session_id,
                object_ref=str(normalized.get(argument) or ""),
                provider=spec.provider,
                profile_ref=profile_ref,
                expected_kind=expected_kind,
            )
            resolved[argument] = raw_id
        payload = transport.execute_read(profile_ref, operation, resolved)
        artifact_ref, public_data = self.artifacts.put(
            owner=context.owner,
            session_id=context.session_id,
            provider=spec.provider,
            profile_ref=profile_ref,
            operation=operation,
            data=payload.data,
        )
        public_data["artifact_ref"] = artifact_ref
        return ToolResult(
            ok=True,
            status=payload.status,
            summary=payload.summary,
            data=public_data,
            evidence=[Evidence(payload.source, "读取实时 Provider API 并返回有界脱敏结果。", _now())],
            suggestions=list(payload.suggestions),
        )
