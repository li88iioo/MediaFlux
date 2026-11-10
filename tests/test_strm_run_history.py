"""真实SQLite历史过滤、完整分页与旧读取契约，不启动同步任务。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, cast
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from tests.support import IsolatedDatabaseTestCase


class StrmRunHistoryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM task_runs")

    def seed(self, count, *, task="strm_sync", status="success"):
        with db.get_conn() as conn:
            return [conn.execute(
                "INSERT INTO task_runs(task_name,trigger_type,status,started_at,result,error) VALUES(?,?,?,?,?,?)",
                (task, "manual", status, "2026-11-05 00:00:00", json.dumps({"mode": "full", "elapsed_seconds": 1.5,
                    "stats": {"total": 1, "generated": 1}, "sources": ["PRIVATE"]}), "SECRET ERROR")
            ).lastrowid for _ in range(count)]

    def test_filter_is_sql_before_limit_and_offset_even_beyond_latest_hundred(self):
        expected = self.seed(5, status="failed")
        self.seed(137)
        self.seed(3, task="other", status="failed")
        page = db.query_task_runs_page("strm_sync", status="failed", limit=2, offset=1)
        self.assertEqual(page["total"], 5)
        self.assertEqual([row["id"] for row in page["items"]], list(reversed(expected))[1:3])
        self.assertEqual((page["returned"], page["has_more"], page["next_offset"]), (2, True, 3))
        last = db.query_task_runs_page("strm_sync", status="failed", limit=2, offset=3)
        self.assertEqual([row["id"] for row in last["items"]], list(reversed(expected))[-2:])
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])

    def test_large_page_and_end_offset_keep_true_total_and_ids(self):
        ids = self.seed(257)
        page = db.query_task_runs_page("strm_sync", limit=200)
        self.assertEqual((page["total"], page["returned"], page["next_offset"]), (257, 200, 200))
        second = db.query_task_runs_page("strm_sync", limit=200, offset=page["next_offset"])
        self.assertEqual([row["id"] for row in page["items"] + second["items"]], list(reversed(ids)))
        self.assertEqual((second["total"], second["returned"], second["has_more"]), (257, 57, False))
        beyond = db.query_task_runs_page("strm_sync", limit=50, offset=500)
        self.assertEqual((beyond["total"], beyond["returned"], beyond["has_more"], beyond["next_offset"]), (257, 0, False, None))
        empty = db.query_task_runs_page("no-such-task")
        self.assertEqual((empty["total"], empty["returned"], empty["items"]), (0, 0, []))

    def test_total_and_items_share_one_read_snapshot_with_a_concurrent_writer(self):
        ids = self.seed(3)
        original = db.get_conn
        inserted = []
        @contextmanager
        def concurrent_connection():
            with original() as conn:
                def trace(statement):
                    if statement.startswith("SELECT * FROM task_runs") and not inserted:
                        with sqlite3.connect(db.DB_PATH) as writer:
                            inserted.append(writer.execute(
                                "INSERT INTO task_runs(task_name,trigger_type,status,started_at,result) VALUES('strm_sync','manual','success','2026-11-05','{}')"
                            ).lastrowid)
                conn.set_trace_callback(trace)
                yield conn
        with patch.object(db, "get_conn", concurrent_connection):
            page = db.query_task_runs_page("strm_sync", limit=10)
        self.assertEqual(page["total"], 3)
        self.assertEqual([row["id"] for row in page["items"]], list(reversed(ids)))
        self.assertEqual(db.query_task_runs_page("strm_sync")["total"], 4)

    def test_legacy_list_stays_raw_rows_with_original_order_and_limit_semantics(self):
        ids = self.seed(35)
        rows = db.list_task_runs("strm_sync", limit=30)
        self.assertIsInstance(rows, list)
        self.assertTrue(all(isinstance(row, sqlite3.Row) for row in rows))
        self.assertEqual([row["id"] for row in rows], list(reversed(ids))[:30])
        self.assertIn("PRIVATE", rows[0]["result"])
        self.assertEqual(rows[0]["error"], "SECRET ERROR")
        self.assertEqual(db.list_task_runs("strm_sync", limit=0), [])
        self.assertEqual(len(db.list_task_runs("strm_sync", limit=-1)), 35)


    def test_public_history_keeps_unknown_records_and_malformed_payloads(self):
        from app import sdk
        unknown = self.seed(1, status="legacy-state")[0]
        failed = self.seed(1, status="failed")[0]
        with db.get_conn() as conn:
            conn.execute("UPDATE task_runs SET result=? WHERE id=?", ("not-json", failed))
        result = sdk.strm.history()
        self.assertEqual(result["total"], 2)
        self.assertEqual([row["run_id"] for row in result["items"]], [failed, unknown])
        self.assertEqual([row["status"] for row in result["items"]], ["failed", "unknown"])
        self.assertEqual(result["items"][0]["stats"], {})
        self.assertEqual(result["items"][0]["mode"], "unknown")
        self.assertIsNone(result["items"][0]["elapsed_seconds"])
        self.assertEqual(result["failure_context"]["open"], 0)
        self.assertIn("change_queue", result["queue_context"])
        for invalid in ({"limit": 0}, {"limit": True}, {"offset": -1}, {"status": "not-a-status"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sdk.strm.history(**cast(Any, invalid))
        with patch.object(db, "query_task_runs_page", side_effect=OSError("database unreadable")), self.assertRaises(OSError):
            sdk.strm.history()

    def test_history_and_run_status_reuse_same_projection_for_real_modes_and_duration(self):
        from app import sdk
        inputs = [("fast_partial", 900000.125, "config-retirement"),
                  ("full_fallback", 0, "organize"), ("fast", -1, "telegram"),
                  ("full", float("nan"), "cron"), ("incremental", float("inf"), "manual"),
                  ("fast_noop", None, "manual")]
        for mode, elapsed, trigger in inputs:
            with self.subTest(mode=mode):
                run_id = db.add_task_run("strm_sync", trigger)
                db.finish_task_run(run_id, "partial", result=json.dumps({"mode": mode, "elapsed_seconds": elapsed,
                    "stats": {"total": 4, "generated": 2, "metadata_skipped": 3}, "error_samples": ["SECRET"]}))
                item = sdk.strm.history(limit=1)["items"][0]
                self.assertEqual(item, sdk.strm.status(run_id)["data"]["task"])
                self.assertEqual((item["mode"], item["trigger_type"]), (mode, trigger))
                self.assertEqual(item["stats"], {"total": 4, "generated": 2, "metadata_skipped": 3})
                self.assertEqual(item["elapsed_seconds"], elapsed if mode in {"fast_partial", "full_fallback"} else None)
                self.assertNotIn("SECRET", json.dumps(item))

    def test_legacy_web_runs_and_scheduler_row_contract_remain_raw(self):
        from app.modules.scheduler import STRMScheduler
        from app.routes import strm_api
        ids = self.seed(35)
        with patch.object(strm_api, "require_api_login"):
            response = strm_api.runs(cast(Any, object()))
        data = json.loads(bytes(response.body))
        self.assertEqual([item["id"] for item in data], list(reversed(ids))[:30])
        self.assertEqual(data[0]["error"], "SECRET ERROR")
        self.assertEqual(data[0]["result"]["sources"], ["PRIVATE"])
        row = db.get_last_task_run("strm_sync")
        self.assertEqual(STRMScheduler._row_to_dict(row)["result"], data[0]["result"])

    def test_overview_preserves_attention_for_partial_and_failed_runs(self):
        from app import sdk
        from app.modules import scheduler

        runtime = scheduler.STRMScheduler()
        with (
            patch.object(scheduler, "get_scheduler", return_value=runtime),
            patch.object(runtime, "validate_config", return_value=""),
            patch.object(scheduler, "configured_strm_source_plans", return_value=([], "")),
        ):
            for status, expected in (("partial", "attention"), ("failed", "attention"), ("success", "ready")):
                with self.subTest(status=status):
                    run_id = db.add_task_run("strm_sync", "manual")
                    db.finish_task_run(run_id, status, result=json.dumps({"stats": {"failed": int(status != "success")}}))
                    overview = sdk.strm.status()
                    self.assertEqual(overview["status"], expected)
                    self.assertEqual(overview["ok"], expected == "ready")
                    self.assertEqual(overview["data"]["last_run"], sdk.strm.status(run_id)["data"]["task"])
