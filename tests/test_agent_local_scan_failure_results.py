"""扫描来源失败必须贯穿实际确认、持久批次、查询与整理终态。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app import config, database as db
from app.repositories import local_media as local_media_repo
from app.agent.action_history import read_actions, record_confirmed_result
from app.modules.local_media_service import prepare_local_source_scan, scan_local_sources
from app.modules.local_media_tasks import local_tasks
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.kernel.bootstrap import get_agent_kernel_runtime, invalidate_agent_kernel_runtime
from app.agent.kernel.effects import InMemoryRateLimiter
from app.agent.models import RiskLevel, ToolContext, ToolResult
from app.bot import handlers
from app.modules.local_media_scan_runs import (
    finish_local_media_scan_report,
    local_media_scan_summary,
    resolve_local_media_scan,
)
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.notifier import render_event
from tests.support import initialized_web_runtime, isolated_test_database
from tests.test_agent_kernel_existing_ports import context_for

OWNER = "webk:v1:" + "b" * 64


def source(root: Path, target: Path, *, name: str = "本地来源") -> int:
    source_id = local_media_repo.create_local_media_source(
        name=name, qb_profile="", qb_path_prefix="", local_root=str(root),
        stable_seconds=0, owner="admin",
    )
    local_media_repo.upsert_local_library_target(source_id, "default", str(target), owner="admin")
    return source_id


def scan_receipt(expected: str) -> ToolResult:
    result = ToolResult(**scan_local_sources(expected_fingerprint=expected))
    if result.data.get("queued_count"):
        result.effect_metadata["completion"] = {"kind": "local_media_scan", "scan_ref": result.data["scan_ref"]}
    return result


@pytest.mark.parametrize("remove_directory", [False, True])
def test_real_confirmation_distinguishes_empty_source_from_disappeared_mount(tmp_path, remove_directory):
    async def scenario():
        with initialized_web_runtime():
            config.set_and_save({"AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            runtime = get_agent_kernel_runtime()
            runtime.session.pipeline.rate_limiter = InMemoryRateLimiter(limit=100)
            root, target = tmp_path / "source", tmp_path / "target"
            root.mkdir()
            target.mkdir()
            source(root, target)
            scheduler = LocalMediaScheduler(service=Mock())
            try:
                context = await context_for(runtime.store, owner=OWNER, session="scan-facts")
                preview = await runtime.session.pipeline.execute("local.scan_sources", {}, context=context)
                assert preview.effect_plan is not None
                if remove_directory:
                    root.rmdir()
                with (
                    patch("app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler),
                    patch.object(scheduler, "start") as start,
                    patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
                ):
                    events = [event async for event in runtime.session.confirm(
                        owner=OWNER, session_id="scan-facts", plan_id=preview.effect_plan.plan_id,
                    )]
                terminal = next(event for event in events if event.type.value in {"effect.completed", "effect.failed"})
                result = terminal.payload["result"]
                assert terminal.type.value == ("effect.failed" if remove_directory else "effect.completed")
                assert result["ok"] is not remove_directory
                assert result["status"] == ("failed" if remove_directory else "completed")
                assert result["data"]["failed_sources"] == int(remove_directory)
                saved = resolve_local_media_scan(result["data"]["scan_ref"])
                assert saved["summary"]["scan_status"] == result["status"]
                row = db.get_task_run(saved["id"])
                assert row is not None
                assert row["status"] == ("failed" if remove_directory else "success")
                if remove_directory:
                    assert result["data"]["sources"][0]["error"] == "目录不存在或不可访问"
                if remove_directory:
                    assert "没有发现" not in repr(result["suggestions"])
                    assert saved["summary"]["scan_errors"][0]["code"] == "source_unavailable"
                start.assert_not_called()
            finally:
                scheduler.shutdown()
                invalidate_agent_kernel_runtime()
    asyncio.run(scenario())


def test_partial_scan_keeps_failure_through_report_query_and_finished_tasks(tmp_path):
    with isolated_test_database():
        root, target = tmp_path / "source", tmp_path / "target"
        root.mkdir()
        target.mkdir()
        (root / "Show.S01E01.mkv").write_bytes(b"video")
        source(root, target)
        source(tmp_path / "missing-mount", target, name="失联来源")
        scheduler = LocalMediaScheduler(service=Mock())
        expected = prepare_local_source_scan()["fingerprint"]
        with (
            patch("app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler),
            patch.object(scheduler, "start") as start,
        ):
            accepted = scan_receipt(expected)
        start.assert_called_once()
        assert accepted.ok and accepted.status == "accepted"
        assert accepted.data["scan_status"] == "partial"
        scan_ref = accepted.data["scan_ref"]
        scan = resolve_local_media_scan(scan_ref)
        original_scan_summary = dict(scan["summary"])
        task_id = scan["task_ids"][0]
        local_media_repo.update_local_media_task(task_id, status="completed")
        local_media_repo.add_local_media_task_item(
            task_id, str(root / "Show.S01E01.mkv"), str(target / "Show.S01E01.mkv"),
            role="video", action="move",
        )
        # 通知阶段只补充文件结果，不能改掉发现阶段已经冻结的失败事实。
        finish_local_media_scan_report(scan_ref, {"failed_sources": 0, "completed": 1, "task_outcomes": []})
        saved = resolve_local_media_scan(scan_ref)["summary"]
        assert local_media_scan_summary(saved) == original_scan_summary
        result = asyncio.run(wait_for_effect_completion(
            accepted, tool="local.scan_sources", context=ToolContext(owner="scan-owner"),
        ))
        assert result.status == "partial" and not result.ok
        assert result.data["stats"]["completed"] == 1
        assert result.data["stats"]["failed_sources"] == 1
        assert result.data["failed_sources"] == 1
        assert result.data["scan_errors"][0]["code"] == "source_unavailable"
        query = local_tasks(scan_ref=scan_ref)
        assert query["status"] == "attention"
        assert query["scan_status"] == "partial"
        assert query["failed_sources"] == 1


@pytest.mark.parametrize("all_fail", [False, True])
def test_candidate_enqueue_errors_are_not_lost_or_confused_with_empty_scan(tmp_path, all_fail):
    with isolated_test_database():
        root, target = tmp_path / "source", tmp_path / "target"
        root.mkdir()
        target.mkdir()
        for name in ("A.mkv", "B.mkv"):
            (root / name).write_bytes(b"video")
        source(root, target)
        create = local_media_repo.create_local_media_task

        def enqueue(*args, **kwargs):
            if all_fail or str(args[2]).endswith("A.mkv"):
                raise OSError("private /secret/directory access_token=token")
            return create(*args, **kwargs)

        with patch("app.repositories.local_media.create_local_media_task", side_effect=enqueue):
            result = LocalMediaScheduler(service=Mock()).enqueue_manual_scan_candidates()
        assert result["scan_status"] == ("failed" if all_fail else "partial")
        assert result["queued_count"] == (0 if all_fail else 1)
        assert result["enqueue_failures"] == (2 if all_fail else 1)
        assert result["failed_sources"] == 1
        summary = resolve_local_media_scan(result["scan_ref"])["summary"]
        assert summary["scan_errors"][0]["code"] == "enqueue_failed"
        assert "secret" not in repr(summary)


def test_partial_discovery_preserves_sanitized_failure_and_real_queued_member(tmp_path):
    with isolated_test_database():
        root, target = tmp_path / "source", tmp_path / "target"
        root.mkdir()
        target.mkdir()
        candidate = root / "Show.mkv"
        candidate.write_bytes(b"video")
        source(root, target)
        scheduler = LocalMediaScheduler(service=Mock())
        with patch.object(scheduler, "_source_candidates", return_value=([candidate], "/secret/private-directory")):
            result = scheduler.enqueue_manual_scan_candidates()
        assert result["scan_status"] == "partial"
        assert result["queued_count"] == 1
        assert result["scan_errors"][0]["code"] == "scan_incomplete"
        assert "secret" not in repr(resolve_local_media_scan(result["scan_ref"])["summary"])


def test_failed_empty_scan_remains_visible_in_queries_notifications_and_history(tmp_path):
    with isolated_test_database():
        target = tmp_path / "target"
        target.mkdir()
        source(tmp_path / "missing", target, name="/private/source access_token=secret")
        scheduler = LocalMediaScheduler(service=Mock())
        with patch("app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler):
            report = handlers._run_local_organize_stage(None, progress_title="本地整理")
        notice = render_event(handlers._local_organize_event(report))
        assert "扫描失败" in notice
        assert "暂未发现" not in notice
        assert "/private" not in notice and "access_token" not in notice
        saved = resolve_local_media_scan(report["scan_ref"])
        assert saved["summary"]["scan_status"] == "failed"
        context = ToolContext(owner="scan-owner", session_id="scan-session")
        query = local_tasks(scan_ref=report["scan_ref"])
        assert query["status"] == "attention" and query["items"] == []
        assert query["scan_errors"][0]["code"] == "source_unavailable"
        expected = prepare_local_source_scan()["fingerprint"]
        with patch("app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler):
            failed = scan_receipt(expected)
        record_confirmed_result(
            owner=context.owner, tool_name="local.scan_sources", risk=RiskLevel.WRITE,
            result=failed, elapsed_ms=1,
        )
        history = read_actions(owner=context.owner, **{"limit": 10, "outcome": "all"})
        details = history["items"][0]["details"]
        assert details["scan_status"] == "failed" and details["failed_sources"] == 1
        assert "private" not in repr(history)
