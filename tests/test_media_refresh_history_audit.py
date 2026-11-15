"""绑定快照、坏上游响应和刷新消费者在 SQLite/ZIP 恢复后的业务闭环。"""

from __future__ import annotations

import os
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from app import config
from app import database as db
from app.repositories import local_media as local_media_repo
from app.modules.backup import create_backup, recover_pending_restore, restore_backup
from app.modules.local_media_service import LocalMediaService, LocalMovePlan
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator, _configuration_revisions
from app.modules.media_server_path_mapping import MediaServerPathMapping, parse_media_server_path_mappings
from app.repositories import media_refresh_queue as queue
from app.routes import media_libraries_api as api
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database
from tests.test_deep_audit_media_chain import MediaEndpoint, Response


class MediaRefreshHistoryAuditTests(unittest.TestCase):
    def test_mapping_repair_during_inflight_lookup_is_picked_up_by_real_worker(self):
        with isolated_test_database("mediaflux.db"):
            queue.enqueue_media_refresh("jellyfin", ["/mf-absent-media/A/E01.strm"], debounce_seconds=0)
            endpoint = MediaEndpoint("//nas/strm", series={"series-a": "//nas/strm/A"})
            worker = MediaRefreshCoordinator()
            lookup_finished, release_lookup, refreshed = threading.Event(), threading.Event(), threading.Event()
            mapping = {"value": ""}
            original_get, original_post = config.get, endpoint.post
            calls = []

            def client_for(provider):
                client = endpoint.client(provider)
                client.path_mappings = parse_media_server_path_mappings(mapping["value"])
                refresh = client.refresh_for_paths

                def controlled_refresh(*args, **kwargs):
                    result = refresh(*args, **kwargs)
                    calls.append(result)
                    if len(calls) == 1:
                        lookup_finished.set()
                        if not release_lookup.wait(5):
                            raise AssertionError("configuration repair gate timed out")
                    return result

                client.refresh_for_paths = controlled_refresh
                return client

            def post(*args, **kwargs):
                result = original_post(*args, **kwargs)
                refreshed.set()
                return result

            with patch.object(worker, "_client_for", side_effect=client_for), patch.object(
                endpoint, "post", side_effect=post,
            ), patch.object(config, "get", side_effect=lambda key, default="": (
                mapping["value"] if key == "JELLYFIN_PATH_MAPPINGS" else original_get(key, default)
            )):
                try:
                    worker.start()
                    self.assertTrue(lookup_finished.wait(5))
                    mapping["value"] = json.dumps([{"local": "/mf-absent-media", "server": "//nas/strm"}])
                    release_lookup.set()
                    self.assertTrue(refreshed.wait(5))
                finally:
                    release_lookup.set()
                    self.assertTrue(worker.stop(timeout=5))
            self.assertEqual(len(calls), 2)
            self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 0)

    def test_unmapped_retry_does_not_postpone_new_pending_refresh_after_restart(self):
        with isolated_test_database("mediaflux.db"):
            old, new = "/unmapped/B", "/mf-absent-media/A/new.strm"
            queue.enqueue_media_refresh("jellyfin", [old], debounce_seconds=0, now_epoch=50)
            # 保留实际的 v1 队列状态：历史失败已经达到 16 分钟退避。
            for now in range(50, 100, 10):
                lease = queue.claim_due_media_refreshes(owner="old", now_epoch=now)[0]
                queue.settle_media_refresh(
                    lease["group_key"], owner="old", lease_generation=lease["lease_generation"],
                    outcome="failed", delay_seconds=5, now_epoch=now,
                )
            worker = MediaRefreshCoordinator()
            lease = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)[0]
            self.assertEqual(lease["attempts"], 5)
            endpoint = MediaEndpoint("/mf-absent-media", series={"series-a": "/mf-absent-media/A"})
            client = endpoint.client("jellyfin")
            refresh = client.refresh_for_paths

            def refresh_with_new_pending(*args, **kwargs):
                queue.enqueue_media_refresh("jellyfin", [new], debounce_seconds=20, now_epoch=101)
                return refresh(*args, **kwargs)

            with patch.object(worker, "_client_for", return_value=client), patch.object(
                client, "refresh_for_paths", side_effect=refresh_with_new_pending,
            ), patch("app.repositories.media_refresh_queue.time.time", return_value=102), patch(
                "app.modules.media_refresh_coordinator._retry_seconds", return_value=960,
            ):
                worker._process_group(lease)
            self.assertEqual(endpoint.posts, [])

            db.init_db()
            worker = MediaRefreshCoordinator()
            self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=120), [])
            pending = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=121)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["paths"], [new])
            with patch.object(worker, "_client_for", side_effect=endpoint.client), patch(
                "app.repositories.media_refresh_queue.time.time", return_value=121,
            ):
                worker._process_group(pending[0])
            self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
            remaining = queue.claim_due_media_refreshes(owner=worker._owner, force=True, now_epoch=122)
            self.assertEqual(remaining, [])
            self.assertEqual(queue.list_waiting_media_refreshes()[0]["paths"], [old])

    def test_unmapped_deleted_or_inaccessible_file_recovers_after_mapping_and_restart(self):
        for provider in ("jellyfin", "emby"):
            for inaccessible in (False, True):
                with (
                    self.subTest(provider=provider, inaccessible=inaccessible),
                    isolated_test_database("mediaflux.db") as database_path,
                ):
                    root = database_path.parent / "strm"
                    changed = root / "A/S01E01.strm"
                    changed.parent.mkdir(parents=True)
                    if inaccessible:
                        changed.write_text("http://fixture.invalid/play")
                    endpoint = MediaEndpoint("//nas/strm", series={"series-a": "//nas/strm/A"})
                    original_get, original_lstat = config.get, os.lstat

                    def lstat(path, *args, **kwargs):
                        if inaccessible and os.fspath(path) == str(changed):
                            raise PermissionError("directory is not searchable")
                        return original_lstat(path, *args, **kwargs)

                    queue.enqueue_media_refresh(provider, [str(changed)], debounce_seconds=0, now_epoch=100)
                    worker = MediaRefreshCoordinator()
                    first = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)[0]
                    with patch.object(worker, "_client_for", side_effect=endpoint.client), patch(
                        "app.modules.media_refresh_coordinator.config.get",
                        side_effect=lambda key, default="": str(root) if key == "STRM_ROOT" else original_get(key, default),
                    ), patch("os.lstat", side_effect=lstat), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=100,
                    ), patch("app.modules.media_refresh_coordinator._retry_seconds", return_value=30):
                        worker._process_group(first)
                    self.assertEqual(endpoint.posts, [])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=101)["paths"], 1)
                    self.assertEqual(changed.exists(), inaccessible)

                    # 补映射并重启后必须继续提交父级刷新，清除旧条目或更新仍存在的文件。
                    db.init_db()
                    worker = MediaRefreshCoordinator()
                    original_get = config.get
                    with patch.object(config, "get", side_effect=lambda key, default="": (
                        f'{root} => //nas/strm' if key == f"{provider.upper()}_PATH_MAPPINGS"
                        else original_get(key, default)
                    )):
                        self.assertEqual(queue.reconcile_media_refresh_configuration(
                            _configuration_revisions(), now_epoch=130,
                        ), 1)
                    retry = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=130)[0]
                    client = endpoint.client(provider)
                    client.path_mappings = (MediaServerPathMapping(str(root), "//nas/strm"),)
                    with patch.object(worker, "_client_for", return_value=client), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=130,
                    ):
                        worker._process_group(retry)
                    self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=131)["paths"], 0)

    def test_unmapped_path_does_not_replay_accepted_refresh_after_restart(self):
        with isolated_test_database("mediaflux.db"):
            for provider in ("jellyfin", "emby"):
                with self.subTest(provider=provider):
                    queue.clear_media_refresh_queue()
                    # 媒体库根取本机不存在的路径：宿主机恰好有 /media 时，假单集会被当作本机已删除。
                    endpoint = MediaEndpoint("/mf-absent-media", series={"series-a": "/mf-absent-media/A"})
                    queue.enqueue_media_refresh(
                        provider, ["/mf-absent-media/A/S01E01.strm", "/unmapped/B"],
                        debounce_seconds=0, now_epoch=100,
                    )
                    worker = MediaRefreshCoordinator()
                    first = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)[0]
                    with patch.object(worker, "_client_for", side_effect=endpoint.client), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=100,
                    ), patch("app.modules.media_refresh_coordinator._retry_seconds", return_value=960):
                        worker._process_group(first)
                    self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=101)["paths"], 1)

                    db.init_db()
                    worker = MediaRefreshCoordinator()
                    self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=1060), [])
                    self.assertEqual(queue.list_waiting_media_refreshes()[0]["paths"], ["/unmapped/B"])
                    # 用户显式重新核对；仍然只重新检查未匹配部分。
                    queue.resolve_waiting_media_refreshes(queue.list_waiting_media_refreshes(), "retry", now_epoch=1060)
                    retry = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=1060)[0]
                    self.assertEqual(retry["paths"], ["/unmapped/B"])
                    with patch.object(worker, "_client_for", side_effect=endpoint.client), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=1060,
                    ):
                        worker._process_group(retry)
                    self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=1061)["paths"], 1)

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
            source_id = local_media_repo.create_local_media_source(
                name="历史来源",
                qb_profile="",
                qb_path_prefix="",
                local_root="/synthetic/downloads",
                owner="admin",
            )
            local_media_repo.upsert_local_library_target(
                source_id,
                "movie",
                "/media/Movies",
                provider="jellyfin",
                library_id="audit-library",
                library_name="审查库",
                owner="admin",
            )
            initial_bindings = api._local_bindings()
            queue.enqueue_media_refresh("emby", ["/unmapped/deleted.strm"], debounce_seconds=0)
            waiting_lease = queue.claim_due_media_refreshes(owner="paused")[0]
            revision = _configuration_revisions()
            queue.settle_media_refresh(
                waiting_lease["group_key"], owner="paused", lease_generation=waiting_lease["lease_generation"],
                outcome="completed", waiting_paths=waiting_lease["paths"], waiting_reason="缺少路径映射",
                configuration_revision=revision["emby"],
            )
            waiting_snapshot = queue.list_waiting_media_refreshes()
            plan = SimpleNamespace(
                provider="jellyfin",
                library_id="audit-library",
                library_name="审查库",
                target=Path("/media/Movies/Film-A/Film.mkv"),
            )
            with patch(
                "app.modules.media_refresh_coordinator.get_media_refresh_coordinator"
            ):
                self.assertEqual(LocalMediaService._refresh_plans([cast(LocalMovePlan, plan)]), [])

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
                self.assertEqual(LocalMediaService._refresh_plans([cast(LocalMovePlan, plan)]), [])
            saved_state = db.kv_get(queue._QUEUE_KEY)
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 3)
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
            self.assertEqual(queue.list_waiting_media_refreshes(), waiting_snapshot)
            self.assertEqual(queue.reconcile_media_refresh_configuration(_configuration_revisions()), 0)

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
                self.assertEqual(queue.media_refresh_queue_status()["paths"], 1)
                self.assertEqual(worker.status()["completed_session"], 1)
                self.assertEqual(endpoint.posts, ["/Items/audit-library/Refresh"])
                try:
                    worker.start()
                finally:
                    self.assertTrue(worker.stop(timeout=5))
            db.init_db()
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 1)
            self.assertEqual(
                queue.recent_media_refresh_target_ids("jellyfin"), ("audit-library",)
            )
            self.assertEqual(endpoint.posts, ["/Items/audit-library/Refresh"])
            self.assertEqual(queue.list_waiting_media_refreshes(), waiting_snapshot)
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
