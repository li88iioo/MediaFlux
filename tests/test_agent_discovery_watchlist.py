"""Agent 探索收藏、最近探索续句与确认边界回归。"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.discovery_watchlist_actions import (
    add_watchlist_arguments,
    add_watchlist_confirmed,
    get_watchlist_summary,
    list_watchlist_summaries,
    prepare_add_watchlist,
    prepare_remove_watchlist,
    remove_watchlist_arguments,
    remove_watchlist_confirmed,
    watchlist_summaries_arguments,
    watchlist_summary_arguments,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.result_projection import project_agent_response_for_llm, public_tool_label
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.discovery.models import MediaCard
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1) -> MediaCard:
    return MediaCard(
        provider="tmdb",
        external_id=str(8800 + index),
        media_type="movie",
        title=f"候选影片 {index}",
        original_title=f"PRIVATE ORIGINAL {index}",
        year="2026",
        overview="PRIVATE OVERVIEW",
        poster_key=f"https://private.example/{index}?api_key=secret",
    )


class AgentDiscoveryWatchlistTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
            conn.execute("DELETE FROM media_watchlist")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _insert(*, external_id: str = "100", title: str = "收藏影片") -> int:
        db.add_media_watchlist(
            "tmdb", external_id, "movie", title, "2026", "PRIVATE_POSTER_KEY"
        )
        row = db.get_media_watchlist("tmdb", external_id, "movie")
        return int(row["id"])

    def test_validators_registry_and_public_projection_are_strict(self) -> None:
        self.assertEqual(watchlist_summaries_arguments({}), {})
        self.assertEqual(
            watchlist_summary_arguments({"watchlist_number": 2}),
            {"watchlist_number": 2},
        )
        self.assertEqual(
            add_watchlist_arguments(
                {"provider": "TMDB", "external_id": "321", "media_type": "MOVIE"}
            ),
            {"provider": "tmdb", "external_id": "321", "media_type": "movie"},
        )
        self.assertEqual(
            remove_watchlist_arguments({"watchlist_number": 3}),
            {"watchlist_number": 3},
        )
        invalid = (
            (watchlist_summaries_arguments, {"limit": 1}),
            (watchlist_summary_arguments, {"watchlist_number": True}),
            (add_watchlist_arguments, {"provider": "tmdb", "external_id": "321"}),
            (
                add_watchlist_arguments,
                {"provider": "tmdb", "external_id": "../private", "media_type": "movie"},
            ),
        )
        for validator, arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                validator(arguments)

        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        self.assertEqual(capabilities["discovery.watchlist_summaries"]["risk"], "read")
        self.assertTrue(capabilities["discovery.add_watchlist"]["requires_confirmation"])
        self.assertTrue(capabilities["discovery.remove_watchlist"]["requires_confirmation"])
        self.assertEqual(public_tool_label("discovery.add_watchlist"), "加入探索收藏")
        projected = project_agent_response_for_llm({
            "tool_call": {"name": "discovery.add_watchlist"},
            "result": {
                "status": "completed",
                "summary": "已加入",
                "data": {
                    "operation": "add",
                    "watchlist_number": 7,
                    "affected": 1,
                    "poster_key": "PRIVATE_POSTER_KEY",
                },
            },
        })
        serialized = json.dumps(projected, ensure_ascii=False)
        self.assertIn("收藏编号", serialized)
        self.assertIn("操作", serialized)
        self.assertNotIn("PRIVATE_POSTER_KEY", serialized)

    def test_read_summaries_only_expose_public_fields(self) -> None:
        number = self._insert()
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True):
            listed = list_watchlist_summaries({})
            single = get_watchlist_summary({"watchlist_number": number})
        self.assertTrue(listed.ok)
        self.assertEqual(single.data["watchlist_number"], number)
        serialized = json.dumps(
            {"listed": listed.to_dict(), "single": single.to_dict()}, ensure_ascii=False
        )
        self.assertNotIn("PRIVATE_POSTER_KEY", serialized)
        self.assertNotIn("external_id", serialized)
        self.assertNotIn("created_at", serialized)

    def test_add_prepare_confirm_replay_and_owner_isolation(self) -> None:
        service = get_agent_service()
        provider = Mock()
        provider.get_detail.return_value = _card(1)
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_watchlist_actions.get_discovery_service",
            return_value=provider,
        ):
            prepared = service.prepare(
                "discovery.add_watchlist",
                {"provider": "tmdb", "external_id": "8801", "media_type": "movie"},
                owner="owner-a",
            )
        self.assertIsNone(db.get_media_watchlist("tmdb", "8801", "movie"))
        confirmation_id = prepared["action_plan"]["plan_id"]
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner-b")
        confirmed = service.confirm(confirmation_id, owner="owner-a")
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertIsNotNone(db.get_media_watchlist("tmdb", "8801", "movie"))
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner-a")
        serialized = json.dumps({"prepared": prepared, "confirmed": confirmed}, ensure_ascii=False)
        self.assertNotIn("PRIVATE_POSTER_KEY", serialized)
        self.assertNotIn("api_key=secret", serialized)

    def test_add_confirmation_detects_concurrent_insert(self) -> None:
        service = get_agent_service()
        provider = Mock()
        provider.get_detail.return_value = _card(2)
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_watchlist_actions.get_discovery_service",
            return_value=provider,
        ):
            prepared = service.prepare(
                "discovery.add_watchlist",
                {"provider": "tmdb", "external_id": "8802", "media_type": "movie"},
                owner="owner",
            )
        db.add_media_watchlist("tmdb", "8802", "movie", "并发收藏", "2026", "")
        confirmed = service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner"
        )
        self.assertEqual(confirmed["result"]["status"], "conflict")

    def test_remove_prepare_confirm_and_stale_snapshot(self) -> None:
        number = self._insert(external_id="201", title="待移除")
        service = get_agent_service()
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True):
            prepared = service.prepare(
                "discovery.remove_watchlist",
                {"watchlist_number": number},
                owner="owner",
            )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_watchlist SET title=? WHERE id=?", ("状态已变化", number)
            )
        stale = service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner"
        )
        self.assertEqual(stale["result"]["status"], "conflict")
        self.assertIsNotNone(db.get_media_watchlist_by_id(number))

        reset_agent_service_for_tests()
        service = get_agent_service()
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True):
            prepared = service.prepare(
                "discovery.remove_watchlist",
                {"watchlist_number": number},
                owner="owner",
            )
        confirmed = service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner"
        )
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertIsNone(db.get_media_watchlist_by_id(number))

    def _orchestrator(self, calls: list[tuple[str, dict]]) -> AgentOrchestrator:
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="discovery.search",
            description="search",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda arguments: ToolResult(
                True,
                "success",
                "搜索完成",
                data={
                    "query": arguments["query"],
                    "items": [
                        {
                            "provider": "tmdb",
                            "external_id": "8801",
                            "media_type": "movie",
                            "title": "候选影片 1",
                            "year": "2026",
                        },
                        {
                            "provider": "tmdb",
                            "external_id": "8802",
                            "media_type": "movie",
                            "title": "候选影片 2",
                            "year": "2026",
                        },
                    ],
                },
            ),
        ))
        registry.register(ToolSpec(
            name="indexer.search_resources",
            description="resource",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda arguments: (
                calls.append(("indexer.search_resources", dict(arguments)))
                or ToolResult(True, "success", "资源搜索完成", data={"items": []})
            ),
        ))
        registry.register(ToolSpec(
            name="discovery.add_watchlist",
            description="add",
            risk=RiskLevel.LOW_WRITE,
            parameters={},
            validator=add_watchlist_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(add_watchlist_confirmed),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(prepare_add_watchlist),
        ))
        registry.register(ToolSpec(
            name="discovery.remove_watchlist",
            description="remove",
            risk=RiskLevel.LOW_WRITE,
            parameters={},
            validator=remove_watchlist_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(remove_watchlist_confirmed),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(prepare_remove_watchlist),
        ))
        registry.register(ToolSpec(
            name="discovery.watchlist_summaries",
            description="list",
            risk=RiskLevel.READ,
            parameters={},
            validator=watchlist_summaries_arguments,
            handler=list_watchlist_summaries,
        ))
        registry.register(ToolSpec(
            name="discovery.get_watchlist_summary",
            description="get",
            risk=RiskLevel.READ,
            parameters={},
            validator=watchlist_summary_arguments,
            handler=get_watchlist_summary,
        ))
        return AgentOrchestrator(registry)

    def test_recent_search_context_routes_add_and_resource_search(self) -> None:
        calls: list[tuple[str, dict]] = []
        agent = self._orchestrator(calls)
        searched = agent.query(
            "在网上找《候选影片》电影", owner="owner", present=False
        )
        self.assertEqual(searched["tool_call"]["name"], "discovery.search")

        provider = Mock()
        provider.get_detail.return_value = _card(2)
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_watchlist_actions.get_discovery_service",
            return_value=provider,
        ):
            prepared = agent.query(
                "把刚才搜索结果第2项加入探索收藏",
                owner="owner",
                present=False,
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "discovery.add_watchlist")
        provider.get_detail.assert_called_once_with("tmdb", "movie", "8802")

        resource = agent.query(
            "把刚才搜索结果第2项找资源", owner="owner", present=False
        )
        self.assertEqual(resource["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(calls[-1][1], {"title": "候选影片 2", "limit": 20})

    def test_recent_search_accepts_implicit_ordinal_followups(self) -> None:
        calls: list[tuple[str, dict]] = []
        agent = self._orchestrator(calls)
        agent.query("在网上找《候选影片》电影", owner="owner", present=False)

        provider = Mock()
        provider.get_detail.return_value = _card(2)
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_watchlist_actions.get_discovery_service",
            return_value=provider,
        ):
            prepared = agent.query("收藏第二个", owner="owner", present=False)
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "discovery.add_watchlist")

        resource = agent.query("搜第1部资源", owner="owner", present=False)
        self.assertEqual(resource["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(calls[-1][1], {"title": "候选影片 1", "limit": 20})

        inspected = agent.query("看看第二个", owner="owner", present=False)
        self.assertEqual(inspected["mode"], "conversation")
        self.assertIn("候选影片 2", inspected["result"]["summary"])

    def test_implicit_ordinal_does_not_steal_quoted_title_or_out_of_range_query(self) -> None:
        agent = self._orchestrator([])
        agent.query("在网上找《候选影片》电影", owner="owner", present=False)

        quoted = agent.query("在网上搜《第二十条》电影", owner="owner", present=False)
        self.assertEqual(quoted["tool_call"]["name"], "discovery.search")
        self.assertEqual(quoted["tool_call"]["arguments"]["query"], "第二十条")

    def test_watchlist_can_be_converted_to_subscription_with_confirmation(self) -> None:
        db.add_media_watchlist(
            "tmdb", "99901", "tv", "收藏剧集", "2026", "PRIVATE_POSTER"
        )
        row = db.get_media_watchlist("tmdb", "99901", "tv")
        number = int(row["id"])
        discovery = Mock()
        discovery.get_detail.return_value = MediaCard(
            provider="tmdb",
            external_id="99901",
            media_type="tv",
            title="收藏剧集",
            year="2026",
        )
        submitted_payloads: list[dict] = []

        async def create_subscription(payload, *, identity_confirmed=False):
            submitted_payloads.append(dict(payload))
            subscription_id = db.add_media_subscription(
                provider=payload["provider"],
                external_id=payload["external_id"],
                tmdb_id=payload["tmdb_id"],
                media_type=payload["media_type"],
                title="收藏剧集",
                year="2026",
                monitor_mode=payload["monitor_mode"],
                seasons=payload["seasons"],
                check_interval_minutes=payload["check_interval_minutes"],
            )
            return {
                "created": True,
                "subscription": {"id": subscription_id, "title": "收藏剧集"},
            }

        subscription_service = Mock()
        subscription_service.create_subscription = create_subscription
        agent = get_agent_service()
        with patch(
            "app.agent.media_subscription_actions.get_discovery_service",
            return_value=discovery,
        ), patch(
            "app.agent.media_subscription_actions.get_media_subscription_service",
            return_value=subscription_service,
        ), patch(
            "app.agent.media_subscription_actions._reload_scheduler",
            return_value=True,
        ):
            prepared = agent.query(
                f"把收藏 {number} 转成每周订阅第 2 季",
                owner="owner",
                present=False,
            )
            self.assertEqual(prepared["mode"], "confirmation_required")
            self.assertEqual(
                prepared["tool_call"]["name"], "media.create_subscription"
            )
            with db.get_conn() as conn:
                self.assertIsNone(conn.execute(
                    "SELECT id FROM media_subscriptions WHERE tmdb_id=? AND media_type=? "
                    "AND deleted_at IS NULL",
                    ("99901", "tv"),
                ).fetchone())
            confirmed = agent.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertEqual(submitted_payloads[0]["check_interval_minutes"], 10080)
        with db.get_conn() as conn:
            subscription = conn.execute(
                "SELECT * FROM media_subscriptions WHERE tmdb_id=? AND media_type=? "
                "AND deleted_at IS NULL",
                ("99901", "tv"),
            ).fetchone()
        self.assertIsNotNone(subscription)
        self.assertEqual(json.loads(subscription["seasons_json"]), [2])
        self.assertEqual(int(subscription["check_interval_minutes"]), 10080)
        self.assertNotIn("PRIVATE_POSTER", repr(prepared) + repr(confirmed))

    def test_watchlist_natural_language_routes_preserve_read_and_write_boundaries(self) -> None:
        calls: list[tuple[str, dict]] = []
        agent = self._orchestrator(calls)

        listed = agent.query("列出探索收藏", present=False)
        self.assertEqual(listed["tool_call"]["name"], "discovery.watchlist_summaries")
        self.assertEqual(listed["tool_call"]["arguments"], {})

        single = agent.query("查看探索收藏编号 2", present=False)
        self.assertEqual(single["tool_call"]["name"], "discovery.get_watchlist_summary")
        self.assertEqual(single["tool_call"]["arguments"], {"watchlist_number": 2})

        number = self._insert(external_id="route-remove", title="待移除影片")
        with patch("app.agent.discovery_watchlist_actions.config.get_bool", return_value=True):
            prepared = agent.query(
                f"移除探索收藏编号 {number}", owner="owner", present=False
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "discovery.remove_watchlist")
        self.assertEqual(prepared["result"]["data"]["watchlist_number"], number)

        ambiguous = agent.query("加入探索收藏", owner="owner", present=False)
        self.assertEqual(ambiguous["result"]["status"], "clarification_required")
        self.assertIsNone(ambiguous["tool_call"])

    def test_recent_resource_search_preserves_tool_rate_identity(self) -> None:
        calls: list[tuple[str, dict]] = []
        agent = self._orchestrator(calls)
        agent.query("在网上找《候选影片》电影", owner="owner", present=False)

        with patch("app.agent.orchestrator.allow_agent_tool", return_value=False) as allow:
            with self.assertRaises(AgentToolError) as limited:
                agent.query(
                    "把刚才搜索结果第1项找资源",
                    owner="owner",
                    query_tool_rate_identity="rate-owner",
                    present=False,
                )

        self.assertEqual(limited.exception.code, "rate_limited")
        allow.assert_called_once_with("rate-owner", "indexer.search_resources")
        self.assertEqual(calls, [])

    def test_recent_context_is_owner_bound_and_reset_clears_it(self) -> None:
        agent = self._orchestrator([])
        agent.query("在网上找《候选影片》电影", owner="owner-a", present=False)
        missing = agent.query(
            "把刚才搜索结果第1项找资源", owner="owner-b", present=False
        )
        self.assertEqual(missing["result"]["status"], "clarification_required")
        agent.reset_session(owner="owner-a")
        cleared = agent.query(
            "把刚才搜索结果第1项找资源", owner="owner-a", present=False
        )
        self.assertEqual(cleared["result"]["status"], "clarification_required")


if __name__ == "__main__":
    import unittest

    unittest.main()
