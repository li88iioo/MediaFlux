"""本地媒体待确认队列与终态历史的安全 Agent 摘要。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.local_media_actions import (
    local_media_history_arguments,
    local_media_review_queue_arguments,
    summarize_local_media_history,
    summarize_local_media_review_queue,
)
from tests.support import IsolatedDatabaseTestCase


class LocalMediaSummaryUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")

    def tearDown(self):
        pass

    @staticmethod
    def _task(
        *,
        status: str,
        trigger: str,
        stamp: str,
        owner: str = "admin",
        suffix: str = "one",
    ) -> int:
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
                "UPDATE local_media_tasks SET created_at=?,updated_at=?,completed_at=? WHERE id=? AND owner=?",
                (
                    stamp,
                    stamp,
                    stamp if status in {"completed", "failed"} else "",
                    task_id,
                    owner,
                ),
            )
        return task_id

    def test_arguments_reject_extra_fields(self):
        self.assertEqual(local_media_review_queue_arguments({}), {})
        self.assertEqual(local_media_history_arguments({}), {})
        for validator in (
            local_media_review_queue_arguments,
            local_media_history_arguments,
        ):
            with self.assertRaises(AgentToolError):
                validator({"path": "/private"})
            with self.assertRaises(AgentToolError):
                validator([])

    def test_review_queue_summary_is_owner_scoped_and_bucketed(self):
        self._task(
            status="requires_manual",
            trigger="qb_completed",
            stamp="2026-08-10 11:30:00",
            suffix="recent",
        )
        self._task(
            status="requires_manual",
            trigger="scan",
            stamp="2026-08-09 12:00:00",
            suffix="day",
        )
        self._task(
            status="requires_manual",
            trigger="manual",
            stamp="2026-07-01 12:00:00",
            suffix="old",
        )
        self._task(
            status="requires_manual",
            trigger="manual",
            stamp="2026-08-10 11:30:00",
            owner="other",
            suffix="other",
        )
        summary = db.get_local_media_review_queue_summary(owner="admin")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(
            summary["by_trigger"], {"manual": 1, "qb_completed": 1, "scan": 1}
        )
        self.assertEqual(sum(summary["age_buckets"].values()), 3)
        result = summarize_local_media_review_queue({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["total"], 3)
        self.assertEqual(
            result.data["by_trigger"],
            {"qb_completed": 1, "scan": 1, "manual": 1, "unknown": 0},
        )
        self.assertEqual(sum(result.data["age_buckets"].values()), 3)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in ("PRIVATE-", "/private", "TMDB-ID", "ERROR", "WARNING"):
            self.assertNotIn(secret, serialized)

    def test_history_summary_classifies_completed_failed_and_unknown_trigger(self):
        self._task(
            status="completed",
            trigger="qb_completed",
            stamp="2026-08-10 10:00:00",
            suffix="completed",
        )
        self._task(
            status="failed",
            trigger="scan",
            stamp="2026-08-08 10:00:00",
            suffix="failed",
        )
        self._task(
            status="failed",
            trigger="manual",
            stamp="2026-07-01 10:00:00",
            suffix="manual",
        )
        raw = db.get_local_media_history_summary(owner="admin")
        self.assertEqual(raw["total"], 3)
        self.assertEqual(raw["by_status"], {"completed": 1, "failed": 2})
        result = summarize_local_media_history({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["by_status"], {"completed": 1, "failed": 2})
        self.assertEqual(
            result.data["by_trigger"],
            {"qb_completed": 1, "scan": 1, "manual": 1, "unknown": 0},
        )
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
            with (
                self.subTest(target=target),
                patch(
                    target, side_effect=RuntimeError("PRIVATE SQL /private/db.sqlite")
                ),
            ):
                result = action({})
                self.assertEqual(result.status, "unavailable")
                serialized = json.dumps(result.to_dict(), ensure_ascii=False)
                self.assertNotIn("PRIVATE SQL", serialized)
                self.assertNotIn("/private", serialized)
