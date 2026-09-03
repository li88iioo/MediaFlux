#!/usr/bin/env python3
"""MediaFlux Agent Kernel 离线验收器；不调用 LLM、网络或业务写接口。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.domain_catalog import build_tool_specs
from app.agent.kernel.capabilities import CapabilityRetriever, ToolEffect
from app.agent.kernel.ports import catalog_from_tool_specs

DEFAULT_FIXTURE = Path("tests/fixtures/agent_kernel_capability_cases.jsonl")


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    message: str
    required: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalOutcome:
    case_id: str
    passed: bool
    selected: tuple[str, ...]
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "selected": list(self.selected),
            "missing": list(self.missing),
        }


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line.strip():
            continue
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {"id", "message", "required"}:
            raise ValueError(f"第 {line_number} 行字段无效")
        case_id = str(raw["id"] or "").strip()
        message = str(raw["message"] or "").strip()
        required_raw = raw["required"]
        if (
            not case_id
            or case_id in seen
            or not message
            or not isinstance(required_raw, list)
            or not required_raw
        ):
            raise ValueError(f"第 {line_number} 行案例无效")
        required = tuple(
            dict.fromkeys(str(item or "").strip() for item in required_raw)
        )
        if any(not item for item in required):
            raise ValueError(f"第 {line_number} 行 required 无效")
        seen.add(case_id)
        cases.append(EvalCase(case_id, message, required))
    if not cases:
        raise ValueError("评测语料为空")
    return cases


def evaluate(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    catalog = catalog_from_tool_specs(build_tool_specs())
    retriever = CapabilityRetriever()
    outcomes: list[EvalOutcome] = []
    for case in load_cases(path):
        selection = retriever.retrieve(
            case.message,
            catalog,
            context={
                "owner": "offline-eval",
                "session_id": "offline-eval-session",
                "channel": "eval",
                "reference_kinds": (),
            },
        )
        selected = selection.names
        missing = tuple(name for name in case.required if name not in selected)
        outcomes.append(EvalOutcome(case.case_id, not missing, selected, missing))

    tools = catalog.visible({})
    invalid_effect_tools: list[str] = []
    for tool in tools:
        if tool.effect is ToolEffect.READ:
            valid = (
                tool.read is not None
                and tool.prepare is None
                and tool.execute_confirmed is None
            )
        else:
            valid = (
                tool.read is None
                and tool.prepare is not None
                and tool.execute_confirmed is not None
            )
        if not valid:
            invalid_effect_tools.append(tool.name)

    candidate_counts = [len(item.selected) for item in outcomes]
    passed = sum(item.passed for item in outcomes)
    safety_ok = not invalid_effect_tools
    return {
        "ok": passed == len(outcomes) and safety_ok,
        "summary": {
            "cases": len(outcomes),
            "passed": passed,
            "failed": len(outcomes) - passed,
            "retrieval_recall": round(passed / len(outcomes), 4),
            "candidate_count_min": min(candidate_counts),
            "candidate_count_max": max(candidate_counts),
            "candidate_count_mean": round(statistics.fmean(candidate_counts), 2),
            "catalog_tools": len(tools),
            "read_tools": sum(tool.effect is ToolEffect.READ for tool in tools),
            "effect_tools": sum(tool.effect is not ToolEffect.READ for tool in tools),
            "effect_gate_valid": safety_ok,
        },
        "invalid_effect_tools": invalid_effect_tools,
        "outcomes": [item.to_dict() for item in outcomes],
    }


def _text_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "MediaFlux Agent Kernel 离线验收",
        f"语料：{summary['passed']}/{summary['cases']} 通过，召回率 {summary['retrieval_recall']:.2%}",
        (
            "候选工具："
            f"{summary['candidate_count_min']}-{summary['candidate_count_max']} 项，"
            f"平均 {summary['candidate_count_mean']} 项"
        ),
        (
            "能力目录："
            f"{summary['catalog_tools']} 项（READ {summary['read_tools']} / "
            f"Effect {summary['effect_tools']}）"
        ),
        "Effect Gate：通过" if summary["effect_gate_valid"] else "Effect Gate：失败",
    ]
    for outcome in result["outcomes"]:
        if not outcome["passed"]:
            lines.append(
                f"[FAIL] {outcome['case_id']} 缺少：{', '.join(outcome['missing'])}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.fixture)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(
            _text_report(result)
            if "summary" in result
            else f"Agent Kernel 验收失败：{result['error']}"
        )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
