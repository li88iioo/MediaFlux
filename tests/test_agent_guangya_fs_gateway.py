"""光鸭通用能力网关与确认写入链路测试。"""
from __future__ import annotations
from types import SimpleNamespace
from tests.cloud_test_support import snapshot as read_snapshot
from app.clients.guangya import GuangYaClient
from typing import cast
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock
from app import database as db
from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_fs_change
from tests.support import PagedDirectoryTestMixin, isolated_test_database

class FakeGatewayClient(PagedDirectoryTestMixin):

    def __init__(self):
        self.logged_in = True
        self.credential_generation = 31
        self.closed = False
        self.counter = 0
        self.directories: dict[str, list[GuangYaFile]] = {'0': [GuangYaFile('source', 'source', True, parent_id='0', etag='s'), GuangYaFile('target', 'target', True, parent_id='0', etag='t')], 'source': [GuangYaFile('rename', '广告-ABC.mp4', False, parent_id='source', size=100, etag='r', extension='mp4'), GuangYaFile('move', 'Move.mp4', False, parent_id='source', size=90, etag='m', extension='mp4'), GuangYaFile('trash', '垃圾残余', True, parent_id='source', etag='x')], 'target': [], 'trash': []}

    def list_dir(self, parent_id='0'):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def rename(self, file_id, new_name):
        item = self._pop(str(file_id))
        item.name = str(new_name)
        self.directories[item.parent_id].append(item)
        return True

    def move(self, file_ids, parent_id):
        for file_id in file_ids:
            item = self._pop(str(file_id))
            item.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(item)
        return True

    def copy(self, file_ids, parent_id):

        def clone(source, parent):
            self.counter += 1
            copied = deepcopy(source)
            copied.file_id = f'copied-{self.counter}'
            copied.parent_id = str(parent)
            self.directories.setdefault(str(parent), []).append(copied)
            if copied.is_dir:
                self.directories[copied.file_id] = []
                for child in self.list_dir(source.file_id):
                    clone(child, copied.file_id)
        for file_id in file_ids:
            source = self.file_info(str(file_id))
            if source is None:
                raise RuntimeError('missing')
            clone(source, parent_id)
        return f'copy-task-{self.counter}'

    def task_status(self, task_id):
        return {'data': {'status': 2, 'detail': {'code': 0}}}

    def delete(self, file_ids):
        for file_id in file_ids:
            item = self._pop(str(file_id))
            if item.is_dir:
                self.directories.pop(item.file_id, None)
        return True

    def create_dir(self, name, parent_id='0'):
        self.counter += 1
        file_id = f'created-{self.counter}'
        self.directories.setdefault(str(parent_id), []).append(GuangYaFile(file_id, str(name), True, parent_id=str(parent_id), etag='new'))
        self.directories[file_id] = []
        return file_id

    def close(self):
        self.closed = True
        return True

    def _pop(self, file_id: str) -> GuangYaFile:
        for items in self.directories.values():
            for index, item in enumerate(items):
                if item.file_id == file_id:
                    return items.pop(index)
        raise RuntimeError('missing')

class GuangYaFSGatewayTests(unittest.TestCase):

    def _query(self, client, **values):
        data = read_snapshot(client, **values)
        self._last_read = data
        self._facts = {item['file_id']: deepcopy(item) for item in [data['root'], *data['items']]}
        return SimpleNamespace(data=data)

    def _build_plan(self, client, **values):
        operations = deepcopy(values['operations'])
        for operation in operations:
            entry = getattr(self, '_facts', {}).get(operation.get('file_id'))
            if entry and 'expected' not in operation:
                operation['expected'] = {key: entry[key] for key in ('file_id', 'parent_id', 'name', 'is_dir', 'size', 'etag', 'updated_at')}
        values['operations'] = operations
        return guangya_fs_change.build_fs_change_plan(client, **values)

    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(mock.patch('socket.socket.connect', side_effect=AssertionError('禁止外联')))
        self.temp = tempfile.TemporaryDirectory()
        self._job_sequence = 0
        self.obs_dir = Path(self.temp.name) / 'observations'
        self.plan_dir = Path(self.temp.name) / 'changes'
        self.patches = [mock.patch.object(guangya_fs_change, '_directory', return_value=self.plan_dir), mock.patch.object(guangya_fs_change, 'get_web_secret', return_value='test-secret'), mock.patch.object(guangya_fs_change, '_owner_digest', side_effect=lambda owner: f'digest:{owner}')]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def _confirmed_plan(self, client: FakeGatewayClient, operation: dict, *, trigger_strm: bool=False, path: str='/source') -> dict:
        observed = self._query(client, path=path)
        entries = {item['name']: item for item in observed.data['items']}
        operation = dict(operation)
        source_name = str(operation.pop('source_name', ''))
        if source_name:
            operation['file_id'] = entries[source_name]['file_id']
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=trigger_strm, operations=[operation])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return plan

    def _execute_with_strm_scope(self, client: FakeGatewayClient, plan: dict, *, strm_source_ids: tuple[str, ...]=('source',), organize_source_ids: tuple[str, ...]=('source',), organize_target_id: str='target') -> tuple[dict, mock.Mock]:
        scheduler = mock.Mock()
        scheduler.trigger.return_value = {'ok': True}
        config_values = {'GY_ORGANIZE_SOURCE_DIRS': json.dumps([{'id': source_id, 'name': source_id} for source_id in organize_source_ids]), 'GY_ORGANIZE_TARGET_DIR': organize_target_id}

        def get_config(key, default=''):
            return config_values.get(key, default)
        with mock.patch('app.modules.scheduler.get_scheduler', return_value=scheduler), mock.patch('app.modules.strm.configured_strm_source_plans', return_value=([{'id': source_id, 'name': source_id} for source_id in strm_source_ids], '')), mock.patch('app.config.get', side_effect=get_config):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        return (result, scheduler)

    def _queued_payload(self, plan: dict, *, job_id: str='') -> dict:
        if not job_id:
            self._job_sequence += 1
            job_id = f'{self._job_sequence:032x}'
        guangya_fs_change.bind_fs_change_plan_job(plan['plan_id'], owner_digest='digest:owner', expected_fingerprint=plan['fingerprint'], job_id=job_id, queue_until_epoch=10 ** 12)
        return {'version': 1, 'plan_id': plan['plan_id'], 'plan_fingerprint': plan['fingerprint'], 'owner_digest': 'digest:owner', 'credential_generation': 31, 'job_id': job_id}

    def _recovery_plan(self, client, first=None):
        observation = read_snapshot(client, path='/source', recursive=True, max_items=100)
        refs = {item['file_id']: item['file_id'] for item in observation['items'] + [observation['root']]}
        changes = [first or {'op': 'rename', 'file_id': refs['rename'], 'new_name': 'A-done.mp4'}, {'op': 'rename', 'file_id': refs['move'], 'new_name': 'B-done.mp4'}]
        if 'source_id' in changes[0]:
            changes[0]['file_id'] = refs[changes[0].pop('source_id')]
        plan = self._build_plan(client, owner='owner', operations=changes, trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return (plan, self._queued_payload(plan))

    def test_recovery_reconciles_rename_without_replaying_and_continues_only_unstarted(self):

        class WorkerExit(BaseException):
            pass
        for crash_at in ('before_write', 'after_write', 'after_checkpoint', 'changed_after_checkpoint'):
            with self.subTest(crash_at=crash_at):
                client = FakeGatewayClient()
                plan, payload = self._recovery_plan(client)
                original_rename, original_save = (client.rename, guangya_fs_change._append_journal)
                calls = []

                def rename(file_id, name):
                    calls.append(file_id)
                    if crash_at == 'before_write':
                        raise WorkerExit()
                    original_rename(file_id, name)
                    if crash_at == 'after_write':
                        raise WorkerExit()
                    return True

                def save(plan_id, record):
                    original_save(plan_id, record)
                    row = (record.get('data') or {}).get('operation_item') or {}
                    if crash_at.endswith('checkpoint') and row.get('position') == 1 and (row.get('status') == 'completed'):
                        raise WorkerExit()
                with mock.patch.object(client, 'rename', side_effect=rename), mock.patch.object(guangya_fs_change, '_append_journal', side_effect=save):
                    with self.assertRaises(WorkerExit):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                self.assertEqual(calls, ['rename'])
                self.assertTrue(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=1)['resumable'])
                self.assertTrue(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=3)['resumable'])
                if crash_at == 'changed_after_checkpoint':
                    original_rename('rename', 'externally-changed.mp4')
                with mock.patch.object(client, 'rename', wraps=original_rename) as resumed:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                safe = crash_at in {'after_write', 'after_checkpoint'}
                self.assertEqual(result['requires_manual'], not safe)
                self.assertEqual(resumed.call_count, int(safe))
                self.assertEqual(result['stats']['renamed'], 2 if safe else 0)
                file_info_result = client.file_info('move')
                assert file_info_result is not None
                self.assertEqual(file_info_result.name, 'B-done.mp4' if safe else 'Move.mp4')
                self.assertEqual(result['operation_items'][1]['status'], 'completed' if safe else 'not_started')
                if safe:
                    resumed.assert_called_once_with('move', 'B-done.mp4')
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                    guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)

    def test_recovery_never_guesses_copy_trash_or_create_success_from_name_or_absence(self):

        class WorkerExit(BaseException):
            pass
        for operation, method, first in (('copy', 'copy', {'op': 'copy', 'source_id': 'rename', 'target_path': '/target'}), ('trash', 'delete', {'op': 'trash', 'source_id': 'trash'}), ('create_directory', 'create_dir', {'op': 'create_directory', 'parent_path': '/target', 'name': 'new-dir'})):
            with self.subTest(operation=operation):
                client = FakeGatewayClient()
                plan, payload = self._recovery_plan(client, first=dict(first))
                original = getattr(client, method)

                def crash(*args, **kwargs):
                    original(*args, **kwargs)
                    raise WorkerExit()
                with mock.patch.object(client, method, side_effect=crash):
                    with self.assertRaises(WorkerExit):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                with mock.patch.object(client, method, wraps=original) as replay, mock.patch.object(client, 'rename', wraps=client.rename) as rename:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                self.assertTrue(result['requires_manual'])
                self.assertEqual(result['operation_items'][0]['status'], 'unknown')
                self.assertEqual(result['operation_items'][1]['status'], 'not_started')
                replay.assert_not_called()
                rename.assert_not_called()

    def test_cancel_arriving_during_checkpoint_prevents_write_and_followup_sync(self):
        from app.modules.organize_tasks import OrganizeOperationCancelled
        for phase in ('write', 'sync'):
            with self.subTest(phase=phase):
                client = FakeGatewayClient()
                plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'Done.mp4'}, trigger_strm=phase == 'sync')
                payload = self._queued_payload(plan)
                cancelled = False
                append = guangya_fs_change._append_journal

                def checkpoint(plan_id, event):
                    nonlocal cancelled
                    append(plan_id, event)
                    data = event.get('data') or {}
                    if phase == 'write' and (data.get('operation_item') or {}).get('status') == 'unknown' or (phase == 'sync' and data.get('finalizing')):
                        cancelled = True

                def check():
                    if cancelled:
                        raise OrganizeOperationCancelled('cancelled while persisting')
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(guangya_fs_change, 'cloud_change_sources', return_value={'source': '/source'}), mock.patch.object(guangya_fs_change, 'trigger_cloud_changes') as sync, mock.patch.object(client, 'rename', wraps=client.rename) as rename:
                    with self.assertRaises(OrganizeOperationCancelled) as caught:
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1, cancel_check=check)
                self.assertEqual(rename.call_count, int(phase == 'sync'))
                sync.assert_not_called()
                row = caught.exception.operation_result['operation_items'][0]
                self.assertEqual(row['status'], 'completed' if phase == 'sync' else 'not_started')

    def test_checkpoint_io_is_linear_not_full_plan_per_item(self):
        journal_sizes = []
        for count in (20, 200):
            with self.subTest(count=count):
                client = FakeGatewayClient()
                client.directories['source'] = [GuangYaFile(f'file-{index}', f'Old-{index}.mkv', False, parent_id='source', size=1, etag=str(index)) for index in range(count)]
                obs = read_snapshot(cast(GuangYaClient, client), path='/source', recursive=True, max_items=500)
                operations = [{'op': 'rename', 'file_id': row['file_id'], 'new_name': f"Done-{row['file_id']}.mkv"} for row in obs['items']]
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                payload = self._queued_payload(plan)
                with mock.patch.object(guangya_fs_change, '_atomic_write', wraps=guangya_fs_change._atomic_write) as snapshot:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                self.assertFalse(result['partial'])
                self.assertEqual(snapshot.call_count, 2, '完整计划只能启动/终态写入，不能随N重复重写')
                journal_sizes.append(guangya_fs_change._journal_path(plan['plan_id']).stat().st_size)
        self.assertLess(journal_sizes[1], journal_sizes[0] * 12, '10倍条目量不应导致平方级写放大')

    def test_corrupt_or_unbound_checkpoint_journal_is_not_resumable(self):

        class WorkerExit(BaseException):
            pass
        client = FakeGatewayClient()
        plan, payload = self._recovery_plan(client)
        original = client.rename

        def crash(*args):
            original(*args)
            raise WorkerExit()
        with mock.patch.object(client, 'rename', side_effect=crash):
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        path = guangya_fs_change._journal_path(plan['plan_id'])
        original_bytes = path.read_bytes()
        for kind in ('truncated', 'missing_all_stages', 'tampered', 'wrong_job', 'missing_sequence'):
            with self.subTest(kind=kind):
                lines = original_bytes.splitlines(keepends=True)
                event = json.loads(lines[-1])
                if kind == 'truncated':
                    changed = original_bytes[:-1]
                elif kind == 'missing_all_stages':
                    offset = guangya_fs_change._read(plan['plan_id'])['execution']['journal_offset']
                    changed = original_bytes[:offset]
                else:
                    if kind == 'tampered':
                        event['data']['operation_item']['status'] = 'completed'
                    elif kind == 'wrong_job':
                        event['job_id'] = 'f' * 32
                        event['auth'] = guangya_fs_change._auth(event)
                    else:
                        event['sequence'] += 1
                        event['auth'] = guangya_fs_change._auth(event)
                    changed = b''.join(lines[:-1]) + (json.dumps(event) + '\n').encode()
                path.write_bytes(changed)
                self.assertFalse(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=1)['resumable'])
        path.write_bytes(original_bytes)
        self.assertTrue(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=1)['resumable'])

    def test_checkpoint_failure_before_provider_call_halts_without_claiming_a_write(self):
        client = FakeGatewayClient()
        plan, payload = self._recovery_plan(client)
        original_save = guangya_fs_change._append_journal

        def save(plan_id, record):
            row = (record.get('data') or {}).get('operation_item') or {}
            if row.get('position') == 1 and row.get('status') == 'unknown':
                raise OSError('disk unavailable')
            return original_save(plan_id, record)
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=save), mock.patch.object(client, 'rename', wraps=client.rename) as rename:
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        rename.assert_not_called()
        self.assertTrue(result['requires_manual'])
        self.assertTrue(all((item['status'] == 'not_started' for item in result['operation_items'])))
        self.assertEqual(result['stats']['audit_failures'], 1)

    def _interrupted_relocation(self, client, *, move_first, stop_before_second=False):

        class WorkerExit(BaseException):
            pass
        first = {'op': 'relocate', 'source_id': 'rename', 'target_path': '/target', 'new_name': 'Move.mp4' if move_first else 'Done.mp4'}
        plan, payload = self._recovery_plan(client, first=first)
        original = guangya_fs_change._append_journal

        def checkpoint(plan_id, event):
            original(plan_id, event)
            row = (event.get('data') or {}).get('operation_item') or {}
            expected_status = 'unknown' if stop_before_second else 'partial'
            if row.get('position') == 1 and row.get('status') == expected_status and (len(row.get('completed_actions') or []) == 1):
                raise WorkerExit()
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint):
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client, lease_generation=1)
        return (plan, payload)

    def test_verified_half_relocation_continues_only_remaining_stage(self):
        for move_first in (False, True):
            with self.subTest(move_first=move_first):
                client = FakeGatewayClient()
                plan, payload = self._interrupted_relocation(client, move_first=move_first)
                progress = guangya_fs_change._read_fs_checkpoints(guangya_fs_change._read(plan['plan_id']))
                self.assertEqual(progress['operation_items'][0]['completed_actions'], ['move' if move_first else 'rename'])
                self.assertEqual(progress['stage_sources']['1'], guangya_fs_change.file_snapshot(cast(GuangYaFile, client.file_info('rename'))))
                with mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                self.assertFalse(result['partial'])
                self.assertEqual((result['stats']['relocated'], result['stats']['renamed'], result['stats']['failed']), (1, 1, 0))
                self.assertEqual(moved.call_count, int(not move_first))
                self.assertEqual(renamed.call_count, 1 + int(move_first))
                file_info_result_2 = client.file_info('rename')
                assert file_info_result_2 is not None
                self.assertEqual(file_info_result_2.parent_id, 'target')
                file_info_result_3 = client.file_info('rename')
                assert file_info_result_3 is not None
                self.assertEqual(file_info_result_3.name, 'Move.mp4' if move_first else 'Done.mp4')
                self.assertNotIn('stage_sources', result)
                self.assertNotIn('parent_id', json.dumps(result))
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                    guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)

    def test_late_resume_preflight_failure_keeps_already_completed_stage(self):
        client = FakeGatewayClient()
        _plan, payload = self._interrupted_relocation(client, move_first=True)
        preflight = guangya_fs_change._preflight_operation
        resumed_checks = 0

        def mutate_before_write(*args, **kwargs):
            nonlocal resumed_checks
            if kwargs.get('source_snapshot') is not None:
                resumed_checks += 1
                if resumed_checks == 2:
                    current = client._pop('rename')
                    current.etag = 'changed-after-reconciliation'
                    client.directories[current.parent_id].append(current)
            return preflight(*args, **kwargs)
        with mock.patch.object(guangya_fs_change, '_preflight_operation', side_effect=mutate_before_write), mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
        self.assertTrue(result['partial'])
        row = result['operation_items'][0]
        self.assertEqual((row['status'], row['completed_actions'], row['reason']), ('partial', ['move'], 'precondition_failed'))
        self.assertEqual(result['stats']['precondition_failed'], 1)
        renamed.assert_called_once_with('move', 'B-done.mp4')

    def test_second_stage_intent_is_not_misclassified_as_unstarted(self):
        for move_first in (False, True):
            with self.subTest(move_first=move_first):
                client = FakeGatewayClient()
                plan, payload = self._interrupted_relocation(client, move_first=move_first, stop_before_second=True)
                with mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                self.assertTrue(result['requires_manual'])
                self.assertEqual(result['operation_items'][0]['reason'], 'verification_pending')
                self.assertEqual(result['operation_items'][1]['status'], 'not_started')
                moved.assert_not_called()
                renamed.assert_not_called()

    def test_half_relocation_does_not_hide_drift_behind_stale_listing(self):
        for drift in ('source_version', 'target_location'):
            with self.subTest(drift=drift):
                client = FakeGatewayClient()
                _plan, payload = self._interrupted_relocation(client, move_first=True)
                info = client.file_info

                def newer_info(file_id):
                    current = info(file_id)
                    if current is not None and file_id == 'rename' and (drift == 'source_version'):
                        current.updated_at += 1
                    if current is not None and file_id == 'target' and (drift == 'target_location'):
                        current.name = 'externally-moved-target'
                    return current
                with mock.patch.object(client, 'file_info', side_effect=newer_info), mock.patch.object(client, 'rename', wraps=client.rename) as rename:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                self.assertTrue(result['requires_manual'])
                self.assertEqual(result['operation_items'][0]['reason'], 'precondition_failed')
                rename.assert_not_called()

    def test_resumed_second_stage_intent_remains_unknown_after_another_crash(self):

        class WorkerExit(BaseException):
            pass
        client = FakeGatewayClient()
        plan, payload = self._interrupted_relocation(client, move_first=True)
        append = guangya_fs_change._append_journal

        def checkpoint(plan_id, event):
            append(plan_id, event)
            row = (event.get('data') or {}).get('operation_item') or {}
            if row.get('position') == 1 and row.get('status') == 'unknown':
                raise WorkerExit()
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(client, 'rename', wraps=client.rename) as rename:
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
            rename.assert_not_called()
        with mock.patch.object(client, 'rename', wraps=client.rename) as rename:
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=5)
        self.assertTrue(result['requires_manual'])
        self.assertEqual(result['operation_items'][0]['reason'], 'verification_pending')
        rename.assert_not_called()

    def test_half_relocation_rejects_drift_conflict_and_missing_evidence(self):
        for move_first in (False, True):
            for changed in ('source_name', 'source_content', 'source_size', 'source_time', 'source_identity', 'source_parent', 'target_name', 'target_conflict', 'missing_snapshot', 'rejected'):
                with self.subTest(move_first=move_first, changed=changed):
                    client = FakeGatewayClient()
                    plan, payload = self._interrupted_relocation(client, move_first=move_first)
                    if changed.startswith('source'):
                        item = client._pop('rename')
                        if changed == 'source_name':
                            item.name = 'externally-renamed.mkv'
                        if changed == 'source_content':
                            item.etag = 'externally-replaced-content'
                        if changed == 'source_size':
                            item.size += 1
                        if changed == 'source_time':
                            item.updated_at += 1
                        if changed == 'source_identity':
                            item.file_id = 'replaced-object'
                        if changed == 'source_parent':
                            item.parent_id = 'trash'
                        client.directories[item.parent_id].append(item)
                    elif changed == 'target_name':
                        client.rename('target', 'externally-renamed-folder')
                    elif changed == 'target_conflict':
                        client.directories['target'].append(GuangYaFile('collision', plan['operations'][0]['new_name'], False, parent_id='target', size=1, etag='collision'))
                    else:
                        stored = guangya_fs_change._read(plan['plan_id'])
                        state = guangya_fs_change._read_fs_checkpoints(stored)
                        if changed == 'missing_snapshot':
                            state.pop('stage_sources', None)
                        else:
                            state['operation_items'][0]['reason'] = 'write_rejected'
                        guangya_fs_change._reset_fs_checkpoint_cursor(plan['plan_id'], state)
                        guangya_fs_change.update_fs_change_plan_execution(plan['plan_id'], status='running', execution=state, expected_statuses={'running'}, expected_job_id=payload['job_id'], expected_lease_generation=1)
                        guangya_fs_change._write_fs_checkpoint(plan['plan_id'], payload['job_id'], state, item=plan['operations'][0], outcome=state['operation_items'][0])
                    with mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                    self.assertTrue(result['requires_manual'])
                    self.assertEqual(result['operation_items'][0]['status'], 'partial')
                    self.assertEqual(result['operation_items'][1]['status'], 'not_started')
                    moved.assert_not_called()
                    renamed.assert_not_called()

    def test_recovery_reports_half_relocation_without_replaying_or_extending_writes(self):

        class WorkerExit(BaseException):
            pass
        client = FakeGatewayClient()
        plan, payload = self._recovery_plan(client, first={'op': 'relocate', 'source_id': 'rename', 'target_path': '/target', 'new_name': 'Move.mp4'})
        original_move = client.move

        def crash(*args):
            original_move(*args)
            raise WorkerExit()
        with mock.patch.object(client, 'move', side_effect=crash):
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        with mock.patch.object(client, 'move', wraps=original_move) as moved, mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
        self.assertTrue(result['requires_manual'])
        self.assertEqual(result['operation_items'][0]['status'], 'partial')
        self.assertEqual(result['operation_items'][0]['completed_actions'], ['move'])
        self.assertEqual(result['operation_items'][1]['status'], 'not_started')
        moved.assert_not_called()
        renamed.assert_not_called()

    def test_recovery_keeps_verified_created_directory_identity_for_remaining_move(self):

        class WorkerExit(BaseException):
            pass
        client = FakeGatewayClient()
        obs = read_snapshot(cast(GuangYaClient, client), path='/source', recursive=True, max_items=100)
        ref = next((item['file_id'] for item in obs['items'] if item['file_id'] == 'move'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'new'}, {'op': 'move', 'file_id': ref, 'target_path': '/target/new'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        payload = self._queued_payload(plan)
        original_save = guangya_fs_change._append_journal

        def save(plan_id, record):
            original_save(plan_id, record)
            row = (record.get('data') or {}).get('operation_item') or {}
            if row.get('position') == 1 and row.get('status') == 'completed':
                raise WorkerExit()
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=save):
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        created_id = client.list_dir('target')[0].file_id
        with mock.patch.object(client, 'create_dir', side_effect=AssertionError('must not recreate')):
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
        self.assertFalse(result['partial'])
        self.assertEqual((result['stats']['created'], result['stats']['moved']), (1, 1))
        file_info_result_4 = client.file_info('move')
        assert file_info_result_4 is not None
        self.assertEqual(file_info_result_4.parent_id, created_id)

    def test_recovery_does_not_repeat_unknown_strm_submission(self):

        class WorkerExit(BaseException):
            pass
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'Done.mp4'}, trigger_strm=True)
        payload = self._queued_payload(plan)
        with mock.patch.object(guangya_fs_change, 'cloud_change_sources', return_value={'source': '/source'}), mock.patch.object(guangya_fs_change, 'trigger_cloud_changes', side_effect=WorkerExit):
            with self.assertRaises(WorkerExit):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        with mock.patch.object(guangya_fs_change, 'trigger_cloud_changes', side_effect=AssertionError('unknown sync replay')) as sync, mock.patch.object(client, 'rename', side_effect=AssertionError('rename replay')):
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
        self.assertTrue(result['requires_manual'])
        self.assertEqual(result['stats']['strm_scope_unknown'], 1)
        self.assertEqual(result['stats']['renamed'], 1)
        sync.assert_not_called()

    def test_verified_terminal_plan_recovers_queue_receipt_without_repeating_writes(self):
        client = FakeGatewayClient()
        plan, payload = self._recovery_plan(client)
        first = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        self.assertTrue(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=1)['resumable'])
        terminal_execution = guangya_fs_change._read(plan['plan_id'])['execution']
        client.logged_in = False
        client.credential_generation += 1
        with mock.patch.object(client, 'rename', side_effect=AssertionError('terminal replay')):
            recovered = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
        self.assertEqual(recovered, first)
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['execution'], {**terminal_execution, 'lease_generation': 3})
        self.assertFalse(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:someone-else', lease_generation=3)['resumable'])
        self.assertFalse(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest='digest:owner', lease_generation=1)['resumable'])

    def test_strm_trigger_skips_successful_unrelated_prefix_sibling_change(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('source-2', 'source-2', True, parent_id='0', etag='s2'))
        client.directories['source-2'] = [GuangYaFile('private', 'Private.mp4', False, parent_id='source-2', size=80, etag='p', extension='mp4')]
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': 'Private.mp4', 'new_name': 'Private-renamed.mp4'}, trigger_strm=True, path='/source-2')
        result, scheduler = self._execute_with_strm_scope(client, plan)
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 1)
        scheduler.trigger.assert_not_called()
        self.assertNotIn('strm_triggered', result['stats'])

    def _nested_strm_root_client(self):
        client = FakeGatewayClient()
        client.directories['0'].extend([GuangYaFile('media', '媒体', True, parent_id='0', etag='media'), GuangYaFile('other', 'other', True, parent_id='0', etag='other')])
        client.directories['media'] = [GuangYaFile('guoman', '国漫', True, parent_id='media', etag='guoman'), GuangYaFile('outside-media', '其他.mp4', False, parent_id='media', size=80, etag='outside', extension='mp4')]
        client.directories['guoman'] = []
        client.directories['other'] = []
        return client

    def test_directory_ancestor_of_strm_root_is_in_scope_but_parent_path_is_not(self):
        cases = (('rename', '/', '媒体', {'new_name': '媒体-改名'}, True), ('move', '/', '媒体', {'target_path': '/other'}, True), ('rename', '/媒体', '其他.mp4', {'new_name': '其他-改名.mp4'}, False))
        for op, path, source_name, extra, should_trigger in cases:
            with self.subTest(op=op, source_name=source_name):
                client = self._nested_strm_root_client()
                operation = {'op': op, 'source_name': source_name, **extra}
                plan = self._confirmed_plan(client, operation, trigger_strm=True, path=path)
                result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('guoman',), organize_source_ids=('0',), organize_target_id='')
                self.assertFalse(result['partial'])
                self.assertEqual(result['stats']['moved' if op == 'move' else 'renamed'], 1)
                if should_trigger:
                    scheduler.trigger.assert_called_once_with('organize', sync_mode='full', selected_source_ids=['guoman'])
                else:
                    scheduler.trigger.assert_not_called()

    def test_organize_input_sources_are_not_strm_scope(self):
        for organize_source_id in ('new-nsfw', '0'):
            with self.subTest(organize_source_id=organize_source_id):
                client = FakeGatewayClient()
                client.directories['0'].extend([GuangYaFile('整理', '整理', True, parent_id='0', etag='archive'), GuangYaFile('new-nsfw', 'NewNsfw', True, parent_id='0', etag='nsfw'), GuangYaFile('zhuxian', '诛仙', True, parent_id='0', etag='novel')])
                client.directories['整理'] = []
                client.directories['new-nsfw'] = [GuangYaFile('new-file', 'NewNsfw.mp4', False, parent_id='new-nsfw', size=80, etag='new', extension='mp4')]
                client.directories['zhuxian'] = [GuangYaFile('zhuxian-file', '诛仙.mp4', False, parent_id='zhuxian', size=80, etag='novel-file', extension='mp4')]
                for source_path, source_name in (('/NewNsfw', 'NewNsfw.mp4'), ('/诛仙', '诛仙.mp4')):
                    with self.subTest(source_path=source_path):
                        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': source_name, 'new_name': f'{source_name}.renamed.mp4'}, trigger_strm=True, path=source_path)
                        result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('整理',), organize_source_ids=(organize_source_id,), organize_target_id='')
                        self.assertFalse(result['partial'])
                        self.assertEqual(result['stats']['renamed'], 1)
                        scheduler.trigger.assert_not_called()

    def test_move_into_and_out_of_strm_source_still_triggers(self):
        for direction in ('in', 'out'):
            with self.subTest(direction=direction):
                client = FakeGatewayClient()
                client.directories['0'].extend([GuangYaFile('整理', '整理', True, parent_id='0', etag='archive'), GuangYaFile('other', 'other', True, parent_id='0', etag='other')])
                client.directories['整理'] = []
                client.directories['other'] = []
                if direction == 'in':
                    client.directories['other'].append(GuangYaFile('other-file', '待整理.mp4', False, parent_id='other', size=80, etag='in-file', extension='mp4'))
                    path, source_name, target_path = ('/other', '待整理.mp4', '/整理')
                else:
                    client.directories['整理'].append(GuangYaFile('archive-file', '已归档.mp4', False, parent_id='整理', size=80, etag='out-file', extension='mp4'))
                    path, source_name, target_path = ('/整理', '已归档.mp4', '/other')
                plan = self._confirmed_plan(client, {'op': 'move', 'source_name': source_name, 'target_path': target_path}, trigger_strm=True, path=path)
                result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('整理',), organize_source_ids=('new-nsfw',), organize_target_id='')
                self.assertFalse(result['partial'])
                self.assertEqual(result['stats']['moved'], 1)
                scheduler.trigger.assert_called_once_with('organize', sync_mode='full', selected_source_ids=['整理'])

    def test_copy_out_of_scope_does_not_trigger_for_unchanged_source(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('other', 'other', True, parent_id='0', etag='o'))
        client.directories['other'] = []
        plan = self._confirmed_plan(client, {'op': 'copy', 'source_name': 'Move.mp4', 'target_path': '/other'}, trigger_strm=True)
        result, scheduler = self._execute_with_strm_scope(client, plan)
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['copied'], 1)
        scheduler.trigger.assert_not_called()

    def test_strm_trigger_covers_move_into_and_out_of_configured_archive(self):
        for direction in ('out', 'in'):
            with self.subTest(direction=direction):
                client = FakeGatewayClient()
                client.directories['0'].append(GuangYaFile('other', 'other', True, parent_id='0', etag='o'))
                client.directories['other'] = []
                if direction == 'out':
                    plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Move.mp4', 'target_path': '/other'}, trigger_strm=True)
                else:
                    client.directories['other'].append(GuangYaFile('other-file', 'Other.mp4', False, parent_id='other', size=70, etag='o1', extension='mp4'))
                    plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Other.mp4', 'target_path': '/target'}, trigger_strm=True, path='/other')
                result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('source', 'target'))
                self.assertFalse(result['partial'])
                self.assertEqual(result['stats']['moved'], 1)
                scheduler.trigger.assert_called_once_with('organize', sync_mode='full', selected_source_ids=['source' if direction == 'out' else 'target'])
                self.assertEqual(result['stats']['strm_triggered'], 1)

    def test_organize_target_not_configured_as_strm_source_does_not_trigger(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('other', 'other', True, parent_id='0', etag='o'))
        client.directories['other'] = [GuangYaFile('other-file', 'Other.mp4', False, parent_id='other', size=70, etag='o1', extension='mp4')]
        plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Other.mp4', 'target_path': '/target'}, trigger_strm=True, path='/other')
        result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('source',), organize_target_id='target')
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 1)
        self.assertEqual(result['stats']['strm_trigger_skipped'], 1)
        scheduler.trigger.assert_not_called()

    def test_strm_trigger_covers_archive_root_object_and_root_destination(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('container', 'container', True, parent_id='0', etag='c'))
        client.directories['container'] = [GuangYaFile('archive', 'archive', True, parent_id='container', etag='a')]
        client.directories['archive'] = []
        plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'archive', 'target_path': '/'}, trigger_strm=True, path='/container')
        result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('source', 'archive'), organize_source_ids=(), organize_target_id='archive')
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 1)
        scheduler.trigger.assert_called_once_with('organize', sync_mode='full', selected_source_ids=['archive'])

    def test_new_empty_directory_never_triggers_strm(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'create_directory', 'parent_path': '/', 'name': 'root-created'}, trigger_strm=True, path='/')
        result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=('source',), organize_source_ids=(), organize_target_id='0')
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['created'], 1)
        scheduler.trigger.assert_not_called()
        self.assertEqual(result['stats']['strm_trigger_skipped'], 1)

    def test_only_successful_relevant_operations_can_trigger_strm(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('source-2', 'source-2', True, parent_id='0', etag='s2'))
        client.directories['source-2'] = [GuangYaFile('private', 'Private.mp4', False, parent_id='source-2', size=80, etag='p', extension='mp4')]
        observed = self._query(client, path='/', operation='tree', page_size=50)
        entries = {item['name']: item['file_id'] for item in observed.data['items']}
        operations = [{'op': 'rename', 'file_id': entries['广告-ABC.mp4'], 'new_name': 'ABC.mp4'}, {'op': 'rename', 'file_id': entries['Private.mp4'], 'new_name': 'Private-renamed.mp4'}]
        config_values = {'GY_ORGANIZE_SOURCE_DIRS': json.dumps([{'id': 'source', 'name': 'source'}]), 'GY_ORGANIZE_TARGET_DIR': 'target'}
        with mock.patch('app.modules.strm.configured_strm_source_plans', return_value=([{'id': 'source', 'name': 'source'}], '')), mock.patch('app.config.get', side_effect=lambda key, default='': config_values.get(key, default)):
            plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=True)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        scheduler = mock.Mock()
        scheduler.trigger.return_value = {'ok': True}
        original_rename = client.rename

        def rename(file_id, new_name):
            if str(file_id) == 'rename':
                raise GuangYaWriteRejected('rename', code='rejected')
            return original_rename(file_id, new_name)
        with mock.patch.object(client, 'rename', side_effect=rename), mock.patch('app.modules.scheduler.get_scheduler', return_value=scheduler), mock.patch('app.modules.strm.configured_strm_source_plans', return_value=([{'id': 'source', 'name': 'source'}], '')), mock.patch('app.config.get', side_effect=lambda key, default='': config_values.get(key, default)):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['failed'], 1)
        self.assertEqual(result['stats']['renamed'], 1)
        scheduler.trigger.assert_not_called()

    def test_scope_lookup_failure_keeps_real_write_result_without_triggering(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': 'Move.mp4', 'new_name': 'Move-renamed.mp4'}, trigger_strm=True)
        scheduler = mock.Mock()
        with mock.patch('app.modules.scheduler.get_scheduler', return_value=scheduler), mock.patch('app.modules.strm.configured_strm_source_plans', side_effect=RuntimeError('scope unavailable')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 1)
        self.assertEqual(result['stats']['strm_scope_unknown'], 1)
        scheduler.trigger.assert_not_called()

    def test_partly_unknown_source_paths_are_reconciled_without_scanning_known_unrelated_roots(self):
        from app.modules.strm import trigger_cloud_changes
        for paths, selected in ((['/A/child/one.mp4', '/B/child/two.mp4'], ['a', 'b']), (['/B/child/two.mp4'], ['b'])):
            with self.subTest(paths=paths):
                operations = [{'op': 'rename', 'source_path': path, 'source': {'file_id': str(index), 'parent_id': f'child-{index}'}} for index, path in enumerate(paths)]
                scheduler = mock.Mock()
                scheduler.trigger.return_value = {'ok': True}
                with mock.patch('app.modules.scheduler.get_scheduler', return_value=scheduler):
                    result = trigger_cloud_changes(operations, sources={'a': '/A', 'b': '', 'c': '/C'})
                self.assertEqual(result, {'strm_scope_unknown': 1, 'strm_triggered': 1})
                scheduler.trigger.assert_called_once_with('organize', sync_mode='full', selected_source_ids=selected)

    def test_trigger_strm_false_never_reads_or_triggers_scope(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': 'Move.mp4', 'new_name': 'Move-renamed.mp4'}, trigger_strm=False)
        scheduler = mock.Mock()
        with mock.patch('app.modules.scheduler.get_scheduler', return_value=scheduler), mock.patch('app.modules.strm.configured_strm_source_plans', side_effect=AssertionError('trigger_strm=False 不应读取 STRM 配置')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        scheduler.trigger.assert_not_called()

    def test_zero_credential_generation_is_a_valid_initial_generation(self):
        client = FakeGatewayClient()
        client.credential_generation = 0
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == '广告-ABC.mp4'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'rename', 'file_id': target['file_id'], 'new_name': 'ABC.mp4'}])
        self.assertEqual(plan['credential_generation'], 0)

    def test_plan_rejects_names_shared_by_multiple_operations(self):
        for kind in ('rename', 'rename_case', 'create_directory', 'move', 'copy', 'relocate', 'target_alias', 'transient_rename'):
            for reverse in (False, True):
                with self.subTest(kind=kind, reverse=reverse):
                    client = FakeGatewayClient()
                    client.directories['source'].append(GuangYaFile('other', 'Other', True, parent_id='source', etag='other'))
                    client.directories['other'] = [GuangYaFile('incoming', 'Shared.mp4', False, parent_id='other', size=123, etag='incoming')]
                    observed = self._query(client, operation='tree')
                    refs = {item['name']: item['file_id'] for item in observed.data['items']}
                    first = {'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'Shared.mp4'}
                    if kind in {'rename', 'rename_case'}:
                        second = {'op': 'rename', 'file_id': refs['Move.mp4'], 'new_name': 'SHARED.MP4' if kind == 'rename_case' else 'Shared.mp4'}
                    elif kind == 'create_directory':
                        second = {'op': kind, 'parent_path': '/SOURCE', 'name': 'Shared.mp4'}
                    elif kind == 'relocate':
                        second = {'op': kind, 'file_id': refs['Move.mp4'], 'target_path': '/SOURCE', 'new_name': 'Shared.mp4'}
                    elif kind == 'transient_rename':
                        if reverse:
                            continue
                        second = {'op': 'relocate', 'file_id': refs['Move.mp4'], 'target_path': '/target', 'new_name': 'Shared.mp4'}
                    elif kind == 'target_alias':
                        first.update(op='relocate', target_path='/target')
                        second = {'op': 'relocate', 'file_id': refs['Move.mp4'], 'target_path': '/TARGET//', 'new_name': 'Shared.mp4'}
                    else:
                        second = {'op': kind, 'file_id': refs['Shared.mp4'], 'target_path': '/SOURCE'}
                    operations = [second, first] if reverse else [first, second]
                    before = deepcopy(client.directories)
                    with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '重复名称'):
                        self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                    self.assertEqual(client.directories, before)
                    self.assertEqual(list(self.plan_dir.glob('*.json')), [], '冲突计划不能落成可确认计划')

    def test_same_planned_name_in_distinct_directories_remains_valid(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'relocate', 'file_id': refs['Move.mp4'], 'new_name': 'Shared.mp4', 'target_path': '/target'}, {'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'Shared.mp4'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        file_info_result_8 = client.file_info('rename')
        assert file_info_result_8 is not None
        self.assertEqual(file_info_result_8.name, 'Shared.mp4')
        file_info_result_9 = client.file_info('move')
        assert file_info_result_9 is not None
        self.assertEqual(file_info_result_9.name, 'Shared.mp4')
        file_info_result_10 = client.file_info('move')
        assert file_info_result_10 is not None
        self.assertEqual(file_info_result_10.parent_id, 'target')

    def test_created_destinations_under_distinct_case_sensitive_parents_do_not_collide(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('other-target', 'TARGET', True, parent_id='0', etag='other-target'))
        client.directories['other-target'] = []
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'New'}, {'op': 'create_directory', 'parent_path': '/TARGET', 'name': 'New'}, {'op': 'relocate', 'file_id': refs['广告-ABC.mp4'], 'target_path': '/target/New', 'new_name': 'Shared.mp4'}, {'op': 'relocate', 'file_id': refs['Move.mp4'], 'target_path': '/TARGET/New', 'new_name': 'Shared.mp4'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['relocated'], 2)
        file_info_result_11 = client.file_info('rename')
        assert file_info_result_11 is not None
        file_info_result_12 = client.file_info('move')
        assert file_info_result_12 is not None
        self.assertNotEqual(file_info_result_11.parent_id, file_info_result_12.parent_id)

    def test_relocate_checks_new_source_name_conflict_before_any_write(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'relocate', 'source_name': 'Move.mp4', 'new_name': 'Shared.mp4', 'target_path': '/target'})
        client.directories['source'].append(GuangYaFile('external', 'Shared.mp4', False, parent_id='source', etag='external'))
        before = deepcopy(client.directories)
        with mock.patch.object(client, 'rename', wraps=client.rename) as rename:
            with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
            rename.assert_not_called()
        self.assertEqual(client.directories, before)

    def test_old_signed_conflicting_rename_plan_stops_before_any_write(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'Shared.mp4'}, {'op': 'rename', 'file_id': refs['Move.mp4'], 'new_name': 'Distinct.mp4'}])
        plan['operations'][1]['new_name'] = 'SHARED.MP4'
        plan['fingerprint'] = guangya_fs_change._fingerprint(plan)
        guangya_fs_change._atomic_write(plan)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        before = deepcopy(client.directories)
        with mock.patch.object(client, 'rename', wraps=client.rename) as rename:
            with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '重复名称'):
                guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
            rename.assert_not_called()
        self.assertEqual(client.directories, before)

    def test_nested_directories_and_batch_relocate_share_one_frozen_plan(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        operations = [{'op': 'relocate', 'file_id': refs['广告-ABC.mp4'], 'target_path': '/target/Series/Season 01', 'new_name': 'Series - S01E01.mp4'}, {'op': 'relocate', 'file_id': refs['Move.mp4'], 'target_path': '/target/Series/Season 01', 'new_name': 'Series - S01E02.mp4'}, {'op': 'create_directory', 'parent_path': '/target/Series', 'name': 'Season 01'}, {'op': 'create_directory', 'parent_path': '/target', 'name': 'Series'}]
        normalized = {'operations': operations}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=normalized['operations'], trigger_strm=False)
        self.assertEqual(client.directories['target'], [], '预览不能创建目录或移动文件')
        self.assertEqual(plan['stats']['create_directory'], 2)
        self.assertEqual(plan['stats']['relocate'], 2)
        self.assertEqual([item['name'] for item in plan['operations'][:2]], ['Series', 'Season 01'])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        payload = self._queued_payload(plan)
        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['created'], 2)
        self.assertEqual(result['stats']['relocated'], 2)
        series = client.directories['target'][0]
        season = client.directories[series.file_id][0]
        self.assertEqual(season.name, 'Season 01')
        self.assertEqual({item.name for item in client.directories[season.file_id]}, {'Series - S01E01.mp4', 'Series - S01E02.mp4'})
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))

    def test_failed_parent_creation_blocks_nested_create_and_move(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        reference = observed.data['items'][0]['file_id']
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'Series'}, {'op': 'create_directory', 'parent_path': '/target/Series', 'name': 'Season 01'}, {'op': 'move', 'file_id': reference, 'target_path': '/target/Series/Season 01'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        before = deepcopy(client.directories)
        with mock.patch.object(client, 'create_dir', side_effect=GuangYaWriteRejected('拒绝', code='denied')) as create:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result['stats']['failed'], 3)
        self.assertEqual(result['stats']['precondition_failed'], 2)
        self.assertEqual(client.directories, before)

    def test_changed_created_ancestor_blocks_remaining_dependent_writes(self):
        for change_after in (1, 2):
            with self.subTest(change_after=change_after):
                client = FakeGatewayClient()
                observed = self._query(client)
                reference = next((item['file_id'] for item in observed.data['items'] if item['name'] == 'Move.mp4'))
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'Series'}, {'op': 'create_directory', 'parent_path': '/target/Series', 'name': 'Season 01'}, {'op': 'move', 'file_id': reference, 'target_path': '/target/Series/Season 01'}])
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                preflight = guangya_fs_change._preflight_operation
                changed = False

                def change_ancestor(client, item, **kwargs):
                    nonlocal changed
                    created = kwargs.get('created_targets')
                    if created is not None and len(created) == change_after and (not changed):
                        changed = True
                        ancestor = client.directories['target'][0]
                        client.rename(ancestor.file_id, 'ChangedByOtherActor')
                        client.move([ancestor.file_id], 'source')
                    return preflight(client, item, **kwargs)
                with mock.patch.object(guangya_fs_change, '_preflight_operation', side_effect=change_ancestor):
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertTrue(changed)
                self.assertEqual(result['stats']['created'], change_after)
                self.assertEqual(result['stats']['moved'], 0)
                self.assertEqual(result['stats']['precondition_failed'], 3 - change_after)
                file_info_result_13 = client.file_info('move')
                assert file_info_result_13 is not None
                self.assertEqual(file_info_result_13.parent_id, 'source')

    def test_nested_create_orders_root_parent_before_reversed_children(self):
        client = FakeGatewayClient()
        self._query(client)
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/Library/Series', 'name': 'Season 01'}, {'op': 'create_directory', 'parent_path': '/Library', 'name': 'Series'}, {'op': 'create_directory', 'parent_path': '/', 'name': 'Library'}])
        self.assertEqual([item['name'] for item in plan['operations']], ['Library', 'Series', 'Season 01'])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertEqual(result['stats']['created'], 3)
        self.assertEqual(result['stats']['failed'], 0)

    def test_created_subdirectory_conflicts_with_incoming_same_name(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '重复名称'):
            self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'Series'}, {'op': 'create_directory', 'parent_path': '/target/Series', 'name': 'Move.mp4'}, {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/target/Series'}])
        self.assertEqual(client.directories['target'], [])

    def test_batch_relocate_can_target_directory_created_in_same_plan(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        normalized = {'operations': [{'op': 'relocate', 'file_id': refs['广告-ABC.mp4'], 'target_path': '/target/Series', 'new_name': 'Series - E01.mp4'}, {'op': 'relocate', 'file_id': refs['Move.mp4'], 'target_path': '/target/Series', 'new_name': 'Series - E02.mp4'}, {'op': 'create_directory', 'parent_path': '/target', 'name': 'Series'}], 'trigger_strm': False}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=normalized['operations'], trigger_strm=False)
        self.assertEqual(plan['stats']['total'], 3)
        self.assertEqual(plan['stats']['create_directory'], 1)
        self.assertEqual(plan['stats']['relocate'], 2)
        self.assertEqual(plan['operations'][0]['op'], 'create_directory')
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        created = next((item for item in client.directories['target'] if item.name == 'Series'))
        self.assertEqual(sorted((item.name for item in client.directories[created.file_id])), ['Series - E01.mp4', 'Series - E02.mp4'])

    def _folder_move_with_child_renames(self, *, count=1, parent_op='move'):
        client = FakeGatewayClient()
        client.directories['source'] = []
        for index in range(count):
            folder, video = (f'folder-{index}', f'video-{index}')
            client.directories['source'].append(GuangYaFile(folder, f'Code-{index}', True, parent_id='source', etag='before'))
            client.directories[folder] = [GuangYaFile(video, f'site.example@Code-{index}.mp4', False, parent_id=folder, size=100, etag='content'), GuangYaFile(f'poster-{index}', 'poster.jpg', False, parent_id=folder, size=5, etag='poster')]
        observed = self._query(client, operation='tree', page_size=50)
        entries = {item['name']: item['file_id'] for item in observed.data['items']}
        operations = []
        for index in range(count):
            operations.append({'op': parent_op, 'file_id': entries[f'Code-{index}'], **({'target_path': '/target'} if parent_op == 'move' else {})})
            operations.append({'op': 'rename', 'file_id': entries[f'site.example@Code-{index}.mp4'], 'new_name': f'Code-{index}.mp4'})
        plan = self._build_plan(client, owner='owner', operations=operations, trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return (client, plan)

    def test_clean_children_then_move_ten_original_folders_in_one_plan(self):
        client, plan = self._folder_move_with_child_renames(count=10)
        self.assertEqual(plan['stats']['rename'], 10)
        self.assertEqual(plan['stats']['move'], 10)
        self.assertEqual([item['op'] for item in plan['operations']], ['rename'] * 10 + ['move'] * 10)
        original_rename = client.rename

        def rename(file_id, new_name):
            original_rename(file_id, new_name)
            info = client.file_info(file_id)
            assert info is not None
            folder = next((item for item in client.directories['source'] if item.file_id == info.parent_id))
            folder.etag = 'after-own-rename'
            folder.updated_at += 1
            return True
        with mock.patch.object(client, 'rename', side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 10)
        self.assertEqual(result['stats']['moved'], 10)
        self.assertEqual(client.directories['source'], [])
        self.assertEqual(len(client.directories['target']), 10)
        for index in range(10):
            file_info_result_14 = client.file_info(f'folder-{index}')
            assert file_info_result_14 is not None
            self.assertEqual(file_info_result_14.parent_id, 'target')
            file_info_result_15 = client.file_info(f'video-{index}')
            assert file_info_result_15 is not None
            self.assertEqual(file_info_result_15.name, f'Code-{index}.mp4')
            file_info_result_16 = client.file_info(f'video-{index}')
            assert file_info_result_16 is not None
            self.assertEqual(file_info_result_16.parent_id, f'folder-{index}')
            file_info_result_17 = client.file_info(f'poster-{index}')
            assert file_info_result_17 is not None
            self.assertEqual(file_info_result_17.parent_id, f'folder-{index}')

    def test_failed_child_rename_never_moves_its_parent(self):
        client, plan = self._folder_move_with_child_renames(count=2)
        original_rename = client.rename

        def rename(file_id, new_name):
            if file_id == 'video-0':
                raise GuangYaWriteRejected('rename', code='rejected')
            return original_rename(file_id, new_name)
        with mock.patch.object(client, 'rename', side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['renamed'], 1)
        self.assertEqual(result['stats']['moved'], 1)
        file_info_result_18 = client.file_info('folder-0')
        assert file_info_result_18 is not None
        self.assertEqual(file_info_result_18.parent_id, 'source')
        file_info_result_19 = client.file_info('video-0')
        assert file_info_result_19 is not None
        self.assertEqual(file_info_result_19.name, 'site.example@Code-0.mp4')
        file_info_result_20 = client.file_info('folder-1')
        assert file_info_result_20 is not None
        self.assertEqual(file_info_result_20.parent_id, 'target')

    def test_child_rename_does_not_allow_parent_trash(self):
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '父目录'):
            self._folder_move_with_child_renames(parent_op='trash')

    def test_cancel_after_child_clean_keeps_parent_and_prevents_replay(self):
        client, plan = self._folder_move_with_child_renames()
        payload = self._queued_payload(plan)

        def cancel():
            info = client.file_info('video-0')
            assert info is not None
            if info.name == 'Code-0.mp4':
                raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), cancel_check=cancel)
        file_info_result_21 = client.file_info('folder-0')
        assert file_info_result_21 is not None
        self.assertEqual(file_info_result_21.parent_id, 'source')
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
        file_info_result_22 = client.file_info('folder-0')
        assert file_info_result_22 is not None
        self.assertEqual(file_info_result_22.parent_id, 'source')


    def test_own_child_rename_does_not_mask_external_parent_rename(self):
        client, plan = self._folder_move_with_child_renames()
        original_rename = client.rename

        def rename(file_id, new_name):
            original_rename(file_id, new_name)
            client.directories['source'][0].name = 'externally-changed'
            return True
        with mock.patch.object(client, 'rename', side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['moved'], 0)
        file_info_result_23 = client.file_info('folder-0')
        assert file_info_result_23 is not None
        self.assertEqual(file_info_result_23.parent_id, 'source')

    def test_relocate_with_correct_name_moves_without_rejected_noop_rename(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == 'Move.mp4'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'relocate', 'file_id': target['file_id'], 'target_path': '/target', 'new_name': 'Move.mp4'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        with mock.patch.object(client, 'rename', side_effect=GuangYaWriteRejected('rename', code='160')) as rename:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['relocated'], 1)
        self.assertEqual(result['stats']['failed'], 0)
        rename.assert_not_called()
        file_info_result_24 = client.file_info('move')
        assert file_info_result_24 is not None
        self.assertEqual(file_info_result_24.parent_id, 'target')
        file_info_result_25 = client.file_info('move')
        assert file_info_result_25 is not None
        self.assertEqual(file_info_result_25.name, 'Move.mp4')

    def test_relocate_combines_move_and_rename_for_one_observed_object(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == 'Move.mp4'))
        normalized = {'operations': [{'op': 'relocate', 'file_id': target['file_id'], 'target_path': '/target', 'new_name': 'Series.S01E01.mp4'}], 'trigger_strm': False}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=normalized['operations'])
        self.assertEqual(plan['stats']['relocate'], 1)
        self.assertIn('移动并改名', plan['samples'][0])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['relocated'], 1)
        self.assertEqual([item.name for item in client.directories['target']], ['Series.S01E01.mp4'])
        self.assertIsNone(next((item for item in client.directories['source'] if item.file_id == 'move'), None))

    def test_mixed_plan_executes_only_frozen_operations_and_verifies_results(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'ABC.mp4'}, {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/target'}, {'op': 'trash', 'file_id': refs['垃圾残余']}, {'op': 'create_directory', 'parent_path': '/target', 'name': '新目录'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 1)
        self.assertEqual(result['stats']['moved'], 1)
        self.assertEqual(result['stats']['trashed'], 1)
        self.assertEqual(result['stats']['created'], 1)
        audits = db.list_organize_delete_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['status'], 'success')
        self.assertEqual(audits[0]['file_id'], 'trash')
        self.assertEqual([item.name for item in client.directories['source']], ['ABC.mp4'])
        self.assertEqual({item.name for item in client.directories['target']}, {'Move.mp4', '新目录'})

    def test_relocate_chooses_a_conflict_free_order_without_touching_siblings(self):
        for new_target in (False, True):
            for move_fails in (False, True):
                with self.subTest(new_target=new_target, move_fails=move_fails):
                    client = FakeGatewayClient()
                    observed = self._query(client)
                    refs = {row['name']: row['file_id'] for row in observed.data['items']}
                    operations = [{'op': 'create_directory', 'parent_path': '/target', 'name': 'new'}] if new_target else []
                    target = '/target/new' if new_target else '/target'
                    operations.append({'op': 'relocate', 'file_id': refs['广告-ABC.mp4'], 'target_path': target, 'new_name': 'Move.mp4'})
                    plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                    self.assertTrue(plan['operations'][-1]['move_first'])
                    guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                    calls = []
                    rename, move = (client.rename, client.move)

                    def relocate(ids, parent):
                        calls.append('move')
                        if move_fails:
                            raise GuangYaWriteRejected('move', code='rejected')
                        return move(ids, parent)

                    def rename_after(fid, name):
                        calls.append('rename')
                        file_info_result_26 = client.file_info(fid)
                        assert file_info_result_26 is not None
                        self.assertNotEqual(file_info_result_26.parent_id, 'source')
                        return rename(fid, name)
                    with mock.patch.object(client, 'move', side_effect=relocate), mock.patch.object(client, 'rename', side_effect=rename_after):
                        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                    self.assertEqual(calls, ['move'] if move_fails else ['move', 'rename'])
                    self.assertEqual(result['partial'], move_fails)
                    self.assertEqual(result['stats']['relocated'], int(not move_fails))
                    untouched = client.file_info('move')
                    assert untouched is not None
                    self.assertEqual((untouched.name, untouched.parent_id), ('Move.mp4', 'source'))
                    selected = client.file_info('rename')
                    assert selected is not None
                    self.assertEqual(selected.name, '广告-ABC.mp4' if move_fails else 'Move.mp4')
                    self.assertEqual(selected.parent_id == 'source', move_fails)

    def test_relocate_waits_for_rename_visibility_before_moving(self):
        client = FakeGatewayClient()
        client.directories['target'].append(GuangYaFile('occupied', '广告-ABC.mp4', False, parent_id='target', size=1, etag='other'))
        plan = self._confirmed_plan(client, {'op': 'relocate', 'source_name': '广告-ABC.mp4', 'target_path': '/target', 'new_name': 'ABC.mp4'})
        list_dir, move, rename = (client.list_dir, client.move, client.rename)
        pending = []
        reads = 0

        def delayed_rename(fid, name):
            pending.append((fid, name))
            return True

        def delayed_list(parent='0'):
            nonlocal reads
            if pending and parent == 'source':
                reads += 1
                if reads == 3:
                    rename(*pending[0])
            return list_dir(parent)

        def move_after_rename(ids, parent):
            info = client.file_info(ids[0])
            assert info is not None
            if info.name != 'ABC.mp4':
                raise GuangYaWriteRejected('move', code='name_conflict')
            return move(ids, parent)
        with mock.patch.object(client, 'rename', side_effect=delayed_rename) as renamed, mock.patch.object(client, 'move', side_effect=move_after_rename) as moved, mock.patch.object(client, 'list_dir', side_effect=delayed_list), mock.patch('app.clients.guangya.sleep'):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertGreaterEqual(reads, 3)
        moved.assert_called_once()
        renamed.assert_called_once()
        file_info_result_27 = client.file_info('rename')
        assert file_info_result_27 is not None
        self.assertEqual(file_info_result_27.name, 'ABC.mp4')
        file_info_result_28 = client.file_info('occupied')
        assert file_info_result_28 is not None
        self.assertEqual(file_info_result_28.name, '广告-ABC.mp4')

    def test_move_first_waits_for_move_visibility_before_renaming(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'relocate', 'source_name': '广告-ABC.mp4', 'target_path': '/target', 'new_name': 'Move.mp4'})
        list_dir, move, rename = (client.list_dir, client.move, client.rename)
        pending = []
        reads = 0

        def delayed_move(ids, parent):
            pending.append((ids, parent))
            return True

        def delayed_list(parent='0'):
            nonlocal reads
            if pending and parent == 'target':
                reads += 1
                if reads == 3:
                    move(*pending[0])
            return list_dir(parent)

        def rename_at_destination(fid, name):
            info = client.file_info(fid)
            assert info is not None
            if info.parent_id != 'target':
                raise GuangYaWriteRejected('rename', code='name_conflict')
            return rename(fid, name)
        with mock.patch.object(client, 'move', side_effect=delayed_move) as moved, mock.patch.object(client, 'rename', side_effect=rename_at_destination) as renamed, mock.patch.object(client, 'list_dir', side_effect=delayed_list), mock.patch('app.clients.guangya.sleep'):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['relocated'], 1)
        self.assertGreaterEqual(reads, 3)
        moved.assert_called_once()
        renamed.assert_called_once()
        file_info_result_29 = client.file_info('rename')
        assert file_info_result_29 is not None
        self.assertEqual(file_info_result_29.name, 'Move.mp4')
        file_info_result_30 = client.file_info('move')
        assert file_info_result_30 is not None
        self.assertEqual(file_info_result_30.parent_id, 'source')

    def test_move_first_reconciles_rename_failure_without_replaying_move(self):
        for mode in ('rejected', 'timeout_before_apply', 'timeout_after_apply'):
            with self.subTest(mode=mode):
                client = FakeGatewayClient()
                plan = self._confirmed_plan(client, {'op': 'relocate', 'source_name': '广告-ABC.mp4', 'target_path': '/target', 'new_name': 'Move.mp4'})
                payload = self._queued_payload(plan)
                original_rename = client.rename

                def rename(fid, name):
                    if mode == 'rejected':
                        raise GuangYaWriteRejected('rename', code='rejected')
                    if mode == 'timeout_after_apply':
                        original_rename(fid, name)
                    raise TimeoutError('response lost')
                with mock.patch.object(client, 'rename', side_effect=rename) as renamed, mock.patch.object(client, 'move', wraps=client.move) as moved:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                    self.assertEqual(result['partial'], mode != 'timeout_after_apply')
                    self.assertEqual(result['requires_manual'], mode == 'timeout_before_apply')
                    item_result = result['operation_items'][0]
                    self.assertEqual(item_result['status'], {'rejected': 'partial', 'timeout_before_apply': 'unknown', 'timeout_after_apply': 'completed'}[mode])
                    self.assertEqual(item_result['completed_actions'], ['move', 'rename'] if mode == 'timeout_after_apply' else ['move'])
                    self.assertNotIn('file_id', item_result)
                    self.assertEqual(result['stats']['relocated'], int(mode == 'timeout_after_apply'))
                    item = client.file_info('rename')
                    assert item is not None
                    self.assertEqual(item.parent_id, 'target')
                    self.assertEqual(item.name, 'Move.mp4' if mode == 'timeout_after_apply' else '广告-ABC.mp4')
                    file_info_result_31 = client.file_info('move')
                    assert file_info_result_31 is not None
                    self.assertEqual(file_info_result_31.parent_id, 'source')
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                    renamed.assert_called_once()
                    moved.assert_called_once()

    def test_move_first_rejects_intermediate_destination_collisions(self):
        for conflict in ('existing', 'planned', 'after_preview'):
            with self.subTest(conflict=conflict):
                client = FakeGatewayClient()
                if conflict == 'existing':
                    client.directories['target'].append(GuangYaFile('occupied', '广告-ABC.mp4', False, parent_id='target', size=1, etag='other'))
                observed = self._query(client)
                refs = {row['name']: row['file_id'] for row in observed.data['items']}
                operations = [{'op': 'create_directory', 'parent_path': '/target', 'name': '广告-ABC.mp4'}] if conflict == 'planned' else []
                operations.append({'op': 'relocate', 'file_id': refs['广告-ABC.mp4'], 'target_path': '/target', 'new_name': 'Move.mp4'})
                if conflict != 'after_preview':
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
                        self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                else:
                    plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                    guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                    client.directories['target'].append(GuangYaFile('occupied', '广告-ABC.mp4', False, parent_id='target', size=1, etag='other'))
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                file_info_result_32 = client.file_info('rename')
                assert file_info_result_32 is not None
                self.assertEqual(file_info_result_32.name, '广告-ABC.mp4')
                file_info_result_33 = client.file_info('rename')
                assert file_info_result_33 is not None
                self.assertEqual(file_info_result_33.parent_id, 'source')

    def test_child_backup_and_parent_relocation_share_one_confirmed_plan(self):
        for copy_fails in (False, True):
            with self.subTest(copy_fails=copy_fails):
                client = FakeGatewayClient()
                observed = self._query(client, operation='tree', path='/', page_size=50)
                refs = {item['name']: item['file_id'] for item in observed.data['items']}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'relocate', 'file_id': refs['source'], 'target_path': '/target', 'new_name': 'archived'}, {'op': 'copy', 'file_id': refs['Move.mp4'], 'target_path': '/target'}])
                self.assertEqual([item['op'] for item in plan['operations']], ['copy', 'relocate'])
                self.assertEqual(plan['operations'][1]['rename_dependencies'], ['move'])
                file_info_result_34 = client.file_info('source')
                assert file_info_result_34 is not None
                self.assertEqual(file_info_result_34.parent_id, '0', '预览不得写入')
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                payload = self._queued_payload(plan)
                with mock.patch.object(client, 'copy', wraps=client.copy) as copy, mock.patch.object(client, 'move', wraps=client.move) as move:
                    if copy_fails:
                        copy.side_effect = GuangYaWriteRejected('copy', code='rejected')
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                    copy.assert_called_once_with(['move'], 'target')
                    self.assertEqual(result['partial'], copy_fails)
                    self.assertEqual(result['stats']['copied'], int(not copy_fails))
                    self.assertEqual(result['stats']['relocated'], int(not copy_fails))
                    file_info_result_35 = client.file_info('move')
                    assert file_info_result_35 is not None
                    self.assertEqual(file_info_result_35.parent_id, 'source', '复制必须保留源文件')
                    if copy_fails:
                        move.assert_not_called()
                        file_info_result_36 = client.file_info('source')
                        assert file_info_result_36 is not None
                        self.assertEqual(file_info_result_36.name, 'source')
                        file_info_result_37 = client.file_info('source')
                        assert file_info_result_37 is not None
                        self.assertEqual(file_info_result_37.parent_id, '0')
                        self.assertEqual(result['stats']['precondition_failed'], 1)
                    else:
                        move.assert_called_once_with(['source'], 'target')
                        file_info_result_38 = client.file_info('source')
                        assert file_info_result_38 is not None
                        self.assertEqual(file_info_result_38.name, 'archived')
                        file_info_result_39 = client.file_info('source')
                        assert file_info_result_39 is not None
                        self.assertEqual(file_info_result_39.parent_id, 'target')
                        copied = next((row for row in client.list_dir('target') if row.name == 'Move.mp4'))
                        self.assertNotEqual(copied.file_id, 'move')
                        file_info_result_40 = client.file_info('move')
                        assert file_info_result_40 is not None
                        self.assertEqual(copied.size, file_info_result_40.size)
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                    copy.assert_called_once()

    def test_child_backup_does_not_authorize_trashing_nonempty_parent(self):
        client = FakeGatewayClient()
        observed = self._query(client, operation='tree', path='/', page_size=50)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '先移出内容'):
            self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'trash', 'file_id': refs['source']}, {'op': 'copy', 'file_id': refs['Move.mp4'], 'target_path': '/target'}])
        file_info_result_41 = client.file_info('source')
        assert file_info_result_41 is not None
        self.assertEqual(file_info_result_41.parent_id, '0')
        self.assertEqual(client.list_dir('target'), [])

    def _named_copy_plan(self, client, *, directory=False):
        plan = self._confirmed_plan(client, {'op': 'copy', 'source_name': 'source' if directory else 'Move.mp4', 'new_name': 'archived' if directory else 'Final.mp4', 'target_path': '/target'}, path='/' if directory else '/source')
        return (plan, self._queued_payload(plan))

    def test_copy_and_name_changes_only_the_verified_copy(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                client = FakeGatewayClient()
                original = deepcopy(client.directories)
                _, payload = self._named_copy_plan(client, directory=directory)
                with mock.patch.object(client, 'copy', wraps=client.copy) as copied, mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                self.assertFalse(result['partial'])
                self.assertEqual(result['stats']['copied'], 1)
                self.assertEqual(result['operation_items'][0]['completed_actions'], ['copy', 'rename'])
                self.assertEqual(client.directories['source'], original['source'])
                self.assertEqual(client.directories['0'], original['0'])
                target = client.list_dir('target')
                self.assertEqual(len(target), 1)
                self.assertEqual(target[0].name, 'archived' if directory else 'Final.mp4')
                renamed.assert_called_once_with(target[0].file_id, target[0].name)
                copied.assert_called_once()

    def test_named_copy_waits_and_resumes_without_recopy_or_second_confirmation(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                client = FakeGatewayClient()
                plan, payload = self._named_copy_plan(client, directory=directory)
                with mock.patch.object(client, 'copy', wraps=client.copy) as copied, mock.patch.object(client, 'rename', wraps=client.rename) as renamed, mock.patch.object(client, 'task_status', return_value={'data': {'status': 1}}) as task, mock.patch.object(guangya_fs_change, '_COPY_FOREGROUND_ATTEMPTS', 1):
                    for generation in (1, 2):
                        with self.assertRaises(guangya_fs_change.GuangYaFSCopyPending):
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=generation)
                        renamed.assert_not_called()
                    task.return_value = {'data': {'status': 2}}
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                    copied.assert_called_once()
                    renamed.assert_called_once()
                self.assertFalse(result['partial'])
                self.assertEqual(result['stats']['copied'], 1)
                file_info_result_42 = client.file_info('source')
                assert file_info_result_42 is not None
                self.assertEqual(file_info_result_42.parent_id, '0')
                self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'completed')

    def test_named_copy_resumes_verified_identity_or_verifies_interrupted_rename(self):

        class WorkerExit(BaseException):
            pass
        for directory in (False, True):
            for boundary in ('copy_verified', 'rename_applied'):
                with self.subTest(directory=directory, boundary=boundary):
                    client = FakeGatewayClient()
                    _, payload = self._named_copy_plan(client, directory=directory)
                    original_checkpoint, original_rename = (guangya_fs_change._append_journal, client.rename)

                    def checkpoint(plan_id, event):
                        original_checkpoint(plan_id, event)
                        row = (event.get('data') or {}).get('operation_item') or {}
                        if boundary == 'copy_verified' and row.get('status') == 'partial':
                            raise WorkerExit()

                    def rename(file_id, name):
                        original_rename(file_id, name)
                        if boundary == 'rename_applied':
                            raise WorkerExit()
                    with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(client, 'rename', side_effect=rename):
                        with self.assertRaises(WorkerExit):
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                    copied_id = client.list_dir('target')[0].file_id
                    with mock.patch.object(client, 'copy') as copied, mock.patch.object(client, 'rename', wraps=original_rename) as renamed, mock.patch.object(client, 'task_status', side_effect=TimeoutError('history unavailable')) as task:
                        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=2)
                        copied.assert_not_called()
                        task.assert_not_called()
                        self.assertEqual(renamed.call_count, int(boundary == 'copy_verified'))
                    self.assertFalse(result['partial'])
                    self.assertEqual(result['stats']['copied'], 1)
                    self.assertEqual(client.list_dir('target')[0].file_id, copied_id)
                    file_info_result_43 = client.file_info('source')
                    assert file_info_result_43 is not None
                    self.assertEqual(file_info_result_43.name, 'source')
                    file_info_result_44 = client.file_info('move')
                    assert file_info_result_44 is not None
                    self.assertEqual(file_info_result_44.name, 'Move.mp4')

    def test_named_copy_rejects_initial_or_final_name_conflicts_before_writes(self):
        for name in ('Move.mp4', 'Final.mp4'):
            with self.subTest(name=name):
                client = FakeGatewayClient()
                client.directories['target'].append(GuangYaFile('existing', name, False, parent_id='target'))
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
                    self._named_copy_plan(client)
                file_info_result_45 = client.file_info('move')
                assert file_info_result_45 is not None
                self.assertEqual(file_info_result_45.name, 'Move.mp4')
                self.assertEqual(len(client.list_dir('target')), 1)

    def test_named_copy_rechecks_copy_identity_and_conflicts_before_renaming(self):
        for change in ('conflict', 'replacement', 'source_changed'):
            with self.subTest(change=change):
                client = FakeGatewayClient()
                _, payload = self._named_copy_plan(client)
                original = guangya_fs_change._append_journal

                def checkpoint(plan_id, event):
                    original(plan_id, event)
                    row = (event.get('data') or {}).get('operation_item') or {}
                    if row.get('status') == 'partial':
                        if change == 'conflict':
                            client.directories['target'].append(GuangYaFile('existing', 'Final.mp4', False, parent_id='target'))
                        elif change == 'replacement':
                            client.directories['target'][0].file_id = 'unrelated'
                        else:
                            client.directories['source'][1].etag = 'new-content'
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                    renamed.assert_not_called()
                self.assertTrue(result['partial'])
                self.assertEqual(result['stats']['copied'], 0)
                self.assertEqual(result['operation_items'][0]['completed_actions'], ['copy'])
                file_info_result_46 = client.file_info('move')
                assert file_info_result_46 is not None
                self.assertEqual(file_info_result_46.name, 'Move.mp4')

    def test_named_copy_supports_new_target_and_omits_noop_rename(self):
        for new_target in (False, True):
            for new_name in ('Final.mp4', 'Move.mp4'):
                with self.subTest(new_target=new_target, new_name=new_name):
                    client = FakeGatewayClient()
                    obs = read_snapshot(cast(GuangYaClient, client), path='/', recursive=True)
                    refs = {row['file_id']: row['file_id'] for row in obs['items']}
                    operations = [{'op': 'create_directory', 'parent_path': '/', 'name': 'archive'}] if new_target else []
                    operations.append({'op': 'copy', 'file_id': refs['move'], 'target_path': '/archive' if new_target else '/target', 'new_name': new_name})
                    plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                    guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                    with mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
                        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                    self.assertFalse(result['partial'])
                    self.assertEqual(result['stats']['created'], int(new_target))
                    self.assertEqual(result['stats']['copied'], 1)
                    self.assertEqual(renamed.call_count, int(new_name != 'Move.mp4'))
                    file_info_result_47 = client.file_info('move')
                    assert file_info_result_47 is not None
                    self.assertEqual(file_info_result_47.name, 'Move.mp4')

    def test_named_copy_batch_can_reuse_the_temporary_original_name(self):
        client = FakeGatewayClient()
        client.directories['0'].append(GuangYaFile('second', 'second', True, parent_id='0', etag='s2'))
        client.directories['second'] = [GuangYaFile('other', 'Move.mp4', False, parent_id='second', size=99, etag='other')]
        obs = read_snapshot(cast(GuangYaClient, client), path='/', recursive=True)
        refs = {row['file_id']: row['file_id'] for row in obs['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'copy', 'file_id': refs[source], 'target_path': '/target', 'new_name': name} for source, name in (('move', 'A.mp4'), ('other', 'B.mp4'))])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['copied'], 2)
        self.assertEqual({row.name for row in client.list_dir('target')}, {'A.mp4', 'B.mp4'})
        file_info_result_48 = client.file_info('move')
        assert file_info_result_48 is not None
        self.assertEqual(file_info_result_48.name, 'Move.mp4')
        file_info_result_49 = client.file_info('other')
        assert file_info_result_49 is not None
        self.assertEqual(file_info_result_49.name, 'Move.mp4')

    def test_cancel_after_verified_copy_prevents_rename(self):
        from app.modules.organize_tasks import OrganizeOperationCancelled
        client = FakeGatewayClient()
        _, payload = self._named_copy_plan(client)
        cancelled = False
        original = guangya_fs_change._append_journal

        def checkpoint(plan_id, event):
            nonlocal cancelled
            original(plan_id, event)
            if ((event.get('data') or {}).get('operation_item') or {}).get('status') == 'partial':
                cancelled = True

        def check():
            if cancelled:
                raise OrganizeOperationCancelled('cancelled after copy')
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(client, 'rename', wraps=client.rename) as renamed:
            with self.assertRaises(OrganizeOperationCancelled):
                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1, cancel_check=check)
            renamed.assert_not_called()
        file_info_result_50 = client.file_info('move')
        assert file_info_result_50 is not None
        self.assertEqual(file_info_result_50.name, 'Move.mp4')
        self.assertEqual(client.list_dir('target')[0].name, 'Move.mp4')

    def _copy_plan(self, client, *, source_id='trash'):
        client.directories['trash'] = [GuangYaFile('leaf', 'leaf.txt', False, parent_id='trash', size=10, etag='hash-leaf'), GuangYaFile('nested', 'nested', True, parent_id='trash', etag='dir'), GuangYaFile('empty', 'empty', True, parent_id='trash', etag='empty')]
        client.directories['nested'] = [GuangYaFile('deep', 'deep.txt', False, parent_id='nested', size=20, etag='hash-deep')]
        client.directories['empty'] = []
        client.directories['0'].append(GuangYaFile('archive', 'archive', True, parent_id='0', etag='archive'))
        client.directories['archive'] = []
        obs = read_snapshot(client, path='/', recursive=True, max_items=500)
        refs = {row['file_id']: row['file_id'] for row in obs['items']}
        plan = self._build_plan(client, owner='owner', trigger_strm=False, operations=[{'op': 'move', 'file_id': refs['source'], 'target_path': '/archive'}, {'op': 'copy', 'file_id': refs[source_id], 'target_path': '/target'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return (plan, self._queued_payload(plan))

    def test_directory_copy_requires_task_and_complete_tree_before_moving_parent(self):
        for case in ('complete', 'delayed_task', 'running', 'unknown', 'failed', 'terminal_error', 'empty_shell', 'missing_leaf', 'missing_empty_dir', 'wrong_size', 'wrong_hash', 'missing_hash', 'extra', 'source_changed', 'read_error'):
            with self.subTest(case=case):
                client = FakeGatewayClient()
                plan, payload = self._copy_plan(client)
                original_copy, original_list = (client.copy, client.list_dir)
                copied_root: str = ''
                task_calls = 0

                def copy(ids, parent):
                    nonlocal copied_root
                    task_id = original_copy(ids, parent)
                    copied_root = client.directories[parent][0].file_id
                    rows = client.directories[copied_root]
                    if case == 'empty_shell':
                        rows.clear()
                    if case == 'missing_leaf':
                        rows.pop(0)
                    if case == 'missing_empty_dir':
                        rows.pop(2)
                    if case == 'wrong_size':
                        rows[0].size = 9
                    if case == 'wrong_hash':
                        rows[0].etag = 'different'
                    if case == 'missing_hash':
                        rows[0].etag = ''
                    if case == 'extra':
                        rows.append(GuangYaFile('extra', 'extra.txt', False, parent_id=copied_root, size=1))
                    if case == 'source_changed':
                        client.directories['nested'][0].size += 1
                    return task_id

                def status(task_id):
                    nonlocal task_calls
                    task_calls += 1
                    if case == 'delayed_task' and task_calls < 3 or case == 'running':
                        return {'data': {'status': 1}}
                    if case == 'unknown':
                        return {'data': {'status': 999}}
                    if case == 'failed':
                        return {'data': {'status': 3}}
                    return {'data': {'status': 2, 'detail': {'code': 7 if case == 'terminal_error' else 0}}}

                def listing(parent='0'):
                    if copied_root and parent == copied_root and (case == 'read_error'):
                        raise RuntimeError('directory listing unavailable')
                    return original_list(parent)
                with mock.patch.object(client, 'copy', side_effect=copy) as copied, mock.patch.object(client, 'task_status', side_effect=status), mock.patch.object(client, 'list_dir', side_effect=listing), mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch('app.clients.guangya.sleep'):
                    if case == 'running':
                        with self.assertRaises(guangya_fs_change.GuangYaFSCopyPending) as waiting:
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                        result = waiting.exception.operation_result
                        self.assertEqual(result['stats']['failed'], 0)
                        self.assertFalse(result['requires_manual'])
                    else:
                        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                complete = case in {'complete', 'delayed_task'}
                self.assertEqual(result['stats']['copied'], int(complete))
                self.assertEqual(result['stats']['moved'], int(complete))
                self.assertEqual(result['partial'], not complete and case != 'running')
                file_info_result_51 = client.file_info('source')
                assert file_info_result_51 is not None
                self.assertEqual(file_info_result_51.parent_id, 'archive' if complete else '0')
                file_info_result_52 = client.file_info('trash')
                assert file_info_result_52 is not None
                self.assertEqual(file_info_result_52.parent_id, 'source')
                self.assertEqual(moved.call_count, int(complete))
                copied.assert_called_once_with(['trash'], 'target')
                self.assertNotIn('copy_tasks', json.dumps(result))
                self.assertNotIn('copy-task-', json.dumps(result))
                if case == 'delayed_task':
                    self.assertGreaterEqual(task_calls, 3)

    def test_directory_copy_freezes_source_tree_and_rejects_old_plan(self):
        for change in ('new_child', 'deep_content', 'missing_snapshot'):
            with self.subTest(change=change):
                client = FakeGatewayClient()
                plan, payload = self._copy_plan(client)
                if change == 'new_child':
                    client.directories['empty'].append(GuangYaFile('new', 'new.txt', False, parent_id='empty', size=1))
                elif change == 'deep_content':
                    client.directories['nested'][0].etag = 'new-content'
                else:
                    stored = guangya_fs_change._read(plan['plan_id'])
                    stored['operations'][0].pop('copy_snapshot')
                    stored['fingerprint'] = guangya_fs_change._fingerprint(stored)
                    payload['plan_fingerprint'] = stored['fingerprint']
                    guangya_fs_change._atomic_write(stored)
                with mock.patch.object(client, 'copy', wraps=client.copy) as copied:
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                copied.assert_not_called()

    def test_directory_copy_snapshot_is_bounded_and_cancellable(self):
        client = FakeGatewayClient()
        plan, _ = self._copy_plan(client)
        self.assertEqual(plan['operations'][0]['copy_snapshot']['items'], 4)
        with mock.patch.object(guangya_fs_change, '_MAX_COPY_ITEMS', 2):
            with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '预算'):
                guangya_fs_change._copy_tree_snapshot(client, 'trash')
        with mock.patch.object(guangya_fs_change, '_MAX_COPY_DIRS', 1):
            with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '预算'):
                guangya_fs_change._copy_tree_snapshot(client, 'trash')
        cancel = mock.Mock(side_effect=RuntimeError('cancelled'))
        with mock.patch.object(client, 'list_dir', wraps=client.list_dir) as listing:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                guangya_fs_change._copy_tree_snapshot(client, 'trash', cancel)
            listing.assert_not_called()
        client.directories['empty'].append(cast(GuangYaFile, client.file_info('trash')))
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change._copy_tree_snapshot(client, 'trash')

    def test_copy_recovery_queries_recorded_task_without_repeating_copy(self):
        for source_id in ('trash', 'move'):
            with self.subTest(source_id=source_id):

                class WorkerExit(BaseException):
                    pass
                for checkpointed in (True, False):
                    for complete in (True, False):
                        with self.subTest(checkpointed=checkpointed, complete=complete):
                            client = FakeGatewayClient()
                            plan, payload = self._copy_plan(client, source_id=source_id)
                            original_checkpoint, original_copy = (guangya_fs_change._append_journal, client.copy)

                            def checkpoint(plan_id, event):
                                original_checkpoint(plan_id, event)
                                if (event.get('data') or {}).get('copy_task_id'):
                                    raise WorkerExit()

                            def copy(ids, parent):
                                tid = original_copy(ids, parent)
                                if not checkpointed:
                                    raise WorkerExit()
                                return tid
                            with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint), mock.patch.object(client, 'copy', side_effect=copy):
                                with self.assertRaises(WorkerExit):
                                    guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                            with mock.patch.object(client, 'copy', wraps=original_copy) as copied, mock.patch.object(client, 'task_status', return_value={'data': {'status': 2 if complete else 1}}) as task:
                                if checkpointed and (not complete):
                                    with self.assertRaises(guangya_fs_change.GuangYaFSCopyPending) as waiting:
                                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                                    result = waiting.exception.operation_result
                                else:
                                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                            copied.assert_not_called()
                            self.assertEqual(result['stats']['copied'], int(checkpointed and complete))
                            file_info_result_53 = client.file_info('source')
                            assert file_info_result_53 is not None
                            self.assertEqual(file_info_result_53.parent_id, 'archive' if checkpointed and complete else '0')
                            self.assertEqual(task.call_count, int(checkpointed))

    def test_pending_copy_can_wait_across_leases_and_only_move_parent_after_completion(self):
        for source_id in ('trash', 'move'):
            with self.subTest(source_id=source_id):
                client = FakeGatewayClient()
                plan, payload = self._copy_plan(client, source_id=source_id)
                with mock.patch.object(client, 'copy', wraps=client.copy) as copy, mock.patch.object(client, 'move', wraps=client.move) as move, mock.patch.object(client, 'task_status', return_value={'data': {'status': 1}}) as task, mock.patch.object(guangya_fs_change, '_COPY_FOREGROUND_ATTEMPTS', 1):
                    for generation in (1, 2, 3):
                        with self.assertRaises(guangya_fs_change.GuangYaFSCopyPending) as waiting:
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=generation)
                        self.assertEqual(waiting.exception.position, 1)
                        self.assertEqual(waiting.exception.operation_result['stats']['failed'], 0)
                        self.assertTrue(guangya_fs_change.inspect_fs_change_plan_recovery(payload, job_id=payload['job_id'], owner_digest=plan['owner_digest'], lease_generation=generation)['resumable'])
                        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'running')
                        file_info_result_54 = client.file_info('source')
                        assert file_info_result_54 is not None
                        self.assertEqual(file_info_result_54.parent_id, '0')
                    self.assertEqual(copy.call_count, 1)
                    move.assert_not_called()
                    self.assertEqual(task.call_count, 3)
                    task.return_value = {'data': {'status': 2}}
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=4)
                    self.assertFalse(result['partial'])
                    self.assertEqual(copy.call_count, 1)
                    move.assert_called_once_with(['source'], 'archive')

    def test_copy_status_read_error_then_known_running_defers_instead_of_failing(self):
        for source_id in ('trash', 'move'):
            with self.subTest(source_id=source_id):
                client = FakeGatewayClient()
                plan, payload = self._copy_plan(client, source_id=source_id)
                with mock.patch.object(client, 'task_status', side_effect=[RuntimeError('temporary read'), {'data': {'status': 1}}]), mock.patch.object(client, 'copy', wraps=client.copy) as copy:
                    with self.assertRaises(guangya_fs_change.GuangYaFSCopyPending) as waiting:
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                self.assertEqual(waiting.exception.operation_result['stats']['failed'], 0)
                self.assertEqual(waiting.exception.operation_result['stats']['verification_failed'], 0)
                file_info_result_55 = client.file_info('source')
                assert file_info_result_55 is not None
                self.assertEqual(file_info_result_55.parent_id, '0')
                copy.assert_called_once()
                with mock.patch.object(client, 'task_status', return_value={'data': {'status': 3}}), mock.patch.object(client, 'copy', wraps=client.copy) as copy:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=2)
                self.assertEqual(result['operation_items'][0]['status'], 'failed')
                self.assertEqual(result['operation_items'][1]['status'], 'blocked')
                copy.assert_not_called()

    def test_copy_wait_checkpoint_failure_does_not_finish_or_continue_plan(self):
        for source_id in ('trash', 'move'):
            with self.subTest(source_id=source_id):
                client = FakeGatewayClient()
                plan, payload = self._copy_plan(client, source_id=source_id)
                original = guangya_fs_change._append_journal
                task_saved = 0

                def fail_wait(plan_id, event):
                    nonlocal task_saved
                    if (event.get('data') or {}).get('copy_task_id'):
                        task_saved += 1
                        if task_saved > 1:
                            raise OSError('disk full')
                    original(plan_id, event)
                with mock.patch.object(client, 'task_status', return_value={'data': {'status': 1}}), mock.patch.object(client, 'move', wraps=client.move) as move, mock.patch.object(guangya_fs_change, '_COPY_FOREGROUND_ATTEMPTS', 1), mock.patch.object(guangya_fs_change, '_append_journal', side_effect=fail_wait):
                    with self.assertRaises(guangya_fs_change.GuangYaFSCheckpointError) as failed:
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                    self.assertEqual(failed.exception.operation_result['stats']['audit_failures'], 1)
                    self.assertTrue(failed.exception.operation_result['requires_manual'])
                    move.assert_not_called()

    def test_copy_recovers_verified_checkpoint_without_rechecking_task(self):

        class WorkerExit(BaseException):
            pass
        for source_id in ('trash', 'move'):
            for response in ('sync', 'async', 'lost'):
                with self.subTest(source_id=source_id, response=response):
                    acknowledged = response != 'lost'
                    result = None
                    client = FakeGatewayClient()
                    _, payload = self._copy_plan(client, source_id=source_id)
                    original_copy = client.copy
                    original_checkpoint = guangya_fs_change._append_journal

                    def copy(ids, target):
                        task_id = original_copy(ids, target)
                        if not acknowledged:
                            raise TimeoutError('copy response lost')
                        return task_id if response == 'async' else ''

                    def checkpoint(plan_id, event):
                        original_checkpoint(plan_id, event)
                        row = (event.get('data') or {}).get('operation_item') or {}
                        if row.get('operation') == 'copy' and row.get('status') == 'completed':
                            raise WorkerExit()
                    with mock.patch.object(client, 'copy', side_effect=copy) as copied, mock.patch.object(client, 'task_status', return_value={'data': {'status': 2}}) as task, mock.patch.object(guangya_fs_change, '_append_journal', side_effect=checkpoint):
                        if acknowledged:
                            with self.assertRaises(WorkerExit):
                                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                        else:
                            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                            self.assertEqual(result['stats']['copied'], 0)
                            file_info_result_56 = client.file_info('source')
                            assert file_info_result_56 is not None
                            self.assertEqual(file_info_result_56.parent_id, '0')
                        copied.assert_called_once()
                        self.assertEqual(task.call_count, int(response == 'async'))
                    with mock.patch.object(client, 'copy') as repeated, mock.patch.object(client, 'task_status', side_effect=TimeoutError('task history unavailable')) as task:
                        if acknowledged:
                            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=2)
                        else:
                            with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                                guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=2)
                        repeated.assert_not_called()
                        task.assert_not_called()
                    assert result is not None
                    self.assertEqual(result['stats']['copied'], int(acknowledged))
                    file_info_result_57 = client.file_info('source')
                    assert file_info_result_57 is not None
                    self.assertEqual(file_info_result_57.parent_id, 'archive' if acknowledged else '0')

    def test_file_copy_visible_target_does_not_override_provider_status(self):
        for status in (3, 999):
            with self.subTest(status=status):
                client = FakeGatewayClient()
                _, payload = self._copy_plan(client, source_id='move')
                with mock.patch.object(client, 'task_status', return_value={'data': {'status': status}}), mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch('app.clients.guangya.sleep'):
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                self.assertEqual(result['stats']['copied'], 0)
                self.assertTrue(result['partial'])
                moved.assert_not_called()
                file_info_result_58 = client.file_info('source')
                assert file_info_result_58 is not None
                self.assertEqual(file_info_result_58.parent_id, '0')

    def test_copy_keeps_source_and_verifies_new_target_object(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        source_ref = next((item['file_id'] for item in observed.data['items'] if item['name'] == 'Move.mp4'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'copy', 'file_id': source_ref, 'target_path': '/target'}])
        self.assertEqual(plan['stats']['copy'], 1)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['copied'], 1)
        assert client.file_info('move') is not None
        self.assertEqual([item.name for item in client.directories['target']], ['Move.mp4'])

    def test_move_waits_for_directory_visibility_without_repeating_write(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Move.mp4', 'target_path': '/target'})
        original_list = client.list_dir
        original_move = client.move
        pending = []
        reads = 0

        def accept_move(ids, parent):
            pending.append((ids, parent))
            return True

        def delayed_list(parent_id='0'):
            nonlocal reads
            if pending and parent_id == 'target':
                reads += 1
                if reads == 3:
                    original_move(*pending[0])
            return original_list(parent_id)
        with mock.patch.object(client, 'move', side_effect=accept_move) as write, mock.patch.object(client, 'list_dir', side_effect=delayed_list), mock.patch('app.clients.guangya.sleep'):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 1)
        write.assert_called_once()
        self.assertGreaterEqual(reads, 3)

    def test_copy_target_does_not_prove_source_was_retained(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'copy', 'source_name': 'Move.mp4', 'target_path': '/target'})
        client.copy(['move'], 'target')
        client.delete(['move'])
        self.assertFalse(guangya_fs_change._verify_after(cast(GuangYaClient, client), plan['operations'][0], ''))

    def test_rename_unknown_write_outcome_requires_manual_review(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        original_rename = client.rename

        def disconnected(fid, name):
            original_rename(fid, name)
            raise TimeoutError('response lost after accepted write')
        with mock.patch.object(client, 'rename', side_effect=disconnected) as write, mock.patch.object(guangya_fs_change, '_verify_after', side_effect=OSError('readback unavailable')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertTrue(result['requires_manual'])
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'manual_review')
        write.assert_called_once()

    def test_execution_rejects_stale_snapshot_before_any_write(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == '垃圾残余'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'trash', 'file_id': target['file_id']}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        client.directories['source'][2].etag = 'changed'
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        assert client.file_info('trash') is not None

    def test_initial_execution_does_not_hide_newer_details_behind_old_listing(self):
        for drift in ('source_version', 'source_missing', 'target_name', 'target_missing'):
            with self.subTest(drift=drift):
                client = FakeGatewayClient()
                plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Move.mp4', 'target_path': '/target'})
                payload = self._queued_payload(plan)
                info = client.file_info

                def newer_info(file_id):
                    current = info(file_id)
                    if file_id == 'move' and drift.startswith('source'):
                        if drift == 'source_missing':
                            return None
                        assert current is not None
                        current.etag = 'changed-after-preview'
                    if file_id == 'target' and drift.startswith('target'):
                        if drift == 'target_missing':
                            return None
                        assert current is not None
                        current.name = 'externally-renamed-target'
                    return current
                with mock.patch.object(client, 'file_info', side_effect=newer_info), mock.patch.object(client, 'move', wraps=client.move) as moved:
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                moved.assert_not_called()

    def test_existing_ancestor_drift_blocks_frozen_real_id_plan(self):
        for role, drift in (('target', 'rename'), ('target', 'move'), ('source', 'rename'), ('parent', 'rename')):
            with self.subTest(role=role, drift=drift):
                client = FakeGatewayClient()
                nested = client.create_dir('B', 'target')
                operation = {'op': 'create_directory', 'parent_path': '/target/B', 'name': 'New'} if role == 'parent' else {'op': 'move', 'file_id': 'move', 'target_path': '/target/B'}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[operation], trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                payload = self._queued_payload(plan)
                if drift == 'move':
                    client.move(['target'], 'source')
                else:
                    client.rename('source' if role == 'source' else 'target', 'ChangedByOtherActor')
                before = deepcopy(client.directories)
                with mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch.object(client, 'create_dir', wraps=client.create_dir) as created:
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                moved.assert_not_called()
                created.assert_not_called()
                self.assertEqual(client.directories, before)
                self.assertEqual(client.directories[nested], [])

    def test_initial_execution_allows_target_content_version_changes(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'move', 'source_name': 'Move.mp4', 'target_path': '/target'})
        payload = self._queued_payload(plan)
        client.directories['0'][1].etag = 'changed-by-other-child'
        with mock.patch.object(client, 'move', wraps=client.move) as moved:
            result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
        self.assertEqual(result['operation_items'][0]['status'], 'completed')
        moved.assert_called_once_with(['move'], 'target')

    def test_ancestor_drift_between_writes_preserves_completed_work(self):
        for phase in ('next_item', 'relocate_half', 'copy_half'):
            with self.subTest(phase=phase):
                client = FakeGatewayClient()
                nested = client.create_dir('B', 'target')
                operations = [{'op': 'move', 'file_id': file_id, 'target_path': '/target/B'} for file_id in ('move', 'rename')] if phase == 'next_item' else [{'op': 'relocate' if phase == 'relocate_half' else 'copy', 'file_id': 'move', 'target_path': '/target/B', 'new_name': 'Done.mp4'}]
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                payload = self._queued_payload(plan)
                original = guangya_fs_change._append_journal

                def change_ancestor(plan_id, event):
                    original(plan_id, event)
                    row = (event.get('data') or {}).get('operation_item') or {}
                    if row.get('position') == 1 and row.get('status') == ('completed' if phase == 'next_item' else 'partial'):
                        client.rename('target', 'ChangedByOtherActor')
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=change_ancestor), mock.patch.object(client, 'move', wraps=client.move) as moved, mock.patch.object(client, 'copy', wraps=client.copy) as copied:
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                self.assertTrue(result['partial'])
                self.assertEqual(result['stats']['precondition_failed'], 1)
                self.assertEqual(moved.call_count, int(phase == 'next_item'))
                self.assertEqual(copied.call_count, int(phase == 'copy_half'))
                last = result['operation_items'][-1]
                self.assertEqual(last['reason'], 'precondition_failed')
                self.assertEqual(last['completed_actions'], [] if phase == 'next_item' else ['rename' if phase == 'relocate_half' else 'copy'])
                self.assertEqual([file.name for file in client.directories[nested]], [] if phase == 'relocate_half' else ['Move.mp4'])

    def test_detail_read_failure_can_fall_back_but_absence_cannot(self):
        client = FakeGatewayClient()
        snapshot = {'file_id': 'move', 'parent_id': 'source'}
        with mock.patch.object(client, 'file_info', side_effect=TimeoutError('detail unavailable')):
            find_current_result = guangya_fs_change._find_current(cast(GuangYaClient, client), snapshot)
            assert find_current_result is not None
            self.assertEqual(find_current_result.file_id, 'move')
            with mock.patch.object(client, 'list_dir', side_effect=TimeoutError('list unavailable')):
                self.assertIsNone(guangya_fs_change._find_current(cast(GuangYaClient, client), snapshot))
        with mock.patch.object(client, 'file_info', return_value=None), mock.patch.object(client, 'list_dir', wraps=client.list_dir) as listing:
            self.assertIsNone(guangya_fs_change._find_current(cast(GuangYaClient, client), snapshot))
        listing.assert_not_called()

    def test_external_parent_change_invalidates_frozen_source_location(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == '垃圾残余'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'trash', 'file_id': target['file_id']}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        client.move(['trash'], 'target')
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        assert client.file_info('trash') is not None
        file_info_result_59 = client.file_info('trash')
        assert file_info_result_59 is not None
        self.assertEqual(file_info_result_59.parent_id, 'target')

    def test_completed_plan_cannot_be_replayed(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next((item for item in observed.data['items'] if item['name'] == '广告-ABC.mp4'))
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'rename', 'file_id': target['file_id'], 'new_name': 'ABC.mp4'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        payload = self._queued_payload(plan)
        first = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(first['partial'])
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))

    def test_multiple_moves_to_same_target_ignore_own_directory_version_changes(self):

        class UpdatingDirectoryClient(FakeGatewayClient):

            def move(self, file_ids, parent_id):
                result = super().move(file_ids, parent_id)
                target = next((item for item in self.directories['0'] if item.file_id == parent_id))
                target.etag += '-changed'
                target.updated_at += 1
                return result
        client = UpdatingDirectoryClient()
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'move', 'file_id': refs['广告-ABC.mp4'], 'target_path': '/target'}, {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/target'}])
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 2)

    def test_plan_state_cas_allows_only_one_concurrent_execution_claim(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        payload = self._queued_payload(plan)
        barrier = threading.Barrier(3)
        claimed: list[bool] = []

        def claim() -> None:
            barrier.wait()
            try:
                guangya_fs_change.update_fs_change_plan_execution(plan['plan_id'], status='running', execution={'started_at': 'now'}, expected_statuses={'queued'}, expected_job_id=payload['job_id'])
            except guangya_fs_change.GuangYaFSChangeStale:
                claimed.append(False)
            else:
                claimed.append(True)
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sorted(claimed), [False, True])
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'running')

    def test_confirmed_plan_cannot_execute_without_bound_durable_job(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
            guangya_fs_change.execute_fs_change_plan({'version': 1, 'plan_id': plan['plan_id'], 'plan_fingerprint': plan['fingerprint'], 'owner_digest': 'digest:owner', 'credential_generation': 31}, client_factory=lambda: cast(GuangYaClient, client))
        assert client.file_info('rename') is not None
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'confirmed')

    def test_queued_execution_uses_bound_job_id_after_confirmation_ttl(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        job_id = 'a' * 32
        guangya_fs_change.bind_fs_change_plan_job(plan['plan_id'], owner_digest='digest:owner', expected_fingerprint=plan['fingerprint'], job_id=job_id, queue_until_epoch=10 ** 12)
        queued = guangya_fs_change._read(plan['plan_id'])
        queued['execute_until_epoch'] = 1
        guangya_fs_change._atomic_write(queued)
        base_payload = {'version': 1, 'plan_id': plan['plan_id'], 'plan_fingerprint': plan['fingerprint'], 'owner_digest': 'digest:owner', 'credential_generation': 31}
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan({**base_payload, 'job_id': 'b' * 32}, client_factory=lambda: cast(GuangYaClient, client))
        assert client.file_info('rename') is not None
        result = guangya_fs_change.execute_fs_change_plan({**base_payload, 'job_id': job_id}, client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 1)
        terminal = guangya_fs_change._read(plan['plan_id'])
        self.assertEqual(terminal['status'], 'completed')
        self.assertEqual(terminal['job_id'], job_id)

    def test_trash_execution_uses_audited_recycle_bin_writer(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'trash', 'source_name': '垃圾残余'})

        def audited_delete(audit_client, *, candidate, **_kwargs):
            audit_client.delete([candidate.file_id])
            return {'audit_id': 1, 'status': 'success'}
        with mock.patch.object(guangya_fs_change, 'execute_recycle_bin_delete', side_effect=audited_delete) as audited:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['trashed'], 1)
        self.assertEqual(audited.call_args.kwargs['candidate'].file_id, 'trash')
        self.assertEqual(audited.call_args.kwargs['trigger'], 'agent_guangya_fs_change')

    def test_successful_delete_audit_and_recovered_readback_do_not_stop_parent_archival(self):
        for transient in ('timeout', 'delayed_visibility'):
            with self.subTest(transient=transient):
                client, plan = self._nested_archive_plan()
                verify = guangya_fs_change._verify_after
                attempts = 0

                def readback(provider, item, *args, **kwargs):
                    nonlocal attempts
                    if item['op'] == 'trash' and item['source']['file_id'] == 'leaf':
                        attempts += 1
                        row = next((row for row in db.list_organize_delete_audits(limit=20) if row['file_id'] == 'leaf'))
                        self.assertEqual(row['status'], 'success')
                        if transient == 'timeout' and attempts == 1:
                            raise TimeoutError('readback response lost')
                        if transient == 'delayed_visibility' and attempts <= 21:
                            return False
                    return verify(provider, item, *args, **kwargs)
                with mock.patch.object(guangya_fs_change, '_verify_after', side_effect=readback), mock.patch('app.clients.guangya.sleep'), mock.patch.object(client, 'delete_empty_directory', wraps=client.delete_empty_directory) as delete:
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertFalse(result['partial'])
                self.assertFalse(result['requires_manual'])
                self.assertEqual(result['stats']['audit_failures'], 0)
                self.assertEqual(result['stats']['verification_failed'], 0)
                self.assertEqual(delete.call_count, 2, '回读恢复不能重发删除')
                file_info_result_60 = client.file_info('work')
                assert file_info_result_60 is not None
                self.assertEqual(file_info_result_60.parent_id, 'target')
                self.assertTrue(all((item['status'] == 'completed' for item in result['operation_items'])))

    def test_unavailable_readback_remains_unknown_without_claiming_missing_audit(self):
        client, plan = self._nested_archive_plan()
        verify = guangya_fs_change._verify_after

        def readback(provider, item, *args, **kwargs):
            if item['op'] == 'trash':
                raise TimeoutError('readback unavailable')
            return verify(provider, item, *args, **kwargs)
        with mock.patch.object(guangya_fs_change, '_verify_after', side_effect=readback), mock.patch.object(client, 'delete_empty_directory', wraps=client.delete_empty_directory) as delete:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'] and result['requires_manual'])
        self.assertEqual(result['stats']['audit_failures'], 0)
        self.assertEqual(result['stats']['verification_failed'], 1)
        self.assertEqual(delete.call_count, 1)
        file_info_result_61 = client.file_info('work')
        assert file_info_result_61 is not None
        self.assertEqual(file_info_result_61.parent_id, 'source')
        self.assertIn('unknown', [item['status'] for item in result['operation_items']])

    def test_actual_delete_success_audit_failure_stops_without_replaying_delete(self):
        for mode in ('exception', 'missing_row'):
            with self.subTest(mode=mode):
                client, plan = self._nested_archive_plan()
                update = db.update_organize_delete_audit

                def write(audit_id, **fields):
                    if fields.get('status') == 'success':
                        if mode == 'exception':
                            raise OSError('audit storage failed')
                        return False
                    return update(audit_id, **fields)
                with mock.patch.object(db, 'update_organize_delete_audit', side_effect=write), mock.patch.object(client, 'delete_empty_directory', wraps=client.delete_empty_directory) as delete:
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertTrue(result['partial'] and result['requires_manual'])
                self.assertEqual(result['stats']['audit_failures'], 1)
                self.assertEqual(result['stats']['trashed'], 1)
                self.assertEqual(delete.call_count, 1)
                file_info_result_62 = client.file_info('work')
                assert file_info_result_62 is not None
                self.assertEqual(file_info_result_62.parent_id, 'source')
                self.assertTrue(any((item['status'] == 'not_started' for item in result['operation_items'])))

    def test_entire_plan_keeps_unstarted_items_after_audit_failure_or_cancel(self):
        from app.repositories.organize_operation_jobs import OrganizeOperationCancelled
        for stop in ('audit', 'cancel'):
            with self.subTest(stop=stop):
                client = FakeGatewayClient()
                observed = self._query(client)
                refs = {row['name']: row['file_id'] for row in observed.data['items']}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'A.mp4'}, {'op': 'rename', 'file_id': refs['Move.mp4'], 'new_name': 'B.mp4'}], trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                payload = self._queued_payload(plan)
                journal = guangya_fs_change._append_journal
                cancelled = False

                def append(pid, event):
                    nonlocal cancelled
                    if event.get('index') == 1:
                        if stop == 'audit':
                            raise OSError('audit unavailable')
                        cancelled = True
                    return journal(pid, event)

                def check():
                    if cancelled:
                        raise OrganizeOperationCancelled('cancelled')
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=append):
                    if stop == 'cancel':
                        with self.assertRaises(OrganizeOperationCancelled) as caught:
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), cancel_check=check)
                        result = caught.exception.operation_result
                    else:
                        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                self.assertTrue(result['partial'])
                self.assertEqual(result['requires_manual'], stop == 'audit')
                self.assertEqual(result['stats']['renamed'], 1)
                self.assertEqual([row['status'] for row in result['operation_items']], ['completed', 'not_started'])
                self.assertEqual(result['operation_items'][0]['completed_actions'], ['rename'])
                file_info_result_63 = client.file_info('rename')
                assert file_info_result_63 is not None
                self.assertEqual(file_info_result_63.name, 'A.mp4')
                file_info_result_64 = client.file_info('move')
                assert file_info_result_64 is not None
                self.assertEqual(file_info_result_64.name, 'Move.mp4')
                self.assertEqual([row['position'] for row in result['operation_items']], [1, 2])
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                    guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))

    def test_creation_identity_survives_first_readback_error(self):
        client = FakeGatewayClient()
        self._query(client)
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'create_directory', 'parent_path': '/target', 'name': 'new'}], trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        verify = guangya_fs_change._verify_after
        identities = []

        def flaky(client, item, created_id='', **kwargs):
            identities.append(created_id)
            if len(identities) == 1:
                raise TimeoutError('readback timeout')
            return verify(client, item, created_id, **kwargs)
        with mock.patch.object(guangya_fs_change, '_verify_after', side_effect=flaky):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(identities, ['created-1', 'created-1'])
        self.assertEqual(result['operation_items'][0]['status'], 'completed')
        self.assertNotIn('created-1', json.dumps(result))

    def test_journal_failure_after_remote_write_returns_manual_partial(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        original_append = guangya_fs_change._append_journal
        calls = 0

        def flaky_append(plan_id, event):
            nonlocal calls
            calls += 1
            if event.get('action') == 'rename':
                raise OSError('journal unavailable')
            return original_append(plan_id, event)
        with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=flaky_append):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertTrue(result['requires_manual'])
        self.assertEqual(result['stats']['renamed'], 1)
        self.assertGreaterEqual(result['stats']['audit_failures'], 1)
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'manual_review')

    def test_terminal_plan_write_failure_does_not_raise_retryable_failure(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {'op': 'rename', 'source_name': '广告-ABC.mp4', 'new_name': 'ABC.mp4'})
        original_update = guangya_fs_change.update_fs_change_plan_execution

        def flaky_update(plan_id, *, status, execution, **kwargs):
            if status != 'running':
                raise OSError('plan store unavailable')
            return original_update(plan_id, status=status, execution=execution, **kwargs)
        with mock.patch.object(guangya_fs_change, 'update_fs_change_plan_execution', side_effect=flaky_update):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertTrue(result['requires_manual'])
        self.assertEqual(result['stats']['renamed'], 1)
        self.assertEqual(guangya_fs_change._read(plan['plan_id'])['status'], 'running')

    def _nested_archive_plan(self, *, extra_file=False, future_target=False):

        class Client(FakeGatewayClient):

            def touch(self, *ids):
                for entries in self.directories.values():
                    for item in entries:
                        if item.file_id in ids:
                            item.etag += '-changed'
                            item.updated_at += 1

            def move(self, file_ids, parent_id):
                parents = []
                for file_id in file_ids:
                    info = self.file_info(file_id)
                    assert info is not None
                    parents.append(info.parent_id)
                result = super().move(file_ids, parent_id)
                self.touch(*parents, parent_id)
                return result

            def delete_empty_directory(self, file_id, *, expected_etag='', expected_updated_at=0):
                current = self.file_info(file_id)
                if current is None or current.etag != expected_etag or self.list_dir(file_id):
                    raise RuntimeError('目录已变化，保留')
                result = super().delete([file_id])
                self.touch(current.parent_id)
                return result
        client = Client()
        client.directories['source'] = [GuangYaFile('work', 'Work-A', True, parent_id='source', etag='work'), GuangYaFile('peer', 'Work-B', True, parent_id='source', etag='peer')]
        client.directories['work'] = [GuangYaFile('mid', 'pack', True, parent_id='work', etag='mid')]
        client.directories['mid'] = [GuangYaFile('leaf', 'V', True, parent_id='mid', etag='leaf')]
        client.directories['leaf'] = [GuangYaFile('video', 'movie.mp4', False, parent_id='leaf', etag='video', size=100)]
        client.directories['peer'] = [GuangYaFile('peer-video', 'code.mp4', False, parent_id='peer', etag='peer-video', size=200)]
        if extra_file:
            client.directories['leaf'].append(GuangYaFile('poster', 'poster.jpg', False, parent_id='leaf', etag='poster'))
        observed = self._query(client, operation='tree', page_size=50)
        entries = {item['name']: item['file_id'] for item in observed.data['items']}
        operations = [{'op': 'move', 'file_id': entries['Work-A'], 'target_path': '/target'}, {'op': 'move', 'file_id': entries['Work-B'], 'target_path': '/target'}, {'op': 'trash', 'file_id': entries['pack']}, {'op': 'trash', 'file_id': entries['V']}, {'op': 'move', 'file_id': entries['movie.mp4'], 'target_path': '/target/Work-A' if future_target else '/source/Work-A'}]
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return (client, plan)

    def test_incoming_file_targets_future_directory_before_its_move_or_rename(self):
        for parent_op in ('move', 'relocate', 'rename'):
            for parent_first in (True, False):
                with self.subTest(parent_op=parent_op, parent_first=parent_first):
                    client = FakeGatewayClient()
                    observed = self._query(client)
                    refs = {item['name']: item['file_id'] for item in observed.data['items']}
                    parent = {'op': parent_op, 'file_id': refs['垃圾残余']}
                    if parent_op != 'rename':
                        parent['target_path'] = '/target'
                    if parent_op != 'move':
                        parent['new_name'] = 'Archived'
                    destination = ('/source' if parent_op == 'rename' else '/target') + '/' + parent.get('new_name', '垃圾残余')
                    child = {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': destination}
                    raw = [parent, child] if parent_first else [child, parent]
                    original = deepcopy(raw)
                    plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=raw, trigger_strm=False)
                    self.assertEqual(raw, original, '不能改写模型原始参数')
                    self.assertEqual([item['source']['file_id'] for item in plan['operations']], ['move', 'trash'])
                    self.assertEqual(plan['operations'][0]['target_id'], 'trash')
                    guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                    self.assertFalse(result['partial'])
                    file_info_result_65 = client.file_info('move')
                    assert file_info_result_65 is not None
                    self.assertEqual(file_info_result_65.parent_id, 'trash')
                    file_info_result_66 = client.file_info('trash')
                    assert file_info_result_66 is not None
                    self.assertEqual(file_info_result_66.parent_id, 'source' if parent_op == 'rename' else 'target')
                    file_info_result_67 = client.file_info('trash')
                    assert file_info_result_67 is not None
                    self.assertEqual(file_info_result_67.name, parent.get('new_name', '垃圾残余'))

    def test_future_target_rejects_observed_directory_replaced_before_freeze(self):
        for name in ('Dir', 'dir'):
            with self.subTest(target_name=name):
                client = FakeGatewayClient()
                client.rename('trash', 'Dir')
                observed = self._query(client, operation='tree', page_size=50)
                refs = {item['name']: item['file_id'] for item in observed.data['items'] + [observed.data['root']]}
                replacement = client._pop('trash')
                replacement.file_id = 'replacement'
                client.directories['source'].append(replacement)
                client.directories['replacement'] = []
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                    self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'move', 'file_id': refs['source'], 'target_path': '/target'}, {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': f'/target/source/{name}', 'target_id': refs['Dir']}])
                file_info_result_68 = client.file_info('move')
                assert file_info_result_68 is not None
                self.assertEqual(file_info_result_68.parent_id, 'source')

    def test_case_alias_target_dependency_uses_directory_identity(self):
        for first in (True, False):
            with self.subTest(parent_first=first):
                client = FakeGatewayClient()
                observed = self._query(client, operation='tree', path='/', page_size=50)
                refs = {item['name']: item['file_id'] for item in observed.data['items']}
                parent = {'op': 'rename', 'file_id': refs['target'], 'new_name': 'archive'}
                child = {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/TARGET'}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[parent, child] if first else [child, parent], trigger_strm=False)
                self.assertEqual([op['source']['file_id'] for op in plan['operations']], ['move', 'target'])
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertFalse(result['partial'])
                file_info_result_69 = client.file_info('move')
                assert file_info_result_69 is not None
                self.assertEqual(file_info_result_69.parent_id, 'target')
                file_info_result_70 = client.file_info('target')
                assert file_info_result_70 is not None
                self.assertEqual(file_info_result_70.name, 'archive')

    def test_creation_under_future_directory_guards_its_parent_change(self):

        class Client(FakeGatewayClient):

            def create_dir(self, name, parent_id='0'):
                created = super().create_dir(name, parent_id)
                parent = self._pop(parent_id)
                parent.etag += '-changed'
                self.directories[parent.parent_id].append(parent)
                return created
        for mode in ('success', 'failure', 'restart'):
            with self.subTest(mode=mode):
                fail_create = mode == 'failure'
                client = Client()
                observed = self._query(client)
                refs = {item['name']: item['file_id'] for item in observed.data['items']}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'rename', 'file_id': refs['垃圾残余'], 'new_name': 'archive'}, {'op': 'create_directory', 'parent_path': '/source/archive', 'name': 'New'}], trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                if fail_create:
                    client.create_dir = mock.Mock(side_effect=RuntimeError('provider unavailable'))
                payload = self._queued_payload(plan)
                if mode == 'restart':

                    class WorkerExit(BaseException):
                        pass
                    original_journal = guangya_fs_change._append_journal

                    def interrupt_after_create(plan_id, record):
                        original_journal(plan_id, record)
                        row = (record.get('data') or {}).get('operation_item') or {}
                        if row.get('position') == 1 and row.get('status') == 'completed':
                            raise WorkerExit()
                    with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=interrupt_after_create):
                        with self.assertRaises(WorkerExit):
                            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=1)
                    client.create_dir = mock.Mock(side_effect=AssertionError('must not recreate'))
                result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), lease_generation=3)
                self.assertEqual(result['partial'], fail_create)
                file_info_result_71 = client.file_info('trash')
                assert file_info_result_71 is not None
                self.assertEqual(file_info_result_71.name, '垃圾残余' if fail_create else 'archive')
                self.assertEqual([x.name for x in client.list_dir('trash')], [] if fail_create else ['New'])

    def test_nested_future_paths_compile_independent_of_argument_order(self):
        for reverse, fail_child in ((False, False), (True, False), (False, True)):
            with self.subTest(reverse=reverse, fail_child=fail_child):
                client = FakeGatewayClient()
                observed = self._query(client, operation='tree', page_size=50)
                refs = {item['name']: item['file_id'] for item in observed.data['items'] + [observed.data['root']]}
                operations = [{'op': 'move', 'file_id': refs['source'], 'target_path': '/target'}, {'op': 'rename', 'file_id': refs['垃圾残余'], 'new_name': 'New'}, {'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/target/source/New'}]
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=list(reversed(operations)) if reverse else operations, trigger_strm=False)
                self.assertEqual([op['source']['file_id'] for op in plan['operations']], ['move', 'trash', 'source'])
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                if fail_child:
                    client.move = mock.Mock(side_effect=RuntimeError('provider unavailable'))
                result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertEqual(result['partial'], fail_child)
                file_info_result_72 = client.file_info('move')
                assert file_info_result_72 is not None
                self.assertEqual(file_info_result_72.parent_id, 'source' if fail_child else 'trash')
                file_info_result_73 = client.file_info('trash')
                assert file_info_result_73 is not None
                self.assertEqual(file_info_result_73.name, '垃圾残余' if fail_child else 'New')
                file_info_result_74 = client.file_info('source')
                assert file_info_result_74 is not None
                self.assertEqual(file_info_result_74.parent_id, '0' if fail_child else 'target')

    def test_future_path_cannot_make_self_move_or_copy_safe(self):
        for op in ('move', 'copy'):
            with self.subTest(op=op):
                client = FakeGatewayClient()
                observed = self._query(client, operation='tree', page_size=50)
                refs = {item['name']: item['file_id'] for item in observed.data['items'] + [observed.data['root']]}
                with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
                    self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'rename', 'file_id': refs['垃圾残余'], 'new_name': 'New'}, {'op': op, 'file_id': refs['source'], 'target_path': '/source/New'}])
                file_info_result_75 = client.file_info('source')
                assert file_info_result_75 is not None
                self.assertEqual(file_info_result_75.parent_id, '0')
                file_info_result_76 = client.file_info('trash')
                assert file_info_result_76 is not None
                self.assertEqual(file_info_result_76.name, '垃圾残余')

    def test_directory_target_dependency_cycle_is_rejected_before_write(self):
        client = FakeGatewayClient()
        client.directories['source'] = [GuangYaFile('a', 'A', True, parent_id='source'), GuangYaFile('b', 'B', True, parent_id='source')]
        client.directories.update(a=[], b=[])
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '依赖'):
            self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'move', 'file_id': refs['A'], 'target_path': '/source/B'}, {'op': 'move', 'file_id': refs['B'], 'target_path': '/source/A'}])
        file_info_result_77 = client.file_info('a')
        assert file_info_result_77 is not None
        self.assertEqual(file_info_result_77.parent_id, 'source')
        file_info_result_78 = client.file_info('b')
        assert file_info_result_78 is not None
        self.assertEqual(file_info_result_78.parent_id, 'source')
        self.assertFalse(list(self.plan_dir.glob('*.json')))

    def test_existing_destination_is_not_replaced_by_planned_directory_alias(self):
        client = FakeGatewayClient()
        client.directories['target'] = [GuangYaFile('occupied', '垃圾残余', True, parent_id='target')]
        client.directories['occupied'] = []
        observed = self._query(client)
        refs = {item['name']: item['file_id'] for item in observed.data['items']}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '同名'):
            self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'move', 'file_id': refs['Move.mp4'], 'target_path': '/target/垃圾残余'}, {'op': 'move', 'file_id': refs['垃圾残余'], 'target_path': '/target'}])
        file_info_result_79 = client.file_info('move')
        assert file_info_result_79 is not None
        self.assertEqual(file_info_result_79.parent_id, 'source')
        file_info_result_80 = client.file_info('occupied')
        assert file_info_result_80 is not None
        self.assertEqual(file_info_result_80.parent_id, 'target')

    def test_future_parent_directory_target_uses_existing_identity_in_one_plan(self):
        client, plan = self._nested_archive_plan(future_target=True)
        child = next((item for item in plan['operations'] if item['source']['file_id'] == 'video'))
        self.assertEqual(child['target_id'], 'work')
        self.assertEqual(child['target_path'], '/source/Work-A')
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        file_info_result_81 = client.file_info('work')
        assert file_info_result_81 is not None
        self.assertEqual(file_info_result_81.parent_id, 'target')
        file_info_result_82 = client.file_info('video')
        assert file_info_result_82 is not None
        self.assertEqual(file_info_result_82.parent_id, 'work')
        self.assertEqual(client.list_dir('source'), [])
        self.assertIsNone(client.file_info('mid'))
        self.assertIsNone(client.file_info('leaf'))

    def test_parent_change_rechecks_completed_child_before_archival(self):
        for drift in ('move', 'rename', 'read_failure', 'none'):
            with self.subTest(drift=drift):
                client, plan = self._nested_archive_plan()
                payload = self._queued_payload(plan)
                original_journal = guangya_fs_change._append_journal
                original_list = client.list_dir
                parent_position = next((i for i, op in enumerate(plan['operations'], 1) if op['source']['file_id'] == 'mid'))
                drifted = False

                def after_checkpoint(plan_id, record):
                    nonlocal drifted
                    original_journal(plan_id, record)
                    row = (record.get('data') or {}).get('operation_item') or {}
                    if not drifted and row.get('position') == parent_position and (row.get('status') == 'completed'):
                        drifted = True
                        if drift == 'move':
                            client.move(['video'], 'source')
                        elif drift == 'rename':
                            client.rename('video', 'Changed.mp4')

                def listing(parent_id='0'):
                    if drifted and drift == 'read_failure' and (parent_id == 'work'):
                        raise TimeoutError('provider unavailable')
                    return original_list(parent_id)
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=after_checkpoint), mock.patch.object(client, 'list_dir', side_effect=listing):
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
                self.assertEqual(result['partial'], drift != 'none')
                file_info_result_83 = client.file_info('work')
                assert file_info_result_83 is not None
                self.assertEqual(file_info_result_83.parent_id, 'target' if drift == 'none' else 'source')
                if drift == 'move':
                    file_info_result_84 = client.file_info('video')
                    assert file_info_result_84 is not None
                    self.assertEqual(file_info_result_84.parent_id, 'source', '不能自动撤销外部移动')
                if drift == 'rename':
                    file_info_result_85 = client.file_info('video')
                    assert file_info_result_85 is not None
                    self.assertEqual(file_info_result_85.name, 'Changed.mp4', '不能自动撤销外部改名')

    def test_backup_output_survives_planned_source_changes_but_detects_replica_drift(self):
        for drift in (False, True):
            with self.subTest(replica_drift=drift):
                client = FakeGatewayClient()
                client.directories['0'].append(GuangYaFile('archive', 'archive', True, parent_id='0', etag='a'))
                client.directories['archive'] = []
                client.directories['trash'] = [GuangYaFile('nested', 'Old.mkv', False, parent_id='trash', size=10, etag='n')]
                observation = read_snapshot(cast(GuangYaClient, client), path='/source', recursive=True, max_items=100)
                refs = {item['file_id']: item['file_id'] for item in observation['items'] + [observation['root']]}
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'copy', 'file_id': refs['trash'], 'target_path': '/target'}, {'op': 'rename', 'file_id': refs['nested'], 'new_name': 'New.mkv'}, {'op': 'move', 'file_id': refs['source'], 'target_path': '/archive'}])
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                original_journal = guangya_fs_change._append_journal

                def after_rename(plan_id, record):
                    original_journal(plan_id, record)
                    row = (record.get('data') or {}).get('operation_item') or {}
                    if drift and row.get('position') == 2 and (row.get('status') == 'completed'):
                        copied = client.list_dir('target')[0]
                        client.rename(client.list_dir(copied.file_id)[0].file_id, 'External.mkv')
                with mock.patch.object(guangya_fs_change, '_append_journal', side_effect=after_rename):
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertEqual(result['partial'], drift)
                file_info_result_86 = client.file_info('source')
                assert file_info_result_86 is not None
                self.assertEqual(file_info_result_86.parent_id, '0' if drift else 'archive')
                file_info_result_87 = client.file_info('nested')
                assert file_info_result_87 is not None
                self.assertEqual(file_info_result_87.name, 'New.mkv')
                backup = client.list_dir('target')[0]
                self.assertEqual(client.list_dir(backup.file_id)[0].name, 'External.mkv' if drift else 'Old.mkv')

    def test_parent_dependency_readback_lists_shared_directory_once(self):
        client = FakeGatewayClient()
        client.directories['source'] = [GuangYaFile(f'f{i}', f'Old-{i}.mkv', False, parent_id='source', size=10, etag=str(i)) for i in range(40)]
        observation = read_snapshot(cast(GuangYaClient, client), path='/source', recursive=True, max_items=100)
        refs = {item['file_id']: item['file_id'] for item in observation['items'] + [observation['root']]}
        operations = [{'op': 'rename', 'file_id': refs[f'f{i}'], 'new_name': f'New-{i}.mkv'} for i in range(40)]
        operations.append({'op': 'move', 'file_id': refs['source'], 'target_path': '/target'})
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        original_verify = guangya_fs_change._verify_after
        dependency_reads = []

        def verify(*args, **kwargs):
            if 'directory_cache' not in kwargs:
                return original_verify(*args, **kwargs)
            with mock.patch.object(client, 'list_dir', wraps=client.list_dir) as listing:
                result = original_verify(*args, **kwargs)
            dependency_reads.append(listing.call_count)
            return result
        with mock.patch.object(guangya_fs_change, '_verify_after', side_effect=verify):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['renamed'], 40)
        self.assertEqual(len(dependency_reads), 40)
        self.assertEqual(sum(dependency_reads), 1, '新增的40项依赖核对不能发送40次同目录请求')

    def test_nested_video_extraction_empty_cleanup_and_folder_archival_share_one_plan(self):
        client, plan = self._nested_archive_plan()
        order = [item['source']['file_id'] for item in plan['operations']]
        self.assertLess(order.index('video'), order.index('leaf'))
        self.assertLess(order.index('leaf'), order.index('mid'))
        self.assertLess(order.index('mid'), order.index('work'))
        self.assertTrue(all((item.get('require_empty') for item in plan['operations'] if item['op'] == 'trash')))
        self.assertTrue(client.file_info('leaf'), '预览阶段不能动文件')
        payload = self._queued_payload(plan)
        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 3)
        self.assertEqual(result['stats']['trashed'], 2)
        file_info_result_88 = client.file_info('video')
        assert file_info_result_88 is not None
        self.assertEqual(file_info_result_88.parent_id, 'work')
        file_info_result_89 = client.file_info('work')
        assert file_info_result_89 is not None
        self.assertEqual(file_info_result_89.parent_id, 'target')
        file_info_result_90 = client.file_info('peer')
        assert file_info_result_90 is not None
        self.assertEqual(file_info_result_90.parent_id, 'target')
        file_info_result_91 = client.file_info('peer-video')
        assert file_info_result_91 is not None
        self.assertEqual(file_info_result_91.parent_id, 'peer')
        self.assertIsNone(client.file_info('leaf'))
        self.assertIsNone(client.file_info('mid'))
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))

    def test_unmoved_companion_blocks_empty_directory_cleanup_at_preview(self):
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '未安排移出'):
            self._nested_archive_plan(extra_file=True)

    def test_failed_extraction_blocks_all_dependent_cleanup_and_parent_move(self):
        client, plan = self._nested_archive_plan()
        original_move = client.move

        def move(file_ids, parent_id):
            if 'video' in file_ids:
                raise GuangYaWriteRejected('move', code='rejected')
            return original_move(file_ids, parent_id)
        with mock.patch.object(client, 'move', side_effect=move), mock.patch.object(client, 'delete_empty_directory', side_effect=AssertionError('不得删目录')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['moved'], 1, '独立作品仍可以完成')
        outcomes = result['operation_items']
        self.assertEqual(len(outcomes), plan['stats']['total'])
        self.assertEqual(sum((row['status'] == 'completed' for row in outcomes)), 1)
        self.assertEqual(sum((row['status'] == 'blocked' for row in outcomes)), 3)
        self.assertEqual(sum((row['status'] == 'failed' for row in outcomes)), 1)
        self.assertTrue(all((row.get('reason') == 'dependency_failed' for row in outcomes if row['status'] == 'blocked')))
        file_info_result_92 = client.file_info('video')
        assert file_info_result_92 is not None
        self.assertEqual(file_info_result_92.parent_id, 'leaf')
        file_info_result_93 = client.file_info('work')
        assert file_info_result_93 is not None
        self.assertEqual(file_info_result_93.parent_id, 'source')
        self.assertTrue(client.file_info('mid'))

    def test_new_child_between_extraction_and_cleanup_is_never_deleted(self):
        client, plan = self._nested_archive_plan()
        original_move = client.move

        def move(file_ids, parent_id):
            result = original_move(file_ids, parent_id)
            if 'video' in file_ids:
                client.directories['leaf'].append(GuangYaFile('new-file', 'new.mp4', False, parent_id='leaf', etag='new'))
            return result
        with mock.patch.object(client, 'move', side_effect=move):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['trashed'], 0)
        file_info_result_94 = client.file_info('new-file')
        assert file_info_result_94 is not None
        self.assertEqual(file_info_result_94.parent_id, 'leaf')
        file_info_result_95 = client.file_info('work')
        assert file_info_result_95 is not None
        self.assertEqual(file_info_result_95.parent_id, 'source')
        file_info_result_96 = client.file_info('video')
        assert file_info_result_96 is not None
        self.assertEqual(file_info_result_96.parent_id, 'work')

    def test_cancel_after_nested_extraction_cannot_replay_the_plan(self):
        client, plan = self._nested_archive_plan()
        payload = self._queued_payload(plan)

        def cancel():
            info = client.file_info('video')
            assert info is not None
            if info.parent_id == 'work':
                raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client), cancel_check=cancel)
        self.assertTrue(client.file_info('mid'))
        self.assertTrue(client.file_info('leaf'))
        file_info_result_97 = client.file_info('work')
        assert file_info_result_97 is not None
        self.assertEqual(file_info_result_97.parent_id, 'source')
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: cast(GuangYaClient, client))

    def test_noop_rename_does_not_block_move_of_same_file_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                client = FakeGatewayClient()
                result = self._query(client)
                entry = next((e for e in result.data['items'] if e['name'] == 'Move.mp4'))
                operations = [{'op': 'rename', 'file_id': entry['file_id'], 'new_name': 'Move.mp4'}, {'op': 'move', 'file_id': entry['file_id'], 'target_path': '/target'}]
                if reverse:
                    operations.reverse()
                plan = self._build_plan(cast(GuangYaClient, client), owner='owner', operations=operations, trigger_strm=False)
                self.assertEqual(plan['stats']['total'], 1)
                self.assertEqual(plan['stats']['unchanged'], 1)
                self.assertEqual(plan['operations'][0]['op'], 'move')
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                outcome = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
                self.assertFalse(outcome['partial'])
                self.assertEqual(outcome['stats']['moved'], 1)
                self.assertEqual(outcome['stats']['renamed'], 0)
                file_info_result_98 = client.file_info('move')
                assert file_info_result_98 is not None
                self.assertEqual(file_info_result_98.parent_id, 'target')

    def test_same_destination_relocate_only_renames_and_noop_move_is_omitted(self):
        client = FakeGatewayClient()
        result = self._query(client)
        entries = {e['name']: e['file_id'] for e in result.data['items']}
        plan = self._build_plan(cast(GuangYaClient, client), owner='owner', trigger_strm=False, operations=[{'op': 'move', 'file_id': entries['Move.mp4'], 'target_path': '/source'}, {'op': 'relocate', 'file_id': entries['广告-ABC.mp4'], 'target_path': '/source', 'new_name': 'ABC.mp4'}])
        self.assertEqual(plan['stats']['unchanged'], 1)
        self.assertEqual(plan['stats']['rename'], 1)
        self.assertEqual(plan['stats']['total'], 1)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        with mock.patch.object(client, 'move', side_effect=AssertionError('原地变更不得调用移动')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: cast(GuangYaClient, client))
        self.assertFalse(result['partial'])
        file_info_result_99 = client.file_info('rename')
        assert file_info_result_99 is not None
        self.assertEqual(file_info_result_99.name, 'ABC.mp4')
        file_info_result_100 = client.file_info('rename')
        assert file_info_result_100 is not None
        self.assertEqual(file_info_result_100.parent_id, 'source')

    def test_noop_does_not_hide_snapshot_drift_or_real_name_conflicts(self):
        client = FakeGatewayClient()
        result = self._query(client)
        entry = next((e for e in result.data['items'] if e['name'] == 'Move.mp4'))
        client.rename('move', 'Changed.mp4')
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'rename', 'file_id': entry['file_id'], 'new_name': 'Changed.mp4'}])
        result = self._query(client)
        refs = {e['name']: e['file_id'] for e in result.data['items']}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '占用'):
            self._build_plan(cast(GuangYaClient, client), owner='owner', operations=[{'op': 'rename', 'file_id': refs['Changed.mp4'], 'new_name': 'Changed.mp4'}, {'op': 'rename', 'file_id': refs['广告-ABC.mp4'], 'new_name': 'Changed.mp4'}])
