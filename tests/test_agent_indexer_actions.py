"""Media Agent 多站资源搜索与确认提交测试。"""

from __future__ import annotations
from typing import Any

import asyncio
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.agent.domains.resources.candidate_actions import (
    present_candidates,
    present_candidates_arguments,
)
from app.agent.domains.resources.indexer_actions import (
    _submit_resource,
    _submit_resource_batch,
    prepare_submit_resource,
    search_arguments,
    search_resources,
)
from app.agent.errors import AgentToolError
from app.indexers.downloads import download_indexer_result
from app.indexers.errors import (
    IndexerChallengeRequired,
    IndexerQueryRejected,
    IndexerResultExpired,
)
from app.indexers.models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerProviderError,
    ResolvedDownload,
)
from app.indexers.providers.empire import EmpireAdapter
from app.modules.download_dispatcher import DownloadInput

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
    def __init__(
        self,
        *,
        item: IndexerItem | None = None,
        items: list[IndexerItem] | None = None,
        enabled_site_ids: tuple[str, ...] = ("nyaa",),
        sites_attempted: tuple[str, ...] = ("nyaa", "broken"),
        sites_succeeded: tuple[str, ...] = ("nyaa",),
        partial: bool = True,
    ):
        self.item = item or _resource_item()
        self.items = list(items) if items is not None else [self.item]
        self.result_store = FakeResultStore(self.item)
        self.enabled_site_ids = frozenset(enabled_site_ids)
        self.sites_attempted = sites_attempted
        self.sites_succeeded = sites_succeeded
        self.partial = partial
        self.search_calls: list[tuple[IndexerMediaSearchRequest, list[str] | None]] = []

    def site_ids_for_scope(self, scope="manual"):
        assert scope == "agent"
        return tuple(self.enabled_site_ids)

    def get_result(self, result_id, *, scope="manual"):
        assert scope == "agent"
        return self.result_store.get(result_id)

    async def search_media(self, request, sites=None, *, scope="manual", timeout_seconds=None):
        assert scope == "agent"
        self.search_calls.append((request, sites))
        return AggregatedIndexerResult(
            query=request.title,
            page=request.page,
            items=self.items,
            sites_attempted=self.sites_attempted,
            sites_succeeded=self.sites_succeeded,
            errors=(
                [IndexerProviderError("broken", "unavailable", "站点暂不可用")]
                if self.partial
                else []
            ),
            partial=self.partial,
            cached=False,
            has_more=True,
        )


class FakeDownloadService:
    def __init__(self, item: IndexerItem):
        self.result_store = FakeResultStore(item)

    def get_result(self, result_id, *, scope="manual"):
        return self.result_store.get(result_id)

    async def resolve(self, _result_id: str, *, scope="manual") -> ResolvedDownload:
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
        "size_bytes": 1200000000,
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


def _candidate_snapshot(*, count=2) -> dict[str, Any]:
    candidates = []
    for position in range(1, count + 1):
        candidates.append(
            {
                "position": position,
                "result_id": f"opaque-result-{position:04d}",
                "title": f"Demo Resource {position}",
                "site_id": "nyaa",
                "site_name": "Nyaa",
                "size_text": "1.2 GB",
                "download_state": "ready",
                "_verification_context": None,
                "download_kinds": ["magnet"],
                "media_title": "",
                "episode_label": "",
                "subscription_number": None,
            }
        )
    return {
        "search_id": "rs_" + "a" * 16,
        "search_status": "success",
        "candidates": candidates,
        "_private_version": 1,
        "_private_items": [
            {"magnet": _SECRET_MAGNET, "torrent_url": _SECRET_TORRENT_URL}
        ],
    }


def _identity(arguments):
    return dict(arguments)


class AgentIndexerActionUnitTests(unittest.TestCase):
    def test_search_arguments_normalize_and_reject_unsafe_fields(self):
        service = FakeIndexerService()
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            normalized = search_arguments(
                {
                    "title": "  Demo  ",
                    "aliases": ["Alias"],
                    "year": 2026,
                    "media_type": "ANIME",
                    "season": 1,
                    "episode": 192,
                    "sites": ["NYAA", "nyaa"],
                    "sort_mode": "PUBLISHED_DESC",
                    "limit": 12,
                }
            )
        self.assertEqual(normalized["title"], "Demo")
        self.assertEqual(normalized["media_type"], "anime")
        self.assertEqual(normalized["sites"], ["nyaa"])
        self.assertEqual(normalized["sort_mode"], "published_desc")
        self.assertEqual((normalized["season"], normalized["episode"]), (1, 192))
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
            {"title": "Demo", "sort_mode": "newest"},
            {"title": "Demo", "season": True},
            {"title": "Demo", "season": -1},
            {"title": "Demo", "season": 101},
            {"title": "Demo", "episode": 0},
            {"title": "Demo", "episode": 1001},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(AgentToolError):
                search_arguments(payload)

    def test_search_arguments_reject_site_absent_from_agent_registry(self):
        service = FakeIndexerService()
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            self.assertRaises(AgentToolError) as rejected,
        ):
            search_arguments({"title": "Demo", "sites": ["mikan"]})
        self.assertIn("未启用", rejected.exception.safe_message)

    def test_disabled_sukebei_does_not_suggest_enabling_all_sites(self):
        service = FakeIndexerService()
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            self.assertRaises(AgentToolError) as rejected,
        ):
            search_arguments(
                {
                    "title": "uncensored",
                    "sites": ["sukebei"],
                    "sort_mode": "published_desc",
                }
            )

        self.assertEqual(rejected.exception.code, "precondition_failed")
        self.assertIn("单独授权成人来源", rejected.exception.safe_message)
        self.assertNotIn("启用所有", rejected.exception.safe_message)

    def test_recent_adult_search_is_restricted_to_sukebei_and_sorted_by_date(self):
        item = _resource_item(
            site_id="sukebei",
            site_name="Sukebei",
            category="Real Life",
        )
        service = FakeIndexerService(
            item=item,
            enabled_site_ids=("sukebei",),
            sites_attempted=("sukebei",),
            sites_succeeded=("sukebei",),
            partial=False,
        )
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            arguments = search_arguments(
                {
                    "title": "uncensored",
                    "sites": ["sukebei"],
                    "sort_mode": "published_desc",
                    "limit": 10,
                }
            )
            result = search_resources(arguments)

        request, sites = service.search_calls[0]
        self.assertEqual(sites, ["sukebei"])
        self.assertEqual(request.title, "uncensored")
        self.assertEqual(request.sort_mode, "published_desc")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["sort_mode"], "published_desc")
        self.assertEqual(result.data["sites_attempted"], ["sukebei"])
        self.assertEqual(result.data["sites_succeeded"], ["sukebei"])
        self.assertEqual(result.data["items"][0]["site_id"], "sukebei")

    def test_search_reports_service_and_tool_caps_without_site_failure(self):
        service = FakeIndexerService(partial=False)
        for limit_source in ("service", "tool", "provider"):
            with self.subTest(limit_source=limit_source):
                result = AggregatedIndexerResult(query="Demo", page=1, items=[service.item],
                                                 sites_attempted=("nyaa",), sites_succeeded=("nyaa",))
                if limit_source == "service":
                    result.site_collected_counts = {"nyaa": 60}
                    result.site_truncated_counts = {"nyaa": 20}
                elif limit_source == "provider":
                    result.errors = [IndexerProviderError("nyaa", "coverage_limited", "本轮仅检索 3/15 个作品候选")]
                    result.partial = True
                else:
                    result.items = [service.item, service.item]
                with (
                    patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
                    patch("app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service),
                    patch.object(service, "search_media", new=AsyncMock(return_value=result)),
                ):
                    output = search_resources({"title": "Demo", "original_title": "", "english_title": "",
                                               "aliases": [], "year": None, "media_type": "",
                                               "page": 1, "sites": ["nyaa"], "limit": 1})
                self.assertTrue(output.ok)
                self.assertEqual(output.status, "partial" if limit_source == "provider" else "success")
                self.assertTrue(output.data["truncated"])
                self.assertIn("未覆盖部分仍未知", output.summary)
                if limit_source == "provider":
                    self.assertEqual(output.data["errors"][0]["code"], "coverage_limited")
                    self.assertIn("3/15", output.data["errors"][0]["message"])

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
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            result = search_resources(arguments)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["items"][0]["result_id"], _RESULT_ID)
        self.assertEqual(result.data["items"][0]["position"], 1)
        self.assertEqual(service.search_calls[0][1], ["nyaa"])
        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0].kind, "resource_candidates")
        self.assertEqual(
            result.references[0].value["candidates"][0]["result_id"],
            _RESULT_ID,
        )
        serialized = str(result.to_dict())
        self.assertNotIn("references", serialized)
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)
        self.assertNotIn(_SECRET_DETAIL_URL, serialized)

    def test_search_resources_propagates_safe_query_errors_and_keeps_candidates(self):
        private_html = "<html>private cookie=secret; upstream diagnostic</html>"
        rejected = IndexerQueryRejected(private_html)
        challenge = IndexerChallengeRequired(private_html)
        detail_error = EmpireAdapter(
            site_id="ys5266",
            site_name="5266影视",
            base_url="https://www.5266ys.net/",
            http=object(),
        )._detail_error(challenge)
        self.assertEqual(detail_error.site_id, "ys5266")

        service = FakeIndexerService(partial=False)
        result = AggregatedIndexerResult(
            query="Demo",
            page=1,
            items=[service.item],
            sites_attempted=("nyaa", "dygang", "ys5266"),
            sites_succeeded=("nyaa",),
            site_item_counts={"nyaa": 1},
            errors=[
                IndexerProviderError("dygang", rejected.code, rejected.public_message),
                detail_error,
            ],
            partial=True,
        )
        arguments = {
            "title": "Demo",
            "original_title": "",
            "english_title": "",
            "aliases": [],
            "year": None,
            "media_type": "",
            "page": 1,
            "sites": ["nyaa", "dygang", "ys5266"],
            "limit": 20,
        }
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch("app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service),
            patch.object(service, "search_media", new=AsyncMock(return_value=result)),
        ):
            output = search_resources(arguments)

        self.assertTrue(output.ok)
        self.assertEqual(output.status, "partial")
        self.assertEqual(output.data["items"][0]["title"], service.item.title)
        self.assertEqual(
            [(error["site_id"], error["code"]) for error in output.data["errors"]],
            [("dygang", "query_rejected"), ("ys5266", "challenge_required")],
        )
        self.assertEqual(output.data["errors"][0]["message"], rejected.public_message)
        self.assertEqual(output.data["errors"][1]["message"], detail_error.message)
        self.assertIn("关键词", output.data["errors"][0]["message"])
        self.assertTrue(
            any(term in output.data["errors"][0]["message"] for term in ("片名", "别名"))
        )
        self.assertNotIn("缩短", output.data["errors"][0]["message"])
        self.assertIn("人机验证", output.data["errors"][1]["message"])
        serialized = str(output.to_dict())
        self.assertNotIn(private_html, serialized)
        self.assertNotIn("btbtla", serialized)

    def test_present_candidates_is_read_only_keeps_original_positions_and_hides_private_data(self):
        snapshot = _candidate_snapshot()
        with (
            patch(
                "app.agent.domains.resources.candidate_actions.restore_resource_candidate_reference"
            ) as restore,
            patch(
                "app.agent.domains.resources.candidate_actions.active_agent_resource_candidates"
            ) as active,
        ):
            result = present_candidates(
                {"resource_candidates": snapshot, "positions": [2, 1]}
            )

        restore.assert_not_called()
        active.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "found")
        self.assertEqual(result.summary, "已筛选2项资源候选")
        self.assertEqual(result.data, {"positions": [2, 1]})
        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0].kind, "resource_candidates")
        self.assertIs(result.references[0].value, snapshot)
        self.assertEqual(
            [item["position"] for item in result.references[0].value["candidates"]],
            [1, 2],
        )
        serialized = str(result.to_dict())
        self.assertNotIn("_private_items", serialized)
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)

    def test_present_candidates_rejects_invalid_positions(self):
        reference = "ref_" + "a" * 16
        for positions in ([0], [51], [1, 1], None, "all"):
            with self.subTest(positions=positions), self.assertRaises(AgentToolError):
                present_candidates_arguments(
                    {"resource_candidates_ref": reference, "positions": positions}
                )

        with self.assertRaisesRegex(AgentToolError, "不存在"):
            present_candidates(
                {"resource_candidates": _candidate_snapshot(count=1), "positions": [2]}
            )

    def test_present_candidates_empty_is_explicit_clear_and_keeps_reference(self):
        snapshot = _candidate_snapshot()
        result = present_candidates(
            {"resource_candidates": snapshot, "positions": []}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data, {"positions": []})
        self.assertEqual(len(result.references), 1)
        self.assertIs(result.references[0].value, snapshot)

    def test_search_candidate_positions_skip_unavailable_items_without_gaps(self):
        unavailable = _resource_item(
            result_id="unavailable-result-1234",
            title="Unavailable",
            download_state="unavailable",
            download_kinds=(),
        )
        first = _resource_item(result_id="eligible-result-0001", title="First")
        second = _resource_item(result_id="eligible-result-0002", title="Second")
        service = FakeIndexerService(items=[unavailable, first, second])
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
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            result = search_resources(arguments)
        self.assertNotIn("position", result.data["items"][0])
        self.assertEqual(
            [item.get("position") for item in result.data["items"]], [None, 1, 2]
        )

    def test_search_resources_enforces_caller_timeout(self):
        service = FakeIndexerService()

        async def slow_search(_request, _sites=None, *, scope="agent", timeout_seconds=None):
            await asyncio.sleep(0.05)
            raise AssertionError("timeout should cancel the slow search")

        self.enterContext(patch.object(service, "search_media", slow_search))
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
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            result = search_resources(arguments, timeout_seconds=0.001)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.error, "资源检索超时。")

    def test_search_resources_disabled_does_not_create_service(self):
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=False),
            patch("app.agent.domains.resources.indexer_actions.get_indexer_service") as service_factory,
        ):
            result = search_resources({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        service_factory.assert_not_called()

    def test_preview_is_safe_and_requires_ready_target(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch(
                "app.agent.domains.resources.indexer_actions.download_target_readiness",
                return_value={"qb": True},
            ),
        ):
            result, _context = prepare_submit_resource(arguments)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "confirmation_required")
        serialized = str(result.to_dict())
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)
        self.assertNotIn(_SECRET_DETAIL_URL, serialized)
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch(
                "app.agent.domains.resources.indexer_actions.download_target_readiness",
                return_value={"qb": False},
            ),
        ):
            unavailable, _context = prepare_submit_resource(arguments)
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "not_configured")

    def test_preview_rejects_expired_and_non_downloadable_results(self):
        service = FakeIndexerService()
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
        ):
            expired, _context = prepare_submit_resource(
                {"result_id": "expired-result-1234", "target": "qb"}
            )
            service.result_store.item = _resource_item(
                download_state="unavailable", download_kinds=()
            )
            blocked, _context = prepare_submit_resource(
                {"result_id": _RESULT_ID, "target": "qb"}
            )
        self.assertFalse(expired.ok)
        self.assertEqual(expired.status, "result_expired")
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "validation_error")

    def test_preview_expires_legacy_combined_ticket_without_rebinding_child_site(self):
        service = FakeIndexerService(
            item=_resource_item(
                site_id="btbtla",
                site_name="综合",
                detail_url="https://www.aipan.me/movie/legacy-resource",
            ),
            enabled_site_ids=("btbtla", "aipan"),
        )
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch("app.agent.domains.resources.indexer_actions.download_target_readiness") as readiness,
        ):
            expired, _context = prepare_submit_resource(
                {"result_id": _RESULT_ID, "target": "qb"}
            )

        self.assertFalse(expired.ok)
        self.assertEqual(expired.status, "result_expired")
        assert expired.error is not None
        self.assertIn("旧资源确认已过期，请重新搜索", expired.error)
        readiness.assert_not_called()

    def test_confirmation_context_changes_when_resource_changes(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch(
                "app.agent.domains.resources.indexer_actions.download_target_readiness",
                return_value={"qb": True},
            ),
        ):
            first = prepare_submit_resource(arguments)[1]
            service.result_store.item = _resource_item(title="Changed Resource")
            second = prepare_submit_resource(arguments)[1]
        self.assertNotEqual(first, second)
        self.assertNotIn("Changed", first + second)

    def test_submit_resource_returns_sanitized_public_result(self):
        service = FakeIndexerService()
        dispatch = AsyncMock(
            return_value={
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
            }
        )
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch("app.agent.domains.resources.indexer_actions.download_indexer_result", dispatch),
        ):
            result = _submit_resource({"result_id": _RESULT_ID, "target": "both"})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            set(result.data),
            {
                "result_id",
                "request_id",
                "created",
                "target",
                "status",
                "succeeded",
                "failed",
                "duplicate",
                "ok",
                "error",
            },
        )
        self.assertNotIn("private backend error", str(result.to_dict()))
        self.assertNotIn("secret", str(result.to_dict()))
        dispatch.assert_awaited_once_with(
            service, _RESULT_ID, "both", origin_namespace="agent", scope="agent"
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
            with (
                self.subTest(expected_status=expected_status),
                patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.domains.resources.indexer_actions.get_indexer_service",
                    return_value=service,
                ),
                patch(
                    "app.agent.domains.resources.indexer_actions.download_indexer_result",
                    AsyncMock(
                        return_value={**payload, "error": "private backend detail"}
                    ),
                ),
            ):
                result = _submit_resource({"result_id": _RESULT_ID, "target": "qb"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, expected_status)
            self.assertNotIn("private backend detail", str(result.to_dict()))
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch(
                "app.agent.domains.resources.indexer_actions.download_indexer_result",
                AsyncMock(side_effect=RuntimeError("secret internal error")),
            ),
        ):
            internal = _submit_resource({"result_id": _RESULT_ID, "target": "qb"})
        self.assertFalse(internal.ok)
        self.assertEqual(internal.status, "unavailable")
        self.assertNotIn("secret internal error", str(internal.to_dict()))

    def test_submit_resource_preserves_manual_review_without_private_error(self):
        service = FakeIndexerService()
        dispatch = AsyncMock(
            return_value={
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
            }
        )
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch("app.agent.domains.resources.indexer_actions.download_indexer_result", dispatch),
        ):
            result = _submit_resource({"result_id": _RESULT_ID, "target": "guangya"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.summary, "下载任务提交结果待核对")
        self.assertNotIn("private timeout", str(result.to_dict()))
        self.assertNotIn("magnet:", str(result.to_dict()))

    def test_single_resource_preserves_safe_guangya_rejection_reason(self):
        with patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.domains.resources.indexer_actions.download_indexer_result", AsyncMock(return_value={
                "ok": False, "result_id": _RESULT_ID, "request_id": 88, "created": True,
                "target": "guangya", "status": "failed", "succeeded": [], "failed": ["guangya"],
                "error": "上游文件违规 https://private.example/token=SECRET", "duplicate": False,
            })
        ):
            result = _submit_resource({"result_id": _RESULT_ID, "target": "guangya"}, service=FakeIndexerService())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "光鸭返回：文件违规")
        self.assertEqual(result.data["error"], result.error)
        self.assertEqual(result.data["request_id"], 88)
        self.assertNotIn("SECRET", str(result.to_dict()))
        self.assertNotIn("https://", str(result.to_dict()))

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
        with (
            patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=service
            ),
            patch("app.agent.domains.resources.indexer_actions.download_indexer_result_public", dispatch),
        ):
            result = _submit_resource_batch(
                {"result_ids": result_ids, "target": "guangya"}
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["review_required"], 2)
        self.assertEqual(result.data["failed"], 0)
        assert result.error is not None
        self.assertIn("勿直接重复提交", result.error)
        self.assertEqual(dispatch.await_count, 2)

    def test_shared_download_service_dispatches_only_server_resolved_value(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = DownloadInput(kind="magnet", title="磁力任务", source_value=_SECRET_MAGNET)
        with (
            patch(
                "app.indexers.downloads.normalize_download_url", return_value=normalized
            ) as normalize,
            patch("app.indexers.downloads.request_keys", return_value=("req:key",)),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_key",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_keys",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.create_request",
                return_value={"id": 7, "created": True},
            ) as create,
            patch(
                "app.indexers.downloads.dispatch_request",
                return_value={"status": "submitted", "succeeded": ["qb"], "failed": []},
            ) as dispatch,
        ):
            result = asyncio.run(download_indexer_result(service, _RESULT_ID, "qb"))
        normalize.assert_called_once_with(_SECRET_MAGNET)
        create.assert_called_once_with(
            replace(normalized, display_title=item.title), "", "", origin="indexer:nyaa", user_id=""
        )
        dispatch.assert_called_once_with(7, "qb")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "submitted")

    def test_shared_download_service_marks_agent_origin_namespace(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = DownloadInput(kind="magnet", title="磁力任务", source_value=_SECRET_MAGNET)
        with (
            patch(
                "app.indexers.downloads.normalize_download_url", return_value=normalized
            ),
            patch("app.indexers.downloads.request_keys", return_value=("req:key",)),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_key",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_keys",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.create_request",
                return_value={"id": 8, "created": True},
            ) as create,
            patch(
                "app.indexers.downloads.dispatch_request",
                return_value={"status": "submitted", "succeeded": ["qb"], "failed": []},
            ),
        ):
            result = asyncio.run(
                download_indexer_result(
                    service, _RESULT_ID, "qb", origin_namespace="agent"
                )
            )
        create.assert_called_once_with(
            replace(normalized, display_title=item.title), "", "", origin="agent:nyaa", user_id=""
        )
        self.assertTrue(result["ok"])


class IndexerReceivingSourceTests(unittest.TestCase):
    """合成资源，仅验证来源路由，不访问索引站或下载后端。"""

    def test_classified_cloud_resource_uses_configured_source_on_shared_dispatch(self):
        for site, target, routed in (("sukebei", "guangya", True), ("sukebei", "both", True),
                                     ("sukebei", "qb", False), ("nyaa", "guangya", False)):
            with (self.subTest(site=site, target=target),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "source-123", "name": "FixtureSpecial"}]),
                patch("app.indexers.downloads.submit_download_input", return_value={
                    "request_id": 7, "created": True, "summary": {"duplicate": False,
                    "succeeded": [target], "failed": [], "status": "submitted", "error": ""},
                }) as submit,
            ):
                result = asyncio.run(download_indexer_result(
                    FakeDownloadService(_resource_item(site_id=site)), _RESULT_ID, target,
                    origin_namespace="agent", scope="agent",
                ))
                self.assertTrue(result["ok"])
                self.assertEqual(submit.call_args.kwargs.get("gy_target_dir", ""), "source-123" if routed else "")
                self.assertEqual(submit.call_args.kwargs.get("gy_target_name", ""), "FixtureSpecial" if routed else "")

    def test_missing_or_ambiguous_source_never_falls_back_to_normal_inbox(self):
        from app.indexers.downloads import download_indexer_result_public
        for sources in ([], [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]):
            with (self.subTest(sources=sources),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=sources),
                patch("app.indexers.downloads.submit_download_input") as submit,
            ):
                result = asyncio.run(download_indexer_result_public(
                    FakeDownloadService(_resource_item(site_id="sukebei")), _RESULT_ID, "guangya", scope="agent",
                ))
                self.assertFalse(result["ok"])
                self.assertTrue(result["pre_submission_failure"])
                self.assertIn("NSFW", result["error"])
                submit.assert_not_called()

    def test_single_and_batch_confirmation_bind_actual_receiving_source(self):
        from app.agent.domains.resources.indexer_actions import (
            prepare_submit_resource_batch,
            submit_resource_batch_confirmed,
            submit_resource_confirmed,
        )
        for batch in (False, True):
            with (self.subTest(batch=batch),
                patch("app.agent.domains.resources.indexer_actions.config.get_bool", return_value=True),
                patch("app.agent.domains.resources.indexer_actions.get_indexer_service", return_value=FakeIndexerService(item=_resource_item(site_id="sukebei"))),
                patch("app.agent.domains.resources.indexer_actions.download_target_readiness", return_value={"guangya": True}),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "source-123", "name": "FixtureSpecial"}]) as sources,
                patch("app.agent.domains.resources.indexer_actions._submit_resource") as single,
                patch("app.agent.domains.resources.indexer_actions._submit_resource_batch") as multiple,
            ):
                arguments = {"result_ids": [_RESULT_ID], "target": "guangya"} if batch else {"result_id": _RESULT_ID, "target": "guangya"}
                preview, token = (prepare_submit_resource_batch(arguments) if batch
                                  else prepare_submit_resource({"result_id": _RESULT_ID, "target": "guangya"}))
                self.assertTrue(preview.ok)
                resources = preview.data["resources"] if batch else [preview.data["resource"]]
                self.assertEqual(resources[0]["receiving_folder"], "FixtureSpecial")
                self.assertNotIn("source-123", str(preview.to_dict()))
                sources.return_value = [{"id": "different-source", "name": "FixtureSpecial"}]
                with self.assertRaises(AgentToolError) as caught:
                    if batch:
                        submit_resource_batch_confirmed(arguments, token)
                    else:
                        submit_resource_confirmed({"result_id": _RESULT_ID, "target": "guangya"}, token)
                self.assertEqual(caught.exception.code, "confirmation_stale")
                single.assert_not_called()
                multiple.assert_not_called()

    def test_receiving_summary_combines_selected_routes_without_false_default(self):
        from app.agent.domains.resources.ingest_actions import _receiving_folders
        from app.modules.offline import OfflineRules
        with patch("app.modules.offline.OfflineRules.from_config", return_value=replace(OfflineRules.from_config(), target_dir_id="normal", target_dir_name="Inbox", secondary_enabled=False)):
            self.assertEqual(_receiving_folders("guangya", []), _receiving_folders("guangya", [{}]))
            folders, _ = _receiving_folders("guangya", [{"receiving_folder": "FixtureSpecial"}])
            self.assertEqual(folders, ["光鸭：FixtureSpecial / 每任务隔离目录"])
            folders, _ = _receiving_folders("guangya", [{"receiving_folder": "FixtureSpecial"}, {}])
            self.assertEqual(set(folders), {"光鸭：FixtureSpecial / 每任务隔离目录", "光鸭：Inbox / 每任务隔离目录"})

    def test_classification_survives_persisted_staging_and_retry_without_remote_writes(self):
        from types import SimpleNamespace

        from app import database as db
        from app.modules import download_dispatcher as dispatcher
        from app.modules.organize_rules import OrganizeRules
        from tests.support import isolated_test_database

        def offline(_url, **kwargs):
            self.assertEqual(kwargs["target_dir_id"], "special-source")
            kwargs["on_staging_created"]({"id": "stage-7", "parent_id": "special-source", "name": "MF-7-Fixture"})
            return {"ok": True, "task_ids": ["fixture-task"], "decision": {"target_dir_id": "stage-7", "target_dir_name": "Fixture"},
                    "staging": {"isolated": True, "parent_id": "special-source", "name": "MF-7-Fixture"}}

        with (
            isolated_test_database(),
            patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "special-source", "name": "FixtureSpecial"}]),
            patch.object(dispatcher, "_recover_guangya_magnet_torrent", return_value=None),
            patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")),
            patch.object(dispatcher, "submit_offline", side_effect=offline) as backend,
        ):
            service = FakeDownloadService(_resource_item(site_id="sukebei"))
            submitted = asyncio.run(download_indexer_result(service, _RESULT_ID, "guangya", origin_namespace="agent", scope="agent"))
            self.assertTrue(submitted["ok"], submitted)
            row = db.get_download_request(submitted["request_id"])
            assert row is not None
            self.assertEqual((row["gy_requested_target_dir"], row["gy_requested_nsfw"]), ("special-source", 1))
            self.assertEqual((row["gy_staging_parent_dir"], row["gy_target_dir"]), ("special-source", "stage-7"))
            rules = OrganizeRules(nsfw_enabled=True, nsfw_source_ids='["special-source"]').for_source(row["gy_target_dir"])
            self.assertTrue(rules.nsfw_enabled)
            self.assertTrue(rules.nsfw_exclusive)
            repeated = asyncio.run(download_indexer_result(service, _RESULT_ID, "guangya", origin_namespace="agent", scope="agent"))
            self.assertTrue(repeated["duplicate"])
            self.assertEqual(repeated["request_id"], row["id"])
            backend.assert_called_once()


def test_explicit_quality_filters_before_limit_without_treating_unknown_as_sdr():
    items = [_resource_item(title='Demo.S03E01.1080p.H264', result_id='unknown-result-1234'),
             _resource_item(title='Demo.S03E01.1080p.HDR10', result_id='hdr-result-id-1234'),
             _resource_item(title='Demo.S03E01.2160p.SDR', result_id='fourk-result-1234'),
             _resource_item(title='Demo.S03E01.1080p.SDR', result_id='matched-result-1234')]
    service = FakeIndexerService(items=items, partial=False)
    with (patch('app.agent.domains.resources.indexer_actions.config.get_bool', return_value=True),
          patch('app.agent.domains.resources.indexer_actions.get_indexer_service', return_value=service)):
        args = search_arguments({'title': 'Demo', 'resolution': '1080p', 'dynamic_range': 'SDR', 'limit': 1})
        result = search_resources(args)
        assert [row['result_id'] for row in result.data['items']] == ['matched-result-1234']
        assert not result.data['truncated']
        assert result.data['technical_filter']['unknown_excluded'] == 1
        assert result.data['technical_filter']['mismatched_excluded'] == 2
        assert result.references[0].value['candidates'][0]['result_id'] == 'matched-result-1234'
        service.items = items[:3]
        result = search_resources(args)
        assert result.data['items'] == [] and result.status == 'empty'
        assert result.references[0].value['candidates'] == []
        assert result.references[0].value['_query_arguments'] == args
        # 未指定规格的旧调用仍返回原候选；不改变源查询条件和默认行为。
        result = search_resources(search_arguments({'title': 'Demo'}))
        assert len(result.data['items']) == 3
        assert all(call[0].title == 'Demo' for call in service.search_calls)


def test_search_quality_enums_reject_invalid_types():
    for field, value in [('resolution', ['1080p']), ('dynamic_range', True), ('dynamic_range', 'guessed SDR')]:
        for validator, base in (
            (search_arguments, {'title': 'Demo'}),
            (present_candidates_arguments, {'resource_candidates_ref': 'ref_' + 'a' * 24, 'positions': [1]}),
        ):
            try:
                validator({**base, field: value})
            except AgentToolError:
                continue
            raise AssertionError(f'{field} accepted invalid value')


def test_quality_filter_keeps_combined_hdr_evidence_and_public_title_boundary():
    for title, desired, matched in [
        ('Demo.2160p.DoVi.HDR10', 'HDR10', True),
        ('Demo.2160p.DoVi.HDR10', 'HDR', True),
        ('Demo.2160p.HLG', 'HDR', True),
        ('Demo.1080p.SDR.HDR10', 'SDR', False),
        ('Demo.' + 'a' * 350 + '.1080p.SDR', 'SDR', False),
    ]:
        service = FakeIndexerService(item=_resource_item(title=title), partial=False)
        with (patch('app.agent.domains.resources.indexer_actions.config.get_bool', return_value=True),
              patch('app.agent.domains.resources.indexer_actions.get_indexer_service', return_value=service)):
            result = search_resources(search_arguments({'title': 'Demo', 'dynamic_range': desired}))
            assert bool(result.data['items']) is matched, (title, desired)


def test_refining_existing_candidates_reuses_search_evidence_without_network_or_renumbering():
    items = [_resource_item(title=title, result_id=f'candidate-{number:016d}') for number, title in enumerate([
        'Demo.1080p.H264', 'Demo.1080p.HDR10', 'Demo.2160p.SDR',
        'Demo.1080p.SDR', 'Demo.1080p.SDR.HDR10', 'Demo.2160p.DoVi.HDR10',
    ])]
    service = FakeIndexerService(items=items, partial=False)
    with (patch('app.agent.domains.resources.indexer_actions.config.get_bool', return_value=True),
          patch('app.agent.domains.resources.indexer_actions.get_indexer_service', return_value=service)):
        original = search_resources(search_arguments({'title': 'Demo'})).references[0].value
        before = deepcopy(original)
        for quality, expected in [
            ({'resolution': '1080p', 'dynamic_range': 'SDR'}, [4]),
            ({'dynamic_range': 'SDR'}, [3, 4]),
            ({'dynamic_range': 'HDR10'}, [2, 5, 6]),
            ({'dynamic_range': 'HDR'}, [2, 5, 6]),
            ({'resolution': '720p', 'dynamic_range': 'SDR'}, []),
            ({'resolution': '', 'dynamic_range': ''}, [1, 2, 3, 4, 5, 6]),
        ]:
            fresh = search_resources(search_arguments({'title': 'Demo', **quality}))
            with patch('app.agent.domains.resources.indexer_actions.get_indexer_service', side_effect=AssertionError('refinement must be offline')):
                presented = present_candidates({'resource_candidates': original, 'positions': list(range(1, 7)), **quality})
            assert presented.data['positions'] == expected
            assert {k: v for k, v in presented.references[0].value.items() if k != '_query_arguments'} == {k: v for k, v in before.items() if k != '_query_arguments'}
            assert presented.data['effective_query'] == {**before['_query_arguments'], **quality}
            assert [row['result_id'] for row in fresh.data['items']] == [original['candidates'][pos - 1]['result_id'] for pos in expected]
            if any(quality.values()):
                assert presented.data['technical_filter'] == fresh.data['technical_filter']
            assert presented.status == ('found' if expected else 'empty')
        # 有限筛选保留请求顺序；没有隐式继承上一轮的 SDR 条件。
        assert present_candidates({'resource_candidates': original, 'positions': [6, 2, 4], 'dynamic_range': 'HDR'}).data['positions'] == [6, 2]
        assert present_candidates({'resource_candidates': original, 'positions': [1]}).data == {'positions': [1], 'effective_query': before['_query_arguments']}
        assert original == before


def test_candidate_refinement_reports_scope_without_confusing_subset_with_complete_snapshot():
    snapshot = _candidate_snapshot(count=50)
    for item in snapshot['candidates']:
        item['title'] = f"Demo.1080p.{'SDR' if item['position'] == 50 else 'H264'}"
    args = present_candidates_arguments({'resource_candidates_ref': 'ref_' + 'a' * 24,
                                        'positions': list(range(1, 51)), 'dynamic_range': 'SDR'})
    result = present_candidates({**args, 'resource_candidates': snapshot})
    assert result.data['positions'] == [50]
    assert result.data['scope'] == {'candidate_count': 50, 'considered_count': 50, 'complete': True}
    assert result.data['technical_filter']['unknown_excluded'] == 49
    partial = present_candidates({**args, 'resource_candidates': snapshot, 'positions': list(range(1, 13))})
    assert partial.data['positions'] == []
    assert partial.data['scope'] == {'candidate_count': 50, 'considered_count': 12, 'complete': False}
    cleared = present_candidates({**args, 'resource_candidates': snapshot, 'positions': []})
    assert cleared.data['positions'] == []
    assert cleared.data['scope']['considered_count'] == 0


def test_query_refinement_normalizes_only_explicit_patch_and_rejects_invalid_values():
    ref = 'ref_' + 'q' * 24
    assert search_arguments({'resource_candidates_ref': ref, 'season': 2}) == {
        'resource_candidates_ref': ref, 'season': 2,
    }
    assert search_arguments({'resource_candidates_ref': ref, 'resolution': '', 'season': None}) == {
        'resource_candidates_ref': ref, 'resolution': '', 'season': None,
    }
    for patch_args in ({'season': True}, {'episode': 0}, {'resolution': None}, {'title': ''},
                       {'aliases': 'Alias'}, {'sites': None}, {'page': None}, {'resource_candidates_ref': None}):
        with unittest.TestCase().assertRaises(AgentToolError):
            search_arguments({'resource_candidates_ref': ref, **patch_args})


def test_empty_search_can_refine_filters_without_cross_query_state_or_snapshot_mutation():
    service = FakeIndexerService(items=[], partial=False)
    with (patch('app.agent.domains.resources.indexer_actions.config.get_bool', return_value=True),
          patch('app.agent.domains.resources.indexer_actions.get_indexer_service', return_value=service)):
        first = search_resources(search_arguments({
            'title': 'Black Mirror', 'original_title': '黑镜', 'year': 2011, 'media_type': 'tv',
            'season': 1, 'episode': 1, 'resolution': '1080p', 'sites': ['nyaa'], 'page': 3,
        }))
        snapshot = first.references[0].value
        before = deepcopy(snapshot)
        assert snapshot['candidates'] == []
        next_result = search_resources({'resource_candidates': snapshot, 'season': 2})
        effective = next_result.data['effective_query']
        assert effective == {**first.data['effective_query'], 'season': 2, 'page': 1}
        narrowed = present_candidates({'resource_candidates': next_result.references[0].value,
                                       'positions': [], 'dynamic_range': 'SDR'})
        assert narrowed.data['effective_query'] == {**effective, 'dynamic_range': 'SDR'}
        continued = search_resources({'resource_candidates': narrowed.references[0].value,
                                      'resolution': '2160p', 'page': 2})
        assert continued.data['effective_query'] == {**effective, 'dynamic_range': 'SDR', 'resolution': '2160p', 'page': 2}
        cleared = search_resources({'resource_candidates': continued.references[0].value,
                                    'resolution': '', 'dynamic_range': '', 'season': None,
                                    'episode': None, 'year': None, 'sites': [], 'original_title': ''})
        assert cleared.data['effective_query'] == {
            **effective, 'resolution': '', 'dynamic_range': '', 'season': None, 'episode': None,
            'year': None, 'sites': [], 'original_title': '',
        }
        independent = search_resources(search_arguments({'title': 'Another Film', 'media_type': 'movie'}))
        assert independent.data['effective_query'] == search_arguments({'title': 'Another Film', 'media_type': 'movie'})
        assert snapshot == before
        assert service.search_calls[1][0].season == 2 and service.search_calls[1][1] == ['nyaa']
        assert service.search_calls[-1][0].season is None and service.search_calls[-1][1] is None


def test_present_inherits_only_the_selected_reference_and_updates_query_for_next_search():
    original = _candidate_snapshot(count=3)
    original['candidates'][0]['title'] = 'Demo.S03E01.1080p.H264'
    original['candidates'][1]['title'] = 'Demo.S03E01.2160p.SDR'
    original['candidates'][2]['title'] = 'Demo.S03E01.1080p.SDR'
    original['_query_arguments'] = search_arguments({'title': 'Demo', 'season': 3, 'episode': 1, 'resolution': '1080p'})
    before = deepcopy(original)
    with patch('app.agent.domains.resources.indexer_actions.get_indexer_service', side_effect=AssertionError('offline refinement')):
        first = present_candidates({'resource_candidates': original, 'positions': [1, 2, 3], 'dynamic_range': 'SDR'})
        assert first.data['positions'] == [3]
        second = present_candidates({'resource_candidates': first.references[0].value, 'positions': [1, 2, 3]})
        assert second.data['positions'] == [3]  # 不重抄规格，也不会放回未知或其它画质。
        relaxed = present_candidates({'resource_candidates': second.references[0].value, 'positions': [1, 2, 3], 'resolution': ''})
        assert relaxed.data['positions'] == [2, 3]
        assert relaxed.data['effective_query']['dynamic_range'] == 'SDR'
        assert present_candidates({'resource_candidates': original, 'positions': [1, 2, 3]}).data['positions'] == [1, 3]
        assert original == before
        assert first.references[0].value['candidates'] == original['candidates']


def test_legacy_and_combined_candidate_snapshots_cannot_guess_a_query_for_refinement():
    from app.agent.domains.resources.candidate_context import (
        restore_resource_candidate_reference,
    )

    legacy = {key: value for key, value in _candidate_snapshot().items() if not key.startswith('_private')}
    for snapshot in (legacy, {**legacy, '_query_arguments': []}):
        with (patch('app.agent.domains.resources.indexer_actions.config.get_bool', return_value=True),
              patch('app.agent.domains.resources.indexer_actions.get_indexer_service', side_effect=AssertionError('no implicit search')),
              unittest.TestCase().assertRaises(AgentToolError)):
            search_resources({'resource_candidates': snapshot, 'resolution': '2160p'})
    # 历史候选正常展示/恢复；新查询元数据不能变成下载身份或权限。
    assert present_candidates({'resource_candidates': legacy, 'positions': [1]}).data == {'positions': [1]}
    assert restore_resource_candidate_reference({**legacy, '_query_arguments': {'title': 'Demo'}}) == legacy
    assert restore_resource_candidate_reference({**legacy, '_query_arguments': {'title': 'Demo'}, '_extra': True}) is None


def test_excluding_hdr_keeps_unknown_distinct_from_explicit_sdr():
    from app.agent.domains.resources.indexer_actions import filter_technical_candidates

    items = [{'title': f'Demo.1080p.{suffix}'} for suffix in ('H264', 'SDR', 'HDR10', 'DoVi', 'HLG', 'SDR.HDR10')]
    selected, stats = filter_technical_candidates(items, {'exclude_hdr': True})
    assert selected == items[:2] and stats['mismatched_excluded'] == 4
    assert stats['requested'] == {'exclude_hdr': True} and not stats['stream_verified']
    selected, stats = filter_technical_candidates(items, {'exclude_hdr': True, 'dynamic_range': 'SDR'})
    assert selected == items[1:2] and stats['unknown_excluded'] == 1
    assert filter_technical_candidates(items, {'exclude_hdr': False})[0] == items
    for value in ('false', 'true', 0, 1, None, []):
        with unittest.TestCase().assertRaises(AgentToolError):
            search_arguments({'title': 'Demo', 'exclude_hdr': value})
