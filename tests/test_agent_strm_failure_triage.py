"""Media Agent STRM 失败分诊的安全、路由与 API 回归测试。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from unittest.mock import patch

from app import database as db
from app import sdk
from tests.support import IsolatedDatabaseTestCase


class StrmFailureTriageUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")

    def tearDown(self):
        pass

    @staticmethod
    def _record(*, source: str, file_id: str, action: str, error: str) -> int:
        return db.record_strm_failure(
            source_id=source,
            source_name=f"source-{source}",
            file_id=file_id,
            parent_id=f"parent-{file_id}",
            filename=f"{file_id}.mkv",
            action=action,
            rel_dir=f"/private/{source}",
            target_rel_path=f"/target/{file_id}.strm",
            error=error,
        )

    def test_database_summary_counts_known_states_without_sensitive_projection(self):
        repeated_id = self._record(
            source="SECRET-SOURCE-A",
            file_id="SECRET-FILE-A",
            action="generate",
            error="TOKEN-SECRET https://private.example/a /private/a",
        )
        self._record(
            source="SECRET-SOURCE-A",
            file_id="SECRET-FILE-A",
            action="generate",
            error="TOKEN-SECRET-2 /private/a2",
        )
        retrying_id = self._record(
            source="SECRET-SOURCE-B",
            file_id="SECRET-FILE-B",
            action="metadata",
            error="UUID-SECRET-B /private/b",
        )
        resolved_id = self._record(
            source="SECRET-SOURCE-C",
            file_id="SECRET-FILE-C",
            action="metadata",
            error="PATH-SECRET-C /private/c",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='retrying',retry_count=2 WHERE id=?",
                (retrying_id,),
            )
            conn.execute(
                "UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?",
                (resolved_id,),
            )
            conn.execute(
                "INSERT INTO strm_failures(source_id,source_name,file_id,parent_id,filename,action,rel_dir,target_rel_path,error,status,failure_count,retry_count,created_at,updated_at,resolved_at) VALUES('ignored','ignored','ignored','ignored','ignored','unknown','','','','open',9,9,datetime('now'),datetime('now'),NULL)"
            )
        summary = db.get_strm_failure_triage_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["retrying"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["active_repeated"], 1)
        self.assertEqual(summary["active_retried"], 1)
        self.assertEqual(
            summary["by_action"]["generate"],
            {"total": 1, "open": 1, "retrying": 0, "resolved": 0},
        )
        self.assertEqual(
            summary["by_action"]["metadata"],
            {"total": 2, "open": 0, "retrying": 1, "resolved": 1},
        )
        assert isinstance(repeated_id, int)
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret in ("SECRET", "/private", "UUID", "PATH", "source-", ".mkv"):
            self.assertNotIn(secret, serialized)
        legacy = sdk.strm.failures(source_id="ignored")
        self.assertEqual(legacy["data"]["failures"], summary)
        self.assertEqual(legacy["data"]["total"], 1)
        self.assertEqual(legacy["data"]["matched_failures"], {"total": 1, "open": 1, "retrying": 0, "resolved": 0})

    def test_native_reads_full_stored_error_and_real_id_in_each_state(self):
        self.assertEqual(sdk.strm.failures()["status"], "healthy")
        identifier = self._record(source="source", file_id="file", action="generate",
                                  error="磁盘写入失败 " + "细节" * 160 + " permission denied password=PRIVATE https://private.invalid/token")
        stored = dict(db.list_strm_failures()[0])
        for state, expected in (("open", "attention"), ("retrying", "running"), ("resolved", "healthy")):
            with db.get_conn() as conn:
                conn.execute("UPDATE strm_failures SET status=? WHERE id=?", (state, identifier))
            result = sdk.strm.failures(status=state)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], expected)
            row, = result["data"]["items"]
            self.assertEqual((row["id"], row["status"]), (identifier, state))
            self.assertEqual(row["error"], stored["error"])
            self.assertIn("permission denied", row["error"])
            self.assertGreater(len(row["error"]), 300)
            self.assertNotIn("PRIVATE", row["error"])
            self.assertNotIn("private.invalid", row["error"])
            self.assertEqual(result["data"]["error_representation"], "stored_redacted_text")
            self.assertIn("不能还原", result["data"]["error_note"])
            self.assertEqual((row["failure_count"], row["retry_count"]), (1, 0))
            self.assertIn("failure_count=1、retry_count=0", result["data"]["counter_note"])

    def test_native_read_status_uses_all_matches_for_the_selected_filters(self):
        self._record(source="other", file_id="other", action="metadata", error="other")
        self._record(source="mixed", file_id="open", action="metadata", error="open")
        retrying = self._record(source="mixed", file_id="retrying", action="generate", error="retrying")
        resolved = self._record(source="settled", file_id="settled", action="generate", error="settled")
        db.claim_strm_failures([retrying])
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?", (resolved,))
        global_summary = db.get_strm_failure_triage_summary()

        generate_counts = {"total": 2, "open": 0, "retrying": 1, "resolved": 1}
        cases = [
            ({}, "attention", {"total": 4, "open": 2, "retrying": 1, "resolved": 1}),
            ({"source_id": " settled "}, "healthy", {"total": 1, "open": 0, "retrying": 0, "resolved": 1}),
            ({"source_id": "missing"}, "healthy", {"total": 0, "open": 0, "retrying": 0, "resolved": 0}),
            ({"status": "resolved"}, "healthy", {"total": 1, "open": 0, "retrying": 0, "resolved": 1}),
            ({"action": "generate"}, "running", generate_counts),
            ({"source_id": "mixed", "action": "generate", "status": "open"}, "healthy",
             {"total": 0, "open": 0, "retrying": 0, "resolved": 0}),
        ]
        for filters, expected_status, counts in cases:
            with self.subTest(filters=filters):
                result = sdk.strm.failures(
                    status=filters.get("status", "all"),
                    action=filters.get("action", ""),
                    source_id=filters.get("source_id", ""),
                )
                data = result["data"]
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(data["matched_failures"], counts)
                self.assertEqual(data["total"], counts["total"])
                self.assertEqual(data["status_scope"], "filtered")
                self.assertEqual(data["failures"], global_summary)
                self.assertEqual(data["summary_scope"], "all")

        # 同一范围内，第一页只有已解决项，第二页有重试项，页外没有返回项。
        for offset, expected_ids in ((0, [resolved]), (1, [retrying]), (2, [])):
            with self.subTest(offset=offset):
                result = sdk.strm.failures(action="generate", limit=1, offset=offset)
                data = result["data"]
                self.assertEqual(result["status"], "running")
                self.assertEqual(data["matched_failures"], generate_counts)
                self.assertEqual([item["id"] for item in data["items"]], expected_ids)
                self.assertEqual(data["next_offset"], 1 if offset == 0 else None)
                if offset == 1:
                    item, = data["items"]
                    self.assertEqual((item["failure_count"], item["retry_count"]), (1, 1))

        mixed_page = sdk.strm.failures(source_id="mixed", limit=1)
        self.assertEqual(mixed_page["status"], "attention")
        self.assertEqual(mixed_page["data"]["matched_failures"]["open"], 1)
        self.assertEqual([item["id"] for item in mixed_page["data"]["items"]], [retrying])

    def test_native_read_page_and_counts_share_snapshot_during_resolution(self):
        identifier = self._record(source="source", file_id="file", action="generate", error="failed")
        original = db.get_conn
        resolved = []

        @contextmanager
        def concurrent_connection():
            with original() as conn:
                def trace(statement):
                    if statement.startswith("SELECT * FROM strm_failures") and not resolved:
                        with closing(sqlite3.connect(db.DB_PATH)) as writer, writer:
                            changed = writer.execute(
                                "UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?",
                                (identifier,),
                            )
                            resolved.append(changed.rowcount)

                conn.set_trace_callback(trace)
                yield conn

        with patch.object(db, "get_conn", concurrent_connection):
            before = sdk.strm.failures(source_id="source")
        self.assertEqual(resolved, [1])
        self.assertEqual(before["status"], "attention")
        self.assertEqual(before["data"]["items"][0]["status"], "open")
        self.assertEqual(before["data"]["matched_failures"]["open"], 1)
        self.assertEqual(before["data"]["failures"]["open"], 1)
        after = sdk.strm.failures(source_id="source")
        self.assertEqual(after["status"], "healthy")
        self.assertEqual(after["data"]["items"][0]["status"], "resolved")
        self.assertEqual(after["data"]["matched_failures"]["resolved"], 1)
        self.assertEqual(after["data"]["failures"]["resolved"], 1)

    def test_native_read_filters_pages_and_does_not_turn_database_failure_into_empty(self):
        identifiers = [self._record(source="source", file_id=f"file-{i}", action="metadata", error=f"完整错误{i}")
                       for i in range(205)]
        self._record(source="other", file_id="other", action="generate", error="other")
        items, offset = [], 0
        while True:
            with patch("app.modules.strm.GuangYaClient", side_effect=AssertionError("不得访问云盘")), \
                 patch("app.modules.strm.retry_strm_failures", side_effect=AssertionError("不得重试")):
                result = sdk.strm.failures(status="open", source_id="source", action="metadata", limit=120, offset=offset)
            data = result["data"]
            self.assertEqual(data["total"], 205)
            items.extend(data["items"])
            if not data["has_more"]:
                self.assertIsNone(data["next_offset"])
                break
            offset = data["next_offset"]
        self.assertEqual([item["id"] for item in items], list(reversed(identifiers)))
        with (
            patch.object(db, "get_conn", side_effect=sqlite3.OperationalError("unavailable")),
            self.assertRaises(sqlite3.OperationalError),
        ):
            sdk.strm.failures()
