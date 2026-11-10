"""下载隔离目录清理闭环：只在临时数据库与显式 fake 云盘运行。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.modules.download_staging_cleanup import cleanup_download_staging
from tests.support import IsolatedDatabaseTestCase


class _CloudTree:
    supports_guarded_empty_directory_delete = True

    def __init__(self):
        self.nodes = {
            'source': self._dir('source', '0', 'Source'),
            'stage': self._dir('stage', 'source', 'MF-case'),
        }
        self.children = {'source': ['stage'], 'stage': []}
        self.deleted = []
        self.reads = []
        self.on_delete = None

    @staticmethod
    def _dir(id_, parent, name):
        return SimpleNamespace(file_id=id_, parent_id=parent, name=name, is_dir=True,
                               etag='v1', updated_at=100, size=0)

    def add_dir(self, id_, parent):
        self.nodes[id_] = self._dir(id_, parent, id_)
        self.children.setdefault(parent, []).append(id_)
        self.children[id_] = []

    def add_file(self, id_, parent):
        self.nodes[id_] = SimpleNamespace(file_id=id_, parent_id=parent, name=id_,
                                         is_dir=False, size=100, etag='f1', updated_at=100)
        self.children.setdefault(parent, []).append(id_)

    def list_dir(self, id_):
        self.reads.append(id_)
        return [self.nodes[key] for key in self.children.get(id_, [])]

    def file_info(self, id_):
        # 模拟回收站详情仍然可读：不能靠file_info存在就重删同一ID。
        return self.nodes.get(id_)

    def delete_empty_directory(self, id_, **kwargs) -> bool:
        if self.on_delete:
            self.on_delete(id_)
        if self.children.get(id_):
            raise RuntimeError('directory became nonempty')
        parent = self.nodes[id_].parent_id
        if id_ not in self.children.get(parent, []):
            raise AssertionError('attempted to delete a recycled directory again')
        self.children[parent].remove(id_)
        self.deleted.append(id_)
        return True


class DownloadStagingCleanupLifecycleTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in ('download_request_keys', 'download_requests', 'organize_confirmations', 'organize_delete_audit'):
                conn.execute(f'DELETE FROM {table}')
        self.cloud = _CloudTree()
        self.request_id, _ = download_requests_repo.create_download_request('staging-cleanup-case', 'magnet')
        download_requests_repo.update_download_request(
            self.request_id, targets='guangya', status='completed', gy_status='completed', gy_isolated=1,
            gy_target_dir='stage', gy_staging_parent_dir='source', gy_staging_name='MF-case',
            gy_staging_cleanup_status='retained', gy_staging_cleanup_error='仍有1个文件',
            organize_started=1, organize_status='completed', organize_task_id='organize-case',
            gy_task_id='cloud-task', gy_task_ids='["cloud-task"]',
        )

    def clean(self, **kwargs):
        return cleanup_download_staging(self.cloud, [self.request_id],
                                        source_ids={'stage'}, protected_ids={'0', 'source', 'target'}, **kwargs)

    def add_confirmation(self, *, status='pending', source_id='stage'):
        token = 'confirmation-' + status
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id='test', source_name='MF-case',
            directory_path='/', payload={'source_dir_id': source_id, 'source_parent_id': source_id,
                                         'files': [{'file_id': 'video', 'parent_id': source_id}]},
            expires_at='2099-01-01 00:00:00',
        )
        db.update_organize_confirmation(token, status=status)
        return token

    def test_retained_completed_request_is_cleaned_and_second_call_does_not_delete_again(self):
        report = self.clean()
        self.assertEqual(report['cleaned'], 1)
        self.assertEqual(self.cloud.deleted, ['stage'])
        get_download_request_result = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result['gy_staging_cleanup_status'], 'completed')
        self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_nested_empty_chain_is_deleted_leaf_first(self):
        self.cloud.add_dir('season', 'stage')
        self.cloud.add_dir('inner', 'season')
        report = self.clean()
        self.assertEqual(report['cleaned'], 3)
        self.assertEqual(self.cloud.deleted, ['inner', 'season', 'stage'])
        self.assertIn('source', self.cloud.nodes)

    def test_nonmedia_file_prevents_any_staging_delete(self):
        self.cloud.add_dir('season', 'stage')
        self.cloud.add_file('subtitle.srt', 'season')
        report = self.clean()
        self.assertGreater(report['not_empty'], 0)
        self.assertEqual(self.cloud.deleted, [])
        get_download_request_result_2 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['gy_staging_cleanup_status'], 'retained')

    def test_pending_confirmation_blocks_even_when_cloud_is_empty(self):
        self.add_confirmation()
        report = self.clean()
        self.assertGreater(report['protected'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_completed_legacy_confirmation_without_parent_task_does_not_block_cleanup(self):
        self.add_confirmation(status='completed')
        self.assertEqual(self.clean()['cleaned'], 1)

    def test_active_cancelled_and_resubmitted_requests_are_protected(self):
        for change in ({'gy_status': 'downloading'}, {'status': 'cancelled'},
                       {'status': 'resubmitted'}, {'organize_status': 'resubmitted'},
                       {'organize_status': 'running'}, {'gy_staging_cleanup_status': 'failed'}):
            with self.subTest(change=change):
                download_requests_repo.update_download_request(self.request_id, status='completed', gy_status='completed',
                                           organize_status='completed', gy_staging_cleanup_status='retained')
                download_requests_repo.update_download_request(self.request_id, **change)
                self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_permanent_source_root_is_never_deleted(self):
        report = cleanup_download_staging(self.cloud, [self.request_id], source_ids={'stage'},
                                         protected_ids={'0', 'source', 'stage'})
        self.assertEqual(report['cleaned'], 0)
        self.assertGreater(report['protected'], 0)

    def test_identity_mismatch_and_detached_recycle_entry_are_retained(self):
        self.cloud.nodes['stage'].name = 'Other'
        self.assertEqual(self.clean()['cleaned'], 0)
        self.cloud.nodes['stage'].name = 'MF-case'
        self.cloud.children['source'] = []
        self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_missing_version_is_retained_before_any_write(self):
        self.cloud.nodes['stage'].etag = ''
        self.cloud.nodes['stage'].updated_at = 0
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_new_file_before_delete_is_not_removed_and_failed_attempt_is_not_replayed(self):
        def add(_id):
            self.cloud.add_file('new.mkv', 'stage')
        self.cloud.on_delete = add
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])
        get_download_request_result_3 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3['gy_staging_cleanup_status'], 'failed')
        self.cloud.on_delete = lambda _id: self.fail('failed write must not be retried')
        self.assertEqual(self.clean()['cleaned'], 0)

    def test_lost_response_after_recycle_is_not_replayed(self):
        original = self.cloud.delete_empty_directory
        def lose(id_, **kwargs):
            original(id_, **kwargs)
            raise TimeoutError('response lost')
        self.cloud.delete_empty_directory = lose
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertGreater(report['delete_failures'], 0)
        get_download_request_result_4 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4['gy_staging_cleanup_status'], 'failed')
        self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, ['stage'])

    def test_source_not_in_explicit_scope_is_not_touched(self):
        report = cleanup_download_staging(self.cloud, [self.request_id], source_ids={'other'}, protected_ids={'0'})
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.reads, [])

    def test_request_rebinding_during_preflight_prevents_old_delete_and_state_overwrite(self):
        original = self.cloud.list_dir
        def rebound(id_):
            if id_ == 'stage':
                download_requests_repo.update_download_request(self.request_id, gy_target_dir='new-stage', gy_status='submitting')
            return original(id_)
        self.cloud.list_dir = rebound
        self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])
        get_download_request_result_5 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5['gy_target_dir'], 'new-stage')
        get_download_request_result_6 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6['gy_staging_cleanup_status'], 'retained')

    def test_disabled_auto_cleanup_does_not_read_or_change_cloud(self):
        self.assertEqual(self.clean(enabled=False)['cleaned'], 0)
        self.assertEqual(self.cloud.reads, [])
        self.assertEqual(self.cloud.deleted, [])

    def test_root_move_after_preflight_prevents_deleting_any_descendant(self):
        self.cloud.add_dir('season', 'stage')
        from app.modules import download_staging_cleanup as module
        original = module.update_staging_cleanup
        def move(expected, **kwargs):
            ok = original(expected, **kwargs)
            if kwargs['status'] == 'failed' and ok:
                self.cloud.children['source'].remove('stage')
                self.cloud.nodes['stage'].parent_id = 'other'
                self.cloud.children['other'] = ['stage']
            return ok
        with patch.object(module, 'update_staging_cleanup', side_effect=move):
            report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_ancestor_move_after_preflight_prevents_deleting_empty_leaf(self):
        self.cloud.add_dir('season', 'stage')
        self.cloud.add_dir('leaf', 'season')
        from app.modules import download_staging_cleanup as module
        original = module.update_staging_cleanup
        def move(expected, **kwargs):
            ok = original(expected, **kwargs)
            if kwargs['status'] == 'failed' and ok:
                self.cloud.children['stage'].remove('season')
                self.cloud.nodes['season'].parent_id = 'other'
                self.cloud.children['other'] = ['season']
            return ok
        with patch.object(module, 'update_staging_cleanup', side_effect=move):
            report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_false_provider_result_is_not_success_and_is_not_replayed(self):
        self.cloud.delete_empty_directory = lambda *_args, **_kwargs: False
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(report['delete_failures'], 1)
        get_download_request_result_7 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_7 is not None
        self.assertEqual(get_download_request_result_7['gy_staging_cleanup_status'], 'failed')
        self.assertEqual(self.clean()['cleaned'], 0)

    def test_malformed_directory_flag_must_not_authorize_deletion(self):
        self.cloud.nodes['stage'].is_dir = 'false'
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_duplicate_directory_id_blocks_the_entire_scan(self):
        self.cloud.add_dir('season', 'stage')
        self.cloud.children['stage'].append('season')
        report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_conflicting_parent_membership_name_is_not_accepted(self):
        original = self.cloud.list_dir
        def wrong_name(id_):
            if id_ == 'source':
                return [SimpleNamespace(file_id='stage', parent_id='source', name='Other', is_dir=True)]
            return original(id_)
        self.cloud.list_dir = wrong_name
        self.assertEqual(self.clean()['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_root_move_during_audit_must_not_delete_descendant(self):
        self.cloud.add_dir('leaf', 'stage')
        original = db.add_organize_delete_audit
        def move_during_audit(**kwargs):
            audit_id = original(**kwargs)
            self.cloud.children['source'].remove('stage')
            self.cloud.nodes['stage'].parent_id = 'elsewhere'
            self.cloud.children['elsewhere'] = ['stage']
            return audit_id
        with patch.object(db, 'add_organize_delete_audit', side_effect=move_during_audit):
            report = self.clean()
        self.assertEqual(report['cleaned'], 0)
        self.assertEqual(self.cloud.deleted, [])

    def test_manual_outer_source_scope_survives_candidate_selection(self):
        from app.modules import organize_tasks
        self.cloud.nodes['selected'] = self.cloud._dir('selected', '0', 'Selected')
        self.cloud.children['selected'] = ['source']
        self.cloud.nodes['source'].parent_id = 'selected'
        original = organize_tasks.staging_roots_in_source
        def move_after_selection(*args, **kwargs):
            result = original(*args, **kwargs)
            self.cloud.children['selected'].remove('source')
            self.cloud.nodes['source'].parent_id = 'elsewhere'
            self.cloud.children['elsewhere'] = ['source']
            return result
        with patch.object(organize_tasks, 'staging_roots_in_source', side_effect=move_after_selection):
            report = organize_tasks.OrganizeTaskManager().clean_empty(
                [{'id': 'selected', 'name': 'Selected'}], client=self.cloud,
            )
        self.assertEqual(report['cleaned'], 0)
        self.assertTrue(report['partial'])
        self.assertEqual(self.cloud.deleted, [])

    def test_live_permanent_roots_added_during_audit_stop_provider(self):
        for setting in ('source', 'target', 'invalid_source'):
            with self.subTest(setting=setting):
                self.setUp()
                values = {}
                original = db.add_organize_delete_audit

                def change(**kwargs):
                    audit_id = original(**kwargs)
                    if setting == 'source':
                        values['GY_ORGANIZE_SOURCE_DIRS'] = '[{"id":"stage","name":"Permanent"}]'
                    elif setting == 'target':
                        values['GY_ORGANIZE_TARGET_DIR'] = 'stage'
                    else:
                        values['GY_ORGANIZE_SOURCE_DIRS'] = '{invalid'
                    return audit_id

                with patch('app.config.get', side_effect=lambda key, default='': values.get(key, default)), \
                        patch.object(db, 'add_organize_delete_audit', side_effect=change):
                    report = self.clean()
                self.assertEqual(self.cloud.deleted, [])
                self.assertTrue(report['_blocked'])

    def test_disabled_policy_does_not_hide_unreviewed_identity_scope_or_permission_errors(self):
        self.add_confirmation(status='completed')
        for error in ('目录身份与原请求记录不一致，已保留', '目录缺少有效版本信息，已保留',
                      '云盘读取权限不足', '下载目录已移出本次选择的来源范围，未执行清理'):
            with self.subTest(error=error):
                download_requests_repo.update_download_request(self.request_id, gy_staging_cleanup_status='retained',
                                           gy_staging_cleanup_error=error)
                self.clean(enabled=False)
                row = download_requests_repo.get_download_request(self.request_id)
                assert row is not None
                self.assertEqual(row['gy_staging_cleanup_status'], 'retained')
                self.assertEqual(row['gy_staging_cleanup_error'], error)
                self.assertEqual(self.cloud.reads, [])

    def test_confirmed_child_delete_then_pure_read_error_remains_recoverable(self):
        self.cloud.add_dir('child', 'stage')
        original = self.cloud.file_info

        def unavailable(id_):
            if self.cloud.deleted:
                raise OSError('temporary metadata error after confirmed child')
            return original(id_)

        with patch.object(self.cloud, 'file_info', side_effect=unavailable):
            report = self.clean()
        self.assertEqual(self.cloud.deleted, ['child'])
        self.assertTrue(report['_retryable'])
        get_download_request_result_8 = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result_8 is not None
        self.assertEqual(get_download_request_result_8['gy_staging_cleanup_status'], 'retained')
        with db.get_conn() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT file_id,status FROM organize_delete_audit')],
                             [('child', 'success')])
        self.clean()
        self.assertEqual(self.cloud.deleted, ['child', 'stage'])
