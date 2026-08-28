"""Agent 第10批：持久确认、链路追踪、异常脱敏与指标。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.metrics import AgentMetricsCollector, agent_metrics
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.observability import safe_exception_summary
from app.agent.orchestrator import AgentOrchestrator
from app.agent.registry import AgentToolError, ToolRegistry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class SQLiteConfirmationStoreTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        SQLiteConfirmationStore().reset()

    def test_ticket_survives_store_recreation_and_owner_is_hashed(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "persistent-ticket-1234567890"
        )
        ticket = first.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments={"items": ["one"]},
            context_fingerprint="snapshot",
            followup_context={"episode": 3},
            confirmation_contract={"action": "测试写入"},
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT owner_digest FROM agent_confirmations WHERE confirmation_id=?",
                (ticket.confirmation_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["owner_digest"], "owner-a")
        self.assertRegex(str(row["owner_digest"]), r"^[0-9a-f]{64}$")

        claimed = SQLiteConfirmationStore().claim(
            owner="owner-a", confirmation_id=ticket.confirmation_id
        )
        self.assertEqual(claimed.arguments, {"items": ["one"]})
        self.assertEqual(claimed.context_fingerprint, "snapshot")
        self.assertEqual(claimed.followup_context, {"episode": 3})
        with self.assertRaises(AgentToolError):
            first.claim(owner="owner-a", confirmation_id=ticket.confirmation_id)

    def test_concurrent_claim_is_atomic_across_store_instances(self) -> None:
        issuer = SQLiteConfirmationStore(
            token_factory=lambda: "concurrent-ticket-123456789"
        )
        ticket = issuer.issue(owner="owner-a", tool_name="write.test", arguments={})
        barrier = threading.Barrier(2)

        def claim_once() -> str:
            barrier.wait(timeout=3)
            try:
                SQLiteConfirmationStore().claim(
                    owner="owner-a", confirmation_id=ticket.confirmation_id
                )
            except AgentToolError as exc:
                return exc.code
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _index: claim_once(), range(2)))
        self.assertEqual(outcomes, ["claimed", "confirmation_invalid"])

    def test_claim_and_rotate_is_atomic_for_two_tickets_across_instances(self) -> None:
        tokens = iter((
            "atomic-ticket-first-123456789",
            "atomic-ticket-second-12345678",
        ))
        issuer = SQLiteConfirmationStore(token_factory=lambda: next(tokens))
        tickets = [
            issuer.issue(owner="owner-a", tool_name="write.test", arguments={})
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)

        def claim(ticket_id: str) -> str:
            barrier.wait(timeout=3)
            try:
                SQLiteConfirmationStore().claim_and_rotate_owner(
                    owner="owner-a", confirmation_id=ticket_id
                )
            except AgentToolError as exc:
                return exc.code
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(
                claim, [item.confirmation_id for item in tickets]
            ))
        self.assertEqual(outcomes, ["claimed", "confirmation_invalid"])

    def test_non_replacement_issue_at_capacity_keeps_sqlite_bounded(self) -> None:
        tokens = iter((
            "persistent-capacity-owner-a-old",
            "persistent-capacity-owner-a-new",
        ))
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0))
        store = SQLiteConfirmationStore(
            max_entries=1,
            clock=lambda: next(ticks),
            token_factory=lambda: next(tokens),
        )
        previous = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        current = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "new"}
        )

        self.assertEqual(len(store.list_active_tickets(owner="owner-a")), 1)
        with self.assertRaises(AgentToolError):
            store.claim(owner="owner-a", confirmation_id=previous.confirmation_id)
        self.assertEqual(
            store.claim(
                owner="owner-a", confirmation_id=current.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_replacing_ticket_at_capacity_preserves_other_owner(self) -> None:
        tokens = iter((
            "persistent-capacity-owner-b-1",
            "persistent-capacity-owner-a-old",
            "persistent-capacity-owner-a-new",
        ))
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
        store = SQLiteConfirmationStore(
            max_entries=2,
            clock=lambda: next(ticks),
            token_factory=lambda: next(tokens),
        )
        other = store.issue(
            owner="owner-b", tool_name="write.test", arguments={"id": "b"}
        )
        previous = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        replacement = store.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments={"id": "new"},
            replace_active_ticket=True,
        )

        self.assertEqual(
            store.claim(owner="owner-b", confirmation_id=other.confirmation_id).arguments,
            {"id": "b"},
        )
        with self.assertRaises(AgentToolError):
            store.claim(owner="owner-a", confirmation_id=previous.confirmation_id)
        self.assertEqual(
            store.claim(
                owner="owner-a", confirmation_id=replacement.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_owner_rotation_can_preserve_ticket_across_store_instances(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "preserved-ticket-123456789"
        )
        ticket = first.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )

        revoked, generation = SQLiteConfirmationStore().rotate_owner(
            owner="owner-a", preserve_active=True
        )

        self.assertEqual(revoked, 0)
        active = SQLiteConfirmationStore().list_active_tickets(owner="owner-a")
        self.assertEqual([item.confirmation_id for item in active], [ticket.confirmation_id])
        self.assertEqual(active[0].owner_generation, generation)
        self.assertEqual(
            SQLiteConfirmationStore().claim(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            ).arguments,
            {"id": 1},
        )

    def test_owner_rotation_revokes_tickets_across_instances(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "rotated-ticket-1234567890"
        )
        ticket = first.issue(owner="owner-a", tool_name="write.test", arguments={})
        revoked, generation = SQLiteConfirmationStore().rotate_owner(owner="owner-a")
        self.assertEqual(revoked, 1)
        self.assertGreater(generation, 0)
        with self.assertRaises(AgentToolError):
            first.claim(owner="owner-a", confirmation_id=ticket.confirmation_id)

    def test_orchestrator_prepare_and_confirm_across_default_store_instances(self) -> None:
        calls: list[dict[str, str]] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.test",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={"type": "object"},
            validator=lambda arguments: {"value": str(arguments.get("value") or "")},
            preview_handler=lambda arguments: ToolResult(
                True, "confirmation_required", "preview", data=arguments
            ),
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "completed", "done")
            ),
            requires_confirmation=True,
        ))
        issuer = AgentOrchestrator(registry)
        confirmer = AgentOrchestrator(registry)

        prepared = issuer.prepare(
            "write.test", {"value": "cross-worker"}, owner="owner-cross"
        )
        confirmation_id = prepared["confirmation"]["confirmation_id"]
        confirmed = confirmer.confirm(confirmation_id, owner="owner-cross")

        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertEqual(calls, [{"value": "cross-worker"}])
        with self.assertRaises(AgentToolError):
            issuer.confirm(confirmation_id, owner="owner-cross")


class AgentTraceAndMetricsTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        agent_metrics.reset()

    def tearDown(self) -> None:
        agent_metrics.reset()

    def test_tool_context_receives_request_and_session_ids(self) -> None:
        seen: list[ToolContext] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="trace.read",
            description="trace",
            risk=RiskLevel.READ,
            parameters={},
            validator=lambda arguments: {},
            handler=lambda _arguments: ToolResult(True, "completed", "ok"),
            context_handler=lambda _arguments, context: (
                seen.append(context) or ToolResult(True, "completed", "ok")
            ),
        ))
        service = AgentOrchestrator(registry)
        response = service.invoke(
            "trace.read",
            {},
            owner="owner-a",
            request_id="request-12345678",
            session_id="session-1234567890",
        )
        self.assertEqual(response["request_id"], "request-12345678")
        self.assertEqual(
            seen,
            [ToolContext(
                owner="owner-a",
                request_id="request-12345678",
                session_id="session-1234567890",
            )],
        )

    def test_query_response_preserves_supplied_request_id(self) -> None:
        response = AgentOrchestrator(ToolRegistry()).query(
            "你好",
            request_id="query-request-123456",
            session_id="session-1234567890",
        )
        self.assertEqual(response["request_id"], "query-request-123456")
        self.assertEqual(response["mode"], "conversation")

    def test_safe_exception_summary_redacts_and_truncates(self) -> None:
        summary = safe_exception_summary(
            RuntimeError(
                "request failed https://example.invalid/api?token=super-secret-value "
                + ("detail " * 100)
            ),
            limit=120,
        )
        self.assertLessEqual(len(summary), 120)
        self.assertIn("RuntimeError", summary)
        self.assertNotIn("super-secret-value", summary)
        self.assertNotIn("\n", summary)

    def test_metrics_collector_is_thread_safe_and_bounded(self) -> None:
        collector = AgentMetricsCollector(max_latency_samples=32)

        def record(index: int) -> None:
            collector.record_query(elapsed_ms=index, ok=index % 2 == 0)
            collector.record_tool("trace.read", elapsed_ms=index, ok=True)
            collector.record_confirmation("issued")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(100)))
        snapshot = collector.snapshot()
        self.assertEqual(snapshot["queries"], {"success": 50, "error": 50})
        self.assertEqual(snapshot["tools"]["by_name"]["trace.read"]["success"], 100)
        self.assertEqual(snapshot["confirmations"]["issued"], 100)
        self.assertEqual(snapshot["latency_ms"]["query"]["count"], 32)
        self.assertGreaterEqual(snapshot["latency_ms"]["query"]["p95"], 90)

    def test_metrics_distinguish_llm_failures_usage_breakdown_and_contention(self) -> None:
        collector = AgentMetricsCollector(max_latency_samples=32)
        db._reset_sqlite_contention_metrics_for_tests()
        for outcome in ("timeout", "rate_limited", "invalid_json", "upstream_5xx"):
            collector.record_llm_request(
                "chat_completions",
                "model/test\nunsafe",
                outcome=outcome,
                elapsed_ms=25,
            )
        collector.record_llm_request(
            "chat_completions",
            "model/test\nunsafe",
            outcome="success",
            elapsed_ms=12,
            usage=SimpleNamespace(
                prompt_tokens=30,
                completion_tokens=9,
                cached_tokens=4,
                reasoning_tokens=2,
            ),
        )
        collector.record_query_breakdown(turns=3, llm_ms=80, tools_ms=17)
        db._observe_sqlite_contention(
            db.sqlite3.OperationalError("database is locked"),
            phase="operation",
            elapsed_ms=3,
        )

        snapshot = collector.snapshot()
        provider = snapshot["llm"]["providers"][0]
        self.assertEqual(provider["model"], "model/test_unsafe")
        for outcome in ("timeout", "rate_limited", "invalid_json", "upstream_5xx"):
            self.assertEqual(provider["outcomes"][outcome], 1)
        self.assertEqual(provider["tokens"]["prompt"], 30)
        self.assertEqual(provider["tokens"]["completion"], 9)
        self.assertEqual(snapshot["llm"]["turns"]["max"], 3)
        self.assertEqual(snapshot["llm"]["llm_ms"]["max"], 80)
        self.assertEqual(snapshot["llm"]["tools_ms"]["max"], 17)
        self.assertEqual(snapshot["sqlite_contention"]["locked"], 1)

        prometheus = collector.prometheus()
        self.assertIn('outcome="rate_limited"', prometheus)
        self.assertIn("mediaflux_agent_llm_tokens_total", prometheus)
        self.assertIn("mediaflux_agent_query_breakdown", prometheus)
        self.assertIn("mediaflux_sqlite_contention_total", prometheus)
        self.assertNotIn("\nunsafe", prometheus)

        collector.record_llm_request(
            "responses", "second-model", outcome="success", elapsed_ms=8
        )
        sample_families = []
        for line in collector.prometheus().splitlines():
            if not line or line.startswith("#"):
                continue
            sample_families.append(line.split("{", 1)[0].split(" ", 1)[0])
        completed_families = set()
        previous = ""
        for family in sample_families:
            if family != previous:
                self.assertNotIn(family, completed_families)
                if previous:
                    completed_families.add(previous)
                previous = family
        db._reset_sqlite_contention_metrics_for_tests()


class AgentMetricsApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        agent_metrics.reset()
        self.client = TestClient(create_app(start_background=False))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        agent_metrics.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def _login(self) -> None:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)

    def test_metrics_endpoint_requires_login_and_supports_json_and_prometheus(self) -> None:
        unauthenticated = self.client.get("/api/agent/metrics")
        self.assertEqual(unauthenticated.status_code, 401)
        self._login()
        agent_metrics.record_query(elapsed_ms=12, ok=True)
        agent_metrics.record_tool("trace.read", elapsed_ms=7, ok=True)
        agent_metrics.record_confirmation("issued")

        json_response = self.client.get("/api/agent/metrics")
        self.assertEqual(json_response.status_code, 200, json_response.text)
        self.assertEqual(json_response.json()["queries"]["success"], 1)
        self.assertEqual(
            json_response.json()["tools"]["by_name"]["trace.read"]["success"], 1
        )

        text_response = self.client.get("/api/agent/metrics?format=prometheus")
        self.assertEqual(text_response.status_code, 200, text_response.text)
        self.assertIn("mediaflux_agent_queries_total", text_response.text)
        self.assertIn('tool="trace.read"', text_response.text)
        self.assertTrue(text_response.headers["content-type"].startswith("text/plain"))

    def test_metrics_scrape_key_is_independent_and_scoped_to_metrics(self) -> None:
        from app.routes import agent_api

        original_get = agent_api.config.get

        def configured_value(key: str, default: str = "") -> str:
            if key == "AGENT_METRICS_SCRAPE_KEY":
                return "metrics-only-secret-123456"
            return original_get(key, default)

        with patch.object(agent_api.config, "get", side_effect=configured_value):
            wrong = self.client.get(
                "/api/agent/metrics",
                headers={"Authorization": "Bearer wrong-key"},
            )
            self.assertEqual(wrong.status_code, 401)

            allowed = self.client.get(
                "/api/agent/metrics?format=prometheus",
                headers={"Authorization": "Bearer metrics-only-secret-123456"},
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertIn("mediaflux_agent_queries_total", allowed.text)
            self.assertNotIn("metrics-only-secret-123456", allowed.text)

            other_route = self.client.get(
                "/api/agent/capabilities",
                headers={"Authorization": "Bearer metrics-only-secret-123456"},
            )
            self.assertEqual(other_route.status_code, 401)


if __name__ == "__main__":
    unittest.main()
