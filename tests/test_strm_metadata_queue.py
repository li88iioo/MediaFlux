"""STRM 伴随元数据持久化队列状态机。"""
from __future__ import annotations

from threading import Event, Thread
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from typing import TypedDict, cast
from app.clients.guangya import GuangYaClient

import sqlite3
import tempfile
import time

from app import database as db
from app.modules import strm_metadata_worker as metadata
from app.modules.process_lock import CrossProcessLock
from app.repositories.media_refresh_queue import media_refresh_queue_status
from tests.support import IsolatedDatabaseTestCase
from tests.support import PagedDirectoryTestMixin
from tests.support import isolated_test_database


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


def _refresh_paths_and_scopes():
    entries = db.list_strm_refresh_entries()
    assert all(len(str(entry["event_token"])) == 32 for entry in entries)
    return [{"path": entry["path"], "allow_emby": entry["allow_emby"]} for entry in entries]


class StrmMetadataQueueTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")
            conn.execute("DELETE FROM strm_refresh_outbox")

    def _rows(self, status: str = "all") -> list[dict]:
        return [dict(row) for row in db.list_strm_metadata_queue(status=status)]

    def test_enqueue_is_idempotent_and_completed_same_snapshot_stays_completed(self):
        first = db.enqueue_strm_metadata_jobs([_job()])
        second = db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")
        state = db.settle_strm_metadata_job(
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
        db.settle_strm_metadata_job(
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

    def test_policy_only_change_reopens_completed_job(self):
        original = _job(refresh_policy={"media_root": "/strm", "enabled": False})
        db.enqueue_strm_metadata_jobs([original])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        db.settle_strm_metadata_job(
            claimed["id"],
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )

        updated = {**original, "refresh_policy": {"media_root": "/strm", "allow_emby": False}}
        result = db.enqueue_strm_metadata_jobs([updated])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(self._rows()[0]["status"], "queued")
        self.assertEqual(self._rows()[0]["revision"], claimed["revision"] + 1)
        self.assertEqual(db.enqueue_strm_metadata_jobs([updated])["deduped"], 1)

    def test_legacy_empty_policy_matches_explicit_defaults_without_revision_change(self):
        from dataclasses import asdict

        from app.repositories.strm import StrmRefreshPolicy

        db.enqueue_strm_metadata_jobs([_job()])
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_metadata_queue SET refresh_policy_json='{}'")
        before = self._rows()[0]
        result = db.enqueue_strm_metadata_jobs([_job(refresh_policy=asdict(StrmRefreshPolicy()))])

        self.assertEqual(result["deduped"], 1)
        self.assertEqual(self._rows()[0]["revision"], before["revision"])

    def test_policy_only_change_fences_running_revision_and_uses_latest_scope(self):
        original = _job(refresh_policy={"media_root": "/strm"})
        db.enqueue_strm_metadata_jobs([original])
        first = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        result = db.enqueue_strm_metadata_jobs([{
            **original, "refresh_policy": {"media_root": "/strm", "allow_emby": False},
        }])

        self.assertEqual(result["dirty"], 1)
        self.assertEqual(self._rows()[0]["revision"], first["revision"] + 1)
        self.assertEqual(db.settle_strm_metadata_job(
            first["id"], expected_lease_generation=first["lease_generation"],
            expected_revision=first["revision"], refresh_paths=["/strm/Film/Movie.nfo"],
        ), "queued")
        self.assertEqual(db.list_strm_refresh_entries(), [])
        latest = db.claim_due_strm_metadata_jobs(owner="successor")[0]
        self.assertEqual(db.settle_strm_metadata_job(
            latest["id"], expected_lease_generation=latest["lease_generation"],
            expected_revision=latest["revision"], refresh_paths=["/strm/Film/Movie.nfo"],
        ), "completed")
        self.assertEqual(
            [entry["allow_emby"] for entry in db.list_strm_refresh_entries()], [False],
        )

    def test_change_while_running_marks_dirty_and_fences_old_revision(self):
        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")

        result = db.enqueue_strm_metadata_jobs([_job(etag="etag-2")])
        state = db.settle_strm_metadata_job(
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

        stale = db.settle_strm_metadata_job(
            first["id"],
            expected_lease_generation=first["lease_generation"],
            expected_revision=first["revision"],
        )
        current = db.settle_strm_metadata_job(
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

        stale = db.settle_strm_metadata_job(
            claimed["id"],
            expected_owner="worker-b",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )
        current = db.settle_strm_metadata_job(
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

        state = db.settle_strm_metadata_job(
            claimed["id"],
            expected_owner="worker-a",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
            refresh_paths=(refresh_path,),
        )

        self.assertEqual(state, "completed")
        self.assertEqual(
            _refresh_paths_and_scopes(),
            [{"path": refresh_path, "allow_emby": True}],
        )
        self.assertEqual(db.count_strm_refresh_paths(), 1)
        self.assertEqual(db.acknowledge_strm_refresh_paths(db.list_strm_refresh_entries()), 1)
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def test_refresh_outbox_preserves_provider_scope_for_same_path(self):
        refresh_path = "/strm/剧集/Example/Season 01"

        self.assertEqual(
            len(db.enqueue_strm_refresh_paths([refresh_path], allow_emby=False)), 1
        )
        self.assertEqual(
            len(db.enqueue_strm_refresh_paths([refresh_path], allow_emby=True)), 1
        )

        self.assertEqual(
            _refresh_paths_and_scopes(),
            [
                {"path": refresh_path, "allow_emby": False},
                {"path": refresh_path, "allow_emby": True},
            ],
        )
        self.assertEqual(db.count_strm_refresh_paths(), 2)
        self.assertEqual(
            db.acknowledge_strm_refresh_paths(
                [entry for entry in db.list_strm_refresh_entries() if not entry["allow_emby"]]
            ),
            1,
        )
        self.assertEqual(
            _refresh_paths_and_scopes(),
            [{"path": refresh_path, "allow_emby": True}],
        )
        self.assertEqual(
            db.acknowledge_strm_refresh_paths(
                db.list_strm_refresh_entries()
            ),
            1,
        )
        self.assertEqual(db.list_strm_refresh_entries(), [])

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
        db.settle_strm_metadata_job(
            claimed["id"],
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
        )

        summary = db.count_strm_metadata_jobs()

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["total"], 2)


class _TreeClient(PagedDirectoryTestMixin):
    credentials_current = True
    logged_in = True

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
            conn.execute("DELETE FROM strm_refresh_outbox")
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
                client=cast(GuangYaClient, client),
                metadata_exts={"nfo"},
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

    def test_full_and_incremental_metadata_keep_frozen_refresh_policy(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from app.clients.guangya import GuangYaFile
        from app.modules import strm
        from app.modules.strm_metadata_worker import STRMMetadataWorker
        from app.repositories.strm import StrmRefreshPolicy
        from tests.support import isolated_test_database

        class SyncOptions(TypedDict):
            source_dir_id: str
            base_url: str
            strm_root: str
            client: GuangYaClient
            metadata_exts: set[str]
            refresh_policy: StrmRefreshPolicy

        for mode in ("full", "incremental"):
            for enabled, allow_emby in ((False, False), (True, False), (True, True)):
                with self.subTest(mode=mode, enabled=enabled, allow_emby=allow_emby), \
                     isolated_test_database(), tempfile.TemporaryDirectory() as root:
                    metadata = GuangYaFile("meta", "Movie.nfo", False, 8, "m1", "film")
                    client = _TreeClient({
                        "source": [GuangYaFile("film", "Film", True, parent_id="source")],
                        "film": [metadata],
                    })
                    policy = StrmRefreshPolicy(
                        media_root=str(Path(root) / strm.STRM_SUBDIR),
                        enabled=enabled, allow_emby=allow_emby,
                    )
                    options: SyncOptions = {
                        "source_dir_id": "source", "base_url": "http://media.invalid",
                        "strm_root": root, "client": cast(GuangYaClient, client),
                        "metadata_exts": {"nfo"}, "refresh_policy": policy,
                    }
                    if mode == "incremental":
                        changes = [{
                            "source_id": "source", "kind": "metadata", "action": "upsert",
                            "file_id": "meta", "parent_id": "film", "name": "Movie.nfo",
                            "etag": "m1", "size": 8, "rel_dir": "Film",
                        }]
                        result = strm.sync_strm_incremental(changes=changes, **options)
                    else:
                        result = strm.sync_strm(**options)
                    self.assertEqual(result["metadata_queued"], 1)
                    worker = STRMMetadataWorker()
                    worker._client = cast(GuangYaClient, client)
                    with patch("app.modules.strm_metadata_worker.get_bool", return_value=True), \
                         patch("app.modules.strm_metadata_worker.get", side_effect=lambda k, d="": {
                             "STRM_ROOT": root, "STRM_METADATA_EXTS": "nfo",
                         }.get(k, d)), \
                         patch("app.modules.strm.requests.get", return_value=_Response()), \
                         patch.object(worker, "_flush_media_refresh"):
                        self.assertTrue(worker._process_one())
                    self.assertEqual(dict(db.list_strm_metadata_queue()[0])["status"], "completed")
                    self.assertEqual(
                        [entry["allow_emby"] for entry in db.list_strm_refresh_entries()],
                        [allow_emby] if enabled else [],
                    )

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
            worker._client = cast(GuangYaClient, client)
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

    def test_media_refresh_failure_keeps_durable_outbox_until_retry_succeeds(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        path = "/strm/Movie/Movie.nfo"
        db.settle_strm_metadata_job(
            claimed["id"],
            expected_owner="worker",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
            refresh_paths=(path,),
        )
        worker = STRMMetadataWorker()
        self.assertFalse(hasattr(worker, "_changed_paths"))
        with patch(
            "app.modules.strm_metadata_worker.get_int",
            side_effect=lambda key, default=0: {
                "STRM_METADATA_REFRESH_BATCH_SIZE": 50,
                "STRM_METADATA_REFRESH_INTERVAL_SECONDS": 300,
            }.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            side_effect=[{"Jellyfin": "failed"}, {"Jellyfin": "queued"}],
        ) as refresh:
            worker._flush_media_refresh(force=True)
            self.assertEqual(
                _refresh_paths_and_scopes(),
                [{"path": path, "allow_emby": True}],
            )
            self.assertTrue(worker._refresh_retry_pending)

            # 进程停止或显式 flush 必须立即重试，不能因普通批处理节流丢失刷新。
            worker._flush_media_refresh(force=True)

        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(refresh.call_args_list[0].args[0], [path])
        self.assertTrue(refresh.call_args_list[0].kwargs["immediate"])
        self.assertEqual(db.list_strm_refresh_entries(), [])
        self.assertFalse(worker._refresh_retry_pending)

    def test_new_worker_replays_durable_media_refresh_after_restart(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        db.enqueue_strm_metadata_jobs([_job()])
        claimed = db.claim_due_strm_metadata_jobs(owner="old-worker")[0]
        path = "/strm/Movie/Movie.nfo"
        db.settle_strm_metadata_job(
            claimed["id"],
            expected_owner="old-worker",
            expected_lease_generation=claimed["lease_generation"],
            expected_revision=claimed["revision"],
            refresh_paths=(path,),
        )
        worker = STRMMetadataWorker()

        with patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as refresh, patch(
            "app.modules.strm_metadata_worker.time.monotonic",
            return_value=120.0,
        ):
            worker._flush_media_refresh(force=False)

        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], [path])
        self.assertFalse(refresh.call_args.kwargs["immediate"])
        self.assertEqual(db.count_strm_refresh_paths(), 0)

    def test_worker_flushes_each_provider_scope_and_acks_only_success(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        path = "/strm/剧集/Example/Season 01"
        db.enqueue_strm_refresh_paths([path], allow_emby=False)
        db.enqueue_strm_refresh_paths([path], allow_emby=True)
        worker = STRMMetadataWorker()

        with patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            side_effect=[
                {"Jellyfin": "queued"},
                {"Jellyfin": "queued", "Emby": "failed"},
                {"Jellyfin": "queued", "Emby": "queued"},
            ],
        ) as refresh:
            worker._flush_media_refresh(force=True)

            self.assertEqual(refresh.call_count, 2)
            self.assertEqual(refresh.call_args_list[0].args[0], [path])
            self.assertFalse(refresh.call_args_list[0].kwargs["allow_emby"])
            self.assertEqual(refresh.call_args_list[1].args[0], [path])
            self.assertTrue(refresh.call_args_list[1].kwargs["allow_emby"])
            self.assertEqual(
                _refresh_paths_and_scopes(),
                [{"path": path, "allow_emby": True}],
            )
            self.assertTrue(worker._refresh_retry_pending)

            worker._flush_media_refresh(force=True)

        self.assertEqual(refresh.call_count, 3)
        self.assertTrue(refresh.call_args_list[2].kwargs["allow_emby"])
        self.assertEqual(db.list_strm_refresh_entries(), [])
        self.assertFalse(worker._refresh_retry_pending)

    def test_stop_does_not_discard_worker_restarted_during_join(self):
        from unittest.mock import patch

        from app.modules.strm_metadata_worker import STRMMetadataWorker

        class FakeClient:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        class FakeThread:
            def __init__(self, *, alive=False, on_join=None):
                self.alive = alive
                self.on_join = on_join

            def is_alive(self):
                return self.alive

            def start(self):
                self.alive = True

            def join(self, timeout=None):
                self.alive = False
                if self.on_join:
                    self.on_join()

        worker = STRMMetadataWorker()
        client = FakeClient()
        replacement = FakeThread()
        old = FakeThread(alive=True, on_join=worker.start)
        worker._thread = cast(Thread, old)
        worker._client = cast(GuangYaClient, client)

        with patch("app.modules.strm_metadata_worker.threading.Thread", return_value=replacement):
            self.assertFalse(worker.stop(timeout=0.1))

        self.assertIs(worker._thread, replacement)
        self.assertTrue(replacement.is_alive())
        self.assertFalse(worker._stop_event.is_set())
        self.assertEqual(client.close_calls, 0)

        replacement.alive = False
        self.assertTrue(worker.stop(timeout=0.1))
        self.assertIsNone(worker._thread)
        self.assertEqual(client.close_calls, 1)


class StrmMetadataWorkerLifecycleTests(TestCase):
    """真实线程、文件锁和 SQLite：存储故障不能切断持久交接或泄漏消费者锁。"""

    def setUp(self):
        from app.clients.guangya import GuangYaFile

        self.enterContext(isolated_test_database())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remote = GuangYaFile("meta-1", "Movie.nfo", False, 8, "etag-1", "parent")
        self.worker = metadata.STRMMetadataWorker()
        self.worker._client = cast(GuangYaClient, _TreeClient({"parent": [self.remote]}))
        self.handed_off = Event()
        self.backoff = Event()
        self.thread_errors = []
        self.enterContext(patch.object(metadata, "get_bool", return_value=True))
        self.enterContext(patch.object(metadata, "get", side_effect=lambda key, default="": {
            "STRM_ROOT": str(self.root), "STRM_METADATA_EXTS": "nfo",
        }.get(key, default)))
        self.download = self.enterContext(patch("app.modules.strm.requests.get", return_value=_Response()))
        self.enterContext(patch(
            "app.modules.media_refresh_coordinator._configured_provider_names", return_value=("jellyfin",),
        ))
        self.enterContext(patch("threading.excepthook", side_effect=lambda args: self.thread_errors.append(args.exc_value)))
        acknowledge = db.acknowledge_strm_refresh_paths
        wait = self.worker._wake_event.wait

        def handoff(entries):
            result = acknowledge(entries)
            self.handed_off.set()
            return result

        def idle_wait(timeout=None):
            self.backoff.set()
            return wait(timeout)

        self.enterContext(patch.object(db, "acknowledge_strm_refresh_paths", side_effect=handoff))
        self.enterContext(patch.object(self.worker._wake_event, "wait", side_effect=idle_wait))
        self.addCleanup(self._stop_worker, self.worker)

    def _stop_worker(self, worker):
        self.assertTrue(worker.stop(timeout=3))
        # 修前反例也必须清理真实锁；断言中的接管检查发生在此兜底之前。
        if worker._worker_lock._handle is not None:
            worker._worker_lock.release()

    def _assert_lock_released(self):
        self.assertFalse(self.worker.status()["consumer_active"])
        successor_lock = CrossProcessLock("strm-metadata-worker")
        self.assertTrue(successor_lock.acquire(blocking=False))
        successor_lock.release()

    def _assert_download_and_handoff(self):
        self.assertTrue(self.handed_off.wait(7), "存储恢复后未继续完成元数据与刷新交接")
        self.assertTrue(self.worker.stop(timeout=3))
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual(dict(db.list_strm_metadata_queue()[0])["status"], "completed")
        self.assertEqual(list(self.root.rglob("*.nfo"))[0].read_bytes(), b"metadata")
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        self.assertEqual(media_refresh_queue_status()["paths"], 1)
        self.assertEqual(self.thread_errors, [])
        self._assert_lock_released()

    def test_idle_refresh_read_error_retries_then_downloads_new_work(self):
        restored = Event()
        read = db.list_strm_refresh_entries

        def read_outbox(**kwargs):
            if not restored.is_set():
                raise sqlite3.OperationalError("isolated outbox read failure")
            return read(**kwargs)

        with patch.object(db, "list_strm_refresh_entries", side_effect=read_outbox):
            self.worker.start()
            self.assertTrue(self.backoff.wait(2), "刷新读取失败逃出了消费循环")
            self.assertTrue(self.worker.status()["worker_running"])
            db.enqueue_strm_metadata_jobs([_job(filename=self.remote.name, size=8)])
            restored.set()
            self.worker.wake()
            self._assert_download_and_handoff()

    def test_processing_error_retries_without_restarting_the_thread(self):
        restored = Event()
        runtime_client = self.worker._runtime_client

        def client():
            if not restored.is_set():
                raise OSError("isolated client initialization failure")
            return runtime_client()

        db.enqueue_strm_metadata_jobs([_job(filename=self.remote.name, size=8)])
        with patch.object(self.worker, "_runtime_client", side_effect=client):
            self.worker.start()
            self.assertTrue(self.backoff.wait(2))
            self.assertEqual(dict(db.list_strm_metadata_queue()[0])["status"], "queued")
            restored.set()
            self.worker.wake()
            self._assert_download_and_handoff()

    def test_startup_recovery_retries_before_claiming_or_flushing(self):
        restored = Event()
        recover = db.recover_stale_strm_metadata_jobs
        db.enqueue_strm_metadata_jobs([_job(filename=self.remote.name, size=8)])
        db.claim_due_strm_metadata_jobs(owner="previous-process")

        def recover_jobs(**kwargs):
            if not kwargs.get("owner") and not restored.is_set():
                raise sqlite3.OperationalError("isolated startup recovery failure")
            return recover(**kwargs)

        with patch.object(db, "recover_stale_strm_metadata_jobs", side_effect=recover_jobs), \
             patch.object(db, "list_strm_refresh_entries", wraps=db.list_strm_refresh_entries) as read:
            self.worker.start()
            self.assertTrue(self.backoff.wait(2), "首次恢复失败终止了消费者")
            self.assertTrue(self.worker.status()["worker_running"])
            self.assertEqual(dict(db.list_strm_metadata_queue()[0])["lease_owner"], "previous-process")
            self.download.assert_not_called()
            read.assert_not_called()
            restored.set()
            self.worker.wake()
            self._assert_download_and_handoff()

    def _exit_with_storage_failure(self, *, recovery_fails):
        claimed = Event()
        db.enqueue_strm_metadata_jobs([_job(filename=self.remote.name, size=8), _job(file_id="other")])
        entries = db.enqueue_strm_refresh_paths([str(self.root / "Film")], allow_emby=False)
        recover = db.recover_stale_strm_metadata_jobs

        def process():
            db.claim_due_strm_metadata_jobs(owner=self.worker._owner)
            db.claim_due_strm_metadata_jobs(owner="other-worker")
            claimed.set()
            if not self.worker._stop_event.wait(10):
                raise AssertionError("测试未及时停止消费者")
            return True

        def recover_jobs(**kwargs):
            if recovery_fails and kwargs.get("owner"):
                raise sqlite3.OperationalError("isolated exit recovery failure")
            return recover(**kwargs)

        with patch.object(self.worker, "_process_one", side_effect=process), \
             patch.object(db, "list_strm_refresh_entries", side_effect=sqlite3.OperationalError("isolated exit flush failure")), \
             patch.object(db, "recover_stale_strm_metadata_jobs", side_effect=recover_jobs) as recovery:
            self.worker.start()
            self.assertTrue(claimed.wait(2))
            self.assertTrue(self.worker.stop(timeout=3))
            self.assertTrue(any(call.kwargs.get("owner") == self.worker._owner for call in recovery.call_args_list))

        rows = {row["file_id"]: dict(row) for row in db.list_strm_metadata_queue()}
        self.assertEqual(rows["meta-1"]["status"], "running" if recovery_fails else "retry_wait")
        self.assertEqual(rows["meta-1"]["attempts"], 0)
        self.assertEqual(rows["other"]["lease_owner"], "other-worker")
        self.assertEqual(db.list_strm_refresh_entries(), entries)
        self.assertEqual(self.thread_errors, [])
        self._assert_lock_released()

        # 后继真实消费者立即接管未确认 outbox，也恢复前驱未能释放的租约。
        successor = metadata.STRMMetadataWorker()
        self.addCleanup(self._stop_worker, successor)
        with patch.object(metadata, "get_bool", return_value=False):
            successor.start()
            self.assertTrue(self.handed_off.wait(7))
            self.assertTrue(successor.stop(timeout=3))
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        self.assertEqual(media_refresh_queue_status()["paths"], 1)
        self.assertEqual(db.count_strm_metadata_jobs()["running"], 0)
        self.download.assert_not_called()

    def test_exit_refresh_error_still_recovers_own_lease_and_releases_lock(self):
        self._exit_with_storage_failure(recovery_fails=False)

    def test_exit_refresh_and_recovery_errors_allow_successor_to_take_over(self):
        self._exit_with_storage_failure(recovery_fails=True)

    def test_stopped_lock_waiter_does_not_flush_or_recover_foreign_work(self):
        holder = CrossProcessLock("strm-metadata-worker")
        self.assertTrue(holder.acquire(blocking=False))
        self.addCleanup(holder.release)
        db.enqueue_strm_metadata_jobs([_job()])
        db.claim_due_strm_metadata_jobs(owner="active-consumer")
        entries = db.enqueue_strm_refresh_paths([str(self.root / "Film")], allow_emby=False)
        attempted = Event()
        acquire = self.worker._worker_lock.acquire

        def acquire_lock(**kwargs):
            acquired = acquire(**kwargs)
            attempted.set()
            return acquired

        with patch.object(self.worker._worker_lock, "acquire", side_effect=acquire_lock), \
             patch.object(db, "recover_stale_strm_metadata_jobs", wraps=db.recover_stale_strm_metadata_jobs) as recover, \
             patch.object(db, "list_strm_refresh_entries", wraps=db.list_strm_refresh_entries) as read:
            self.worker.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(self.worker.status()["consumer_active"])
            self.assertTrue(self.worker.stop(timeout=3))
            recover.assert_not_called()
            read.assert_not_called()

        self.assertEqual(db.list_strm_refresh_entries(), entries)
        self.assertEqual(dict(db.list_strm_metadata_queue()[0])["lease_owner"], "active-consumer")
        self.assertEqual(media_refresh_queue_status()["paths"], 0)
        self.assertEqual(self.thread_errors, [])
