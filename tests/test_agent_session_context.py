"""Agent 短期会话上下文持久化回归测试。"""

from __future__ import annotations

import json
import threading

from app import database as db
from app.agent.models import ToolResult
from app.agent.recent_download_submissions import RecentDownloadSubmissionStore
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.session_context import SQLiteAgentSessionContextRepository
from tests.support import IsolatedDatabaseTestCase


def _patrol_result() -> ToolResult:
    return ToolResult(
        True,
        "completed",
        "巡检完成",
        data={
            "as_of": "2026-08-03",
            "findings_truncated": False,
            "findings": [
                {
                    "status": "updates_available",
                    "title": "示例剧集",
                    "tmdb_id": "12345",
                    "missing_sample_truncated": False,
                    "missing_sample": [
                        {"season": 2, "episode": 3, "private": "/secret/path"},
                        {"season": 2, "episode": 4, "token": "secret"},
                    ],
                }
            ],
        },
    )


def _resource_result() -> ToolResult:
    return ToolResult(
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

    def test_guangya_fs_change_context_is_persisted_and_guarded(self):
        owner = "guangya-fs-change-owner"
        guard = self.repository.begin_context(
            owner=owner, context_type="guangya_fs_change"
        )
        stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="guangya_fs_change",
            payload={
                "plan_id": "a" * 32,
                "fingerprint": "b" * 64,
                "preview_safe": {"total": 1, "sample_changes": []},
            },
            expires_at=1100.0,
            guard=guard,
        )
        self.assertIsNotNone(stored)
        loaded = self.repository.get_latest(
            owner=owner, context_type="guangya_fs_change", now=1000.0
        )
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.payload["plan_id"], "a" * 32)
        self.assertTrue(
            self.repository.consume_latest_guarded(
                owner=owner,
                context_type="guangya_fs_change",
                guard=type(guard)(stored.generation, stored.revision),
            )
        )

    def test_owner_is_hmac_fingerprinted_and_payload_is_versioned(self):
        owner = "csrf-owner-should-never-be-stored"
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
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
            context_type="patrol",
            payload={"safe": "a"},
            expires_at=1100.0,
        )
        self.repository.append_download(
            owner="session-a", payload={"request_id": 1}, expires_at=1100.0, max_items=8
        )
        self.repository.replace_latest(
            owner="session-b",
            context_type="patrol",
            payload={"safe": "b"},
            expires_at=1100.0,
        )
        self.assertEqual(self.repository.delete_owner(owner="session-a"), 2)
        self.assertIsNone(
            self.repository.get_latest(
                owner="session-a", context_type="patrol", now=1000.0
            )
        )
        self.assertEqual(
            self.repository.list_downloads(owner="session-a", now=1000.0, limit=8), ()
        )
        remaining = self.repository.get_latest(
            owner="session-b", context_type="patrol", now=1000.0
        )
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.payload, {"safe": "b"})

    def test_guarded_context_is_latest_wins_single_use_and_reset_safe(self):
        owner = "guarded-owner"
        first = self.repository.begin_context(
            owner=owner, context_type="discovery_mapping"
        )
        stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="discovery_mapping",
            payload={"safe": "first"},
            expires_at=1100.0,
            guard=first,
        )
        self.assertIsNotNone(stored)
        self.assertGreater(stored.revision, 0)
        self.assertEqual(stored.generation, first.generation)
        second = self.repository.begin_context(
            owner=owner, context_type="discovery_mapping"
        )
        self.assertNotEqual(second.generation, first.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_mapping",
                payload={"safe": "late-first"},
                expires_at=1100.0,
                guard=stored,
            )
        )
        current = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="discovery_mapping",
            payload={"safe": "second"},
            expires_at=1100.0,
            guard=second,
        )
        self.assertIsNotNone(current)
        self.assertTrue(
            self.repository.consume_latest_guarded(
                owner=owner, context_type="discovery_mapping", guard=current
            )
        )
        self.assertFalse(
            self.repository.consume_latest_guarded(
                owner=owner, context_type="discovery_mapping", guard=current
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
        self.assertIsNotNone(scrape_stored)
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
                guard=scrape_stored,
            )
        )

    def test_owner_invalidation_advances_every_guarded_context_type(self):
        owner = "all-context-reset-owner"
        context_types = (
            "patrol",
            "resource_candidates",
            "discovery_candidates",
            "read_operation",
            "local_media_tasks",
            "discovery_mapping",
            "directory_scrape",
            "guangya_rename",
            "guangya_cleanup",
            "guangya_workspace",
            "guangya_fs_change",
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
            context_type="local_media_tasks",
            payload={"safe": "seed"},
            expires_at=1100.0,
        )
        first_snapshot, first_guard = self.repository.begin_context_update(
            owner=owner, context_type="local_media_tasks"
        )
        self.assertIsNotNone(first_snapshot)
        self.assertEqual(first_snapshot.payload, {"safe": "seed"})
        self.assertEqual(first_snapshot.generation, first_guard.generation)
        self.assertEqual(first_snapshot.revision, first_guard.revision)
        second_snapshot, second_guard = self.repository.begin_context_update(
            owner=owner, context_type="local_media_tasks"
        )
        self.assertIsNotNone(second_snapshot)
        self.assertEqual(second_snapshot.payload, {"safe": "seed"})
        self.assertNotEqual(second_guard.generation, first_guard.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="local_media_tasks",
                payload={"safe": "late-first"},
                expires_at=1100.0,
                guard=first_guard,
            )
        )
        stored = self.repository.replace_latest_guarded(
            owner=owner,
            context_type="local_media_tasks",
            payload={"safe": "second"},
            expires_at=1100.0,
            guard=second_guard,
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.payload, {"safe": "second"})

    def test_single_context_invalidation_blocks_guard_without_deleting_others(self):
        owner = "single-context-reset-owner"
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
            payload={"safe": "patrol"},
            expires_at=1100.0,
        )
        _snapshot, guard = self.repository.begin_context_update(
            owner=owner, context_type="local_media_tasks"
        )
        self.assertEqual(
            self.repository.invalidate_context(
                owner=owner, context_type="local_media_tasks"
            ),
            0,
        )
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="local_media_tasks",
                payload={"safe": "late"},
                expires_at=1100.0,
                guard=guard,
            )
        )
        remaining = self.repository.get_latest(
            owner=owner, context_type="patrol", now=1000.0
        )
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.payload, {"safe": "patrol"})

    def test_pruned_epoch_never_reuses_an_old_guard_after_reset(self):
        owner = "guarded-aba-owner"
        stale = self.repository.begin_context(
            owner=owner, context_type="discovery_mapping"
        )
        self.repository.invalidate_owner(owner=owner)
        self.wall[0] += 3601.0
        current = self.repository.begin_context(
            owner=owner, context_type="discovery_mapping"
        )
        self.assertGreater(current.generation, stale.generation)
        self.assertIsNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_mapping",
                payload={"safe": "stale"},
                expires_at=self.wall[0] + 100.0,
                guard=stale,
            )
        )
        self.assertIsNotNone(
            self.repository.replace_latest_guarded(
                owner=owner,
                context_type="discovery_mapping",
                payload={"safe": "current"},
                expires_at=self.wall[0] + 100.0,
                guard=current,
            )
        )

    def test_context_capacity_never_evicts_another_owner_active_flow(self):
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "capacity-test-secret",
            clock=lambda: self.wall[0],
            max_rows=128,
        )
        protected_owner = "protected-flow-owner"
        guard = repository.begin_context(
            owner=protected_owner, context_type="discovery_mapping"
        )
        protected = repository.replace_latest_guarded(
            owner=protected_owner,
            context_type="discovery_mapping",
            payload={"safe": "protected"},
            expires_at=1100.0,
            guard=guard,
        )
        self.assertIsNotNone(protected)
        for index in range(127):
            repository.replace_latest(
                owner=f"capacity-owner-{index}",
                context_type="patrol",
                payload={"index": index},
                expires_at=1100.0,
            )
        with self.assertRaisesRegex(RuntimeError, "容量已满"):
            repository.replace_latest(
                owner="overflow-owner",
                context_type="patrol",
                payload={"overflow": True},
                expires_at=1100.0,
            )
        restored = repository.get_latest(
            owner=protected_owner, context_type="discovery_mapping", now=self.wall[0]
        )
        self.assertIsNotNone(restored)
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
        self.assertIsNotNone(
            repository.replace_latest_guarded(
                owner=active_owner,
                context_type="directory_scrape",
                payload={"safe": "active"},
                expires_at=1100.0,
                guard=active_guard,
            )
        )
        for index in range(128):
            repository.begin_context(
                owner=f"epoch-inactive-owner-{index}", context_type="discovery_mapping"
            )
        with db.get_conn() as conn:
            epoch_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_session_context_epochs"
                ).fetchone()[0]
            )
        self.assertEqual(epoch_count, 128)
        self.assertIsNotNone(
            repository.get_latest(
                owner=active_owner, context_type="directory_scrape", now=self.wall[0]
            )
        )

    def test_expired_malformed_and_oversized_contexts_fail_closed(self):
        owner = "session-a"
        digest = self.repository.owner_digest_for_tests(owner)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digest, "patrol", "{broken", 1100.0, db.now()),
            )
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="patrol", now=self.wall[0]
            )
        )
        with self.assertRaises(ValueError):
            self.repository.replace_latest(
                owner=owner,
                context_type="patrol",
                payload={"large": "x" * (33 * 1024)},
                expires_at=1100.0,
            )
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
            payload={"safe": True},
            expires_at=1005.0,
        )
        self.wall[0] = 1006.0
        self.assertIsNone(
            self.repository.get_latest(
                owner=owner, context_type="patrol", now=self.wall[0]
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
            context_type="patrol",
            payload={"safe": True},
            expires_at=1100.0,
        )
        same_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stable-test-secret", clock=lambda: self.wall[0]
        )
        changed_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "different-test-secret", clock=lambda: self.wall[0]
        )
        self.assertEqual(
            same_secret.get_latest(
                owner="session-a", context_type="patrol", now=self.wall[0]
            ).payload,
            {"safe": True},
        )
        self.assertIsNone(
            changed_secret.get_latest(
                owner="session-a", context_type="patrol", now=self.wall[0]
            )
        )

    def test_valid_shape_tampering_fails_integrity_check(self):
        owner = "session-a"
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
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
                owner=owner, context_type="patrol", now=self.wall[0]
            )
        )

    def test_database_initialization_physically_prunes_expired_context(self):
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digest, "patrol", "{}", 1.0, db.now()),
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

    def test_patrol_and_download_stores_restore_safe_projection_after_recreation(self):
        monotonic = [20.0]
        patrol = RecentPatrolStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        downloads = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        patrol.capture(owner="session-a", result=_patrol_result())
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
        restored_patrol = RecentPatrolStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-a")
        restored_downloads = RecentDownloadSubmissionStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-a")
        self.assertEqual(restored_patrol["options"][0]["episode_sample"], [3, 4])
        self.assertEqual(restored_downloads[0].request_id, 77)
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
        self.assertIsNone(
            RecentPatrolStore(
                repository=self.repository, wall_clock=lambda: self.wall[0]
            ).get(owner="session-b")
        )

    def test_generic_resource_candidates_restore_current_snapshots(self):
        monotonic = [20.0]
        store = RecentResourceCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        store.capture(owner="session-generic", result=_generic_resource_result())
        restored = RecentResourceCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-generic")
        self.assertEqual(restored["candidates"][0]["download_kinds"], ["magnet"])
        self.assertEqual(restored["candidates"][0]["media_title"], "Example")
        self.assertNotIn("magnet:?", repr(restored))

    def test_candidate_snapshots_enforce_bounded_eviction_and_clear_all_rows(self):
        monotonic = [20.0]
        store = RecentResourceCandidateStore(
            repository=self.repository,
            max_snapshots_per_owner=3,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        search_ids = []
        for index in range(5):
            result = _resource_result()
            selected = result.data["search"]["recommendation"]["selected"]
            selected["result_id"] = f"resource-result-{index}"
            selected["title"] = f"Example.S02E{index + 1:02d}.1080p"
            search_ids.append(store.capture(owner="session-bounded", result=result))
        self.assertIsNone(store.get(owner="session-bounded", search_id=search_ids[0]))
        self.assertIsNone(store.get(owner="session-bounded", search_id=search_ids[1]))
        self.assertEqual(
            store.get(owner="session-bounded", search_id=search_ids[-1])["candidates"][
                0
            ]["result_id"],
            "resource-result-4",
        )
        digest = self.repository.owner_digest_for_tests("session-bounded")
        with db.get_conn() as conn:
            persisted = conn.execute(
                "SELECT COUNT(*) AS total FROM agent_session_context WHERE owner_digest=? AND context_type='resource_candidates'",
                (digest,),
            ).fetchone()["total"]
        self.assertEqual(persisted, 3)
        restored = RecentResourceCandidateStore(
            repository=self.repository,
            max_snapshots_per_owner=3,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        self.assertIsNone(
            restored.get(owner="session-bounded", search_id=search_ids[0])
        )
        self.assertTrue(store.clear_owner(owner="session-bounded"))
        self.assertIsNone(
            RecentResourceCandidateStore(
                repository=self.repository,
                max_snapshots_per_owner=3,
                wall_clock=lambda: self.wall[0],
            ).get(owner="session-bounded")
        )
        with db.get_conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS total FROM agent_session_context WHERE owner_digest=? AND context_type='resource_candidates'",
                (digest,),
            ).fetchone()["total"]
        self.assertEqual(remaining, 0)

    def test_patrol_and_download_clear_owner_remove_persisted_rows(self):
        patrol = RecentPatrolStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        )
        downloads = RecentDownloadSubmissionStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        )
        patrol.capture(owner="session-a", result=_patrol_result())
        downloads.capture(owner="session-a", result=_download_result(91))
        self.assertTrue(patrol.clear_owner(owner="session-a"))
        self.assertTrue(downloads.clear_owner(owner="session-a"))
        self.assertIsNone(
            RecentPatrolStore(
                repository=self.repository, wall_clock=lambda: self.wall[0]
            ).get(owner="session-a")
        )
        self.assertEqual(
            RecentDownloadSubmissionStore(
                repository=self.repository, wall_clock=lambda: self.wall[0]
            ).get(owner="session-a"),
            (),
        )
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS total FROM agent_session_context WHERE owner_digest=? AND context_type IN ('patrol','download_submission')",
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

    def test_invalid_store_payload_is_rejected_after_repository_decode(self):
        self.repository.replace_latest(
            owner="session-a",
            context_type="patrol",
            payload={"safe": True},
            expires_at=1100.0,
        )
        restored = RecentPatrolStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-a")
        self.assertIsNone(restored)

    def test_concurrent_patrol_capture_keeps_memory_and_repository_order_aligned(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingRepository:
            def __init__(self):
                self.latest = None

            def replace_latest(self, **kwargs):
                title = kwargs["payload"]["options"][0]["title"]
                if title == "较早巡检":
                    entered.set()
                    release.wait(timeout=2)
                self.latest = kwargs["payload"]

            def get_latest(self, **_kwargs):
                return None

        repository = BlockingRepository()
        store = RecentPatrolStore(repository=repository)
        earlier = _patrol_result()
        earlier.data["findings"][0]["title"] = "较早巡检"
        later = _patrol_result()
        later.data["findings"][0]["title"] = "较新巡检"
        first = threading.Thread(
            target=store.capture, kwargs={"owner": "session-a", "result": earlier}
        )
        second = threading.Thread(
            target=store.capture, kwargs={"owner": "session-a", "result": later}
        )
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(
            store.get(owner="session-a")["options"][0]["title"], "较新巡检"
        )
        self.assertEqual(repository.latest["options"][0]["title"], "较新巡检")

    def test_repository_failure_keeps_existing_in_memory_behavior(self):

        class BrokenRepository:
            def replace_latest(self, **_kwargs):
                raise RuntimeError("unavailable")

            def get_latest(self, **_kwargs):
                raise RuntimeError("unavailable")

        store = RecentPatrolStore(repository=BrokenRepository())
        with self.assertLogs("app.agent.recent_patrol", level="WARNING"):
            store.capture(owner="session-a", result=_patrol_result())
        self.assertEqual(store.get(owner="session-a")["options"][0]["tmdb_id"], "12345")
