"""Agent 短期会话上下文持久化回归测试。"""

from __future__ import annotations
from app.agent.session_context import AgentContextWriteGuard
from typing import cast

import json

from app import database as db
from app.agent.domains.downloads.recent_submissions import RecentDownloadSubmissionStore
from app.agent.models import ToolResult
from app.agent.session_context import SQLiteAgentSessionContextRepository
from tests.support import IsolatedDatabaseTestCase


def _resource_result() -> ToolResult:
    result = ToolResult(
        True,
        "completed",
        "搜索完成",
        data={
            "verification": {"season": 2, "episode": 3},
            "search": {
                "recommendation": {
                    "selected": {
                        "result_id": "resource-result-0001",
                        "title": "Example.S02E03.1080p.WEB-DL",
                        "site_id": "nyaa",
                        "site_name": "Nyaa",
                        "rank": 1,
                        "score": 320,
                        "confidence": "high",
                        "match": "exact_episode",
                        "download_state": "ready",
                        "magnet": "magnet:?xt=urn:btih:secret",
                        "path": "/secret/download",
                        "upstream_url": "https://private.example/item",
                    },
                    "alternatives": [],
                }
            },
        },
    )

    candidate = result.data["search"]["recommendation"]["selected"]
    result.data["search"]["items"] = [{**candidate, "quality": {key: candidate[key] for key in ("rank", "score", "confidence", "match")} | {"eligible": True}}]
    return result


def _generic_resource_result() -> ToolResult:
    return ToolResult(
        True,
        "success",
        "搜索完成",
        data={
            "items": [
                {
                    "result_id": "generic-resource-0001",
                    "title": "Example.S01E01.1080p",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "size_text": "1.2 GiB",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                    "media_title": "Example",
                    "episode_label": "S01E01",
                    "subscription_number": 7,
                    "magnet": "magnet:?xt=urn:btih:secret",
                }
            ]
        },
    )


def _discovery_result() -> ToolResult:
    return ToolResult(
        True,
        "success",
        "探索完成",
        data={
            "query": "候选影片",
            "items": [
                {
                    "provider": "tmdb",
                    "external_id": "8801",
                    "media_type": "movie",
                    "title": "候选影片 1",
                    "year": "2026",
                    "overview": "private overview",
                    "poster_key": "https://private.example/poster",
                }
            ],
        },
    )


def _download_result(request_id: int) -> ToolResult:
    return ToolResult(
        True,
        "accepted",
        "提交完成",
        data={
            "request_id": request_id,
            "target": "qb",
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "created": True,
            "duplicate": False,
            "magnet": "magnet:?xt=urn:btih:secret",
            "path": "/secret/download",
            "backend_task_id": "private-task-id",
        },
    )


class AgentSessionContextRepositoryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        self.wall = [1000.0]
        self.repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stable-test-secret", clock=lambda: self.wall[0]
        )

    def tearDown(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")

    def test_owner_is_hmac_fingerprinted_and_payload_is_versioned(self):
        owner = "csrf-owner-should-never-be-stored"
        self.repository.replace_latest(
            owner=owner,
            context_type="read_operation",
            payload={"safe": True},
            expires_at=1100.0,
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT owner_digest,payload FROM agent_session_context"
            ).fetchone()
        self.assertNotEqual(row["owner_digest"], owner)
        self.assertEqual(len(row["owner_digest"]), 64)
        self.assertNotIn(owner, row["payload"])
        envelope = json.loads(row["payload"])
        self.assertEqual(set(envelope), {"auth", "data", "version"})
        self.assertEqual(envelope["data"], {"safe": True})
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(len(envelope["auth"]), 64)

    def test_delete_owner_removes_only_target_session_contexts(self):
        self.repository.replace_latest(
            owner="session-a",
            context_type="read_operation",
            payload={"safe": "a"},
            expires_at=1100.0,
        )
        self.repository.append_download(
            owner="session-a", payload={"request_id": 1}, expires_at=1100.0, max_items=8
        )
        self.repository.replace_latest(
            owner="session-b",
            context_type="read_operation",
            payload={"safe": "b"},
            expires_at=1100.0,
        )
        self.assertEqual(self.repository.delete_owner(owner="session-a"), 2)
        self.assertIsNone(
            self.repository.get_latest(
                owner="session-a", context_type="read_operation", now=1000.0
            )
        )
        self.assertEqual(
            self.repository.list_downloads(owner="session-a", now=1000.0, limit=8), ()
        )
        remaining = self.repository.get_latest(
            owner="session-b", context_type="read_operation", now=1000.0
        )
        assert (remaining) is not None
        self.assertEqual(remaining.payload, {"safe": "b"})

    def test_guarded_context_is_latest_wins_single_use_and_reset_safe(self):
        owner = "guarded-owner"
        first = self.repository.begin_context(
            owner=owner, context_type="discovery_candidates"
        )
        stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="discovery_candidates",
            payload={"safe": "first"},
            expires_at=1100.0,
            guard=first,
        )
        assert (stored) is not None
        self.assertGreater(stored.revision, 0)
        self.assertEqual(stored.generation, first.generation)
        second = self.repository.begin_context(
            owner=owner, context_type="discovery_candidates"
        )
        self.assertNotEqual(second.generation, first.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_candidates",
                payload={"safe": "late-first"},
                expires_at=1100.0,
                guard=cast(AgentContextWriteGuard, stored),
            )
        )
        current = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="discovery_candidates",
            payload={"safe": "second"},
            expires_at=1100.0,
            guard=second,
        )
        assert (current) is not None
        self.assertTrue(
            self.repository.consume_latest_guarded(
                owner=owner, context_type="discovery_candidates", guard=cast(AgentContextWriteGuard, current)
            )
        )
        self.assertFalse(
            self.repository.consume_latest_guarded(
                owner=owner, context_type="discovery_candidates", guard=cast(AgentContextWriteGuard, current)
            )
        )
        scrape = self.repository.begin_context(
            owner=owner, context_type="directory_scrape"
        )
        scrape_stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="directory_scrape",
            payload={"safe": "scrape"},
            expires_at=1100.0,
            guard=scrape,
        )
        assert (scrape_stored) is not None
        self.assertEqual(self.repository.invalidate_owner(owner=owner), 1)
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="directory_scrape", now=1000.0
            )
        )
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="directory_scrape",
                payload={"safe": "late-scrape"},
                expires_at=1100.0,
                guard=cast(AgentContextWriteGuard, scrape_stored),
            )
        )

    def test_owner_invalidation_advances_every_guarded_context_type(self):
        owner = "all-context-reset-owner"
        context_types = (
            "discovery_candidates",
            "read_operation",
            "directory_scrape",
        )
        guards = {
            context_type: self.repository.begin_context(
                owner=owner, context_type=context_type
            )
            for context_type in context_types
        }
        self.repository.invalidate_owner(owner=owner)
        for context_type, guard in guards.items():
            with self.subTest(context_type=context_type):
                self.assertIsNone(
                    self.repository.replace_latest_guarded(
                        owner=owner,
                        context_type=context_type,
                        payload={"safe": context_type},
                        expires_at=1100.0,
                        guard=guard,
                    )
                )

    def test_guarded_update_preserves_snapshot_and_rejects_stale_writer(self):
        owner = "guarded-update-owner"
        self.repository.replace_latest(
            owner=owner,
            context_type="directory_scrape",
            payload={"safe": "seed"},
            expires_at=1100.0,
        )
        first_snapshot, first_guard = self.repository.begin_context_update(
            owner=owner, context_type="directory_scrape"
        )
        assert (first_snapshot) is not None
        self.assertEqual(first_snapshot.payload, {"safe": "seed"})
        self.assertEqual(first_snapshot.generation, first_guard.generation)
        self.assertEqual(first_snapshot.revision, first_guard.revision)
        second_snapshot, second_guard = self.repository.begin_context_update(
            owner=owner, context_type="directory_scrape"
        )
        assert (second_snapshot) is not None
        self.assertEqual(second_snapshot.payload, {"safe": "seed"})
        self.assertNotEqual(second_guard.generation, first_guard.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="directory_scrape",
                payload={"safe": "late-first"},
                expires_at=1100.0,
                guard=first_guard,
            )
        )
        stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="directory_scrape",
            payload={"safe": "second"},
            expires_at=1100.0,
            guard=second_guard,
        )
        assert (stored) is not None
        self.assertEqual(stored.payload, {"safe": "second"})

    def test_single_context_invalidation_blocks_guard_without_deleting_others(self):
        owner = "single-context-reset-owner"
        self.repository.replace_latest(
            owner=owner,
            context_type="read_operation",
            payload={"safe": "read_operation"},
            expires_at=1100.0,
        )
        _snapshot, guard = self.repository.begin_context_update(
            owner=owner, context_type="directory_scrape"
        )
        self.assertEqual(
            self.repository.invalidate_context(
                owner=owner, context_type="directory_scrape"
            ),
            0,
        )
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="directory_scrape",
                payload={"safe": "late"},
                expires_at=1100.0,
                guard=guard,
            )
        )
        remaining = self.repository.get_latest(
            owner=owner, context_type="read_operation", now=1000.0
        )
        assert (remaining) is not None
        self.assertEqual(remaining.payload, {"safe": "read_operation"})

    def test_pruned_epoch_never_reuses_an_old_guard_after_reset(self):
        owner = "guarded-aba-owner"
        stale = self.repository.begin_context(
            owner=owner, context_type="discovery_candidates"
        )
        self.repository.invalidate_owner(owner=owner)
        self.wall[0] += 3601.0
        current = self.repository.begin_context(
            owner=owner, context_type="discovery_candidates"
        )
        self.assertGreater(current.generation, stale.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_candidates",
                payload={"safe": "stale"},
                expires_at=self.wall[0] + 100.0,
                guard=stale,
            )
        )
        assert (self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_candidates",
                payload={"safe": "current"},
                expires_at=self.wall[0] + 100.0,
                guard=current,
            )) is not None

    def test_context_capacity_never_evicts_another_owner_active_flow(self):
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "capacity-test-secret",
            clock=lambda: self.wall[0],
            max_rows=128,
        )
        protected_owner = "protected-flow-owner"
        guard = repository.begin_context(
            owner=protected_owner, context_type="discovery_candidates"
        )
        protected = repository.replace_latest_guarded(
            owner=protected_owner,
            context_type="discovery_candidates",
            payload={"safe": "protected"},
            expires_at=1100.0,
            guard=guard,
        )
        assert (protected) is not None
        for index in range(127):
            repository.replace_latest(
                owner=f"capacity-owner-{index}",
                context_type="read_operation",
                payload={"index": index},
                expires_at=1100.0,
            )
        with self.assertRaisesRegex(RuntimeError, "容量已满"):
            repository.replace_latest(
                owner="overflow-owner",
                context_type="read_operation",
                payload={"overflow": True},
                expires_at=1100.0,
            )
        restored = repository.get_latest(
            owner=protected_owner, context_type="discovery_candidates", now=self.wall[0]
        )
        assert (restored) is not None
        self.assertEqual(restored.payload, {"safe": "protected"})

    def test_epoch_capacity_reclaims_only_inactive_rows(self):
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "epoch-capacity-test-secret",
            clock=lambda: self.wall[0],
            max_epochs=128,
        )
        active_owner = "epoch-active-owner"
        active_guard = repository.begin_context(
            owner=active_owner, context_type="directory_scrape"
        )
        assert (repository.replace_latest_guarded(
                owner=active_owner,
                context_type="directory_scrape",
                payload={"safe": "active"},
                expires_at=1100.0,
                guard=active_guard,
            )) is not None
        for index in range(128):
            repository.begin_context(
                owner=f"epoch-inactive-owner-{index}", context_type="discovery_candidates"
            )
        with db.get_conn() as conn:
            epoch_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_session_context_epochs"
                ).fetchone()[0]
            )
        self.assertEqual(epoch_count, 128)
        assert (repository.get_latest(
                owner=active_owner, context_type="directory_scrape", now=self.wall[0]
            )) is not None

    def test_expired_malformed_and_oversized_contexts_fail_closed(self):
        owner = "session-a"
        digest = self.repository.owner_digest_for_tests(owner)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digest, "read_operation", "{broken", 1100.0, db.now()),
            )
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="read_operation", now=self.wall[0]
            )
        )
        with self.assertRaises(ValueError):
            self.repository.replace_latest(
                owner=owner,
                context_type="read_operation",
                payload={"large": "x" * (33 * 1024)},
                expires_at=1100.0,
            )
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")
        self.repository.replace_latest(
            owner=owner,
            context_type="read_operation",
            payload={"safe": True},
            expires_at=1005.0,
        )
        self.wall[0] = 1006.0
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="read_operation", now=self.wall[0]
            )
        )
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_session_context"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_context_cannot_be_restored_with_a_different_secret(self):
        self.repository.replace_latest(
            owner="session-a",
            context_type="read_operation",
            payload={"safe": True},
            expires_at=1100.0,
        )
        same_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stable-test-secret", clock=lambda: self.wall[0]
        )
        changed_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "different-test-secret", clock=lambda: self.wall[0]
        )
        get_latest_result = same_secret.get_latest(
                owner="session-a", context_type="read_operation", now=self.wall[0]
            )
        assert get_latest_result is not None
        self.assertEqual(
            get_latest_result.payload,
            {"safe": True},
        )
        self.assertIsNone(
            changed_secret.get_latest(
                owner="session-a", context_type="read_operation", now=self.wall[0]
            )
        )

    def test_valid_shape_tampering_fails_integrity_check(self):
        owner = "session-a"
        self.repository.replace_latest(
            owner=owner,
            context_type="read_operation",
            payload={"safe": True},
            expires_at=1100.0,
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,payload FROM agent_session_context"
            ).fetchone()
            envelope = json.loads(row["payload"])
            envelope["data"] = {"safe": False}
            conn.execute(
                "UPDATE agent_session_context SET payload=? WHERE id=?",
                (json.dumps(envelope), int(row["id"])),
            )
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="read_operation", now=self.wall[0]
            )
        )

    def test_database_initialization_physically_prunes_expired_context(self):
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digest, "read_operation", "{}", 1.0, db.now()),
            )
        db.init_db()
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_session_context"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_download_rows_are_owner_isolated_and_bounded(self):
        for request_id in range(1, 5):
            self.repository.append_download(
                owner="session-a",
                payload={"request_id": request_id},
                expires_at=1100.0,
                max_items=2,
            )
        self.repository.append_download(
            owner="session-b", payload={"request_id": 9}, expires_at=1100.0, max_items=2
        )
        first = self.repository.list_downloads(
            owner="session-a", now=self.wall[0], limit=8
        )
        second = self.repository.list_downloads(
            owner="session-b", now=self.wall[0], limit=8
        )
        self.assertEqual([item.payload["request_id"] for item in first], [4, 3])
        self.assertEqual([item.payload["request_id"] for item in second], [9])

    def test_download_store_restores_safe_projection_after_recreation(self):
        monotonic = [20.0]
        downloads = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        downloads.capture(
            owner="session-a",
            result=_download_result(77),
            verification_context={
                "title": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-03",
            },
        )
        restored_downloads = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-a")
        self.assertEqual(restored_downloads[0].request_id, 77)
        assert restored_downloads[0].verification is not None
        self.assertEqual(restored_downloads[0].verification.title, "The Show")
        self.assertEqual(restored_downloads[0].verification.tmdb_id, "12345")
        self.assertEqual(
            (
                restored_downloads[0].verification.season,
                restored_downloads[0].verification.episode,
            ),
            (2, 3),
        )
        self.assertEqual(restored_downloads[0].verification.as_of, "2026-08-03")
        with db.get_conn() as conn:
            persisted_payloads = " ".join(

                    row["payload"]
                    for row in conn.execute(
                        "SELECT payload FROM agent_session_context ORDER BY id"
                    ).fetchall()

            )
        for forbidden in ("magnet:", "/secret", "private.example", "private-task-id"):
            self.assertNotIn(forbidden, persisted_payloads)



    def test_download_clear_owner_removes_persisted_rows(self):
        downloads = RecentDownloadSubmissionStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        )
        downloads.capture(owner="session-a", result=_download_result(91))
        self.assertTrue(downloads.clear_owner(owner="session-a"))
        self.assertEqual(
            RecentDownloadSubmissionStore(
                repository=self.repository, wall_clock=lambda: self.wall[0]
            ).get(owner="session-a"),
            (),
        )
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS total FROM agent_session_context WHERE owner_digest=? AND context_type='download_submission'",
                (digest,),
            ).fetchone()["total"]
        self.assertEqual(remaining, 0)

    def test_legacy_and_malformed_download_payloads_are_rejected(self):
        legacy = {
            "request_id": 81,
            "target": "qb",
            "dispatch_status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "created": True,
            "duplicate": False,
            "result_status": "accepted",
            "captured_at": "2026-08-03T12:00:00+08:00",
        }
        malformed = {
            **legacy,
            "request_id": 82,
            "verification": {
                "title": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "9999-12-31",
            },
        }
        self.repository.append_download(
            owner="session-a", payload=legacy, expires_at=1100.0, max_items=8
        )
        self.repository.append_download(
            owner="session-a", payload=malformed, expires_at=1100.0, max_items=8
        )
        restored = RecentDownloadSubmissionStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-a")
        self.assertEqual(restored, ())

    def test_download_capture_after_recreation_keeps_older_persisted_records(self):
        monotonic = [20.0]
        first = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        first.capture(owner="session-a", result=_download_result(1))
        first.capture(owner="session-a", result=_download_result(2))
        recreated = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        recreated.capture(owner="session-a", result=_download_result(3))
        self.assertEqual(
            [item.request_id for item in recreated.get(owner="session-a")], [3, 2, 1]
        )
