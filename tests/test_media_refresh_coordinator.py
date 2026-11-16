from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

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
            first["group_key"],
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
            queued["group_key"],
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

    def test_completed_targets_enter_persistent_dedupe_window(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
        settle_media_refresh(
            queued["group_key"],
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

    def test_settlement_keeps_only_deferred_and_new_paths_after_restart(self):
        for deferred in ([], ["/media/B"]):
            with self.subTest(deferred=deferred):
                clear_media_refresh_queue()
                queued = enqueue_media_refresh(
                    "jellyfin", ["/media/A", "/media/B"], debounce_seconds=0, now_epoch=100,
                )
                first = claim_due_media_refreshes(owner="old", now_epoch=100)[0]
                # 同一路径在执行中又变化，结束旧请求不能吞掉新的 A。
                enqueue_media_refresh(
                    "jellyfin", ["/media/A", "/media/C"], debounce_seconds=20, now_epoch=101,
                )
                self.assertTrue(settle_media_refresh(
                    queued["group_key"], owner="old", lease_generation=first["lease_generation"],
                    deferred_paths=deferred, refreshed_target_ids=["series-a"],
                    delay_seconds=90, now_epoch=102,
                ))
                stored = json.loads(db.kv_get("media_refresh_queue:v1"))
                self.assertEqual(stored["version"], 1)
                self.assertEqual(stored["groups"][queued["group_key"]]["inflight_paths"], [])
                self.assertEqual(stored["groups"][queued["group_key"]]["due_at"], 121)
                db.init_db()
                self.assertEqual(claim_due_media_refreshes(owner="new", now_epoch=120), [])
                next_group = claim_due_media_refreshes(owner="new", now_epoch=121)[0]
                self.assertEqual(next_group["paths"], deferred + ["/media/A", "/media/C"])
                self.assertEqual(recent_media_refresh_target_ids("jellyfin", now_epoch=121), ("series-a",))
                self.assertEqual(recover_media_refresh_leases(now_epoch=122), 1)
                recovered = claim_due_media_refreshes(owner="restarted", now_epoch=122)[0]
                self.assertEqual(recovered["paths"], next_group["paths"])

    def test_settlement_is_idempotent_and_delays_only_deduplicated_paths(self):
        for deferred in ([], ["/media/A"]):
            with self.subTest(deferred=deferred):
                clear_media_refresh_queue()
                queued = enqueue_media_refresh(
                    "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
                )
                lease = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]
                arguments = dict(
                    owner="worker", lease_generation=lease["lease_generation"],
                    deferred_paths=deferred, delay_seconds=90,
                    refreshed_target_ids=["series-a"], now_epoch=101,
                )
                self.assertTrue(settle_media_refresh(queued["group_key"], **arguments))
                snapshot = db.kv_get("media_refresh_queue:v1")
                self.assertFalse(settle_media_refresh(queued["group_key"], **arguments))
                self.assertEqual(db.kv_get("media_refresh_queue:v1"), snapshot)
                self.assertEqual(recent_media_refresh_target_ids("jellyfin", now_epoch=102), ("series-a",))
                self.assertEqual(claim_due_media_refreshes(owner="next", now_epoch=190), [])
                claimed = claim_due_media_refreshes(owner="next", now_epoch=191)
                self.assertEqual([path for group in claimed for path in group["paths"]], deferred)

    def test_superseded_lease_and_foreign_deferred_path_cannot_change_queue(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        old = claim_due_media_refreshes(owner="worker", lease_seconds=30, now_epoch=100)[0]
        new = claim_due_media_refreshes(owner="worker", now_epoch=131)[0]
        snapshot = db.kv_get("media_refresh_queue:v1")
        self.assertFalse(settle_media_refresh(
            queued["group_key"], owner="worker", lease_generation=old["lease_generation"],
            refreshed_target_ids=["must-not-be-recorded"], now_epoch=132,
        ))
        self.assertEqual(db.kv_get("media_refresh_queue:v1"), snapshot)
        with self.assertRaises(ValueError):
            settle_media_refresh(
                queued["group_key"], owner="worker", lease_generation=new["lease_generation"],
                deferred_paths=["/media/foreign"], refreshed_target_ids=["must-not-be-recorded"], now_epoch=132,
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
            skip_item_ids=(),
        )
        self.assertTrue(queued["group_key"])

    def test_server_failure_ends_attempt_and_records_reason(self):
        for response in ({"ok": False, "retryable": True, "fallback": "HTTP 503"}, OSError("连接超时")):
            with self.subTest(response=response):
                clear_media_refresh_queue()
                enqueue_media_refresh("jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100)
                coordinator = MediaRefreshCoordinator()
                claimed = claim_due_media_refreshes(owner=coordinator._owner, now_epoch=100)[0]
                client = Mock(display_name="Jellyfin")
                if isinstance(response, Exception):
                    client.refresh_for_paths.side_effect = response
                    reason = str(response)
                else:
                    client.refresh_for_paths.return_value = response
                    reason = response["fallback"]
                with patch.object(coordinator, "_client_for", return_value=client), self.assertLogs(
                    "app.modules.media_refresh_coordinator", level="WARNING",
                ) as logs:
                    coordinator._process_group(claimed)
                self.assertEqual(media_refresh_queue_status()["paths"], 0)
                self.assertEqual(claim_due_media_refreshes(owner="new", now_epoch=10000), [])
                self.assertIn(reason, logs.output[0])
                self.assertIn("手动刷新", logs.output[0])
                self.assertEqual(coordinator._failed_session, 1)
                client.close.assert_called_once()

    def test_database_failure_does_not_consume_unsettled_work(self):
        for operation in ("recent_media_refresh_target_ids", "settle_media_refresh"):
            with self.subTest(operation=operation):
                clear_media_refresh_queue()
                enqueue_media_refresh("jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100)
                coordinator = MediaRefreshCoordinator()
                claimed = claim_due_media_refreshes(owner=coordinator._owner, now_epoch=100)[0]
                client = Mock(display_name="Jellyfin")
                client.refresh_for_paths.return_value = {"ok": True}
                with patch.object(coordinator, "_client_for", return_value=client), patch(
                    f"app.modules.media_refresh_coordinator.{operation}",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ), self.assertRaises(sqlite3.OperationalError):
                    coordinator._process_group(claimed)
                self.assertEqual(media_refresh_queue_status(now_epoch=101)["running"], 1)
                self.assertEqual(media_refresh_queue_status(now_epoch=101)["paths"], 1)
                self.assertEqual(recover_media_refresh_leases(now_epoch=102), 1)
                recovered = claim_due_media_refreshes(owner="next", now_epoch=102)[0]
                self.assertEqual(recovered["paths"], ["/media/A"])
                self.assertEqual(client.refresh_for_paths.call_count, int(operation == "settle_media_refresh"))

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
            "deferred_paths": ["/media/new-episode"],
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
        self.assertEqual(status["queued"], 1)
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
                    "deferred_paths": ["/media/B"] if deferred else [],
                    "succeeded_target_ids": ["series-a"], "succeeded_paths": ["/media/A"],
                    "fallback": "等待去重窗口" if deferred else "1 个刷新请求失败",
                }
                coordinator = MediaRefreshCoordinator()
                coordinator._owner = "old"
                with patch.object(coordinator, "_client_for", return_value=client), patch(
                    "app.modules.media_refresh_coordinator._recent_ttl_seconds", return_value=90,
                ), patch("app.repositories.media_refresh_queue.time.time", return_value=100):
                    coordinator._process_group(first)

                self.assertEqual(media_refresh_queue_status(now_epoch=101)["paths"], int(deferred))
                if not deferred:
                    self.assertEqual(claim_due_media_refreshes(owner="new", now_epoch=1060), [])
                    # 新的用户变化才产生下一次请求，失败不会自动重放。
                    enqueue_media_refresh("jellyfin", ["/media/B"], debounce_seconds=0, now_epoch=1060)
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

    def test_mixed_batch_discards_failed_paths_and_keeps_only_known_dedupe(self):
        with tempfile.TemporaryDirectory() as root:
            deleted = f"{root}/光鸭云盘/old.strm"
            unmapped = f"{root}/光鸭云盘/整理/综艺/new.strm"
            Path(unmapped).parent.mkdir(parents=True)
            Path(unmapped).write_text("unchanged")
            paths = ["/media/accepted", "/media/deduped", "/media/failed", deleted, unmapped, "/server-only/media"]
            coordinator = MediaRefreshCoordinator()
            enqueue_media_refresh("jellyfin", paths, debounce_seconds=0, now_epoch=100)
            claimed = claim_due_media_refreshes(owner=coordinator._owner, now_epoch=100)[0]
            client = Mock(display_name="Jellyfin")
            client.refresh_for_paths.return_value = {
                "ok": False, "retryable": True, "deduplicated": 1,
                "deferred_paths": ["/media/deduped"], "succeeded_paths": ["/media/accepted"],
                "succeeded_target_ids": ["accepted"], "unmatched_paths": paths[3:],
                "fallback": "1 个刷新请求失败，3 个路径未匹配",
            }
            with patch.object(coordinator, "_client_for", return_value=client), patch(
                "app.repositories.media_refresh_queue.time.time", return_value=100,
            ), patch("app.modules.media_refresh_coordinator._recent_ttl_seconds", return_value=90), self.assertLogs(
                "app.modules.media_refresh_coordinator", level="WARNING",
            ) as logs:
                coordinator._process_group(claimed)
            self.assertIn("手动刷新", logs.output[0])
            self.assertEqual(coordinator._failed_session, 1)
            self.assertEqual(claim_due_media_refreshes(owner="next", now_epoch=189), [])
            pending = claim_due_media_refreshes(owner="next", now_epoch=190)[0]
            self.assertEqual(pending["paths"], ["/media/deduped"])
            self.assertEqual(Path(unmapped).read_text(), "unchanged")
            self.assertFalse(Path(deleted).exists())

    def test_disabled_provider_ends_refresh(self):
        enqueue_media_refresh("jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100)
        coordinator = MediaRefreshCoordinator()
        claimed = claim_due_media_refreshes(owner=coordinator._owner, now_epoch=100)[0]
        with patch.object(coordinator, "_client_for", return_value=None), self.assertLogs(
            "app.modules.media_refresh_coordinator", level="WARNING",
        ) as logs:
            coordinator._process_group(claimed)
        self.assertEqual(media_refresh_queue_status()["paths"], 0)
        self.assertIn("已停用或配置不完整", logs.output[0])
        self.assertEqual(coordinator._failed_session, 1)
