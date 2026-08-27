from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app import config as app_config
from app.indexers.models import (
    AggregatedIndexerResult,
    IndexerCapabilities,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerProviderError,
    ResolvedDownload,
)
from app.main import create_app
from app.modules import indexer_download
from app.routes import indexers_api


class FakeResultStore:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def get(self, result_id):
        self.calls.append(result_id)
        if result_id == "expired":
            from app.indexers.errors import IndexerResultExpired
            raise IndexerResultExpired()
        return self.item


class FakeAdapter:
    site_id = "nyaa"
    site_name = "Nyaa"
    default_enabled = True
    capabilities = IndexerCapabilities(True, ("magnet", "torrent"))

    def __init__(self, resolved):
        self.resolved = resolved
        self.resolve_calls = []
        self.http = SimpleNamespace(get=AsyncMock())

    async def resolve(self, item):
        self.resolve_calls.append(item)
        return self.resolved


class FakeRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def ids(self):
        return ("nyaa",)

    def enabled_ids(self):
        return ("nyaa",)

    def get(self, site_id):
        if site_id != "nyaa":
            raise KeyError(site_id)
        return self.adapter


class FakeIndexerService:
    def __init__(self, resolved=None):
        item = IndexerItem(
            result_id="opaque-result",
            site_id="nyaa",
            site_name="Nyaa",
            title="Demo",
            download_state="ready",
            download_kinds=("magnet",),
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
        )
        self.result_store = FakeResultStore(item)
        self.adapter = FakeAdapter(resolved or ResolvedDownload(kind="magnet", value=item.magnet))
        self.registry = FakeRegistry(self.adapter)
        self.search_calls = []
        self.search_sort_modes = []
        self.media_search_calls = []

    async def resolve(self, result_id):
        item = self.result_store.get(result_id)
        return await self.adapter.resolve(item)

    async def search(self, query, page, site_ids, *, sort_mode="relevance_desc"):
        self.search_calls.append((query, page, site_ids))
        self.search_sort_modes.append(sort_mode)
        return AggregatedIndexerResult(
            query=query,
            page=page,
            items=[self.result_store.item],
            sites_attempted=("nyaa",),
            sites_succeeded=("nyaa",),
            errors=[],
            partial=False,
            cached=False,
        )

    async def search_media(self, request, site_ids):
        self.media_search_calls.append((request, site_ids))
        return AggregatedIndexerResult(
            query=request.title,
            page=request.page,
            items=[self.result_store.item],
            sites_attempted=("nyaa",),
            sites_succeeded=("nyaa",),
            site_item_counts={"nyaa": 1},
            site_queries={"nyaa": "Tefuda ga Oome no Victoria"},
            site_attempt_counts={"nyaa": 2},
            errors=[],
            partial=False,
            cached=False,
        )


class FakeStatusAdapter:
    capabilities = IndexerCapabilities(False, ("torrent",))

    def __init__(self, site_id, site_name):
        self.site_id = site_id
        self.site_name = site_name


class FakeStatusRegistry:
    def __init__(self):
        self.adapters = {
            "nyaa": FakeStatusAdapter("nyaa", "Nyaa"),
            "1lou": FakeStatusAdapter("1lou", "一楼"),
            "btbtla": FakeStatusAdapter("btbtla", "BTBTLA"),
            "sukebei": FakeStatusAdapter("sukebei", "Sukebei"),
        }

    def ids(self):
        return tuple(self.adapters)

    def get(self, site_id):
        return self.adapters[site_id]


class IndexerAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_patch = patch.object(app_config, "ENV_FILE", Path(self.temp.name) / "user.env")
        self.env_patch.start()
        self.cache_patch = patch.object(app_config, "_cache", {})
        self.cache_patch.start()
        self.os_patch = patch.dict(os.environ, {
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": "indexer-api-test-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
            "INDEXER_SEARCH_ENABLED": "1",
        }, clear=False)
        self.os_patch.start()
        from app.modules import first_run

        first_run._reset_startup_state_for_tests()
        self.request_lookup_patch = patch(
            "app.modules.indexer_download.db.get_download_request_by_request_key",
            return_value=None,
        )
        self.request_lookup_patch.start()
        self.request_alias_lookup_patch = patch(
            "app.modules.indexer_download.db.get_download_request_by_request_keys",
            return_value=None,
        )
        self.request_alias_lookup_patch.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.request_alias_lookup_patch.stop()
        self.request_lookup_patch.stop()
        self.os_patch.stop()
        from app.modules import first_run

        first_run._reset_startup_state_for_tests()
        self.cache_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _csrf(response):
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("missing csrf token")
        return match.group(1)

    def authenticate(self):
        login = self.client.get("/login")
        token = self._csrf(login)
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "123456"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/settings")
        return {"X-CSRF-Token": self._csrf(page)}

    def test_indexer_api_requires_login(self):
        self.assertEqual(self.client.get("/api/indexers/sites").status_code, 401)
        self.assertEqual(self.client.get("/api/indexers/search?q=demo").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["opaque-result"], "target": "qb"},
            ).status_code,
            401,
        )

    def test_sites_and_search_return_only_public_fields(self):
        self.authenticate()
        service = FakeIndexerService()
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            sites = self.client.get("/api/indexers/sites")
            search = self.client.get("/api/indexers/search?q=Demo&page=1&sites=nyaa")
        self.assertEqual(sites.status_code, 200)
        self.assertEqual(search.status_code, 200)
        payload = search.json()
        self.assertEqual(service.search_calls, [("Demo", 1, ["nyaa"])])
        self.assertEqual(service.search_sort_modes, ["relevance_desc"])
        self.assertEqual(payload["items"][0]["result_id"], "opaque-result")
        self.assertNotIn("magnet", payload["items"][0])
        self.assertNotIn("torrent_url", payload["items"][0])
        self.assertNotIn("detail_url", payload["items"][0])

    def test_get_search_forwards_requested_sort_mode(self):
        self.authenticate()
        service = FakeIndexerService()

        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            response = self.client.get(
                "/api/indexers/search?q=Demo&page=1&sites=nyaa&sort=published_desc"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.search_sort_modes, ["published_desc"])

    def test_public_search_items_include_safe_season_episode_sort_fields(self):
        item = IndexerItem(
            result_id="position-result",
            site_id="nyaa",
            site_name="Nyaa",
            title="Demo.Show.S02E11-E13.1080p.WEB-DL.mkv",
        )

        payload = indexers_api._public_search_item(item)

        self.assertEqual(payload["season"], 2)
        self.assertEqual(payload["episode"], 11)
        self.assertEqual(payload["episode_end"], 13)
        self.assertNotIn("magnet", payload)
        self.assertNotIn("torrent_url", payload)

    def test_structured_media_search_uses_post_route(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        body = {
            "title": " 奇招百出的维多利亚 ",
            "original_title": "手札が多めのビクトリア",
            "english_title": "Victoria of Many Faces",
            "aliases": ["Tefuda ga Oome no Victoria"],
            "year": 2026,
            "media_type": "tv",
            "sort_mode": "published_desc",
            "season": 2,
            "episode": 11,
            "page": 1,
            "sites": ["nyaa"],
        }

        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            response = self.client.post("/api/indexers/search", json=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        request, site_ids = service.media_search_calls[0]
        self.assertIsInstance(request, IndexerMediaSearchRequest)
        self.assertEqual(request.title, "奇招百出的维多利亚")
        self.assertEqual(request.year, 2026)
        self.assertEqual(request.sort_mode, "published_desc")
        self.assertEqual(request.season, 2)
        self.assertEqual(request.episode, 11)
        self.assertEqual(site_ids, ["nyaa"] )
        status = response.json()["site_statuses"][0]
        self.assertEqual(status["query"], "Tefuda ga Oome no Victoria")
        self.assertEqual(status["attempts"], 2)
        self.assertNotIn("magnet", response.json()["items"][0])

    def test_terminal_download_retries_only_missing_target_in_new_request(self):
        existing = {
            "id": 7, "status": "completed", "qb_status": "completed", "gy_status": "failed",
        }
        item = SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:" + "a" * 40, torrent_data=None)
        with patch("app.modules.indexer_download.db.get_download_request_by_request_key", return_value=existing), patch(
            "app.modules.indexer_download.create_request", return_value={"id": 8, "created": True}
        ) as create, patch("app.modules.indexer_download.dispatch_request", return_value={"ok": True}) as dispatch:
            created, request_id, _result = indexer_download._persist_and_dispatch(
                item,
                "indexer:nyaa",
                "both",
                chat_id="100",
                user_id="9",
                message_id="77",
            )
        self.assertTrue(created["created"])
        self.assertEqual(request_id, 8)
        create.assert_called_once_with(
            item, "100", "77", origin="indexer:nyaa", user_id="9"
        )
        dispatch.assert_called_once_with(8, "guangya")

    def test_manual_review_download_cannot_be_replayed_from_indexer(self):
        existing = {
            "id": 7,
            "status": "manual_review",
            "qb_status": "manual_review",
            "gy_status": "failed",
        }
        item = SimpleNamespace(
            kind="magnet",
            title="Demo",
            source_value="magnet:?xt=urn:btih:" + "a" * 40,
            torrent_data=None,
        )
        with patch(
            "app.modules.indexer_download.db.get_download_request_by_request_key",
            return_value=existing,
        ), patch(
            "app.modules.indexer_download.dispatch_missing_targets"
        ) as append, patch(
            "app.modules.indexer_download.create_request"
        ) as create, patch(
            "app.modules.indexer_download.dispatch_request"
        ) as dispatch:
            created, request_id, result = indexer_download._persist_and_dispatch(
                item, "indexer:nyaa", "guangya"
            )

        self.assertFalse(created["created"])
        self.assertEqual(request_id, 7)
        self.assertEqual(result["status"], "duplicate")
        append.assert_not_called()
        create.assert_not_called()
        dispatch.assert_not_called()

    def test_completed_duplicate_exposes_explicit_resubmit_action(self):
        existing = {
            "id": 7,
            "status": "completed",
            "qb_status": "",
            "gy_status": "completed",
        }
        item = SimpleNamespace(
            kind="magnet",
            title="Demo",
            source_value="magnet:?xt=urn:btih:" + "a" * 40,
            torrent_data=None,
        )
        capabilities = {
            "qb": {"enabled": False, "reason": ""},
            "guangya": {"enabled": True, "reason": ""},
            "both": {"enabled": False, "reason": ""},
        }
        with patch(
            "app.modules.indexer_download.db.get_download_request_by_request_key",
            return_value=existing,
        ), patch(
            "app.modules.indexer_download.download_resubmit_capabilities",
            return_value=capabilities,
        ), patch("app.modules.indexer_download.create_request") as create, patch(
            "app.modules.indexer_download.dispatch_request"
        ) as dispatch:
            created, request_id, result = indexer_download._persist_and_dispatch(
                item, "indexer:nyaa", "guangya"
            )

        self.assertFalse(created["created"])
        self.assertEqual(request_id, 7)
        self.assertEqual(result["existing_status"], "completed")
        self.assertTrue(result["can_resubmit"])
        self.assertEqual(result["resubmit_target"], "guangya")
        self.assertEqual(result["error"], "已有历史任务")
        create.assert_not_called()
        dispatch.assert_not_called()

    def test_cancelled_download_retries_only_missing_target(self):
        existing = {
            "id": 7,
            "status": "cancelled",
            "qb_status": "completed",
            "gy_status": "failed",
        }
        item = SimpleNamespace(
            kind="magnet",
            title="Demo",
            source_value="magnet:?xt=urn:btih:" + "b" * 40,
            torrent_data=None,
        )
        with patch(
            "app.modules.indexer_download.db.get_download_request_by_request_key",
            return_value=existing,
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 8, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={"ok": True},
        ) as dispatch:
            created, request_id, _result = indexer_download._persist_and_dispatch(
                item, "indexer:nyaa", "both"
            )

        self.assertTrue(created["created"])
        self.assertEqual(request_id, 8)
        dispatch.assert_called_once_with(8, "guangya")

    def test_public_source_url_rejects_malformed_port_without_raising(self):
        adapter = SimpleNamespace(http=SimpleNamespace(allowed_hosts={"nyaa.si"}))
        service = SimpleNamespace(registry=SimpleNamespace(get=lambda _site_id: adapter))
        malformed = IndexerItem(
            site_id="nyaa", site_name="Nyaa", title="Demo",
            detail_url="https://nyaa.si:99999/view/1",
        )
        self.assertIsNone(indexers_api._public_source_url(service, malformed))

    def test_search_uses_aggregated_has_more_instead_of_guessing_from_items(self):
        self.authenticate()
        service = FakeIndexerService()
        service.search = AsyncMock(return_value=SimpleNamespace(
            query="Demo",
            page=1,
            items=[service.result_store.item],
            sites_attempted=("nyaa",),
            sites_succeeded=("nyaa",),
            errors=[],
            partial=False,
            cached=False,
            has_more=False,
        ))

        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            response = self.client.get("/api/indexers/search?q=Demo&page=1&sites=nyaa")

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["has_more"], False)

    def test_search_exposes_server_derived_site_statuses_without_magnet(self):
        self.authenticate()
        service = SimpleNamespace(
            registry=FakeStatusRegistry(),
            enabled_site_ids=frozenset({"nyaa", "1lou", "btbtla"}),
            search=AsyncMock(return_value=AggregatedIndexerResult(
                query="Demo",
                page=1,
                items=[
                    IndexerItem(
                        result_id="opaque-result",
                        site_id="nyaa",
                        site_name="Nyaa",
                        title="Demo",
                    ),
                ],
                sites_attempted=("nyaa", "1lou", "btbtla"),
                sites_succeeded=("nyaa", "1lou"),
                errors=[IndexerProviderError("btbtla", "unavailable", "站点不可用")],
            )),
        )

        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            response = self.client.get("/api/indexers/search?q=Demo")

        self.assertEqual(response.status_code, 200)
        statuses = {item["site_id"]: item for item in response.json()["site_statuses"]}
        self.assertEqual(statuses["nyaa"]["status"], "success")
        self.assertEqual(statuses["1lou"]["status"], "empty")
        self.assertEqual(statuses["btbtla"]["status"], "error")
        self.assertEqual(statuses["btbtla"]["code"], "unavailable")
        self.assertIs(statuses["btbtla"]["retryable"], True)
        self.assertEqual(statuses["btbtla"]["message"], "站点暂不可用，请稍后重试")
        self.assertEqual(statuses["sukebei"]["status"], "disabled")
        self.assertIs(statuses["sukebei"]["retryable"], False)
        self.assertNotIn("magnet", response.text)

    def test_search_status_marks_btbtla_as_onelou_fallback(self):
        service = SimpleNamespace(
            registry=FakeStatusRegistry(),
            enabled_site_ids=frozenset({"btbtla", "1lou"}),
        )
        result = AggregatedIndexerResult(
            query="Demo",
            page=1,
            items=[],
            sites_attempted=("btbtla", "1lou"),
            sites_succeeded=("1lou",),
            site_item_counts={"1lou": 2},
            errors=[IndexerProviderError("btbtla", "unavailable", "索引站点暂不可用")],
            site_fallbacks={"btbtla": "1lou"},
            partial=True,
        )

        statuses = {row["site_id"]: row for row in indexers_api._search_site_statuses(service, result)}

        self.assertEqual(statuses["btbtla"]["status"], "fallback")
        self.assertEqual(statuses["btbtla"]["fallback_site_id"], "1lou")
        self.assertIn("1LOU", statuses["btbtla"]["message"] )

    def test_search_site_statuses_use_pre_dedupe_item_counts(self):
        self.authenticate()
        service = SimpleNamespace(
            registry=FakeStatusRegistry(),
            enabled_site_ids=frozenset({"nyaa", "1lou"}),
            search=AsyncMock(return_value=AggregatedIndexerResult(
                query="Demo",
                page=1,
                items=[
                    IndexerItem(
                        result_id="opaque-result",
                        site_id="nyaa",
                        site_name="Nyaa",
                        title="Shared hash",
                    ),
                ],
                sites_attempted=("nyaa", "1lou"),
                sites_succeeded=("nyaa", "1lou"),
                site_item_counts={"nyaa": 1, "1lou": 1},
            )),
        )

        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            response = self.client.get("/api/indexers/search?q=Demo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)
        statuses = {item["site_id"]: item for item in response.json()["site_statuses"]}
        self.assertEqual(statuses["nyaa"]["status"], "success")
        self.assertEqual(statuses["nyaa"]["count"], 1)
        self.assertEqual(statuses["1lou"]["status"], "success")
        self.assertEqual(statuses["1lou"]["count"], 1)

    def test_download_resolves_opaque_result_and_dispatches_magnet(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service), \
             patch("app.modules.indexer_download.normalize_download_url", return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None)) as normalize, \
             patch("app.modules.indexer_download.create_request", return_value={"id": 31, "created": True}) as create, \
             patch("app.modules.indexer_download.dispatch_request", return_value={"ok": True, "status": "submitted", "succeeded": ["qb"], "failed": []}) as dispatch:
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "qb"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        normalize.assert_called_once_with("magnet:?xt=urn:btih:" + "a" * 40)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["origin"], "indexer:nyaa")
        dispatch.assert_called_once_with(31, "qb")
        self.assertNotIn("magnet", response.text)

    def test_download_accepts_both_target(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb", "guangya"],
                "failed": [],
            },
        ) as dispatch:
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "both"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once_with(31, "both")

    def test_download_duplicate_returns_history_resubmit_contract(self):
        headers = self.authenticate()
        item = {
            "result_id": "opaque-result",
            "ok": False,
            "request_id": 31,
            "created": False,
            "target": "guangya",
            "status": "duplicate",
            "succeeded": [],
            "failed": [],
            "duplicate": True,
            "error": "已有历史任务",
            "existing_status": "completed",
            "can_resubmit": True,
            "resubmit_target": "guangya",
        }
        with patch(
            "app.routes.indexers_api._download_result",
            new=AsyncMock(return_value=item),
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "guangya"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["existing_status"], "completed")
        self.assertTrue(payload["can_resubmit"])
        self.assertEqual(payload["resubmit_target"], "guangya")

    def test_indexer_history_resubmit_dispatches_successor(self):
        headers = self.authenticate()
        result = {
            "ok": True,
            "status": "submitted",
            "request_id": 32,
            "created": True,
            "succeeded": ["guangya"],
            "failed": [],
            "duplicate": False,
            "error": "",
        }
        with patch(
            "app.routes.indexers_api.resubmit_indexer_download_request",
            return_value=result,
        ) as resubmit:
            response = self.client.post(
                "/api/indexers/download/resubmit",
                json={"request_id": 31, "target": "guangya"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_request_id"], 31)
        self.assertEqual(payload["request_id"], 32)
        resubmit.assert_called_once_with(31, "guangya")

    def test_download_both_reports_sanitized_partial_targets(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb", "internal-success"],
                "failed": ["guangya", "magnet:?xt=urn:btih:secret"],
                "error": (
                    "guangya: private https://indexer.example/details/1 "
                    "https://indexer.example/demo.torrent b'torrent-bytes'"
                ),
            },
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "both"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "partial")
        self.assertEqual(payload.get("succeeded"), ["qb"])
        self.assertEqual(payload.get("failed"), ["guangya"])
        self.assertEqual(payload["error"], "光鸭提交失败")
        self.assertNotIn("internal-success", response.text)
        self.assertNotIn("magnet:", response.text)
        self.assertNotIn(".torrent", response.text)
        self.assertNotIn("/details/", response.text)
        self.assertNotIn("torrent-bytes", response.text)

    def test_download_hides_internal_dispatch_errors(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": False,
                "status": "failed",
                "succeeded": [],
                "failed": ["qb"],
                "error": (
                    "RuntimeError: secret magnet:?xt=urn:btih:deadbeef "
                    "https://indexer.example/demo.torrent "
                    "https://indexer.example/details/1 b'torrent-bytes'"
                ),
            },
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "下载提交失败")
        self.assertNotIn("RuntimeError", response.text)
        self.assertNotIn("magnet:", response.text)
        self.assertNotIn(".torrent", response.text)
        self.assertNotIn("/details/", response.text)
        self.assertNotIn("torrent-bytes", response.text)

    def test_download_exposes_known_guangya_failure_without_internal_details(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": False,
                "status": "failed",
                "succeeded": [],
                "failed": ["guangya"],
                "error": (
                    "guangya: 资源中没有符合下载规则的文件：解析器标记为排除 "
                    "magnet:?xt=urn:btih:secret https://indexer.example/demo.torrent"
                ),
            },
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "guangya"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "光鸭未找到符合下载规则的文件")
        self.assertNotIn("magnet:", response.text)
        self.assertNotIn("indexer.example", response.text)
        self.assertNotIn("解析器标记为排除", response.text)

    def test_download_preserves_unknown_submission_as_manual_review(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": False,
                "status": "submitted",
                "succeeded": [],
                "failed": ["guangya"],
                "outcome_unknown": True,
                "review_required": True,
                "error": "guangya: timeout magnet:?xt=urn:btih:secret",
            },
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "guangya"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["status"], "manual_review")
        self.assertEqual(
            payload["error"],
            "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        )
        self.assertNotIn("timeout", response.text)
        self.assertNotIn("magnet:", response.text)

    def test_download_preserves_mixed_unknown_submission_as_manual_review(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(
                kind="magnet",
                title="Demo",
                source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                torrent_data=None,
            ),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": ["guangya"],
                "outcome_unknown": True,
                "review_required": True,
                "error": "guangya: timeout magnet:?xt=urn:btih:secret",
            },
        ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "both"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "manual_review")
        self.assertEqual(payload["succeeded"], ["qb"])
        self.assertEqual(payload["failed"], ["guangya"])
        self.assertEqual(
            payload["error"],
            "部分下载后端已提交，其余结果待核对，请先核对下载器，勿直接重复提交",
        )
        self.assertNotIn("timeout", response.text)
        self.assertNotIn("magnet:", response.text)

    def test_download_fetches_and_validates_torrent_server_side(self):
        headers = self.authenticate()
        resolved = ResolvedDownload(kind="torrent", value="https://nyaa.si/download/1.torrent", filename="demo.torrent")
        service = FakeIndexerService(resolved)
        service.adapter.http.get.return_value = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/x-bittorrent"},
            body=b"torrent-bytes",
        )
        item = SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None)
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service), \
             patch("app.modules.indexer_download.torrent_download_input", return_value=item) as torrent_input, \
             patch("app.modules.indexer_download.create_request", return_value={"id": 41, "created": True}), \
             patch("app.modules.indexer_download.dispatch_request", return_value={"ok": True, "status": "submitted", "succeeded": ["guangya"], "failed": []}):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "guangya"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        service.adapter.http.get.assert_awaited_once_with("https://nyaa.si/download/1.torrent")
        torrent_input.assert_called_once_with("demo.torrent", b"torrent-bytes")

    def test_download_accepts_provider_resolved_torrent_bytes_without_refetching(self):
        headers = self.authenticate()
        resolved = ResolvedDownload(kind="torrent", value=b"provider-torrent-bytes", filename="demo.torrent")
        service = FakeIndexerService(resolved)
        item = SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None)
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service), \
             patch("app.modules.indexer_download.torrent_download_input", return_value=item) as torrent_input, \
             patch("app.modules.indexer_download.create_request", return_value={"id": 42, "created": True}), \
             patch("app.modules.indexer_download.dispatch_request", return_value={"ok": True, "status": "submitted", "succeeded": ["qb"], "failed": []}):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        service.adapter.http.get.assert_not_awaited()
        torrent_input.assert_called_once_with("demo.torrent", b"provider-torrent-bytes")

    def test_download_rejects_oversized_provider_resolved_torrent_bytes(self):
        headers = self.authenticate()
        resolved = ResolvedDownload(kind="torrent", value=b"x" * 9, filename="large.torrent")
        service = FakeIndexerService(resolved)
        service.adapter.http.max_response_bytes = 8
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service), \
             patch("app.modules.indexer_download.torrent_download_input") as torrent_input, \
             patch(
                 "app.modules.indexer_download.create_request",
                 return_value={"id": 43, "created": True},
             ) as create, patch(
                 "app.modules.indexer_download.dispatch_request",
                 return_value={"ok": True},
             ):
            response = self.client.post(
                "/api/indexers/download",
                json={"result_id": "opaque-result", "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "response_too_large")
        service.adapter.http.get.assert_not_awaited()
        torrent_input.assert_not_called()
        create.assert_not_called()

    def test_download_rejects_expired_result_and_invalid_target(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch("app.routes.indexers_api.get_indexer_service", return_value=service):
            expired = self.client.post(
                "/api/indexers/download", json={"result_id": "expired", "target": "qb"}, headers=headers
            )
            invalid = self.client.post(
                "/api/indexers/download", json={"result_id": "opaque-result", "target": "aria2"}, headers=headers
            )
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(invalid.status_code, 400)

    def test_batch_download_returns_ordered_partial_summary(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": [],
            },
        ) as dispatch:
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["opaque-result", "expired", "opaque-result"], "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [item["result_id"] for item in payload["items"]],
            ["opaque-result", "expired"],
        )
        self.assertEqual(payload["summary"], {
            "total": 2,
            "succeeded": 1,
            "partial": 0,
            "review_required": 0,
            "failed": 1,
            "duplicate": 0,
        })
        self.assertTrue(payload["ok"])
        dispatch.assert_called_once_with(31, "qb")

    def test_batch_download_counts_real_both_partial_separately(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": ["guangya"],
            },
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["opaque-result"], "target": "both"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["status"], "partial")
        self.assertEqual(payload["summary"], {
            "total": 1,
            "succeeded": 0,
            "partial": 1,
            "review_required": 0,
            "failed": 0,
            "duplicate": 0,
        })

    def test_batch_download_counts_unknown_submission_as_review_required(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(
                kind="magnet",
                title="Demo",
                source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                torrent_data=None,
            ),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": False,
                "status": "submitted",
                "succeeded": [],
                "failed": ["guangya"],
                "outcome_unknown": True,
                "review_required": True,
                "error": "private timeout magnet:?xt=urn:btih:secret",
            },
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["opaque-result"], "target": "guangya"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["items"][0]["status"], "manual_review")
        self.assertEqual(payload["summary"], {
            "total": 1,
            "succeeded": 0,
            "partial": 0,
            "review_required": 1,
            "failed": 0,
            "duplicate": 0,
        })
        self.assertEqual(
            payload["items"][0]["error"],
            "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        )
        self.assertNotIn("private timeout", response.text)
        self.assertNotIn("magnet:", response.text)

    def test_batch_download_limits_concurrency_to_three(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        active = 0
        max_active = 0

        async def resolve(result_id):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return service.adapter.resolved

        service.resolve = resolve
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": [],
            },
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": [f"result-{index}" for index in range(6)], "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(max_active, 3)
        self.assertEqual(
            [item["result_id"] for item in response.json()["items"]],
            [f"result-{index}" for index in range(6)],
        )

    def test_sync_dispatch_runs_off_loop_so_ticker_keeps_running(self):
        service = FakeIndexerService()
        dispatch_started = threading.Event()
        dispatch_finished = threading.Event()
        ticks_during_dispatch = 0

        def blocking_dispatch(request_id, target):
            dispatch_started.set()
            time.sleep(0.08)
            dispatch_finished.set()
            return {
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": [],
            }

        async def scenario():
            async def ticker():
                nonlocal ticks_during_dispatch
                while not dispatch_finished.is_set():
                    await asyncio.sleep(0.005)
                    if dispatch_started.is_set() and not dispatch_finished.is_set():
                        ticks_during_dispatch += 1

            ticker_task = asyncio.create_task(ticker())
            try:
                with patch(
                    "app.modules.indexer_download.normalize_download_url",
                    return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
                ), patch(
                    "app.modules.indexer_download.db.get_download_request_by_request_key",
                    return_value=None,
                ), patch(
                    "app.modules.indexer_download.create_request",
                    return_value={"id": 31, "created": True},
                ), patch(
                    "app.modules.indexer_download.dispatch_request",
                    side_effect=blocking_dispatch,
                ):
                    result = await indexers_api._download_result(service, "opaque-result", "qb")
            finally:
                await ticker_task
            return result

        result = asyncio.run(scenario())

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(ticks_during_dispatch, 3)

    def test_two_concurrent_batch_lifecycles_share_total_capacity_three(self):
        service = FakeIndexerService()
        state_lock = threading.Lock()
        request_ids = iter(range(100, 108))
        active = 0
        max_active = 0

        async def resolve(result_id):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.03)
            return service.adapter.resolved

        def dispatch(request_id, target):
            nonlocal active
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return {
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": [],
            }

        service.resolve = resolve

        async def scenario():
            async def run_batch(prefix):
                return await asyncio.gather(*(
                    indexers_api._download_result_public(service, f"{prefix}-{index}", "qb")
                    for index in range(4)
                ))

            with patch(
                "app.modules.indexer_download.normalize_download_url",
                return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
            ), patch(
                "app.modules.indexer_download.create_request",
                side_effect=lambda *args, **kwargs: {"id": next(request_ids), "created": True},
            ), patch(
                "app.modules.indexer_download.dispatch_request",
                side_effect=dispatch,
            ):
                return await asyncio.gather(run_batch("first"), run_batch("second"))

        batches = asyncio.run(scenario())

        self.assertEqual([len(batch) for batch in batches], [4, 4])
        self.assertLessEqual(max_active, 3)

    def test_batch_download_rejects_invalid_payload_boundaries(self):
        headers = self.authenticate()
        invalid_payloads = [
            None,
            [],
            {"result_ids": [], "target": "qb"},
            {"result_ids": "opaque-result", "target": "qb"},
            {"result_ids": [f"result-{index}" for index in range(51)], "target": "qb"},
            {"result_ids": ["x" * 129], "target": "qb"},
            {"result_ids": [""], "target": "qb"},
            {"result_ids": [31], "target": "qb"},
            {"result_ids": ["opaque-result"], "target": "aria2"},
        ]

        with patch("app.routes.indexers_api.get_indexer_service") as get_service:
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(
                        "/api/indexers/download/batch",
                        json=payload,
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 400)
            get_service.assert_not_called()

    def test_batch_download_accepts_fifty_unique_ids_after_deduplication(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        result_ids = [f"result-{index}" for index in range(50)] + ["result-0"]
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": True,
                "status": "submitted",
                "succeeded": ["qb"],
                "failed": [],
            },
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": result_ids, "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["total"], 50)
        self.assertEqual(
            [item["result_id"] for item in response.json()["items"]],
            result_ids[:50],
        )

    def test_batch_download_counts_dispatch_duplicates_separately(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            side_effect=[
                {
                    "ok": True,
                    "status": "submitted",
                    "succeeded": ["qb"],
                    "failed": [],
                },
                {
                    "ok": False,
                    "duplicate": True,
                    "error": "internal duplicate claim detail",
                },
            ],
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["one", "two"], "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], {
            "total": 2,
            "succeeded": 1,
            "partial": 0,
            "review_required": 0,
            "failed": 0,
            "duplicate": 1,
        })
        duplicate_item = next(item for item in response.json()["items"] if item["status"] == "duplicate")
        self.assertEqual(duplicate_item["error"], "该下载请求已提交或正在处理")
        self.assertNotIn("internal duplicate claim detail", response.text)

    def test_batch_download_contains_invalid_data_and_unexpected_exceptions(self):
        headers = self.authenticate()
        invalid_service = FakeIndexerService(ResolvedDownload(
            kind="torrent",
            value=b"not-a-valid-torrent",
            filename="secret.torrent",
        ))
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=invalid_service,
        ):
            invalid = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["invalid"], "target": "qb"},
                headers=headers,
            )

        exploding_service = FakeIndexerService()
        exploding_service.resolve = AsyncMock(
            side_effect=RuntimeError(
                "private https://indexer.example/details/1 magnet:?xt=urn:btih:secret"
            )
        )
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=exploding_service,
        ):
            unexpected = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["explode"], "target": "qb"},
                headers=headers,
            )

        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.json()["items"][0]["error"], "资源下载数据无效")
        self.assertNotIn("not-a-valid-torrent", invalid.text)
        self.assertNotIn("secret.torrent", invalid.text)
        self.assertEqual(unexpected.status_code, 200)
        self.assertEqual(unexpected.json()["items"][0]["error"], "下载处理失败")
        self.assertNotIn("/details/", unexpected.text)
        self.assertNotIn("magnet:", unexpected.text)

    def test_batch_download_exposes_only_public_fields_and_hides_internal_errors(self):
        headers = self.authenticate()
        service = FakeIndexerService()
        internal_error = (
            "RuntimeError: secret magnet:?xt=urn:btih:deadbeef "
            "https://indexer.example/demo.torrent "
            "https://indexer.example/details/1 b'torrent-bytes'"
        )
        with patch(
            "app.routes.indexers_api.get_indexer_service",
            return_value=service,
        ), patch(
            "app.modules.indexer_download.normalize_download_url",
            return_value=SimpleNamespace(kind="magnet", title="Demo", source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", torrent_data=None),
        ), patch(
            "app.modules.indexer_download.create_request",
            return_value={"id": 31, "created": True},
        ), patch(
            "app.modules.indexer_download.dispatch_request",
            return_value={
                "ok": False,
                "status": "failed",
                "succeeded": [],
                "failed": ["qb"],
                "duplicate": False,
                "error": internal_error,
            },
        ):
            response = self.client.post(
                "/api/indexers/download/batch",
                json={"result_ids": ["opaque-result"], "target": "qb"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"], {
            "total": 1,
            "succeeded": 0,
            "partial": 0,
            "review_required": 0,
            "failed": 1,
            "duplicate": 0,
        })
        self.assertEqual(set(payload["items"][0]), {
            "result_id",
            "ok",
            "request_id",
            "created",
            "target",
            "status",
            "succeeded",
            "failed",
            "duplicate",
            "error",
        })
        self.assertEqual(payload["items"][0]["error"], "下载提交失败")
        self.assertNotIn("RuntimeError", response.text)
        self.assertNotIn("magnet:", response.text)
        self.assertNotIn(".torrent", response.text)
        self.assertNotIn("/details/", response.text)
        self.assertNotIn("torrent-bytes", response.text)


if __name__ == "__main__":
    unittest.main()
