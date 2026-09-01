from __future__ import annotations

from unittest.mock import patch

from app.modules.scheduler import STRMScheduler
from tests.support import IsolatedDatabaseTestCase


class StrmFastFullModeTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.scheduler = STRMScheduler()
        self.source = {"id": "source", "name": "来源", "rel_prefix": ""}
        self.values = {
            "GY_STRM_BASE_URL": "http://media.invalid",
            "STRM_ROOT": "/tmp/mediaflux-fast-full",
        }

    def _patch_runtime(self):
        return (
            patch.object(self.scheduler, "validate_config", return_value=""),
            patch.object(self.scheduler, "_source_dirs", return_value=[self.source]),
            patch.object(self.scheduler, "_video_exts", return_value={"mkv"}),
            patch.object(self.scheduler, "_metadata_exts", return_value=set()),
            patch.object(self.scheduler, "_refresh_media_servers", return_value={}),
            patch.object(self.scheduler, "_notify_success"),
            patch.object(self.scheduler, "_notify_details"),
            patch(
                "app.modules.scheduler.get",
                side_effect=lambda key, default="": self.values.get(key, default),
            ),
            patch("app.modules.scheduler.get_int", return_value=0),
        )

    def test_fast_mode_with_empty_queue_is_explicit_noop(self):
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as refresh, \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler, "_run_incremental_sources"
                ) as incremental, patch.object(
                    self.scheduler, "_run_full_sources"
                ) as full:
            result = self.scheduler.run_blocking("telegram", sync_mode="fast")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "fast_noop")
        incremental.assert_not_called()
        full.assert_not_called()
        refresh.assert_called_once()
        self.assertFalse(refresh.call_args.kwargs["has_changes"])

    def test_fast_mode_claims_trusted_changes_without_full_scan(self):
        changes = [{
            "source_id": "source",
            "kind": "video",
            "action": "upsert",
            "file_id": "video-1",
            "name": "Episode.mkv",
        }]
        aggregate = self.scheduler._empty_stats()
        aggregate.update({"total": 1, "generated": 1})
        source_results = [{"id": "source", "name": "来源", "stats": aggregate}]
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler,
                    "_claim_change_targets",
                    return_value=[{"changes": changes}],
                ), patch.object(
                    self.scheduler,
                    "_run_incremental_sources",
                    return_value=(aggregate, source_results, False, ""),
                ) as incremental, patch.object(
                    self.scheduler, "_run_full_sources"
                ) as full:
            result = self.scheduler.run_blocking("telegram", sync_mode="fast")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "fast")
        incremental.assert_called_once()
        full.assert_not_called()

    def test_notification_failure_does_not_rewrite_completed_strm_run(self):
        aggregate = self.scheduler._empty_stats()
        aggregate.update({"total": 1, "generated": 1})
        source_results = [{"id": "source", "name": "来源", "stats": aggregate}]
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            self.scheduler, "_notify_success", side_effect=RuntimeError("outbox unavailable")
        ), patches[6], patches[7], patches[8], patch.object(
            self.scheduler, "_run_full_sources", return_value=(aggregate, source_results, False)
        ), patch.object(
            self.scheduler, "_settle_change_targets"
        ) as settle:
            result = self.scheduler.run_blocking("manual", sync_mode="full")

        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        settle.assert_called_once()
        self.assertEqual(settle.call_args.args[1], "completed")

    def test_fast_mode_never_auto_expands_to_full_cleanup_on_fallback(self):
        changes = [{
            "source_id": "source", "kind": "video", "action": "upsert",
            "file_id": "video-1", "name": "Episode.mkv",
        }]
        aggregate = self.scheduler._empty_stats()
        aggregate.update({"failed": 1})
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler,
                    "_claim_change_targets",
                    return_value=[{"changes": changes}],
                ), patch.object(
                    self.scheduler,
                    "_run_incremental_sources",
                    return_value=(aggregate, [], False, "远端快照变化"),
                ), patch.object(
                    self.scheduler, "_run_full_sources"
                ) as full:
            result = self.scheduler.run_blocking("telegram", sync_mode="fast")

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["mode"], "fast_partial")
        self.assertEqual(result["fallback_reason"], "远端快照变化")
        full.assert_not_called()

    def test_full_mode_always_runs_complete_calibration(self):
        aggregate = self.scheduler._empty_stats()
        source_results = [{"id": "source", "name": "来源", "stats": aggregate}]
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler,
                    "_run_full_sources",
                    return_value=(aggregate, source_results, False),
                ) as full, patch.object(
                    self.scheduler, "_run_incremental_sources"
                ) as incremental:
            result = self.scheduler.run_blocking("telegram", sync_mode="full")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "full")
        full.assert_called_once()
        incremental.assert_not_called()

    def test_cron_forces_full_even_if_fast_mode_is_requested(self):
        aggregate = self.scheduler._empty_stats()
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler,
                    "_run_full_sources",
                    return_value=(aggregate, [], False),
                ) as full, patch.object(
                    self.scheduler, "_run_incremental_sources"
                ) as incremental:
            result = self.scheduler.run_blocking("cron", sync_mode="fast")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "full")
        full.assert_called_once()
        incremental.assert_not_called()

    def test_empty_directory_maintenance_alone_does_not_refresh_media_library(self):
        aggregate = self.scheduler._empty_stats()
        aggregate["empty_dirs_cleaned"] = 3
        patches = self._patch_runtime()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as refresh, \
                patches[5], patches[6], patches[7], patches[8], patch.object(
                    self.scheduler,
                    "_run_full_sources",
                    return_value=(aggregate, [], False),
                ):
            result = self.scheduler.run_blocking("telegram", sync_mode="full")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["empty_dirs_cleaned"], 3)
        refresh.assert_called_once()
        self.assertFalse(refresh.call_args.kwargs["has_changes"])
