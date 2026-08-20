"""STRM 变化目标持久化状态机（Sprint 2）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 2.1 队列表状态转换、幂等合并与 dirty 不丢事件
- Task 2.2 租约领取、崩溃恢复、dirty 重放与有界退避重试
- Task 2.3 变化目录结构化结果（changed_strm_paths / changed_dirs）
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from app import database as db
from app.modules.scheduler import STRMScheduler
from app.notifier import TelegramSendResult
from app.modules.strm import finalize_changed_paths
from tests.support import IsolatedDatabaseTestCase


def _change(
    *,
    source_id: str = "source",
    rel_dir: str = "剧集/作品 A/Season 01",
    file_id: str = "f1",
    name: str = "A - S01E01.mkv",
    kind: str = "video",
    action: str = "upsert",
) -> dict:
    return {
        "source_id": source_id,
        "rel_dir": rel_dir,
        "file_id": file_id,
        "name": name,
        "kind": kind,
        "action": action,
    }


class StrmChangeQueueStateTests(IsolatedDatabaseTestCase):
    """Task 2.1：状态转换、幂等合并与 dirty 语义。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_change_queue")

    @staticmethod
    def _rows() -> list[dict]:
        return [dict(row) for row in db.list_strm_change_queue()]

    def test_changes_are_grouped_per_source_and_target_directory(self):
        written = db.enqueue_strm_change_targets([
            _change(rel_dir="剧集/A/Season 01", file_id="f1"),
            _change(rel_dir="剧集/A/Season 01", file_id="f2"),
            _change(rel_dir="电影/B", file_id="f3"),
            _change(source_id="other", rel_dir="剧集/A/Season 01", file_id="f4"),
        ])

        self.assertEqual(written, 3)
        rows = {(row["source_id"], row["rel_dir"]) for row in self._rows()}
        self.assertEqual(rows, {
            ("source", "剧集/A/Season 01"),
            ("source", "电影/B"),
            ("other", "剧集/A/Season 01"),
        })

    def test_repeated_enqueue_merges_idempotently(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        db.enqueue_strm_change_targets([_change(file_id="f1"), _change(file_id="f2")])

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        claimed = db.claim_strm_change_targets(owner="test")
        self.assertEqual(
            sorted(item["file_id"] for item in claimed[0]["changes"]), ["f1", "f2"]
        )

    def test_change_during_running_becomes_dirty_without_losing_events(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        db.claim_strm_change_targets(owner="test")

        db.enqueue_strm_change_targets([_change(file_id="f2")])

        row = self._rows()[0]
        self.assertEqual(row["state"], "dirty")
        self.assertEqual(row["dirty"], 1)
        # 本轮 inflight 与新到达的 pending 必须同时保留。
        self.assertIn("f1", str(row["inflight_changes_json"]))
        self.assertIn("f2", str(row["pending_changes_json"]))

    def test_completing_a_dirty_target_requeues_the_new_changes(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="test")
        db.enqueue_strm_change_targets([_change(file_id="f2")])

        state = db.complete_strm_change_target(
            claimed[0]["id"],
            expected_owner=claimed[0]["lease_owner"],
            expected_lease_generation=claimed[0]["lease_generation"],
        )

        self.assertEqual(state, "queued")
        replay = db.claim_strm_change_targets(owner="test")
        self.assertEqual([item["file_id"] for item in replay[0]["changes"]], ["f2"])

    def test_completing_a_clean_target_marks_completed(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="test")

        state = db.complete_strm_change_target(
            claimed[0]["id"],
            expected_owner=claimed[0]["lease_owner"],
            expected_lease_generation=claimed[0]["lease_generation"],
        )

        self.assertEqual(state, "completed")
        self.assertEqual(self._rows()[0]["version"], 1)
        self.assertEqual(db.count_pending_strm_change_targets(), 0)

    def test_new_change_after_completion_reopens_the_target(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="test")
        db.complete_strm_change_target(
            claimed[0]["id"],
            expected_owner=claimed[0]["lease_owner"],
            expected_lease_generation=claimed[0]["lease_generation"],
        )

        db.enqueue_strm_change_targets([_change(file_id="f2")])

        self.assertEqual(self._rows()[0]["state"], "queued")
        self.assertEqual(db.count_pending_strm_change_targets(), 1)

    def test_invalid_or_sourceless_changes_are_ignored(self):
        written = db.enqueue_strm_change_targets([
            "not-a-dict",
            {"rel_dir": "剧集/A", "file_id": "f1"},
        ])

        self.assertEqual(written, 0)
        self.assertEqual(self._rows(), [])


class StrmChangeQueueWorkerTests(IsolatedDatabaseTestCase):
    """Task 2.2：租约领取、崩溃恢复与有界退避重试。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_change_queue")

    @staticmethod
    def _row() -> dict:
        return dict(db.list_strm_change_queue()[0])

    def test_claim_moves_pending_into_inflight_and_holds_a_lease(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])

        claimed = db.claim_strm_change_targets(owner="worker-a", lease_seconds=60)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["source_id"], "source")
        self.assertEqual(claimed[0]["rel_dir"], "剧集/作品 A/Season 01")
        row = self._row()
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["lease_owner"], "worker-a")
        self.assertGreater(float(row["lease_until"]), time.time())
        self.assertEqual(str(row["pending_changes_json"]), "[]")

    def test_second_worker_cannot_claim_a_leased_target(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        db.claim_strm_change_targets(owner="worker-a", lease_seconds=600)

        self.assertEqual(db.claim_strm_change_targets(owner="worker-b"), [])

    def test_expired_lease_is_reclaimable_after_a_crash(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        db.claim_strm_change_targets(owner="worker-a", lease_seconds=1)
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_change_queue SET lease_until=0")

        reclaimed = db.claim_strm_change_targets(owner="worker-b")

        self.assertEqual(len(reclaimed), 1)
        self.assertEqual([item["file_id"] for item in reclaimed[0]["changes"]], ["f1"])

    def test_stale_worker_cannot_settle_a_reclaimed_target(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        first = db.claim_strm_change_targets(owner="worker-a", lease_seconds=1)[0]
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_change_queue SET lease_until=0 WHERE id=?", (first["id"],))
        second = db.claim_strm_change_targets(owner="worker-b", lease_seconds=60)[0]

        self.assertEqual(
            db.complete_strm_change_target(
                first["id"],
                expected_owner=first["lease_owner"],
                expected_lease_generation=first["lease_generation"],
            ),
            "stale",
        )
        self.assertEqual(
            db.fail_strm_change_target(
                first["id"],
                expected_owner=first["lease_owner"],
                expected_lease_generation=first["lease_generation"],
                error="late",
            ),
            "stale",
        )
        self.assertEqual(db.release_strm_change_targets([first], reason="late"), 0)
        row = self._row()
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["lease_owner"], "worker-b")
        self.assertEqual(row["lease_generation"], second["lease_generation"])

    def test_heartbeat_renews_current_lease_and_rejects_stale_generation(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="worker-a", lease_seconds=30)
        before = float(self._row()["lease_until"])

        renewed = db.renew_strm_change_target_leases(
            claimed, owner="worker-a", lease_seconds=120
        )
        stale = dict(claimed[0])
        stale["lease_generation"] = int(stale["lease_generation"]) - 1

        self.assertEqual(renewed, 1)
        self.assertGreater(float(self._row()["lease_until"]), before)
        self.assertEqual(
            db.renew_strm_change_target_leases([stale], owner="worker-a", lease_seconds=120),
            0,
        )
        self.assertEqual(db.claim_strm_change_targets(owner="worker-b"), [])

    def test_concurrent_claimers_only_receive_target_once(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        barrier = threading.Barrier(2)
        claims: list[list[dict]] = []
        errors: list[BaseException] = []

        def worker(owner: str) -> None:
            try:
                barrier.wait(timeout=2)
                claims.append(db.claim_strm_change_targets(owner=owner, lease_seconds=60))
            except BaseException as exc:  # pragma: no cover - diagnostic safety
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("worker-a",)),
            threading.Thread(target=worker, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(sum(len(batch) for batch in claims), 1)
        self.assertEqual(self._row()["state"], "running")

    def test_startup_recovery_requeues_interrupted_targets(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        db.claim_strm_change_targets(owner="worker-a", lease_seconds=1)
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_change_queue SET lease_until=0")

        recovered = db.recover_stale_strm_change_targets()

        self.assertEqual(recovered, 1)
        row = self._row()
        self.assertEqual(row["state"], "queued")
        self.assertIn("f1", str(row["pending_changes_json"]))
        self.assertEqual(db.count_pending_strm_change_targets(), 1)

    def test_failure_returns_inflight_changes_and_backs_off(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="worker-a")

        state = db.fail_strm_change_target(
            claimed[0]["id"],
            expected_owner=claimed[0]["lease_owner"],
            expected_lease_generation=claimed[0]["lease_generation"],
            error="provider timeout", backoff_seconds=60,
        )

        self.assertEqual(state, "queued")
        row = self._row()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "provider timeout")
        self.assertIn("f1", str(row["pending_changes_json"]))
        # 退避窗口未到之前不得再次领取。
        self.assertEqual(db.claim_strm_change_targets(owner="worker-b"), [])

    def test_exhausted_retries_stay_failed_for_manual_recovery(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        for _attempt in range(3):
            with db.get_conn() as conn:
                conn.execute("UPDATE strm_change_queue SET next_attempt_at='2000-01-01 00:00:00'")
            claimed = db.claim_strm_change_targets(owner="worker-a")
            state = db.fail_strm_change_target(
                claimed[0]["id"],
                expected_owner=claimed[0]["lease_owner"],
                expected_lease_generation=claimed[0]["lease_generation"],
                error="boom", max_attempts=3,
            )

        self.assertEqual(state, "failed")
        row = self._row()
        self.assertEqual(row["attempts"], 3)
        self.assertIn("f1", str(row["pending_changes_json"]))

    def test_stop_releases_claimed_targets_without_losing_changes(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = db.claim_strm_change_targets(owner="worker-a")

        released = db.release_strm_change_targets(
            claimed, reason="服务停止",
        )

        self.assertEqual(released, 1)
        row = self._row()
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertIn("f1", str(row["pending_changes_json"]))

    def test_pending_count_tracks_active_states_only(self):
        db.enqueue_strm_change_targets([
            _change(rel_dir="a", file_id="f1"),
            _change(rel_dir="b", file_id="f2"),
        ])
        claimed = db.claim_strm_change_targets(owner="worker-a")
        db.complete_strm_change_target(
            claimed[0]["id"],
            expected_owner=claimed[0]["lease_owner"],
            expected_lease_generation=claimed[0]["lease_generation"],
        )

        self.assertEqual(db.count_pending_strm_change_targets(), 1)


class SchedulerChangeQueueIntegrationTests(IsolatedDatabaseTestCase):
    """Task 2.2：调度器与持久队列的结算契约。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_change_queue")
        self.scheduler = STRMScheduler()

    def test_trigger_persists_changes_before_running(self):
        self.scheduler._persist_change_targets([_change(file_id="f1")])

        self.assertEqual(db.count_pending_strm_change_targets(), 1)

    def test_trigger_fails_closed_when_change_persistence_fails(self):
        with patch(
            "app.modules.scheduler.db.enqueue_strm_change_targets",
            side_effect=RuntimeError("db down"),
        ):
            result = self.scheduler.trigger(
                "organize", organize_changes=[_change(file_id="f1")]
            )

        self.assertFalse(result["ok"])
        self.assertIn("持久化失败", result["error"])
        self.assertTrue(self.scheduler._run_lock.acquire(blocking=False))
        self.scheduler._run_lock.release()

    def test_only_organize_runs_claim_persisted_targets(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])

        self.assertEqual(self.scheduler._claim_change_targets("cron"), [])
        self.assertEqual(len(self.scheduler._claim_change_targets("organize")), 1)

    def test_successful_run_completes_claimed_targets(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = self.scheduler._claim_change_targets("organize")

        self.scheduler._settle_change_targets(claimed, "completed")

        self.assertEqual(db.count_pending_strm_change_targets(), 0)

    def test_partial_run_keeps_targets_for_retry(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = self.scheduler._claim_change_targets("organize")

        self.scheduler._settle_change_targets(
            claimed, "failed", error="STRM 同步部分完成，等待重试",
        )

        row = dict(db.list_strm_change_queue()[0])
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("f1", str(row["pending_changes_json"]))

    def test_stopped_run_releases_targets(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = self.scheduler._claim_change_targets("organize")

        self.scheduler._settle_change_targets(claimed, "stopped")

        row = dict(db.list_strm_change_queue()[0])
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)

    def test_dirty_target_triggers_another_organize_run(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = self.scheduler._claim_change_targets("organize")
        db.enqueue_strm_change_targets([_change(file_id="f2")])

        with patch.object(STRMScheduler, "trigger") as trigger:
            self.scheduler._settle_change_targets(claimed, "completed")

        trigger.assert_called_once_with("organize")
        self.assertEqual(db.count_pending_strm_change_targets(), 1)

    def test_clean_completion_does_not_retrigger(self):
        db.enqueue_strm_change_targets([_change(file_id="f1")])
        claimed = self.scheduler._claim_change_targets("organize")

        with patch.object(STRMScheduler, "trigger") as trigger:
            self.scheduler._settle_change_targets(claimed, "completed")

        trigger.assert_not_called()

    def test_claim_limit_is_automatically_drained_by_a_followup_run(self):
        db.enqueue_strm_change_targets([
            _change(rel_dir=f"剧集/作品 {index}/Season 01", file_id=f"f{index}")
            for index in range(201)
        ])
        claimed = self.scheduler._claim_change_targets("organize")

        self.assertEqual(len(claimed), 200)
        self.assertEqual(db.count_pending_strm_change_targets(), 201)
        with patch.object(STRMScheduler, "trigger") as trigger:
            self.scheduler._settle_change_targets(claimed, "completed")

        trigger.assert_called_once_with("organize")
        self.assertEqual(db.count_pending_strm_change_targets(), 1)
        remainder = self.scheduler._claim_change_targets("organize")
        self.assertEqual(len(remainder), 1)
        self.assertEqual(remainder[0]["rel_dir"], "剧集/作品 200/Season 01")

    def test_due_count_excludes_targets_still_in_backoff(self):
        db.enqueue_strm_change_targets([
            _change(rel_dir="ready", file_id="f1"),
            _change(rel_dir="later", file_id="f2"),
        ])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_change_queue SET next_attempt_at='2999-01-01 00:00:00' "
                "WHERE rel_dir='later'"
            )

        self.assertEqual(db.count_pending_strm_change_targets(), 2)
        self.assertEqual(db.count_due_strm_change_targets(), 1)


class ChangedPathProjectionTests(IsolatedDatabaseTestCase):
    """Task 2.3：变化目录结构化结果。"""

    def test_scheduler_loop_drains_due_notifications(self):
        """瞬时投递失败后，无需等待下一次整理即可由调度循环补发。"""
        from app.modules.organize_notification_outbox import (
            deliver_organize_notification,
        )
        from app.repositories.organize_notifications import (
            count_pending_organize_notifications,
        )

        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_notification_outbox")
        with patch(
            "app.notifier.send_result",
            return_value=TelegramSendResult(ok=False, error="timeout"),
        ):
            deliver_organize_notification("task:loop", "整理完成", chat_id="1")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_notification_outbox SET next_attempt_at='2000-01-01 00:00:00'"
            )

        sent: list[str] = []
        with patch(
            "app.notifier.send_result",
            side_effect=lambda text, chat_id=None: sent.append(text) or TelegramSendResult(ok=True),
        ):
            STRMScheduler._drain_notification_outbox()

        self.assertEqual(sent, ["整理完成"])
        self.assertEqual(count_pending_organize_notifications(), 0)

    def test_changed_paths_dedupe_and_expose_parent_directories(self):
        stats = {
            "changed_strm_paths": [
                "/data/strm/光鸭云盘/剧集/A/Season 01/E01.strm",
                "/data/strm/光鸭云盘/剧集/A/Season 01/E02.strm",
                "/data/strm/光鸭云盘/剧集/A/Season 01/E01.strm",
                "/data/strm/光鸭云盘/电影/B/B.strm",
            ]
        }

        finalize_changed_paths(stats)

        self.assertEqual(len(stats["changed_strm_paths"]), 3)
        self.assertEqual(stats["changed_dirs"], [
            "/data/strm/光鸭云盘/剧集/A/Season 01",
            "/data/strm/光鸭云盘/电影/B",
        ])

    def test_empty_change_set_produces_empty_projection(self):
        stats: dict = {}

        finalize_changed_paths(stats)

        self.assertEqual(stats["changed_strm_paths"], [])
        self.assertEqual(stats["changed_dirs"], [])

    def test_source_stats_merge_unions_changed_paths(self):
        aggregate = STRMScheduler._empty_stats()

        STRMScheduler._merge_source_stats(aggregate, {
            "changed_strm_paths": ["/root/光鸭云盘/A/E01.strm"],
        }, "源 A")
        STRMScheduler._merge_source_stats(aggregate, {
            "changed_strm_paths": ["/root/光鸭云盘/A/E01.strm", "/root/光鸭云盘/B/E01.strm"],
        }, "源 B")

        self.assertEqual(aggregate["changed_strm_paths"], [
            "/root/光鸭云盘/A/E01.strm", "/root/光鸭云盘/B/E01.strm",
        ])
        self.assertEqual(aggregate["changed_dirs"], [
            "/root/光鸭云盘/A", "/root/光鸭云盘/B",
        ])


if __name__ == "__main__":
    import unittest

    unittest.main()
