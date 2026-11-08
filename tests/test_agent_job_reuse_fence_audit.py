"""确认复用的是具体旧任务，不能在写事务等待后悄悄创建或接管另一任务。"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from contextlib import contextmanager
from typing import TypedDict
from unittest.mock import Mock, patch

from app import database as db
from app.modules import library_audit_jobs as actions
from app.modules.library_patrol import empty_patrol_projection
from app.modules.library_audit_jobs import (
    prepare_start_audit,
    start_audit,
)
from app.repositories.agent_jobs import AgentJobError
from tests.support import isolated_test_database

OWNER = "r7-temporary-owner"


class AuditArguments(TypedDict):
    as_of: str
    max_series: int


ARGUMENTS: AuditArguments = {"as_of": "2026-01-01", "max_series": 2}


class AgentJobReuseFenceAuditTests(unittest.TestCase):
    def test_confirmed_reuse_cannot_create_second_job_after_original_commits(self):
        with (
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network forbidden"),
            ),
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")
            ),
            isolated_test_database(),
            patch(
                "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler",
                return_value=Mock(),
            ),
        ):
            projection = json.dumps(empty_patrol_projection(as_of=ARGUMENTS["as_of"]))
            first, created = db.create_agent_job(
                owner=OWNER,
                job_type="library_episode_audit",
                dedupe_key="2026-01-01:2",
                input_json=json.dumps(ARGUMENTS),
                checkpoint_json=json.dumps(
                    {"as_of": ARGUMENTS["as_of"], "cursor": "", "stall_attempts": 0}
                ),
                projection_json=projection,
            )
            self.assertTrue(created)
            first_id = str(first["job_id"])
            running = db.claim_due_agent_job(job_type="library_episode_audit")
            assert running is not None
            self.assertEqual(running["job_id"], first_id)
            context = OWNER
            preview, fingerprint = prepare_start_audit(owner=context, **ARGUMENTS)
            self.assertTrue(preview["data"]["reused"])
            self.assertEqual(fingerprint["dedupe_active_id"], first_id)

            completion_uncommitted = threading.Event()
            allow_completion_commit = threading.Event()
            confirmation_checked = threading.Event()
            outcomes: dict[str, bool | dict] = {}
            failures = []
            real_get_conn = db.get_conn
            real_create = actions.jobs.create_agent_job

            @contextmanager
            def gated_connection():
                with real_get_conn() as conn:
                    yield conn
                    if threading.current_thread().name == "r7-existing-job-completion":
                        # 正式 complete_agent_job 已执行 UPDATE，但其 SQLite 提交尚未完成。
                        completion_uncommitted.set()
                        if not allow_completion_commit.wait(3):
                            raise AssertionError("completion gate timed out")

            def observed_create(**kwargs):
                # 公共service已通过同范围活动任务的预检快照复核；
                # 随后的真实 BEGIN IMMEDIATE 必须等待旧 job 的成功事务提交。
                confirmation_checked.set()
                return real_create(**kwargs)

            def complete_existing():
                try:
                    assert running is not None
                    outcomes["completed"] = db.complete_agent_job(
                        first_id,
                        expected_lease_generation=int(running["lease_generation"]),
                        projection_json=projection,
                        progress_current=0,
                        progress_total=0,
                        summary="原有全库巡检正常完成",
                    )
                except BaseException as exc:
                    failures.append(exc)

            def confirm_reuse():
                try:
                    outcomes["confirmed"] = start_audit(
                        owner=context, expected_snapshot=fingerprint, **ARGUMENTS
                    )
                except BaseException as exc:
                    failures.append(exc)

            completer = threading.Thread(
                target=complete_existing, name="r7-existing-job-completion"
            )
            confirmer = threading.Thread(
                target=confirm_reuse, name="r7-confirm-existing-job"
            )
            with (
                patch.object(db, "get_conn", gated_connection),
                patch.object(actions.jobs, "create_agent_job", observed_create),
            ):
                try:
                    completer.start()
                    self.assertTrue(completion_uncommitted.wait(2))
                    confirmer.start()
                    self.assertTrue(
                        confirmation_checked.wait(2), "WAL 读取没有到达确认后的创建入口"
                    )
                finally:
                    allow_completion_commit.set()
                    completer.join(4)
                    if confirmer.ident is not None:
                        confirmer.join(4)
            self.assertFalse(completer.is_alive() or confirmer.is_alive())
            self.assertTrue(outcomes["completed"])
            self.assertEqual(len(failures), 1)
            assert isinstance(failures[0], AgentJobError)
            self.assertEqual(failures[0].code, "confirmation_stale")
            self.assertNotIn("confirmed", outcomes)
            get_agent_job_result = db.get_agent_job(owner=OWNER, job_id=first_id)
            assert get_agent_job_result is not None
            self.assertEqual(get_agent_job_result["status"], "succeeded")
            self.assertEqual(len(db.list_agent_jobs(owner=OWNER)), 1)

    def test_current_reuse_and_other_owner_keep_single_owner_scoped_job(self):
        from app.modules import agent_jobs_scheduler

        with (
            isolated_test_database(),
            patch.object(agent_jobs_scheduler, "get_agent_jobs_scheduler"),
        ):
            args = ARGUMENTS.copy()
            context = OWNER
            _, fingerprint = prepare_start_audit(owner=context, **args)
            first = start_audit(owner=context, expected_snapshot=fingerprint, **args)
            self.assertTrue(first["data"]["created"])
            _, reuse_fingerprint = prepare_start_audit(owner=context, **args)
            reused = start_audit(
                owner=context, expected_snapshot=reuse_fingerprint, **args
            )
            self.assertFalse(reused["data"]["created"])
            self.assertEqual(reused["data"]["job_id"], first["data"]["job_id"])
            other = "other-owner"
            _, other_fingerprint = prepare_start_audit(owner=other, **args)
            second = start_audit(
                owner=other, expected_snapshot=other_fingerprint, **args
            )
            self.assertTrue(second["data"]["created"])
            self.assertNotEqual(second["data"]["job_id"], first["data"]["job_id"])
            self.assertEqual(len(db.list_agent_jobs(owner=OWNER)), 1)

    def test_cancelled_reuse_cannot_attach_to_new_active_replacement(self):
        from app.modules import agent_jobs_scheduler

        with (
            isolated_test_database(),
            patch.object(agent_jobs_scheduler, "get_agent_jobs_scheduler"),
        ):
            args = ARGUMENTS.copy()
            context = OWNER
            _, fingerprint = prepare_start_audit(owner=context, **args)
            first = start_audit(owner=context, expected_snapshot=fingerprint, **args)
            _, reuse_fingerprint = prepare_start_audit(owner=context, **args)
            real_create = actions.jobs.create_agent_job

            def replace_before_transaction(**kwargs):
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE agent_jobs SET status='cancelled' WHERE job_id=?",
                        (first["data"]["job_id"],),
                    )
                replacement_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key != "expected_active_job_id"
                }
                replacement, created = real_create(**replacement_kwargs)
                self.assertTrue(created)
                self.assertNotEqual(replacement["job_id"], first["data"]["job_id"])
                return real_create(**kwargs)

            with patch.object(
                actions.jobs, "create_agent_job", side_effect=replace_before_transaction
            ):
                with self.assertRaises(AgentJobError) as raised:
                    start_audit(
                        owner=context, expected_snapshot=reuse_fingerprint, **args
                    )
            self.assertEqual(raised.exception.code, "confirmation_stale")
            self.assertEqual(len(db.list_agent_jobs(owner=OWNER)), 2)
