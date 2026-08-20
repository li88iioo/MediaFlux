"""Agent 最近安全只读操作的会话隔离、过期与自然语言重试测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.recent_read_operations import READ_PLAN_OPERATION, RecentReadOperationStore
from app.agent.registry import ToolRegistry


def _identity(arguments):
    return dict(arguments)


class RecentReadOperationStoreTests(unittest.TestCase):
    def test_owner_isolation_deep_copy_ttl_and_lru(self):
        now = [100.0]
        store = RecentReadOperationStore(
            ttl_seconds=10,
            max_entries=2,
            clock=lambda: now[0],
        )
        source = {"status": "active", "filters": ["queued"]}
        self.assertTrue(
            store.capture(
                owner="owner-a",
                tool_name="downloads.diagnose_queue",
                arguments=source,
            )
        )
        source["filters"].append("mutated")
        captured = store.get(owner="owner-a")
        self.assertEqual(
            captured,
            ("downloads.diagnose_queue", {"status": "active", "filters": ["queued"]}),
        )
        assert captured is not None
        captured[1]["filters"].append("returned-copy")
        self.assertEqual(
            store.get(owner="owner-a"),
            ("downloads.diagnose_queue", {"status": "active", "filters": ["queued"]}),
        )
        self.assertIsNone(store.get(owner="owner-b"))

        self.assertTrue(store.capture(owner="owner-b", tool_name="workspace.health", arguments={}))
        # 读取 owner-a 后它成为最近使用项；新增 owner-c 应淘汰 owner-b。
        self.assertIsNotNone(store.get(owner="owner-a"))
        self.assertTrue(store.capture(owner="owner-c", tool_name="workspace.todo", arguments={}))
        self.assertIsNone(store.get(owner="owner-b"))
        self.assertIsNotNone(store.get(owner="owner-a"))

        now[0] = 111.0
        self.assertIsNone(store.get(owner="owner-a"))
        self.assertIsNone(store.get(owner="owner-c"))

    def test_capture_rejects_unreviewed_or_sensitive_arguments(self):
        store = RecentReadOperationStore()
        rejected = (
            ("write.test", {}),
            ("downloads.diagnose_queue", {"api_key": "secret"}),
            ("downloads.diagnose_queue", {"token": "secret"}),
            ("downloads.diagnose_queue", {"path": "/volume/private/file"}),
            ("downloads.diagnose_queue", {"url": "https://example.invalid/private"}),
        )
        for tool_name, arguments in rejected:
            with self.subTest(tool_name=tool_name, arguments=arguments):
                self.assertFalse(
                    store.capture(
                        owner="owner",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
        self.assertIsNone(store.get(owner="owner"))

    def test_capture_plan_is_atomic_and_rejects_unsafe_step(self):
        store = RecentReadOperationStore()
        steps = [
            ("workspace.health", {}),
            ("downloads.diagnose_queue", {"status": "active"}),
        ]
        self.assertTrue(store.capture_plan(owner="owner", steps=steps))
        self.assertEqual(store.get(owner="owner"), (
            READ_PLAN_OPERATION,
            {"steps": [
                {"tool_name": "workspace.health", "arguments": {}},
                {
                    "tool_name": "downloads.diagnose_queue",
                    "arguments": {"status": "active"},
                },
            ]},
        ))
        self.assertFalse(store.capture_plan(owner="owner", steps=[
            ("workspace.health", {}),
            ("downloads.diagnose_queue", {"url": "https://private.invalid"}),
        ]))


class RecentReadOperationRetryTests(unittest.TestCase):
    def _agent(self):
        calls: list[dict[str, object]] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="downloads.diagnose_queue",
            description="diagnose",
            risk=RiskLevel.READ,
            parameters={"type": "object"},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "healthy", "queue ok", data={"echo": dict(arguments)})
            ),
            validator=_identity,
        ))
        return AgentOrchestrator(registry), calls

    def test_retry_replays_latest_safe_read_for_same_owner(self):
        agent, calls = self._agent()
        first = agent.invoke(
            "downloads.diagnose_queue",
            {"status": "active"},
            owner="owner-a",
        )
        self.assertEqual(first["tool_call"]["arguments"], {"status": "active"})

        retried = agent.query("重试", owner="owner-a", present=False)
        self.assertEqual(retried["tool_call"]["name"], "downloads.diagnose_queue")
        self.assertEqual(retried["tool_call"]["arguments"], {"status": "active"})
        self.assertEqual(calls, [{"status": "active"}, {"status": "active"}])

    def test_retry_is_owner_scoped_and_reset_clears_context(self):
        agent, _calls = self._agent()
        agent.invoke("downloads.diagnose_queue", {}, owner="owner-a")

        other_owner = agent.query("再查一次", owner="owner-b", present=False)
        self.assertEqual(other_owner["result"]["status"], "unsupported")
        self.assertIn("没有可安全重试", other_owner["result"]["summary"])

        reset = agent.reset_session(owner="owner-a")
        self.assertTrue(reset["reset"])
        after_reset = agent.query("重试", owner="owner-a", present=False)
        self.assertEqual(after_reset["result"]["status"], "unsupported")
        self.assertIn("没有可安全重试", after_reset["result"]["summary"])

    def test_retry_invalidates_episode_audit_cache_before_replay(self):
        registry = ToolRegistry()
        calls: list[dict[str, object]] = []

        def normalize(arguments):
            return {
                "query": str(arguments.get("query") or "").strip(),
                "as_of": str(arguments.get("as_of") or "2026-08-14"),
            }

        registry.register(ToolSpec(
            name="library.audit_episodes",
            description="audit",
            risk=RiskLevel.READ,
            parameters={"type": "object"},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "up_to_date", "ok")
            ),
            validator=normalize,
        ))
        agent = AgentOrchestrator(registry)
        arguments = {"query": "  Show  "}
        normalized = {"query": "Show", "as_of": "2026-08-14"}
        first = agent.invoke("library.audit_episodes", arguments, owner="owner")
        self.assertEqual(first["tool_call"]["arguments"], normalized)

        with patch(
            "app.agent.orchestrator.invalidate_episode_audit_cache"
        ) as invalidate:
            retried = agent.query("再查一次", owner="owner", present=False)

        invalidate.assert_called_once_with(normalized)
        self.assertEqual(retried["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(calls, [normalized, normalized])


if __name__ == "__main__":
    unittest.main()
