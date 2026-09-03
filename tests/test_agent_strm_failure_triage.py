"""Media Agent STRM 失败分诊的安全、路由与 API 回归测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.strm_failure_actions import triage_strm_failures
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
        self.assertIsInstance(repeated_id, int)
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret in ("SECRET", "/private", "UUID", "PATH", "source-", ".mkv"):
            self.assertNotIn(secret, serialized)

    def test_result_states_and_sensitive_fields_are_fixed_and_redacted(self):
        empty = triage_strm_failures({})
        self.assertTrue(empty.ok)
        self.assertEqual(empty.status, "healthy")
        self.assertEqual(empty.data["probe_mode"], "database")
        self.assertFalse(empty.data["network_accessed"])
        self.assertFalse(empty.data["filesystem_accessed"])
        failure_id = self._record(
            source="PRIVATE-SOURCE-ID",
            file_id="PRIVATE-FILE-ID",
            action="generate",
            error="secret-token=https://private.example/token /home/private/video.mkv",
        )
        attention = triage_strm_failures({})
        self.assertFalse(attention.ok)
        self.assertEqual(attention.status, "attention")
        self.assertEqual(attention.data["failures"]["open"], 1)
        serialized = json.dumps(attention.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-SOURCE-ID",
            "PRIVATE-FILE-ID",
            "source-PRIVATE",
            "video.mkv",
            "/home/private",
            "private.example",
            "secret-token",
        ):
            self.assertNotIn(secret, serialized)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='retrying',retry_count=1 WHERE id=?",
                (failure_id,),
            )
        running = triage_strm_failures({})
        self.assertTrue(running.ok)
        self.assertEqual(running.status, "running")
        self.assertEqual(running.data["failures"]["retrying"], 1)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?",
                (failure_id,),
            )
        resolved = triage_strm_failures({})
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.status, "healthy")
        self.assertEqual(resolved.data["failures"]["resolved"], 1)

    def test_triage_never_reuses_raw_helpers_filesystem_or_retry(self):
        self._record(
            source="source-a",
            file_id="file-a",
            action="metadata",
            error="private failure",
        )
        with (
            patch(
                "app.database.list_strm_failures",
                side_effect=AssertionError("raw rows used"),
            ) as raw,
            patch(
                "app.database.summarize_strm_failures",
                side_effect=AssertionError("source summary used"),
            ) as source_summary,
            patch(
                "app.database.list_strm_index_diagnostics",
                side_effect=AssertionError("filesystem used"),
            ) as diagnostics,
            patch(
                "app.modules.strm.retry_strm_failures",
                side_effect=AssertionError("retry used"),
            ) as retry,
        ):
            result = triage_strm_failures({})
        self.assertEqual(result.status, "attention")
        raw.assert_not_called()
        source_summary.assert_not_called()
        diagnostics.assert_not_called()
        retry.assert_not_called()
