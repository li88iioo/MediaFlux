"""日志 8885 回归：已入库作品重整只清理旧归档，凭成功审计恢复目录。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import json
from unittest.mock import patch

from app import config
from app import database as db
from app.clients.guangya import GuangYaReadRejected
from app.modules.organize import OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from tests.support import IsolatedDatabaseTestCase
from tests.test_reorganize_source_cleanup import _Cloud, _Scraper


class _ArchiveCloud(_Cloud):
    def __init__(self):
        super().__init__()
        self.nodes.pop("source")
        self.nodes.pop("leaf")
        for file_id, parent_id, name in (
            ("category", "target", "TV"), ("region", "category", "US"),
            ("year", "region", "1994"),
            ("old-root", "year", "Wrong series (1994) {tmdb-1994}"),
            ("old-season", "old-root", "Season1"),
        ):
            self.add(file_id, parent_id, name, is_dir=True)
        self.nodes["video"].parent_id = "old-season"
        self.nodes["video"].name = "Wrong series - S01E01.mkv"
        self.info_reads = []
        self.reject_info = set()

    def file_info(self, file_id):
        self.info_reads.append(file_id)
        if file_id not in self.nodes or file_id in self.reject_info:
            raise GuangYaReadRejected("file_info", code="149")
        return super().file_info(file_id)


class ReorganizeArchiveCleanupTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        # 包含初始化夹具在内禁止真实云盘、TMDB 或通知请求。
        for method in ("connect", "connect_ex"):
            cls.enterClassContext(patch(
                f"socket.socket.{method}", side_effect=AssertionError("测试禁止网络"),
            ))
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            for table in (
                "download_staging_reconcile", "organize_confirmations", "download_request_keys",
                "download_requests", "organize_log", "organize_delete_audit",
            ):
                conn.execute(f"DELETE FROM {table}")
        self.cloud = _ArchiveCloud()
        self.rules = OrganizeRules(
            target_dir_id="target", region_split=False, year_split=False, clean_empty=True,
            link_strm=False, notify_enabled=False, library_notify=False, nsfw_enabled=False,
        )
        self.enterContext(patch.object(config, "get", side_effect=lambda key, default="":
            json.dumps([{"id": "incoming", "name": "Incoming"}])
            if key == "GY_ORGANIZE_SOURCE_DIRS" else default))
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.service = OrganizeCorrectionService(client=cast(GuangYaClient, self.cloud), scraper=cast(TMDBScraper, _Scraper()))
        self.addCleanup(self.service.close)
        video = self.cloud.nodes["video"]
        self.old_path = f"TV/US/1994/{self.cloud.nodes['old-root'].name}/Season1/{video.name}"
        self.log_id = db.add_organize_log(
            source="guangya", original_path="Synthetic source/Season 1", new_path=self.old_path,
            file_id="video", status="success", tmdb_id="1994", provider="tmdb", external_id="1994",
            source_dir_id="source", original_parent_id="leaf", original_name="Original.S01E01.mkv",
            current_parent_id="old-season", current_name=video.name, target_parent_id="old-season",
            media_type="tv", title="Wrong series", year="1994", season=1, episode=1,
            legacy_incomplete=False,
        )
        db.add_organize_log_items(self.log_id, [{
            "file_id": "video", "role": "video", "original_parent_id": "leaf",
            "original_name": "Original.S01E01.mkv", "current_parent_id": "old-season",
            "current_name": video.name, "target_parent_id": "old-season", "target_name": video.name,
            "size": video.size, "etag": video.etag, "status": "success",
        }])

    def _reorganize(self):
        get_organize_log_result = db.get_organize_log(self.log_id)
        assert get_organize_log_result is not None
        version = get_organize_log_result["version"]
        return self.service.reorganize(self.log_id, f"archive-{version}", version, "4242", "tv")

    def _revert(self):
        get_organize_log_result_2 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_2 is not None
        version = get_organize_log_result_2["version"]
        return self.service.revert_latest(self.log_id, f"undo-archive-{version}", version)

    def _add_split_subtitle(self):
        self.cloud.add("subtitle", "incoming", "Wrong series - S01E01.zh.srt")
        subtitle = self.cloud.nodes["subtitle"]
        db.add_organize_log_items(self.log_id, [{
            "file_id": subtitle.file_id, "role": "subtitle",
            "original_parent_id": subtitle.parent_id, "original_name": subtitle.name,
            "current_parent_id": subtitle.parent_id, "current_name": subtitle.name,
            "size": subtitle.size, "etag": subtitle.etag, "status": "success",
        }])

    def _audits(self):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM organize_delete_audit WHERE organize_log_id=? ORDER BY id",
                (self.log_id,),
            )]

    def _assert_retained_success(self, result):
        self.assertTrue(result["success"])
        self.assertEqual(result["empty_dirs_cleaned"], 0)
        self.assertEqual(self.cloud.deleted, [])
        self.assertTrue({"old-season", "old-root"}.issubset(self.cloud.nodes))
        get_organize_log_result_3 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_3 is not None
        self.assertEqual(get_organize_log_result_3["status"], "success")
        self.assertEqual(self.cloud.nodes["video"].parent_id, result["target_dir_id"])
        self.assertFalse({"source", "leaf"}.intersection(self.cloud.info_reads))

    def test_success_cleans_only_empty_archive_after_commit_with_complete_audits(self):
        delete = self.cloud.delete_empty_directory

        def committed_delete(file_id, **guards):
            get_organize_log_result_4 = db.get_organize_log(self.log_id)
            assert get_organize_log_result_4 is not None
            self.assertEqual(get_organize_log_result_4["status"], "success")
            audit = self._audits()[-1]
            self.assertEqual((audit["file_id"], audit["status"]), (file_id, "pending"))
            return delete(file_id, **guards)

        with patch.object(self.cloud, "delete_empty_directory", side_effect=committed_delete):
            result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(result["empty_dirs_cleaned"], 2)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.cloud.deleted, ["old-season", "old-root"])
        self.assertTrue({"incoming", "target", "category", "region", "year"}.issubset(self.cloud.nodes))
        self.assertFalse({"source", "leaf"}.intersection(self.cloud.info_reads))
        self.assertEqual(self.cloud.nodes["video"].parent_id, result["target_dir_id"])
        log = db.get_organize_log(self.log_id)
        assert log is not None
        self.assertEqual((log["status"], log["operation_type"], log["tmdb_id"], log["year"]),
                         ("success", "reorganize", "4242", "2026"))
        self.assertEqual([(a["file_id"], a["file_name"], a["parent_id"], a["status"], a["trigger"])
                          for a in self._audits()], [
            ("old-season", "Season1", "old-root", "success", "reorganize_empty_dir_cleanup"),
            ("old-root", "Wrong series (1994) {tmdb-1994}", "year", "success",
             "reorganize_empty_dir_cleanup"),
        ])
        self.assertTrue(all(a["gcid"] for a in self._audits()))

    def test_shared_season_preserves_other_media_and_archive(self):
        self.cloud.add("other-video", "old-season", "Wrong series - S01E02.mkv")
        other = self.cloud.file_info("other-video")
        self._assert_retained_success(self._reorganize())
        self.assertEqual(self.cloud.file_info("other-video"), other)
        self.assertEqual({a["status"] for a in self._audits()}, {"blocked"})
        self.assertEqual({a["trigger"] for a in self._audits()}, {"reorganize_empty_dir_cleanup"})

    def test_disabled_cleanup_preserves_archive_without_audits_or_source_warning(self):
        self.rules.clean_empty = False
        result = self._reorganize()
        self._assert_retained_success(result)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self._audits(), [])

    def test_archive_capture_read_149_retains_directories_without_failing_reorganize(self):
        self.cloud.reject_info.add("old-season")
        result = self._reorganize()
        self._assert_retained_success(result)
        self.assertTrue(result["warnings"])

    def test_archive_empty_scan_failure_retains_directories_after_successful_move(self):
        self.cloud.fail_empty_scan = "old-season"
        result = self._reorganize()
        self._assert_retained_success(result)
        self.assertTrue(result["warnings"])

    def test_archive_cleanup_read_149_retains_directories_after_successful_move(self):
        move = self.cloud.move

        def reject_after_move(ids, parent):
            result = move(ids, parent)
            self.cloud.reject_info.add("old-season")
            return result

        with patch.object(self.cloud, "move", side_effect=reject_after_move):
            result = self._reorganize()
        self._assert_retained_success(result)
        self.assertTrue(result["warnings"])

    def test_archive_moved_into_source_during_transition_is_preserved(self):
        move = self.cloud.move

        def move_archive_after_video(ids, parent):
            result = move(ids, parent)
            self.cloud.nodes["old-root"].parent_id = "incoming"
            self.cloud._touch("year")
            self.cloud._touch("incoming")
            return result

        with patch.object(self.cloud, "move", side_effect=move_archive_after_video):
            result = self._reorganize()
        self._assert_retained_success(result)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.cloud.nodes["old-root"].parent_id, "incoming")

    def test_failed_reorganize_never_cleans_old_archive(self):
        with (
            patch.object(self.cloud, "move", side_effect=RuntimeError("synthetic move failure")),
            self.assertRaisesRegex(RuntimeError, "synthetic move failure"),
        ):
            self._reorganize()
        self.assertTrue({"old-season", "old-root"}.issubset(self.cloud.nodes))
        self.assertFalse({"old-season", "old-root"}.intersection(self.cloud.deleted))
        self.assertEqual(self.cloud.nodes["video"].parent_id, "old-season")
        self.assertFalse(any(a["trigger"] == "reorganize_empty_dir_cleanup" for a in self._audits()))
        get_organize_log_result_5 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_5 is not None
        self.assertEqual(get_organize_log_result_5["status"], "failed")

    def test_revert_restores_deleted_archive_using_complete_audits_despite_read_149(self):
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, ["old-season", "old-root"])
        for file_id in self.cloud.deleted:
            with self.assertRaises(GuangYaReadRejected) as error:
                self.cloud.file_info(file_id)
            self.assertEqual((error.exception.operation, error.exception.code), ("file_info", "149"))
        self.assertTrue(self._revert()["success"])
        video = self.cloud.nodes["video"]
        season = self.cloud.nodes[video.parent_id]
        root = self.cloud.nodes[season.parent_id]
        self.assertEqual((season.name, root.name, root.parent_id),
                         ("Season1", "Wrong series (1994) {tmdb-1994}", "year"))
        self.assertNotEqual(season.file_id, "old-season")
        self.assertNotEqual(root.file_id, "old-root")
        self.assertEqual(video.name, "Wrong series - S01E01.mkv")
        log = db.get_organize_log(self.log_id)
        assert log is not None
        self.assertEqual((log["status"], log["tmdb_id"], log["year"], log["new_path"]),
                         ("reverted", "1994", "1994", self.old_path))
        self.assertEqual((log["current_parent_id"], log["target_parent_id"]),
                         (season.file_id, season.file_id))
        item = db.list_organize_log_items(self.log_id)[0]
        self.assertEqual((item["current_parent_id"], item["target_parent_id"], item["target_name"]),
                         (season.file_id, season.file_id, video.name))
        with db.get_conn() as conn:
            receipts = conn.execute(
                "SELECT from_parent_id,to_parent_id,status FROM organize_operation_steps "
                "WHERE log_id=? AND action='restore_directory' ORDER BY id", (self.log_id,),
            ).fetchall()
        self.assertEqual([tuple(row) for row in receipts], [
            ("old-root", root.file_id, "success"), ("old-season", season.file_id, "success"),
        ])
        self.assertNotIn("source", self.cloud.nodes)
        self.assertNotIn("leaf", self.cloud.nodes)

    def test_revert_existing_target_conflict_never_restores_another_directory(self):
        """离线云盘：原重整生成真实步骤；整组冲突须先于任何目录恢复。"""
        self._add_split_subtitle()
        self.assertTrue(self._reorganize()["success"])
        self.assertEqual(self.cloud.deleted, ["old-season", "old-root"])
        for is_dir in (False, True):
            with self.subTest(is_dir=is_dir):
                self.cloud.add("foreign", "incoming", "Wrong series - S01E01.zh.srt", is_dir=is_dir)
                before = {key: (node.parent_id, node.name) for key, node in self.cloud.nodes.items()}
                with (
                    patch.object(self.cloud, "create_dir", wraps=self.cloud.create_dir) as create,
                    patch.object(self.cloud, "move", wraps=self.cloud.move) as move,
                    patch.object(self.cloud, "rename", wraps=self.cloud.rename) as rename,
                    self.assertRaisesRegex(RuntimeError, "同名文件"),
                ):
                    self._revert()
                create.assert_not_called()
                move.assert_not_called()
                rename.assert_not_called()
                self.assertEqual(before, {key: (node.parent_id, node.name) for key, node in self.cloud.nodes.items()})

    def test_revert_preflight_preserves_complete_snapshot_and_writes_nothing(self):
        self.assertTrue(self._reorganize()["success"])
        before = self.service.detail(self.log_id)
        with (
            patch.object(self.cloud, "create_dir", wraps=self.cloud.create_dir) as create,
            patch.object(self.cloud, "move", wraps=self.cloud.move) as move,
            patch.object(self.cloud, "rename", wraps=self.cloud.rename) as rename,
        ):
            prepared = self.service.preflight_revert(self.log_id)
        self.assertEqual(prepared["detail"], before)
        self.assertEqual(prepared["snapshot"]["log"]["tmdb_id"], "1994")
        self.assertEqual(self.service.detail(self.log_id), before)
        self.assertNotIn("old-season", self.cloud.nodes)
        create.assert_not_called()
        move.assert_not_called()
        rename.assert_not_called()

    def test_revert_preflight_rejects_changed_member_without_restoring_directories(self):
        self.assertTrue(self._reorganize()["success"])
        self.cloud.nodes["video"].name = "external-change.mkv"
        count = self.cloud.directory_sequence
        with self.assertRaisesRegex(RuntimeError, "文件名已变化"):
            self.service.preflight_revert(self.log_id)
        self.assertEqual(self.cloud.directory_sequence, count)
        self.assertNotIn("old-root", self.cloud.nodes)

    def test_sdk_undo_rechecks_conflict_before_claim_or_background_admission(self):
        """离线 SDK + 真实补偿账本与 service；不是 T26 真实云盘验收。"""
        from app import sdk
        from app.modules import activity_undo
        from app.repositories import agent_compensations as receipts

        self._add_split_subtitle()
        self.assertTrue(self._reorganize()["success"])
        owner = "offline-t26-undo"
        with (
            patch("app.modules.organize_correction.OrganizeCorrectionService", return_value=self.service),
            patch("app.modules.organize_tasks.get_organize_manager") as manager,
            patch.object(self.cloud, "logged_in", True, create=True),
            sdk.call_owner(owner),
        ):
            receipt = sdk.activity.inspect_undo(self.log_id)["data"]["receipt_id"]
            self.assertEqual(activity_undo.prepare_undo(receipt, owner=owner)["preview"]["status"], "confirmation_required")
            self.cloud.add("foreign", "incoming", "Wrong series - S01E01.zh.srt")
            count = self.cloud.directory_sequence
            with self.assertRaisesRegex(activity_undo.UndoError, "同名文件"):
                activity_undo.prepare_undo(receipt, owner=owner)
            with self.assertRaisesRegex(activity_undo.UndoError, "同名文件"):
                sdk.activity.undo(receipt)
        self.assertEqual(receipts.state(receipt, db.media_owner_digest(owner)), "available")
        self.assertEqual(self.cloud.directory_sequence, count)
        self.assertNotIn("old-root", self.cloud.nodes)
        manager.assert_not_called()

    def test_revert_partial_directory_restore_is_failed_not_success(self):
        self.assertTrue(self._reorganize()["success"])
        create = self.cloud.create_dir

        def fail_season(name, parent):
            if name == "Season1":
                raise RuntimeError("offline directory creation failed")
            return create(name, parent)

        with (
            patch.object(self.cloud, "create_dir", side_effect=fail_season),
            self.assertRaisesRegex(RuntimeError, "offline directory creation failed"),
        ):
            self._revert()
        detail = self.service.detail(self.log_id)
        self.assertEqual(detail["status"], "revert_failed")
        self.assertEqual(self.cloud.nodes["video"].parent_id, detail["current_parent_id"])
        restored = [step for step in detail["operations"] if step["action"] == "restore_directory"]
        self.assertEqual({step["status"] for step in restored}, {"success", "failed"})

    def test_queued_sdk_undo_rechecks_conflict_and_never_reports_success(self):
        from app import sdk
        from app.repositories import agent_compensations as receipts

        self._add_split_subtitle()
        self.assertTrue(self._reorganize()["success"])
        owner = "offline-t26-queued"
        callbacks = []
        with (
            patch("app.modules.organize_correction.OrganizeCorrectionService", return_value=self.service),
            patch("app.modules.organize_tasks.get_organize_manager") as manager_factory,
            patch.object(self.cloud, "logged_in", True, create=True),
            sdk.call_owner(owner),
        ):
            manager = manager_factory.return_value
            manager.start_operation.side_effect = lambda _name, _title, callback: (
                callbacks.append(callback) or {"ok": True, "task_id": "offline-t26-queued"})
            receipt = sdk.activity.inspect_undo(self.log_id)["data"]["receipt_id"]
            self.assertEqual(sdk.activity.undo(receipt)["status"], "accepted")
            self.assertEqual(len(callbacks), 1)
            self.cloud.add("foreign", "incoming", "Wrong series - S01E01.zh.srt")
            count = self.cloud.directory_sequence
            with self.assertRaisesRegex(RuntimeError, "同名文件"):
                callbacks[0]()
        self.assertEqual(self.cloud.directory_sequence, count)
        self.assertEqual(receipts.state(receipt, db.media_owner_digest(owner)), "outcome_unknown")
        self.assertNotIn("old-root", self.cloud.nodes)

    def test_revert_reuses_live_original_directories_without_recreating_or_renaming_them(self):
        self.assertTrue(self._reorganize()["success"])
        # 模拟回收站中的原目录已被恢复；成功删除审计仍然保留。
        self.cloud.add("old-root", "year", "Wrong series (1994) {tmdb-1994}", is_dir=True)
        self.cloud.add("old-season", "old-root", "Season1", is_dir=True)
        deleted = list(self.cloud.deleted)
        with patch.object(self.cloud, "create_dir", wraps=self.cloud.create_dir) as create, \
                patch.object(self.cloud, "rename", wraps=self.cloud.rename) as rename:
            self.assertTrue(self._revert()["success"])
        create.assert_not_called()
        self.assertTrue(all(call.args[0] == "video" for call in rename.call_args_list))
        self.assertEqual(self.cloud.deleted, deleted)
        self.assertEqual(self.cloud.nodes["video"].parent_id, "old-season")
        self.assertEqual(self.cloud.nodes["old-season"].parent_id, "old-root")
        self.assertEqual(self.cloud.nodes["old-root"].parent_id, "year")

    def test_reorganize_keeps_completed_download_and_confirmation_accounting(self):
        request_id, _ = db.create_download_request("archive-accounting", "magnet")
        db.update_download_request(
            request_id, targets="guangya", status="completed", gy_status="completed", gy_isolated=1,
            gy_target_dir="source", gy_staging_parent_dir="incoming", gy_staging_name="Synthetic source",
            gy_staging_cleanup_status="completed", organize_started=1, organize_status="completed",
            organize_task_id="archive-task", gy_task_id="cloud-task",
        )
        token = "archive-confirmation"
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id="synthetic", source_name="Synthetic source",
            directory_path="Season 1", payload={
                "source_dir_id": "source", "organize_task_id": "archive-task",
                "download_request_ids": [request_id], "files": [{
                    "file_id": "video", "parent_id": "leaf", "size": 100, "etag": "etag-video",
                }],
            }, expires_at="2099-01-01 00:00:00",
        )
        db.update_organize_confirmation(token, status="completed", result_json=json.dumps({"moved": 1}))
        get_download_request_result = db.get_download_request(request_id)
        assert get_download_request_result is not None
        request = dict(get_download_request_result)
        get_organize_confirmation_result = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result is not None
        confirmation = dict(get_organize_confirmation_result)
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(result["warnings"], [])
        self.assertFalse({"source", "leaf"}.intersection(self.cloud.info_reads))
        get_download_request_result_2 = db.get_download_request(request_id)
        assert get_download_request_result_2 is not None
        self.assertEqual(dict(get_download_request_result_2), request)
        get_organize_confirmation_result_2 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_2 is not None
        self.assertEqual(dict(get_organize_confirmation_result_2), confirmation)

    def test_same_parent_reorganize_does_not_clean_or_audit_active_archive(self):
        first = self._reorganize()
        audits = self._audits()
        result = self._reorganize()
        self.assertTrue(result["success"])
        self.assertEqual(result["target_dir_id"], first["target_dir_id"])
        self.assertEqual(result["empty_dirs_cleaned"], 0)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.cloud.deleted, ["old-season", "old-root"])
        self.assertEqual(self._audits(), audits)
        self.assertFalse({"source", "leaf"}.intersection(self.cloud.info_reads))

    def test_reorganize_revert_reorganize_cleans_restored_archive_without_source_reads(self):
        first = self._reorganize()
        self.assertTrue(first["success"])
        self.assertEqual(first["empty_dirs_cleaned"], 2)
        self.assertTrue(self._revert()["success"])
        season = self.cloud.nodes[self.cloud.nodes["video"].parent_id]
        root = self.cloud.nodes[season.parent_id]
        self.assertNotEqual(season.file_id, "old-season")
        self.assertNotEqual(root.file_id, "old-root")
        log = db.get_organize_log(self.log_id)
        assert log is not None
        self.assertEqual((log["status"], log["new_path"]), ("reverted", self.old_path))
        self.assertEqual((log["current_parent_id"], log["target_parent_id"]),
                         (season.file_id, season.file_id))
        self.assertNotEqual(log["current_parent_id"], log["original_parent_id"])
        self.cloud.info_reads.clear()
        second = self._reorganize()
        self.assertTrue(second["success"])
        self.assertEqual(second["warnings"], [])
        self.assertFalse({"source", "leaf"}.intersection(self.cloud.info_reads))
        self.assertEqual(second["empty_dirs_cleaned"], 2)
        self.assertEqual(self.cloud.deleted,
                         ["old-season", "old-root", season.file_id, root.file_id])
        self.assertFalse({season.file_id, root.file_id, "source", "leaf"}.intersection(self.cloud.nodes))
        self.assertTrue({"category", "region", "year", "target"}.issubset(self.cloud.nodes))
        self.assertEqual(self.cloud.nodes["video"].parent_id, second["target_dir_id"])
        log = db.get_organize_log(self.log_id)
        assert log is not None
        self.assertEqual((log["status"], log["tmdb_id"], log["year"]), ("success", "4242", "2026"))
        self.assertEqual([(a["file_id"], a["status"], a["trigger"]) for a in self._audits()], [
            (file_id, "success", "reorganize_empty_dir_cleanup")
            for file_id in ("old-season", "old-root", season.file_id, root.file_id)
        ])
