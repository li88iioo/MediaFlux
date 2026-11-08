"""Agent 跨轮续接状态的两阶段提交测试。"""
from __future__ import annotations

import asyncio
import unittest

from app.agent.state_commit import (
    AgentStateCommitBuffer,
    commit_or_defer_agent_state,
    defer_agent_state_commits,
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




if __name__ == "__main__":
    unittest.main()
