"""活动选择与状态聚合在独立进程退出、事务回滚、通知重试后保持业务事实。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import database as db
from app.repositories.activity import timeline_snapshot
from app.modules.media_automation_rules import drain_automation_rules
from app.modules.telegram_notification_center import NotificationPublishResult
from app.repositories import activity
from app.repositories import media_automation_rules as rules
from tests.support import IsolatedDatabaseTestCase


class ActivityRecoveryAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.clock = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)
        with db.get_conn() as conn:
            for table in (
                "media_automation_rules",
                "local_media_task_items",
                "local_media_tasks",
                "local_media_sources",
            ):
                conn.execute(f"DELETE FROM {table}")
            source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) "
                "VALUES('audit','/fixture','2026-01-01 00:00:00','2026-01-01 00:00:00')"
            ).lastrowid
            self.task = conn.execute(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture','restart-old','restart-old','completed','2026-01-01 00:00:00','2026-01-01 00:00:00')",
                (source,),
            ).lastrowid
            conn.executemany(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture',?,?,'completed','2026-01-02 00:00:00','2026-01-02 00:00:00')",
                [(source, f"restart-{i}", f"restart-{i}") for i in range(25)],
            )
            conn.executemany(
                "INSERT INTO local_media_task_items(task_id,source_path,role,status,created_at,updated_at) "
                "VALUES(?,?,'subtitle',?,'2026-01-01 00:00:00','2026-01-01 00:00:00')",
                [
                    (
                        self.task,
                        f"/fixture/member-{i}",
                        "verified" if i < 149 else "failed",
                    )
                    for i in range(150)
                ],
            )
        self.target = {"kind": "local_media", "id": self.task}

    def _child(self, *, commit):
        script = """
import tests
import os, sys
from app import database as db
db.configure_database(sys.argv[1], test_mode=True)
db.init_db()
with db.get_conn() as conn:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE local_media_task_items SET status='verified' WHERE task_id=?", (int(sys.argv[2]),))
    conn.execute("UPDATE local_media_tasks SET updated_at='2026-01-09 00:00:00' WHERE id=?", (int(sys.argv[2]),))
    if sys.argv[3] == 'rollback':
        os._exit(17)
os._exit(17)
"""
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.test_db_path),
                str(self.task),
                "commit" if commit else "rollback",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 17, done.stderr)

    def _business(self):
        identifier = self.target["id"]
        assert identifier is not None
        result = timeline_snapshot(self.target["kind"], identifier)
        return {
            "status": result["status"],
            "terminal": result["data"]["terminal"],
            "attention": result["data"]["needs_attention"],
            "members": next(
                stage
                for stage in result["data"]["stages"]
                if stage["stage"] == "成员处理"
            ),
            "selected": [
                (row["kind"], row["id"])
                for row in activity.search_page(query="restart", limit=2)["items"]
            ],
        }

    def test_committed_child_repair_is_visible_after_exit_without_stale_window(self):
        self.assertEqual(self._business()["status"], "attention")
        self._child(commit=True)
        current = self._business()
        self.assertEqual(current["status"], "completed")
        self.assertFalse(current["attention"])
        self.assertEqual(current["members"]["total_count"], 150)
        self.assertEqual(current["selected"][0], ("local_media", self.task))
        db.init_db()
        self.assertEqual(self._business(), current)

    def test_uncommitted_child_repair_rolls_back_both_order_and_member_state(self):
        expected = self._business()
        self._child(commit=False)
        db.init_db()
        self.assertEqual(self._business(), expected)

    def test_declined_handoff_reopens_and_retries_same_attention_key_once(self):
        rules.save_rule(
            "activity-recovery-audit",
            kind="activity_follow",
            settings={
                "target": self.target,
                "title": "Fixture",
                "expires_at": (self.clock + timedelta(hours=1)).isoformat(),
            },
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        with (
            patch(
                "app.modules.media_automation_rules.is_agent_enabled", return_value=True
            ),
            patch(
                "app.modules.media_automation_rules._authorized_notification_chat",
                return_value="123",
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event",
                side_effect=[
                    NotificationPublishResult(False, status="failed"),
                    NotificationPublishResult(True, queued=True, status="queued"),
                ],
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 0)
            db.init_db()
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=4)), 0
            )
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=5)), 1
            )
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=10)), 0
            )
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(
            publish.call_args_list[0].args[0], publish.call_args_list[1].args[0]
        )
        self.assertIn("需要关注", publish.call_args.args[1].title)
