"""整理写入/纠偏语义闭环回归；只使用隔离 SQLite 和内存云盘。"""

from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import tests  # noqa: F401 -- 必须先建立隔离运行环境
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.repositories import organize_history as organize_history_repo
from app.repositories import organize_probe as organize_probe_repo
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.organize_execution import execute_organize_plans
from app.modules.guangya_compensation import restore_guangya_file
from app.modules.organize_probe_worker import OrganizeProbeWorker
from app.modules.scraper import MatchResult
from app.repositories.recognition import list_manual_episode_confirmations
from tests.support import IsolatedDatabaseTestCase


class MemoryCloud:
    def __init__(self, fail_operation="", *, unreadable=False, rollback_fails=False):
        self.files = {
            "video": GuangYaFile("video", "Archive.mkv", False, 100, "etag", "target")
        }
        self.fail_operation = fail_operation
        self.failed = False
        self.unreadable = unreadable
        self.rollback_fails = rollback_fails
        self.calls = []

    def file_info(self, file_id):
        if self.failed and self.unreadable:
            raise TimeoutError("无法确认写后状态")
        item = self.files.get(file_id)
        return replace(item) if item else None

    def list_dir(self, parent_id):
        return [
            replace(item) for item in self.files.values() if item.parent_id == parent_id
        ]

    def rename(self, file_id, name):
        self.calls.append(("rename", file_id, name))
        if self.failed and self.rollback_fails:
            raise TimeoutError("补偿请求失败")
        self.files[file_id].name = name
        self._after_write("rename")
        return True

    def move(self, file_ids, parent_id):
        self.calls.append(("move", tuple(file_ids), parent_id))
        if self.failed and self.rollback_fails:
            raise TimeoutError("补偿请求失败")
        for file_id in file_ids:
            self.files[file_id].parent_id = parent_id
        self._after_write("move")
        return True

    def _after_write(self, operation):
        if self.fail_operation == operation and not self.failed:
            self.failed = True
            raise TimeoutError("服务端已提交，但响应丢失")


class ForegroundReplacementCompensationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("禁止外联")))

    def _execute(self, cloud, companions=()):
        incoming = GuangYaFile("incoming", "Source.mkv", False, 200, "new-etag", "source")
        cloud.files[incoming.file_id] = incoming
        organizer = Organizer(client=cast(GuangYaClient, cloud), scraper=cast(TMDBScraper, object()))
        self.addCleanup(organizer.close)
        plan = OrganizePlan(
            file_id=incoming.file_id, original_name=incoming.name,
            original_path="incoming", original_parent_id=incoming.parent_id,
            size=incoming.size, etag=incoming.etag,
            match=MatchResult(tmdb_id="1", title="Archive", year="2025", media_type="movie"),
            target_path="Archive", new_name="Archive.mkv",
        )
        stats = organizer._initial_stats()
        with (
            patch.object(organizer, "_ensure_dir_chain", return_value="target"),
            patch.object(organizer, "_resolve_variant_conflict", return_value=(
                replace(cloud.files["video"]), "replace", "同版本新文件胜出",
            )),
        ):
            execute_organize_plans(
                organizer, [plan], OrganizeRules(target_dir_id="target"), stats,
                {"incoming": list(companions)},
            )
        self.assertEqual((stats["failed"], stats["moved"]), (1, 0))
        self.assertEqual(cloud.files["incoming"].parent_id, "source")
        log = dict(db.list_organize_logs(limit=1)[0])
        self.assertEqual(log["status"], "failed")
        audit = db.list_organize_delete_audits(log["id"])[0]
        self.assertEqual((audit["file_id"], audit["status"]), ("video", "blocked"))
        return log, audit

    def test_foreground_restores_old_backup_after_lost_response(self):
        cloud = MemoryCloud("rename")
        with patch.object(cloud, "file_info", wraps=cloud.file_info) as read:
            log, audit = self._execute(cloud)
        self.assertEqual(cloud.files["video"].name, "Archive.mkv")
        self.assertEqual(cloud.calls, [
            ("rename", "video", "Archive.mkv.mediaflux-backup-video"),
            ("rename", "video", "Archive.mkv"),
        ])
        self.assertGreaterEqual(sum(call.args == ("video",) for call in read.call_args_list), 3)
        self.assertFalse(log["legacy_incomplete"])
        self.assertEqual(audit["file_name"], "Archive.mkv")

    def test_foreground_backup_rejected_before_commit_is_read_without_blind_rename(self):
        cloud = MemoryCloud()
        with (
            patch.object(cloud, "rename", side_effect=TimeoutError("请求未提交")) as rename,
            patch.object(cloud, "file_info", wraps=cloud.file_info) as read,
        ):
            log, audit = self._execute(cloud)
        self.assertEqual(cloud.files["video"].name, "Archive.mkv")
        self.assertEqual(rename.call_count, 1)
        self.assertGreaterEqual(sum(call.args == ("video",) for call in read.call_args_list), 2)
        self.assertFalse(log["legacy_incomplete"])
        self.assertEqual(audit["file_name"], "Archive.mkv")

    def test_foreground_uncertain_backup_restore_requires_manual_review(self):
        for failure in ("unreadable", "rollback_fails", "ineffective_ack"):
            with self.subTest(failure=failure):
                cloud = MemoryCloud("rename", **(
                    {failure: True} if failure != "ineffective_ack" else {}
                ))
                original_rename = cloud.rename

                def rename(file_id, name, *, failure=failure, cloud=cloud, original_rename=original_rename):
                    if failure == "ineffective_ack" and cloud.failed:
                        cloud.calls.append(("rename", file_id, name))
                        return True  # ACK 到达，但云端文件名未恢复。
                    return original_rename(file_id, name)

                with (
                    patch.object(cloud, "rename", side_effect=rename),
                    patch("app.modules.guangya_compensation.time.sleep"),
                ):
                    log, audit = self._execute(cloud)
                self.assertEqual(cloud.files["video"].name, "Archive.mkv.mediaflux-backup-video")
                self.assertTrue(log["legacy_incomplete"])
                self.assertEqual((log["current_parent_id"], log["current_name"]), ("", ""))
                self.assertIn("回滚未完成", log["error"])
                self.assertIn("人工核验", audit["reason"])
                self.assertEqual(len(cloud.calls), 1 if failure == "unreadable" else 2)

    def test_foreground_later_move_failure_restores_both_versions(self):
        cloud = MemoryCloud("move")
        log, audit = self._execute(cloud)
        self.assertEqual(cloud.files["video"].name, "Archive.mkv")
        self.assertFalse(log["legacy_incomplete"])
        self.assertEqual(audit["file_name"], "Archive.mkv")
        self.assertEqual(cloud.calls, [
            ("rename", "video", "Archive.mkv.mediaflux-backup-video"),
            ("move", ("incoming",), "target"),
            ("move", ("incoming",), "source"),
            ("rename", "video", "Archive.mkv"),
        ])

    def test_companion_failure_restores_whole_write_set_and_isolates_rollback_failure(self):
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                cloud = MemoryCloud()
                companions = [
                    GuangYaFile("nfo", "Source.nfo", False, 20, "nfo", "source"),
                    GuangYaFile("poster", "Source.jpg", False, 30, "poster", "source"),
                ]
                cloud.files.update((item.file_id, replace(item)) for item in companions)
                original_rename = cloud.rename

                def rename(file_id, name):
                    if rollback_fails and (file_id, name) == ("poster", "Source.jpg"):
                        raise TimeoutError("补偿请求失败")
                    result = original_rename(file_id, name)
                    if (file_id, name) == ("poster", "Archive.jpg"):
                        raise TimeoutError("服务端已改名，响应丢失")
                    return result

                with patch.object(cloud, "rename", side_effect=rename):
                    log, _audit = self._execute(cloud, companions)
                self.assertEqual(log["legacy_incomplete"], rollback_fails)
                expected = {"video": ("target", "Archive.mkv"), "incoming": ("source", "Source.mkv")}
                expected.update((item.file_id, (item.parent_id, item.name)) for item in companions)
                if rollback_fails:
                    expected["poster"] = ("target", "Archive.jpg")
                self.assertEqual(
                    {key: (item.parent_id, item.name) for key, item in cloud.files.items()}, expected,
                )
                items = {item["file_id"]: item for item in db.list_organize_log_items(log["id"])}
                self.assertEqual(items["nfo"]["current_name"], "Source.nfo")
                self.assertEqual(items["nfo"]["target_name"], "Archive.nfo")
                self.assertEqual(items["poster"]["current_name"], "" if rollback_fails else "Source.jpg")
                self.assertEqual(items["poster"]["target_name"], "Source.jpg")
                self.assertEqual(cloud.calls, [
                    ("rename", "video", "Archive.mkv.mediaflux-backup-video"),
                    ("move", ("incoming",), "target"),
                    ("rename", "incoming", "Archive.mkv"),
                    ("move", ("nfo",), "target"),
                    ("rename", "nfo", "Archive.nfo"),
                    ("move", ("poster",), "target"),
                    ("rename", "poster", "Archive.jpg"),
                ] + ([] if rollback_fails else [
                    ("rename", "poster", "Source.jpg"),
                    ("move", ("poster",), "source"),
                ]) + [
                    ("rename", "nfo", "Source.nfo"),
                    ("move", ("nfo",), "source"),
                    ("rename", "incoming", "Source.mkv"),
                    ("move", ("incoming",), "source"),
                    ("rename", "video", "Archive.mkv"),
                ])


class ReleaseChainCompensationTests(IsolatedDatabaseTestCase):
    def _log(self):
        log_id = db.add_organize_log(
            "guangya",
            "incoming",
            "Archive/Archive.mkv",
            "video",
            "success",
            "1",
            original_parent_id="source",
            original_name="Source.mkv",
            source_dir_id="source",
            current_parent_id="target",
            current_name="Archive.mkv",
            target_parent_id="target",
            media_type="movie",
            title="Archive",
            year="2025",
            legacy_incomplete=False,
        )
        db.add_organize_log_items(
            log_id,
            [
                {
                    "file_id": "video",
                    "role": "video",
                    "original_parent_id": "source",
                    "original_name": "Source.mkv",
                    "current_parent_id": "target",
                    "current_name": "Archive.mkv",
                    "target_parent_id": "target",
                    "target_name": "Archive.mkv",
                    "size": 100,
                    "etag": "etag",
                    "status": "success",
                }
            ],
        )
        return log_id

    def test_correction_restores_committed_move_and_rename_after_lost_response(self):
        for operation in ("rename", "move"):
            with self.subTest(operation=operation):
                log_id = self._log()
                cloud = MemoryCloud(operation)
                service = OrganizeCorrectionService(client=cast(GuangYaClient, cloud), scraper=cast(TMDBScraper, object()))
                with patch.object(
                    service,
                    "_capture_archive_cleanup_directories",
                    return_value=([], set(), []),
                ):
                    with self.assertRaises(TimeoutError):
                        service.return_to_source(
                            log_id,
                            "return-" + operation,
                            service.detail(log_id)["version"],
                        )
                remote = cloud.files["video"]
                self.assertEqual(
                    (remote.parent_id, remote.name), ("target", "Archive.mkv")
                )
                item = dict(db.list_organize_log_items(log_id)[0])
                self.assertEqual(
                    (item["current_parent_id"], item["current_name"]),
                    (remote.parent_id, remote.name),
                )

    def test_uncertain_correction_is_frozen_for_manual_review(self):
        for failure in ("unreadable", "rollback_fails"):
            with self.subTest(failure=failure):
                log_id = self._log()
                cloud = MemoryCloud("rename", **{failure: True})
                service = OrganizeCorrectionService(client=cast(GuangYaClient, cloud), scraper=cast(TMDBScraper, object()))
                with patch.object(
                    service,
                    "_capture_archive_cleanup_directories",
                    return_value=([], set(), []),
                ):
                    with self.assertRaises(RuntimeError):
                        service.return_to_source(
                            log_id, failure, service.detail(log_id)["version"]
                        )
                item = dict(db.list_organize_log_items(log_id)[0])
                self.assertEqual(item["status"], "rollback_failed")
                get_organize_log_result = db.get_organize_log(log_id)
                assert get_organize_log_result is not None
                self.assertEqual(
                    get_organize_log_result["status"], "partial_failed"
                )
                self.assertFalse(
                    service.detail(log_id)["allowed_actions"]["return_to_source"]
                )

    def _due(self, job_id):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00' WHERE id=?",
                (job_id,),
            )

    def _job(self, job_id):
        with db.get_conn() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM organize_probe_queue WHERE id=?", (job_id,)
                ).fetchone()
            )

    def test_probe_retry_recovers_own_uncertain_rename_instead_of_cancelling(self):
        log_id = self._log()
        cloud = MemoryCloud("rename")
        worker = OrganizeProbeWorker()
        worker._client = cast(GuangYaClient, cloud)
        job_id = organize_probe_repo.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="Archive", rules={}
        )
        with (
            patch("app.modules.media_probe.probe_media_profile", return_value=object()),
            patch.object(
                worker,
                "_desired_plan",
                return_value=(
                    object(),
                    OrganizeRules(link_strm=False),
                    SimpleNamespace(new_name="Archive-1080p.mkv"),
                ),
            ),
        ):
            self._due(job_id)
            self.assertTrue(worker._process_one())
            self.assertEqual(cloud.files["video"].name, "Archive.mkv")
            self._due(job_id)
            self.assertTrue(worker._process_one())
        self.assertEqual(self._job(job_id)["status"], "completed")
        get_organize_log_result_2 = db.get_organize_log(log_id)
        assert get_organize_log_result_2 is not None
        self.assertEqual(
            get_organize_log_result_2["current_name"], cloud.files["video"].name
        )

    def test_probe_unverifiable_rollback_is_manual_failure_not_external_change(self):
        log_id = self._log()
        cloud = MemoryCloud("rename", unreadable=True)
        worker = OrganizeProbeWorker()
        worker._client = cast(GuangYaClient, cloud)
        job_id = organize_probe_repo.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="Archive", rules={}
        )
        with (
            patch("app.modules.media_probe.probe_media_profile", return_value=object()),
            patch.object(
                worker,
                "_desired_plan",
                return_value=(
                    object(),
                    OrganizeRules(link_strm=False),
                    SimpleNamespace(new_name="Archive-1080p.mkv"),
                ),
            ),
        ):
            self._due(job_id)
            worker._process_one()
            self._due(job_id)
            worker._process_one()
        get_organize_log_result_3 = db.get_organize_log(log_id)
        assert get_organize_log_result_3 is not None
        self.assertEqual(get_organize_log_result_3["status"], "partial_failed")
        self.assertEqual(
            db.list_organize_log_items(log_id)[0]["status"], "rollback_failed"
        )
        self.assertEqual(self._job(job_id)["status"], "failed")

    def test_foreground_does_not_claim_restored_when_lookup_is_unavailable(self):
        cloud = MemoryCloud(unreadable=True)
        cloud.failed = True
        with self.assertRaises(RuntimeError):
            restore_guangya_file(
                cloud,
                GuangYaFile("video", "Archive.mkv", False, 100, "etag", "target"),
            )

    def test_probe_interrupted_write_intent_freezes_instead_of_external_cancel(self):
        log_id = self._log()
        cloud = MemoryCloud()
        cloud.files["video"].name = "Archive-1080p.mkv"
        worker = OrganizeProbeWorker()
        worker._client = cast(GuangYaClient, cloud)
        job_id = organize_probe_repo.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="Archive", rules={}
        )
        organize_history_repo.add_organize_operation_step(
            log_id,
            f"probe:{job_id}:interrupted-test",
            1,
            "probe_rename",
            file_id="video",
            from_parent_id="target",
            from_name="Archive.mkv",
            to_parent_id="target",
            to_name="Archive-1080p.mkv",
            status="interrupted",
        )
        for _ in range(2):
            self._due(job_id)
            self.assertTrue(worker._process_one())
        self.assertEqual(self._job(job_id)["status"], "failed")
        get_organize_log_result_4 = db.get_organize_log(log_id)
        assert get_organize_log_result_4 is not None
        self.assertEqual(get_organize_log_result_4["status"], "partial_failed")
        self.assertEqual(cloud.calls, [])

    def test_probe_committed_rename_only_finishes_interrupted_step(self):
        log_id = self._log()
        worker = OrganizeProbeWorker()
        job_id = organize_probe_repo.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="Archive", rules={}
        )
        step_id = organize_history_repo.add_organize_operation_step(
            log_id,
            f"probe:{job_id}:committed-test",
            1,
            "probe_rename",
            file_id="video",
            from_parent_id="target",
            from_name="Archive.mkv",
            to_parent_id="target",
            to_name="Archive-1080p.mkv",
            status="interrupted",
        )
        item = db.list_organize_log_items(log_id)[0]
        db.update_organize_log_item(item["id"], current_name="Archive-1080p.mkv")
        db.update_organize_log(log_id, current_name="Archive-1080p.mkv")
        with patch.object(
            worker, "_runtime_client", side_effect=AssertionError("不应重复远端操作")
        ):
            worker._recover_unfinished_rename({"id": job_id, "organize_log_id": log_id})
        steps = {
            row["id"]: row["status"] for row in organize_history_repo.list_organize_operation_steps(log_id)
        }
        self.assertEqual(steps[step_id], "success")

    def test_probe_pending_intent_survives_more_than_display_window_of_unrelated_steps(
        self,
    ):
        log_id = self._log()
        cloud = MemoryCloud()
        cloud.files["video"].name = "Archive-1080p.mkv"
        worker = OrganizeProbeWorker()
        worker._client = cast(GuangYaClient, cloud)
        job_id = organize_probe_repo.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="Archive", rules={}
        )
        pending_id = organize_history_repo.add_organize_operation_step(
            log_id,
            f"probe:{job_id}:old-intent",
            1,
            "probe_rename",
            file_id="video",
            from_parent_id="target",
            from_name="Archive.mkv",
            to_parent_id="target",
            to_name="Archive-1080p.mkv",
            status="interrupted",
        )
        for index in range(1010):
            organize_history_repo.add_organize_operation_step(
                log_id, "later-audit", index + 1, "audit_note", status="success"
            )
        self._due(job_id)
        self.assertTrue(worker._process_one())
        get_organize_log_result_5 = db.get_organize_log(log_id)
        assert get_organize_log_result_5 is not None
        self.assertEqual(get_organize_log_result_5["status"], "partial_failed")
        self.assertEqual(self._job(job_id)["status"], "retry_wait")
        with db.get_conn() as conn:
            status = conn.execute(
                "SELECT status FROM organize_operation_steps WHERE id=?", (pending_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "rollback_failed")
        self.assertEqual(cloud.calls, [])


class ReleaseChainBusinessSnapshotTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM manual_episode_confirmations")

    def _fixture(self):
        from app.modules.scraper import TMDBScraper

        cloud = MemoryCloud()
        cloud.files["video"] = GuangYaFile(
            "video", "First.2025.S01E07.mkv", False, 100, "etag", "old-season"
        )
        log_id = db.add_organize_log(
            "guangya",
            "incoming",
            "First/Season 1/First.2025.S01E07.mkv",
            "video",
            "success",
            "1",
            source_dir_id="source",
            original_parent_id="incoming",
            original_name="First.S01E07.mkv",
            current_parent_id="old-season",
            current_name=cloud.files["video"].name,
            target_parent_id="old-season",
            provider="tmdb",
            external_id="1",
            media_type="tv",
            title="First",
            year="2025",
            season=1,
            episode=7,
            legacy_incomplete=False,
            release_parse={"original_evidence": "keep"},
        )
        db.add_organize_log_items(
            log_id,
            [
                {
                    "file_id": "video",
                    "role": "video",
                    "original_parent_id": "incoming",
                    "original_name": "First.S01E07.mkv",
                    "current_parent_id": "old-season",
                    "current_name": cloud.files["video"].name,
                    "target_parent_id": "old-season",
                    "target_name": cloud.files["video"].name,
                    "size": 100,
                    "etag": "etag",
                    "status": "success",
                }
            ],
        )
        scraper = TMDBScraper()
        self.addCleanup(scraper.close)
        scraper.get_detail = lambda tmdb_id, media_type, *, force_refresh=False: {
            "id": int(tmdb_id),
            "name": "First" if tmdb_id == "1" else "Second",
            "first_air_date": "2025-01-01" if tmdb_id == "1" else "2026-01-01",
            "genres": [],
            "origin_country": ["US"],
            "seasons": [
                {"season_number": 0, "episode_count": 12},
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        }
        service = OrganizeCorrectionService(client=cast(GuangYaClient, cloud), scraper=scraper)
        self.enterContext(
            patch.object(
                OrganizeRules,
                "from_config",
                return_value=OrganizeRules(
                    target_dir_id="library",
                    region_split=False,
                    year_split=False,
                    link_strm=False,
                    emby_refresh=False,
                ),
            )
        )
        self.enterContext(
            patch.object(service, "_ensure_target_dir", return_value=("new-season", []))
        )
        self.enterContext(
            patch.object(service, "_notify_reorganize_result", return_value=[])
        )
        self.enterContext(patch.object(service, "_run_post_actions", return_value=[]))
        return log_id, cloud, service

    def test_confirmed_position_survives_next_preview_and_partial_override(self):
        import json

        log_id, cloud, service = self._fixture()
        self.assertTrue(db.update_organize_log(log_id, confirmation_actor="agent"))
        before = organize_history_repo.capture_organize_business_snapshot(log_id)
        self.assertEqual(list_manual_episode_confirmations("1"), [])
        service.reorganize(
            log_id,
            "manual-pos",
            service.detail(log_id)["version"],
            "1",
            "tv",
            season=2,
            episode=3,
        )
        get_organize_log_result_10 = db.get_organize_log(log_id)
        assert get_organize_log_result_10 is not None
        row = dict(get_organize_log_result_10)
        self.assertEqual((row["season"], row["episode"]), (2, 3))
        self.assertEqual(row["confirmation_actor"], "human")
        learning = [dict(item) for item in list_manual_episode_confirmations("1")]
        self.assertEqual(len(learning), 1)
        self.assertEqual(
            (learning[0]["log_id"], learning[0]["file_id"], learning[0]["season"], learning[0]["episode"]),
            (log_id, "video", 2, 3),
        )
        self.assertEqual(
            json.loads(row["release_parse_json"])["original_evidence"], "keep"
        )
        self.assertEqual(
            json.loads(row["release_parse_json"])["manual_position"]["source"],
            "manual_correction",
        )
        preview = service.preview_reorganize(log_id, "1", "tv")
        self.assertEqual((preview["season"], preview["episode"]), (2, 3))
        preview = service.preview_reorganize(log_id, "1", "tv", episode=4)
        self.assertEqual((preview["season"], preview["episode"]), (2, 4))
        self.assertIn("S02E03", cloud.files["video"].name)
        self.assertEqual(row["original_name"], "First.S01E07.mkv")
        service.revert_latest(log_id, "undo-human-correction", service.detail(log_id)["version"])
        self.assertEqual(organize_history_repo.capture_organize_business_snapshot(log_id), before)
        self.assertEqual(service.detail(log_id)["confirmation_actor"], "agent")
        self.assertEqual(list_manual_episode_confirmations("1"), [])

    def test_no_manual_marker_keeps_inferred_anime_position(self):
        import json

        log_id, _cloud, service = self._fixture()
        with patch.object(
            service.scraper,
            "parse_media",
            return_value=SimpleNamespace(effective_season=2, effective_episode=3),
        ):
            service.reorganize(
                log_id, "inferred", service.detail(log_id)["version"], "1", "tv"
            )
            preview = service.preview_reorganize(log_id, "1", "tv")
        get_organize_log_result_11 = db.get_organize_log(log_id)
        assert get_organize_log_result_11 is not None
        row = dict(get_organize_log_result_11)
        self.assertEqual((preview["season"], preview["episode"]), (2, 3))
        self.assertEqual((row["season"], row["episode"]), (2, 3))
        self.assertNotIn("manual_position", json.loads(row["release_parse_json"]))

    def test_revert_restores_business_identity_position_and_member_targets(self):
        log_id, cloud, service = self._fixture()
        service.reorganize(
            log_id, "first-confirmation", service.detail(log_id)["version"],
            "1", "tv", season=1, episode=8,
        )
        before = organize_history_repo.capture_organize_business_snapshot(log_id)
        archived = replace(cloud.files["video"])
        learning_before = [dict(row) for row in list_manual_episode_confirmations("1")]
        self.assertEqual(len(learning_before), 1)
        service.reorganize(
            log_id,
            "correct",
            service.detail(log_id)["version"],
            "2",
            "tv",
            season=2,
            episode=3,
        )
        self.assertEqual(list_manual_episode_confirmations("1"), [])
        changed_learning = list_manual_episode_confirmations("2")
        self.assertEqual(len(changed_learning), 1)
        self.assertEqual(
            (changed_learning[0]["log_id"], changed_learning[0]["season"], changed_learning[0]["episode"]),
            (log_id, 2, 3),
        )
        steps = [dict(row) for row in organize_history_repo.list_organize_operation_steps(log_id)]
        self.assertTrue(any(step["state_before_json"] for step in steps))
        service.revert_latest(log_id, "undo", service.detail(log_id)["version"])
        self.assertEqual(organize_history_repo.capture_organize_business_snapshot(log_id), before)
        self.assertEqual(cloud.files["video"], archived)
        self.assertEqual(
            [dict(row) for row in list_manual_episode_confirmations("1")], learning_before,
        )
        self.assertEqual(list_manual_episode_confirmations("2"), [])
        get_organize_log_result_6 = db.get_organize_log(log_id)
        assert get_organize_log_result_6 is not None
        self.assertEqual(get_organize_log_result_6["confirmation_actor"], "human")
        self.assertEqual(
            get_organize_log_result_6["current_name"], cloud.files["video"].name
        )

        # 送回前发现源目录同名文件：只记录失败，不能撤销刚恢复的经验。
        cloud.files["source-conflict"] = GuangYaFile(
            "source-conflict", "First.S01E07.mkv", False, 100, "other-etag", "incoming",
        )
        writes_before = list(cloud.calls)
        with self.assertRaisesRegex(RuntimeError, "目标目录存在同名文件"):
            service.return_to_source(log_id, "blocked-return", service.detail(log_id)["version"])
        self.assertEqual(cloud.calls, writes_before)
        self.assertEqual(cloud.files["video"], archived)
        detail = service.detail(log_id)
        self.assertEqual((detail["status"], detail["confirmation_actor"]), ("reverted", "human"))
        self.assertEqual(organize_history_repo.capture_organize_business_snapshot(log_id), before)
        self.assertEqual(
            [dict(row) for row in list_manual_episode_confirmations("1")], learning_before,
        )
        self.assertEqual(list_manual_episode_confirmations("2"), [])

    def test_legacy_path_only_revert_explicitly_discloses_missing_identity_snapshot(
        self,
    ):
        log_id, cloud, service = self._fixture()
        service.reorganize(
            log_id,
            "legacy",
            service.detail(log_id)["version"],
            "2",
            "tv",
            season=2,
            episode=3,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_operation_steps SET state_before_json='' WHERE log_id=?",
                (log_id,),
            )
        result = service.revert_latest(
            log_id, "undo-legacy", service.detail(log_id)["version"]
        )
        self.assertTrue(result["success"])
        self.assertEqual(cloud.files["video"].name, "First.2025.S01E07.mkv")
        get_organize_log_result_7 = db.get_organize_log(log_id)
        assert get_organize_log_result_7 is not None
        self.assertEqual(get_organize_log_result_7["tmdb_id"], "2")
        self.assertTrue(
            any("身份" in warning and "仅" in warning for warning in result["warnings"])
        )

    def test_snapshot_commit_failure_compensates_file_revert_and_does_not_fake_identity(
        self,
    ):
        import sqlite3

        log_id, cloud, service = self._fixture()
        original = replace(cloud.files["video"])
        cloud.fail_operation = "rename"
        with self.assertRaisesRegex(TimeoutError, "服务端已提交，但响应丢失"):
            service.reorganize(
                log_id, "failed-first-correction", service.detail(log_id)["version"],
                "2", "tv", season=2, episode=3,
            )
        self.assertEqual(cloud.files["video"], original)
        self.assertEqual(service.detail(log_id)["status"], "failed")
        self.assertEqual(list_manual_episode_confirmations("1"), [])
        self.assertEqual(list_manual_episode_confirmations("2"), [])

        service.reorganize(
            log_id,
            "correct",
            service.detail(log_id)["version"],
            "2",
            "tv",
            season=2,
            episode=3,
        )
        before_undo = replace(cloud.files["video"])
        learning_before = [dict(row) for row in list_manual_episode_confirmations("2")]
        self.assertEqual(len(learning_before), 1)
        with patch.object(
            organize_history_repo,
            "restore_organize_business_snapshot",
            side_effect=sqlite3.OperationalError("isolated commit failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                service.revert_latest(
                    log_id, "undo-fail", service.detail(log_id)["version"]
                )
        self.assertEqual(cloud.files["video"], before_undo)
        get_organize_log_result_8 = db.get_organize_log(log_id)
        assert get_organize_log_result_8 is not None
        self.assertEqual(get_organize_log_result_8["tmdb_id"], "2")
        get_organize_log_result_9 = db.get_organize_log(log_id)
        assert get_organize_log_result_9 is not None
        self.assertEqual(get_organize_log_result_9["status"], "revert_failed")
        self.assertEqual(get_organize_log_result_9["confirmation_actor"], "human")
        self.assertEqual(
            [dict(row) for row in list_manual_episode_confirmations("2")], learning_before,
        )

    def test_revert_uses_complete_operation_not_display_limit_and_stores_one_snapshot(
        self,
    ):
        log_id, cloud, service = self._fixture()
        members = []
        for index in range(305):
            file_id = f"metadata-{index}"
            name = f"First.2025.S01E07.extra{index}.nfo"
            cloud.files[file_id] = GuangYaFile(
                file_id, name, False, 100, "etag", "old-season"
            )
            members.append(
                {
                    "file_id": file_id,
                    "role": "metadata",
                    "original_parent_id": "incoming",
                    "original_name": f"First.S01E07.extra{index}.nfo",
                    "current_parent_id": "old-season",
                    "current_name": name,
                    "target_parent_id": "old-season",
                    "target_name": name,
                    "size": 100,
                    "etag": "etag",
                    "status": "success",
                }
            )
        db.add_organize_log_items(log_id, members)
        before = organize_history_repo.capture_organize_business_snapshot(log_id)
        originals = {file_id: replace(item) for file_id, item in cloud.files.items()}
        service.reorganize(
            log_id,
            "large-correct",
            service.detail(log_id)["version"],
            "2",
            "tv",
            season=2,
            episode=3,
        )
        self.assertEqual(len(service.detail(log_id)["operations"]), 300)
        steps = organize_history_repo.list_latest_reversible_organize_steps(log_id)
        self.assertEqual(len(steps), 306)
        self.assertEqual(sum(bool(row["state_before_json"]) for row in steps), 1)
        result = service.revert_latest(
            log_id, "large-undo", service.detail(log_id)["version"]
        )
        self.assertEqual(result["item_count"], 306)
        self.assertEqual(cloud.files, originals)
        self.assertEqual(organize_history_repo.capture_organize_business_snapshot(log_id), before)

    def test_special_zero_position_is_confirmed_and_movie_clears_marker(self):
        import json

        log_id, _cloud, service = self._fixture()
        service.reorganize(
            log_id,
            "special",
            service.detail(log_id)["version"],
            "1",
            "tv",
            season=0,
            episode=3,
        )
        self.assertEqual(service.preview_reorganize(log_id, "1", "tv")["season"], 0)
        service.reorganize(
            log_id, "movie", service.detail(log_id)["version"], "1", "movie"
        )
        get_organize_log_result_12 = db.get_organize_log(log_id)
        assert get_organize_log_result_12 is not None
        row = dict(get_organize_log_result_12)
        self.assertIsNone(row["season"])
        self.assertIsNone(row["episode"])
        self.assertNotIn("manual_position", json.loads(row["release_parse_json"]))
