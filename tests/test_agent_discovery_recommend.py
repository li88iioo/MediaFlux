"""Media Agent 外部影视默认推荐列表测试。"""

from __future__ import annotations
from typing import Any, Callable, Mapping
from typing import cast

import json
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from app.agent.domains.discovery.actions import recommend_arguments, recommend_discovery
from app.agent.errors import AgentToolError
from app.agent.kernel.conversation_context import capability_retrieval_context
from app.agent.models import ToolContext
from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderHealth,
    ProviderRateLimited,
    ProviderTimeout,
)
from app.discovery.registry import list_filter_definitions, validate_filters


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1, **overrides) -> MediaCard:
    values = {
        "provider": "tmdb",
        "external_id": str(2000 + index),
        "media_type": "movie",
        "title": f"推荐影片 {index}",
        "original_title": f"Recommended Movie {index}",
        "year": "2026",
        "overview": "安全简介\x00" * 80,
        "poster_key": "https://image.example/poster?token=secret",
        "backdrop_key": "https://image.example/backdrop?api_key=secret",
        "rating": 8.6,
        "rating_source": "tmdb",
        "release_date": "2026-08-01",
        "tmdb_id": str(2000 + index),
        "state": "watchlisted",
    }
    values.update(overrides)
    return MediaCard(**values)


class FakeDiscoveryService:
    def __init__(self, page: DiscoveryPage):
        self.page = page
        self.calls: list[tuple[str, str, str, int, dict]] = []

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        return self.page


class PagedDiscoveryService(FakeDiscoveryService):
    def __init__(self, pages: Mapping[int, DiscoveryPage | Exception] | Callable[[int], DiscoveryPage | Exception], after_page=None):
        self.pages, self.after_page, self.calls = pages, after_page, []

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        value = self.pages(page) if callable(self.pages) else self.pages[page]
        if isinstance(value, Exception):
            raise value
        if self.after_page:
            self.after_page(page)
        return value


def _page(items=(), *, number=1, has_more=False):
    return DiscoveryPage(items=tuple(items), page=number, has_more=has_more,
                         provider=ProviderHealth(name="tmdb", status="healthy"))


@contextmanager
def _recommend_fixture(service, inventory=None):
    client = Mock()
    client.list_media_identity_inventory.return_value = inventory or MediaIdentityInventory(candidates=[], total=0)
    with patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True), \
            patch("app.agent.domains.discovery.actions.get_discovery_service", return_value=service), \
            patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        yield client


class AgentRecommendationCompletionTests(unittest.TestCase):
    def test_recommendation_uses_shared_provider_genre_options(self):
        for provider, media_type, key in (
            ("tmdb", "movie", "with_genres"), ("tmdb", "tv", "with_genres"),
            ("douban", "movie", "tags"),
        ):
            definition = list_filter_definitions(provider, media_type)
            choices = next(item["options"] for item in definition["filters"] if item["key"] == key)
            for option in choices:
                with self.subTest(provider=provider, media_type=media_type, genre=option["label"]):
                    service = PagedDiscoveryService({1: _page([])})
                    with _recommend_fixture(service):
                        recommend_discovery({
                            "provider": provider, "media_type": media_type, "genre": option["label"],
                        })
                    self.assertEqual(service.calls[0][4][key], option["value"])

    def test_unsupported_constraints_cannot_be_silently_dropped(self):
        for arguments in (
            {"provider": "douban", "media_type": "movie", "region": "日本", "genre": "科幻"},
            {"provider": "douban", "media_type": "tv", "region": "日本", "genre": "科幻"},
            {"provider": "douban", "media_type": "tv", "region": "意大利"},
            {"provider": "tmdb", "media_type": "movie", "region": "不存在的地区"},
            {"provider": "tmdb", "media_type": "movie", "region": "欧美"},
            {"provider": "douban", "media_type": "tv", "region": "欧美"},
            {"provider": "tmdb", "media_type": "tv", "genre": "不存在的题材"},
            {"provider": "douban", "media_type": "movie", "genre": "不存在的题材"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(AgentToolError):
                    recommend_arguments(arguments)
                service = PagedDiscoveryService({1: _page([_card()])})
                with _recommend_fixture(service), self.assertRaises(AgentToolError):
                    recommend_discovery(arguments)
                self.assertEqual(service.calls, [])

    def test_tmdb_region_accepts_country_codes_and_common_names(self):
        for region, expected in (("意大利", "IT"), ("it", "IT"), ("ＥＳ", "ES")):
            with self.subTest(region=region):
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    recommend_discovery({"media_type": "movie", "region": region})
                self.assertEqual(service.calls[0][4]["with_origin_country"], expected)

    def test_supported_recommendation_aliases_keep_their_original_meaning(self):
        for provider, media_type, argument, expected in (
            ("tmdb", "movie", {"genre": "动漫"}, {"with_genres": "16"}),
            ("tmdb", "tv", {"genre": "动作"}, {"with_genres": "10759"}),
            ("tmdb", "tv", {"genre": "科幻"}, {"with_genres": "10765"}),
            ("tmdb", "tv", {"genre": "历史"}, {"with_genres": "10768"}),
            ("douban", "tv", {"region": "国产"}, {"tags": "国产剧"}),
            ("douban", "tv", {"region": "日本"}, {"tags": "日剧"}),
        ):
            with self.subTest(provider=provider, media_type=media_type, argument=argument):
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    recommend_discovery({"provider": provider, "media_type": media_type, **argument})
                self.assertEqual(service.calls[0][4], expected)

    def test_continuation_consumes_every_card_including_last_source_page_remainder(self):
        for exclude_library in (False, True):
            with self.subTest(exclude_library=exclude_library):
                service = PagedDiscoveryService({
                    1: _page([_card(i) for i in range(1, 21)], has_more=True),
                    2: _page([_card(i) for i in range(21, 41)], number=2),
                })
                results = []
                arguments = {"exclude_library": exclude_library}
                with _recommend_fixture(service):
                    for _ in range(4):
                        result = recommend_discovery(arguments)
                        self.assertEqual(result.data["returned"], 10)
                        results.extend(item["tmdb_id"] for item in result.data["items"])
                        scope = result.data["scope"]
                        arguments.update(page=scope["next_page"], offset=scope.get("next_offset", 0))
                self.assertEqual(results, [str(2000 + i) for i in range(1, 41)])
                self.assertIsNone(scope["next_page"])
                self.assertFalse(result.data["has_more"])
                self.assertEqual([call[3] for call in service.calls], [1, 1, 2, 2])

    def test_continuation_offset_uses_source_positions_not_filtered_positions(self):
        service = PagedDiscoveryService({1: _page([
            _card(1, year="2010"), _card(2), _card(3), _card(3),
            _card(4, media_type="tv"), _card(5), _card(6),
        ])})
        inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(
            name="推荐影片 2", tmdb_id="2002", media_type="movie")], total=1)
        with _recommend_fixture(service, inventory):
            first = recommend_discovery({"limit": 1, "year_from": "2020", "exclude_library": True})
            self.assertEqual(first.data["scope"]["next_page"], 1)
            second = recommend_discovery({
                "limit": 2, "year_from": "2020", "exclude_library": True,
                "page": 1, "offset": first.data["scope"]["next_offset"],
            })
        self.assertEqual([i["tmdb_id"] for i in first.data["items"]], ["2003"])
        self.assertEqual([i["tmdb_id"] for i in second.data["items"]], ["2005", "2006"])
        self.assertIsNone(second.data["scope"]["next_page"])

    def test_continuation_retries_failed_page_without_dropping_offset(self):
        service = PagedDiscoveryService({1: ProviderTimeout("timeout")})
        with _recommend_fixture(service):
            result = recommend_discovery({"page": 1, "offset": 10})
        self.assertEqual(result.data["scope"]["next_page"], 1)
        self.assertEqual(result.data["scope"]["next_offset"], 10)

    def test_recommendation_offset_rejects_invalid_values(self):
        for offset in (-1, True, "10", 0.5, None):
            with self.subTest(offset=offset), self.assertRaises(AgentToolError):
                recommend_arguments({"offset": offset})

    def test_named_region_is_origin_country_not_original_language(self):
        for region, country in (("中国大陆", "CN"), ("台湾", "TW"), ("香港", "HK"),
                                ("英国", "GB"), ("美国", "US"), ("日本", "JP")):
            for media_type in ("tv", "movie"):
                with self.subTest(region=region, media_type=media_type):
                    service = PagedDiscoveryService({1: _page([])})
                    with _recommend_fixture(service):
                        recommend_discovery({"region": region, "media_type": media_type})
                    filters = validate_filters("tmdb", "discover", media_type, service.calls[0][4])
                    self.assertEqual(filters["with_origin_country"], country)
                    self.assertNotIn("with_original_language", filters)

    def test_language_filter_stays_separate_from_origin_country(self):
        filters = validate_filters("tmdb", "discover", "tv",
                                   {"with_origin_country": "cn", "with_original_language": "JA"})
        self.assertEqual(filters["with_origin_country"], "CN")
        self.assertEqual(filters["with_original_language"], "ja")
        for value in ("", "CN|JP", "中国", "C", "CNA", "1A"):
            if value:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_filters("tmdb", "discover", "tv", {"with_origin_country": value})

    def test_model_recommendation_has_one_canonical_list_and_preserves_audit(self):
        from app.agent.kernel.projection import DefaultProjector

        service = PagedDiscoveryService({
            1: _page([_card(i, overview="") for i in range(1, 21)], has_more=True),
            2: _page([_card(i, overview="") for i in range(21, 41)], number=2),
        })
        inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(
            name="推荐影片 1", year="2026", tmdb_id="2001", media_type="movie")], total=1)
        with _recommend_fixture(service, inventory):
            result = recommend_discovery({"limit": 20, "exclude_library": True})
        original_check = result.data["library_check"]
        self.assertEqual(len(original_check["items"]), 40)
        outcome = DefaultProjector().project(result)
        model_data = json.loads(outcome.model_content)["data"]
        self.assertEqual(model_data["items"], result.data["items"])
        self.assertEqual(len(model_data["items"]), 20)
        self.assertTrue(model_data["scope"]["complete"])
        self.assertNotIn("items", model_data["library_check"])
        self.assertEqual(model_data["library_check"]["counts"], original_check["counts"])
        self.assertEqual(model_data["library_check"]["sources"], original_check["sources"])
        self.assertEqual(result.to_dict()["data"]["library_check"], original_check)
        self.assertNotIn("推荐影片 40", outcome.model_content)
        self.assertTrue(all(row["library_status"] == "missing" for row in model_data["items"]))

    def test_empty_front_pages_and_library_filter_still_fill_requested_number(self):
        service = PagedDiscoveryService({
            1: _page(has_more=True), 2: _page(number=2, has_more=True),
            3: _page([_card(1), _card(2, year="2010"), _card(3)], number=3, has_more=True),
            4: _page([_card(3), _card(4), _card(5)], number=4),
        })
        inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(
            name="推荐影片 1", year="2026", tmdb_id="2001", media_type="movie")], total=1)
        with _recommend_fixture(service, inventory) as client:
            result = recommend_discovery({"limit": 3, "year_from": "2016", "exclude_library": True})
        self.assertEqual([item["tmdb_id"] for item in result.data["items"]], ["2003", "2004", "2005"])
        self.assertEqual(client.list_media_identity_inventory.call_count, 1)
        self.assertEqual([call[3] for call in service.calls], [1, 2, 3, 4])
        self.assertEqual(result.data["scope"]["stop_reason"], "target_reached")
        self.assertEqual(result.data["scope"]["shortfall"], 0)
        self.assertTrue(result.data["scope"]["complete"])
        self.assertTrue(all(item["library_status"] == "missing" for item in result.data["items"]))

    def test_candidates_are_deduplicated_before_counting_and_preserve_provider_order(self):
        service = PagedDiscoveryService({
            1: _page([_card(1)], has_more=True),
            2: _page([_card(1), _card(2)], number=2, has_more=True),
            3: _page([_card(3)], number=3),
        })
        with _recommend_fixture(service):
            result = recommend_discovery({"limit": 3})
        self.assertEqual([item["tmdb_id"] for item in result.data["items"]], ["2001", "2002", "2003"])
        self.assertEqual(result.data["scope"]["duplicates"], 1)
        self.assertEqual(result.data["scope"]["pages_read"], [1, 2, 3])

    def test_real_last_page_reports_shortfall_without_inventing_next_page(self):
        service = PagedDiscoveryService({1: _page([_card(1)])})
        with _recommend_fixture(service):
            result = recommend_discovery({"limit": 3})
        scope = result.data["scope"]
        self.assertEqual((scope["requested"], scope["returned"], scope["shortfall"]), (3, 1, 2))
        self.assertEqual(scope["stop_reason"], "source_exhausted")
        self.assertIsNone(scope["next_page"])
        self.assertFalse(scope["complete"])
        self.assertFalse(result.data["has_more"])

    def test_repeated_or_empty_pages_stop_at_bound_without_claiming_exhaustion(self):
        for items in ([], [_card(1)]):
            service = PagedDiscoveryService(lambda page, items=items: _page(items, number=page, has_more=True))
            with self.subTest(items=len(items)), _recommend_fixture(service):
                result = recommend_discovery({"limit": 3})
            self.assertEqual(len(service.calls), 8)
            self.assertEqual(result.data["scope"]["stop_reason"], "page_limit")
            self.assertEqual(result.data["scope"]["next_page"], 9)
            self.assertFalse(result.data["scope"]["complete"])
            self.assertTrue(result.data["has_more"])

    def test_source_page_hundred_is_never_followed_by_invalid_page_101(self):
        service = PagedDiscoveryService({100: _page([], number=100, has_more=True)})
        with _recommend_fixture(service):
            result = recommend_discovery({"page": 100})
        self.assertEqual([call[3] for call in service.calls], [100])
        self.assertEqual(result.data["scope"]["stop_reason"], "page_limit")
        self.assertIsNone(result.data["scope"]["next_page"])

    def test_unknown_and_truncated_inventory_never_become_missing(self):
        service = PagedDiscoveryService({1: _page([_card(1), _card(2, tmdb_id="")])})
        for inventory in (MediaIdentityInventory(candidates=[], total=10, truncated=True), RuntimeError("private outage")):
            with self.subTest(inventory=type(inventory).__name__), _recommend_fixture(service) as client:
                if isinstance(inventory, Exception):
                    client.list_media_identity_inventory.side_effect = inventory
                else:
                    client.list_media_identity_inventory.return_value = inventory
                result = recommend_discovery({"limit": 2, "exclude_library": True})
            self.assertEqual(result.data["items"], [])
            self.assertEqual(result.data["library_check"]["counts"]["indeterminate"], 1)
            self.assertEqual(result.data["library_check"]["unmapped_candidates"], 1)
            self.assertEqual(result.data["scope"]["shortfall"], 2)
            self.assertEqual(result.status, "partial")
            self.assertNotIn("private outage", repr(result.to_dict()))

    def test_provider_failure_after_first_page_preserves_verified_partial_result(self):
        for error, reason in ((ProviderTimeout("源超时"), "timeout"),
                              (ProviderRateLimited("源限流", retry_after=12), "provider_error")):
            service = PagedDiscoveryService({1: _page([_card(1)], has_more=True), 2: error})
            with self.subTest(reason=reason), _recommend_fixture(service):
                result = recommend_discovery({"limit": 3, "exclude_library": True})
            self.assertEqual([item["tmdb_id"] for item in result.data["items"]], ["2001"])
            self.assertEqual(result.data["scope"]["pages_read"], [1])
            self.assertEqual(result.data["scope"]["stop_reason"], reason)
            self.assertEqual(result.status, "partial")
            self.assertTrue(result.ok)

    def test_cooperative_deadline_preserves_received_page_and_stops_more_io(self):
        clock = [0.0]
        service = PagedDiscoveryService(lambda page: _page([_card(page)], number=page, has_more=True),
                                        after_page=lambda _: clock.__setitem__(0, 21.0))
        with _recommend_fixture(service), patch("app.services.time.monotonic", side_effect=lambda: clock[0]):
            result = recommend_discovery({"limit": 3})
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual([call[3] for call in service.calls], [1])
        self.assertEqual(result.data["scope"]["stop_reason"], "timeout")

    def test_deadline_during_local_matching_does_not_start_another_page(self):
        from app.services import summarize_media_identity_batch

        clock = [0.0]
        service = PagedDiscoveryService(lambda page: _page([_card(page)], number=page, has_more=True))

        def summarize(identities, sources):
            result = summarize_media_identity_batch(identities, sources)
            clock[0] = 21.0
            return result

        with _recommend_fixture(service), patch("app.services.time.monotonic", side_effect=lambda: clock[0]), \
                patch("app.services.summarize_media_identity_batch", side_effect=summarize):
            result = recommend_discovery({"limit": 3, "exclude_library": True})
        self.assertEqual([call[3] for call in service.calls], [1])
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(result.data["scope"]["stop_reason"], "timeout")

    def test_failed_page_hundred_remains_a_valid_retry_scope(self):
        service = PagedDiscoveryService({99: _page([_card(1)], number=99, has_more=True),
                                         100: ProviderTimeout("源超时")})
        with _recommend_fixture(service):
            result = recommend_discovery({"page": 99, "limit": 3})
        self.assertEqual(result.data["scope"]["pages_read"], [99])
        self.assertEqual(result.data["scope"]["next_page"], 100)

    def test_independent_recommendation_requests_refresh_library_inventory(self):
        service = PagedDiscoveryService({1: _page([_card(1), _card(2)])})
        with _recommend_fixture(service) as client:
            results = []
            for existing in (1, 2):
                client.list_media_identity_inventory.return_value = MediaIdentityInventory(
                    candidates=[MediaIdentityCandidate(name="库内作品", tmdb_id=str(2000 + existing))], total=1)
                results.append(recommend_discovery({"limit": 1, "exclude_library": True}))
        self.assertEqual([result.data["items"][0]["tmdb_id"] for result in results], ["2002", "2001"])
        self.assertEqual(client.list_media_identity_inventory.call_count, 2)

    def test_library_check_preserves_public_text_projection(self):
        service = PagedDiscoveryService({1: _page([_card(1, title="标题\x00" * 100, year="2026\x00")])})
        with _recommend_fixture(service):
            result = recommend_discovery({"limit": 1, "exclude_library": True})
        candidate = result.data["library_check"]["items"][0]
        self.assertEqual(candidate["title"], result.data["items"][0]["title"])
        self.assertEqual(candidate["year"], result.data["items"][0]["year"])
        self.assertNotIn("\\x00", repr(result.to_dict()))

    def test_cancel_after_page_does_not_start_inventory_io(self):
        cancelled = [False]
        service = PagedDiscoveryService({1: _page([_card(1)], has_more=True)},
            after_page=lambda _: cancelled.__setitem__(0, True))
        context = ToolContext(owner="test", session_id="test", cancelled=lambda: cancelled[0])
        with _recommend_fixture(service) as client:
            result = recommend_discovery({"exclude_library": True}, context)
        client.list_media_identity_inventory.assert_not_called()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.data["items"], [])
        self.assertEqual(result.data["library_check"]["counts"]["indeterminate"], 1)

    def test_cancel_during_inventory_keeps_verified_items_but_never_claims_complete(self):
        cancelled = [False]
        context = ToolContext(owner="test", session_id="test", cancelled=lambda: cancelled[0])
        service = PagedDiscoveryService({1: _page([_card(1)], has_more=True)})

        def inventory(*_, **__):
            cancelled[0] = True
            return MediaIdentityInventory(candidates=[], total=0)

        with _recommend_fixture(service) as client:
            client.list_media_identity_inventory.side_effect = inventory
            result = recommend_discovery({"limit": 1, "exclude_library": True}, context)
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.data["scope"]["complete"])
        self.assertEqual([call[3] for call in service.calls], [1])

    def test_registered_context_handler_obeys_cancel_before_start_and_between_pages(self):
        from app.agent.domain_catalog import build_tool_specs
        spec = next(tool for tool in build_tool_specs() if tool.name == "discovery.recommend")
        for initially_cancelled in (False, True):
            cancelled = [initially_cancelled]
            service = PagedDiscoveryService(lambda page: _page([_card(page)], number=page, has_more=True),
                after_page=lambda _, cancelled=cancelled: cancelled.__setitem__(0, True))
            context = ToolContext(owner="test", session_id="test", cancelled=lambda cancelled=cancelled: cancelled[0])
            with self.subTest(initial=initially_cancelled), _recommend_fixture(service):
                assert spec.context_handler is not None
                result = spec.context_handler({"limit": 3}, context)
            self.assertEqual(len(service.calls), 0 if initially_cancelled else 1)
            self.assertEqual(result.status, "cancelled")
            self.assertFalse(result.data["scope"]["complete"])
            self.assertEqual(result.data["scope"]["stop_reason"], "cancelled")


class AgentRecommendationDeclarationTests(unittest.TestCase):
    def test_existing_history_recommendation_case_keeps_both_tools_in_native_first_window(self):
        import json
        from pathlib import Path

        from app.agent.domain_catalog import build_tool_specs
        from app.agent.kernel.capabilities import CapabilityRetriever
        from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs
        from app.agent.kernel.session_state import SessionState

        fixture = Path(__file__).parent / "fixtures" / "agent_kernel_capability_cases.jsonl"
        case = next(row for line in fixture.read_text().splitlines()
                    if (row := json.loads(line))["id"] == "history-based-recommendation")
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = SessionState(owner="offline-eval", session_id="offline-eval-session",
                             conversation=[{"role": "user", "content": case["message"]}])
        selection = CapabilityRetriever().retrieve(case["message"], catalog, context={
            "owner": state.owner, "session_id": state.session_id, "channel": "eval", "reference_kinds": (),
            **capability_retrieval_context(state),
        })
        self.assertEqual(case["required"], ["media.recently_played", "discovery.recommend"])
        self.assertTrue(set(case["required"]) <= set(selection.names), selection.names)

    def test_explicit_no_recommendation_still_excludes_recommendation_tools(self):
        from app.agent.domain_catalog import build_tool_specs
        from app.agent.kernel.capabilities import CapabilityRetriever
        from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs

        catalog = catalog_from_tool_specs(build_tool_specs())
        message = "我最近看了哪些内容？只要真实播放历史，不要推荐"
        selection = CapabilityRetriever().retrieve(message, catalog, context={"recent_user_messages": (message,)})
        self.assertIn("media.recently_played", selection.names)
        self.assertNotIn("discovery.recommend", selection.names)
        self.assertNotIn("media.recommend_from_library", selection.names)

    def test_external_limit_is_final_target_and_scope_describes_actual_coverage(self):
        from app.agent.domain_catalog import build_tool_specs

        spec = next(tool for tool in build_tool_specs() if tool.name == "discovery.recommend")
        properties = spec.parameters["properties"]
        self.assertEqual(set(properties), {"provider", "media_type", "page", "offset", "limit", "year",
                                          "year_from", "year_to", "exclude_library", "region", "genre"})
        limit = properties["limit"]
        self.assertEqual((limit["minimum"], limit["maximum"], limit["default"]), (1, 20, 10))
        self.assertIn("最终推荐目标数", limit.get("description", ""))
        self.assertIn("不是单页", limit.get("description", ""))
        self.assertIn("起始页", properties["page"].get("description", ""))
        self.assertIn("有界", spec.description)
        self.assertIn("scope", spec.description)
        self.assertIn("覆盖范围", spec.description)
        self.assertIn("scope.next_page", spec.description)
        self.assertNotIn("候选不足且has_more时继续下一页", spec.description)

    def test_recommendation_labels_have_one_positive_retrieval_declaration(self):
        from app.agent.domain_catalog import build_tool_specs
        from app.agent.domain_tool_metadata import TOOL_RETRIEVAL_TERMS
        from app.agent.kernel.capabilities import CapabilityRetriever
        from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs

        phrases = {
            "media.recommend_from_library": (
                "从媒体库推荐", "本地片单推荐", "今晚看什么", "今天心情很丧", "不用脑子", "爆笑", "无厘头",
                "日番", "推荐没看过的", "排除已看作品", "结合观看历史推荐", "Jellyfin 里值得看",
            ),
            "discovery.recommend": (
                "最近推荐", "新剧推荐", "国漫推荐", "片单推荐", "根据观看记录推荐", "根据最近播放推荐",
            ),
        }
        specs = {spec.name: spec for spec in build_tool_specs()}
        catalog = catalog_from_tool_specs(specs.values())
        retriever = CapabilityRetriever(minimum=6, maximum=8)
        for name, labels in phrases.items():
            with self.subTest(name=name):
                self.assertEqual(TOOL_RETRIEVAL_TERMS[name], labels)
                self.assertTrue(set(labels).isdisjoint(specs[name].examples))
                terms = catalog.get(name).metadata["retrieval_terms"]
                self.assertTrue(all(terms.count(label) == 1 for label in labels))
                self.assertEqual(len(specs[name].examples), len(set(specs[name].examples)))
        for query, name in (("今晚看什么", "media.recommend_from_library"),
                            ("本地片单推荐", "media.recommend_from_library"),
                            ("最近有什么推荐的国漫", "discovery.recommend"),
                            ("国漫推荐", "discovery.recommend")):
            with self.subTest(query=query):
                self.assertIn(name, retriever.retrieve(query, catalog).names)

    def test_fulfilled_twenty_recommendations_are_complete_despite_more_source_pages(self):
        from app.agent.domain_catalog import build_tool_specs

        service = PagedDiscoveryService({
            1: _page([_card(index) for index in range(1, 11)], has_more=True),
            2: _page([_card(index) for index in range(11, 21)], number=2, has_more=True),
        })
        with _recommend_fixture(service):
            result = recommend_discovery({"limit": 20, "exclude_library": True})
        self.assertEqual([call[3] for call in service.calls], [1, 2])
        self.assertEqual(result.data["returned"], 20)
        self.assertTrue(result.data["scope"]["complete"])
        self.assertTrue(result.data["has_more"])
        self.assertEqual(result.data["scope"]["next_page"], 3)
        spec = next(tool for tool in build_tool_specs() if tool.name == "discovery.recommend")
        self.assertIn("scope.complete=true", spec.description)
        self.assertIn("scope.returned>=scope.requested", spec.description)
        self.assertIn("has_more 仅表示来源仍有候选", spec.description)
        self.assertIn("用户明确要求更多", spec.description)


class AgentDiscoveryRecommendTests(unittest.TestCase):
    def test_arguments_normalize_defaults_and_reject_unsafe_fields(self):
        self.assertEqual(
            recommend_arguments(
                {
                    "provider": " ＤＯＵＢＡＮ ",
                    "media_type": " ＴＶ ",
                    "page": 2,
                    "limit": 8,
                }
            ),
            {"provider": "douban", "media_type": "tv", "page": 2, "limit": 8},
        )
        self.assertEqual(
            recommend_arguments({}),
            {"provider": "tmdb", "media_type": "movie", "page": 1, "limit": 10},
        )
        self.assertEqual(
            recommend_arguments(
                {
                    "provider": "tmdb",
                    "media_type": "tv",
                    "year": "2025",
                    "region": "美国",
                    "genre": "科幻",
                }
            ),
            {
                "provider": "tmdb",
                "media_type": "tv",
                "page": 1,
                "limit": 10,
                "year": "2025",
                "region": "美国",
                "genre": "科幻",
            },
        )
        invalid = (
            None,
            {"provider": "bangumi"},
            {"provider": 1},
            {"media_type": "all"},
            {"media_type": False},
            {"page": True},
            {"page": 101},
            {"limit": 0},
            {"limit": 21},
            {"year": "202A"},
            {"year": "1899"},
            {"region": "\x00"},
            {"genre": "\x00"},
            {"filters": {"sort_by": "vote_average.desc"}},
            {"url": "https://example.invalid"},
            {"token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                recommend_arguments(cast(dict[str, Any], arguments))

    def test_year_range_contract_and_provider_filters(self):
        for provider, media_type in (("tmdb", "movie"), ("tmdb", "tv"), ("douban", "tv")):
            page = DiscoveryPage(items=tuple(_card(i, year=y, media_type=media_type) for i, y in enumerate(
                ("2015", "2016", "2020", "2026", "", "unknown"), 1)), page=1, has_more=True,
                provider=ProviderHealth(name="tmdb", status="healthy"))
            service = FakeDiscoveryService(page)
            with patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True), \
                    patch("app.agent.domains.discovery.actions.get_discovery_service", return_value=service):
                result = recommend_discovery({"provider": provider, "media_type": media_type,
                    "year_from": "2016", "year_to": "2020"})
            self.assertEqual([item["year"] for item in result.data["items"]], ["2016", "2020"])
            if provider == "tmdb":
                date = "release_date" if media_type == "movie" else "first_air_date"
                filters = {f"{date}.gte": "2016-01-01", f"{date}.lte": "2020-12-31"}
                self.assertEqual(service.calls[0][4], filters)
                self.assertEqual(validate_filters(provider, "discover", media_type, filters),
                                 {**filters, "sort_by": "popularity.desc"})
            self.assertTrue(result.data["has_more"])
        for args in ({"year_from": "x"}, {"year_to": "2100"},
                     {"year_from": "2025", "year_to": "2016"},
                     {"year": "2015", "year_from": "2016"}, {"exclude_library": "true"}):
            with self.subTest(args=args), self.assertRaises(AgentToolError):
                recommend_arguments(args)

    def test_library_exclusion_uses_batch_identity_before_display_limit(self):
        page = DiscoveryPage(items=tuple(_card(i) for i in range(1, 6)), page=1, has_more=True,
            provider=ProviderHealth(name="tmdb", status="healthy"))
        source = {"server_type": "jellyfin", "server_name": "Jellyfin", "status": "ready",
            "inventories": {"movie": {"total": 500, "truncated": False, "unmapped": 1}},
            "items": [{"tmdb_id": str(2000+i), "media_type": "movie", "status": status, "match": "provider_id"}
                for i, status in enumerate(("present", "possible", "missing", "missing", "missing"), 1)]}
        with patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True), \
                patch("app.agent.domains.discovery.actions.get_discovery_service", return_value=FakeDiscoveryService(page)), \
                patch("app.services.inspect_media_identity_batch", return_value=[source]) as inspect:
            result = recommend_discovery({"exclude_library": True, "limit": 2})
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(len(inspect.call_args.args[0]), 5)
        self.assertEqual([item["tmdb_id"] for item in result.data["items"]], ["2003", "2004"])
        self.assertTrue(all(item["library_status"] == "missing" for item in result.data["items"]))
        self.assertEqual(result.data["total"], 3)
        self.assertEqual(result.data["library_check"]["counts"]["present"], 1)
        self.assertEqual(result.status, "partial")  # 未映射同名同年不能伪称不在库。
        self.assertTrue(result.data["has_more"])

    def test_exclusion_never_turns_unknown_inventory_or_identity_into_missing(self):
        page = DiscoveryPage(items=(_card(1), _card(2, tmdb_id="")), page=1, has_more=True,
            provider=ProviderHealth(name="tmdb", status="healthy"))
        for sources in ([], [{"server_type": "jellyfin", "server_name": "Jellyfin", "status": "unavailable", "items": []}]):
            with patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True), \
                    patch("app.agent.domains.discovery.actions.get_discovery_service", return_value=FakeDiscoveryService(page)), \
                    patch("app.services.inspect_media_identity_batch", return_value=sources):
                result = recommend_discovery({"exclude_library": True})
            self.assertEqual(result.data["items"], [])
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.data["library_check"]["unmapped_candidates"], 1)
            self.assertEqual(result.data["library_check"]["counts"]["indeterminate"], 1)
        with patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True), \
                patch("app.agent.domains.discovery.actions.get_discovery_service", return_value=FakeDiscoveryService(page)), \
                patch("app.services.inspect_media_identity_batch") as inspect:
            result = recommend_discovery({})
        inspect.assert_not_called()
        self.assertEqual(len(result.data["items"]), 2)
        self.assertNotIn("library_check", result.data)

    def test_filtered_tmdb_recommendation_uses_discover_filters(self):
        page = DiscoveryPage(
            items=(
                _card(1, media_type="tv", year="2025"),
                _card(2, media_type="tv", year="2024"),
            ),
            page=1,
            has_more=False,
            provider=ProviderHealth(name="tmdb", status="healthy"),
        )
        service = FakeDiscoveryService(page)
        with (
            patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True),
            patch(
                "app.agent.domains.discovery.actions.get_discovery_service",
                return_value=service,
            ),
        ):
            result = recommend_discovery(
                {
                    "provider": "tmdb",
                    "media_type": "tv",
                    "year": "2025",
                    "region": "美国",
                    "genre": "科幻",
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(result.data["items"][0]["year"], "2025")
        self.assertEqual(
            result.data["filters"], {"year": "2025", "region": "美国", "genre": "科幻"}
        )
        self.assertEqual(
            service.calls,
            [
                (
                    "tmdb",
                    "discover",
                    "tv",
                    1,
                    {
                        "first_air_date_year": "2025",
                        "with_origin_country": "US",
                        "with_genres": "10765",
                    },
                )
            ],
        )

    def test_disabled_feature_does_not_create_or_call_service(self):
        service = Mock()
        with (
            patch("app.agent.domains.discovery.actions.config.get_bool", return_value=False),
            patch(
                "app.agent.domains.discovery.actions.get_discovery_service",
                return_value=service,
            ) as getter,
        ):
            result = recommend_discovery({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.list_items.assert_not_called()

    def test_tmdb_and_douban_use_fixed_categories_and_safe_allowlist(self):
        for provider, category in (("tmdb", "discover"), ("douban", "recommend")):
            page = DiscoveryPage(
                items=(_card(1, provider=provider), _card(2, provider=provider)),
                page=2,
                has_more=True,
                cached=True,
                stale=True,
                provider=ProviderHealth(
                    name=provider, status="degraded", retry_after=12
                ),
            )
            service = FakeDiscoveryService(page)
            with (
                self.subTest(provider=provider),
                patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.domains.discovery.actions.get_discovery_service",
                    return_value=service,
                ),
            ):
                result = recommend_discovery(
                    {"provider": provider, "media_type": "movie", "page": 2, "limit": 1}
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "success")
            self.assertEqual(service.calls, [(provider, category, "movie", 2, {})])
            self.assertEqual(result.data["total"], 2)
            self.assertEqual(result.data["returned"], 1)
            self.assertTrue(result.data["has_more"])
            self.assertTrue(result.data["cached"])
            self.assertTrue(result.data["stale"])
            self.assertEqual(result.data["provider_status"], "degraded")
            self.assertEqual(result.data["retry_after"], 12)
            serialized = repr(result.to_dict()).casefold()
            for secret in (
                "poster_key",
                "backdrop_key",
                "watchlisted",
                "api_key",
                "token=secret",
                "\\x00",
            ):
                self.assertNotIn(secret, serialized)

    def test_provider_and_unexpected_errors_are_generic(self):

        class FailingService:
            def __init__(self, error):
                self.error = error

            def list_items(self, *_args, **_kwargs):
                raise self.error

        for error, expected_retry in (
            (
                ProviderRateLimited(
                    "请求受限", retry_after=42, detail="Bearer should-not-leak"
                ),
                42,
            ),
            (RuntimeError("should-not-leak"), 0),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("app.agent.domains.discovery.actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.domains.discovery.actions.get_discovery_service",
                    return_value=FailingService(error),
                ),
            ):
                result = recommend_discovery({"provider": "tmdb"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.data["retry_after"], expected_retry)
            self.assertIn(
                result.data["provider_status"],
                {"healthy", "degraded", "disabled", "unavailable", "not_configured"},
            )
            self.assertNotIn("should-not-leak", repr(result.to_dict()))
