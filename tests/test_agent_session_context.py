"""Agent 短期会话上下文持久化回归测试。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.models import ToolResult
from app.agent.owner_routes import web_agent_owner
from app.agent.recent_discovery_candidates import RecentDiscoveryCandidateStore
from app.agent.recent_download_submissions import RecentDownloadSubmissionStore
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.rate_limit import agent_rate_limiter
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _patrol_result() -> ToolResult:
    return ToolResult(
        True,
        "completed",
        "巡检完成",
        data={
            "as_of": "2026-08-03",
            "findings_truncated": False,
            "findings": [{
                "status": "updates_available",
                "title": "示例剧集",
                "tmdb_id": "12345",
                "missing_sample_truncated": False,
                "missing_sample": [
                    {"season": 2, "episode": 3, "private": "/secret/path"},
                    {"season": 2, "episode": 4, "token": "secret"},
                ],
            }],
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


def _discovery_result() -> ToolResult:
    return ToolResult(
        True,
        "success",
        "探索完成",
        data={
            "query": "候选影片",
            "items": [{
                "provider": "tmdb",
                "external_id": "8801",
                "media_type": "movie",
                "title": "候选影片 1",
                "year": "2026",
                "overview": "private overview",
                "poster_key": "https://private.example/poster",
            }],
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
        self.wall = [1_000.0]
        self.repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stable-test-secret",
            clock=lambda: self.wall[0],
        )

    def tearDown(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")

    def test_owner_is_hmac_fingerprinted_and_payload_is_versioned(self):
        owner = "csrf-owner-should-never-be-stored"
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
            payload={"safe": True},
            expires_at=1_100.0,
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
            expires_at=1_100.0,
        )
        self.repository.append_download(
            owner="session-a",
            payload={"request_id": 1},
            expires_at=1_100.0,
            max_items=8,
        )
        self.repository.replace_latest(
            owner="session-b",
            context_type="patrol",
            payload={"safe": "b"},
            expires_at=1_100.0,
        )

        self.assertEqual(self.repository.delete_owner(owner="session-a"), 2)
        self.assertIsNone(self.repository.get_latest(
            owner="session-a", context_type="patrol", now=1_000.0
        ))
        self.assertEqual(self.repository.list_downloads(
            owner="session-a", now=1_000.0, limit=8
        ), ())
        remaining = self.repository.get_latest(
            owner="session-b", context_type="patrol", now=1_000.0
        )
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.payload, {"safe": "b"})

    def test_expired_malformed_and_oversized_contexts_fail_closed(self):
        owner = "session-a"
        digest = self.repository.owner_digest_for_tests(owner)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
                (digest, "patrol", "{broken", 1_100.0, db.now()),
            )
        self.assertIsNone(self.repository.get_latest(
            owner=owner, context_type="patrol", now=self.wall[0]
        ))

        with self.assertRaises(ValueError):
            self.repository.replace_latest(
                owner=owner,
                context_type="patrol",
                payload={"large": "x" * (33 * 1024)},
                expires_at=1_100.0,
            )

        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
            payload={"safe": True},
            expires_at=1_005.0,
        )
        self.wall[0] = 1_006.0
        self.assertIsNone(self.repository.get_latest(
            owner=owner, context_type="patrol", now=self.wall[0]
        ))
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
            expires_at=1_100.0,
        )
        same_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stable-test-secret",
            clock=lambda: self.wall[0],
        )
        changed_secret = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "different-test-secret",
            clock=lambda: self.wall[0],
        )
        self.assertEqual(
            same_secret.get_latest(
                owner="session-a", context_type="patrol", now=self.wall[0]
            ).payload,
            {"safe": True},
        )
        self.assertIsNone(changed_secret.get_latest(
            owner="session-a", context_type="patrol", now=self.wall[0]
        ))

    def test_valid_shape_tampering_fails_integrity_check(self):
        owner = "session-a"
        self.repository.replace_latest(
            owner=owner,
            context_type="patrol",
            payload={"safe": True},
            expires_at=1_100.0,
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
        self.assertIsNone(self.repository.get_latest(
            owner=owner, context_type="patrol", now=self.wall[0]
        ))

    def test_database_initialization_physically_prunes_expired_context(self):
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
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
                expires_at=1_100.0,
                max_items=2,
            )
        self.repository.append_download(
            owner="session-b",
            payload={"request_id": 9},
            expires_at=1_100.0,
            max_items=2,
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
            (restored_downloads[0].verification.season, restored_downloads[0].verification.episode),
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
        self.assertIsNone(RecentPatrolStore(
            repository=self.repository,
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-b"))

    def test_resource_and_discovery_candidates_restore_safe_snapshots(self):
        monotonic = [20.0]
        resource = RecentResourceCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        discovery = RecentDiscoveryCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        )
        resource.capture(owner="session-a", result=_resource_result())
        discovery.capture(owner="session-a", result=_discovery_result())

        restored_resource = RecentResourceCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-a")
        restored_discovery = RecentDiscoveryCandidateStore(
            repository=self.repository,
            clock=lambda: monotonic[0],
            wall_clock=lambda: self.wall[0],
        ).get(owner="session-a")

        self.assertEqual(restored_resource["candidates"][0]["result_id"], "resource-result-0001")
        self.assertEqual(restored_discovery["candidates"][0]["title"], "候选影片 1")
        with db.get_conn() as conn:
            payloads = " ".join(
                row["payload"] for row in conn.execute(
                    "SELECT payload FROM agent_session_context "
                    "WHERE context_type IN ('resource_candidates','discovery_candidates')"
                ).fetchall()
            )
        for forbidden in ("magnet:", "/secret", "private.example", "overview"):
            self.assertNotIn(forbidden, payloads)
        self.assertIsNone(RecentResourceCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-b"))
        self.assertIsNone(RecentDiscoveryCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-b"))

    def test_candidate_restore_rejects_tampered_payload_and_clear_is_type_scoped(self):
        resource = RecentResourceCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        )
        discovery = RecentDiscoveryCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        )
        resource.capture(owner="session-a", result=_resource_result())
        discovery.capture(owner="session-a", result=_discovery_result())
        digest = self.repository.owner_digest_for_tests("session-a")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,payload FROM agent_session_context "
                "WHERE owner_digest=? AND context_type='resource_candidates'",
                (digest,),
            ).fetchone()
            envelope = json.loads(row["payload"])
            envelope["data"]["candidates"][0]["title"] = "被篡改"
            conn.execute(
                "UPDATE agent_session_context SET payload=? WHERE id=?",
                (json.dumps(envelope, ensure_ascii=False), row["id"]),
            )
        self.assertIsNone(RecentResourceCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-a"))
        self.assertTrue(discovery.clear_owner(owner="session-a"))
        self.assertIsNone(RecentDiscoveryCandidateStore(
            repository=self.repository, wall_clock=lambda: self.wall[0]
        ).get(owner="session-a"))

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
            owner="session-a", payload=legacy, expires_at=1_100.0, max_items=8
        )
        self.repository.append_download(
            owner="session-a", payload=malformed, expires_at=1_100.0, max_items=8
        )
        restored = RecentDownloadSubmissionStore(
            repository=self.repository,
            wall_clock=lambda: self.wall[0],
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
            [item.request_id for item in recreated.get(owner="session-a")],
            [3, 2, 1],
        )

    def test_invalid_store_payload_is_rejected_after_repository_decode(self):
        self.repository.replace_latest(
            owner="session-a",
            context_type="patrol",
            payload={"safe": True},
            expires_at=1_100.0,
        )
        restored = RecentPatrolStore(
            repository=self.repository,
            wall_clock=lambda: self.wall[0],
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
        self.assertEqual(store.get(owner="session-a")["options"][0]["title"], "较新巡检")
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
class AgentServiceSessionContextTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")

    def test_service_singleton_recreation_restores_restart_safe_context(self):
        owner = "stable-session-owner"
        first = get_agent_service()
        first.recent_patrol_store.capture(owner=owner, result=_patrol_result())
        first.recent_resource_store.capture(owner=owner, result=_resource_result())
        first.recent_discovery_store.capture(owner=owner, result=_discovery_result())
        first.recent_download_store.capture(owner=owner, result=_download_result(101))

        reset_agent_service_for_tests()
        second = get_agent_service()
        self.assertEqual(second.recent_patrol_store.get(owner=owner)["options"][0]["season"], 2)
        self.assertEqual(
            second.recent_resource_store.get(owner=owner)["candidates"][0]["position"], 1
        )
        self.assertEqual(
            second.recent_discovery_store.get(owner=owner)["candidates"][0]["title"],
            "候选影片 1",
        )
        self.assertEqual(second.recent_download_store.get(owner=owner)[0].request_id, 101)




class AgentSessionContextAPITests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_session_context")

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)
    def test_http_query_restores_patrol_context_after_service_reset(self):
        csrf = self._login()
        get_agent_service().recent_patrol_store.capture(owner=web_agent_owner(csrf, session_id="session_context_http_0001"), result=_patrol_result())
        reset_agent_service_for_tests()
        audit = ToolResult(
            True,
            "updates_available",
            "audit",
            data={
                "title": "示例剧集",
                "tmdb_id": "12345",
                "missing_count": 2,
                "missing_sample": [
                    {"season": 2, "episode": 3},
                    {"season": 2, "episode": 4},
                ],
                "missing_sample_truncated": False,
            },
        )
        searched = ToolResult(True, "success", "searched", data={
            "query": "示例剧集 S02E03",
            "items": [],
            "sites_attempted": [],
            "sites_succeeded": [],
            "errors": [],
            "partial": False,
            "cached": False,
            "has_more": False,
        })
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=audit,
        ), patch(
            "app.agent.episode_resource_actions.search_resources",
            return_value=searched,
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"message": "把刚才巡检发现的缺集找资源", "session_id": "session_context_http_0001"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["tool_call"]["name"],
            "library.search_missing_season_resources",
        )

    def test_http_query_restores_resource_choice_but_reports_expired_handle(self):
        csrf = self._login()
        get_agent_service().recent_resource_store.capture(
            owner=web_agent_owner(csrf, session_id="session_context_http_0001"), result=_resource_result()
        )
        reset_agent_service_for_tests()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"message": "下载刚才推荐的第 1 个到 qB", "session_id": "session_context_http_0001"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["status"], "precondition_failed")
        self.assertIn("重新搜索", " ".join(result.get("suggestions") or []))

    def test_http_query_restores_download_context_after_service_reset(self):
        csrf = self._login()
        request_id, _ = db.create_download_request(
            "persistent-recent-download", "magnet", origin="agent"
        )
        db.update_download_request(
            request_id,
            targets="qb",
            status="submitted",
            qb_status="submitted",
        )
        get_agent_service().recent_download_store.capture(
            owner=web_agent_owner(csrf, session_id="session_context_http_0001"),
            result=_download_result(request_id),
        )
        reset_agent_service_for_tests()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"message": "刚才下载到哪了", "session_id": "session_context_http_0001"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["result"]["data"]
        self.assertEqual(data["phase"], "submitted")
        self.assertNotIn("request_id", data)
