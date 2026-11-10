"""首次提交和补目标必须复用同一执行核，同时保持各自公开合同。"""
from __future__ import annotations

import ast
import inspect
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.clients.qbittorrent import TorrentAddResult
from app.indexers import downloads
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database


class DownloadDispatchSingleTrackTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(downloads, 'guangya_download_credential_generation', return_value=1))
        self.serial = 0

    def request(self):
        self.serial += 1
        request_id, _ = db.create_download_request('single-track-'+str(self.serial), 'magnet', title='统一分发', source_value='magnet:?xt=urn:btih:'+'a'*40)
        return request_id

    def test_url_preview_is_read_only_and_changed_identity_never_dispatches(self):
        url = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Example'
        with patch.dict(os.environ, {'QB_URL': 'http://qb.invalid'}), \
             patch.object(dispatcher, '_submit_qb') as backend:
            preview, frozen = downloads.preview_download_url(url, 'qb')
            self.assertEqual(preview['title'], 'Example')
            self.assertNotIn('magnet:', json.dumps(preview))
            changed = 'magnet:?xt=urn:btih:' + 'c' * 40
            with self.assertRaises(downloads.DownloadSnapshotChanged):
                downloads.submit_download_url(changed, 'qb', origin='agent', expected_snapshot=frozen)
            backend.assert_not_called()
            with db.get_conn() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM download_requests').fetchone()[0], 0)

    def test_url_submit_preserves_partial_request_without_implicit_retry(self):
        url = 'magnet:?xt=urn:btih:' + 'c' * 40 + '&dn=Example'
        with patch.dict(os.environ, {'QB_URL': 'http://qb.invalid'}), \
             patch.object(downloads, 'GuangYaClient') as factory, \
             patch('socket.socket.connect', side_effect=AssertionError('no real download')), \
             patch.object(dispatcher, '_submit_qb', return_value={'ok': True, 'task_id': 'd' * 40}) as qb, \
             patch.object(dispatcher, '_submit_guangya', return_value={'ok': False, 'error': 'token=secret'}) as gy:
            factory.return_value.logged_in = True
            _, frozen = downloads.preview_download_url(url, 'both')
            first = downloads.submit_download_url(url, 'both', origin='agent', expected_snapshot=frozen)
            db.init_db()
            retry = downloads.submit_download_url(url, 'both', origin='agent', expected_snapshot=frozen)
            _, qb_snapshot = downloads.preview_download_url(url, 'qb')
            duplicate = downloads.submit_download_url(url, 'qb', origin='agent', expected_snapshot=qb_snapshot)
        qb.assert_called_once()
        gy.assert_called_once()
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['succeeded'], ['qb'])
        self.assertEqual(first['failed'], ['guangya'])
        self.assertEqual(retry['request_id'], first['request_id'])
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['failed'], [])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['request_id'], first['request_id'])
        self.assertNotIn('token=secret', json.dumps(first))
        row = db.get_download_request(first['request_id'])
        assert row is not None
        self.assertEqual((row['qb_status'], row['gy_status']), ('submitted', 'failed'))

    def test_url_preview_rejects_share_and_plain_page_without_backend_access(self):
        with patch.object(downloads, 'GuangYaClient') as client:
            for url in ('https://example.com/', 'https://guangyapan.com/s/test-share', 'magnet:?dn=missing'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    downloads.preview_download_url(url, 'guangya')
        client.assert_not_called()

    def test_url_terminal_request_allows_new_confirmed_submission(self):
        for serial, status in enumerate(('completed', 'failed', 'cancelled'), 1):
            with self.subTest(status=status), patch.dict(os.environ, {'QB_URL': 'http://qb.invalid'}), \
                 patch.object(dispatcher, '_submit_qb', return_value={'ok': True}) as qb:
                url = f'magnet:?xt=urn:btih:{serial:040x}'
                old = dispatcher.create_request(dispatcher.normalize_download_url(url), '', '', origin='agent')
                db.update_download_request(old['id'], targets='qb', status=status, qb_status=status)
                db.init_db()
                fresh = downloads.submit_download_url(url, 'qb', origin='agent')
                duplicate = downloads.submit_download_url(url, 'qb', origin='agent')
                self.assertTrue(fresh['created'])
                self.assertNotEqual(fresh['request_id'], old['id'])
                prior = db.get_download_request(old['id'])
                assert prior is not None
                self.assertEqual(prior['status'], status)
                self.assertTrue(duplicate['duplicate'])
                self.assertEqual(duplicate['request_id'], fresh['request_id'])
                qb.assert_called_once()

    def test_url_http_keeps_dispatcher_input_without_indexer_prefetch(self):
        url = 'https://fixture.invalid/download/Show.torrent?passkey=secret'
        with patch.dict(os.environ, {'QB_URL': 'http://qb.invalid'}), \
             patch.object(downloads, 'GuangYaClient') as client, \
             patch.object(downloads, 'prepare_download_input', side_effect=AssertionError('direct URL must not prefetch')), \
             patch.object(dispatcher, '_submit_qb', return_value={'ok': True}) as qb, \
             patch.object(dispatcher, '_submit_guangya', return_value={'ok': True}) as gy:
            client.return_value.logged_in = True
            result = downloads.submit_download_url(url, 'both', origin='agent')
        self.assertEqual(result['status'], 'submitted')
        row = db.get_download_request(result['request_id'])
        assert row is not None
        self.assertEqual((row['kind'], row['source_value'], row['torrent_data']), ('http', url, None))
        qb.assert_called_once()
        gy.assert_called_once()

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
                    assert row is not None
                    self.assertEqual(row['status'], expected_status)
                    self.assertEqual(outcome['status'], expected_status)
                    rows.append(row)
                    self.assertEqual(outcome['results'], {'guangya': result})
                    if additional:
                        self.assertTrue(outcome['handled'])
                        self.assertFalse(outcome['duplicate'])
                        assert row is not None
                        self.assertEqual((row['qb_status'], row['qb_task_id']), ('completed', 'kept-qb'))
                    else:
                        self.assertNotIn('handled', outcome)
                    logs = db.list_download_logs()
                    own = [log for log in logs if log['request_id'] == request_id]
                    self.assertEqual(len(own), 1)
                    self.assertEqual(own[0]['path'], '[magnet]')
                for field in ('gy_status','gy_task_id','gy_task_ids','gy_batch_count','gy_target_dir','gy_isolated','gy_staging_parent_dir','gy_expected_file_count'):
                    assert rows[0] is not None
                    assert rows[1] is not None
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
                get_download_request_result = db.get_download_request(request_id)
                assert get_download_request_result is not None
                self.assertEqual(get_download_request_result['gy_status'], 'manual_review')

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
        assert row is not None
        self.assertEqual((row['targets'], row['qb_status'], row['gy_status']), ('both','submitted','submitted'))

    def test_all_submission_paths_share_qb_route_and_frozen_config(self):
        runtime = {
            'url': 'http://frozen.invalid', 'username': 'frozen-user',
            'password': 'synthetic-password', 'api_key': 'synthetic-key', 'timeout': 7,
            'category': 'frozen-default', 'default_save_path': '/synthetic/frozen-default',
        }
        operations = {
            'first': dispatcher.dispatch_request,
            'missing': dispatcher.dispatch_missing_targets,
            'retry_in_place': dispatcher.resubmit_download_request,
            'successor': dispatcher.resubmit_download_request,
        }
        for path, operation in operations.items():
            for save_path, category in (('/synthetic/subscription', 'rss'), ('', '')):
                with self.subTest(path=path, save_path=save_path, category=category):
                    self.serial += 1
                    item = dispatcher.normalize_download_url(f'magnet:?xt=urn:btih:{self.serial:040x}')
                    request_id = dispatcher.create_request(item, '', 'route-test')['id']
                    if path in {'missing', 'retry_in_place'}:
                        db.update_download_request(
                            request_id, status='downloading', targets='both',
                            qb_status='failed' if path == 'retry_in_place' else '',
                            gy_status='downloading', gy_task_id='kept-cloud',
                        )
                    elif path == 'successor':
                        db.update_download_request(request_id, status='failed', targets='qb', qb_status='failed')
                    with (
                        patch.object(dispatcher, 'get', return_value=''),
                        patch.object(dispatcher, 'analyze_offline_url', return_value=SimpleNamespace(allowed=True)),
                        patch.object(dispatcher, 'QBittorrentClient') as factory,
                        patch.object(dispatcher, '_submit_guangya') as cloud,
                    ):
                        qb = factory.return_value
                        qb.add_torrent_detailed.return_value = TorrentAddResult(True)
                        result = operation(request_id, 'qb', qb_save_path=save_path,
                                           qb_category=category, qb_runtime_config=runtime)
                    self.assertTrue(result['ok'], result)
                    qb.add_torrent_detailed.assert_called_once()
                    self.assertEqual(qb.add_torrent_detailed.call_args.kwargs['save_path'], save_path)
                    self.assertEqual(qb.add_torrent_detailed.call_args.kwargs['category'], category)
                    self.assertEqual(factory.call_args.kwargs, {
                        'url': runtime['url'], 'username': runtime['username'],
                        'password': runtime['password'], 'api_key': runtime['api_key'], 'timeout': 7,
                    })
                    cloud.assert_not_called()
                    if path in {'missing', 'retry_in_place'}:
                        row = db.get_download_request(request_id)
                        assert row is not None
                        self.assertEqual((row['gy_status'], row['gy_task_id']), ('downloading', 'kept-cloud'))
                        self.assertEqual(result['request_id'], request_id)
                    elif path == 'successor':
                        self.assertNotEqual(result['request_id'], request_id)

    def test_both_cloud_recovery_paths_use_frozen_or_default_qb_connection(self):
        from tests.test_torrent_manifest import _torrent

        frozen = {'url': 'http://frozen.invalid', 'username': 'frozen-user',
                  'password': 'synthetic-password', 'api_key': 'synthetic-key', 'timeout': 7}
        live = {'url': 'http://live.invalid', 'username': 'live-user',
                'password': 'live-password', 'api_key': 'live-key', 'timeout': 10}
        for active_peer in (True, False):
            for runtime in (frozen, {}, None):
                with self.subTest(active_peer=active_peer, runtime=runtime):
                    self.serial += 1
                    payload = _torrent({b'name': f'fixture-{self.serial}.mkv'.encode(),
                                        b'length': 4096, b'piece length': 16384, b'pieces': b'x' * 20})
                    torrent = dispatcher.torrent_download_input('fixture.torrent', payload)
                    request_id = dispatcher.create_request(
                        dispatcher.normalize_download_url(torrent.source_value), '', 'cloud-recovery-test')['id']
                    torrent_id = dispatcher.parse_torrent_metadata(payload)[1]
                    db.update_download_request(
                        request_id, targets='both', status='submitted' if active_peer else 'completed',
                        qb_status='downloading' if active_peer else 'completed',
                        qb_task_id=torrent_id, gy_status='failed',
                    )
                    values = {'QB_' + key.upper(): value for key, value in live.items()}
                    with (
                        patch.object(dispatcher, 'get', side_effect=lambda key, default='': values.get(key, default)),
                        patch.object(dispatcher, 'analyze_offline_url', return_value=SimpleNamespace(allowed=True)),
                        patch('app.modules.organize_sources.list_nsfw_download_sources', return_value=[]),
                        patch.object(dispatcher, 'QBittorrentClient') as factory,
                        patch.object(dispatcher, '_submit_qb') as submit_qb,
                        patch.object(dispatcher, 'submit_offline', return_value={'ok': True, 'task_ids': ['cloud-task']}) as cloud,
                    ):
                        factory.return_value.export_torrent.return_value = payload
                        if runtime is not None:
                            result = dispatcher.resubmit_download_request(request_id, 'guangya', qb_runtime_config=runtime)
                        else:
                            result = dispatcher.resubmit_download_request(request_id, 'guangya')
                    self.assertTrue(result['ok'], result)
                    submit_qb.assert_not_called()
                    if runtime == {}:
                        factory.assert_not_called()
                        self.assertIsNone(cloud.call_args.kwargs['torrent_data'])
                    else:
                        factory.assert_called_once()
                        connection = dict(factory.call_args.kwargs)
                        connection.setdefault('timeout', 10)
                        self.assertEqual(connection, frozen if runtime is not None else live)
                        factory.return_value.export_torrent.assert_called_once_with(torrent_id)
                        factory.return_value.close.assert_called_once()
                        self.assertEqual(cloud.call_args.kwargs['torrent_data'], payload)
                    self.assertEqual(result['request_id'] == request_id, active_peer)

    def test_completed_request_requires_successor_instead_of_reopening_in_place(self):
        request_id = self.request()
        db.update_download_request(request_id, status='completed', targets='qb', qb_status='completed')
        with patch.object(dispatcher, '_submit_guangya') as backend:
            result = dispatcher.dispatch_missing_targets(request_id, 'guangya')
        backend.assert_not_called()
        self.assertFalse(result['handled'])
        self.assertTrue(result['duplicate'])
        get_download_request_result_2 = db.get_download_request(request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['status'], 'completed')

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
                    assert row is not None
                    self.assertEqual(row[field], 'outcome_unknown')
                    self.assertNotIn('fixture-secret', row['error'])
                    self.assertTrue(first['dispatch']['outcome_unknown'])
                    if target == 'qb':
                        assert row is not None
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
                assert row is not None
                self.assertEqual((row['gy_isolated'], row['gy_target_dir'], row['gy_staging_parent_dir'], row['gy_staging_name']), (1, 'owned-stage', 'chosen-parent', 'MF-audit'))
                matching = {'id': 'actual', 'target_dir': 'owned-stage', 'name': 'Different title'}
                other = {'id': 'other', 'target_dir': 'unrelated', 'name': row['title']}
                self.assertIs(DownloadTracker._match_gy(row, [other, matching]), matching)
                if additional:
                    assert row is not None
                    self.assertEqual((row['qb_status'], row['qb_task_id']), ('submitted', 'kept'))

    def test_qb_missing_receipt_uses_content_identity_not_another_same_title(self):
        from types import SimpleNamespace
        from app.modules.download_tracker import DownloadTracker

        request_id = self.request()
        row = db.get_download_request(request_id)
        assert row is not None
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
                    assert unchanged is not None
                    self.assertFalse(unchanged['qb_task_missing_since'])
                    self.assertEqual(unchanged['qb_status'], 'submitted')
                    tracker._update_request(unchanged, [], [], gy_available=False)
                    missing = db.get_download_request(request_id)
                    assert missing is not None
                    self.assertEqual(missing['qb_task_id'], expected)
                    self.assertTrue(missing['qb_task_missing_since'])
                    self.assertEqual(missing['qb_status'], 'submitted')
                    db.update_download_request(request_id, qb_task_missing_since='2000-01-01 00:00:00')
                    tracker._update_request(db.get_download_request(request_id), [], [], gy_available=False)
                get_download_request_result_3 = db.get_download_request(request_id)
                assert get_download_request_result_3 is not None
                self.assertEqual(get_download_request_result_3['qb_status'], 'manual_review')

    def test_legacy_qb_without_verifiable_identity_does_not_remain_submitted(self):
        from app.modules.download_tracker import DownloadTracker

        tracker = DownloadTracker()
        request_id = self.request()
        db.update_download_request(request_id, targets='qb', status='submitted', qb_status='submitted', source_value='')
        with patch.object(tracker, '_notify_completion'):
            tracker._update_request(db.get_download_request(request_id), [], [], gy_available=False)
        row = db.get_download_request(request_id)
        assert row is not None
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
        assert row is not None
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
        assert row is not None
        self.assertEqual((row['qb_task_id'], row['qb_status']), (identity, 'downloading'))

    def test_explicit_rejection_does_not_fabricate_backend_task_identity(self):
        request_id = self.request()
        with patch.object(dispatcher, '_submit_qb', return_value={'ok': False, 'error': 'rejected before submission'}):
            result = dispatcher.dispatch_request(request_id, 'qb')
        row = db.get_download_request(request_id)
        self.assertFalse(result['ok'])
        assert row is not None
        self.assertFalse(row['qb_task_id'])
        self.assertEqual(row['qb_status'], 'failed')

    def test_cloud_unknown_receipt_without_staging_never_claims_an_unrelated_same_title(self):
        from app.modules.download_tracker import DownloadTracker

        request_id = self.request()
        with patch.object(dispatcher, '_submit_guangya', side_effect=RuntimeError('lost response')):
            dispatcher.dispatch_request(request_id, 'guangya')
        row = db.get_download_request(request_id)
        assert row is not None
        other = {'id': 'unrelated', 'name': row['title'], 'target_dir': 'unrelated-dir',
                 'raw': {'url': 'magnet:?xt=urn:btih:' + 'b' * 40}, 'status': 2, 'progress': 1}
        tracker = DownloadTracker()
        with patch.object(tracker, '_notify_completion'), patch.object(tracker, '_update_backend_log'), patch.object(tracker, '_start_organize') as organize:
            tracker._update_request(row, [], [other], qb_available=False)
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row['gy_status'], 'manual_review')
        self.assertFalse(row['gy_task_id'])
        organize.assert_not_called()
