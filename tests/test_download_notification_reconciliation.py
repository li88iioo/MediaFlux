"""下载撤销/后端状态变化必须收口独立通知队列；不调用真实下载器或 Telegram。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.modules import telegram_notification_center as center
from app.modules.download_tracker import DownloadTracker
from app.modules.telegram_download_lifecycle import publish_download_lifecycle
from app.notifier import TelegramSendResult
from app.repositories.telegram_notifications import get_notification
from tests.support import isolated_test_database


class DownloadNotificationReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch('app.modules.telegram_notification_policy.notifications_enabled', return_value=True))
        self.enterContext(patch('app.modules.telegram_notification_policy.notification_level', return_value='standard'))
        stopped = center._dispatch_stop.is_set()
        center._dispatch_stop.clear()
        self.addCleanup(center._dispatch_stop.set if stopped else center._dispatch_stop.clear)
        self.sender = self.enterContext(patch.object(center, 'send_event_result', return_value=TelegramSendResult(ok=True, message_id=81)))
        self.editor = self.enterContext(patch.object(center, 'edit_event_result', return_value=TelegramSendResult(ok=True, message_id=81)))
        self.enterContext(patch.object(center, 'wake_telegram_notification_dispatcher'))

    def request(self, suffix='a', *, gy=''):
        request_id, _ = download_requests_repo.create_download_request('notify-reconcile-'+suffix, 'magnet', title='通知任务 '+suffix, chat_id='100')
        download_requests_repo.update_download_request(
            request_id, status='manual_review', qb_status='manual_review', qb_task_id=suffix*40,
            gy_status=gy, targets='both' if gy else 'qb',
            qb_task_missing_since='2000-01-01 00:00:00',
            notification_event_status='manual_review', notification_delivery_status='sent',
        )
        published = publish_download_lifecycle(request_id, deliver_now=False)
        self.assertTrue(published.accepted)
        get_notification_result = get_notification(published.event_key)
        assert get_notification_result is not None
        self.assertEqual(get_notification_result['status'], 'pending')
        return request_id, published.event_key

    def test_cancelled_qb_only_does_not_emit_queued_attention_after_restart(self):
        request_id, event_key = self.request()
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        db.init_db()
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_not_called()
        get_notification_result_2 = get_notification(event_key)
        assert get_notification_result_2 is not None
        self.assertEqual(get_notification_result_2['status'], 'suppressed')
        get_download_request_result = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result['status'], 'cancelled')

    def request_with_skipped_strm_receipt(self, request_key, *, chat_id=''):
        request_id, _ = download_requests_repo.create_download_request(
            request_key, 'share', title='同版本分享', chat_id=chat_id,
        )
        task_id = 'organize-' + request_key
        run_id = db.add_task_run('guangya_organize', 'download')
        receipt = {'task_id': task_id, 'stats': {'strm': {'ok': True, 'skipped': True, 'reason': '无变更'}}}
        organized_at = db.now()
        db.finish_task_run(run_id, 'success', json.dumps(receipt))
        download_requests_repo.update_download_request(
            request_id, targets='guangya', status='completed', gy_status='completed',
            organize_started=1, organize_status='completed', organize_run_id=run_id,
            organize_task_id=task_id, organize_finished_at=organized_at, strm_status='pending',
        )
        return request_id

    def test_restart_settles_skipped_strm_and_edits_existing_download_message_once(self):
        request_id = self.request_with_skipped_strm_receipt('share-same-version', chat_id='100')
        published = publish_download_lifecycle(request_id, media_refresh='未触发')
        self.assertTrue(published.accepted)
        self.assertEqual(self.sender.call_args.args[0].state, 'processing')

        db.init_db()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row['strm_status'], row['strm_error']), ('skipped', '无变更'))
        self.assertTrue(row['strm_finished_at'])
        self.assertIsNone(row['strm_run_id'])
        DownloadTracker()._update_request(row, [], [])
        center.drain_telegram_notifications(event_key=published.event_key)
        event = self.editor.call_args.args[0]
        self.assertEqual(event.state, 'completed')
        self.assertIn('已跳过', dict(event.fields)['STRM'])
        self.assertEqual(dict(event.fields)['媒体库'], '未触发')
        db.init_db()
        self.assertNotIn(request_id, [r['id'] for r in download_requests_repo.list_active_download_requests()])
        self.sender.assert_called_once()
        self.editor.assert_called_once()

    def test_restart_requeues_skipped_strm_notification_only_in_supplied_default_chat(self):
        default_chat, other_chat = '100', '200'
        matching = self.request_with_skipped_strm_receipt('legacy-share-default')
        nonmatching = self.request_with_skipped_strm_receipt('legacy-share-other')
        self.enterContext(patch('app.notifier._init'))
        old_outbox = {}
        for request_id, chat_id in ((matching, default_chat), (matching, other_chat), (nonmatching, other_chat)):
            with patch('app.notifier._chat_id', chat_id):
                published = publish_download_lifecycle(request_id)
            self.assertTrue(published.accepted)
            notification = get_notification(published.event_key)
            assert notification is not None
            self.assertEqual((notification['chat_id'], notification['status']), (chat_id, 'sent'))
            old_outbox[published.event_key] = notification

        with patch('app.config.get', side_effect=AssertionError('数据库恢复不得读取配置')) as read_config, patch(
            'app.config._ensure_loaded', side_effect=AssertionError('数据库恢复不得加载配置'),
        ) as load_config:
            db.init_db(default_notification_chat_id=default_chat)
        read_config.assert_not_called()
        load_config.assert_not_called()

        for request_id, delivery in ((matching, 'pending'), (nonmatching, '')):
            row = download_requests_repo.get_download_request(request_id)
            assert row is not None
            self.assertEqual((row['chat_id'], row['notification_payload_json']), ('', ''))
            self.assertEqual((row['strm_status'], row['notification_delivery_status']), ('skipped', delivery))

        active = download_requests_repo.list_active_download_requests()
        self.assertEqual([row['id'] for row in active], [matching])
        with patch('app.notifier._chat_id', default_chat):
            DownloadTracker()._update_request(active[0], [], [])
            center.drain_telegram_notifications()
        self.assertEqual(self.sender.call_count, len(old_outbox))
        self.editor.assert_called_once()
        self.assertEqual(self.editor.call_args.kwargs, {'chat_id': default_chat, 'message_id': 81})
        self.assertEqual(self.editor.call_args.args[0].state, 'completed')
        for event_key, notification in old_outbox.items():
            if notification['chat_id'] == other_chat:
                self.assertEqual(get_notification(event_key), notification)

    def test_old_interruption_is_repaired_only_for_its_unclaimed_skipped_run(self):
        from app.repositories.strm_request_ownership import INTERRUPTED_ERROR

        request_ids = {}
        for case in (
            'skipped', 'different_task', 'owned_work', 'new_generation', 'independent_failure',
            'started', 'manual_confirmation', 'invalid_receipt',
        ):
            request_id, _ = download_requests_repo.create_download_request(case, 'share')
            task_id = 'organize-' + case
            handoff = {'ok': True, 'skipped': True, 'reason': '未启用'} if case != 'started' else {'ok': True}
            receipt = {'task_id': task_id, 'stats': {'strm': handoff, 'need_confirm': int(case == 'manual_confirmation')}}
            run_id = db.add_task_run('guangya_organize', 'download')
            organized_at = db.now()
            db.finish_task_run(run_id, 'success', 'invalid' if case == 'invalid_receipt' else json.dumps(receipt))
            download_requests_repo.update_download_request(
                request_id, targets='guangya', status='completed', gy_status='completed',
                organize_status='completed', organize_started=1, organize_run_id=run_id, organize_finished_at=organized_at,
                organize_task_id='another-task' if case == 'different_task' else task_id,
                strm_status='failed', strm_error='独立失败' if case == 'independent_failure' else INTERRUPTED_ERROR,
            )
            if case == 'owned_work':
                with db.get_conn() as conn:
                    conn.execute(
                        "INSERT INTO strm_request_work(request_id,generation,organize_task_id,kind,work_key) "
                        "VALUES(?,0,?,'change','file')", (request_id, task_id),
                    )
            elif case == 'new_generation':
                with db.get_conn() as conn:
                    conn.execute("UPDATE download_requests SET strm_generation=1 WHERE id=?", (request_id,))
            request_ids[case] = request_id
        db.init_db()
        for case, request_id in request_ids.items():
            with self.subTest(case=case):
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual(row['strm_status'], 'skipped' if case == 'skipped' else 'failed')
                self.assertEqual(row['notification_delivery_status'], '')

    def test_other_backend_failure_is_preserved_but_old_qb_alert_is_not_sent(self):
        request_id, event_key = self.request(gy='failed')
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row['notification_delivery_status'], 'pending')
        self.assertIn(request_id, [value['id'] for value in download_requests_repo.list_active_download_requests()])
        get_notification_result_3 = get_notification(event_key)
        assert get_notification_result_3 is not None
        self.assertEqual(get_notification_result_3['status'], 'suppressed')
        DownloadTracker()._update_request(row, [], [])
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_called_once()
        event = self.sender.call_args.args[0]
        self.assertEqual(dict(event.fields)['下载'], 'qB 已停止跟踪 · 光鸭 失败')
        self.assertNotIn('发现 qB 缺失', dict(event.fields))
        get_download_request_result_2 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['gy_status'], 'failed')

    def test_cancel_is_exact_and_leaves_another_pending_request_notification(self):
        first, first_key = self.request('a')
        second, second_key = self.request('b')
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        center.drain_telegram_notifications()
        self.sender.assert_called_once()
        self.assertEqual(dict(self.sender.call_args.args[0].fields)['媒体'], '通知任务 b')
        get_notification_result_4 = get_notification(first_key)
        assert get_notification_result_4 is not None
        self.assertEqual(get_notification_result_4['status'], 'suppressed')
        get_notification_result_5 = get_notification(second_key)
        assert get_notification_result_5 is not None
        self.assertEqual(get_notification_result_5['status'], 'sent')
        get_download_request_result_3 = download_requests_repo.get_download_request(second)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3['qb_status'], 'manual_review')
        get_download_request_result_4 = download_requests_repo.get_download_request(first)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4['qb_status'], 'cancelled')

    def test_other_backend_still_running_gets_latest_processing_projection(self):
        request_id, event_key = self.request(gy='downloading')
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        tracker = DownloadTracker()
        with patch.object(tracker, '_match_gy', return_value=None):
            tracker._update_request(download_requests_repo.get_download_request(request_id), [], [], gy_available=False)
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_called_once()
        event = self.sender.call_args.args[0]
        self.assertEqual(dict(event.fields)['下载'], 'qB 已停止跟踪 · 光鸭 下载中')
        get_download_request_result_5 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5['gy_status'], 'downloading')

    def test_legacy_cancelled_row_and_stale_snapshot_repair_through_original_producer(self):
        request_id, event_key = self.request(gy='failed')
        # 模拟旧备份里的混合状态：不调用新撤销方法，不预先设置刷新标记。
        download_requests_repo.update_download_request(request_id, qb_status='cancelled', status='failed')
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_not_called()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row['notification_delivery_status'], 'pending')
        DownloadTracker()._update_request(row, [], [])
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_called_once()
        self.assertEqual(dict(self.sender.call_args.args[0].fields)['下载'], 'qB 已停止跟踪 · 光鸭 失败')

    def test_cancel_after_claim_is_checked_before_transport(self):
        request_id, event_key = self.request()
        claimed = center.claim_due_notifications(event_key=event_key)[0]
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        center._dispatch_item(claimed)
        self.sender.assert_not_called()
        self.editor.assert_not_called()
        get_notification_result_6 = get_notification(event_key)
        assert get_notification_result_6 is not None
        self.assertEqual(get_notification_result_6['status'], 'suppressed')
        get_download_request_result_6 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6['status'], 'cancelled')

    def test_unknown_send_is_never_replayed_to_refresh_other_backend(self):
        request_id, event_key = self.request(gy='failed')
        self.sender.return_value = TelegramSendResult(ok=False, error='ReadTimeout', status_code=408)
        center.drain_telegram_notifications(event_key=event_key)
        get_notification_result_7 = get_notification(event_key)
        assert get_notification_result_7 is not None
        self.assertEqual(get_notification_result_7['status'], 'outcome_unknown')
        download_requests_repo.cancel_qb_download_tracking(['a'*40])
        DownloadTracker()._update_request(download_requests_repo.get_download_request(request_id), [], [])
        db.init_db()
        center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_called_once()
        latest = get_notification(event_key)
        assert latest is not None
        self.assertEqual(latest['status'], 'outcome_unknown')
        self.assertIn('已停止跟踪', latest['event_json'])

    def test_download_state_read_failure_does_not_send_unverified_old_message(self):
        _, event_key = self.request()
        with patch.object(download_requests_repo, 'get_download_request', side_effect=RuntimeError('temporary DB error')):
            center.drain_telegram_notifications(event_key=event_key)
        self.sender.assert_not_called()
        get_notification_result_8 = get_notification(event_key)
        assert get_notification_result_8 is not None
        self.assertEqual(get_notification_result_8['status'], 'retry_wait')
