"""后台完成身份贯穿真实准入、worker、SQLite与冷回执。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import httpx

from app import database as db
from app.repositories import local_media as local_media_repo
from app.repositories import agent_library_patrol as agent_library_patrol_repo
from app.agent import completion_observers as observers
from app.agent import effect_completion as effects
from app.agent.confirmation import SQLiteConfirmationStore
from app.modules import library_patrol
from app.modules.library_patrol import patrol_status
from app.modules import organize_service
from app.modules.organize_results import observe_organize_task
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import _capture_creation_binding

from app.agent.kernel.completion_receipts import _completion_cipher
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.modules.process_lock import CrossProcessLock
from app.agent.models import ToolContext, ToolResult
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.modules.episode_audit import EpisodeAuditResult
from app.modules.scheduler import STRMScheduler
from app.modules.organize import OrganizeRules, Organizer
from app.modules.organize_results import build_organize_result
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.local_media_service import LocalMediaService
from app.modules.scraper import MatchResult, TMDBScraper
from tests.support import isolated_test_database
from tests.test_agent_task_first_slice import NativeScript
from tests.test_local_media_service import FakeScraper

class CompletionScript(NativeScript):
    def __init__(self, calls):
        super().__init__("fixture")
        self.audit_calls = list(calls)

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.audit_calls:
            name, args = self.audit_calls.pop(0)
            message = {"tool_calls": [{"id": f"audit-{len(self.requests)}", "type": "function",
                "function": {"name": name.replace(".", "__"), "arguments": json.dumps(args)}}]}
        else:
            message = {"content": "请以实际任务回执为准。"}
        return httpx.Response(200, json={"choices": [{"message": message,
            "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}]})


def completion_session(spec_names, calls, *, actions=None):
    store = SQLiteKernelStore()
    from app.agent.kernel.capabilities import ToolCatalog
    catalog = ToolCatalog()
    if "strm.run" in spec_names:
        from app.agent.kernel.ports.sdk_effects import strm_run_tool
        catalog.register(strm_run_tool())
    if "library.run_patrol" in spec_names:
        from app.agent.kernel.ports.sdk_effects import library_patrol_tool
        catalog.register(library_patrol_tool())
    from app.agent.kernel.ports.sdk_effects import local_task_tools, cloud_organize_tools
    for tool in (*local_task_tools(), *cloud_organize_tools()):
        if tool.name in spec_names:
            catalog.register(tool)
    script = CompletionScript(calls)
    session = AgentSession(model=script.adapter, catalog=catalog,
         state_store=store, journal=store,
        pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
            effect_lifecycle=MediaFluxEffectLifecycle()))
    session.continuation_binding = _capture_creation_binding()
    return session, store, script


async def assert_strm_identity(later_count: int, later_source: str):
    with isolated_test_database(), ExitStack() as stack:
        initialize_runtime_fence()
        scheduler = STRMScheduler()
        scheduler._run_lock = cast(CrossProcessLock, threading.Lock())
        sources = [{"id": key, "name": key.upper(), "path": "/" + key, "namespace": key}
                   for key in ("a", "b")]
        stack.enter_context(patch("app.modules.scheduler.get_scheduler", return_value=scheduler))
        stack.enter_context(patch("app.modules.scheduler.configured_strm_source_plans", return_value=(sources, "")))
        stack.enter_context(patch.object(scheduler, "validate_config", return_value=""))
        stack.enter_context(patch.object(scheduler, "_source_dirs", return_value=sources))
        stack.enter_context(patch.object(scheduler, "_refresh_media_servers", return_value={}))
        stack.enter_context(patch.object(scheduler, "_notify_scoped_results"))
        stack.enter_context(patch.object(scheduler, "_notify_scoped_failure"))
        original_trigger = scheduler.trigger
        def fail_original(*args, **kwargs):
            # Actual STRMScheduler admission, worker, task_run creation and failure finalization.
            with patch.object(scheduler, "validate_config", return_value="offline provider failed"):
                accepted = original_trigger(*args, **kwargs)
                worker = scheduler._worker
                if worker:
                    worker.join(5)
                    assert not worker.is_alive()
                return accepted
        write = "strm.run"
        session, store, script = completion_session([write], [(write, {"source_ids": ["a"]})])
        context = {"owner": "native-strm-owner", "session_id": "native-strm-session"}
        preview = [e async for e in session.run(AgentInput(**context, request_id="strm-preview",
            message="只同步 STRM 来源 A 并核验最终结果。", channel="web"))]
        assert preview[-1].payload["status"] == "approval_required", preview[-1].payload
        plan_id = (await store.load(**context)).pending_effect_plan_id
        async def transient_read(result, **options):
            # A failed first observation is an existing supported durable-handoff path.
            # It cannot create a terminal result or mark this task delivered.
            with patch.object(observers, "_poll", side_effect=RuntimeError("transient reader unavailable")):
                return await wait_for_effect_completion(result, timeout_seconds=0, **options)
        with patch.object(scheduler, "trigger", fail_original), patch.object(
                effects, "wait_for_effect_completion", transient_read):
            confirmed = [e async for e in session.confirm(**context, plan_id=plan_id,
                request_id="strm-confirm", channel="web")]
        assert confirmed[-1].payload["status"] == "waiting"
        before = await SQLiteKernelStore().load(**context)
        wait = before.metadata["effect_waits"][plan_id]
        assert not wait["delivered"] and wait["state"] == "pending" and "final_result" not in wait
        seed = json.loads(_completion_cipher().decrypt(wait["sealed"].encode()))
        assert (seed["owner"], seed["session_id"], seed["plan_id"], seed["request_id"]) == (
            context["owner"], context["session_id"], plan_id, "strm-confirm")
        original = db.list_task_runs("strm_sync", limit=1)[0]
        assert original["status"] == "failed"
        for index in range(later_count):
            # Real independent manual runs; fast + no queued changes is a real successful no-op.
            accepted = original_trigger("manual", sync_mode="fast", selected_source_ids=[later_source])
            assert accepted["ok"]
            worker = scheduler._worker
            if worker:
                worker.join(5)
                assert not worker.is_alive()
            assert db.list_task_runs("strm_sync", limit=1)[0]["status"] == "success"
        future = time.time() + 11
        cold = SQLiteKernelStore(clock=lambda: future)
        with patch.object(effects.time, "time", return_value=future):
            count = await effects.poll_effect_receipts(cold)
        assert count == 1
        after = await cold.load(**context)
        final = after.metadata["effect_waits"][plan_id]["final_result"]
        native_tool = next(r for r in after.conversation if r.get("role") == "tool" and r.get("effect_plan_id") == plan_id)
        assert json.loads(native_tool["content"]) == final
        assert after.metadata["effect_waits"][plan_id]["delivered"]
        assert final["status"] == "failed"
        assert not final["ok"]
        assert seed["tracker"]["value"]["run_id"] == original["id"]


async def assert_patrol_identity(case):
    with isolated_test_database(), ExitStack() as stack:
        initialize_runtime_fence()
        state = {"clock": datetime.now().replace(microsecond=0), "status": "up_to_date"}
        def observation(_arguments):
            status = state["status"]
            return EpisodeAuditResult(status == "up_to_date", status, status,
                data={"checked_series_count": 0, "findings": []}), 0
        scheduler = AgentLibraryPatrolScheduler(clock=lambda: state["clock"], audit_executor=observation)
        get_bool = library_patrol.config.get_bool
        stack.enter_context(patch.object(library_patrol.config, "get_bool",
            side_effect=lambda key, default=False: True if key == "AGENT_LIBRARY_PATROL_ENABLED" else get_bool(key, default)))
        stack.enter_context(patch("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", return_value=scheduler))
        if case != "overwritten-cycle":
            library_patrol.run_patrol(library_patrol.prepare_patrol())
            assert scheduler.run_once() == 1
            prior_row = agent_library_patrol_repo.get_agent_library_patrol()
            assert prior_row is not None
            prior = dict(prior_row)
        else:
            prior = None
            state["status"] = "not_configured"
        write = "library.run_patrol"
        session, store, script = completion_session([write], [(write, {})])
        context = {"owner": "patrol-identity-owner", "session_id": "patrol-identity-session"}
        preview = [e async for e in session.run(AgentInput(**context, request_id="patrol-preview",
            message="立即执行一次全库巡检并核验本次结果。", channel="web"))]
        assert preview[-1].payload["status"] == "approval_required", preview[-1].payload
        plan_id = (await store.load(**context)).pending_effect_plan_id
        original = {}
        async def run_before_observe(accepted, **options):
            if case == "separate-second":
                state["clock"] += timedelta(seconds=2)
            assert scheduler.run_once() == 1
            original_row = agent_library_patrol_repo.get_agent_library_patrol()
            assert original_row is not None
            original.update(dict(original_row))
            if case == "overwritten-cycle":
                assert original["outcome"] == "not_configured"
                state["clock"] += timedelta(seconds=2)
                state["status"] = "up_to_date"
                library_patrol.run_patrol(library_patrol.prepare_patrol())
                assert scheduler.run_once() == 1
            return await wait_for_effect_completion(accepted, timeout_seconds=0, **options)
        with patch.object(effects, "wait_for_effect_completion", run_before_observe):
            confirmed = [e async for e in session.confirm(**context, plan_id=plan_id,
                request_id="patrol-confirm", channel="web")]
        current_row = agent_library_patrol_repo.get_agent_library_patrol()
        assert current_row is not None
        current = dict(current_row)
        saved = await store.load(**context)
        wait = saved.metadata["effect_waits"][plan_id]
        final = wait.get("final_result")
        assert final and wait["delivered"]
        assert final["status"] == ("not_configured" if case == "overwritten-cycle" else "up_to_date")
        if case == "same-second":
            assert prior is not None
            assert current["last_finished_at"] == prior["last_finished_at"]
            assert current["lease_generation"] == prior["lease_generation"] + 1
        assert final["ok"] == (case != "overwritten-cycle")
        assert confirmed[-1].payload["status"] == ("partial" if case == "overwritten-cycle" else "success")


async def assert_patrol_outcome(status: str):
    with isolated_test_database(), ExitStack() as stack:
        initialize_runtime_fence()
        succeeded = status in {"updates_available", "up_to_date"}
        result = EpisodeAuditResult(succeeded, status, "巡检观测结果", data={
            "checked_series_count": 0, "findings": []})
        scheduler = AgentLibraryPatrolScheduler(audit_executor=lambda _args: (result, 0))
        get_bool = library_patrol.config.get_bool
        stack.enter_context(patch.object(library_patrol.config, "get_bool",
            side_effect=lambda key, default=False: True if key == "AGENT_LIBRARY_PATROL_ENABLED" else get_bool(key, default)))
        stack.enter_context(patch("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", return_value=scheduler))
        write = "library.run_patrol"
        session, store, _ = completion_session([write], [(write, {})])
        context = {"owner": "patrol-outcome-owner", "session_id": "patrol-outcome-session"}
        preview = [e async for e in session.run(AgentInput(**context, request_id="patrol-preview",
            message="执行一次全库巡检并回报真实结果。", channel="web"))]
        assert preview[-1].payload["status"] == "approval_required"
        plan_id = (await store.load(**context)).pending_effect_plan_id
        async def finish_before_wait(accepted, **options):
            assert scheduler.run_once() == 1
            return await wait_for_effect_completion(accepted, timeout_seconds=0, **options)
        with patch.object(effects, "wait_for_effect_completion", finish_before_wait):
            confirmed = [e async for e in session.confirm(**context, plan_id=plan_id,
                request_id="patrol-confirm", channel="web")]
        effect = next(e for e in confirmed if e.type.value in {"effect.completed", "effect.failed"})
        final = effect.payload["result"]
        assert final["status"] == status
        assert final["ok"] == succeeded
        assert effect.type.value == ("effect.completed" if succeeded else "effect.failed")
        assert confirmed[-1].payload["status"] == ("success" if succeeded else "partial")
        saved = await SQLiteKernelStore().load(**context)
        wait = saved.metadata["effect_waits"][plan_id]
        assert wait["delivered"] and wait["final_result"] == final
        native_tool = next(r for r in saved.conversation if r.get("role") == "tool" and r.get("effect_plan_id") == plan_id)
        assert json.loads(native_tool["content"]) == final
        # A successful read of a failed patrol stays a successful READ query.
        read = patrol_status()
        assert read["ok"] and read["status"] == status


async def assert_organize_identity(status: str, later_count: int):
    with isolated_test_database(), ExitStack() as stack:
        initialize_runtime_fence()
        manager = OrganizeTaskManager()
        active = {"manager": manager, "status": status}
        sources = [{"id": "source", "name": "Source"}]
        rules = OrganizeRules(target_dir_id="target", clean_empty=False,
            link_strm=False, notify_enabled=False, library_notify=False)
        client = Mock()
        client.logged_in = True
        client.credential_generation = 0
        client.file_info.return_value = SimpleNamespace(is_dir=True, parent_id="0")
        stack.enter_context(patch.object(organize_service, "configured_inputs", return_value=(sources, rules, "")))
        stack.enter_context(patch.object(organize_service, "GuangYaClient", return_value=client))
        stack.enter_context(patch.object(organize_service, "get_organize_manager", side_effect=lambda: active["manager"]))
        stack.enter_context(patch("app.modules.organize_tasks.get_organize_manager", side_effect=lambda: active["manager"]))
        stack.enter_context(patch("app.modules.telegram_organize_lifecycle.publish_organize_lifecycle", return_value=False))
        def remote_observation(_self, _source, _rules, **options):
            if options.get("dry_run"):
                return [], {"total": 1, "matched": 1, "failed": 0, "scan_errors": []}
            if active["status"] == "failed":
                raise OSError("transient provider list unavailable")
            return [], {"total": 1, "matched": 1, "skipped": 1, "failed": 0, "scan_errors": []}
        stack.enter_context(patch.object(Organizer, "organize", remote_observation))
        write = "cloud.organize"
        session, store, _ = completion_session([write], [(write, {})])
        context = {"owner": "organize-identity-owner", "session_id": "organize-identity-session"}
        preview = [e async for e in session.run(AgentInput(**context,
            request_id="organize-preview", message="执行一次光鸭整理并核验结果。", channel="web"))]
        assert preview[-1].payload["status"] == "approval_required", preview[-1].payload
        plan_id = (await store.load(**context)).pending_effect_plan_id
        async def first_read_fails(accepted, **options):
            worker = manager._worker
            if worker:
                worker.join(5)
                assert not worker.is_alive()
            with patch.object(observers, "_poll", side_effect=RuntimeError("temporary reader failure")):
                return await wait_for_effect_completion(accepted, timeout_seconds=0, **options)
        from app.agent import effect_completion
        with patch.object(effect_completion, "wait_for_effect_completion", first_read_fails):
            confirmed = [e async for e in session.confirm(**context, plan_id=plan_id,
                request_id="organize-confirm", channel="web")]
        assert confirmed[-1].payload["status"] == "waiting"
        wait = (await store.load(**context)).metadata["effect_waits"][plan_id]
        assert wait["state"] == "pending" and not wait["delivered"] and "final_result" not in wait
        seed = json.loads(_completion_cipher().decrypt(wait["sealed"].encode()))
        tracker = observers._CompletionTracker(seed["tracker"]["kind"], seed["tracker"]["value"])
        task_id = tracker.value["task_id"]
        persisted = db.get_task_run(tracker.value["run_id"])
        assert persisted is not None
        assert json.loads(persisted["result"])["task_id"] == task_id
        assert persisted["status"] == ("failed" if status == "failed" else "success")
        _, warm_status, _ = await observers._poll(tracker, ToolContext(**context))
        assert warm_status == status
        active["status"] = "completed"
        for _ in range(later_count):
            accepted = manager.start(sources, rules, client=client)
            assert accepted["ok"]
            worker = manager._worker
            if worker:
                worker.join(5)
                assert not worker.is_alive()
        if later_count:
            assert manager.task_result(task_id) is None
            _, after_eviction, _ = await observers._poll(tracker, ToolContext(**context))
            assert after_eviction == status
        cold_manager = OrganizeTaskManager()
        active["manager"] = cold_manager
        # Run the actual startup recovery; no synthetic task_runs/operation_jobs mirror.
        db.init_db()
        cold_manager.resume()
        try:
            assert cold_manager.task_result(task_id) is None
            cold_snapshot = ToolResult(**observe_organize_task(tracker.value))
            assert cold_snapshot.data["task"]["status"] == status
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 0
            future = time.time() + 11
            cold_store = SQLiteKernelStore(clock=lambda: future)
            with patch.object(effects.time, "time", return_value=future):
                delivered = await effects.poll_effect_receipts(cold_store)
            assert delivered == 1
            saved = await cold_store.load(**context)
            final = saved.metadata["effect_waits"][plan_id]["final_result"]
            assert final["status"] == status and final["ok"] == (status == "completed")
            assert saved.metadata["effect_waits"][plan_id]["delivered"]
            native_tool = next(r for r in saved.conversation if r.get("role") == "tool" and r.get("effect_plan_id") == plan_id)
            assert json.loads(native_tool["content"]) == final
            assert task_id not in json.dumps(final)
        finally:
            assert cold_manager.shutdown(timeout=1)
            if cold_manager._operation_dispatcher:
                cold_manager._operation_dispatcher.join(2)
                assert not cold_manager._operation_dispatcher.is_alive()


async def assert_local_file_outcome(root: Path, mode: str):
    with isolated_test_database():
        initialize_runtime_fence()
        source_root, target_root = root / "downloads", root / "library"
        source_root.mkdir()
        target_root.mkdir()
        movie = source_root / "Movie.2026.mkv"
        movie.write_bytes(b"video")
        source_id = local_media_repo.create_local_media_source(name="source", qb_profile="", qb_path_prefix="",
            local_root=str(source_root), stable_seconds=0, mode=mode, owner="admin")
        local_media_repo.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
        service = LocalMediaService(scraper=cast(TMDBScraper, FakeScraper(MatchResult(
            tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0))))
        service_patch = patch("app.modules.local_media_tasks.get_local_media_service", return_value=service)
        service_patch.start()
        try:
            inspection = service.inspect_source("admin", source_id, movie)
            preview = service.preview("admin", inspection["inspection_id"], tmdb_id="1", media_type="movie")
            task_id = service.create_manual_task("admin", inspection["inspection_id"], preview_digest=preview["preview_digest"])
            local_media_repo.update_local_media_task(task_id, status="failed", error="transient prewrite failure")
            read, write = "local.tasks", "local.retry"
            session, store, script = completion_session([read, write], [
                (read, {"scope": "attention", "limit": 12}), (write, {"task_id": task_id})])
            context = {"owner": "local-outcome-owner", "session_id": "local-outcome-session"}
            events = [e async for e in session.run(AgentInput(**context,
                request_id="local-preview", message="重试第一个本地媒体失败任务并回报实际文件结果。", channel="web"))]
            assert events[-1].payload["status"] == "approval_required", events[-1].payload
            plan_id = (await store.load(**context)).pending_effect_plan_id
            async def finish_before_wait(result, **options):
                assert local_media_repo.claim_local_media_task(task_id, owner="admin")
                service.execute_task("admin", task_id)
                return await wait_for_effect_completion(result, timeout_seconds=0, **options)
            from app.agent.kernel.ports import sdk_effects
            with patch("app.modules.local_media_scheduler.get_local_media_scheduler"), patch.object(
                    sdk_effects.effect_completion, "wait_for_effect_completion", finish_before_wait):
                confirmed = [e async for e in session.confirm(**context, plan_id=plan_id,
                    request_id="local-confirm", channel="web")]
            effect = next(e for e in confirmed if e.type.value == "effect.completed")
            saved = await SQLiteKernelStore().load(**context)
            native_tool = next(r for r in saved.conversation if r.get("role") == "tool" and r.get("effect_plan_id") == plan_id)
            final = json.loads(native_tool["content"])
            assert final == effect.payload["result"]
            assert final["status"] == "completed" and final["ok"]
            expected_outcome, archived = ("preview_only", 0) if mode == "preview_only" else ("archived", 1)
            assert final["data"]["file_outcome"] == expected_outcome
            assert final["data"]["archived_video_count"] == archived
            assert final["data"]["background_job"]["file_outcome"] == expected_outcome
            assert f"归档 {archived} 个视频" in final["summary"]
            assert final["data"]["task_id"] == task_id
            model_tools = [row for row in script.requests[-1]["messages"] if row.get("role") == "tool"]
            model_final = json.loads(model_tools[-1]["content"])
            assert model_final["data"]["file_outcome"] == expected_outcome
            assert model_final["data"]["archived_video_count"] == archived
            assert saved.metadata["effect_waits"][plan_id]["final_result"] == final
            assert confirmed[-1].payload["status"] == "success"
            if mode == "preview_only":
                assert movie.exists() and not list(target_root.rglob("*.mkv"))
                assert "预览已完成" in final["summary"] and "未移动文件" in final["summary"]
            else:
                assert not movie.exists() and len(list(target_root.rglob("*.mkv"))) == 1
        finally:
            service_patch.stop()
            assert service.close()


class AgentCompletionIdentityTests(unittest.TestCase):
    def test_strm_failed_run_survives_later_runs_and_cold_receipt(self):
        for later_count, later_source in ((0, "a"), (19, "a"), (20, "a"), (20, "b")):
            with self.subTest(later_count=later_count, later_source=later_source):
                asyncio.run(assert_strm_identity(later_count, later_source))

    def test_patrol_cycle_is_exact_when_same_second_or_overwritten(self):
        for case in ("same-second", "separate-second", "overwritten-cycle"):
            with self.subTest(case=case):
                asyncio.run(assert_patrol_identity(case))

    def test_patrol_completion_reports_business_outcome(self):
        for status in ("not_configured", "unavailable", "failed", "inconclusive", "up_to_date", "updates_available"):
            with self.subTest(status=status):
                asyncio.run(assert_patrol_outcome(status))

    def test_organize_result_survives_process_restart_and_history_eviction(self):
        for status, later_count in (("failed", 0), ("completed", 0), ("failed", 33)):
            with self.subTest(status=status, later_count=later_count):
                asyncio.run(assert_organize_identity(status, later_count))

    def test_organize_completion_requires_matching_run_and_task_identity(self):
        with isolated_test_database():
            payload = build_organize_result({"moved": 1}, status="completed", task_id="exact-task")
            run_id = db.add_task_run("guangya_organize", "manual", result=json.dumps(payload))
            db.finish_task_run(run_id, "success", result=json.dumps(payload))
            other = db.add_task_run("strm_sync", "manual", result=json.dumps(payload))
            for completion in (
                {"task_id": "another-task", "run_id": run_id, "operation": "run"},
                {"task_id": "exact-task", "run_id": other, "operation": "run"},
                {"task_id": "exact-task", "operation": "run"},
            ):
                with self.subTest(completion=completion):
                    result = ToolResult(**observe_organize_task(completion))
                    self.assertFalse(result.ok)
                    self.assertEqual(result.status, "unknown")

    def test_interrupted_organize_result_is_recovered_by_exact_run(self):
        with isolated_test_database():
            payload = build_organize_result({}, status="running", task_id="interrupted-task")
            run_id = db.add_task_run("guangya_organize", "manual", result=json.dumps(payload))
            db.init_db()
            result = ToolResult(**observe_organize_task({
                "task_id": "interrupted-task", "run_id": run_id, "operation": "run"}))
            self.assertFalse(result.ok)
            self.assertEqual(result.data["task"]["status"], "failed")

    def test_local_preview_and_actual_move_receipts_match_real_files(self):
        for mode in ("preview_only", "move"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                asyncio.run(assert_local_file_outcome(Path(directory), mode))
