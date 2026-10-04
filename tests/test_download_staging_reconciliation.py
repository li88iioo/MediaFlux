"""持久确认收尾：所有数据库/云盘操作只用测试隔离库和 fake。"""
from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.modules import download_staging_reconcile as worker
from app.modules.organize import OrganizeRules
from app.modules.process_lock import CrossProcessLock
from app.repositories import download_staging_reconcile as queue
from tests.support import IsolatedDatabaseTestCase
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class DownloadStagingReconciliationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in ('download_staging_reconcile', 'download_request_keys', 'download_requests',
                          'organize_confirmations', 'organize_delete_audit'):
                conn.execute(f'DELETE FROM {table}')
        db.kv_set(queue.LEGACY_CURSOR_KEY, '0')
        self.cloud = _CloudTree()
        self.writer_lock = CrossProcessLock('guangya-organize', directory=self.test_db_path.parent)
        self.rules = OrganizeRules(target_dir_id='target', clean_empty=True, link_strm=False)
        self.request_id = self.request('one', 'stage')
        self.payload = {'source_dir_id': 'stage', 'source_parent_id': 'stage',
                        'files': [{'file_id': 'video', 'parent_id': 'stage', 'name': 'video.mkv'}]}
        self.confirm('done')

    def request(self, name, root):
        request_id, _ = db.create_download_request(f'reconcile-{name}', 'magnet')
        db.update_download_request(
            request_id, targets='guangya', status='completed', gy_status='completed', gy_isolated=1,
            gy_target_dir=root, gy_staging_parent_dir='source', gy_staging_name='MF-case' if root == 'stage' else root,
            gy_staging_cleanup_status='retained', gy_staging_cleanup_error='隔离目录仍有 1 项未整理或未识别：video.mkv',
            organize_started=1, organize_status='completed', organize_task_id=f'task-{name}',
            gy_task_id=f'cloud-{name}', gy_task_ids=json.dumps([f'cloud-{name}']),
        )
        return request_id

    def confirm(self, token, *, payload=None, status='completed', fingerprint=None, stats=None):
        db.create_organize_confirmation(
            token=token, fingerprint=fingerprint or token, chat_id='test', source_name='test',
            directory_path='/', payload=payload or self.payload, expires_at='2099-01-01 00:00:00',
        )
        db.update_organize_confirmation(token, status=status, result_json=json.dumps(
            stats if stats is not None else {'moved': 1, 'failed': 0, 'need_confirm': 0},
        ))

    def enqueue(self, token='done'):
        with db.get_conn() as conn:
            return queue.enqueue_confirmation_cleanup(conn, token=token, timestamp=db.now())

    def drain(self, *, config_values=None, **kwargs):
        self.assertTrue(self.writer_lock.acquire(blocking=False))
        try:
            values = config_values if config_values is not None else {}
            with patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': values.get(key, default)):
                return worker.reconcile_with_client(self.cloud, rules=self.rules, writer_lock=self.writer_lock, **kwargs)
        finally:
            self.writer_lock.release()

    def due_now(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE download_staging_reconcile SET next_attempt_at='' WHERE status IN ('pending','retry')")

    def job(self, token='done'):
        with db.get_conn() as conn:
            return dict(conn.execute(
                'SELECT q.* FROM download_staging_reconcile q JOIN organize_confirmations c '
                'ON c.id=q.confirmation_id WHERE c.token=?', (token,),
            ).fetchone())

    def test_three_legacy_completed_cards_without_business_ids_are_bounded_and_recovered(self):
        ids = [self.request_id]
        for suffix in ('two', 'three'):
            root = f'stage-{suffix}'
            self.cloud.add_dir(root, 'source')
            ids.append(self.request(suffix, root))
            self.confirm(suffix, payload={**self.payload, 'source_dir_id': root})
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(limit=2), 2)
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(limit=2), 1)
        self.drain()
        for request_id in ids:
            row = db.get_download_request(request_id)
            self.assertEqual(row['gy_staging_cleanup_status'], 'completed')
            self.assertEqual(row['gy_staging_cleanup_error'], '')
        self.assertCountEqual(self.cloud.deleted, ['stage', 'stage-two', 'stage-three'])
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(limit=2), 0)
        self.drain()
        self.assertEqual(len(self.cloud.deleted), 3)

    def test_new_confirmation_and_intent_commit_atomically_without_notification(self):
        db.update_organize_confirmation('done', status='running')
        db.complete_organize_confirmation_with_delivery(
            'done', result_json='{"moved":1,"failed":0}', event_json='{}', chat_id='',
            message_id=None, enqueue_delivery=False,
        )
        self.assertEqual(self.job()['status'], 'pending')
        # 模拟终态提交后进程消失：仅持久队列足以恢复，不重跑确认。
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_atomic_enqueue_failure_rolls_back_terminal_confirmation(self):
        db.update_organize_confirmation('done', status='running')
        with patch.object(queue, 'enqueue_confirmation_cleanup', side_effect=RuntimeError('db unavailable')), self.assertRaises(RuntimeError):
            db.complete_organize_confirmation_with_delivery(
                'done', result_json='{"moved":1}', event_json='{}', chat_id='',
                message_id=None, enqueue_delivery=False,
            )
        self.assertEqual(db.get_organize_confirmation('done')['status'], 'running')

    def test_read_error_persists_backoff_then_new_worker_recovers_once(self):
        self.enqueue()
        with patch.object(self.cloud, 'list_dir', side_effect=OSError('offline')):
            self.drain()
        self.assertEqual(self.job()['status'], 'retry')
        self.assertTrue(self.job()['next_attempt_at'])
        self.assertEqual(self.cloud.deleted, [])
        attempts = self.job()['attempt_count']
        self.drain()
        self.assertEqual(self.job()['attempt_count'], attempts)
        self.due_now()
        self.drain()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(db.get_organize_confirmation('done')['status'], 'completed')

    def test_prewrite_process_interruption_keeps_durable_intent(self):
        self.enqueue()
        with patch.object(self.cloud, 'list_dir', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.drain()
        self.assertIn(self.job()['status'], ('pending', 'retry'))
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_unknown_delete_and_post_write_crash_are_never_replayed(self):
        self.enqueue()
        with patch.object(self.cloud, 'delete_empty_directory', side_effect=KeyboardInterrupt) as delete:
            with self.assertRaises(KeyboardInterrupt):
                self.drain()
            self.assertEqual(delete.call_count, 1)
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'failed')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')

    def test_clean_empty_disabled_updates_policy_reason_without_cloud_or_false_completed(self):
        self.enqueue()
        self.rules.clean_empty = False
        self.drain()
        row = db.get_download_request(self.request_id)
        self.assertEqual(row['gy_staging_cleanup_status'], 'skipped')
        self.assertFalse(row['attention_cleared_at'])
        self.assertEqual(db.count_download_requests_requiring_attention(), 0)
        self.assertIn('stage', db.list_protected_guangya_staging_ids())
        self.assertIn('策略', row['gy_staging_cleanup_error'])
        self.assertNotIn('未识别', row['gy_staging_cleanup_error'])
        self.assertEqual(self.cloud.reads, [])
        self.assertEqual(self.job()['status'], 'retained')

    def test_nonempty_tree_is_retained_not_completed_and_old_alarm_is_replaced(self):
        self.enqueue()
        self.cloud.add_file('unselected.srt', 'stage')
        self.drain()
        self.assertEqual(self.job()['status'], 'retained')
        self.assertEqual(self.cloud.deleted, [])
        row = db.get_download_request(self.request_id)
        self.assertEqual(row['gy_staging_cleanup_status'], 'retained')
        self.assertNotIn('未识别', row['gy_staging_cleanup_error'])

    def test_changed_request_identity_blocks_even_if_new_root_is_empty(self):
        self.enqueue()
        db.update_download_request(self.request_id, gy_task_id='replacement-task')
        self.drain()
        self.assertEqual(self.job()['status'], 'blocked')
        self.assertEqual(self.cloud.reads, [])
        self.assertEqual(self.cloud.deleted, [])

    def test_duplicate_owner_and_cancelled_failed_cleared_are_fail_closed(self):
        for mutation in ('duplicate', 'cancelled', 'failed', 'cleared', 'hidden', 'delete_unknown'):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.enqueue()
                if mutation == 'duplicate':
                    self.request('duplicate', 'stage')
                elif mutation == 'hidden':
                    self.assertEqual(db.clear_download_request_attentions([self.request_id], batch=False)['cleared'], [self.request_id])
                elif mutation == 'cleared':
                    db.update_download_request(self.request_id, organize_status='cleared')
                elif mutation == 'delete_unknown':
                    db.update_download_request(self.request_id, gy_staging_cleanup_status='failed')
                else:
                    db.update_download_request(self.request_id, status=mutation)
                self.drain()
                self.assertEqual(self.job()['status'], 'blocked')
                self.assertEqual(self.cloud.reads, [])
                self.assertEqual(self.cloud.deleted, [])

    def test_newer_or_unsafe_confirmation_invalidates_persisted_success(self):
        for status in ('pending', 'failed', 'cancelled', 'completed'):
            with self.subTest(status=status):
                self.setUp()
                self.enqueue()
                self.confirm('newer', fingerprint='done', status=status)
                self.drain()
                self.assertEqual(self.job()['status'], 'blocked')
                self.assertEqual(self.cloud.reads, [])

    def test_old_explicit_wrong_binding_cannot_fall_back_to_source_match(self):
        with db.get_conn() as conn:
            conn.execute('UPDATE organize_confirmations SET payload_json=? WHERE token=?', (
                json.dumps({**self.payload, 'download_request_ids': [self.request_id + 1]}), 'done',
            ))
        self.assertIsNone(self.enqueue())
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.drain()
        self.assertEqual(self.cloud.deleted, [])

    def test_scheduler_lock_busy_records_backoff_without_creating_client(self):
        self.enqueue()
        with patch.object(worker, 'get_organize_manager') as manager, patch.object(worker, 'GuangYaClient') as client:
            manager.return_value.start_operation.return_value = {'ok': False, 'error': 'busy'}
            worker.schedule_staging_reconciliation()
            worker.schedule_staging_reconciliation()
            self.assertEqual(manager.return_value.start_operation.call_count, 1)
            client.assert_not_called()
        self.assertEqual(self.job()['status'], 'retry')

    def test_tracker_with_no_active_downloads_reaches_real_scheduler_callback(self):
        from app.modules.download_tracker import DownloadTracker
        tracker = DownloadTracker()

        def launch(_operation, _reference, callback, **_kwargs):
            self.assertTrue(self.writer_lock.acquire(blocking=False))
            try:
                callback()
            finally:
                self.writer_lock.release()
            return {'ok': True}

        with patch.object(tracker, '_run_torrent_data_cleanup_if_due'), \
                patch.object(db, 'recover_stale_submitting_download_requests', return_value=0), \
                patch.object(db, 'list_local_media_sources', return_value=[]), \
                patch.object(db, 'list_active_download_requests', return_value=[]), \
                patch.object(worker, 'get_organize_manager') as manager, \
                patch.object(worker, 'GuangYaClient', return_value=self.cloud), \
                patch.object(worker, 'close_guangya_client'), \
                patch.object(worker.OrganizeRules, 'from_config', return_value=self.rules), \
                patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
            manager.return_value._lock = self.writer_lock
            manager.return_value.start_operation.side_effect = launch
            self.assertEqual(tracker.run_once(), 0)
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_ambiguous_owner_introduced_during_audit_stops_before_provider(self):
        self.enqueue()
        original = db.add_organize_delete_audit

        def add_owner(**kwargs):
            audit_id = original(**kwargs)
            self.request('late-duplicate', 'stage')
            return audit_id

        with patch.object(db, 'add_organize_delete_audit', side_effect=add_owner):
            self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')

    def test_cancelled_card_introduced_during_audit_stops_before_provider(self):
        self.enqueue()
        original = db.add_organize_delete_audit

        def cancel(**kwargs):
            audit_id = original(**kwargs)
            self.confirm('cancelled-late', status='cancelled')
            return audit_id

        with patch.object(db, 'add_organize_delete_audit', side_effect=cancel):
            self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')

    def test_precommit_download_identity_snapshot_cannot_rebind_to_replacement(self):
        before = dict(db.get_download_request(self.request_id))
        db.update_organize_confirmation('done', result_json=json.dumps({
            'moved': 1, 'download_staging_identity': before,
        }))
        db.update_download_request(self.request_id, gy_task_id='new-backend')
        self.assertIsNone(self.enqueue())
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.drain()
        self.assertEqual(self.cloud.deleted, [])

    def test_retryable_read_failure_during_audit_is_recorded_as_no_provider_write(self):
        self.enqueue()
        original = db.add_organize_delete_audit
        info = self.cloud.file_info

        def unavailable(**kwargs):
            audit_id = original(**kwargs)
            self.cloud.file_info = lambda *_: (_ for _ in ()).throw(OSError('offline'))
            return audit_id

        with patch.object(db, 'add_organize_delete_audit', side_effect=unavailable):
            self.drain()
        self.assertEqual(self.job()['status'], 'retry')
        with db.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM organize_delete_audit').fetchone()['status'], 'blocked')
        self.cloud.file_info = info
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_completed_download_and_audit_survive_queue_finish_crash_without_another_delete(self):
        self.enqueue()
        with patch.object(worker, 'finish_cleanup_intent', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'completed')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])
        self.assertEqual(self.job()['status'], 'completed')

    def test_pending_sibling_waits_and_successful_latest_card_allows_recovery(self):
        self.enqueue()
        self.confirm('waiting', status='pending')
        self.drain()
        self.assertEqual(self.job()['status'], 'retry')
        self.assertEqual(self.cloud.reads, [])
        db.update_organize_confirmation('waiting', status='completed', result_json='{"moved":1}')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_disabled_policy_never_relabels_known_remaining_media(self):
        self.enqueue()
        db.update_download_request(self.request_id, gy_staging_cleanup_error='下载隔离目录仍有媒体、伴随或其他文件，已保留')
        self.rules.clean_empty = False
        self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'retained')
        self.assertEqual(db.count_download_requests_requiring_attention(), 1)
        self.assertEqual(self.cloud.reads, [])

    def test_policy_skip_does_not_need_a_usable_cloud_client(self):
        self.enqueue()
        self.rules.clean_empty = False
        self.assertTrue(self.writer_lock.acquire(blocking=False))
        try:
            with patch.object(worker, 'GuangYaClient', side_effect=AssertionError('no cloud client')), \
                    patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
                worker.reconcile_with_client(rules=self.rules, writer_lock=self.writer_lock)
        finally:
            self.writer_lock.release()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'skipped')

    def test_policy_skip_can_later_be_manually_cleaned_with_all_original_guards(self):
        from app.modules.download_staging_cleanup import cleanup_download_staging
        self.enqueue()
        self.rules.clean_empty = False
        self.drain()
        self.assertTrue(self.writer_lock.acquire(blocking=False))
        try:
            report = cleanup_download_staging(
                self.cloud, [self.request_id], source_ids={'stage'}, protected_ids={'0', 'source', 'target'},
            )
        finally:
            self.writer_lock.release()
        self.assertEqual(report['cleaned'], 1)
        self.assertNotIn('stage', db.list_protected_guangya_staging_ids())

    def test_worker_rejects_unheld_lock_before_any_state_or_cloud_operation(self):
        self.enqueue()
        with self.assertRaises(RuntimeError):
            worker.reconcile_with_client(self.cloud, rules=self.rules, writer_lock=self.writer_lock)
        self.assertEqual(self.job()['attempt_count'], 0)
        self.assertEqual(self.cloud.reads, [])

    def test_duplicate_cas_snapshot_cannot_defer_completed_intent(self):
        self.enqueue()
        old = queue.list_due_cleanup_intents()[0]
        self.drain()
        self.assertFalse(queue.defer_cleanup_intent(old, error='late busy tick'))
        self.assertEqual(self.job()['status'], 'completed')

    def test_read_error_retry_cap_is_bounded_to_one_hour(self):
        self.enqueue()
        # 退避从尝试前检查点计算，finish 会更新 updated_at；固定时间避免跨秒误报。
        with patch.object(db, 'now', return_value=db.now()), \
                patch.object(self.cloud, 'list_dir', side_effect=OSError('offline')):
            for _ in range(11):
                self.due_now()
                self.drain()
        from datetime import datetime
        job = self.job()
        delta = datetime.fromisoformat(job['next_attempt_at']) - datetime.fromisoformat(job['updated_at'])
        self.assertEqual(delta.total_seconds(), 3600)
        self.assertEqual(self.cloud.deleted, [])

    def test_unknown_audit_is_not_replayed_or_policy_hidden_even_if_row_was_reset(self):
        self.enqueue()
        db.add_organize_delete_audit(
            trigger='download_staging_cleanup', reason='simulated lost response', status='pending',
            file_id='stage', file_name='MF-case', parent_id='source',
        )
        self.rules.clean_empty = False
        self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'retained')
        self.assertEqual(db.count_download_requests_requiring_attention(), 1)
        self.assertEqual(self.cloud.reads, [])

    def test_rebinding_between_phase_close_and_cleanup_does_not_become_new_authority(self):
        self.enqueue()
        original = worker.complete_staging_confirmation_phase

        def rebind(expected):
            result = original(expected)
            db.update_download_request(self.request_id, gy_task_id='late-replacement')
            return result

        with patch.object(worker, 'complete_staging_confirmation_phase', side_effect=rebind):
            self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')
        self.assertEqual(db.get_download_request(self.request_id)['gy_task_id'], 'late-replacement')

    def test_replaced_success_card_during_audit_does_not_authorize_old_intent(self):
        self.enqueue()
        original = db.add_organize_delete_audit

        def newer_success(**kwargs):
            audit_id = original(**kwargs)
            self.confirm('newer-success', fingerprint='done')
            return audit_id

        with patch.object(db, 'add_organize_delete_audit', side_effect=newer_success):
            self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')

    def test_real_manager_holds_cross_process_writer_lock_and_duplicate_ticks_do_not_delete_twice(self):
        import subprocess
        import sys
        import threading

        from app.modules.organize_tasks import OrganizeTaskManager

        manager = OrganizeTaskManager()
        entered = threading.Event()
        release = threading.Event()
        original = self.cloud.list_dir

        def hold_first_read(id_):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test synchronization timeout')
            return original(id_)

        with patch.object(worker, 'get_organize_manager', return_value=manager), \
                patch.object(worker, 'GuangYaClient', return_value=self.cloud), \
                patch.object(worker, 'close_guangya_client'), \
                patch.object(worker.OrganizeRules, 'from_config', return_value=self.rules), \
                patch.object(manager, '_wake_download_tracker'), \
                patch.object(self.cloud, 'list_dir', side_effect=hold_first_read), \
                patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
            thread = None
            try:
                self.assertEqual(worker.schedule_staging_reconciliation(), 1)
                thread = manager._worker
                self.assertTrue(entered.wait(3))
                self.assertFalse(self.writer_lock.acquire(blocking=False))
                script = (
                    'import sys; import tests; from app.modules.process_lock import CrossProcessLock; '
                    'lock=CrossProcessLock("guangya-organize", directory=sys.argv[1]); '
                    'got=lock.acquire(blocking=False); lock.release() if got else None; '
                    'sys.exit(1 if got else 0)'
                )
                result = subprocess.run(
                    [sys.executable, '-c', script, str(self.test_db_path.parent)],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(worker.schedule_staging_reconciliation(), 0)
            finally:
                release.set()
                if thread is not None:
                    thread.join(timeout=5)
            self.assertEqual(self.cloud.deleted, ['stage'])
            self.assertEqual(self.job()['status'], 'completed')
            self.assertEqual(worker.schedule_staging_reconciliation(), 0)
        self.assertTrue(self.writer_lock.acquire(blocking=False))
        self.writer_lock.release()

    def test_legacy_discovery_failure_does_not_starve_durable_due_intents(self):
        self.enqueue()

        def launch(_operation, _reference, callback, **_kwargs):
            self.assertTrue(self.writer_lock.acquire(blocking=False))
            try:
                callback()
            finally:
                self.writer_lock.release()
            return {'ok': True}

        with patch.object(worker, 'discover_legacy_confirmation_cleanup', side_effect=RuntimeError('bad legacy page')), \
                patch.object(worker, 'get_organize_manager') as manager, \
                patch.object(worker, 'GuangYaClient', return_value=self.cloud), \
                patch.object(worker, 'close_guangya_client'), \
                patch.object(worker.OrganizeRules, 'from_config', return_value=self.rules), \
                patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
            manager.return_value._lock = self.writer_lock
            manager.return_value.start_operation.side_effect = launch
            worker.schedule_staging_reconciliation()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_cloud_identity_change_is_terminal_not_periodically_retried(self):
        self.enqueue()
        self.cloud.nodes['stage'].name = 'different-owner'
        self.drain()
        self.assertEqual(self.job()['status'], 'blocked')
        reads = list(self.cloud.reads)
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.cloud.reads, reads)

    def test_clean_title_execution_policy_survives_restart_and_current_enabled_rules(self):
        db.update_organize_confirmation('done', result_json=json.dumps({
            'moved': 1, 'download_staging_policy': 'retained',
        }))
        self.enqueue()
        self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'skipped')
        self.assertEqual(self.cloud.reads, [])

    def test_cleanup_failure_in_fast_path_does_not_poison_success_evidence_for_retry(self):
        from app.modules import organize_confirmations
        self.enqueue()
        stats = {'moved': 1, 'failed': 0}
        self.assertTrue(self.writer_lock.acquire(blocking=False))
        try:
            with patch('app.modules.organize_tasks.get_organize_manager') as manager, \
                    patch.object(self.cloud, 'list_dir', side_effect=OSError('offline')), \
                    patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
                manager.return_value._lock = self.writer_lock
                self.assertEqual(organize_confirmations._finalize_confirmed_downloads(
                    self.payload, self.cloud, stats, self.rules, confirmation_token='done',
                ), [self.request_id])
        finally:
            self.writer_lock.release()
        self.assertNotIn('empty_dir_cleanup_failed', stats)
        db.update_organize_confirmation('done', result_json=json.dumps(stats))
        self.assertEqual(self.job()['status'], 'retry')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_policy_skip_survives_queue_finish_crash_without_becoming_pending_again(self):
        self.enqueue()
        self.rules.clean_empty = False
        with patch.object(worker, 'finish_cleanup_intent', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'skipped')
        self.due_now()
        self.rules.clean_empty = True
        self.drain()
        self.assertEqual(self.job()['status'], 'retained')
        self.assertEqual(db.count_download_requests_requiring_attention(), 0)
        self.assertEqual(self.cloud.reads, [])

    def test_repository_policy_cas_cannot_overwrite_failed_even_without_an_audit_row(self):
        from app.repositories.download_staging import update_staging_cleanup
        db.update_download_request(self.request_id, gy_staging_cleanup_status='failed')
        row = dict(db.get_download_request(self.request_id))
        self.assertFalse(update_staging_cleanup(row, status='skipped', error='policy'))
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'failed')
        self.assertEqual(db.count_download_requests_requiring_attention(), 1)

    def test_worker_refreshes_permanent_source_at_final_audit_guard(self):
        self.enqueue()
        self.cloud.add_dir('child', 'stage')
        values = {}
        original = db.add_organize_delete_audit

        def protect(**kwargs):
            audit_id = original(**kwargs)
            values['GY_ORGANIZE_SOURCE_DIRS'] = '[{"id":"stage","name":"New permanent"}]'
            return audit_id

        with patch.object(db, 'add_organize_delete_audit', side_effect=protect):
            self.drain(config_values=values)
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.job()['status'], 'blocked')

    def test_disabled_policy_needs_exact_legacy_warning_file_success_evidence(self):
        self.enqueue()
        self.rules.clean_empty = False
        db.update_download_request(self.request_id, gy_staging_cleanup_error=(
            '隔离目录仍有 2 项未整理或未识别：video.mkv、unconfirmed.srt'
        ))
        self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'retained')
        self.assertEqual(db.count_download_requests_requiring_attention(), 1)
        self.assertEqual(self.cloud.reads, [])

    def test_worker_known_child_success_and_read_error_retries_only_remaining_root(self):
        self.enqueue()
        self.cloud.add_dir('child', 'stage')
        original = self.cloud.file_info

        def unavailable(id_):
            if self.cloud.deleted:
                raise OSError('next node metadata unavailable')
            return original(id_)

        with patch.object(self.cloud, 'file_info', side_effect=unavailable):
            self.drain()
        self.assertEqual(self.cloud.deleted, ['child'])
        self.assertEqual(self.job()['status'], 'retry')
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'retained')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['child', 'stage'])
        self.assertEqual(self.job()['status'], 'completed')
        with db.get_conn() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT file_id,status FROM organize_delete_audit ORDER BY id')],
                             [('child', 'success'), ('stage', 'success')])

    def test_known_child_checkpoint_survives_process_interrupt_between_nodes(self):
        self.enqueue()
        self.cloud.add_dir('child', 'stage')
        original = self.cloud.file_info

        def interrupt(id_):
            if self.cloud.deleted:
                raise KeyboardInterrupt
            return original(id_)

        with patch.object(self.cloud, 'file_info', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.drain()
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'retained')
        self.assertEqual(self.cloud.deleted, ['child'])
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['child', 'stage'])

    def test_provider_success_without_confirmed_success_audit_is_never_replayed(self):
        self.enqueue()
        self.cloud.add_dir('child', 'stage')
        original = db.update_organize_delete_audit

        def audit_unavailable(audit_id, **kwargs):
            if kwargs.get('status') == 'success':
                raise OSError('audit commit unavailable')
            return original(audit_id, **kwargs)

        with patch.object(db, 'update_organize_delete_audit', side_effect=audit_unavailable):
            self.drain()
        self.assertEqual(self.cloud.deleted, ['child'])
        self.assertEqual(db.get_download_request(self.request_id)['gy_staging_cleanup_status'], 'failed')
        self.assertEqual(self.job()['status'], 'blocked')
        self.due_now()
        self.drain()
        self.assertEqual(self.cloud.deleted, ['child'])
