"""STRM 请求归属与恢复业务验收：只使用隔离 DB、内存云盘及临时 STRM。"""

from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import tests  # noqa: F401  # 先加载测试隔离环境，再导入 app。
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories.organize_history import settle_organize_task_run
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient


class _ParkedThread:
    """只停放 scheduler waiter；测试显式执行已入队 options。"""

    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def is_alive(self):
        return True

    def join(self, **kwargs):
        pass


class STRMRequestOwnershipRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("ownership.db"))
        self.root = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="strm-ownership-"))
        )
        self.network = self.enterContext(
            patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")
            )
        )
        self.enterContext(
            patch(
                "socket.socket.connect_ex",
                side_effect=AssertionError("network forbidden"),
            )
        )
        self.enterContext(
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            )
        )
        self.enterContext(
            patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden"))
        )
        self.sub = db.add_media_subscription(
            provider="tmdb",
            external_id="991",
            tmdb_id="991",
            media_type="tv",
            title="恢复验收",
            monitor_mode="missing",
            action="confirm",
            download_target="guangya",
            check_interval_minutes=60,
        )
        self.cloud = _TreeClient({"source": []})
        self.refresh = self.enterContext(
            patch(
                "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
                return_value={"Jellyfin": "queued"},
            )
        )
        self.enterContext(
            patch("app.modules.scheduler._publish_linked_notification_threads")
        )

    def tearDown(self):
        self.network.assert_not_called()

    def seed(self, episode=1):
        key = f"tmdb:991:tv:S01E{episode:03d}"
        candidate = db.replace_media_subscription_candidates(
            self.sub,
            key,
            season=1,
            episode=episode,
            candidates=[{"result_id": key, "title": key}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission = db.claim_media_download_admission(
            media_key=key,
            tmdb_id="991",
            media_type="tv",
            subscription_id=self.sub,
            candidate_id=candidate,
            season=1,
            episode=episode,
            subscription_revision=1,
        )
        assert admission is not None
        self.assertTrue(
            db.begin_media_download_dispatch(
                admission,
                subscription_id=self.sub,
                subscription_revision=1,
            )
        )
        request, _ = db.create_download_request(key, "magnet", admission_id=admission)
        db.update_download_request(
            request,
            status="completed",
            gy_status="completed",
            targets="guangya",
            gy_target_dir=f"incoming-{episode}",
            organize_started=1,
            organize_task_id=f"organize-{episode}",
            organize_status="completed",
            notification_delivery_status="sent",
        )
        db.sync_media_download_admission_for_request(request)
        self.cloud.tree["source"].append(
            GuangYaFile(
                f"v{episode}",
                f"Show.S01E{episode:02d}.mkv",
                False,
                128,
                f"e{episode}",
                "source",
            )
        )
        return request, admission, key

    @staticmethod
    def change(episode=1, rel_dir=""):
        return {
            "source_id": "source",
            "kind": "video",
            "action": "upsert",
            "file_id": f"v{episode}",
            "parent_id": "source",
            "name": f"Show.S01E{episode:02d}.mkv",
            "rel_dir": rel_dir,
        }

    def scheduler(self):
        scheduler = STRMScheduler()
        source = {"id": "source", "name": "source", "rel_prefix": "Films"}
        values = {
            "STRM_ROOT": str(self.root),
            "GY_STRM_BASE_URL": "http://media.invalid",
        }
        for name, value in [
            ("validate_config", ""),
            ("_source_dirs", [source]),
            ("_video_exts", {"mkv"}),
            ("_metadata_exts", set()),
        ]:
            self.enterContext(patch.object(scheduler, name, return_value=value))
        for name in (
            "_notify_success",
            "_notify_details",
            "_notify_scoped_results",
            "_notify_scoped_failure",
        ):
            self.enterContext(patch.object(scheduler, name))
        self.enterContext(
            patch(
                "app.modules.scheduler.get",
                side_effect=lambda k, d="": values.get(k, d),
            )
        )
        self.enterContext(patch("app.modules.scheduler.get_int", return_value=0))
        self.enterContext(
            patch(
                "app.modules.scheduler.configured_strm_source_plans",
                return_value=([source], ""),
            )
        )
        self.enterContext(
            patch(
                "app.modules.scheduler.sync_strm_incremental",
                side_effect=lambda **kw: strm.sync_strm_incremental(
                    client=cast(GuangYaClient, self.cloud), **kw
                ),
            )
        )
        self.enterContext(
            patch(
                "app.modules.scheduler.sync_strm",
                side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, self.cloud), **kw),
            )
        )
        self.enterContext(patch.object(scheduler, "_drain_persisted_change_queue"))
        return scheduler

    def queue(self, scheduler, requests, changes):
        with patch("app.modules.scheduler.threading.Thread", _ParkedThread):
            result = scheduler.trigger(
                "organize",
                download_request_ids=requests,
                organize_changes=changes,
                debounce_seconds=30,
                notify_override=False,
            )
        self.assertTrue(result["ok"], result)
        options = dict(scheduler._pending_organize_options)
        db.reschedule_strm_change_targets(changes, not_before_seconds=0)
        return options

    def execute(self, scheduler, options):
        scheduler._pending_organize_options = None
        self.assertTrue(scheduler._run_lock.acquire(blocking=False))
        scheduler._run_options = dict(options)
        scheduler._running = True
        return scheduler._execute_locked("organize", db.add_task_run("strm_sync", "organize"))

    def recovered_options(self, scheduler):
        with patch.object(
            scheduler, "_queue_organize_trigger", return_value={"ok": True}
        ) as queue:
            scheduler._schedule_persisted_change_queue(0)
        return queue.call_args.args[0]

    def resubmit_qb(self, request):
        db.update_download_request(request, targets="both", status="manual_review", qb_status="manual_review")
        successor, _ = db.create_download_request(f"qb-successor-{request}", "magnet", initial_targets="qb")
        self.assertTrue(db.mark_download_request_resubmitted(
            request, successor_request_id=successor, targets="qb",
        ))
        return successor

    def test_qb_only_resubmit_preserves_organize_to_strm_completion(self):
        request, _, _ = self.seed()
        db.update_download_request(request, organize_started=0, organize_status="pending", strm_status="pending")
        self.resubmit_qb(request)
        self.assertTrue(db.claim_download_request_organize(request))
        task_id = "cloud-followup-after-qb-retry"
        run_id = db.add_task_run("guangya_organize", "download", result=json.dumps({"task_id": task_id}))
        db.update_download_request(request, organize_task_id=task_id, organize_run_id=run_id, organize_status="running")
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        settle_organize_task_run(run_id, {"task_id": task_id, "request_settlement": {
            "status": "completed", "error": "", "message": "整理完成", "requests": {
                str(request): {"organize_status": "completed", "organize_finished_at": db.now(), "_strm_generation": 0},
            },
        }})
        result = self.execute(scheduler, options)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        self.refresh.assert_called_once()
        db.init_db()
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["organize_status"], row["strm_status"]), ("completed", "completed"))
        self.assertEqual((row["status"], row["qb_status"], row["gy_status"]),
                         ("resubmitted", "resubmitted", "completed"))

    def test_qb_only_resubmit_keeps_queued_work_recoverable_after_restart(self):
        request, _, _ = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        owner = options["download_request_owners"][0]
        successor = self.resubmit_qb(request)
        db.init_db()
        # 新 qB 提交可由启动恢复独立转为待核验；后续光鸭工作不得再修改它。
        next_row = db.get_download_request(successor)
        assert next_row is not None
        successor_snapshot = dict(next_row)
        resumed = self.scheduler()
        result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(row["strm_generation"], owner["generation"])
        self.assertEqual(dict(db.get_download_request(successor) or {}), successor_snapshot)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)

    def test_qb_only_resubmit_keeps_failed_change_retry_owned(self):
        request, _, _ = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        old_owner = options["download_request_owners"][0]
        with patch("app.modules.scheduler.sync_strm_incremental", side_effect=OSError("transient source failure")):
            self.assertFalse(self.execute(scheduler, options)["ok"])
        self.resubmit_qb(request)
        db.reschedule_strm_change_targets([self.change()], not_before_seconds=0)
        resumed = self.scheduler()
        self.assertTrue(self.execute(resumed, self.recovered_options(resumed))["ok"])
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertGreater(row["strm_generation"], old_owner["generation"])
        self.assertFalse(db.update_strm_request_state(old_owner, strm_status="failed", strm_error="late failure"))

    def test_qb_only_resubmit_keeps_refresh_receipt_owned_after_restart(self):
        request, _, _ = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        self.refresh.side_effect = OSError("refresh temporarily unavailable")
        result = self.execute(scheduler, options)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["refresh_pending"])
        self.assertGreater(db.count_strm_refresh_paths(), 0)
        self.resubmit_qb(request)
        db.init_db()
        self.refresh.side_effect = None
        STRMMetadataWorker()._flush_media_refresh(force=True)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)

    def test_qb_only_resubmit_does_not_bypass_cloud_or_generation_revocation(self):
        cases = ("cancelled", "failed", "guangya", "both", "cleared", "qb_target", "new_task", "new_generation")
        for episode, case in enumerate(cases, start=1):
            with self.subTest(case=case):
                request, _, _ = self.seed(episode)
                successor = self.resubmit_qb(request)
                change = self.change(episode)
                scheduler = self.scheduler()
                owner = self.queue(scheduler, [request], [change])["download_request_owners"][0]
                if case in {"cancelled", "failed"}:
                    db.update_download_request(request, status=case)
                elif case in {"guangya", "both"}:
                    db.update_download_request(request, organize_status="failed", strm_status="failed")
                    self.assertTrue(db.mark_download_request_resubmitted(
                        request, successor_request_id=successor, targets=case,
                    ))
                elif case == "cleared":
                    db.update_download_request(request, organize_status="cleared", attention_cleared_at=db.now())
                elif case == "qb_target":
                    db.update_download_request(request, targets="qb")
                elif case == "new_task":
                    db.update_download_request(request, organize_task_id="replacement-task")
                else:
                    self.queue(scheduler, [request], [change])
                row = db.get_download_request(request)
                assert row is not None
                snapshot = dict(row)
                self.assertFalse(db.update_strm_request_state(owner, strm_status="running", strm_error="stale"))
                self.assertEqual(dict(db.get_download_request(request) or {}), snapshot)
                work_ids = [row["id"] for row in db.list_strm_change_queue()]
                self.assertNotIn(owner, db.request_owners_for_work("change", work_ids))
                if case != "new_generation":
                    _, owners = db.enqueue_strm_change_targets([change], download_request_ids=[request], with_owners=True)
                    if case != "new_task":
                        self.assertEqual(owners, [], "已撤销的光鸭任务不能重新领取归属")
                db.init_db()
                self.assertFalse(db.update_strm_request_state(owner, strm_status="completed"))

    @staticmethod
    def admission(admission_id):
        with db.get_conn() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
            )

    def test_cold_recovery_projects_original_request_and_admission_without_regeneration(
        self,
    ):
        request, admission, key = self.seed()
        scheduler = self.scheduler()
        self.queue(scheduler, [request], [self.change()])
        db.init_db()
        db.reconcile_startup_media_download_admissions()
        resumed = self.scheduler()
        result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        get_download_request_result = db.get_download_request(request)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["strm_status"], "completed")
        self.assertEqual(self.admission(admission)["status"], "processing")
        db.reconcile_media_download_admissions(self.sub, {key}, expected_revision=1)
        self.assertEqual(self.admission(admission)["status"], "completed")
        self.assertEqual(self.refresh.call_count, 1)

    def test_refresh_retry_clears_only_handoff_partial_without_asserting_library_presence(
        self,
    ):
        request, admission, key = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        self.refresh.side_effect = OSError("synthetic handoff outage")
        first = self.execute(scheduler, options)
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["strm_partial"])
        self.assertTrue(first["refresh_pending"])
        self.assertEqual(first["stats"]["generated"], 1)
        get_download_request_result_2 = db.get_download_request(request)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2["strm_status"], "partial")
        self.assertGreater(db.count_strm_refresh_paths(), 0)
        self.refresh.side_effect = None
        STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        get_download_request_result_3 = db.get_download_request(request)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["strm_status"], "completed")
        get_download_request_result_4 = db.get_download_request(request)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["strm_error"], "")
        self.assertEqual(self.admission(admission)["status"], "processing")
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        db.reconcile_media_download_admissions(self.sub, {key}, expected_revision=1)
        self.assertEqual(self.admission(admission)["status"], "completed")

    @patch("app.modules.scheduler.threading.Thread", _ParkedThread)
    def test_cold_recovery_does_not_reactivate_superseded_admission(self):
        """新准入已占用媒体键时，旧 STRM 仍应恢复，但不能抢回准入归属。"""
        request, admission, key = self.seed()
        scheduler = self.scheduler()
        self.queue(scheduler, [request], [self.change()])
        db.init_db()
        db.reconcile_startup_media_download_admissions()
        get_download_request_result_5 = db.get_download_request(request)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["strm_status"], "failed")
        self.assertEqual(self.admission(admission)["status"], "failed")

        candidate = db.replace_media_subscription_candidates(
            self.sub,
            key,
            season=1,
            episode=1,
            candidates=[{"result_id": "replacement", "title": "replacement"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        newer_admission = db.claim_media_download_admission(
            media_key=key,
            tmdb_id="991",
            media_type="tv",
            subscription_id=self.sub,
            candidate_id=candidate,
            season=1,
            episode=1,
            subscription_revision=1,
        )
        self.assertTrue(newer_admission)
        self.assertNotEqual(newer_admission, admission)
        newer_snapshot = self.admission(newer_admission)
        self.assertEqual(newer_snapshot["status"], "claimed")

        resumed = self.scheduler()
        result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.admission(newer_admission), newer_snapshot)
        self.assertNotIn(
            self.admission(admission)["status"],
            {"claimed", "dispatching", "submitted", "downloading", "processing"},
        )

    def test_successful_same_work_lease_retry_recovers_request_and_admission(self):
        self._assert_same_work_lease_retry(legacy_error=False)

    def test_same_work_lease_retry_recovers_legacy_unredacted_admission(self):
        self._assert_same_work_lease_retry(legacy_error=True)

    @patch("app.modules.scheduler.threading.Thread", _ParkedThread)
    def _assert_same_work_lease_retry(self, *, legacy_error):
        """本任务失败后的新 lease 成功必须收敛状态，不依赖重新 trigger。"""
        request, admission, _ = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        error = "synthetic transient source failure api_key=super-secret"
        with patch(
            "app.modules.scheduler.sync_strm_incremental", side_effect=OSError(error)
        ):
            first = self.execute(scheduler, options)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["error"], error)
        get_download_request_result_6 = db.get_download_request(request)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6["strm_status"], "failed")
        self.assertEqual(self.admission(admission)["status"], "failed")
        self.assertNotIn("super-secret", self.admission(admission)["error"])
        if legacy_error:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE media_download_admissions SET error=? WHERE id=?",
                    (f"下载后处理失败（STRM 联动）：{error}", admission),
                )
        self.assertEqual(db.count_pending_strm_change_targets(), 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 0)

        # 仅缩短既有退避，不能重新 trigger 生成新归属来掩盖重试投影缺陷。
        db.reschedule_strm_change_targets([self.change()], not_before_seconds=0)
        resumed = self.scheduler()
        result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        # STRM/刷新交接成功不等于媒体库已提供存在性证据。
        self.assertEqual(self.admission(admission)["status"], "processing")

    @patch("app.modules.scheduler.threading.Thread", _ParkedThread)
    def test_same_text_later_failure_is_not_revived_by_successful_new_lease(self):
        """通用更新再次写入相同失败文本，也必须撤销队列自身失败的恢复资格。"""
        request, admission, _ = self.seed()
        scheduler = self.scheduler()
        options = self.queue(scheduler, [request], [self.change()])
        error = "synthetic transient source failure"
        with patch(
            "app.modules.scheduler.sync_strm_incremental", side_effect=OSError(error)
        ):
            first = self.execute(scheduler, options)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["error"], error)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("failed", error))
        self.assertEqual(self.admission(admission)["status"], "failed")
        self.assertEqual(db.count_pending_strm_change_targets(), 1)
        with db.get_conn() as conn:
            target = conn.execute(
                "SELECT id,lease_generation FROM strm_change_queue"
            ).fetchone()
            target_id, failed_lease = target["id"], target["lease_generation"]

        # 不能因文本相同就把后来独立写入误认为旧任务自身的失败。
        db.update_download_request(request, strm_status="failed", strm_error=error)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("failed", error))
        db.reschedule_strm_change_targets([self.change()], not_before_seconds=0)
        resumed = self.scheduler()
        result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.root.rglob("*.strm"))), 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        with db.get_conn() as conn:
            completed = conn.execute(
                "SELECT state,lease_generation FROM strm_change_queue WHERE id=?",
                (target_id,),
            ).fetchone()
            self.assertEqual(completed["state"], "completed")
            self.assertGreater(completed["lease_generation"], failed_lease)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("failed", error))
        self.assertEqual(self.admission(admission)["status"], "failed")

    @patch("app.modules.scheduler.threading.Thread", _ParkedThread)
    def test_overflow_refresh_waits_for_all_paths_after_cold_recovery(self):
        """溢出目录同样归属于恢复请求；只补投刷新，不重复生成 STRM。"""
        request, admission, _ = self.seed()
        self.cloud.tree["source"] = []
        changes = []
        for episode, dirname in enumerate(("A", "B", "C"), start=1):
            parent = "dir-" + dirname
            self.cloud.tree["source"].append(
                GuangYaFile(parent, dirname, True, 0, "", "source")
            )
            self.cloud.tree[parent] = [
                GuangYaFile(
                    f"v{episode}",
                    f"Show.S01E{episode:02d}.mkv",
                    False,
                    128,
                    f"e{episode}",
                    parent,
                )
            ]
            change = self.change(episode, dirname)
            change["parent_id"] = parent
            changes.append(change)

        scheduler = self.scheduler()
        self.queue(scheduler, [request], changes)
        db.init_db()
        db.reconcile_startup_media_download_admissions()
        resumed = self.scheduler()

        def handoff(paths, **kwargs):
            if any({"B", "C"}.intersection(Path(path).parts) for path in paths):
                raise OSError("synthetic overflow handoff outage")
            return {"Jellyfin": "queued"}

        self.refresh.side_effect = handoff
        # 缩小预算以低成本触发真实溢出分支，不伪造 STRM 输出或生成统计。
        with (
            patch("app.modules.strm._MAX_TRACKED_CHANGED_PATHS", 1),
            patch("app.modules.strm._MAX_TRACKED_OVERFLOW_DIRS", 1),
        ):
            result = self.execute(resumed, self.recovered_options(resumed))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 3)
        self.assertFalse(result["strm_partial"])
        self.assertEqual(db.count_pending_strm_change_targets(), 0)
        generated = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*.strm")
        }
        self.assertEqual(len(generated), 3)
        pending_dirs = {
            Path(entry["path"]).name for entry in db.list_strm_refresh_entries()
        }
        self.assertTrue({"B", "C"} <= pending_dirs, pending_dirs)
        get_download_request_result_7 = db.get_download_request(request)
        assert get_download_request_result_7 is not None
        self.assertNotEqual(get_download_request_result_7["strm_status"], "completed")

        worker = STRMMetadataWorker()
        worker._flush_media_refresh(force=True)
        self.assertGreater(db.count_strm_refresh_paths(), 0)
        get_download_request_result_8 = db.get_download_request(request)
        assert get_download_request_result_8 is not None
        self.assertNotEqual(get_download_request_result_8["strm_status"], "completed")
        self.refresh.side_effect = None
        worker._flush_media_refresh(force=True)
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.admission(admission)["status"], "processing")
        self.assertEqual(
            {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*.strm")
            },
            generated,
        )

    def owned_queue(self, request, changes):
        count, owners = db.enqueue_strm_change_targets(
            changes, download_request_ids=[request], with_owners=True
        )
        return owners

    def complete_target(self, target):
        return db.complete_strm_change_target(
            target["id"],
            expected_owner=target["lease_owner"],
            expected_lease_generation=target["lease_generation"],
        )

    def handoff(self, request, paths):
        from app.repositories.strm_request_ownership import REFRESH_PENDING_ERROR

        owners = self.owned_queue(request, [])
        db.update_download_request(
            request, strm_status="partial", strm_error=REFRESH_PENDING_ERROR
        )
        return db.enqueue_strm_refresh_paths(paths, request_owners=owners)

    def test_request_waits_for_all_change_targets(self):
        request, _, _ = self.seed()
        self.owned_queue(request, [self.change(), self.change(2, "other")])
        first = db.claim_strm_change_targets(owner="first", limit=1)[0]
        self.assertEqual(self.complete_target(first), "completed")
        get_download_request_result_9 = db.get_download_request(request)
        assert get_download_request_result_9 is not None
        self.assertNotEqual(
            get_download_request_result_9["strm_status"], "completed"
        )
        second = db.claim_strm_change_targets(owner="second", limit=1)[0]
        self.assertEqual(self.complete_target(second), "completed")
        get_download_request_result_10 = db.get_download_request(request)
        assert get_download_request_result_10 is not None
        self.assertEqual(get_download_request_result_10["strm_status"], "completed")

    def test_dirty_target_does_not_complete_either_request_early(self):
        first, _, _ = self.seed()
        second, _, _ = self.seed(2)
        self.owned_queue(first, [self.change()])
        claimed = db.claim_strm_change_targets(owner="first")[0]
        self.owned_queue(second, [self.change(2)])
        self.assertEqual(self.complete_target(claimed), "queued")
        for request in (first, second):
            get_download_request_result_11 = db.get_download_request(request)
            assert get_download_request_result_11 is not None
            self.assertNotEqual(
                get_download_request_result_11["strm_status"], "completed"
            )
        self.assertEqual(
            self.complete_target(db.claim_strm_change_targets(owner="second")[0]),
            "completed",
        )
        for request in (first, second):
            get_download_request_result_12 = db.get_download_request(request)
            assert get_download_request_result_12 is not None
            self.assertEqual(
                get_download_request_result_12["strm_status"], "completed"
            )

    def test_stale_change_lease_cannot_complete_request(self):
        request, _, _ = self.seed()
        self.owned_queue(request, [self.change()])
        old = db.claim_strm_change_targets(owner="old")[0]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_change_queue SET lease_until=0 WHERE id=?", (old["id"],)
            )
        new = db.claim_strm_change_targets(owner="new")[0]
        self.assertEqual(self.complete_target(old), "stale")
        get_download_request_result_13 = db.get_download_request(request)
        assert get_download_request_result_13 is not None
        self.assertNotEqual(
            get_download_request_result_13["strm_status"], "completed"
        )
        self.assertEqual(self.complete_target(new), "completed")
        get_download_request_result_14 = db.get_download_request(request)
        assert get_download_request_result_14 is not None
        self.assertEqual(get_download_request_result_14["strm_status"], "completed")

    def test_refresh_ack_waits_for_all_paths(self):
        request, _, _ = self.seed()
        entries = self.handoff(
            request, [str(self.root / "a.strm"), str(self.root / "b.strm")]
        )
        db.acknowledge_strm_refresh_paths(entries[:1])
        get_download_request_result_15 = db.get_download_request(request)
        assert get_download_request_result_15 is not None
        self.assertEqual(get_download_request_result_15["strm_status"], "partial")
        db.acknowledge_strm_refresh_paths(entries[1:])
        get_download_request_result_16 = db.get_download_request(request)
        assert get_download_request_result_16 is not None
        self.assertEqual(get_download_request_result_16["strm_status"], "completed")

    def test_old_refresh_token_cannot_complete_new_path_event(self):
        request, _, _ = self.seed()
        path = str(self.root / "same.strm")
        old = self.handoff(request, [path])
        new = db.enqueue_strm_refresh_paths([path])
        self.assertEqual(db.acknowledge_strm_refresh_paths(old), 0)
        get_download_request_result_17 = db.get_download_request(request)
        assert get_download_request_result_17 is not None
        self.assertEqual(get_download_request_result_17["strm_status"], "partial")
        self.assertEqual(db.acknowledge_strm_refresh_paths(new), 1)
        get_download_request_result_18 = db.get_download_request(request)
        assert get_download_request_result_18 is not None
        self.assertEqual(get_download_request_result_18["strm_status"], "completed")

    def test_previous_organize_attempt_cannot_finish_new_generation(self):
        request, _, _ = self.seed()
        old = self.handoff(request, [str(self.root / "old.strm")])
        owner = db.current_strm_request_owners([request])[0]
        db.update_download_request(request, organize_task_id="new-organize")
        self.owned_queue(request, [])
        self.assertFalse(
            db.update_strm_request_state(owner, strm_status="completed", strm_error="")
        )
        db.acknowledge_strm_refresh_paths(old)
        get_download_request_result_19 = db.get_download_request(request)
        assert get_download_request_result_19 is not None
        self.assertEqual(get_download_request_result_19["strm_status"], "queued")

    def test_refresh_success_does_not_override_later_failure(self):
        request, admission, _ = self.seed()
        entries = self.handoff(request, [str(self.root / "failed.strm")])
        db.update_download_request_and_sync_media_admission(
            request, strm_status="failed", strm_error="new independent failure"
        )
        db.acknowledge_strm_refresh_paths(entries)
        get_download_request_result_20 = db.get_download_request(request)
        assert get_download_request_result_20 is not None
        self.assertEqual(get_download_request_result_20["strm_status"], "failed")
        get_download_request_result_21 = db.get_download_request(request)
        assert get_download_request_result_21 is not None
        self.assertEqual(
            get_download_request_result_21["strm_error"], "new independent failure"
        )
        self.assertEqual(self.admission(admission)["status"], "failed")

    def test_refresh_success_does_not_revive_cancelled_request(self):
        request, _, _ = self.seed()
        entries = self.handoff(request, [str(self.root / "cancelled.strm")])
        db.update_download_request(request, status="cancelled", strm_status="failed")
        db.acknowledge_strm_refresh_paths(entries)
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual((row["status"], row["strm_status"]), ("cancelled", "failed"))

    def test_legacy_unowned_refresh_does_not_guess_download_ownership(self):
        request, _, _ = self.seed()
        db.update_download_request(
            request, strm_status="partial", strm_error="legacy handoff"
        )
        entries = db.enqueue_strm_refresh_paths([str(self.root / "legacy.strm")])
        db.acknowledge_strm_refresh_paths(entries)
        get_download_request_result_22 = db.get_download_request(request)
        assert get_download_request_result_22 is not None
        self.assertEqual(get_download_request_result_22["strm_status"], "partial")

    def test_v28_migration_is_idempotent_and_does_not_guess_legacy_links(self):
        import sqlite3
        from app.database_migrations import _migrate_download_resource_and_strm_ownership_v29

        with sqlite3.connect(":memory:") as conn:
            conn.execute(
                "CREATE TABLE download_requests(id INTEGER PRIMARY KEY, status TEXT)"
            )
            conn.execute("INSERT INTO download_requests VALUES(1,'completed')")
            _migrate_download_resource_and_strm_ownership_v29(conn)
            _migrate_download_resource_and_strm_ownership_v29(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT strm_generation,content_type FROM download_requests"
                ).fetchone(),
                (0, ""),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM strm_request_work").fetchone()[0], 0
            )


if __name__ == "__main__":
    unittest.main()
