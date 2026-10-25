"""Task 12：字幕归一化与回收站删除审计回归测试。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import json
import tempfile
from dataclasses import replace
from pathlib import Path
import unittest
import unittest.mock
from unittest.mock import patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import database as db
from app.modules import strm
from app.clients.guangya import GuangYaFile
from app.modules.subtitle_identity import SubtitleIdentity, plan_subtitle_companions
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_execution import execute_organize_plans
from app.modules.organize_runtime import OrganizeTaskRuntime
from app.modules.organize_postprocess import replacement_delete_block_reason
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.scraper import MatchResult
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
    record_blocked_delete,
)
from tests.test_organize_multiversion import _VariantScraper, _VariantTreeClient
from tests.support import IsolatedDatabaseTestCase


def cloud_file(file_id: str, name: str, *, size: int = 100, etag: str = "") -> GuangYaFile:
    return GuangYaFile(
        file_id=file_id,
        name=name,
        is_dir=False,
        size=size,
        etag=etag,
        parent_id="source",
    )


class SubtitleIdentityTests(unittest.TestCase):
    def test_language_aliases_normalize_to_stable_canonical_values(self):
        cases = {
            "Movie.chs.srt": ("Movie", "zh-Hans"),
            "Movie.SC.ass": ("Movie", "zh-Hans"),
            "Movie.cht.ssa": ("Movie", "zh-Hant"),
            "Movie.tc.sub": ("Movie", "zh-Hant"),
            "Movie.zh-cn.srt": ("Movie", "zh-Hans"),
            "Movie.zh-Hans.srt": ("Movie", "zh-Hans"),
            "Movie.zh-tw.srt": ("Movie", "zh-Hant"),
            "Movie.zh-Hant.srt": ("Movie", "zh-Hant"),
            "Movie.zh.srt": ("Movie", "zh"),
            "Movie.chi.srt": ("Movie", "zh"),
            "Movie.zho.srt": ("Movie", "zh"),
            "Movie.en.srt": ("Movie", "en"),
            "Movie.ENG.srt": ("Movie", "en"),
            "Movie.ja.srt": ("Movie", "ja"),
            "Movie.JPN.srt": ("Movie", "ja"),
            "Movie.ko.srt": ("Movie", "ko"),
            "Movie.KOR.srt": ("Movie", "ko"),
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                identity = SubtitleIdentity.parse(filename)
                self.assertEqual((identity.media_stem, identity.language), expected)

    def test_forced_and_default_markers_have_stable_order(self):
        first = SubtitleIdentity.parse("Movie.default.CHS.forced.SRT")
        second = SubtitleIdentity.parse("Movie.forced.sc.default.srt")

        self.assertEqual(first, second)
        self.assertEqual(first.normalized_suffix, "zh-Hans.forced.default.srt")
        self.assertEqual(first.target_name("Canonical.Movie.2026.mkv"),
                         "Canonical.Movie.2026.zh-Hans.forced.default.srt")

    def test_canonical_language_suffix_round_trips_without_stem_drift(self):
        for language in ("zh-Hans", "zh-Hant"):
            with self.subTest(language=language):
                first = SubtitleIdentity.parse(f"Movie.{language}.forced.srt")
                target = first.target_name("Canonical.Movie.mkv")
                second = SubtitleIdentity.parse(target)
                self.assertEqual(second.media_stem, "Canonical.Movie")
                self.assertEqual(second.language, language)
                self.assertEqual(second.normalized_suffix, f"{language}.forced.srt")

    def test_unknown_tail_tokens_remain_part_of_media_stem(self):
        identity = SubtitleIdentity.parse("Movie.commentary.forced.ass")

        self.assertEqual(identity.media_stem, "Movie.commentary")
        self.assertEqual(identity.language, "")
        self.assertEqual(identity.normalized_suffix, "forced.ass")


class SubtitleCompanionPlanningTests(unittest.TestCase):
    def test_multi_video_directory_assigns_each_subtitle_to_one_exact_episode(self):
        videos = [
            cloud_file("v1", "Show.S01E01.mkv"),
            cloud_file("v2", "Show.S01E02.mkv"),
        ]
        subtitles = [
            cloud_file("s2", "Show.S01E02.eng.default.srt"),
            cloud_file("s1", "Show.S01E01.chs.forced.srt"),
        ]

        result = plan_subtitle_companions(videos, subtitles)

        self.assertEqual(
            [(item.file.file_id, item.video_file_id) for item in result.plans],
            [("s1", "v1"), ("s2", "v2")],
        )
        self.assertEqual(result.skipped, [])

    def test_full_subtitle_stem_exact_match_wins_before_language_suffix_stripping(self):
        result = plan_subtitle_companions(
            [cloud_file("plain", "Movie.mkv"), cloud_file("english-cut", "Movie.en.mkv")],
            [cloud_file("subtitle", "Movie.en.srt")],
        )

        self.assertEqual(len(result.plans), 1)
        self.assertEqual(result.plans[0].video_file_id, "english-cut")
        self.assertEqual(result.skipped, [])

    def test_unrelated_subtitle_is_never_attached_by_loose_prefix(self):
        result = plan_subtitle_companions(
            [cloud_file("v1", "Movie.mkv")],
            [cloud_file("s1", "Movie.Extras.en.srt")],
        )

        self.assertEqual(result.plans, [])
        self.assertEqual(result.skipped[0].reason_code, "unmatched")

    def test_equal_video_stems_are_ambiguous_and_not_guessed(self):
        result = plan_subtitle_companions(
            [cloud_file("v1", "Movie.mkv"), cloud_file("v2", "Movie.mp4")],
            [cloud_file("s1", "Movie.en.srt")],
        )

        self.assertEqual(result.plans, [])
        self.assertEqual(result.skipped[0].reason_code, "ambiguous-video")

    def test_aliases_that_collapse_to_same_target_are_all_skipped(self):
        result = plan_subtitle_companions(
            [cloud_file("v1", "Movie.mkv")],
            [cloud_file("s1", "Movie.zh-cn.srt"), cloud_file("s2", "Movie.zh-Hans.srt")],
        )

        self.assertEqual(result.plans, [])
        self.assertEqual(
            [(item.file.file_id, item.reason_code) for item in result.skipped],
            [("s1", "duplicate-target"), ("s2", "duplicate-target")],
        )


class _AuditedVariantClient(_VariantTreeClient):
    def file_info(self, file_id):
        try:
            _parent, item = self._find(file_id)
            return item
        except AssertionError:
            return None


class SubtitleRecycleAuditUiTests(unittest.TestCase):
    def test_organize_page_exposes_default_off_stable_recycle_switch(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))

        self.assertIn('id="r_recycle_replaced" data-key="GY_ORGANIZE_RECYCLE_REPLACED_ENABLED"', html)
        self.assertIn("同版本替换后移入光鸭回收站", html)
        self.assertNotIn('id="r_recycle_replaced" data-key="GY_ORGANIZE_RECYCLE_REPLACED_ENABLED" data-bool="1" checked', html)
        self.assertNotIn("recycle_replaced_enabled:bool('r_recycle_replaced')", html)
        self.assertIn("fillConfigFields(workspace,config)", html)
        self.assertIn("saveAppConfig(workspace)", html)

    def test_batch_delete_copy_uses_recycle_bin_semantics_not_permanent_delete(self):
        html = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))

        self.assertIn("批量移入光鸭回收站", html)
        self.assertIn("输入 DELETE 确认批量移入回收站", html)
        self.assertNotIn("永久删除所选剧集媒体组", html)
        self.assertNotIn("输入 DELETE 确认永久删除", html)

    def test_logs_show_delete_reason_replacement_and_provider_without_restore_button(self):
        html = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))

        self.assertIn('id="organizeDeleteAudits"', html)
        self.assertIn("audit.reason", html)
        self.assertIn("audit.replacement_name", html)
        self.assertIn("audit.provider_result", html)
        self.assertIn("移入光鸭回收站", html)
        self.assertNotIn('id="organizeRestoreDeleteBtn"', html)


class ManualRecycleBinDeleteTests(IsolatedDatabaseTestCase):
    def _log(self):
        log_id = db.add_organize_log(
            "guangya", "source", "target/Movie.mkv", "video", "success", "1",
            original_parent_id="source", original_name="Movie.mkv",
            current_parent_id="target", current_name="Movie.mkv",
            legacy_incomplete=False,
        )
        db.add_organize_log_items(log_id, [
            {"file_id": "video", "role": "video", "original_parent_id": "source",
             "original_name": "Movie.mkv", "current_parent_id": "target",
             "current_name": "Movie.mkv", "size": 100, "etag": "gcid-video"},
            {"file_id": "sub", "role": "subtitle", "original_parent_id": "source",
             "original_name": "Movie.en.srt", "current_parent_id": "target",
             "current_name": "Movie.en.srt", "size": 2, "etag": "gcid-sub"},
        ])
        return log_id

    def test_manual_delete_preflights_whole_group_and_audits_each_provider_call(self):
        log_id = self._log()
        remote = {
            "video": cloud_file("video", "Movie.mkv", size=100, etag="gcid-video"),
            "sub": cloud_file("sub", "Movie.en.srt", size=2, etag="gcid-sub"),
        }
        for item in remote.values(): item.parent_id = "target"

        class Client:
            def file_info(self, file_id): return remote.get(file_id)
            def list_dir(self, parent_id): return list(remote.values()) if parent_id == "target" else []
            def delete(inner, file_ids):
                audit = db.list_organize_delete_audits(log_id, limit=10)[0]
                self.assertEqual(audit["status"], "pending")
                remote.pop(file_ids[0])
                return True

        service = OrganizeCorrectionService(client=cast(GuangYaClient, Client()), scraper=cast(TMDBScraper, object()))
        version = service.detail(log_id)["version"]
        with patch.object(Organizer, "_post_organize_link") as post_action:
            result = service.delete_group(log_id, "manual-token", version, "DELETE")

        post_action.assert_not_called()
        self.assertIn("无法核验父目录 target", "；".join(result["warnings"]))
        self.assertEqual(result["deleted"], 2)
        self.assertEqual({row["status"] for row in db.list_organize_delete_audits(log_id)}, {"success"})
        self.assertEqual(len(service.detail(log_id)["delete_audits"]), 2)

    def test_batch_delete_preflights_and_claims_every_log_before_any_provider_call(self):
        def add_tv_log(index, file_id, parent_id, gcid):
            log_id = db.add_organize_log(
                "guangya", f"source-{index}", f"target-{index}/Episode.mkv",
                file_id, "success", str(index), media_type="tv",
                original_parent_id=f"source-{index}", original_name="Episode.mkv",
                current_parent_id=parent_id, current_name="Episode.mkv",
                legacy_incomplete=False,
            )
            db.add_organize_log_items(log_id, [{
                "file_id": file_id, "role": "video",
                "original_parent_id": f"source-{index}", "original_name": "Episode.mkv",
                "current_parent_id": parent_id, "current_name": "Episode.mkv",
                "size": 100, "etag": gcid,
            }])
            return log_id

        first_log = add_tv_log(1, "video-1", "target-1", "gcid-1")
        second_log = add_tv_log(2, "video-2", "target-2", "gcid-2")
        video_1 = cloud_file("video-1", "Episode.mkv", size=100, etag="gcid-1")
        video_2 = cloud_file("video-2", "Episode.mkv", size=100, etag="gcid-2")
        duplicate = cloud_file("duplicate", "Copy.mkv", size=100, etag="gcid-2")
        video_1.parent_id = "target-1"
        video_2.parent_id = duplicate.parent_id = "target-2"
        remote = {item.file_id: item for item in (video_1, video_2, duplicate)}
        delete_calls = []

        class Client:
            def file_info(self, file_id): return remote.get(file_id)
            def list_dir(self, parent_id):
                return [item for item in remote.values() if item.parent_id == parent_id]
            def delete(self, file_ids):
                delete_calls.append(list(file_ids))
                remote.pop(file_ids[0], None)
                return True

        service = OrganizeCorrectionService(client=cast(GuangYaClient, Client()), scraper=cast(TMDBScraper, object()))
        entries = [
            {"log_id": first_log, "expected_version": service.detail(first_log)["version"],
             "operation_token": "batch-one"},
            {"log_id": second_log, "expected_version": service.detail(second_log)["version"],
             "operation_token": "batch-two"},
        ]

        result = service.run_batch("delete", entries, "DELETE")

        self.assertFalse(result["success"])
        self.assertEqual(delete_calls, [])
        get_organize_log_result = db.get_organize_log(first_log)
        assert get_organize_log_result is not None
        self.assertEqual(get_organize_log_result["status"], "success")
        get_organize_log_result_2 = db.get_organize_log(second_log)
        assert get_organize_log_result_2 is not None
        self.assertEqual(get_organize_log_result_2["status"], "success")

    def test_batch_delete_version_drift_blocks_before_any_provider_call(self):
        log_ids = []
        remote = {}
        for index in (1, 2):
            file_id = f"claim-video-{index}"
            parent_id = f"claim-target-{index}"
            gcid = f"claim-gcid-{index}"
            log_id = db.add_organize_log(
                "guangya", f"source-{index}", f"target-{index}/Episode.mkv",
                file_id, "success", str(index), media_type="tv",
                original_parent_id=f"source-{index}", original_name="Episode.mkv",
                current_parent_id=parent_id, current_name="Episode.mkv",
                legacy_incomplete=False,
            )
            db.add_organize_log_items(log_id, [{
                "file_id": file_id, "role": "video",
                "original_parent_id": f"source-{index}", "original_name": "Episode.mkv",
                "current_parent_id": parent_id, "current_name": "Episode.mkv",
                "size": 100, "etag": gcid,
            }])
            item = cloud_file(file_id, "Episode.mkv", size=100, etag=gcid)
            item.parent_id = parent_id
            remote[file_id] = item
            log_ids.append(log_id)
        delete_calls = []

        class Client:
            def file_info(self, file_id): return remote.get(file_id)
            def list_dir(self, parent_id):
                return [item for item in remote.values() if item.parent_id == parent_id]
            def delete(self, file_ids):
                delete_calls.append(list(file_ids))
                return True

        service = OrganizeCorrectionService(client=cast(GuangYaClient, Client()), scraper=cast(TMDBScraper, object()))
        entries = [
            {"log_id": log_ids[0], "expected_version": service.detail(log_ids[0])["version"],
             "operation_token": "claim-one"},
            {"log_id": log_ids[1], "expected_version": 999,
             "operation_token": "claim-two"},
        ]

        result = service.run_batch("delete", entries, "DELETE")

        self.assertFalse(result["success"])
        self.assertEqual(delete_calls, [])
        statuses = []
        for log_id in log_ids:
            log = db.get_organize_log(log_id)
            assert log is not None
            statuses.append(log["status"])
        self.assertEqual(statuses, ["success", "success"])

    def test_manual_delete_gcid_size_ambiguity_blocks_before_first_provider_call(self):
        log_id = self._log()
        video = cloud_file("video", "Movie.mkv", size=100, etag="gcid-video")
        sub = cloud_file("sub", "Movie.en.srt", size=2, etag="gcid-sub")
        duplicate = cloud_file("copy", "Copy.mkv", size=100, etag="gcid-video")
        for item in (video, sub, duplicate): item.parent_id = "target"
        client = unittest.mock.Mock()
        client.file_info.side_effect = lambda file_id: {"video": video, "sub": sub}.get(file_id)
        client.list_dir.return_value = [video, sub, duplicate]
        service = OrganizeCorrectionService(client=client, scraper=cast(TMDBScraper, object()))
        version = service.detail(log_id)["version"]

        with self.assertRaisesRegex(RuntimeError, "GCID/size 歧义"):
            service.delete_group(log_id, "manual-token", version, "DELETE")

        client.delete.assert_not_called()
        audits = db.list_organize_delete_audits(log_id)
        self.assertEqual({row["status"] for row in audits}, {"blocked"})
        self.assertTrue(all("GCID/size 歧义" in row["reason"] for row in audits))
        get_organize_log_result_3 = db.get_organize_log(log_id)
        assert get_organize_log_result_3 is not None
        self.assertEqual(get_organize_log_result_3["status"], "success")


class SkippedLogBatchRecycleTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        from app.modules import organize_tasks
        from tests.support import isolated_test_database

        self.enterContext(isolated_test_database())
        self.rules = OrganizeRules(target_dir_id="archive", link_strm=False, clean_empty=False)
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        original_get = organize_tasks.config.get
        self.enterContext(patch.object(organize_tasks.config, "get", side_effect=lambda key, default="":
            '[{"id":"incoming","name":"待整理"}]' if key == "GY_ORGANIZE_SOURCE_DIRS" else original_get(key, default)))
        self.remote = {
            key: GuangYaFile(key, key, True, 0, "directory-v1", parent, updated_at=1)
            for key, parent in (("incoming", "0"), ("archive", "0"), ("media", "incoming"))
        }
        self.client = unittest.mock.Mock(spec=GuangYaClient)
        self.client.logged_in = True
        self.client.supports_guarded_empty_directory_delete = True
        self.client.file_info.side_effect = self.remote.get
        self.client.list_dir.side_effect = lambda parent: [f for f in self.remote.values() if f.parent_id == parent]
        self.deleted: list[str] = []

        def delete(ids):
            for file_id in ids:
                item = self.remote[file_id]
                if item.is_dir and self.client.list_dir(file_id):
                    raise RuntimeError("目录非空，不递归删除")
                self.deleted.append(file_id)
                del self.remote[file_id]
            return True

        def delete_empty(file_id, *, expected_etag, expected_updated_at):
            item = self.remote[file_id]
            self.assertTrue(item.is_dir)
            self.assertEqual((item.etag, item.updated_at), (expected_etag, expected_updated_at))
            return delete([file_id])

        self.client.delete.side_effect = delete
        self.client.delete_empty_directory.side_effect = delete_empty
        self.service = OrganizeCorrectionService(client=self.client, scraper=cast(TMDBScraper, object()))
        self.addCleanup(self.service.close)

    def seed(self, file_id="video", *, parent="media", media_type="", subtitle=False):
        filename = file_id + ".mp4"
        file = GuangYaFile(file_id, filename, False, 100, "etag-" + file_id, parent)
        self.remote[file_id] = file
        members = [file]
        if subtitle:
            sub = GuangYaFile(file_id + "-sub", file_id + ".srt", False, 2, "etag-sub", parent)
            self.remote[sub.file_id] = sub
            members.append(sub)
        log_id = db.add_organize_log(
            "guangya", parent, filename, file_id, "skipped", "", media_type=media_type,
            source_dir_id="incoming", original_parent_id=parent, current_parent_id=parent,
            original_name=filename, current_name=filename, legacy_incomplete=False,
        )
        db.add_organize_log_items(log_id, [{
            "file_id": item.file_id, "role": "video" if item is file else "subtitle",
            "original_parent_id": parent, "current_parent_id": parent,
            "original_name": item.name, "current_name": item.name, "size": item.size, "etag": item.etag,
        } for item in members])
        return log_id

    def entries(self, ids):
        return [{"log_id": log_id, "operation_token": "recycle-" + str(log_id),
                 "expected_version": self.service.detail(log_id)["version"]} for log_id in ids]

    def test_single_selected_unknown_log_recycles_files_and_empty_parent(self):
        log_id = self.seed(subtitle=True)
        result = self.service.run_batch("delete", self.entries([log_id]), "DELETE")
        self.assertTrue(result["success"])
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (2, 1))
        self.assertEqual(self.deleted, ["video-sub", "video", "media"])
        self.assertIn("incoming", self.remote)
        self.assertFalse(result["warnings"])
        self.assertEqual(len(db.list_organize_delete_audits(log_id)), 3)

    def test_shared_parent_is_cleaned_once_after_all_selected_movies(self):
        ids = [self.seed("a", media_type="movie"), self.seed("b", media_type="")]
        result = self.service.run_batch("delete", self.entries(ids), "DELETE")
        self.assertTrue(result["success"])
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (2, 1))
        self.assertEqual(self.deleted, ["a", "b", "media"])
        self.assertFalse(result["warnings"])

    def test_repeated_log_for_same_file_only_sends_one_cloud_delete(self):
        ids = [self.seed(), self.seed()]
        result = self.service.run_batch("delete", self.entries(ids), "DELETE")
        self.assertTrue(result["success"])
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (1, 1))
        self.assertEqual(self.deleted, ["video", "media"])
        self.assertTrue(all(self.service.detail(log_id)["status"] == "deleted" for log_id in ids))

    def test_single_detail_delete_reuses_empty_parent_cleanup(self):
        log_id = self.seed(media_type="movie")
        result = self.service.delete_group(log_id, "single", self.service.detail(log_id)["version"], "DELETE")
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (1, 1))

    def test_nonempty_parent_and_unselected_media_are_preserved(self):
        selected = self.seed("selected")
        self.seed("unselected")
        result = self.service.run_batch("delete", self.entries([selected]), "DELETE")
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (1, 0))
        self.assertEqual(self.deleted, ["selected"])
        self.assertIn("media", self.remote)
        self.assertIn("unselected", self.remote)
        self.assertTrue(result["warnings"])

    def test_permanent_source_and_archive_roots_are_never_deleted(self):
        ids = [self.seed("a", parent="incoming"), self.seed("b", parent="archive")]
        result = self.service.run_batch("delete", self.entries(ids), "DELETE")
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (2, 0))
        self.client.delete_empty_directory.assert_not_called()
        self.assertFalse(result["warnings"])

    def test_changed_parent_identity_is_not_deleted(self):
        log_id = self.seed()
        original_delete = self.client.delete.side_effect

        def move_parent(ids):
            result = original_delete(ids)
            self.remote["media"].parent_id = "elsewhere"
            return result

        self.client.delete.side_effect = move_parent
        result = self.service.run_batch("delete", self.entries([log_id]), "DELETE")
        self.assertEqual(result["empty_dirs_cleaned"], 0)
        self.assertIn("media", self.remote)
        self.assertTrue(result["warnings"])
        self.client.delete_empty_directory.assert_not_called()

    def test_partial_failure_reports_actual_deleted_files_without_parent_cleanup(self):
        log_id = self.seed(subtitle=True)
        original_delete = self.client.delete.side_effect

        def fail_video(ids):
            if ids == ["video"]:
                raise RuntimeError("provider rejected video")
            return original_delete(ids)

        self.client.delete.side_effect = fail_video
        result = self.service.run_batch("delete", self.entries([log_id]), "DELETE")
        self.assertFalse(result["success"])
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (1, 0))
        self.assertEqual(self.service.detail(log_id)["status"], "partial_failed")
        self.assertIn("media", self.remote)

    def test_parent_receiving_new_content_during_cleanup_is_preserved(self):
        log_id = self.seed()
        original_delete_empty = self.client.delete_empty_directory.side_effect

        def add_new_file(file_id, **kwargs):
            self.remote["new"] = GuangYaFile("new", "new.mp4", False, 100, "new-etag", file_id)
            return original_delete_empty(file_id, **kwargs)

        self.client.delete_empty_directory.side_effect = add_new_file
        result = self.service.run_batch("delete", self.entries([log_id]), "DELETE")
        self.assertEqual((result["deleted"], result["empty_dirs_cleaned"]), (1, 0))
        self.assertIn("media", self.remote)
        self.assertIn("new", self.remote)
        self.assertTrue(result["warnings"])

    def test_batch_version_conflict_stops_all_provider_writes(self):
        log_id = self.seed()
        entries = self.entries([log_id])
        entries[0]["expected_version"] += 1
        result = self.service.run_batch("delete", entries, "DELETE")
        self.assertFalse(result["success"])
        self.assertEqual(self.deleted, [])
        self.assertIn("media", self.remote)

    def test_http_batch_accepts_one_skipped_movie_after_auth_and_confirmation(self):
        from app.routes import logs_api

        log_id = self.seed(media_type="movie")
        body = {"action": "delete", "entries": self.entries([log_id]), "confirm": "DELETE"}
        application = FastAPI()
        application.add_middleware(SessionMiddleware, secret_key="isolated-log-delete-test")
        application.include_router(logs_api.router)

        @application.post("/__test/login")
        def login(request: Request):
            request.session["logged_in"] = True
            return {"ok": True}

        manager = unittest.mock.Mock()
        receipts = []

        def start_operation(_label, _description, callback):
            receipts.append(callback())
            return {"ok": True, "task_id": "isolated-delete-task"}

        manager.start_operation.side_effect = start_operation
        with patch.object(logs_api, "OrganizeCorrectionService", return_value=self.service), \
                patch.object(logs_api, "get_organize_manager", return_value=manager), TestClient(application) as client:
            self.assertIn(client.post("/api/logs/organize/batch", json=body).status_code, (401, 403))
            client.post("/__test/login")
            self.assertEqual(client.post("/api/logs/organize/batch", json={**body, "confirm": ""}).status_code, 400)
            manager.start_operation.assert_not_called()
            response = client.post("/api/logs/organize/batch", json=body)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((receipts[0]["deleted"], receipts[0]["empty_dirs_cleaned"]), (1, 1))
        self.assertEqual(self.deleted, ["video", "media"])


class LogDeleteSTRMClosureTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        # 每个用例独立安装目录和数据库，禁止测试副作用流入后续用例。
        from tests.support import isolated_test_database

        self.enterContext(isolated_test_database())
        # 本组文件直接放在永久来源根，只测试定向 STRM 收尾，不删除来源根。
        self.enterContext(patch("app.modules.organize_tasks._protected_organize_root_ids",
                                return_value=({"0", "source"}, "")))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        original_get = strm.get
        self.enterContext(patch.object(strm, "get", side_effect=lambda key, default="":
            str(self.root) if key == "STRM_ROOT" else original_get(key, default)))
        self.rules = OrganizeRules(link_strm=True, emby_refresh=False)
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.sync = self.enterContext(patch.object(Organizer, "_post_organize_link"))
        self.wake = self.enterContext(patch("app.modules.strm_metadata_worker.get_strm_metadata_worker"))
        self.remote: dict[str, GuangYaFile] = {}
        self.client = unittest.mock.Mock(spec=GuangYaClient)
        self.client.file_info.side_effect = self.remote.get
        self.client.list_dir.side_effect = lambda parent: [f for f in self.remote.values() if f.parent_id == parent]
        self.client.delete.side_effect = lambda ids: self.remote.pop(ids[0])
        self.service = OrganizeCorrectionService(client=self.client, scraper=cast(TMDBScraper, object()))
        self.addCleanup(self.service.close)
        self.addCleanup(self.sync.assert_not_called)

    def seed(self, file_id="video", *, subtitle=False, status="skipped"):
        files = [cloud_file(file_id, file_id + ".mkv", etag="gcid-" + file_id)]
        if subtitle:
            files.append(cloud_file(file_id + "-sub", file_id + ".srt", size=2, etag="subtitle"))
        for file in files:
            self.remote[file.file_id] = file
        log_id = db.add_organize_log(
            "guangya", "incoming", files[0].name, file_id, status, "1", media_type="tv",
            original_parent_id="source", original_name=files[0].name,
            current_parent_id="source", current_name=files[0].name, legacy_incomplete=False,
        )
        db.add_organize_log_items(log_id, [{
            "file_id": f.file_id, "role": "video" if f is files[0] else "subtitle",
            "original_parent_id": f.parent_id, "current_parent_id": f.parent_id,
            "original_name": f.name, "current_name": f.name, "size": f.size, "etag": f.etag,
        } for f in files])
        return log_id, files

    def index(self, file, *, source="library", path=None, fingerprint=True):
        metadata = not file.name.endswith(".mkv")
        path = path or self.root / strm.STRM_SUBDIR / source / (
            file.name if metadata else file.file_id + ".strm"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("managed-" + file.file_id)
        db.upsert_strm_index(
            ("guangya-meta:" if metadata else "guangya:") + source,
            file.file_id, file.etag, file.size, file.name, str(path),
            strm._content_fingerprint(path) if fingerprint else "",
        )
        return path

    def delete(self, log_id):
        return self.service.delete_group(log_id, "delete-token", self.service.detail(log_id)["version"], "DELETE")

    def pending(self):
        with db.get_conn() as conn:
            return conn.execute("SELECT * FROM strm_path_cleanup ORDER BY id").fetchall()

    def test_unindexed_skipped_file_does_not_schedule_any_local_work(self):
        log_id, _ = self.seed()
        result = self.delete(log_id)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.pending(), [])
        self.wake.assert_not_called()
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def test_indexed_skipped_group_removes_only_owned_paths_and_hands_off_refresh(self):
        log_id, files = self.seed(subtitle=True)
        paths = [self.index(file) for file in files]
        paths.append(self.index(files[0], source="second-library"))
        _, neighbors = self.seed("neighbor")
        neighbor = self.index(neighbors[0])
        result = self.delete(log_id)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(neighbor.exists())
        self.assertEqual(len(db.list_strm_index("guangya:library")), 1)
        self.assertEqual(db.list_strm_index("guangya-meta:library"), [])
        self.assertEqual(self.pending(), [])
        refresh = db.list_strm_refresh_entries()
        self.assertTrue(refresh)
        self.assertTrue(all(not row["allow_emby"] for row in refresh))
        self.wake.return_value.wake.assert_called_once()
        calls = self.client.delete.call_count
        with self.assertRaises(ValueError):
            self.delete(log_id)
        self.assertEqual(self.client.delete.call_count, calls)
        repeated = strm.cleanup_deleted_cloud_files({f.file_id for f in files}, allow_emby=False)
        self.assertEqual(repeated["cleaned"], 0)
        self.assertEqual(repeated["metadata_cleaned"], 0)
        self.assertEqual(self.client.delete.call_count, calls)

    def test_only_confirmed_cloud_deletions_are_cleaned_after_partial_failure(self):
        log_id, files = self.seed(subtitle=True)
        video, subtitle = [self.index(file) for file in files]

        def delete(ids):
            if ids == ["video"]:
                raise RuntimeError("provider rejected video")
            self.remote.pop(ids[0])

        self.client.delete.side_effect = delete
        with self.assertRaisesRegex(RuntimeError, "provider rejected video"):
            self.delete(log_id)
        self.assertTrue(video.exists())
        self.assertFalse(subtitle.exists())
        log = db.get_organize_log(log_id)
        assert log is not None
        self.assertEqual(log["status"], "partial_failed")
        self.assertEqual(len(db.list_strm_index("guangya:library")), 1)
        self.assertEqual(db.list_strm_index("guangya-meta:library"), [])

    def test_changed_local_file_is_preserved_with_persistent_cleanup_warning(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        path.write_text("user edited contents")
        result = self.delete(log_id)
        self.assertTrue(result["success"])
        self.assertTrue(result["warnings"])
        self.assertEqual(path.read_text(), "user edited contents")
        self.assertEqual(len(self.pending()), 1)
        log = db.get_organize_log(log_id)
        assert log is not None
        self.assertIn("STRM", log["error"])
        self.assertEqual(log["status"], "deleted")
        self.wake.assert_not_called()
        # 人工核对恢复受管内容后，只重试本地收尾，无须重放云盘删除。
        path.write_text("managed-video")
        recovery = strm.cleanup_deleted_cloud_files({"video"}, allow_emby=False)
        self.assertEqual(recovery["cleaned"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.client.delete.call_count, 1)

    def test_missing_fingerprint_never_loses_cleanup_ownership(self):
        log_id, files = self.seed()
        path = self.index(files[0], fingerprint=False)
        result = self.delete(log_id)
        self.assertTrue(path.exists())
        self.assertTrue(result["warnings"])
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]["content_fingerprint"], "")

    def test_shared_local_path_keeps_other_cloud_file_owner(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        other = cloud_file("neighbor", "neighbor.mkv")
        self.index(other, source="other-library", path=path)
        result = self.delete(log_id)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(path.exists())
        self.assertEqual(len(db.list_strm_index("guangya:other-library")), 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def test_outside_root_and_symlink_paths_are_preserved(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        symlink = self.root / strm.STRM_SUBDIR / "link"
        symlink.parent.mkdir(parents=True)
        symlink.symlink_to(outside, target_is_directory=True)
        for file_id, path in (("outside", outside / "outside.strm"), ("linked", symlink / "linked.strm")):
            with self.subTest(file_id=file_id):
                log_id, files = self.seed(file_id)
                self.index(files[0], path=path)
                result = self.delete(log_id)
                self.assertTrue(result["warnings"])
                self.assertTrue(path.is_file())
        self.assertEqual(len(self.pending()), 2)
        self.wake.assert_not_called()

    def test_batch_continues_after_failure_and_cleans_only_successful_objects(self):
        entries, paths = [], []
        for file_id in ("first", "rejected", "last"):
            log_id, files = self.seed(file_id, status="success")
            paths.append(self.index(files[0]))
            entries.append({"log_id": log_id, "operation_token": "batch-" + file_id,
                            "expected_version": self.service.detail(log_id)["version"]})

        def delete(ids):
            if ids == ["rejected"]:
                raise RuntimeError("provider rejected file")
            self.remote.pop(ids[0])

        self.client.delete.side_effect = delete
        result = self.service.run_batch("delete", entries, "DELETE")
        self.assertFalse(result["success"])
        self.assertEqual([r["log_id"] for r in result["completed"]], [entries[0]["log_id"], entries[2]["log_id"]])
        self.assertEqual([r["log_id"] for r in result["failed"]], [entries[1]["log_id"]])
        self.assertEqual([path.exists() for path in paths], [False, True, False])
        self.assertEqual(self.client.delete.call_count, 3)

    def test_batch_keeps_cleanup_warning_in_cloud_success_receipt(self):
        log_id, files = self.seed(status="success")
        path = self.index(files[0])
        path.write_text("changed locally")
        second, _ = self.seed("second", status="success")
        result = self.service.run_batch("delete", [{
            "log_id": item, "operation_token": "batch-warning-" + str(item),
            "expected_version": self.service.detail(item)["version"],
        } for item in (log_id, second)], "DELETE")
        self.assertTrue(result["success"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["completed"][0]["result"]["deleted"], 1)
        self.assertTrue(result["warnings"])
        self.assertTrue(path.exists())

    def test_metadata_only_queue_is_cancelled_without_triggering_sync(self):
        log_id, files = self.seed(subtitle=True)
        db.enqueue_strm_metadata_jobs([{
            "source_id": "library", "file_id": files[1].file_id,
            "filename": files[1].name, "size": files[1].size, "etag": files[1].etag,
        }])
        self.delete(log_id)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM strm_metadata_queue").fetchone()[0], "cancelled")
        self.assertEqual(self.pending(), [])
        self.wake.assert_not_called()

    def test_cleanup_works_after_cloud_rename_with_an_older_index_name(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_index SET filename='previous-name.mkv'")
        result = self.delete(log_id)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(path.exists())

    def test_disabled_strm_link_keeps_existing_behavior(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        self.rules.link_strm = False
        self.delete(log_id)
        self.assertTrue(path.exists())
        self.assertEqual(len(db.list_strm_index("guangya:library")), 1)
        self.wake.assert_not_called()

    def test_cleanup_failure_does_not_replay_successful_cloud_delete(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        with patch.object(strm, "retire_deleted_strm_files", side_effect=RuntimeError("storage secret")):
            result = self.delete(log_id)
        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(result["warnings"])
        self.assertNotIn("storage secret", json.dumps(result))
        log = db.get_organize_log(log_id)
        assert log is not None
        self.assertEqual(log["status"], "deleted")
        self.assertTrue(path.exists())
        self.assertTrue(strm.STRM_OPERATION_LOCK.acquire(blocking=False))
        strm.STRM_OPERATION_LOCK.release()
        self.assertEqual(self.client.delete.call_count, 1)

    def test_cleanup_ack_failure_keeps_receipt_for_idempotent_local_recovery(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        with patch.object(db, "delete_strm_path_cleanup", side_effect=RuntimeError("ACK interrupted")):
            result = self.delete(log_id)
        self.assertTrue(result["warnings"])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.pending()), 1)
        self.assertTrue(db.list_strm_refresh_entries())
        restored = strm.cleanup_deleted_cloud_files({"video"}, allow_emby=False)
        self.assertEqual(restored["cleaned"], 0)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.client.delete.call_count, 1)

    def test_local_cleanup_includes_cloud_success_when_log_item_write_fails(self):
        log_id, files = self.seed()
        path = self.index(files[0])
        with patch.object(db, "update_organize_log_item", side_effect=RuntimeError("log write failed")):
            with self.assertRaisesRegex(RuntimeError, "log write failed"):
                self.delete(log_id)
        self.assertFalse(path.exists())
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.client.delete.call_count, 1)


class AutomaticReplacementDeleteTests(IsolatedDatabaseTestCase):
    def test_blocked_delete_reason_is_redacted_and_bounded(self):
        audit_id = record_blocked_delete(
            trigger="replacement",
            reason=("failed api_key=secret https://x.invalid?a=1&signature=token " * 30),
            candidate=DeleteCandidate("old", "Movie.mkv", "target"),
        )
        row = db.get_organize_delete_audit(audit_id)
        assert row is not None
        self.assertNotIn("secret", row["reason"])
        self.assertNotIn("signature=token", row["reason"])
        self.assertLessEqual(len(row["reason"]), 500)

    def _rules(self, enabled=False):
        return OrganizeRules(
            target_dir_id="target", region_split=False, year_split=False,
            small_file_mb=0, clean_empty=False, link_strm=False,
            notify_enabled=False, library_notify=False, conflict_strategy=2,
            keep_multi_versions=True, keep_remux_variant=False,
            recycle_replaced_enabled=enabled,
        )

    def _run(self, *, enabled=False, client=None):
        incoming = cloud_file(
            "incoming", "Variant.Movie.2026.2160p.SDR.Remux.mkv",
            size=3000, etag="new-gcid",
        )
        existing = cloud_file(
            "existing", "Variant.Movie.2026.2160p.SDR.BluRay.x265.mkv",
            size=2000, etag="old-gcid",
        )
        client = client or _AuditedVariantClient(incoming, existing)
        with patch("app.modules.organize.add_organize_log", return_value=99), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            _plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, _VariantScraper())).organize(
                "source", self._rules(enabled), dry_run=False, post_actions=False
            )
        return client, stats

    def test_recycle_replaced_defaults_off_and_preserves_backup_with_reason(self):
        self.assertFalse(OrganizeRules().recycle_replaced_enabled)

        client, stats = self._run(enabled=False)

        self.assertEqual(stats["moved"], 1)
        self.assertEqual(client.deleted, [])
        self.assertTrue(any("mediaflux-backup" in item.name for item in client.tree["movie"]))
        audit = db.list_organize_delete_audits(organize_log_id=99)[0]
        self.assertEqual(audit["status"], "blocked")
        self.assertIn("开关已关闭", audit["reason"])

    def test_enabled_safe_replacement_audits_then_moves_old_file_to_recycle_bin(self):
        client, stats = self._run(enabled=True)

        self.assertEqual(stats["moved"], 1)
        self.assertEqual(client.deleted, ["existing"])
        audit = db.list_organize_delete_audits(organize_log_id=99)[0]
        self.assertEqual(audit["status"], "success")
        self.assertEqual(audit["replacement_file_id"], "incoming")

    def test_exact_old_file_id_allows_recycle_even_with_same_content_duplicate(self):
        old = cloud_file("old", "old.backup.mkv", size=10, etag="same")
        new = cloud_file("new", "new.mkv", size=20, etag="new")
        duplicate = cloud_file("other", "copy.mkv", size=10, etag="same")

        reason = replacement_delete_block_reason(
            expected_old=old, expected_new=new,
            old_detail=old, new_detail=new,
            target_files=[old, duplicate, new], scan_errors=[],
            move_succeeded=True,
        )

        self.assertEqual(reason, "")

    def test_replacement_verification_retries_until_new_file_is_visible(self):
        old = cloud_file("old", "old.backup.mkv", size=10, etag="same")
        new = cloud_file("new", "new.mkv", size=20, etag="new")
        client = unittest.mock.Mock()
        client.list_dir.side_effect = [[old], [old, new]]
        client.file_info.side_effect = lambda file_id: {
            "old": old, "new": new,
        }.get(file_id)
        organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))

        with patch("app.modules.organize.time.sleep") as sleep:
            files, old_detail, new_detail = organizer._replacement_verification_snapshot(
                "target", "old", "new"
            )

        self.assertEqual({item.file_id for item in files}, {"old", "new"})
        assert old_detail is not None
        self.assertEqual(old_detail.file_id, "old")
        assert new_detail is not None
        self.assertEqual(new_detail.file_id, "new")
        self.assertEqual(client.list_dir.call_count, 2)
        sleep.assert_called_once()

    def test_all_circuit_breaker_reasons_are_fail_closed(self):
        old = cloud_file("old", "old.backup.mkv", size=10, etag="same")
        new = cloud_file("new", "new.mkv", size=20, etag="new")
        cases = [
            ({"scan_errors": ["source unavailable"], "old_detail": old, "new_detail": new,
              "target_files": [old, new]}, "扫描错误"),
            ({"scan_errors": [], "old_detail": None, "new_detail": new,
              "target_files": [old, new]}, "详情不可读"),
            ({"scan_errors": [], "old_detail": old, "new_detail": new,
              "target_files": [old]}, "替换文件缺失"),
            ({"scan_errors": [], "old_detail": old, "new_detail": new,
              "target_files": [old, new], "move_succeeded": False}, "移动失败"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                reason = replacement_delete_block_reason(
                    expected_old=old, expected_new=new,
                    old_detail=kwargs["old_detail"], new_detail=kwargs["new_detail"],
                    target_files=cast(list[GuangYaFile], kwargs["target_files"]),
                    scan_errors=cast(list[str], kwargs["scan_errors"]),
                    move_succeeded=cast(bool, kwargs.get("move_succeeded", True)),
                )
                self.assertIn(expected, reason)


class DeleteAuditPersistenceTests(IsolatedDatabaseTestCase):
    def test_blocked_delete_is_persisted_without_provider_call(self):
        client = unittest.mock.Mock()
        audit_id = record_blocked_delete(
            trigger="replacement", reason="自动回收站开关已关闭",
            candidate=DeleteCandidate("old", "Movie.backup.mkv", "target", 10, "gcid-old"),
            replacement=DeleteCandidate("new", "Movie.mkv", "target", 20, "gcid-new"),
        )

        row = db.get_organize_delete_audit(audit_id)
        assert row is not None
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["reason"], "自动回收站开关已关闭")
        self.assertEqual(row["replacement_file_id"], "new")
        client.delete.assert_not_called()

    def test_init_db_marks_legacy_pending_delete_audit_interrupted_for_manual_review(self):
        audit_id = db.add_organize_delete_audit(
            trigger="manual", file_id="old", file_name="Movie.mkv",
            reason="用户显式确认", status="pending",
            provider_result="等待光鸭回收站响应",
        )

        db.init_db()

        row = db.get_organize_delete_audit(audit_id)
        assert row is not None
        self.assertEqual(row["status"], "interrupted")
        self.assertIn("人工核验", row["provider_result"])
        self.assertIn("人工核验", row["error"])

    def test_audit_exists_before_provider_and_success_result_is_updated(self):
        class Client:
            def delete(inner, file_ids):
                rows = db.list_organize_delete_audits(limit=10)
                self.assertEqual(rows[0]["status"], "pending")
                self.assertEqual(rows[0]["file_id"], "old")
                self.assertEqual(file_ids, ["old"])
                return True

        result = execute_recycle_bin_delete(
            Client(), trigger="replacement", reason="同版本新文件胜出",
            candidate=DeleteCandidate("old", "Movie.backup.mkv", "target", 10, "gcid-old"),
            replacement=DeleteCandidate("new", "Movie.mkv", "target", 20, "gcid-new"),
        )

        row = db.get_organize_delete_audit(result["audit_id"])
        assert row is not None
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["provider"], "guangya")
        self.assertIn("回收站", row["provider_result"])

    def test_missing_audit_update_is_not_a_successful_delete_receipt(self):
        for provider_fails in (False, True):
            with self.subTest(provider_fails=provider_fails):
                calls = []
                class Client:
                    def delete(self, file_ids):
                        calls.append(file_ids)
                        if provider_fails:
                            raise RuntimeError("provider unavailable")
                        return True
                with patch.object(db, "update_organize_delete_audit", return_value=False), \
                     self.assertRaisesRegex(RuntimeError, "删除审计记录更新失败"):
                    execute_recycle_bin_delete(Client(), trigger="manual", reason="测试已确认",
                        candidate=DeleteCandidate("old", "empty", "parent"))
                self.assertEqual(calls, [["old"]], "审计失败不能重发 Provider 删除")
                self.assertEqual(db.list_organize_delete_audits(limit=1)[0]["status"], "pending")

    def test_provider_failure_redacts_secrets_signed_url_and_limits_persisted_text(self):
        secret_token = "task12-secret-token"
        secret_signature = "task12-secret-signature"
        signed_url = (
            "https://download.invalid/media/file.mkv"
            f"?signature={secret_signature}&token={secret_token}"
        )

        class Client:
            def delete(self, _file_ids):
                raise RuntimeError(
                    f"provider failed access_token={secret_token} "
                    f"signature={secret_signature} url={signed_url} " + "x" * 1200
                )

        with self.assertRaises(RuntimeError) as caught:
            execute_recycle_bin_delete(
                Client(), trigger="manual", reason="用户显式确认",
                candidate=DeleteCandidate("old", "Movie.mkv", "target", 10, "gcid-old"),
                organize_log_id=7,
            )

        surfaced = str(caught.exception)
        self.assertNotIn(secret_token, surfaced)
        self.assertNotIn(secret_signature, surfaced)
        self.assertNotIn(signed_url, surfaced)
        self.assertLessEqual(len(surfaced), 500)
        row = db.list_organize_delete_audits(limit=10)[0]
        persisted = f"{row['error']} {row['provider_result']}"
        self.assertNotIn(secret_token, persisted)
        self.assertNotIn(secret_signature, persisted)
        self.assertNotIn(signed_url, persisted)
        self.assertLessEqual(len(row["error"]), 500)
        self.assertLessEqual(len(row["provider_result"]), 500)

    def test_provider_failure_is_preserved_in_audit(self):
        class Client:
            def delete(self, _file_ids):
                raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            execute_recycle_bin_delete(
                Client(), trigger="manual", reason="用户显式确认",
                candidate=DeleteCandidate("old", "Movie.mkv", "target", 10, "gcid-old"),
                organize_log_id=7,
            )

        row = db.list_organize_delete_audits(limit=10)[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("provider unavailable", row["provider_result"])


class DirectoryScrapeOrganizerSecretPersistenceTests(IsolatedDatabaseTestCase):
    SECRET = "opaque-provider-secret"

    @staticmethod
    def _plan(file_id="new"):
        return OrganizePlan(
            file_id=file_id, original_name="Movie.2026.mkv",
            original_path="source", original_parent_id="source",
            size=200, etag=f"etag-{file_id}",
            match=MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie"
            ),
            target_path="电影/Movie (2026) {tmdb-1}",
            new_name="Movie.Canonical.2026.mkv", action="move",
        )

    @staticmethod
    def _stats():
        return {
            "moved": 0, "renamed": 0, "metadata_moved": 0,
            "subtitle_moved": 0, "stopped": 0, "skipped": 0,
            "conflict": 0, "failed": 0,
        }

    def _assert_latest_log_safe(self):
        log = dict(db.list_organize_logs(limit=1)[0])
        items = [dict(item) for item in db.list_organize_log_items(log["id"])]
        serialized = f"{log.get('error', '')} {items}"
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(log["error"], "文件整理失败，请稍后重试")
        self.assertTrue(items)
        self.assertTrue(all(
            item.get("error") == "文件整理失败，请稍后重试"
            for item in items if item.get("status") == "failed"
        ))

    def test_move_failure_does_not_leak_to_logs_or_organize_records(self):
        client = unittest.mock.Mock()
        client.list_dir.return_value = []
        client.move.side_effect = RuntimeError(self.SECRET)
        organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))
        stats = self._stats()

        with patch.object(organizer, "_ensure_dir_chain", return_value="target"), \
                self.assertLogs("app.modules.organize_execution", level="ERROR") as captured:
            execute_organize_plans(organizer,
                [self._plan("move-failure")],
                OrganizeRules(target_dir_id="archive"),
                stats, {}, None,
            )

        serialized = "\n".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertIn("RuntimeError", serialized)
        self._assert_latest_log_safe()

    def test_first_write_failure_stops_remaining_plans_in_same_media_group(self):
        client = unittest.mock.Mock()
        client.list_dir.return_value = []
        client.move.side_effect = RuntimeError(self.SECRET)
        organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))
        stats = self._stats()
        first = self._plan("group-a-1")
        second = self._plan("group-a-2")
        second.original_name = "Movie.2026.E02.mkv"
        second.new_name = "Movie.Canonical.2026.E02.mkv"
        first.source_group_id = second.source_group_id = "group-a"
        first.source_group_path = second.source_group_path = "作品 A"

        with patch.object(organizer, "_ensure_dir_chain", return_value="target"):
            execute_organize_plans(
                organizer, [first, second],
                OrganizeRules(target_dir_id="archive"), stats, {}, None,
            )

        attempted_file_ids = {str(call.args[0]) for call in client.move.call_args_list if call.args}
        self.assertNotIn(second.file_id, attempted_file_ids)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped"], 1)
        logs = [dict(row) for row in db.list_organize_logs(limit=10)]
        statuses = {str(row.get("status") or "") for row in logs}
        self.assertIn("failed", statuses)
        self.assertIn("skipped", statuses)
        self.assertTrue(any(
            "同媒体目录前序写入失败" in str(row.get("error") or "")
            for row in logs
        ))

    def test_rename_failure_and_rollback_failure_do_not_leak_to_logs_or_organize_records(self):
        client = unittest.mock.Mock()
        client.list_dir.return_value = []
        client.move.side_effect = [True, RuntimeError(self.SECRET)]
        client.rename.side_effect = RuntimeError(self.SECRET)
        organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))
        stats = self._stats()

        with patch.object(organizer, "_ensure_dir_chain", return_value="target"), \
                self.assertLogs("app.modules.organize_execution", level="ERROR") as captured:
            execute_organize_plans(organizer,
                [self._plan()],
                OrganizeRules(target_dir_id="archive", rename_enabled=True),
                stats, {}, None,
            )

        serialized = "\n".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertIn("RuntimeError", serialized)
        self.assertEqual(stats["failed"], 1)
        self._assert_latest_log_safe()

    def test_target_read_and_create_failures_do_not_leak_to_logs_or_organize_records(self):
        for stage in ("read", "create"):
            with self.subTest(stage=stage):
                client = unittest.mock.Mock()
                if stage == "read":
                    client.list_dir.side_effect = RuntimeError(self.SECRET)
                else:
                    client.list_dir.return_value = []
                    client.create_dir.side_effect = RuntimeError(self.SECRET)
                organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))
                stats = self._stats()
                with self.assertLogs("app.modules.organize", level="WARNING") as captured:
                    execute_organize_plans(organizer,
                        [self._plan(f"new-{stage}")],
                        OrganizeRules(target_dir_id="archive"),
                        stats, {}, None,
                    )
                serialized = "\n".join(captured.output)
                self.assertNotIn(self.SECRET, serialized)
                self.assertIn("RuntimeError", serialized)
                self._assert_latest_log_safe()

    def test_replacement_delete_provider_failure_is_safe_in_logs_and_delete_audit(self):
        existing = cloud_file("old", "Movie.2026.mkv", size=100, etag="etag-old")
        existing.parent_id = "target"
        incoming = self._plan()
        new_remote = cloud_file("new", incoming.new_name, size=200, etag=incoming.etag)
        new_remote.parent_id = "target"
        old_backup = cloud_file(
            "old", "Movie.2026.mkv.mediaflux-backup-old", size=100, etag="etag-old"
        )
        old_backup.parent_id = "target"

        incoming_source = cloud_file(
            "new", incoming.original_name, size=incoming.size, etag=incoming.etag
        )
        incoming_source.parent_id = "source"
        file_info_calls = {"old": 0, "new": 0}

        def current_file(file_id):
            file_info_calls[file_id] += 1
            if file_id == "old":
                return existing if file_info_calls[file_id] == 1 else old_backup
            return incoming_source if file_info_calls[file_id] <= 2 else new_remote

        client = unittest.mock.Mock()
        client.list_dir.side_effect = [[existing], [old_backup, new_remote]]
        client.file_info.side_effect = current_file
        client.delete.side_effect = RuntimeError(self.SECRET)
        organizer = Organizer(client=client, scraper=cast(TMDBScraper, object()))
        stats = self._stats()

        with patch.object(organizer, "_ensure_dir_chain", return_value="target"), patch.object(
            organizer, "_resolve_variant_conflict",
            return_value=(existing, "replace", "同版本新文件胜出"),
        ), self.assertLogs("app.modules.organize_execution", level="ERROR") as captured:
            execute_organize_plans(organizer,
                [incoming],
                OrganizeRules(
                    target_dir_id="archive", rename_enabled=False,
                    recycle_replaced_enabled=True,
                ),
                stats, {}, None,
            )

        audits = [dict(row) for row in db.list_organize_delete_audits(limit=10)]
        serialized = json.dumps(audits, ensure_ascii=False) + " " + " ".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["status"], "failed")
        self.assertEqual(audits[0]["error"], "光鸭回收站操作失败")
        self.assertEqual(
            audits[0]["provider_result"],
            "光鸭回收站调用失败：光鸭回收站操作失败",
        )
        self.assertIn("RuntimeError", serialized)



class OrganizerSubtitleExecutionTests(unittest.TestCase):
    def test_companion_names_agree_across_cloud_audit_inventory_and_strm(self):
        class SnapshotClient(_VariantTreeClient):
            def list_dir(self, parent_id="0"):
                return [replace(item) for item in super().list_dir(parent_id)]

            def file_info(self, file_id):
                item = super().file_info(file_id)
                return replace(item) if item is not None else None

        for rename_enabled in (False, True):
            with self.subTest(rename_enabled=rename_enabled):
                incoming = cloud_file("incoming", "Variant.Movie.2026.mkv", etag="video")
                client = SnapshotClient(incoming, cloud_file("placeholder", "placeholder.txt"))
                originals = {
                    "sub": "Variant.Movie.2026.chs.default.forced.SRT",
                    "nfo": "variant.movie.2026.nfo",
                }
                client.tree["source"].extend(
                    cloud_file(file_id, name, etag=file_id) for file_id, name in originals.items()
                )
                rules = OrganizeRules(
                    target_dir_id="target", region_split=False, year_split=False,
                    small_file_mb=0, clean_empty=False, link_strm=False,
                    notify_enabled=False, library_notify=False, rename_enabled=rename_enabled,
                )
                runtime = OrganizeTaskRuntime()
                organizer = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, _VariantScraper()))
                bound_names = []
                write_names = {}

                class WriteGuard:
                    def __call__(self, *_args, **_kwargs):
                        pass

                    def bind_target_context(self, _plan, _target, *, companions, target_names):
                        bound_names.extend(target_names)

                    def before_companion_write(self, _plan, item, _target, target_name):
                        write_names[item.file_id] = target_name

                plans, stats = organizer.organize("source", rules, dry_run=True)
                companions = [item for item in client.list_dir("source") if item.file_id in originals]
                subtitles = plan_subtitle_companions([incoming], [item for item in companions if item.file_id == "sub"])
                with patch("app.modules.organize.add_organize_log", return_value=1), patch(
                    "app.modules.organize.add_organize_log_items"
                ) as add_items, patch.object(organizer, "_before_plan_write", WriteGuard(), create=True):
                    execute_organize_plans(
                        organizer, plans, rules, stats, {plans[0].original_path: companions},
                        {"incoming": subtitles.plans}, task_runtime=runtime,
                    )

                self.assertEqual((stats["moved"], stats["failed"], stats["metadata_moved"]), (1, 0, 2))
                expected = dict(originals)
                if rename_enabled:
                    expected["sub"] = SubtitleIdentity.parse(originals["sub"]).target_name(plans[0].new_name)
                    expected["nfo"] = plans[0].new_name.rsplit(".", 1)[0] + ".nfo"
                else:
                    self.assertEqual(client.renames, [])
                self.assertEqual(write_names, expected)
                self.assertEqual(set(bound_names[1:]), set(expected.values()))
                snapshot = runtime.get_inventory("movie")
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                audits = {item["file_id"]: item for item in add_items.call_args.args[1]}
                changes = {item["file_id"]: item for item in stats["strm_changes"]}
                cached = {item.file_id: item for item in snapshot.files}
                for file_id, name in expected.items():
                    with self.subTest(file_id=file_id):
                        remote = client.file_info(file_id)
                        assert remote is not None
                        self.assertEqual((remote.parent_id, remote.name), ("movie", name))
                        self.assertEqual(audits[file_id]["original_name"], originals[file_id])
                        self.assertEqual(audits[file_id]["current_name"], name)
                        self.assertEqual(audits[file_id]["target_name"], name)
                        self.assertEqual(changes[file_id]["name"], name)
                        self.assertEqual(cached[file_id].name, name)

    def test_only_unique_subtitle_is_moved_and_renamed_canonically(self):
        incoming = cloud_file("incoming", "Variant.Movie.2026.mkv", size=2000, etag="new-gcid")
        placeholder = cloud_file("placeholder", "placeholder.txt", size=1)
        client = _VariantTreeClient(incoming, placeholder)
        matching = cloud_file("sub-match", "Variant.Movie.2026.chs.default.forced.srt", etag="sub-gcid")
        unrelated = cloud_file("sub-extra", "Variant.Movie.2026.Extras.en.srt", etag="extra-gcid")
        client.tree["source"].extend([matching, unrelated])
        rules = OrganizeRules(
            target_dir_id="target", region_split=False, year_split=False,
            small_file_mb=0, clean_empty=False, link_strm=False,
            notify_enabled=False, library_notify=False, conflict_strategy=2,
            rename_enabled=True,
        )

        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ) as add_items:
            _plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, _VariantScraper())).organize(
                "source", rules, dry_run=False, post_actions=False
            )

        self.assertIn((("sub-match",), "movie"), client.moves)
        self.assertNotIn((("sub-extra",), "movie"), client.moves)
        subtitle_rename = next(name for file_id, name in client.renames if file_id == "sub-match")
        self.assertTrue(subtitle_rename.endswith(".zh-Hans.forced.default.srt"))
        logged_items = add_items.call_args.args[1]
        subtitle_item = next(item for item in logged_items if item["file_id"] == "sub-match")
        self.assertEqual(subtitle_item["role"], "subtitle")
        self.assertEqual(stats["subtitle_moved"], 1)
        self.assertEqual(stats["subtitle_skipped"], 1)
        self.assertIn("字幕未唯一匹配任何视频", stats["subtitle_reasons"])

    def test_successful_cloud_move_with_log_item_failure_does_not_create_failed_log(self):
        incoming = cloud_file("incoming", "Variant.Movie.2026.mkv", size=2000, etag="new-gcid")
        placeholder = cloud_file("placeholder", "placeholder.txt", size=1)
        client = _VariantTreeClient(incoming, placeholder)
        rules = OrganizeRules(
            target_dir_id="target", region_split=False, year_split=False,
            small_file_mb=0, clean_empty=False, link_strm=False,
            notify_enabled=False, library_notify=False, conflict_strategy=2,
            rename_enabled=True,
        )

        with patch("app.modules.organize.add_organize_log", return_value=77) as add_log, patch(
            "app.modules.organize.add_organize_log_items", side_effect=RuntimeError("db detail failed")
        ), patch("app.modules.organize.db.update_organize_log", return_value=True) as update_log:
            _plans, stats = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, _VariantScraper())).organize(
                "source", rules, dry_run=False, post_actions=False
            )

        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["audit_failures"], 1)
        self.assertEqual(add_log.call_count, 1)
        update_log.assert_called_once_with(
            77, legacy_incomplete=True,
            error="云盘整理成功，但操作明细写入失败，请人工核对",
        )


if __name__ == "__main__":
    unittest.main()
