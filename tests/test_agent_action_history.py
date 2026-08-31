"""Agent 受确认动作审计历史的持久化、脱敏与查询测试。"""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.action_history import (
    action_history_arguments,
    action_history_owner_digest,
    list_action_history,
    record_confirmed_result,
)
from app.agent.confirmation import ConfirmationStore, SQLiteConfirmationStore
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, agent_action_history_request
from app.agent.owner_routes import web_agent_owner
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


OWNER = "test-action-history-owner"
OWNER_DIGEST = "a" * 64


class AgentActionHistoryCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._database = isolated_test_database("agent-action-history.db")
        self._database.__enter__()

    def tearDown(self) -> None:
        self._database.__exit__(None, None, None)

    def test_database_orders_filters_and_bounds_results(self):
        first = db.add_agent_action_history(
            owner_digest=OWNER_DIGEST,
            tool_name="strm.run_once",
            risk="danger",
            status="accepted",
            ok=True,
            summary="STRM 手动同步：已提交",
            safe_details={"accepted": True, "trigger": "manual"},
            elapsed_ms=12,
        )
        second = db.add_agent_action_history(
            owner_digest=OWNER_DIGEST,
            tool_name="rss.retry_failed_to_qb",
            risk="danger",
            status="failed",
            ok=False,
            summary="RSS 失败条目重试：执行失败",
            safe_details={"submitted": 0, "failed": 2},
            error_code="confirmation_stale",
            elapsed_ms=4,
        )
        self.assertGreater(second, first)
        rows = db.list_agent_action_history(owner_digest=OWNER_DIGEST, limit=10)
        self.assertEqual([row["tool_name"] for row in rows], [
            "rss.retry_failed_to_qb", "strm.run_once",
        ])
        self.assertEqual(len(db.list_agent_action_history(owner_digest=OWNER_DIGEST, limit=10, outcome="success")), 1)
        self.assertEqual(len(db.list_agent_action_history(owner_digest=OWNER_DIGEST, limit=10, outcome="failed")), 1)
        with self.assertRaises(ValueError):
            db.list_agent_action_history(owner_digest=OWNER_DIGEST, limit=51)
        with self.assertRaises(ValueError):
            db.add_agent_action_history(
                owner_digest=OWNER_DIGEST,
                tool_name="strm.run_once",
                risk="read",
                status="accepted",
                ok=True,
                summary="invalid",
            )
        with self.assertRaises(ValueError):
            db.add_agent_action_history(
                owner_digest=OWNER_DIGEST,
                tool_name="strm.run_once",
                risk="danger",
                status="accepted",
                ok=True,
                summary="invalid",
                safe_details={"nested": {"path": "/private"}},
            )

    def test_first_write_repairs_missing_history_table_in_legacy_database(self):
        with db.get_conn() as conn:
            conn.execute("DROP TABLE agent_action_history")

        history_id = db.add_agent_action_history(
            owner_digest=OWNER_DIGEST,
            tool_name="telegram.send_test_notification",
            risk="low_write",
            status="executing",
            ok=False,
            summary="Telegram 测试通知：执行中",
            confirmation_id="legacy-ticket-1",
        )

        self.assertGreater(history_id, 0)
        row = db.list_agent_action_history(
            owner_digest=OWNER_DIGEST,
            limit=1,
        )[0]
        self.assertEqual(row["status"], "executing")
        with db.get_conn() as conn:
            confirmation = conn.execute(
                "SELECT confirmation_id FROM agent_action_history WHERE id=?",
                (history_id,),
            ).fetchone()
            indexes = {
                str(index["name"])
                for index in conn.execute("PRAGMA index_list(agent_action_history)")
            }
        self.assertEqual(confirmation["confirmation_id"], "legacy-ticket-1")
        self.assertIn("idx_agent_action_history_confirmation", indexes)
        self.assertIn("idx_agent_action_history_owner_id", indexes)

    def test_failed_filter_excludes_in_progress_and_unknown_outcomes(self):
        owner_digest = action_history_owner_digest(OWNER)
        for status in ("executing", "outcome_unknown"):
            db.add_agent_action_history(
                owner_digest=owner_digest,
                tool_name="strm.run_once",
                risk="danger",
                status=status,
                ok=False,
                summary=f"STRM 手动同步：{status}",
            )
        db.add_agent_action_history(
            owner_digest=owner_digest,
            tool_name="strm.run_once",
            risk="danger",
            status="failed",
            ok=False,
            summary="STRM 手动同步：执行失败",
        )

        failed = db.list_agent_action_history(
            owner_digest=owner_digest,
            limit=10,
            outcome="failed",
        )
        self.assertEqual([row["status"] for row in failed], ["failed"])
        all_items = list_action_history(
            {"limit": 10, "outcome": "all"},
            ToolContext(owner=OWNER),
        ).data["items"]
        outcomes = {item["status"]: item["outcome"] for item in all_items}
        self.assertEqual(outcomes["executing"], "pending")
        self.assertEqual(outcomes["outcome_unknown"], "unknown")

    def test_projection_keeps_only_strict_safe_fields(self):
        record_confirmed_result(
            owner=OWNER,
            tool_name="indexer.submit_candidate",
            risk=RiskLevel.DANGER,
            result=ToolResult(
                ok=True,
                status="completed",
                summary="raw magnet:?xt=urn:btih:secret /private/path",
                data={
                    "target": "qb",
                    "status": "completed",
                    "created": True,
                    "succeeded": 1,
                    "failed": 0,
                    "duplicate": False,
                    "result_id": "secret-result",
                    "request_id": "secret-request",
                    "magnet": "magnet:?xt=urn:btih:secret",
                    "path": "/private/path",
                },
                error="token=secret",
            ),
            elapsed_ms=8,
        )
        row = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )[0]
        details = json.loads(row["safe_details"])
        self.assertEqual(details, {
            "created": True,
            "duplicate": False,
            "failed": 0,
            "status": "completed",
            "succeeded": 1,
            "target": "qb",
        })
        serialized = json.dumps(dict(row), ensure_ascii=False)
        for secret in ("secret-result", "secret-request", "magnet:", "/private", "token="):
            self.assertNotIn(secret, serialized)

    def test_confirmation_contract_round_trips_through_safe_action_history(self):
        contract = {
            "version": 1,
            "action": "提交资源下载",
            "object": "你刚才选择的资源候选",
            "impact": "会向所选下载目标创建任务。",
            "reversibility": "可在目标下载器中暂停或删除。",
            "preflight_at": "2026-08-09T12:34:56+08:00",
            "risk": "danger",
            "preflight_summary": "预检通过:目标下载器可用。",
        }
        record_confirmed_result(
            owner=OWNER,
            tool_name="indexer.submit_candidate",
            risk=RiskLevel.DANGER,
            result=ToolResult(
                ok=True,
                status="completed",
                summary="done",
                data={"target": "qb", "created": True},
            ),
            elapsed_ms=9,
            confirmation_contract=contract,
        )

        result = list_action_history(
            {"limit": 20, "outcome": "all"},
            ToolContext(owner=OWNER),
        )
        self.assertEqual(result.data["items"][0]["confirmation"], contract)

        row = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )[0]
        stored = json.loads(row["safe_details"])
        self.assertEqual(stored["contract_risk"], "danger")
        self.assertEqual(
            stored["contract_preflight_summary"],
            "预检通过:目标下载器可用。",
        )
        serialized = json.dumps(stored, ensure_ascii=False)
        self.assertNotIn("confirmation_id", serialized)
        self.assertNotIn("magnet:", serialized)

    def test_long_confirmation_summary_is_truncated_to_database_scalar_limit(self):
        contract = {
            "version": 1,
            "action": "批量刷新 RSS",
            "object": "当前已启用的订阅源",
            "impact": "会抓取最新条目。",
            "reversibility": "刷新请求无法撤回。",
            "preflight_at": "2026-08-09T12:34:56+08:00",
            "risk": "write",
            "preflight_summary": "订阅" * 100,
        }
        record_confirmed_result(
            owner=OWNER,
            tool_name="rss.refresh_subscriptions",
            risk=RiskLevel.WRITE,
            result=ToolResult(
                ok=True,
                status="completed",
                summary="done",
                data={"requested": 101, "refreshed": 101, "failed": 0},
            ),
            elapsed_ms=9,
            confirmation_contract=contract,
        )

        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )
        self.assertEqual(len(rows), 1)
        stored = json.loads(rows[0]["safe_details"])
        self.assertLessEqual(len(stored["contract_preflight_summary"]), 128)

    def test_organize_stop_history_keeps_only_requested_boolean(self):
        record_confirmed_result(
            owner=OWNER,
            tool_name="guangya.organize.stop",
            risk=RiskLevel.DANGER,
            result=ToolResult(
                ok=True,
                status="accepted",
                summary="raw secret-task /private/path",
                data={
                    "accepted": True,
                    "task_id": "secret-task",
                    "path": "/private/path",
                },
            ),
            elapsed_ms=5,
        )
        row = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )[0]
        self.assertEqual(row["tool_name"], "guangya.organize.stop")
        self.assertEqual(json.loads(row["safe_details"]), {"accepted": True})
        serialized = json.dumps(dict(row), ensure_ascii=False)
        self.assertNotIn("secret-task", serialized)
        self.assertNotIn("/private/path", serialized)

    def test_local_media_action_history_keeps_tool_identity_and_safe_counts(self):
        cases = (
            (
                "local_media.retry_task",
                {
                    "operation": "retry",
                    "task_number": 2,
                    "affected": 1,
                    "runtime_refreshed": True,
                    "path": "/private/source",
                },
            ),
            (
                "local_media.refresh_task_library",
                {
                    "operation": "precise_refresh",
                    "task_number": 3,
                    "refreshed": 1,
                    "matched_paths": 2,
                    "library_id": "private-library",
                    "server_url": "http://private-server",
                },
            ),
        )
        for tool_name, data in cases:
            record_confirmed_result(
                owner=OWNER,
                tool_name=tool_name,
                risk=RiskLevel.LOW_WRITE,
                result=ToolResult(True, "completed", "done", data=data),
                elapsed_ms=3,
            )
        items = list_action_history(
            {"limit": 10, "outcome": "all"}, ToolContext(owner=OWNER)
        ).data["items"]
        by_tool = {item["tool"]: item for item in items}
        self.assertIn("local_media.retry_task", by_tool)
        self.assertIn("local_media.refresh_task_library", by_tool)
        self.assertEqual(
            by_tool["local_media.retry_task"]["details"],
            {
                "operation": "retry",
                "task_number": 2,
                "affected": 1,
                "runtime_refreshed": True,
            },
        )
        self.assertEqual(
            by_tool["local_media.refresh_task_library"]["details"],
            {
                "operation": "precise_refresh",
                "task_number": 3,
                "refreshed": 1,
                "matched_paths": 2,
            },
        )
        serialized = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("/private", serialized)
        self.assertNotIn("private-library", serialized)
        self.assertNotIn("private-server", serialized)

    def test_canonical_cleanup_history_keeps_only_safe_counts(self):
        record_confirmed_result(
            owner=OWNER,
            tool_name="guangya.organize.cleanup.execute",
            risk=RiskLevel.DANGER,
            result=ToolResult(
                ok=True,
                status="completed",
                summary="raw secret-source /private/path",
                data={
                    "empty_dir_count": 4,
                    "residual_dir_count": 1,
                    "selected_count": 3,
                    "kept_count": 2,
                    "sources": [{"id": "secret-source", "path": "/private/path"}],
                },
            ),
            elapsed_ms=5,
        )
        row = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )[0]
        self.assertEqual(row["tool_name"], "guangya.organize.cleanup.execute")
        self.assertEqual(
            json.loads(row["safe_details"]),
            {
                "empty_dir_count": 4,
                "kept_count": 2,
                "residual_dir_count": 1,
                "selected_count": 3,
            },
        )
        serialized = json.dumps(dict(row), ensure_ascii=False)
        self.assertNotIn("secret-source", serialized)
        self.assertNotIn("/private/path", serialized)

    def test_canonical_rename_history_keeps_one_tool_identity(self):
        record_confirmed_result(
            owner=OWNER,
            tool_name="guangya.rename.execute",
            risk=RiskLevel.DANGER,
            result=ToolResult(
                ok=True,
                status="accepted",
                summary="queued",
                data={
                    "queued": True,
                    "queue_position": 1,
                    "replayed": False,
                    "rename_count": 3,
                    "requires_manual": False,
                },
            ),
            elapsed_ms=5,
        )

        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER), limit=1
        )
        self.assertEqual({row["tool_name"] for row in rows}, {"guangya.rename.execute"})
        self.assertEqual(
            json.loads(rows[0]["safe_details"]),
            {
                "queue_position": 1,
                "queued": True,
                "rename_count": 3,
                "replayed": False,
                "requires_manual": False,
            },
        )

    def test_confirm_records_success_stale_and_replay_only_once(self):
        context = {"value": "one"}
        registry = ToolRegistry()
        def prepare_run_once(_arguments: dict) -> tuple[ToolResult, str]:
            return (
                ToolResult(True, "confirmation_required", "preview"),
                context["value"],
            )

        def confirm_run_once(
            _arguments: dict, expected_context: str
        ) -> ToolResult:
            if context["value"] != expected_context:
                raise AgentToolError(
                    "STRM 运行上下文已变化，请重新确认",
                    code="confirmation_stale",
                )
            return ToolResult(
                True,
                "accepted",
                "raw /private/path",
                data={
                    "accepted": True,
                    "trigger": "manual",
                    "path": "/private/path",
                },
            )

        registry.register(ToolSpec(
            name="strm.run_once",
            description="test",
            risk=RiskLevel.DANGER,
            parameters={},
            validator=lambda arguments: {},
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(prepare_run_once),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(confirm_run_once),
            requires_confirmation=True,
        ))
        store = ConfirmationStore(token_factory=lambda: "ticket-1234567890abcdef")
        service = AgentOrchestrator(registry, store, record_actions=True)

        prepared = service.prepare("strm.run_once", {}, owner="owner")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "accepted")
        success = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=10
        )
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["status"], "accepted")
        self.assertNotIn("/private", success[0]["safe_details"])

        prepared = service.prepare("strm.run_once", {}, owner="owner")
        context["value"] = "two"
        confirmation_id = prepared["action_plan"]["plan_id"]
        with self.assertRaises(AgentToolError) as stale:
            service.confirm(confirmation_id, owner="owner")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=10
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["error_code"], "confirmation_stale")
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner")
        self.assertEqual(
            len(db.list_agent_action_history(
                owner_digest=action_history_owner_digest("owner"), limit=10
            )),
            2,
        )

    def test_execution_ledger_failure_prevents_untracked_side_effect(self):
        calls: list[str] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="strm.run_once",
            description="test",
            risk=RiskLevel.DANGER,
            parameters={},
            validator=lambda arguments: {},
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(lambda arguments: (
                ToolResult(True, "confirmation_required", "preview"),
                "strm-run-once",
            )),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(lambda arguments, _expected_context: (
                calls.append("executed")
                or ToolResult(True, "accepted", "done")
            )),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-abcdefghijklmnop"),
            record_actions=True,
        )
        prepared = service.prepare("strm.run_once", {}, owner="owner")
        with patch("app.agent.action_history.db.add_agent_action_history", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                service.confirm(
                    prepared["action_plan"]["plan_id"],
                    owner="owner",
                )
        self.assertEqual(calls, [])

    def test_interrupted_confirm_is_persisted_as_outcome_unknown(self):
        registry = ToolRegistry()

        def interrupt(_arguments, _expected_context):
            raise KeyboardInterrupt("simulated process interruption")

        registry.register(ToolSpec(
            name="strm.run_once",
            description="test",
            risk=RiskLevel.DANGER,
            parameters={},
            validator=lambda arguments: {},
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(lambda arguments: (
                ToolResult(True, "confirmation_required", "preview"),
                "strm-run-once",
            )),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(interrupt),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            SQLiteConfirmationStore(),
            record_actions=True,
        )
        prepared = service.prepare("strm.run_once", {}, owner="owner")
        confirmation_id = prepared["action_plan"]["plan_id"]

        with self.assertRaises(KeyboardInterrupt):
            service.confirm(confirmation_id, owner="owner")

        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"),
            limit=10,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "outcome_unknown")
        self.assertEqual(rows[0]["error_code"], "execution_interrupted")
        self.assertEqual(rows[0]["risk"], "danger")
        with db.get_conn() as conn:
            stored = conn.execute(
                "SELECT confirmation_id FROM agent_action_history"
            ).fetchone()
        self.assertTrue(
            str(stored["confirmation_id"]).startswith(f"{confirmation_id}-")
        )
        with self.assertRaises(AgentToolError) as replay:
            service.confirm(confirmation_id, owner="owner")
        self.assertEqual(replay.exception.code, "confirmation_invalid")

    def test_startup_marks_orphaned_executing_action_as_unknown(self):
        confirmation_id = "orphaned-confirmation-1234567890"
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest("owner"),
            tool_name="strm.run_once",
            risk="danger",
            status="executing",
            ok=False,
            summary="STRM 手动同步：执行中",
            confirmation_id=confirmation_id,
        )

        db.init_db()

        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"),
            limit=1,
        )
        self.assertEqual(rows[0]["status"], "outcome_unknown")
        self.assertEqual(rows[0]["error_code"], "execution_interrupted")

    def test_read_tool_arguments_and_natural_language_request_are_strict(self):
        self.assertEqual(action_history_arguments({}), {"limit": 20, "outcome": "all"})
        self.assertEqual(
            agent_action_history_request("查看最近 5 条 Agent 失败操作历史"),
            {"limit": 5, "outcome": "failed"},
        )
        self.assertIsNone(agent_action_history_request("查看下载历史"))
        with self.assertRaises(AgentToolError):
            action_history_arguments({"limit": 10, "token": "secret"})
        with self.assertRaisesRegex(ValueError, "1 到 50"):
            agent_action_history_request("查看最近 99 条 Agent 操作历史")

    def test_read_tool_returns_safe_rows(self):
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER),
            tool_name="config.set_feature_state",
            risk="low_write",
            status="completed",
            ok=True,
            summary="功能开关修改：已完成",
            safe_details={"feature": "discovery", "enabled": False},
            elapsed_ms=3,
        )
        result = list_action_history(
            {"limit": 20, "outcome": "all"}, ToolContext(owner=OWNER)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["items"][0]["details"], {
            "feature": "discovery", "enabled": False,
        })

    def test_read_tool_resanitizes_polluted_persisted_rows(self):
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest(OWNER),
            tool_name="strm.run_once",
            risk="danger",
            status="token-secret",
            ok=False,
            summary="magnet:?xt=secret /private/path token=secret",
            safe_details={"accepted": True, "token": "secret", "path": "/private"},
            error_code="private_error",
            elapsed_ms=3,
            finished_at="token-secret",
        )
        item = list_action_history(
            {"limit": 1, "outcome": "all"}, ToolContext(owner=OWNER)
        ).data["items"][0]
        self.assertEqual(item["tool"], "strm.run_once")
        self.assertEqual(item["status"], "unavailable")
        self.assertEqual(item["summary"], "STRM 手动同步：暂时不可用")
        self.assertEqual(item["details"], {"accepted": True})
        self.assertEqual(item["error_code"], "")
        self.assertEqual(item["finished_at"], "")
        serialized = json.dumps(item, ensure_ascii=False)
        for secret in ("magnet:", "/private", "token=", "token-secret", "private_error"):
            self.assertNotIn(secret, serialized)

    def test_history_is_isolated_by_owner_and_requires_identity(self):
        owner_a = "owner-a"
        owner_b = "owner-b"
        for owner, status in ((owner_a, "accepted"), (owner_b, "completed")):
            db.add_agent_action_history(
                owner_digest=action_history_owner_digest(owner),
                tool_name="strm.run_once",
                risk="danger",
                status=status,
                ok=True,
                summary="STRM 手动同步：已记录",
            )

        result_a = list_action_history(
            {"limit": 20, "outcome": "all"}, ToolContext(owner=owner_a)
        )
        result_b = list_action_history(
            {"limit": 20, "outcome": "all"}, ToolContext(owner=owner_b)
        )
        self.assertEqual([item["status"] for item in result_a.data["items"]], ["accepted"])
        self.assertEqual([item["status"] for item in result_b.data["items"]], ["completed"])
        with self.assertRaises(AgentToolError) as missing:
            list_action_history(
                {"limit": 20, "outcome": "all"}, ToolContext()
            )
        self.assertEqual(missing.exception.code, "identity_required")

    def test_history_retention_keeps_latest_two_thousand_rows(self):
        timestamp = db.now()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO agent_action_history("
                "owner_digest,tool_name,risk,status,ok,mode,summary,safe_details,error_code,"
                "elapsed_ms,started_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (OWNER_DIGEST, "strm.run_once", "danger", "accepted", 1, "confirmed_action",
                     "STRM 手动同步：已提交", "{}", "", 1, timestamp, timestamp)
                    for _ in range(2000)
                ],
            )
        newest = db.add_agent_action_history(
            owner_digest=OWNER_DIGEST,
            tool_name="strm.run_once",
            risk="danger",
            status="accepted",
            ok=True,
            summary="STRM 手动同步：已提交",
            elapsed_ms=1,
        )
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_action_history").fetchone()[0]
            oldest = conn.execute("SELECT MIN(id) FROM agent_action_history").fetchone()[0]
        self.assertEqual(count, 2000)
        self.assertEqual(oldest, newest - 1999)

    def test_history_retention_does_not_evict_another_owner(self):
        other_owner = action_history_owner_digest("owner-b")
        db.add_agent_action_history(
            owner_digest=other_owner,
            tool_name="strm.run_once",
            risk="danger",
            status="accepted",
            ok=True,
            summary="另一会话的历史",
        )
        for _ in range(2001):
            db.add_agent_action_history(
                owner_digest=OWNER_DIGEST,
                tool_name="strm.run_once",
                risk="danger",
                status="accepted",
                ok=True,
                summary="当前会话的历史",
            )
        with db.get_conn() as conn:
            current_count = conn.execute(
                "SELECT COUNT(*) FROM agent_action_history WHERE owner_digest=?",
                (OWNER_DIGEST,),
            ).fetchone()[0]
            other_count = conn.execute(
                "SELECT COUNT(*) FROM agent_action_history WHERE owner_digest=?",
                (other_owner,),
            ).fetchone()[0]
        self.assertEqual(current_count, 2000)
        self.assertEqual(other_count, 1)

    def test_history_global_cap_keeps_new_record_and_evicts_oldest_fairly(self):
        owners = [action_history_owner_digest(f"owner-{index}") for index in range(4)]
        with patch("app.database._AGENT_ACTION_HISTORY_GLOBAL_LIMIT", 3):
            for index, owner in enumerate(owners):
                db.add_agent_action_history(
                    owner_digest=owner,
                    tool_name="strm.run_once",
                    risk="danger",
                    status="accepted",
                    ok=True,
                    summary=f"审计 {index}",
                )
        with db.get_conn() as conn:
            stored = conn.execute(
                "SELECT owner_digest FROM agent_action_history ORDER BY id"
            ).fetchall()
        self.assertEqual([row["owner_digest"] for row in stored], owners[1:])

    def test_history_global_cap_prefers_owner_with_largest_share(self):
        owner_a = action_history_owner_digest("owner-large-share")
        owner_b = action_history_owner_digest("owner-small-share")
        owner_c = action_history_owner_digest("owner-new-share")
        with patch("app.database._AGENT_ACTION_HISTORY_GLOBAL_LIMIT", 3):
            for summary in ("A1", "A2"):
                db.add_agent_action_history(
                    owner_digest=owner_a, tool_name="strm.run_once",
                    risk="danger", status="accepted", ok=True, summary=summary,
                )
            db.add_agent_action_history(
                owner_digest=owner_b, tool_name="strm.run_once",
                risk="danger", status="accepted", ok=True, summary="B1",
            )
            newest_id = db.add_agent_action_history(
                owner_digest=owner_c, tool_name="strm.run_once",
                risk="danger", status="accepted", ok=True, summary="C1",
            )
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,owner_digest,summary FROM agent_action_history ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [(row["owner_digest"], row["summary"]) for row in rows],
            [(owner_a, "A2"), (owner_b, "B1"), (owner_c, "C1")],
        )
        self.assertEqual(int(rows[-1]["id"]), newest_id)

    def test_history_global_cap_converges_from_legacy_overflow(self):
        owner_a = action_history_owner_digest("legacy-owner-a")
        owner_b = action_history_owner_digest("legacy-owner-b")
        owner_c = action_history_owner_digest("legacy-owner-c")
        with db.get_conn() as conn:
            for owner, summary in (
                (owner_a, "A1"), (owner_a, "A2"), (owner_a, "A3"),
                (owner_b, "B1"), (owner_b, "B2"),
            ):
                conn.execute(
                    "INSERT INTO agent_action_history("
                    "owner_digest,tool_name,risk,status,ok,mode,summary,"
                    "safe_details,error_code,elapsed_ms,started_at,finished_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        owner, "strm.run_once", "danger", "accepted", 1,
                        "confirmed_action", summary, "{}", "", 0,
                        "2026-08-01", "2026-08-01",
                    ),
                )
        with patch("app.database._AGENT_ACTION_HISTORY_GLOBAL_LIMIT", 3):
            newest_id = db.add_agent_action_history(
                owner_digest=owner_c, tool_name="strm.run_once",
                risk="danger", status="accepted", ok=True, summary="C1",
            )
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,owner_digest,summary FROM agent_action_history ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [(row["owner_digest"], row["summary"]) for row in rows],
            [(owner_a, "A3"), (owner_b, "B2"), (owner_c, "C1")],
        )
        self.assertEqual(int(rows[-1]["id"]), newest_id)


class AgentActionHistoryAPITests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app())
        self.client.__enter__()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_api_query_requires_auth_and_returns_persisted_history(self):
        unauthorized = self.client.post(
            "/api/agent/query", json={"session_id": "test_session_identifier_0001", "message": "查看 Agent 操作历史"}
        )
        self.assertEqual(unauthorized.status_code, 401)
        csrf = self.login()
        db.add_agent_action_history(
            owner_digest=action_history_owner_digest(web_agent_owner(csrf, session_id="action_history_http_0001")),
            tool_name="strm.run_once",
            risk="danger",
            status="accepted",
            ok=True,
            summary="STRM 手动同步：已提交",
            safe_details={"accepted": True, "trigger": "manual"},
            elapsed_ms=5,
        )
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"message": "查看最近 1 条 Agent 操作历史", "session_id": "action_history_http_0001"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["tool_call"]["name"], "agent.action_history")
        self.assertEqual(body["result"]["data"]["count"], 1)
        self.assertNotIn("confirmation_id", response.text)
        self.assertNotIn(csrf, response.text)
    def test_api_rejects_excessive_history_limit(self):
        csrf = self.login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "查看最近 99 条 Agent 操作历史"},
        )
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
