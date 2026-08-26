from __future__ import annotations

import unittest

from app.agent.models import ToolResult
from app.agent.orchestrator import AgentOrchestrator
from app.agent.progress_events import (
    AgentProgressEvent,
    bind_agent_progress_listener,
    emit_agent_progress,
)


class _ReadRegistry:
    def execute(self, name, arguments=None, *, context=None):
        return ToolResult(True, "healthy", "检查完成"), 12


class AgentProgressEventTests(unittest.TestCase):
    def test_listener_is_scoped_to_current_request(self):
        events: list[AgentProgressEvent] = []

        emit_agent_progress("outside")
        with bind_agent_progress_listener(events.append):
            emit_agent_progress("routing")
        emit_agent_progress("outside-again")

        self.assertEqual(events, [AgentProgressEvent(phase="routing")])

    def test_listener_failure_does_not_break_agent_execution(self):
        def broken_listener(_event):
            raise RuntimeError("progress transport unavailable")

        with bind_agent_progress_listener(broken_listener):
            emit_agent_progress("routing")

    def test_orchestrator_emits_real_tool_boundaries_without_arguments(self):
        events: list[AgentProgressEvent] = []
        service = AgentOrchestrator(_ReadRegistry())

        with bind_agent_progress_listener(events.append):
            response = service.invoke(
                "workspace.health",
                {"private": "must-not-be-emitted"},
            )

        self.assertTrue(response["result"]["ok"])
        self.assertEqual(
            events,
            [
                AgentProgressEvent(
                    phase="tool_start",
                    tool_name="workspace.health",
                ),
                AgentProgressEvent(
                    phase="tool_finish",
                    tool_name="workspace.health",
                    ok=True,
                ),
            ],
        )
        self.assertNotIn("must-not-be-emitted", repr(events))


if __name__ == "__main__":
    unittest.main()
