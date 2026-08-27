"""Agent 跨轮续接状态的两阶段提交测试。"""
from __future__ import annotations

import asyncio
import unittest

from app.agent.state_commit import (
    AgentStateCommitBuffer,
    active_agent_state_owns_resource,
    commit_or_defer_agent_state,
    defer_agent_state_commits,
    isolate_agent_resource_results,
    stage_agent_resource_result_ids,
)


class AgentStateCommitBufferTests(unittest.TestCase):
    def test_commit_applies_deferred_state_once(self):
        committed: list[str] = []
        buffer = AgentStateCommitBuffer(owner="owner-a")
        with defer_agent_state_commits(buffer):
            self.assertTrue(
                commit_or_defer_agent_state(lambda: committed.append("done"))
            )
            self.assertEqual(committed, [])

        self.assertEqual(buffer.commit(), 1)
        self.assertEqual(buffer.commit(), 0)
        self.assertEqual(committed, ["done"])

    def test_discard_rejects_late_background_state(self):
        committed: list[str] = []
        buffer = AgentStateCommitBuffer(owner="owner-a")
        with defer_agent_state_commits(buffer):
            self.assertTrue(
                commit_or_defer_agent_state(lambda: committed.append("early"))
            )
            self.assertEqual(buffer.discard(), 1)
            self.assertFalse(
                commit_or_defer_agent_state(lambda: committed.append("late"))
            )

        self.assertEqual(buffer.commit(), 0)
        self.assertEqual(committed, [])

    def test_commit_does_not_count_rejected_guarded_action(self):
        buffer = AgentStateCommitBuffer(owner="owner-a")
        with defer_agent_state_commits(buffer):
            self.assertTrue(commit_or_defer_agent_state(lambda: False))

        self.assertEqual(buffer.commit(), 0)

    def test_to_thread_inherits_deferred_commit_context(self):
        committed: list[str] = []
        buffer = AgentStateCommitBuffer(owner="owner-a")

        async def run() -> None:
            with defer_agent_state_commits(buffer):
                accepted = await asyncio.to_thread(
                    commit_or_defer_agent_state,
                    lambda: committed.append("thread"),
                )
                self.assertTrue(accepted)

        asyncio.run(run())
        self.assertEqual(committed, [])
        self.assertEqual(buffer.commit(), 1)
        self.assertEqual(committed, ["thread"])

    def test_isolated_resource_capability_is_owner_bound_and_scope_limited(self):
        with isolate_agent_resource_results():
            self.assertTrue(stage_agent_resource_result_ids(
                owner="owner-a", result_ids={"resource-result-0002"}
            ))
            self.assertTrue(active_agent_state_owns_resource(
                owner="owner-a", result_id="resource-result-0002"
            ))
            self.assertFalse(active_agent_state_owns_resource(
                owner="owner-b", result_id="resource-result-0002"
            ))

        self.assertFalse(active_agent_state_owns_resource(
            owner="owner-a", result_id="resource-result-0002"
        ))

    def test_staged_resource_capability_is_owner_bound_and_revoked_on_discard(self):
        buffer = AgentStateCommitBuffer(owner="owner-a")
        with defer_agent_state_commits(buffer):
            self.assertTrue(stage_agent_resource_result_ids(
                owner="owner-a", result_ids={"resource-result-0001"}
            ))
            self.assertTrue(active_agent_state_owns_resource(
                owner="owner-a", result_id="resource-result-0001"
            ))
            self.assertFalse(active_agent_state_owns_resource(
                owner="owner-b", result_id="resource-result-0001"
            ))
            buffer.discard()
            self.assertFalse(active_agent_state_owns_resource(
                owner="owner-a", result_id="resource-result-0001"
            ))


if __name__ == "__main__":
    unittest.main()
