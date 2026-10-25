"""重新整理成功后复用来源/下载隔离目录收尾；数据库与云盘完全隔离。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app import database as db
from app.clients.guangya import GuangYaFile, GuangYaReadRejected
from app.modules.organize import OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.process_lock import CrossProcessLock
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _Cloud:
    supports_guarded_empty_directory_delete = True

    def __init__(self):
        self.nodes: dict[str, GuangYaFile] = {}
        self.deleted: list[str] = []
        self.fail_empty_scan = ""
        self.fail_delete = ""
        self.fail_move = False
        self.directory_sequence = 0
        self.add("incoming", "0", "Incoming", is_dir=True)
        self.add("source", "incoming", "Synthetic source", is_dir=True)
        self.add("leaf", "source", "Season 1", is_dir=True)
        self.add("target", "0", "Archive", is_dir=True)
        self.add("video", "leaf", "Original.S01E01.mkv")

    def add(self, file_id, parent_id, name, *, is_dir=False):
        self.nodes[file_id] = GuangYaFile(
            file_id, name, is_dir, 0 if is_dir else 100,
            f"etag-{file_id}", parent_id, updated_at=100,
        )

    def file_info(self, file_id):
        return replace(self.nodes[file_id]) if file_id in self.nodes else None

    def list_dir(self, parent_id):
        if parent_id not in self.nodes:
            raise FileNotFoundError(parent_id)
        items = [replace(item) for item in self.nodes.values() if item.parent_id == parent_id]
        if parent_id == self.fail_empty_scan and not items:
            raise RuntimeError("synthetic scan failure")
        return items

    def _touch(self, parent_id):
        parent = self.nodes[parent_id]
        parent.updated_at += 1
        parent.etag = f"directory-version-{parent.updated_at}"

    def create_dir(self, name, parent_id):
        self.directory_sequence += 1
        file_id = f"created-{self.directory_sequence}"
        self.add(file_id, parent_id, name, is_dir=True)
        self._touch(parent_id)
        return file_id

    def move(self, file_ids, parent_id):
        if self.fail_move and parent_id != "leaf":
            raise RuntimeError("synthetic move failure")
        for file_id in file_ids:
            item = self.nodes[file_id]
            self._touch(item.parent_id)
            item.parent_id = parent_id
            self._touch(parent_id)
        return True

    def rename(self, file_id, name):
        self.nodes[file_id].name = name
        self._touch(self.nodes[file_id].parent_id)
        return True

    def delete_empty_directory(self, file_id, *, expected_etag="", expected_updated_at=0):
        info = self.file_info(file_id)
        if info is None or not info.is_dir or self.list_dir(file_id):
            raise RuntimeError("directory is not empty")
        if expected_etag != info.etag or expected_updated_at != info.updated_at:
            raise RuntimeError("stale directory version")
        if file_id == self.fail_delete:
            raise RuntimeError("synthetic delete failure")
        self.nodes.pop(file_id)
        self._touch(info.parent_id)
        self.deleted.append(file_id)
        return True

    def delete(self, file_ids):
        # 失败补偿只会清理本次创建的目标目录，不可用于成功后的来源清理。
        for file_id in file_ids:
            if not file_id.startswith("created-"):
                raise AssertionError("unguarded source delete")
            info = self.file_info(file_id)
            assert info is not None
            self.delete_empty_directory(
                file_id, expected_etag=info.etag, expected_updated_at=info.updated_at,
            )


class _Scraper:
    def match_from_tmdb(self, tmdb_id, media_type):
        return MatchResult(
            tmdb_id=tmdb_id, media_type=media_type, title="Corrected series", year="2026",
            confidence=1.0, locked=True, provider="tmdb", external_id=tmdb_id,
        )

    def parse_media(self, filename, parent_path="", match=None):
        return release_parse_result(
            {"type": "tv", "season": 1, "episode": 1},
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id, media_type, *, force_refresh=False):
        return {"genres": [], "origin_country": ["US"], "original_language": "en"}

    def confirm(self, *args, **kwargs):
        return None


class ReorganizeSourceCleanupTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in (
                "download_staging_reconcile", "organize_confirmations", "download_request_keys",
                "download_requests", "organize_log", "organize_delete_audit",
            ):
                conn.execute(f"DELETE FROM {table}")
        self.cloud = _Cloud()
        self.rules = OrganizeRules(
            target_dir_id="target", region_split=False, year_split=False,
            clean_empty=True, link_strm=False, notify_enabled=False,
            library_notify=False, nsfw_enabled=False,
        )
        self.permanent_sources = [{"id": "incoming", "name": "Incoming"}]
        config_patch = patch.object(config, "get", side_effect=self._config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        rules_patch = patch.object(OrganizeRules, "from_config", return_value=self.rules)
        rules_patch.start()
        self.addCleanup(rules_patch.stop)
        self.service = OrganizeCorrectionService(client=cast(GuangYaClient, self.cloud), scraper=cast(TMDBScraper, _Scraper()))
        self.addCleanup(self.service.close)
        video = self.cloud.nodes["video"]
        self.log_id = db.add_organize_log(
            source="guangya", original_path="Synthetic source/Season 1", new_path="",
            file_id=video.file_id, status="failed", source_dir_id="source",
            original_parent_id=video.parent_id, original_name=video.name,
            current_parent_id=video.parent_id, current_name=video.name,
            media_type="tv", title="Original", legacy_incomplete=False,
        )
        self._add_item("video", "video")

    def _config(self, key, default=""):
        if key == "GY_ORGANIZE_SOURCE_DIRS":
            return json.dumps(self.permanent_sources)
        return default

    def _add_item(self, file_id, role):
        item = self.cloud.nodes[file_id]
        db.add_organize_log_items(self.log_id, [{
            "file_id": item.file_id, "role": role,
            "original_parent_id": item.parent_id, "original_name": item.name,
            "current_parent_id": item.parent_id, "current_name": item.name,
            "size": item.size, "etag": item.etag, "status": "failed",
        }])

    def _reorganize(self):
        get_organize_log_result = db.get_organize_log(self.log_id)
        assert get_organize_log_result is not None
        version = int(get_organize_log_result["version"])
        return self.service.reorganize(
            self.log_id, f"bug1-operation-{version}", version, "4242", "tv",
        )

    def _staging(self, *, status="expired", extra_files=None):
        request_id, _ = db.create_download_request("bug1-staging", "magnet")
        db.update_download_request(
            request_id, targets="guangya", status="completed", gy_status="completed", gy_isolated=1,
            gy_target_dir="source", gy_staging_parent_dir="incoming", gy_staging_name="Synthetic source",
            gy_staging_cleanup_status="retained", organize_started=1,
            organize_status="requires_manual", organize_task_id="bug1-task", gy_task_id="cloud-task",
        )
        token = "bug1-confirmation"
        video = self.cloud.nodes["video"]
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id="synthetic-chat", source_name="Synthetic source",
            directory_path="Season 1", payload={
                "source_dir_id": "source", "source_parent_id": "leaf",
                "organize_task_id": "bug1-task", "download_request_ids": [request_id],
                "rules": {"clean_empty": self.rules.clean_empty},
                "files": [{"file_id": video.file_id, "parent_id": video.parent_id,
                           "size": video.size, "etag": video.etag}, *(extra_files or [])],
            }, expires_at="2099-01-01 00:00:00",
        )
        db.update_organize_confirmation(token, status=status)
        lock = CrossProcessLock("guangya-organize", directory=self.test_db_path.parent)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        manager_patch = patch("app.modules.organize_tasks.get_organize_manager",
                              return_value=SimpleNamespace(_lock=lock))
        manager_patch.start()
        self.addCleanup(manager_patch.stop)
        return request_id, token

    def test_success_cleans_nested_source_and_temporary_root_after_commit(self):
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        self.assertIn("incoming", self.cloud.nodes)
        self.assertIn("target", self.cloud.nodes)
        log = db.get_organize_log(self.log_id)
        assert log is not None
        self.assertEqual((log["status"], log["operation_type"]), ("success", "reorganize"))
        self.assertEqual(self.cloud.nodes["video"].parent_id, result["target_dir_id"])
        with db.get_conn() as conn:
            audits = conn.execute("SELECT file_id,file_name,parent_id,status FROM organize_delete_audit ORDER BY id").fetchall()
        self.assertEqual([(row["file_id"], row["file_name"], row["parent_id"], row["status"]) for row in audits],
                         [("leaf", "Season 1", "source", "success"),
                          ("source", "Synthetic source", "incoming", "success")])

    def test_cleaned_source_is_recreated_for_return_with_companions(self):
        self.cloud.add("subtitle", "leaf", "Original.S01E01.zh.srt")
        self._add_item("subtitle", "subtitle")
        self._reorganize()
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        result = self.service.return_to_source(
            self.log_id, "return-cleaned", self.service.detail(self.log_id)["version"],
        )
        self.assertTrue(result['success'])
        parent = self.cloud.file_info(self.cloud.nodes['video'].parent_id)
        assert parent is not None
        file_info_result = self.cloud.file_info(parent.parent_id)
        assert file_info_result is not None
        self.assertEqual((parent.name, file_info_result.name),
                         ('Season 1', 'Synthetic source'))
        self.assertEqual(self.cloud.nodes['subtitle'].parent_id, parent.file_id)
        self.assertEqual(self.cloud.nodes['video'].name, 'Original.S01E01.mkv')
        get_organize_log_result_2 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_2 is not None
        self.assertEqual(get_organize_log_result_2['current_parent_id'], parent.file_id)
        self.assertNotIn('leaf', self.cloud.nodes)

    def test_confirmation_cleanup_receipts_preserve_return_with_companions(self):
        from app.modules.organize_confirmations import _cleanup_confirmed_source

        self.cloud.add("subtitle", "leaf", "Original.S01E01.zh.srt")
        self._add_item("subtitle", "subtitle")
        # 入库沿真实事务执行，改由确认入口收尾，验证使用同一种可回退删除审计。
        with patch.object(self.service, "_finalize_reorganized_source", return_value=[]):
            self.assertTrue(self._reorganize()["success"])
        stats = {"moved": 1}
        _cleanup_confirmed_source(
            {"source_dir_id": "source", "source_parent_id": "leaf"},
            self.service.organizer, self.rules, stats,
        )
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        self.assertEqual(stats["empty_dirs_cleaned"], 2)
        result = self.service.return_to_source(
            self.log_id, "return-confirmed-cleanup", self.service.detail(self.log_id)["version"],
        )
        self.assertTrue(result["success"])
        parent = self.cloud.file_info(self.cloud.nodes["video"].parent_id)
        assert parent is not None
        file_info_result_2 = self.cloud.file_info(parent.parent_id)
        assert file_info_result_2 is not None
        self.assertEqual((parent.name, file_info_result_2.name),
                         ("Season 1", "Synthetic source"))
        self.assertEqual(self.cloud.nodes["subtitle"].parent_id, parent.file_id)

    def test_cleaned_source_is_recreated_for_revert(self):
        self._reorganize()
        self.assertTrue(self.service.revert_latest(
            self.log_id, "undo-cleaned", self.service.detail(self.log_id)['version'],
        )['success'])
        parent = self.cloud.file_info(self.cloud.nodes['video'].parent_id)
        assert parent is not None
        self.assertEqual(parent.name, 'Season 1')
        self.assertEqual(self.cloud.nodes['video'].name, 'Original.S01E01.mkv')
        self.assertNotIn('leaf', self.cloud.nodes)

    def test_corrections_report_rejected_strm_handoff_without_replaying_cloud_writes(self):
        self.rules.link_strm = True
        actions = (
            self._reorganize,
            lambda: self.service.return_to_source(
                self.log_id, 'return-strm-rejected', self.service.detail(self.log_id)['version'],
            ),
            self._reorganize,
            lambda: self.service.revert_latest(
                self.log_id, 'revert-strm-rejected', self.service.detail(self.log_id)['version'],
            ),
        )
        with patch('app.modules.organize.get', side_effect=lambda key, default='': {
            'GY_STRM_BASE_URL': 'http://synthetic.invalid', 'STRM_ROOT': '/synthetic/strm',
        }.get(key, default)), patch('app.modules.scheduler.get_scheduler') as scheduler:
            scheduler.return_value.trigger.return_value = {
                'ok': False, 'error': 'STRM 调度器正在停止',
            }
            for index, action in enumerate(actions, start=1):
                result = action()
                with self.subTest(action=index):
                    self.assertTrue(result['success'])
                    self.assertIn('STRM 调度器正在停止', '；'.join(result['warnings']))
                    self.assertEqual(scheduler.return_value.trigger.call_count, index)
                    detail = self.service.detail(self.log_id)
                    self.assertEqual(detail['status'], 'success' if index % 2 else 'reverted')
                    self.assertEqual(detail['current_name'], self.cloud.nodes['video'].name)
        self.assertEqual(self.cloud.nodes['video'].name, 'Original.S01E01.mkv')

    def test_correction_strm_handoff_distinguishes_queued_skipped_and_failed(self):
        self.rules.link_strm = True
        with patch('app.modules.organize.get', side_effect=lambda key, default='': {
            'GY_STRM_BASE_URL': 'http://synthetic.invalid', 'STRM_ROOT': '/synthetic/strm',
        }.get(key, default)), patch('app.modules.scheduler.get_scheduler') as scheduler:
            trigger = scheduler.return_value.trigger
            for receipt in ({'ok': True, 'queued': True}, {'ok': False, 'skipped': True}):
                with self.subTest(receipt=receipt):
                    trigger.return_value = receipt
                    self.assertEqual(self.service._run_post_actions(
                        items=[], rules=self.rules, moved=1,
                    ), [])
            trigger.side_effect = RuntimeError('synthetic queue failure')
            warnings = self.service._run_post_actions(items=[], rules=self.rules, moved=1)
            self.assertIn('STRM 联动启动失败', '；'.join(warnings))
            trigger.reset_mock()
            self.rules.link_strm = False
            self.assertEqual(self.service._run_post_actions(items=[], rules=self.rules, moved=1), [])
            trigger.assert_not_called()

    def test_reorganize_after_return_cleans_restored_source_and_can_return_again(self):
        self._reorganize()
        self.service.return_to_source(self.log_id, 'return-first', self.service.detail(self.log_id)['version'])
        restored_leaf = self.cloud.nodes['video'].parent_id
        restored_source = self.cloud.nodes[restored_leaf].parent_id
        self.assertTrue(self._reorganize()['success'])
        self.assertNotIn(restored_leaf, self.cloud.nodes)
        self.assertNotIn(restored_source, self.cloud.nodes)
        self.assertTrue(self.service.return_to_source(
            self.log_id, 'return-second', self.service.detail(self.log_id)['version'],
        )['success'])
        leaf = self.cloud.file_info(self.cloud.nodes['video'].parent_id)
        assert leaf is not None
        self.assertEqual(leaf.name, 'Season 1')
        file_info_result_3 = self.cloud.file_info(leaf.parent_id)
        assert file_info_result_3 is not None
        self.assertEqual(file_info_result_3.name, 'Synthetic source')

    def test_failed_request_does_not_recover_when_remote_member_changed_after_move(self):
        request_id, _ = self._staging()
        db.update_download_request(request_id, organize_started=-1, organize_status='failed')
        move = self.cloud.move
        def changed_after_move(ids, parent):
            result = move(ids, parent)
            self.cloud.nodes['video'].etag = 'external-content-change'
            return result
        with patch.object(self.cloud, 'move', side_effect=changed_after_move):
            self.assertTrue(self._reorganize()['success'])
        get_download_request_result = db.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result['organize_started'], -1)
        self.assertIn('source', self.cloud.nodes)

    def test_return_restores_audited_deleted_directories_when_provider_rejects_old_ids(self):
        self._reorganize()
        read = self.cloud.file_info

        def reject_deleted(file_id):
            if file_id not in self.cloud.nodes:
                raise GuangYaReadRejected("file_info", code="149")
            return read(file_id)

        with patch.object(self.cloud, "file_info", side_effect=reject_deleted):
            result = self.service.return_to_source(
                self.log_id, "return-provider-149", self.service.detail(self.log_id)["version"],
            )
        self.assertTrue(result["success"])
        leaf = self.cloud.nodes[self.cloud.nodes["video"].parent_id]
        parent = self.cloud.nodes[leaf.parent_id]
        self.assertEqual((leaf.name, parent.name, parent.parent_id),
                         ("Season 1", "Synthetic source", "incoming"))

    def test_restore_read_rejection_never_bypasses_parent_or_receipt_proofs(self):
        self._reorganize()
        read, listing = self.cloud.file_info, self.cloud.list_dir
        original_parent = self.cloud.nodes["video"].parent_id
        count = self.cloud.directory_sequence
        for case in ("other_code", "parent_info", "parent_list", "invalid_receipt"):
            with self.subTest(case=case):
                if case == "invalid_receipt":
                    with db.get_conn() as conn:
                        conn.execute("UPDATE organize_delete_audit SET status='failed' WHERE file_id='leaf'")

                def reject(file_id, scenario=case):
                    if file_id not in self.cloud.nodes or (scenario == "parent_info" and file_id == "incoming"):
                        raise GuangYaReadRejected("file_info", code="101" if scenario == "other_code" else "149")
                    return read(file_id)

                def reject_list(file_id, scenario=case):
                    if scenario == "parent_list" and file_id == "incoming":
                        raise GuangYaReadRejected("list_dir", code="149")
                    return listing(file_id)

                with (
                    patch.object(self.cloud, "file_info", side_effect=reject),
                    patch.object(self.cloud, "list_dir", side_effect=reject_list),
                    self.assertRaises(GuangYaReadRejected),
                ):
                    self.service.return_to_source(
                        self.log_id, f"blocked-{case}", self.service.detail(self.log_id)["version"],
                    )
                self.assertEqual(self.cloud.directory_sequence, count)
                self.assertEqual(self.cloud.nodes["video"].parent_id, original_parent)

    def test_restore_still_rejects_audited_directory_moved_by_external_actor(self):
        self._reorganize()
        self.cloud.add("elsewhere", "0", "Other", is_dir=True)
        self.cloud.add("source", "elsewhere", "Synthetic source", is_dir=True)
        count = self.cloud.directory_sequence
        with self.assertRaisesRegex(RuntimeError, "外部移动或改名"):
            self.service.return_to_source(
                self.log_id, "externally-restored", self.service.detail(self.log_id)["version"],
            )
        self.assertEqual(self.cloud.directory_sequence, count)
        self.assertEqual(self.cloud.nodes["source"].parent_id, "elsewhere")

    def test_return_reuses_persisted_restore_receipt_after_file_failure(self):
        self._reorganize()
        self.cloud.fail_move = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic move failure'):
            self.service.return_to_source(self.log_id, 'first-return', self.service.detail(self.log_id)['version'])
        created_count = self.cloud.directory_sequence
        self.cloud.fail_move = False
        self.assertTrue(self.service.return_to_source(
            self.log_id, 'retry-return', self.service.detail(self.log_id)['version'],
        )['success'])
        self.assertEqual(self.cloud.directory_sequence, created_count)

    def test_return_rejects_same_name_directory_without_restore_receipt(self):
        self._reorganize()
        self.cloud.add('external', 'incoming', 'Synthetic source', is_dir=True)
        before = self.cloud.nodes['video'].parent_id
        with self.assertRaisesRegex(RuntimeError, '同名'):
            self.service.return_to_source(self.log_id, 'collision', self.service.detail(self.log_id)['version'])
        self.assertEqual(self.cloud.nodes['video'].parent_id, before)
        self.assertEqual(self.cloud.list_dir('external'), [])

    def test_failed_cleanup_keeps_existing_original_directory_returnable_without_recreation(self):
        self.cloud.fail_delete = 'leaf'
        self._reorganize()
        self.assertIn('leaf', self.cloud.nodes)
        count = self.cloud.directory_sequence
        self.assertTrue(self.service.return_to_source(
            self.log_id, 'return-retained', self.service.detail(self.log_id)['version'],
        )['success'])
        self.assertEqual(self.cloud.nodes['video'].parent_id, 'leaf')
        self.assertEqual(self.cloud.directory_sequence, count)

    def test_return_never_builds_without_successful_complete_delete_receipt(self):
        self._reorganize()
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_delete_audit SET file_name='空目录 leaf',parent_id='' WHERE file_id='leaf'")
        count = self.cloud.directory_sequence
        with self.assertRaisesRegex(RuntimeError, '清理凭据'):
            self.service.return_to_source(self.log_id, 'incomplete', self.service.detail(self.log_id)['version'])
        self.assertEqual(self.cloud.directory_sequence, count)

    def test_unknown_restore_response_is_not_replayed_or_adopted_by_name(self):
        self._reorganize()
        create = self.cloud.create_dir
        def lose_response(name, parent):
            create(name, parent)
            raise TimeoutError('unknown create')
        with (
            patch.object(self.cloud, 'create_dir', side_effect=lose_response),
            self.assertRaisesRegex(TimeoutError, 'unknown create'),
        ):
            self.service.return_to_source(self.log_id, 'lost-create', self.service.detail(self.log_id)['version'])
        count = self.cloud.directory_sequence
        with self.assertRaisesRegex(RuntimeError, '结果未知'):
            self.service.return_to_source(self.log_id, 'retry-create', self.service.detail(self.log_id)['version'])
        self.assertEqual(self.cloud.directory_sequence, count)

    def test_stale_version_cannot_create_restore_directories(self):
        version = self.service.detail(self.log_id)['version']
        self._reorganize()
        count = self.cloud.directory_sequence
        with self.assertRaisesRegex(RuntimeError, '版本'):
            self.service.return_to_source(self.log_id, 'stale', version)
        self.assertEqual(self.cloud.directory_sequence, count)

    def test_failed_request_recovers_only_after_actual_success_and_empty_source(self):
        request_id, token = self._staging()
        db.update_download_request(request_id, organize_started=-1, organize_status='failed', organize_error='move failed')
        result = self._reorganize()
        self.assertTrue(result['success'])
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row['organize_status'], row['organize_started'], row['organize_error']), ('completed', 1, ''))
        get_organize_confirmation_result = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result is not None
        self.assertEqual(get_organize_confirmation_result['status'], 'completed')
        self.assertEqual(row['gy_staging_cleanup_status'], 'completed')
        self.assertEqual(self.cloud.deleted, ['leaf', 'source'])

    def test_failed_request_with_no_confirmation_recovers_and_cleans_source(self):
        request_id, token = self._staging()
        with db.get_conn() as conn:
            conn.execute('DELETE FROM organize_confirmations WHERE token=?', (token,))
        db.update_download_request(request_id, organize_started=-1, organize_status='failed')
        self.assertTrue(self._reorganize()['success'])
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row['organize_status'], row['organize_started'], row['gy_staging_cleanup_status']),
                         ('completed', 1, 'completed'))

    def test_failed_request_keeps_failure_when_unlogged_file_remains(self):
        request_id, _ = self._staging()
        db.update_download_request(request_id, organize_started=-1, organize_status='failed')
        self.cloud.add('unlogged', 'leaf', 'unlogged.mkv')
        self.assertTrue(self._reorganize()['success'])
        get_download_request_result_2 = db.get_download_request(request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2['organize_status'], 'failed')
        self.assertIn('source', self.cloud.nodes)
        self.assertIn('unlogged', self.cloud.nodes)

    def test_request_cancelled_during_reorganize_is_not_resurrected(self):
        request_id, _ = self._staging()
        db.update_download_request(request_id, organize_started=-1, organize_status='failed')
        move = self.cloud.move
        def cancel_after_move(ids, parent):
            result = move(ids, parent)
            db.update_download_request(request_id, status='cancelled')
            return result
        with patch.object(self.cloud, 'move', side_effect=cancel_after_move):
            self.assertTrue(self._reorganize()['success'])
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row['status'], row['organize_status'], row['organize_started']), ('cancelled', 'failed', -1))
        self.assertIn('source', self.cloud.nodes)

    def test_permanent_source_root_is_preserved_but_empty_child_is_cleaned(self):
        self.permanent_sources.append({"id": "source", "name": "Synthetic source"})
        self._reorganize()
        self.assertEqual(self.cloud.deleted, ["leaf"])
        self.assertIn("source", self.cloud.nodes)

    def test_disabled_cleanup_retains_all_source_directories(self):
        self.rules.clean_empty = False
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, [])

    def test_remaining_unselected_file_preserves_source_tree(self):
        self.cloud.add("unselected", "leaf", "readme.txt")
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, [])
        self.assertIn("unselected", self.cloud.nodes)

    def test_all_companions_are_moved_before_source_cleanup(self):
        self.cloud.add("subtitle", "leaf", "Original.S01E01.zh.srt")
        self._add_item("subtitle", "subtitle")
        result = self._reorganize()
        self.assertEqual(self.cloud.nodes["subtitle"].parent_id, result["target_dir_id"])
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])

    def test_move_failure_never_cleans_source_or_closes_confirmation(self):
        self._staging()
        self.cloud.fail_move = True
        with self.assertRaisesRegex(RuntimeError, "synthetic move failure"):
            self._reorganize()
        self.assertIn("source", self.cloud.nodes)
        self.assertIn("leaf", self.cloud.nodes)
        self.assertEqual(self.cloud.nodes["video"].parent_id, "leaf")
        get_organize_confirmation_result_2 = db.get_organize_confirmation("bug1-confirmation")
        assert get_organize_confirmation_result_2 is not None
        self.assertEqual(get_organize_confirmation_result_2["status"], "expired")

    def test_scan_failure_retains_source_without_rolling_back_success(self):
        self.cloud.fail_empty_scan = "leaf"
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertTrue(result["warnings"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_log_result_3 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_3 is not None
        self.assertEqual(get_organize_log_result_3["status"], "success")

    def test_delete_failure_is_audited_and_does_not_roll_back_reorganize(self):
        self.cloud.fail_delete = "leaf"
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertTrue(result["warnings"])
        self.assertEqual(self.cloud.deleted, [])
        with db.get_conn() as conn:
            audit = conn.execute("SELECT status FROM organize_delete_audit WHERE file_id='leaf'").fetchone()
        self.assertEqual(audit["status"], "failed")

    def test_staging_confirmation_is_closed_before_safe_cleanup(self):
        request_id, token = self._staging()
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        confirmation = db.get_organize_confirmation(token)
        assert confirmation is not None
        self.assertEqual(confirmation["status"], "completed")
        self.assertEqual(json.loads(confirmation["result_json"])["previous_confirmation"]["status"], "expired")
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual((request["organize_status"], request["gy_staging_cleanup_status"]),
                         ("completed", "completed"))
        with db.get_conn() as conn:
            states = conn.execute("SELECT status FROM download_staging_reconcile").fetchall()
        self.assertEqual([row["status"] for row in states], ["completed"])

    def test_unresolved_staging_confirmation_blocks_generic_cleanup(self):
        request_id, token = self._staging(status="pending", extra_files=[{
            "file_id": "not-reorganized", "parent_id": "leaf", "size": 100,
        }])
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_confirmation_result_3 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_3 is not None
        self.assertEqual(get_organize_confirmation_result_3["status"], "pending")
        get_download_request_result_3 = db.get_download_request(request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["organize_status"], "requires_manual")

    def test_disabled_staging_cleanup_still_closes_confirmation_and_request(self):
        self.rules.clean_empty = False
        request_id, token = self._staging()
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_confirmation_result_4 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_4 is not None
        self.assertEqual(get_organize_confirmation_result_4["status"], "completed")
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual(request["organize_status"], "completed")
        self.assertEqual(request["gy_staging_cleanup_status"], "skipped")

    def test_completed_staging_without_confirmation_uses_existing_safe_cleanup(self):
        request_id, token = self._staging()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations WHERE token=?", (token,))
        db.update_download_request(request_id, organize_status="completed")
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        get_download_request_result_4 = db.get_download_request(request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["gy_staging_cleanup_status"], "completed")

    def test_completed_staging_with_already_completed_confirmation_is_cleaned(self):
        request_id, token = self._staging(status="completed")
        db.update_organize_confirmation(token, result_json=json.dumps({"moved": 1, "failed": 0}))
        db.update_download_request(request_id, organize_status="completed")
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, ["leaf", "source"])
        get_organize_confirmation_result_5 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_5 is not None
        self.assertEqual(get_organize_confirmation_result_5["status"], "completed")
        get_download_request_result_5 = db.get_download_request(request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["gy_staging_cleanup_status"], "completed")

    def test_manual_staging_without_confirmation_evidence_is_not_force_completed(self):
        request_id, token = self._staging()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations WHERE token=?", (token,))
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, [])
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual((request["organize_status"], request["gy_staging_cleanup_status"]),
                         ("requires_manual", "retained"))

    def test_source_inside_staging_cannot_bypass_confirmation_protection(self):
        request_id, token = self._staging()
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET source_dir_id='leaf' WHERE id=?", (self.log_id,))
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_confirmation_result_6 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_6 is not None
        self.assertEqual(get_organize_confirmation_result_6["status"], "expired")
        get_download_request_result_6 = db.get_download_request(request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6["organize_status"], "requires_manual")

    def test_unsupported_safe_delete_preserves_directories_and_success(self):
        self.cloud.supports_guarded_empty_directory_delete = False
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertTrue(result["warnings"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_log_result_4 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_4 is not None
        self.assertEqual(get_organize_log_result_4["status"], "success")

    def test_failed_business_commit_does_not_run_cleanup_or_confirmation_hook(self):
        self._staging()
        original_update = db.update_organize_log

        def reject_commit(log_id, **fields):
            if fields.get("operation_type") == "reorganize" and fields.get("status") == "success":
                return False
            return original_update(log_id, **fields)

        with patch.object(db, "update_organize_log", side_effect=reject_commit), patch(
            "app.repositories.download_staging_reconcile.reconcile_reorganized_confirmations",
        ) as hook, self.assertRaisesRegex(RuntimeError, "重整业务结果未能持久化"):
            self._reorganize()
        hook.assert_not_called()
        self.assertNotIn("source", self.cloud.deleted)
        self.assertNotIn("leaf", self.cloud.deleted)
        self.assertEqual(self.cloud.nodes["video"].parent_id, "leaf")
        get_organize_confirmation_result_7 = db.get_organize_confirmation("bug1-confirmation")
        assert get_organize_confirmation_result_7 is not None
        self.assertEqual(get_organize_confirmation_result_7["status"], "expired")

    def test_confirmation_hook_failure_preserves_source_and_reports_warning(self):
        with patch(
            "app.repositories.download_staging_reconcile.reconcile_reorganized_confirmations",
            side_effect=RuntimeError("synthetic hook failure"),
        ):
            result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertTrue(result["warnings"])
        self.assertEqual(self.cloud.deleted, [])
        get_organize_log_result_5 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_5 is not None
        self.assertEqual(get_organize_log_result_5["status"], "success")
