"""STRM 伴随元数据持久化队列状态机。"""
from __future__ import annotations

import time

from app import database as db
from tests.support import IsolatedDatabaseTestCase


def _job(**updates) -> dict:
    payload = {
        "source_id": "source",
        "source_name": "整理",
        "file_id": "meta-1",
        "parent_id": "parent",
        "filename": "movie.nfo",
        "etag": "etag-1",
        "size": 128,
        "rel_dir": "电影/Movie (2026)",
        "target_rel_path": "整理/电影/Movie (2026)/movie.nfo",
    }
    payload.update(updates)
    return payload


class StrmMetadataQueueTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")
            conn.execute("DELETE FROM strm_metadata_refresh_outbox")

    def _rows(self, status: str = "all") -> list[dict]:
        return [dict(row) for row in db.list_strm_metadata_queue(status=status)]

    def test_enqueue_is_idempotent_and_completed_same_snapshot_stays_completed(self):
        first = db.enqueue_strm_metadata_jobs([_job()])
        second = db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")
        state = db.complete_strm_metadata_job(
            claimed[0]["id"],
            expected_lease_generation=claimed[0]["lease_generation"],
            expected_revision=claimed[0]["revision"],
        )
        third = db.enqueue_strm_metadata_jobs([_job()])

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["deduped"], 1)
        self.assertEqual(state, "completed")
        self.assertEqual(third["deduped"], 1)
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self._rows()[0]["status"], "completed")

    def test_changed_snapshot_reopens_completed_job(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")
        db.complete_strm_metadata_job(
            claimed[0]["id"],
            expected_lease_generation=claimed[0]["lease_generation"],
            expected_revision=claimed[0]["revision"],
        )

        result = db.enqueue_strm_metadata_jobs([_job(etag="etag-2", size=256)])

        row = self._rows()[0]
        self.assertEqual(result["updated"], 1)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["etag"], "etag-2")
        self.assertEqual(row["attempts"], 0)

    def test_change_while_running_marks_dirty_and_fences_old_revision(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")

        result = db.enqueue_strm_metadata_jobs([_job(etag="etag-2")])
        state = db.complete_strm_metadata_job(
            claimed[0]["id"],
            expected_lease_generation=claimed[0]["lease_generation"],
            expected_revision=claimed[0]["revision"],
        )

        row = self._rows()[0]
        self.assertEqual(result["dirty"], 1)
        self.assertEqual(state, "queued")
        self.assertEqual(row["etag"], "etag-2")
        self.assertEqual(row["status"], "queued")

    def test_expired_lease_reclaim_rejects_late_worker_result(self):
        db.enqueue_strm_metadata_jobs([_job()])
        first = db.claim_due_strm_metadata_jobs(owner="worker-a", lease_seconds=1)[0]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_metadata_queue SET lease_until=? WHERE id=?",
                (time.time() - 1, first["id"]),
            )
        second = db.claim_due_strm_metadata_jobs(owner="worker-b", lease_seconds=60)[0]

        stale = db.complete_strm_metadata_job(
            first["id"],
            expected_lease_generation=first["lease_generation"],
            expected_revision=first["revision"],
        )
        current = db.complete_strm_metadata_job(
            second["id"],
            expected_lease_generation=second["lease_generation"],
            expected_revision=second["revision"],
        )

        self.assertEqual(stale, "stale")
        self.assertEqual(current, "completed")

    def test_lease_renewal_is_fenced_by_owner_and_generation(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(
            owner="worker-a", lease_seconds=30
        )[0]
        before = float(claimed["lease_until"] or 0)

        renewed = db.renew_strm_metadata_job_lease(
            claimed["id"],
            expected_owner="worker-a",
            expected_lease_generation=claimed["lease_generation"],
            lease_seconds=120,
        )
        wrong_owner = db.renew_strm_metadata_job_lease(
            claimed["id"],
            expected_owner="worker-b",
            expected_lease_generation=claimed["lease_generation"],
            lease_seconds=120,
        )
        row = self._rows()[0]

        self.assertTrue(renewed)
        self.assertFalse(wrong_owner)
        self.assertGreater(float(row["lease_until"] or 0), before)

    def test_completion_rejects_wrong_lease_owner(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker-a")[0]

        stale = db.complete_strm_metadata_job(
            claimed["id"],
            expected_owner="worker-b",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )
        current = db.complete_strm_metadata_job(
            claimed["id"],
            expected_owner="worker-a",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )

        self.assertEqual(stale, "stale")
        self.assertEqual(current, "completed")

    def test_completed_metadata_persists_media_refresh_until_acknowledged(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker-a")[0]
        refresh_path = "/strm/电影/Movie/Movie.nfo"

        state = db.complete_strm_metadata_job(
            claimed["id"],
            expected_owner="worker-a",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
            refresh_path=refresh_path,
        )

        self.assertEqual(state, "completed")
        self.assertEqual(db.list_strm_metadata_refresh_paths(), [refresh_path])
        self.assertEqual(db.count_strm_metadata_refresh_paths(), 1)
        self.assertEqual(db.acknowledge_strm_metadata_refresh_paths([refresh_path]), 1)
        self.assertEqual(db.list_strm_metadata_refresh_paths(), [])

    def test_failures_use_exponential_retry_and_end_in_failed(self):
        db.enqueue_strm_metadata_jobs([_job()], max_attempts=2)
        first = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        state = db.fail_or_retry_strm_metadata_job(
            first["id"],
            expected_lease_generation=first["lease_generation"],
            expected_revision=first["revision"],
            error_type="IncompleteRead",
            error="https://secret.invalid/file?token=SECRET",
            base_backoff_seconds=1,
        )
        self.assertEqual(state, "retry_wait")
        row = self._rows()[0]
        self.assertNotIn("SECRET", row["last_error"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_metadata_queue SET next_attempt_at=? WHERE id=?",
                (db.now(), first["id"]),
            )
        second = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        state = db.fail_or_retry_strm_metadata_job(
            second["id"],
            expected_lease_generation=second["lease_generation"],
            expected_revision=second["revision"],
            error_type="IncompleteRead",
            error="again",
            base_backoff_seconds=1,
        )
        self.assertEqual(state, "failed")
        self.assertEqual(self._rows()[0]["attempts"], 2)

    def test_startup_recovery_requeues_running_without_incrementing_attempts(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="old-worker")[0]

        recovered = db.recover_stale_strm_metadata_jobs(force=True)

        row = self._rows()[0]
        self.assertEqual(recovered, 1)
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["attempts"], 0)
        self.assertGreater(row["lease_generation"], claimed["lease_generation"])

    def test_forced_recovery_can_be_scoped_to_one_worker_owner(self):
        db.enqueue_strm_metadata_jobs([
            _job(file_id="meta-a", filename="a.nfo"),
            _job(file_id="meta-b", filename="b.nfo"),
        ])
        first = db.claim_due_strm_metadata_jobs(owner="worker-a", limit=1)[0]
        second = db.claim_due_strm_metadata_jobs(owner="worker-b", limit=1)[0]

        recovered = db.recover_stale_strm_metadata_jobs(
            force=True, owner="worker-a"
        )

        rows = {row["id"]: row for row in self._rows()}
        self.assertEqual(recovered, 1)
        self.assertEqual(rows[first["id"]]["status"], "retry_wait")
        self.assertEqual(rows[second["id"]]["status"], "running")

    def test_full_scan_can_cancel_stale_jobs_and_reappearance_reopens(self):
        db.enqueue_strm_metadata_jobs([_job(file_id="keep"), _job(file_id="stale")])

        cancelled = db.cancel_stale_strm_metadata_jobs("source", {"keep"})
        reopened = db.enqueue_strm_metadata_jobs([_job(file_id="stale", etag="etag-2")])

        rows = {row["file_id"]: row for row in self._rows()}
        self.assertEqual(cancelled, 1)
        self.assertEqual(reopened["updated"], 1)
        self.assertEqual(rows["keep"]["status"], "queued")
        self.assertEqual(rows["stale"]["status"], "queued")

    def test_summary_reports_pending_and_terminal_counts(self):
        db.enqueue_strm_metadata_jobs([_job(file_id="a"), _job(file_id="b")])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker", limit=1)[0]
        db.complete_strm_metadata_job(
            claimed["id"],
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )

        summary = db.count_strm_metadata_jobs()

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["total"], 2)


class _TreeClient:
    def __init__(self, tree):
        self.tree = tree
        self.info = {}
        for values in tree.values():
            for item in values:
                self.info[item.file_id] = item

    def list_dir(self, file_id):
        return list(self.tree.get(file_id, []))

    def file_info(self, file_id):
        return self.info.get(str(file_id))

    def get_download_url(self, file_id):
        return f"https://download.invalid/{file_id}"

    def close(self):
        return None


class _Response:
    headers = {"Content-Length": "8"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield b"metadata"


class StrmMetadataQueueIntegrationTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")
            conn.execute("DELETE FROM strm_metadata_refresh_outbox")
            conn.execute("DELETE FROM strm_index")
            conn.execute("DELETE FROM strm_failures")

    def test_full_sync_queues_metadata_without_resolving_download_url(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        from app.clients.guangya import GuangYaFile
        from app.modules.strm import STRM_SUBDIR, sync_strm

        source_id = "source"
        video = GuangYaFile("video", "Movie.mkv", False, 1024, "v1", source_id)
        metadata = GuangYaFile("meta", "Movie.nfo", False, 8, "m1", source_id)
        client = _TreeClient({source_id: [video, metadata]})
        client.get_download_url = Mock(side_effect=AssertionError("sync must not download metadata"))

        with tempfile.TemporaryDirectory() as root:
            result = sync_strm(
                source_id,
                "http://media.invalid",
                root,
                client=client,
                metadata_exts={"nfo"},
                defer_metadata=True,
            )

            self.assertEqual(result["generated"], 1)
            self.assertEqual(result["metadata_generated"], 0)
            self.assertEqual(result["metadata_queued"], 1)
            self.assertTrue(list((Path(root) / STRM_SUBDIR).rglob("*.strm")))
            self.assertFalse(list((Path(root) / STRM_SUBDIR).rglob("*.nfo")))
            client.get_download_url.assert_not_called()
            rows = [dict(row) for row in db.list_strm_metadata_queue()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "queued")

    def test_background_worker_downloads_and_commits_queued_metadata(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from app.clients.guangya import GuangYaFile
        from app.modules.strm import STRM_SUBDIR
        from app.modules.strm_metadata_worker import STRMMetadataWorker

        source_id = "source"
        metadata = GuangYaFile("meta", "Movie.nfo", False, 8, "m1", source_id)
        client = _TreeClient({source_id: [metadata]})

        with tempfile.TemporaryDirectory() as root:
            db.enqueue_strm_metadata_jobs([{
                "source_id": source_id,
                "source_name": "整理",
                "file_id": metadata.file_id,
                "parent_id": metadata.parent_id,
                "filename": metadata.name,
                "etag": metadata.etag,
                "size": metadata.size,
                "rel_dir": "Movie",
                "target_rel_path": "光鸭云盘/Movie/Movie.nfo",
            }])
            worker = STRMMetadataWorker()
            worker._client = client
            with patch(
                "app.modules.strm_metadata_worker.get_bool", return_value=True
            ), patch(
                "app.modules.strm_metadata_worker.get",
                side_effect=lambda key, default="": {
                    "STRM_ROOT": root,
                    "STRM_METADATA_EXTS": "nfo",
                }.get(key, default),
            ), patch(
                "app.modules.strm.requests.get", return_value=_Response()
            ):
                worked = worker._process_one()

            self.assertTrue(worked)
            row = dict(db.list_strm_metadata_queue()[0])
            self.assertEqual(row["status"], "completed")
            target = Path(root) / STRM_SUBDIR / "Movie" / "Movie.nfo"
            self.assertEqual(target.read_bytes(), b"metadata")
            index = db.list_strm_index(f"guangya-meta:{source_id}")
            self.assertEqual([item["file_id"] for item in index], ["meta"])

    def test_media_refresh_failure_keeps_changed_paths_until_retry_succeeds(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        worker = STRMMetadataWorker()
        worker._changed_paths = ["/strm/Movie/Movie.nfo", "/strm/Movie/Movie.nfo"]
        with patch(
            "app.modules.strm_metadata_worker.get_int",
            side_effect=lambda key, default=0: {
                "STRM_METADATA_REFRESH_BATCH_SIZE": 50,
                "STRM_METADATA_REFRESH_INTERVAL_SECONDS": 300,
            }.get(key, default),
        ), patch(
            "app.modules.scheduler.STRMScheduler._refresh_media_servers",
            side_effect=[{"Jellyfin": False}, {"Jellyfin": True}],
        ) as refresh:
            worker._flush_media_refresh(force=True)
            self.assertEqual(worker._changed_paths, [
                "/strm/Movie/Movie.nfo", "/strm/Movie/Movie.nfo",
            ])
            self.assertTrue(worker._refresh_retry_pending)

            # 进程停止或显式 flush 必须立即重试，不能因普通批处理节流丢失刷新。
            worker._flush_media_refresh(force=True)

        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(
            refresh.call_args_list[0].kwargs["changed_paths"],
            ["/strm/Movie/Movie.nfo"],
        )
        self.assertEqual(worker._changed_paths, [])
        self.assertFalse(worker._refresh_retry_pending)

    def test_new_worker_replays_durable_media_refresh_after_restart(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="old-worker")[0]
        path = "/strm/Movie/Movie.nfo"
        db.complete_strm_metadata_job(
            claimed["id"],
            expected_owner="old-worker",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
            refresh_path=path,
        )
        worker = STRMMetadataWorker()

        with patch(
            "app.modules.scheduler.STRMScheduler._refresh_media_servers",
            return_value={"Jellyfin": True},
        ) as refresh:
            worker._flush_media_refresh(force=True)

        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs["changed_paths"], [path])
        self.assertEqual(db.count_strm_metadata_refresh_paths(), 0)
