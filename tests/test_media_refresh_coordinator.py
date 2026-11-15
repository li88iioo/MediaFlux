from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from app import config
from app import database as db
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    clear_media_refresh_queue,
    enqueue_media_refresh,
    media_refresh_queue_status,
    recent_media_refresh_target_ids,
    recover_media_refresh_leases,
    settle_media_refresh,
)
from tests.support import IsolatedDatabaseTestCase


class MediaRefreshQueueTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        clear_media_refresh_queue()

    def test_quiet_window_merges_paths_and_moves_due_time(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=20, now_epoch=100,
        )
        enqueue_media_refresh(
            "jellyfin", ["/media/B", "/media/A"],
            debounce_seconds=20, now_epoch=110,
        )

        self.assertEqual(
            claim_due_media_refreshes(owner="worker", now_epoch=129), [],
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=130)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["paths"], ["/media/A", "/media/B"])

    def test_enqueue_during_running_survives_completion(self):
        first = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
        enqueue_media_refresh(
            "jellyfin", ["/media/B"], debounce_seconds=20, now_epoch=101,
        )

        self.assertTrue(settle_media_refresh(
            first["group_key"], outcome="completed",
            owner="worker",
            lease_generation=claimed["lease_generation"],
            now_epoch=102,
        ))
        self.assertEqual(media_refresh_queue_status(now_epoch=102)["queued"], 1)
        self.assertEqual(
            claim_due_media_refreshes(owner="worker", now_epoch=120), [],
        )
        second = claim_due_media_refreshes(owner="worker", now_epoch=121)[0]
        self.assertEqual(second["paths"], ["/media/B"])

    def test_producer_never_recovers_a_slow_running_consumer(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(
            owner="worker", lease_seconds=30, now_epoch=100,
        )[0]

        enqueue_media_refresh(
            "jellyfin", ["/media/B"], debounce_seconds=20, now_epoch=131,
        )

        self.assertTrue(settle_media_refresh(
            queued["group_key"], outcome="completed",
            owner="worker",
            lease_generation=claimed["lease_generation"],
            now_epoch=132,
        ))
        self.assertEqual(
            claim_due_media_refreshes(owner="worker", now_epoch=150), [],
        )
        next_group = claim_due_media_refreshes(owner="worker", now_epoch=151)[0]
        self.assertEqual(next_group["paths"], ["/media/B"])

    def test_expired_running_lease_requeues_inflight_paths(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        first = claim_due_media_refreshes(
            owner="old", lease_seconds=30, now_epoch=100,
        )[0]

        recovered = claim_due_media_refreshes(owner="new", now_epoch=131)[0]

        self.assertEqual(recovered["paths"], ["/media/A"])
        self.assertGreater(
            recovered["lease_generation"], first["lease_generation"],
        )

    def test_exclusive_consumer_can_recover_unexpired_previous_lease(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claim_due_media_refreshes(
            owner="old", lease_seconds=300, now_epoch=100,
        )

        self.assertEqual(recover_media_refresh_leases(now_epoch=101), 1)
        recovered = claim_due_media_refreshes(owner="new", now_epoch=101)[0]

        self.assertEqual(recovered["paths"], ["/media/A"])

    def test_partial_success_enters_dedupe_window_before_retry(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]

        self.assertTrue(settle_media_refresh(
            queued["group_key"], outcome="failed",
            owner="worker",
            lease_generation=claimed["lease_generation"],
            error="one endpoint failed",
            delay_seconds=30,
            refreshed_target_ids=("series-a",),
            recent_ttl_seconds=90,
            now_epoch=100,
        ))

        self.assertEqual(
            recent_media_refresh_target_ids("jellyfin", now_epoch=101),
            ("series-a",),
        )
        status = media_refresh_queue_status(now_epoch=101)
        self.assertEqual(status["retry_wait"], 1)
        self.assertEqual(status["paths"], 1)

    def test_completed_targets_enter_persistent_dedupe_window(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
        settle_media_refresh(
            queued["group_key"], outcome="completed",
            owner="worker",
            lease_generation=claimed["lease_generation"],
            refreshed_target_ids=("series-a",),
            recent_ttl_seconds=90,
            now_epoch=100,
        )

        self.assertEqual(
            recent_media_refresh_target_ids("jellyfin", now_epoch=189),
            ("series-a",),
        )
        self.assertEqual(
            recent_media_refresh_target_ids("jellyfin", now_epoch=190), (),
        )

    def test_partial_acknowledgement_preserves_new_changes_and_restart(self):
        for outcome in ("failed", "deferred"):
            with self.subTest(outcome=outcome):
                clear_media_refresh_queue()
                queued = enqueue_media_refresh(
                    "jellyfin", ["/media/A", "/media/B"], debounce_seconds=0, now_epoch=100,
                )
                first = claim_due_media_refreshes(owner="old", now_epoch=100)[0]
                # A 在领取之后又有了新变化；确认旧 A 不能吞掉新 A。
                enqueue_media_refresh(
                    "jellyfin", ["/media/A", "/media/C"], debounce_seconds=20, now_epoch=101,
                )
                self.assertTrue(settle_media_refresh(
                    queued["group_key"], owner="old", lease_generation=first["lease_generation"],
                    outcome=outcome, acknowledged_paths=["/media/A"],
                    refreshed_target_ids=["series-a"], delay_seconds=960, now_epoch=102,
                ))
                stored = json.loads(db.kv_get("media_refresh_queue:v1"))
                self.assertEqual(stored["version"], 1)
                self.assertEqual(stored["groups"][queued["group_key"]]["inflight_paths"], [])
                self.assertEqual(stored["groups"][queued["group_key"]]["due_at"], 121)
                db.init_db()
                self.assertEqual(claim_due_media_refreshes(owner="new", now_epoch=120), [])
                retry = claim_due_media_refreshes(owner="new", now_epoch=121)[0]
                self.assertEqual(retry["paths"], ["/media/B", "/media/A", "/media/C"])
                self.assertEqual(recent_media_refresh_target_ids("jellyfin", now_epoch=121), ("series-a",))
                self.assertEqual(recover_media_refresh_leases(now_epoch=122), 1)
                recovered = claim_due_media_refreshes(owner="restarted", now_epoch=122)[0]
                self.assertEqual(recovered["paths"], retry["paths"])

    def test_acknowledging_all_inflight_paths_leaves_no_empty_retry(self):
        for outcome in ("failed", "deferred"):
            with self.subTest(outcome=outcome):
                queued = enqueue_media_refresh(
                    "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
                )
                claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
                self.assertTrue(settle_media_refresh(
                    queued["group_key"], owner="worker", lease_generation=claimed["lease_generation"],
                    outcome=outcome, acknowledged_paths=["/media/A"], now_epoch=101,
                ))
                self.assertEqual(media_refresh_queue_status(now_epoch=102)["retry_wait"], 0)
                self.assertEqual(claim_due_media_refreshes(owner="worker", now_epoch=2000), [])

    def test_deferred_refresh_preserves_inflight_and_pending_paths(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
        enqueue_media_refresh(
            "jellyfin", ["/media/B"], debounce_seconds=20, now_epoch=101,
        )

        self.assertTrue(settle_media_refresh(
            queued["group_key"], outcome="deferred",
            owner="worker",
            lease_generation=claimed["lease_generation"],
            delay_seconds=90,
            error="deduplicated",
            refreshed_target_ids=("series-a",),
            recent_ttl_seconds=90,
            now_epoch=102,
        ))

        status = media_refresh_queue_status(now_epoch=102)
        self.assertEqual(status["retry_wait"], 1)
        self.assertEqual(status["paths"], 2)
        self.assertEqual(
            recent_media_refresh_target_ids("jellyfin", now_epoch=103),
            ("series-a",),
        )
        self.assertEqual(
            claim_due_media_refreshes(owner="worker", now_epoch=120), [],
        )
        retried = claim_due_media_refreshes(owner="worker", now_epoch=121)[0]
        self.assertEqual(retried["paths"], ["/media/A", "/media/B"])
        self.assertEqual(retried["attempts"], 0)

    def test_settlement_preserves_paths_attempts_and_due_time_for_each_outcome(self):
        for outcome in ("completed", "deferred", "failed"):
            for pending in (False, True):
                with self.subTest(outcome=outcome, pending=pending):
                    clear_media_refresh_queue()
                    queued = enqueue_media_refresh(
                        "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
                    )
                    first = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
                    settle_media_refresh(
                        queued["group_key"], owner="worker",
                        lease_generation=first["lease_generation"], outcome="failed",
                        error="第一次失败", delay_seconds=5, now_epoch=101,
                    )
                    lease = claim_due_media_refreshes(owner="worker", now_epoch=106)[0]
                    if pending:
                        enqueue_media_refresh(
                            "jellyfin", ["/media/B"], debounce_seconds=20, now_epoch=107,
                        )
                    arguments: dict = dict(
                        owner="worker", lease_generation=lease["lease_generation"],
                        outcome=outcome, delay_seconds=0, error=" fixture\n reason ",
                        refreshed_target_ids=["series-a"], now_epoch=108,
                    )
                    self.assertTrue(settle_media_refresh(queued["group_key"], **arguments))
                    snapshot = db.kv_get("media_refresh_queue:v1")
                    self.assertFalse(settle_media_refresh(queued["group_key"], **arguments))
                    self.assertEqual(db.kv_get("media_refresh_queue:v1"), snapshot)
                    self.assertEqual(
                        recent_media_refresh_target_ids("jellyfin", now_epoch=109),
                        ("series-a",),
                    )
                    groups = json.loads(snapshot)["groups"]
                    if outcome == "completed" and not pending:
                        self.assertEqual(groups, {})
                        continue
                    group = groups[queued["group_key"]]
                    self.assertEqual(group["lease_owner"], "")
                    self.assertEqual(group["inflight_paths"], [])
                    self.assertEqual(group["status"], "queued" if outcome == "completed" else "retry_wait")
                    self.assertEqual(group["attempts"], {"completed": 0, "deferred": 1, "failed": 2}[outcome])
                    self.assertEqual(group["due_at"], 127 if pending else {"deferred": 109, "failed": 113}[outcome])
                    expected = [] if outcome == "completed" else ["/media/A"]
                    self.assertEqual(group["pending_paths"], expected + (["/media/B"] if pending else []))
                    self.assertEqual(group["last_error"], "" if outcome == "completed" else "fixture reason")

    def test_every_settlement_rejects_superseded_lease_without_recording_success(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        old = claim_due_media_refreshes(owner="worker", lease_seconds=30, now_epoch=100)[0]
        claim_due_media_refreshes(owner="worker", now_epoch=131)
        snapshot = db.kv_get("media_refresh_queue:v1")
        for outcome in ("completed", "deferred", "failed"):
            with self.subTest(outcome=outcome):
                self.assertFalse(settle_media_refresh(
                    queued["group_key"], owner="worker",
                    lease_generation=old["lease_generation"], outcome=outcome,
                    refreshed_target_ids=["must-not-be-recorded"], now_epoch=132,
                ))
                self.assertEqual(db.kv_get("media_refresh_queue:v1"), snapshot)
        self.assertRaises(
                ValueError, settle_media_refresh, queued["group_key"], owner="worker",
                lease_generation=old["lease_generation"] + 1, outcome="invalid",
            )
        self.assertEqual(db.kv_get("media_refresh_queue:v1"), snapshot)


class MediaRefreshCoordinatorTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        clear_media_refresh_queue()

    def test_transient_startup_recovery_failure_retries_without_losing_refresh(self):
        enqueue_media_refresh("jellyfin", ["/media/A.strm"], debounce_seconds=0)
        worker = MediaRefreshCoordinator()
        wait = worker._wake_event.wait
        attempts = 0
        errors: list[threading.ExceptHookArgs] = []

        def recover():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return recover_media_refresh_leases()

        def refresh(*_args, **_kwargs):
            self.assertEqual(attempts, 2)
            worker._stop_event.set()
            return {"ok": True, "scope": "item", "requested": 1, "succeeded_target_ids": ["film"]}

        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.side_effect = refresh
        with patch("threading.excepthook", side_effect=errors.append), patch(
            "app.modules.media_refresh_coordinator.recover_media_refresh_leases", side_effect=recover,
        ), patch.object(worker, "_client_for", return_value=client), patch.object(
            worker._wake_event, "wait", side_effect=lambda timeout: wait(min(timeout, 0.01)),
        ):
            worker.start()
            assert worker._thread is not None
            worker._thread.join(timeout=2.0)
            self.assertTrue(worker.stop(timeout=1.0))

        self.assertEqual(errors, [])
        client.refresh_for_paths.assert_called_once()
        self.assertEqual(media_refresh_queue_status()["paths"], 0)
        self.assertEqual(worker.status()["completed_session"], 1)
        self.assertEqual(worker.status()["last_error_type"], "")
        self.assertTrue(worker._worker_lock.acquire(blocking=False))
        worker._worker_lock.release()

    def test_stop_while_startup_recovery_unavailable_keeps_refresh_queued(self):
        enqueue_media_refresh("jellyfin", ["/media/A.strm"], debounce_seconds=0)
        worker = MediaRefreshCoordinator()
        attempted = threading.Event()
        errors: list[threading.ExceptHookArgs] = []

        def unavailable():
            attempted.set()
            raise sqlite3.OperationalError("database is locked")

        with patch("threading.excepthook", side_effect=errors.append), patch(
            "app.modules.media_refresh_coordinator.recover_media_refresh_leases", side_effect=unavailable,
        ), patch.object(worker, "_client_for") as client:
            worker.start()
            self.assertTrue(attempted.wait(timeout=2.0))
            self.assertTrue(worker.stop(timeout=1.0))

        self.assertEqual(errors, [])
        client.assert_not_called()
        self.assertEqual(media_refresh_queue_status()["paths"], 1)
        self.assertFalse(worker.status()["consumer_active"])
        self.assertEqual(worker.status()["last_error_type"], "OperationalError")
        self.assertTrue(worker._worker_lock.acquire(blocking=False))
        worker._worker_lock.release()

    def test_successful_group_is_acknowledged_and_deduped(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(
            owner="media-refresh-test", now_epoch=100,
        )[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {
            "ok": True,
            "scope": "item",
            "requested": 1,
            "items": ["series-a"],
            "folders": [],
            "libraries": [],
            "deduplicated": 0,
            "retryable": False,
            "succeeded_target_ids": ["series-a"],
            "fallback": "",
        }
        coordinator = MediaRefreshCoordinator()
        coordinator._owner = "media-refresh-test"

        with patch.object(coordinator, "_client_for", return_value=client), patch(
            "app.modules.media_refresh_coordinator._recent_ttl_seconds",
            return_value=90,
        ), patch("app.services.clear_dashboard_cache"):
            coordinator._process_group(claimed)

        self.assertEqual(media_refresh_queue_status()["paths"], 0)
        self.assertEqual(recent_media_refresh_target_ids("jellyfin"), ("series-a",))
        client.refresh_for_paths.assert_called_once_with(
            ["/media/A"],
            allowed_library_ids=(),
            allow_global_fallback=False,
            skip_item_ids=(),
        )
        self.assertTrue(queued["group_key"])

    def test_retryable_result_keeps_only_refresh_work_pending(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(
            owner="media-refresh-test", now_epoch=100,
        )[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {
            "ok": False,
            "scope": "skipped",
            "requested": 1,
            "items": [],
            "folders": [],
            "libraries": [],
            "deduplicated": 0,
            "retryable": True,
            "succeeded_target_ids": ["series-a"],
            "fallback": "temporary",
        }
        coordinator = MediaRefreshCoordinator()
        coordinator._owner = "media-refresh-test"

        with patch.object(coordinator, "_client_for", return_value=client), patch(
            "app.modules.media_refresh_coordinator._retry_seconds", return_value=30,
        ):
            coordinator._process_group(claimed)

        status = media_refresh_queue_status()
        self.assertEqual(status["retry_wait"], 1)
        self.assertEqual(status["paths"], 1)
        self.assertEqual(recent_media_refresh_target_ids("jellyfin"), ("series-a",))

    def test_deduplicated_target_is_deferred_instead_of_dropped(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/new-episode"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(
            owner="media-refresh-test", now_epoch=100,
        )[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {
            "ok": True,
            "scope": "deduplicated",
            "requested": 1,
            "items": [],
            "folders": [],
            "libraries": [],
            "deduplicated": 1,
            "retryable": False,
            "succeeded_target_ids": [],
            "fallback": "",
        }
        coordinator = MediaRefreshCoordinator()
        coordinator._owner = "media-refresh-test"

        with patch.object(coordinator, "_client_for", return_value=client), patch(
            "app.modules.media_refresh_coordinator._recent_ttl_seconds",
            return_value=90,
        ):
            coordinator._process_group(claimed)

        status = media_refresh_queue_status()
        self.assertEqual(status["retry_wait"], 1)
        self.assertEqual(status["paths"], 1)
        self.assertEqual(coordinator._completed_session, 0)
        self.assertEqual(coordinator._failed_session, 0)

    def test_partial_success_is_not_replayed_after_dedupe_expires(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                clear_media_refresh_queue()
                enqueue_media_refresh(
                    "jellyfin", ["/media/A", "/media/B"], debounce_seconds=0, now_epoch=100,
                )
                first = claim_due_media_refreshes(owner="old", now_epoch=100)[0]
                client = Mock(display_name="Jellyfin")
                client.refresh_for_paths.return_value = {
                    "ok": deferred, "scope": "item", "requested": 2,
                    "items": ["series-a"], "retryable": not deferred,
                    "deduplicated": int(deferred),
                    "succeeded_target_ids": ["series-a"], "succeeded_paths": ["/media/A"],
                    "fallback": "等待去重窗口" if deferred else "1 个刷新请求失败，将重试",
                }
                coordinator = MediaRefreshCoordinator()
                coordinator._owner = "old"
                with patch.object(coordinator, "_client_for", return_value=client), patch(
                    "app.modules.media_refresh_coordinator._retry_seconds", return_value=960,
                ), patch(
                    "app.modules.media_refresh_coordinator._recent_ttl_seconds", return_value=90,
                ), patch("app.repositories.media_refresh_queue.time.time", return_value=100):
                    coordinator._process_group(first)

                self.assertEqual(media_refresh_queue_status(now_epoch=101)["paths"], 1)
                # 换一个消费者并越过去重期，已接受的 A 不再靠临时去重防止重放。
                second = claim_due_media_refreshes(owner="new", now_epoch=1060)[0]
                coordinator = MediaRefreshCoordinator()
                coordinator._owner = "new"
                client.refresh_for_paths.return_value = {
                    "ok": True, "scope": "item", "requested": 1,
                    "succeeded_target_ids": ["series-b"], "succeeded_paths": ["/media/B"],
                }
                with patch.object(coordinator, "_client_for", return_value=client), patch(
                    "app.repositories.media_refresh_queue.time.time", return_value=1060,
                ), patch("app.services.clear_dashboard_cache"):
                    coordinator._process_group(second)
                self.assertEqual([call.args[0] for call in client.refresh_for_paths.call_args_list], [
                    ["/media/A", "/media/B"], ["/media/B"],
                ])
                self.assertEqual(client.refresh_for_paths.call_args.kwargs["skip_item_ids"], ())
                self.assertEqual(media_refresh_queue_status(now_epoch=1061)["paths"], 0)

    def _settle_unmatched(self, strm_root, outcome, *, paths, now=100):
        enqueue_media_refresh("jellyfin", paths, debounce_seconds=0, now_epoch=now)
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=now)[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {
            "ok": False, "requested": len(paths), "retryable": False, "deduplicated": 0,
            "succeeded_target_ids": [], "succeeded_paths": [], **outcome,
        }
        coordinator = MediaRefreshCoordinator()
        coordinator._owner = "worker"
        original_get = config.get
        with patch.object(coordinator, "_client_for", return_value=client), patch(
            "app.modules.media_refresh_coordinator._retry_seconds", return_value=960,
        ), patch(
            "app.modules.media_refresh_coordinator._recent_ttl_seconds", return_value=90,
        ), patch(
            "app.modules.media_refresh_coordinator.config.get",
            side_effect=lambda key, default="": strm_root if key == "STRM_ROOT" else original_get(key, default),
        ), patch("app.repositories.media_refresh_queue.time.time", return_value=now), patch(
            "app.services.clear_dashboard_cache",
        ):
            coordinator._process_group(claimed)
        groups = json.loads(db.kv_get("media_refresh_queue:v1"))["groups"]
        return coordinator, list(groups.values())

    def test_unmatched_paths_keep_refresh_intent_without_replaying_accepted_paths(self):
        # 文件不在本机不能证明 Jellyfin 中没有旧条目；缺映射的删除事件也必须保留。
        # 只确认上游已接受的路径，避免一次未匹配把同批已成功的刷新反复重放。
        with tempfile.TemporaryDirectory() as root:
            deleted = f"{root}/光鸭云盘/NSFW/old"
            unmapped = f"{root}/光鸭云盘/整理/综艺/节目/S01E01.strm"
            Path(unmapped).parent.mkdir(parents=True)
            Path(unmapped).write_text("x")
            server_only = "/server-only/剧集/作品/E01.strm"
            cases = {
                "已删除但未匹配的路径仍需清理媒体库旧条目": (
                    {"scope": "skipped", "unmatched_paths": [deleted]}, [deleted], [deleted], 1060,
                    "MediaRefreshUnmapped",
                ),
                "文件仍在的缺映射路径保留并单列": (
                    {"scope": "skipped", "unmatched_paths": [unmapped]}, [unmapped], [unmapped], 1060,
                    "MediaRefreshUnmapped",
                ),
                "无法证明已删除的服务端路径保留": (
                    {"scope": "skipped", "unmatched_paths": [server_only]}, [server_only], [server_only], 1060,
                    "MediaRefreshUnmapped",
                ),
                "正常刷新照常确认不被缺映射拖住": (
                    {"scope": "item", "succeeded_paths": ["/media/new"], "succeeded_target_ids": ["ep"],
                     "unmatched_paths": [deleted, unmapped]},
                    ["/media/new", deleted, unmapped], [deleted, unmapped], 1060, "MediaRefreshUnmapped",
                ),
                "去重等待不被拖成失败退避": (
                    {"scope": "deduplicated", "deduplicated": 1, "unmatched_paths": [deleted]},
                    ["/media/new", deleted], ["/media/new", deleted], 190, "",
                ),
                "可重试失败保留所有尚未被接受的路径": (
                    {"scope": "item", "retryable": True, "fallback": "1 个刷新请求失败，将重试",
                     "unmatched_paths": [deleted]},
                    ["/media/new", deleted], ["/media/new", deleted], 1060, "MediaRefreshRetryable",
                ),
            }
            for label, (outcome, paths, remaining, due_at, error_type) in cases.items():
                with self.subTest(label):
                    clear_media_refresh_queue()
                    coordinator, groups = self._settle_unmatched(root, outcome, paths=paths)
                    self.assertEqual([path for group in groups for path in group["pending_paths"]], remaining)
                    self.assertEqual([group["due_at"] for group in groups], [due_at] if due_at else [])
                    self.assertEqual(coordinator._last_error_type, error_type)

            # 补上映射后，保留的路径在下一轮被正常刷新并出队。
            clear_media_refresh_queue()
            self._settle_unmatched(root, {"scope": "skipped", "unmatched_paths": [unmapped]}, paths=[unmapped])
            _coordinator, groups = self._settle_unmatched(root, {
                "ok": True, "scope": "item", "succeeded_paths": [unmapped], "succeeded_target_ids": ["ep"],
            }, paths=[], now=1060)
            self.assertEqual(groups, [])

    def test_disabled_provider_keeps_refresh_pending(self):
        enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(
            owner="media-refresh-test", now_epoch=100,
        )[0]
        coordinator = MediaRefreshCoordinator()
        coordinator._owner = "media-refresh-test"

        with patch.object(coordinator, "_client_for", return_value=None), patch(
            "app.modules.media_refresh_coordinator._retry_seconds", return_value=30,
        ):
            coordinator._process_group(claimed)

        status = media_refresh_queue_status()
        self.assertEqual(status["retry_wait"], 1)
        self.assertEqual(status["paths"], 1)
        self.assertEqual(coordinator._failed_session, 1)
        self.assertEqual(coordinator._last_error_type, "MediaServerUnavailable")
