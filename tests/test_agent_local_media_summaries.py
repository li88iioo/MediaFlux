"""本地媒体待确认队列与终态历史的安全 Agent 摘要。"""
from __future__ import annotations

from contextlib import nullcontext
import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.local_media_actions import (
    local_media_history_arguments,
    local_media_review_queue_arguments,
    summarize_local_media_history,
    summarize_local_media_review_queue,
)
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class LocalMediaSummaryUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
        reset_agent_service_for_tests()

    def tearDown(self):
        reset_agent_service_for_tests()

    @staticmethod
    def _task(*, status: str, trigger: str, stamp: str, owner: str = "admin",
              suffix: str = "one") -> int:
        source_id = db.create_local_media_source(
            name=f"PRIVATE-SOURCE-{suffix}",
            qb_profile="qb",
            qb_path_prefix=f"/private/qb/{suffix}",
            local_root=f"/private/local/{suffix}",
            owner=owner,
        )
        task_id = db.create_local_media_task(
            source_id,
            f"PRIVATE-HASH-{suffix}",
            f"/private/TITLE-{suffix}.mkv",
            owner=owner,
            trigger=trigger,
        )
        db.update_local_media_task(
            task_id,
            owner=owner,
            status=status,
            title=f"PRIVATE-TITLE-{suffix}",
            error=f"PRIVATE-ERROR-{suffix} /private/path",
            warning=f"PRIVATE-WARNING-{suffix}",
            tmdb_id="PRIVATE-TMDB-ID",
            completed_at=stamp if status in {"completed", "failed"} else "",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_tasks SET created_at=?,updated_at=?,completed_at=? "
                "WHERE id=? AND owner=?",
                (stamp, stamp, stamp if status in {"completed", "failed"} else "", task_id, owner),
            )
        return task_id

    def test_arguments_reject_extra_fields(self):
        self.assertEqual(local_media_review_queue_arguments({}), {})
        self.assertEqual(local_media_history_arguments({}), {})
        for validator in (local_media_review_queue_arguments, local_media_history_arguments):
            with self.assertRaises(AgentToolError):
                validator({"path": "/private"})
            with self.assertRaises(AgentToolError):
                validator([])  # type: ignore[arg-type]

    def test_review_queue_summary_is_owner_scoped_and_bucketed(self):
        self._task(
            status="requires_manual", trigger="qb_completed",
            stamp="2026-08-10 11:30:00", suffix="recent",
        )
        self._task(
            status="requires_manual", trigger="scan",
            stamp="2026-08-09 12:00:00", suffix="day",
        )
        self._task(
            status="requires_manual", trigger="manual",
            stamp="2026-07-01 12:00:00", suffix="old",
        )
        self._task(
            status="requires_manual", trigger="manual",
            stamp="2026-08-10 11:30:00", owner="other", suffix="other",
        )
        summary = db.get_local_media_review_queue_summary(owner="admin")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_trigger"], {
            "manual": 1, "qb_completed": 1, "scan": 1,
        })
        self.assertEqual(sum(summary["age_buckets"].values()), 3)

        result = summarize_local_media_review_queue({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["total"], 3)
        self.assertEqual(result.data["by_trigger"], {
            "qb_completed": 1, "scan": 1, "manual": 1, "unknown": 0,
        })
        self.assertEqual(sum(result.data["age_buckets"].values()), 3)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in ("PRIVATE-", "/private", "TMDB-ID", "ERROR", "WARNING"):
            self.assertNotIn(secret, serialized)

    def test_history_summary_classifies_completed_failed_and_unknown_trigger(self):
        self._task(
            status="completed", trigger="qb_completed",
            stamp="2026-08-10 10:00:00", suffix="completed",
        )
        self._task(
            status="failed", trigger="scan",
            stamp="2026-08-08 10:00:00", suffix="failed",
        )
        self._task(
            status="failed", trigger="manual",
            stamp="2026-07-01 10:00:00", suffix="manual",
        )
        raw = db.get_local_media_history_summary(owner="admin")
        self.assertEqual(raw["total"], 3)
        self.assertEqual(raw["by_status"], {"completed": 1, "failed": 2})

        result = summarize_local_media_history({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["by_status"], {"completed": 1, "failed": 2})
        self.assertEqual(result.data["by_trigger"], {
            "qb_completed": 1, "scan": 1, "manual": 1, "unknown": 0,
        })
        self.assertEqual(sum(result.data["age_buckets"].values()), 3)

    def test_unavailable_results_are_stable(self):
        for target, action in (
            (
                "app.agent.local_media_actions.db.get_local_media_review_queue_summary",
                summarize_local_media_review_queue,
            ),
            (
                "app.agent.local_media_actions.db.get_local_media_history_summary",
                summarize_local_media_history,
            ),
        ):
            with self.subTest(target=target), patch(
                target, side_effect=RuntimeError("PRIVATE SQL /private/db.sqlite")
            ):
                result = action({})
                self.assertEqual(result.status, "unavailable")
                serialized = json.dumps(result.to_dict(), ensure_ascii=False)
                self.assertNotIn("PRIVATE SQL", serialized)
                self.assertNotIn("/private", serialized)

    def test_registry_and_natural_language_routing(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        for name in (
            "local_media.review_queue_summary",
            "local_media.history_summary",
        ):
            self.assertEqual(capabilities[name]["risk"], "read")
            self.assertFalse(capabilities[name]["requires_confirmation"])
            self.assertFalse(capabilities[name]["parameters"]["additionalProperties"])

        agent = AgentOrchestrator(registry)
        review = agent.query("查看本地媒体待确认统计")
        history = agent.query("查看本地整理处理历史")
        diagnosis = agent.query("检查本地媒体配置状态")
        self.assertEqual(review["tool_call"]["name"], "local_media.review_queue_summary")
        self.assertEqual(history["tool_call"]["name"], "local_media.history_summary")
        self.assertEqual(diagnosis["tool_call"]["name"], "local_media.diagnose")


class LocalMediaSummaryAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.lifecycle_patch = patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lambda _paths: nullcontext(),
        )
        self.lifecycle_patch.start()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.lifecycle_patch.stop()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_auth_csrf_and_shared_scope_between_summary_tools(self):
        path = "/api/agent/tools/local_media.review_queue_summary"
        body = {"session_id": "test_session_identifier_0001", "arguments": {}}
        self.assertEqual(self.client.post(path, json=body).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json=body).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        for _ in range(4):
            response = self.client.post(path, headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["tool_call"]["name"],
                "local_media.review_queue_summary",
            )
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "查看本地媒体处理历史"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
