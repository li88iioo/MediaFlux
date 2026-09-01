"""Media Agent 多站资源搜索与确认提交测试。"""
from __future__ import annotations

import asyncio
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.agent.indexer_actions import (
    _submit_resource,
    _submit_resource_batch,
    prepare_submit_resource,
    search_arguments,
    search_resources,
    submit_arguments,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.indexers.downloads import download_result
from app.indexers.errors import IndexerResultExpired
from app.indexers.models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerProviderError,
    ResolvedDownload,
)
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase

_RESULT_ID = "opaque-result-1234"
_SECRET_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
_SECRET_TORRENT_URL = "https://private-indexer.example/resource.torrent"
_SECRET_DETAIL_URL = "https://private-indexer.example/details/1"


class FakeResultStore:
    def __init__(self, item: IndexerItem):
        self.item = item
        self.calls: list[str] = []

    def get(self, result_id: str) -> IndexerItem:
        self.calls.append(result_id)
        if result_id == "expired-result-1234":
            raise IndexerResultExpired()
        return self.item


class FakeIndexerService:
    def __init__(self, *, item: IndexerItem | None = None):
        self.item = item or _resource_item()
        self.result_store = FakeResultStore(self.item)
        self.enabled_site_ids = frozenset({"nyaa"})
        self.search_calls: list[tuple[object, object]] = []

    async def search_media(self, request, sites=None):
        self.search_calls.append((request, sites))
        return AggregatedIndexerResult(
            query=request.title,
            page=request.page,
            items=[self.item],
            sites_attempted=("nyaa", "broken"),
            sites_succeeded=("nyaa",),
            errors=[IndexerProviderError("broken", "unavailable", "站点暂不可用")],
            partial=True,
            cached=False,
            has_more=True,
        )


class FakeDownloadService:
    def __init__(self, item: IndexerItem):
        self.result_store = FakeResultStore(item)

    async def resolve(self, _result_id: str) -> ResolvedDownload:
        return ResolvedDownload(kind="magnet", value=_SECRET_MAGNET)


def _resource_item(**overrides) -> IndexerItem:
    values = {
        "result_id": _RESULT_ID,
        "site_id": "nyaa",
        "site_name": "Nyaa",
        "title": "Demo Resource",
        "detail_url": _SECRET_DETAIL_URL,
        "category": "Anime",
        "size_text": "1.2 GB",
        "size_bytes": 1_200_000_000,
        "seeders": 42,
        "leechers": 3,
        "downloads": 9,
        "published_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "download_state": "ready",
        "download_kinds": ("magnet", "torrent"),
        "magnet": _SECRET_MAGNET,
        "torrent_url": _SECRET_TORRENT_URL,
    }
    values.update(overrides)
    return IndexerItem(**values)


def _identity(arguments):
    return dict(arguments)


class AgentIndexerActionUnitTests(unittest.TestCase):
    def test_search_arguments_normalize_and_reject_unsafe_fields(self):
        service = FakeIndexerService()
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ):
            normalized = search_arguments({
                "title": "  Demo  ",
                "aliases": ["Alias"],
                "year": 2026,
                "media_type": "ANIME",
                "sites": ["NYAA", "nyaa"],
                "limit": 12,
            })
        self.assertEqual(normalized["title"], "Demo")
        self.assertEqual(normalized["media_type"], "anime")
        self.assertEqual(normalized["sites"], ["nyaa"])
        self.assertEqual(normalized["limit"], 12)

        invalid_payloads = (
            {"title": "Demo", "magnet": _SECRET_MAGNET},
            {"title": ""},
            {"title": "Demo\x00"},
            {"title": "Demo", "aliases": ["x"] * 9},
            {"title": "Demo", "year": True},
            {"title": "Demo", "page": 0},
            {"title": "Demo", "limit": 51},
            {"title": "Demo", "sites": ["../nyaa"]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(AgentToolError):
                search_arguments(payload)

    def test_search_arguments_reject_disabled_site(self):
        service = FakeIndexerService()
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), self.assertRaises(AgentToolError) as rejected:
            search_arguments({"title": "Demo", "sites": ["mikan"]})
        self.assertIn("未启用", rejected.exception.safe_message)

    def test_search_resources_returns_only_public_projection(self):
        service = FakeIndexerService()
        arguments = {
            "title": "Demo",
            "original_title": "",
            "english_title": "",
            "aliases": [],
            "year": None,
            "media_type": "",
            "page": 1,
            "sites": ["nyaa"],
            "limit": 20,
        }
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ):
            result = search_resources(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["items"][0]["result_id"], _RESULT_ID)
        self.assertEqual(service.search_calls[0][1], ["nyaa"])
        serialized = str(result.to_dict())
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)
        self.assertNotIn(_SECRET_DETAIL_URL, serialized)

    def test_search_resources_enforces_caller_timeout(self):
        service = FakeIndexerService()

        async def slow_search(_request, _sites=None):
            await asyncio.sleep(0.05)
            raise AssertionError("timeout should cancel the slow search")

        service.search_media = slow_search
        arguments = {
            "title": "Demo",
            "original_title": "",
            "english_title": "",
            "aliases": [],
            "year": None,
            "media_type": "",
            "page": 1,
            "sites": ["nyaa"],
            "limit": 20,
        }
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ):
            result = search_resources(arguments, timeout_seconds=0.001)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.error, "资源检索超时。")

    def test_search_resources_disabled_does_not_create_service(self):
        with patch("app.agent.indexer_actions.config.get_bool", return_value=False), patch(
            "app.agent.indexer_actions.get_indexer_service"
        ) as service_factory:
            result = search_resources({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        service_factory.assert_not_called()

    def test_submit_arguments_and_registry_confirmation_gate(self):
        self.assertEqual(
            submit_arguments({"result_id": f"  {_RESULT_ID}  ", "target": "QB"}),
            {"result_id": _RESULT_ID, "target": "qb"},
        )
        for payload in (
            {"result_id": "short", "target": "qb"},
            {"result_id": _RESULT_ID, "target": "other"},
            {"result_id": _RESULT_ID, "target": "qb", "url": "https://attacker.invalid"},
        ):
            with self.subTest(payload=payload), self.assertRaises(AgentToolError):
                submit_arguments(payload)

        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["indexer.search_resources"]["risk"], "read")
        self.assertEqual(capabilities["indexer.submit_resource"]["risk"], "danger")
        self.assertTrue(capabilities["indexer.submit_resource"]["requires_confirmation"])
        with self.assertRaises(AgentToolError) as blocked:
            registry.execute("indexer.submit_resource", {"result_id": _RESULT_ID, "target": "qb"})
        self.assertEqual(blocked.exception.code, "confirmation_required")

    def test_preview_is_safe_and_requires_ready_target(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions._target_readiness", return_value={"qb": True}):
            result, _context = prepare_submit_resource(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "confirmation_required")
        serialized = str(result.to_dict())
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)
        self.assertNotIn(_SECRET_DETAIL_URL, serialized)

        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions._target_readiness", return_value={"qb": False}):
            unavailable, _context = prepare_submit_resource(arguments)
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "not_configured")

    def test_preview_rejects_expired_and_non_downloadable_results(self):
        service = FakeIndexerService()
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ):
            expired, _context = prepare_submit_resource({
                "result_id": "expired-result-1234",
                "target": "qb",
            })
            service.result_store.item = _resource_item(
                download_state="unavailable",
                download_kinds=(),
            )
            blocked, _context = prepare_submit_resource({
                "result_id": _RESULT_ID,
                "target": "qb",
            })

        self.assertFalse(expired.ok)
        self.assertEqual(expired.status, "result_expired")
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "validation_error")

    def test_confirmation_context_changes_when_resource_changes(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions._target_readiness", return_value={"qb": True}):
            first = prepare_submit_resource(arguments)[1]
            service.result_store.item = _resource_item(title="Changed Resource")
            second = prepare_submit_resource(arguments)[1]
        self.assertNotEqual(first, second)
        self.assertNotIn("Changed", first + second)

    def test_submit_resource_returns_sanitized_public_result(self):
        service = FakeIndexerService()
        dispatch = AsyncMock(return_value={
            "result_id": _RESULT_ID,
            "ok": True,
            "request_id": 12,
            "created": True,
            "target": "both",
            "status": "partial",
            "succeeded": ["qb"],
            "failed": ["guangya"],
            "duplicate": False,
            "error": "private backend error",
            "results": {"qb": {"token": "secret"}},
        })
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions.download_result", dispatch):
            result = _submit_resource({"result_id": _RESULT_ID, "target": "both"})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(set(result.data), {
            "result_id", "request_id", "created", "target", "status", "succeeded", "failed", "duplicate",
        })
        self.assertNotIn("private backend error", str(result.to_dict()))
        self.assertNotIn("secret", str(result.to_dict()))
        dispatch.assert_awaited_once_with(
            service,
            _RESULT_ID,
            "both",
            origin_namespace="agent",
        )

    def test_submit_resource_maps_duplicate_failure_and_internal_error(self):
        service = FakeIndexerService()
        base = {
            "result_id": _RESULT_ID,
            "request_id": 12,
            "created": False,
            "target": "qb",
            "succeeded": [],
            "failed": [],
        }
        outcomes = (
            (
                {**base, "ok": False, "status": "duplicate", "duplicate": True},
                "conflict",
            ),
            (
                {**base, "ok": False, "status": "failed", "duplicate": False},
                "unavailable",
            ),
        )
        for payload, expected_status in outcomes:
            with self.subTest(expected_status=expected_status), patch(
                "app.agent.indexer_actions.config.get_bool", return_value=True
            ), patch(
                "app.agent.indexer_actions.get_indexer_service", return_value=service
            ), patch(
                "app.agent.indexer_actions.download_result",
                AsyncMock(return_value={**payload, "error": "private backend detail"}),
            ):
                result = _submit_resource({"result_id": _RESULT_ID, "target": "qb"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, expected_status)
            self.assertNotIn("private backend detail", str(result.to_dict()))

        with patch(
            "app.agent.indexer_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch(
            "app.agent.indexer_actions.download_result",
            AsyncMock(side_effect=RuntimeError("secret internal error")),
        ):
            internal = _submit_resource({"result_id": _RESULT_ID, "target": "qb"})
        self.assertFalse(internal.ok)
        self.assertEqual(internal.status, "unavailable")
        self.assertNotIn("secret internal error", str(internal.to_dict()))

    def test_submit_resource_preserves_manual_review_without_private_error(self):
        service = FakeIndexerService()
        dispatch = AsyncMock(return_value={
            "result_id": _RESULT_ID,
            "ok": False,
            "request_id": 12,
            "created": True,
            "target": "guangya",
            "status": "manual_review",
            "succeeded": [],
            "failed": ["guangya"],
            "duplicate": False,
            "error": "private timeout magnet:?xt=urn:btih:secret",
        })
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions.download_result", dispatch):
            result = _submit_resource({"result_id": _RESULT_ID, "target": "guangya"})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.summary, "下载任务提交结果待核对")
        self.assertNotIn("private timeout", str(result.to_dict()))
        self.assertNotIn("magnet:", str(result.to_dict()))

    def test_submit_resource_batch_counts_manual_review_separately(self):
        service = FakeIndexerService()
        public_result = {
            "result_id": _RESULT_ID,
            "ok": False,
            "request_id": 12,
            "created": True,
            "target": "guangya",
            "status": "manual_review",
            "succeeded": [],
            "failed": ["guangya"],
            "duplicate": False,
            "error": "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        }
        dispatch = AsyncMock(return_value=public_result)
        result_ids = [_RESULT_ID, "opaque-result-5678"]
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions.download_result_public", dispatch):
            result = _submit_resource_batch({"result_ids": result_ids, "target": "guangya"})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["review_required"], 2)
        self.assertEqual(result.data["failed"], 0)
        self.assertIn("勿直接重复提交", result.error)
        self.assertEqual(dispatch.await_count, 2)

    def test_shared_download_service_dispatches_only_server_resolved_value(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = object()
        normalize = Mock(return_value=normalized)
        create = Mock(return_value={"id": 7, "created": True})
        dispatch = Mock(return_value={"duplicate": False, "succeeded": ["qb"], "failed": []})

        result = asyncio.run(download_result(
            service,
            _RESULT_ID,
            "qb",
            normalize=normalize,
            create=create,
            dispatch=dispatch,
        ))

        normalize.assert_called_once_with(_SECRET_MAGNET)
        create.assert_called_once_with(normalized, "", "", origin="indexer:nyaa")
        dispatch.assert_called_once_with(7, "qb")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "submitted")

    def test_shared_download_service_marks_agent_origin_namespace(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = object()
        create = Mock(return_value={"id": 8, "created": True})
        dispatch = Mock(return_value={"duplicate": False, "succeeded": ["qb"], "failed": []})

        result = asyncio.run(download_result(
            service,
            _RESULT_ID,
            "qb",
            origin_namespace="agent",
            normalize=Mock(return_value=normalized),
            create=create,
            dispatch=dispatch,
        ))

        create.assert_called_once_with(normalized, "", "", origin="agent:nyaa")
        self.assertTrue(result["ok"])

    def test_natural_language_resource_search_does_not_replace_library_search(self):
        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in ("indexer.search_resources", "library.search"):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments))) or ToolResult(True, "success", tool)
                ),
                validator=_identity,
            ))
        service = AgentOrchestrator(registry)

        resource = service.query("搜索《沙丘2》的资源")
        self.assertEqual(resource["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(resource["tool_call"]["arguments"], {"title": "沙丘2", "limit": 20})
        library = service.query("帮我找《沙丘2》")
        self.assertEqual(library["tool_call"]["name"], "library.search")

    def test_natural_language_resource_search_enforces_tool_rate_limit_per_identity(self):
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="indexer.search_resources",
            description="indexer.search_resources",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "indexer.search_resources")
            ),
            validator=_identity,
        ))
        service = AgentOrchestrator(registry)
        agent_rate_limiter.reset()
        try:
            for _ in range(6):
                response = service.query(
                    "搜索《沙丘2》的资源",
                    query_tool_rate_identity="owner-a",
                )
                self.assertEqual(
                    response["tool_call"]["name"], "indexer.search_resources"
                )

            with self.assertRaises(AgentToolError) as limited:
                service.query(
                    "搜索《沙丘2》的资源",
                    query_tool_rate_identity="owner-a",
                )
            self.assertEqual(limited.exception.code, "rate_limited")
            self.assertEqual(len(calls), 6)

            response = service.query(
                "搜索《沙丘2》的资源",
                query_tool_rate_identity="owner-b",
            )
            self.assertEqual(
                response["tool_call"]["name"], "indexer.search_resources"
            )
            self.assertEqual(len(calls), 7)
        finally:
            agent_rate_limiter.reset()


class AgentIndexerActionAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_search_api_and_prepare_confirm_replay(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeIndexerService()
        dispatch = AsyncMock(return_value={
            "result_id": _RESULT_ID,
            "ok": True,
            "request_id": 12,
            "created": True,
            "target": "qb",
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "duplicate": False,
            "error": "",
        })

        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions._target_readiness", return_value={"qb": True}), patch(
            "app.agent.indexer_actions.download_result", dispatch
        ):
            searched = self.client.post(
                "/api/agent/tools/indexer.search_resources",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"title": "Demo", "sites": ["nyaa"]}},
            )
            self.assertEqual(searched.status_code, 200, searched.text)
            self.assertEqual(searched.json()["result"]["status"], "partial")
            self.assertNotIn(_SECRET_MAGNET, searched.text)

            prepared = self.client.post(
                "/api/agent/actions/indexer.submit_resource/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"result_id": _RESULT_ID, "target": "qb"}},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            confirmation_id = prepared.json()["action_plan"]["plan_id"]
            dispatch.assert_not_awaited()

            direct = self.client.post(
                "/api/agent/tools/indexer.submit_resource",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"result_id": _RESULT_ID, "target": "qb"}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "accepted")
            dispatch.assert_awaited_once()

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertEqual(dispatch.await_count, 1)

    def test_query_and_direct_resource_search_share_tool_rate_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeIndexerService()

        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ):
            queried = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "搜索《Demo》的资源"},
            )
            self.assertEqual(queried.status_code, 200, queried.text)
            self.assertEqual(
                queried.json()["tool_call"]["name"],
                "indexer.search_resources",
            )

            for _ in range(5):
                direct = self.client.post(
                    "/api/agent/tools/indexer.search_resources",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"title": "Demo"}},
                )
                self.assertEqual(direct.status_code, 200, direct.text)

            limited = self.client.post(
                "/api/agent/tools/indexer.search_resources",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"title": "Demo"}},
            )

        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertIn("请求过于频繁", limited.json()["error"])
        self.assertEqual(len(service.search_calls), 6)

    def test_confirmation_becomes_stale_when_resource_changes(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeIndexerService()
        dispatch = AsyncMock()

        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch("app.agent.indexer_actions._target_readiness", return_value={"qb": True}), patch(
            "app.agent.indexer_actions.download_result", dispatch
        ):
            searched = self.client.post(
                "/api/agent/tools/indexer.search_resources",
                headers=headers,
                json={
                    "session_id": "test_session_identifier_0001",
                    "arguments": {"title": "Demo", "sites": ["nyaa"]},
                },
            )
            self.assertEqual(searched.status_code, 200, searched.text)

            prepared = self.client.post(
                "/api/agent/actions/indexer.submit_resource/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"result_id": _RESULT_ID, "target": "qb"}},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            service.result_store.item = _resource_item(download_state="resolvable", download_kinds=("torrent",))
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": prepared.json()["action_plan"]["plan_id"]},
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertIn("变化", stale.json()["error"])
            dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
