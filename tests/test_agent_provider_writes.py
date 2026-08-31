from __future__ import annotations

from pathlib import Path

import pytest

from app import database as db
from app.agent.models import RiskLevel, ToolContext
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_gateway import ProviderGateway
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderOperationSpec,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.registry import AgentToolError
from app.repositories.agent_provider_plans import claim_provider_plan


class _WriteTransport:
    provider = "demo"

    def __init__(self) -> None:
        self.executions: list[tuple[str, str, dict]] = []

    def profiles(self):
        return [ProviderProfileView("configured:demo", "demo", "Demo", "online")]

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

    def execute_write(self, profile_ref: str, operation: str, arguments: dict):
        self.executions.append((profile_ref, operation, dict(arguments)))
        return ProviderPayload(
            summary="演示目标已更新",
            data={"accepted": True, "verification": "verified"},
            source="demo_api",
        )


def _catalog() -> ProviderCatalog:
    catalog = ProviderCatalog()
    catalog.register(ProviderOperationSpec(
        operation_id="demo.items.list",
        provider="demo",
        description="读取目标",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        result_kind="demo_items",
    ))
    catalog.register(ProviderOperationSpec(
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
    ))
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


class _UncertainWriteTransport(_WriteTransport):
    def execute_write(self, profile_ref: str, operation: str, arguments: dict):
        self.executions.append((profile_ref, operation, dict(arguments)))
        raise ProviderGatewayError(
            "上游写入响应中断", code="provider_write_failed"
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
