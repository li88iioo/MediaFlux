from __future__ import annotations

from unittest.mock import Mock, patch

from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    clear_media_refresh_queue,
    complete_media_refresh,
    enqueue_media_refresh,
    fail_media_refresh,
    media_refresh_queue_status,
    recent_media_refresh_target_ids,
    recover_media_refresh_leases,
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

        self.assertTrue(complete_media_refresh(
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

        self.assertTrue(complete_media_refresh(
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

    def test_partial_success_enters_dedupe_window_before_retry(self):
        queued = enqueue_media_refresh(
            "jellyfin", ["/media/A"], debounce_seconds=0, now_epoch=100,
        )
        claimed = claim_due_media_refreshes(owner="worker", now_epoch=100)[0]

        self.assertTrue(fail_media_refresh(
            queued["group_key"],
            owner="worker",
            lease_generation=claimed["lease_generation"],
            error="one endpoint failed",
            retry_seconds=30,
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
        complete_media_refresh(
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


class MediaRefreshCoordinatorTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        clear_media_refresh_queue()

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
