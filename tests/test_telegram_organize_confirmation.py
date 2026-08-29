"""Telegram 整理候选按钮、持久化与幂等契约。"""
from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db, notifier
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules import organize_confirmations as confirmation_module
from app.modules.organize_confirmations import (
    _dispatch_next_queued_confirmation,
    cancel_confirmation,
    confirmation_event,
    create_confirmation_actions,
    create_local_media_confirmation_actions,
    semantic_candidate_category,
    start_confirmation,
    stop_confirmation_dispatcher,
    wake_confirmation_dispatcher,
)
from app.modules.scraper import Candidate, MatchResult
from app.modules.telegram_notification_center import (
    NotificationPublishResult, notification_thread_event_key,
)
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import TelegramSendResult
from app.repositories.telegram_notifications import get_notification
from tests.support import IsolatedDatabaseTestCase


class _PositionScraper:
    @staticmethod
    def parse_source_position(_name, _path=""):
        return 2, 4


class ConfirmationGroupingTests(unittest.TestCase):
    def test_pending_episode_files_are_grouped_and_candidates_deduplicated(self):
        organizer = Organizer(client=SimpleNamespace(), scraper=_PositionScraper())
        candidate = Candidate(
            tmdb_id="105556", title="不要欺负我，长瀞同学", year="2021",
            score=0.88, media_type="tv", metadata={"genre_ids": [16]},
        )
        plans = []
        for index in (4, 5):
            match = MatchResult(
                tmdb_id="105556", title="不要欺负我，长瀞同学", year="2021",
                media_type="tv", confidence=0.88, candidates=[candidate],
                need_confirm=True, error="需要人工确认",
            )
            plans.append(OrganizePlan(
                file_id=f"file-{index}", original_name=f"Nagatoro - {index:02d}.mp4",
                original_path="长瀞同学 2nd Attack", original_parent_id="parent",
                size=100 + index, etag=f"etag-{index}", match=match,
                action="skip",
            ))

        groups = organizer._build_confirmation_groups(
            plans, {}, source_dir_id="source", source_name="下载"
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["files"]), 2)
        self.assertEqual(groups[0]["files"][0]["season"], 2)
        self.assertEqual(groups[0]["files"][0]["episode"], 4)
        self.assertEqual(len(groups[0]["candidates"]), 1)
        self.assertEqual(groups[0]["candidates"][0]["support"], 2)
        self.assertEqual(groups[0]["candidates"][0]["tmdb_id"], "105556")
        self.assertEqual(groups[0]["candidates"][0]["genre_ids"], [16])


class CandidateSemanticCategoryTests(unittest.TestCase):
    def test_tmdb_type_and_genres_render_user_facing_categories(self):
        cases = (
            ({"media_type": "tv", "genre_ids": [16]}, "剧集 · 动漫"),
            ({"media_type": "tv", "genre_ids": [99]}, "剧集 · 纪录片"),
            ({"media_type": "tv", "genre_ids": [10764]}, "剧集 · 综艺"),
            ({"media_type": "movie", "genre_ids": [16]}, "电影 · 动画"),
            ({"media_type": "movie", "genre_ids": [99]}, "电影 · 纪录片"),
            ({"media_type": "tv", "genre_ids": []}, "剧集"),
            ({"media_type": "movie", "genre_ids": []}, "电影"),
        )
        for candidate, expected in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(semantic_candidate_category(candidate), expected)


class ConfirmationPersistenceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        stop_confirmation_dispatcher()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")

    def tearDown(self):
        stop_confirmation_dispatcher()

    @staticmethod
    def _group():
        return {
            "source_dir_id": "source",
            "source_name": "下载",
            "directory": "长瀞同学 2nd Attack",
            "source_parent_id": "parent",
            "identity": "不要欺负我，长瀞同学",
            "reason": "需要人工确认",
            "files": [{
                "file_id": "file-4", "name": "Nagatoro - 04.mp4",
                "parent_id": "parent", "size": 104, "etag": "etag-4",
                "season": 2, "episode": 4,
            }],
            "companions": [],
            "candidates": [{
                "tmdb_id": "105556", "media_type": "tv",
                "title": "不要欺负我，长瀞同学", "year": "2021",
                "score": 0.88, "support": 1, "genre_ids": [16],
            }],
        }

    def test_actions_use_opaque_token_and_persist_private_payload(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        self.assertEqual(len(actions), 2)
        self.assertIn("不要欺负我", str(actions[0].label))
        self.assertIn("TMDB 105556", str(actions[0].label))
        self.assertRegex(actions[0].callback_data, r"^orgc:[A-Za-z0-9_-]+:0$")
        self.assertNotIn("105556", actions[0].callback_data)
        token = actions[0].callback_data.split(":")[1]
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "pending")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["files"][0]["file_id"], "file-4")
        self.assertEqual(payload["candidates"][0]["genre_ids"], [16])

    def test_callback_message_binding_is_persisted_and_chat_scoped(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="-100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.bind_organize_confirmation_message(
            token, chat_id="-100", message_id=77
        )
        payload = json.loads(db.get_organize_confirmation(token)["payload_json"])
        self.assertEqual(payload["_telegram_message_id"], 77)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            db.bind_organize_confirmation_message(
                token, chat_id="100", message_id=78
            )

    def test_candidate_click_is_atomic_and_same_selection_replays_task(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(start_operation=lambda *args, **kwargs: {
            "ok": True, "task_id": "task-1"
        })
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            result = start_confirmation(token, 0, chat_id="100")
            replay = start_confirmation(token, 0, chat_id="100")
        self.assertRegex(result["task_id"], r"^queue-\d{6}$")
        self.assertEqual(replay["task_id"], result["task_id"])
        self.assertTrue(replay["replayed"])
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["selected_index"], 0)

    def test_concurrent_same_candidate_clicks_are_idempotent(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(start_operation=lambda *args, **kwargs: {
            "ok": True, "task_id": "task-1"
        })
        original_claim = db.claim_organize_confirmation
        barrier = threading.Barrier(2)

        def synchronized_claim(*args, **kwargs):
            barrier.wait(timeout=2)
            return original_claim(*args, **kwargs)

        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations.db.claim_organize_confirmation",
            side_effect=synchronized_claim,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(start_confirmation, token, 0, chat_id="100")
                for _ in range(2)
            ]
            results = [future.result(timeout=3) for future in futures]

        self.assertEqual({result["task_id"] for result in results}, {
            results[0]["task_id"]
        })
        self.assertEqual(sum(bool(result.get("replayed")) for result in results), 1)
        self.assertEqual(db.get_organize_confirmation(token)["status"], "running")

    def test_different_candidate_cannot_replace_already_selected_choice(self):
        group = self._group()
        group["candidates"].append({
            "tmdb_id": "200", "media_type": "tv",
            "title": "其他候选", "year": "2022",
            "score": 0.55, "support": 1,
        })
        actions = create_confirmation_actions(
            group, OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(start_operation=lambda *args, **kwargs: {
            "ok": True, "task_id": "task-1"
        })
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            start_confirmation(token, 0, chat_id="100")
            with self.assertRaisesRegex(ValueError, "已选择其他候选"):
                start_confirmation(token, 1, chat_id="100")
        self.assertEqual(db.get_organize_confirmation(token)["selected_index"], 0)

    def test_busy_manager_keeps_selection_in_persistent_queue(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(start_operation=lambda *args, **kwargs: {
            "ok": False, "error": "网盘整理任务正在运行"
        })
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations.wake_confirmation_dispatcher"
        ) as dispatcher:
            result = start_confirmation(token, 0, chat_id="100")
        row = db.get_organize_confirmation(token)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["selected_index"], 0)
        dispatcher.assert_called_once_with()

    def test_new_click_does_not_bypass_older_queued_confirmation(self):
        older_group = self._group()
        older_group["directory"] = "先点击但仍在排队"
        older_actions = create_confirmation_actions(
            older_group, OrganizeRules(), source_name="下载", chat_id="100"
        )
        older_token = older_actions[0].callback_data.split(":")[1]
        db.claim_organize_confirmation(
            older_token, chat_id="100", selected_index=0
        )

        newer_group = self._group()
        newer_group["directory"] = "后点击的媒体"
        newer_actions = create_confirmation_actions(
            newer_group, OrganizeRules(), source_name="下载", chat_id="100"
        )
        newer_token = newer_actions[0].callback_data.split(":")[1]
        executed = []
        manager = SimpleNamespace(
            start_operation=lambda _name, reference, _callback: (
                executed.append(reference) or {"ok": True, "task_id": "task-1"}
            )
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations.wake_confirmation_dispatcher"
        ):
            result = start_confirmation(newer_token, 0, chat_id="100")

        self.assertEqual(executed, ["先点击但仍在排队"])
        self.assertEqual(result["status"], "queued")
        self.assertEqual(db.get_organize_confirmation(older_token)["status"], "running")
        self.assertEqual(db.get_organize_confirmation(newer_token)["status"], "queued")

    def test_queue_position_counts_current_running_confirmation(self):
        running_group = self._group()
        running_group["directory"] = "正在整理的媒体"
        running_actions = create_confirmation_actions(
            running_group, OrganizeRules(), source_name="下载", chat_id="100"
        )
        running_token = running_actions[0].callback_data.split(":")[1]
        db.claim_organize_confirmation(
            running_token, chat_id="100", selected_index=0
        )
        db.update_organize_confirmation(running_token, status="running")

        queued_group = self._group()
        queued_group["directory"] = "稍后整理的媒体"
        queued_actions = create_confirmation_actions(
            queued_group, OrganizeRules(), source_name="下载", chat_id="100"
        )
        queued_token = queued_actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(start_operation=lambda *args, **kwargs: {
            "ok": False, "error": "网盘整理任务正在运行"
        })
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations.wake_confirmation_dispatcher"
        ):
            result = start_confirmation(queued_token, 0, chat_id="100")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["queue_position"], 1)

    def test_cancel_never_enters_dispatchable_queue(self):
        log_id = db.add_organize_log(
            "guangya", "长瀞同学 2nd Attack", "", "file-4", "manual", "",
            original_parent_id="parent", original_name="Nagatoro - 04.mp4",
            current_parent_id="parent", current_name="Nagatoro - 04.mp4",
            media_type="tv", title="不要欺负我，长瀞同学",
            error="需要人工确认", legacy_incomplete=False,
        )
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]

        result = cancel_confirmation(token, chat_id="100")

        self.assertTrue(result["cancelled"])
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["queued_at"])
        self.assertIsNone(db.get_next_queued_organize_confirmation())
        log = db.get_organize_log(log_id)
        self.assertEqual(log["status"], "skipped")
        self.assertEqual(log["error"], "用户选择暂不处理")

    def test_cancel_persists_terminal_receipt_when_telegram_is_unavailable(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]

        with patch(
            "app.modules.telegram_notification_center.edit_event_result",
            return_value=TelegramSendResult(
                ok=False, error="temporarily unavailable", status_code=503,
            ),
        ) as edit_mock:
            result = cancel_confirmation(token, chat_id="100", message_id=77)

        self.assertTrue(result["cancelled"])
        self.assertEqual(db.get_organize_confirmation(token)["status"], "cancelled")
        key = notification_thread_event_key(
            f"confirmation:{token}",
            topic=NotificationTopic.CONFIRMATION,
            chat_id="100",
        )
        delivery = get_notification(key)
        self.assertEqual(delivery["status"], "retry_wait")
        self.assertEqual(delivery["message_id"], 77)
        self.assertIn("已暂不处理", str(delivery["event_json"]))
        edit_mock.assert_called_once()

    def test_dispatcher_wakeup_stays_disabled_after_stop(self):
        stop_confirmation_dispatcher()
        self.assertFalse(wake_confirmation_dispatcher())

    def test_dispatch_loop_rechecks_stop_before_database_query(self):
        with patch.object(
            confirmation_module._dispatch_stop,
            "is_set",
            side_effect=[False, True],
        ), patch.object(
            confirmation_module.db,
            "get_next_queued_organize_confirmation",
        ) as get_next:
            confirmation_module._confirmation_dispatch_loop()

        get_next.assert_not_called()

    def test_cancel_marks_record_and_rejects_later_candidate_click(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        result = cancel_confirmation(token, chat_id="100")
        self.assertTrue(result["cancelled"])
        self.assertEqual(db.get_organize_confirmation(token)["status"], "cancelled")
        with self.assertRaisesRegex(ValueError, "已处理"):
            start_confirmation(token, 0, chat_id="100")


    def test_wrong_chat_is_rejected_without_claiming(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            start_confirmation(token, 0, chat_id="200")
        self.assertEqual(db.get_organize_confirmation(token)["status"], "pending")

    def test_expired_claim_persists_expired_status(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        expired_at = (datetime.now() - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET expires_at=? WHERE token=?",
                (expired_at, token),
            )
        with self.assertRaisesRegex(ValueError, "已过期"):
            start_confirmation(token, 0, chat_id="100")
        self.assertEqual(db.get_organize_confirmation(token)["status"], "expired")

    def test_manager_exception_keeps_claim_queued_for_later_dispatch(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        manager = SimpleNamespace(
            start_operation=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("线程启动失败")
            )
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations.wake_confirmation_dispatcher"
        ) as dispatcher:
            result = start_confirmation(token, 0, chat_id="100")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(db.get_organize_confirmation(token)["status"], "queued")
        dispatcher.assert_called_once_with()

    def test_corrupted_queued_payload_fails_with_persisted_terminal_receipt(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.claim_organize_confirmation(token, chat_id="100", selected_index=0)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET payload_json='{' WHERE token=?",
                (token,),
            )

        published = []
        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                published.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            result = _dispatch_next_queued_confirmation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["error"], "确认任务数据损坏，请重新执行整理")
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], result["error"])
        self.assertIsNone(db.get_organize_confirmation_delivery(token))
        event = published[-1]
        self.assertIn("确认任务数据损坏", event.footer)
        self.assertEqual(event.actions, ())

    def test_transient_worker_failure_releases_and_sends_retry_button(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "task-1"}
            )
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            start_confirmation(token, 0, chat_id="100")
        published = []
        with patch(
            "app.modules.organize_confirmations.OrganizeRules.from_config",
            side_effect=RuntimeError("TMDB 网络暂时不可用"),
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                published.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "TMDB 网络"):
                callbacks[0]()
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "pending")
        event = published[-1]
        self.assertEqual(event.actions[0].callback_data, f"orgc:{token}:0")
        self.assertIn("重新尝试", str(event.actions[0].label))

    def test_init_marks_interrupted_confirmation_failed(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET status='running' WHERE token=?",
                (token,),
            )
        db.init_db()
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "failed")
        self.assertIn("进程", row["error"])

    def test_init_queues_and_delivers_interrupted_confirmation_receipt(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.bind_organize_confirmation_message(
            token, chat_id="100", message_id=77
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET status='running' WHERE token=?",
                (token,),
            )

        db.init_db()

        delivery = db.get_organize_confirmation_delivery(token)
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery["status"], "pending")
        self.assertEqual(delivery["message_id"], 77)
        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as publish_mock:
            attempted = confirmation_module._dispatch_due_confirmation_delivery(token)

        self.assertTrue(attempted)
        publish_mock.assert_called_once()
        event = publish_mock.call_args.args[0]
        self.assertIn("已中断", str(event.title))
        self.assertEqual(publish_mock.call_args.kwargs["token"], token)
        self.assertEqual(publish_mock.call_args.kwargs["message_id"], 77)
        self.assertEqual(
            db.get_organize_confirmation_delivery(token)["status"], "sent"
        )

    def test_terminal_receipt_retries_persistently_without_duplicate_after_sent(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.bind_organize_confirmation_message(
            token, chat_id="100", message_id=77
        )
        db.claim_organize_confirmation(token, chat_id="100", selected_index=0)
        self.assertIsNotNone(db.claim_queued_organize_confirmation(token))
        event = notifier.NotificationEvent(
            "✅ 人工确认整理完成", footer="已完成", layout="relaxed"
        )
        db.complete_organize_confirmation_with_delivery(
            token,
            result_json='{"moved":1}',
            event_json=confirmation_module._serialize_notification_event(event),
            chat_id="100",
            message_id=77,
        )

        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=False,
        ) as publish_mock:
            attempted = confirmation_module._dispatch_due_confirmation_delivery(token)
        self.assertTrue(attempted)
        publish_mock.assert_called_once()
        delivery = db.get_organize_confirmation_delivery(token)
        self.assertEqual(delivery["status"], "retry_wait")
        self.assertEqual(delivery["attempts"], 1)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmation_delivery_outbox SET next_attempt_at=? "
                "WHERE confirmation_token=?",
                (db.now(), token),
            )
        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as retry_publish:
            self.assertTrue(
                confirmation_module._dispatch_due_confirmation_delivery(token)
            )
            self.assertFalse(
                confirmation_module._dispatch_due_confirmation_delivery(token)
            )
        retry_publish.assert_called_once()
        self.assertEqual(
            db.get_organize_confirmation_delivery(token)["status"], "sent"
        )

    def test_interrupted_confirmation_cannot_be_overwritten_by_late_worker(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.bind_organize_confirmation_message(
            token, chat_id="100", message_id=77
        )
        db.claim_organize_confirmation(token, chat_id="100", selected_index=0)
        db.claim_queued_organize_confirmation(token)

        db.init_db()
        interrupted_delivery = db.get_organize_confirmation_delivery(token)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            db.complete_organize_confirmation_with_delivery(
                token,
                result_json='{"moved":1}',
                event_json=confirmation_module._serialize_notification_event(
                    notifier.NotificationEvent("✅ 迟到成功")
                ),
                chat_id="100",
                message_id=77,
            )

        row = db.get_organize_confirmation(token)
        delivery = db.get_organize_confirmation_delivery(token)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(delivery["id"], interrupted_delivery["id"])
        self.assertIn("已中断", str(delivery["event_json"]))
        self.assertNotIn("迟到成功", str(delivery["event_json"]))

    def test_init_preserves_queued_confirmation_for_restart_resume(self):
        actions = create_confirmation_actions(
            self._group(), OrganizeRules(), source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.claim_organize_confirmation(token, chat_id="100", selected_index=0)

        db.init_db()

        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["selected_index"], 0)

    def test_queued_confirmations_dispatch_in_click_order(self):
        tokens = []
        for name in ("先出现的消息", "后出现但先点击的消息"):
            group = self._group()
            group["directory"] = name
            actions = create_confirmation_actions(
                group, OrganizeRules(), source_name="下载", chat_id="100"
            )
            tokens.append(actions[0].callback_data.split(":")[1])

        db.claim_organize_confirmation(tokens[1], chat_id="100", selected_index=0)
        db.claim_organize_confirmation(tokens[0], chat_id="100", selected_index=0)
        executed = []

        def start_operation(_name, reference, callback):
            executed.append(reference)
            callback()
            return {"ok": True, "task_id": f"task-{len(executed)}"}

        def complete(token, *_args, **_kwargs):
            db.update_organize_confirmation(
                token, status="completed", completed_at=db.now()
            )
            return {"ok": True}

        manager = SimpleNamespace(start_operation=start_operation)
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.organize_confirmations._execute_confirmation",
            side_effect=complete,
        ):
            first = _dispatch_next_queued_confirmation()
            second = _dispatch_next_queued_confirmation()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(executed, ["后出现但先点击的消息", "先出现的消息"])
        self.assertEqual(
            [db.get_organize_confirmation(token)["status"] for token in tokens],
            ["completed", "completed"],
        )


    def test_worker_success_runs_preview_execute_and_post_actions(self):
        rules = OrganizeRules()
        group = self._group()
        group["candidates"].append({
            "tmdb_id": "999999", "media_type": "tv",
            "title": "错误候选", "year": "2020", "score": 0.7,
            "support": 1,
        })
        actions = create_confirmation_actions(
            group, rules, source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        db.bind_organize_confirmation_message(token, chat_id="100", message_id=77)
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "task-1"}
            )
        )
        calls = []
        post_action_kwargs = []

        class FakeOrganizer:
            def __init__(self, **_kwargs):
                pass

            def _validate_target_outside_source(self, *_args):
                calls.append("validated")

            def organize(self, *_args, **kwargs):
                calls.append(("organize", kwargs["dry_run"]))
                plans = [SimpleNamespace(file_id="file-4")]
                return plans, ({"moved": 1} if not kwargs["dry_run"] else {})

            @staticmethod
            def trigger_post_actions(*_args, **kwargs):
                calls.append("post_actions")
                post_action_kwargs.append(dict(kwargs))

        current_file = GuangYaFile(
            "file-4", "Nagatoro - 04.mp4", False, size=104,
            etag="etag-4", parent_id="parent",
        )
        fake_client = SimpleNamespace(file_info=lambda _file_id: current_file)
        fake_match = MatchResult(
            tmdb_id="105556", title="不要欺负我，长瀞同学", year="2021",
            media_type="tv", confidence=1.0, need_confirm=False,
        )
        confirmation_calls = []
        fake_scraper = SimpleNamespace(
            get_detail_with_credits=lambda *_args: {"id": 105556},
            match_from_tmdb=lambda *_args: fake_match,
            confirm=lambda *args, **kwargs: confirmation_calls.append((args, kwargs)),
        )
        fake_scoped = SimpleNamespace(begin_source_scan=lambda: calls.append("scan"))
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            start_confirmation(token, 0, chat_id="100")
        published = []
        with patch(
            "app.modules.organize_confirmations.OrganizeRules.from_config",
            return_value=rules,
        ), patch(
            "app.modules.organize_confirmations.GuangYaClient",
            return_value=fake_client,
        ), patch(
            "app.modules.organize_confirmations.TMDBScraper",
            return_value=fake_scraper,
        ), patch(
            "app.modules.organize_confirmations.ScopedGuangYaClient",
            return_value=fake_scoped,
        ), patch(
            "app.modules.organize_confirmations.FixedMatchScraper",
            return_value=SimpleNamespace(),
        ), patch(
            "app.modules.organize_confirmations.Organizer", FakeOrganizer,
        ), patch(
            "app.modules.organize_confirmations.get", return_value="8",
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                published.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            result = callbacks[0]()
        self.assertEqual(result["stats"], {"moved": 1})
        self.assertEqual(
            [item for item in calls if isinstance(item, tuple)],
            [("organize", True), ("organize", False)],
        )
        self.assertIn("post_actions", calls)
        self.assertEqual(len(confirmation_calls), 1)
        args, kwargs = confirmation_calls[0]
        self.assertEqual(args[:5], (
            "Nagatoro - 04.mp4", "105556", "不要欺负我，长瀞同学", "2021", "tv"
        ))
        self.assertEqual(kwargs["parent_path"], "长瀞同学 2nd Attack")
        self.assertEqual(kwargs["rejected_tmdb_ids"], ["999999"])
        self.assertFalse(post_action_kwargs[0]["notify_result"])
        self.assertEqual(post_action_kwargs[0]["strm_debounce_seconds"], 8)
        thread_ref = post_action_kwargs[0]["notification_threads"][0]
        self.assertEqual(thread_ref["topic"], "confirmation")
        self.assertEqual(thread_ref["token"], token)
        self.assertEqual(db.get_organize_confirmation(token)["status"], "completed")
        self.assertIn("人工确认整理完成", published[-1].title)

    def test_snapshot_conflict_is_terminal_and_requires_new_scan(self):
        rules = OrganizeRules()
        actions = create_confirmation_actions(
            self._group(), rules, source_name="下载", chat_id="100"
        )
        token = actions[0].callback_data.split(":")[1]
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "task-1"}
            )
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            start_confirmation(token, 0, chat_id="100")
        missing_client = SimpleNamespace(file_info=lambda _file_id: None)
        published = []
        with patch(
            "app.modules.organize_confirmations.OrganizeRules.from_config",
            return_value=rules,
        ), patch(
            "app.modules.organize_confirmations.GuangYaClient",
            return_value=missing_client,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                published.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            with self.assertRaisesRegex(Exception, "已不存在"):
                callbacks[0]()
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "failed")
        event = published[-1]
        self.assertIn("重新执行整理生成新候选", event.footer)
        self.assertEqual(event.actions, ())

    def test_confirmation_event_uses_relaxed_episode_summary_layout(self):
        event = confirmation_event(
            "⚠️ 发现需要确认的媒体",
            {
                "媒体": "不要欺负我，长瀞同学",
                "剧集": "第 2 季 · E04 · 共 1 个视频",
                "来源": "下载/长瀞同学 2nd Attack",
            },
            self._group(),
            OrganizeRules(),
            source_name="下载",
            chat_id="100",
        )

        rendered = notifier.render_event(event)
        self.assertEqual(event.layout, "relaxed")
        self.assertIn("<b>剧集</b>  第 2 季 · E04 · 共 1 个视频", rendered)
        self.assertIn("需要人工确认\n\n请选择下方候选继续整理。", rendered)
        self.assertIn("TMDB 105556 · 剧集 · 动漫 · 匹配 88% · 支持 1 个文件", rendered)
        self.assertIn("不要欺负我，长瀞同学 (2021)", rendered)
        self.assertIn("TMDB 105556", str(event.actions[0].label))
        self.assertTrue(str(event.actions[0].label).startswith("1  "))

    def test_task_notification_groups_episodes_and_keeps_confirmation_card_separate(self):
        group = self._group()
        stats = {
            "task_id": "task-grouped", "total": 3, "moved": 2,
            "need_confirm": 1, "skipped": 0, "failed": 0,
            "metadata_moved": 0,
            "media_items": [
                {"tmdb_id": "105556", "media_type": "tv",
                 "title": "不要欺负我，长瀞同学", "year": "2021",
                 "season": 2, "episode": 1},
                {"tmdb_id": "105556", "media_type": "tv",
                 "title": "不要欺负我，长瀞同学", "year": "2021",
                 "season": 2, "episode": 9},
            ],
            "confirmation_groups": [group],
        }
        summaries = []
        confirmations = []
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: summaries.append(event) or True,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                confirmations.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            self.assertTrue(Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="2 个源目录", chat_id="100"
            ))

        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(confirmations), 1)
        summary = summaries[0]
        self.assertTrue(any("S02" in line and "E01" in line and "E09" in line for line in summary.lines))
        self.assertIn(("人工确认", "1 个文件 / 1 组按钮卡"), summary.fields)
        event = confirmations[0]
        self.assertEqual(event.title, "⚠️ 待确认媒体 1/1")
        self.assertIn(("剧集", "第 2 季 · E04 · 共 1 个视频"), event.fields)
        self.assertTrue(event.actions)

    def test_directory_notification_creates_clickable_candidate_event(self):
        group = self._group()
        stats = {
            "task_id": "directory-confirmation",
            "directories": {group["directory"]: {
                "total": 1, "moved": 0, "metadata_moved": 0,
                "skipped": 0, "need_confirm": 1, "failed": 0,
            }},
            "confirmation_groups": [group], "media_items": [],
        }
        summaries = []
        confirmations = []
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: summaries.append(event) or True,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                confirmations.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            Organizer.notify_directory_results(
                stats, OrganizeRules(), source_name="下载", chat_id="100"
            )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(confirmations), 1)
        event = confirmations[0]
        self.assertEqual(event.title, "⚠️ 待确认媒体 1/1")
        self.assertEqual(event.layout, "relaxed")
        self.assertIn(("剧集", "第 2 季 · E04 · 共 1 个视频"), event.fields)
        self.assertEqual(len(event.actions), 2)
        token = event.actions[0].callback_data.split(":")[1]
        self.assertEqual(db.get_organize_confirmation(token)["status"], "pending")


class LocalMediaConfirmationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        stop_confirmation_dispatcher()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
            conn.execute("DELETE FROM download_requests")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
        self.source_id = db.create_local_media_source(
            name="本地下载",
            qb_profile="",
            qb_path_prefix="",
            local_root="/downloads",
            owner="admin",
        )
        self.task_id = db.create_local_media_task(
            self.source_id,
            "",
            "/downloads/Movie.2026.mkv",
            owner="admin",
            trigger="scan",
        )
        db.update_local_media_task(
            self.task_id,
            owner="admin",
            status="requires_manual",
            snapshot_digest="digest-1",
            error="匹配置信度不足",
        )

    def tearDown(self):
        stop_confirmation_dispatcher()

    def _context(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        preview = {
            "reason": "匹配置信度不足",
            "snapshot_digest": "digest-1",
            "rules_snapshot": "{}",
            "files": [{"name": "Movie.2026.mkv"}],
            "candidate": {
                "tmdb_id": "101",
                "media_type": "movie",
                "title": "电影甲",
                "year": "2026",
                "confidence": 0.82,
                "provider": "tmdb",
            },
            "candidates": [
                {
                    "tmdb_id": "101",
                    "media_type": "movie",
                    "title": "电影甲",
                    "year": "2026",
                    "score": 0.82,
                    "provider": "tmdb",
                },
                {
                    "tmdb_id": "202",
                    "media_type": "movie",
                    "title": "电影乙",
                    "year": "2025",
                    "score": 0.76,
                    "provider": "tmdb",
                },
            ],
        }
        return task, source, preview

    def _actions(self, *, chat_id="100"):
        task, source, preview = self._context()
        return create_local_media_confirmation_actions(
            task, source, preview, owner="admin", chat_id=chat_id
        )

    def test_local_actions_reuse_orgc_protocol_and_persist_kind(self):
        actions = self._actions()

        self.assertEqual(len(actions), 3)
        self.assertRegex(actions[0].callback_data, r"^orgc:[A-Za-z0-9_-]+:0$")
        token = actions[0].callback_data.split(":")[1]
        payload = json.loads(db.get_organize_confirmation(token)["payload_json"])
        self.assertEqual(payload["kind"], "local_media")
        self.assertEqual(payload["local_task_id"], self.task_id)
        self.assertEqual(payload["snapshot_digest"], "digest-1")
        self.assertNotIn("/downloads/", json.dumps(payload, ensure_ascii=False))

    def test_payload_without_kind_still_dispatches_to_guangya_executor(self):
        candidate = {"tmdb_id": "1", "media_type": "movie"}
        with patch.object(
            confirmation_module,
            "_execute_guangya_confirmation",
            return_value={"path": "guangya"},
        ) as guangya, patch.object(
            confirmation_module,
            "_execute_local_media_confirmation",
        ) as local:
            result = confirmation_module._execute_confirmation(
                "token", {"version": 1}, candidate, selected_index=0, chat_id="100"
            )

        self.assertEqual(result, {"path": "guangya"})
        guangya.assert_called_once()
        local.assert_not_called()

    def test_local_candidate_executes_through_shared_queue(self):
        request_id, _created = db.create_download_request(
            "local-confirmation-status", "magnet", title="Movie.2026"
        )
        self.assertTrue(db.link_download_request_to_local_media_task(
            request_id, self.task_id, "/downloads/Movie.2026.mkv"
        ))
        db.update_download_request_for_local_media_task(
            self.task_id, "requires_manual", error="匹配置信度不足"
        )
        actions = self._actions()
        token = actions[0].callback_data.split(":")[1]
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "worker-1"}
            )
        )

        def execute(owner, task_id, qb_client=None):
            self.assertEqual(owner, "admin")
            self.assertEqual(task_id, self.task_id)
            self.assertIsNone(qb_client)
            db.update_local_media_task(
                task_id, owner=owner, status="completed", completed_at=db.now()
            )
            return {
                "status": "completed", "task_id": task_id,
                "moved": ["/library/Movie.2026.mkv"],
                "deleted_junk": [], "warnings": [],
                "media_refresh_status": "completed",
            }

        scheduler = SimpleNamespace(
            service=SimpleNamespace(
                inspect_source=lambda *_args: {"digest": "digest-1"},
                execute_task=execute,
            ),
            qb_factory=lambda: self.fail("无 qB 任务时不应创建客户端"),
        )
        published = []
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler",
            return_value=scheduler,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                published.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            queued = start_confirmation(token, 0, chat_id="100")
            worker_result = callbacks[0]()

        self.assertEqual(queued["status"], "running")
        self.assertEqual(worker_result["local_task_id"], self.task_id)
        self.assertEqual(db.get_local_media_task(self.task_id, owner="admin").status, "completed")
        request = db.get_download_request(request_id)
        self.assertEqual(request["local_import_status"], "completed")
        self.assertEqual(request["local_import_error"], "")
        self.assertTrue(request["local_import_completed_at"])
        self.assertEqual(db.get_organize_confirmation(token)["status"], "completed")
        self.assertIn("本地媒体确认整理完成", published[-1].title)
        self.assertIn(("媒体库", "已刷新"), published[-1].fields)

    def test_local_confirmation_failure_settles_linked_download_request(self):
        request_id, _created = db.create_download_request(
            "local-confirmation-failure", "magnet", title="Movie.2026"
        )
        self.assertTrue(db.link_download_request_to_local_media_task(
            request_id, self.task_id, "/downloads/Movie.2026.mkv"
        ))
        db.update_download_request_for_local_media_task(
            self.task_id, "requires_manual", error="匹配置信度不足"
        )
        actions = self._actions()
        token = actions[0].callback_data.split(":")[1]
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "worker-1"}
            )
        )
        scheduler = SimpleNamespace(
            service=SimpleNamespace(
                inspect_source=lambda *_args: {"digest": "digest-1"},
                execute_task=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("归档写入失败")
                ),
            ),
            qb_factory=lambda: self.fail("无 qB 任务时不应创建客户端"),
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler",
            return_value=scheduler,
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event", return_value=True
        ):
            start_confirmation(token, 0, chat_id="100")
            with self.assertRaisesRegex(RuntimeError, "归档写入失败"):
                callbacks[0]()

        task = db.get_local_media_task(self.task_id, owner="admin")
        request = db.get_download_request(request_id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(request["local_import_status"], "failed")
        self.assertIn("归档写入失败", request["local_import_error"] or "")
        self.assertEqual(db.get_organize_confirmation(token)["status"], "failed")

    def test_changed_local_snapshot_fails_without_claiming_task(self):
        actions = self._actions()
        token = actions[0].callback_data.split(":")[1]
        callbacks = []
        manager = SimpleNamespace(
            start_operation=lambda _name, _reference, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "worker-1"}
            )
        )
        scheduler = SimpleNamespace(
            service=SimpleNamespace(
                inspect_source=lambda *_args: {"digest": "changed"},
                execute_task=lambda *_args, **_kwargs: self.fail("快照变化后不得执行"),
            ),
            qb_factory=lambda: self.fail("不得创建 qB 客户端"),
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler",
            return_value=scheduler,
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event", return_value=True
        ):
            start_confirmation(token, 0, chat_id="100")
            with self.assertRaisesRegex(ValueError, "源文件在通知后发生变化"):
                callbacks[0]()

        self.assertEqual(
            db.get_local_media_task(self.task_id, owner="admin").status,
            "requires_manual",
        )
        self.assertEqual(db.get_organize_confirmation(token)["status"], "failed")

    def test_cancel_local_confirmation_leaves_task_for_web_review(self):
        actions = self._actions()
        token = actions[0].callback_data.split(":")[1]

        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event", return_value=True
        ):
            cancel_confirmation(token, chat_id="100")

        self.assertEqual(db.get_organize_confirmation(token)["status"], "cancelled")
        self.assertEqual(
            db.get_local_media_task(self.task_id, owner="admin").status,
            "requires_manual",
        )


class NotificationActionTests(unittest.TestCase):
    class TelegramPhotoRejected(RuntimeError):
        result_json = {
            "error_code": 400,
            "description": "Bad Request: failed to get HTTP URL content",
        }

    class FakeBot:
        def __init__(self):
            self.messages = []
            self.photos = []
            self.fail_photo = False

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

        def send_photo(self, chat_id, image_url, **kwargs):
            self.photos.append((chat_id, image_url, kwargs))
            if self.fail_photo:
                raise NotificationActionTests.TelegramPhotoRejected("图片不可用")

    def setUp(self):
        notifier.reset()

    def tearDown(self):
        notifier.reset()

    def test_event_action_is_attached_to_last_message(self):
        bot = self.FakeBot()
        notifier._bot = bot
        notifier._chat_id = "100"
        notifier._initialized = True
        event = notifier.NotificationEvent(
            "待确认",
            actions=(notifier.NotificationAction("候选 A", "orgc:token:0"),),
        )
        self.assertTrue(notifier.send_event(event))
        markup = bot.messages[-1][2]["reply_markup"]
        button = markup.keyboard[0][0]
        self.assertEqual(button.text, "候选 A")
        self.assertEqual(button.callback_data, "orgc:token:0")


    def test_photo_fallback_keeps_action_markup(self):
        bot = self.FakeBot()
        bot.fail_photo = True
        notifier._bot = bot
        notifier._chat_id = "100"
        notifier._initialized = True
        event = notifier.NotificationEvent(
            "待确认",
            image_url="https://example.invalid/poster.jpg",
            actions=(notifier.NotificationAction("候选 A", "orgc:token:0"),),
        )
        self.assertTrue(notifier.send_event(event))
        markup = bot.messages[-1][2]["reply_markup"]
        self.assertEqual(markup.keyboard[0][0].callback_data, "orgc:token:0")

    def test_invalid_actions_do_not_create_empty_markup(self):
        event = notifier.NotificationEvent(
            "待确认", actions=(notifier.NotificationAction("", ""),)
        )
        self.assertIsNone(notifier._event_markup(event))


    def test_callback_data_over_telegram_limit_is_ignored(self):
        event = notifier.NotificationEvent(
            "待确认",
            actions=(notifier.NotificationAction("过长", "x" * 65),),
        )
        self.assertIsNone(notifier._event_markup(event))


class TelegramCallbackTests(unittest.TestCase):
    class FakeBot:
        def __init__(self):
            self.answers = []
            self.edits = []

        def answer_callback_query(self, *args, **kwargs):
            self.answers.append((args, kwargs))

        def edit_message_text(self, *args, **kwargs):
            self.edits.append((args, kwargs))

    def test_callback_edits_message_and_removes_buttons(self):
        from app.bot.handlers import _handle_organize_confirmation_callback

        bot = self.FakeBot()
        call = SimpleNamespace(
            id="callback", data="orgc:token:0",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=100), message_id=77,
            ),
        )
        result = {
            "task_id": "task-1", "file_count": 11,
            "status": "queued", "queue_position": 2,
            "scope_summary": "第 2 季 · E13 · 共 11 个视频",
            "source_name": "1/待确认目录",
            "media_type": "tv",
            "candidate": {
                "tmdb_id": "105556", "title": "不要欺负我，长瀞同学",
                "year": "2021", "media_type": "tv",
            },
        }
        with patch(
            "app.bot.handlers.db.bind_organize_confirmation_message"
        ) as bind_mock, patch(
            "app.modules.organize_confirmations.start_confirmation", return_value=result
        ):
            _handle_organize_confirmation_callback(bot, call)
        bind_mock.assert_called_once_with("token", chat_id="100", message_id=77)
        self.assertEqual(bot.answers[0][0][1], "已加入整理队列")
        edited = bot.edits[0][0][0]
        self.assertIn("已选择：不要欺负我，长瀞同学 (2021)", edited)
        self.assertIn("<b>类型</b>  剧集 · 第 2 季 · E13 · 共 11 个视频", edited)
        self.assertIn("<b>文件</b>  11 个视频", edited)
        self.assertIn("<b>来源</b>  1/待确认目录", edited)
        self.assertIn("等待执行 · 前方 2 项", edited)
        self.assertIn("继续选择其他待确认媒体", edited)
        self.assertIsNone(bot.edits[0][1]["reply_markup"])


    def test_cancel_callback_uses_persisted_receipt_instead_of_direct_edit(self):
        from app.bot.handlers import _handle_organize_confirmation_callback

        bot = self.FakeBot()
        call = SimpleNamespace(
            id="callback", data="orgc:token:cancel",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=100), message_id=77,
            ),
        )
        with patch(
            "app.modules.organize_confirmations.cancel_confirmation",
            return_value={"cancelled": True, "directory": "/待确认"},
        ) as cancel_mock:
            _handle_organize_confirmation_callback(bot, call)

        cancel_mock.assert_called_once_with(
            "token", chat_id="100", message_id=77
        )
        self.assertEqual(bot.answers[0][0][1], "已保留待确认文件")
        self.assertEqual(bot.edits, [])

    def test_authorized_group_callback_starts_bound_confirmation(self):
        from app.bot.handlers import _handle_organize_confirmation_callback

        bot = self.FakeBot()
        call = SimpleNamespace(
            id="callback", data="orgc:token:0",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"), message_id=77,
            ),
        )
        result = {
            "task_id": "task-1", "file_count": 1, "status": "running",
            "scope_summary": "E01 · 共 1 个视频", "source_name": "下载",
            "media_type": "tv",
            "candidate": {
                "tmdb_id": "105556", "title": "不要欺负我，长瀞同学",
                "year": "2021", "media_type": "tv",
            },
        }
        with patch(
            "app.bot.handlers.db.bind_organize_confirmation_message"
        ) as bind_mock, patch(
            "app.modules.organize_confirmations.start_confirmation", return_value=result
        ) as start_mock:
            _handle_organize_confirmation_callback(bot, call)
        bind_mock.assert_called_once_with("token", chat_id="-100", message_id=77)
        start_mock.assert_called_once_with("token", 0, chat_id="-100")
        self.assertEqual(bot.answers[0][0][1], "已开始确认整理")
        self.assertTrue(bot.edits)



if __name__ == "__main__":
    unittest.main()
