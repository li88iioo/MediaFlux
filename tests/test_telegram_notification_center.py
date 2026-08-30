"""统一 Telegram 通知中心的幂等、线程更新与按钮保留契约。"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.modules import telegram_notification_center as notification_center
from app.modules.telegram_notification_center import (
    deserialize_notification_event,
    drain_telegram_notifications,
    get_notification_thread_snapshot,
    notification_thread_event_key,
    publish_notification_event,
    publish_notification_thread,
    serialize_notification_event,
)
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import (
    NotificationAction,
    NotificationEvent,
    TelegramSendResult,
    render_event,
    telegram_text_length,
)
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    fail_notification,
    get_notification,
    retry_notification,
    suppress_notification,
    upsert_notification,
)
from app.routes.api import save_config
from tests.support import IsolatedDatabaseTestCase


class TelegramNotificationCenterTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        self._dispatcher_was_stopped = notification_center._dispatch_stop.is_set()
        notification_center._dispatch_stop.clear()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM telegram_notification_outbox")

    def tearDown(self) -> None:
        if self._dispatcher_was_stopped:
            notification_center._dispatch_stop.set()
        else:
            notification_center._dispatch_stop.clear()

    def test_round_trip_keeps_actions_and_rendering_flags(self) -> None:
        event = NotificationEvent(
            "待确认",
            fields=(("媒体", "测试"),),
            lines=("候选 1",),
            footer="请选择",
            actions=(NotificationAction("确认", "orgc:token:0"),),
            layout="relaxed",
            field_emojis=False,
            state="running",
        )
        restored = deserialize_notification_event(serialize_notification_event(event))
        self.assertEqual(restored, event)
        self.assertEqual(restored.actions[0].callback_data, "orgc:token:0")

    def test_serialization_converges_dynamic_objects_to_safe_strings(self) -> None:
        restored = deserialize_notification_event(serialize_notification_event(
            NotificationEvent(
                Path("/tmp/poster"),
                fields=(("路径", Path("/tmp/media")),),
                lines=(Path("/tmp/line"),),
            )
        ))

        self.assertEqual(restored.title, "/tmp/poster")
        self.assertEqual(restored.fields, (("路径", "/tmp/media"),))
        self.assertEqual(restored.lines, ("/tmp/line",))

    def test_invalid_and_oversized_logical_keys_are_safely_bounded(self) -> None:
        invalid = publish_notification_event(
            "   ", NotificationEvent("无效"),
            topic=NotificationTopic.SYSTEM, chat_id="100", deliver_now=False,
        )
        self.assertFalse(invalid.accepted)
        self.assertEqual(invalid.status, "invalid_key")

        raw_key = "payload:" + ('{"title":"敏感示例"}' * 200)
        bounded = publish_notification_event(
            raw_key, NotificationEvent("有效"),
            topic=NotificationTopic.SYSTEM, chat_id="100", deliver_now=False,
        )
        self.assertTrue(bounded.accepted)
        self.assertLessEqual(len(bounded.event_key.encode("utf-8")), 160)
        self.assertNotIn("title", bounded.event_key)
        self.assertNotIn("敏感示例", bounded.event_key)

        thread = publish_notification_thread(
            raw_key, NotificationEvent("线程"),
            topic=NotificationTopic.SYSTEM, chat_id="100", deliver_now=False,
        )
        thread_row = get_notification(thread.event_key)
        self.assertTrue(str(thread_row["thread_key"]).startswith("sha256:"))
        self.assertNotIn("敏感示例", str(thread_row["thread_key"]))

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_one_shot_event_is_idempotent(self, sender) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=81)
        event = NotificationEvent("订阅更新")
        first = publish_notification_event(
            "subscription:1:missing", event,
            topic=NotificationTopic.MEDIA_SUBSCRIPTION, chat_id="100",
        )
        second = publish_notification_event(
            "subscription:1:missing", event,
            topic=NotificationTopic.MEDIA_SUBSCRIPTION, chat_id="100",
        )
        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_inflight_delivery_is_not_reclaimed_by_parallel_drain(self, sender) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        call_lock = threading.Lock()

        def deliver(_event, **_kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                current = calls
            if current == 1:
                started.set()
                release.wait(2.0)
            return TelegramSendResult(ok=True, message_id=80 + current)

        sender.side_effect = deliver
        result = {}

        def publish() -> None:
            result["outcome"] = publish_notification_thread(
                "local-media:12",
                NotificationEvent("本地媒体整理失败"),
                topic=NotificationTopic.LOCAL_MEDIA,
                importance=NotificationImportance.ERROR,
                chat_id="100",
            )

        worker = threading.Thread(target=publish)
        worker.start()
        self.assertTrue(started.wait(1.0))
        try:
            key = notification_thread_event_key(
                "local-media:12", topic=NotificationTopic.LOCAL_MEDIA, chat_id="100",
            )
            self.assertFalse(drain_telegram_notifications(event_key=key))
        finally:
            release.set()
            worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(result["outcome"].delivered)
        sender.assert_called_once()
        row = get_notification(key)
        self.assertEqual(row["lease_generation"], 1)
        self.assertEqual(row["message_id"], 81)

    def test_repeated_dispatcher_start_does_not_recover_live_leases(self) -> None:
        live_thread = SimpleNamespace(is_alive=lambda: True)
        stop_event = threading.Event()
        wake_event = threading.Event()
        with patch.object(notification_center, "_dispatch_thread", live_thread), patch.object(
            notification_center, "_dispatch_stop", stop_event,
        ), patch.object(
            notification_center, "_dispatch_wakeup", wake_event,
        ), patch.object(
            notification_center, "recover_notifications",
        ) as recover:
            notification_center.start_telegram_notification_dispatcher()

        recover.assert_not_called()
        self.assertTrue(wake_event.is_set())

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_essential_level_still_closes_existing_action_thread(
        self, sender, editor,
    ) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=91)
        editor.return_value = TelegramSendResult(ok=True, message_id=91)
        with patch(
            "app.modules.telegram_notification_policy.notification_level",
            return_value="essential",
        ):
            initial = publish_notification_thread(
                "confirmation:essential",
                NotificationEvent(
                    "待确认",
                    actions=(NotificationAction("确认", "orgc:token:0"),),
                ),
                topic=NotificationTopic.CONFIRMATION,
                importance=NotificationImportance.ACTION,
                chat_id="100",
            )
            terminal = publish_notification_thread(
                "confirmation:essential",
                NotificationEvent("已完成", footer="按钮已失效"),
                topic=NotificationTopic.CONFIRMATION,
                importance=NotificationImportance.RESULT,
                chat_id="100",
            )

        self.assertTrue(initial.delivered)
        self.assertTrue(terminal.delivered)
        sender.assert_called_once()
        editor.assert_called_once()
        self.assertEqual(tuple(editor.call_args.args[0].actions), ())

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_newer_thread_revision_survives_prior_unknown_edit(
        self, sender, editor,
    ) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=92)
        self.assertTrue(publish_notification_thread(
            "organize:unknown-revision",
            NotificationEvent("初始状态"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        ).delivered)

        started = threading.Event()
        release = threading.Event()
        edits = 0

        def edit(_event, **_kwargs):
            nonlocal edits
            edits += 1
            if edits == 1:
                started.set()
                release.wait(2.0)
                return TelegramSendResult(
                    ok=False, status_code=408, error="ReadTimeout", message_id=92,
                )
            return TelegramSendResult(ok=True, message_id=92)

        editor.side_effect = edit
        worker = threading.Thread(target=lambda: publish_notification_thread(
            "organize:unknown-revision",
            NotificationEvent("中间状态"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        ))
        worker.start()
        self.assertTrue(started.wait(1.0))
        publish_notification_thread(
            "organize:unknown-revision",
            NotificationEvent("最新终态"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        )
        release.set()
        worker.join(2.0)

        key = notification_thread_event_key(
            "organize:unknown-revision",
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        )
        pending = get_notification(key)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["revision"], 3)
        self.assertTrue(drain_telegram_notifications(event_key=key))
        final = get_notification(key)
        self.assertEqual(final["status"], "sent")
        self.assertEqual(final["delivered_revision"], 3)
        self.assertEqual(editor.call_count, 2)

    def test_stale_permanent_failure_requeues_newer_revision(self) -> None:
        event_key = "test:stale-failure"
        upsert_notification(
            event_key,
            thread_key="organize:stale-failure",
            topic="organize",
            importance="result",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 1")),
            replace=True,
        )
        claimed = claim_due_notifications(limit=1, event_key=event_key)[0]
        upsert_notification(
            event_key,
            thread_key="organize:stale-failure",
            topic="organize",
            importance="result",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 2")),
            replace=True,
        )

        self.assertTrue(fail_notification(
            claimed["id"],
            lease_generation=claimed["lease_generation"],
            claimed_revision=claimed["revision"],
            error="TelegramRequestRejected",
        ))
        row = get_notification(event_key)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["attempts"], 0)

    def test_stale_suppression_does_not_consume_newer_revision(self) -> None:
        event_key = "test:stale-suppression"
        upsert_notification(
            event_key,
            thread_key="organize:stale-suppression",
            topic="organize",
            importance="detail",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 1")),
            replace=True,
        )
        claimed = claim_due_notifications(limit=1, event_key=event_key)[0]
        upsert_notification(
            event_key,
            thread_key="organize:stale-suppression",
            topic="organize",
            importance="result",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 2")),
            replace=True,
        )

        self.assertTrue(suppress_notification(
            claimed["id"],
            lease_generation=claimed["lease_generation"],
            claimed_revision=claimed["revision"],
        ))
        row = get_notification(event_key)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["delivered_revision"], 0)

    def test_retry_exhaustion_does_not_fail_newer_revision(self) -> None:
        event_key = "test:stale-retry"
        upsert_notification(
            event_key,
            thread_key="organize:stale-retry",
            topic="organize",
            importance="result",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 1")),
            replace=True,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET attempts=6 WHERE event_key=?",
                (event_key,),
            )
        claimed = claim_due_notifications(limit=1, event_key=event_key)[0]
        upsert_notification(
            event_key,
            thread_key="organize:stale-retry",
            topic="organize",
            importance="result",
            chat_id="100",
            event_json=serialize_notification_event(NotificationEvent("revision 2")),
            replace=True,
        )

        status = retry_notification(
            claimed["id"],
            lease_generation=claimed["lease_generation"],
            claimed_revision=claimed["revision"],
            error="TelegramUnavailable",
        )
        self.assertEqual(status, "pending")
        row = get_notification(event_key)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["attempts"], 0)

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_unknown_fallback_send_drops_stale_message_identity_and_never_replays(
        self, sender, editor,
    ) -> None:
        sender.side_effect = [
            TelegramSendResult(ok=True, message_id=193),
            TelegramSendResult(ok=False, status_code=408, error="ReadTimeout"),
        ]
        editor.return_value = TelegramSendResult(
            ok=False,
            status_code=400,
            error="Bad Request: message to edit not found",
        )
        thread_key = "organize:fallback-unknown"
        first = publish_notification_thread(
            thread_key,
            NotificationEvent("initial"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        )
        self.assertTrue(first.delivered)

        second = publish_notification_thread(
            thread_key,
            NotificationEvent("terminal"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        )
        self.assertTrue(second.accepted)
        self.assertFalse(second.delivered)
        row = get_notification(second.event_key)
        self.assertEqual(row["status"], "outcome_unknown")
        self.assertEqual(int(row["message_id"] or 0), 0)

        publish_notification_thread(
            thread_key,
            NotificationEvent("newer terminal"),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
        )
        row = get_notification(second.event_key)
        self.assertEqual(row["status"], "outcome_unknown")
        self.assertEqual(row["revision"], 3)
        self.assertEqual(int(row["message_id"] or 0), 0)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(editor.call_count, 1)

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_thread_is_bounded_to_one_editable_text_message(self, sender) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=93)
        result = publish_notification_thread(
            "system:long-thread",
            NotificationEvent(
                "长生命周期",
                lines=("超长详情" * 1500,),
                image_url="https://example.invalid/poster.jpg",
            ),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
        )
        self.assertTrue(result.delivered)
        delivered_event = sender.call_args.args[0]
        self.assertEqual(delivered_event.image_url, "")
        self.assertLessEqual(telegram_text_length(render_event(delivered_event)), 3800)
        self.assertIn("完整详情请在 Web", str(delivered_event.footer))

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_thread_with_non_bmp_text_is_bounded_by_utf16_units(self, sender) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=94)
        result = publish_notification_thread(
            "system:emoji-thread",
            NotificationEvent("😀" * 500, lines=(("😀" * 3000),)),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
        )

        self.assertTrue(result.delivered)
        delivered_event = sender.call_args.args[0]
        self.assertLessEqual(telegram_text_length(render_event(delivered_event)), 3800)

    def test_long_action_label_is_normalized_without_losing_action(self) -> None:
        result = publish_notification_event(
            "long-action-label",
            NotificationEvent(
                "待确认",
                actions=(NotificationAction("😀" * 80, "orgc:token:0"),),
            ),
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.ACTION,
            chat_id="100",
            deliver_now=False,
        )

        self.assertTrue(result.accepted)
        row = get_notification(result.event_key)
        restored = deserialize_notification_event(row["event_json"])
        self.assertEqual(restored.actions[0].callback_data, "orgc:token:0")
        self.assertEqual(telegram_text_length(restored.actions[0].label), 64)

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_action_event_with_only_invalid_buttons_is_rejected(self, sender) -> None:
        result = publish_notification_event(
            "invalid-actions",
            NotificationEvent(
                "待确认",
                actions=(NotificationAction("确认", "x" * 65),),
            ),
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.ACTION,
            chat_id="100",
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "invalid_actions")
        sender.assert_not_called()

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_permanent_telegram_4xx_fails_without_retry(self, sender) -> None:
        sender.return_value = TelegramSendResult(
            ok=False, status_code=403, error="Forbidden",
        )
        result = publish_notification_event(
            "forbidden",
            NotificationEvent("不可投递"),
            topic=NotificationTopic.SYSTEM,
            importance=NotificationImportance.ERROR,
            chat_id="100",
        )
        row = get_notification(result.event_key)
        self.assertEqual(result.status, "failed")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_corrupt_persisted_event_fails_once_without_retry(self, sender) -> None:
        result = publish_notification_event(
            "corrupt-event",
            NotificationEvent("稍后损坏"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            deliver_now=False,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET event_json='{',"
                "next_attempt_at=? WHERE event_key=?",
                (db.now(), result.event_key),
            )

        self.assertFalse(drain_telegram_notifications(event_key=result.event_key))
        row = get_notification(result.event_key)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("InvalidEventPayload", row["last_error"])
        sender.assert_not_called()

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_structurally_corrupt_payload_fails_once_without_retry(self, sender) -> None:
        result = publish_notification_event(
            "corrupt-event-shape",
            NotificationEvent("稍后损坏"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            deliver_now=False,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox "
                "SET event_json=?,next_attempt_at=? WHERE event_key=?",
                ('{"title":"损坏","lines":1}', db.now(), result.event_key),
            )

        self.assertFalse(drain_telegram_notifications(event_key=result.event_key))
        row = get_notification(result.event_key)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("InvalidEventPayload", row["last_error"])
        sender.assert_not_called()

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_legacy_caption_thread_falls_back_to_new_text_message(
        self, sender, editor,
    ) -> None:
        editor.return_value = TelegramSendResult(
            ok=False,
            status_code=400,
            error="Bad Request: there is no text in the message to edit",
            message_id=41,
        )
        sender.return_value = TelegramSendResult(ok=True, message_id=42)

        result = publish_notification_thread(
            "legacy-caption",
            NotificationEvent("迁移后的文本终态"),
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.RESULT,
            chat_id="100",
            preferred_message_id=41,
        )

        self.assertTrue(result.delivered)
        editor.assert_called_once()
        sender.assert_called_once()
        self.assertEqual(get_notification(result.event_key)["message_id"], 42)

    def test_recovery_retries_known_edits_but_quarantines_unknown_first_send(self) -> None:
        first_send = publish_notification_event(
            "interrupted-first-send",
            NotificationEvent("首次发送"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            deliver_now=False,
        )
        edit = publish_notification_thread(
            "interrupted-edit",
            NotificationEvent("线程编辑"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            preferred_message_id=77,
            deliver_now=False,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET status='sending' "
                "WHERE event_key IN (?,?)",
                (first_send.event_key, edit.event_key),
            )

        self.assertEqual(notification_center.recover_notifications(), 2)
        unknown_row = get_notification(first_send.event_key)
        retry_row = get_notification(edit.event_key)
        self.assertEqual(unknown_row["status"], "outcome_unknown")
        self.assertEqual(unknown_row["last_error"], "DeliveryOutcomeUnknown")
        self.assertEqual(retry_row["status"], "retry_wait")
        self.assertEqual(retry_row["last_error"], "ProcessInterrupted")

    def test_stale_lease_quarantines_unknown_first_send_but_reclaims_known_edit(self) -> None:
        first_send = publish_notification_event(
            "stale-first-send",
            NotificationEvent("首次发送"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            deliver_now=False,
        )
        edit = publish_notification_thread(
            "stale-edit",
            NotificationEvent("线程编辑"),
            topic=NotificationTopic.SYSTEM,
            chat_id="100",
            preferred_message_id=88,
            deliver_now=False,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET status='sending',"
                "updated_at='2000-01-01 00:00:00' WHERE event_key IN (?,?)",
                (first_send.event_key, edit.event_key),
            )

        claimed = claim_due_notifications(limit=10)

        self.assertEqual([row["event_key"] for row in claimed], [edit.event_key])
        unknown_row = get_notification(first_send.event_key)
        self.assertEqual(unknown_row["status"], "outcome_unknown")
        self.assertEqual(unknown_row["last_error"], "DeliveryOutcomeUnknown")
        self.assertEqual(unknown_row["lease_generation"], 1)
        self.assertEqual(claimed[0]["message_id"], 88)

    def test_periodic_purge_is_throttled_for_long_running_dispatcher(self) -> None:
        with patch.object(notification_center, "_next_purge_at", 0.0), patch.object(
            notification_center.time,
            "monotonic",
            side_effect=[100.0, 101.0, 100.0 + notification_center._PURGE_INTERVAL_SECONDS + 1],
        ), patch.object(
            notification_center, "purge_notifications", return_value=3,
        ) as purge:
            self.assertEqual(notification_center._maybe_purge_notifications(), 3)
            self.assertEqual(notification_center._maybe_purge_notifications(), 0)
            self.assertEqual(notification_center._maybe_purge_notifications(), 3)

        self.assertEqual(purge.call_count, 2)

    def test_media_detail_bounding_uses_logarithmic_render_search(self) -> None:
        from app.modules import telegram_media_projection as projection

        blocks = tuple(f"媒体 {index} " + ("x" * 80) for index in range(2048))
        original_render = projection.render_event
        with patch.object(
            projection, "render_event", wraps=original_render,
        ) as renderer:
            event = projection.attach_bounded_media_details(
                NotificationEvent("整理完成"), blocks,
            )

        self.assertLess(renderer.call_count, 20)
        self.assertTrue(event.lines)
        self.assertIn("未展开", event.lines[-1])

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_thread_updates_same_message_and_preserves_buttons(self, sender, editor) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=42)
        editor.return_value = TelegramSendResult(ok=True, message_id=42)
        initial = NotificationEvent(
            "待确认",
            actions=(NotificationAction("选择", "orgc:abc:0"),),
        )
        terminal = NotificationEvent("整理完成")

        first = publish_notification_thread(
            "confirmation:abc", initial,
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.ACTION,
            chat_id="100",
        )
        second = publish_notification_thread(
            "confirmation:abc", terminal,
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.RESULT,
            chat_id="100",
        )

        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()
        editor.assert_called_once()
        self.assertEqual(editor.call_args.kwargs["message_id"], 42)
        key = notification_thread_event_key(
            "confirmation:abc", topic=NotificationTopic.CONFIRMATION, chat_id="100",
        )
        row = get_notification(key)
        self.assertEqual(row["message_id"], 42)
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["delivered_revision"], 2)
        self.assertEqual(row["status"], "sent")

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_thread_snapshot_distinguishes_queued_and_delivered_revisions(
        self, sender, editor,
    ) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=43)
        editor.return_value = TelegramSendResult(ok=True, message_id=43)
        thread_key = "organize:snapshot"

        first = publish_notification_thread(
            thread_key,
            NotificationEvent("整理完成", fields=(("STRM", "已排队"),)),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
            deliver_now=False,
        )
        queued = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.ORGANIZE, chat_id="100",
        )
        self.assertTrue(first.queued)
        self.assertIsNotNone(queued)
        self.assertFalse(queued.current_revision_delivered)
        self.assertEqual(queued.revision, 1)
        self.assertEqual(queued.delivered_revision, 0)

        self.assertTrue(drain_telegram_notifications(event_key=first.event_key))
        delivered = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.ORGANIZE, chat_id="100",
        )
        self.assertTrue(delivered.current_revision_delivered)

        second = publish_notification_thread(
            thread_key,
            NotificationEvent("整理完成", fields=(("STRM", "完成"),)),
            topic=NotificationTopic.ORGANIZE,
            chat_id="100",
            deliver_now=False,
        )
        updating = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.ORGANIZE, chat_id="100",
        )
        self.assertTrue(second.queued)
        self.assertFalse(updating.current_revision_delivered)
        self.assertEqual(updating.revision, 2)
        self.assertEqual(updating.delivered_revision, 1)

        self.assertTrue(drain_telegram_notifications(event_key=second.event_key))
        final = get_notification_thread_snapshot(
            thread_key, topic=NotificationTopic.ORGANIZE, chat_id="100",
        )
        self.assertTrue(final.current_revision_delivered)
        self.assertEqual(final.revision, 2)
        self.assertEqual(final.delivered_revision, 2)

    def test_global_switch_rejects_proactive_event_without_creating_outbox(self) -> None:
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=False,
        ):
            outcome = publish_notification_event(
                "disabled-event",
                NotificationEvent("不应发送"),
                topic=NotificationTopic.SYSTEM,
                chat_id="100",
            )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.status, "disabled")
        with db.get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM telegram_notification_outbox"
            ).fetchone()[0]
        self.assertEqual(total, 0)

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_dispatch_rechecks_level_and_suppresses_downgraded_detail(self, sender) -> None:
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_policy.config.get",
            return_value="detailed",
        ):
            outcome = publish_notification_event(
                "detail-event",
                NotificationEvent("技术明细"),
                topic=NotificationTopic.STRM,
                importance=NotificationImportance.DETAIL,
                chat_id="100",
                deliver_now=False,
            )
        self.assertTrue(outcome.queued)
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_policy.config.get",
            return_value="standard",
        ):
            self.assertTrue(drain_telegram_notifications(event_key=outcome.event_key))
        sender.assert_not_called()
        self.assertEqual(get_notification(outcome.event_key)["status"], "suppressed")

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_retry_keeps_single_outbox_row(self, sender) -> None:
        sender.side_effect = [
            TelegramSendResult(ok=False, error="temporary", status_code=503),
            TelegramSendResult(ok=True, message_id=7),
        ]
        outcome = publish_notification_thread(
            "download:9", NotificationEvent("处理中"),
            topic=NotificationTopic.DOWNLOAD, chat_id="100",
            deliver_now=True,
        )
        self.assertTrue(outcome.accepted)
        self.assertTrue(outcome.queued)
        key = outcome.event_key
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET next_attempt_at=? WHERE event_key=?",
                (db.now(), key),
            )
        self.assertTrue(drain_telegram_notifications(event_key=key))
        row = get_notification(key)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["message_id"], 7)

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_confirmation_thread_keeps_token_and_receives_downstream_result(
        self, sender, editor,
    ) -> None:
        from app.modules.organize_confirmations import publish_confirmation_event
        from app.modules.scheduler import _publish_linked_notification_threads

        sender.return_value = TelegramSendResult(ok=True, message_id=73)
        editor.return_value = TelegramSendResult(ok=True, message_id=73)
        token = "confirm-token"
        db.create_organize_confirmation(
            token=token,
            fingerprint="fingerprint",
            chat_id="100",
            source_name="光鸭云盘",
            directory_path="/动漫",
            payload={"files": [{"file_id": "1"}]},
            expires_at="2099-01-01 00:00:00",
        )
        initial = NotificationEvent(
            "待确认",
            fields=(("媒体", "测试动画"),),
            actions=(NotificationAction("确认", f"orgc:{token}:0"),),
        )
        self.assertTrue(publish_confirmation_event(
            initial, chat_id="100", token=token,
        ))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET status='completed' WHERE token=?",
                (token,),
            )

        accepted = _publish_linked_notification_threads(
            {"notification_threads": [{
                "topic": "confirmation",
                "thread_key": f"confirmation:{token}",
                "token": token,
                "chat_id": "100",
                "topic_enabled": True,
            }]},
            strm_status="完成",
            media_refresh="Jellyfin 完成",
        )

        self.assertTrue(accepted)
        sender.assert_called_once()
        editor.assert_called_once()
        final_event = editor.call_args.args[0]
        self.assertIn(("STRM 状态", "完成 ✅"), final_event.fields)
        self.assertIn(("媒体库刷新", "Jellyfin 完成 🎯"), final_event.fields)
        self.assertEqual(final_event.actions, ())

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_download_pipeline_collapses_into_one_message(self, sender, editor) -> None:
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle

        sender.return_value = TelegramSendResult(ok=True, message_id=99)
        editor.return_value = TelegramSendResult(ok=True, message_id=99)
        request_id, _ = db.create_download_request(
            "tg-lifecycle", "magnet", title="测试剧", chat_id="100",
        )
        db.update_download_request(
            request_id, status="completed", qb_status="completed",
            organize_status="running", strm_status="pending",
        )
        first = publish_download_lifecycle(request_id, stats={
            "media_items": [{
                "title": "测试剧", "year": "2026", "media_type": "tv",
                "season": 1, "episode": 3,
            }],
        })
        db.update_download_request(
            request_id, organize_status="completed", strm_status="completed",
        )
        second = publish_download_lifecycle(
            request_id, media_refresh="Jellyfin 已刷新",
            verification_status="visible", verification_result="目标剧集已可见",
        )
        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()
        editor.assert_called_once()
        final_event = editor.call_args.args[0]
        self.assertIn("下载与入库完成", final_event.title)
        self.assertIn(("媒体库", "Jellyfin 已刷新"), final_event.fields)
        self.assertIn(("入库复核", "目标剧集已可见"), final_event.fields)
        self.assertTrue(any("S01E03" in line for line in final_event.lines))


class TelegramNotificationSettingsTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(state=SimpleNamespace(
                background_services_enabled=False,
                media_proxy_manager=None,
            )),
        )

    def test_settings_expose_stable_global_switch_and_level_control(self) -> None:
        template = Path("app/templates/settings.html").read_text(encoding="utf-8")
        script = Path("app/static/js/settings.js").read_text(encoding="utf-8")
        self.assertEqual(template.count('data-key="TG_NOTIFICATION_ENABLED"'), 1)
        self.assertEqual(template.count('data-key="TG_NOTIFICATION_LEVEL"'), 1)
        for value in ("essential", "standard", "detailed"):
            self.assertIn(f'<option value="{value}"', template)
        self.assertIn("TG_NOTIFICATION_ENABLED:'1'", script)
        self.assertIn("TG_NOTIFICATION_LEVEL:'standard'", script)

    def test_config_api_normalizes_switch_and_rejects_unknown_level(self) -> None:
        with patch(
            "app.routes.api.config.get", side_effect=lambda _key, default="": default,
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ):
            response = save_config(self._request(), {
                "TG_NOTIFICATION_ENABLED": "true",
                "TG_NOTIFICATION_LEVEL": "DETAILED",
            })
        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "TG_NOTIFICATION_ENABLED": "1",
            "TG_NOTIFICATION_LEVEL": "detailed",
        })

        with patch("app.routes.api.config.set_and_save") as rejected:
            response = save_config(
                self._request(), {"TG_NOTIFICATION_LEVEL": "verbose"}
            )
        self.assertEqual(response.status_code, 400)
        rejected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
