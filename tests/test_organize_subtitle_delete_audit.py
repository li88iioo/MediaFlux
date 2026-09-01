"""Task 12：字幕归一化与回收站删除审计回归测试。"""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.subtitle_identity import SubtitleIdentity, plan_subtitle_companions
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_execution import execute_organize_plans
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

        service = OrganizeCorrectionService(client=Client(), scraper=object())
        version = service.detail(log_id)["version"]
        with patch.object(Organizer, "_post_organize_link") as post_action:
            result = service.delete_group(log_id, "manual-token", version, "DELETE")

        post_action.assert_called_once()
        self.assertEqual(post_action.call_args.args[0], {"moved": 2})
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

        service = OrganizeCorrectionService(client=Client(), scraper=object())
        entries = [
            {"log_id": first_log, "expected_version": service.detail(first_log)["version"],
             "operation_token": "batch-one"},
            {"log_id": second_log, "expected_version": service.detail(second_log)["version"],
             "operation_token": "batch-two"},
        ]

        result = service.run_batch("delete", entries, "DELETE")

        self.assertFalse(result["success"])
        self.assertEqual(delete_calls, [])
        self.assertEqual(db.get_organize_log(first_log)["status"], "success")
        self.assertEqual(db.get_organize_log(second_log)["status"], "success")

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

        service = OrganizeCorrectionService(client=Client(), scraper=object())
        entries = [
            {"log_id": log_ids[0], "expected_version": service.detail(log_ids[0])["version"],
             "operation_token": "claim-one"},
            {"log_id": log_ids[1], "expected_version": 999,
             "operation_token": "claim-two"},
        ]

        result = service.run_batch("delete", entries, "DELETE")

        self.assertFalse(result["success"])
        self.assertEqual(delete_calls, [])
        self.assertEqual([db.get_organize_log(log_id)["status"] for log_id in log_ids],
                         ["success", "success"])

    def test_manual_delete_gcid_size_ambiguity_blocks_before_first_provider_call(self):
        log_id = self._log()
        video = cloud_file("video", "Movie.mkv", size=100, etag="gcid-video")
        sub = cloud_file("sub", "Movie.en.srt", size=2, etag="gcid-sub")
        duplicate = cloud_file("copy", "Copy.mkv", size=100, etag="gcid-video")
        for item in (video, sub, duplicate): item.parent_id = "target"
        client = unittest.mock.Mock()
        client.file_info.side_effect = lambda file_id: {"video": video, "sub": sub}.get(file_id)
        client.list_dir.return_value = [video, sub, duplicate]
        service = OrganizeCorrectionService(client=client, scraper=object())
        version = service.detail(log_id)["version"]

        with self.assertRaisesRegex(RuntimeError, "GCID/size 歧义"):
            service.delete_group(log_id, "manual-token", version, "DELETE")

        client.delete.assert_not_called()
        audits = db.list_organize_delete_audits(log_id)
        self.assertEqual({row["status"] for row in audits}, {"blocked"})
        self.assertTrue(all("GCID/size 歧义" in row["reason"] for row in audits))
        self.assertEqual(db.get_organize_log(log_id)["status"], "success")


class AutomaticReplacementDeleteTests(IsolatedDatabaseTestCase):
    def test_blocked_delete_reason_is_redacted_and_bounded(self):
        audit_id = record_blocked_delete(
            trigger="replacement",
            reason=("failed api_key=secret https://x.invalid?a=1&signature=token " * 30),
            candidate=DeleteCandidate("old", "Movie.mkv", "target"),
        )
        row = db.get_organize_delete_audit(audit_id)
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
            _plans, stats = Organizer(client=client, scraper=_VariantScraper()).organize(
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
        organizer = Organizer(client=client, scraper=object())

        with patch("app.modules.organize.time.sleep") as sleep:
            files, old_detail, new_detail = organizer._replacement_verification_snapshot(
                "target", "old", "new"
            )

        self.assertEqual({item.file_id for item in files}, {"old", "new"})
        self.assertEqual(old_detail.file_id, "old")
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
                params = {"expected_old": old, "expected_new": new, "move_succeeded": True}
                params.update(kwargs)
                reason = replacement_delete_block_reason(**params)
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
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["provider"], "guangya")
        self.assertIn("回收站", row["provider_result"])

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
        organizer = Organizer(client=client, scraper=object())
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
        organizer = Organizer(client=client, scraper=object())
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
        organizer = Organizer(client=client, scraper=object())
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
                organizer = Organizer(client=client, scraper=object())
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
        organizer = Organizer(client=client, scraper=object())
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
            _plans, stats = Organizer(client=client, scraper=_VariantScraper()).organize(
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
            _plans, stats = Organizer(client=client, scraper=_VariantScraper()).organize(
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
