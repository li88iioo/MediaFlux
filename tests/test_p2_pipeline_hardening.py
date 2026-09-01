from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizeRules
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.scheduler import STRMScheduler
from app.modules.strm import (
    STRM_SUBDIR,
    build_play_url,
    clean_invalid_strm,
    clean_retired_strm_sources,
    generate_strm,
    sync_strm,
)
from tests.support import IsolatedDatabaseTestCase


class _TreeClient:
    def __init__(self, tree):
        self.tree = tree

    def list_dir(self, file_id):
        return self.tree.get(file_id, [])

    def file_info(self, file_id):
        for children in self.tree.values():
            for item in children:
                if str(item.file_id) == str(file_id):
                    return item
        return None

    def get_download_url(self, file_id):
        return f"https://storage.invalid/{file_id}"


def _process_metadata_once(worker, root: str, extensions: str = "jpg") -> bool:
    with patch(
        "app.modules.strm_metadata_worker.get_bool", return_value=True
    ), patch(
        "app.modules.strm_metadata_worker.get",
        side_effect=lambda key, default="": {
            "STRM_ROOT": root,
            "STRM_METADATA_EXTS": extensions,
        }.get(key, default),
    ):
        return worker._process_one()


class P2StrmOwnershipTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _fingerprint(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def test_retired_source_deletes_only_fingerprinted_owned_file(self) -> None:
        source_id = f"retired-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "Movie.strm"
            target.parent.mkdir(parents=True)
            payload = b"http://localhost/playgy/file-1/etag/1/Movie.mkv"
            target.write_bytes(payload)
            db.upsert_strm_index(
                f"guangya:{source_id}", "file-1", "etag", 1, "Movie.mkv",
                str(target), self._fingerprint(payload),
            )
            db.enqueue_strm_retired_source(source_id, "旧来源", root)

            result = clean_retired_strm_sources(set())

            self.assertEqual(result["cleaned"], 1)
            self.assertFalse(target.exists())
            self.assertEqual(db.list_strm_index(f"guangya:{source_id}"), [])
            self.assertFalse(any(row["source_id"] == source_id for row in db.list_strm_retired_sources()))

    def test_retired_source_preserves_locally_modified_metadata(self) -> None:
        source_id = f"retired-meta-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            original = b"mediaflux-owned"
            target.write_bytes(b"user-modified!")
            db.upsert_strm_index(
                f"guangya-meta:{source_id}", "poster-1", "etag", len(original),
                "poster.jpg", str(target), self._fingerprint(original),
            )
            db.enqueue_strm_retired_source(source_id, "旧元数据来源", root)

            result = clean_retired_strm_sources(set())

            self.assertEqual(result["cleaned"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertTrue(target.exists())
            self.assertEqual(len(db.list_strm_index(f"guangya-meta:{source_id}")), 1)
            self.assertTrue(any(row["source_id"] == source_id for row in db.list_strm_retired_sources()))

    def test_sync_repairs_locally_modified_indexed_strm(self) -> None:
        source_id = f"modified-{uuid.uuid4().hex}"
        video = GuangYaFile("video-1", "Movie.mkv", False, 1024, "etag-1", source_id)
        source_key = f"guangya:{source_id}"
        with tempfile.TemporaryDirectory() as root:
            first = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [video]}),
            )
            target = next((Path(root) / STRM_SUBDIR).rglob("*.strm"))
            target.write_text("broken-address", encoding="utf-8")

            second = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [video]}),
            )
            row = db.list_strm_index(source_key)[0]
            expected = build_play_url(
                "http://localhost:1258", video.file_id, video.etag,
                video.size, video.name,
            )

            self.assertEqual(first["generated"], 1)
            self.assertEqual(second["generated"], 1)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(second["failed"], 0)
            self.assertEqual(target.read_text(encoding="utf-8"), expected)
            self.assertEqual(row["etag"], "etag-1")
            self.assertEqual(row["content_fingerprint"], self._fingerprint(expected.encode()))

    def test_sync_preserves_unknown_same_name_strm_without_index(self) -> None:
        source_id = f"unknown-{uuid.uuid4().hex}"
        video = GuangYaFile("video-1", "Movie.mkv", False, 1024, "etag-1", source_id)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "Movie.strm"
            target.parent.mkdir(parents=True)
            target.write_text("user-owned", encoding="utf-8")

            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [video]}),
            )

            self.assertEqual(stats["generated"], 0)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "user-owned")
            self.assertEqual(db.list_strm_index(f"guangya:{source_id}"), [])

    def test_full_sync_preserves_unindexed_tgto_strm_and_local_sidecars(self) -> None:
        source_id = f"tgto-{uuid.uuid4().hex}"
        video = GuangYaFile("video-1", "Movie.mkv", False, 1024, "etag-1", source_id)
        with tempfile.TemporaryDirectory() as root:
            target_dir = Path(root) / STRM_SUBDIR
            target_dir.mkdir(parents=True)
            strm_target = target_dir / "Movie.strm"
            nfo_target = target_dir / "Movie.nfo"
            poster_target = target_dir / "poster.jpg"
            strm_target.write_text("https://tgto.invalid/play/video-1", encoding="utf-8")
            nfo_target.write_text("<movie><title>Movie</title></movie>", encoding="utf-8")
            poster_target.write_bytes(b"tgto-poster")

            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [video]}),
                metadata_exts={"nfo", "jpg"},
            )

            self.assertEqual(stats["generated"], 0)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(
                strm_target.read_text(encoding="utf-8"),
                "https://tgto.invalid/play/video-1",
            )
            self.assertEqual(
                nfo_target.read_text(encoding="utf-8"),
                "<movie><title>Movie</title></movie>",
            )
            self.assertEqual(poster_target.read_bytes(), b"tgto-poster")
            self.assertEqual(db.list_strm_index(f"guangya:{source_id}"), [])
            self.assertEqual(db.list_strm_index(f"guangya-meta:{source_id}"), [])

    def test_incremental_rename_preserves_modified_old_path_and_index(self) -> None:
        from app.modules.strm import sync_strm_incremental

        source_id = f"rename-modified-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        original = GuangYaFile("video-1", "Old.mkv", False, 100, "etag-1", "parent")
        renamed = GuangYaFile("video-1", "New.mkv", False, 100, "etag-2", "parent")

        class _IncrementalClient:
            def file_info(self, file_id):
                return renamed if file_id == renamed.file_id else None

        with tempfile.TemporaryDirectory() as root:
            old_path = generate_strm(original, "电影/Test", "http://localhost:1258", root)
            db.upsert_strm_index(
                source_key, original.file_id, original.etag, original.size, original.name,
                str(old_path), self._fingerprint(old_path.read_bytes()),
            )
            old_path.write_text("user-owned", encoding="utf-8")
            stats = sync_strm_incremental(
                source_id, [{
                    "source_id": source_id, "kind": "video", "action": "upsert",
                    "file_id": renamed.file_id, "rel_dir": "电影/Test",
                    "name": renamed.name, "etag": renamed.etag, "size": renamed.size,
                    "parent_id": renamed.parent_id,
                }], "http://localhost:1258", root, client=_IncrementalClient(),
            )
            rows = db.list_strm_index(source_key)
            new_paths = list((Path(root) / STRM_SUBDIR).rglob("New.strm"))

            self.assertTrue(stats["fallback_required"])
            self.assertEqual(old_path.read_text(encoding="utf-8"), "user-owned")
            self.assertEqual(new_paths, [])
            self.assertEqual(rows[0]["strm_path"], str(old_path))

    def test_invalid_cleanup_preserves_modified_file_and_index(self) -> None:
        source_id = f"cleanup-modified-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "stale.strm"
            target.parent.mkdir(parents=True)
            original = b"mediaflux-owned"
            target.write_bytes(original)
            db.upsert_strm_index(
                source_key, "stale", "etag", len(original), "stale.mkv",
                str(target), self._fingerprint(original),
            )
            target.write_bytes(b"user-owned")

            result = clean_invalid_strm(root, source_key=source_key, valid_ids=set())

            self.assertTrue(result["skipped"])
            self.assertEqual(result["ownership_blocked"], 1)
            self.assertTrue(target.exists())
            self.assertEqual(len(db.list_strm_index(source_key)), 1)

    def test_full_metadata_sync_preserves_locally_modified_target(self) -> None:
        source_id = f"metadata-modified-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        metadata = GuangYaFile("poster", "poster.jpg", False, 8, "etag-2", source_id)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            original = b"original"
            target.write_bytes(original)
            db.upsert_strm_index(
                source_key, metadata.file_id, "etag-1", len(original), metadata.name,
                str(target), self._fingerprint(original),
            )
            target.write_bytes(b"modified")

            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [metadata]}), metadata_exts={"jpg"},
            )
            row = db.list_strm_index(source_key)[0]

            self.assertEqual(stats["metadata_generated"], 0)
            self.assertEqual(stats["metadata_failed"], 1)
            self.assertEqual(target.read_bytes(), b"modified")
            self.assertEqual(row["etag"], "etag-1")

    def test_sync_cancellation_stops_before_generation_and_cleanup(self) -> None:
        source_id = f"cancel-{uuid.uuid4().hex}"
        video = GuangYaFile("video-1", "Movie.mkv", False, 1024, "etag", source_id)
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm.clean_invalid_strm"
        ) as cleanup:
            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [video]}),
                should_stop=lambda: True,
            )

        self.assertTrue(stats["stopped"])
        self.assertEqual(stats["stop_stage"], "scan")
        self.assertEqual(stats["generated"], 0)
        cleanup.assert_not_called()
        self.assertEqual(db.list_strm_index(f"guangya:{source_id}"), [])


class P2SchedulerCancellationTests(IsolatedDatabaseTestCase):
    def test_stopped_source_skips_refresh_and_persists_skipped_run(self) -> None:
        scheduler = STRMScheduler()
        scheduler._run_lock.acquire()
        source = {"id": "source", "name": "来源", "rel_prefix": ""}
        stopped_stats = {
            "total": 1, "generated": 0, "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0,
            "metadata_skipped": 0, "metadata_failed": 0,
            "metadata_cleaned": 0, "cleaned": 0, "clean_skipped": True,
            "empty_dirs_cleaned": 0, "directories": 1,
            "scan_elapsed_seconds": 0.01, "metadata_elapsed_seconds": 0.0,
            "error_samples": [], "changes": [], "omitted_count": 0,
            "stopped": True, "stop_stage": "generate",
        }
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[source]
        ), patch("app.modules.scheduler.clean_retired_strm_sources", return_value={}), patch(
            "app.modules.scheduler.sync_strm", return_value=stopped_stats
        ), patch.object(scheduler, "_refresh_media_servers") as refresh, patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": {
                "GY_STRM_BASE_URL": "http://localhost:1258",
                "STRM_ROOT": "/tmp/strm-root",
            }.get(key, default)
        ):
            result = scheduler._execute_locked("manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        refresh.assert_not_called()
        self.assertEqual(db.get_last_task_run("strm_sync")["status"], "skipped")

class P2RecoveryAndRaceTests(IsolatedDatabaseTestCase):
    def test_organize_request_is_marked_running_before_worker_start(self) -> None:
        manager = OrganizeTaskManager()
        observed = {"started": False}

        class Thread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                observed["started"] = True
                assert any(
                    call.args[0] == 42
                    and call.kwargs.get("organize_status") == "running"
                    and call.kwargs.get("strm_status") == "pending"
                    for call in update.call_args_list
                )

        with patch.object(manager._lock, "acquire", return_value=True), patch.object(
            manager._lock, "release"
        ), patch("app.modules.organize_tasks.Organizer"), patch(
            "app.modules.organize_tasks.db.add_task_run", return_value=77
        ), patch(
            "app.modules.organize_tasks.db.update_download_request"
        ) as update, patch(
            "app.modules.organize_tasks.threading.Thread", Thread
        ):
            result = manager.start(
                [{"id": "source", "name": "来源"}],
                OrganizeRules(target_dir_id="target"),
                trigger_type="download",
                download_request_ids=[42],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], 77)
        self.assertTrue(observed["started"])

    def test_queued_strm_request_is_persisted_before_waiter_start(self) -> None:
        scheduler = STRMScheduler()
        observed = {"started": False}

        class Thread:
            def __init__(self, *args, **kwargs):
                pass

            def is_alive(self):
                return False

            def start(self):
                observed["started"] = True
                assert any(
                    call.args[0] == 42
                    and call.kwargs.get("strm_status") == "queued"
                    for call in update.call_args_list
                )

        with patch(
            "app.modules.scheduler.db.update_download_request"
        ) as update, patch(
            "app.modules.scheduler.threading.Thread", Thread
        ):
            result = scheduler._queue_organize_trigger({"download_request_ids": [42]})

        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertTrue(observed["started"])

    def test_init_db_marks_interrupted_pipeline_states_for_review(self) -> None:
        request_id, _ = db.create_download_request(
            f"recovery-{uuid.uuid4().hex}", "http", title="恢复测试"
        )
        db.update_download_request(
            request_id, organize_started=1, organize_status="running",
            strm_status="queued",
        )
        organize_run = db.add_task_run("guangya_organize", "download")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE task_runs SET result=? WHERE id=?",
                (
                    json.dumps({
                        "task_id": "legacy-task",
                        "current_source": "待整理/动漫",
                        "stats": {"total": 4, "moved": 3},
                        "source_results": [{"id": "root"}],
                    }, ensure_ascii=False),
                    organize_run,
                ),
            )
        strm_run = db.add_task_run("strm_sync", "organize")

        db.init_db()

        row = db.get_download_request(request_id)
        self.assertEqual(row["organize_started"], -1)
        self.assertEqual(row["organize_status"], "failed")
        self.assertEqual(row["strm_status"], "failed")
        with db.get_conn() as conn:
            organize_row = conn.execute(
                "SELECT status,result,error FROM task_runs WHERE id=?", (organize_run,)
            ).fetchone()
            self.assertEqual(organize_row["status"], "failed")
            organize_result = json.loads(organize_row["result"])
            self.assertEqual(organize_result["schema_version"], 1)
            self.assertEqual(organize_result["status"], "failed")
            self.assertEqual(organize_result["task_id"], "legacy-task")
            self.assertEqual(organize_result["current_source"], "待整理/动漫")
            self.assertEqual(organize_result["counters"]["moved"], 3)
            self.assertEqual(organize_result["sources"], [{"id": "root"}])
            self.assertEqual(organize_result["error"], organize_row["error"])
            self.assertEqual(
                conn.execute("SELECT status FROM task_runs WHERE id=?", (strm_run,)).fetchone()[0],
                "failed",
            )

    def test_pending_strm_requests_are_closed_when_scheduler_stops(self) -> None:
        scheduler = STRMScheduler()
        scheduler._pending_organize_options = {"download_request_ids": [41, 42]}
        scheduler._stop_event.set()
        with patch("app.modules.scheduler.db.update_download_request") as update:
            scheduler._wait_and_run_pending_organize()

        self.assertEqual(update.call_count, 2)
        self.assertTrue(all(call.kwargs["strm_status"] == "stopped" for call in update.call_args_list))

    def test_legacy_unfingerprinted_strm_is_fail_closed_on_retirement(self) -> None:
        source_id = f"legacy-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "Legacy.strm"
            target.parent.mkdir(parents=True)
            target.write_text(
                "http://localhost/playgy/file-legacy/etag/1/Legacy.mkv",
                encoding="utf-8",
            )
            db.upsert_strm_index(
                f"guangya:{source_id}", "file-legacy", "etag", 1,
                "Legacy.mkv", str(target), "",
            )
            db.enqueue_strm_retired_source(source_id, "历史来源", root)

            result = clean_retired_strm_sources(set())

            self.assertEqual(result["cleaned"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertTrue(target.exists())

    def test_retirement_checks_stop_between_owned_files(self) -> None:
        source_id = f"stop-retire-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / STRM_SUBDIR
            base.mkdir(parents=True)
            for index in range(2):
                payload = f"owned-{index}".encode()
                target = base / f"{index}.jpg"
                target.write_bytes(payload)
                db.upsert_strm_index(
                    f"guangya-meta:{source_id}", f"file-{index}", "etag",
                    len(payload), target.name, str(target),
                    f"sha256:{hashlib.sha256(payload).hexdigest()}",
                )
            db.enqueue_strm_retired_source(source_id, "待停止来源", root)
            checks = 0

            def should_stop() -> bool:
                nonlocal checks
                checks += 1
                return checks >= 3

            result = clean_retired_strm_sources(set(), should_stop=should_stop)

            self.assertTrue(result["stopped"])
            self.assertTrue(any(path.is_file() for path in base.iterdir()))
            self.assertTrue(any(row["source_id"] == source_id for row in db.list_strm_retired_sources()))

class P2MetadataCancellationTests(IsolatedDatabaseTestCase):
    def test_metadata_cancellation_restores_previous_file_without_failure_ledger(self) -> None:
        from app.modules.strm_metadata_worker import STRMMetadataWorker

        source_id = f"meta-cancel-{uuid.uuid4().hex}"
        remote = GuangYaFile("poster-1", "poster.jpg", False, 7, "new-etag", source_id)

        class Client(_TreeClient):
            pass

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield b"new"
                worker._stop_event.set()
                yield b"data"

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            old_payload = b"old-poster"
            target.write_bytes(old_payload)
            db.upsert_strm_index(
                f"guangya-meta:{source_id}", remote.file_id, "old-etag",
                len(old_payload), remote.name, str(target),
                f"sha256:{hashlib.sha256(old_payload).hexdigest()}",
            )
            client = Client({source_id: [remote]})
            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=client, metadata_exts={"jpg"},
            )
            worker = STRMMetadataWorker()
            worker._client = client
            with patch("app.modules.strm.requests.get", return_value=Response()):
                worked = _process_metadata_once(worker, root)

            self.assertFalse(worked)
            self.assertEqual(stats["metadata_queued"], 1)
            self.assertEqual(stats["metadata_failed"], 0)
            self.assertEqual(target.read_bytes(), old_payload)
            row = db.list_strm_index(f"guangya-meta:{source_id}")[0]
            self.assertEqual(row["etag"], "old-etag")
            self.assertEqual(db.list_strm_failures(status="open"), [])
            self.assertEqual(
                db.list_strm_metadata_queue()[0]["status"], "retry_wait"
            )


class P2OwnershipRaceRegressionTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _fingerprint(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def test_video_changed_after_backup_is_not_overwritten_or_restored(self) -> None:
        import app.modules.strm as strm_module

        source_id = f"video-race-{uuid.uuid4().hex}"
        original = GuangYaFile("video-1", "Movie.mkv", False, 100, "etag-1", source_id)
        changed = GuangYaFile("video-1", "Movie.mkv", False, 200, "etag-2", source_id)
        source_key = f"guangya:{source_id}"
        with tempfile.TemporaryDirectory() as root:
            sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [original]}),
            )
            target = next((Path(root) / STRM_SUBDIR).rglob("*.strm"))
            original_copy = strm_module._copy_backup
            changed_once = {"done": False}

            def copy_then_modify(path):
                backup = original_copy(path)
                if Path(path) == target and not changed_once["done"]:
                    target.write_text("user-during-sync", encoding="utf-8")
                    changed_once["done"] = True
                return backup

            with patch("app.modules.strm._copy_backup", side_effect=copy_then_modify):
                stats = sync_strm(
                    source_id, "http://localhost:1258", root,
                    client=_TreeClient({source_id: [changed]}),
                )

            self.assertEqual(stats["generated"], 0)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "user-during-sync")
            row = db.list_strm_index(source_key)[0]
            self.assertEqual(row["etag"], "etag-1")

    def test_external_write_after_install_is_not_overwritten_by_rollback(self) -> None:

        source_id = f"rollback-race-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        original = GuangYaFile("video-1", "Movie.mkv", False, 100, "etag-1", source_id)
        changed = GuangYaFile("video-1", "Movie.mkv", False, 200, "etag-2", source_id)
        with tempfile.TemporaryDirectory() as root:
            sync_strm(
                source_id, "http://localhost:1258", root,
                client=_TreeClient({source_id: [original]}),
            )
            target = next((Path(root) / STRM_SUBDIR).rglob("*.strm"))
            def fail_after_external_write(*args, **kwargs):
                target.write_text("user-after-install", encoding="utf-8")
                raise RuntimeError("index unavailable")

            with patch(
                "app.modules.strm.db.upsert_strm_index",
                side_effect=fail_after_external_write,
            ):
                stats = sync_strm(
                    source_id, "http://localhost:1258", root,
                    client=_TreeClient({source_id: [changed]}),
                )

            self.assertEqual(stats["generated"], 0)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "user-after-install")
            row = db.list_strm_index(source_key)[0]
            self.assertEqual(row["etag"], "etag-1")

    def test_full_metadata_changed_after_backup_is_not_overwritten_by_rollback(self) -> None:
        import app.modules.strm as strm_module
        from app.modules.strm_metadata_worker import STRMMetadataWorker

        source_id = f"meta-race-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        remote = GuangYaFile("poster", "poster.jpg", False, 8, "etag-2", source_id)

        class Client(_TreeClient):
            def get_download_url(self, file_id):
                return "https://storage.invalid/poster"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield b"new-poster"

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            original = b"old-poster"
            target.write_bytes(original)
            db.upsert_strm_index(
                source_key, remote.file_id, "etag-1", len(original), remote.name,
                str(target), self._fingerprint(original),
            )
            original_copy = strm_module._copy_backup
            changed_once = {"done": False}

            def copy_then_modify(path):
                backup = original_copy(path)
                if Path(path) == target and not changed_once["done"]:
                    target.write_bytes(b"user-during-download")
                    changed_once["done"] = True
                return backup

            client = Client({source_id: [remote]})
            stats = sync_strm(
                source_id, "http://localhost:1258", root,
                client=client, metadata_exts={"jpg"},
            )
            worker = STRMMetadataWorker()
            worker._client = client
            with patch(
                "app.modules.strm._copy_backup", side_effect=copy_then_modify
            ), patch("app.modules.strm.requests.get", return_value=Response()):
                worked = _process_metadata_once(worker, root)

            self.assertTrue(worked)
            self.assertEqual(stats["metadata_generated"], 0)
            self.assertEqual(stats["metadata_queued"], 1)
            self.assertEqual(stats["metadata_failed"], 0)
            self.assertEqual(target.read_bytes(), b"user-during-download")
            row = db.list_strm_index(source_key)[0]
            self.assertEqual(row["etag"], "etag-1")
            self.assertEqual(
                db.list_strm_metadata_queue()[0]["status"], "retry_wait"
            )
            failures = db.list_strm_failures(
                status="open", source_id=source_id, action="metadata"
            )
            self.assertEqual(len(failures), 1)

    def test_invalid_cleanup_rechecks_ownership_immediately_before_delete(self) -> None:
        import app.modules.strm as strm_module

        source_id = f"cleanup-race-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "stale.strm"
            target.parent.mkdir(parents=True)
            owned = b"mediaflux-owned"
            target.write_bytes(owned)
            db.upsert_strm_index(
                source_key, "stale", "etag", len(owned), "stale.mkv",
                str(target), self._fingerprint(owned),
            )
            original_require = strm_module._require_owned_file
            checks = {"count": 0}

            def modify_before_final_check(path, rows, action):
                checks["count"] += 1
                if checks["count"] == 2:
                    Path(path).write_bytes(b"user-before-delete")
                return original_require(path, rows, action)

            with patch(
                "app.modules.strm._require_owned_file",
                side_effect=modify_before_final_check,
            ):
                result = clean_invalid_strm(
                    root, source_key=source_key, valid_ids=set()
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(target.read_bytes(), b"user-before-delete")
            self.assertEqual(len(db.list_strm_index(source_key)), 1)
