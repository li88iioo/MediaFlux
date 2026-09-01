"""本地媒体来源、目标和任务数据库契约。"""
from __future__ import annotations

import threading
import unittest

from app import database as db
from app.modules.local_media_models import LocalMediaSource, LocalMediaTask
from app.modules.process_lock import CrossProcessLock
from tests.support import IsolatedDatabaseTestCase


class LocalMediaDatabaseTests(IsolatedDatabaseTestCase):
    def test_waiting_task_queue_is_oldest_first_beyond_scheduler_batch_limit(self):
        owner = "queue-order-owner"
        source_id = db.create_local_media_source(
            name="queue-order", qb_profile="", qb_path_prefix="",
            local_root="/tmp/queue-order", owner=owner,
        )
        task_ids = [
            db.create_local_media_task(
                source_id,
                "",
                f"/tmp/queue-order/Movie-{index:03d}.mkv",
                owner=owner,
                trigger="manual",
            )
            for index in range(501)
        ]

        waiting = db.list_waiting_local_media_tasks(owner=owner, limit=500)

        self.assertEqual([task.id for task in waiting], task_ids[:500])
        self.assertIn(task_ids[0], {task.id for task in waiting})
        self.assertNotIn(task_ids[-1], {task.id for task in waiting})

    def test_source_media_type_accepts_explicit_nsfw_and_rejects_unknown_values(self):
        source_id = db.create_local_media_source(
            name="adult-source", qb_profile="", qb_path_prefix="",
            local_root="/tmp/adult-source", media_type="nsfw", owner="admin",
        )
        self.assertEqual(
            db.get_local_media_source(source_id, owner="admin").media_type,
            "nsfw",
        )
        with self.assertRaises(ValueError):
            db.create_local_media_source(
                name="bad-source", qb_profile="", qb_path_prefix="",
                local_root="/tmp/bad-source", media_type="unknown", owner="admin",
            )

    def test_source_target_and_task_round_trip(self):
        source_id = db.create_local_media_source(
            name="qB 下载目录 1",
            qb_profile="configured:qb",
            qb_path_prefix="/downloads/1",
            local_root="/mnt/downloads/1",
            enabled=1,
            stable_seconds=300,
            scan_enabled=1,
            scan_interval_minutes=10,
            owner="admin",
        )
        target_id = db.upsert_local_library_target(
            source_id, "movie", "/mnt/media/Movies",
            provider="jellyfin", library_name="电影", library_id="movies",
            server_path="//NAS/Media/Movies", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "hash-1", "/mnt/downloads/1/Movie.mkv", owner="admin"
        )

        source = db.get_local_media_source(source_id, owner="admin")
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertIsInstance(source, LocalMediaSource)
        self.assertIsInstance(task, LocalMediaTask)
        self.assertEqual(source.mode, "move")
        self.assertEqual(
            (source.stable_seconds, source.scan_enabled, source.scan_interval_minutes),
            (0, False, 10),
        )
        self.assertEqual(task.status, "waiting_stable")
        target = db.list_local_library_targets(source_id, owner="admin")[0]
        self.assertEqual(target.id, target_id)
        self.assertEqual(target.library_id, "movies")
        self.assertEqual(target.library_name, "电影")
        self.assertEqual(target.server_path, "//NAS/Media/Movies")
        self.assertTrue(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        self.assertFalse(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "recognizing")

    def test_concurrent_scan_task_creation_reuses_one_active_task(self):
        source_id = db.create_local_media_source(
            name="concurrent-scan", qb_profile="", qb_path_prefix="",
            local_root="/tmp/concurrent-scan", owner="admin",
        )
        workers = 8
        barrier = threading.Barrier(workers + 1)
        task_ids: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def create_task() -> None:
            barrier.wait()
            try:
                task_id = db.create_local_media_task(
                    source_id, "", "/tmp/concurrent-scan/Movie.mkv",
                    owner="admin", trigger="scan",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        threads = [threading.Thread(target=create_task) for _ in range(workers)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(task_ids), workers)
        self.assertEqual(len(set(task_ids)), 1)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM local_media_tasks WHERE source_id=? AND content_path=?",
                (source_id, "/tmp/concurrent-scan/Movie.mkv"),
            ).fetchall()
        self.assertEqual([int(row["id"]) for row in rows], [task_ids[0]])

    def test_active_task_admission_rejects_ancestor_and_descendant_overlap(self):
        source_id = db.create_local_media_source(
            name="path-overlap", qb_profile="", qb_path_prefix="",
            local_root="/tmp/path-overlap", owner="admin",
        )
        parent_id = db.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show", owner="admin", trigger="manual",
        )

        with self.assertRaisesRegex(ValueError, "范围重叠"):
            db.create_local_media_task(
                source_id, "", "/tmp/path-overlap/Show/E01.mkv",
                owner="admin", trigger="manual",
            )

        db.update_local_media_task(parent_id, owner="admin", status="completed")
        child_id = db.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show/E01.mkv",
            owner="admin", trigger="manual",
        )
        with self.assertRaisesRegex(ValueError, "范围重叠"):
            db.create_local_media_task(
                source_id, "", "/tmp/path-overlap/Show",
                owner="admin", trigger="manual",
            )

        db.update_local_media_task(child_id, owner="admin", status="failed")
        retried_parent = db.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show",
            owner="admin", trigger="manual",
        )
        self.assertNotEqual(retried_parent, parent_id)

    def test_generic_task_update_cannot_mutate_content_path_identity(self):
        source_id = db.create_local_media_source(
            name="immutable-task-path", qb_profile="", qb_path_prefix="",
            local_root="/tmp/immutable-task-path", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/immutable-task-path/Movie.mkv", owner="admin",
        )

        with self.assertRaisesRegex(ValueError, "原子准入接口"):
            db.update_local_media_task(
                task_id, owner="admin",
                content_path="/tmp/immutable-task-path/Other.mkv",
            )

        self.assertEqual(
            db.get_local_media_task(task_id, owner="admin").content_path,
            "/tmp/immutable-task-path/Movie.mkv",
        )

    def test_qb_binding_reuses_manual_confirmation_without_resetting_state(self):
        source_id = db.create_local_media_source(
            name="manual-qb-single-track", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/single-track", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/single-track/season/../Movie.mkv",
            owner="admin", trigger="manual", operation_token="manual-operation-token",
        )
        db.update_local_media_task(
            task_id,
            owner="admin",
            status="requires_manual",
            snapshot_digest="snapshot-v1",
            rules_snapshot='{"rule":"manual"}',
            recognition_summary='{"candidate":"42"}',
            tmdb_id="42",
            media_type="tv",
            season_override=2,
            episode_override=7,
            numbering_mode="season_continuous",
            title="保留人工选择",
            year="2026",
            error="等待人工确认",
            warning="保留提示",
        )
        db.add_local_media_task_item(
            task_id,
            "/tmp/single-track/Movie.mkv",
            "/tmp/library/Movie.mkv",
            role="video",
            owner="admin",
        )
        before = db.get_local_media_task(task_id, owner="admin")
        request_id, _ = db.create_download_request("manual-qb-binding", "magnet")

        linked_id, restarted = db.create_and_link_qb_local_media_task(
            request_id,
            source_id,
            "HASH-MANUAL-BINDING",
            "/tmp/single-track/./Movie.mkv",
            owner="admin",
        )

        self.assertEqual(linked_id, task_id)
        self.assertFalse(restarted)
        after = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(after.qb_hash, "hash-manual-binding")
        self.assertEqual(after.content_path, "/tmp/single-track/Movie.mkv")
        self.assertEqual(
            (
                after.status,
                after.trigger,
                after.operation_token,
                after.snapshot_digest,
                after.rules_snapshot,
                after.recognition_summary,
                after.tmdb_id,
                after.media_type,
                after.season_override,
                after.episode_override,
                after.numbering_mode,
                after.title,
                after.year,
                after.error,
                after.warning,
                after.version,
            ),
            (
                before.status,
                before.trigger,
                before.operation_token,
                before.snapshot_digest,
                before.rules_snapshot,
                before.recognition_summary,
                before.tmdb_id,
                before.media_type,
                before.season_override,
                before.episode_override,
                before.numbering_mode,
                before.title,
                before.year,
                before.error,
                before.warning,
                before.version,
            ),
        )
        self.assertEqual(len(db.list_local_media_task_items(task_id, owner="admin")), 1)
        self.assertEqual(
            db.get_download_request(request_id)["local_import_target"],
            f"local-media-task:{task_id}",
        )

    def test_manual_admission_reuses_active_qb_task_by_canonical_path(self):
        source_id = db.create_local_media_source(
            name="qb-manual-single-track", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/qb-manual", owner="admin",
        )
        request_id, _ = db.create_download_request("qb-manual-binding", "magnet")
        qb_task_id, restarted = db.create_and_link_qb_local_media_task(
            request_id,
            source_id,
            "HASH-QB-FIRST",
            "/tmp/qb-manual/Movie.mkv",
            owner="admin",
        )
        manual_task_id = db.create_local_media_task(
            source_id,
            "",
            "/tmp/qb-manual/subdir/../Movie.mkv",
            owner="admin",
            trigger="manual",
        )

        self.assertFalse(restarted)
        self.assertEqual(manual_task_id, qb_task_id)
        task = db.get_local_media_task(qb_task_id, owner="admin")
        self.assertEqual(task.qb_hash, "hash-qb-first")
        self.assertEqual(task.trigger, "qb_completed")

    def test_active_content_path_cannot_be_admitted_by_another_source(self):
        first_source = db.create_local_media_source(
            name="path-owner-one", qb_profile="", qb_path_prefix="",
            local_root="/tmp/shared-path", owner="admin",
        )
        second_source = db.create_local_media_source(
            name="path-owner-two", qb_profile="", qb_path_prefix="",
            local_root="/tmp/shared-path/subdir", owner="admin",
        )
        task_id = db.create_local_media_task(
            first_source,
            "",
            "/tmp/shared-path/Movie.mkv",
            owner="admin",
            trigger="manual",
        )

        with self.assertRaisesRegex(ValueError, "其他本地媒体来源"):
            db.create_local_media_task(
                second_source,
                "",
                "/tmp/shared-path/child/../Movie.mkv",
                owner="admin",
                trigger="manual",
            )
        self.assertEqual(len(db.list_local_media_tasks(owner="admin")), 1)
        self.assertEqual(db.list_local_media_tasks(owner="admin")[0].id, task_id)

    def test_new_qb_request_transfers_hash_to_active_path_task(self):
        source_id = db.create_local_media_source(
            name="terminal-hash-transfer", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/hash-transfer", owner="admin",
        )
        first_request, _ = db.create_download_request("hash-transfer-first", "magnet")
        terminal_id, _ = db.create_and_link_qb_local_media_task(
            first_request,
            source_id,
            "HASH-TRANSFER",
            "/tmp/hash-transfer/Movie.mkv",
            owner="admin",
        )
        db.update_local_media_task(
            terminal_id, owner="admin", status="completed", completed_at=db.now()
        )
        db.update_download_request_for_local_media_task(terminal_id, "completed")
        active_id = db.create_local_media_task(
            source_id,
            "",
            "/tmp/hash-transfer/./Movie.mkv",
            owner="admin",
            trigger="manual",
            operation_token="preserved-manual-token",
        )
        db.update_local_media_task(
            active_id,
            owner="admin",
            status="requires_manual",
            tmdb_id="99",
            title="人工任务",
        )
        before = db.get_local_media_task(active_id, owner="admin")
        second_request, _ = db.create_download_request("hash-transfer-second", "magnet")

        linked_id, restarted = db.create_and_link_qb_local_media_task(
            second_request,
            source_id,
            "hash-transfer",
            "/tmp/hash-transfer/Movie.mkv",
            owner="admin",
        )

        self.assertEqual(linked_id, active_id)
        self.assertFalse(restarted)
        active = db.get_local_media_task(active_id, owner="admin")
        terminal = db.get_local_media_task(terminal_id, owner="admin")
        self.assertEqual(active.qb_hash, "hash-transfer")
        self.assertEqual(
            (active.status, active.operation_token, active.tmdb_id, active.title, active.version),
            (before.status, before.operation_token, before.tmdb_id, before.title, before.version),
        )
        self.assertEqual(terminal.qb_hash, "")
        self.assertEqual(
            db.get_download_request(second_request)["local_import_target"],
            f"local-media-task:{active_id}",
        )

    def test_concurrent_manual_and_qb_admission_create_one_path_task(self):
        source_id = db.create_local_media_source(
            name="concurrent-manual-qb", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/concurrent-manual-qb", owner="admin",
        )
        request_id, _ = db.create_download_request("concurrent-manual-qb", "magnet")
        barrier = threading.Barrier(3)
        task_ids: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def admit_manual() -> None:
            barrier.wait()
            try:
                task_id = db.create_local_media_task(
                    source_id,
                    "",
                    "/tmp/concurrent-manual-qb/a/../Movie.mkv",
                    owner="admin",
                    trigger="manual",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        def admit_qb() -> None:
            barrier.wait()
            try:
                task_id, _ = db.create_and_link_qb_local_media_task(
                    request_id,
                    source_id,
                    "HASH-CONCURRENT-PATH",
                    "/tmp/concurrent-manual-qb/./Movie.mkv",
                    owner="admin",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        threads = [threading.Thread(target=admit_manual), threading.Thread(target=admit_qb)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(task_ids), 2)
        self.assertEqual(len(set(task_ids)), 1)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,qb_hash,content_path FROM local_media_tasks "
                "WHERE owner=? AND source_id=? AND status NOT IN ('completed','failed')",
                ("admin", source_id),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["qb_hash"]), "hash-concurrent-path")
        self.assertEqual(str(rows[0]["content_path"]), "/tmp/concurrent-manual-qb/Movie.mkv")
        self.assertEqual(
            db.get_download_request(request_id)["local_import_target"],
            f"local-media-task:{task_ids[0]}",
        )

    def test_init_db_purges_deprecated_source_fields_but_keeps_schema_columns(self):
        source_id = db.create_local_media_source(
            name="legacy-smb", qb_profile="", qb_path_prefix="",
            local_root="/media/downloads", owner="admin",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_sources SET smb_user=?,smb_pass=?,stable_seconds=300,"
                "scan_enabled=1,scan_interval_minutes=30 WHERE id=?",
                ("nas-user", "nas-secret", source_id),
            )
        db.init_db()
        source = db.get_local_media_source(source_id, owner="admin")
        self.assertEqual((source.smb_user, source.smb_pass), ("", ""))
        self.assertEqual(
            (source.stable_seconds, source.scan_enabled, source.scan_interval_minutes),
            (0, False, 10),
        )
        self.assertNotIn(
            "scan_enabled",
            db.get_local_media_diagnostic_summary(owner="admin")["sources"],
        )
        with db.get_conn() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(local_media_sources)")}
        self.assertTrue({
            "smb_user", "smb_pass", "stable_seconds", "scan_enabled", "scan_interval_minutes",
        }.issubset(columns))

    def test_qb_task_link_is_atomic_and_new_request_restarts_terminal_attempt(self):
        source_id = db.create_local_media_source(
            name="qb-atomic", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root="/mnt/downloads", owner="admin",
        )
        first_request, _ = db.create_download_request("qb-atomic-first", "magnet")
        task_id, restarted = db.create_and_link_qb_local_media_task(
            first_request, source_id, "HASH-ATOMIC", "/mnt/downloads/Movie.mkv", owner="admin",
        )
        self.assertFalse(restarted)
        first_download = db.get_download_request(first_request)
        self.assertEqual(
            first_download["local_import_target"], f"local-media-task:{task_id}",
        )
        self.assertTrue(first_download["local_import_started_at"])

        first_attempt = db.get_local_media_task(task_id, owner="admin")
        db.add_local_media_task_item(
            task_id, "/mnt/downloads/Movie.mkv", "/mnt/library/Movie.mkv",
            role="video", owner="admin",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="completed", completed_at=db.now(),
        )
        db.update_download_request_for_local_media_task(task_id, "completed")

        second_request, _ = db.create_download_request("qb-atomic-second", "magnet")
        reused_id, restarted = db.create_and_link_qb_local_media_task(
            second_request, source_id, "hash-atomic",
            "/mnt/downloads/readded/Movie.mkv", owner="admin",
        )
        self.assertEqual(reused_id, task_id)
        self.assertTrue(restarted)
        retried = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.content_path, "/mnt/downloads/readded/Movie.mkv")
        self.assertNotEqual(retried.operation_token, first_attempt.operation_token)
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin"), [])
        self.assertEqual(
            db.get_download_request(second_request)["local_import_target"],
            f"local-media-task:{task_id}",
        )
        self.assertEqual(
            db.get_download_request(first_request)["local_import_status"], "completed",
        )

        db.update_local_media_task(
            task_id, owner="admin", status="completed", completed_at=db.now(),
        )
        same_id, restarted = db.create_and_link_qb_local_media_task(
            second_request, source_id, "hash-atomic",
            "/mnt/downloads/readded/Movie.mkv", owner="admin",
        )
        self.assertEqual(same_id, task_id)
        self.assertFalse(restarted)
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "completed")

    def test_qb_hash_is_idempotent_and_owner_isolated(self):
        source_id = db.create_local_media_source(
            name="source-idempotent", qb_profile="qb", qb_path_prefix="/downloads",
            local_root="/mnt/downloads", owner="admin",
        )
        first = db.create_local_media_task(source_id, "HASH", "/mnt/downloads/A", owner="admin")
        second = db.create_local_media_task(source_id, "hash", "/mnt/downloads/A", owner="admin")
        self.assertEqual(first, second)
        self.assertIsNone(db.get_local_media_source(source_id, owner="other"))
        self.assertIsNone(db.get_local_media_task(first, owner="other"))
        self.assertEqual(db.list_local_media_tasks(owner="other"), [])

    def test_task_items_and_atomic_concurrent_claim(self):
        source_id = db.create_local_media_source(
            name="source-items", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-items", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads/A.mkv", owner="admin", trigger="manual"
        )
        item_id = db.add_local_media_task_item(
            task_id, "/tmp/downloads/A.mkv", "/tmp/library/A.mkv",
            role="video", size=123, owner="admin",
        )
        self.assertGreater(item_id, 0)
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin")[0]["role"], "video")

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def claim() -> None:
            barrier.wait()
            results.append(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_selected_log_clear_skips_busy_tasks_and_cascades_children(self):
        source_id = db.create_local_media_source(
            name="source-clear", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-clear", owner="admin",
        )
        completed_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Done.mkv", owner="admin", trigger="manual",
        )
        db.add_local_media_task_item(
            completed_id, "/tmp/downloads-clear/Done.mkv", "/tmp/library/Done.mkv",
            role="video", owner="admin",
        )
        completed = db.get_local_media_task(completed_id, owner="admin")
        db.add_local_media_operation_step(
            completed_id, completed.operation_token, 0, "move", owner="admin",
        )
        db.update_local_media_task(completed_id, owner="admin", status="completed")
        busy_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Busy.mkv", owner="admin", trigger="scan",
        )

        result = db.delete_local_media_tasks([completed_id, busy_id, 999999], owner="admin")

        self.assertEqual(result, {"requested": 3, "deleted": 1, "skipped_busy": 1, "missing": 1})
        self.assertIsNone(db.get_local_media_task(completed_id, owner="admin"))
        self.assertEqual(db.list_local_media_task_items(completed_id, owner="admin"), [])
        self.assertEqual(db.list_local_media_operation_steps(completed_id, owner="admin"), [])
        self.assertIsNotNone(db.get_local_media_task(busy_id, owner="admin"))

    def test_invalid_status_category_and_cross_owner_target_are_rejected(self):
        source_id = db.create_local_media_source(
            name="source-validation", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-validation", owner="admin",
        )
        with self.assertRaisesRegex(ValueError, "分类"):
            db.upsert_local_library_target(source_id, "music", "/tmp/music", owner="admin")
        with self.assertRaisesRegex(LookupError, "来源"):
            db.upsert_local_library_target(source_id, "movie", "/tmp/movies", owner="other")
        task_id = db.create_local_media_task(source_id, "x", "/tmp/downloads/x", owner="admin")
        with self.assertRaisesRegex(ValueError, "状态"):
            db.update_local_media_task(task_id, owner="admin", status="running")

    def test_source_bundle_rolls_back_fields_and_targets_on_database_failure(self):
        before_ids = {item.id for item in db.list_local_media_sources(owner="admin")}
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_tv_target BEFORE INSERT ON local_library_targets "
                "WHEN NEW.category='tv' BEGIN SELECT RAISE(ABORT, 'simulated target failure'); END"
            )
        try:
            with self.assertRaisesRegex(Exception, "simulated target failure"):
                db.save_local_media_source_bundle(
                    name="atomic", qb_profile="configured:qb", qb_path_prefix="/downloads",
                    local_root="/mnt/downloads", enabled=True, stable_seconds=0, scan_enabled=False,
                    scan_interval_minutes=10, media_type="auto", mode="move", owner="admin",
                    targets=[
                        {"category": "movie", "path": "/media/movies"},
                        {"category": "tv", "path": "/media/tv"},
                    ],
                )
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER IF EXISTS fail_tv_target")
        sources = db.list_local_media_sources(owner="admin")
        self.assertEqual({item.id for item in sources}, before_ids)
        self.assertNotIn("atomic", {item.name for item in sources})

    def test_active_source_allows_name_only_change_but_freezes_runtime_configuration(self):
        source_id = db.save_local_media_source_bundle(
            name="active-source", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root="/tmp/active-source", enabled=True, media_type="auto", mode="move",
            owner="admin",
            targets=[{"category": "movie", "path": "/tmp/library/movies"}],
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/active-source/Movie.mkv", owner="admin",
        )

        self.assertEqual(
            db.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies"}],
            ),
            source_id,
        )
        self.assertEqual(
            db.get_local_media_source(source_id, owner="admin").name,
            "renamed-source",
        )

        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            db.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source-new", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies"}],
            )
        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            db.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies-new"}],
            )
        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            db.update_local_media_source(source_id, owner="admin", media_type="movie")

        db.update_local_media_task(task_id, owner="admin", status="completed")
        self.assertTrue(
            db.update_local_media_source(source_id, owner="admin", media_type="movie")
        )
        self.assertEqual(
            db.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="movie", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies-new"}],
            ),
            source_id,
        )

    def test_manual_task_persists_position_overrides_and_retry_keeps_selection(self):
        source_id = db.create_local_media_source(
            name="manual-position", qb_profile="", qb_path_prefix="",
            local_root="/tmp/manual-position", media_type="tv", owner="admin",
        )
        task_id = db.prepare_manual_local_media_task(
            source_id, "/tmp/manual-position/Show.S01E07.mkv", owner="admin",
            tmdb_id="42", media_type="tv", season_override=2, episode_override=7,
            numbering_mode="season_continuous",
        )
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual((task.season_override, task.episode_override), (2, 7))
        self.assertEqual(task.numbering_mode, "season_continuous")
        db.add_local_media_task_item(
            task_id, "/tmp/manual-position/Show.S01E07.mkv",
            "/tmp/library/Show.S01E07.mkv", role="video", owner="admin",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="requires_manual", title="旧标题", year="2025",
            recognition_summary='{"schema_version":1,"media":[]}',
        )
        previous_token = db.get_local_media_task(task_id, owner="admin").operation_token
        self.assertTrue(db.reset_local_media_task(task_id, owner="admin"))
        retried = db.get_local_media_task(task_id, owner="admin")
        self.assertNotEqual(retried.operation_token, previous_token)
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin"), [])
        self.assertEqual(retried.recognition_summary, "")
        self.assertEqual((retried.title, retried.year), ("", ""))
        self.assertEqual(retried.tmdb_id, "42")
        self.assertEqual(retried.media_type, "tv")
        self.assertEqual((retried.season_override, retried.episode_override), (2, 7))
        self.assertEqual(retried.numbering_mode, "season_continuous")

    def test_interrupted_write_retry_requires_explicit_confirmation(self):
        source_id = db.create_local_media_source(
            name="interrupted-write", qb_profile="", qb_path_prefix="",
            local_root="/tmp/interrupted-write", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/interrupted-write/Movie.mkv", owner="admin",
        )
        db.add_local_media_task_item(
            task_id, "/tmp/interrupted-write/Movie.mkv",
            "/tmp/library/Movie.mkv", role="video", owner="admin",
        )
        db.update_local_media_task(task_id, owner="admin", status="moving")

        db.init_db()

        recovered = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(recovered.status, "requires_manual")
        self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
        self.assertFalse(recovered.completed_at)
        self.assertFalse(db.reset_local_media_task(task_id, owner="admin"))
        self.assertEqual(len(db.list_local_media_task_items(task_id, owner="admin")), 1)

        self.assertTrue(db.reset_local_media_task(
            task_id, owner="admin", confirm_interrupted_write=True,
        ))
        self.assertEqual(
            db.get_local_media_task(task_id, owner="admin").status,
            "waiting_stable",
        )
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin"), [])

    def test_init_db_defers_local_recovery_until_pipeline_writer_releases(self):
        source_id = db.create_local_media_source(
            name="live-writer", qb_profile="", qb_path_prefix="",
            local_root="/tmp/live-writer", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "a" * 40, "/tmp/live-writer/Movie.mkv", owner="admin",
        )
        task = db.get_local_media_task(task_id, owner="admin")
        step_id = db.add_local_media_operation_step(
            task_id, task.operation_token, 0, "move", owner="admin",
        )
        db.update_local_media_task(task_id, owner="admin", status="moving")
        db.update_local_media_operation_step(step_id, "running")
        live_writer = CrossProcessLock(
            "local-media-pipeline-write", directory=db.resolve_db_path().parent,
        )
        self.assertTrue(live_writer.acquire(blocking=False))
        try:
            db.init_db()
            active = db.get_local_media_task(task_id, owner="admin")
            active_steps = db.list_local_media_operation_steps(task_id, owner="admin")
            self.assertEqual(active.status, "moving")
            self.assertEqual(active_steps[0]["status"], "running")
        finally:
            live_writer.release()

        # 下一次启动检查能够拿到 writer，才把真正的 orphan 收束为人工核验。
        db.init_db()
        recovered = db.get_local_media_task(task_id, owner="admin")
        recovered_steps = db.list_local_media_operation_steps(task_id, owner="admin")
        self.assertEqual(recovered.status, "requires_manual")
        self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
        self.assertEqual(recovered_steps[0]["status"], "failed")

    def test_init_db_keeps_all_postwrite_interruptions_for_manual_review(self):
        source_id = db.create_local_media_source(
            name="postwrite", qb_profile="", qb_path_prefix="",
            local_root="/tmp/postwrite", owner="admin",
        )
        task_ids = []
        for status in ("moving", "verifying", "refreshing", "rolling_back"):
            task_id = db.create_local_media_task(
                source_id, "", f"/tmp/postwrite/{status}.mkv", owner="admin",
            )
            db.add_local_media_task_item(
                task_id, f"/tmp/postwrite/{status}.mkv",
                f"/tmp/library/{status}.mkv", role="video", owner="admin",
            )
            db.update_local_media_task(task_id, owner="admin", status=status)
            task_ids.append(task_id)

        db.init_db()

        for task_id in task_ids:
            recovered = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(recovered.status, "requires_manual")
            self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
            self.assertEqual(
                len(db.list_local_media_task_items(task_id, owner="admin")), 1,
            )

    def test_init_db_keeps_prewrite_interruptions_retryable(self):
        source_id = db.create_local_media_source(
            name="prewrite", qb_profile="", qb_path_prefix="",
            local_root="/tmp/prewrite", owner="admin",
        )
        task_ids = []
        for status in ("recognizing", "planned"):
            task_id = db.create_local_media_task(
                source_id, "", f"/tmp/prewrite/{status}.mkv", owner="admin",
            )
            db.update_local_media_task(task_id, owner="admin", status=status)
            task_ids.append(task_id)

        db.init_db()

        for task_id in task_ids:
            recovered = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(recovered.status, "failed")
            self.assertIn("可重试", recovered.error)
            self.assertTrue(db.reset_local_media_task(task_id, owner="admin"))

    def test_task_recognition_summary_round_trip_is_versioned(self):
        source_id = db.create_local_media_source(
            name="recognition-summary", qb_profile="", qb_path_prefix="",
            local_root="/tmp/recognition-summary", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/recognition-summary/Show.S02E08.mkv",
            owner="admin", trigger="scan",
        )
        before = db.get_local_media_task(task_id, owner="admin")
        summary = '{"schema_version":1,"media":[]}'
        self.assertTrue(db.update_local_media_task(
            task_id, owner="admin", recognition_summary=summary,
        ))
        after = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(after.recognition_summary, summary)
        self.assertEqual(after.version, before.version + 1)
        self.assertEqual(
            db.list_local_media_tasks(owner="admin")[0].recognition_summary, summary,
        )

    def test_local_confirmation_claim_is_versioned_and_atomic(self):
        source_id = db.create_local_media_source(
            name="telegram-confirm", qb_profile="", qb_path_prefix="",
            local_root="/tmp/telegram-confirm", media_type="movie", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/telegram-confirm/Movie.mkv",
            owner="admin", trigger="scan",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="requires_manual", snapshot_digest="digest-1"
        )
        task = db.get_local_media_task(task_id, owner="admin")

        self.assertFalse(db.claim_local_media_confirmation_task(
            task_id,
            owner="admin",
            expected_version=task.version + 1,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
        ))
        self.assertTrue(db.claim_local_media_confirmation_task(
            task_id,
            owner="admin",
            expected_version=task.version,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
            title="电影甲",
            year="2026",
        ))
        claimed = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(claimed.status, "recognizing")
        self.assertEqual((claimed.tmdb_id, claimed.media_type), ("101", "movie"))
        self.assertEqual((claimed.title, claimed.year), ("电影甲", "2026"))

    def test_prepare_manual_retry_clears_previous_attempt_items(self):
        source_id = db.create_local_media_source(
            name="manual-retry", qb_profile="", qb_path_prefix="",
            local_root="/tmp/manual-retry", owner="admin",
        )
        content_path = "/tmp/manual-retry/Movie.mkv"
        task_id = db.prepare_manual_local_media_task(
            source_id, content_path, owner="admin", tmdb_id="1", media_type="movie",
        )
        original = db.get_local_media_task(task_id, owner="admin")
        db.add_local_media_task_item(
            task_id, content_path, "/tmp/library/Movie.mkv", role="video", owner="admin",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="failed", error="move failed",
            title="旧电影", year="2025",
        )

        reused_id = db.prepare_manual_local_media_task(
            source_id, content_path, owner="admin", tmdb_id="2", media_type="movie",
        )
        retried = db.get_local_media_task(reused_id, owner="admin")
        self.assertEqual(reused_id, task_id)
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.tmdb_id, "2")
        self.assertEqual((retried.title, retried.year), ("", ""))
        self.assertNotEqual(retried.operation_token, original.operation_token)
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin"), [])

    def test_manual_task_prepare_never_resets_an_active_task(self):
        source_id = db.create_local_media_source(
            name="manual-race", qb_profile="", qb_path_prefix="", local_root="/tmp/manual", owner="admin"
        )
        task_id = db.prepare_manual_local_media_task(
            source_id, "/tmp/manual/Movie.mkv", owner="admin", tmdb_id="1", media_type="movie"
        )
        self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
        with self.assertRaisesRegex(ValueError, "正在处理中"):
            db.prepare_manual_local_media_task(
                source_id, "/tmp/manual/Movie.mkv", owner="admin", tmdb_id="2", media_type="movie"
            )
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(task.status, "recognizing")
        self.assertEqual(task.tmdb_id, "1")


if __name__ == "__main__":
    unittest.main()
