from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.agent.kernel.bootstrap import build_agent_kernel

FIXTURE = Path(__file__).parent / "fixtures" / "agent_kernel_capability_cases.jsonl"


class NoModel:
    async def stream(self, request, *, cancellation):
        if False:  # pragma: no cover
            yield request


class AgentKernelCapabilityCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.session = build_agent_kernel(model=NoModel())

    def test_global_capability_question_prefers_agent_catalog_over_cloud_catalog(
        self,
    ) -> None:
        selection = self.session.retriever.retrieve(
            "你是谁？你能做什么？",
            self.session.catalog,
            context={
                "owner": "corpus-owner",
                "session_id": "corpus-session",
                "channel": "test",
                "reference_kinds": (),
            },
        )
        self.assertIn("agent.capabilities", selection.names)
        self.assertGreater(
            selection.scores["agent.capabilities"],
            selection.scores["guangya.capabilities"],
        )

    def test_real_failure_corpus_retrieves_required_atomic_capabilities(self) -> None:
        failures: list[str] = []
        for raw_line in FIXTURE.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            case = json.loads(raw_line)
            selection = self.session.retriever.retrieve(
                case["message"],
                self.session.catalog,
                context={
                    "owner": "corpus-owner",
                    "session_id": "corpus-session",
                    "channel": "test",
                    "reference_kinds": (),
                },
            )
            selected = set(selection.names)
            missing = [name for name in case["required"] if name not in selected]
            if missing:
                failures.append(
                    f"{case['id']}: missing={missing}, selected={list(selection.names)}"
                )
            self.assertGreaterEqual(len(selection.tools), 6)
            self.assertLessEqual(len(selection.tools), 12)
        self.assertEqual(failures, [], "\n".join(failures))
