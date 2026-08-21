#!/usr/bin/env python3
"""MediaFlux Agent 自然语言契约的离线黄金集评测器。

只调用确定性解析/分类函数，不创建 Agent 服务、不访问网络，也不调用 Provider。
默认评测 ``tests/fixtures/agent_eval_cases.jsonl``，失败时以非零状态退出。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.intents import match_read_intent
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.llm_router import (
    is_agent_action_request,
    is_confirmation_planning_request,
)
from app.agent.orchestrator import (
    AgentOrchestrator,
    _DIAGNOSTIC_READ_INTENTS,
    contextual_media_rating_request,
    recent_discovery_candidate_request,
    recent_resource_submit_request,
)
from app.agent.registry import ToolRegistry
from app.sensitive_data import contains_sensitive_credential

DEFAULT_FIXTURE = Path("tests/fixtures/agent_eval_cases.jsonl")
EVALUATORS = frozenset({
    "route_tool",
    "diagnostic_tool",
    "action_intent",
    "confirmation_planning",
    "discovery_followup",
    "resource_followup",
    "media_rating",
    "sensitive_input",
})
CATEGORIES = frozenset({
    "read",
    "write",
    "clarification",
    "multi_turn",
    "argument_validation",
    "safety_adversarial",
})
_ALLOWED_KEYS = frozenset({
    "case_id",
    "category",
    "domain",
    "evaluator",
    "message",
    "expected",
    "allow_implicit",
    "conversation_context",
    "tags",
    "notes",
})
_CASE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CONTEXT_KEYS = frozenset({"role", "text", "tool_name", "status", "media_context"})
_MEDIA_CONTEXT_KEYS = frozenset({"title", "original_title", "year", "media_type"})
_DIAGNOSTIC_TOOLS = frozenset(spec.tool_name for spec in _DIAGNOSTIC_READ_INTENTS)
_OFFLINE_ROUTE_TOOLS = frozenset({
    "media.subscription_updates",
    "media.subscription_summaries",
    "media.get_subscription_summary",
    "downloads.diagnose_queue",
    "rss.diagnose",
    "local_media.diagnose",
    "automation.diagnose_pipeline",
    "workspace.health",
    "indexer.search_resources",
    "discovery.recommend",
    "discovery.search",
    "library.check_updates",
    "library.count_series_episodes",
})


@dataclass(frozen=True, slots=True)
class AgentEvalCase:
    case_id: str
    category: str
    domain: str
    evaluator: str
    message: str
    expected: Any
    allow_implicit: bool = False
    conversation_context: tuple[dict[str, Any], ...] = ()
    tags: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AgentEvalOutcome:
    case_id: str
    category: str
    domain: str
    evaluator: str
    expected: Any
    actual: Any
    matched: bool


def _schema_error(label: str, message: str) -> ValueError:
    return ValueError(f"{label}: {message}")


def _validate_expected(case_id: str, evaluator: str, expected: Any) -> None:
    if evaluator == "route_tool":
        if expected is None:
            return
        if not isinstance(expected, Mapping) or set(expected) != {"tool_name", "arguments"}:
            raise _schema_error(case_id, "路由 expected 必须包含 tool_name 和 arguments")
        if expected.get("tool_name") not in _OFFLINE_ROUTE_TOOLS:
            raise _schema_error(case_id, "路由 tool_name 不在离线白名单")
        if not isinstance(expected.get("arguments"), Mapping):
            raise _schema_error(case_id, "路由 arguments 必须是 object")
        return
    if evaluator in {"action_intent", "confirmation_planning", "sensitive_input"}:
        if not isinstance(expected, bool):
            raise _schema_error(case_id, "expected 必须是 boolean")
        return
    if evaluator == "diagnostic_tool":
        if expected is not None and expected not in _DIAGNOSTIC_TOOLS:
            raise _schema_error(case_id, "expected 必须是已声明诊断工具名或 null")
        return
    if evaluator == "discovery_followup":
        if expected is None:
            return
        if not isinstance(expected, Mapping) or set(expected) != {
            "action", "position", "explicit"
        }:
            raise _schema_error(case_id, "探索续句 expected 结构无效")
        if expected.get("action") not in {"watchlist_add", "resource_search", "inspect"}:
            raise _schema_error(case_id, "探索续句 action 无效")
        if not isinstance(expected.get("position"), int) or isinstance(expected.get("position"), bool):
            raise _schema_error(case_id, "探索续句 position 必须是整数")
        if not isinstance(expected.get("explicit"), bool):
            raise _schema_error(case_id, "探索续句 explicit 必须是 boolean")
        return
    if evaluator == "resource_followup":
        if expected is None:
            return
        if not isinstance(expected, Mapping) or not set(expected).issubset({
            "position", "episode", "target"
        }):
            raise _schema_error(case_id, "资源续句 expected 结构无效")
        if set(expected) not in ({"position", "target"}, {"position", "target", "episode"}):
            raise _schema_error(case_id, "资源续句 expected 字段不完整")
        position = expected.get("position")
        if position is not None and (
            not isinstance(position, int) or isinstance(position, bool) or position < 1
        ):
            raise _schema_error(case_id, "资源续句 position 无效")
        episode = expected.get("episode")
        if episode is not None and (
            not isinstance(episode, int) or isinstance(episode, bool) or episode < 1
        ):
            raise _schema_error(case_id, "资源续句 episode 无效")
        if expected.get("target") not in {None, "qb", "guangya", "both"}:
            raise _schema_error(case_id, "资源续句 target 无效")
        return
    if evaluator == "media_rating":
        if expected is None:
            return
        if not isinstance(expected, Mapping) or not set(expected).issubset({
            "query", "media_type", "year", "allow_web_fallback"
        }):
            raise _schema_error(case_id, "评分续句 expected 结构无效")
        if not isinstance(expected.get("query"), str) or not expected.get("query"):
            raise _schema_error(case_id, "评分续句 query 必须是非空字符串")
        if expected.get("allow_web_fallback") is not True:
            raise _schema_error(case_id, "评分续句必须声明 allow_web_fallback=true")
        if "media_type" in expected and expected.get("media_type") not in {"movie", "tv"}:
            raise _schema_error(case_id, "评分续句 media_type 无效")
        if "year" in expected and not re.fullmatch(r"(?:19|20)\d{2}", str(expected.get("year"))):
            raise _schema_error(case_id, "评分续句 year 无效")
        return
    raise _schema_error(case_id, f"未知 evaluator: {evaluator}")


def _validate_context(case_id: str, raw: Any) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > 16:
        raise _schema_error(case_id, "conversation_context 必须是至多 16 项的数组")
    context: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping) or set(item) - _CONTEXT_KEYS:
            raise _schema_error(case_id, f"conversation_context[{index}] 结构无效")
        projected = dict(item)
        role = projected.get("role")
        if role not in {"user", "assistant", "summary"}:
            raise _schema_error(case_id, f"conversation_context[{index}].role 无效")
        for key in ("text", "tool_name", "status"):
            if key in projected and not isinstance(projected[key], str):
                raise _schema_error(case_id, f"conversation_context[{index}].{key} 必须是字符串")
        media = projected.get("media_context")
        if media is not None:
            if not isinstance(media, Mapping) or set(media) - _MEDIA_CONTEXT_KEYS:
                raise _schema_error(case_id, f"conversation_context[{index}].media_context 无效")
            title = media.get("title")
            if not isinstance(title, str) or not title.strip():
                raise _schema_error(case_id, f"conversation_context[{index}] 缺少媒体标题")
            projected["media_context"] = dict(media)
        context.append(projected)
    return tuple(context)


def validate_agent_eval_rows(rows: Iterable[Mapping[str, Any]]) -> list[AgentEvalCase]:
    cases: list[AgentEvalCase] = []
    seen_ids: set[str] = set()
    seen_inputs: set[tuple[str, str, bool, str]] = set()
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping):
            raise _schema_error(f"第 {index} 行", "样本必须是 JSON object")
        row = dict(raw)
        label = str(row.get("case_id") or f"第 {index} 行")
        unknown = sorted(set(row) - _ALLOWED_KEYS)
        if unknown:
            raise _schema_error(label, f"未知字段: {', '.join(unknown)}")
        missing = sorted({
            "case_id", "category", "domain", "evaluator", "message", "expected"
        } - set(row))
        if missing:
            raise _schema_error(label, f"缺少字段: {', '.join(missing)}")

        case_id = row["case_id"]
        if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
            raise _schema_error(label, "case_id 必须是小写字母/数字/连字符 slug")
        if case_id in seen_ids:
            raise _schema_error(case_id, "case_id 重复")
        seen_ids.add(case_id)

        category = row["category"]
        if category not in CATEGORIES:
            raise _schema_error(case_id, "category 无效")
        domain = row["domain"]
        if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain):
            raise _schema_error(case_id, "domain 必须是小写 snake_case")
        evaluator = row["evaluator"]
        if evaluator not in EVALUATORS:
            raise _schema_error(case_id, "evaluator 无效")
        message = row["message"]
        if (
            not isinstance(message, str)
            or not message.strip()
            or message != message.strip()
            or len(message) > 1000
        ):
            raise _schema_error(case_id, "message 必须是首尾无空白的非空字符串")
        allow_implicit = row.get("allow_implicit", False)
        if not isinstance(allow_implicit, bool):
            raise _schema_error(case_id, "allow_implicit 必须是 boolean")
        if allow_implicit and evaluator not in {"discovery_followup", "resource_followup"}:
            raise _schema_error(case_id, "只有候选续句可启用 allow_implicit")
        context = _validate_context(case_id, row.get("conversation_context"))
        if context and evaluator != "media_rating":
            raise _schema_error(case_id, "conversation_context 仅供 media_rating 使用")
        expected = row["expected"]
        _validate_expected(case_id, evaluator, expected)

        tags_raw = row.get("tags", [])
        if not isinstance(tags_raw, list) or any(
            not isinstance(tag, str) or not _TAG_RE.fullmatch(tag) for tag in tags_raw
        ):
            raise _schema_error(case_id, "tags 必须是 slug 字符串数组")
        tags = tuple(tags_raw)
        if len(set(tags)) != len(tags):
            raise _schema_error(case_id, "tags 不得重复")
        notes = row.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 300:
            raise _schema_error(case_id, "notes 必须是不超过 300 字的字符串")

        input_key = (evaluator, message, allow_implicit, json.dumps(context, ensure_ascii=False, sort_keys=True))
        if input_key in seen_inputs:
            raise _schema_error(case_id, "evaluator/message/context 组合重复")
        seen_inputs.add(input_key)
        cases.append(AgentEvalCase(
            case_id=case_id,
            category=category,
            domain=domain,
            evaluator=evaluator,
            message=message,
            expected=expected,
            allow_implicit=allow_implicit,
            conversation_context=context,
            tags=tags,
            notes=notes,
        ))
    return cases


def load_agent_eval_cases(path: Path | str = DEFAULT_FIXTURE) -> list[AgentEvalCase]:
    fixture = Path(path)
    rows: list[Mapping[str, Any]] = []
    with fixture.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_number} 行: JSON 无效") from exc
            rows.append(row)
    if not rows:
        raise ValueError("Agent 评测集不能为空")
    return validate_agent_eval_rows(rows)


def _offline_route_projection(message: str) -> dict[str, Any] | None:
    registry = ToolRegistry()

    def _identity(arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    for tool_name in sorted(_OFFLINE_ROUTE_TOOLS):
        registry.register(ToolSpec(
            name=tool_name,
            description="offline agent route evaluator",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda _arguments, name=tool_name: ToolResult(
                True, "success", name, data={}
            ),
        ))
    response = AgentOrchestrator(registry)._query_raw(
        message, owner="offline-agent-eval", allow_model_routing=False
    )
    tool_call = response.get("tool_call") if isinstance(response, dict) else None
    if not isinstance(tool_call, dict) or not str(tool_call.get("name") or "").strip():
        return None
    arguments = tool_call.get("arguments")
    return {
        "tool_name": str(tool_call["name"]),
        "arguments": dict(arguments) if isinstance(arguments, dict) else {},
    }


def evaluate_agent_case(case: AgentEvalCase) -> AgentEvalOutcome:
    if case.evaluator == "route_tool":
        actual = _offline_route_projection(case.message)
    elif case.evaluator == "diagnostic_tool":
        actual = match_read_intent(case.message.casefold(), _DIAGNOSTIC_READ_INTENTS)
    elif case.evaluator == "action_intent":
        actual = is_agent_action_request(case.message)
    elif case.evaluator == "confirmation_planning":
        actual = is_confirmation_planning_request(case.message)
    elif case.evaluator == "discovery_followup":
        actual = recent_discovery_candidate_request(
            case.message, allow_implicit=case.allow_implicit
        )
    elif case.evaluator == "resource_followup":
        actual = recent_resource_submit_request(
            case.message, allow_implicit=case.allow_implicit
        )
    elif case.evaluator == "media_rating":
        actual = contextual_media_rating_request(
            case.message, [dict(item) for item in case.conversation_context]
        )
    elif case.evaluator == "sensitive_input":
        actual = contains_sensitive_credential(case.message)
    else:  # pragma: no cover - schema validation owns this branch
        raise ValueError(f"unsupported evaluator: {case.evaluator}")
    return AgentEvalOutcome(
        case_id=case.case_id,
        category=case.category,
        domain=case.domain,
        evaluator=case.evaluator,
        expected=case.expected,
        actual=actual,
        matched=actual == case.expected,
    )


def evaluate_agent_cases(cases: Iterable[AgentEvalCase]) -> list[AgentEvalOutcome]:
    return [evaluate_agent_case(case) for case in cases]


def _bucket_metrics(outcomes: Iterable[AgentEvalOutcome], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[AgentEvalOutcome]] = defaultdict(list)
    for outcome in outcomes:
        groups[str(getattr(outcome, key))].append(outcome)
    return {
        name: {
            "total": len(rows),
            "passed": sum(row.matched for row in rows),
            "failed": sum(not row.matched for row in rows),
            "pass_rate": round(sum(row.matched for row in rows) / len(rows), 4),
        }
        for name, rows in sorted(groups.items())
    }


def agent_eval_metrics(outcomes: Sequence[AgentEvalOutcome]) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(outcome.matched for outcome in outcomes)
    return {
        "overall": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
        },
        "by_category": _bucket_metrics(outcomes, "category"),
        "by_domain": _bucket_metrics(outcomes, "domain"),
        "by_evaluator": _bucket_metrics(outcomes, "evaluator"),
        "failed_case_ids": [outcome.case_id for outcome in outcomes if not outcome.matched],
    }


def format_agent_eval_report(outcomes: Sequence[AgentEvalOutcome]) -> str:
    metrics = agent_eval_metrics(outcomes)
    overall = metrics["overall"]
    lines = [
        "MediaFlux Agent 离线评测",
        f"总体: {overall['passed']}/{overall['total']} "
        f"({overall['pass_rate'] * 100:.1f}%)，失败 {overall['failed']}",
    ]
    for label, key in (("分类", "by_category"), ("领域", "by_domain"), ("评估器", "by_evaluator")):
        lines.append(f"{label}:")
        for name, row in metrics[key].items():
            lines.append(
                f"  - {name}: {row['passed']}/{row['total']} "
                f"({row['pass_rate'] * 100:.1f}%)"
            )
    failures = [outcome for outcome in outcomes if not outcome.matched]
    if failures:
        lines.append("失败样本:")
        for outcome in failures:
            lines.append(
                f"  - {outcome.case_id} [{outcome.evaluator}] "
                f"expected={outcome.expected!r} actual={outcome.actual!r}"
            )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--category", choices=sorted(CATEGORIES))
    parser.add_argument("--domain")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cases = load_agent_eval_cases(args.fixture)
    if args.category:
        cases = [case for case in cases if case.category == args.category]
    if args.domain:
        cases = [case for case in cases if case.domain == args.domain]
    if not cases:
        print("筛选后没有 Agent 评测样本", file=sys.stderr)
        return 2
    outcomes = evaluate_agent_cases(cases)
    if args.format == "json":
        rendered = json.dumps(agent_eval_metrics(outcomes), ensure_ascii=False, indent=2)
    else:
        rendered = format_agent_eval_report(outcomes)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if all(outcome.matched for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
