"""本地媒体任务安全序号、检查、重试、精准刷新与可见性闭环。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time
from unittest.mock import Mock, patch

from app import database as db
from app.agent.local_media_intents import local_media_task_request
from app.agent.local_media_task_actions import (
    LocalMediaAgentContextStore,
    _TaskRef,
    reset_local_media_agent_context_for_tests,
)
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.agent.state_commit import AgentStateCommitBuffer, defer_agent_state_commits
from app.modules.local_media_service import LocalMediaServiceError
from app.modules.media_server_path_mapping import MediaServerPathMapping
from tests.support import IsolatedDatabaseTestCase


class AgentLocalMediaTaskTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_operation_steps")
            conn.execute("DELETE FROM local_media_task_items")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM agent_action_history")
            conn.execute("DELETE FROM agent_session_context")
            conn.execute("DELETE FROM agent_action_leases")
        reset_agent_service_for_tests()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()

    def _task(
        self,
        *,
        status: str,
        title: str = "示例影片",
        bound: bool = False,
        media_type: str = "movie",
        tmdb_id: str = "12345",
        season: int | None = None,
        episode: int | None = None,
        server_path: str = "",
    ) -> int:
        source_id = db.create_local_media_source(
            name="PRIVATE-SOURCE",
            qb_profile="qb",
            qb_path_prefix="/private/downloads",
            local_root="/private/downloads",
            owner="admin",
        )
        if bound:
            db.upsert_local_library_target(
                source_id,
                "movie" if media_type == "movie" else "tv",
                "/private/library",
                provider="jellyfin",
                library_id="private-library-id",
                library_name="媒体库",
                server_path=server_path,
                owner="admin",
            )
        task_id = db.create_local_media_task(
            source_id,
            "PRIVATE-HASH",
            "/private/downloads/SECRET.mkv",
            owner="admin",
        )
        db.update_local_media_task(
            task_id,
            owner="admin",
            status=status,
            title=title,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season_override=season,
            episode_override=episode,
            snapshot_digest="digest-1",
            error="PRIVATE-ERROR /private/downloads/SECRET.mkv",
            warning="PRIVATE-WARNING",
            completed_at=db.now() if status == "completed" else "",
        )
        if bound:
            db.add_local_media_task_item(
                task_id,
                "/private/downloads/SECRET.mkv",
                "/private/library/示例影片/示例影片.mkv",
                role="video",
                owner="admin",
            )
        return task_id

    @staticmethod
    def _profile(*, url: str = "http://private-server"):
        return SimpleNamespace(
            server_type="jellyfin",
            label="Jellyfin",
            url=url,
            credential="PRIVATE-TOKEN",
            enabled=True,
            configured=True,
        )

    def test_task_list_is_owner_bound_and_redacts_internal_fields(self) -> None:
        self._task(status="requires_manual")
        service = get_agent_service()
        listed = service.invoke(
            "local_media.task_summaries",
            {"scope": "attention", "limit": 12},
            owner="owner-a",
        )
        item = listed["result"]["data"]["tasks"][0]
        self.assertEqual(item["task_number"], 1)
        self.assertEqual(item["title"], "示例影片")
        self.assertTrue(item["can_inspect"])
        serialized = repr(listed)
        self.assertNotIn("id", item)
        for secret in (
            "PRIVATE-HASH",
            "/private",
            "PRIVATE-ERROR",
            "PRIVATE-WARNING",
            "digest-1",
        ):
            self.assertNotIn(secret, serialized)

        with self.assertRaises(AgentToolError):
            service.invoke(
                "local_media.inspect_task",
                {"task_number": 1},
                owner="owner-b",
            )

    def test_inspection_and_preview_use_owner_bound_safe_handles(self) -> None:
        self._task(status="requires_manual")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries",
            {"scope": "attention", "limit": 12},
            owner="owner-a",
        )
        local_service = Mock()
        local_service.inspect_task.return_value = {
            "inspection_id": "PRIVATE-INTERNAL-INSPECTION",
            "digest": "digest-1",
            "task_title": "示例影片",
            "task_year": "2026",
            "task_media_type": "movie",
            "suggested_query": "示例影片",
            "parsed_season": None,
            "parsed_episode": None,
            "files": [{
                "name": "示例影片.mkv",
                "relative_path": "SECRET/示例影片.mkv",
                "role": "video",
                "size": 123,
            }],
        }
        local_service.preview.return_value = {
            "status": "planned",
            "candidates": [{
                "tmdb_id": "PRIVATE-TMDB",
                "title": "示例影片",
                "year": "2026",
                "media_type": "movie",
                "confidence": "high",
            }],
            "plans": [{
                "role": "video",
                "action": "move",
                "target_name": "示例影片 (2026).mkv",
                "target_path": "/private/library/示例影片.mkv",
            }],
            "rules_snapshot": "PRIVATE-RULES",
        }
        with patch(
            "app.agent.local_media_task_actions.get_local_media_service",
            return_value=local_service,
        ):
            inspected = service.invoke(
                "local_media.inspect_task", {"task_number": 1}, owner="owner-a"
            )
            inspection_number = inspected["result"]["data"]["inspection_number"]
            with self.assertRaises(AgentToolError):
                service.invoke(
                    "local_media.preview_task",
                    {"inspection_number": inspection_number},
                    owner="owner-b",
                )
            preview = service.invoke(
                "local_media.preview_task",
                {"inspection_number": inspection_number},
                owner="owner-a",
            )
        serialized = repr(inspected) + repr(preview)
        for secret in (
            "PRIVATE-INTERNAL-INSPECTION",
            "SECRET/",
            "/private/library",
            "PRIVATE-TMDB",
            "PRIVATE-RULES",
        ):
            self.assertNotIn(secret, serialized)
        self.assertFalse(preview["result"]["data"]["cloud_write"])
        local_service.preview.assert_called_once()

    def test_retry_is_confirmation_gated_versioned_and_replay_safe(self) -> None:
        task_id = self._task(status="failed")
        db.add_local_media_task_item(
            task_id,
            "/private/downloads/OLD.mkv",
            "/private/library/旧目标/OLD.mkv",
            role="video",
            owner="admin",
        )
        before = db.get_local_media_task(task_id, owner="admin")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries",
            {"scope": "attention", "limit": 12},
            owner="owner-a",
        )
        scheduler = Mock()
        with patch(
            "app.agent.local_media_task_actions.get_local_media_scheduler",
            return_value=scheduler,
        ):
            prepared = service.prepare(
                "local_media.retry_task", {"task_number": 1}, owner="owner-a"
            )
            self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "failed")
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner-a"
            )
        after = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(confirmed["result"]["status"], "accepted")
        self.assertEqual(after.status, "waiting_stable")
        self.assertEqual(after.version, before.version + 1)
        self.assertNotEqual(after.operation_token, before.operation_token)
        self.assertEqual(
            db.list_local_media_task_items(task_id, owner="admin"), []
        )
        scheduler.reload.assert_called_once_with()
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")

    def test_retry_stale_or_non_retryable_task_fails_closed(self) -> None:
        task_id = self._task(status="failed")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries",
            {"scope": "attention", "limit": 12},
            owner="owner-a",
        )
        prepared = service.prepare(
            "local_media.retry_task", {"task_number": 1}, owner="owner-a"
        )
        db.update_local_media_task(task_id, owner="admin", warning="changed")
        with self.assertRaises(AgentToolError) as stale:
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "failed")

        reset_agent_service_for_tests()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
        self._task(status="completed")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "all", "limit": 12}, owner="owner-a"
        )
        with self.assertRaises(AgentToolError):
            service.prepare(
                "local_media.retry_task", {"task_number": 1}, owner="owner-a"
            )

    def test_precise_refresh_uses_only_bound_library_and_stales_on_drift(self) -> None:
        task_id = self._task(
            status="completed", bound=True, server_path="//NAS/Video",
        )
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [{"id": "private-library-id", "name": "媒体库"}]
        client.refresh_for_paths.return_value = {
            "ok": True,
            "skipped": False,
            "matched": 1,
            "scope": "item",
        }
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider",
            return_value=client,
        ):
            prepared = service.prepare(
                "local_media.refresh_task_library",
                {"task_number": 1},
                owner="owner-a",
            )
            self.assertNotIn("private-library-id", repr(prepared))
            self.assertNotIn("/private", repr(prepared))
            client.refresh_for_paths.assert_not_called()
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner-a"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        client.refresh_for_paths.assert_called_once_with(
            ["//NAS/Video/示例影片"],
            allowed_library_ids=("private-library-id",),
            allow_library_fallback=False,
        )
        self.assertNotIn("private-library-id", repr(confirmed))
        self.assertNotIn("/private", repr(confirmed))

        reset_agent_service_for_tests()
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider",
            return_value=client,
        ):
            prepared = service.prepare(
                "local_media.refresh_task_library", {"task_number": 1}, owner="owner-a"
            )
            db.update_local_media_task(task_id, owner="admin", warning="binding-drift")
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(
                    prepared["action_plan"]["plan_id"], owner="owner-a"
                )
        self.assertEqual(stale.exception.code, "confirmation_stale")

    def test_refresh_validation_failure_closes_media_server_client(self) -> None:
        self._task(status="completed", bound=True)
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries",
            {"scope": "history", "limit": 12},
            owner="owner-a",
        )
        client = Mock()
        client.list_virtual_folders.return_value = []
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider",
            return_value=client,
        ), self.assertRaises(AgentToolError) as raised:
            service.prepare(
                "local_media.refresh_task_library",
                {"task_number": 1},
                owner="owner-a",
            )

        self.assertEqual(raised.exception.code, "precondition_failed")
        client.close.assert_called_once_with()

    def test_visibility_distinguishes_indexed_from_playback(self) -> None:
        self._task(status="completed", bound=True, media_type="movie", tmdb_id="12345")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [{"id": "private-library-id", "name": "媒体库"}]
        client.has_tmdb_media.return_value = True
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider",
            return_value=client,
        ):
            result = service.invoke(
                "local_media.verify_task_library_visibility",
                {"task_number": 1},
                owner="owner-a",
            )
        data = result["result"]["data"]
        self.assertEqual(data["index_status"], "visible")
        self.assertEqual(data["playback_status"], "not_checked")
        self.assertEqual(data["playback_claim"], "not_probed")
        self.assertNotIn("可播放", result["result"]["summary"])
        client.has_tmdb_media.assert_called_once_with(
            "12345", "movie", parent_id="private-library-id"
        )
        client.close.assert_called_once_with()

    def test_natural_query_routes_list_and_retry_through_confirmation(self) -> None:
        self._task(status="failed")
        service = get_agent_service()
        listed = service.query(
            "列出失败的本地媒体任务", owner="owner-a", present=False
        )
        self.assertEqual(listed["tool_call"]["name"], "local_media.task_summaries")
        prepared = service.query(
            "重试本地媒体任务 1", owner="owner-a", present=False
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "local_media.retry_task")
        service.reset_session(owner="owner-a")
        with self.assertRaises(AgentToolError):
            service.invoke(
                "local_media.inspect_task", {"task_number": 1}, owner="owner-a"
            )

    def test_preview_rebuilds_process_local_inspection_after_worker_change(self) -> None:
        self._task(status="requires_manual")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries",
            {"scope": "attention", "limit": 12},
            owner="owner-a",
        )
        local_service = Mock()
        local_service.inspect_task.side_effect = [
            {
                "inspection_id": "worker-a-inspection",
                "digest": "digest-1",
                "files": [{"role": "video", "name": "示例影片.mkv"}],
                "task_title": "示例影片",
            },
            {
                "inspection_id": "worker-b-inspection",
                "digest": "digest-1",
            },
        ]
        local_service.preview.side_effect = [
            LocalMediaServiceError("检查记录不存在或已过期"),
            {
                "status": "planned",
                "candidates": [],
                "plans": [{"role": "video", "action": "move", "target_name": "示例影片.mkv"}],
            },
        ]
        with patch(
            "app.agent.local_media_task_actions.get_local_media_service",
            return_value=local_service,
        ):
            inspected = service.invoke(
                "local_media.inspect_task", {"task_number": 1}, owner="owner-a"
            )
            reset_local_media_agent_context_for_tests()
            previewed = service.invoke(
                "local_media.preview_task",
                {"inspection_number": inspected["result"]["data"]["inspection_number"]},
                owner="owner-a",
            )
        self.assertEqual(previewed["result"]["status"], "completed")
        self.assertEqual(local_service.inspect_task.call_count, 2)
        self.assertEqual(local_service.preview.call_count, 2)
        self.assertEqual(
            local_service.preview.call_args_list[-1].args[1],
            "worker-b-inspection",
        )

    def test_context_handles_survive_worker_recreation_and_remain_owner_bound(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        task = SimpleNamespace(id=9, version=3, status="requires_manual", source_id=4)
        first = LocalMediaAgentContextStore(repository=repository)
        first.capture_tasks(owner="owner-a", tasks=[task])

        second = LocalMediaAgentContextStore(repository=repository)
        ref = second.task(owner="owner-a", number=1)
        self.assertEqual(ref, _TaskRef(9, 3, "requires_manual", 4))
        self.assertIsNone(second.task(owner="owner-b", number=1))
        inspection_number = second.capture_inspection(
            owner="owner-a",
            task=ref,
            inspection_id="inspection-private",
            digest="digest-private",
        )

        third = LocalMediaAgentContextStore(repository=repository)
        inspection = third.inspection(owner="owner-a", number=inspection_number)
        self.assertIsNotNone(inspection)
        self.assertEqual(inspection.task, ref)
        self.assertIsNone(third.inspection(owner="owner-b", number=inspection_number))
        third.clear_owner(owner="owner-a")
        self.assertIsNone(
            LocalMediaAgentContextStore(repository=repository).task(
                owner="owner-a", number=1
            )
        )

    def test_concurrent_worker_inspections_get_distinct_persisted_numbers(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        tasks = [
            SimpleNamespace(id=9, version=3, status="requires_manual", source_id=4),
            SimpleNamespace(id=10, version=2, status="requires_manual", source_id=5),
        ]
        seed = LocalMediaAgentContextStore(repository=repository)
        refs = seed.capture_tasks(owner="owner-a", tasks=tasks)
        barrier = __import__("threading").Barrier(2)

        def capture(index: int) -> int:
            store = LocalMediaAgentContextStore(repository=repository)
            barrier.wait(timeout=3)
            return store.capture_inspection(
                owner="owner-a",
                task=refs[index],
                inspection_id=f"inspection-{index}",
                digest=f"digest-{index}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            numbers = sorted(pool.map(capture, range(2)))
        self.assertEqual(numbers, [1, 2])
        restored = LocalMediaAgentContextStore(repository=repository)
        self.assertEqual(
            {
                restored.inspection(owner="owner-a", number=number).task.task_id
                for number in numbers
            },
            {9, 10},
        )

    def test_context_store_bounds_inspection_owner_counters(self) -> None:
        store = LocalMediaAgentContextStore(max_owners=1)
        first = SimpleNamespace(id=1, version=1, status="requires_manual", source_id=1)
        second = SimpleNamespace(id=2, version=1, status="requires_manual", source_id=2)
        store.capture_tasks(owner="owner-a", tasks=[first])
        ref_a = store.task(owner="owner-a", number=1)
        store.capture_inspection(
            owner="owner-a", task=ref_a, inspection_id="a", digest="a"
        )
        store.capture_tasks(owner="owner-b", tasks=[second])
        ref_b = store.task(owner="owner-b", number=1)
        store.capture_inspection(
            owner="owner-b", task=ref_b, inspection_id="b", digest="b"
        )
        self.assertNotIn("owner-a", store._next_inspection)
        self.assertLessEqual(len(store._next_inspection), 1)

    def test_context_store_buffers_same_request_chain_until_commit(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        store = LocalMediaAgentContextStore(repository=repository)
        task = SimpleNamespace(
            id=9, version=3, status="requires_manual", source_id=4
        )
        buffer = AgentStateCommitBuffer(owner="owner-a")

        with defer_agent_state_commits(buffer):
            refs = store.capture_tasks(owner="owner-a", tasks=[task])
            self.assertEqual(store.task(owner="owner-a", number=1), refs[0])
            inspection_number = store.capture_inspection(
                owner="owner-a",
                task=refs[0],
                inspection_id="inspection-private",
                digest="digest-private",
            )
            self.assertEqual(inspection_number, 1)
            self.assertIsNotNone(
                store.inspection(owner="owner-a", number=inspection_number)
            )

        self.assertIsNone(store.task(owner="owner-a", number=1))
        self.assertEqual(buffer.commit(), 1)
        restored = LocalMediaAgentContextStore(repository=repository)
        self.assertEqual(
            restored.task(owner="owner-a", number=1),
            _TaskRef(9, 3, "requires_manual", 4),
        )
        self.assertIsNotNone(restored.inspection(owner="owner-a", number=1))

    def test_context_store_discard_blocks_late_context_resurrection(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        store = LocalMediaAgentContextStore(repository=repository)
        task = SimpleNamespace(
            id=9, version=3, status="requires_manual", source_id=4
        )
        buffer = AgentStateCommitBuffer(owner="owner-a")

        with defer_agent_state_commits(buffer):
            refs = store.capture_tasks(owner="owner-a", tasks=[task])
            self.assertEqual(len(refs), 1)
            store.clear_owner(owner="owner-a")
            self.assertEqual(buffer.discard(), 1)
            self.assertEqual(store.capture_tasks(owner="owner-a", tasks=[task]), ())
            self.assertEqual(
                store.capture_inspection(
                    owner="owner-a",
                    task=refs[0],
                    inspection_id="late-inspection",
                    digest="late-digest",
                ),
                0,
            )

        self.assertIsNone(
            repository.get_latest(
                owner="owner-a",
                context_type="local_media_tasks",
                now=time.time(),
            )
        )
        self.assertIsNone(store.task(owner="owner-a", number=1))

    def test_context_store_stale_cross_worker_commit_cannot_undo_reset(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        old_store = LocalMediaAgentContextStore(repository=repository)
        reset_store = LocalMediaAgentContextStore(repository=repository)
        task = SimpleNamespace(
            id=9, version=3, status="requires_manual", source_id=4
        )
        buffer = AgentStateCommitBuffer(owner="owner-a")

        with defer_agent_state_commits(buffer):
            refs = old_store.capture_tasks(owner="owner-a", tasks=[task])
            old_store.capture_inspection(
                owner="owner-a",
                task=refs[0],
                inspection_id="inspection-private",
                digest="digest-private",
            )
            reset_store.clear_owner(owner="owner-a")

        self.assertEqual(buffer.commit(), 0)
        self.assertIsNone(
            repository.get_latest(
                owner="owner-a",
                context_type="local_media_tasks",
                now=time.time(),
            )
        )
        self.assertIsNone(old_store.task(owner="owner-a", number=1))

    def test_context_store_newer_cross_worker_buffer_wins(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "test-secret"
        )
        old_store = LocalMediaAgentContextStore(repository=repository)
        new_store = LocalMediaAgentContextStore(repository=repository)
        first = SimpleNamespace(
            id=9, version=3, status="requires_manual", source_id=4
        )
        second = SimpleNamespace(
            id=10, version=2, status="requires_manual", source_id=5
        )
        old_buffer = AgentStateCommitBuffer(owner="owner-a")
        new_buffer = AgentStateCommitBuffer(owner="owner-a")

        with defer_agent_state_commits(old_buffer):
            old_store.capture_tasks(owner="owner-a", tasks=[first])
        with defer_agent_state_commits(new_buffer):
            new_store.capture_tasks(owner="owner-a", tasks=[second])

        self.assertEqual(new_buffer.commit(), 1)
        self.assertEqual(old_buffer.commit(), 0)
        restored = LocalMediaAgentContextStore(repository=repository)
        self.assertEqual(
            restored.task(owner="owner-a", number=1),
            _TaskRef(10, 2, "requires_manual", 5),
        )

    def test_precise_refresh_stales_when_server_endpoint_changes(self) -> None:
        self._task(status="completed", bound=True)
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "媒体库"}
        ]
        client.refresh_for_paths.return_value = {"ok": True, "skipped": False, "matched": 1, "scope": "item"}
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            side_effect=[[self._profile(url="http://server-a")], [self._profile(url="http://server-b")]],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider", return_value=client
        ):
            prepared = service.prepare(
                "local_media.refresh_task_library", {"task_number": 1}, owner="owner-a"
            )
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(
                    prepared["action_plan"]["plan_id"], owner="owner-a"
                )
        self.assertEqual(stale.exception.code, "confirmation_stale")
        client.refresh_for_paths.assert_not_called()
        self.assertEqual(client.close.call_count, 2)

    def test_precise_refresh_stales_when_path_mapping_changes(self) -> None:
        self._task(status="completed", bound=True)
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "媒体库"}
        ]
        first_options = {
            "path_mappings": (MediaServerPathMapping("/private", "/server-a"),),
            "allow_global_refresh_fallback": False,
        }
        second_options = {
            "path_mappings": (MediaServerPathMapping("/private", "/server-b"),),
            "allow_global_refresh_fallback": False,
        }
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions.configured_media_server_refresh_options",
            side_effect=[first_options, second_options],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider", return_value=client
        ):
            prepared = service.prepare(
                "local_media.refresh_task_library", {"task_number": 1}, owner="owner-a"
            )
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(
                    prepared["action_plan"]["plan_id"], owner="owner-a"
                )
        self.assertEqual(stale.exception.code, "confirmation_stale")
        client.refresh_for_paths.assert_not_called()
        self.assertEqual(client.close.call_count, 2)

    def test_tv_visibility_is_inconclusive_without_episode_identity(self) -> None:
        self._task(status="completed", bound=True, media_type="tv", tmdb_id="12345")
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "媒体库"}
        ]
        client.find_series_candidates_by_tmdb.return_value = SimpleNamespace(
            candidates=(SimpleNamespace(id="series-private"),), truncated=False
        )
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider", return_value=client
        ):
            result = service.invoke(
                "local_media.verify_task_library_visibility",
                {"task_number": 1},
                owner="owner-a",
            )
        self.assertEqual(result["result"]["data"]["index_status"], "inconclusive")
        self.assertEqual(
            result["result"]["data"]["reason_code"],
            "series_indexed_episode_unverified",
        )
        self.assertNotIn("媒体已在绑定媒体库中可见", result["result"]["summary"])
        client.list_series_episode_inventory.assert_not_called()
        client.close.assert_called_once_with()

    def test_two_refresh_tickets_cannot_execute_concurrently(self) -> None:
        self._task(status="completed", bound=True)
        service = get_agent_service()
        service.invoke(
            "local_media.task_summaries", {"scope": "history", "limit": 12}, owner="owner-a"
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "媒体库"}
        ]
        client.refresh_for_paths.return_value = {"ok": True, "skipped": False, "matched": 1, "scope": "item"}
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider", return_value=client
        ):
            tickets = [
                service.prepare(
                    "local_media.refresh_task_library", {"task_number": 1}, owner="owner-a"
                )["action_plan"]["plan_id"]
                for _ in range(2)
            ]

            def confirm(ticket: str) -> str:
                try:
                    service.confirm(ticket, owner="owner-a")
                except AgentToolError as exc:
                    return exc.code
                return "confirmed"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(pool.map(confirm, tickets))
        self.assertEqual(outcomes, ["confirmation_invalid", "confirmed"])
        client.refresh_for_paths.assert_called_once()

    def test_cross_owner_refresh_confirmations_share_a_deduplication_lease(self) -> None:
        self._task(status="completed", bound=True)
        service = get_agent_service()
        for owner in ("owner-a", "owner-b"):
            service.invoke(
                "local_media.task_summaries",
                {"scope": "history", "limit": 12},
                owner=owner,
            )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "媒体库"}
        ]
        client.refresh_for_paths.return_value = {
            "ok": True,
            "skipped": False,
            "matched": 1,
            "scope": "item",
        }
        with patch(
            "app.agent.local_media_task_actions.list_configured_profiles",
            return_value=[self._profile()],
        ), patch(
            "app.agent.local_media_task_actions._client_for_provider", return_value=client
        ):
            tickets = {
                owner: service.prepare(
                    "local_media.refresh_task_library",
                    {"task_number": 1},
                    owner=owner,
                )["action_plan"]["plan_id"]
                for owner in ("owner-a", "owner-b")
            }

            def confirm(owner: str) -> str:
                result = service.confirm(tickets[owner], owner=owner)
                return result["result"]["status"]

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(pool.map(confirm, ("owner-a", "owner-b")))
        self.assertEqual(outcomes, ["completed", "conflict"])
        client.refresh_for_paths.assert_called_once_with(
            ["/private/library/示例影片"],
            allowed_library_ids=("private-library-id",),
            allow_library_fallback=False,
        )

    def test_intents_cover_task_chain_without_guessing(self) -> None:
        self.assertEqual(
            local_media_task_request("列出失败的本地媒体任务"),
            ("local_media.task_summaries", {"scope": "attention", "limit": 12}),
        )
        self.assertEqual(
            local_media_task_request("检查本地媒体任务 2"),
            ("local_media.inspect_task", {"task_number": 2}),
        )
        self.assertEqual(
            local_media_task_request("预览本地媒体检查 3"),
            ("local_media.preview_task", {"inspection_number": 3}),
        )
        self.assertEqual(
            local_media_task_request("重试本地媒体任务 2"),
            ("local_media.retry_task", {"task_number": 2}),
        )
        self.assertEqual(
            local_media_task_request("刷新本地媒体任务 2 的媒体库"),
            ("local_media.refresh_task_library", {"task_number": 2}),
        )
        self.assertEqual(
            local_media_task_request("本地媒体任务 2 入库可见了吗"),
            ("local_media.verify_task_library_visibility", {"task_number": 2}),
        )
        self.assertIsNone(local_media_task_request("刷新 /Library/Refresh"))
