"""人工确认与下载隔离目录的真实业务状态衔接（临时DB / fake provider）。"""
from __future__ import annotations

from typing import Any

import json
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.modules import organize_confirmations as confirmations
from app.modules.organize import OrganizeRules
from app.repositories.download_staging import requests_for_download_confirmation
from tests.support import IsolatedDatabaseTestCase
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class DownloadConfirmationCleanupTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in ('download_request_keys', 'download_requests', 'organize_confirmations', 'organize_delete_audit'):
                conn.execute(f'DELETE FROM {table}')
        self.cloud = _CloudTree()
        self.request_id, _ = download_requests_repo.create_download_request('confirmation-cleanup', 'magnet')
        download_requests_repo.update_download_request(
            self.request_id, targets='guangya', status='completed', gy_status='completed',
            gy_isolated=1, gy_target_dir='stage', gy_staging_parent_dir='source', gy_staging_name='MF-case',
            gy_staging_cleanup_status='retained', organize_started=1,
            organize_status='requires_manual', organize_task_id='task-case',
            gy_task_id='cloud-task', gy_task_ids='["cloud-task"]',
        )
        self.payload: dict[str, Any] = {
            'source_dir_id': 'stage', 'source_parent_id': 'stage', 'source_name': 'MF-case', 'directory': '/',
            'files': [{'file_id': 'video', 'parent_id': 'stage'}],
        }
        self.rules = OrganizeRules(target_dir_id='target', clean_empty=True, link_strm=False)
        self.stats: dict[str, Any] = {'moved': 1, 'failed': 0, 'need_confirm': 0}
        self.add_confirmation('done', status='completed')

    def add_confirmation(self, token, *, status, source_id='stage'):
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id='test', source_name='MF-case', directory_path='/',
            payload={**self.payload, 'source_dir_id': source_id}, expires_at='2099-01-01 00:00:00',
        )
        db.update_organize_confirmation(token, status=status,
                                       result_json=json.dumps({'moved': 1, 'failed': 0, 'need_confirm': 0}))

    def finish(self, payload=None):
        with patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default):
            return confirmations._finalize_confirmed_downloads(
                payload or self.payload, self.cloud, self.stats, self.rules,
            )

    def test_completed_legacy_card_closes_original_request_and_cleans_its_exact_root(self):
        self.assertEqual(self.finish(), [self.request_id])
        row = download_requests_repo.get_download_request(self.request_id)
        assert row is not None
        self.assertEqual(row['organize_status'], 'completed')
        self.assertEqual(row['gy_staging_cleanup_status'], 'completed')
        self.assertEqual(self.cloud.deleted, ['stage'])
        self.assertEqual(self.stats['empty_dirs_cleaned'], 1)

    def test_other_pending_card_preserves_root_and_requires_manual_phase(self):
        self.add_confirmation('pending', status='pending')
        self.assertEqual(self.finish(), [self.request_id])
        get_download_request_result = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result['organize_status'], 'requires_manual')
        self.assertEqual(self.cloud.deleted, [])

    def test_new_payload_checks_business_request_and_task_without_notification_rollup(self):
        payload: dict[str, Any] = {**self.payload, 'download_request_ids': [self.request_id], 'organize_task_id': 'task-case'}
        self.assertEqual(self.finish(payload), [self.request_id])
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_wrong_explicit_request_or_task_does_not_fall_back_to_legacy_matching(self):
        for extra in ({'download_request_ids': [self.request_id + 1]}, {'download_request_ids': []},
                      {'download_request_ids': [True]}, {'organize_task_id': 'other-task'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.finish({**self.payload, **extra}), [])
        self.assertEqual(self.cloud.deleted, [])

    def test_same_title_cannot_resolve_a_different_source(self):
        payload: dict[str, Any] = {**self.payload, 'source_dir_id': 'other', 'source_name': 'MF-case'}
        self.assertEqual(requests_for_download_confirmation(payload), [])
        self.assertEqual(self.finish(payload), [])

    def test_cleanup_toggle_off_still_completes_confirmation_phase_without_cloud_cleanup(self):
        self.rules.clean_empty = False
        self.assertEqual(self.finish(), [self.request_id])
        get_download_request_result_2 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['organize_status'], 'completed')
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.cloud.reads, [])

    def test_nonempty_real_provider_view_prevents_filtered_scope_from_deleting(self):
        self.cloud.add_file('hidden-subtitle.srt', 'stage')
        self.finish()
        self.assertEqual(self.cloud.deleted, [])
        self.assertIn('其他文件', ' '.join(self.stats['empty_dir_cleanup_reasons']))

    def test_source_path_display_root_relative_absolute_and_unknown(self):
        cases = [
            ({'source_name': 'MF-case', 'directory': '/'}, 'MF-case'),
            ({'source_name': 'MF-case', 'directory': 'Season 1'}, 'MF-case/Season 1'),
            ({'source_name': 'MF-case', 'directory': '/待确认'}, '/待确认'),
            ({'source_name': '', 'directory': ''}, '路径未记录'),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                event = confirmations._confirmation_result_event(payload, {'title': 'Test'}, {'moved': 1})
                self.assertEqual(dict(event.fields)['源文件目录'], expected)

    def manual_clean(self, source_id='source'):
        from app.modules.organize_tasks import OrganizeTaskManager
        config_values = {'GY_ORGANIZE_SOURCE_DIRS': '[{"id":"source","name":"Source"}]'}
        with patch('app.modules.organize_tasks.OrganizeRules.from_config', return_value=self.rules), patch(
            'app.modules.organize_tasks.config.get', side_effect=lambda key, default='': config_values.get(key, default),
        ):
            return OrganizeTaskManager().clean_empty([{'id': source_id, 'name': 'Selected'}], client=self.cloud)

    def test_manual_button_reconciles_completed_legacy_confirmation_and_retained_root(self):
        report = self.manual_clean()
        self.assertTrue(report['ok'])
        self.assertFalse(report['partial'])
        self.assertEqual(report['cleaned'], 1)
        self.assertEqual(self.cloud.deleted, ['stage'])
        self.assertNotIn('_cleaned_roots', report)
        self.assertEqual(report['sources'][0]['cleaned'], 1)
        get_download_request_result_3 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3['gy_staging_cleanup_status'], 'completed')
        get_download_request_result_4 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4['organize_status'], 'completed')

    def test_manual_button_repeated_does_not_delete_recycle_entry_again(self):
        self.assertEqual(self.manual_clean()['cleaned'], 1)
        self.assertEqual(self.manual_clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_manual_button_with_pending_confirmation_explains_zero_without_deleting(self):
        self.add_confirmation('pending', status='pending')
        report = self.manual_clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(report['protected'], 1)
        self.assertTrue(report['reasons'])
        self.assertEqual(self.cloud.deleted, [])

    def test_manual_button_direct_active_root_or_descendant_cannot_bypass_protection(self):
        download_requests_repo.update_download_request(self.request_id, gy_status='downloading')
        self.cloud.add_dir('inner', 'stage')
        self.assertEqual(self.manual_clean('stage')['cleaned'], 0)
        self.assertEqual(self.manual_clean('inner')['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_manual_button_only_touches_selected_source(self):
        self.cloud.nodes['other'] = self.cloud._dir('other', '0', 'Other')
        self.cloud.children['other'] = []
        report = self.manual_clean('other')
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_manual_button_includes_completed_empty_root_when_it_is_selected_directly(self):
        self.assertEqual(self.manual_clean('stage')['cleaned'], 1)
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_download_candidate_persists_business_parent_without_organize_rollup(self):
        from app.modules.organize import Organizer
        group = {
            **self.payload,
            'candidates': [{'tmdb_id': '10', 'media_type': 'movie', 'title': 'Test', 'score': 1.0}],
        }
        stats = {'task_id': 'task-case', 'download_request_ids': [self.request_id]}
        with patch.object(Organizer, '_validated_task_confirmation_groups', return_value=([group], 1)), patch(
            'app.modules.organize_confirmations.publish_confirmation_event', return_value=True,
        ):
            self.assertTrue(Organizer.notify_task_confirmations(stats, self.rules, chat_id='test'))
        rows = db.list_organize_confirmations_for_task('task-case', chat_id='test')
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]['payload_json'])
        self.assertEqual(payload['download_request_ids'], [self.request_id])
        self.assertEqual(payload['organize_task_id'], 'task-case')
        self.assertNotIn('organize_rollup', payload)
        threads = confirmations._confirmation_notification_threads('token', payload, chat_id='test')
        self.assertEqual([item['topic'] for item in threads], ['confirmation'])

    def test_requires_manual_parent_does_not_publish_download_and_library_completed(self):
        from app.modules.telegram_download_lifecycle import (
            build_download_lifecycle_event,
        )
        with patch('app.modules.telegram_download_lifecycle.get_notification_thread_event', return_value=None):
            event = build_download_lifecycle_event(download_requests_repo.get_download_request(self.request_id))
        self.assertNotIn('下载与入库完成', str(event.title))
        self.assertEqual(dict(event.fields)['光鸭整理'], '需要确认')
        self.assertIn('候选卡', str(event.footer))

    def test_cancelled_parent_does_not_get_completed_or_trigger_cleanup(self):
        download_requests_repo.update_download_request(self.request_id, status='cancelled')
        self.assertEqual(self.finish(), [])
        self.assertEqual(self.cloud.deleted, [])
        get_download_request_result_5 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5['organize_status'], 'requires_manual')

    def test_legacy_failed_then_latest_completed_confirmation_can_close_staging(self):
        self.add_confirmation('old-failed', status='failed')
        self.add_confirmation('new-completed', status='completed')
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET fingerprint='same-file' WHERE token IN ('old-failed','new-completed')")
        self.assertEqual(self.finish(), [self.request_id])
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_download_business_task_without_rollup_is_not_scheduled_as_unapplied_organize_notice(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET organize_task_id='download-only' WHERE token='done'")
        tasks = db.list_unapplied_organize_confirmation_tasks()
        self.assertNotIn('download-only', [row['organize_task_id'] for row in tasks])

    def test_failed_completed_card_does_not_authorize_cleanup_without_successful_phase_close(self):
        self.stats['failed'] = 1
        db.update_organize_confirmation('done', result_json=json.dumps(self.stats))
        self.assertEqual(self.finish(), [self.request_id])
        get_download_request_result_6 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6['organize_status'], 'requires_manual')
        self.assertEqual(self.cloud.deleted, [])

    def test_known_legacy_task_mismatch_is_not_downgraded_to_ordinary_organize(self):
        payload: dict[str, Any] = {**self.payload, 'organize_task_id': 'old-task'}
        db.create_organize_confirmation(
            token='legacy-mismatch', fingerprint='legacy-mismatch', chat_id='test',
            source_name='MF-case', directory_path='/', payload=payload, expires_at='2099-01-01 00:00:00',
        )
        db.update_organize_confirmation('legacy-mismatch', status='running')
        with patch.object(confirmations.OrganizeRules, 'from_config', return_value=self.rules), patch.object(
            confirmations, 'organize_rules_snapshot_changes', return_value=[],
        ), patch.object(confirmations, 'GuangYaClient') as client_factory, patch.object(
            confirmations, '_dispatch_due_confirmation_delivery', return_value=False,
        ), self.assertRaises(ValueError):
            confirmations._execute_guangya_confirmation(
                'legacy-mismatch', payload, {'tmdb_id': '10', 'title': 'Test', 'media_type': 'movie'},
                selected_index=0, chat_id='test',
            )
        client_factory.assert_not_called()

    def test_explicitly_unsafe_completed_statistics_never_grant_cleanup(self):
        for extra in ({'scan_complete': False}, {'scan_limited': 1}, {'audit_failures': 1},
                      {'replacement_cleanup_failed': 1}, {'empty_dir_cleanup_failed': 1},
                      {'need_confirm': 1}, {'stopped': 1}):
            with self.subTest(extra=extra):
                self.stats: dict[str, Any] = {'moved': 1, 'failed': 0, 'need_confirm': 0, **extra}
                db.update_organize_confirmation('done', result_json=json.dumps(self.stats))
                self.assertEqual(self.finish(), [self.request_id])
                get_download_request_result_7 = download_requests_repo.get_download_request(self.request_id)
                assert get_download_request_result_7 is not None
                self.assertEqual(get_download_request_result_7['organize_status'], 'requires_manual')
                self.assertEqual(self.cloud.deleted, [])

    def _execute_with_probe_context(self, *, payload=None):
        """真实确认 orchestration + 假 Organizer/通知边界，不触发远端服务。"""
        from contextlib import ExitStack
        from types import SimpleNamespace

        from app.modules import organize_probe_notifications
        from app.modules.process_lock import CrossProcessLock

        payload = payload or self.payload
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=?,status='running' WHERE token='done'",
                         (json.dumps(payload),))
        writer_lock = CrossProcessLock('guangya-organize', directory=self.test_db_path.parent)
        self.assertTrue(writer_lock.acquire(blocking=False))
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(confirmations.OrganizeRules, 'from_config', return_value=self.rules))
                stack.enter_context(patch.object(confirmations, 'organize_rules_snapshot_changes', return_value=[]))
                stack.enter_context(patch.object(confirmations, 'GuangYaClient', return_value=self.cloud))
                stack.enter_context(patch.object(confirmations, 'close_guangya_client'))
                stack.enter_context(patch.object(confirmations, '_validate_snapshot'))
                stack.enter_context(patch.object(confirmations, '_resolve_guangya_confirmation_candidate', return_value=(
                    SimpleNamespace(), SimpleNamespace(), {}, 'tmdb',
                )))
                stack.enter_context(patch.object(confirmations, '_record_confirmation_learning', return_value=[]))
                stack.enter_context(patch.object(confirmations, 'FixedMatchScraper'))
                stack.enter_context(patch.object(confirmations, '_dispatch_due_confirmation_delivery', return_value=False))
                stack.enter_context(patch('app.modules.telegram_download_lifecycle.publish_download_lifecycle', return_value=False))
                stack.enter_context(patch('app.modules.organize_tasks.config.get', side_effect=lambda key, default='': default))
                manager = stack.enter_context(patch('app.modules.organize_tasks.get_organize_manager'))
                manager.return_value._lock = writer_lock
                organizer = stack.enter_context(patch.object(confirmations, 'Organizer', autospec=True))
                plans = [SimpleNamespace(file_id='video')]
                organizer.return_value.organize.side_effect = [(plans, {}), (plans, dict(self.stats))]
                builder = stack.enter_context(patch.object(
                    organize_probe_notifications, 'build_notification_context',
                    wraps=organize_probe_notifications.build_notification_context,
                ))
                confirmations._execute_guangya_confirmation(
                    'done', payload, {'tmdb_id': '10', 'title': 'Test', 'media_type': 'movie'},
                    selected_index=0, chat_id='test',
                )
                calls = organizer.return_value.organize.call_args_list
                self.assertNotIn('notification_context', calls[0].kwargs)
                self.assertIs(calls[1].kwargs['dry_run'], False)
                return calls[1].kwargs['notification_context'], builder.call_args.kwargs
        finally:
            writer_lock.release()

    def test_real_confirmation_probe_context_uses_checked_owner_not_payload_list_or_unrelated_parent(self):
        context, arguments = self._execute_with_probe_context(payload={
            **self.payload, 'organize_task_id': 'task-case',
            'download_request_ids': [self.request_id, self.request_id + 1000],
        })
        self.assertEqual(arguments['download_request_ids'], [self.request_id])
        self.assertEqual(arguments['confirmation_token'], 'done')
        self.assertEqual(arguments['task_id'], '')
        self.assertEqual(context['task_id'], '')
        self.assertTrue(context['notify_enabled'])
        self.assertFalse(any(item['topic'] == 'organize' for item in context['notification_threads']))
        self.assertTrue(any(item['topic'] == 'confirmation' for item in context['notification_threads']))
        get_organize_confirmation_result = db.get_organize_confirmation('done')
        assert get_organize_confirmation_result is not None
        persisted = json.loads(get_organize_confirmation_result['result_json'])
        self.assertEqual(persisted['download_staging_identity']['gy_task_id'], 'cloud-task')

    def test_real_confirmation_probe_context_preserves_existing_rollup_thread_only(self):
        context, _arguments = self._execute_with_probe_context(payload={
            **self.payload, 'organize_task_id': 'task-case', 'organize_rollup': {'total': 1},
        })
        self.assertEqual(context['task_id'], '')
        parents = [item['task_id'] for item in context['notification_threads'] if item['topic'] == 'organize']
        self.assertEqual(parents, ['task-case'])

    def test_real_confirmation_probe_context_respects_silent_and_rule_switches(self):
        for mode in ('silent', 'notify_disabled', 'topic_disabled'):
            with self.subTest(mode=mode):
                self.setUp()
                payload = dict(self.payload)
                if mode == 'silent':
                    payload['_notification_suppressed'] = True
                elif mode == 'notify_disabled':
                    self.rules.notify_enabled = False
                else:
                    self.rules.library_notify = False
                context, _arguments = self._execute_with_probe_context(payload=payload)
                self.assertEqual(context['notify_enabled'], mode == 'topic_disabled')
                self.assertFalse(context['topic_enabled'])
