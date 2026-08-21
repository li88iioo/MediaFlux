"""Agent 媒体评分查询与跨轮媒体身份承接回归测试。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.media_rating_actions import (
    _rating_from_douban_html,
    _select_card,
    _web_rating,
    lookup_media_rating,
    media_rating_arguments,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, contextual_media_rating_request
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import build_tool_registry
from app.discovery.models import MediaCard
from app.discovery.search import DiscoverySearchResult


def _card(
    *,
    title: str = "九门",
    media_type: str = "tv",
    year: str = "2026",
    rating: float | None = 7.6,
    external_id: str = "12345678",
) -> MediaCard:
    return MediaCard(
        provider="douban",
        external_id=external_id,
        media_type=media_type,
        title=title,
        original_title="Jiu Men" if title == "九门" else "",
        year=year,
        rating=rating,
        rating_source="douban",
    )


def _search_result(
    *items: MediaCard,
    errors: tuple[dict, ...] = (),
    succeeded: tuple[str, ...] = ("douban",),
) -> DiscoverySearchResult:
    return DiscoverySearchResult(
        query="九门",
        page=1,
        items=tuple(items),
        has_more=False,
        providers_attempted=("douban",),
        providers_succeeded=succeeded,
        errors=errors,
    )


class MediaRatingArgumentTests(unittest.TestCase):
    def test_arguments_are_strict_and_normalized(self):
        self.assertEqual(
            media_rating_arguments({
                "query": "  九門  ",
                "media_type": "TV",
                "year": 2026,
                "allow_web_fallback": False,
            }),
            {
                "query": "九門",
                "media_type": "tv",
                "year": "2026",
                "allow_web_fallback": False,
            },
        )
        invalid = (
            None,
            {},
            {"query": ""},
            {"query": "x", "unknown": True},
            {"query": "x", "media_type": "anime"},
            {"query": "x", "year": True},
            {"query": "x", "year": "26"},
            {"query": "x", "allow_web_fallback": 1},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AgentToolError):
                media_rating_arguments(value)  # type: ignore[arg-type]

    def test_arguments_reject_credentials_before_network_access(self):
        with self.assertRaises(AgentToolError) as raised:
            media_rating_arguments({"query": "九门 api_key=abcdefgh123456"})
        self.assertEqual(raised.exception.code, "sensitive_external_input")

    def test_ambiguous_exact_titles_are_not_guessed(self):
        self.assertIsNone(_select_card(
            (_card(media_type="tv", year="2026"), _card(media_type="movie", year="2025")),
            query="九门",
            media_type="",
            year="",
        ))
        selected = _select_card(
            (_card(media_type="tv", year="2026"), _card(media_type="movie", year="2025")),
            query="九门",
            media_type="tv",
            year="2026",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.media_type, "tv")


class MediaRatingExecutionTests(unittest.TestCase):
    @patch("app.agent.media_rating_actions.config.get_bool", return_value=True)
    @patch("app.agent.media_rating_actions.get_discovery_search_service")
    @patch("app.agent.media_rating_actions.get_discovery_service")
    @patch("app.agent.media_rating_actions.search_web")
    def test_structured_search_rating_wins_without_detail_or_web(
        self, web_search, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(_card(rating=8.1))

        result = lookup_media_rating({"query": "九门", "media_type": "tv", "year": "2026"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["rating"], 8.1)
        self.assertEqual(result.data["source_method"], "douban_search")
        self.assertIn("电视剧", result.summary)
        detail_factory.assert_not_called()
        web_search.assert_not_called()

    @patch("app.agent.media_rating_actions.config.get_bool", return_value=True)
    @patch("app.agent.media_rating_actions.get_discovery_search_service")
    @patch("app.agent.media_rating_actions.get_discovery_service")
    @patch("app.agent.media_rating_actions.search_web")
    def test_detail_supplies_rating_when_search_card_has_none(
        self, web_search, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(_card(rating=None))
        detail_factory.return_value.get_detail.return_value = _card(rating=7.4)

        result = lookup_media_rating({"query": "九门", "media_type": "tv", "year": "2026"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["source_method"], "douban_detail")
        detail_factory.return_value.get_detail.assert_called_once_with("douban", "tv", "12345678")
        web_search.assert_not_called()

    @patch("app.agent.media_rating_actions.config.get_bool", return_value=False)
    @patch("app.agent.media_rating_actions.search_web")
    def test_controlled_web_fallback_accepts_only_verified_douban_subject(
        self, web_search, _disabled
    ):
        web_search.return_value = ToolResult(
            True,
            "success",
            "找到网页结果",
            data={"results": [{
                "title": "九门 (豆瓣)",
                "url": "https://movie.douban.com/subject/12345678/",
                "source": "movie.douban.com",
                "snippet": "九门 电视剧 2026 豆瓣评分 7.7",
            }]},
        )

        result = lookup_media_rating({"query": "九门", "media_type": "tv", "year": "2026"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["rating"], 7.7)
        self.assertEqual(result.data["source_method"], "web_search")
        self.assertTrue(result.data["web_fallback_used"])


    def test_controlled_fetch_extracts_rating_only_from_matching_douban_page(self):
        html = """
        <html><head>
          <meta property="og:title" content="九门 (豆瓣)">
          <meta property="og:description" content="九门 电视剧 2026">
        </head><body><strong class="ll rating_num" property="v:average">7.8</strong></body></html>
        """
        self.assertEqual(
            _rating_from_douban_html(
                html, query="九门", year="2026", media_type="tv"
            ),
            7.8,
        )
        self.assertIsNone(
            _rating_from_douban_html(html, query="老九门", year="2026")
        )
        self.assertIsNone(
            _rating_from_douban_html(html, query="九门", year="2025")
        )
        self.assertIsNone(
            _rating_from_douban_html(
                html.replace("电视剧", "电影"),
                query="九门",
                year="2026",
                media_type="tv",
            )
        )

    @patch("app.agent.media_rating_actions._fetch_douban_rating", return_value=7.9)
    @patch("app.agent.media_rating_actions.search_web")
    def test_web_fallback_fetches_verified_subject_when_snippet_has_no_score(
        self, web_search, fetch_rating
    ):
        web_search.return_value = ToolResult(
            True,
            "success",
            "网页结果",
            data={"results": [{
                "title": "九门 (豆瓣)",
                "url": "https://movie.douban.com/subject/12345678/",
                "source": "movie.douban.com",
                "snippet": "九门 电视剧 2026 剧情简介",
            }]},
        )

        result = _web_rating(query="九门", media_type="tv", year="2026")

        self.assertIsNotNone(result)
        self.assertEqual(result[0], 7.9)
        self.assertEqual(result[2], "web_fetch")
        fetch_rating.assert_called_once_with(
            "https://movie.douban.com/subject/12345678/",
            query="九门",
            year="2026",
            media_type="tv",
        )

    @patch("app.agent.media_rating_actions.search_web")
    def test_web_fallback_rejects_wrong_host_path_year_and_media_type(self, web_search):
        web_search.return_value = ToolResult(
            True,
            "success",
            "网页结果",
            data={"results": [
                {
                    "title": "九门",
                    "url": "https://evil.example/subject/123/",
                    "snippet": "九门 电视剧 2026 豆瓣评分 9.9",
                },
                {
                    "title": "九门",
                    "url": "https://movie.douban.com/search?q=九门",
                    "snippet": "九门 电视剧 2026 豆瓣评分 9.8",
                },
                {
                    "title": "九门 (豆瓣)",
                    "url": "https://movie.douban.com/subject/321/",
                    "snippet": "九门 电影 2025 豆瓣评分 8.8",
                },
                {
                    "title": "老九门 (豆瓣)",
                    "url": "https://movie.douban.com/subject/654/",
                    "snippet": "老九门 电视剧 2026 豆瓣评分 9.9",
                },
            ]},
        )
        self.assertIsNone(_web_rating(query="九门", media_type="tv", year="2026"))
        self.assertIsNone(_web_rating(query="九门", media_type="", year="2026"))

    @patch("app.agent.media_rating_actions.config.get_bool", return_value=True)
    @patch("app.agent.media_rating_actions.get_discovery_search_service")
    @patch("app.agent.media_rating_actions.search_web")
    def test_successful_empty_provider_is_not_reported_as_unavailable(
        self, web_search, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result()
        result = lookup_media_rating({
            "query": "九门", "media_type": "tv", "year": "2026", "allow_web_fallback": False,
        })
        self.assertEqual(result.status, "not_found")
        self.assertFalse(result.data["web_fallback_used"])
        web_search.assert_not_called()

    @patch("app.agent.media_rating_actions.config.get_bool", return_value=True)
    @patch("app.agent.media_rating_actions.get_discovery_search_service")
    @patch("app.agent.media_rating_actions.search_web")
    def test_total_provider_failure_is_unavailable_without_optional_fallback(
        self, web_search, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(
            errors=({"provider": "douban", "code": "timeout"},),
            succeeded=(),
        )
        result = lookup_media_rating({
            "query": "九门", "media_type": "tv", "year": "2026", "allow_web_fallback": False,
        })
        self.assertEqual(result.status, "unavailable")
        web_search.assert_not_called()

    @patch("app.agent.media_rating_actions.config.get_bool", return_value=False)
    @patch("app.agent.media_rating_actions.search_web")
    def test_unknown_media_type_requires_clarification_and_skips_web(self, web_search, _disabled):
        result = lookup_media_rating({"query": "九门"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertIn("电影还是电视剧", result.summary)
        web_search.assert_not_called()


class MediaRatingContextTests(unittest.TestCase):
    def setUp(self):
        self.series_context = [{
            "role": "assistant",
            "text": "《九门》共 22 集。",
            "tool_name": "library.count_series_episodes",
            "status": "success",
            "media_context": {"title": "九门", "year": "2026", "media_type": "tv"},
        }]

    def test_followup_inherits_verified_media_identity(self):
        self.assertEqual(
            contextual_media_rating_request("这部剧的豆瓣评分", self.series_context),
            {
                "query": "九门",
                "media_type": "tv",
                "year": "2026",
                "allow_web_fallback": True,
            },
        )

    def test_type_correction_and_retry_keep_same_identity(self):
        failed_context = self.series_context + [{
            "role": "assistant",
            "text": "暂未找到可核验评分。",
            "tool_name": "discovery.lookup_rating",
            "status": "not_found",
            "media_context": {"title": "九门", "year": "2026", "media_type": "tv"},
        }]
        self.assertEqual(
            contextual_media_rating_request("九门电视剧", failed_context),
            {
                "query": "九门", "media_type": "tv", "year": "2026", "allow_web_fallback": True,
            },
        )
        self.assertEqual(
            contextual_media_rating_request("重试", failed_context),
            {
                "query": "九门", "media_type": "tv", "year": "2026", "allow_web_fallback": True,
            },
        )

        conversational_gap = failed_context + [
            {"role": "user", "text": "谢谢"},
            {"role": "assistant", "text": "不客气，还想了解什么？"},
        ]
        self.assertEqual(
            contextual_media_rating_request("重试", conversational_gap),
            {
                "query": "九门", "media_type": "tv", "year": "2026",
                "allow_web_fallback": True,
            },
        )

        stale_topic = conversational_gap + [
            {"role": "user", "text": "下载队列怎么样"},
            {
                "role": "assistant",
                "text": "下载队列正常。",
                "tool_name": "downloads.status",
            },
        ]
        self.assertIsNone(contextual_media_rating_request("重试", stale_topic))
        self.assertIsNone(contextual_media_rating_request("电视剧", stale_topic))

    def test_summary_media_context_survives_plain_conversation(self):
        context = [{
            "role": "summary",
            "text": "之前在看《九门》。",
            "media_context": {"title": "九门", "year": "2026", "media_type": "tv"},
        }, {
            "role": "assistant",
            "text": "可以，继续说。",
        }]
        self.assertEqual(
            contextual_media_rating_request("这部剧评分呢", context),
            {
                "query": "九门", "media_type": "tv", "year": "2026",
                "allow_web_fallback": True,
            },
        )

    def test_new_title_does_not_inherit_stale_year_or_type(self):
        request = contextual_media_rating_request("老九门的豆瓣评分", self.series_context)
        self.assertEqual(request, {"query": "老九门", "allow_web_fallback": True})

    def test_orchestrator_routes_followup_to_dedicated_tool(self):
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="discovery.lookup_rating",
            description="rating",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "《九门》的豆瓣评分是 7.7 分。")
            ),
            validator=lambda arguments: dict(arguments),
        ))

        response = AgentOrchestrator(registry).query(
            "这部剧的豆瓣评分",
            conversation_context=self.series_context,
            present=False,
        )

        self.assertEqual(response["tool_call"]["name"], "discovery.lookup_rating")
        self.assertEqual(calls, [{
            "query": "九门", "media_type": "tv", "year": "2026", "allow_web_fallback": True,
        }])

    def test_episode_audit_followup_inherits_verified_series_identity(self):
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.audit_episodes",
            description="episode audit",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "audit complete")
            ),
            validator=lambda arguments: dict(arguments),
        ))
        agent = AgentOrchestrator(registry)

        response = agent.query(
            "这部剧有没有缺集",
            conversation_context=self.series_context,
            present=False,
        )
        explicit = agent.query(
            "检查《老九门》有没有缺集",
            conversation_context=self.series_context,
            present=False,
        )

        self.assertEqual(response["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(explicit["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(calls, [{"query": "九门"}, {"query": "老九门"}])


    def test_resource_search_followup_inherits_verified_series_identity(self):
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="indexer.search_resources",
            description="resource search",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "resource search complete")
            ),
            validator=lambda arguments: dict(arguments),
        ))

        response = AgentOrchestrator(registry).query(
            "搜索这部剧资源",
            conversation_context=self.series_context,
            present=False,
        )

        self.assertEqual(response["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(calls, [{"title": "九门", "limit": 20}])

    def test_registry_exposes_safe_read_tool(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["discovery.lookup_rating"]["risk"], "read")
        self.assertFalse(capabilities["discovery.lookup_rating"]["requires_confirmation"])
        self.assertEqual(registry.risk_for("discovery.lookup_rating"), RiskLevel.READ)


if __name__ == "__main__":
    unittest.main()
