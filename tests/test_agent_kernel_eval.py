from __future__ import annotations
from typing import Any

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import eval_agent


class AgentKernelEvalTests(unittest.TestCase):
    def fixture_rows(self) -> list[dict]:
        return [json.loads(line) for line in eval_agent.DEFAULT_FIXTURE.read_text().splitlines()]

    def write_cases(self, rows: list[dict]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cases.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        return path

    def test_current_effect_gate_and_lifecycle_are_independent_of_retired_tool_recall(self) -> None:
        result = eval_agent.evaluate()
        self.assertEqual(result["scope"], "effect_gate_and_lifecycle_only")
        self.assertEqual(result["legacy_corpus"]["status"], "retired_not_scored")
        self.assertEqual(result["legacy_corpus"]["cases"], len(self.fixture_rows()))
        self.assertNotIn("retrieval_recall", result["summary"])
        self.assertTrue(result["summary"]["effect_gate_valid"])
        self.assertEqual(result["summary"]["lifecycle_failed"], 0)
        self.assertGreaterEqual(result["summary"]["lifecycle_cases"], 4)
        self.assertEqual(result["invalid_effect_tools"], [])

    def test_optional_provenance_and_legacy_normalization(self) -> None:
        row: dict[str, Any] = {"id": " x ", "message": " query ", "required": ["library.search", " library.search "]}
        legacy = eval_agent.load_cases(self.write_cases([row]))[0]
        self.assertEqual((legacy.case_id, legacy.message, legacy.required), ("x", "query", ("library.search",)))
        self.assertEqual(legacy.provenance, {"kind": "legacy_unverified"})
        row["provenance"] = {"kind": "synthetic"}
        self.assertEqual(eval_agent.load_cases(self.write_cases([row]))[0].provenance, {"kind": "synthetic"})
        for provenance in (None, {}, {"kind": "production_user_real_model"}, {"kind": "synthetic", "extra": True}):
            with self.subTest(provenance=provenance):
                row["provenance"] = provenance
                with self.assertRaisesRegex(ValueError, "provenance"):
                    eval_agent.load_cases(self.write_cases([row]))

    def test_real_model_evidence_is_self_contained_and_not_final_failure(self) -> None:
        row = self.fixture_rows()[-1]
        case = eval_agent.load_cases(self.write_cases([row]))[0]
        self.assertEqual(case.required, ("library.search", "library.count_series_episodes"))
        provenance = case.provenance
        assert provenance is not None
        self.assertEqual(provenance["kind"], "simulated_user_real_model")
        self.assertEqual(provenance["source_commit"], "abb81acda678b26048e6c85e9f813aeb8f8020e2")
        excerpt = provenance["excerpt"]
        self.assertEqual(case.message, "查一下 Jellyfin 里是否有《9号秘事》，再统计一下《黑镜》实际收录了多少集。只查询现有媒体库，不找资源、不刷新、不播放、不修改。")
        self.assertEqual(excerpt["initial_missing"], ["library.search"])
        self.assertEqual(excerpt["http_status"], 200)
        self.assertEqual((excerpt["final_status"], excerpt["business_pass"]), ("success", True))
        self.assertEqual(excerpt["completed_tool_calls"], [
            {"model_index": 1, "name": "library__count_series_episodes", "arguments": {"query": "黑镜"}},
            {"model_index": 1, "name": "agent__capabilities", "arguments": {"tool_names": ["library.search"]}},
            {"model_index": 2, "name": "library__search", "arguments": {"query": "9号秘事"}},
        ])
        public = json.dumps(row, ensure_ascii=False)
        for private_marker in (".superpowers", "http://", "https://", "Authorization", "Bearer ", "session_id", "user.env"):
            self.assertNotIn(private_marker, public)

    def test_provided_evidence_is_always_validated(self) -> None:
        row = self.fixture_rows()[-1]
        edits = [
            (("kind",), "unknown"),
            (("source_commit",), "main"),
            (("source_commit",), 1),
            (("extra",), True),
            (("evidence_sha256",), {}),
            (("evidence_sha256", "model-request-1.json"), "bad-hash"),
            (("evidence_sha256", "../private.json"), "a" * 64),
            (("excerpt",), {}),
            (("excerpt", "message"), "different user request"),
            (("excerpt", "initial_tools"), [None]),
            (("excerpt", "initial_missing"), []),
            (("excerpt", "http_status"), True),
            (("excerpt", "business_pass"), "true"),
            (("excerpt", "completed_tool_calls"), [{"name": "library__search"}]),
        ]
        for keys, value in edits:
            with self.subTest(keys=keys, value=value):
                candidate = copy.deepcopy(row)
                target = candidate["provenance"]
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaisesRegex(ValueError, "provenance"):
                    eval_agent.load_cases(self.write_cases([candidate]))
        with self.assertRaisesRegex(ValueError, "provenance"):
            eval_agent.load_cases(self.write_cases([{**row, "provenance": {"kind": "simulated_user_real_model"}}]))


    def test_invalid_fixture_fails_closed(self) -> None:
        row = {"id": "x", "message": "test", "required": ["agent.capabilities"]}
        for rows in ([{**row, "required": []}], [{**row, "unknown": 1}], [row, row]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    eval_agent.evaluate(self.write_cases(rows))

    def test_invalid_lifecycle_fixture_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-lifecycle.jsonl"
            path.write_text(
                '{"id":"x","message":"test","result":{},"followup":"done","expected":{}}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                eval_agent.evaluate_lifecycle(path)

    def test_json_cli_is_stable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = eval_agent.main(["--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["scope"], "effect_gate_and_lifecycle_only")
        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertEqual(payload["legacy_corpus"]["status"], "retired_not_scored")


if __name__ == "__main__":
    unittest.main()
