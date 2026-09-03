"""Agent 探索收藏、最近探索续句与确认边界回归。"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.discovery_watchlist_actions import (
    get_watchlist_summary,
    list_watchlist_summaries,
)
from app.agent.errors import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.discovery.models import MediaCard
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
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

    def test_read_summaries_only_expose_public_fields(self) -> None:
        number = self._insert()
        with patch(
            "app.agent.discovery_watchlist_actions.config.get_bool", return_value=True
        ):
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
        with (
            patch(
                "app.agent.discovery_watchlist_actions.config.get_bool",
                return_value=True,
            ),
            patch(
                "app.agent.discovery_watchlist_actions.get_discovery_service",
                return_value=provider,
            ),
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
        serialized = json.dumps(
            {"prepared": prepared, "confirmed": confirmed}, ensure_ascii=False
        )
        self.assertNotIn("PRIVATE_POSTER_KEY", serialized)
        self.assertNotIn("api_key=secret", serialized)

    def test_add_confirmation_detects_concurrent_insert(self) -> None:
        service = get_agent_service()
        provider = Mock()
        provider.get_detail.return_value = _card(2)
        with (
            patch(
                "app.agent.discovery_watchlist_actions.config.get_bool",
                return_value=True,
            ),
            patch(
                "app.agent.discovery_watchlist_actions.get_discovery_service",
                return_value=provider,
            ),
        ):
            prepared = service.prepare(
                "discovery.add_watchlist",
                {"provider": "tmdb", "external_id": "8802", "media_type": "movie"},
                owner="owner",
            )
        db.add_media_watchlist("tmdb", "8802", "movie", "并发收藏", "2026", "")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")

    def test_remove_prepare_confirm_and_stale_snapshot(self) -> None:
        number = self._insert(external_id="201", title="待移除")
        service = get_agent_service()
        with patch(
            "app.agent.discovery_watchlist_actions.config.get_bool", return_value=True
        ):
            prepared = service.prepare(
                "discovery.remove_watchlist",
                {"watchlist_number": number},
                owner="owner",
            )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_watchlist SET title=? WHERE id=?", ("状态已变化", number)
            )
        stale = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(stale["result"]["status"], "conflict")
        self.assertIsNotNone(db.get_media_watchlist_by_id(number))
        reset_agent_service_for_tests()
        service = get_agent_service()
        with patch(
            "app.agent.discovery_watchlist_actions.config.get_bool", return_value=True
        ):
            prepared = service.prepare(
                "discovery.remove_watchlist",
                {"watchlist_number": number},
                owner="owner",
            )
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertIsNone(db.get_media_watchlist_by_id(number))
