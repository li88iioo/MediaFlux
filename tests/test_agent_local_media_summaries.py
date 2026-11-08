"""本地待确认与处理历史共用完整整理时间线，保留真实任务身份与权限边界。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from unittest.mock import patch

from app import database as db, sdk
from tests.support import IsolatedDatabaseTestCase


class LocalMediaSummaryUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")

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

    def test_review_queue_is_complete_owner_scoped_and_keeps_task_facts(self):
        ids = [self._task(status="requires_manual", trigger=trigger, stamp=stamp, suffix=suffix)
               for trigger, stamp, suffix in (("qb_completed", "2026-08-10 11:30:00", "recent"),
                   ("scan", "2026-08-09 12:00:00", "day"), ("manual", "2026-07-01 12:00:00", "old"))]
        self._task(status="requires_manual", trigger="manual", stamp="2026-08-10 12:00:00", owner="other", suffix="other")
        first = sdk.activity.organize_logs(origin="local", status="manual", limit=2)
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        last = sdk.activity.organize_logs(origin="local", status="manual", offset=first["next_offset"], limit=2)
        self.assertFalse(last["has_more"])
        items = first["items"] + last["items"]
        self.assertEqual({item["id"] for item in items}, set(ids))
        self.assertEqual(Counter(item["trigger"] for item in items), {"manual": 1, "qb_completed": 1, "scan": 1})
        for item in items:
            self.assertEqual(item["kind"], "local_media")
            self.assertEqual(item["raw_status"], "requires_manual")
            self.assertTrue(item["title"].startswith("PRIVATE-TITLE-"))
            self.assertTrue(item["original_path"].startswith("/private/TITLE-"))
            self.assertTrue(item["updated_at"])
            timeline = sdk.activity.timeline(item["kind"], item["id"])
            self.assertTrue(timeline["ok"])
        self.assertNotIn("TITLE-other", json.dumps(items))

    def test_history_preserves_terminal_statuses_triggers_and_complete_errors(self):
        self._task(status="completed", trigger="qb_completed", stamp="2026-08-10 10:00:00", suffix="completed")
        failed = self._task(status="failed", trigger="scan", stamp="2026-08-08 10:00:00", suffix="failed")
        self._task(status="requires_manual", trigger="manual", stamp="2026-08-09 10:00:00", suffix="pending")
        with db.get_conn() as conn:
            conn.execute("UPDATE local_media_tasks SET error=? WHERE id=?",
                         ("完整诊断" * 180 + " password=DO_NOT_LEAK", failed))
        success = sdk.activity.organize_logs(origin="local", status="success")
        failures = sdk.activity.organize_logs(origin="local", status="failed")
        self.assertEqual(success["total"], 1)
        self.assertEqual(success["items"][0]["raw_status"], "completed")
        self.assertEqual(failures["total"], 1)
        item = failures["items"][0]
        self.assertEqual(item["raw_status"], "failed")
        self.assertEqual(item["trigger"], "scan")
        self.assertEqual(item["completed_at"], "2026-08-08 10:00:00")
        self.assertIn("完整诊断" * 180, item["error"])
        self.assertNotIn("DO_NOT_LEAK", item["error"])
        self.assertEqual(sdk.activity.organize_logs(origin="local")["by_status"], {"success": 1, "failed": 1, "manual": 1})

    def test_query_failure_never_becomes_an_empty_queue(self):
        for status in ("manual", "failed", "success"):
            with self.subTest(status=status), patch("app.database.organize_history_page",
                    side_effect=sqlite3.OperationalError("PRIVATE SQL /private/db.sqlite")):
                with self.assertRaisesRegex(RuntimeError, "暂时无法读取整理记录") as caught:
                    sdk.activity.organize_logs(origin="local", status=status)
                self.assertNotIn("PRIVATE SQL", str(caught.exception))
