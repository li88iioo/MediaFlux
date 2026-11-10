"""共用收藏读取、真实游标/统计快照与原生写确认边界。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import discovery as discovery_repo
from app.agent.errors import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.discovery.models import MediaCard
from app.discovery.service import DiscoveryService
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


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
    def _insert(
        *, external_id: str = "100", title: str = "收藏影片", media_type: str = "movie"
    ) -> int:
        discovery_repo.add_media_watchlist(
            "tmdb", external_id, media_type, title, "2026", "PRIVATE_POSTER_KEY"
        )
        row = discovery_repo.get_media_watchlist("tmdb", external_id, media_type)
        assert row is not None
        return int(row["id"])

    def test_read_list_and_exact_item_keep_business_identities(self):
        number = self._insert()
        listed = DiscoveryService.list_watchlist()
        single = DiscoveryService.get_watchlist(number)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["id"], number)
        self.assertEqual(
            single["item"],
            {
                "id": number,
                "provider": "tmdb",
                "external_id": "100",
                "media_type": "movie",
                "title": "收藏影片",
                "year": "2026",
            },
        )
        self.assertEqual(DiscoveryService.get_watchlist(number + 1), {"item": None})
        self.assertNotIn("PRIVATE_POSTER_KEY", json.dumps(single))

    def test_watchlist_pagination_reads_all_rows_without_false_total(self):
        expected = [
            self._insert(external_id=str(n), title=f"收藏 {n}") for n in range(45)
        ]
        received, arguments = [], {"limit": 20}
        while True:
            page = DiscoveryService.list_watchlist(**arguments)
            ids = [item["id"] for item in page["items"]]
            self.assertEqual(page["total"], len(expected))
            self.assertLessEqual(len(ids), 20)
            if "before_number" in arguments:
                self.assertTrue(
                    all(number < arguments["before_number"] for number in ids)
                )
            received.extend(ids)
            if not page["has_more"]:
                self.assertIsNone(page["next"])
                break
            self.assertEqual(page["next"]["before_number"], ids[-1])
            arguments = page["next"]
        self.assertEqual(received, list(reversed(expected)))
        self.assertEqual(len(DiscoveryService.list_watchlist()["items"]), 45)
        self.assertEqual(
            DiscoveryService.get_watchlist(expected[0])["item"]["id"], expected[0]
        )

    def test_sparse_numbers_and_duplicate_external_ids_keep_provider_identity(self):
        expected = [1_000_000_007 + index * 104_729 for index in range(45)]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_watchlist(id,provider,external_id,media_type,title,year,poster_key,created_at) "
                "VALUES(?,?,'100','tv','同名剧','2026','PRIVATE_POSTER_KEY','2026-11-05')",
                [(number, f"legacy-{index}") for index, number in enumerate(expected)],
            )
        seen, arguments = [], {"limit": 20}
        while True:
            page = DiscoveryService.list_watchlist(**arguments)
            seen.extend(
                (item["id"], item["provider"], item["external_id"])
                for item in page["items"]
            )
            if not page["has_more"]:
                break
            arguments = page["next"]
        self.assertEqual([item[0] for item in seen], list(reversed(expected)))
        self.assertEqual(len({(item[1], item[2]) for item in seen}), len(expected))

    def test_cursor_is_exclusive_and_does_not_shift_after_insert_or_delete(self):
        ids = [self._insert(external_id=str(n)) for n in range(25)]
        first = DiscoveryService.list_watchlist(limit=20)
        cursor = first["next"]["before_number"]
        self._insert(external_id="new")
        discovery_repo.delete_media_watchlist("tmdb", "24", "movie")
        second = DiscoveryService.list_watchlist(**first["next"])
        self.assertEqual(
            [item["id"] for item in second["items"]], list(reversed(ids[:5]))
        )
        self.assertTrue(all(item["id"] < cursor for item in second["items"]))
        self.assertFalse(second["has_more"])
        empty = DiscoveryService.list_watchlist(before_number=ids[0])
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["total"], 25)
        self.assertIsNone(empty["next"])
        self.assertFalse(DiscoveryService.list_watchlist(limit=25)["has_more"])

    def test_all_read_variants_are_local_and_do_not_write_when_discovery_is_disabled(
        self,
    ):
        number = self._insert()
        queries, real_conn = [], db.get_conn

        @contextmanager
        def read_only():
            with real_conn() as conn:
                conn.execute("PRAGMA query_only=ON")
                conn.set_trace_callback(queries.append)
                yield conn

        with (
            patch.object(db, "get_conn", read_only),
            patch("app.discovery.service.config.get_bool", return_value=False),
            patch.object(DiscoveryService, "get_detail") as provider,
        ):
            self.assertEqual(DiscoveryService.list_watchlist()["total"], 1)
            self.assertEqual(DiscoveryService.watchlist_statistics()["total"], 1)
            self.assertIsNotNone(DiscoveryService.get_watchlist(number)["item"])
            provider.assert_not_called()
        self.assertTrue(queries)
        self.assertTrue(
            all(
                query.lstrip().upper().startswith(("SELECT", "WITH"))
                for query in queries
            )
        )

    def test_read_arguments_fail_before_database_queries(self):
        with patch.object(
            db, "get_conn", side_effect=AssertionError("invalid input must not query")
        ):
            for encoded in ("true", '"12"', "12.5", "[]", "{}", "9223372036854775808", "0", "-1"):
                value = json.loads(encoded)
                with self.subTest(cursor=value), self.assertRaises(ValueError):
                    DiscoveryService.list_watchlist(before_number=value)
                with self.subTest(number=value), self.assertRaises(ValueError):
                    DiscoveryService.get_watchlist(value)
            for encoded in ("true", '"20"', "null", "[]", "{}"):
                value = json.loads(encoded)
                with self.subTest(limit=value), self.assertRaises(ValueError):
                    DiscoveryService.list_watchlist(limit=value)

    def test_watchlist_statistics_empty_database(self) -> None:
        self.assertEqual(discovery_repo.get_media_watchlist_statistics(), [])
        result = DiscoveryService.watchlist_statistics()
        self.assertEqual(result, {"total": 0, "by_media_type": []})

    def test_watchlist_statistics_single_type_counts_all_and_limits_latest(
        self,
    ) -> None:
        ids = [self._insert(external_id=str(index)) for index in range(2)]
        first = DiscoveryService.watchlist_statistics()
        ids.extend((self._insert(external_id=str(index)) for index in range(2, 7)))
        second = DiscoveryService.watchlist_statistics()
        for result, expected_ids in ((first, ids[:2]), (second, ids)):
            with self.subTest(total=len(expected_ids)):
                self.assertEqual(result["total"], len(expected_ids))
                self.assertEqual(len(result["by_media_type"]), 1)
                group = result["by_media_type"][0]
                self.assertEqual(group["media_type"], "movie")
                self.assertEqual(group["count"], len(expected_ids))
                self.assertEqual(
                    [item["id"] for item in group["latest"]],
                    list(reversed(expected_ids))[:3],
                )

    def test_watchlist_statistics_sparse_ids_and_historical_types_are_not_omitted(
        self,
    ) -> None:
        rows = [
            (3, "movie"),
            (19, "tv"),
            (47, "movie"),
            (89, "tv"),
            (144, "movie"),
            (233, "movie"),
            (610, "episode"),
            (987, ""),
            (1597, "historical_type_a"),
            (2584, "historical_type_b"),
        ]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_watchlist(id,provider,external_id,media_type,title,created_at) VALUES(?,'tmdb',?,?,?,?)",
                [
                    (
                        number,
                        f"PRIVATE_EXTERNAL_{number}",
                        kind,
                        f"收藏 {number}",
                        f"PRIVATE_TIME_{3000 - number}",
                    )
                    for number, kind in rows
                ],
            )
        result = DiscoveryService.watchlist_statistics()
        groups = result["by_media_type"]
        self.assertEqual(result["total"], len(rows))
        self.assertEqual(sum((group["count"] for group in groups)), len(rows))
        self.assertEqual(len(groups), len({kind for _, kind in rows}))
        self.assertEqual(
            {group["media_type"] for group in groups}, {kind for _, kind in rows}
        )
        for group, kind in zip(groups, sorted({kind for _, kind in rows}), strict=True):
            expected_ids = sorted(
                (number for number, media_type in rows if media_type == kind),
                reverse=True,
            )
            self.assertEqual(group["count"], len(expected_ids))
            self.assertEqual([item["id"] for item in group["latest"]], expected_ids[:3])

    def test_watchlist_statistics_over_repository_page_limit_uses_one_read_query(
        self,
    ) -> None:
        rows = [
            (index * 7 + 10, "tv" if index % 3 == 0 else "movie")
            for index in range(1207)
        ]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_watchlist(id,provider,external_id,media_type,title,year,poster_key,created_at) VALUES(?,'tmdb',?,?,'收藏','2026','PRIVATE_POSTER_KEY','PRIVATE_CREATED_AT')",
                [(number, f"PRIVATE_EXTERNAL_{number}", kind) for number, kind in rows],
            )
        queries = []
        real_conn = db.get_conn

        @contextmanager
        def traced():
            with real_conn() as conn:
                conn.execute("PRAGMA query_only=ON")
                conn.set_trace_callback(queries.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            result = DiscoveryService.watchlist_statistics()
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].count("COUNT(*) OVER"), 2)
        self.assertIn("ROW_NUMBER() OVER", queries[0])
        self.assertNotIn("SELECT *", queries[0])
        self.assertEqual(result["total"], len(rows))
        for group in result["by_media_type"]:
            expected_ids = [
                number for number, kind in reversed(rows) if kind == group["media_type"]
            ]
            self.assertEqual(group["count"], len(expected_ids))
            self.assertEqual([item["id"] for item in group["latest"]], expected_ids[:3])
            for item in group["latest"]:
                self.assertEqual(
                    set(item), {"id", "provider", "media_type", "title", "year"}
                )
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            "poster",
            "external_id",
            "created_at",
            "PRIVATE_",
            "next_before_number",
            "has_more",
        ):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, queries[0])

    def test_watchlist_statistics_insert_update_and_delete_are_reflected(self) -> None:
        movie = self._insert(external_id="movie")
        self._insert(external_id="tv", media_type="tv")
        first = DiscoveryService.watchlist_statistics()
        self.assertEqual(first["total"], 2)
        self.assertTrue(discovery_repo.delete_media_watchlist("tmdb", "movie", "movie"))
        remaining = DiscoveryService.watchlist_statistics()
        self.assertEqual(remaining["total"], 1)
        self.assertEqual(remaining["by_media_type"][0]["media_type"], "tv")
        replacement = self._insert(external_id="replacement", title="新收藏")
        self._insert(external_id="replacement", title="更新标题")
        latest = DiscoveryService.watchlist_statistics()
        self.assertEqual(latest["total"], 2)
        group = next(
            (
                group
                for group in latest["by_media_type"]
                if group["media_type"] == "movie"
            )
        )
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["latest"][0]["id"], replacement)
        self.assertGreater(replacement, movie)
        self.assertEqual(group["latest"][0]["title"], "更新标题")
        discovery_repo.delete_media_watchlist("tmdb", "replacement", "movie")
        discovery_repo.delete_media_watchlist("tmdb", "tv", "tv")
        self.assertEqual(
            DiscoveryService.watchlist_statistics(), {"total": 0, "by_media_type": []}
        )

    def test_watchlist_statistics_counts_and_latest_share_snapshot_during_writes(
        self,
    ) -> None:
        rows = [
            (number, "movie" if number % 2 == 0 else "tv") for number in range(1, 65)
        ]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_watchlist(id,provider,external_id,media_type,title,created_at) VALUES(?,'tmdb',?,?,'收藏','PRIVATE_TIME')",
                [(number, str(number), kind) for number, kind in rows],
            )
        real_conn = db.get_conn
        queries = []
        mutated = False

        def mutate_during_read():
            nonlocal mutated
            if not mutated:
                mutated = True
                with real_conn() as writer:
                    writer.execute(
                        "DELETE FROM media_watchlist WHERE id=?", (rows[-1][0],)
                    )
                    writer.executemany(
                        "INSERT INTO media_watchlist(id,provider,external_id,media_type,title,created_at) VALUES(?,'tmdb',?,'movie','新增','PRIVATE_TIME')",
                        [(number, str(number)) for number in (200, 201)],
                    )
            return 0

        @contextmanager
        def traced():
            with real_conn() as reader:
                reader.execute("PRAGMA query_only=ON")
                reader.set_trace_callback(queries.append)
                reader.set_progress_handler(mutate_during_read, 1000)
                try:
                    yield reader
                finally:
                    reader.set_progress_handler(None, 0)

        with patch.object(db, "get_conn", traced):
            snapshot = DiscoveryService.watchlist_statistics()
        fresh = DiscoveryService.watchlist_statistics()
        self.assertTrue(mutated)
        self.assertEqual(len(queries), 1)
        self.assertEqual(snapshot["total"], len(rows))
        for group in snapshot["by_media_type"]:
            expected_ids = [
                number for number, kind in reversed(rows) if kind == group["media_type"]
            ]
            self.assertEqual(group["count"], len(expected_ids))
            self.assertEqual([item["id"] for item in group["latest"]], expected_ids[:3])
        self.assertEqual(fresh["total"], len(rows) + 1)
        movie_group = next(
            (
                group
                for group in fresh["by_media_type"]
                if group["media_type"] == "movie"
            )
        )
        self.assertEqual(movie_group["count"], len(rows) // 2 + 1)
        self.assertEqual(
            [item["id"] for item in movie_group["latest"]], [201, 200, rows[-3][0]]
        )

    def test_add_prepare_confirm_replay_and_owner_isolation(self) -> None:
        self.enterContext(
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
            )
        )
        service = get_agent_service()
        with (
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool",
                return_value=True,
            ),
            patch.object(
                DiscoveryService, "get_detail", return_value=_card(1)
            ) as detail,
        ):
            prepared = service.prepare(
                "watchlist.add",
                {"provider": "tmdb", "external_id": "8801", "media_type": "movie"},
                owner="owner-a",
            )
        self.assertIsNone(discovery_repo.get_media_watchlist("tmdb", "8801", "movie"))
        confirmation_id = prepared["action_plan"]["plan_id"]
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner-b")
        confirmed = service.confirm(confirmation_id, owner="owner-a")
        detail.assert_called_once_with("tmdb", "movie", "8801")
        self.assertEqual(confirmed["result"]["status"], "success")
        assert (discovery_repo.get_media_watchlist("tmdb", "8801", "movie")) is not None
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner-a")
        serialized = json.dumps(
            {"prepared": prepared, "confirmed": confirmed}, ensure_ascii=False
        )
        self.assertNotIn("PRIVATE_POSTER_KEY", serialized)
        self.assertNotIn("api_key=secret", serialized)

    def test_add_confirmation_detects_concurrent_insert(self) -> None:
        self.enterContext(
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
            )
        )
        service = get_agent_service()
        with (
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool",
                return_value=True,
            ),
            patch.object(DiscoveryService, "get_detail", return_value=_card(2)),
        ):
            prepared = service.prepare(
                "watchlist.add",
                {"provider": "tmdb", "external_id": "8802", "media_type": "movie"},
                owner="owner",
            )
        discovery_repo.add_media_watchlist("tmdb", "8802", "movie", "并发收藏", "2026", "")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")
        stored_row_1 = discovery_repo.get_media_watchlist("tmdb", "8802", "movie")
        assert stored_row_1 is not None
        self.assertEqual(
            stored_row_1["title"], "并发收藏"
        )

    def test_provider_value_error_does_not_expose_private_details_or_prepare_write(
        self,
    ) -> None:
        service = get_agent_service()
        with (
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
            ),
            patch.object(
                DiscoveryService,
                "get_detail",
                side_effect=ValueError(
                    "https://private.invalid/poster?api_key=secret /srv/private/provider.json",
                ),
            ),
            self.assertRaises(AgentToolError) as failure,
        ):
            service.prepare(
                "watchlist.add",
                {"provider": "tmdb", "external_id": "8801", "media_type": "movie"},
                owner="owner",
            )
        self.assertEqual(failure.exception.code, "unavailable")
        self.assertIsNone(discovery_repo.get_media_watchlist("tmdb", "8801", "movie"))
        serialized = str(failure.exception)
        for private in ("private.invalid", "api_key", "secret", "/srv/private"):
            self.assertNotIn(private, serialized)

    def test_remove_prepare_confirm_and_stale_snapshot(self) -> None:
        self.enterContext(
            patch(
                "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
            )
        )
        number = self._insert(external_id="201", title="待移除")
        service = get_agent_service()
        with patch(
            "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
        ):
            prepared = service.prepare(
                "watchlist.remove",
                {"watchlist_id": number},
                owner="owner",
            )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_watchlist SET title=? WHERE id=?", ("状态已变化", number)
            )
        stale = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(stale["result"]["status"], "conflict")
        assert (discovery_repo.get_media_watchlist_by_id(number)) is not None
        reset_agent_service_for_tests()
        service = get_agent_service()
        with patch(
            "app.agent.kernel.ports.sdk_effects.config.get_bool", return_value=True
        ):
            prepared = service.prepare(
                "watchlist.remove",
                {"watchlist_id": number},
                owner="owner",
            )
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "success")
        self.assertIsNone(discovery_repo.get_media_watchlist_by_id(number))
