"""RSS Agent 只读诊断的聚合、安全与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.rss_actions import diagnose_rss, rss_diagnosis_arguments
from tests.support import IsolatedDatabaseTestCase

_SNAPSHOT = "2026-08-01 12:00:00"


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")


def _set_entry(
    entry_id: int,
    *,
    status: str | None,
    processed: int = 0,
    created_at: str = _SNAPSHOT,
    submitted_at: str = "",
    processed_at: str = "",
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE rss_entries SET status=?,processed=?,created_at=?,submitted_at=?,processed_at=? WHERE id=?",
            (status, processed, created_at, submitted_at, processed_at, entry_id),
        )


class RssDiagnosisUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        _clear_rss()

    def _diagnose(self):
        with patch("app.agent.rss_actions.db.now", return_value=_SNAPSHOT):
            return diagnose_rss({})

    def test_arguments_reject_extra_fields(self):
        self.assertEqual(rss_diagnosis_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, "^rss\\.diagnose 不接受参数$"):
            rss_diagnosis_arguments({"RSS_PASSKEY_9f": 1})

    def test_empty_and_disabled_states_are_explicit(self):
        result = self._diagnose()
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.data["subscriptions"]["total"], 0)
        db.add_rss_subscription(
            name="disabled", urls="https://example.invalid/private", enabled=0
        )
        result = self._diagnose()
        self.assertEqual(result.status, "inactive")
        self.assertEqual(result.data["subscriptions"]["disabled"], 1)

    def test_counts_schedule_backlog_stale_failed_and_inconsistent_states(self):
        scheduled = db.add_rss_subscription(
            name="scheduled",
            urls="https://example.invalid/a",
            enabled=1,
            refresh_interval_minutes=30,
        )
        db.add_rss_subscription(
            name="disabled", urls="https://example.invalid/b", enabled=0
        )
        manual = db.add_rss_subscription(
            name="manual",
            urls="https://example.invalid/c",
            enabled=1,
            refresh_cron="0 4 * * *",
            refresh_interval_minutes=0,
        )
        invalid_refresh = db.add_rss_subscription(
            name="invalid-refresh",
            urls="https://example.invalid/d",
            enabled=1,
            refresh_interval_minutes=30,
        )
        db.update_rss_subscription(invalid_refresh, {"last_refreshed_at": "not-a-time"})
        pending_old = db.add_rss_entry(scheduled, "old", "g-old")
        pending_recent = db.add_rss_entry(scheduled, "recent", "g-recent")
        submitting_old = db.add_rss_entry(scheduled, "sub-old", "g-sub-old")
        submitting_recent = db.add_rss_entry(scheduled, "sub-recent", "g-sub-recent")
        failed = db.add_rss_entry(manual, "failed", "g-failed")
        downloaded = db.add_rss_entry(manual, "downloaded", "g-downloaded")
        skipped = db.add_rss_entry(manual, "skipped", "g-skipped")
        inconsistent = db.add_rss_entry(manual, "bad", "g-bad")
        null_status = db.add_rss_entry(invalid_refresh, "null-status", "g-null-status")
        assert all(

                item is not None
                for item in (
                    pending_old,
                    pending_recent,
                    submitting_old,
                    submitting_recent,
                    failed,
                    downloaded,
                    skipped,
                    inconsistent,
                    null_status,
                )

        )
        _set_entry(pending_old, status="pending", created_at="2026-07-31 12:00:00")
        _set_entry(pending_recent, status="pending", created_at="2026-07-31 12:00:01")
        _set_entry(
            submitting_old,
            status="submitting",
            created_at="2026-08-01 11:00:00",
            submitted_at="2026-08-01 11:45:00",
        )
        _set_entry(
            submitting_recent,
            status="submitting",
            created_at="2026-08-01 11:00:00",
            submitted_at="2026-08-01 11:45:01",
        )
        _set_entry(failed, status="failed")
        _set_entry(downloaded, status="downloaded", processed=1, processed_at=_SNAPSHOT)
        _set_entry(skipped, status="skipped", processed=1, processed_at=_SNAPSHOT)
        _set_entry(inconsistent, status="mystery", processed=0)
        _set_entry(null_status, status=None, processed=0)
        result = self._diagnose()
        self.assertEqual(result.status, "attention")
        self.assertEqual(
            result.data["subscriptions"],
            {
                "total": 4,
                "enabled": 3,
                "disabled": 1,
                "scheduled": 2,
                "manual_only": 1,
                "never_refreshed": 2,
                "invalid_last_refreshed_at": 1,
                "due_now": 2,
                "cron_configured_but_not_scheduled": 1,
            },
        )
        entries = result.data["entries"]
        self.assertEqual(entries["total"], 9)
        self.assertEqual(entries["pending"], 2)
        self.assertEqual(entries["pending_backlog"], 1)
        self.assertEqual(entries["pending_recent"], 1)
        self.assertEqual(entries["submitting"], 2)
        self.assertEqual(entries["stale_submitting"], 1)
        self.assertEqual(entries["submitting_in_flight"], 1)
        self.assertEqual(entries["failed"], 1)
        self.assertEqual(entries["downloaded"], 1)
        self.assertEqual(entries["skipped"], 1)
        self.assertEqual(entries["terminal"], 2)
        self.assertEqual(entries["unknown_or_inconsistent"], 2)
        self.assertEqual(result.data["attention"]["total"], 7)
        self.assertFalse(result.data["attention"]["truncated"])
        invalid_attention = next(

                item
                for item in result.data["attention"]["subscriptions"]
                if item["subscription_id"] == invalid_refresh

        )
        self.assertEqual(invalid_attention["schedule_state"], "scheduled_invalid")
        self.assertTrue(invalid_attention["invalid_last_refreshed_at"])
        self.assertEqual(
            invalid_attention["entry_counts"]["unknown_or_inconsistent"], 1
        )
        self.assertIn(
            invalid_refresh,
            {row["id"] for row in db.list_due_rss_subscriptions(_SNAPSHOT)},
        )

    def test_nullable_enabled_subscription_is_consistently_disabled(self):
        sub_id = db.add_rss_subscription(
            name="nullable-enabled", urls="https://example.invalid/nullable", enabled=1
        )
        entry_id = db.add_rss_entry(sub_id, "failed", "nullable-guid")
        assert entry_id is not None
        _set_entry(entry_id, status="failed")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_items SET enabled=NULL WHERE id=?", (sub_id,))
        result = self._diagnose()
        self.assertEqual(result.status, "inactive")
        self.assertEqual(result.data["subscriptions"]["disabled"], 1)
        attention = result.data["attention"]["subscriptions"]
        self.assertEqual(attention[0]["schedule_state"], "disabled")

    def test_sensitive_subscription_and_entry_fields_never_leave_projection(self):
        secrets = [
            "RSS_PASSKEY_9f",
            "RSS_GUID_SECRET",
            "RSS_PAYLOAD_SECRET",
            "RSS_PATH_SECRET",
            "RSS_TITLE_SECRET",
        ]
        sub_id = db.add_rss_subscription(
            name="RSS_TITLE_SECRET",
            urls="https://tracker.invalid/rss?passkey=RSS_PASSKEY_9f",
            enabled=1,
            qb_save_path="/srv/RSS_PATH_SECRET",
            gy_target_dir="C:\\RSS_PATH_SECRET",
            gy_target_dir_name="RSS_PATH_SECRET",
        )
        entry_id = db.add_rss_entry(
            sub_id,
            "RSS_TITLE_SECRET magnet:?xt=urn:btih:RSS_PAYLOAD_SECRET",
            "RSS_GUID_SECRET",
            payload=json.dumps(
                {
                    "torrent_url": "ed2k://RSS_PAYLOAD_SECRET/",
                    "token": "RSS_PASSKEY_9f",
                    "path": "/srv/RSS_PATH_SECRET",
                }
            ),
        )
        assert entry_id is not None
        _set_entry(entry_id, status="failed", created_at="2026-07-01 00:00:00")
        serialized = json.dumps(self._diagnose().to_dict(), ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        for forbidden_key in (
            '"urls"',
            '"guid"',
            '"payload"',
            '"torrent_url"',
            '"qb_save_path"',
            '"gy_target_dir"',
            '"name"',
            '"title"',
        ):
            self.assertNotIn(forbidden_key, serialized)

    def test_attention_projection_is_bounded_and_marks_truncation(self):
        for index in range(25):
            sub_id = db.add_rss_subscription(
                name=f"sub-{index}", urls=f"https://example.invalid/{index}", enabled=1
            )
            entry_id = db.add_rss_entry(sub_id, f"failed-{index}", f"guid-{index}")
            assert entry_id is not None
            _set_entry(entry_id, status="failed")
        result = self._diagnose()
        self.assertEqual(result.data["entries"]["failed"], 25)
        self.assertEqual(len(result.data["attention"]["subscriptions"]), 20)
        self.assertTrue(result.data["attention"]["truncated"])

    def test_database_exception_is_fixed_and_sanitized(self):
        with patch("app.agent.rss_actions.db.get_rss_diagnostic_summary") as mocked:
            mocked.side_effect = RuntimeError("RSS_PASSKEY_9f /srv/RSS_PATH_SECRET")
            result = self._diagnose()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("RSS_PASSKEY_9f", serialized)
        self.assertNotIn("RSS_PATH_SECRET", serialized)
