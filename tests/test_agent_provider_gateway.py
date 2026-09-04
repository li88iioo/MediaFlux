from __future__ import annotations

import pytest

from app.agent.models import RiskLevel, ToolContext
from app.agent.provider_artifacts import ProviderArtifactStore
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_gateway import ProviderGateway
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderOperationSpec,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.provider_operations import build_provider_catalog
from app.agent.provider_policy import validate_provider_arguments


class _FakeTransport:
    provider = "demo"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def profiles(self):
        return [ProviderProfileView("configured:demo", "demo", "Demo", "online")]

    def execute_read(self, profile_ref: str, operation: str, arguments: dict):
        self.calls.append((profile_ref, operation, dict(arguments)))
        if operation == "demo.items.files":
            return ProviderPayload(
                summary="详情已读取",
                data={"resolved": arguments["item_ref"]},
                source="demo_api",
            )
        return ProviderPayload(
            summary="条目已读取",
            data={
                "items": [{
                    "__object_id": "raw-provider-id",
                    "__object_kind": "demo_item",
                    "name": "Example",
                    "path": "/secret/path",
                    "token": "secret",
                    "hash": "deadbeef",
                }],
                "count": 1,
            },
            source="demo_api",
        )


def _catalog() -> ProviderCatalog:
    catalog = ProviderCatalog()
    catalog.register(ProviderOperationSpec(
        operation_id="demo.items.list",
        provider="demo",
        description="list demo items",
        risk=RiskLevel.READ,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        result_kind="demo_items",
    ))
    catalog.register(ProviderOperationSpec(
        operation_id="demo.items.files",
        provider="demo",
        description="read selected item",
        risk=RiskLevel.READ,
        parameters={
            "type": "object",
            "required": ["item_ref"],
            "properties": {
                "item_ref": {"type": "string", "minLength": 8, "maxLength": 64},
            },
            "additionalProperties": False,
        },
        result_kind="demo_files",
        reference_arguments={"item_ref": "demo_item"},
    ))
    return catalog


def test_default_catalog_contains_media_and_qb_read_operations():
    catalog = build_provider_catalog()
    operations = {item.operation_id for item in catalog.operations()}
    assert "media.items.counts" in operations
    assert "media.items.recent_added" in operations
    assert "media.items.recent_played" in operations
    assert "media.items.recommend_from_library" in operations
    assert "media.items.continue_watching" in operations
    assert "media.libraries.list" in operations
    assert "media.library.counts" in operations
    assert "media.series.episodes" in operations
    assert "qb.torrents.info" in operations
    assert "qb.torrents.files" in operations

    library_counts = catalog.get("media.library.counts")
    assert library_counts.reference_arguments == {"library_ref": "media_library"}
    named_library_operations = {
        item.operation_id
        for item in catalog.list(
            provider="media", intent="动漫媒体库里有多少部剧集", limit=8
        )
    }
    assert {"media.libraries.list", "media.library.counts"} <= named_library_operations


def test_catalog_rejects_duplicate_and_invalid_operations():
    catalog = _catalog()
    with pytest.raises(ValueError):
        catalog.register(next(iter(catalog.operations())))
    with pytest.raises(ValueError):
        catalog.register(ProviderOperationSpec(
            operation_id="raw_http",
            provider="demo",
            description="invalid",
            risk=RiskLevel.READ,
            parameters={"type": "object", "properties": {}},
            result_kind="invalid",
        ))


def test_policy_rejects_extra_network_and_credential_arguments():
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    }
    assert validate_provider_arguments(schema, {"query": "test"}) == {"query": "test"}
    with pytest.raises(ProviderGatewayError) as exc_info:
        validate_provider_arguments(schema, {"query": "test", "url": "http://internal"})
    assert exc_info.value.code == "invalid_arguments"


def test_gateway_projects_sensitive_fields_and_issues_owner_scoped_refs():
    transport = _FakeTransport()
    gateway = ProviderGateway(catalog=_catalog(), transports=[transport])
    context = ToolContext(owner="owner-a", session_id="session-a")

    result = gateway.query(
        profile_ref="configured:demo",
        operation="demo.items.list",
        arguments={},
        context=context,
    )

    item = result.data["items"][0]
    assert item["name"] == "Example"
    assert item["object_ref"].startswith("PO-")
    assert result.data["artifact_ref"].startswith("PA-")
    assert "path" not in item
    assert "token" not in item
    assert "hash" not in item

    detail = gateway.query(
        profile_ref="configured:demo",
        operation="demo.items.files",
        arguments={"item_ref": item["object_ref"]},
        context=context,
    )
    assert "raw-provider-id" not in str(detail.data)
    assert transport.calls[-1][2]["item_ref"] == "raw-provider-id"

    with pytest.raises(ProviderGatewayError) as exc_info:
        gateway.query(
            profile_ref="configured:demo",
            operation="demo.items.files",
            arguments={"item_ref": item["object_ref"]},
            context=ToolContext(owner="owner-b", session_id="session-a"),
        )
    assert exc_info.value.code == "artifact_expired"


def test_artifact_store_clear_owner_keeps_other_owner_isolated():
    store = ProviderArtifactStore()
    _first_artifact, first_public = store.put(
        owner="owner-a",
        session_id="s1",
        provider="demo",
        profile_ref="configured:demo",
        operation="demo.items.list",
        data={"items": [{"__object_id": "raw-a", "__object_kind": "movie"}]},
    )
    _second_artifact, second_public = store.put(
        owner="owner-b",
        session_id="s2",
        provider="demo",
        profile_ref="configured:demo",
        operation="demo.items.list",
        data={"items": [{"__object_id": "raw-b", "__object_kind": "movie"}]},
    )
    first_ref = first_public["items"][0]["object_ref"]
    second_ref = second_public["items"][0]["object_ref"]

    assert store.clear_owner(owner="owner-a") == 1
    with pytest.raises(ProviderGatewayError) as expired:
        store.resolve_object(
            owner="owner-a",
            session_id="s1",
            object_ref=first_ref,
            provider="demo",
            profile_ref="configured:demo",
            expected_kind="movie",
        )
    assert expired.value.code == "artifact_expired"
    raw_id, _snapshot = store.resolve_object(
        owner="owner-b",
        session_id="s2",
        object_ref=second_ref,
        provider="demo",
        profile_ref="configured:demo",
        expected_kind="movie",
    )
    assert raw_id == "raw-b"


def test_artifact_store_rejects_wrong_kind_and_session():
    store = ProviderArtifactStore()
    _artifact, public = store.put(
        owner="owner",
        session_id="s1",
        provider="demo",
        profile_ref="configured:demo",
        operation="demo.items.list",
        data={"items": [{"__object_id": "1", "__object_kind": "movie", "name": "A"}]},
    )
    ref = public["items"][0]["object_ref"]
    with pytest.raises(ProviderGatewayError):
        store.resolve_object(
            owner="owner",
            session_id="s2",
            object_ref=ref,
            provider="demo",
            profile_ref="configured:demo",
            expected_kind="movie",
        )
    with pytest.raises(ProviderGatewayError) as exc_info:
        store.resolve_object(
            owner="owner",
            session_id="s1",
            object_ref=ref,
            provider="demo",
            profile_ref="configured:demo",
            expected_kind="series",
        )
    assert exc_info.value.code == "invalid_arguments"
