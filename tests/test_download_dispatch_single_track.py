"""首次提交和补目标必须复用同一执行核，同时保持各自公开合同。"""
from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database


class DownloadDispatchSingleTrackTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.serial = 0

    def request(self):
        self.serial += 1
        request_id, _ = db.create_download_request('single-track-'+str(self.serial), 'magnet', title='统一分发', source_value='magnet:?xt=urn:btih:'+'a'*40)
        return request_id

    def test_entrypoints_only_claim_then_delegate_and_never_submit_directly(self):
        for function in (dispatcher.dispatch_request, dispatcher.dispatch_missing_targets):
            with self.subTest(entry=function.__name__):
                tree = ast.parse(inspect.getsource(function))
                calls = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
                self.assertEqual(calls.count('_dispatch_claimed_targets'), 1)
                self.assertNotIn('_safe_submit', calls)
                self.assertNotIn('_submit_qb', calls)
                self.assertNotIn('_submit_guangya', calls)

    def test_both_entrypoints_share_result_and_staging_projection(self):
        cases = (
            {'ok': True, 'task_id': 'gy-1', 'task_ids': ['gy-1', 'gy-2'], 'selected_count': 2,
             'decision': {'target_dir_id': 'stage', 'target_dir_name': '测试'},
             'staging': {'isolated': True, 'parent_id': 'root', 'name': 'stage'}},
            {'ok': False, 'partial_success': True, 'task_ids': ['gy-1'], 'error': '部分提交'},
            {'ok': False, 'error': '明确失败'},
        )
        for result in cases:
            with self.subTest(result=result):
                rows = []
                for additional in (False, True):
                    request_id = self.request()
                    if additional:
                        db.update_download_request(request_id, targets='qb', status='downloading', qb_status='completed', qb_task_id='kept-qb')
                    operation = dispatcher.dispatch_missing_targets if additional else dispatcher.dispatch_request
                    with patch.object(dispatcher, '_submit_guangya', return_value=result) as gy, patch.object(dispatcher, '_submit_qb') as qb:
                        outcome = operation(request_id, 'guangya', gy_target_dir='selected', gy_target_name='selected-name', log_path='[magnet]')
                    gy.assert_called_once()
                    qb.assert_not_called()
                    row = db.get_download_request(request_id)
                    # 用真实仓储业务结果补足旧 Mock-only 的 status 参数断言。
                    expected_status = (
                        'submitted' if result.get('ok') or result.get('partial_success')
                        else 'completed' if additional else 'failed'
                    )
                    self.assertEqual(row['status'], expected_status)
                    self.assertEqual(outcome['status'], expected_status)
                    rows.append(row)
                    self.assertEqual(outcome['results'], {'guangya': result})
                    if additional:
                        self.assertTrue(outcome['handled'])
                        self.assertFalse(outcome['duplicate'])
                        self.assertEqual((row['qb_status'], row['qb_task_id']), ('completed', 'kept-qb'))
                    else:
                        self.assertNotIn('handled', outcome)
                    logs = db.list_download_logs()
                    own = [log for log in logs if log['request_id'] == request_id]
                    self.assertEqual(len(own), 1)
                    self.assertEqual(own[0]['path'], '[magnet]')
                for field in ('gy_status','gy_task_id','gy_task_ids','gy_batch_count','gy_target_dir','gy_isolated','gy_staging_parent_dir','gy_expected_file_count'):
                    self.assertEqual(rows[0][field], rows[1][field], field)

    def test_duplicate_and_restart_do_not_repeat_backend(self):
        request_id = self.request()
        with patch.object(dispatcher, '_submit_qb', return_value={'ok': True, 'task_id': 'a'*40}) as backend:
            first = dispatcher.dispatch_request(request_id, 'qb')
            self.assertTrue(first['ok'])
            db.init_db()
            second = dispatcher.dispatch_request(request_id, 'qb')
            third = dispatcher.dispatch_missing_targets(request_id, 'qb')
        backend.assert_called_once()
        self.assertTrue(second['duplicate'])
        self.assertTrue(third['duplicate'])
        self.assertFalse(third['handled'])

    def test_late_completion_does_not_overwrite_recovered_state_in_either_entry(self):
        for additional in (False, True):
            with self.subTest(additional=additional):
                request_id = self.request()
                if additional:
                    db.update_download_request(request_id, targets='qb', status='downloading', qb_status='completed')
                def submit(_row, **_kwargs):
                    db.update_download_request(request_id, status='manual_review', gy_status='manual_review')
                    return {'ok': True, 'task_id': 'external-task'}
                operation = dispatcher.dispatch_missing_targets if additional else dispatcher.dispatch_request
                with patch.object(dispatcher, '_submit_guangya', side_effect=submit):
                    result = operation(request_id, 'guangya')
                self.assertTrue(result['stale_result'])
                self.assertTrue(result['outcome_unknown'])
                self.assertEqual(db.get_download_request(request_id)['gy_status'], 'manual_review')

    def test_same_request_can_add_only_the_missing_backend(self):
        request_id = self.request()
        with patch.object(dispatcher, '_submit_qb', return_value={'ok': True, 'task_id': 'a'*40}) as qb, patch.object(dispatcher, '_submit_guangya', return_value={'ok': True, 'task_id': 'gy-1'}) as gy:
            dispatcher.dispatch_request(request_id, 'qb')
            result = dispatcher.dispatch_missing_targets(request_id, 'both')
        qb.assert_called_once()
        gy.assert_called_once()
        self.assertEqual(result['succeeded'], ['guangya'])
        self.assertEqual(set(result['results']), {'guangya'})
        row = db.get_download_request(request_id)
        self.assertEqual((row['targets'], row['qb_status'], row['gy_status']), ('both','submitted','submitted'))

    def test_completed_request_requires_successor_instead_of_reopening_in_place(self):
        request_id = self.request()
        db.update_download_request(request_id, status='completed', targets='qb', qb_status='completed')
        with patch.object(dispatcher, '_submit_guangya') as backend:
            result = dispatcher.dispatch_missing_targets(request_id, 'guangya')
        backend.assert_not_called()
        self.assertFalse(result['handled'])
        self.assertTrue(result['duplicate'])
        self.assertEqual(db.get_download_request(request_id)['status'], 'completed')

    def test_unexpected_or_invalid_receipts_never_allow_a_second_submission(self):
        from app.indexers.downloads import submit_download_input

        for target in ('qb', 'guangya'):
            for receipt in ('raise', None, {}, {'ok': 'true'}):
                with self.subTest(target=target, receipt=receipt):
                    self.serial += 1
                    item = dispatcher.normalize_download_url(f'magnet:?xt=urn:btih:{self.serial:040x}')
                    calls = []

                    def accepted_then_lost(row, **kwargs):
                        calls.append(int(row['id']))
                        if receipt == 'raise':
                            raise RuntimeError('receipt failed: api_key=fixture-secret')
                        return receipt

                    with patch.object(dispatcher, '_submit_qb' if target == 'qb' else '_submit_guangya', side_effect=accepted_then_lost):
                        first = submit_download_input(item, target, origin='audit')
                        second = submit_download_input(item, target, origin='audit')
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(first['request_id'], second['request_id'])
                    row = db.get_download_request(first['request_id'])
                    field = 'qb_status' if target == 'qb' else 'gy_status'
                    self.assertEqual(row[field], 'outcome_unknown')
                    self.assertNotIn('fixture-secret', row['error'])
                    self.assertTrue(first['dispatch']['outcome_unknown'])
                    if target == 'qb':
                        self.assertEqual(row['qb_task_id'], f'{self.serial:040x}')

    def test_cloud_receipt_error_preserves_durable_staging_and_can_be_verified(self):
        from app.modules.download_tracker import DownloadTracker

        for additional in (False, True):
            with self.subTest(additional=additional):
                request_id = self.request()
                if additional:
                    db.update_download_request(request_id, targets='qb', status='submitted', qb_status='submitted', qb_task_id='kept')
                operation = dispatcher.dispatch_missing_targets if additional else dispatcher.dispatch_request

                def accepted_then_error(row, **kwargs):
                    self.assertTrue(db.bind_download_request_guangya_staging(
                        request_id, staging_id='owned-stage', parent_id='chosen-parent',
                        staging_name='MF-audit', target_name='Chosen / MF-audit',
                    ))
                    raise RuntimeError('receipt decoding failed')

                with patch.object(dispatcher, '_submit_guangya', side_effect=accepted_then_error):
                    result = operation(request_id, 'guangya')
                self.assertTrue(result['outcome_unknown'])
                row = db.get_download_request(request_id)
                self.assertEqual((row['gy_isolated'], row['gy_target_dir'], row['gy_staging_parent_dir'], row['gy_staging_name']), (1, 'owned-stage', 'chosen-parent', 'MF-audit'))
                matching = {'id': 'actual', 'target_dir': 'owned-stage', 'name': 'Different title'}
                other = {'id': 'other', 'target_dir': 'unrelated', 'name': row['title']}
                self.assertIs(DownloadTracker._match_gy(row, [other, matching]), matching)
                if additional:
                    self.assertEqual((row['qb_status'], row['qb_task_id']), ('submitted', 'kept'))

    def test_qb_missing_receipt_uses_content_identity_not_another_same_title(self):
        from types import SimpleNamespace
        from app.modules.download_tracker import DownloadTracker

        request_id = self.request()
        row = db.get_download_request(request_id)
        other = SimpleNamespace(hash='b' * 40, name=row['title'])
        correct = SimpleNamespace(hash='a' * 40, name='Renamed by downloader')
        self.assertIsNone(DownloadTracker._match_qb(row, [other]))
        self.assertIs(DownloadTracker._match_qb(row, [other, correct]), correct)
        self.assertIsNone(DownloadTracker._match_qb({'kind': 'magnet', 'title': row['title']}, [other]))
        self.assertIsNone(DownloadTracker._match_qb({**dict(row), 'qb_task_id': 'c' * 40}, [correct]))

    def test_legacy_qb_identity_recovery_and_missing_task_grace(self):
        from app.modules.download_tracker import DownloadTracker

        tracker = DownloadTracker()
        for source, expected in (
            ('magnet:?xt=urn:btih:' + 'a' * 40, 'a' * 40),
            ('magnet:?xt=urn:btmh:1220' + 'b' * 64, 'b' * 40),
        ):
            with self.subTest(source=source):
                request_id = self.request()
                db.update_download_request(request_id, targets='qb', status='submitted', qb_status='submitted', source_value=source)
                with patch.object(tracker, '_notify_completion'):
                    tracker._update_request(db.get_download_request(request_id), [], [], qb_available=False, gy_available=False)
                    unchanged = db.get_download_request(request_id)
                    self.assertFalse(unchanged['qb_task_missing_since'])
                    self.assertEqual(unchanged['qb_status'], 'submitted')
                    tracker._update_request(unchanged, [], [], gy_available=False)
                    missing = db.get_download_request(request_id)
                    self.assertEqual(missing['qb_task_id'], expected)
                    self.assertTrue(missing['qb_task_missing_since'])
                    self.assertEqual(missing['qb_status'], 'submitted')
                    db.update_download_request(request_id, qb_task_missing_since='2000-01-01 00:00:00')
                    tracker._update_request(db.get_download_request(request_id), [], [], gy_available=False)
                self.assertEqual(db.get_download_request(request_id)['qb_status'], 'manual_review')

    def test_legacy_qb_without_verifiable_identity_does_not_remain_submitted(self):
        from app.modules.download_tracker import DownloadTracker

        tracker = DownloadTracker()
        request_id = self.request()
        db.update_download_request(request_id, targets='qb', status='submitted', qb_status='submitted', source_value='')
        with patch.object(tracker, '_notify_completion'):
            tracker._update_request(db.get_download_request(request_id), [], [], gy_available=False)
        row = db.get_download_request(request_id)
        self.assertEqual(row['qb_status'], 'manual_review')
        self.assertIn('未返回可跟踪任务标识', row['error'])

    def test_qb_unknown_receipt_recovers_completion_from_exact_identity(self):
        from types import SimpleNamespace
        from app.modules.download_tracker import DownloadTracker

        tracker = DownloadTracker()
        request_id = self.request()
        with patch.object(dispatcher, '_submit_qb', side_effect=RuntimeError('lost receipt')):
            dispatcher.dispatch_request(request_id, 'qb')
        db.update_download_request(request_id, targets='both', gy_status='downloading', gy_task_id='peer')
        task = SimpleNamespace(hash='a' * 40, name='Downloader renamed title', progress=1, state='pausedUP')
        with patch.object(tracker, '_notify_completion'), patch.object(tracker, '_start_local_import') as organize:
            tracker._update_request(db.get_download_request(request_id), [task], [], gy_available=False)
        row = db.get_download_request(request_id)
        self.assertEqual((row['qb_status'], row['gy_status'], row['gy_task_id']), ('completed', 'downloading', 'peer'))
        organize.assert_called_once()

    def test_legacy_http_qb_recovers_identity_from_saved_torrent_without_refetch(self):
        from types import SimpleNamespace
        from app.modules.download_tracker import DownloadTracker

        tracker = DownloadTracker()
        payload = b'd4:infod6:lengthi10485760e4:name9:Movie.mkvee'
        identity = dispatcher.parse_torrent_metadata(payload)[1]
        request_id, _ = db.create_download_request('legacy-http', 'http',
            source_value='https://fixture.invalid/rotating.torrent', torrent_data=payload)
        db.update_download_request(request_id, targets='qb', status='submitted', qb_status='submitted')
        task = SimpleNamespace(hash=identity, name='Changed title', progress=0.5, state='downloading')
        with patch.object(tracker, '_update_backend_log'), patch.object(tracker, '_notify_completion'):
            tracker._update_request(db.get_download_request(request_id), [task], [], gy_available=False)
        row = db.get_download_request(request_id)
        self.assertEqual((row['qb_task_id'], row['qb_status']), (identity, 'downloading'))

    def test_explicit_rejection_does_not_fabricate_backend_task_identity(self):
        request_id = self.request()
        with patch.object(dispatcher, '_submit_qb', return_value={'ok': False, 'error': 'rejected before submission'}):
            result = dispatcher.dispatch_request(request_id, 'qb')
        row = db.get_download_request(request_id)
        self.assertFalse(result['ok'])
        self.assertFalse(row['qb_task_id'])
        self.assertEqual(row['qb_status'], 'failed')

    def test_cloud_unknown_receipt_without_staging_never_claims_an_unrelated_same_title(self):
        from app.modules.download_tracker import DownloadTracker

        request_id = self.request()
        with patch.object(dispatcher, '_submit_guangya', side_effect=RuntimeError('lost response')):
            dispatcher.dispatch_request(request_id, 'guangya')
        row = db.get_download_request(request_id)
        other = {'id': 'unrelated', 'name': row['title'], 'target_dir': 'unrelated-dir',
                 'raw': {'url': 'magnet:?xt=urn:btih:' + 'b' * 40}, 'status': 2, 'progress': 1}
        tracker = DownloadTracker()
        with patch.object(tracker, '_notify_completion'), patch.object(tracker, '_update_backend_log'), patch.object(tracker, '_start_organize') as organize:
            tracker._update_request(row, [], [other], qb_available=False)
        row = db.get_download_request(request_id)
        self.assertEqual(row['gy_status'], 'manual_review')
        self.assertFalse(row['gy_task_id'])
        organize.assert_not_called()
