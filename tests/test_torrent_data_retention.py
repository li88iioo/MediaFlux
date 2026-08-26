from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from tests.support import IsolatedDatabaseTestCase


class TorrentDataRetentionRepositoryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")

    def _create_request(
        self,
        name: str,
        *,
        status: str,
        age_days: int,
        qb_status: str = "",
        gy_status: str = "",
    ) -> int:
        request_id, created = db.create_download_request(
            f"torrent-retention:{name}",
            "torrent",
            title=name,
            source_value=f"magnet:?xt=urn:btih:{name}",
            torrent_data=f"torrent:{name}".encode(),
        )
        self.assertTrue(created)
        modifier = f"-{max(0, int(age_days))} days"
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET status=?,qb_status=?,gy_status=?,"
                "updated_at=datetime('now','localtime',?),"
                "completed_at=datetime('now','localtime',?) WHERE id=?",
                (status, qb_status, gy_status, modifier, modifier, request_id),
            )
        return request_id

    def test_zero_retention_keeps_original_torrent_data(self):
        request_id = self._create_request(
            "permanent",
            status="completed",
            age_days=120,
            qb_status="completed",
        )

        self.assertEqual(db.purge_expired_download_request_torrent_data(0), 0)
        self.assertEqual(
            db.get_download_request(request_id)["torrent_data"],
            b"torrent:permanent",
        )

    def test_cleanup_only_clears_expired_safe_terminal_torrent_blobs(self):
        clearable = {
            self._create_request(
                "completed-old", status="completed", age_days=31, qb_status="completed"
            ),
            self._create_request(
                "failed-old", status="failed", age_days=31, qb_status="failed"
            ),
            self._create_request(
                "cancelled-old", status="cancelled", age_days=31
            ),
            self._create_request(
                "resubmitted-old",
                status="resubmitted",
                age_days=31,
                qb_status="completed",
                gy_status="resubmitted",
            ),
        }
        retained = {
            self._create_request(
                "completed-recent", status="completed", age_days=2, qb_status="completed"
            ),
            self._create_request(
                "manual-review-old",
                status="manual_review",
                age_days=31,
                gy_status="manual_review",
            ),
            self._create_request(
                "downloading-old",
                status="downloading",
                age_days=31,
                qb_status="downloading",
            ),
            self._create_request(
                "resubmitted-active",
                status="resubmitted",
                age_days=31,
                qb_status="downloading",
                gy_status="resubmitted",
            ),
        }
        audited_id = next(iter(clearable))
        db.add_download_log(
            "qb",
            title="保留日志",
            request_id=audited_id,
            status="success",
        )

        self.assertEqual(
            db.purge_expired_download_request_torrent_data(30, limit=50),
            len(clearable),
        )

        for request_id in clearable:
            row = db.get_download_request(request_id)
            self.assertIsNotNone(row)
            self.assertIsNone(row["torrent_data"])
            self.assertTrue(str(row["source_value"] or "").startswith("magnet:?"))
        for request_id in retained:
            self.assertIsNotNone(db.get_download_request(request_id)["torrent_data"])
        with db.get_conn() as conn:
            log_count = conn.execute(
                "SELECT COUNT(*) FROM download_log WHERE request_id=?",
                (audited_id,),
            ).fetchone()[0]
        self.assertEqual(log_count, 1)

    def test_cleanup_respects_batch_limit(self):
        request_ids = [
            self._create_request(
                f"batch-{index}",
                status="completed",
                age_days=60,
                qb_status="completed",
            )
            for index in range(3)
        ]

        self.assertEqual(
            db.purge_expired_download_request_torrent_data(30, limit=2),
            2,
        )
        remaining = sum(
            1
            for request_id in request_ids
            if db.get_download_request(request_id)["torrent_data"] is not None
        )
        self.assertEqual(remaining, 1)
        self.assertEqual(
            db.purge_expired_download_request_torrent_data(30, limit=2),
            1,
        )

    def test_cleaned_torrent_explains_why_qb_resubmit_is_unavailable(self):
        request_id = self._create_request(
            "qb-resubmit-expired",
            status="failed",
            age_days=60,
            qb_status="failed",
        )
        self.assertEqual(db.purge_expired_download_request_torrent_data(30), 1)

        with patch(
            "app.modules.download_dispatcher.get",
            side_effect=lambda key, default="": (
                "http://qb.invalid" if key == "QB_URL" else default
            ),
        ):
            capabilities = download_dispatcher.download_resubmit_capabilities(
                db.get_download_request(request_id)
            )

        self.assertFalse(capabilities["qb"]["enabled"])
        self.assertIn("保留策略清理", capabilities["qb"]["reason"])


class TorrentDataRetentionTrackerTests(unittest.TestCase):
    def test_tracker_runs_enabled_cleanup_once_per_interval(self):
        tracker = DownloadTracker()
        with patch(
            "app.modules.download_tracker.get",
            side_effect=lambda key, default="": (
                "30" if key == "DOWNLOAD_TORRENT_RETENTION_DAYS" else default
            ),
        ), patch(
            "app.modules.download_tracker.time.monotonic",
            side_effect=[100.0, 101.0],
        ), patch(
            "app.modules.download_tracker.db.purge_expired_download_request_torrent_data",
            return_value=4,
        ) as purge:
            self.assertEqual(tracker._run_torrent_data_cleanup_if_due(), 4)
            self.assertEqual(tracker._run_torrent_data_cleanup_if_due(), 0)

        purge.assert_called_once_with(30, limit=500)

    def test_tracker_skips_disabled_or_invalid_retention(self):
        for configured in ("0", "-1", "3651", "not-a-number"):
            tracker = DownloadTracker()
            with patch(
                "app.modules.download_tracker.get",
                return_value=configured,
            ), patch(
                "app.modules.download_tracker.db.purge_expired_download_request_torrent_data"
            ) as purge:
                self.assertEqual(tracker._run_torrent_data_cleanup_if_due(), 0)
            purge.assert_not_called()

    def test_reload_can_force_retention_check_on_next_tracker_cycle(self):
        tracker = DownloadTracker()
        tracker._last_torrent_data_cleanup_at = 123.0

        tracker.reload(reset_torrent_cleanup=True)

        self.assertEqual(tracker._last_torrent_data_cleanup_at, 0.0)
        self.assertTrue(tracker._wake_event.is_set())


if __name__ == "__main__":
    unittest.main()
