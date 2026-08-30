"""结构化结果协议与 TG 可靠通知（Sprint 6）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 6.1 版本化任务结果 Schema 与新旧兼容
- Task 6.2 Telegram 通知 outbox 的重试与幂等
- Task 6.3 STRM 明细刷屏上限
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from app import database as db
from app.notifier import TelegramSendResult
from app.modules.organize_notification_outbox import (
    deliver_organize_notification,
    drain_organize_notifications,
    recover_organize_notifications,
    summary_idempotency_key,
)
from app.modules.organize_results import (
    ORGANIZE_RESULT_SCHEMA_VERSION,
    build_organize_result,
    read_organize_result,
)
from app.modules.strm_notifications import build_strm_detail_messages
from app.repositories.organize_notifications import (
    claim_due_organize_notifications,
    count_pending_organize_notifications,
    list_organize_notifications,
    mark_organize_notification_sent,
    recover_stale_organize_notifications,
    retry_organize_notification,
)
from tests.support import IsolatedDatabaseTestCase


class OrganizeResultSchemaTests(unittest.TestCase):
    """Task 6.1：版本化结构化结果。"""

    def test_result_exposes_counters_groups_and_strm_without_log_parsing(self):
        result = build_organize_result(
            {
                "total": 10, "moved": 8, "failed": 1,
                "group_results": [{"group_path": "作品 A", "status": "completed"}],
                "strm": {"ok": True},
                "strm_changes": [{"rel_dir": "剧集/A", "name": "E01.mkv"}],
                "media_refresh": {"Jellyfin": True},
                "group_progress": {"total": 3, "completed": 3},
            },
            status="completed",
            source_results=[{"id": "root", "name": "待整理"}],
            notification_sent=True,
        )

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["counters"]["moved"], 8)
        self.assertEqual(result["groups"][0]["group_path"], "作品 A")
        self.assertEqual(result["strm"], {"ok": True})
        self.assertEqual(result["media_refresh"], {"Jellyfin": True})
        self.assertTrue(result["notification"]["sent"])
        self.assertEqual(result["sources"][0]["id"], "root")

    def test_missing_fields_default_without_raising(self):
        result = build_organize_result({}, status="stopped")

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["counters"]["moved"], 0)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["strm"], {})

    def test_legacy_stats_payload_is_still_readable(self):
        legacy = {"total": 4, "moved": 3, "status": "completed"}

        result = read_organize_result(legacy)

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["counters"]["moved"], 3)
        self.assertEqual(result["status"], "completed")

    def test_legacy_task_wrapper_preserves_identifiers_sources_and_counters(self):
        legacy = {
            "task_id": "task-legacy",
            "status": "partial",
            "current_source": "待整理/动漫",
            "error": "部分目录待确认",
            "notification_sent": True,
            "stats": {"total": 4, "moved": 3, "need_confirm": 1},
            "source_results": [{"id": "root", "status": "partial"}],
        }

        result = read_organize_result(legacy)

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["task_id"], "task-legacy")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["current_source"], "待整理/动漫")
        self.assertEqual(result["error"], "部分目录待确认")
        self.assertEqual(result["counters"]["moved"], 3)
        self.assertEqual(result["counters"]["need_confirm"], 1)
        self.assertEqual(result["sources"], [{"id": "root", "status": "partial"}])
        self.assertTrue(result["notification"]["sent"])

    def test_unknown_future_version_is_read_best_effort(self):
        payload = {
            "schema_version": 99,
            "status": "completed",
            "counters": {"moved": 5},
            "brand_new_field": {"a": 1},
        }

        result = read_organize_result(payload)

        self.assertEqual(result["schema_version"], 99)
        self.assertEqual(result["counters"]["moved"], 5)
        self.assertEqual(result["counters"]["failed"], 0)
        self.assertEqual(result["brand_new_field"], {"a": 1})

    def test_non_dict_payload_degrades_to_an_empty_result(self):
        for payload in (None, "text", 5, []):
            with self.subTest(payload=payload):
                result = read_organize_result(payload)

                self.assertEqual(result["counters"]["moved"], 0)

    def test_changed_target_dirs_are_derived_from_strm_changes(self):
        # 任务级 stats 不单独维护 changed_target_dirs，必须能从变化清单推导。
        result = build_organize_result(
            {
                "strm_changes": [
                    {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
                    {"rel_dir": "剧集/A/Season 01", "name": "E02.mkv"},
                    {"rel_dir": "电影/B", "name": "B.mkv"},
                ],
            },
            status="completed",
        )

        self.assertEqual(
            result["changed_target_dirs"], ["剧集/A/Season 01", "电影/B"]
        )

    def test_explicit_changed_target_dirs_take_precedence(self):
        result = build_organize_result(
            {
                "changed_target_dirs": ["显式目录"],
                "strm_changes": [{"rel_dir": "别的", "name": "x.mkv"}],
            },
            status="completed",
        )

        self.assertEqual(result["changed_target_dirs"], ["显式目录"])

    def test_malformed_counters_never_raise(self):
        result = build_organize_result(
            {"moved": "not-a-number", "failed": None}, status="partial",
        )

        self.assertEqual(result["counters"]["moved"], 0)
        self.assertEqual(result["counters"]["failed"], 0)


class OrganizeNotificationOutboxTests(IsolatedDatabaseTestCase):
    """Task 6.2：通知 outbox 的重试与幂等。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_notification_outbox")

    def test_successful_delivery_marks_the_event_sent(self):
        sent: list[str] = []
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            delivered = deliver_organize_notification("task:a", "整理完成", chat_id="1")

        self.assertTrue(delivered)
        self.assertEqual(sent, ["整理完成"])
        self.assertEqual(count_pending_organize_notifications(), 0)

    def test_cover_is_persisted_and_forwarded_by_outbox(self):
        sent: list[tuple[str, str, str]] = []

        def send_result(text, chat_id=None, *, image_url=""):
            sent.append((text, str(chat_id or ""), image_url))
            return TelegramSendResult(ok=True)

        with patch("app.notifier.send_result", side_effect=send_result):
            delivered = deliver_organize_notification(
                "task:cover",
                "整理完成",
                chat_id="1",
                image_url="https://image.example/poster.jpg",
            )

        self.assertTrue(delivered)
        self.assertEqual(sent, [(
            "整理完成", "1", "https://image.example/poster.jpg",
        )])
        row = dict(list_organize_notifications()[0])
        self.assertEqual(row["image_url"], "https://image.example/poster.jpg")

    def test_disabled_notification_policy_consumes_legacy_outbox_without_sending(self):
        with patch(
            "app.modules.telegram_notification_policy.allows_notification",
            return_value=False,
        ), patch("app.notifier.send_result") as sender:
            delivered = deliver_organize_notification(
                "task:disabled", "整理完成", chat_id="1",
            )

        self.assertTrue(delivered)
        sender.assert_not_called()
        row = dict(list_organize_notifications()[0])
        self.assertEqual(row["status"], "sent")
        self.assertEqual(count_pending_organize_notifications(), 0)

    def test_temporary_failure_is_retried_and_not_duplicated(self):
        sent: list[str] = []
        with patch("app.notifier.send_result", return_value=TelegramSendResult(ok=False, error="timeout", status_code=503)):
            self.assertFalse(
                deliver_organize_notification("task:b", "整理完成", chat_id="1")
            )
        row = dict(list_organize_notifications()[0])
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["attempts"], 1)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_notification_outbox SET next_attempt_at='2000-01-01 00:00:00'"
            )
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            self.assertTrue(drain_organize_notifications())

        self.assertEqual(sent, ["整理完成"])
        self.assertEqual(count_pending_organize_notifications(), 0)

    def test_unknown_delivery_is_consumed_without_replay(self):
        with patch(
            "app.notifier.send_result",
            return_value=TelegramSendResult(
                ok=False, error="ReadTimeout", status_code=408,
            ),
        ):
            self.assertFalse(
                deliver_organize_notification(
                    "task:unknown", "整理完成", chat_id="1"
                )
            )

        row = dict(list_organize_notifications()[0])
        self.assertEqual(row["status"], "sent")
        self.assertEqual(count_pending_organize_notifications(), 0)
        with patch("app.notifier.send_result") as sender:
            self.assertTrue(
                deliver_organize_notification(
                    "task:unknown", "整理完成", chat_id="1"
                )
            )
        sender.assert_not_called()

    def test_one_shot_unknown_delivery_is_not_enqueued(self):
        with patch(
            "app.notifier.send_result",
            return_value=TelegramSendResult(
                ok=False, error="ReadTimeout", status_code=408,
            ),
        ):
            self.assertFalse(
                deliver_organize_notification("", "整理完成", chat_id="1")
            )

        self.assertEqual(list_organize_notifications(), [])

    def test_already_sent_event_is_never_sent_twice(self):
        with patch("app.notifier.send_result", return_value=TelegramSendResult(ok=True)):
            deliver_organize_notification("task:c", "整理完成", chat_id="1")

        sent: list[str] = []
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            delivered = deliver_organize_notification("task:c", "整理完成", chat_id="1")

        self.assertTrue(delivered)
        self.assertEqual(sent, [])

    def test_identical_content_from_two_tasks_is_still_delivered_twice(self):
        sent: list[str] = []
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            deliver_organize_notification("task:d1", "结果完全相同", chat_id="1")
            deliver_organize_notification("task:d2", "结果完全相同", chat_id="1")

        self.assertEqual(len(sent), 2)

    def test_send_exception_is_captured_for_retry(self):
        with patch("app.notifier.send_result", side_effect=RuntimeError("timeout")):
            self.assertFalse(
                deliver_organize_notification("task:e", "整理完成", chat_id="1")
            )

        self.assertEqual(count_pending_organize_notifications(), 1)

    def test_keyless_delivery_does_not_dedupe_across_runs(self):
        sent: list[str] = []
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            deliver_organize_notification("", "无任务 ID 的通知", chat_id="1")
            deliver_organize_notification("", "无任务 ID 的通知", chat_id="1")

        self.assertEqual(len(sent), 2)
        self.assertEqual(count_pending_organize_notifications(), 0)

    def test_keyless_failure_still_enqueues_a_unique_retry(self):
        with patch("app.notifier.send_result", return_value=TelegramSendResult(ok=False, error="timeout", status_code=503)):
            deliver_organize_notification("", "无任务 ID 的通知", chat_id="1")
            deliver_organize_notification("", "无任务 ID 的通知", chat_id="1")

        self.assertEqual(count_pending_organize_notifications(), 2)

    def test_restart_recovers_interrupted_sending_events(self):
        with patch("app.notifier.send_result", return_value=TelegramSendResult(ok=False, error="timeout", status_code=503)):
            deliver_organize_notification("task:f", "整理完成", chat_id="1")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_notification_outbox SET status='sending'")

        sent: list[str] = []
        with patch("app.notifier.send_result", side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True)):
            recovered = recover_organize_notifications()

        self.assertEqual(recovered, 1)
        self.assertEqual(sent, ["整理完成"])

    def test_retry_after_from_telegram_extends_next_attempt(self):
        with patch(
            "app.notifier.send_result",
            return_value=TelegramSendResult(
                ok=False, status_code=429, retry_after_seconds=300,
                error="Too Many Requests",
            ),
        ):
            self.assertFalse(
                deliver_organize_notification("task:429", "整理完成", chat_id="1")
            )

        row = dict(list_organize_notifications()[0])
        delay = (
            datetime.strptime(row["next_attempt_at"], "%Y-%m-%d %H:%M:%S")
            - datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
        ).total_seconds()
        self.assertGreaterEqual(delay, 300)
        self.assertEqual(row["last_error"], "Too Many Requests")

    def test_retry_backoff_is_exponential(self):
        with db.get_conn() as conn:
            stamp = db.now()
            conn.execute(
                "INSERT INTO organize_notification_outbox("
                "idempotency_key,chat_id,body,status,next_attempt_at,created_at,updated_at"
                ") VALUES(?,?,?,'pending',?,?,?)",
                ("task:backoff", "1", "整理完成", stamp, stamp, stamp),
            )
        observed: list[int] = []
        for _ in range(3):
            claimed = claim_due_organize_notifications(limit=1)[0]
            state = retry_organize_notification(
                claimed["id"],
                expected_lease_generation=claimed["lease_generation"],
                error="timeout",
            )
            self.assertEqual(state, "retry_wait")
            row = dict(list_organize_notifications()[0])
            delay = int((
                datetime.strptime(row["next_attempt_at"], "%Y-%m-%d %H:%M:%S")
                - datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds())
            observed.append(delay)
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE organize_notification_outbox "
                    "SET next_attempt_at='2000-01-01 00:00:00' WHERE id=?",
                    (claimed["id"],),
                )
        self.assertEqual(observed, [30, 60, 120])

    def test_recovered_lease_fences_late_worker_results(self):
        with db.get_conn() as conn:
            stamp = db.now()
            conn.execute(
                "INSERT INTO organize_notification_outbox("
                "idempotency_key,chat_id,body,status,next_attempt_at,created_at,updated_at"
                ") VALUES(?,?,?,'pending',?,?,?)",
                ("task:fence", "1", "整理完成", stamp, stamp, stamp),
            )
        first = claim_due_organize_notifications(limit=1)[0]
        self.assertEqual(recover_stale_organize_notifications(), 1)
        second = claim_due_organize_notifications(limit=1)[0]
        self.assertGreater(second["lease_generation"], first["lease_generation"])
        self.assertFalse(mark_organize_notification_sent(
            first["id"], expected_lease_generation=first["lease_generation"],
        ))
        self.assertEqual(
            retry_organize_notification(
                first["id"],
                expected_lease_generation=first["lease_generation"],
                error="late",
            ),
            "stale",
        )
        self.assertTrue(mark_organize_notification_sent(
            second["id"], expected_lease_generation=second["lease_generation"],
        ))

    def test_summary_key_requires_a_task_id(self):
        self.assertEqual(summary_idempotency_key("task-1", chat_id="9"), "organize-summary:task-1:9")
        self.assertEqual(summary_idempotency_key(""), "")
        self.assertEqual(summary_idempotency_key("   "), "")


class StrmDetailFloodProtectionTests(unittest.TestCase):
    """Task 6.3：STRM 明细刷屏上限。"""

    @staticmethod
    def _changes(count: int) -> list[dict]:
        return [
            {"action": "generated", "directory": f"剧集/作品 {index // 20}", "filename": f"E{index:04d}.strm"}
            for index in range(count)
        ]

    def test_details_are_paged_at_twenty_files(self):
        messages = build_strm_detail_messages(self._changes(21))

        self.assertEqual(len(messages), 2)

    def test_exactly_twenty_files_stay_in_one_message(self):
        messages = build_strm_detail_messages(self._changes(20))

        self.assertEqual(len(messages), 1)

    def test_large_sync_falls_back_to_a_single_summary(self):
        messages = build_strm_detail_messages(self._changes(200), max_messages=3)

        self.assertEqual(len(messages), 1)
        self.assertIn("只发送摘要", messages[0])
        self.assertIn("200", messages[0])

    def test_summary_fallback_reports_previously_omitted_changes(self):
        messages = build_strm_detail_messages(
            self._changes(200), max_messages=3, omitted_count=45,
        )

        self.assertIn("45", messages[0])

    def test_limit_disabled_keeps_full_detail_pages(self):
        messages = build_strm_detail_messages(self._changes(200), max_messages=0)

        self.assertGreater(len(messages), 3)

    def test_no_changes_produces_no_message(self):
        self.assertEqual(build_strm_detail_messages([], max_messages=3), [])


if __name__ == "__main__":
    unittest.main()
