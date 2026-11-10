"""本轮 Agent jobs 的业务结果验收：每类独立可运行，无真实 Provider。"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from app import database as db
from app.repositories import agent_library_patrol as agent_library_patrol_repo
from app.repositories import agent_jobs as agent_jobs_repo
from app.modules.library_patrol import empty_patrol_projection
from app.modules.library_audit_jobs import (
    audit_job,
    prepare_start_audit,
    start_audit,
)
from app.modules.episode_audit import EpisodeAuditResult
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests.support import isolated_test_database

AS_OF = "2026-09-07"
OWNER = "deep-audit-owner"
NOW = datetime(2099, 1, 1)


@pytest.fixture(autouse=True)
def isolated_jobs():
    with (
        isolated_test_database(),
        patch("socket.socket.connect", side_effect=AssertionError("禁止实际网络连接")),
    ):
        yield


def create_job(*, checkpoint=None, projection=None):
    row, created = agent_jobs_repo.create_agent_job(
        owner=OWNER,
        job_type="library_episode_audit",
        dedupe_key=f"{AS_OF}:2",
        input_json=json.dumps({"as_of": AS_OF, "max_series": 2}),
        checkpoint_json=json.dumps(
            checkpoint or {"as_of": AS_OF, "cursor": "", "stall_attempts": 0}
        ),
        projection_json=json.dumps(
            projection
            if projection is not None
            else empty_patrol_projection(as_of=AS_OF)
        ),
    )
    assert created
    return str(row["job_id"])


def batch(*, checked=1, missing=0, cursor="", status=None):
    status = status or ("updates_available" if missing else "up_to_date")
    return EpisodeAuditResult(
        True,
        status,
        "已核对",
        data={
            "as_of": AS_OF,
            "max_series": 2,
            "checked_series_count": checked,
            "mapped_series_count": checked,
            "unmapped_series_count": 0,
            "updates_available_count": int(bool(missing)),
            "missing_episode_count": missing,
            "inconclusive_count": 0,
            "findings": [],
            "findings_truncated": bool(missing),
            "continuation_pending": bool(cursor),
            "last_processed_tmdb_id": cursor,
            "stalled_tmdb_id": "",
        },
    )


def run_worker(executor):
    worker = AgentJobsScheduler(audit_executor=executor, clock=lambda: NOW)
    assert worker.run_once() == 1
    return worker


def load(job_id):
    row = agent_jobs_repo.get_agent_job(owner=OWNER, job_id=job_id)
    assert row is not None
    return dict(row)


def test_normal_confirmed_audit_has_persisted_business_receipt():
    context = OWNER
    arguments = {"as_of": AS_OF, "max_series": 2}
    preview, fingerprint = prepare_start_audit(owner=context, **arguments)
    assert preview["ok"] and preview["status"] == "confirmation_required"
    with patch("app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler") as scheduler:
        accepted = start_audit(owner=context, expected_snapshot=fingerprint, **arguments)
    assert accepted["ok"] and accepted["status"] == "accepted"
    assert accepted["data"]["created"] is True
    scheduler.return_value.wake.assert_called_once()
    job_id = accepted["data"]["job_id"]
    executor = Mock(return_value=batch(checked=2))
    run_worker(executor)
    executor.assert_called_once_with({**arguments, "after_tmdb_id": ""})
    assert load(job_id)["status"] == "succeeded"
    receipt = audit_job(owner=context, **{"job_id": job_id, "limit": 5})
    assert receipt["ok"] and receipt["status"] == "up_to_date"
    assert "2" in receipt["summary"]


def test_batch_cross_module_accumulates_prior_findings_after_restart():
    job_id = create_job()
    run_worker(Mock(return_value=batch(checked=2, missing=3, cursor="100")))
    assert load(job_id)["status"] == "pending"
    second = Mock(return_value=batch(checked=1))
    worker = run_worker(second)  # 新 worker 实例只能从 SQLite 恢复上一批。
    second.assert_called_once_with(
        {"as_of": AS_OF, "max_series": 2, "after_tmdb_id": "100"}
    )
    row = load(job_id)
    assert row["status"] == "succeeded" and row["progress_current"] == 3
    projection = json.loads(row["projection_json"])
    assert projection["missing_episode_count"] == 3
    assert projection["updates_available_count"] == 1
    assert projection["patrol_status"] == "updates_available"
    receipt = audit_job(owner=OWNER, **{"job_id": job_id, "limit": 5})
    assert receipt["status"] == "updates_available"
    assert worker.run_once() == 0


def test_restart_repeat_cancelled_batch_never_publishes_or_reexecutes():
    job_id = create_job()

    def cancel_during_batch(_arguments):
        row, outcome = agent_jobs_repo.cancel_agent_job(owner=OWNER, job_id=job_id)
        assert row is not None
        assert outcome == "requested" and row["cancel_requested"] == 1
        return batch(checked=2, missing=3, cursor="100")

    executor = Mock(side_effect=cancel_during_batch)
    run_worker(executor)
    assert load(job_id)["status"] == "cancelled"
    assert load(job_id)["progress_current"] == 0
    db.init_db()
    restart = AgentJobsScheduler(audit_executor=executor, clock=lambda: NOW)
    assert restart.run_once() == 0 and restart.run_once() == 0
    assert executor.call_count == 1


def test_history_valid_checkpoint_preserves_prior_missing_count():
    previous = empty_patrol_projection(as_of=AS_OF)
    previous.update(
        checked_series_count=2, updates_available_count=1, missing_episode_count=3
    )
    job_id = create_job(
        checkpoint={"as_of": AS_OF, "cursor": "100", "stall_attempts": 0},
        projection=previous,
    )
    db.init_db()
    executor = Mock(return_value=batch(checked=1))
    run_worker(executor)
    projection = json.loads(load(job_id)["projection_json"])
    assert projection["checked_series_count"] == 3
    assert projection["missing_episode_count"] == 3
    assert projection["patrol_status"] == "updates_available"


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"as_of": AS_OF},
        {**empty_patrol_projection(as_of="2026-09-06"), "checked_series_count": 2},
    ],
)
def test_history_missing_progress_cannot_resume_past_unverified_series(invalid):
    """旧/损坏进度不可直接清零但保留游标，否则遗漏前批并误报完整。"""
    job_id = create_job(
        checkpoint={"as_of": AS_OF, "cursor": "100", "stall_attempts": 0},
        projection=invalid,
    )
    executor = Mock(return_value=batch(checked=1))
    run_worker(executor)
    row = load(job_id)
    assert row["status"] != "succeeded", "失去前批历史后不能仅凭尾批宣告全库完整"
    assert row["error_code"]
    executor.assert_not_called()


def test_history_scheduled_patrol_missing_progress_never_publishes_completion():
    """定时与手动巡检共用恢复验证，不得读尾批后把缺失历史当空白历史。"""
    from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler

    agent_library_patrol_repo.ensure_agent_library_patrol(next_run_at="2000-01-01 00:00:00")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_library_patrol SET cycle_as_of=?,"
            "cycle_cursor_tmdb_id='100',cycle_accumulator_json='{}'",
            (AS_OF,),
        )
    executor = Mock(return_value=(batch(checked=1), 0))
    worker = AgentLibraryPatrolScheduler(audit_executor=executor, clock=lambda: NOW)
    with (
        patch.object(worker, "_enabled", return_value=True),
        patch.object(worker, "_notifications_enabled", return_value=False),
    ):
        assert worker.run_once() == 1
    get_agent_library_patrol_result = agent_library_patrol_repo.get_agent_library_patrol()
    assert get_agent_library_patrol_result is not None
    row = dict(get_agent_library_patrol_result)
    assert row["outcome"] != "up_to_date"
    assert row["error_type"] and row["status"] == "retry_wait"
    assert row["cycle_cursor_tmdb_id"] == "100"
    assert row["cycle_accumulator_json"] == "{}"
    executor.assert_not_called()


def test_history_scheduled_patrol_valid_progress_preserves_prior_findings():
    from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler

    previous = empty_patrol_projection(as_of=AS_OF)
    previous.update(
        checked_series_count=2, updates_available_count=1, missing_episode_count=3
    )
    agent_library_patrol_repo.ensure_agent_library_patrol(next_run_at="2000-01-01 00:00:00")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_library_patrol SET cycle_as_of=?,"
            "cycle_cursor_tmdb_id='100',cycle_accumulator_json=?",
            (AS_OF, json.dumps(previous)),
        )
    executor = Mock(return_value=(batch(checked=1), 0))
    worker = AgentLibraryPatrolScheduler(audit_executor=executor, clock=lambda: NOW)
    with (
        patch.object(worker, "_enabled", return_value=True),
        patch.object(worker, "_max_series", return_value=2),
        patch.object(worker, "_notifications_enabled", return_value=False),
    ):
        assert worker.run_once() == 1
    executor.assert_called_once_with(
        {"as_of": AS_OF, "max_series": 2, "after_tmdb_id": "100"}
    )
    get_agent_library_patrol_result_2 = agent_library_patrol_repo.get_agent_library_patrol()
    assert get_agent_library_patrol_result_2 is not None
    row = dict(get_agent_library_patrol_result_2)
    assert row["outcome"] == "updates_available"
    assert row["checked_series_count"] == 3 and row["missing_episode_count"] == 3
    assert row["cycle_cursor_tmdb_id"] == ""
