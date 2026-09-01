from __future__ import annotations

import hashlib
import hmac
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.agent.action_history import (
    record_confirmation_claimed,
    record_confirmation_error,
    record_confirmation_interrupted,
    record_confirmed_result,
)
from app.agent.models import RiskLevel, ToolResult
from app.modules.web_secret import get_web_secret
from app.repositories.agent_provider_plans import (
    claim_provider_plan,
    create_provider_plan,
    finish_provider_plan,
)
from tests.support import IsolatedDatabaseTestCase


class _ObservedConnection:
    def __init__(self, connection, *, before_execute=None, after_execute=None):
        self._connection = connection
        self._before_execute = before_execute
        self._after_execute = after_execute

    def execute(self, sql, parameters=()):
        if self._before_execute is not None:
            self._before_execute(sql, parameters)
        cursor = self._connection.execute(sql, parameters)
        if self._after_execute is not None:
            self._after_execute(sql, parameters)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


class AgentPrivacyLifecycleTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _digest(domain: bytes, value: str) -> str:
        return hmac.new(
            get_web_secret().encode("utf-8"), domain + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def test_subject_purge_clears_all_agent_partitions(self):
        owner = "web:test-owner"
        principal = "principal:test-owner"
        now = db.now()
        digests = {
            "action": self._digest(b"mediaflux-agent-action-history:v1\0", owner),
            "session": self._digest(b"mediaflux-agent-session-context:v1\0", owner),
            "confirmation": self._digest(b"mediaflux-agent-confirmation:v1\0", owner),
            "job": self._digest(b"mediaflux-agent-durable-job:v1\0", owner),
            "provider": self._digest(
                b"mediaflux-agent-provider-plan-owner:v1\0", owner
            ),
            "workflow": self._digest(b"mediaflux-agent-missing-workflow:v1\0", owner),
            "tg_action": self._digest(b"mediaflux-telegram-agent-action:v1\0", owner),
            "conversation": self._digest(
                b"mediaflux-agent-conversation-principal:v1\0", principal
            ),
        }
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_action_history(owner_digest,tool_name,risk,status,ok,summary,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (digests["action"], "demo", "read", "ok", 1, "done", now, now),
            )
            conn.execute(
                "INSERT INTO agent_media_preferences("
                "owner_digest,preferred_server,preferred_download_target,revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?)",
                (digests["action"], "emby", "qb", 1, now, now),
            )
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digests["session"], "latest_tool", "{}", time.time() + 60, now),
            )
            conn.execute(
                "INSERT INTO agent_session_context_epochs("
                "owner_digest,context_type,generation,touched_at,updated_at"
                ") VALUES(?,?,?,?,?)",
                (digests["session"], "directory_scrape", 1, time.time(), now),
            )
            conn.execute(
                "INSERT INTO agent_confirmation_epochs(owner_digest,generation,touched_at,updated_at) VALUES(?,?,?,?)",
                (digests["confirmation"], 1, time.time(), now),
            )
            conn.execute(
                "INSERT INTO agent_confirmations(confirmation_id,owner_digest,tool_name,expires_at,owner_generation,created_at) VALUES(?,?,?,?,?,?)",
                ("confirm_privacy_0001", digests["confirmation"], "demo", time.time() + 60, 1, now),
            )
            conn.execute(
                "INSERT INTO agent_jobs(job_id,owner_digest,job_type,dedupe_key,status,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("job_privacy_00000001", digests["job"], "library_episode_audit", "one", "pending", now, now, now),
            )
            conn.execute(
                "INSERT INTO agent_provider_plans("
                "plan_id,owner_digest,session_digest,provider,profile_ref,operation,risk,"
                "status,context_fingerprint,expires_at,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "provider_privacy_0001",
                    digests["provider"],
                    "f" * 64,
                    "demo",
                    "configured:demo",
                    "demo.items.update",
                    "write",
                    "prepared",
                    "context",
                    time.time() + 60,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO organize_operation_jobs("
                "job_id,job_kind,owner_digest,operation,reference,payload_json,"
                "dedupe_digest,status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "a" * 32, "agent_directory_scrape", digests["job"],
                    "目录刮削", "", "{}", "b" * 64, "pending", now, now,
                ),
            )
            conn.execute(
                "INSERT INTO agent_missing_media_workflows(workflow_id,owner_digest,source_tool,title,tmdb_id,season,as_of,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("workflow_privacy_0001", digests["workflow"], "library.search_missing_episode_resources", "Demo", "1", 1, "2026-08-22", "search_ready", now, now),
            )
            conn.execute(
                "INSERT INTO telegram_agent_actions(action_id,owner_digest,action_kind,group_id,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                ("tg_action_privacy_1", digests["tg_action"], "result", "g1", time.time() + 60, now),
            )
            conn.execute(
                "INSERT INTO agent_conversations(principal_digest,session_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (digests["conversation"], "privacy_session_0001", "Demo", now, now),
            )
            conn.execute(
                "INSERT INTO agent_conversation_epochs(principal_digest,session_id,generation,updated_at) VALUES(?,?,?,?)",
                (digests["conversation"], "privacy_session_0001", 1, now),
            )

        deleted = db.purge_agent_subject_data(owner=owner, principal=principal)
        aggregate = {
            key: value
            for key, value in deleted.items()
            if key not in {"provider_plans_scrubbed_running", "provider_plans_deleted"}
        }
        self.assertTrue(all(value == 1 for value in aggregate.values()), deleted)
        self.assertEqual(deleted["provider_plans_scrubbed_running"], 0)
        self.assertEqual(deleted["provider_plans_deleted"], 1)

    def test_ttl_cleanup_only_removes_terminal_records_and_maintenance_runs(self):
        old = "2026-07-01 00:00:00"
        now = "2026-08-22 00:00:00"
        with db.get_conn() as conn:
            for suffix, status in (("done", "succeeded"), ("active", "running")):
                conn.execute(
                    "INSERT INTO agent_jobs(job_id,owner_digest,job_type,dedupe_key,status,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (f"job_ttl_{suffix}_0001", "a" * 64, "library_episode_audit", suffix, status, now, old, old),
                )
            for suffix, state in (("done", "visible"), ("active", "verification_pending")):
                conn.execute(
                    "INSERT INTO agent_missing_media_workflows(workflow_id,owner_digest,source_tool,title,tmdb_id,season,as_of,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"workflow_ttl_{suffix}", "b" * 64, "library.search_missing_episode_resources", "Demo", "1", 1, "2026-07-01", state, old, old),
                )
            for suffix, status in (("done", "succeeded"), ("active", "running")):
                conn.execute(
                    "INSERT INTO agent_provider_plans("
                    "plan_id,owner_digest,session_digest,provider,profile_ref,operation,risk,"
                    "status,context_fingerprint,expires_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"provider_ttl_{suffix}_0001",
                        "c" * 64,
                        "d" * 64,
                        "demo",
                        "configured:demo",
                        "demo.items.update",
                        "write",
                        status,
                        "context",
                        time.time() + 60,
                        old,
                        old,
                    ),
                )
        with db.get_conn() as conn:
            for suffix, status in (("done", "completed"), ("active", "running")):
                conn.execute(
                    "INSERT INTO organize_operation_jobs("
                    "job_id,job_kind,owner_digest,operation,reference,payload_json,"
                    "dedupe_digest,status,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        ("c" if suffix == "done" else "d") * 32,
                        "agent_directory_scrape", "e" * 64, "目录刮削", "", "{}",
                        ("f" if suffix == "done" else "1") * 64, status, old, old,
                    ),
                )
        result = db.purge_expired_agent_task_history(
            current_time=now, next_cleanup_at="2026-08-23 00:00:00",
            terminal_before="2026-08-01 00:00:00", limit_per_table=50,
        )
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["provider_plans"], 1)
        self.assertEqual(result["organize_operation_jobs"], 1)
        self.assertEqual(result["missing_media_workflows"], 1)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_provider_plans").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_missing_media_workflows").fetchone()[0], 1)
        maintenance = db.maintain_sqlite_database(incremental_pages=1)
        self.assertTrue(maintenance["optimized"])
        self.assertGreaterEqual(maintenance["freelist_before"], maintenance["freelist_after"])

    def test_subject_purge_prevents_late_confirmation_audit_recreation(self):
        contract = {
            "version": 1,
            "action": "执行 Provider 写计划",
            "object": "敏感目标",
            "impact": "执行冻结操作",
            "reversibility": "需人工核对",
            "preflight_at": "2026-09-01T12:00:00+08:00",
            "risk": "write",
            "preflight_summary": "敏感目标名称",
        }
        plan_ref = "PP-" + "A" * 24
        terminal_writers = (
            lambda owner, confirmation_id, generation: record_confirmed_result(
                owner=owner,
                tool_name="provider.change.execute",
                risk=RiskLevel.WRITE,
                result=ToolResult(
                    True,
                    "succeeded",
                    "Provider 写计划已完成",
                    data={"plan_ref": plan_ref, "status": "succeeded"},
                ),
                elapsed_ms=1,
                confirmation_contract=contract,
                confirmation_id=confirmation_id,
                owner_generation=generation,
            ),
            lambda owner, confirmation_id, generation: record_confirmation_error(
                owner=owner,
                tool_name="provider.change.execute",
                risk=RiskLevel.WRITE,
                code="confirmation_stale",
                confirmation_contract=contract,
                confirmation_id=confirmation_id,
                owner_generation=generation,
            ),
            lambda owner, confirmation_id, generation: record_confirmation_interrupted(
                owner=owner,
                confirmation_id=confirmation_id,
                owner_generation=generation,
                tool_name="provider.change.execute",
                risk=RiskLevel.WRITE,
                confirmation_contract=contract,
            ),
        )

        for index, terminal_writer in enumerate(terminal_writers, start=1):
            owner = f"web:late-provider-audit-{index}"
            confirmation_id = f"late-provider-confirmation-{index}"
            generation = index
            record_confirmation_claimed(
                owner=owner,
                confirmation_id=confirmation_id,
                owner_generation=generation,
                tool_name="provider.change.execute",
                risk=RiskLevel.WRITE,
                confirmation_contract=contract,
                action_arguments={"plan_ref": plan_ref},
            )
            owner_digest = self._digest(
                b"mediaflux-agent-action-history:v1\0", owner
            )
            with db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agent_action_history WHERE owner_digest=?",
                        (owner_digest,),
                    ).fetchone()[0],
                    1,
                )

            db.purge_agent_subject_data(owner=owner)
            terminal_writer(owner, confirmation_id, generation)

            with db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agent_action_history WHERE owner_digest=?",
                        (owner_digest,),
                    ).fetchone()[0],
                    0,
                )

    def test_running_provider_plan_is_scrubbed_then_removed_by_finisher(self):
        owner = "web:running-provider-owner"
        plan = create_provider_plan(
            owner=owner,
            session_id="session-running-provider",
            provider="demo",
            profile_ref="configured:demo",
            operation="demo.items.update",
            risk="write",
            arguments={"private": "secret"},
            target_snapshot={"title": "Sensitive target"},
            context_fingerprint="context-running-provider",
        )
        claim_provider_plan(
            owner=owner,
            session_id="session-running-provider",
            plan_ref=plan["plan_ref"],
            expected_context="context-running-provider",
        )

        deleted = db.purge_agent_subject_data(owner=owner)

        self.assertEqual(deleted["provider_plans"], 1)
        self.assertEqual(deleted["provider_plans_scrubbed_running"], 1)
        self.assertEqual(deleted["provider_plans_deleted"], 0)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT owner_digest,session_digest,provider,profile_ref,operation,"
                "arguments_json,target_snapshot_json,result_json,"
                "context_fingerprint,error_code,status "
                "FROM agent_provider_plans WHERE plan_id=?",
                (plan["plan_ref"].removeprefix("provider_plan:"),),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["owner_digest"], self._digest(
            b"mediaflux-agent-provider-plan-owner:v1\0", owner
        ))
        self.assertRegex(str(row["owner_digest"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(row["session_digest"]), r"^[0-9a-f]{64}$")
        self.assertEqual(row["provider"], "")
        self.assertEqual(row["profile_ref"], "")
        self.assertEqual(row["operation"], "")
        self.assertEqual(row["arguments_json"], "{}")
        self.assertEqual(row["target_snapshot_json"], "{}")
        self.assertEqual(row["result_json"], "{}")
        self.assertEqual(row["context_fingerprint"], "")
        self.assertEqual(row["error_code"], "privacy_purge_pending")
        self.assertEqual(row["status"], "running")

        finish_provider_plan(
            plan_ref=plan["plan_ref"],
            status="succeeded",
            result={"private": "must-not-persist"},
            summary="done",
        )
        with db.get_conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM agent_provider_plans WHERE plan_id=?",
                (plan["plan_ref"].removeprefix("provider_plan:"),),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_provider_plan_purge_first_never_rewrites_sensitive_finish_payload(self):
        owner = "web:provider-purge-first-owner"
        session_id = "session-provider-purge-first"
        plan = create_provider_plan(
            owner=owner,
            session_id=session_id,
            provider="demo",
            profile_ref="configured:demo",
            operation="demo.items.update",
            risk="write",
            arguments={"private": "purge-first-argument"},
            target_snapshot={"title": "purge-first-target"},
            context_fingerprint="context-provider-purge-first",
        )
        claim_provider_plan(
            owner=owner,
            session_id=session_id,
            plan_ref=plan["plan_ref"],
            expected_context="context-provider-purge-first",
        )
        first_statement_gate = threading.Barrier(2)
        allow_finish = threading.Event()
        first_statements: list[str] = []
        finish_updates: list[tuple[str, str]] = []
        finish_errors: list[Exception] = []
        original_get_conn = db.get_conn

        def before_finish_execute(sql, parameters):
            normalized_sql = " ".join(str(sql).split()).upper()
            if not first_statements:
                first_statements.append(normalized_sql)
                first_statement_gate.wait(timeout=5)
                if not allow_finish.wait(timeout=5):
                    raise TimeoutError("finisher 未获准继续")
            if normalized_sql.startswith(
                "UPDATE AGENT_PROVIDER_PLANS SET STATUS=?"
            ):
                finish_updates.append((str(parameters[1]), str(parameters[2])))

        @contextmanager
        def observed_finish_connection():
            with original_get_conn() as connection:
                yield _ObservedConnection(
                    connection, before_execute=before_finish_execute
                )

        def run_finish() -> None:
            try:
                finish_provider_plan(
                    plan_ref=plan["plan_ref"],
                    status="succeeded",
                    result={"private": "purge-first-result-must-not-persist"},
                    summary="purge-first-summary-must-not-persist",
                )
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - 主线程统一断言
                finish_errors.append(exc)

        finisher = threading.Thread(
            target=run_finish, name="provider-purge-first-finisher", daemon=True
        )
        with patch(
            "app.repositories.agent_provider_plans.get_conn",
            new=observed_finish_connection,
        ):
            finisher.start()
            try:
                first_statement_gate.wait(timeout=5)
                deleted = db.purge_agent_subject_data(owner=owner)
            finally:
                allow_finish.set()
                finisher.join(timeout=5)

        self.assertFalse(finisher.is_alive())
        self.assertEqual(finish_errors, [])
        self.assertEqual(deleted["provider_plans"], 1)
        self.assertEqual(first_statements, ["BEGIN IMMEDIATE"])
        self.assertEqual(finish_updates, [("{}", "")])
        plan_id = str(plan["plan_ref"])
        with db.get_conn() as connection:
            row = connection.execute(
                "SELECT result_json,summary,context_fingerprint "
                "FROM agent_provider_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            leaked = connection.execute(
                "SELECT COUNT(*) FROM agent_provider_plans "
                "WHERE result_json LIKE ? OR summary LIKE ?",
                ("%purge-first-result-must-not-persist%", "%purge-first-summary%"),
            ).fetchone()[0]
        self.assertIsNone(row)
        self.assertEqual(leaked, 0)

    def test_provider_plan_finish_first_is_removed_by_concurrent_subject_purge(self):
        owner = "web:provider-finish-first-owner"
        session_id = "session-provider-finish-first"
        plan = create_provider_plan(
            owner=owner,
            session_id=session_id,
            provider="demo",
            profile_ref="configured:demo",
            operation="demo.items.update",
            risk="write",
            arguments={"private": "finish-first-argument"},
            target_snapshot={"title": "finish-first-target"},
            context_fingerprint="context-provider-finish-first",
        )
        claim_provider_plan(
            owner=owner,
            session_id=session_id,
            plan_ref=plan["plan_ref"],
            expected_context="context-provider-finish-first",
        )
        finish_updated_gate = threading.Barrier(2)
        allow_finish_commit = threading.Event()
        purge_begin_gate = threading.Barrier(2)
        purge_acquired = threading.Event()
        finish_errors: list[Exception] = []
        purge_errors: list[Exception] = []
        purge_results: list[dict[str, int]] = []
        finish_statements: list[str] = []
        original_get_conn = db.get_conn
        purge_thread_name = "provider-finish-first-purge"

        def before_finish_execute(sql, _parameters):
            if not finish_statements:
                finish_statements.append(" ".join(str(sql).split()).upper())

        def after_finish_execute(sql, _parameters):
            normalized_sql = " ".join(str(sql).split()).upper()
            if normalized_sql.startswith(
                "UPDATE AGENT_PROVIDER_PLANS SET STATUS=?"
            ):
                finish_updated_gate.wait(timeout=5)
                if not allow_finish_commit.wait(timeout=5):
                    raise TimeoutError("finisher 未获准提交")

        @contextmanager
        def observed_finish_connection():
            with original_get_conn() as connection:
                yield _ObservedConnection(
                    connection,
                    before_execute=before_finish_execute,
                    after_execute=after_finish_execute,
                )

        def before_purge_execute(sql, _parameters):
            if (
                threading.current_thread().name == purge_thread_name
                and " ".join(str(sql).split()).upper() == "BEGIN IMMEDIATE"
            ):
                purge_begin_gate.wait(timeout=5)

        def after_purge_execute(sql, _parameters):
            if (
                threading.current_thread().name == purge_thread_name
                and " ".join(str(sql).split()).upper() == "BEGIN IMMEDIATE"
            ):
                purge_acquired.set()

        @contextmanager
        def observed_database_connection():
            with original_get_conn() as connection:
                yield _ObservedConnection(
                    connection,
                    before_execute=before_purge_execute,
                    after_execute=after_purge_execute,
                )

        def run_finish() -> None:
            try:
                finish_provider_plan(
                    plan_ref=plan["plan_ref"],
                    status="succeeded",
                    result={"private": "finish-first-result-must-not-persist"},
                    summary="finish-first-summary-must-not-persist",
                )
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - 主线程统一断言
                finish_errors.append(exc)

        def run_purge() -> None:
            try:
                purge_results.append(db.purge_agent_subject_data(owner=owner))
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - 主线程统一断言
                purge_errors.append(exc)

        finisher = threading.Thread(
            target=run_finish, name="provider-finish-first-finisher", daemon=True
        )
        purger = threading.Thread(
            target=run_purge, name=purge_thread_name, daemon=True
        )
        with patch(
            "app.repositories.agent_provider_plans.get_conn",
            new=observed_finish_connection,
        ), patch.object(db, "get_conn", new=observed_database_connection):
            finisher.start()
            try:
                finish_updated_gate.wait(timeout=5)
                purger.start()
                purge_begin_gate.wait(timeout=5)
                self.assertFalse(purge_acquired.is_set())
            finally:
                allow_finish_commit.set()
                finisher.join(timeout=5)
                if purger.ident is not None:
                    purger.join(timeout=5)

        self.assertFalse(finisher.is_alive())
        self.assertFalse(purger.is_alive())
        self.assertEqual(finish_errors, [])
        self.assertEqual(purge_errors, [])
        self.assertEqual(finish_statements, ["BEGIN IMMEDIATE"])
        self.assertTrue(purge_acquired.is_set())
        self.assertEqual(len(purge_results), 1)
        self.assertEqual(purge_results[0]["provider_plans"], 1)
        plan_id = str(plan["plan_ref"])
        with db.get_conn() as connection:
            row = connection.execute(
                "SELECT result_json,summary,context_fingerprint "
                "FROM agent_provider_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            leaked = connection.execute(
                "SELECT COUNT(*) FROM agent_provider_plans "
                "WHERE result_json LIKE ? OR summary LIKE ?",
                ("%finish-first-result-must-not-persist%", "%finish-first-summary%"),
            ).fetchone()[0]
        self.assertIsNone(row)
        self.assertEqual(leaked, 0)
