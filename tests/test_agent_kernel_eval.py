from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import eval_agent


class AgentKernelEvalTests(unittest.TestCase):
    def test_real_failure_corpus_and_effect_gate_pass(self) -> None:
        result = eval_agent.evaluate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertGreaterEqual(result["summary"]["candidate_count_min"], 6)
        self.assertLessEqual(result["summary"]["candidate_count_max"], 12)
        self.assertTrue(result["summary"]["effect_gate_valid"])
        self.assertEqual(result["invalid_effect_tools"], [])

    def test_invalid_fixture_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.jsonl"
            path.write_text(
                '{"id":"x","message":"test","required":[]}\n', encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                eval_agent.evaluate(path)

    def test_json_cli_is_stable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = eval_agent.main(["--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)


if __name__ == "__main__":
    unittest.main()
