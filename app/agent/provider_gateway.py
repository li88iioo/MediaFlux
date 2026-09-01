"""Agent 统一 Provider Gateway。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, RiskLevel, ToolContext, ToolResult
from app.agent.provider_artifacts import ProviderArtifactStore
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.provider_policy import validate_provider_arguments
from app.agent.provider_projection import project_provider_value
from app.modules.process_lock import CrossProcessLock
from app.repositories.agent_provider_plans import (
    claim_provider_plan,
    create_provider_plan,
    finish_provider_plan,
    get_provider_plan,
    recover_orphaned_provider_plans_under_writer_lease,
)

_PROVIDER_WRITE_LOCK = CrossProcessLock("agent-provider-write")


class ProviderTransport(Protocol):
    provider: str

    def profiles(self) -> list[ProviderProfileView]: ...

    def profile_revision(self, profile_ref: str) -> str: ...

    def execute_read(
        self, profile_ref: str, operation: str, arguments: dict[str, Any]
    ) -> ProviderPayload: ...

    def preview_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        target_snapshot: dict[str, Any],
    ) -> ProviderPayload: ...

    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        expected_profile_revision: str,
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
            evidence=[
                Evidence(
                    "provider_catalog",
                    "读取服务端静态操作目录与非敏感配置状态；未连接 Provider。",
                    _now(),
                )
            ],
            suggestions=["先选择已启用 profile，再调用对应操作。"],
        )

    def _resolve_profile(self, *, provider: str, profile_ref: str) -> ProviderTransport:
        transport = self.transports.get(provider)
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
        return transport

    @staticmethod
    def _profile_revision(transport: ProviderTransport, profile_ref: str) -> str:
        """返回不公开且必需的配置世代摘要。"""
        resolver = getattr(transport, "profile_revision", None)
        if not callable(resolver):
            raise ProviderGatewayError(
                "Provider transport 缺少配置世代能力", code="invalid_response"
            )
        revision = str(resolver(profile_ref) or "").strip()
        if not revision or len(revision) > 256:
            raise ProviderGatewayError(
                "Provider profile 配置摘要无效", code="invalid_response"
            )
        return revision

    def _resolve_references(
        self,
        *,
        spec: Any,
        profile_ref: str,
        normalized: dict[str, Any],
        context: ToolContext,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        resolved = dict(normalized)
        snapshots: dict[str, Any] = {}
        for argument, expected_kind in spec.reference_arguments.items():
            value = normalized.get(argument)
            if isinstance(value, list):
                raw_ids: list[str] = []
                items: list[dict[str, Any]] = []
                for object_ref in value:
                    raw_id, snapshot = self.artifacts.resolve_object(
                        owner=context.owner,
                        session_id=context.session_id,
                        object_ref=str(object_ref or ""),
                        provider=spec.provider,
                        profile_ref=profile_ref,
                        expected_kind=expected_kind,
                    )
                    if raw_id not in raw_ids:
                        raw_ids.append(raw_id)
                        items.append(snapshot)
                if not raw_ids:
                    raise ProviderGatewayError(
                        "至少需要一个有效对象引用", code="invalid_arguments"
                    )
                resolved[argument] = raw_ids
                snapshots[argument] = items
                continue
            raw_id, snapshot = self.artifacts.resolve_object(
                owner=context.owner,
                session_id=context.session_id,
                object_ref=str(value or ""),
                provider=spec.provider,
                profile_ref=profile_ref,
                expected_kind=expected_kind,
            )
            resolved[argument] = raw_id
            snapshots[argument] = snapshot
        return resolved, snapshots

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
        transport = self._resolve_profile(
            provider=spec.provider, profile_ref=profile_ref
        )
        resolved, _snapshots = self._resolve_references(
            spec=spec,
            profile_ref=profile_ref,
            normalized=normalized,
            context=context,
        )
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
            evidence=[
                Evidence(
                    payload.source,
                    "读取实时 Provider API 并返回有界脱敏结果。",
                    _now(),
                )
            ],
            suggestions=list(payload.suggestions),
        )

    def preview_change(
        self,
        *,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        spec = self.catalog.get(operation)
        if spec.risk is RiskLevel.READ:
            raise ProviderGatewayError(
                "只读操作不需要创建写计划", code="operation_not_allowed"
            )
        normalized = validate_provider_arguments(spec.parameters, arguments)
        transport = self._resolve_profile(
            provider=spec.provider, profile_ref=profile_ref
        )
        resolved, snapshots = self._resolve_references(
            spec=spec,
            profile_ref=profile_ref,
            normalized=normalized,
            context=context,
        )
        profile_revision_before = self._profile_revision(transport, profile_ref)
        payload = transport.preview_write(profile_ref, operation, resolved, snapshots)
        profile_revision_after = self._profile_revision(transport, profile_ref)
        if profile_revision_before != profile_revision_after:
            raise ProviderGatewayError(
                "Provider 配置在写前检查期间发生变化，请重新预检",
                code="confirmation_stale",
            )
        public_snapshot = project_provider_value(payload.data)
        if not isinstance(public_snapshot, dict):
            raise ProviderGatewayError(
                "Provider 写前检查响应无效", code="invalid_response"
            )
        frozen_arguments = dict(resolved)
        frozen_arguments["__profile_revision"] = profile_revision_after
        context_fingerprint = confirmation_context_fingerprint(
            {
                "provider": spec.provider,
                "profile_ref": profile_ref,
                "operation": operation,
                "arguments": frozen_arguments,
                "target_snapshot": public_snapshot,
            },
            domain="provider-change-plan",
        )
        plan = create_provider_plan(
            owner=context.owner,
            session_id=context.session_id,
            provider=spec.provider,
            profile_ref=profile_ref,
            operation=operation,
            risk=spec.risk.value,
            arguments=frozen_arguments,
            target_snapshot=public_snapshot,
            context_fingerprint=context_fingerprint,
            summary=payload.summary,
        )
        return ToolResult(
            ok=True,
            status="preview",
            summary=payload.summary,
            data={
                "plan_ref": plan["plan_ref"],
                "provider": spec.provider,
                "profile_ref": profile_ref,
                "operation": operation,
                "risk": spec.risk.value,
                "status": "prepared",
                "target_snapshot": public_snapshot,
            },
            evidence=[
                Evidence(
                    payload.source,
                    "已完成实时写前检查并冻结短期写计划；尚未修改 Provider。",
                    _now(),
                )
            ],
            suggestions=["确认目标无误后执行该写计划；过期或状态变化时重新预检。"],
        )

    def prepare_change_execution(
        self, *, plan_ref: str, context: ToolContext
    ) -> tuple[ToolResult, str]:
        plan = get_provider_plan(
            owner=context.owner,
            session_id=context.session_id,
            plan_ref=plan_ref,
        )
        if plan["status"] != "prepared":
            raise ProviderGatewayError(
                "Provider 写计划已失效，请重新预检", code="confirmation_stale"
            )
        if not str(plan["arguments"].get("__profile_revision") or "").strip():
            raise ProviderGatewayError(
                "Provider 写计划来自已停用的旧链路，请重新预检",
                code="confirmation_stale",
            )
        spec = self.catalog.get(str(plan["operation"]))
        if (
            spec.risk is RiskLevel.READ
            or spec.provider != plan["provider"]
            or spec.risk.value != plan["risk"]
        ):
            raise ProviderGatewayError(
                "Provider 写计划与当前能力目录不一致", code="confirmation_stale"
            )
        self._resolve_profile(
            provider=spec.provider,
            profile_ref=str(plan["profile_ref"]),
        )
        return (
            ToolResult(
                ok=True,
                status="confirmation_required",
                summary=str(plan["summary"] or "Provider 写计划已完成预检，等待确认"),
                data={
                    "plan_ref": plan["plan_ref"],
                    "provider": plan["provider"],
                    "profile_ref": plan["profile_ref"],
                    "operation": plan["operation"],
                    "risk": plan["risk"],
                    "status": plan["status"],
                    "target_snapshot": plan["target_snapshot"],
                },
                evidence=[
                    Evidence(
                        "provider_plan_store",
                        "读取 owner/session 绑定的冻结计划；未执行写操作。",
                        _now(),
                    )
                ],
            ),
            str(plan["context_fingerprint"]),
        )

    def execute_change(
        self,
        *,
        plan_ref: str,
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        # Provider 写是低频人工确认动作。跨进程串行化既阻止两个 Worker 同时
        # 执行相互冲突的计划，也让启动恢复能够辨别真正失去执行者的 running 行。
        acquired = _PROVIDER_WRITE_LOCK.acquire(blocking=True)
        if not acquired:  # pragma: no cover - blocking 锁只会在锁目录不可用时失败
            return ToolResult(
                ok=False,
                status="failed",
                summary="Provider 写入队列暂不可用，请稍后重新预检",
                data={
                    "plan_ref": str(plan_ref or ""),
                    "status": "failed",
                },
                error="Provider 写入队列暂不可用",
            )
        try:
            # 成功取得全局 writer lease 即可证明数据库中此前残留的 running
            # 计划已失去执行者；先统一收束，再认领本次 prepared 计划。
            recover_orphaned_provider_plans_under_writer_lease()
            return self._execute_change_locked(
                plan_ref=plan_ref,
                expected_context=expected_context,
                context=context,
            )
        finally:
            _PROVIDER_WRITE_LOCK.release()

    def _execute_change_locked(
        self,
        *,
        plan_ref: str,
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        plan = claim_provider_plan(
            owner=context.owner,
            session_id=context.session_id,
            plan_ref=plan_ref,
            expected_context=expected_context,
        )
        transport_entered = False
        external_write_possible = False
        try:
            spec = self.catalog.get(str(plan["operation"]))
            if (
                spec.risk is RiskLevel.READ
                or spec.provider != plan["provider"]
                or spec.risk.value != plan["risk"]
            ):
                raise ProviderGatewayError(
                    "Provider 写计划与当前能力目录不一致",
                    code="confirmation_stale",
                )
            transport = self._resolve_profile(
                provider=spec.provider,
                profile_ref=str(plan["profile_ref"]),
            )
            arguments = dict(plan["arguments"])
            expected_profile_revision = str(
                arguments.pop("__profile_revision", "") or ""
            )
            if not expected_profile_revision:
                raise ProviderGatewayError(
                    "Provider 写计划来自已停用的旧链路，请重新预检",
                    code="confirmation_stale",
                )
            if expected_profile_revision != (
                self._profile_revision(transport, str(plan["profile_ref"]))
            ):
                raise ProviderGatewayError(
                    "Provider 配置已变化，请重新预检",
                    code="confirmation_stale",
                )
            transport_entered = True
            payload = transport.execute_write(
                str(plan["profile_ref"]),
                str(plan["operation"]),
                arguments,
                expected_profile_revision=expected_profile_revision,
            )
            # Transport 正常返回意味着外部写已经得到响应；其后的投影或持久化
            # 失败都不能再声称“未写入”。
            external_write_possible = True
            projected_result = project_provider_value(payload.data)
            if not isinstance(projected_result, dict):
                raise ProviderGatewayError(
                    "Provider 写后核验响应无效", code="invalid_response"
                )
            public_result = {
                "plan_ref": plan["plan_ref"],
                "provider": plan["provider"],
                "profile_ref": plan["profile_ref"],
                "operation": plan["operation"],
                "status": "succeeded",
                "result": projected_result,
            }
            for key in ("affected", "accepted", "delete_files", "global_refresh"):
                value = projected_result.get(key)
                if isinstance(value, (bool, int)):
                    public_result[key] = value
            try:
                finish_provider_plan(
                    plan_ref=plan["plan_ref"],
                    status="succeeded",
                    result=public_result,
                    summary=payload.summary,
                )
            except Exception:
                return ToolResult(
                    ok=False,
                    status="outcome_unknown",
                    summary="Provider 写入已完成，但结果状态未能可靠持久化，请先核对上游",
                    data={
                        "plan_ref": plan["plan_ref"],
                        "provider": plan["provider"],
                        "operation": plan["operation"],
                        "status": "outcome_unknown",
                    },
                    error="Provider 写入结果需要人工核对",
                )
            return ToolResult(
                ok=True,
                status=payload.status,
                summary=payload.summary,
                data=public_result,
                evidence=[
                    Evidence(
                        payload.source,
                        "用户确认后执行冻结的 Provider 写计划，并完成受限写后核验。",
                        _now(),
                    )
                ],
                suggestions=list(payload.suggestions),
            )
        except ProviderGatewayError as exc:
            if external_write_possible or exc.external_write_possible:
                terminal_status = "outcome_unknown"
                summary = "Provider 可能已接收写操作，请先核对上游状态再重新预检"
            elif exc.code == "confirmation_stale":
                terminal_status = "stale"
                summary = exc.safe_message
            else:
                terminal_status = "failed"
                summary = exc.safe_message
            try:
                finish_provider_plan(
                    plan_ref=plan["plan_ref"],
                    status=terminal_status,
                    result={},
                    summary=summary,
                    error_code=exc.code,
                )
            except Exception:
                terminal_status = "outcome_unknown"
                summary = "Provider 计划终态未能可靠持久化，请先核对上游状态"
            return ToolResult(
                ok=False,
                status=terminal_status,
                summary=summary,
                data={
                    "plan_ref": plan["plan_ref"],
                    "provider": plan["provider"],
                    "operation": plan["operation"],
                    "status": terminal_status,
                },
                error=summary,
            )
        except Exception:
            # 内建 transport 会把可判定的写前失败包装为 ProviderGatewayError。
            # 未分类异常一旦进入 transport 只能保守视为外部写可能发生。
            write_outcome_unknown = external_write_possible or transport_entered
            terminal_status = "outcome_unknown" if write_outcome_unknown else "failed"
            summary = (
                "Provider 写操作结果未知，请先核对上游状态"
                if write_outcome_unknown
                else "Provider 写前检查异常，未执行外部写入"
            )
            error_code = (
                "provider_write_outcome_unknown"
                if write_outcome_unknown
                else "provider_prewrite_failed"
            )
            try:
                finish_provider_plan(
                    plan_ref=plan["plan_ref"],
                    status=terminal_status,
                    result={},
                    summary=summary,
                    error_code=error_code,
                )
            except Exception:
                terminal_status = "outcome_unknown"
                summary = "Provider 计划终态未能可靠持久化，请先核对上游状态"
            return ToolResult(
                ok=False,
                status=terminal_status,
                summary=summary,
                data={
                    "plan_ref": plan["plan_ref"],
                    "provider": plan["provider"],
                    "operation": plan["operation"],
                    "status": terminal_status,
                },
                error=summary,
            )

    def change_status(self, *, plan_ref: str, context: ToolContext) -> ToolResult:
        plan = get_provider_plan(
            owner=context.owner,
            session_id=context.session_id,
            plan_ref=plan_ref,
        )
        if plan["status"] == "running":
            try:
                acquired = _PROVIDER_WRITE_LOCK.acquire(blocking=False)
            except OSError as exc:
                raise ProviderGatewayError(
                    "Provider 状态恢复暂不可用", code="provider_unavailable"
                ) from exc
            if acquired:
                try:
                    recover_orphaned_provider_plans_under_writer_lease()
                except Exception as exc:
                    raise ProviderGatewayError(
                        "Provider 状态恢复暂不可用", code="provider_unavailable"
                    ) from exc
                finally:
                    _PROVIDER_WRITE_LOCK.release()
                plan = get_provider_plan(
                    owner=context.owner,
                    session_id=context.session_id,
                    plan_ref=plan_ref,
                )
        running = str(plan["status"]) == "running"
        public_data = {
            "plan_ref": plan["plan_ref"],
            "status": plan["status"],
            "attempts": plan["attempts"],
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
            "finished_at": plan["finished_at"],
            "error_code": plan["error_code"],
        }
        if not running:
            public_data.update(
                {
                    "provider": plan["provider"],
                    "profile_ref": plan["profile_ref"],
                    "operation": plan["operation"],
                    "risk": plan["risk"],
                    "target_snapshot": plan["target_snapshot"],
                    "result": plan["result"],
                }
            )
        return ToolResult(
            ok=True,
            status=str(plan["status"]),
            summary=(
                "Provider 写计划执行中"
                if running
                else str(plan["summary"] or "Provider 写计划状态已读取")
            ),
            data=public_data,
            evidence=[
                Evidence(
                    "provider_plan_store",
                    "读取 owner/session 绑定的持久写计划状态。",
                    _now(),
                )
            ],
        )
