"""R03 失败归属围栏：隔离 DB、内存云盘及临时 STRM，不调用外部服务。"""

from __future__ import annotations

# isort: off
import tests  # noqa: F401  # 必须先建立测试隔离，再导入应用。

# isort: on
import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import strm as strm_repo
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.repositories import strm_request_ownership as strm_request_ownership_repo
from app.clients.guangya import GuangYaFile
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories.strm_request_ownership import INTERRUPTED_ERROR
from tests import test_strm_request_ownership_recovery as recovery_tests


class STRMFailureLeaseScopeTests(unittest.TestCase):
    def setUp(self):
        # 复用业务夹具但不继承 TestCase，避免重复收集已有 16 个用例。
        self.fixture = recovery_tests.STRMRequestOwnershipRecoveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.enterContext(
            patch(
                "app.modules.scheduler.threading.Thread",
                recovery_tests._ParkedThread,
            )
        )

    def execute_recovered(self):
        scheduler = self.fixture.scheduler()
        return self.fixture.execute(
            scheduler, self.fixture.recovered_options(scheduler)
        )

    def queue_two_targets(self):
        request, admission, _ = self.fixture.seed()
        self.fixture.cloud.tree["source"] = []
        changes = []
        for episode, dirname in enumerate(("A", "B"), start=1):
            parent = "dir-" + dirname
            self.fixture.cloud.tree["source"].append(
                GuangYaFile(parent, dirname, True, 0, "", "source")
            )
            self.fixture.cloud.tree[parent] = [
                GuangYaFile(
                    f"v{episode}",
                    f"Show.S01E{episode:02d}.mkv",
                    False,
                    128,
                    f"e{episode}",
                    parent,
                )
            ]
            change = self.fixture.change(episode, dirname)
            change["parent_id"] = parent
            changes.append(change)
        scheduler = self.fixture.scheduler()
        self.fixture.queue(scheduler, [request], changes)
        return request, admission, changes

    def exhaust_first_target(self, *, include_second_in_last_failure=False):
        request, admission, changes = self.queue_two_targets()
        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=3600)
        db.init_db()
        media_subscriptions_repo.reconcile_startup_media_download_admissions()
        error = "target A permanent failure"
        for attempt in range(5):
            if attempt:
                strm_repo.reschedule_strm_change_targets([changes[0]], not_before_seconds=0)
            if include_second_in_last_failure and attempt == 4:
                strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=0)
            # scheduler() 装配真实生成入口后再注入本次失败。
            resumed = self.fixture.scheduler()
            with patch(
                "app.modules.scheduler.sync_strm_incremental",
                side_effect=OSError(error),
            ):
                result = self.fixture.execute(
                    resumed, self.fixture.recovered_options(resumed)
                )
            self.assertFalse(result["ok"], result)
        with db.get_conn() as conn:
            targets = {
                row["rel_dir"]: dict(row)
                for row in conn.execute("SELECT * FROM strm_change_queue")
            }
        self.assertEqual(targets["A"]["state"], "failed")
        self.assertEqual(targets["A"]["attempts"], 5)
        self.assertEqual(targets["B"]["state"], "queued")
        self.assertEqual(
            targets["B"]["lease_generation"],
            1 if include_second_in_last_failure else 0,
        )
        get_download_request_result = download_requests_repo.get_download_request(request)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["strm_status"], "failed")
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")
        return request, admission, changes[1], error

    def assert_second_target_does_not_clear_first_failure(self, *, also_failed):
        request, admission, second, error = self.exhaust_first_target(
            include_second_in_last_failure=also_failed
        )
        strm_repo.reschedule_strm_change_targets([second], not_before_seconds=0)
        result = self.execute_recovered()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(len(list(self.fixture.root.rglob("*.strm"))), 1)
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("failed", error))
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")

    def fail_recovered(self, error):
        scheduler = self.fixture.scheduler()
        with patch(
            "app.modules.scheduler.sync_strm_incremental", side_effect=OSError(error)
        ):
            result = self.fixture.execute(
                scheduler, self.fixture.recovered_options(scheduler)
            )
        self.assertFalse(result["ok"], result)

    def failure_receipts(self, request):
        with db.get_conn() as conn:
            return {
                row["rel_dir"]: row["failed_lease_generation"]
                for row in conn.execute(
                    "SELECT q.rel_dir,w.failed_lease_generation FROM strm_request_work w "
                    "JOIN strm_change_queue q ON q.id=w.work_key "
                    "WHERE w.request_id=? AND w.kind='change'", (request,),
                )
            }

    def fail_first_and_park(self):
        request, admission, changes = self.queue_two_targets()
        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=3600)
        self.fail_recovered("target A failure")
        strm_repo.reschedule_strm_change_targets([changes[0]], not_before_seconds=3600)
        return request, admission, changes

    def test_interleaved_failures_preserve_second_target_until_its_new_lease_succeeds(self):
        """A 先失败、B 后独立耗尽，不能由 A 的成功隐藏仍失败的 B。"""
        request, admission, changes = self.fail_first_and_park()
        get_download_request_result_9 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_9 is not None
        failed_request = dict(get_download_request_result_9)
        failed_admission = self.fixture.admission(admission)
        for _ in range(5):
            strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=0)
            self.fail_recovered("target B failure")
            # 只合并失败凭据，不替换 A 的父错误、完成时间或准入状态。
            get_download_request_result_10 = download_requests_repo.get_download_request(request)
            assert get_download_request_result_10 is not None
            self.assertEqual(dict(get_download_request_result_10), failed_request)
            self.assertEqual(self.fixture.admission(admission), failed_admission)
        with db.get_conn() as conn:
            second = dict(conn.execute(
                "SELECT * FROM strm_change_queue WHERE rel_dir='B'"
            ).fetchone())
        self.assertEqual((second["state"], second["attempts"]), ("failed", 5))

        strm_repo.reschedule_strm_change_targets([changes[0]], not_before_seconds=0)
        result = self.execute_recovered()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        get_download_request_result_2 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2["strm_status"], "failed")
        get_download_request_result_11 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_11 is not None
        self.assertEqual(dict(get_download_request_result_11), failed_request)
        self.assertEqual(self.fixture.admission(admission), failed_admission)
        self.assertEqual(self.failure_receipts(request), {"B": second["lease_generation"]})
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)
        self.assertEqual(len(list(self.fixture.root.rglob("*.strm"))), 1)

        # 已耗尽 B 通过既有变化入队恢复；不传 request_ids、不重新 trigger，
        # 不能先生成新 request generation 来掩盖原归属凭据的收口缺陷。
        self.assertEqual(strm_repo.enqueue_strm_change_targets([changes[1]]), 1)
        get_download_request_result_12 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_12 is not None
        self.assertEqual(dict(get_download_request_result_12), failed_request)
        self.assertEqual(self.failure_receipts(request), {"B": second["lease_generation"]})
        last = self.execute_recovered()
        self.assertTrue(last["ok"], last)
        self.assertEqual(last["stats"]["generated"], 1)
        with db.get_conn() as conn:
            recovered = conn.execute(
                "SELECT state,lease_generation FROM strm_change_queue WHERE id=?",
                (second["id"],),
            ).fetchone()
        self.assertEqual(recovered["state"], "completed")
        self.assertGreater(recovered["lease_generation"], second["lease_generation"])
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertGreater(row["strm_generation"], failed_request["strm_generation"])
        self.assertEqual(self.failure_receipts(request), {})
        self.assertEqual(self.fixture.admission(admission)["status"], "processing")
        self.assertEqual(len(list(self.fixture.root.rglob("*.strm"))), 2)

    def test_independent_revocation_blocks_running_worker_from_rearming_same_text_failure(self):
        """真实 B worker 仍持有有效 lease，也不能重授被通用写入撤销的凭据。"""
        request, admission, changes = self.fail_first_and_park()
        get_download_request_result_3 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_3 is not None
        error = get_download_request_result_3["strm_error"]
        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=0)
        independent = {}

        def revoke_while_worker_holds_lease(**_kwargs):
            with db.get_conn() as conn:
                running = [dict(row) for row in conn.execute(
                    "SELECT rel_dir,lease_owner,lease_generation FROM strm_change_queue "
                    "WHERE state='running'"
                )]
            self.assertEqual([row["rel_dir"] for row in running], ["B"])
            self.assertTrue(running[0]["lease_owner"])
            self.assertGreater(running[0]["lease_generation"], 0)
            self.assertGreater(self.failure_receipts(request)["A"], 0)
            # 同文本、同 request generation；不能靠错误文案判定恢复权限。
            download_requests_repo.update_download_request(request, strm_status="failed", strm_error=error)
            self.assertEqual(self.failure_receipts(request), {"A": -1, "B": -1})
            get_download_request_result_13 = download_requests_repo.get_download_request(request)
            assert get_download_request_result_13 is not None
            independent["request"] = dict(get_download_request_result_13)
            independent["admission"] = self.fixture.admission(admission)
            raise OSError(error)

        scheduler = self.fixture.scheduler()
        with patch(
            "app.modules.scheduler.sync_strm_incremental",
            side_effect=revoke_while_worker_holds_lease,
        ):
            failed = self.fixture.execute(
                scheduler, self.fixture.recovered_options(scheduler)
            )
        self.assertFalse(failed["ok"], failed)
        self.assertEqual(self.failure_receipts(request), {"A": -1, "B": -1})
        get_download_request_result_14 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_14 is not None
        self.assertEqual(dict(get_download_request_result_14), independent["request"])
        self.assertEqual(self.fixture.admission(admission), independent["admission"])

        # 后续真实新 lease 即使完成文件写入/ACK，也不能清除独立失败或重开准入。
        strm_repo.reschedule_strm_change_targets(changes, not_before_seconds=0)
        result = self.execute_recovered()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 2)
        self.assertEqual(self.failure_receipts(request), {})
        get_download_request_result_15 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_15 is not None
        self.assertEqual(dict(get_download_request_result_15), independent["request"])
        self.assertEqual(self.fixture.admission(admission), independent["admission"])
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)

    def test_failed_parent_merges_only_current_dirty_lease_without_mutating_parent(self):
        request, admission, changes = self.fail_first_and_park()
        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=0)
        claimed = strm_repo.claim_strm_change_targets(owner="current-worker", limit=1)[0]
        owner = strm_request_ownership_repo.current_strm_request_owners([request])[0]
        get_download_request_result_16 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_16 is not None
        before = dict(get_download_request_result_16)
        before_admission = self.fixture.admission(admission)
        receipts = self.failure_receipts(request)
        for invalid_owner, invalid_claims in (
            ({**owner, "generation": owner["generation"] - 1}, [claimed]),
            ({**owner, "organize_task_id": "other-task"}, [claimed]),
            (owner, [{**claimed, "lease_owner": "other-worker"}]),
            (owner, [{**claimed, "lease_generation": claimed["lease_generation"] + 1}]),
            (owner, []),
        ):
            with self.subTest(owner=invalid_owner, claims=invalid_claims):
                self.assertFalse(strm_request_ownership_repo.update_strm_request_state(
                    invalid_owner, claimed_targets=invalid_claims,
                    strm_status="failed", strm_error="target B failure",
                ))
                self.assertEqual(self.failure_receipts(request), receipts)
        strm_repo.enqueue_strm_change_targets([changes[1]])  # 本轮仍有 lease 的 dirty target。
        self.assertTrue(strm_request_ownership_repo.update_strm_request_state(
            owner, claimed_targets=[claimed],
            strm_status="failed", strm_error="target B failure",
        ))
        self.assertEqual(self.failure_receipts(request), {
            "A": receipts["A"], "B": claimed["lease_generation"],
        })
        get_download_request_result_17 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_17 is not None
        self.assertEqual(dict(get_download_request_result_17), before)
        self.assertEqual(self.fixture.admission(admission), before_admission)

    def test_split_failed_targets_recover_after_each_actual_target_succeeds(self):
        """保留其它目标错误不能阻断合法分批重试；最后一个失败目标可正常恢复。"""
        request, admission, changes = self.queue_two_targets()
        resumed = self.fixture.scheduler()
        error = "shared initial failure"
        with patch(
            "app.modules.scheduler.sync_strm_incremental", side_effect=OSError(error)
        ):
            first = self.fixture.execute(
                resumed, self.fixture.recovered_options(resumed)
            )
        self.assertFalse(first["ok"], first)
        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=3600)
        strm_repo.reschedule_strm_change_targets([changes[0]], not_before_seconds=0)
        first_retry = self.execute_recovered()
        self.assertTrue(first_retry["ok"], first_retry)
        self.assertEqual(first_retry["stats"]["generated"], 1)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("failed", error))
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")

        strm_repo.reschedule_strm_change_targets([changes[1]], not_before_seconds=0)
        last_retry = self.execute_recovered()
        self.assertTrue(last_retry["ok"], last_retry)
        self.assertEqual(last_retry["stats"]["generated"], 1)
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)
        self.assertEqual(len(list(self.fixture.root.rglob("*.strm"))), 2)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.fixture.admission(admission)["status"], "processing")

    def test_unclaimed_target_cannot_clear_an_exhausted_target_failure(self):
        self.assert_second_target_does_not_clear_first_failure(also_failed=False)

    def test_retry_of_one_failed_target_keeps_other_exhausted_failure(self):
        # B 确实失败过也不能单独释放 A 的错误，不能只把 >=0 改成 >0。
        self.assert_second_target_does_not_clear_first_failure(also_failed=True)

    def test_same_text_independent_failure_revokes_cold_change_recovery(self):
        request, admission, _ = self.fixture.seed()
        scheduler = self.fixture.scheduler()
        self.fixture.queue(scheduler, [request], [self.fixture.change()])
        db.init_db()
        media_subscriptions_repo.reconcile_startup_media_download_admissions()
        get_download_request_result_4 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_4 is not None
        self.assertEqual(
            get_download_request_result_4["strm_error"], INTERRUPTED_ERROR
        )
        download_requests_repo.update_download_request(
            request, strm_status="failed", strm_error=INTERRUPTED_ERROR
        )
        db.init_db()  # 已 failed 的同文本独立写入不能在再次启动时补发凭据。
        result = self.execute_recovered()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual(
            (row["strm_status"], row["strm_error"]), ("failed", INTERRUPTED_ERROR)
        )
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")

    def test_legacy_queued_change_work_recovers_on_first_startup(self):
        request, admission, _ = self.fixture.seed()
        scheduler = self.fixture.scheduler()
        self.fixture.queue(scheduler, [request], [self.fixture.change()])
        get_download_request_result_5 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["strm_status"], "queued")
        with db.get_conn() as conn:
            # 升级前 work 的真实默认形态，不通过新代码 seed 的 0 掩盖兼容性。
            conn.execute(
                "UPDATE strm_request_work SET failed_lease_generation=-1 WHERE request_id=?",
                (request,),
            )
        db.init_db()
        media_subscriptions_repo.reconcile_startup_media_download_admissions()
        result = self.execute_recovered()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stats"]["generated"], 1)
        self.assertEqual(strm_repo.count_pending_strm_change_targets(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.fixture.admission(admission)["status"], "processing")

    def test_legacy_queued_refresh_work_recovers_on_first_startup(self):
        request, admission = self.cold_refresh(legacy_work=True)
        STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.fixture.admission(admission)["status"], "processing")

    def test_startup_only_arms_matching_active_request_ownership(self):
        requests = []
        for episode, mismatch in enumerate(
            ("generation", "task", "cancelled", "failed", "resubmitted"), start=1
        ):
            request, _, _ = self.fixture.seed(episode)
            self.fixture.owned_queue(request, [self.fixture.change(episode)])
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE strm_request_work SET failed_lease_generation=-1 WHERE request_id=?",
                    (request,),
                )
                if mismatch == "generation":
                    conn.execute(
                        "UPDATE strm_request_work SET generation=generation-1 WHERE request_id=?",
                        (request,),
                    )
                elif mismatch == "task":
                    conn.execute(
                        "UPDATE strm_request_work SET organize_task_id='previous-task' WHERE request_id=?",
                        (request,),
                    )
            if mismatch not in {"generation", "task"}:
                download_requests_repo.update_download_request(request, status=mismatch)
            requests.append(request)
        db.init_db()
        with db.get_conn() as conn:
            for request in requests:
                proof = conn.execute(
                    "SELECT failed_lease_generation FROM strm_request_work WHERE request_id=?",
                    (request,),
                ).fetchone()[0]
                self.assertEqual(proof, -1, request)

    def cold_refresh(self, *, legacy_work=False):
        request, admission, _ = self.fixture.seed()
        owners = self.fixture.owned_queue(request, [])
        path = self.fixture.root / "cold.strm"
        path.write_text("http://media.invalid/v1\n", encoding="utf-8")
        strm_repo.enqueue_strm_refresh_paths([str(path)], request_owners=owners)
        if legacy_work:
            get_download_request_result_6 = download_requests_repo.get_download_request(request)
            assert get_download_request_result_6 is not None
            self.assertEqual(get_download_request_result_6["strm_status"], "queued")
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE strm_request_work SET failed_lease_generation=-1 WHERE request_id=?",
                    (request,),
                )
        else:
            self.assertTrue(
                strm_request_ownership_repo.update_strm_request_state(owners[0], strm_status="running")
            )
        db.init_db()
        media_subscriptions_repo.reconcile_startup_media_download_admissions()
        get_download_request_result_7 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_7 is not None
        self.assertEqual(get_download_request_result_7["strm_status"], "failed")
        get_download_request_result_8 = download_requests_repo.get_download_request(request)
        assert get_download_request_result_8 is not None
        self.assertEqual(
            get_download_request_result_8["strm_error"], INTERRUPTED_ERROR
        )
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")
        return request, admission

    def test_cold_outbox_ack_recovers_owned_interruption(self):
        request, admission = self.cold_refresh()
        STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual((row["strm_status"], row["strm_error"]), ("completed", ""))
        self.assertEqual(self.fixture.admission(admission)["status"], "processing")

    def test_same_text_independent_failure_blocks_cold_outbox_ack(self):
        request, admission = self.cold_refresh()
        download_requests_repo.update_download_request(
            request, strm_status="failed", strm_error=INTERRUPTED_ERROR
        )
        db.init_db()  # 已 failed 的同文本独立写入不能在再次启动时补发凭据。
        STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)
        row = download_requests_repo.get_download_request(request)
        assert row is not None
        self.assertEqual(
            (row["strm_status"], row["strm_error"]), ("failed", INTERRUPTED_ERROR)
        )
        self.assertEqual(self.fixture.admission(admission)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
