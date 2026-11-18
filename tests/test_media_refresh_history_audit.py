"""绑定快照、坏上游响应和刷新消费者在 SQLite/ZIP 恢复后的业务闭环。"""

from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from app import database as db
from app.modules.backup import create_backup, recover_pending_restore, restore_backup
from app.modules.local_media_service import LocalMediaService, LocalMovePlan
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.modules.media_server_path_mapping import MediaServerPathMapping
from app.repositories import local_media as local_media_repo
from app.repositories import media_refresh_queue as queue
from app.routes import media_libraries_api as api
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database
from tests.test_deep_audit_media_chain import MediaEndpoint, Response


class MediaRefreshHistoryAuditTests(unittest.TestCase):
    def test_unmapped_failure_preserves_new_pending_refresh_after_restart(self):
        with isolated_test_database("mediaflux.db"):
            old, new = "/unmapped/B", "/mf-absent-media/A/new.strm"
            queue.enqueue_media_refresh("jellyfin", [old], debounce_seconds=0, now_epoch=100)
            worker = MediaRefreshCoordinator()
            lease = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)[0]
            endpoint = MediaEndpoint("/mf-absent-media", series={"series-a": "/mf-absent-media/A"})
            client = endpoint.client("jellyfin")
            refresh = client.refresh_for_paths

            def refresh_with_new_pending(*args, **kwargs):
                queue.enqueue_media_refresh("jellyfin", [new], debounce_seconds=20, now_epoch=101)
                return refresh(*args, **kwargs)

            with patch.object(worker, "_client_for", return_value=client), patch.object(
                client, "refresh_for_paths", side_effect=refresh_with_new_pending,
            ), patch("app.repositories.media_refresh_queue.time.time", return_value=102):
                worker._process_group(lease)
            self.assertEqual(endpoint.posts, [])
            self.assertEqual(worker.status()["failed_session"], 1)
            self.assertEqual(queue.media_refresh_queue_status(now_epoch=102)["paths"], 1)

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
            self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, force=True, now_epoch=122), [])
            self.assertEqual(queue.media_refresh_queue_status(now_epoch=122)["paths"], 0)

    def test_unmapped_deleted_or_inaccessible_file_needs_new_change_after_mapping_and_restart(self):
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
                    def lstat(path, *args, blocked=inaccessible, blocked_path=changed, fallback=os.lstat, **kwargs):
                        if blocked and os.fspath(path) == str(blocked_path):
                            raise PermissionError("directory is not searchable")
                        return fallback(path, *args, **kwargs)

                    queue.enqueue_media_refresh(provider, [str(changed)], debounce_seconds=0, now_epoch=100)
                    worker = MediaRefreshCoordinator()
                    first = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)[0]
                    with patch.object(worker, "_client_for", side_effect=endpoint.client), patch("os.lstat", side_effect=lstat), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=100,
                    ):
                        worker._process_group(first)
                    self.assertEqual(endpoint.posts, [])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=101)["paths"], 0)
                    self.assertEqual(worker.status()["failed_session"], 1)
                    self.assertEqual(changed.exists(), inaccessible)

                    # 补映射并重启不会重试旧失败；下一次变化仍可刷新已删除或不可访问的路径。
                    db.init_db()
                    worker = MediaRefreshCoordinator()
                    self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, force=True, now_epoch=130), [])
                    queue.enqueue_media_refresh(provider, [str(changed)], debounce_seconds=0, now_epoch=130)
                    next_change = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=130)[0]
                    self.assertEqual(next_change["paths"], [str(changed)])
                    client = endpoint.client(provider)
                    client.path_mappings = (MediaServerPathMapping(str(root), "//nas/strm"),)
                    with patch.object(worker, "_client_for", return_value=client), patch("os.lstat", side_effect=lstat), patch(
                        "app.repositories.media_refresh_queue.time.time", return_value=130,
                    ):
                        worker._process_group(next_change)
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
                    ):
                        worker._process_group(first)
                    self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=101)["paths"], 0)
                    self.assertEqual(worker.status()["failed_session"], 1)

                    db.init_db()
                    worker = MediaRefreshCoordinator()
                    self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, force=True, now_epoch=1060), [])
                    self.assertEqual(endpoint.posts, ["/Items/series-a/Refresh"])
                    self.assertEqual(queue.media_refresh_queue_status(now_epoch=1061)["paths"], 0)

    def test_dc938c40_mixed_waiting_retry_and_running_groups_are_consumed_once(self):
        with isolated_test_database("mediaflux.db"):
            root = "/mf-absent-media"
            groups = []
            for provider, status, allowed in (
                ("jellyfin", "waiting_configuration", ()),
                ("emby", "retry_wait", ()),
                ("jellyfin", "running", ("audit-library",)),
            ):
                entry = queue.enqueue_media_refresh(
                    provider, [f"{root}/{status}/pending.strm"], allowed_library_ids=allowed,
                    debounce_seconds=0, now_epoch=50,
                )
                groups.append((entry["group_key"], status))
            # 真实 dc938c40 持久化格式：waiting_paths 是含原因/配置版本的字典。
            state = json.loads(db.kv_get(queue._QUEUE_KEY))
            expected = {}
            for key, status in groups:
                group = state["groups"][key]
                group.pop("intent")
                group.update(status=status, attempts=10_000, last_error="历史刷新失败", due_at=10_000)
                group["waiting_paths"] = {f"{root}/{status}/waiting.strm": {
                    "reason": "缺少路径映射", "configuration_revision": "dc938c40",
                }}
                if status == "waiting_configuration":
                    group["pending_paths"] = []
                elif status == "running":
                    group.update(
                        inflight_paths=[f"{root}/{status}/inflight.strm"],
                        lease_owner="dead-worker", lease_until=10_000, lease_generation=7,
                    )
                expected[key] = set(group["pending_paths"] + group["inflight_paths"] + list(group["waiting_paths"]))
            db.kv_set(queue._QUEUE_KEY, json.dumps(state))
            db.init_db()
            snapshot_status = queue.media_refresh_queue_status(now_epoch=100)
            self.assertEqual((snapshot_status["queued"], snapshot_status["running"], snapshot_status["paths"]), (2, 1, 6))
            self.assertEqual(queue.next_media_refresh_due_in(now_epoch=100), 0)
            self.assertEqual(queue.recover_media_refresh_leases(now_epoch=100), 1)
            self.assertEqual(queue.recover_media_refresh_leases(now_epoch=100), 0)
            worker = MediaRefreshCoordinator()
            claimed = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=100)
            self.assertEqual({item["group_key"]: set(item["paths"]) for item in claimed}, expected)
            self.assertEqual(sum(len(item["paths"]) for item in claimed), 6)
            endpoint = MediaEndpoint(root)
            endpoint.fail = True
            with patch.object(worker, "_client_for") as legacy_client, patch(
                "app.repositories.media_refresh_queue.time.time", return_value=101,
            ):
                for item in claimed:
                    worker._process_group(item)
            legacy_client.assert_not_called()
            self.assertEqual(worker.status()["failed_session"], 3)
            self.assertEqual(queue.media_refresh_queue_status(now_epoch=102)["paths"], 0)
            db.init_db()
            self.assertEqual(queue.claim_due_media_refreshes(owner=worker._owner, force=True, now_epoch=20_000), [])
            self.assertEqual(endpoint.posts, [])
            # 历史等待只执行一次；同一路径后续的新变化仍能正常入队并刷新。
            changed = f"{root}/waiting_configuration/waiting.strm"
            queue.enqueue_media_refresh("jellyfin", [changed], debounce_seconds=0, now_epoch=20_001)
            next_change = queue.claim_due_media_refreshes(owner=worker._owner, now_epoch=20_001)[0]
            self.assertEqual(next_change["paths"], [changed])
            endpoint.fail = False
            with patch.object(worker, "_client_for", side_effect=endpoint.client), patch(
                "app.repositories.media_refresh_queue.time.time", return_value=20_001,
            ):
                worker._process_group(next_change)
            self.assertEqual(endpoint.posts, ["/Items/audit-library/Refresh"])
            self.assertEqual(worker.status()["completed_session"], 1)
            self.assertEqual(queue.media_refresh_queue_status(now_epoch=20_002)["paths"], 0)

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
            plan = SimpleNamespace(
                provider="jellyfin",
                library_id="audit-library",
                library_name="审查库",
                target=Path("/media/Movies/Film-A/Film.mkv"),
            )
            with patch(
                "app.modules.media_refresh_coordinator.get_media_refresh_coordinator"
            ):
                self.assertEqual(LocalMediaService._refresh_plans([cast(LocalMovePlan, plan)]), ("queued", []))

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
            self.assertEqual(queue.media_refresh_queue_status()["paths"], 0)
            self.assertEqual(queue.claim_due_media_refreshes(owner=old_worker._owner, force=True), [])
            # 新变化重新入队，模拟进程在消费新工作时中断，而非媒体服务失败自动重试。
            with patch("app.modules.media_refresh_coordinator.get_media_refresh_coordinator"):
                self.assertEqual(LocalMediaService._refresh_plans([cast(LocalMovePlan, plan)]), ("queued", []))
            old_lease = queue.claim_due_media_refreshes(
                owner=old_worker._owner, force=True
            )[0]
            plan.target = Path("/media/Movies/Film-B/Film.mkv")
            with patch(
                "app.modules.media_refresh_coordinator.get_media_refresh_coordinator"
            ):
                self.assertEqual(LocalMediaService._refresh_plans([cast(LocalMovePlan, plan)]), ("queued", []))
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
                            old_lease["group_key"],
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
