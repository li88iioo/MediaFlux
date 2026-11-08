"""脚本写方法接入已有确认执行器；业务预检和提交仍由公共服务完成。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from functools import partial
from typing import Any

from app import config, database as db
from app.agent import effect_completion
from app.agent.confirmation_contract import build_confirmation_contract
from app.modules.resource_candidates import _deserialize_private_item, _serialize_private_item
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import (
    AgentRuntimeDisabled, RuntimeFenceError, read_runtime_fence, require_runtime_fence,
)
from app.agent.models import RiskLevel, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text, sanitize_untrusted_filename, sanitize_resource_title
from app.clients.guangya import GuangYaClient, GuangYaShareError, GuangYaWriteRejected, close_guangya_client
from app.discovery.models import MediaCard, ProviderError
from app.discovery.service import DiscoveryService, get_discovery_service, watchlist_identity
from app.indexers import downloads
from app.indexers.errors import IndexerResultExpired, IndexerResultNotFound
from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync
from app.modules import download_dispatcher, guangya_fs_change, guangya_recycle, share_transfer
from app.modules.web_secret import signed_context_fingerprint
from app.modules.media_preferences import (
    PREFERENCE_PROPERTIES, apply_media_preferences_change, media_preferences_preview,
    prepare_media_preferences_change, public_media_preferences, validate_preference_updates,
    resolve_download_target,
)
from app.modules.guangya_workspace import GuangYaWorkspaceError, GuangYaWorkspaceStale
from app.modules.organize_tasks import get_organize_operation_status
from app.repositories.organize_operation_jobs import OrganizeOperationAdmissionError, organize_operation_public_ref
from app.repositories import activity as activity_repository
from app.modules import activity_undo
from app.sdk import activity, cloud, downloads as sdk_downloads, preferences, resources

from ..capabilities import KernelToolSpec, ToolEffect
from ..tool_calls import PreparedEffect
from ..projection import DefaultProjector
from ..scoped_trust import watchlist_approval_scope
from .mediaflux_policy import confirmation_runtime


def _fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def subscription_tools() -> tuple[KernelToolSpec, ...]:
    """媒体订阅原生写入，业务快照和事务由 Web 共用服务持有。"""
    from app import sdk
    from app.modules import media_subscription_notifications as notifications
    from app.modules.media_subscriptions import MediaSubscriptionError, get_media_subscription_service

    def risk_for(operation, arguments):
        return RiskLevel.WRITE if operation == "create" and arguments.get("action") == "auto" else RiskLevel.DANGER if operation == "delete" or (operation == "update" and arguments.keys() - {"subscription_id", "enabled"}) else RiskLevel.LOW_WRITE

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                values = sdk.prepare_sdk_method("subscriptions." + operation, arguments, owner=context.owner)
                context.cancellation.raise_if_cancelled()
                prepared = (notifications.prepare_rule_change(operation, values) if operation.endswith("notification")
                            else get_media_subscription_service().prepare_change(operation, values))
                risk = risk_for(operation, prepared["arguments"])
                return PreparedEffect(preview=prepared["preview"], arguments=prepared["arguments"],
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": risk.value, "subscription_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(tool_name="subscriptions." + operation,
                                  risk=risk, preview=ToolResult(**prepared["preview"]))})
        except (AgentRuntimeDisabled, RuntimeFenceError, MediaSubscriptionError, ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments")) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                plan = context.active_effect_plan
                if (plan is None or plan.owner != context.owner or plan.session_id != context.session_id
                        or plan.tool_name != "subscriptions." + operation or dict(plan.arguments) != arguments):
                    raise ToolPipelineError("缺少原媒体订阅确认范围", code="confirmation_invalid")
                snapshot = plan.metadata.get("subscription_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("媒体订阅确认已失效，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                apply = notifications.apply_rule_change if operation.endswith("notification") else get_media_subscription_service().apply_change
                return ToolResult(**apply(operation, arguments, expected_snapshot=snapshot, check_cancelled=context.cancellation.raise_if_cancelled))
        except (AgentRuntimeDisabled, RuntimeFenceError, MediaSubscriptionError, ToolPipelineError) as exc:
            started = getattr(exc, "execution_started", False) is True
            error = ToolPipelineError("写入后的媒体订阅结果无法确认，请先读取核实，不能重放" if started else str(exc),
                                      code="outcome_unknown" if started else getattr(exc, "code", "agent_disabled"))
            error.mark_execution_started(started)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("媒体订阅写入结果无法确认，请先读取核实，不能重放", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error from exc

    identity = {"subscription_id": {"type": "integer", "minimum": 1, "maximum": 2147483647}}
    settings = {"enabled": {"type": "boolean"}, "monitor_mode": {"type": "string", "enum": ["missing", "future", "selected"]},
                "seasons": {"type": "array", "maxItems": 20, "items": {"type": "integer", "minimum": 1, "maximum": 100}},
                "include_specials": {"type": "boolean"}, "action": {"type": "string", "enum": ["notify", "confirm", "auto"]},
                "download_target": {"type": "string", "enum": ["qb", "guangya", "both"]},
                "check_interval_minutes": {"type": "integer", "minimum": 5, "maximum": 10080}}
    schemas = {
        "create": ({"provider": {"type": "string", "enum": ["tmdb", "douban", "bangumi"]},
                    "external_id": {"type": "string", "minLength": 1, "maxLength": 180}, "media_type": {"type": "string", "enum": ["movie", "tv"]},
                    "season": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 1, "maximum": 100}]},
                    "check_interval_minutes": settings["check_interval_minutes"], "action": settings["action"],
                    "download_target": settings["download_target"], "enabled": {"type": "boolean"},
                    "sites": {"anyOf": [{"type": "null"}, {"type": "array", "maxItems": 6, "items": {"type": "string"}}]}}, ["provider", "external_id", "media_type"]),
        "update": ({**identity, **settings}, ["subscription_id"]),
        "delete": (identity, ["subscription_id"]),
        "set_notification": ({**identity, **{key: {"type": "boolean"} for key in ("enabled", "notify_on_missing", "notify_on_satisfied", "notify_on_error")}}, ["subscription_id"]),
        "reset_notification": (identity, ["subscription_id"]),
    }
    return tuple(KernelToolSpec(name="subscriptions." + operation, domain="subscriptions",
        description=sdk.METHODS["subscriptions." + operation].__doc__ or "",
        effect=ToolEffect.DANGER if operation == "delete" else ToolEffect.WRITE,
        effect_resolver=(lambda args: ToolEffect.DANGER if risk_for("update", args) is RiskLevel.DANGER else ToolEffect.WRITE) if operation == "update" else None,
        metadata={"risk": "danger" if operation == "delete" else "low_write"},
        input_schema={"type": "object", "properties": props, "required": required, "additionalProperties": False},
        prepare=partial(prepare, operation=operation), execute_confirmed=partial(execute, operation=operation))
        for operation, (props, required) in schemas.items())


def automation_tools() -> tuple[KernelToolSpec, ...]:
    """原生 SDK 写入口；公共订阅/摘要服务拥有唯一业务编排。"""
    from app import sdk
    from app.agent.errors import AgentToolError
    from app.modules import media_automation_rules as digests

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                values = sdk.prepare_sdk_method("automation." + operation, arguments, owner=context.owner)
                context.cancellation.raise_if_cancelled()
                prepared = digests.prepare_set_digest(values, owner=context.owner, delivery_route=context.delivery_route)
                return PreparedEffect(preview=prepared["preview"], arguments=values,
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "write", "automation_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(tool_name="automation." + operation,
                                  risk=RiskLevel.WRITE, preview=ToolResult(**prepared["preview"]))})
        except (AgentRuntimeDisabled, RuntimeFenceError, AgentToolError, ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments")) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.session_id != context.session_id
                        or active.tool_name != "automation." + operation or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原自动化确认范围", code="confirmation_invalid")
                snapshot = active.metadata.get("automation_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("自动化确认已失效，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                value = digests.set_digest(arguments, owner=context.owner, delivery_route=context.delivery_route,
                    expected_snapshot=snapshot, check_cancelled=context.cancellation.raise_if_cancelled)
                return ToolResult(**value)
        except (AgentRuntimeDisabled, RuntimeFenceError, AgentToolError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "agent_disabled"))
            error.mark_execution_started(getattr(exc, "execution_started", False) is True)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("自动化写入结果无法确认，请读取规则核实，不能直接重放", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error from exc

    schemas = {
        "set_digest": {"type": "object", "required": ["enabled"], "additionalProperties": False,
            "properties": {"rule_id": {"type": "string", "maxLength": 100}, "enabled": {"type": "boolean"},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23}, "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                "errors_only": {"type": "boolean"}, "send_empty": {"type": "boolean"}},
            "anyOf": [{"required": ["hour"]}, {"required": ["rule_id"], "properties": {"rule_id": {"minLength": 1}}}]},
    }
    return tuple(KernelToolSpec(name="automation." + operation, domain="automation", effect=ToolEffect.WRITE,
        description=sdk.METHODS["automation." + operation].__doc__ or "", metadata={"risk": "write"},
        input_schema=schema, prepare=partial(prepare, operation=operation), execute_confirmed=partial(execute, operation=operation))
        for operation, schema in schemas.items())


def media_user_tools() -> tuple[KernelToolSpec, ...]:
    """真实媒体ID进入唯一确认账本；公共业务快照不依赖Provider引用。"""
    from app import sdk
    from app.modules import media_user
    from app.modules.media_library import MediaLibraryError

    def prepare(arguments, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                values = sdk.prepare_sdk_method(name, arguments, owner=context.owner)
                public = dict(values)
                if operation in {"set_played", "set_favorite"}:
                    public["value"] = public.pop("played" if operation == "set_played" else "favorite")
                prepared = media_user.prepare_change(operation, **public, check_cancelled=context.cancellation.raise_if_cancelled)
                snapshot = prepared["snapshot"]
                risk = RiskLevel.WRITE if operation == "create_playlist" else RiskLevel.LOW_WRITE
                return PreparedEffect(preview=prepared["preview"],
                    arguments={**values, "server": prepared["server"]},
                    snapshot_fingerprint=prefix + _fingerprint(snapshot),
                    metadata={"risk": risk.value, "media_user_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(tool_name=name, risk=risk,
                                                                          preview=ToolResult(**prepared["preview"]))})
        except (AgentRuntimeDisabled, RuntimeFenceError, MediaLibraryError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "agent_disabled")) from exc

    def execute(arguments, expected_snapshot, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != name
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("media_user_snapshot")
                if (not isinstance(snapshot, dict) or snapshot.get("operation") != operation
                        or prefix + _fingerprint(snapshot) != expected_snapshot):
                    raise ToolPipelineError("媒体确认快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                return ToolResult(**media_user.execute_change(snapshot, check_cancelled=context.cancellation.raise_if_cancelled))
        except (AgentRuntimeDisabled, RuntimeFenceError, MediaLibraryError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "agent_disabled"))
            error.mark_execution_started(getattr(exc, "execution_started", False))
            raise error from exc

    identity = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}
    identities = {"type": "array", "items": identity, "minItems": 1, "maxItems": 20, "uniqueItems": True}
    server = {"type": "string", "enum": ["auto", "jellyfin", "emby"], "default": "auto"}
    specs = []
    for operation, properties, required in (
        ("set_played", {"item_id": identity, "played": {"type": "boolean", "default": True}}, ["item_id"]),
        ("set_favorite", {"item_id": identity, "favorite": {"type": "boolean", "default": True}}, ["item_id"]),
        ("create_playlist", {"name": {"type": "string", "minLength": 1, "maxLength": 120}, "item_ids": identities}, ["name", "item_ids"]),
        ("add_to_playlist", {"playlist_id": identity, "item_ids": identities}, ["playlist_id", "item_ids"]),
        ("remove_from_playlist", {"playlist_id": identity, "entry_ids": identities}, ["playlist_id", "entry_ids"]),
    ):
        name = "library." + operation
        specs.append(KernelToolSpec(name=name, domain="library", effect=ToolEffect.WRITE,
            description=sdk.METHODS[name].__doc__ or name,
            input_schema={"type": "object", "properties": {**properties, "server": server},
                          "required": required, "additionalProperties": False},
            metadata={"risk": "write" if operation == "create_playlist" else "low_write"},
            prepare=partial(prepare, operation=operation, name=name),
            execute_confirmed=partial(execute, operation=operation, name=name)))
    return tuple(specs)


def cloud_organize_tools() -> tuple[KernelToolSpec, ...]:
    """整理写入共用原确认账本、公共服务及持久任务回执。"""
    from app import sdk
    from app.modules import organize_service as service

    def tool(operation):
        name = "cloud.organize" if operation == "run" else "cloud.stop_organize"

        def prepare(arguments, context):
            try:
                with confirmation_runtime(context) as prefix:
                    context.cancellation.raise_if_cancelled()
                    values = sdk.prepare_sdk_method(name, arguments, owner=context.owner)
                    preview, snapshot = (service.prepare_run if operation == "run" else service.prepare_stop)(**values)
                    context.cancellation.raise_if_cancelled()
                    result = ToolResult(**preview)
                    if not result.ok:
                        raise ToolPipelineError(result.error or result.summary, code=result.status)
                    if result.status == "no_changes":
                        return DefaultProjector().project(result)
                    return PreparedEffect(arguments=values, preview=preview,
                        snapshot_fingerprint=prefix + _fingerprint({"snapshot": snapshot}),
                        metadata={"risk": "danger", "organize_snapshot": snapshot,
                            "confirmation": build_confirmation_contract(tool_name=name, risk=RiskLevel.DANGER, preview=result)})
            except (AgentRuntimeDisabled, RuntimeFenceError, ValueError) as exc:
                raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_configuration")) from exc

        def execute(arguments, expected_snapshot, context):
            try:
                with confirmation_runtime(context) as prefix:
                    active = context.active_effect_plan
                    if (active is None or active.owner != context.owner or active.session_id != context.session_id
                            or active.tool_name != name or dict(active.arguments) != arguments):
                        raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                    snapshot = active.metadata.get("organize_snapshot")
                    if prefix + _fingerprint({"snapshot": snapshot}) != expected_snapshot:
                        raise ToolPipelineError("整理配置或原确认计划已变化", code="confirmation_stale")
                    context.cancellation.raise_if_cancelled()
                    value = (service.run(**arguments, expected_fingerprint=snapshot,
                                        check_cancelled=context.cancellation.raise_if_cancelled)
                             if operation == "run" else service.stop(expected_snapshot=snapshot,
                                        check_cancelled=context.cancellation.raise_if_cancelled))
                    result = ToolResult(**value)
                    if result.ok and result.status == "accepted":
                        result.effect_metadata["completion"] = {"kind": "guangya_organize_task", "operation": operation,
                            "task_id": result.data.pop("task_id"), "run_id": result.data["run_id"]}
                    return result
            except (AgentRuntimeDisabled, RuntimeFenceError, service.OrganizeServiceError, ToolPipelineError) as exc:
                error = ToolPipelineError(str(exc), code=getattr(exc, "code", "agent_disabled"))
                error.mark_execution_started(False)
                raise error from exc
            except Exception as exc:
                error = ToolPipelineError("整理请求结果未知，请查询原任务回执", code="outcome_unknown")
                error.mark_execution_started(True)
                raise error from exc

        async def verify(arguments, result, context):
            if context.wait_for_completion:
                return await effect_completion.wait_for_effect_completion(result, tool=name,
                    context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                        cancelled=lambda: context.cancellation.cancelled),
                    report_progress=context.report_progress, scope=context.completion_scope)
            tracker = effect_completion.observers._completion_tracker(result)
            if result.ok and tracker is not None:
                await effect_completion._register_effect_wait(result, tracker, name, context.completion_scope)
            return result

        properties = ({"source_ids": {"type": ["array", "null"], "items": {"type": "string", "minLength": 1}}}
                      if operation == "run" else {"run_id": {"type": ["integer", "null"], "minimum": 1}})
        return KernelToolSpec(name=name, domain="cloud", effect=ToolEffect.DANGER,
            description=sdk.METHODS[name].__doc__ or name,
            input_schema={"type": "object", "properties": properties, "additionalProperties": False},
            metadata={"risk": "danger"}, prepare=prepare, execute_confirmed=execute, verify=verify)

    return (tool("run"), tool("stop"))


def media_refresh_tools() -> tuple[KernelToolSpec, ...]:
    from app import sdk
    from app.modules import media_library

    def prepare(arguments, context, *, method, scope):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                arguments = sdk.prepare_sdk_method(method, arguments, owner=context.owner)
                prepared = media_library.prepare_refresh(**arguments)
                arguments["server"] = prepared["server"]
                preview = ToolResult(True, "confirmation_required", f"将精准刷新 {prepared[scope]['name']}",
                                     data={"server": prepared["server"], "target": prepared[scope],
                                           "scope": scope, **prepared["snapshot"]["options"]})
                return PreparedEffect(preview=preview.to_dict(), arguments=arguments,
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "low_write", "library_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(
                                  tool_name=method, risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (media_library.MediaLibraryError, ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments")) from exc

    def execute(arguments, expected_snapshot, context, *, method):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != method
                        or active.session_id != context.session_id
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata["library_snapshot"]
                if prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("配置或原确认计划已变化", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                return ToolResult(**media_library.refresh(**arguments, expected_snapshot=snapshot,
                                                         check_cancelled=context.cancellation.raise_if_cancelled))
        except (AgentRuntimeDisabled, RuntimeFenceError, media_library.MediaLibraryError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "agent_disabled"))
            error.mark_execution_started(getattr(exc, "execution_started", False))
            raise error from exc

    return tuple(KernelToolSpec(name=method, domain=method.split(".")[0], effect=ToolEffect.WRITE,
        description=sdk.METHODS[method].__doc__ or "", metadata={"risk": "low_write", "confirmation_required": False},
        input_schema={"type": "object", "required": ["server", key] if scope == "library" else [key], "additionalProperties": False,
                      "properties": {"server": {"type": "string", "enum": ["auto", "jellyfin", "emby"]},
                                     key: {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"},
                                     "mode": {"type": "string", "enum": ["scan", "missing", "replace"]},
                                     "replace_images": {"type": "boolean"},
                                     "regenerate_trickplay": {"type": "boolean"}}},
        prepare=partial(prepare, method=method, scope=scope), execute_confirmed=partial(execute, method=method))
        for method, scope, key in (("library.refresh", "library", "library_id"), ("media.refresh", "item", "item_id")))


def library_patrol_tool() -> KernelToolSpec:
    from app.modules import library_patrol
    from app.sdk import library

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                snapshot = library_patrol.prepare_patrol()
                context.cancellation.raise_if_cancelled()
                policy, state = snapshot["policy"], snapshot["state"]
                if not policy["enabled"]:
                    raise ToolPipelineError("全库缺集巡检当前未启用", code="disabled")
                preview = ToolResult(True, "confirmation_required", "确认后将按当前策略把全库缺集巡检排到现在",
                    data={**policy, "task_status": state["task_status"], "run_id": state["run_id"],
                          "effects": ["唤醒后台巡检；已运行时复用当前任务。", "不修改策略、不搜索资源或下载。"]})
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(snapshot),
                    metadata={"risk": "low_write", "patrol_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(
                                  tool_name="library.run_patrol", risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.session_id != context.session_id
                        or active.tool_name != "library.run_patrol" or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("patrol_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("配置或巡检快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                value = library_patrol.run_patrol(snapshot, check_cancelled=context.cancellation.raise_if_cancelled)
                if not value["ok"]:
                    raise ToolPipelineError(value["summary"], code="confirmation_stale" if value["status"] == "conflict" else "precondition_failed")
                result = ToolResult(**value)
                result.effect_metadata["completion"] = {"kind": "library_patrol", "run_id": result.data["run_id"], "operation": "run"}
                return result
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool="library.run_patrol",
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, "library.run_patrol", context.completion_scope)
        return result

    return KernelToolSpec(name="library.run_patrol", domain="library", effect=ToolEffect.WRITE,
        description=library.run_patrol.__doc__ or "", input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        prepare=prepare, execute_confirmed=execute, verify=verify,
        metadata={"risk": "low_write"})


def library_audit_job_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    """后台审计启动/取消直接使用公共业务及统一冻结确认账本。"""
    from app.modules import library_audit_jobs
    from app.repositories.agent_jobs import AgentJobError
    from app.sdk import library

    def tool(operation):
        name = "library.start_audit" if operation == "start" else "library.cancel_audit"

        def prepare(arguments, context):
            try:
                with confirmation_runtime(context) as prefix:
                    context.cancellation.raise_if_cancelled()
                    preview, snapshot = (library_audit_jobs.prepare_start_audit if operation == "start"
                                         else library_audit_jobs.prepare_cancel_audit)(owner=context.owner, **arguments)
                    frozen = ({key: preview["data"][key] for key in ("as_of", "max_series")}
                              if operation == "start" else {"job_id": snapshot["job_id"]})
                    context.cancellation.raise_if_cancelled()
                    return PreparedEffect(arguments=frozen, preview=preview,
                        snapshot_fingerprint=prefix + _fingerprint(snapshot),
                        metadata={"risk": "low_write", "audit_job_snapshot": snapshot,
                                  "confirmation": build_confirmation_contract(tool_name=name, risk=RiskLevel.LOW_WRITE,
                                                                              preview=ToolResult(**preview))})
            except AgentJobError as exc:
                raise ToolPipelineError(str(exc), code=exc.code) from exc
            except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
                raise ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled") from exc

        def execute(arguments, expected_snapshot, context):
            started = False
            try:
                with confirmation_runtime(context) as prefix:
                    active = context.active_effect_plan
                    if (active is None or active.owner != context.owner or active.session_id != context.session_id
                            or active.tool_name != name or dict(active.arguments) != arguments):
                        raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                    snapshot = active.metadata.get("audit_job_snapshot")
                    if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                        raise ToolPipelineError("后台任务或配置快照已变化，请重新预检", code="confirmation_stale")
                    context.cancellation.raise_if_cancelled()
                    started = True
                    value = (library_audit_jobs.start_audit if operation == "start"
                             else library_audit_jobs.cancel_audit)(owner=context.owner, expected_snapshot=snapshot, **arguments)
                    result = ToolResult(**value)
                    if operation == "start" or result.data.get("accepted"):
                        result.effect_metadata["completion"] = {"kind": "agent_job", "job_id": result.data["job_id"],
                                                                "operation": "audit" if operation == "start" else "cancel"}
                    return result
            except AgentJobError as exc:
                error = ToolPipelineError(str(exc), code=exc.code)
                error.mark_execution_started(False)  # 仓储事务fence/身份拒绝均发生在写入前。
                raise error from exc
            except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
                error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
                error.mark_execution_started(started)
                raise error from exc
            except ToolPipelineError as exc:
                exc.mark_execution_started(started)
                raise

        async def verify(arguments, result, context):
            if context.wait_for_completion:
                return await effect_completion.wait_for_effect_completion(result, tool=name,
                    context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                        cancelled=lambda: context.cancellation.cancelled),
                    report_progress=context.report_progress, scope=context.completion_scope)
            tracker = effect_completion.observers._completion_tracker(result)
            if result.ok and tracker is not None:
                await effect_completion._register_effect_wait(result, tracker, name, context.completion_scope)
            return result

        properties = ({"as_of": {"type": ["string", "null"], "format": "date", "default": None},
                       "max_series": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}
                      if operation == "start" else {"job_id": {"type": ["string", "null"],
                          "pattern": "^job_[A-Za-z0-9_-]{16,80}$", "default": None}})
        return KernelToolSpec(name=name, domain="library", effect=ToolEffect.WRITE,
            description=(library.start_audit if operation == "start" else library.cancel_audit).__doc__ or "",
            input_schema={"type": "object", "properties": properties, "additionalProperties": False},
            prepare=prepare, execute_confirmed=execute, verify=verify,
            metadata={"risk": "low_write"})

    return tool("start"), tool("cancel")


def activity_undo_tool() -> KernelToolSpec:
    """公共补偿业务接入既有确认、审计和整理结果观察，不解析旧 action/ref。"""
    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = activity_undo.prepare_undo(arguments["receipt_id"], owner=context.owner)
                preview = ToolResult(**prepared["preview"])
                return PreparedEffect(
                    preview=prepared["preview"], snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "danger", "undo_fingerprint": prepared["fingerprint"],
                              "confirmation": build_confirmation_contract(
                                  tool_name="activity.undo", risk=RiskLevel.DANGER, preview=preview)},
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "precondition_failed")) from exc

    def submit(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "activity.undo"
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                expected = active.metadata["undo_fingerprint"]
                if prefix + expected != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                result = ToolResult(**activity_undo.execute_undo(
                    arguments["receipt_id"], expected_fingerprint=expected, owner=context.owner))
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except (activity_undo.UndoError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "precondition_failed"))
            error.mark_execution_started(False)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("回退结果待核对，请查询原活动，勿重试", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error from exc
        if result.ok and result.status == "accepted" and result.data.get("task_id"):
            result.effect_metadata["completion"] = {
                "kind": "guangya_organize_task", "task_id": result.data["task_id"], "operation": "undo",
            }
        return result

    async def execute(arguments, expected_snapshot, context):
        result = await asyncio.to_thread(submit, arguments, expected_snapshot, context)
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(
                result, tool="activity.undo",
                context=ToolContext(owner=context.owner, session_id=context.session_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, "activity.undo", context.completion_scope)
        return result

    return KernelToolSpec(
        name="activity.undo", domain="activity", effect=ToolEffect.DANGER,
        description=activity.undo.__doc__ or "", metadata={"risk": "danger"},
        input_schema={"type": "object", "required": ["receipt_id"], "additionalProperties": False,
                      "properties": {"receipt_id": {"type": "string", "minLength": 1, "maxLength": 200}}},
        prepare=prepare, execute_confirmed=execute,
    )


def activity_follow_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    """按持久活动身份关注/停用；业务条件提交复用同一规则仓储。"""
    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = (activity_repository.prepare_follow(**arguments, owner=context.owner, delivery_route=context.delivery_route)
                            if operation == "follow" else activity_repository.prepare_unfollow(**arguments, owner=context.owner))
                preview = ToolResult(**prepared["preview"])
                if preview.status == "no_changes":
                    return DefaultProjector().project(preview)
                return PreparedEffect(
                    preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "write", "activity_follow_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(
                                  tool_name=f"activity.{operation}", risk=RiskLevel.WRITE, preview=preview)},
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "precondition_failed")) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != f"activity.{operation}"
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("activity_follow_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("活动跟踪确认已失效，请按持久活动编号重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                result = (activity_repository.follow(**arguments, owner=context.owner, expected_snapshot=snapshot,
                                                     delivery_route=context.delivery_route)
                          if operation == "follow" else activity_repository.unfollow(
                              **arguments, owner=context.owner, expected_snapshot=snapshot))
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except ValueError as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "precondition_failed"))
            error.mark_execution_started(False)
            raise error from exc
        return ToolResult(**result)

    return (
        KernelToolSpec(name="activity.follow", domain="activity", effect=ToolEffect.WRITE,
            description=activity.follow.__doc__ or "", metadata={"risk": "write"},
            input_schema={"type": "object", "required": ["kind", "identifier"], "additionalProperties": False,
                          "properties": {"kind": {"type": "string", "enum": ["download", "organize", "local_media"]},
                                         "identifier": {"type": "integer", "minimum": 1},
                                         "hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24}}},
            prepare=partial(prepare, operation="follow"), execute_confirmed=partial(execute, operation="follow")),
        KernelToolSpec(name="activity.unfollow", domain="activity", effect=ToolEffect.WRITE,
            description=activity.unfollow.__doc__ or "", metadata={"risk": "write"},
            input_schema={"type": "object", "required": ["rule_id"], "additionalProperties": False,
                          "properties": {"rule_id": {"type": "string", "minLength": 8, "maxLength": 100}}},
            prepare=partial(prepare, operation="unfollow"), execute_confirmed=partial(execute, operation="unfollow")),
    )


def preferences_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    """偏好只用公共条件提交；既有 effect lifecycle 负责审计与回退凭证。"""
    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = prepare_media_preferences_change(
                    db.media_owner_digest(context.owner), arguments if operation == "update" else None,
                )
                if operation == "clear" and not prepared["current"]["explicit"]:
                    raise ToolPipelineError("当前没有已保存的显式媒体偏好", code="precondition_failed")
                preview = ToolResult(True, "confirmation_required",
                    "确认后将保存当前用户的显式媒体偏好" if operation == "update" else "确认后清除当前用户的显式媒体偏好并恢复默认值",
                    data=media_preferences_preview(prepared))
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "low_write", "preference_fingerprint": prepared["fingerprint"],
                              "confirmation": build_confirmation_contract(
                                  tool_name=f"preferences.{operation}", risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner
                        or active.tool_name != f"preferences.{operation}" or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                expected = active.metadata["preference_fingerprint"]
                if prefix + expected != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                updated = apply_media_preferences_change(db.media_owner_digest(context.owner),
                    arguments if operation == "update" else None, expected_fingerprint=expected)
                if updated is None:
                    raise ToolPipelineError("媒体偏好已变化，请重新预检", code="confirmation_stale")
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        return ToolResult(True, "completed", "显式媒体偏好已保存" if operation == "update" else "显式媒体偏好已清除",
            data={"operation": operation, "affected": 1,
                  **(public_media_preferences(updated) if operation == "update" else {})},
            effect_metadata={"compensation_after": deepcopy(updated)})

    return (
        KernelToolSpec(name="preferences.update", domain="subscription", description=preferences.update.__doc__ or "",
            input_schema={"type": "object", "properties": PREFERENCE_PROPERTIES,
                          "minProperties": 1, "additionalProperties": False},
            validator=validate_preference_updates,
            effect=ToolEffect.WRITE, prepare=partial(prepare, operation="update"),
            execute_confirmed=partial(execute, operation="update"), metadata={"risk": "low_write", "batch_mode": "sync"}),
        KernelToolSpec(name="preferences.clear", domain="subscription", description=preferences.clear.__doc__ or "",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE, prepare=partial(prepare, operation="clear"),
            execute_confirmed=partial(execute, operation="clear"), metadata={"risk": "low_write"}),
    )


def resource_download_tool() -> KernelToolSpec:
    def preflight(service, arguments):
        preview, snapshot = downloads.preview_indexer_download_batch(
            service, arguments["result_ids"], arguments["target"], scope="agent",
        )
        if not preview["ok"]:
            raise ToolPipelineError(preview["error"] or preview["summary"], code=preview["status"])
        return ToolResult(**preview), snapshot

    def fingerprint(snapshot, verifications):
        return _fingerprint({"download": snapshot, "verification_targets": verifications} if verifications else snapshot)

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                target = arguments.get("target", "preferred")
                if target == "preferred":
                    target, _ = resolve_download_target(context.owner)
                binding = arguments.get("_resource_selection")
                arguments = {key: value for key, value in arguments.items() if not key.startswith("_")}
                arguments["target"] = target
                service = get_indexer_service()
                preview, snapshot = preflight(service, arguments)
                verifications = downloads.prepare_download_verifications(
                    service, arguments["result_ids"], arguments.get("verification_targets") or [], scope="agent",
                )
                if binding:
                    for item in preview.data.get("resources", []):
                        if item.get("result_id") in binding["positions"]:
                            item["position"] = binding["positions"][item["result_id"]]
                if verifications:
                    preview.data["verification_targets"] = verifications
                    preview.data["verification_count"] = sum(map(len, verifications.values()))
                snapshot_fingerprint = prefix + fingerprint(snapshot, verifications)
                private_items = [_serialize_private_item(identity, service.result_store.get(identity))
                                 for identity in arguments["result_ids"]]
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        return PreparedEffect(
            preview=preview.to_dict(), snapshot_fingerprint=snapshot_fingerprint, arguments=arguments,
            metadata={"risk": "write", "source_kind": "indexer_results", "resource_items": private_items,
                      **({"resource_selection": binding} if binding else {}),
                      **({"verification_targets": verifications, "missing_media_candidates": [
                          {"result_id": identity, "verification": proof, "candidate_title": item["title"], "target": target}
                          for identity, item in zip(arguments["result_ids"], private_items)
                          for proof in verifications.get(identity, [])
                      ]} if verifications else {}),
                      "confirmation": build_confirmation_contract(
                          tool_name="resources.download", risk=RiskLevel.WRITE, preview=preview)},
        )

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                if arguments.get("target") not in {"qb", "guangya", "both"}:
                    raise ToolPipelineError("确认缺少冻结下载目标，请重新预检", code="confirmation_stale")
                service = get_indexer_service()
                assert context.active_effect_plan is not None
                # 私有候选随原计划持久化；只恢复缺失或过期缓存，不覆盖有效候选或绕过下方业务复验。
                for identity, raw in zip(arguments["result_ids"], context.active_effect_plan.metadata["resource_items"], strict=True):
                    try:
                        service.result_store.get(identity)
                    except (IndexerResultNotFound, IndexerResultExpired):
                        item = _deserialize_private_item(raw, expected_result_id=identity)
                        if item is None:
                            raise ToolPipelineError("确认资源快照无效，请重新预检", code="confirmation_stale")
                        service.result_store.restore(identity, item)
                _, snapshot = preflight(service, arguments)
                verifications = context.active_effect_plan.metadata.get("verification_targets") or {}
                if arguments.get("verification_targets") and not verifications:
                    raise ToolPipelineError("确认缺少冻结的复核目标，请重新预检", code="confirmation_stale")
                if prefix + fingerprint(snapshot, verifications) != expected_snapshot:
                    raise ToolPipelineError("资源或下载配置已变化，请重新预检", code="confirmation_stale")
                result = run_indexer_awaitable_sync(downloads.submit_indexer_download_batch(
                    service, arguments["result_ids"], arguments["target"],
                    expected_snapshot=snapshot, origin_namespace="agent", scope="agent",
                    verification_targets=verifications or None, verification_owner=context.owner if verifications else "",
                ))
        except downloads.DownloadSnapshotChanged as exc:
            error = ToolPipelineError(exc.public_message, code=exc.code)
            error.mark_execution_started(False)
            raise error from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        binding = context.active_effect_plan.metadata.get("resource_selection") or {}
        titles = {item["result_id"]: item["title"] for item in context.active_effect_plan.metadata["resource_items"]}
        result["source_type"] = "resource_candidates"
        for item in result["items"]:
            identity = item["result_id"]
            item.update(title=sanitize_resource_title(titles.get(identity, ""), limit=160), target=arguments["target"])
            if identity in binding.get("positions", {}):
                item["position"] = binding["positions"][identity]
        return ToolResult(
            ok=result["ok"], status=result["status"], summary=result["summary"], error=result["error"],
            data={key: value for key, value in result.items() if key not in {"ok", "status", "summary", "error"}},
        )

    return KernelToolSpec(
        name="resources.download", domain="resources", effect=ToolEffect.WRITE,
        description=resources.download.__doc__ or "",
        input_schema={"type": "object", "required": ["result_ids"], "additionalProperties": False,
                      "properties": {"result_ids": {"type": "array", "minItems": 1,
                                                      "items": {"type": "string", "minLength": 1}},
                                     "target": {"type": "string", "enum": ["preferred", "qb", "guangya", "both"],
                                                "default": "preferred"},
                                     "verification_targets": {"type": ["array", "null"], "items": {
                                         "type": "object", "required": ["result_id", "query", "season", "episode"],
                                         "additionalProperties": False, "properties": {
                                             "result_id": {"type": "string", "minLength": 1},
                                             "query": {"type": "string"}, "tmdb_id": {"type": "string"},
                                             "season": {"type": "integer", "minimum": 1, "maximum": 100},
                                             "episode": {"type": "integer", "minimum": 1, "maximum": 1000},
                                             "as_of": {"type": "string"}, "library_name": {"type": "string"},
                                         }}}}},
        prepare=prepare, execute_confirmed=execute,
        metadata={"source_kind": "indexer_results", "risk": "write"},
    )


def share_transfer_tool() -> KernelToolSpec:
    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = share_transfer.prepare_share_transfer(arguments["url"], arguments["file_ids"], context.owner, "agent")
                snapshot = prepared["snapshot"]
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        preview = ToolResult(True, "confirmation_required", f"确认后将转存 {len(snapshot['files'])} 项到光鸭",
            data={"files": snapshot["files"], "count": len(snapshot["files"]), "target": "guangya",
                  "target_name": snapshot["target_name"], "effects": ["转存所选文件", "按现有配置继续整理与STRM"]})
        return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(snapshot),
            arguments={"url": snapshot["url"], "file_ids": [item["id"] for item in snapshot["files"]]},
            metadata={"risk": "danger", "source_kind": "guangya_share", "share_snapshot": snapshot,
                      "confirmation": build_confirmation_contract(tool_name="cloud.transfer_share", risk=RiskLevel.DANGER, preview=preview)})

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if active is None or active.tool_name != "cloud.transfer_share" or dict(active.arguments) != arguments:
                    raise ValueError("缺少原分享确认范围")
                snapshot = active.metadata["share_snapshot"]
                if prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ValueError("分享确认配置已变化")
                context.cancellation.raise_if_cancelled()
                public = share_transfer.submit_prepared_share_transfer(snapshot, context.owner, user_id="agent", origin="agent", tracker_chat_id="", cancel_check=context.cancellation.raise_if_cancelled)
        except (AgentRuntimeDisabled, RuntimeFenceError, ValueError) as exc:
            error = ToolPipelineError("分享内容、目标或授权已变化，请重新检查", code="confirmation_stale")
            error.mark_execution_started(False)
            raise error from exc
        ok = public["success"] or bool(public.get("accepted"))
        return ToolResult(ok, "accepted" if ok else public["status"],
            "分享已转存；后续整理与入库请核对原请求" if public["success"] else "分享请求已受理" if ok else "分享转存未完成，请核对原请求",
            data=public, error=public.get("error", ""))

    return KernelToolSpec(name="cloud.transfer_share", domain="cloud_files", effect=ToolEffect.DANGER,
        description=cloud.transfer_share.__doc__ or "",
        input_schema={"type": "object", "required": ["url", "file_ids"], "additionalProperties": False,
                      "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 8192},
                                     "file_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}}},
        prepare=prepare, execute_confirmed=execute, metadata={"risk": "danger", "source_kind": "guangya_share"})


def download_diagnose_tool() -> KernelToolSpec:
    """工作区接力使用 SDK 的同一公共诊断入口。"""
    return KernelToolSpec(
        name="downloads.diagnose", domain="downloads", effect=ToolEffect.READ,
        description=sdk_downloads.diagnose.__doc__ or "",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        read=lambda arguments, context: sdk_downloads.diagnose(**arguments),
    )


def download_submit_tool() -> KernelToolSpec:
    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                target = arguments.get("target", "preferred")
                if target == "preferred":
                    target, _ = resolve_download_target(context.owner)
                data, snapshot = downloads.preview_download_url(arguments["url"], target)
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        preview = ToolResult(True, "confirmation_required", "确认后将把该资源提交到所选下载目标", data=data)
        return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(snapshot),
            arguments={"url": arguments["url"], "target": target},
            metadata={"risk": "danger", "source_kind": "direct_url",
                      "confirmation": build_confirmation_contract(
                          tool_name="downloads.submit", risk=RiskLevel.DANGER, preview=preview)})

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                if not expected_snapshot.startswith(prefix) or arguments.get("target") not in downloads.DOWNLOAD_TARGETS:
                    raise downloads.DownloadSnapshotChanged()
                try:
                    _, snapshot = downloads.preview_download_url(arguments["url"], arguments["target"])
                except ValueError as exc:
                    raise downloads.DownloadSnapshotChanged() from exc
                if prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise downloads.DownloadSnapshotChanged()
                context.cancellation.raise_if_cancelled()
                public = downloads.submit_download_url(arguments["url"], arguments["target"], origin="agent",
                                                        expected_snapshot=snapshot)
        except (AgentRuntimeDisabled, RuntimeFenceError, downloads.DownloadSnapshotChanged) as exc:
            if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
                message, code = "Media Agent 授权或运行状态已变化", "agent_disabled"
            else:
                message, code = "资源或下载配置已变化，请重新预检", "confirmation_stale"
            error = ToolPipelineError(message, code=code)
            error.mark_execution_started(False)
            raise error from exc
        status = {"submitted": "accepted", "partial": "partial", "duplicate": "conflict",
                  "manual_review": "outcome_unknown"}.get(public["status"], "unavailable")
        return ToolResult(public["ok"], status, public["summary"], error=public["error"],
            data={key: value for key, value in public.items() if key not in {"ok", "summary", "error"}})

    return KernelToolSpec(name="downloads.submit", domain="downloads", effect=ToolEffect.DANGER,
        description=sdk_downloads.submit.__doc__ or "",
        input_schema={"type": "object", "required": ["url"], "additionalProperties": False,
                      "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 8192, "writeOnly": True},
                                     "target": {"type": "string", "enum": ["preferred", "qb", "guangya", "both"],
                                                "default": "preferred"}}},
        prepare=prepare, execute_confirmed=execute, metadata={"risk": "danger", "source_kind": "direct_url"})


def download_retry_tool() -> KernelToolSpec:
    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                snapshot = download_dispatcher.prepare_download_retry(arguments["request_id"], arguments["target"])
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        preview = ToolResult(True, "confirmation_required",
            f"确认后将把下载请求 #{snapshot['request_id']} 重试到 {snapshot['target_label']}",
            data={key: value for key, value in snapshot.items() if key != "fingerprint"})
        return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + snapshot["fingerprint"],
            arguments={"request_id": snapshot["request_id"], "target": snapshot["target"]},
            metadata={"risk": "danger", "source_kind": "download_request",
                      "confirmation": build_confirmation_contract(
                          tool_name="downloads.retry", risk=RiskLevel.DANGER, preview=preview)})

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                if not expected_snapshot.startswith(prefix):
                    raise download_dispatcher.DownloadRetryChanged("确认时的配置已变化，请重新预检")
                result = download_dispatcher.resubmit_download_request(
                    arguments["request_id"], arguments["target"], origin="agent",
                    expected_fingerprint=expected_snapshot[len(prefix):],
                )
        except (AgentRuntimeDisabled, RuntimeFenceError, download_dispatcher.DownloadRetryChanged) as exc:
            code = "confirmation_stale" if isinstance(exc, download_dispatcher.DownloadRetryChanged) else "agent_disabled"
            error = ToolPipelineError(str(exc) if code == "confirmation_stale" else "Media Agent 授权或运行状态已变化", code=code)
            error.mark_execution_started(False)
            raise error from exc
        public = download_dispatcher.public_download_retry_result(
            result, source_request_id=arguments["request_id"], target=arguments["target"])
        return ToolResult(public["ok"], public["status"], public["summary"], error=public["error"],
                          data={key: value for key, value in public.items() if key not in {"ok", "status", "summary", "error"}})

    return KernelToolSpec(name="downloads.retry", domain="downloads", effect=ToolEffect.DANGER,
        description=sdk_downloads.retry.__doc__ or "",
        input_schema={"type": "object", "required": ["request_id", "target"], "additionalProperties": False,
                      "properties": {"request_id": {"type": "integer", "minimum": 1},
                                     "target": {"type": "string", "enum": ["qb", "guangya", "both"]}}},
        prepare=prepare, execute_confirmed=execute, metadata={"risk": "danger", "source_kind": "download_request"})


def cloud_change_tool() -> KernelToolSpec:
    def effect(arguments):
        return ToolEffect.DANGER if any(
            str(item["op"]).strip().casefold() == "trash" or
            (str(item["op"]).strip().casefold() == "batch_files" and
             ("trash_filter" in item or item.get("cleanup_empty_dirs")))
            for item in arguments["operations"]
        ) else ToolEffect.WRITE

    def public_error(exc, *, submission_started=False):
        if submission_started:
            error = ToolPipelineError("光鸭文件变更已受理但回执未知，请先核验原操作，勿重新提交", code="outcome_unknown")
            error.mark_execution_started(True)
            return error
        if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            message, code = "Media Agent 授权或运行状态已变化", "agent_disabled"
        elif isinstance(exc, (guangya_fs_change.GuangYaFSChangeStale, GuangYaWorkspaceStale)):
            message, code = str(exc), "confirmation_stale"
        elif isinstance(exc, (guangya_fs_change.GuangYaFSChangeError, GuangYaWorkspaceError, OrganizeOperationAdmissionError)):
            message, code = str(exc), "precondition_failed"
        else:
            message, code = "光鸭文件变更当前不可用", "unavailable"
        error = ToolPipelineError(message, code=code)
        error.mark_execution_started(False)
        return error

    def resource(item):
        labels = {"rename": "改名", "move": "移动", "copy": "复制", "relocate": "移动并改名",
                  "trash": "移入回收站", "create_directory": "新建目录"}
        source = str(item.get("source_path") or item.get("created_path") or "")
        target = str(item.get("target_path") or item.get("new_name") or "")
        if item.get("target_path") and item.get("new_name"):
            target += " / " + item["new_name"]
        source = source if len(source) <= 240 else source[:237] + "…"
        target = target if len(target) <= 240 else target[:237] + "…"
        return {"title": labels[item["op"]] + "：" + source + (" → " + target if target else ""), "count": 1}

    def preflight(arguments, context):
        plan = None
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                client = GuangYaClient()
                try:
                    plan = guangya_fs_change.build_fs_change_plan(
                        client, owner=context.owner, operations=arguments["operations"],
                        trigger_strm=arguments["trigger_strm"],
                    )
                finally:
                    client.close()
                context.cancellation.raise_if_cancelled()
                unchanged = plan["status"] == "no_changes"
                preview = ToolResult(
                    True, "no_changes" if unchanged else "confirmation_required",
                    "所选文件操作无需变更" if unchanged else f"确认后执行光鸭文件变更，共 {plan['stats']['total']} 项",
                    data={"operations": plan["operations"], "stats": plan["stats"],
                          "resources": [resource(item) for item in plan["operations"]], "count": plan["stats"]["total"],
                          "trigger_strm": plan["trigger_strm"], "cloud_write": False},
                )
                if unchanged:
                    return DefaultProjector().project(preview)
                risk = RiskLevel(effect(arguments).value)
                return PreparedEffect(
                    preview=preview.to_dict(), snapshot_fingerprint=prefix + plan["fingerprint"],
                    metadata={"source_kind": "native_cloud_change", "risk": risk.value,
                              "fs_change_plan_id": plan["plan_id"],
                              "confirmation": build_confirmation_contract(
                                  tool_name="cloud.change", risk=risk, preview=preview)},
                )
        except BaseException as exc:
            if plan is not None and plan.get("plan_id"):
                guangya_fs_change.discard_fs_change_plan(plan["plan_id"], preview_only=True)
            if isinstance(exc, Exception) and not isinstance(exc, ToolPipelineError):
                raise public_error(exc) from exc
            raise

    async def prepare(arguments, context):
        worker = asyncio.create_task(asyncio.to_thread(preflight, arguments, context))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            context.cancellation.cancel()
            try:
                undelivered = await asyncio.shield(worker)
            except BaseException:
                pass  # preflight 已按本次局部计划清理；保留原取消结果。
            else:
                if isinstance(undelivered, PreparedEffect):
                    guangya_fs_change.discard_fs_change_plan(
                        undelivered.metadata["fs_change_plan_id"], preview_only=True,
                    )
            raise

    def execute(arguments, expected_snapshot, context):
        submission_started = False
        try:
            with confirmation_runtime(context) as prefix:
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                active = context.active_effect_plan
                if active is None or active.owner != context.owner or context.completion_scope is None:
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                plan_id = active.metadata["fs_change_plan_id"]
                fingerprint = expected_snapshot.removeprefix(prefix)
                plan = guangya_fs_change.load_fs_change_plan(
                    plan_id, owner=active.owner, expected_fingerprint=fingerprint,
                )
                with db.get_conn() as conn:
                    fence = read_runtime_fence(conn)
                    require_runtime_fence(conn, fence)
                if prefix and not prefix.startswith(f"mfkr2:{fence.incarnation}:{fence.epoch}:"):
                    raise RuntimeFenceError("runtime_fence_changed")

                def admitted(conn, row, replayed):
                    nonlocal submission_started
                    require_runtime_fence(conn, fence)
                    effect_completion.admit_organize_operation(
                        conn, row, replayed, tool="cloud.change", scope=context.completion_scope,
                    )
                    submission_started = True

                context.cancellation.raise_if_cancelled()
                task = guangya_fs_change.submit_fs_change_plan(
                    plan_id, owner=active.owner, expected_fingerprint=fingerprint, on_admitted=admitted,
                )
                submission_started = True
            data = {"operation": "filesystem_change", "operation_ref": organize_operation_public_ref(task["task_id"]),
                    "queued": task["queued"], "queue_position": task["queue_position"],
                    "replayed": bool(task.get("replayed")), "stats": plan["stats"],
                    "total": plan["stats"]["total"], "trigger_strm": plan["trigger_strm"]}
            if task.get("replayed"):
                result = ToolResult(**get_organize_operation_status(data["operation_ref"], owner=active.owner))
                result.data = {**data, **result.data}
                return result
            return ToolResult(
                True, "accepted", "光鸭文件变更已排队" if task["queued"] else "光鸭文件变更任务已受理",
                data=data,
            )
        except Exception as exc:
            if isinstance(exc, ToolPipelineError) and not submission_started:
                exc.mark_execution_started(False)
                raise
            raise public_error(exc, submission_started=submission_started) from exc

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(
                result, tool="cloud.change",
                context=ToolContext(owner=context.owner, session_id=context.session_id,
                    request_id=context.request_id, cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope,
            )
        return result

    snapshot = {"type": "object", "additionalProperties": False,
                "required": ["file_id", "parent_id", "name", "is_dir", "size", "etag"],
                "properties": {**{key: {"type": "string"} for key in ("file_id", "parent_id", "name", "etag")},
                               "path": {"type": "string"},
                               "is_dir": {"type": "boolean"}, "size": {"type": "integer", "minimum": 0},
                               "updated_at": {"type": "integer", "minimum": 0}}}
    return KernelToolSpec(
        name="cloud.change", domain="guangya", effect=ToolEffect.WRITE, effect_resolver=effect,
        description=cloud.change.__doc__ or "光鸭文件变更",
        input_schema={"type": "object", "required": ["operations"], "additionalProperties": False,
                      "properties": {
                          "operations": {"type": "array", "minItems": 1, "items": {
                              "type": "object", "required": ["op"], "additionalProperties": False,
                              "properties": {
                                  "op": {"type": "string", "enum": ["rename", "move", "copy", "relocate", "trash", "create_directory", "batch_files"]},
                                  "trash_filter": {"type": "object"},
                                  "move_filter": {"type": "object"},
                                  "cleanup_empty_dirs": {"type": "boolean"},
                                  "conflict": {"type": "string", "enum": ["keep_both", "error"]},
                                  "expected": snapshot,
                                  "expected_tree": {"type": "array", "minItems": 1, "items": snapshot},
                                  "require_empty": {"type": "boolean"},
                                  **{name: {"type": "string", "minLength": 1} for name in (
                                      "file_id", "new_name", "target_path", "parent_path", "name", "target_id", "parent_id")},
                              },
                          }},
                          "trigger_strm": {"type": "boolean", "default": True},
                      }},
        validator=lambda arguments: {"operations": arguments["operations"], "trigger_strm": arguments.get("trigger_strm", True)},
        prepare=prepare, execute_confirmed=execute, verify=verify,
        metadata={"source_kind": "native_cloud_change", "risk": "write",
                  "workflow": "guangya_fs_change", "workflow_stage": 10, "batch_mode": "tracked"},
    )


def qb_control_tools() -> tuple[KernelToolSpec, ...]:
    """qB 控制复用公共预检/执行和唯一 EffectExecutor 确认账本。"""
    from app.modules import qb_control

    def failure(exc):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, qb_control.QBControlStale):
            error = ToolPipelineError(str(exc), code="confirmation_stale")
        elif isinstance(exc, qb_control.QBControlSafetyUnavailable):
            error = ToolPipelineError(str(exc), code="provider_unavailable")
        elif isinstance(exc, qb_control.QBControlConflict):
            error = ToolPipelineError(str(exc), code="provider_conflict")
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
        else:
            error = ToolPipelineError("qBittorrent 控制当前不可用，请重新查询后确认", code="provider_unavailable")
        error.mark_execution_started(False)
        return error

    def prepare(arguments, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                snapshot = qb_control.prepare_qb_control(operation, arguments["hashes"])
                context.cancellation.raise_if_cancelled()
                risk = RiskLevel.WRITE if operation == "delete" else RiskLevel.LOW_WRITE
                action = {"pause": "暂停", "resume": "恢复", "delete": "移除并保留文件"}[operation]
                preview = ToolResult(True, "confirmation_required", f"确认后将{action} {len(snapshot['targets'])} 个 qB 任务",
                    data={"count": len(snapshot["targets"]), "delete_files": False,
                          "resources": [{"title": sanitize_untrusted_filename(item["name"]) or "下载任务", "count": 1}
                                        for item in snapshot["targets"]]})
                return PreparedEffect(preview=preview.to_dict(),
                    snapshot_fingerprint=prefix + signed_context_fingerprint(snapshot, domain="qb-task-control"),
                    metadata={"risk": risk.value, "source_kind": "native_qb_control", "qb_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(tool_name=name, risk=risk, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def submit(arguments, expected_snapshot, context, *, name):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != name
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("qb_snapshot")
                if (not isinstance(snapshot, dict)
                        or prefix + signed_context_fingerprint(snapshot, domain="qb-task-control") != expected_snapshot):
                    raise ToolPipelineError("确认时的配置或 qB 快照已变化，请重新预检", code="confirmation_stale")
                receipt = qb_control.execute_qb_control(snapshot, cancel_check=context.cancellation.raise_if_cancelled)
        except Exception as exc:
            raise failure(exc) from exc
        return ToolResult(receipt["ok"], receipt["status"], receipt["summary"],
                          data={key: value for key, value in receipt.items() if key not in {"ok", "status", "summary"}})

    async def execute(arguments, expected_snapshot, context, *, name):
        return await asyncio.to_thread(submit, arguments, expected_snapshot, context, name=name)

    specs = []
    for operation, method in (("pause", "pause"), ("resume", "resume"), ("delete", "remove")):
        name = "downloads." + method
        specs.append(KernelToolSpec(name=name, domain="downloads", effect=ToolEffect.WRITE,
            description=getattr(sdk_downloads, method).__doc__ or name,
            input_schema={"type": "object", "required": ["hashes"], "additionalProperties": False,
                          "properties": {"hashes": {"type": "array", "minItems": 1, "maxItems": 20,
                            "items": {"type": "string", "pattern": "^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"}}}},
            prepare=partial(prepare, operation=operation, name=name), execute_confirmed=partial(execute, name=name),
            metadata={"risk": "write" if operation == "delete" else "low_write"}))
    return tuple(specs)


def cloud_share_tools() -> tuple[KernelToolSpec, ...]:
    """分享写只冻结公共客户端快照，确认、CAS 和重复执行沿唯一账本。"""
    def fingerprint(snapshot, revoke):
        return signed_context_fingerprint(snapshot, domain="guangya-share-revoke" if revoke else "guangya-share-create")

    def failure(exc):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
        elif isinstance(exc, GuangYaShareError):
            error = ToolPipelineError(str(exc), code=exc.code)
        elif isinstance(exc, GuangYaWriteRejected):
            error = ToolPipelineError("光鸭拒绝了分享操作", code="provider_rejected")
        else:
            error = ToolPipelineError("光鸭分享操作当前不可用", code="unavailable")
        error.mark_execution_started(False)
        return error

    def prepare(arguments, context, *, revoke):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                client = GuangYaClient()
                try:
                    snapshot = (client.prepare_user_share_revoke(arguments["share_ids"]) if revoke
                                else client.prepare_user_share(**arguments))
                finally:
                    close_guangya_client(client)
                context.cancellation.raise_if_cancelled()
                items = snapshot["items"]
                data: dict[str, Any] = {"count": len(items), "cloud_write": False,
                        "resources": [{"title": sanitize_resource_title(item.get("title") if revoke else item["name"]) or "未命名对象",
                                       "count": 1} for item in items]}
                if not revoke:
                    policy = snapshot["policy"]
                    data.update({key: policy[key] for key in ("title", "expires_days", "max_restore_count", "allow_download")},
                                access_policy="custom_code" if policy["access_code"] else "auto_code" if policy["auto_access_code"] else "no_code")
                data["effects"] = (["只撤销选中分享链接；不删除、移动或改名原始文件。"] if revoke else
                                   ["确认时重新校验真实对象、登录凭据与分享策略。", "分享链接和访问码仅返回当前用户。"])
                name = "cloud.revoke_share" if revoke else "cloud.share"
                preview = ToolResult(True, "confirmation_required",
                    f"确认后将撤销 {len(items)} 条光鸭分享" if revoke else f"确认后将为 {len(items)} 个光鸭对象创建分享", data=data)
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + fingerprint(snapshot, revoke),
                    metadata={"risk": "write", "source_kind": "native_cloud_share", "share_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(tool_name=name, risk=RiskLevel.WRITE, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def submit(arguments, expected_snapshot, context, *, revoke):
        try:
            with confirmation_runtime(context) as prefix:
                name = "cloud.revoke_share" if revoke else "cloud.share"
                active = context.active_effect_plan
                if (active is None or context.completion_scope is None or active.owner != context.owner
                        or active.tool_name != name or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("share_snapshot")
                if not isinstance(snapshot, dict) or prefix + fingerprint(snapshot, revoke) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或分享快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                client = GuangYaClient()
                try:
                    receipt = client.submit_prepared_user_share(snapshot, revoke=revoke,
                        cancel_check=context.cancellation.raise_if_cancelled)
                finally:
                    close_guangya_client(client)
        except Exception as exc:
            raise failure(exc) from exc
        data = {key: value for key, value in receipt.items() if key not in {"ok", "status", "error"}}
        return ToolResult(receipt["ok"], receipt["status"],
            ("光鸭分享撤销" if revoke else "光鸭分享创建") + ("已完成" if receipt["verified"] else "已受理")
            if receipt["ok"] else receipt["error"], data=data, error=receipt.get("error", ""),
            model_data={key: value for key, value in data.items() if key not in {"share_url", "access_code"}})

    async def execute(arguments, expected_snapshot, context, *, revoke):
        return await asyncio.to_thread(submit, arguments, expected_snapshot, context, revoke=revoke)

    ids = {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "minLength": 1}}
    return tuple(KernelToolSpec(name=name, domain="guangya", effect=ToolEffect.WRITE,
        description=method.__doc__ or name,
        input_schema={"type": "object", "required": ["share_ids" if revoke else "file_ids"], "additionalProperties": False,
                      "properties": {"share_ids": ids} if revoke else {
                          "file_ids": ids, "title": {"type": "string", "maxLength": 180},
                          "expires_days": {"type": "integer", "minimum": 0, "maximum": 3650},
                          "access_code": {"type": "string", "pattern": "^(?:[A-Za-z0-9]{4,16})?$"},
                          "auto_access_code": {"type": "boolean"}, "allow_download": {"type": "boolean"},
                          "max_restore_count": {"type": "integer", "minimum": 0, "maximum": 1_000_000}}},
        prepare=partial(prepare, revoke=revoke), execute_confirmed=partial(execute, revoke=revoke),
        metadata={"risk": "write"})
        for name, method, revoke in (("cloud.share", cloud.share, False), ("cloud.revoke_share", cloud.revoke_share, True)))


def cloud_recycle_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    """回收站沿唯一确认账本；冻结真实对象，复用既有 Provider task 回执。"""
    def fingerprint(snapshot, operation):
        return signed_context_fingerprint(snapshot, domain="guangya-recycle-" + operation)

    def failure(exc):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
        elif isinstance(exc, guangya_recycle.GuangYaRecycleError):
            error = ToolPipelineError(str(exc), code=exc.code)
        elif isinstance(exc, GuangYaWriteRejected):
            error = ToolPipelineError(exc.public_message or "光鸭拒绝了回收站操作", code="provider_rejected")
        else:
            error = ToolPipelineError("光鸭回收站操作当前不可用", code="unavailable")
        # 公共服务仅将尚未提交或 Provider 明确拒绝的异常抛出；写后未知返回真实回执。
        error.mark_execution_started(False)
        return error

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                client = GuangYaClient()
                try:
                    snapshot = (guangya_recycle.prepare_restore(client, arguments["file_ids"])
                                if operation == "restore" else guangya_recycle.prepare_clear(client))
                finally:
                    client.close()
                context.cancellation.raise_if_cancelled()
                clear = operation == "clear"
                name = "cloud.clear_recycle" if clear else "cloud.restore"
                risk = RiskLevel.DANGER if clear else RiskLevel.WRITE
                items = snapshot["items"]
                data = {"count": len(items), "total_size": sum(item["size"] for item in items),
                        "samples": [sanitize_untrusted_filename(item["name"]) or "未命名对象" for item in items[:6]],
                        "cloud_write": False}
                if clear:
                    data.update(irreversible=True, effects=[
                        "永久删除整个账号回收站；无法通过 MediaFlux 或光鸭恢复。",
                        "执行前复核完整集合；Provider 全局清空期间新进入回收站的对象也可能被删除。",
                    ])
                else:
                    data["resources"] = [{"title": sanitize_untrusted_filename(item["name"]) or "未命名对象",
                                          "count": 1} for item in items]
                preview = ToolResult(True, "confirmation_required",
                    f"确认后将永久清空光鸭回收站，共 {len(items)} 个对象" if clear
                    else f"确认后将从光鸭回收站恢复 {len(items)} 个对象", data=data)
                return PreparedEffect(preview=preview.to_dict(),
                    snapshot_fingerprint=prefix + fingerprint(snapshot, operation),
                    metadata={"risk": risk.value, "source_kind": "native_cloud_recycle",
                              "recycle_snapshot": snapshot, "confirmation": build_confirmation_contract(
                                  tool_name=name, risk=risk, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def submit(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                name = "cloud.restore" if operation == "restore" else "cloud.clear_recycle"
                active = context.active_effect_plan
                if (active is None or context.completion_scope is None or active.owner != context.owner or active.tool_name != name
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("recycle_snapshot")
                if not isinstance(snapshot, dict) or prefix + fingerprint(snapshot, operation) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或回收站快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                client = GuangYaClient()
                try:
                    submit = guangya_recycle.restore_prepared if operation == "restore" else guangya_recycle.clear_prepared
                    receipt = submit(client, snapshot, cancel_check=context.cancellation.raise_if_cancelled)
                finally:
                    client.close()
        except Exception as exc:
            raise failure(exc) from exc
        task_id = receipt["task_id"]
        return ToolResult(receipt["ok"], receipt["status"],
            (f"光鸭已受理 {receipt['count']} 个对象的恢复请求" if operation == "restore"
             else f"光鸭已受理清空回收站请求，涉及 {receipt['count']} 个对象")
            if receipt["ok"] else receipt["error"],
            data={key: value for key, value in receipt.items() if key not in {"ok", "status", "error"}},
            error=receipt.get("error", ""),
            references=[ToolReference("guangya_task", {"task_id": task_id, "operation": receipt["operation"]},
                                      ttl_seconds=24 * 60 * 60)] if task_id else [])

    async def execute(arguments, expected_snapshot, context, *, operation):
        result = await asyncio.to_thread(submit, arguments, expected_snapshot, context, operation=operation)
        name = "cloud.restore" if operation == "restore" else "cloud.clear_recycle"
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool=name,
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, name, context.completion_scope)
        return result

    return (
        KernelToolSpec(name="cloud.restore", domain="guangya", effect=ToolEffect.WRITE,
            description=cloud.restore.__doc__ or "恢复光鸭回收站对象",
            input_schema={"type": "object", "required": ["file_ids"], "additionalProperties": False,
                          "properties": {"file_ids": {"type": "array", "minItems": 1,
                            "maxItems": guangya_recycle.MAX_RESTORE_ITEMS,
                            "items": {"type": "string", "minLength": 1}}}},
            prepare=partial(prepare, operation="restore"), execute_confirmed=partial(execute, operation="restore"),
            metadata={"risk": "write"}),
        KernelToolSpec(name="cloud.clear_recycle", domain="guangya", effect=ToolEffect.DANGER,
            description=cloud.clear_recycle.__doc__ or "永久清空光鸭回收站",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            prepare=partial(prepare, operation="clear"), execute_confirmed=partial(execute, operation="clear"),
            metadata={"risk": "danger"}),
    )


def media_mapping_tool() -> KernelToolSpec:
    """显式来源与目标沿原确认账本执行；公共服务负责复核与 CAS。"""
    from app.sdk import media

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                if not config.get_bool("DISCOVERY_ENABLED"):
                    raise ToolPipelineError("影视探索当前未启用", code="disabled")
                snapshot = get_discovery_service().prepare_tmdb_mapping(**arguments)
                context.cancellation.raise_if_cancelled()
                if snapshot is None:
                    raise ToolPipelineError("没有找到映射来源详情", code="not_found")
                source, target, mapping = snapshot["source"], snapshot["target"], snapshot["mapping"]
                if source["provider"] == "tmdb" or (mapping and mapping["confirmed"] and mapping["tmdb_id"] == target["tmdb_id"]):
                    return DefaultProjector().project(ToolResult(True, "no_changes", "该条目已有相同的确认映射"))
                preview = ToolResult(
                    True, "confirmation_required", f"确认后将《{source['title']}》映射到《{target['title']}》",
                    data={"provider": source["provider"], "external_id": source["external_id"],
                          "media_type": source["media_type"], "source_title": source["title"], "source_year": source["year"],
                          "tmdb_id": target["tmdb_id"], "candidate_title": target["title"], "candidate_year": target["year"],
                          "affected": 1, "resources": [{"title": source["title"], "count": 1}],
                          "effects": ["保存已核验的来源到 TMDB 身份映射。", "不会加入收藏、创建订阅、搜索资源或下载。"]},
                )
                return PreparedEffect(
                    preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(snapshot),
                    metadata={"risk": "low_write", "source_kind": "discovery_mapping", "mapping_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(
                                  tool_name="media.confirm_mapping", risk=RiskLevel.LOW_WRITE, preview=preview)},
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ProviderError as exc:
            raise ToolPipelineError(exc.safe_message or "影视数据源暂时不可用", code="unavailable") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.session_id != context.session_id
                        or active.tool_name != "media.confirm_mapping" or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("mapping_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                if not config.get_bool("DISCOVERY_ENABLED"):
                    raise ToolPipelineError("影视探索当前未启用", code="disabled")
                context.cancellation.raise_if_cancelled()
                saved = get_discovery_service().confirm_tmdb_mapping_if_unchanged(
                    **arguments, expected_mapping=snapshot["mapping"], expected_source=snapshot["source"],
                    expected_target=snapshot["target"], check_cancelled=context.cancellation.raise_if_cancelled,
                )
                if saved is None:
                    raise ToolPipelineError("来源、目标或现有映射已变化，请重新预检", code="confirmation_stale")
        except (AgentRuntimeDisabled, RuntimeFenceError, ProviderError, ValueError, ToolPipelineError) as exc:
            code = (exc.code if isinstance(exc, ToolPipelineError) else
                    "agent_disabled" if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)) else
                    "unavailable" if isinstance(exc, ProviderError) else "confirmation_stale")
            message = exc.safe_message if isinstance(exc, ProviderError) else str(exc)
            error = ToolPipelineError(message, code=code)
            error.mark_execution_started(False)
            raise error from exc
        return ToolResult(True, "completed", f"已确认《{snapshot['source']['title']}》的 TMDB 映射",
                          data={**arguments, "affected": 1, "mapping_confirmed": True,
                                "candidate_title": snapshot["target"]["title"]})

    return KernelToolSpec(
        name="media.confirm_mapping", domain="discovery", effect=ToolEffect.WRITE,
        description=media.confirm_mapping.__doc__ or "",
        input_schema={"type": "object", "required": ["provider", "external_id", "media_type", "tmdb_id"],
                      "additionalProperties": False, "properties": {
                          "provider": {"type": "string", "enum": ["tmdb", "douban", "bangumi"]},
                          "external_id": {"type": "string", "minLength": 1, "maxLength": 180},
                          "media_type": {"type": "string", "enum": ["movie", "tv"]},
                          "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"}}},
        prepare=prepare, execute_confirmed=execute,
        metadata={"source_kind": "discovery_mapping", "risk": "low_write"},
    )


def watchlist_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    """收藏两阶段写入只冻结公共业务快照，确认票据由 EffectExecutor 持有。"""
    def normalize_add(arguments):
        provider, media_type, external_id = watchlist_identity(
            arguments["provider"], arguments["media_type"], arguments["external_id"],
        )
        return {"provider": provider, "media_type": media_type, "external_id": external_id}

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                if not config.get_bool("DISCOVERY_ENABLED"):
                    raise ToolPipelineError("影视探索当前未启用", code="disabled")
                if operation == "add":
                    snapshot = get_discovery_service().prepare_watchlist_add(**arguments)
                    if snapshot is None:
                        raise ToolPipelineError("没有找到可加入收藏的影视条目", code="not_found")
                    unchanged = not snapshot["expected_absent"]
                    item = snapshot.get("canonical", {})
                else:
                    snapshot = DiscoveryService.get_watchlist_snapshot(arguments["watchlist_id"])
                    unchanged = not snapshot["exists"]
                    item = snapshot
                context.cancellation.raise_if_cancelled()
                if unchanged:
                    return DefaultProjector().project(ToolResult(
                        True, "no_changes", "该条目已在探索收藏中" if operation == "add" else "该收藏已不存在，无需移除",
                    ))
                title = item["title"] or "未命名条目"
                preview = ToolResult(
                    True, "confirmation_required",
                    f"确认后将《{title}》加入探索收藏" if operation == "add" else f"确认后从探索收藏移除《{title}》",
                    data={"operation": operation, "title": title, "provider": item["provider"],
                          "media_type": item["media_type"], "year": item["year"], "affected": 1,
                          "resources": [{"title": title, "count": 1}],
                          **({"id": arguments["watchlist_id"]} if operation == "remove" else {})},
                )
                return PreparedEffect(
                    preview=preview.to_dict(), snapshot_fingerprint=prefix + _fingerprint(snapshot),
                    metadata={"risk": "low_write", "source_kind": "discovery_watchlist", "watchlist_snapshot": snapshot,
                              "confirmation": build_confirmation_contract(
                                  tool_name=f"watchlist.{operation}", risk=RiskLevel.LOW_WRITE, preview=preview)},
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ProviderError as exc:
            raise ToolPipelineError(exc.safe_message or "影视数据源暂时不可用", code="unavailable") from exc
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="invalid_arguments") from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner
                        or active.tool_name != f"watchlist.{operation}" or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata["watchlist_snapshot"]
                if prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                if not config.get_bool("DISCOVERY_ENABLED"):
                    raise ToolPipelineError("影视探索当前未启用", code="disabled")
                context.cancellation.raise_if_cancelled()
                if operation == "add":
                    item = snapshot["canonical"]
                    number = DiscoveryService.add_watchlist(MediaCard(**item), expected_absent=True)
                    changed = number is not None
                else:
                    item, number = snapshot, arguments["watchlist_id"]
                    changed = DiscoveryService.remove_watchlist(
                        item["provider"], item["media_type"], item["external_id"], expected_snapshot=snapshot,
                    )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        if not changed:
            return ToolResult(False, "conflict", "探索收藏状态已变化，请重新预检", error="确认快照已失效。")
        title = item["title"] or "该条目"
        return ToolResult(
            True, "success", f"已将《{title}》加入探索收藏" if operation == "add" else f"已从探索收藏移除《{title}》",
            data={"operation": operation, "id": number, "title": title, "affected": 1},
        )

    return (
        KernelToolSpec(
            name="watchlist.add", domain="discovery", effect=ToolEffect.WRITE,
            description="确认后将核实的作品加入本地探索收藏，不创建订阅或下载；已收藏时无需变更。",
            input_schema={"type": "object", "required": ["provider", "media_type", "external_id"],
                          "additionalProperties": False, "properties": {
                              name: {"type": "string", "minLength": 1} for name in ("provider", "media_type", "external_id")}},
            validator=normalize_add,
            approval_scope=watchlist_approval_scope,
            prepare=partial(prepare, operation="add"), execute_confirmed=partial(execute, operation="add"),
            metadata={"source_kind": "discovery_watchlist", "risk": "low_write", "batch_mode": "sync"},
        ),
        KernelToolSpec(
            name="watchlist.remove", domain="discovery", effect=ToolEffect.WRITE,
            description="确认后按 watchlist.list 返回的 id 移除该收藏，不删除媒体文件或取消订阅。",
            input_schema={"type": "object", "required": ["watchlist_id"], "additionalProperties": False,
                          "properties": {"watchlist_id": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}}},
            prepare=partial(prepare, operation="remove"), execute_confirmed=partial(execute, operation="remove"),
            metadata={"source_kind": "discovery_watchlist", "risk": "low_write"},
        ),
    )


def directory_scrape_tool() -> KernelToolSpec:
    """真实对象和媒体身份直接冻结到既有effect/job账本，不保留Agent刮削Flow。"""
    from inspect import signature

    from app.modules.directory_scrape import get_directory_scrape_service
    from app.modules.directory_scrape_errors import DirectoryScrapePublicError, public_error_message
    from app.modules.episode_mapping import NUMBERING_MODES

    parameters = signature(cloud.scrape)

    def normalize(arguments):
        try:
            bound = parameters.bind(**arguments)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError as exc:
            raise ToolPipelineError("刮削参数不完整或包含未知字段", code="invalid_arguments") from exc

    def failure(exc, *, submitted=None):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
        elif isinstance(exc, DirectoryScrapePublicError):
            error = ToolPipelineError(public_error_message(exc),
                code="confirmation_stale" if exc.status_code in {409, 410} else "precondition_failed")
        elif isinstance(exc, ValueError):
            error = ToolPipelineError("刮削冻结参数无效，请重新预检", code="confirmation_stale")
        else:
            error = ToolPipelineError("目录刮削当前不可用", code="unavailable")
        if submitted is not None:
            error.mark_execution_started(submitted and not isinstance(exc, (
                DirectoryScrapePublicError, OrganizeOperationAdmissionError, AgentRuntimeDisabled, RuntimeFenceError,
            )))
        return error

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                built = get_directory_scrape_service().build_execution(context.owner, **arguments)
                context.cancellation.raise_if_cancelled()
                data = deepcopy(built["preview"])
                match = data["match"]
                resources = []
                for item in [*data["plans"], *data["companion_plans"]]:
                    source = str(item.get("original_path") or item.get("original_name") or item.get("file_id") or "")
                    target = str(item.get("target_path") or "") + "/" + str(item.get("new_name") or "")
                    title = f"{match['provider']}:{match['external_id']} · {item.get('action', '伴随文件')}：{source} → {target}"
                    resources.append({"title": title if len(title) <= 320 else title[:317] + "…", "count": 1})
                data.update(resources=resources, count=len(resources))
                preview = ToolResult(True, "confirmation_required", f"确认后刮削{len(data['plans'])}个视频计划", data=data)
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + built["fingerprint"],
                    metadata={"risk": "danger", "source_kind": "native_directory_scrape",
                        "directory_scrape_payload": deepcopy(built["execution_payload"]),
                        "confirmation": build_confirmation_contract(tool_name="cloud.scrape", risk=RiskLevel.DANGER, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def execute(arguments, expected_snapshot, context):
        submitted = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "cloud.scrape"
                        or dict(active.arguments) != arguments or context.completion_scope is None):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                payload = active.metadata.get("directory_scrape_payload")
                if not isinstance(payload, dict) or prefix + str(payload.get("expected_context") or "") != expected_snapshot:
                    raise ToolPipelineError("刮削冻结计划已变化，请重新预检", code="confirmation_stale")
                with db.get_conn() as conn:
                    fence = read_runtime_fence(conn)
                    require_runtime_fence(conn, fence)
                if prefix and not prefix.startswith(f"mfkr2:{fence.incarnation}:{fence.epoch}:"):
                    raise RuntimeFenceError("runtime_fence_changed")

                def admitted(conn, row, replayed):
                    require_runtime_fence(conn, fence)
                    effect_completion.admit_organize_operation(conn, row, replayed,
                        tool="cloud.scrape", scope=context.completion_scope)

                context.cancellation.raise_if_cancelled()
                submitted = True
                return ToolResult(**get_directory_scrape_service().submit_execution(active.owner, payload, on_admitted=admitted))
        except Exception as exc:
            raise failure(exc, submitted=submitted) from exc

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool="cloud.scrape",
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        return result

    return KernelToolSpec(name="cloud.scrape", domain="guangya", effect=ToolEffect.DANGER,
        description=cloud.scrape.__doc__ or "光鸭媒体刮削",
        input_schema={"type": "object", "required": ["file_id", "media_type"], "additionalProperties": False,
            "properties": {"file_id": {"type": "string", "minLength": 1},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
                "provider": {"type": "string", "enum": ["tmdb", "metatube"], "default": "tmdb"},
                "external_id": {"type": "string", "default": ""}, "tmdb_id": {"type": "string", "default": ""},
                "season": {"type": ["integer", "null"], "minimum": 0, "maximum": 99, "default": None},
                "episode": {"type": ["integer", "null"], "minimum": 1, "maximum": 999, "default": None},
                "numbering_mode": {"type": "string", "enum": list(NUMBERING_MODES), "default": "auto"}}},
        validator=normalize, prepare=prepare, execute_confirmed=execute, verify=verify,
        metadata={"source_kind": "native_directory_scrape", "risk": "danger"})


def local_task_tools() -> tuple[KernelToolSpec, ...]:
    from inspect import signature
    from app.modules import local_media_tasks as tasks, local_media_library as library
    from app.modules.local_media_service import LocalMediaServiceError
    from app.modules.episode_mapping import NUMBERING_MODES
    from app.repositories.local_media import LocalMediaTaskChanged
    from app.sdk import local

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = tasks.prepare_local_task_retry(**arguments) if operation == "retry" else library.prepare_local_task_refresh(**arguments)
                preview = ToolResult(**prepared["preview"])
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "low_write", "source_kind": "native_local_task", "task_fingerprint": prepared["fingerprint"],
                              "confirmation": build_confirmation_contract(tool_name="local." + operation, risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (LocalMediaServiceError, ValueError, LookupError) as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "local." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if prefix + active.metadata["task_fingerprint"] != expected_snapshot:
                    raise ToolPipelineError("运行配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                function = tasks.retry_local_task if operation == "retry" else library.refresh_local_task_library
                result = ToolResult(**function(**arguments, expected_fingerprint=active.metadata["task_fingerprint"],
                                               cancelled=lambda: context.cancellation.cancelled))
                if operation == "retry" and result.ok:
                    result.effect_metadata["completion"] = {"kind": "local_media_task", "task_id": arguments["task_id"], "operation": "retry"}
                return result
        except LocalMediaTaskChanged as exc:
            error = ToolPipelineError(str(exc), code="confirmation_stale")
            error.mark_execution_started(False)
            raise error from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except Exception as exc:
            error = ToolPipelineError("本地任务操作回执未知，请按真实task_id读取核实，勿直接重投", code="outcome_unknown")
            error.mark_execution_started(started)
            raise error from exc

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool="local.retry",
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, "local.retry", context.completion_scope)
        return result

    identity = {"task_id": {"type": "integer", "minimum": 1}}
    selection = {"tmdb_id": {"type": "string", "maxLength": 32}, "media_type": {"type": "string", "enum": ["movie", "tv"]},
                 "season": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
                 "episode": {"type": ["integer", "null"], "minimum": 1, "maximum": 999},
                 "numbering_mode": {"type": "string", "enum": list(NUMBERING_MODES)}}
    tools = []
    for operation, method, properties, required in (
        ("tasks", local.tasks, {"scope": {"type": "string", "enum": sorted(tasks.SCOPES)},
            "source_id": {"type": ["integer", "null"], "minimum": 1}, "scan_ref": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}}, []),
        ("task", local.task, {**identity, "inspect": {"type": "boolean", "default": False}}, ["task_id"]),
        ("preview", local.preview, {**identity, **selection}, ["task_id"]),
        ("retry", local.retry, {**identity, **selection, "confirm_interrupted_write": {"type": "boolean", "default": False}}, ["task_id"]),
        ("refresh_library", local.refresh_library, identity, ["task_id"]),
        ("verify_library", local.verify_library, identity, ["task_id"]), ("diagnose", local.diagnose, {}, []),
    ):
        def normalize(arguments, parameters=signature(method)):
            bound = parameters.bind(**arguments)
            bound.apply_defaults()
            return {key: value for key, value in bound.arguments.items() if value is not Ellipsis}
        write = operation in {"retry", "refresh_library"}
        tools.append(KernelToolSpec(name="local." + operation, domain="local_media", effect=ToolEffect.WRITE if write else ToolEffect.READ,
            description=method.__doc__ or "", input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            validator=normalize, read=None if write else lambda arguments, context, function=method: function(**arguments),
            prepare=partial(prepare, operation=operation) if write else None, execute_confirmed=partial(execute, operation=operation) if write else None,
            verify=verify if operation == "retry" else None, metadata={"risk": "low_write" if write else "read", "source_kind": "native_local_task",
                 "related_tools": ("local.tasks", "local.task") if write else ()}))
    return tuple(tools)


def local_source_tools() -> tuple[KernelToolSpec, ...]:
    from inspect import signature
    from app.modules import local_media_service as service
    from app.repositories.local_media import LocalMediaSourceChanged
    from app.sdk import local

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = (service.prepare_local_source_scan(**arguments) if operation == "scan_sources" else
                    service.prepare_local_source_change(operation.removesuffix("_source"), arguments.get("source_id"),
                        {key: value for key, value in arguments.items() if key != "source_id"}))
                preview = ToolResult(**{key: prepared[key] for key in ("ok", "status", "summary", "data")})
                risk = RiskLevel.DANGER if operation == "delete_source" else RiskLevel.LOW_WRITE if operation == "scan_sources" else RiskLevel.WRITE
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": risk.value, "source_kind": "native_local_source", "local_fingerprint": prepared["fingerprint"],
                              "source_ids": prepared.get("arguments", {}).get("source_ids"),
                              "confirmation": build_confirmation_contract(tool_name="local." + operation, risk=risk, preview=preview)})
        except (ValueError, LookupError) as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "local." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if prefix + active.metadata["local_fingerprint"] != expected_snapshot:
                    raise ToolPipelineError("运行配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                raw = (service.scan_local_sources(active.metadata["source_ids"], arguments.get("query", ""),
                        expected_fingerprint=active.metadata["local_fingerprint"], cancelled=lambda: context.cancellation.cancelled)
                    if operation == "scan_sources" else service.apply_local_source_change(operation.removesuffix("_source"),
                        arguments.get("source_id"), {key: value for key, value in arguments.items() if key != "source_id"},
                        expected_fingerprint=active.metadata["local_fingerprint"], cancelled=lambda: context.cancellation.cancelled))
            result = ToolResult(**raw)
            if operation == "scan_sources" and result.data.get("queued_count"):
                result.effect_metadata["completion"] = {"kind": "local_media_scan", "scan_ref": result.data["scan_ref"]}
            return result
        except LocalMediaSourceChanged as exc:
            error = ToolPipelineError(str(exc), code="confirmation_stale")
            error.mark_execution_started(False)
            raise error from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except (ValueError, LookupError) as exc:
            error = ToolPipelineError(str(exc), code="precondition_failed")
            error.mark_execution_started(False)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("本地来源操作结果未知，请读取来源/任务核实，勿直接重投", code="unavailable")
            error.mark_execution_started(started)
            raise error from exc

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool="local.scan_sources",
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, "local.scan_sources", context.completion_scope)
        return result

    fields = {"name": {"type": "string", "minLength": 1, "maxLength": 128}, "local_root": {"type": "string", "minLength": 1},
              "qb_profile": {"type": "string"}, "qb_path_prefix": {"type": "string"}, "enabled": {"type": "boolean"},
              "media_type": {"type": "string", "enum": ["auto", "movie", "tv", "nsfw"]},
              "mode": {"type": "string", "enum": ["move", "preview_only"]},
              "targets": {"type": ["array", "null"], "items": {"type": "object"}}}
    identity = {"source_id": {"type": "integer", "minimum": 1}}
    tools = []
    for operation, method, properties, required in (
        ("sources", local.sources, {}, []), ("source", local.source, identity, ["source_id"]),
        ("create_source", local.create_source, fields, ["name", "local_root"]),
        ("update_source", local.update_source, {**identity, **fields}, ["source_id"]),
        ("delete_source", local.delete_source, identity, ["source_id"]),
        ("scan_sources", local.scan_sources, {"source_ids": {"type": ["array", "null"], "items": {"type": "integer", "minimum": 1}},
                                            "query": {"type": "string", "maxLength": 120}}, []),
    ):
        def normalize(arguments, parameters=signature(method)):
            bound = parameters.bind(**arguments)
            bound.apply_defaults()
            return {key: value for key, value in bound.arguments.items() if value is not Ellipsis}
        write = operation not in {"sources", "source"}
        risk = "danger" if operation == "delete_source" else "low_write" if operation == "scan_sources" else "write" if write else "read"
        tools.append(KernelToolSpec(name="local." + operation, domain="local_media", effect=ToolEffect.DANGER if operation == "delete_source" else ToolEffect.WRITE if write else ToolEffect.READ,
            description=method.__doc__ or "", input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            validator=normalize, read=None if write else lambda arguments, context, function=method: function(**arguments),
            prepare=partial(prepare, operation=operation) if write else None, execute_confirmed=partial(execute, operation=operation) if write else None,
            verify=verify if operation == "scan_sources" else None,
            metadata={"risk": risk, "source_kind": "native_local_source",
                      "related_tools": ("local.sources", "local.source") if write else ()}))
    return tuple(tools)


def playback_tools() -> tuple[KernelToolSpec, ...]:
    from contextlib import contextmanager
    from inspect import signature
    from app.modules import media_proxy as service
    from app.sdk import playback

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = service.prepare_playback_proxy_change(operation, **arguments)
                fingerprint = prepared.pop("fingerprint")
                preview = ToolResult(**prepared)
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + fingerprint,
                    metadata={"risk": "low_write", "source_kind": "native_playback_proxy", "proxy_fingerprint": fingerprint,
                              "confirmation": build_confirmation_contract(tool_name="playback." + operation,
                                  risk=RiskLevel.LOW_WRITE, preview=preview)})
        except service.PlaybackProxyError as exc:
            raise ToolPipelineError(str(exc), code=exc.code) from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        started = False

        @contextmanager
        def admission():
            nonlocal started
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "playback." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if prefix + active.metadata["proxy_fingerprint"] != expected_snapshot:
                    raise ToolPipelineError("确认时运行配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                yield

        try:
            active = context.active_effect_plan
            if active is None:
                raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
            return ToolResult(**service.apply_playback_proxy_change(operation, **arguments,
                expected_fingerprint=active.metadata["proxy_fingerprint"], cancelled=lambda: context.cancellation.cancelled,
                admission=admission))
        except service.PlaybackProxyError as exc:
            error = ToolPipelineError(str(exc), code=exc.code)
            error.mark_execution_started(False)
            raise error from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except Exception as exc:
            error = ToolPipelineError("媒体反代操作结果无法可靠确认，请读取实例状态，勿直接重投", code="unavailable")
            error.mark_execution_started(started)
            raise error from exc

    tools = []
    for operation, method, properties, required in (
        ("instances", playback.instances, {}, []),
        ("failures", playback.failures, {"hours": {"type": "integer", "enum": [1, 6, 24, 72]},
            "instance_id": {"type": ["integer", "null"], "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}, []),
        ("test", playback.test, {"instance_id": {"type": "integer", "minimum": 1}}, ["instance_id"]),
        ("set_enabled", playback.set_enabled, {"instance_id": {"type": "integer", "minimum": 1}, "enabled": {"type": "boolean"}}, ["instance_id", "enabled"]),
        ("restart", playback.restart, {"instance_id": {"type": "integer", "minimum": 1}}, ["instance_id"]),
    ):
        write = operation in {"set_enabled", "restart"}
        tools.append(KernelToolSpec(name="playback." + operation, domain="playback", effect=ToolEffect.WRITE if write else ToolEffect.READ,
            description=method.__doc__ or "", input_schema={"type": "object", "properties": properties,
                "required": required, "additionalProperties": False},
            validator=lambda arguments, parameters=signature(method): dict(parameters.bind(**arguments).arguments),
            read=None if write else lambda arguments, context, function=method: function(**arguments),
            prepare=partial(prepare, operation=operation) if write else None,
            execute_confirmed=partial(execute, operation=operation) if write else None,
            metadata={"risk": "low_write" if write else "read", "source_kind": "native_playback_proxy"}))
    return tuple(tools)


def strm_metadata_tools() -> tuple[KernelToolSpec, ...]:
    """伴随元数据控制只冻结公共service凭证，沿用原EffectExecutor确认。"""
    from inspect import signature
    from app.modules import strm_metadata_management as service
    from app.sdk import strm

    def failure(exc, *, confirmed=False, started=False):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, ValueError):
            error = ToolPipelineError(str(exc), code="confirmation_stale" if confirmed else "precondition_failed")
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
        else:
            error = ToolPipelineError("伴随元数据操作未能可靠完成，请读取状态核实", code="unavailable")
        if confirmed:
            error.mark_execution_started(started and not isinstance(exc, (ValueError, ToolPipelineError, AgentRuntimeDisabled, RuntimeFenceError)))
        return error

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                data, token = (service.prepare_policy(arguments["enabled"], context.owner)
                    if operation == "set_metadata_enabled" else service.prepare_backlog_cancel(context.owner))
                preview = ToolResult(True, "confirmation_required",
                    "确认调整伴随元数据同步开关，保留队列和文件" if operation == "set_metadata_enabled"
                    else f"确认取消{data['count']}项伴随元数据待办，不删除文件", data=data)
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + token,
                    metadata={"risk": "write", "source_kind": "native_strm_metadata",
                        "confirmation": build_confirmation_contract(tool_name="strm." + operation, risk=RiskLevel.WRITE, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "strm." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                data = (service.set_policy_confirmed(expected_snapshot.removeprefix(prefix), active.owner)
                    if operation == "set_metadata_enabled" else service.cancel_backlog_confirmed(expected_snapshot.removeprefix(prefix), active.owner))
        except Exception as exc:
            raise failure(exc, confirmed=True, started=started) from exc
        verified = bool(data.get("verified", True))
        return ToolResult(verified, "completed" if verified else "outcome_unknown", str(data["summary"]), data=data)

    return tuple(KernelToolSpec(name="strm." + operation, domain="strm", effect=ToolEffect.WRITE,
        description=method.__doc__ or "", input_schema=schema,
        validator=lambda arguments, parameters=signature(method): dict(parameters.bind(**arguments).arguments),
        prepare=partial(prepare, operation=operation), execute_confirmed=partial(execute, operation=operation),
        metadata={"risk": "write", "source_kind": "native_strm_metadata"})
        for operation, method, schema in (
            ("set_metadata_enabled", strm.set_metadata_enabled, {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False}),
            ("cancel_metadata_pending", strm.cancel_metadata_pending, {"type": "object", "properties": {}, "additionalProperties": False}),
        ))


def strm_failure_tools() -> tuple[KernelToolSpec, KernelToolSpec]:
    from app.modules import strm as service
    from app.sdk import strm

    def prepare(arguments, context):
        with confirmation_runtime(context) as prefix:
            context.cancellation.raise_if_cancelled()
            prepared = service.prepare_strm_failure_retry(**arguments)
            fingerprint = prepared.pop("fingerprint")
            if prepared["status"] == "no_changes":
                return DefaultProjector().project(ToolResult(**prepared))
            if not prepared["ok"]:
                raise ToolPipelineError(prepared["error"], code=prepared["status"])
            preview = ToolResult(**prepared)
            return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + fingerprint,
                metadata={"risk": "danger", "source_kind": "native_strm_failures",
                          "confirmation": build_confirmation_contract(
                              tool_name="strm.retry_failures", risk=RiskLevel.DANGER, preview=preview)})

    def execute(arguments, expected_snapshot, context):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "strm.retry_failures"
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                return ToolResult(**service.retry_selected_strm_failures(
                    **arguments, expected_fingerprint=expected_snapshot.removeprefix(prefix),
                    should_stop=lambda: context.cancellation.cancelled))
        except service.StrmFailureRetryStale as exc:
            error = ToolPipelineError(str(exc), code="confirmation_stale")
            error.mark_execution_started(False)
            raise error from exc
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except Exception as exc:
            error = ToolPipelineError("STRM 重试结果无法可靠确认，请读取失败账本核实，不要直接重投", code="unavailable")
            error.mark_execution_started(started)
            raise error from exc

    return (
        KernelToolSpec(name="strm.failures", domain="strm", effect=ToolEffect.READ,
            description=strm.failures.__doc__ or "",
            input_schema={"type": "object", "additionalProperties": False, "properties": {
                "status": {"type": "string", "enum": ["all", "open", "retrying", "resolved"]},
                "action": {"type": "string", "enum": ["", "generate", "metadata"]},
                "source_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "offset": {"type": "integer", "minimum": 0}}},
            read=lambda arguments, context: strm.failures(**arguments)),
        KernelToolSpec(name="strm.retry_failures", domain="strm", effect=ToolEffect.DANGER,
            description=strm.retry_failures.__doc__ or "",
            input_schema={"type": "object", "additionalProperties": False, "required": ["ids"],
                          "properties": {"ids": {"type": "array", "items": {"type": "integer", "minimum": 1}}}},
            validator=lambda arguments: {"ids": service.normalize_strm_failure_ids(arguments["ids"])},
            prepare=prepare, execute_confirmed=execute,
            metadata={"risk": "danger", "source_kind": "native_strm_failures"}),
    )


def strm_run_tool() -> KernelToolSpec:
    """冻结/提交均走公共scheduler；这里只保留授权、原确认与完成回执。"""
    from inspect import signature
    from app.modules import scheduler as service
    from app.sdk import strm

    def failure(exc, *, confirmed=False, started=False):
        if isinstance(exc, ToolPipelineError):
            error = exc
        elif isinstance(exc, ValueError):
            error = ToolPipelineError(str(exc), code="confirmation_stale" if confirmed else "precondition_failed")
        elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
            error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
        else:
            error = ToolPipelineError("STRM同步操作未能可靠完成，请读取原任务状态核实", code="unavailable")
        if confirmed:
            error.mark_execution_started(started and not isinstance(exc, (ValueError, ToolPipelineError, AgentRuntimeDisabled, RuntimeFenceError)))
        return error

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = service.prepare_strm_run(**arguments)
                fingerprint = prepared.pop("fingerprint")
                if not prepared["ok"]:
                    raise ToolPipelineError(prepared["error"], code=prepared["status"])
                preview = ToolResult(**prepared)
                preview.data.update(count=preview.data["source_count"], resources=[
                    {"title": f"同步来源 {item['name']}（{item['id']}）"} for item in preview.data["sources"]])
                return PreparedEffect(preview=preview.to_dict(), snapshot_fingerprint=prefix + fingerprint,
                    metadata={"risk": "danger", "source_kind": "native_strm_run",
                              "confirmation": build_confirmation_contract(tool_name="strm.run", risk=RiskLevel.DANGER, preview=preview)})
        except Exception as exc:
            raise failure(exc) from exc

    def execute(arguments, expected_snapshot, context):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "strm.run"
                        or dict(active.arguments) != arguments or context.completion_scope is None):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                result = ToolResult(**service.submit_strm_run(**arguments, expected_fingerprint=expected_snapshot.removeprefix(prefix)))
                if result.ok:
                    result.effect_metadata["completion"] = {"kind": "strm_run", "run_id": result.data["run_id"], "operation": "run"}
                return result
        except Exception as exc:
            raise failure(exc, confirmed=True, started=started) from exc

    async def verify(arguments, result, context):
        if context.wait_for_completion:
            return await effect_completion.wait_for_effect_completion(result, tool="strm.run",
                context=ToolContext(owner=context.owner, session_id=context.session_id, request_id=context.request_id,
                                    cancelled=lambda: context.cancellation.cancelled),
                report_progress=context.report_progress, scope=context.completion_scope)
        tracker = effect_completion.observers._completion_tracker(result)
        if result.ok and tracker is not None:
            await effect_completion._register_effect_wait(result, tracker, "strm.run", context.completion_scope)
        return result

    return KernelToolSpec(name="strm.run", domain="strm", effect=ToolEffect.DANGER,
        description=strm.run.__doc__ or "", input_schema={"type": "object", "additionalProperties": False,
            "properties": {"source_ids": {"type": ["array", "null"], "items": {"type": "string", "minLength": 1}}}},
        validator=lambda arguments: dict(signature(strm.run).bind(**arguments).arguments),
        prepare=prepare, execute_confirmed=execute, verify=verify,
        metadata={"risk": "danger", "source_kind": "native_strm_run"})

def indexer_configuration_tool() -> KernelToolSpec:
    from app.indexers import config as service
    from app.sdk import resources
    name = "resources.configure_sites"

    def validate(arguments):
        try:
            return service.normalize_site_selection(**arguments)
        except (TypeError, ValueError) as exc:
            raise ToolPipelineError(str(exc), code="invalid_arguments") from exc

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                value, fingerprint = service.prepare_site_configuration(**arguments)
                result = ToolResult(**value)
                if not result.ok:
                    raise ToolPipelineError(result.error or result.summary, code=result.status)
                if result.status == "no_changes":
                    return DefaultProjector().project(result)
                return PreparedEffect(preview=result.to_dict(), snapshot_fingerprint=prefix + fingerprint,
                    metadata={"risk": "low_write", "site_fingerprint": fingerprint, "confirmation": build_confirmation_contract(
                        tool_name=name, risk=RiskLevel.LOW_WRITE, preview=result)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context):
        from contextlib import contextmanager
        started = False
        active = context.active_effect_plan
        fingerprint = active.metadata.get("site_fingerprint") if active else None

        @contextmanager
        def admission():
            nonlocal started
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.session_id != context.session_id
                        or active.tool_name != name or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not isinstance(fingerprint, str) or prefix + fingerprint != expected_snapshot:
                    raise ToolPipelineError("运行配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                yield

        try:
            # publication holds authorization; worker shutdown must run after its locks are released.
            return ToolResult(**service.apply_site_configuration(**arguments,
                expected_fingerprint=str(fingerprint or ""), admission=admission))
        except Exception as exc:
            if started and not isinstance(exc, service.SiteConfigurationChanged):
                return ToolResult(False, "outcome_unknown", "站点保存结果待核验",
                    data={"execution_started": True}, error="请先读取当前站点配置核实，勿重复提交。")
            if isinstance(exc, service.SiteConfigurationChanged):
                error = ToolPipelineError(str(exc), code="confirmation_stale")
            elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
                error = ToolPipelineError("Media Agent授权或运行状态已变化", code="agent_disabled")
            elif isinstance(exc, ToolPipelineError):
                error = exc
            else:
                error = ToolPipelineError("站点配置修改未开始，请重新预检", code="unavailable")
            error.mark_execution_started(False)
            raise error from exc

    return KernelToolSpec(name=name, domain="resources", effect=ToolEffect.WRITE,
        description=resources.configure_sites.__doc__ or "", validator=validate,
        input_schema={"type": "object", "required": ["site_ids"], "additionalProperties": False,
                      "properties": {"site_ids": {"type": "array", "minItems": 1,
                          "maxItems": len(service.MANUAL_INDEXER_SITE_ORDER), "uniqueItems": True,
                          "items": {"type": "string", "enum": list(service.MANUAL_INDEXER_SITE_ORDER)}},
                          "enable_search": {"type": ["boolean", "null"], "default": None}}},
        prepare=prepare, execute_confirmed=execute, metadata={"risk": "low_write"})


def schedule_policy_tools() -> tuple[KernelToolSpec, ...]:
    """三项策略共用原确认执行器；快照、CAS及调度刷新属于公共服务。"""
    from app.agent.models import Evidence
    from app.modules import schedule_policy as service
    from app.sdk import METHODS

    def result(value: dict[str, Any]) -> ToolResult:
        return ToolResult(
            ok=value["ok"],
            status=value["status"],
            summary=value["summary"],
            data=value["data"],
            suggestions=value["suggestions"],
            error=value["error"],
            evidence=[Evidence(**item) for item in value["evidence"]],
        )

    def prepare(arguments, context, *, kind, name):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                value, fingerprint = service.prepare_policy_change(kind, arguments)
                preview = result(value)
                if not preview.ok:
                    raise ToolPipelineError(preview.error or preview.summary, code=preview.status)
                if preview.status == "no_changes":
                    return DefaultProjector().project(preview)
                return PreparedEffect(
                    preview=preview.to_dict(),
                    snapshot_fingerprint=prefix + fingerprint,
                    metadata={
                        "risk": "low_write",
                        "source_kind": "schedule_policy",
                        "confirmation": build_confirmation_contract(
                            tool_name=name, risk=RiskLevel.LOW_WRITE, preview=preview
                        ),
                    },
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc

    def execute(arguments, expected_snapshot, context, *, kind, name):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (
                    active is None
                    or active.owner != context.owner
                    or active.tool_name != name
                    or dict(active.arguments) != arguments
                ):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                return result(
                    service.apply_policy_change(
                        kind, arguments, expected_fingerprint=expected_snapshot.removeprefix(prefix)
                    )
                )
        except Exception as exc:
            # 只有输入/快照拒绝能够证明未发布；提交后的回读、刷新上下文等异常保留未知回执。
            if started and not isinstance(exc, service.PolicySnapshotChanged):
                return ToolResult(False, "outcome_unknown", "策略保存结果待核验",
                    data={"execution_started": True}, error="请先读取当前策略核实，勿重复提交。")
            if isinstance(exc, service.PolicySnapshotChanged):
                error = ToolPipelineError(str(exc), code="confirmation_stale")
            elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
                error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            elif isinstance(exc, ToolPipelineError):
                error = exc
            else:
                error = ToolPipelineError("策略操作未能开始，请重新预检", code="unavailable")
            error.mark_execution_started(False)
            raise error from exc

    def validate(arguments, *, kind):
        try:
            return service.normalize_changes(kind, arguments)
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="invalid_arguments") from exc

    tools = []
    for kind, name in (
        ("strm", "strm.set_schedule"),
        ("organize", "cloud.set_organize_schedule"),
        ("patrol", "library.set_patrol_policy"),
    ):
        properties: dict[str, dict[str, Any]] = {"enabled": {"type": "boolean"}, "notify_enabled": {"type": "boolean"}}
        properties.update(
            {
                "interval_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
            }
            if kind == "patrol"
            else {"cron": {"type": "string", "minLength": 1, "maxLength": 128}}
        )
        tools.append(
            KernelToolSpec(
                name=name,
                domain="library" if kind == "patrol" else "strm" if kind == "strm" else "cloud",
                effect=ToolEffect.WRITE,
                description=METHODS[name].__doc__ or "",
                input_schema={
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": False,
                    "properties": properties,
                },
                validator=partial(validate, kind=kind),
                prepare=partial(prepare, kind=kind, name=name),
                execute_confirmed=partial(execute, kind=kind, name=name),
                metadata={"risk": "low_write", "source_kind": "schedule_policy"},
            )
        )
    return tuple(tools)


def _release_format_text(value: object, fallback: str, limit: int = 240) -> str:
    return sanitize_public_text(value, limit=limit) or fallback


def _release_format_resource(row: dict[str, Any]) -> dict[str, Any]:
    def point(value: dict[str, Any]) -> tuple[dict[str, Any], str]:
        title = sanitize_resource_title(value["title"], limit=180) or "未识别标题"
        season, episode = value["season"], value["episode"]
        if season is not None and episode is not None:
            label = f"第{season}季第{episode}集"
        elif episode is not None:
            label = f"第{episode}集"
        elif season is not None:
            label = f"第{season}季，集号未识别"
        else:
            label = "季集未识别"
        return {"title": title, "season": season, "episode": episode}, label

    filename = sanitize_resource_title(row["filename"], limit=255) or "未命名样本文件"
    before, before_label = point(row["before"])
    after, after_label = point(row["after"])
    return {
        # 各字段已经净化且有界；不再用工具名过滤器重扫文件名，或截掉末尾的集号。
        "title": (f"季集 {before_label} → {after_label}；标题《{before['title']}》→《{after['title']}》；"
                  f"样本文件「{filename}」"),
        "filename": filename,
        "before": before,
        "after": after,
        "status": _release_format_text(row["status"], "未确定", 40),
        "passed": bool(row["passed"]),
    }


def _release_format_confirmation(preview: dict[str, Any]) -> ToolResult:
    summary, examples = preview["summary"], preview["examples"]
    batch_total, sample_count = summary["total"], len(examples)
    display_total = batch_total or sample_count
    matched, unmatched = summary["matched"], summary["unmatched"]
    blocked, conflicts = summary["blocked"], summary["conflicts"]
    review_required = unmatched + blocked + conflicts
    scope = "仅此目录" if preview["draft"]["scope"] == "directory" else "跨目录发布范围"
    batch_effect = (f"批量核对 {batch_total} 个文件：命中 {matched} 个，未匹配 {unmatched} 个，特别篇/受保护 {blocked} 个，格式冲突 {conflicts} 个；这些文件仍走原有识别流程。"
                    if batch_total else
                    f"已核对 {sample_count} 个标注样本；未提供额外批量文件，特别篇/受保护 {blocked} 个、格式冲突 {conflicts} 个仍走原有识别流程。")
    data = {
        "resources": [_release_format_resource(row) for row in examples[:8]],
        "effects": [f"适用范围：{scope}；只影响以后识别。", batch_effect, "不移动文件、不绑定 TMDB、不偏移季集编号。"],
        "total": display_total, "count": display_total, "batch_total": batch_total,
        "sample_count": sample_count, "matched": matched, "unmatched": unmatched,
        "review_required": review_required, "can_save": preview["can_save"],
        "summary": {**summary, "total": display_total, "batch_total": batch_total, "sample_count": sample_count},
    }
    text = (f"确认后保存发布格式教学规则，批量核对 {batch_total} 个文件"
            if batch_total else f"确认后保存发布格式教学规则，已核对 {sample_count} 个标注样本")
    return ToolResult(True, "confirmation_required", text, data=data,
                      suggestions=["请核对样本前后标题与季集；确认后才会保存规则。"])


def media_path_mapping_tools() -> tuple[KernelToolSpec, ...]:
    """原生路径映射沿公共服务和既有确认账本，文件CAS及锁序只有一套。"""
    from app import sdk
    from app.modules import media_server_path_mapping as service

    def prepare(arguments, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                values = sdk.prepare_sdk_method(name, arguments, owner=context.owner)
                prepared = service.prepare_path_mapping_change(operation, **values)
                preview = ToolResult(True, "confirmation_required", "确认修改媒体库路径映射", data={
                    "operation": operation, "server": values["server"], "before": prepared["before"],
                    "after": prepared["after"], "effects": ["只修改指定服务器的路径映射，不移动文件、刷新媒体库或修改本地归档绑定。"]})
                return PreparedEffect(preview=preview.to_dict(), arguments=values,
                    snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "write", "source_kind": "media_path_mapping",
                              "confirmation": build_confirmation_contract(tool_name=name, risk=RiskLevel.WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context, *, operation, name):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, name) or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                return ToolResult(**service.apply_path_mapping_change(operation, **arguments,
                    expected_fingerprint=expected_snapshot.removeprefix(prefix),
                    check_cancelled=context.cancellation.raise_if_cancelled))
        except Exception as exc:
            if started and not isinstance(exc, service.MappingSnapshotChanged):
                return ToolResult(False, "outcome_unknown", "路径映射保存结果待核验",
                    data={"execution_started": True}, error="请先读取当前映射核实，不要重复提交。")
            code = ("confirmation_stale" if isinstance(exc, service.MappingSnapshotChanged) else
                    "agent_disabled" if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)) else
                    getattr(exc, "code", "unavailable"))
            error = ToolPipelineError(str(exc), code=code)
            error.mark_execution_started(False)
            raise error from exc

    path = {"type": "string", "minLength": 1, "maxLength": 2048}
    tools = []
    for operation in ("create", "update", "delete"):
        name = f"library.{operation}_path_mapping"
        properties = {"server": {"type": "string", "enum": ["jellyfin", "emby"]}, "local_path": path}
        if operation != "delete":
            properties["server_path"] = path
        if operation == "update":
            properties["new_local_path"] = path
        tools.append(KernelToolSpec(name=name, domain="library", effect=ToolEffect.WRITE,
            description=sdk.METHODS[name].__doc__ or name,
            input_schema={"type": "object", "properties": properties, "additionalProperties": False,
                          "required": ["server", "local_path", *(["server_path"] if operation == "create" else [])]},
            prepare=partial(prepare, operation=operation, name=name),
            execute_confirmed=partial(execute, operation=operation, name=name)))
    return tuple(tools)


def feature_config_tool() -> KernelToolSpec:
    """只将单项配置发布接入确认账本；解析、CAS、回读与 reload 归公共服务。"""
    from app import sdk
    from app.modules import feature_config as service

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                values = sdk.prepare_sdk_method("settings.set_feature", arguments, owner=context.owner)
                prepared = service.prepare_feature(**values)
                preview = ToolResult(**prepared["preview"])
                if not preview.ok:
                    raise ToolPipelineError(preview.summary, code=preview.status)
                if preview.status == "no_changes":
                    return DefaultProjector().project(preview)
                return PreparedEffect(preview=preview.to_dict(), arguments=values,
                    snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "low_write", "feature_fingerprint": prepared["fingerprint"], "confirmation": build_confirmation_contract(
                        tool_name="settings.set_feature", risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (ValueError, TypeError) as exc:
            raise ToolPipelineError("功能名称或状态无效", code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context):
        from contextlib import contextmanager
        started = False
        active = context.active_effect_plan
        fingerprint = active.metadata.get("feature_fingerprint") if active else None

        @contextmanager
        def admission():
            nonlocal started
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, "settings.set_feature") or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not isinstance(fingerprint, str) or prefix + fingerprint != expected_snapshot:
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                yield

        try:
            return ToolResult(**service.set_feature(**arguments, expected_fingerprint=str(fingerprint or ""),
                check_write=context.cancellation.raise_if_cancelled, admission=admission))
        except Exception as exc:
            if started and not isinstance(exc, service.FeatureConflict):
                return ToolResult(False, "outcome_unknown", "功能配置保存结果待核验",
                    data={"execution_started": True}, error="请先读取状态核实，不要重放确认或原脚本。")
            if isinstance(exc, service.FeatureConflict):
                code = "confirmation_stale"
            elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
                code = "agent_disabled"
            elif isinstance(exc, ToolPipelineError):
                code = exc.code
            else:
                code = "precondition_failed"
            error = ToolPipelineError("功能配置或确认范围已变化，请重新读取后预检", code=code)
            error.mark_execution_started(False)
            raise error from exc

    return KernelToolSpec(name="settings.set_feature", domain="config", effect=ToolEffect.WRITE,
        description=sdk.settings.set_feature.__doc__ or "", metadata={"risk": "low_write"},
        input_schema={"type": "object", "additionalProperties": False, "required": ["feature", "enabled"],
                      "properties": {"feature": {"type": "string", "enum": list(service.FEATURES)}, "enabled": {"type": "boolean"}}},
        prepare=prepare, execute_confirmed=execute)


def notification_test_tool() -> KernelToolSpec:
    """固定连接测试通过原确认账本，实际发送复用 Web 公共服务。"""
    from app import sdk
    from app.modules import telegram_connection_test as service

    name = "settings.test_notification"

    def prepare(arguments, context):
        with confirmation_runtime(context) as prefix:
            values = sdk.prepare_sdk_method(name, arguments, owner=context.owner)
            context.cancellation.raise_if_cancelled()
            preview, fingerprint = service.prepare_test()
            if not preview["ok"]:
                raise ToolPipelineError(preview["summary"], code=preview["status"])
            return PreparedEffect(preview=preview, arguments=values, snapshot_fingerprint=prefix + fingerprint,
                metadata={"risk": "low_write", "confirmation": build_confirmation_contract(
                    tool_name=name, risk=RiskLevel.LOW_WRITE, preview=ToolResult(**preview))})

    def execute(arguments, expected_snapshot, context):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, name) or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not expected_snapshot.startswith(prefix):
                    raise ToolPipelineError("通知配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                return ToolResult(**service.send_configured_test(expected_snapshot.removeprefix(prefix),
                    check_cancelled=context.cancellation.raise_if_cancelled))
        except Exception as exc:
            if started and not isinstance(exc, service.NotificationConfigChanged):
                return ToolResult(False, "outcome_unknown", "Telegram 发送结果待核验",
                    data={"execution_started": True}, error="请先查看 Telegram；不要重复确认原计划。")
            error = ToolPipelineError("通知配置或授权已变化，请重新预检", code=(
                "confirmation_stale" if isinstance(exc, service.NotificationConfigChanged) else
                "agent_disabled" if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)) else getattr(exc, "code", "unavailable")))
            error.mark_execution_started(False)
            raise error from exc

    return KernelToolSpec(name=name, domain="config", effect=ToolEffect.WRITE,
        description=sdk.settings.test_notification.__doc__ or "", metadata={"risk": "low_write"},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        prepare=prepare, execute_confirmed=execute)


def policy_config_tool() -> KernelToolSpec:
    """只将单项配置发布接入确认账本；解析、CAS、回读与 reload 归公共服务。"""
    from app import sdk
    from app.modules import policy_config as service

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                values = sdk.prepare_sdk_method("settings.set_policy", arguments, owner=context.owner)
                prepared = service.prepare_policy(**values)
                preview = ToolResult(**prepared["preview"])
                if not preview.ok:
                    raise ToolPipelineError(preview.summary, code=preview.status)
                if preview.status == "no_changes":
                    return DefaultProjector().project(preview)
                return PreparedEffect(preview=preview.to_dict(), arguments=prepared["arguments"],
                    snapshot_fingerprint=prefix + prepared["fingerprint"],
                    metadata={"risk": "low_write", "policy_fingerprint": prepared["fingerprint"], "confirmation": build_confirmation_contract(
                        tool_name="settings.set_policy", risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (ValueError, TypeError) as exc:
            raise ToolPipelineError("策略名称或状态无效", code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context):
        from contextlib import contextmanager
        started = False
        active = context.active_effect_plan
        fingerprint = active.metadata.get("policy_fingerprint") if active else None

        @contextmanager
        def admission():
            nonlocal started
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, "settings.set_policy") or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                if not isinstance(fingerprint, str) or prefix + fingerprint != expected_snapshot:
                    raise ToolPipelineError("确认时的配置已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                started = True
                yield

        try:
            return ToolResult(**service.set_policy(**arguments, expected_fingerprint=str(fingerprint or ""),
                check_write=context.cancellation.raise_if_cancelled, admission=admission))
        except Exception as exc:
            if started and not isinstance(exc, service.PolicyConflict):
                return ToolResult(False, "outcome_unknown", "策略配置保存结果待核验",
                    data={"execution_started": True}, error="请先读取状态核实，不要重放确认或原脚本。")
            if isinstance(exc, service.PolicyConflict):
                code = "confirmation_stale"
            elif isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)):
                code = "agent_disabled"
            elif isinstance(exc, ToolPipelineError):
                code = exc.code
            else:
                code = "precondition_failed"
            error = ToolPipelineError("策略配置或确认范围已变化，请重新读取后预检", code=code)
            error.mark_execution_started(False)
            raise error from exc

    return KernelToolSpec(name="settings.set_policy", domain="config", effect=ToolEffect.WRITE,
        description=sdk.settings.set_policy.__doc__ or "", metadata={"risk": "low_write"},
        input_schema={"type": "object", "additionalProperties": False, "required": ["policy", "value"],
                      "properties": {"policy": {"type": "string", "enum": list(service.POLICY_IDS)},
                                     "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}},
        prepare=prepare, execute_confirmed=execute)


def recognition_rule_tool() -> KernelToolSpec:
    """三类规则仅沿公共更新入口；知识启停归现有 update_knowledge。"""
    from app import sdk
    from app.modules import recognition_service as service

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                values = sdk.prepare_sdk_method("recognition.set_rule_enabled", arguments, owner=context.owner)
                prepared = service.prepare_rule_enabled(**values)
                preview = ToolResult(**prepared["preview"])
                return PreparedEffect(preview=prepared["preview"], arguments=values,
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "low_write", "rule_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(tool_name="recognition.set_rule_enabled",
                                  risk=RiskLevel.LOW_WRITE, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context):
        started = False
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, "recognition.set_rule_enabled") or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("rule_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                with db.get_conn() as conn:
                    fence = read_runtime_fence(conn)

                def check_write(conn):
                    context.cancellation.raise_if_cancelled()
                    require_runtime_fence(conn, fence)

                context.cancellation.raise_if_cancelled()
                started = True
                return ToolResult(**service.set_rule_enabled(**arguments, expected_snapshot=snapshot, check_write=check_write))
        except (AgentRuntimeDisabled, RuntimeFenceError, service.RuleConflict, ToolPipelineError) as exc:
            code = ("conflict" if isinstance(exc, service.RuleConflict) else
                    "agent_disabled" if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)) else exc.code)
            error = ToolPipelineError(str(exc), code=code)
            error.mark_execution_started(False)
            raise error from exc
        except Exception as exc:
            if started:
                return ToolResult(False, "outcome_unknown", "识别规则启停结果待核验",
                    data={"execution_started": True}, error="请先读取当前规则核实，不要重放确认或原脚本。")
            raise ToolPipelineError("规则启停未能开始", code="precondition_failed") from exc

    return KernelToolSpec(name="recognition.set_rule_enabled", domain="recognition", effect=ToolEffect.WRITE,
        description=sdk.recognition.set_rule_enabled.__doc__ or "", metadata={"risk": "low_write"},
        input_schema={"type": "object", "additionalProperties": False, "required": ["rule_type", "rule_id", "enabled"],
                      "properties": {"rule_type": {"type": "string", "enum": ["preprocess_rule", "tmdb_regex_rule", "release_format"]},
                                     "rule_id": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}, "enabled": {"type": "boolean"}}},
        prepare=prepare, execute_confirmed=execute)


def recognition_knowledge_tools() -> tuple[KernelToolSpec, ...]:
    """知识 CRUD 接入唯一确认账本；公共模块拥有预检、事务 CAS 和缓存失效。"""
    from app import sdk
    from app.modules import recognition_knowledge as knowledge

    def prepare(arguments, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                values = sdk.prepare_sdk_method(name, arguments, owner=context.owner)
                fields = {key: value for key, value in values.items() if key != "entry_id"}
                if operation == "update" and not fields:
                    raise ValueError("至少提供一个需要修改的识别知识字段")
                prepared = knowledge.prepare_entry(operation, fields, entry_id=values.get("entry_id"))
                risk = RiskLevel.DANGER if operation == "delete" else RiskLevel.WRITE
                preview = ToolResult(True, "confirmation_required", "确认识别知识变更", data={
                    "operation": operation, "before": prepared["before"], "changes": fields,
                    "effects": ["只修改识别知识，不扫描、移动或重命名媒体文件。"]})
                return PreparedEffect(preview=preview.to_dict(), arguments=values,
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": risk.value, "knowledge_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(tool_name=name, risk=risk, preview=preview)})
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except (ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context, *, operation, name):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or (active.owner, active.session_id, active.tool_name) !=
                        (context.owner, context.session_id, name) or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("knowledge_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                with db.get_conn() as conn:
                    fence = read_runtime_fence(conn)

                def check_write(conn):
                    context.cancellation.raise_if_cancelled()
                    require_runtime_fence(conn, fence)

                fields = {key: value for key, value in arguments.items() if key != "entry_id"}
                if operation == "create":
                    row = knowledge.create_entry(fields, check_write=check_write)
                elif operation == "update":
                    row = knowledge.update_entry(arguments["entry_id"], fields, expected_snapshot=snapshot, check_write=check_write)
                else:
                    knowledge.delete_entry(arguments["entry_id"], expected_snapshot=snapshot, check_write=check_write)
                    row = None
                ok = bool(row) if operation != "delete" else knowledge.get_entry(arguments["entry_id"]) is None
                return ToolResult(ok, "completed" if ok else "partial",
                    "识别知识已更新并回读核对" if ok else "识别知识写后核对失败", data={"operation": operation, "item": row})
        except (AgentRuntimeDisabled, RuntimeFenceError, ValueError, ToolPipelineError) as exc:
            code = ("agent_disabled" if isinstance(exc, (AgentRuntimeDisabled, RuntimeFenceError)) else
                    "confirmation_stale" if isinstance(exc, knowledge.KnowledgeConflict) else
                    getattr(exc, "code", "precondition_failed"))
            error = ToolPipelineError(str(exc), code=code)
            error.mark_execution_started(False)
            raise error from exc

    fields = {"knowledge_type": {"type": "string", "enum": ["release_group", "release_suffix"]},
              "canonical_value": {"type": "string", "minLength": 1, "maxLength": 160},
              "aliases": {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 24},
              "disabled": {"type": "boolean"}}
    tools = []
    for operation in ("create", "update", "delete"):
        name = f"recognition.{operation}_knowledge"
        properties = {} if operation == "delete" else dict(fields)
        if operation != "create":
            properties["entry_id"] = {"type": "integer", "minimum": 1}
        tools.append(KernelToolSpec(name=name, domain="recognition",
            effect=ToolEffect.DANGER if operation == "delete" else ToolEffect.WRITE,
            description=sdk.METHODS[name].__doc__ or name,
            input_schema={"type": "object", "properties": properties, "additionalProperties": False,
                          "required": ["knowledge_type", "canonical_value"] if operation == "create" else ["entry_id"]},
            prepare=partial(prepare, operation=operation, name=name),
            execute_confirmed=partial(execute, operation=operation, name=name)))
    return tuple(tools)


def release_format_save_tool() -> KernelToolSpec:
    """原生 recognition 保存只沿已有确认执行器和 Web 格式事务。"""
    from app.modules import recognition_service
    from app.modules.recognition import formats
    from app.sdk import recognition

    def prepare(arguments, context):
        try:
            with confirmation_runtime(context) as prefix:
                context.cancellation.raise_if_cancelled()
                prepared = recognition_service.prepare_release_format(arguments)
                preview = _release_format_confirmation(prepared["preview"])
                return PreparedEffect(
                    preview=preview.to_dict(),
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "write", "release_format_snapshot": prepared["snapshot"],
                              "confirmation": build_confirmation_contract(
                                  tool_name="recognition.save_release_format", risk=RiskLevel.WRITE, preview=preview)},
                )
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            raise ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled") from exc
        except ValueError as exc:
            raise ToolPipelineError(_release_format_text(str(exc), "发布格式预览未通过"), code="precondition_failed") from exc

    def execute(arguments, expected_snapshot, context):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner
                        or active.tool_name != "recognition.save_release_format" or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原确认执行范围", code="confirmation_invalid")
                snapshot = active.metadata.get("release_format_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("确认时的配置或快照已变化，请重新预检", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                return ToolResult(**recognition_service.save_release_format(arguments, expected_snapshot=snapshot))
        except (AgentRuntimeDisabled, RuntimeFenceError) as exc:
            error = ToolPipelineError("Media Agent 授权或运行状态已变化", code="agent_disabled")
            error.mark_execution_started(False)
            raise error from exc
        except formats.FormatConflict as exc:
            error = ToolPipelineError(_release_format_text(str(exc), "发布格式预览已失效，请重新预览"), code="confirmation_stale")
            error.mark_execution_started(False)
            raise error from exc
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        except ValueError as exc:
            error = ToolPipelineError(_release_format_text(str(exc), "发布格式确认无法执行"), code="precondition_failed")
            error.mark_execution_started(False)
            raise error from exc

    return KernelToolSpec(
        name="recognition.save_release_format", domain="recognition", effect=ToolEffect.WRITE,
        description=recognition.save_release_format.__doc__ or "", metadata={"risk": "write"},
        input_schema={
            "type": "object",
            "properties": {
                "draft": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 120},
                        "template": {
                            "type": "string", "maxLength": 512,
                            "description": "由你生成而非要求用户填写。仅{title}/{episode}必填，{season}/{version}/{resolution}/{checksum}可选，其余字面匹配；保留分隔符和视频扩展名。",
                        },
                        "scope": {"type": "string", "enum": ["directory", "release"]},
                        "parent_path": {
                            "type": "string", "maxLength": 4096,
                            "description": "目录范围使用用户明确提供或工具核实的实际父目录上下文。目录整理按“整理起点目录名/起点下的相对父目录”匹配，不能用来源显示别名或直接照抄磁盘绝对路径；先确认本次整理起点，或在目录教学页选择后自动带入。保留已有规则原值，不猜测、不自动改写历史范围；跨作品release时填空字符串。",
                        },
                    },
                    "required": ["name", "template", "scope", "parent_path"],
                    "additionalProperties": False,
                },
                "examples": {
                    "type": "array", "minItems": 2, "maxItems": 8,
                    "description": "用户明确标注的不同文件且至少两个不同原始集号。标题是原文件中的片名，不是译名；不能编造样本或把偏移后的编号当原集号。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string", "maxLength": 1024},
                            "title": {"type": "string", "minLength": 1, "maxLength": 180},
                            "episode": {"type": "integer", "minimum": 1, "maximum": 9999},
                            "season": {"type": "integer", "minimum": 1, "maximum": 99},
                        },
                        "required": ["filename", "title", "episode"],
                        "additionalProperties": False,
                    },
                },
                "filenames": {
                    "type": ["array", "null"], "maxItems": 100,
                    "items": {"type": "string", "maxLength": 1024},
                    "description": "需要批量核对的真实文件名，不读文件内容；没有额外文件时为空数组。",
                },
            },
            "required": ["draft", "examples"],
            "additionalProperties": False,
        },
        validator=recognition_service.teaching_arguments, prepare=prepare, execute_confirmed=execute,
    )


def rss_configuration_tools() -> tuple[KernelToolSpec, ...]:
    from app import sdk
    from app.modules import rss_subscriptions as service

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                values = sdk.prepare_sdk_method("rss." + operation, arguments, owner=context.owner)
                context.cancellation.raise_if_cancelled()
                prepared = service.prepare_change(operation, values)
                return PreparedEffect(preview=prepared["preview"], arguments=values,
                    snapshot_fingerprint=prefix + _fingerprint(prepared["snapshot"]),
                    metadata={"risk": "danger" if operation == "delete" else "low_write",
                              "rss_snapshot": prepared["snapshot"]})
        except (AgentRuntimeDisabled, RuntimeFenceError, service.RSSSubscriptionError, ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments")) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "rss." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原RSS确认范围", code="confirmation_invalid")
                snapshot = active.metadata.get("rss_snapshot")
                if not isinstance(snapshot, dict) or prefix + _fingerprint(snapshot) != expected_snapshot:
                    raise ToolPipelineError("RSS配置确认已失效", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                return ToolResult(**service.apply_change(operation, arguments, expected_snapshot=snapshot,
                    check_cancelled=context.cancellation.raise_if_cancelled))
        except (AgentRuntimeDisabled, RuntimeFenceError, service.RSSSubscriptionError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments"))
            error.mark_execution_started(False)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("RSS写入结果无法确认，请读取配置核实，不能直接重放", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error from exc

    properties: dict[str, Any] = {key: {"type": "string"} for key in ("name", "exclude_keywords", "action", "download_method",
        "media_tmdb_id", "parser", "refresh_cron", "qb_save_path", "gy_target_dir", "gy_target_dir_name")}
    properties.update(urls={"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        enabled={"type": "boolean"}, skip_existing_episodes={"type": "boolean"},
        refresh_interval_minutes={"type": "integer"}, media_default_season={"type": "integer"})
    identity = {"subscription_id": {"type": "integer", "minimum": 1, "maximum": 2147483647}}
    return tuple(KernelToolSpec(name="rss." + operation, domain="subscriptions", effect=ToolEffect.WRITE,
        description=sdk.METHODS["rss." + operation].__doc__ or "", metadata={"risk": "danger" if operation == "delete" else "low_write"},
        input_schema={"type": "object", "properties": properties if operation == "create" else identity if operation == "delete" else {**properties, **identity},
            "required": ["name", "urls"] if operation == "create" else ["subscription_id"], "additionalProperties": False},
        prepare=partial(prepare, operation=operation), execute_confirmed=partial(execute, operation=operation))
        for operation in ("create", "update", "delete"))


def rss_delivery_tools() -> tuple[KernelToolSpec, ...]:
    from app import sdk
    from app.modules import rss_delivery as service

    def prepare(arguments, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                values = service.normalize(operation, sdk.prepare_sdk_method("rss." + operation, arguments, owner=context.owner))
                context.cancellation.raise_if_cancelled()
                preview, fingerprint = service.prepare(operation, values)
                if not preview["ok"]:
                    raise ToolPipelineError(preview.get("error") or preview["summary"], code=preview["status"])
                if preview["status"] == "no_changes":
                    return DefaultProjector().project(ToolResult(**preview))
                return PreparedEffect(preview=preview, arguments=values,
                    snapshot_fingerprint=prefix + fingerprint,
                    metadata={"risk": "danger" if operation in {"download", "retry"} else "write",
                              "rss_fingerprint": fingerprint,
                              "confirmation": build_confirmation_contract(tool_name="rss." + operation,
                                  risk=RiskLevel.DANGER if operation in {"download", "retry"} else RiskLevel.WRITE,
                                  preview=ToolResult(**preview))})
        except (AgentRuntimeDisabled, RuntimeFenceError, service.RSSSubscriptionError, ValueError, TypeError) as exc:
            raise ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments")) from exc

    def execute(arguments, expected_snapshot, context, *, operation):
        try:
            with confirmation_runtime(context) as prefix:
                active = context.active_effect_plan
                if (active is None or active.owner != context.owner or active.tool_name != "rss." + operation
                        or dict(active.arguments) != arguments):
                    raise ToolPipelineError("缺少原RSS确认范围", code="confirmation_invalid")
                snapshot = active.metadata.get("rss_fingerprint")
                if not isinstance(snapshot, str) or prefix + snapshot != expected_snapshot:
                    raise ToolPipelineError("RSS投递确认已失效", code="confirmation_stale")
                context.cancellation.raise_if_cancelled()
                return ToolResult(**service.execute(operation, arguments, expected_context=snapshot,
                    check_cancelled=context.cancellation.raise_if_cancelled))
        except (AgentRuntimeDisabled, RuntimeFenceError, service.RSSSubscriptionError, ToolPipelineError) as exc:
            error = ToolPipelineError(str(exc), code=getattr(exc, "code", "invalid_arguments"))
            error.mark_execution_started(False)
            raise error from exc
        except Exception as exc:
            error = ToolPipelineError("RSS投递结果无法确认，请读取条目和回执核实，不能直接重放", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error from exc

    identity = {"type": "integer", "minimum": 1, "maximum": 2147483647}
    ids = {"type": "array", "minItems": 1, "uniqueItems": True, "items": identity}
    optional_ids = {**ids, "type": ["array", "null"]}
    scopes = {"entry_ids": optional_ids, "limit": {"type": ["integer", "null"], "minimum": 1},
              "subscription_id": {**identity, "type": ["integer", "null"]}}
    return tuple(KernelToolSpec(name="rss." + operation, domain="subscriptions", effect=ToolEffect.WRITE,
        description=sdk.METHODS["rss." + operation].__doc__ or "",
        metadata={"risk": "danger" if operation in {"download", "retry"} else "write"},
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        prepare=partial(prepare, operation=operation), execute_confirmed=partial(execute, operation=operation))
        for operation, properties, required in (
            ("mark", {"entry_ids": ids, "processed": {"type": "boolean"}}, ["entry_ids", "processed"]),
            ("refresh", {"subscription_ids": optional_ids, "scope": {"type": ["string", "null"], "enum": ["all_configured", None]}}, []),
            ("download", scopes, []), ("retry", scopes, [])))
