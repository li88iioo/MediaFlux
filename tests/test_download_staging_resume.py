"""移动后的隔离目录：真实SQLite交接，云端使用不联网的目录树。"""
from __future__ import annotations

import json
import socket
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.download_tracker import DownloadTracker, resume_relocated_staging
from app.modules.organize_rules import OrganizeRules
from app.repositories.download_staging import (
    list_staging_cleanup_requests, requeue_relocated_staging,
    staging_confirmation_summary, unresolved_staging_confirmations,
)
from tests.support import IsolatedDatabaseTestCase


class StagingResumeTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in ('download_request_keys', 'download_requests', 'organize_confirmations', 'organize_delete_audit', 'download_staging_reconcile'):
                conn.execute(f'DELETE FROM {table}')
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('unexpected network')))
        self.request, _ = db.create_download_request('moved-fixture', 'magnet')
        db.update_download_request(
            self.request, targets='guangya', status='completed', gy_status='completed',
            gy_isolated=1, gy_target_dir='stage', gy_target_name='MF-Fixture',
            gy_staging_name='MF-Fixture', gy_staging_parent_dir='old',
            gy_staging_cleanup_status='retained', organize_started=1,
            organize_status='requires_manual', organize_task_id='old-task',
            gy_settle_snapshot='old-snapshot', gy_settle_stable_count=2,
            gy_settle_attempts=4, gy_expected_file_count=1,
        )
        db.create_organize_confirmation(
            token='old-card', fingerprint='old-card', chat_id='test', source_name='Fixture',
            directory_path='/', payload={'source_dir_id': 'stage', 'organize_task_id': 'old-task',
            'download_request_ids': [self.request]}, organize_task_id='old-task',
            expires_at='2099-01-01 00:00:00', review_requested=True,
        )
        self.stage = GuangYaFile('stage', 'MF-Fixture', True, parent_id='new')
        self.video = GuangYaFile('video', 'Fixture.mp4', False, size=1000, parent_id='inner', etag='v1')
        self.tree = {'new': [self.stage], 'stage': [GuangYaFile('inner', 'nested', True, parent_id='stage')], 'inner': [self.video]}
        class Client:
            logged_in = True
            def file_info(_, file_id):
                return self.stage if file_id == 'stage' else None
            def list_dir(_, file_id):
                return self.tree.get(file_id, [])
            def close(_):
                pass
        self.client = Client()
        self.stats = {'protected_staging_dirs': [{'source_dir_id': 'new', 'file_id': 'stage', 'path': 'New/MF-Fixture', 'reason': '受保护'}], 'skip_reasons': ['受保护']}

    def resume(self):
        return resume_relocated_staging(self.client, self.stats, source_ids={'new'}, nsfw_source_ids={'new'})

    def test_handoff_expires_old_card_and_reuses_existing_tracker_after_fresh_settle(self):
        self.assertEqual(self.resume(), [self.request])
        row = db.get_download_request(self.request)
        assert row is not None
        self.assertEqual((row['gy_staging_parent_dir'], row['gy_requested_target_dir'], row['gy_requested_nsfw']), ('new', 'new', 1))
        self.assertEqual((row['organize_started'], row['organize_status'], row['gy_settle_stable_count'], row['gy_settle_snapshot']), (0, 'settling', 0, ''))
        self.assertIn('stage', db.list_protected_guangya_staging_ids())
        get_organize_confirmation_result = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result is not None
        self.assertEqual(get_organize_confirmation_result['status'], 'expired')
        with self.assertRaises(ValueError):
            db.claim_organize_confirmation('old-card', chat_id='test', selected_index=0)
        self.assertEqual(staging_confirmation_summary('stage')['pending'], 0)
        self.assertEqual(unresolved_staging_confirmations('stage'), 0)
        self.assertEqual(self.resume(), [])
        self.assertTrue(OrganizeRules(nsfw_enabled=True, nsfw_source_ids='["new"]').for_source('stage').nsfw_exclusive)
        tracker = DownloadTracker()
        with patch('app.modules.download_tracker.GuangYaClient', return_value=self.client), patch.object(tracker, '_publish_lifecycle'):
            self.assertFalse(tracker._staging_ready_for_organize(row))
            self.assertTrue(tracker._staging_ready_for_organize(db.get_download_request(self.request)))
        self.assertIn(self.request, [r['id'] for r in db.list_active_download_requests()])
        self.assertTrue(db.claim_download_request_organize(self.request))
        self.assertFalse(db.claim_download_request_organize(self.request))

    def test_active_unknown_or_cancelled_request_cannot_be_taken_over(self):
        get_download_request_result_4 = db.get_download_request(self.request)
        assert get_download_request_result_4 is not None
        original = dict(get_download_request_result_4)
        for fields in ({'gy_status': 'downloading'}, {'gy_status': 'manual_review'}, {'status': 'cancelled'},
                       {'organize_status': 'running'}, {'organize_started': -1},
                       {'gy_staging_cleanup_status': 'failed'}, {'attention_cleared_at': db.now()}):
            with self.subTest(fields=fields):
                with db.get_conn() as conn:
                    conn.execute('UPDATE download_requests SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', [*fields.values(), self.request])
                self.assertEqual(self.resume(), [])
                get_organize_confirmation_result_2 = db.get_organize_confirmation('old-card')
                assert get_organize_confirmation_result_2 is not None
                self.assertEqual(get_organize_confirmation_result_2['status'], 'pending')
                with db.get_conn() as conn:
                    conn.execute('UPDATE download_requests SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', [*(original[k] for k in fields), self.request])

    def test_claimed_or_failed_confirmation_blocks_atomic_transfer(self):
        for status in ('queued', 'running', 'failed', 'cancelled'):
            with self.subTest(status=status):
                db.update_organize_confirmation('old-card', status=status)
                self.assertEqual(self.resume(), [])
                get_download_request_result = db.get_download_request(self.request)
                assert get_download_request_result is not None
                self.assertEqual(get_download_request_result['gy_staging_parent_dir'], 'old')
        db.update_organize_confirmation('old-card', status='pending')
        self.assertEqual(self.resume(), [self.request])

    def test_identity_scope_and_parent_membership_must_agree(self):
        for field, value in (('file_id', 'different'), ('parent_id', 'other'), ('name', 'renamed')):
            with self.subTest(field=field):
                prior = getattr(self.stage, field)
                setattr(self.stage, field, value)
                self.assertEqual(self.resume(), [])
                setattr(self.stage, field, prior)
        self.tree['new'] = []
        self.assertEqual(self.resume(), [])
        self.assertEqual(resume_relocated_staging(self.client, self.stats, source_ids={'elsewhere'}, nsfw_source_ids=set()), [])
        get_organize_confirmation_result_3 = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result_3 is not None
        self.assertEqual(get_organize_confirmation_result_3['status'], 'pending')

    def test_stale_snapshot_and_ambiguous_owner_do_not_expire_confirmation(self):
        expected = list_staging_cleanup_requests(source_id='stage')[0]
        db.update_download_request(self.request, organize_task_id='new-task')
        self.assertFalse(requeue_relocated_staging(expected, parent_id='new', nsfw=True))
        db.update_download_request(self.request, organize_task_id='old-task')
        other, _ = db.create_download_request('ambiguous-fixture', 'magnet')
        db.update_download_request(other, gy_target_dir='stage', gy_isolated=1)
        self.assertEqual(self.resume(), [])
        get_organize_confirmation_result_4 = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result_4 is not None
        self.assertEqual(get_organize_confirmation_result_4['status'], 'pending')

    def test_ordinary_cancelled_card_is_not_hidden_by_relocation_resolution(self):
        db.update_organize_confirmation('old-card', status='cancelled', result_json=json.dumps({'resolution': 'cancelled'}))
        self.assertEqual(unresolved_staging_confirmations('stage'), 1)
        self.assertEqual(staging_confirmation_summary('stage')['failed'], 1)

    def test_transfer_and_card_cancellation_rollback_together_on_database_failure(self):
        with patch('app.repositories.download_requests._update_download_request_conn', side_effect=RuntimeError('fixture failure')):
            self.assertEqual(self.resume(), [])
        row = db.get_download_request(self.request)
        assert row is not None
        self.assertEqual((row['gy_staging_parent_dir'], row['gy_settle_snapshot']), ('old', 'old-snapshot'))
        get_organize_confirmation_result_5 = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result_5 is not None
        self.assertEqual(get_organize_confirmation_result_5['status'], 'pending')

    def test_wrong_owner_or_malformed_card_never_becomes_retired(self):
        with db.get_conn() as conn:
            original = conn.execute("SELECT payload_json FROM organize_confirmations WHERE token='old-card'").fetchone()[0]
        for payload in ('{bad-json', json.dumps({'source_dir_id': 'stage', 'download_request_ids': [self.request + 99]})):
            with self.subTest(payload=payload), db.get_conn() as conn:
                conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='old-card'", (payload,))
            self.assertEqual(self.resume(), [])
            get_organize_confirmation_result_6 = db.get_organize_confirmation('old-card')
            assert get_organize_confirmation_result_6 is not None
            self.assertEqual(get_organize_confirmation_result_6['status'], 'pending')
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='old-card'", (original,))
        self.assertEqual(self.resume(), [self.request])

    def test_relocation_does_not_wait_for_files_already_organized_from_original_pack(self):
        db.update_download_request(self.request, gy_expected_file_count=20)
        self.assertEqual(self.resume(), [self.request])
        get_download_request_result_2 = db.get_download_request(self.request)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['gy_expected_file_count'], 0)
        tracker = DownloadTracker()
        with patch('app.modules.download_tracker.GuangYaClient', return_value=self.client), patch.object(tracker, '_publish_lifecycle'):
            self.assertFalse(tracker._staging_ready_for_organize(db.get_download_request(self.request)))
            self.assertTrue(tracker._staging_ready_for_organize(db.get_download_request(self.request)))

    def test_queued_old_card_is_not_hidden_by_newer_card_with_same_fingerprint(self):
        db.claim_organize_confirmation('old-card', chat_id='test', selected_index=0)
        db.create_organize_confirmation(token='new-card', fingerprint='old-card', chat_id='test',
            source_name='Fixture', directory_path='/', payload={'source_dir_id': 'stage'}, expires_at='2099-01-01 00:00:00')
        self.assertEqual(self.resume(), [])
        get_organize_confirmation_result_7 = db.get_organize_confirmation('new-card')
        assert get_organize_confirmation_result_7 is not None
        self.assertEqual(get_organize_confirmation_result_7['status'], 'pending')

    def test_late_review_and_background_reconciliation_cannot_revive_retired_card(self):
        from app.repositories.download_staging_reconcile import reconcile_reorganized_confirmations
        db.update_organize_confirmation('old-card', review_status='running')
        self.assertEqual(self.resume(), [self.request])
        get_organize_confirmation_result_8 = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result_8 is not None
        self.assertEqual(get_organize_confirmation_result_8['review_status'], 'cancelled')
        with self.assertRaises(ValueError):
            db.claim_organize_confirmation('old-card', chat_id='test', selected_index=0, actor='agent')
        db.update_download_request(self.request, organize_status='completed', organize_started=1)
        with patch('app.repositories.download_staging_reconcile.requests_for_download_confirmation', side_effect=AssertionError('retired card selected')):
            self.assertEqual(reconcile_reorganized_confirmations(['stage']), [])
        get_organize_confirmation_result_9 = db.get_organize_confirmation('old-card')
        assert get_organize_confirmation_result_9 is not None
        self.assertEqual(get_organize_confirmation_result_9['status'], 'expired')

    def test_real_manager_reports_resume_and_leaves_actual_organizing_to_tracker(self):
        from app.modules import organize_tasks
        from app.modules.organize import Organizer
        from app.modules.telegram_organize_lifecycle import build_organize_lifecycle_event
        self.client.file_info = lambda file_id: self.stage if file_id == 'stage' else GuangYaFile(file_id, 'Fixture', True, parent_id='0')
        manager = organize_tasks.OrganizeTaskManager()
        run_id = db.add_task_run('guangya_organize', 'telegram', result=json.dumps({'task_id': 'fixture-scan'}))
        manager._task = {'id': 'fixture-scan', 'run_id': run_id, 'status': 'running', 'stats': {}}
        self.assertTrue(manager._lock.acquire(blocking=False))
        rules = OrganizeRules(target_dir_id='target', clean_empty=False, link_strm=False, notify_enabled=False,
                              nsfw_enabled=True, nsfw_source_ids='["new"]')
        with patch.object(Organizer, '_validate_target_outside_source'), patch.object(Organizer, 'trigger_post_actions'), patch.object(
            Organizer, 'notify_task_results'), patch.object(organize_tasks, '_publish_download_lifecycles'), patch.object(
            manager, '_wake_download_tracker'), patch.object(organize_tasks, 'resolve_organize_workers', return_value=1):
            manager._run('fixture-scan', run_id, [{'id': 'new', 'name': 'Fixture'}], rules, trigger_type='telegram', client=self.client)
        result = manager.task_status()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['result']['resumed_download_ids'], [self.request])
        self.assertIn('等待后台落稳核验', result['message'])
        self.assertEqual(result['stats']['total'], 0)
        get_download_request_result_3 = db.get_download_request(self.request)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3['organize_status'], 'settling')
        event = build_organize_lifecycle_event(result['stats'], source_name='Fixture', strm_status='未启用或无变更', media_refresh='未触发')
        self.assertIn('无需重复下达指令', str(event.footer))
        self.assertIn('后台续接', str(event.fields))
