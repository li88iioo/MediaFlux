"""重整真实成功后解除旧确认；所有目录删除均运行于 fake 云盘。"""

from tests.support import assert_present

from typing import Any
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.modules import download_staging_reconcile as worker
from app.modules import organize_confirmations as confirmations
from app.modules import telegram_notification_center as notifications
from app.modules.organize import OrganizeRules
from app.modules.process_lock import CrossProcessLock
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import NotificationAction, NotificationEvent, TelegramSendResult
from app.repositories import download_staging_reconcile as queue
from app.repositories import organize_history
from app.repositories.telegram_notifications import get_notification, _enqueue_confirmation_delivery
from tests.support import IsolatedDatabaseTestCase
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class ReorganizedDownloadReconciliationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            for table in ('download_staging_reconcile', 'download_request_keys', 'download_requests',
                          'organize_confirmation_delivery_outbox', 'telegram_notification_outbox',
                          'organize_confirmations', 'organize_log_items', 'organize_log', 'organize_delete_audit'):
                conn.execute(f'DELETE FROM {table}')
        self.enterContext(patch.object(notifications._dispatch_stop, 'is_set', return_value=True))
        self.enterContext(patch('app.modules.telegram_notification_policy.notifications_enabled', return_value=True))
        self.enterContext(patch.object(notifications, 'wake_telegram_notification_dispatcher'))
        self.enterContext(patch.object(confirmations, 'wake_recognition_review_dispatcher'))
        self.sender = self.enterContext(patch.object(notifications, 'send_event_result'))
        self.editor = self.enterContext(patch.object(
            notifications, 'edit_event_result', return_value=TelegramSendResult(ok=True, message_id=77),
        ))
        db.kv_set(queue.LEGACY_CURSOR_KEY, '0')
        self.request_id, _ = download_requests_repo.create_download_request('reorganized', 'magnet')
        download_requests_repo.update_download_request(
            self.request_id, targets='guangya', status='completed', gy_status='completed', gy_isolated=1,
            gy_target_dir='stage', gy_staging_parent_dir='source', gy_staging_name='MF-case',
            gy_staging_cleanup_status='retained', gy_staging_cleanup_error='仍有待确认事项',
            organize_started=1, organize_status='requires_manual', organize_error='仍有1项媒体等待确认',
            organize_task_id='task', gy_task_id='cloud', gy_task_ids='["cloud"]',
        )
        self.payload: dict[str, Any] = {
            'source_dir_id': 'stage', 'source_parent_id': 'stage', 'organize_task_id': 'task',
            'download_request_ids': [self.request_id],
            'files': [{'file_id': 'video', 'parent_id': 'stage', 'size': 100, 'etag': 'hash'}],
            'candidates': [{'title': '旧候选', 'tmdb_id': '1', 'media_type': 'tv'}],
        }
        self.confirm('card', 'failed')
        self.log_id = self.evidence('video')

    def confirm(self, token, status, *, fingerprint='fingerprint'):
        # 明确候选早于成功证据；缺少 ID 上界的旧 payload 不能靠同秒时间猜先后。
        with patch.object(db, 'now', return_value='2026-01-01 00:00:00'):
            db.create_organize_confirmation(
                token=token, fingerprint=fingerprint, chat_id='test', source_name='stage', directory_path='/',
                payload=self.payload, expires_at='2099-01-01 00:00:00',
            )
        db.update_organize_confirmation(token, status=status, error='旧确认失败')

    def evidence(self, file_id):
        log_id = db.add_organize_log(
            'guangya', '/stage/video.mkv', '/target/video.mkv', file_id, 'success',
            operation_type='reorganize', source_dir_id='stage', original_parent_id='stage',
            original_name='video.mkv', current_parent_id='target', current_name='video.mkv',
            target_parent_id='target', media_type='tv', title='实际入库剧集', year='2026', season=1, episode=31,
        )
        db.add_organize_log_items(log_id, [{
            'file_id': file_id, 'role': 'video', 'original_parent_id': 'stage', 'original_name': 'video.mkv',
            'current_parent_id': 'target', 'current_name': 'video.mkv',
            'target_parent_id': 'target', 'target_name': 'video.mkv', 'size': 100, 'etag': 'hash',
        }])
        return log_id

    def drain(self, cloud):
        lock = CrossProcessLock('guangya-organize', directory=self.test_db_path.parent)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
                return worker.reconcile_with_client(
                    cloud, rules=OrganizeRules(target_dir_id='target', clean_empty=True, link_strm=False),
                    writer_lock=lock,
                )
        finally:
            lock.release()

    def show_card(self):
        self.assertTrue(confirmations.publish_confirmation_event(
            NotificationEvent('请选择旧候选', actions=(NotificationAction('选择', 'orgc:card:0'),)),
            token='card', chat_id='test', message_id=77, deliver_now=False,
        ))
        key = notifications.notification_thread_event_key(
            'confirmation:card', topic=NotificationTopic.CONFIRMATION, chat_id='test',
        )
        self.assertTrue(notifications.drain_telegram_notifications(limit=1, event_key=key))
        self.editor.reset_mock()
        return key

    def test_successful_reorganize_closes_card_and_request_then_cleans_empty_root(self):
        key = self.show_card()
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])
        card = db.get_organize_confirmation('card')
        assert card is not None
        stats = json.loads(card['result_json'])
        self.assertEqual(card['status'], 'completed')
        self.assertIsNone(card['selected_index'])
        self.assertEqual(stats['previous_confirmation']['status'], 'failed')
        self.assertEqual(stats['organize_log_ids'], [self.log_id])
        delivery = assert_present(db.get_organize_confirmation_delivery('card'))
        event = notifications.deserialize_notification_event(delivery['event_json'])
        self.assertIn('Web', str(event.title))
        self.assertIn('完成', str(event.title))
        self.assertEqual(event.actions, ())
        self.assertNotIn('旧候选', str(event))
        self.assertIn('实际入库剧集 (2026) · S01E31', str(event.lines))
        self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        self.assertTrue(notifications.drain_telegram_notifications(limit=1, event_key=key))
        self.editor.assert_called_once_with(event, chat_id='test', message_id=77)
        self.sender.assert_not_called()
        delivered = assert_present(get_notification(key))
        self.assertEqual((delivered['revision'], delivered['delivered_revision'], delivered['message_id']), (2, 2, 77))
        cloud = _CloudTree()
        self.drain(cloud)
        request = download_requests_repo.get_download_request(self.request_id)
        assert request is not None
        self.assertEqual(request['organize_status'], 'completed')
        self.assertEqual(request['organize_error'], '')
        self.assertEqual(request['gy_staging_cleanup_status'], 'completed')
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 0)
        self.assertEqual(cloud.deleted, ['stage'])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.drain(cloud)
        self.assertEqual(cloud.deleted, ['stage'])
        self.assertFalse(confirmations._dispatch_due_confirmation_delivery('card'))
        self.assertEqual(get_notification(key), delivered)

    def test_completed_reorganized_card_repairs_existing_initial_message_on_next_reconcile(self):
        key = self.show_card()
        stats = {'moved': 1, 'failed': 0, 'resolution': 'reorganized', 'organize_log_ids': [self.log_id]}
        db.update_organize_confirmation('card', status='completed', result_json=json.dumps(stats),
                                       completed_at='2026-11-09 00:16:08')
        download_requests_repo.update_download_request(self.request_id, organize_status='completed', gy_staging_cleanup_status='completed')
        before = dict(assert_present(db.get_organize_confirmation('card')))
        with db.get_conn() as conn:
            conn.execute('DELETE FROM organize_log_items')
            conn.execute('DELETE FROM organize_log')
        db.kv_set(queue.LEGACY_CURSOR_KEY, str(self.request_id))
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.assertEqual(db.kv_get(queue.LEGACY_CURSOR_KEY), '0')
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.assertIsNone(db.get_organize_confirmation_delivery('card'))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertIsNotNone(db.get_organize_confirmation_delivery('card'))
        self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        self.assertTrue(notifications.drain_telegram_notifications(limit=1, event_key=key))
        self.assertEqual(dict(assert_present(db.get_organize_confirmation('card'))), before)
        self.assertEqual(queue.list_due_cleanup_intents(), [])
        self.assertEqual(self.editor.call_count, 1)
        self.sender.assert_not_called()

    def test_completed_history_without_a_delivered_initial_candidate_message_never_sends(self):
        stats = {'moved': 1, 'failed': 0, 'resolution': 'reorganized', 'organize_log_ids': [self.log_id]}
        db.update_organize_confirmation('card', status='completed', result_json=json.dumps(stats))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertIsNone(db.get_organize_confirmation_delivery('card'))
        key = self.show_card()
        baseline = assert_present(get_notification(key))
        mutations = (
            {'message_id': None}, {'revision': 2}, {'delivered_revision': 0}, {'status': 'retry_wait'},
            {'event_json': notifications.serialize_notification_event(NotificationEvent('已完成'))},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with db.get_conn() as conn:
                    conn.execute('UPDATE telegram_notification_outbox SET ' + ','.join(f'{key}=?' for key in mutation)
                                 + ' WHERE id=?', (*mutation.values(), baseline['id']))
                before = get_notification(key)
                self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
                self.assertIsNone(db.get_organize_confirmation_delivery('card'))
                self.assertEqual(get_notification(key), before)
                with db.get_conn() as conn:
                    conn.execute('UPDATE telegram_notification_outbox SET ' + ','.join(f'{key}=?' for key in mutation)
                                 + ' WHERE id=?', (*(baseline[key] for key in mutation), baseline['id']))
        self.sender.assert_not_called()
        self.editor.assert_not_called()

    def test_history_repair_does_not_overwrite_thread_updated_after_reconcile(self):
        key = self.show_card()
        stats = {'moved': 1, 'failed': 0, 'resolution': 'reorganized', 'organize_log_ids': [self.log_id]}
        db.update_organize_confirmation('card', status='completed', result_json=json.dumps(stats))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertIsNotNone(db.get_organize_confirmation_delivery('card'))
        newer = NotificationEvent('后续媒体库已刷新')
        self.assertTrue(confirmations.publish_confirmation_event(
            newer, token='card', chat_id='test', terminal=True, deliver_now=False,
        ))
        before = get_notification(key)
        self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        self.assertEqual(get_notification(key), before)
        self.assertEqual(assert_present(db.get_organize_confirmation_delivery('card'))['status'], 'sent')
        self.assertFalse(confirmations._dispatch_due_confirmation_delivery('card'))
        self.editor.assert_not_called()
        self.sender.assert_not_called()

    def test_media_summary_only_aggregates_committed_log_ids_and_actual_episode_positions(self):
        second = self.evidence('second')
        db.update_organize_log(second, episode=32)
        unrelated = self.evidence('unrelated')
        db.update_organize_log(unrelated, title='无关作品', season=3, episode=99)
        self.payload['files'].append({'file_id': 'second', 'parent_id': 'stage', 'size': 100, 'etag': 'hash'})
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])
        delivery = assert_present(db.get_organize_confirmation_delivery('card'))
        event = notifications.deserialize_notification_event(delivery['event_json'])
        self.assertIn('实际入库剧集 (2026) · S01E31-S01E32', str(event.lines))
        self.assertNotIn('无关作品', str(event))
        self.assertNotIn('旧候选', str(event))
        self.assertIsNone(assert_present(db.get_organize_confirmation('card'))['selected_index'])

    def test_repeated_reconciliation_preserves_delivery_retry_and_later_downstream_card(self):
        key = self.show_card()
        organize_history.reconcile_reorganized_confirmations(['stage'])
        with patch.object(confirmations, 'publish_confirmation_event', return_value=False):
            self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        retry = dict(assert_present(db.get_organize_confirmation_delivery('card')))
        self.assertEqual(retry['status'], 'retry_wait')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertEqual(dict(assert_present(db.get_organize_confirmation_delivery('card'))), retry)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmation_delivery_outbox SET next_attempt_at='' WHERE confirmation_token='card'")
        self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        self.editor.return_value = TelegramSendResult(ok=False, status_code=503, error='temporary edit failure')
        self.assertFalse(notifications.drain_telegram_notifications(limit=1, event_key=key))
        edit_retry = assert_present(get_notification(key))
        self.assertEqual(edit_retry['status'], 'retry_wait')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertEqual(get_notification(key), edit_retry)
        self.editor.return_value = TelegramSendResult(ok=True, message_id=77)
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='' WHERE id=?", (edit_retry['id'],))
        self.assertTrue(notifications.drain_telegram_notifications(limit=1, event_key=key))
        self.assertTrue(confirmations.update_confirmation_lifecycle_downstream(
            'card', chat_id='test', strm_status='完成', media_refresh='已刷新',
        ))
        latest = assert_present(get_notification(key))
        final_delivery = dict(assert_present(db.get_organize_confirmation_delivery('card')))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertFalse(confirmations._dispatch_due_confirmation_delivery('card'))
        self.assertEqual(get_notification(key), latest)
        self.assertEqual(dict(assert_present(db.get_organize_confirmation_delivery('card'))), final_delivery)

    def test_reorganize_invalidates_inflight_old_failure_delivery(self):
        key = self.show_card()
        old_event = NotificationEvent('旧确认失败', actions=(NotificationAction('重试', 'orgc:card:0'),))
        with db.get_conn() as conn:
            _enqueue_confirmation_delivery(
                conn, token='card', event_json=notifications.serialize_notification_event(old_event),
                chat_id='test', message_id=77, timestamp=db.now(),
            )
        old = assert_present(db.claim_due_organize_confirmation_delivery(
            current_time=db.now(), stale_before='', token='card',
        ))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])
        self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
        terminal = assert_present(get_notification(key))
        self.assertFalse(confirmations.publish_confirmation_event(
            old_event, token='card', chat_id='test', terminal=True,
            expected_confirmation_delivery=(int(old['id']), int(old['lease_generation'])),
        ))
        self.assertEqual(get_notification(key), terminal)

    def test_web_completion_wins_over_tg_selection_that_already_read_pending(self):
        db.update_organize_confirmation('card', status='pending')
        key = self.show_card()
        ready, resume = threading.Event(), threading.Event()
        original_claim = db.claim_organize_confirmation

        def delayed_claim(*args, **kwargs):
            ready.set()
            if not resume.wait(timeout=5):
                raise RuntimeError('TG selection timed out')
            return original_claim(*args, **kwargs)

        with patch.object(db, 'claim_organize_confirmation', side_effect=delayed_claim), ThreadPoolExecutor(max_workers=1) as executor:
            selection = executor.submit(confirmations.start_confirmation, 'card', 0, chat_id='test')
            try:
                self.assertTrue(ready.wait(timeout=5))
                self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])
                self.assertTrue(confirmations._dispatch_due_confirmation_delivery('card'))
                terminal = assert_present(get_notification(key))
            finally:
                resume.set()
            with self.assertRaisesRegex(ValueError, '已处理'):
                selection.result(timeout=5)
        self.assertEqual(get_notification(key), terminal)
        self.assertIsNone(assert_present(db.get_organize_confirmation('card'))['selected_index'])

    def test_tg_selection_wins_before_web_reconciliation_and_is_not_overwritten(self):
        db.update_organize_confirmation('card', status='pending')
        key = self.show_card()
        with patch.object(confirmations, '_dispatch_next_queued_confirmation'), patch.object(
            confirmations, '_dispatch_due_confirmation_delivery', return_value=False,
        ), patch.object(confirmations, 'wake_confirmation_dispatcher', return_value=False):
            selected = confirmations.start_confirmation('card', 0, chat_id='test')
        self.assertEqual(selected['status'], 'queued')
        accepted = dict(assert_present(db.get_organize_confirmation_delivery('card')))
        visible = assert_present(get_notification(key))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.assertEqual(dict(assert_present(db.get_organize_confirmation_delivery('card'))), accepted)
        self.assertEqual(get_notification(key), visible)
        self.assertEqual(assert_present(db.get_organize_confirmation('card'))['selected_index'], 0)

    def failed_recovery(self):
        from app.repositories.download_staging import list_staging_cleanup_requests
        download_requests_repo.update_download_request(self.request_id, organize_status='failed', organize_started=-1)
        return {
            'request': list_staging_cleanup_requests(source_id='stage')[0],
            'members': queue.reorganized_source_evidence('stage'),
        }

    def test_failed_request_requires_explicit_verified_recovery_not_background_discovery(self):
        recovery = self.failed_recovery()
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        get_download_request_result = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result['organize_started'], -1)
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), ['card'])
        row = download_requests_repo.get_download_request(self.request_id)
        assert row is not None
        self.assertEqual((row['organize_status'], row['organize_started']), ('completed', 1))
        cloud = _CloudTree()
        self.drain(cloud)
        get_download_request_result_2 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['gy_staging_cleanup_status'], 'completed')
        self.assertEqual(cloud.deleted, ['stage'])

    def test_failed_recovery_rejects_cancel_resubmit_rebind_hidden_and_unknown_cleanup(self):
        mutations = [
            {'status': 'cancelled'}, {'status': 'resubmitted'}, {'organize_task_id': 'new-task'},
            {'gy_task_id': 'new-cloud-task'}, {'gy_target_dir': 'new-stage'},
            {'attention_cleared_at': db.now()}, {'gy_staging_cleanup_status': 'failed'},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                recovery = self.failed_recovery()
                original = {key: assert_present(download_requests_repo.get_download_request(self.request_id))[key] for key in mutation}
                with db.get_conn() as conn:
                    conn.execute('UPDATE download_requests SET ' + ','.join(f'{key}=?' for key in mutation)
                                 + ' WHERE id=?', (*mutation.values(), self.request_id))
                self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
                row = download_requests_repo.get_download_request(self.request_id)
                assert row is not None
                self.assertEqual(row['organize_started'], -1)
                for key, value in mutation.items():
                    assert row is not None
                    self.assertEqual(row[key], value)
                with db.get_conn() as conn:
                    conn.execute('UPDATE download_requests SET ' + ','.join(f'{key}=?' for key in original)
                                 + ' WHERE id=?', (*original.values(), self.request_id))

    def test_failed_recovery_rechecks_member_versions_in_transaction(self):
        recovery = self.failed_recovery()
        db.update_organize_log(self.log_id, status='failed')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
        get_download_request_result_3 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3['organize_status'], 'failed')

    def test_failed_recovery_requires_every_latest_group_not_only_last_corrected_video(self):
        recovery = self.failed_recovery()
        other = self.evidence('second')
        db.update_organize_log(other, status='failed')
        self.assertEqual(queue.reorganized_source_evidence('stage'), [])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
        get_download_request_result_4 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4['organize_started'], -1)

    def test_failed_recovery_rejects_missing_confirmation_member_even_when_known_logs_succeed(self):
        recovery = self.failed_recovery()
        self.payload['companions'] = [{'file_id': 'unlogged-subtitle', 'parent_id': 'stage'}]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'",
                         (json.dumps(self.payload),))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
        get_download_request_result_5 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5['organize_status'], 'failed')

    def test_failed_recovery_rejects_active_confirmation_and_unknown_delete(self):
        recovery = self.failed_recovery()
        db.update_organize_confirmation('card', status='queued')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
        db.update_organize_confirmation('card', status='failed')
        db.add_organize_delete_audit(trigger='download_staging_cleanup', reason='lost response',
                                    file_id='stage', file_name='MF-case', parent_id='source', status='failed')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery), [])
        get_download_request_result_6 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6['organize_started'], -1)

    def test_failed_recovery_and_confirmation_queue_commit_atomically(self):
        import sqlite3
        recovery = self.failed_recovery()
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON download_staging_reconcile "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic queue failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                organize_history.reconcile_reorganized_confirmations(['stage'], recovery=recovery)
        finally:
            with db.get_conn() as conn:
                conn.execute('DROP TRIGGER reject_recovery')
        get_download_request_result_7 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_7 is not None
        self.assertEqual(get_download_request_result_7['organize_started'], -1)
        get_organize_confirmation_result = db.get_organize_confirmation('card')
        assert get_organize_confirmation_result is not None
        self.assertEqual(get_organize_confirmation_result['status'], 'failed')

    def test_successful_manual_organize_also_supersedes_old_expired_card(self):
        db.update_organize_log(self.log_id, operation_type='organize')
        db.update_organize_confirmation('card', status='expired')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])

    def test_newer_failed_log_prevents_reusing_old_success_evidence(self):
        newer = self.evidence('video')
        db.update_organize_log(newer, status='failed')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_newer_failed_primary_without_items_cannot_fall_back_to_old_success(self):
        db.add_organize_log('guangya', '/stage/video.mkv', '', 'video', 'failed', source_dir_id='stage')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_incomplete_audit_is_not_success_evidence(self):
        db.update_organize_log(self.log_id, legacy_incomplete=1)
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_missing_payload_companion_blocks_resolution(self):
        self.payload['companions'] = [{'file_id': 'subtitle', 'parent_id': 'stage', 'size': 100, 'etag': 'hash'}]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        db.add_organize_log_items(self.log_id, [{
            'file_id': 'subtitle', 'role': 'subtitle', 'original_parent_id': 'stage',
            'current_parent_id': 'target', 'current_name': 'video.srt',
            'target_parent_id': 'target', 'target_name': 'video.srt', 'size': 100, 'etag': 'hash',
        }])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])

    def test_newer_failed_companion_without_details_blocks_old_group_success(self):
        self.payload['companions'] = [{'file_id': 'subtitle', 'parent_id': 'stage'}]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        db.add_organize_log_items(self.log_id, [{
            'file_id': 'subtitle', 'role': 'subtitle', 'original_parent_id': 'stage',
            'current_parent_id': 'target', 'current_name': 'video.srt',
            'target_parent_id': 'target', 'target_name': 'video.srt',
        }])
        db.add_organize_log('guangya', '/stage/video.srt', '', 'subtitle', 'failed', source_dir_id='stage')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_newer_failed_companion_under_other_video_log_blocks_old_group(self):
        self.payload['companions'] = [{'file_id': 'subtitle', 'parent_id': 'stage'}]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        item = {
            'file_id': 'subtitle', 'role': 'subtitle', 'original_parent_id': 'stage',
            'current_parent_id': 'target', 'current_name': 'video.srt',
            'target_parent_id': 'target', 'target_name': 'video.srt',
        }
        db.add_organize_log_items(self.log_id, [item])
        newer = self.evidence('other-video')
        db.add_organize_log_items(newer, [{**item, 'status': 'failed'}])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_history_discovery_repairs_old_reorganized_records_without_redownload(self):
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 1)
        get_organize_confirmation_result_2 = db.get_organize_confirmation('card')
        assert get_organize_confirmation_result_2 is not None
        self.assertEqual(get_organize_confirmation_result_2['status'], 'completed')
        self.assertEqual(len(queue.list_due_cleanup_intents()), 1)

    def test_only_latest_card_is_closed_and_terminal_history_is_preserved(self):
        self.confirm('retry', 'pending')
        # 新票据不能复用签发前的成功；为重试补充新的成功提交。
        self.evidence('video')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['retry'])
        get_organize_confirmation_result_3 = db.get_organize_confirmation('card')
        assert get_organize_confirmation_result_3 is not None
        self.assertEqual(get_organize_confirmation_result_3['status'], 'failed')
        get_organize_confirmation_result_4 = db.get_organize_confirmation('retry')
        assert get_organize_confirmation_result_4 is not None
        self.assertEqual(get_organize_confirmation_result_4['status'], 'completed')

    def test_expired_and_cancelled_cards_can_be_resolved_but_active_execution_cannot(self):
        for status in ('expired', 'cancelled', 'queued', 'running'):
            with self.subTest(status=status):
                db.update_organize_confirmation('card', status=status)
                tokens = organize_history.reconcile_reorganized_confirmations(['stage'])
                self.assertEqual(tokens, ['card'] if status in ('expired', 'cancelled') else [])

    def test_all_files_in_confirmation_must_have_successful_evidence(self):
        self.payload['files'].append({'file_id': 'second', 'parent_id': 'stage', 'size': 100, 'etag': 'hash'})
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        self.evidence('second')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), ['card'])

    def test_mismatched_identity_failed_move_and_unmoved_file_never_close_card(self):
        for column, value in (('etag', 'other'), ('size', 999), ('current_parent_id', 'stage'),
                              ('current_name', 'wrong.mkv'), ('status', 'rollback_failed')):
            with self.subTest(column=column):
                with db.get_conn() as conn:
                    old = conn.execute(f'SELECT {column} FROM organize_log_items WHERE log_id=?', (self.log_id,)).fetchone()[0]
                    conn.execute(f'UPDATE organize_log_items SET {column}=? WHERE log_id=?', (value, self.log_id))
                self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
                with db.get_conn() as conn:
                    conn.execute(f'UPDATE organize_log_items SET {column}=? WHERE log_id=?', (old, self.log_id))

    def test_failed_companion_blocks_resolution(self):
        db.add_organize_log_items(self.log_id, [{
            'file_id': 'subtitle', 'role': 'subtitle', 'status': 'failed', 'original_parent_id': 'stage',
        }])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_unrelated_task_or_cancelled_download_is_not_resurrected(self):
        download_requests_repo.update_download_request(self.request_id, organize_task_id='new-task')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])
        download_requests_repo.update_download_request(self.request_id, organize_task_id='task', status='cancelled')
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(['stage']), [])

    def test_confirmation_and_cleanup_intent_roll_back_together_on_queue_failure(self):
        import sqlite3
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_reorganized_queue BEFORE INSERT ON download_staging_reconcile "
                "BEGIN SELECT RAISE(ABORT,'queue-failure'); END"
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'queue-failure'):
                organize_history.reconcile_reorganized_confirmations(['stage'])
            get_organize_confirmation_result_5 = db.get_organize_confirmation('card')
            assert get_organize_confirmation_result_5 is not None
            self.assertEqual(get_organize_confirmation_result_5['status'], 'failed')
            self.assertEqual(queue.list_due_cleanup_intents(), [])
        finally:
            with db.get_conn() as conn:
                conn.execute('DROP TRIGGER reject_reorganized_queue')

    def test_original_cleanup_disabled_policy_is_preserved(self):
        self.payload['rules'] = {'clean_empty': False}
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='card'", (json.dumps(self.payload),))
        organize_history.reconcile_reorganized_confirmations(['stage'])
        cloud = _CloudTree()
        self.drain(cloud)
        get_download_request_result_8 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_8 is not None
        self.assertEqual(get_download_request_result_8['organize_status'], 'completed')
        self.assertEqual(cloud.deleted, [])

    def test_remaining_file_keeps_attention_even_after_old_confirmation_is_resolved(self):
        organize_history.reconcile_reorganized_confirmations(['stage'])
        cloud = _CloudTree()
        cloud.add_file('unrelated.mkv', 'stage')
        self.drain(cloud)
        get_download_request_result_9 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_9 is not None
        self.assertEqual(get_download_request_result_9['organize_status'], 'completed')
        get_download_request_result_10 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_10 is not None
        self.assertEqual(get_download_request_result_10['gy_staging_cleanup_status'], 'retained')
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 1)
        self.assertEqual(cloud.deleted, [])
