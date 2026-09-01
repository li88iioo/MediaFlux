from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.models import RiskLevel, ToolContext, ToolResult
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_gateway import _PROVIDER_WRITE_LOCK, ProviderGateway
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderOperationSpec,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.registry import AgentToolError
from app.repositories.agent_provider_plans import (
    claim_provider_plan,
    create_provider_plan,
    get_latest_prepared_provider_plan,
    invalidate_provider_plans_for_owner,
)


class _WriteTransport:
    provider = "demo"

    def __init__(self) -> None:
        self.executions: list[tuple[str, str, dict]] = []
        self.expected_profile_revisions: list[str] = []

    def profiles(self):
        return [ProviderProfileView("configured:demo", "demo", "Demo", "online")]

    def profile_revision(self, profile_ref: str) -> str:
        assert profile_ref == "configured:demo"
        return "revision-1"

    def execute_read(self, profile_ref: str, operation: str, arguments: dict):
        return ProviderPayload(
            summary="目标已读取",
            data={
                "items": [
                    {
                        "__object_id": "raw-demo-id",
                        "__object_kind": "demo_item",
                        "name": "演示目标",
                        "path": "/hidden/path",
                    }
                ]
            },
            source="demo_api",
        )

    def preview_write(
        self, profile_ref: str, operation: str, arguments: dict, target_snapshot: dict
    ):
        return ProviderPayload(
            summary="将更新演示目标",
            data={"target": target_snapshot["item_ref"], "mode": "bounded"},
            source="demo_api",
        )

    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ):
        self.expected_profile_revisions.append(expected_profile_revision)
        self.executions.append((profile_ref, operation, dict(arguments)))
        return ProviderPayload(
            summary="演示目标已更新",
            data={"accepted": True, "verification": "verified"},
            source="demo_api",
        )


def _catalog() -> ProviderCatalog:
    catalog = ProviderCatalog()
    catalog.register(
        ProviderOperationSpec(
            operation_id="demo.items.list",
            provider="demo",
            description="读取目标",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            result_kind="demo_items",
        )
    )
    catalog.register(
        ProviderOperationSpec(
            operation_id="demo.items.update",
            provider="demo",
            description="更新目标",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["item_ref"],
                "properties": {
                    "item_ref": {"type": "string", "minLength": 8, "maxLength": 64}
                },
                "additionalProperties": False,
            },
            result_kind="demo_change",
            reference_arguments={"item_ref": "demo_item"},
        )
    )
    return catalog


@pytest.fixture
def isolated_provider_db(tmp_path: Path):
    previous_path = db.DB_PATH
    previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "provider-plans.db", test_mode=True)
    db.init_db()
    try:
        yield
    finally:
        db.configure_database(previous_path, test_mode=previous_test_mode)


def test_provider_owner_reset_treats_missing_plan_table_as_empty(
    tmp_path: Path,
) -> None:
    previous_path = db.DB_PATH
    previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "empty-provider-plans.db", test_mode=True)
    try:
        assert invalidate_provider_plans_for_owner(owner="owner-a") == {
            "scrubbed_running": 0,
            "deleted": 0,
        }
    finally:
        db.configure_database(previous_path, test_mode=previous_test_mode)


def _insert_provider_executing_audit(
    plan_ref: str, confirmation_id: str
) -> None:
    stamp = db.now()
    db.add_agent_action_history(
        owner_digest="a" * 64,
        tool_name="provider.change.execute",
        risk="write",
        status="executing",
        ok=False,
        summary="Provider 原生写计划执行：执行中",
        safe_details={"plan_ref": plan_ref},
        started_at=stamp,
        finished_at=stamp,
        confirmation_id=confirmation_id,
    )


def _preview(gateway: ProviderGateway, context: ToolContext):
    query = gateway.query(
        profile_ref="configured:demo",
        operation="demo.items.list",
        arguments={},
        context=context,
    )
    object_ref = query.data["items"][0]["object_ref"]
    return gateway.preview_change(
        profile_ref="configured:demo",
        operation="demo.items.update",
        arguments={"item_ref": object_ref},
        context=context,
    )


def test_provider_write_plan_is_owner_bound_persistent_and_exactly_once(
    isolated_provider_db,
):
    transport = _WriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")

    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    assert plan_ref.startswith("PP-")
    assert "raw-demo-id" not in repr(preview.data)
    assert "/hidden/path" not in repr(preview.data)

    with pytest.raises(AgentToolError) as cross_owner:
        gateway.change_status(
            plan_ref=plan_ref,
            context=ToolContext(owner="owner-b", session_id="session-a"),
        )
    assert cross_owner.value.code == "plan_not_found"

    confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    assert confirmation.data["status"] == "prepared"

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )
    assert result.ok
    assert transport.executions == [
        (
            "configured:demo",
            "demo.items.update",
            {"item_ref": "raw-demo-id"},
        )
    ]
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "succeeded"

    with pytest.raises(AgentToolError) as repeated:
        gateway.execute_change(
            plan_ref=plan_ref,
            expected_context=fingerprint,
            context=context,
        )
    assert repeated.value.code == "already_executed"
    assert len(transport.executions) == 1


def test_provider_write_plan_rejects_changed_confirmation_context(isolated_provider_db):
    gateway = ProviderGateway(catalog=_catalog(), transports=[_WriteTransport()])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)

    with pytest.raises(AgentToolError) as stale:
        gateway.execute_change(
            plan_ref=preview.data["plan_ref"],
            expected_context="wrong-context",
            context=context,
        )
    assert stale.value.code == "confirmation_stale"
    status = gateway.change_status(plan_ref=preview.data["plan_ref"], context=context)
    assert status.data["status"] == "stale"


class _EmptyRevisionTransport(_WriteTransport):
    def profile_revision(self, profile_ref: str) -> str:
        assert profile_ref == "configured:demo"
        return ""


def test_provider_preview_requires_single_revisioned_transport_path(
    isolated_provider_db,
):
    transport = _EmptyRevisionTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    query = gateway.query(
        profile_ref="configured:demo",
        operation="demo.items.list",
        arguments={},
        context=context,
    )

    with pytest.raises(ProviderGatewayError) as invalid:
        gateway.preview_change(
            profile_ref="configured:demo",
            operation="demo.items.update",
            arguments={"item_ref": query.data["items"][0]["object_ref"]},
            context=context,
        )

    assert invalid.value.code == "invalid_response"
    with db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM agent_provider_plans").fetchone()[0]
    assert count == 0


def test_provider_execution_rejects_frozen_plan_without_revision(
    isolated_provider_db,
):
    transport = _WriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT arguments_json FROM agent_provider_plans WHERE plan_id=?",
            (plan_ref,),
        ).fetchone()
        arguments = json.loads(str(row["arguments_json"]))
        arguments.pop("__profile_revision", None)
        conn.execute(
            "UPDATE agent_provider_plans SET arguments_json=? WHERE plan_id=?",
            (json.dumps(arguments, ensure_ascii=False), plan_ref),
        )

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )

    assert not result.ok
    assert result.status == "stale"
    assert transport.executions == []
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "stale"


class _UncertainWriteTransport(_WriteTransport):
    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ):
        self.expected_profile_revisions.append(expected_profile_revision)
        self.executions.append((profile_ref, operation, dict(arguments)))
        raise ProviderGatewayError(
            "上游写入响应中断",
            code="provider_write_failed",
            external_write_possible=True,
        )


def test_provider_write_unknown_outcome_cannot_be_retried(isolated_provider_db):
    transport = _UncertainWriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )
    assert not result.ok
    assert result.status == "outcome_unknown"
    assert len(transport.executions) == 1
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "outcome_unknown"
    with pytest.raises(AgentToolError) as repeated:
        gateway.execute_change(
            plan_ref=plan_ref,
            expected_context=fingerprint,
            context=context,
        )
    assert repeated.value.code == "outcome_unknown"
    assert len(transport.executions) == 1


class _StaleAfterWriteTransport(_WriteTransport):
    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ):
        self.expected_profile_revisions.append(expected_profile_revision)
        self.executions.append((profile_ref, operation, dict(arguments)))
        raise ProviderGatewayError(
            "写请求后目标版本无法确认",
            code="confirmation_stale",
            external_write_possible=True,
        )


def test_provider_stale_after_transport_entry_is_unknown_and_not_retryable(
    isolated_provider_db,
):
    transport = _StaleAfterWriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )

    assert not result.ok
    assert result.status == "outcome_unknown"
    assert len(transport.executions) == 1
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "outcome_unknown"
    assert status.data["error_code"] == "confirmation_stale"
    with pytest.raises(AgentToolError) as repeated:
        gateway.execute_change(
            plan_ref=plan_ref,
            expected_context=fingerprint,
            context=context,
        )
    assert repeated.value.code == "outcome_unknown"
    assert len(transport.executions) == 1


def test_running_provider_plan_recovers_as_unknown_after_restart(
    isolated_provider_db,
):
    gateway = ProviderGateway(catalog=_catalog(), transports=[_WriteTransport()])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    claim_provider_plan(
        owner=context.owner,
        session_id=context.session_id,
        plan_ref=plan_ref,
        expected_context=fingerprint,
    )

    db.init_db()

    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "outcome_unknown"
    assert status.data["error_code"] == "execution_interrupted"


def test_new_writer_recovers_orphan_before_claiming_next_plan(
    isolated_provider_db,
):
    transport = _WriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    orphan_preview = _preview(gateway, context)
    orphan_ref = orphan_preview.data["plan_ref"]
    _confirmation, orphan_fingerprint = gateway.prepare_change_execution(
        plan_ref=orphan_ref, context=context
    )
    claim_provider_plan(
        owner=context.owner,
        session_id=context.session_id,
        plan_ref=orphan_ref,
        expected_context=orphan_fingerprint,
    )
    next_preview = _preview(gateway, context)
    next_ref = next_preview.data["plan_ref"]
    _confirmation, next_fingerprint = gateway.prepare_change_execution(
        plan_ref=next_ref, context=context
    )

    result = gateway.execute_change(
        plan_ref=next_ref,
        expected_context=next_fingerprint,
        context=context,
    )

    assert result.ok
    assert result.data["status"] == "succeeded"
    assert len(transport.executions) == 1
    orphan_status = gateway.change_status(plan_ref=orphan_ref, context=context)
    assert orphan_status.data["status"] == "outcome_unknown"
    assert orphan_status.data["error_code"] == "execution_interrupted"


class _InvalidWritePayloadTransport(_WriteTransport):
    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ):
        self.expected_profile_revisions.append(expected_profile_revision)
        self.executions.append((profile_ref, operation, dict(arguments)))
        return ProviderPayload(
            summary="上游声称已处理",
            data=[{"accepted": True}],  # type: ignore[arg-type]
            source="demo_api",
        )


def test_provider_invalid_write_payload_is_unknown_and_not_retryable(
    isolated_provider_db,
):
    transport = _InvalidWritePayloadTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )

    assert not result.ok
    assert result.status == "outcome_unknown"
    assert len(transport.executions) == 1
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "outcome_unknown"
    with pytest.raises(AgentToolError) as repeated:
        gateway.execute_change(
            plan_ref=plan_ref,
            expected_context=fingerprint,
            context=context,
        )
    assert repeated.value.code == "outcome_unknown"
    assert len(transport.executions) == 1


class _RevisionedWriteTransport(_WriteTransport):
    def __init__(self) -> None:
        super().__init__()
        self.revision = "revision-1"

    def profile_revision(self, profile_ref: str) -> str:
        assert profile_ref == "configured:demo"
        return self.revision


def test_provider_plan_rejects_changed_profile_revision_before_write(
    isolated_provider_db,
):
    transport = _RevisionedWriteTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    transport.revision = "revision-2"

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )

    assert not result.ok
    assert result.status == "stale"
    assert transport.executions == []


class _BrokenRevisionTransport(_WriteTransport):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def profile_revision(self, profile_ref: str) -> str:
        self.calls += 1
        if self.calls > 2:
            raise RuntimeError("unexpected local revision failure")
        return "revision-1"


def test_provider_prewrite_internal_failure_is_failed_without_external_write(
    isolated_provider_db,
):
    transport = _BrokenRevisionTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )

    result = gateway.execute_change(
        plan_ref=plan_ref,
        expected_context=fingerprint,
        context=context,
    )

    assert not result.ok
    assert result.status == "failed"
    assert transport.executions == []
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "failed"
    assert status.data["error_code"] == "provider_prewrite_failed"


class _BlockingPreviewRevisionTransport(_RevisionedWriteTransport):
    def __init__(self) -> None:
        super().__init__()
        self.preview_entered = Event()
        self.preview_release = Event()

    def preview_write(
        self, profile_ref: str, operation: str, arguments: dict, target_snapshot: dict
    ):
        self.preview_entered.set()
        if not self.preview_release.wait(timeout=3):
            raise AssertionError("preview barrier timed out")
        return super().preview_write(profile_ref, operation, arguments, target_snapshot)


def test_provider_preview_rejects_revision_change_across_barrier(
    isolated_provider_db,
):
    transport = _BlockingPreviewRevisionTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    query = gateway.query(
        profile_ref="configured:demo",
        operation="demo.items.list",
        arguments={},
        context=context,
    )
    object_ref = query.data["items"][0]["object_ref"]
    outcomes: list[object] = []

    def run_preview() -> None:
        try:
            outcomes.append(
                gateway.preview_change(
                    profile_ref="configured:demo",
                    operation="demo.items.update",
                    arguments={"item_ref": object_ref},
                    context=context,
                )
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = Thread(target=run_preview)
    worker.start()
    assert transport.preview_entered.wait(timeout=3)
    transport.revision = "revision-2"
    transport.preview_release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], ProviderGatewayError)
    assert outcomes[0].code == "confirmation_stale"
    with db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM agent_provider_plans").fetchone()[0]
    assert count == 0


class _ExecutionRevisionBarrierTransport(_RevisionedWriteTransport):
    def __init__(self) -> None:
        super().__init__()
        self.arm_revision_barrier = False
        self.revision_checked = Event()
        self.revision_release = Event()

    def profile_revision(self, profile_ref: str) -> str:
        snapshot = super().profile_revision(profile_ref)
        if self.arm_revision_barrier:
            self.arm_revision_barrier = False
            self.revision_checked.set()
            if not self.revision_release.wait(timeout=3):
                raise AssertionError("execution revision barrier timed out")
        return snapshot

    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ):
        self.expected_profile_revisions.append(expected_profile_revision)
        if expected_profile_revision != self.revision:
            raise ProviderGatewayError("配置快照已变化", code="confirmation_stale")
        self.executions.append((profile_ref, operation, dict(arguments)))
        return ProviderPayload(
            summary="演示目标已更新",
            data={"accepted": True, "verification": "verified"},
            source="demo_api",
        )


def test_provider_transport_recheck_blocks_race_without_external_side_effect(
    isolated_provider_db,
):
    transport = _ExecutionRevisionBarrierTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    transport.arm_revision_barrier = True
    outcomes: list[ToolResult | BaseException] = []

    def run_execute() -> None:
        try:
            outcomes.append(
                gateway.execute_change(
                    plan_ref=plan_ref,
                    expected_context=fingerprint,
                    context=context,
                )
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = Thread(target=run_execute)
    worker.start()
    assert transport.revision_checked.wait(timeout=3)
    transport.revision = "revision-2"
    transport.revision_release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], ToolResult)
    result = outcomes[0]
    assert not result.ok
    assert result.status == "stale"
    assert transport.expected_profile_revisions == ["revision-1"]
    assert transport.executions == []
    status = gateway.change_status(plan_ref=plan_ref, context=context)
    assert status.data["status"] == "stale"
    assert status.data["error_code"] == "confirmation_stale"


def test_latest_provider_plan_uses_creation_order_with_second_precision_timestamps(
    isolated_provider_db,
):
    common = {
        "owner": "owner-order",
        "session_id": "session-order",
        "provider": "demo",
        "profile_ref": "configured:demo",
        "operation": "demo.pause",
        "risk": "write",
        "target_snapshot": {"id": "target"},
        "context_fingerprint": "fingerprint",
    }
    with patch(
        "app.repositories.agent_provider_plans.now",
        return_value="2026-09-01 12:00:00",
    ), patch(
        "app.repositories.agent_provider_plans.secrets.token_hex",
        side_effect=("1" * 24, "0" * 24),
    ):
        first = create_provider_plan(
            **common, arguments={"value": "first"}, summary="first"
        )
        second = create_provider_plan(
            **common, arguments={"value": "second"}, summary="second"
        )

    latest = get_latest_prepared_provider_plan(
        owner="owner-order", session_id="session-order"
    )
    assert latest["plan_ref"] == second["plan_ref"]
    assert latest["plan_ref"] != first["plan_ref"]
    assert latest["arguments"] == {"value": "second"}


def test_provider_owner_reset_removes_prepared_and_scrubs_running_plan(
    isolated_provider_db,
):
    gateway = ProviderGateway(catalog=_catalog(), transports=[_WriteTransport()])
    owner_context = ToolContext(owner="owner-a", session_id="session-a")
    prepared = _preview(gateway, owner_context)
    running = _preview(gateway, owner_context)
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=running.data["plan_ref"], context=owner_context
    )
    claim_provider_plan(
        owner=owner_context.owner,
        session_id=owner_context.session_id,
        plan_ref=running.data["plan_ref"],
        expected_context=fingerprint,
    )
    other_context = ToolContext(owner="owner-b", session_id="session-b")
    other = _preview(gateway, other_context)

    result = invalidate_provider_plans_for_owner(owner=owner_context.owner)

    assert result == {"scrubbed_running": 1, "deleted": 1}
    with pytest.raises(AgentToolError) as missing:
        gateway.prepare_change_execution(
            plan_ref=prepared.data["plan_ref"], context=owner_context
        )
    assert missing.value.code == "plan_not_found"
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT owner_digest,session_digest,provider,profile_ref,operation,"
            "arguments_json,target_snapshot_json,context_fingerprint,error_code,status "
            "FROM agent_provider_plans WHERE plan_id=?",
            (running.data["plan_ref"],),
        ).fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["provider"] == ""
    assert row["profile_ref"] == ""
    assert row["operation"] == ""
    assert row["arguments_json"] == "{}"
    assert row["target_snapshot_json"] == "{}"
    assert row["context_fingerprint"] == ""
    assert row["error_code"] == "session_reset_pending"
    assert gateway.prepare_change_execution(
        plan_ref=other.data["plan_ref"], context=other_context
    )[0].ok


def test_startup_recovery_does_not_reclassify_plan_held_by_live_writer(
    isolated_provider_db,
):
    gateway = ProviderGateway(catalog=_catalog(), transports=[_WriteTransport()])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    claim_provider_plan(
        owner=context.owner,
        session_id=context.session_id,
        plan_ref=plan_ref,
        expected_context=fingerprint,
    )
    confirmation_id = "provider-live-confirmation-1"
    _insert_provider_executing_audit(plan_ref, confirmation_id)

    assert _PROVIDER_WRITE_LOCK.acquire(blocking=False)
    try:
        db.init_db()
        while_locked = gateway.change_status(plan_ref=plan_ref, context=context)
        assert while_locked.data["status"] == "running"
        assert while_locked.summary == "Provider 写计划执行中"
        assert "target_snapshot" not in while_locked.data
        assert "result" not in while_locked.data
        with db.get_conn() as conn:
            audit = conn.execute(
                "SELECT status,error_code FROM agent_action_history "
                "WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
        assert audit is not None
        assert audit["status"] == "executing"
        assert audit["error_code"] == ""
    finally:
        _PROVIDER_WRITE_LOCK.release()

    recovered = gateway.change_status(plan_ref=plan_ref, context=context)
    assert recovered.data["status"] == "outcome_unknown"
    assert recovered.data["error_code"] == "execution_interrupted"
    with db.get_conn() as conn:
        audit = conn.execute(
            "SELECT status,error_code FROM agent_action_history WHERE confirmation_id=?",
            (confirmation_id,),
        ).fetchone()
    assert audit is not None
    assert audit["status"] == "outcome_unknown"
    assert audit["error_code"] == "execution_interrupted"


def test_change_status_translates_writer_lock_failure(
    isolated_provider_db,
):
    gateway = ProviderGateway(catalog=_catalog(), transports=[_WriteTransport()])
    context = ToolContext(owner="owner-a", session_id="session-a")
    preview = _preview(gateway, context)
    plan_ref = preview.data["plan_ref"]
    _confirmation, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    claim_provider_plan(
        owner=context.owner,
        session_id=context.session_id,
        plan_ref=plan_ref,
        expected_context=fingerprint,
    )

    with patch.object(
        _PROVIDER_WRITE_LOCK,
        "acquire",
        side_effect=OSError("sensitive lock directory"),
    ):
        with pytest.raises(ProviderGatewayError) as failure:
            gateway.change_status(plan_ref=plan_ref, context=context)

    assert failure.value.code == "provider_unavailable"
    assert "lock directory" not in str(failure.value)
