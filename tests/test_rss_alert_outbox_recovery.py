"""RSS 告警与真实隔离通知 outbox：恢复复发、跨订阅、崩溃后不丢不重。"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from app import database as db
from app.modules import telegram_notification_center as center
from app.modules.rss_scheduler import RSSScheduler
from app.notifier import NotificationEvent, render_event
from app.repositories import rss as rss_repo
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    complete_notification,
)
from tests.support import IsolatedDatabaseTestCase


class RSSAlertOutboxRecoveryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM telegram_notification_outbox")
            conn.execute("DELETE FROM settings_kv WHERE key LIKE 'rss.scheduler.alert_signature.%'")
        for mock in (
            patch("app.repositories.rss.get_rss_subscription", return_value={"name": "测试订阅"}),
            patch.object(center, "notification_target_chat_id", return_value="test-chat"),
            patch.object(center, "allows_notification", return_value=True),
            patch.object(center._dispatch_stop, "is_set", return_value=True),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def _notify(scheduler, sub_id=1, code="partial_failure"):
        scheduler._notify_issue(sub_id, code, [("失败", 1)])

    @staticmethod
    def _outbox():
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM telegram_notification_outbox ORDER BY id")]

    @staticmethod
    def _ack_all():
        for row in claim_due_notifications(limit=100):
            complete_notification(
                row["id"], lease_generation=row["lease_generation"],
                claimed_revision=row["revision"], message_id=row["id"] + 100,
            )

    def test_recovery_then_identical_failure_creates_new_deliverable_event(self) -> None:
        scheduler = RSSScheduler()
        self._notify(scheduler)
        self._ack_all()
        self._notify(scheduler)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        rows = self._outbox()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["status"] for row in rows], ["sent", "pending"])
        self.assertNotEqual(rows[0]["event_key"], rows[1]["event_key"])

    def test_different_issue_then_same_issue_is_a_new_occurrence(self) -> None:
        scheduler = RSSScheduler()
        for code in ("partial_failure", "outcome_unknown", "partial_failure"):
            self._notify(scheduler, code=code)
            self._ack_all()
        self.assertEqual(len(self._outbox()), 3)

    def test_subscriptions_keep_independent_occurrence_identities(self) -> None:
        scheduler = RSSScheduler()
        self._notify(scheduler, 1)
        self._notify(scheduler, 2)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler(), 1)
        self._notify(RSSScheduler(), 2)
        self.assertEqual(len(self._outbox()), 3)

    def test_crash_after_outbox_before_ack_reuses_reserved_event(self) -> None:
        publish = center.publish_notification_event

        def interrupted(*args, **kwargs):
            publish(*args, **kwargs)
            raise KeyboardInterrupt("crash after durable outbox insert")

        with (
            patch.object(center, "publish_notification_event", side_effect=interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._notify(RSSScheduler())
        self._ack_all()
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        # 即使 ACK 写入前崩溃，也要记住本次 occurrence，恢复后复发不能被旧 key 吞掉。
        RSSScheduler()._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_legacy_signature_is_not_replayed_but_recurrence_is_new(self) -> None:
        scheduler = RSSScheduler()
        legacy = json.dumps(("partial_failure", "1"), ensure_ascii=False, separators=(",", ":"))
        db.kv_set(scheduler._alert_key(1), legacy)
        digest = hashlib.sha256(legacy.encode()).hexdigest()[:20]
        center.publish_notification_event(
            f"rss-alert:1:{digest}", NotificationEvent("历史告警"), topic="rss", importance="error",
        )
        self._ack_all()
        self._notify(scheduler)
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_ack_write_failure_retries_same_outbox_event_after_restart(self) -> None:
        scheduler = RSSScheduler()
        save = scheduler._save_alert

        def fail_ack(sub_id, alert):
            if alert["accepted"]:
                raise RuntimeError("disk busy after outbox commit")
            return save(sub_id, alert)

        with patch.object(scheduler, "_save_alert", side_effect=fail_ack):
            self._notify(scheduler)
        pending = json.loads(db.kv_get(scheduler._alert_key(1)))
        self.assertFalse(pending["accepted"])
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        self.assertTrue(json.loads(db.kv_get(scheduler._alert_key(1)))["accepted"])

    def test_failed_reservation_does_not_publish_unrecoverable_event(self) -> None:
        with (
            patch.object(RSSScheduler, "_save_alert", side_effect=RuntimeError("disk unavailable")),
            patch.object(center, "publish_notification_event") as publish,
        ):
            self._notify(RSSScheduler())
        publish.assert_not_called()
        self.assertEqual(self._outbox(), [])

    def test_failed_publish_retries_pending_identity_and_recovery_clears_it(self) -> None:
        scheduler = RSSScheduler()
        with patch.object(center, "publish_notification_event", return_value=False):
            self._notify(scheduler)
        reserved = json.loads(db.kv_get(scheduler._alert_key(1)))["event_key"]
        with patch.object(center, "publish_notification_event", wraps=center.publish_notification_event) as publish:
            self._notify(RSSScheduler())
        self.assertEqual(publish.call_args.args[0], reserved)
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_unknown_history_does_not_replay_until_business_recovery(self) -> None:
        scheduler = RSSScheduler()
        db.kv_set(scheduler._alert_key(1), '{"version":999,"signature":["partial_failure","1"]}')
        self._notify(scheduler)
        self.assertEqual(self._outbox(), [])
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)

    def test_failure_details_reach_outbox_with_grouped_reasons_and_bounded_safe_examples(self):
        sid = rss_repo.add_rss_subscription("蜜柑", "https://example.invalid/rss")
        codes = ["torrent_invalid", "guangya_auth_failed", "guangya_outcome_unknown",
                 "guangya_auth_failed", "guangya_auth_failed"]
        failures = []
        for number, code in enumerate(codes):
            eid = int(rss_repo.add_rss_entry_with_media(
                sid, f"<剧集{number}> token=title-secret " + "长标题" * 100, str(number),
            )["id"])
            rss_repo.record_rss_entry_failure(eid, code, False)
            failures.append({"id": eid, "error": (
                "提交结果待核对，请先检查下载器状态，勿直接重复提交"
                if code == "guangya_outcome_unknown" else "光鸭认证失败（HTTP 401），token=error-secret"
            )})
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.auto_download.return_value = {
                "failed": 5, "outcome_unknown_count": 1, "failure_details": failures,
            }
            RSSScheduler()._execute(sid, "download")

        rows = self._outbox()
        self.assertEqual(len(rows), 1)
        event = center.deserialize_notification_event(rows[0]["event_json"])
        fields = list(event.fields)
        self.assertIn(("失败", "4"), fields)
        self.assertIn(("待核对", "1"), fields)
        reasons = [value for key, value in fields if key == "原因"]
        self.assertCountEqual(reasons, ["光鸭认证失败 · 3 条", "光鸭提交结果待确认 · 1 条", "种子文件无效 · 1 条"])
        self.assertEqual(sum(str(line).startswith("• #") for line in event.lines), 3)
        rendered = render_event(event)
        self.assertIn("HTTP 401", rendered)
        self.assertIn("另有 2 条未展开", rendered)
        self.assertIn("&lt;剧集4&gt;", rendered)
        self.assertIn("勿直接重投", rendered)
        self.assertNotIn("title-secret", rows[0]["event_json"])
        self.assertNotIn("error-secret", rows[0]["event_json"])
        self.assertLess(len(rendered), 2500)

    def test_same_count_new_reason_alerts_but_title_changes_and_restart_do_not(self):
        sid = rss_repo.add_rss_subscription("蜜柑", "https://example.invalid/rss")
        eid = int(rss_repo.add_rss_entry_with_media(sid, "剧集 E20", "episode")["id"])
        rss_repo.record_rss_entry_failure(eid, "guangya_auth_failed", False)
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.auto_download.return_value = {
                "failed": 1, "failure_details": [{"id": eid, "error": "光鸭认证失败（HTTP 401）"}],
            }
            engine.return_value.refresh.return_value = {"total": 0, "new": 0}
            scheduler._execute(sid, "download")
            self._ack_all()
            # 同类问题仅换了名称，不应在空轮次或重启后重新通知。
            with db.get_conn() as conn:
                conn.execute("UPDATE rss_entries SET title='剧集新标题' WHERE id=?", (eid,))
            scheduler._execute(sid, "subscribe")
            RSSScheduler()._execute(sid, "subscribe")
            self.assertEqual(len(self._outbox()), 1)
            # 仍为一条，但改成另一种失败时必须通知。
            rss_repo.record_rss_entry_failure(eid, "torrent_invalid", False)
            RSSScheduler()._execute(sid, "subscribe")
            rows = self._outbox()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["event_key"], rows[1]["event_key"])
            self.assertIn("种子文件无效", rows[1]["event_json"])
            self.assertEqual(rows[1]["status"], "pending")

    def test_current_retryable_failures_are_reported_without_unrelated_entries(self):
        sid = rss_repo.add_rss_subscription("蜜柑", "https://example.invalid/rss")
        other = rss_repo.add_rss_subscription("其他订阅", "https://example.invalid/other")
        ids = []
        for subscription, title, retryable in (
            (sid, "本轮超时", True), (sid, "历史可重试失败", True),
            (sid, "已处理", False), (other, "别的订阅", False),
        ):
            eid = int(rss_repo.add_rss_entry_with_media(subscription, title, title)["id"])
            rss_repo.record_rss_entry_failure(eid, "torrent_fetch_timeout", retryable)
            ids.append(eid)
        rss_repo.update_rss_entries_processed([ids[2]], True)
        summary = rss_repo.get_rss_failure_summary(sid, entry_ids=[ids[0], ids[2], ids[3]])
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["reasons"], [{"code": "torrent_fetch_timeout", "count": 1}])
        self.assertEqual([item["id"] for item in summary["entries"]], [ids[0]])
        self.assertEqual(rss_repo.get_rss_failure_summary(sid)["failed_count"], 0)
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.auto_download.return_value = {
                "failed": 1, "failure_details": [{"id": ids[0], "error": "种子文件下载超时"}],
            }
            RSSScheduler()._execute(sid, "download")
        self.assertIn("种子文件下载超时", self._outbox()[0]["event_json"])
        self.assertNotIn("历史可重试失败", self._outbox()[0]["event_json"])

    def test_current_unknown_without_details_does_not_mix_historical_failure_counts(self):
        sid = rss_repo.add_rss_subscription("蜜柑", "https://example.invalid/rss")
        old_id = int(rss_repo.add_rss_entry_with_media(sid, "历史失败", "old")["id"])
        rss_repo.record_rss_entry_failure(old_id, "guangya_auth_failed", False)
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.auto_download.return_value = {
                "failed": 1, "outcome_unknown_count": 1, "failure_details": [],
            }
            scheduler = RSSScheduler()
            scheduler._execute(sid, "download")
            event = center.deserialize_notification_event(self._outbox()[0]["event_json"])
            self.assertIn(("失败", "0"), event.fields)
            self.assertIn(("待核对", "1"), event.fields)
            self.assertNotIn("光鸭认证失败", render_event(event))
            self.assertIn("未取得条目详情", render_event(event))
            # 无新投递时再报告历史未处理失败，不能把两个集合用 max() 混算。
            engine.return_value.refresh.return_value = {"total": 0, "new": 0}
            scheduler._execute(sid, "subscribe")
            self.assertEqual(len(self._outbox()), 2)
            self.assertIn("光鸭认证失败", self._outbox()[1]["event_json"])
