"""绑定快照、坏上游响应和刷新消费者在 SQLite/ZIP 恢复后的业务闭环。"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.modules.backup import create_backup, recover_pending_restore, restore_backup
from app.modules.local_media_service import LocalMediaService
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.repositories import media_refresh_queue as queue
from app.routes import media_libraries_api as api
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database
from tests.test_deep_audit_media_chain import MediaEndpoint, Response


class MediaRefreshHistoryAuditTests(unittest.TestCase):
    def test_bound_refresh_backup_restore_and_real_worker_restart_do_not_replay(self):
        with isolated_test_database("mediaflux.db") as database_path:
            root = database_path.parent
            paths = RuntimePaths(
                root / "program",
                root,
                root,
                root / "cache",
                root / "logs",
                root / "strm",
                root / "trash",
            )
            paths.ensure_writable_dirs()
            paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
            source_id = db.create_local_media_source(
                name="历史来源",
                qb_profile="",
                qb_path_prefix="",
                local_root="/synthetic/downloads",
                owner="admin",
            )
            db.upsert_local_library_target(
                source_id,
                "movie",
                "/media/Movies",
                provider="jellyfin",
                library_id="audit-library",
                library_name="审查库",
                owner="admin",
            )
            initial_bindings = api._local_bindings()
            plan = SimpleNamespace(
                provider="jellyfin",
                library_id="audit-library",
                library_name="审查库",
                target=Path("/media/Movies/Film-A/Film.mkv"),
            )
            with patch(
                "app.modules.media_refresh_coordinator.get_media_refresh_coordinator"
            ):
                self.assertEqual(LocalMediaService._refresh_plans([plan]), [])

            endpoint = MediaEndpoint("/media/Movies")
            old_worker = MediaRefreshCoordinator()
            first = queue.claim_due_media_refreshes(
                owner=old_worker._owner, force=True
            )[0]
            with (
                patch.object(old_worker, "_client_for", side_effect=endpoint.client),
                patch.object(endpoint, "get", return_value=Response({"Items": None})),
            ):
                old_worker._process_group(first)
            self.assertEqual(endpoint.posts, [])
            self.assertEqual(queue.media_refresh_queue_status()["retry_wait"], 1)
            old_lease = queue.claim_due_media_refreshes(
                owner=old_worker._owner, force=True
            )[0]
            plan.target = Path("/media/Movies/Film-B/Film.mkv")
            with patch(
                "app.modules.media_refresh_coordinator.get_media_refresh_coordinator"
            ):
                self.assertEqual(LocalMediaService._refresh_plans([plan]), [])
            saved_state = db.kv_get(queue._QUEUE_KEY)
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 2)
            archive = create_backup(paths)

            queue.clear_media_refresh_queue()
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE local_media_sources SET name='备份后变更' WHERE id=?",
                    (source_id,),
                )
                conn.execute(
                    "UPDATE local_library_targets SET path='/after' WHERE source_id=?",
                    (source_id,),
                )
            restore_backup(paths, archive)
            db.configure_database(database_path, test_mode=True)
            db.init_db()
            self.assertFalse(recover_pending_restore(paths))
            self.assertEqual(api._local_bindings(), initial_bindings)
            self.assertEqual(db.kv_get(queue._QUEUE_KEY), saved_state)

            worker = MediaRefreshCoordinator()
            entered_post = threading.Event()
            release_post = threading.Event()
            original_post = endpoint.post

            def pause_post(*args, **kwargs):
                entered_post.set()
                if not release_post.wait(5):
                    raise AssertionError("refresh post gate timed out")
                return original_post(*args, **kwargs)

            with (
                patch.object(worker, "_client_for", side_effect=endpoint.client),
                patch.object(endpoint, "post", side_effect=pause_post),
                patch("app.services.clear_dashboard_cache"),
            ):
                try:
                    worker.start()
                    self.assertTrue(entered_post.wait(5))
                    self.assertFalse(
                        queue.settle_media_refresh(
                            old_lease["group_key"], outcome="completed",
                            owner=old_worker._owner,
                            lease_generation=old_lease["lease_generation"],
                        )
                    )
                    active_thread = worker._thread
                    self.assertFalse(worker.stop(timeout=0.1))
                    worker.start()
                    self.assertIs(worker._thread, active_thread)
                    self.assertTrue(worker._stop_event.is_set())
                finally:
                    release_post.set()
                    self.assertTrue(worker.stop(timeout=5))
                self.assertEqual(queue.media_refresh_queue_status()["paths"], 0)
                self.assertEqual(worker.status()["completed_session"], 1)
                self.assertEqual(endpoint.posts, ["/Items/audit-library/Refresh"])
                try:
                    worker.start()
                finally:
                    self.assertTrue(worker.stop(timeout=5))
            db.init_db()
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 0)
            self.assertEqual(
                queue.recent_media_refresh_target_ids("jellyfin"), ("audit-library",)
            )
            self.assertEqual(endpoint.posts, ["/Items/audit-library/Refresh"])
            self.assertEqual(api._local_bindings(), initial_bindings)

    def test_invalid_historical_queue_is_not_silently_rewritten_on_recovery(self):
        with isolated_test_database("mediaflux.db"):
            for raw in (
                '{"version":1,"groups":',
                '{"version":1,"groups":[],"recent":{}}',
                '{"version":2,"groups":{},"recent":{}}',
            ):
                with self.subTest(raw=raw):
                    db.kv_set(queue._QUEUE_KEY, raw)
                    db.init_db()
                    with self.assertRaises(RuntimeError):
                        queue.recover_media_refresh_leases()
                    self.assertEqual(db.kv_get(queue._QUEUE_KEY), raw)
