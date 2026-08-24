from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.clients.ai_recognition import AIRecognitionInput, AIRecognitionResult
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.discovery.models import ProviderTimeout
from app.modules.download_tracker import DownloadTracker
from app.modules.media_probe import ProbeBudget, probe_media_profile
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.scraper import (
    MatchResult,
    TMDBScraper,
    verified_automatic_identity_proof,
)
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _ImmediateThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


class AutoOrganizeHardeningTests(IsolatedDatabaseTestCase):

    def test_web_rules_override_cannot_lower_saved_automatic_match_policy(self):
        from app.routes.guangya_api import _build_rules

        with patch("app.modules.organize.get") as get_config:
            get_config.side_effect = lambda key, default="": (
                "conservative"
                if key == "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET"
                else default
            )
            rules = _build_rules({
                "target_dir_id": "target",
                "rules": {"automatic_match_preset": "aggressive"},
            })

        self.assertEqual(rules.automatic_match_preset, "conservative")

    def test_empty_directory_cleanup_requires_explicit_safe_capability(self):
        delete_empty = Mock()
        organizer = Organizer(
            client=SimpleNamespace(delete_empty_directory=delete_empty),
            scraper=object(),
        )

        report = organizer._clean_empty_dirs_report([
            ("empty-dir", 2, "dir-etag", 123),
        ])

        self.assertEqual(report["cleaned"], 0)
        self.assertEqual(report["unsupported"], 1)
        self.assertIn("不支持安全空目录清理", report["reasons"][0])
        delete_empty.assert_not_called()

    def test_multibatch_selection_without_all_task_ids_is_not_auto_trackable(self):
        raw = Mock()
        raw.request.side_effect = [
            _Response({"code": 0, "data": {"taskId": "task-a"}}),
            _Response({"code": 0, "data": {}}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            client = GuangYaClient(token_file=Path(tmp) / "missing-token.json")
            raw.token = ""
            raw.refresh_token_value = ""
            raw.token_expires_at = None
            client._raw = raw

            result = client.add_offline_selection("magnet:?xt=urn:btih:test", "target", list(range(501)))

        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertTrue(result["tracking_incomplete"])
        self.assertEqual(result["task_ids"], ["task-a"])

    def test_tracker_waits_for_all_persisted_guangya_tasks(self):
        tracker = DownloadTracker()
        row = {
            "id": 7, "title": "Batch", "status": "downloading", "chat_id": "100",
            "qb_status": "", "gy_status": "downloading", "qb_task_id": "",
            "gy_task_id": "gy-1", "gy_task_ids": json.dumps(["gy-1", "gy-2"]),
            "gy_batch_count": 2, "source_value": "magnet:?xt=urn:btih:test",
            "organize_started": 0, "gy_target_dir": "source", "gy_target_name": "Batch",
        }
        one_done = [
            {"id": "gy-1", "status": "completed", "progress": 1},
            {"id": "gy-2", "status": "downloading", "progress": 0.5},
        ]
        all_done = [
            {"id": "gy-1", "status": "completed", "progress": 1},
            {"id": "gy-2", "status": "completed", "progress": 1},
        ]
        with patch.object(tracker, "_start_organize") as start, patch.object(
            tracker, "_update_backend_log"
        ), patch.object(db, "update_download_request_and_sync_media_admission") as update, patch(
            "app.modules.download_tracker.send"
        ):
            tracker._update_request(row, [], one_done)
            self.assertEqual(update.call_args.kwargs["gy_status"], "downloading")
            start.assert_not_called()
            tracker._update_request(row, [], all_done)
            self.assertEqual(update.call_args.kwargs["gy_status"], "completed")
            start.assert_called_once_with(row)

    def test_tracker_waits_for_expected_staging_files_before_organize(self):
        tracker = DownloadTracker()
        row = {
            "id": 71, "title": "Batch", "status": "downloading", "chat_id": "100",
            "qb_status": "", "gy_status": "downloading", "qb_task_id": "",
            "gy_task_id": "gy-1", "gy_task_ids": json.dumps(["gy-1"]),
            "gy_batch_count": 1, "source_value": "magnet:?xt=urn:btih:test",
            "organize_started": 0, "organize_status": "",
            "gy_target_dir": "staging", "gy_target_name": "Batch",
            "gy_isolated": 1, "gy_staging_parent_dir": "parent",
            "gy_staging_name": "MF-71", "gy_expected_file_count": 13,
            "gy_settle_attempts": 0, "gy_settle_snapshot": "",
            "gy_settle_stable_count": 0,
        }
        done = [{"id": "gy-1", "status": "completed", "progress": 1}]
        client = SimpleNamespace(
            logged_in=True,
            file_info=Mock(return_value=GuangYaFile(
                "staging", "MF-71", True, parent_id="parent", etag="dir",
            )),
            list_dir=Mock(return_value=[
                GuangYaFile(f"f-{index}", f"Episode-{index}.mkv", False, 1024, f"e-{index}", "staging")
                for index in range(3)
            ]),
        )
        with patch("app.modules.download_tracker.GuangYaClient", return_value=client), patch.object(
            tracker, "_start_organize"
        ) as start, patch.object(tracker, "_update_backend_log"), patch.object(
            db, "update_download_request"
        ) as update, patch("app.modules.download_tracker.send"):
            tracker._update_request(row, [], done)

        start.assert_not_called()
        settle_updates = [call.kwargs for call in update.call_args_list if call.kwargs.get("organize_status") == "settling"]
        self.assertEqual(len(settle_updates), 1)
        self.assertEqual(settle_updates[0]["gy_settle_observed_file_count"], 3)
        self.assertIn("3/13", settle_updates[0]["organize_error"])

    def test_tracker_starts_organize_after_expected_staging_files_arrive(self):
        tracker = DownloadTracker()
        files = [
            GuangYaFile(f"f-{index}", f"Episode-{index}.mkv", False, 1024, f"e-{index}", "staging")
            for index in range(3)
        ]
        snapshot_client = SimpleNamespace(list_dir=Mock(return_value=files))
        _, settled_snapshot = tracker._scan_staging_snapshot(snapshot_client, "staging")
        row = {
            "id": 72, "title": "Batch", "status": "downloading", "chat_id": "100",
            "qb_status": "", "gy_status": "downloading", "qb_task_id": "",
            "gy_task_id": "gy-1", "gy_task_ids": json.dumps(["gy-1"]),
            "gy_batch_count": 1, "source_value": "magnet:?xt=urn:btih:test",
            "organize_started": 0, "organize_status": "settling",
            "gy_target_dir": "staging", "gy_target_name": "Batch",
            "gy_isolated": 1, "gy_staging_parent_dir": "parent",
            "gy_staging_name": "MF-72", "gy_expected_file_count": 3,
            "gy_settle_attempts": 1, "gy_settle_snapshot": settled_snapshot,
            "gy_settle_stable_count": 1,
        }
        done = [{"id": "gy-1", "status": "completed", "progress": 1}]
        client = SimpleNamespace(
            logged_in=True,
            file_info=Mock(return_value=GuangYaFile(
                "staging", "MF-72", True, parent_id="parent", etag="dir",
            )),
            list_dir=Mock(return_value=files),
        )
        with patch("app.modules.download_tracker.GuangYaClient", return_value=client), patch.object(
            tracker, "_start_organize"
        ) as start, patch.object(tracker, "_update_backend_log"), patch.object(
            db, "update_download_request"
        ), patch("app.modules.download_tracker.send"):
            tracker._update_request(row, [], done)

        start.assert_called_once_with(row)

    def test_tracker_does_not_start_when_expected_count_is_visible_but_snapshot_is_new(self):
        tracker = DownloadTracker()
        row = {
            "id": 73, "title": "Batch", "status": "downloading", "chat_id": "100",
            "qb_status": "", "gy_status": "downloading", "qb_task_id": "",
            "gy_task_id": "gy-1", "gy_task_ids": json.dumps(["gy-1"]),
            "gy_batch_count": 1, "source_value": "magnet:?xt=urn:btih:test",
            "organize_started": 0, "organize_status": "settling",
            "gy_target_dir": "staging", "gy_target_name": "Batch",
            "gy_isolated": 1, "gy_staging_parent_dir": "parent",
            "gy_staging_name": "MF-73", "gy_expected_file_count": 3,
            "gy_settle_attempts": 1, "gy_settle_snapshot": "",
            "gy_settle_stable_count": 0,
        }
        done = [{"id": "gy-1", "status": "completed", "progress": 1}]
        client = SimpleNamespace(
            logged_in=True,
            file_info=Mock(return_value=GuangYaFile(
                "staging", "MF-73", True, parent_id="parent", etag="dir",
            )),
            list_dir=Mock(return_value=[
                GuangYaFile(f"f-{index}", f"Episode-{index}.mkv", False, 1024, f"e-{index}", "staging")
                for index in range(3)
            ]),
        )
        with patch("app.modules.download_tracker.GuangYaClient", return_value=client), patch.object(
            tracker, "_start_organize"
        ) as start, patch.object(tracker, "_update_backend_log"), patch.object(
            db, "update_download_request"
        ) as update, patch("app.modules.download_tracker.send"):
            tracker._update_request(row, [], done)

        start.assert_not_called()
        settle = next(
            call.kwargs for call in update.call_args_list
            if call.kwargs.get("organize_status") == "settling"
        )
        self.assertEqual(settle["gy_settle_stable_count"], 1)
        self.assertEqual(settle["strm_status"], "")

    def test_tracker_matches_unverified_magnet_by_unique_staging_directory(self):
        row = {
            "gy_task_id": "", "title": "磁力任务", "source_value": "magnet:?xt=urn:btih:test",
            "gy_target_dir": "staging-21",
        }
        tasks = [
            {"id": "a", "name": "磁力任务", "target_dir": "staging-other", "raw": {}},
            {"id": "b", "name": "磁力任务", "target_dir": "staging-21", "raw": {}},
        ]

        self.assertEqual(DownloadTracker._match_gy(row, tasks)["id"], "b")

    def test_tracker_respects_organize_backoff_while_other_backend_is_downloading(self):
        tracker = DownloadTracker()
        row = {
            "id": 8, "title": "Both", "status": "downloading", "chat_id": "100",
            "qb_status": "downloading", "gy_status": "completed", "qb_task_id": "qb-1",
            "gy_task_id": "gy-1", "gy_task_ids": "[]", "gy_batch_count": 0,
            "source_value": "magnet:?xt=urn:btih:test", "organize_started": 0,
            "organize_status": "queued", "gy_target_dir": "source", "gy_target_name": "Both",
            "organize_next_retry_at": (
                datetime.now() + timedelta(minutes=5)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with patch.object(tracker, "_start_organize") as start, patch.object(
            tracker, "_update_backend_log"
        ), patch.object(db, "update_download_request_and_sync_media_admission"), patch(
            "app.modules.download_tracker.send"
        ):
            tracker._update_request(row, [], [])

        start.assert_not_called()

    def test_tracker_does_not_fallback_to_title_when_qb_hash_is_persisted(self):
        row = {"qb_task_id": "expected-hash", "title": "Same title"}
        tasks = [SimpleNamespace(hash="other-hash", name="Same title")]

        self.assertIsNone(DownloadTracker._match_qb(row, tasks))

    def test_tracker_keeps_legacy_qb_title_fallback_without_hash(self):
        row = {"qb_task_id": "", "title": "Same title"}
        task = SimpleNamespace(hash="other-hash", name="Same title")

        self.assertIs(DownloadTracker._match_qb(row, [task]), task)

    def test_tracker_does_not_title_match_http_submission_without_task_id(self):
        row = {"kind": "http", "qb_task_id": "", "title": "Same title"}
        task = SimpleNamespace(hash="other-hash", name="Same title")

        self.assertIsNone(DownloadTracker._match_qb(row, [task]))

    def test_tracker_marks_untrackable_ed2k_submission_for_manual_review(self):
        tracker = DownloadTracker()
        row = {
            "id": 91, "kind": "ed2k", "title": "Legacy link",
            "status": "submitted", "chat_id": "",
            "qb_status": "submitted", "gy_status": "",
            "qb_task_id": "", "gy_task_id": "", "organize_started": 0,
        }
        with patch.object(tracker, "_update_backend_log"), patch.object(
            tracker, "_notify_completion"
        ), patch.object(db, "update_download_request_and_sync_media_admission") as update:
            tracker._update_request(row, [], [], qb_available=True, gy_available=False)

        self.assertEqual(update.call_args.kwargs["qb_status"], "manual_review")
        self.assertEqual(update.call_args.kwargs["status"], "manual_review")
        self.assertIn("未返回可跟踪任务标识", update.call_args.kwargs["error"])

    def test_tracker_does_not_fallback_when_guangya_id_is_persisted(self):
        row = {
            "gy_task_id": "expected-id", "title": "Same title",
            "source_value": "magnet:?xt=urn:btih:same",
        }
        tasks = [{
            "id": "other-id", "name": "Same title",
            "raw": {"url": "magnet:?xt=urn:btih:same"},
        }]

        self.assertIsNone(DownloadTracker._match_gy(row, tasks))

    def test_tracker_keeps_legacy_guangya_fallback_without_id(self):
        row = {
            "gy_task_id": "", "title": "Same title",
            "source_value": "magnet:?xt=urn:btih:same",
        }
        source_task = {
            "id": "source-match", "name": "Other",
            "raw": {"url": "magnet:?xt=urn:btih:same"},
        }
        title_task = {"id": "title-match", "name": "Same title", "raw": {}}

        self.assertIs(DownloadTracker._match_gy(row, [source_task, title_task]), source_task)
        row["source_value"] = ""
        self.assertIs(DownloadTracker._match_gy(row, [source_task, title_task]), title_task)

    def test_automatic_plan_requires_strict_confidence(self):
        scraper = Mock()
        scraper.supports_parent_path = True
        scraper.match.return_value = MatchResult(
            tmdb_id="100", title="Example", year="2026", media_type="movie",
            confidence=0.75, status="matched", need_confirm=False,
        )
        organizer = Organizer(client=Mock(), scraper=scraper)
        file = GuangYaFile("f", "Example.2026.mkv", False, 100, "etag", "source")

        plan = organizer._plan_one(file, "", OrganizeRules(), automatic=True)

        self.assertEqual(plan.action, "skip")
        self.assertTrue(plan.match.need_confirm)
        self.assertIn("90%", plan.note)

    @staticmethod
    def _verified_episode_match(*, target_episode: int = 3) -> MatchResult:
        validation = {
            "required": True,
            "passed": True,
            "season": 1,
            "episode": target_episode,
            "episode_count": 12,
            "reason": "episode_verified",
        }
        return MatchResult(
            tmdb_id="9001",
            external_id="9001",
            provider="tmdb",
            title="Example Show",
            year="2024",
            media_type="tv",
            confidence=0.85,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="匹配置信度 85% 低于严格模式阈值 90%",
            metadata={
                "verified_automatic_identity_proof": {
                    "version": 2,
                    "kind": "tmdb_tv_episode_identity",
                    "provider": "tmdb",
                    "external_id": "9001",
                    "media_type": "tv",
                    "confidence": 0.85,
                    "recognition_threshold": 0.9,
                    "automatic_match_preset": "balanced",
                    "global_threshold": 0.9,
                    "strong_title_score": 0.85,
                    "candidate_count": 1,
                    "candidate_gap": 1.0,
                    "decision_constraints": [],
                    "selected_constraints": [],
                    "expected_year": "",
                    "candidate_year": "2024",
                    "source_title_key": "exampleshow",
                    "matched_title_key": "exampleshow",
                    "source_position": {"season": 1, "episode": 3},
                    "target_position": {
                        "season": 1, "episode": target_episode,
                    },
                    "position_validation": validation,
                },
            },
        )

    @staticmethod
    def _proof_scraper(match: MatchResult) -> TMDBScraper:
        client = SimpleNamespace(
            api_key="test-key",
            base_url="https://tmdb.test/3",
            config_error="",
            session=None,
        )
        scraper = TMDBScraper(client=client)
        scraper.match_mode = "strict"
        scraper.get_detail = Mock(return_value={
            "id": 9001,
            "genres": [],
            "origin_country": [],
            "first_air_date": "2024-01-01",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        })
        scraper.match = Mock(return_value=match)
        return scraper

    def test_verified_low_score_episode_can_pass_only_in_automatic_planning(self):
        match = self._verified_episode_match()
        scraper = self._proof_scraper(match)
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example.Show.S01E03.1080p.mkv", False, 100, "etag", "source"
        )

        automatic = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )
        manual = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=False
        )

        self.assertNotEqual(automatic.action, "skip")
        self.assertEqual(automatic.match.status, "matched")
        self.assertFalse(automatic.match.need_confirm)
        self.assertEqual((automatic.season, automatic.episode), (1, 3))
        self.assertEqual(
            automatic.match.metadata[
                "verified_automatic_identity_proof_accepted"
            ]["episode"],
            3,
        )
        self.assertEqual(manual.action, "skip")
        self.assertTrue(manual.match.need_confirm)

    def test_automatic_final_position_refreshes_stale_tmdb_detail_once(self):
        match = MatchResult(
            tmdb_id="9001",
            external_id="9001",
            provider="tmdb",
            title="Example Show",
            year="2024",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="matched",
            need_confirm=False,
        )
        scraper = self._proof_scraper(match)
        scraper.get_detail.side_effect = [
            {
                "id": 9001,
                "genres": [],
                "origin_country": [],
                "seasons": [{"season_number": 1, "episode_count": 12}],
            },
            {
                "id": 9001,
                "genres": [],
                "origin_country": [],
                "seasons": [{"season_number": 1, "episode_count": 13}],
            },
        ]
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example.Show.S01E13.1080p.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertNotEqual(plan.action, "skip")
        self.assertEqual((plan.season, plan.episode), (1, 13))
        self.assertTrue(plan.match.metadata["tmdb_detail_force_refreshed"])
        self.assertEqual(scraper.get_detail.call_count, 2)
        self.assertEqual(
            scraper.get_detail.call_args_list[-1].kwargs.get("force_refresh"), True
        )

    def test_verified_bare_episode_proof_uses_tmdb_validated_season_one(self):
        match = self._verified_episode_match()
        match.threshold = 0.6
        proof = match.metadata["verified_automatic_identity_proof"]
        proof["recognition_threshold"] = 0.6
        proof["source_position"] = {"season": None, "episode": 3}
        scraper = self._proof_scraper(match)
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example Show - 03 [1080p].mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertNotEqual(plan.action, "skip")
        self.assertEqual((plan.season, plan.episode), (1, 3))
        self.assertEqual(plan.match.status, "matched")
        self.assertFalse(plan.match.need_confirm)

    def test_verified_bare_absolute_episode_proof_uses_mapped_tmdb_position(self):
        match = self._verified_episode_match(target_episode=1073)
        proof = match.metadata["verified_automatic_identity_proof"]
        validation = {
            "required": True,
            "passed": True,
            "season": 2,
            "episode": 1073,
            "episode_count": 1100,
            "reason": "episode_verified",
        }
        mapping = {
            "source_season": None,
            "source_episode": 1173,
            "target_season": 2,
            "target_episode": 1073,
            "mode": "absolute",
            "reason": "absolute_episode_mapping",
            "confidence": 1.0,
            "range_start": None,
            "range_end": None,
            "changed": True,
            "label": "按绝对集数映射",
        }
        proof["source_position"] = {"season": None, "episode": 1173}
        proof["target_position"] = {"season": 2, "episode": 1073}
        proof["position_validation"] = validation
        proof["episode_mapping"] = mapping
        match.metadata["episode_mapping"] = mapping
        scraper = self._proof_scraper(match)
        scraper.get_detail = Mock(return_value={
            "id": 9001,
            "genres": [],
            "origin_country": [],
            "first_air_date": "2024-01-01",
            "seasons": [
                {"season_number": 1, "episode_count": 100},
                {"season_number": 2, "episode_count": 1100},
            ],
        })
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example Show - 1173 [1080p].mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertNotEqual(plan.action, "skip")
        self.assertIsNone(match.metadata["episode_mapping"]["source_season"])
        self.assertEqual(match.metadata["episode_mapping"]["source_episode"], 1173)
        self.assertEqual((plan.season, plan.episode), (2, 1073))
        self.assertEqual(plan.match.status, "matched")
        self.assertFalse(plan.match.need_confirm)

    @staticmethod
    def _merged_cour_episodes():
        episodes = []
        first_start = datetime(2026, 1, 11)
        second_start = datetime(2026, 7, 5)
        for number in range(1, 26):
            aired_on = (
                first_start + timedelta(days=7 * (number - 1))
                if number <= 12
                else second_start + timedelta(days=7 * (number - 13))
            )
            episodes.append({
                "episode_number": number,
                "air_date": aired_on.date().isoformat(),
            })
        return episodes

    def test_explicit_tmdb_marker_maps_publisher_second_cour_into_merged_season(self):
        client = SimpleNamespace(
            api_key="test-key",
            base_url="https://tmdb.test/3",
            config_error="",
            session=None,
        )
        scraper = TMDBScraper(client=client)
        scraper.get_detail = Mock(return_value={
            "id": 278043,
            "name": "正相反的你与我",
            "first_air_date": "2026-01-11",
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": 25}],
        })
        scraper.get_tv_season_detail = Mock(return_value={
            "season_number": 1,
            "episodes": self._merged_cour_episodes(),
        })
        match = MatchResult(
            tmdb_id="278043",
            external_id="278043",
            provider="tmdb",
            title="正相反的你与我",
            year="2026",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="matched",
            need_confirm=False,
        )
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f",
            "You.and.I.Are.Polar.Opposites.S02E06.tmdb278043.mkv",
            False, 100, "etag", "source",
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "move")
        self.assertEqual((plan.source_season, plan.source_episode), (2, 6))
        self.assertEqual((plan.season, plan.episode), (1, 18))
        self.assertIn("Season 1", plan.target_path)
        self.assertIn("S01E18", plan.new_name)
        self.assertEqual(
            plan.episode_mapping.reason,
            "publisher_cour_mapped_to_merged_tmdb_season",
        )
        scraper.get_tv_season_detail.assert_called_once_with("278043", 1)

    def test_explicit_tmdb_marker_does_not_map_merged_season_without_hiatus(self):
        client = SimpleNamespace(
            api_key="test-key",
            base_url="https://tmdb.test/3",
            config_error="",
            session=None,
        )
        scraper = TMDBScraper(client=client)
        scraper.get_detail = Mock(return_value={
            "id": 278043,
            "name": "正相反的你与我",
            "first_air_date": "2026-01-11",
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": 25}],
        })
        scraper.get_tv_season_detail = Mock(return_value={
            "season_number": 1,
            "episodes": [
                {
                    "episode_number": number,
                    "air_date": (
                        datetime(2026, 1, 11) + timedelta(days=7 * (number - 1))
                    ).date().isoformat(),
                }
                for number in range(1, 26)
            ],
        })
        match = MatchResult(
            tmdb_id="278043", external_id="278043", provider="tmdb",
            title="正相反的你与我", year="2026", media_type="tv",
            confidence=1.0, threshold=0.9, status="matched", need_confirm=False,
        )
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Show.S02E06.tmdb278043.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "skip")
        self.assertTrue(plan.match.need_confirm)
        self.assertIn("季号", plan.note)

    def test_explicit_tmdb_marker_rejects_mismatched_internal_tmdb_id(self):
        client = SimpleNamespace(
            api_key="test-key",
            base_url="https://tmdb.test/3",
            config_error="",
            session=None,
        )
        scraper = TMDBScraper(client=client)
        scraper.get_detail = Mock(return_value={})
        scraper.get_tv_season_detail = Mock(return_value={})
        match = MatchResult(
            tmdb_id="999",
            external_id="278043",
            provider="tmdb",
            title="错误结果",
            year="2026",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="matched",
            need_confirm=False,
        )
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Show.S02E06.tmdb278043.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "skip")
        self.assertTrue(plan.match.need_confirm)
        self.assertIn("999/278043", plan.note)
        scraper.get_detail.assert_not_called()
        scraper.get_tv_season_detail.assert_not_called()

    def test_merged_cour_mapping_requires_automatic_explicit_marker(self):
        detail = {
            "id": 278043,
            "name": "正相反的你与我",
            "first_air_date": "2026-01-11",
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": 25}],
        }
        for filename, automatic in (
            ("You.and.I.Are.Polar.Opposites.S02E06.mkv", True),
            ("You.and.I.Are.Polar.Opposites.S02E06.tmdb278043.mkv", False),
        ):
            with self.subTest(filename=filename, automatic=automatic):
                client = SimpleNamespace(
                    api_key="test-key",
                    base_url="https://tmdb.test/3",
                    config_error="",
                    session=None,
                )
                scraper = TMDBScraper(client=client)
                scraper.get_detail = Mock(return_value=detail)
                scraper.get_tv_season_detail = Mock(return_value={
                    "season_number": 1,
                    "episodes": self._merged_cour_episodes(),
                })
                match = MatchResult(
                    tmdb_id="278043",
                    external_id="278043",
                    provider="tmdb",
                    title="正相反的你与我",
                    year="2026",
                    media_type="tv",
                    confidence=1.0,
                    threshold=0.9,
                    status="matched",
                    need_confirm=False,
                )
                organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
                file = GuangYaFile(
                    "f", filename, False, 100, "etag", "source"
                )

                plan = organizer._plan_one(
                    file, "", OrganizeRules(), match_override=match,
                    automatic=automatic,
                )

                self.assertEqual(plan.action, "skip")
                self.assertNotEqual(
                    getattr(plan.episode_mapping, "reason", ""),
                    "publisher_cour_mapped_to_merged_tmdb_season",
                )
                scraper.get_tv_season_detail.assert_not_called()

    def test_verified_episode_proof_fails_closed_when_final_position_differs(self):
        match = self._verified_episode_match(target_episode=4)
        scraper = self._proof_scraper(match)
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example.Show.S01E03.1080p.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "skip")
        self.assertIn("最终季集位置不一致", plan.note)
        self.assertTrue(plan.match.need_confirm)

    def test_verified_episode_proof_is_not_directory_cacheable(self):
        match = self._verified_episode_match()

        self.assertIsNotNone(verified_automatic_identity_proof(match))
        self.assertFalse(Organizer._cacheable_directory_match(match))

    def test_verified_episode_proof_rejects_threshold_binding_mismatch(self):
        match = self._verified_episode_match()
        match.threshold = 0.6

        self.assertIsNone(verified_automatic_identity_proof(match))

    def test_verified_episode_proof_accepts_bound_loose_recognition_threshold(self):
        match = self._verified_episode_match()
        match.threshold = 0.6
        match.metadata["verified_automatic_identity_proof"][
            "recognition_threshold"
        ] = 0.6

        self.assertIsNotNone(verified_automatic_identity_proof(match))

    def test_verified_episode_proof_rejects_nonfinite_numeric_fields(self):
        for field, value in (
            ("confidence", float("nan")),
            ("candidate_gap", float("inf")),
            ("strong_title_score", float("-inf")),
        ):
            with self.subTest(field=field):
                match = self._verified_episode_match()
                proof = match.metadata["verified_automatic_identity_proof"]
                if field == "confidence":
                    match.confidence = value
                    proof["confidence"] = value
                else:
                    proof[field] = value
                self.assertIsNone(verified_automatic_identity_proof(match))

    def test_verified_episode_proof_fails_closed_when_detail_identity_missing_or_differs(self):
        for detail_id in (None, 9002):
            with self.subTest(detail_id=detail_id):
                match = self._verified_episode_match()
                scraper = self._proof_scraper(match)
                detail = dict(scraper.get_detail.return_value)
                if detail_id is None:
                    detail.pop("id", None)
                else:
                    detail["id"] = detail_id
                scraper.get_detail.return_value = detail
                organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
                file = GuangYaFile(
                    "f", "Example.Show.S01E03.1080p.mkv", False, 100, "etag", "source"
                )

                plan = organizer._plan_one(
                    file, "", OrganizeRules(), match_override=match, automatic=True
                )

                self.assertEqual(plan.action, "skip")
                self.assertTrue(plan.match.need_confirm)
                self.assertIn("最终季集位置不一致", plan.note)

    def test_verified_episode_proof_fails_closed_when_match_ids_diverge(self):
        match = self._verified_episode_match()
        match.tmdb_id = "9002"
        scraper = self._proof_scraper(match)
        scraper.get_detail.return_value["id"] = 9002
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example.Show.S01E03.1080p.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "skip")
        self.assertTrue(plan.match.need_confirm)

    def test_verified_target_season_year_proof_rechecks_current_detail(self):
        match = self._verified_episode_match()
        proof = match.metadata["verified_automatic_identity_proof"]
        mapping = {
            "source_season": 1,
            "source_episode": 3,
            "target_season": 1,
            "target_episode": 3,
            "changed": False,
            "mode": "identity",
            "confidence": 1.0,
            "reason": "identity",
        }
        proof["episode_mapping"] = mapping
        proof["expected_year"] = "2025"
        proof["target_season_year_evidence"] = {
            "kind": "tmdb_tv_target_season_air_year",
            "tmdb_id": "9001",
            "media_type": "tv",
            "expected_year": "2025",
            "source_season": 1,
            "source_episode": 3,
            "target_season": 1,
            "target_episode": 3,
            "season_air_date": "2025-01-05",
            "position_validation": dict(proof["position_validation"]),
            "episode_mapping": dict(mapping),
        }
        scraper = self._proof_scraper(match)
        scraper.get_detail.return_value["seasons"] = [{
            "season_number": 1,
            "episode_count": 12,
            "air_date": "2024-01-05",
        }]
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        file = GuangYaFile(
            "f", "Example.Show.2025.S01E03.1080p.mkv", False, 100, "etag", "source"
        )

        plan = organizer._plan_one(
            file, "", OrganizeRules(), match_override=match, automatic=True
        )

        self.assertEqual(plan.action, "skip")
        self.assertTrue(plan.match.need_confirm)
        self.assertIn("最终季集位置不一致", plan.note)

    def test_unresolved_sequel_gate_only_uses_nearest_media_directory(self):
        scraper = Mock()
        scraper.supports_parent_path = True
        scraper.match.return_value = MatchResult(
            tmdb_id="200", title="Normal Show", year="2026", media_type="tv",
            confidence=1.0, status="matched", need_confirm=False,
        )
        scraper.parse_media = Mock(side_effect=lambda filename, parent_path="", match=None: release_parse_result(
            {"title": "Normal Show", "season": None, "episode": 1, "type": "tv"},
            filename=filename, parent_path=parent_path,
        ))
        scraper.get_detail.return_value = {
            "genres": [], "origin_country": [], "first_air_date": "2026-01-01",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }
        organizer = Organizer(client=Mock(), scraper=scraper)

        normal = organizer._plan_one(
            GuangYaFile("normal", "Normal Show - 01.mkv", False, 100, "e", "p"),
            "/downloads/Part 2/Normal Show", OrganizeRules(), automatic=True,
        )
        unresolved = organizer._plan_one(
            GuangYaFile(
                "unresolved", "Example Show Part 2 - 01.mkv", False, 100, "e", "p"
            ),
            "/downloads/Example Show Part 2", OrganizeRules(), automatic=True,
        )

        self.assertNotEqual(normal.action, "skip")
        self.assertEqual(normal.season, 1)
        self.assertEqual(unresolved.action, "skip")
        self.assertTrue(unresolved.match.need_confirm)
        self.assertIn("无法安全确定 TMDB 季号", unresolved.note)

    def test_tmdb_circuit_stops_repeated_transport_failures(self):
        client = SimpleNamespace(
            api_key="key", base_url="https://tmdb.example", config_error="", session=None,
            search=Mock(side_effect=ProviderTimeout("timeout")),
        )
        scraper = TMDBScraper(client=client)

        for index in range(3):
            self.assertEqual(scraper.search(f"Title {index}", "2026", "movie"), [])
        self.assertEqual(scraper.search("Title 4", "2026", "movie"), [])

        self.assertEqual(client.search.call_count, 3)
        self.assertEqual(scraper._last_search_status, "request_error")
        self.assertGreaterEqual(scraper.performance_snapshot()["tmdb_circuit_rejections"], 1)

    def test_probe_failure_cache_and_budget_prevent_repeat_work(self):
        unique = uuid.uuid4().hex
        file = GuangYaFile(
            f"probe-failure-{unique}", "Example.mkv", False, 100,
            f"etag-{unique}", "source",
        )
        client = SimpleNamespace(get_download_url=Mock(return_value="https://example/video"))
        budget = ProbeBudget(1)
        with patch("app.modules.media_probe.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 5)) as run:
            self.assertIsNone(probe_media_profile(file, client, enabled=True, timeout=5, budget=budget))
            self.assertIsNone(probe_media_profile(file, client, enabled=True, timeout=5, budget=budget))

        self.assertEqual(run.call_count, 1)
        self.assertEqual(client.get_download_url.call_count, 1)
        self.assertEqual(budget.attempted, 1)
        self.assertEqual(budget.failure_cache_hits, 1)

    def test_probe_uses_configured_executable_and_retries_transient_signed_url_once(self):
        file = GuangYaFile("retry-probe", "Retry.mkv", False, 100, "etag", "source")
        client = SimpleNamespace(get_download_url=Mock(side_effect=[
            "https://example/first?token=secret",
            "https://example/second?token=secret2",
        ]))
        payload = json.dumps({
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 1920,
                 "height": 1080, "avg_frame_rate": "24/1", "color_transfer": "bt709"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2},
            ]
        })
        first = subprocess.CalledProcessError(
            1, ["ffprobe"], stderr="Server returned 403 Forbidden for https://example/first?token=secret"
        )
        success = subprocess.CompletedProcess(["ffprobe"], 0, stdout=payload, stderr="")
        budget = ProbeBudget(2)
        with patch.dict("os.environ", {"MEDIAFLUX_FFPROBE": "/opt/mediaflux/ffprobe"}), patch(
            "app.modules.media_probe.subprocess.run", side_effect=[first, success]
        ) as run, patch.object(db, "get_media_probe_cache", return_value=""), patch.object(
            db, "upsert_media_probe_cache"
        ):
            profile = probe_media_profile(file, client, enabled=True, timeout=5, budget=budget)

        self.assertIsNotNone(profile)
        self.assertEqual(profile.render(), "1080p.SDR.H.265.24fps.AAC.2.0")
        self.assertEqual(client.get_download_url.call_count, 2)
        self.assertEqual(run.call_count, 2)
        # Windows 会把 POSIX 路径规范化为反斜杠，按路径语义比较。
        self.assertEqual(
            Path(run.call_args_list[0].args[0][0]),
            Path("/opt/mediaflux/ffprobe"),
        )
        self.assertEqual(budget.attempted, 2)

    def test_probe_does_not_retry_non_transient_ffprobe_failure(self):
        file = GuangYaFile("bad-codec-probe", "Broken.mkv", False, 100, "etag", "source")
        client = SimpleNamespace(get_download_url=Mock(return_value="https://example/video?token=secret"))
        failure = subprocess.CalledProcessError(
            1, ["ffprobe"], stderr="Invalid data found when processing input"
        )
        with patch("app.modules.media_probe.subprocess.run", side_effect=failure) as run, patch.object(
            db, "get_media_probe_cache", return_value=""
        ), patch.object(db, "upsert_media_probe_cache"):
            self.assertIsNone(probe_media_profile(file, client, enabled=True))

        self.assertEqual(run.call_count, 1)
        self.assertEqual(client.get_download_url.call_count, 1)

    def test_completed_isolated_staging_is_deleted_only_after_empty_recheck(self):
        delete_empty = Mock(return_value=True)
        organizer = SimpleNamespace(
            client=SimpleNamespace(
                list_dir=Mock(return_value=[]),
                file_info=Mock(return_value=SimpleNamespace(
                    name="MF-7", parent_id="parent", is_dir=True,
                    etag="dir-etag", updated_at=123,
                )),
                supports_atomic_empty_directory_delete=True,
                delete_empty_directory=delete_empty,
            )
        )
        row = {
            "gy_isolated": 1, "gy_target_dir": "staging", "gy_staging_name": "MF-7",
            "gy_staging_parent_dir": "parent",
        }
        with patch.object(db, "get_download_request", return_value=row), patch(
            "app.modules.organize_tasks.execute_recycle_bin_delete"
        ) as delete, patch.object(db, "update_download_request") as update:
            OrganizeTaskManager._cleanup_download_staging(
                organizer, [7], [{"id": "staging", "name": "MF-7"}]
            )

        delete.assert_called_once()
        operation = delete.call_args.kwargs["delete_operation"]
        operation()
        delete_empty.assert_called_once_with(
            "staging", expected_etag="dir-etag", expected_updated_at=123
        )
        self.assertEqual(update.call_args.kwargs["gy_staging_cleanup_status"], "completed")

    def test_completed_isolated_staging_is_retained_without_atomic_delete_capability(self):
        organizer = SimpleNamespace(
            client=SimpleNamespace(
                list_dir=Mock(return_value=[]),
                file_info=Mock(return_value=SimpleNamespace(
                    name="MF-7", parent_id="parent", is_dir=True,
                    etag="", updated_at=0,
                )),
                supports_atomic_empty_directory_delete=False,
            )
        )
        row = {
            "gy_isolated": 1, "gy_target_dir": "staging", "gy_staging_name": "MF-7",
            "gy_staging_parent_dir": "parent",
        }
        with patch.object(db, "get_download_request", return_value=row), patch(
            "app.modules.organize_tasks.execute_recycle_bin_delete"
        ) as delete, patch.object(db, "update_download_request") as update:
            OrganizeTaskManager._cleanup_download_staging(
                organizer, [7], [{"id": "staging", "name": "MF-7"}]
            )

        delete.assert_not_called()
        self.assertEqual(update.call_args.kwargs["gy_staging_cleanup_status"], "retained")

    def test_completed_isolated_staging_cleanup_failure_is_persisted(self):
        organizer = SimpleNamespace(
            client=SimpleNamespace(
                list_dir=Mock(return_value=[]),
                file_info=Mock(return_value=SimpleNamespace(
                    name="MF-7", parent_id="parent", is_dir=True,
                    etag="dir-etag", updated_at=123,
                )),
                supports_atomic_empty_directory_delete=True,
                delete_empty_directory=Mock(side_effect=RuntimeError("version changed")),
            )
        )
        row = {
            "gy_isolated": 1, "gy_target_dir": "staging", "gy_staging_name": "MF-7",
            "gy_staging_parent_dir": "parent",
        }

        def execute(_client, **kwargs):
            kwargs["delete_operation"]()

        with patch.object(db, "get_download_request", return_value=row), patch(
            "app.modules.organize_tasks.execute_recycle_bin_delete",
            side_effect=execute,
        ), patch.object(db, "update_download_request") as update:
            OrganizeTaskManager._cleanup_download_staging(
                organizer, [7], [{"id": "staging", "name": "MF-7"}]
            )

        self.assertEqual(update.call_args.kwargs["gy_staging_cleanup_status"], "failed")
        self.assertIn("RuntimeError", update.call_args.kwargs["gy_staging_cleanup_error"])

    def test_completed_isolated_staging_is_retained_on_identity_mismatch(self):
        organizer = SimpleNamespace(
            client=SimpleNamespace(
                list_dir=Mock(return_value=[]),
                file_info=Mock(return_value=SimpleNamespace(
                    name="other", parent_id="parent", is_dir=True,
                    etag="dir-etag", updated_at=123,
                )),
                supports_atomic_empty_directory_delete=True,
                delete_empty_directory=Mock(return_value=True),
            )
        )
        row = {
            "gy_isolated": 1, "gy_target_dir": "staging", "gy_staging_name": "MF-7",
            "gy_staging_parent_dir": "parent",
        }
        with patch.object(db, "get_download_request", return_value=row), patch(
            "app.modules.organize_tasks.execute_recycle_bin_delete"
        ) as delete, patch.object(db, "update_download_request") as update:
            OrganizeTaskManager._cleanup_download_staging(
                organizer, [7], [{"id": "staging", "name": "MF-7"}]
            )

        delete.assert_not_called()
        self.assertEqual(update.call_args.kwargs["gy_staging_cleanup_status"], "retained")
        self.assertIn("身份", update.call_args.kwargs["gy_staging_cleanup_error"])

    def test_download_task_propagates_automatic_mode_to_organizer(self):
        organizer = Mock()
        organizer.organize.return_value = ([], {
            "total": 1, "moved": 0, "failed": 0, "scan_errors": [],
            "replacement_cleanup_failed": 0, "strm_changes": [],
        })
        manager = OrganizeTaskManager()
        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch.object(db, "add_task_run", return_value=0):
            result = manager.start(
                [{"id": "source", "name": "来源"}],
                OrganizeRules(target_dir_id="archive"),
                trigger_type="download",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "completed")
        self.assertTrue(organizer.organize.call_args.kwargs["automatic"])

    def test_telegram_task_propagates_automatic_mode_to_organizer(self):
        organizer = Mock()
        organizer.organize.return_value = ([], {
            "total": 1, "moved": 0, "failed": 0, "scan_errors": [],
            "replacement_cleanup_failed": 0, "strm_changes": [],
        })
        manager = OrganizeTaskManager()
        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch.object(db, "add_task_run", return_value=0):
            result = manager.start(
                [{"id": "source", "name": "来源"}],
                OrganizeRules(target_dir_id="archive"),
                trigger_type="telegram",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "completed")
        self.assertTrue(organizer.organize.call_args.kwargs["automatic"])

    def test_stopped_download_organize_is_marked_for_attention(self):
        request_id, _created = db.create_download_request(
            "stopped-organize", "magnet", title="Stopped",
            source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        )
        db.update_download_request(
            request_id, status="completed", gy_status="completed",
            organize_started=1, organize_status="running",
        )
        manager = OrganizeTaskManager()
        manager._lock.acquire()
        manager._task = {"id": "stop-task", "status": "running", "stats": {}}
        organizer = Mock()
        organizer._validate_target_outside_source = Mock()

        def stop_during_organize(*_args, **_kwargs):
            manager._cancel_event.set()
            return [], {
                "total": 1, "moved": 0, "failed": 0, "scan_errors": [],
                "replacement_cleanup_failed": 0, "empty_dir_cleanup_failed": 0,
                "source_dir_cleanup_failed": 0, "audit_failures": 0,
                "strm_changes": [],
            }

        organizer.organize.side_effect = stop_during_organize
        with patch("app.modules.organize_tasks.Organizer", return_value=organizer) as organizer_cls, patch.object(
            manager, "_wake_download_tracker"
        ):
            organizer_cls.trigger_post_actions = Mock()
            organizer_cls.notify_task_results = Mock()
            manager._run(
                "stop-task", 0, [{"id": "source", "name": "来源"}],
                OrganizeRules(target_dir_id="archive"),
                download_request_ids=[request_id], trigger_type="download",
            )

        row = db.get_download_request(request_id)
        self.assertEqual(row["organize_status"], "stopped")
        self.assertEqual(row["organize_started"], -1)
        self.assertEqual(row["strm_status"], "skipped")
        self.assertIn("勿直接重复执行", row["organize_error"])
        attention_ids = {
            int(item["id"]) for item in db.list_download_requests_requiring_attention()
        }
        self.assertIn(request_id, attention_ids)

    def test_conflict_preview_uses_initialized_shared_listing_cache(self):
        organizer = Organizer(client=Mock(), scraper=Mock())
        plan = OrganizePlan(
            file_id="f", original_name="Movie.mkv", original_path="Movie.mkv",
            match=MatchResult(tmdb_id="1", media_type="movie"),
            target_path="电影/Movie (2026) {tmdb-1}", new_name="Movie.2026.mkv",
        )
        with patch.object(organizer, "_apply_identity_guards"), patch.object(
            organizer, "_find_existing_dir_chain", return_value=None
        ) as find:
            organizer._preview_conflicts([plan], OrganizeRules(target_dir_id="archive"))

        self.assertEqual(plan.action, "move")
        self.assertEqual(plan.conflict_decision, "new")
        self.assertIsInstance(find.call_args.args[2], dict)

    def test_credits_detail_failures_share_circuit_and_success_resets_it(self):
        client = SimpleNamespace(
            api_key="key", base_url="https://tmdb.example", config_error="", session=None,
            get=Mock(side_effect=[
                ProviderTimeout("one"), ProviderTimeout("two"), ProviderTimeout("three"),
                {"id": 5, "credits": {}}, ProviderTimeout("after-recovery"),
                {"id": 7, "credits": {}},
            ]),
        )
        scraper = TMDBScraper(client=client)
        for tmdb_id in ("1", "2", "3"):
            self.assertEqual(scraper.get_detail_with_credits(tmdb_id, "movie"), {})
        self.assertEqual(scraper.get_detail_with_credits("4", "movie"), {})
        self.assertEqual(client.get.call_count, 3)

        with scraper._tmdb_state_lock:
            scraper._tmdb_circuit_open_until = 0.0
        self.assertEqual(scraper.get_detail_with_credits("5", "movie")["id"], 5)
        self.assertEqual(scraper.get_detail_with_credits("6", "movie"), {})
        self.assertEqual(scraper.get_detail_with_credits("7", "movie")["id"], 7)
        self.assertEqual(client.get.call_count, 6)

    def test_probe_download_url_failure_is_negative_cached(self):
        file = GuangYaFile("url-f", "Broken.mkv", False, 100, "etag", "source")
        client = SimpleNamespace(get_download_url=Mock(side_effect=RuntimeError("offline")))

        self.assertIsNone(probe_media_profile(file, client, enabled=True))
        self.assertIsNone(probe_media_profile(file, client, enabled=True))

        self.assertEqual(client.get_download_url.call_count, 1)

    def test_ai_different_inputs_do_not_share_global_network_lock(self):
        scraper = TMDBScraper(client=SimpleNamespace(
            api_key="key", base_url="https://tmdb.example", session=None,
        ))
        client_key = ("https://ai.example/v1/chat/completions", "model", "", 5, "")
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        def recognize(value):
            if value.normalized_title == "first":
                first_started.set()
            else:
                second_started.set()
            release.wait(timeout=2)
            return AIRecognitionResult(
                value.normalized_title, "", 2026, "movie", None, None, (), 0.9
            )

        def client_factory(**_kwargs):
            return SimpleNamespace(recognize=recognize)

        results = []
        with patch("app.modules.scraper.AIRecognitionClient", side_effect=client_factory):
            threads = [
                threading.Thread(target=lambda name=name: results.append(
                    scraper._recognize_with_ai_cache(
                        AIRecognitionInput(normalized_title=name),
                        client_key=client_key, cache_key=name,
                    )
                ))
                for name in ("first", "second")
            ]
            threads[0].start()
            self.assertTrue(first_started.wait(timeout=1))
            threads[1].start()
            self.assertTrue(second_started.wait(timeout=1))
            release.set()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual({item.title for item in results}, {"first", "second"})

    def test_queue_position_excludes_expired_queued_items(self):
        expired_at = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        valid_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        old_id = db.create_organize_confirmation(
            token="old", fingerprint="old", chat_id="1", source_name="source",
            directory_path="old", payload={}, expires_at=expired_at,
        )
        new_id = db.create_organize_confirmation(
            token="new", fingerprint="new", chat_id="1", source_name="source",
            directory_path="new", payload={}, expires_at=valid_at,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET status='queued',queued_at=? WHERE id IN (?,?)",
                (db.now(), old_id, new_id),
            )

        self.assertEqual(db.get_organize_confirmation_queue_position(new_id), 0)
        self.assertEqual(db.get_organize_confirmation("old")["status"], "expired")


if __name__ == "__main__":
    unittest.main()
