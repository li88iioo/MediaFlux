"""成员展示窗口不能决定本地任务是否有异常，跟踪应使用全量持久状态。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import database as db
from app import sdk
from app.modules.media_automation_rules import drain_automation_rules
from app.modules.telegram_notification_policy import NotificationImportance
from app.repositories import activity
from app.repositories.activity import timeline_snapshot
from app.repositories import media_automation_rules as rules
from tests.support import IsolatedDatabaseTestCase


class ActivityMemberRollupAuditTests(IsolatedDatabaseTestCase):
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
            task_id = conn.execute(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture','fixture-rollup','Fixture','completed','2026-01-01 00:00:00','2026-01-01 00:00:00')",
                (source,),
            ).lastrowid
            assert task_id is not None
            self.task = task_id
        self.target = {"kind": "local_media", "id": self.task}

    def _members(self, good_count, *, failures=0):
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO local_media_task_items(task_id,source_path,role,status,error,created_at,updated_at) "
                "VALUES(?,?,'subtitle',?,?,'2026-01-01 00:00:00','2026-01-01 00:00:00')",
                [
                    (
                        self.task,
                        f"/fixture/member-{i}.srt",
                        "verified" if i < good_count else "failed",
                        "" if i < good_count else "fixture member failed",
                    )
                    for i in range(good_count + failures)
                ],
            )

    @staticmethod
    def _stage(result):
        return next(
            stage for stage in result["data"]["stages"] if stage["stage"] == "成员处理"
        )

    def test_failure_after_101_members_is_current_attention_not_completed(self):
        self._members(149, failures=1)
        result = timeline_snapshot(self.target["kind"], self.target["id"])
        self.assertEqual(result["status"], "attention")
        self.assertTrue(result["data"]["needs_attention"])
        stage = self._stage(result)
        self.assertEqual((stage["total_count"], stage["attention_count"]), (150, 1))
        self.assertEqual((stage["count"], stage["truncated"]), (100, True))

    def test_small_and_empty_normal_results_keep_display_contract(self):
        empty = timeline_snapshot(self.target["kind"], self.target["id"])
        self.assertEqual(empty["status"], "completed")
        self.assertEqual(
            (self._stage(empty)["count"], self._stage(empty)["truncated"]), (0, False)
        )
        self._members(3)
        result = timeline_snapshot(self.target["kind"], self.target["id"])
        self.assertEqual(result["status"], "completed")
        stage = self._stage(result)
        self.assertEqual((stage["count"], stage["truncated"]), (3, False))
        self.assertFalse(result["data"]["needs_attention"])

    def test_large_member_rollup_uses_two_selects_and_no_prefix_item_read(self):
        self._members(1_000, failures=1)
        original = db.get_conn
        statements = []

        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            assert self.task is not None
            snapshot = activity.snapshot("local_media", self.task)
        assert snapshot is not None
        self.assertEqual(
            snapshot.get("item_status_counts"), {"failed": 1, "verified": 1_000}
        )
        self.assertNotIn("items", snapshot)
        self.assertEqual(sum(sql.startswith("SELECT") for sql in statements), 2)
        self.assertEqual(sum("GROUP BY status" in sql for sql in statements), 1)

    def _linked_download(self, oldest_status):
        request, _ = db.create_download_request(
            "fixture-linked", "magnet", title="Linked"
        )
        db.update_download_request(
            request,
            targets="qb",
            status="completed",
            qb_status="completed",
            qb_task_id="a" * 40,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_tasks SET qb_hash=?,status=? WHERE id=?",
                ("a" * 40, oldest_status, self.task),
            )
            # 每个来源同qB身份唯一；不同来源仍可保留同一下载的各自处理历史。
            for i in range(20):
                source = conn.execute(
                    "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) "
                    "VALUES(?,?,'2026-01-02 00:00:00','2026-01-02 00:00:00')",
                    (f"linked-source-{i}", f"/fixture/source-{i}"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO local_media_tasks(source_id,qb_hash,content_path,operation_token,title,status,created_at,updated_at) "
                    "VALUES(?,?,'/fixture',?,?,'completed','2026-01-02 00:00:00','2026-01-02 00:00:00')",
                    (source, "a" * 40, f"linked-{i}", f"Linked-{i}"),
                )
        return {"kind": "download", "id": request}

    def test_download_cannot_hide_failed_linked_task_behind_20_completed_tasks(self):
        target = self._linked_download("failed")
        result = timeline_snapshot(target["kind"], target["id"])
        self.assertTrue(result["data"]["needs_attention"])
        self.assertEqual(result["status"], "attention")
        summary = next(
            stage
            for stage in result["data"]["stages"]
            if stage["stage"] == "关联本地任务汇总"
        )
        self.assertEqual((summary["total_count"], summary["attention_count"]), (21, 1))
        self.assertEqual(len(result["data"]["activities"]), 21)

    def test_download_does_not_complete_while_hidden_linked_task_is_pending(self):
        target = self._linked_download("waiting_stable")
        result = timeline_snapshot(target["kind"], target["id"])
        self.assertEqual(result["status"], "in_progress")
        self.assertFalse(result["data"]["terminal"])
        self.assertFalse(result["data"]["needs_attention"])

    def test_hidden_member_failure_hands_off_one_attention_notification(self):
        self._members(149, failures=1)
        rules.save_rule(
            "member-rollup-audit",
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
                return_value=Mock(status="queued"),
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 1)
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=6)), 0
            )
        publish.assert_called_once()
        self.assertIn("需要关注", publish.call_args.args[1].title)
        self.assertEqual(
            publish.call_args.kwargs["importance"], NotificationImportance.ERROR
        )
        self.assertFalse(publish.call_args.kwargs["deliver_now"])

    def test_recovered_historical_failure_finishes_once_without_new_alarm(self):
        with db.get_conn() as conn:
            log_id = conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) "
                "VALUES('guangya','/fixture/private-old','/fixture/private-new','Recovered','success',"
                "'2026-01-01 00:00:00','2026-01-02 00:00:00')"
            ).lastrowid
            conn.execute(
                "INSERT INTO organize_operation_steps(log_id,operation_token,step_index,action,status,error) "
                "VALUES(?,'private-operation',0,'move_rename','failed','旧失败已处理')",
                (log_id,),
            )
        assert log_id is not None
        result = timeline_snapshot("organize", log_id)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["data"]["needs_attention"])
        self.assertTrue(result["data"]["stages"][1]["historical"])
        self.assertIn("旧失败", result["data"]["stages"][1]["reason"])
        rules.save_rule(
            "recovered-history-audit",
            kind="activity_follow",
            settings={
                "target": {"kind": "organize", "id": log_id},
                "title": "Recovered",
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
                return_value=Mock(status="queued"),
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 1)
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=6)), 0
            )
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1].title, "活动跟踪结束")
        self.assertEqual(
            publish.call_args.kwargs["importance"], NotificationImportance.RESULT
        )
        self.assertNotIn("/fixture", str(publish.call_args.args[1]))

    def test_small_linked_list_does_not_pay_for_overflow_aggregation(self):
        request, _ = db.create_download_request("small-linked", "magnet", title="Small")
        db.update_download_request(
            request,
            targets="qb",
            status="completed",
            qb_status="completed",
            qb_task_id="b" * 40,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_tasks SET qb_hash=? WHERE id=?",
                ("b" * 40, self.task),
            )
        original = db.get_conn
        statements = []

        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            snapshot = activity.snapshot("download", request)
        assert snapshot is not None
        self.assertEqual(len(snapshot["local_tasks"]), 1)
        self.assertEqual(snapshot["local_task_status_counts"], {})
        self.assertFalse(any("GROUP BY status" in sql for sql in statements))
        result = timeline_snapshot("download", request)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(
            "关联本地任务汇总", [stage["stage"] for stage in result["data"]["stages"]]
        )

    def test_overflow_failure_and_pending_both_remain_visible_without_false_terminal(
        self,
    ):
        target = self._linked_download("failed")
        with db.get_conn() as conn:
            second = conn.execute(
                "SELECT id FROM local_media_tasks WHERE id<>? ORDER BY id LIMIT 1",
                (self.task,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE local_media_tasks SET status='waiting_stable' WHERE id=?",
                (second,),
            )
            source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) "
                "VALUES('extra-linked','/fixture/extra','2026-01-02 00:00:00','2026-01-02 00:00:00')"
            ).lastrowid
            conn.execute(
                "INSERT INTO local_media_tasks(source_id,qb_hash,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,?,'/fixture','linked-newest','Newest','completed','2026-01-02 00:00:00','2026-01-02 00:00:00')",
                (source, "a" * 40),
            )
        result = timeline_snapshot(target["kind"], target["id"])
        summary = next(
            stage
            for stage in result["data"]["stages"]
            if stage["stage"] == "关联本地任务汇总"
        )
        self.assertEqual(
            (
                summary["total_count"],
                summary["attention_count"],
                summary["pending_count"],
            ),
            (22, 1, 1),
        )
        self.assertEqual(result["status"], "attention")
        self.assertFalse(result["data"]["terminal"])
        self.assertEqual(len(result["data"]["activities"]), 21)

    def test_member_aggregate_and_parent_share_snapshot_during_concurrent_repair(self):
        self._members(149, failures=1)
        original = db.get_conn
        repaired = False

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(cursor_self):
                nonlocal repaired
                row = cursor_self.cursor.fetchone()
                if not repaired:
                    repaired = True
                    with original() as writer:
                        writer.execute(
                            "UPDATE local_media_task_items SET status='verified' WHERE task_id=?",
                            (self.task,),
                        )
                return row

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                cursor = self.conn.execute(sql, *args)
                return (
                    Cursor(cursor)
                    if sql.startswith("SELECT * FROM local_media_tasks")
                    and not repaired
                    else cursor
                )

        @contextmanager
        def hooked():
            with original() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            current = timeline_snapshot(self.target["kind"], self.target["id"])
        self.assertTrue(repaired)
        self.assertEqual(current["status"], "attention")
        self.assertEqual(self._stage(current)["attention_count"], 1)
        self.assertEqual(
            timeline_snapshot(self.target["kind"], self.target["id"])["status"],
            "completed",
        )

    def test_all_timeline_members_remain_navigable_by_native_ids(self):
        target = self._linked_download("failed")
        members = sdk.activity.timeline(target["kind"], target["id"])["data"]["activities"]
        self.assertEqual(len(members), 21)  # 当前对象 + 20 个关联对象。
        last = members[-1]
        result = sdk.activity.timeline(last["kind"], last["id"])
        self.assertTrue(result["ok"])
        self.assertIn(last, result["data"]["activities"])
