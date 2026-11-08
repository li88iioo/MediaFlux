#!/usr/bin/env python3
"""MediaFlux Agent 验收：默认离线回归，可评判隔离真实运行的事件记录。

provenance 字段可选，提供即校验；未标记的旧语料归入 legacy_unverified。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelToolCall,
)
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_result_state

DEFAULT_FIXTURE = Path("tests/fixtures/agent_kernel_capability_cases.jsonl")
DEFAULT_LIFECYCLE_FIXTURE = Path("tests/fixtures/agent_kernel_lifecycle_cases.jsonl")
DEFAULT_LIVE_FIXTURE = Path("tests/fixtures/agent_live_task_cases.jsonl")
DEFAULT_NATURAL_FIXTURE = Path("tests/fixtures/agent_natural_task_cases.jsonl")
DEFAULT_MATRIX_FIXTURE = Path("tests/fixtures/agent_live_task_matrix.jsonl")
DEFAULT_MATRIX_CASES = Path("tests/fixtures/agent_live_matrix_cases.jsonl")
DEFAULT_SDK_FIXTURE = Path("tests/fixtures/agent_sdk_task_cases.jsonl")
DEFAULT_CAPABILITY_FIXTURE = Path("tests/fixtures/agent_capability_task_cases_v2.jsonl")
SDK_TASK_KINDS = frozenset({"year_identity", "read_recovery", "scope_aba", "preview_cancel"})
PROVENANCE_KINDS = ("legacy_unverified", "synthetic", "simulated_user_real_model")
EIGHT_TURN_CONSTRAINTS = (
    (1, "1080p", False, False), (2, "1080p", False, False),
    (2, "2160p", False, False), (2, "2160p", False, True),
    (3, "2160p", False, True), (3, "2160p", True, True),
    (3, "1080p", True, True), (3, "1080p", True, True),
)


def load_live_cases(path: Path = DEFAULT_LIVE_FIXTURE) -> list[dict[str, Any]]:
    """版本化业务判据独立于旧首窗召回语料；holdout 不参与召回调参。"""
    parameters = {
        "actor_count": {"actor"},
        "actor_over_fifty": {"actor"},
        "year_identity": {"title", "year"},
        "weekly_calendar": {"source"},
        "recommendation": {"count", "min_year"},
        "favorites_search": set(),
        "scope_aba": {"scopes"},
        "preview_cancel": {"count"},
        "read_recovery": {"actor", "fault"},
        "constraint_memory": {"title", "season", "episode"},
    }
    natural_parameters = {"undo_inspection": set(), "favorite_search": set(), "constraint_memory": {"identity"},
                          **{kind: parameters[kind] for kind in SDK_TASK_KINDS}}
    cases, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "version", "split", "messages", "rubric"}
            or not isinstance(case["id"], str)
            or not case["id"]
            or type(case["version"]) is not int
            or case["version"] not in {1, 2}
            or (case["id"], case["version"]) in seen
            or not isinstance(case["split"], str)
            or case["split"] not in {"regression", "holdout"}
            or not isinstance(case["messages"], list)
            or not case["messages"]
            or any(
                not isinstance(message, str) or not message.strip()
                for message in case["messages"]
            )
            or not isinstance(case["rubric"], dict)
            or not isinstance(case["rubric"].get("kind"), str)
            or case["rubric"]["kind"] not in (
                parameters if case["version"] == 1 else natural_parameters
            )
        ):
            raise ValueError("真实任务语料字段、版本或判据无效")
        rubric = case["rubric"]
        required = (parameters if case["version"] == 1 else natural_parameters)[rubric["kind"]]
        if (
            set(rubric) != {"kind", *required}
            or (case["version"] == 2 and len(case["messages"]) != {"constraint_memory": 8, "favorite_search": 2, "scope_aba": 3}.get(rubric["kind"], 1))
            or any(
                type(rubric[key]) is not int or rubric[key] < 1
                for key in ("count", "min_year", "season", "episode")
                if key in rubric
            )
            or any(
                not isinstance(rubric[key], str) or not rubric[key].strip()
                for key in ("actor", "source", "title")
                if key in rubric
            )
            or (
                rubric["kind"] == "year_identity"
                and (not isinstance(rubric["year"], str) or not re.fullmatch(r"(?:19|20)\d{2}", rubric["year"]))
            )
            or (
                rubric["kind"] == "read_recovery"
                and (not isinstance(rubric["fault"], str) or rubric["fault"] not in {"missing_query", "invalid_enum"})
            )
            or (
                rubric["kind"] == "constraint_memory"
                and len(case["messages"]) != 8
            )
            or (
                rubric["kind"] == "scope_aba"
                and (
                    rubric.get("scopes") != ["电视剧", "动漫", "电视剧"]
                    or len(case["messages"]) != 3
                )
            )
        ):
            raise ValueError("真实任务判据参数无效")
        if case["version"] == 2 and rubric["kind"] == "constraint_memory":
            _natural_identity(rubric["identity"])
        seen.add((case["id"], case["version"]))
        cases.append(case)
    if not cases:
        raise ValueError("真实任务语料为空")
    return cases


def export_live_event(
    path: Path, kind: str, payload: dict[str, Any], *, redactions: Sequence[str] = ()
) -> None:
    """逐条落盘公开事件；不导出 Provider 私有块、headers 或凭据。"""
    private = {
        "raw",
        "provider_state",
        "headers",
        "authorization",
        "api_key",
        "password",
        "secret",
        "cookie",
        "signature",
    }

    def public(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: public(item)
                for key, item in value.items()
                if key.lower() not in private
            }
        if isinstance(value, (list, tuple)):
            return [public(item) for item in value]
        return value

    text = json.dumps({"kind": kind, "payload": public(payload)}, ensure_ascii=False)
    for secret in redactions:
        if secret:
            text = text.replace(
                json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]"
            )
    with path.open("a", encoding="utf-8") as target:
        target.write(text + "\n")


def _assess_eight_constraints(
    case: dict[str, Any], trace: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, Any]]:
    """T13 v1 的逐轮可观察证据；引用候选不等于推荐，文本语义留人工审查。"""
    from app.indexers.release import parse_indexer_release_position
    from app.modules.media_probe import infer_dynamic_ranges, infer_media_profile

    turns, rubric = trace.get("turns", []), case["rubric"]
    # 第 4/5 轮只排除 HDR；第 6 轮才新增必须明确 SDR。
    constraints = EIGHT_TURN_CONSTRAINTS
    checks = {"eight_actual_user_turns": len(turns) == 8 and all(
        turn.get("message") == message
        for turn, message in zip(turns, case["messages"])
    )}
    current_batch: dict[str, Any] | None = None
    candidates, support, reports = {}, {}, []

    def model_receipt(row):
        content = row.get("model_content")
        if content is None:
            return row.get("public", {})
        try:
            value = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    for number, (season, resolution, sdr, exclude_hdr) in enumerate(constraints, 1):
        key = (season, resolution, sdr, exclude_hdr)
        expected = {"title": rubric["title"], "season": season, "episode": 1,
                    "resolution": resolution, "dynamic_range": "SDR" if sdr else None,
                    "exclude_hdr": exclude_hdr, "read_only": True, "download": False, "save_preferences": False}

        def scope_matches(arguments):
            return (arguments.get("title") in {rubric["title"], "Black Mirror"}
                    and arguments.get("season") == season and arguments.get("episode") == 1)

        def quality_matches(arguments):
            return (arguments.get("resolution") == resolution
                    and (not sdr or arguments.get("dynamic_range") == "SDR")
                    and (not exclude_hdr or arguments.get("exclude_hdr") is True
                         or arguments.get("dynamic_range") == "SDR"))

        def matches(item):
            title, technical = item.get("title", ""), item.get("technical_evidence") or {}
            position = parse_indexer_release_position(title)
            ranges = set(technical.get("dynamic_ranges") or [])
            dynamic_range = technical.get("dynamic_range")
            hdr = bool({"HDR", "HDR10", "HDR10+", "DoVi", "HLG"} & (
                ranges | {dynamic_range} | set(infer_dynamic_ranges(title))))
            # 未标 HDR 不能证明 SDR；明确的混合 HDR/SDR 声明同样不能通过。
            return bool(item.get("result_id")) and (
                "black mirror" in title.casefold().replace(".", " ") or "黑镜" in title
            ) and position.get("season") == season and position.get("episode") == 1 and (
                technical.get("resolution") == resolution and infer_media_profile(title).resolution == resolution
            ) and (not exclude_hdr or not hdr) and (not sdr or (
                dynamic_range == "SDR" and ranges <= {"SDR"} and set(infer_dynamic_ranges(title)) == {"SDR"}
            ))

        searches, deliveries = [], []
        selection_valid = True
        for index, row in enumerate(trace.get("tools", [])):
            if row.get("turn") != number or row.get("public", {}).get("ok") is not True:
                continue
            document = model_receipt(row)
            payload = document.get("data") or {}
            arguments = row.get("arguments") or {}
            effective_query = payload.get("effective_query")
            # 判据看实际观察事实和真实ID，不要求模型调用某个窄工具或携带引用/位置。
            if isinstance(payload.get("items"), list):
                query = effective_query if isinstance(effective_query, dict) else payload.get("request") or arguments
                batch = []
                for item in payload["items"]:
                    if not isinstance(item, dict):
                        continue
                    observed = {**item, "source_turn": number, "tool_index": index,
                                "technical_evidence": item.get("technical_evidence") or {}}
                    batch.append(observed)
                    if item.get("result_id"):
                        candidates[item["result_id"]] = observed
                ids = [item.get("result_id") for item in batch]
                observed_receipt = document.get("ok") is True and all(isinstance(identity, str) and identity for identity in ids) and len(set(ids)) == len(ids)
                current_batch = {"arguments": query, "candidates": batch} if observed_receipt else None
                searches.append({"tool_index": index, "source_turn": number, "arguments": arguments,
                                 "effective_query": query, "receipt_observed": observed_receipt,
                                 "candidate_count": len(batch), "matching_candidate_count": sum(matches(item) for item in batch),
                                 "partial": payload.get("partial", False), "technical_filter": payload.get("technical_filter")})
                if observed_receipt and scope_matches(query) and quality_matches(query):
                    support.setdefault(key, []).append(index)
            elif "result_ids" in payload:
                identities = payload.get("result_ids")
                requested_ids = arguments.get("result_ids")
                source_query = (current_batch or {}).get("arguments", {})
                query = effective_query if isinstance(effective_query, dict) else source_query
                by_id = {item.get("result_id"): item for item in (current_batch or {}).get("candidates", [])}
                valid = (document.get("ok") is True and current_batch is not None
                         and isinstance(identities, list) and isinstance(requested_ids, list)
                         and all(isinstance(identity, str) for identity in identities + requested_ids)
                         and len(set(identities)) == len(identities) and len(set(requested_ids)) == len(requested_ids)
                         and set(identities).issubset(requested_ids) and set(requested_ids).issubset(by_id))
                requested_specs = {name: arguments[name] for name in ("resolution", "dynamic_range", "exclude_hdr") if arguments.get(name)}
                inherited = {name: source_query[name] for name in ("resolution", "dynamic_range", "exclude_hdr") if source_query.get(name) and not requested_specs.get(name)}
                expected_query = {**source_query, **requested_specs, **inherited}
                query_consistent = query == expected_query and not any(
                    source_query.get(name) and value and source_query[name] != value for name, value in requested_specs.items())
                selected = [by_id[identity] for identity in identities] if valid else []
                scoped = valid and scope_matches(source_query) and scope_matches(query)
                delivered_valid = (scoped and query_consistent and quality_matches(query)
                                   and all(matches(item) for item in selected))
                selection_valid = selection_valid and delivered_valid
                requested = (payload.get("technical_filter") or {}).get("requested", {})
                # 空展示必须由本轮的真实规格过滤回执证明，不能用空数组声称没有资源。
                if delivered_valid and (selected or quality_matches(requested)):
                    support.setdefault(key, []).append(index)
                deliveries.append({"tool_index": index, "requested_ids": requested_ids,
                                   "effective_query": query, "effective_query_consistent": query_consistent,
                                   "source_observed": valid, "scope_matches": scoped,
                                   "candidate_ids": [item["result_id"] for item in selected],
                                   "matches_constraints": delivered_valid, "technical_filter": payload.get("technical_filter"),
                                   "scope": payload.get("scope")})
        checks[f"turn_{number}_constraints_observed"] = bool(support.get(key))
        checks[f"turn_{number}_no_invalid_structured_delivery"] = selection_valid

        answer = turns[number - 1].get("answer", "") if number <= len(turns) else ""
        references = []
        # 仅识别完整 ID/发布标题；允许删除尾部站点标签，但缩写必须唯一。
        aliases: dict[str, set[str]] = {}
        for candidate_id, item in candidates.items():
            for alias in {candidate_id, item.get("title", ""),
                          re.sub(r"\[[^\]]*\]$", "", item.get("title", "")).strip()}:
                if alias:
                    aliases.setdefault(alias, set()).add(candidate_id)
        referenced_ids = {next(iter(ids)) for alias, ids in aliases.items() if len(ids) == 1 and alias in answer}
        for candidate_id, item in candidates.items():
            if candidate_id in referenced_ids:
                references.append({"result_id": candidate_id, "title": item.get("title"),
                                   "source_turn": item["source_turn"], "tool_index": item["tool_index"],
                                   "position": item.get("position"), "technical_evidence": item["technical_evidence"],
                                   "matches_constraints": matches(item)})
        reports.append({"turn": number, "constraints": expected, "searches": searches,
                        "supporting_tool_indexes": list(support.get(key, [])),
                        "structured_deliveries": deliveries, "referenced_candidates": references,
                        "language_review": "required: mentions/endorsement/empty-result scope cannot be inferred"})
    checks["no_preference_write_or_download"] = _read_only_execution(trace)
    # 不用标题提及、空列表或 success 状态替代八轮自由文本语义签核。
    checks["answer_semantics_verified"] = False
    return checks, {"turns": reports, "language_review_required": True,
                    "final_referenced_candidate_ids": [item["result_id"] for item in reports[-1]["referenced_candidates"]]}


def _read_only_execution(trace: dict[str, Any], sdk_reads=()) -> bool:
    """核对真实工具/SDK 调用与 effect 记录，不把脚本字符串或展示摘要当执行证据。"""
    from app.sdk import WRITE_METHODS

    writes = set(WRITE_METHODS) | {
        "media.set_preferences", "media.clear_preferences", "ingest.submit",
        "preferences.update", "preferences.clear",
    }
    writes.update(tool.name for tool in build_tool_catalog().visible() if tool.effect is not ToolEffect.READ)

    def is_write(name):
        return isinstance(name, str) and name.replace("__", ".") in writes

    if any(trace.get(key) for key in ("confirmed_executions", "confirmation_attempts", "effects", "blocked_writes")):
        return False
    if any(is_write(row.get("method")) for row in sdk_reads):
        return False
    for row in trace.get("tools", []):
        if is_write(row.get("tool")):
            return False
        data = (row.get("public") or {}).get("data")
        if (row.get("tool") in {"agent.run_code", "agent__run_code"} and isinstance(data, dict)
                and any(isinstance(call, dict) and is_write(call.get("method")) for call in data.get("sdk_calls", []))):
            return False
    for turn in trace.get("turns", []):
        for event in turn.get("events", []):
            if event.get("type") == "effect.approval_required":
                return False
            if event.get("type") in {"tool.started", "tool.completed", "tool.failed", "model.tool_call"}:
                payload = event.get("payload") or {}
                if is_write(payload.get("tool") or payload.get("tool_name")):
                    return False
    return True


def _host_sdk_reads(trace, host_evidence):
    """只读显式宿主输入；原始 SDK 记录不改名为旧工具，也不读取模型提供的路径。"""
    if host_evidence is not None and not isinstance(host_evidence, dict):
        raise ValueError("宿主证据必须是对象")
    if host_evidence is None or "sdk_reads" not in host_evidence:
        return None
    if (not isinstance(trace.get("run_id"), str) or not trace["run_id"].strip()
            or type(host_evidence.get("case_version")) is not int
            or tuple(host_evidence.get(key) for key in ("run_id", "case_id", "case_version"))
            != tuple(trace.get(key) for key in ("run_id", "case_id", "case_version"))):
        raise ValueError("SDK 宿主观察与运行、任务或版本不匹配")
    rows, seen = host_evidence["sdk_reads"], set()
    if not isinstance(rows, list):
        raise ValueError("SDK 宿主观察必须为列表")
    for row in rows:
        if (not isinstance(row, dict) or row.get("kind") != "sdk_read"
                or any(not isinstance(row.get(key), str) or not row[key].strip()
                       for key in ("evidence_id", "source", "method"))
                or row["evidence_id"] in seen
                or type(row.get("turn")) is not int or not 1 <= row["turn"] <= len(trace.get("turns", []))
                or not isinstance(row.get("arguments"), dict)
                or not (isinstance(row.get("result"), dict) or row.get("error_type") or row.get("error"))):
            raise ValueError("SDK 宿主观察缺少唯一来源、实际参数、响应或有效回合")
        seen.add(row["evidence_id"])
    return rows


def _sdk_results(rows, method):
    return [row for row in rows or () if row["method"] == method
            and isinstance(row.get("result"), dict) and row["result"].get("ok") is not False
            and not row.get("error_type") and not row.get("error")]


def _recommendation_reads(trace, sdk_reads):
    if sdk_reads is not None:
        return [(row["arguments"], row["result"]) for row in _sdk_results(sdk_reads, "media.catalog")]
    return [(row["arguments"], row["public"].get("data") or {}) for row in trace.get("tools", [])
            if row.get("tool") == "discovery.recommend" and row.get("public", {}).get("ok") is True]


def _native_calendar_evidence(sdk_reads, source, answers):
    """完整周快照直接核验七日/来源/事件；不伪造旧分页的 total 或 has_more。"""
    checks = dict.fromkeys(("complete_week_evidence", "correct_week_dates", "all_titles_delivered"), False)
    rows = _sdk_results(sdk_reads, "media.calendar")
    evidence: dict[str, Any] = {"observation_ids": [row["evidence_id"] for row in rows], "coverage": "whole_week_snapshot"}
    if not rows:
        return checks, evidence
    snapshot = rows[-1]["result"]
    try:
        start = date.fromisoformat(snapshot["week_start"])
        dates = [(start + timedelta(days=index)).isoformat() for index in range(7)]
        days = snapshot["days"]
        checks["correct_week_dates"] = (start.weekday() == 0 and snapshot.get("timezone") == "Asia/Shanghai"
                                        and [day["date"] for day in days] == dates)
        sources = [row for row in snapshot["sources"] if row.get("id") == source]
        complete = len(sources) == 1 and (
            sources[0].get("status") == "ok"
            or sources[0].get("status") == "partial" and sources[0].get("covered_dates") == dates
        )
        events = {}
        for day in days:
            for card in day["items"]:
                if card.get("source") != source:
                    continue
                complete &= card.get("stale") is not True
                slots = card.get("events") or [{"date": day["date"], "update_time": card.get("update_time", ""),
                                                "audience": card.get("schedule_audience"), "schedule": card.get("schedule")}]
                for event in slots:
                    valid = (event.get("date") == day["date"] and bool(card.get("stable_id"))
                             and bool(card.get("title")) and bool(event.get("schedule"))
                             and event.get("audience") in {"free", "member", "unknown"})
                    complete &= valid
                    if valid:
                        key = (card["stable_id"], event["date"], event.get("update_time", ""),
                               event["audience"], event["schedule"])
                        events[key] = card["title"]
        checks["complete_week_evidence"] = bool(complete and checks["correct_week_dates"])
        checks["all_titles_delivered"] = bool(events) and all(title in answers for title in events.values())
        evidence.update(returned_events=len(events), requested_dates=[day["date"] for day in days],
                        covered_dates=sources[0].get("covered_dates", []) if len(sources) == 1 else [],
                        provider_statuses=[row.get("status", "unknown") for row in sources],
                        distinct_title_date_time_slots=len({(title, key[1], key[2]) for key, title in events.items()}))
    except (KeyError, TypeError, ValueError, AttributeError):
        evidence["contract_error"] = "周历宿主响应缺少有效七日、来源或事件结构"
        checks["complete_week_evidence"] = False
    return checks, evidence


def _natural_identity(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str) or not value[key].strip()
        for key in ("provider", "external_id", "media_type")
    ) or value["media_type"] != "tv":
        raise ValueError("观察缺少可靠剧集身份")
    return value["provider"], value["external_id"], value["media_type"]


def _natural_target(value: Any) -> tuple[tuple[str, str, str], int, int]:
    if not isinstance(value, dict) or any(
        type(value.get(key)) is not int or value[key] < 1 for key in ("season", "episode")
    ):
        raise ValueError("观察缺少有效季集")
    return _natural_identity(value.get("identity")), value["season"], value["episode"]


def _assess_natural_constraints(case, trace, host_evidence, *, sdk_reads=None):
    """八轮 v2：collector 取证，服务事实核对交付；不要求工具名或模型 JSON 格式。

    observations 保存服务实际候选和覆盖范围；deliveries 是 collector 从交付产物
    提取的候选 ID 与来源关联，不是事实声明。两者均由模型不可写的宿主保存。
    允许工作区自行过滤、复用旧结果；未选择候选不自动证明没有资源。
    自然语言是否推荐/拒绝、是否多余追问，仍须逐轮人工复核。
    """
    from app.modules.media_probe import infer_dynamic_ranges

    turns = trace.get("turns", [])
    bound = isinstance(host_evidence, dict) and isinstance(trace.get("run_id"), str) and bool(trace["run_id"].strip()) and (
        host_evidence.get("run_id"), host_evidence.get("case_id"), host_evidence.get("case_version")
    ) == (trace["run_id"], case["id"], case["version"])
    checks = {"host_evidence_bound": bound, "host_observations_valid": False,
              "eight_actual_user_turns": len(turns) == 8 and all(
                  row.get("message") == message and row.get("status") == "success"
                  for row, message in zip(turns, case["messages"])),
              "all_deliveries_match_constraints": False, "read_only_observed": False,
              "business_scope_complete": False, "answer_semantics_verified": False}
    evidence: dict[str, Any] = {"turns": [], "unknown_ranges": [], "language_review_required": True}
    if not bound or host_evidence is None:
        return checks, evidence, "failed"
    if sdk_reads is not None and "observations" not in host_evidence and "deliveries" not in host_evidence:
        # SDK 原始响应能证明实际调用，但不能凭请求或答案补造作品身份和交付关联。
        checks["host_observations_valid"] = True
        checks["read_only_observed"] = isinstance(host_evidence.get("effects"), list) and not host_evidence["effects"]
        evidence["missing_evidence"] = ["verified_identity", "delivery_observation_links"]
        for number in range(1, len(turns) + 1):
            available = [row for row in sdk_reads if row["turn"] <= number
                         and row["method"] in {"resources.search", "resources.rank_episode"}]
            evidence["turns"].append({"turn": number, "available_observation_ids": [row["evidence_id"] for row in available],
                                      "new_observation_ids": [row["evidence_id"] for row in available if row["turn"] == number],
                                      "delivery_verified": False})
        evidence["observations"] = [{"evidence_id": row["evidence_id"], "source": row["source"], "turn": row["turn"],
                                    "method": row["method"], "arguments": row["arguments"],
                                    **{key: (row.get("result") or {})[key] for key in
                                       ("cached", "complete", "partial", "truncated", "has_more", "technical_filter")
                                       if key in (row.get("result") or {})},
                                    "returned_items": len((row.get("result") or {}).get("items") or [])}
                                   for row in sdk_reads]
        return checks, evidence, "inconclusive" if checks["eight_actual_user_turns"] and checks["read_only_observed"] else "failed"
    seen, observations, delivered = set(), {}, set()

    def record(row):
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key].strip() for key in ("evidence_id", "source")
        ) or row["evidence_id"] in seen or type(row.get("turn")) is not int or not 1 <= row["turn"] <= 8:
            raise ValueError("八轮宿主观察缺少唯一编号、来源或有效回合")
        seen.add(row["evidence_id"])

    try:
        for row in host_evidence["observations"]:
            record(row)
            if row.get("status") not in {"complete", "partial", "failed"} or not isinstance(row.get("items"), list):
                raise ValueError("候选观察缺少范围和列表")
            items = {}
            for item in row["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("result_id"), str) or not item["result_id"] or item["result_id"] in items:
                    raise ValueError("候选缺少唯一业务ID")
                items[item["result_id"]] = item
            observations[row["evidence_id"]] = {**row, "items": items}
        valid = True
        identity = _natural_identity(case["rubric"]["identity"])
        for row in host_evidence["deliveries"]:
            record(row)
            number = row["turn"]
            if number in delivered:
                raise ValueError("同一回合重复交付；collector 应保留该回合全部交付项")
            delivered.add(number)
            refs, selected = row["observation_ids"], row["selected_ids"]
            if not isinstance(refs, list) or not refs or not isinstance(selected, list) or any(not isinstance(value, str) for value in [*refs, *selected]):
                raise ValueError("交付缺少来源关联或候选ID列表")
            sources = [observations[ref] for ref in refs]
            if any(source["turn"] > number for source in sources):
                raise ValueError("交付引用了未来回合的观察")
            referenced_ids = {candidate_id for source in sources for candidate_id in source["items"]}
            candidates = {}
            for source in sorted(observations.values(), key=lambda source: source["turn"]):
                if source["turn"] <= number:
                    candidates.update({candidate_id: (item, source) for candidate_id, item in source["items"].items()})
            effective_sources = {source["evidence_id"]: source for source in sources}
            season, resolution, sdr, exclude_hdr = EIGHT_TURN_CONSTRAINTS[number - 1]
            matching = True
            for candidate_id in selected:
                if candidate_id not in referenced_ids:
                    raise ValueError("交付候选不在引用的服务观察中")
                item, source = candidates[candidate_id]
                effective_sources[source["evidence_id"]] = source
                technical = item.get("technical_evidence") or {}
                ranges = set(technical.get("dynamic_ranges") or []) | set(infer_dynamic_ranges(item.get("title", "")))
                ranges.add(technical.get("dynamic_range"))
                episodes = item.get("episodes")
                matching &= (_natural_identity(item.get("identity")) == identity
                             and type(item.get("season")) is int and item["season"] == season
                             and isinstance(episodes, list) and all(type(episode) is int and episode > 0 for episode in episodes) and 1 in episodes
                             and technical.get("resolution") == resolution
                             and (not exclude_hdr or not ranges.intersection({"HDR", "HDR10", "HDR10+", "DoVi", "HLG"}))
                             and (not sdr or technical.get("dynamic_range") == "SDR"))
            valid &= matching
            if not selected or any(source["status"] != "complete" for source in effective_sources.values()):
                evidence["unknown_ranges"].append({"turn": number, "empty_selection": not selected,
                                                   "sources": [{"id": source["evidence_id"], "status": source["status"]} for source in effective_sources.values()]})
            evidence["turns"].append({"turn": number, "selected_ids": selected, "matches_constraints": bool(matching)})
        checks["host_observations_valid"] = True
        checks["all_deliveries_match_constraints"] = delivered == set(range(1, 9)) and bool(valid)
        checks["read_only_observed"] = isinstance(host_evidence.get("effects"), list) and not host_evidence["effects"]
        checks["business_scope_complete"] = not evidence["unknown_ranges"]
    except (KeyError, TypeError, ValueError) as exc:
        evidence["contract_error"] = str(exc)
    structural = all(value for name, value in checks.items() if name not in {"answer_semantics_verified", "business_scope_complete"})
    return checks, evidence, "failed" if not structural else "inconclusive" if evidence["unknown_ranges"] else "manual_review"


def _assess_natural_favorites(
    case: dict[str, Any], trace: dict[str, Any], host_evidence: dict[str, Any] | None,
) -> tuple[dict[str, bool], dict[str, Any], str]:
    """v2只信任调用方独立传入的宿主观察，绝不从模型文本或tools推断事实。

    collector须在模型不可写的位置保存bundle。watchlist是本次完整来源快照；
    audits/target-resource观察来自实际服务调用，按发生顺序追加；审计必须覆盖第一轮，
    第二轮重读时使用最新审计。ready审计须带complete及missing季集；资源状态为
    complete/partial/failed，候选match_status为verified/unverified/rejected，
    verified候选须带identity/season/episodes。effects必须是完整的副作用观察列表。
    identity使用已核实的业务身份，
    无映射项保留unknown。source/evidence_id供追溯，不是可信性的自签声明。
    此函数只核验结构化事实；回答、未知范围的表述及多余询问仍需语义复核。
    """
    turns = trace.get("turns", [])
    bound = isinstance(host_evidence, dict) and isinstance(trace.get("run_id"), str) and bool(trace["run_id"].strip()) and (
        host_evidence.get("run_id"), host_evidence.get("case_id"), host_evidence.get("case_version")
    ) == (trace["run_id"], case["id"], case["version"]) and type(host_evidence.get("case_version")) is int
    checks = {
        "host_evidence_bound": bound,
        "host_observations_valid": False,
        "all_favorites_audited": False,
        "all_missing_targets_searched": False,
        "candidate_identity_and_coverage": False,
        "read_only_observed": False,
        "two_user_turns": len(turns) == 2 and all(
            row.get("message") == message and row.get("status") in {"success", "waiting"}
            for row, message in zip(turns, case["messages"])
        ),
        "business_scope_complete": False,
        "answer_semantics_verified": False,
    }
    evidence: dict[str, Any] = {"unknown_ranges": [], "not_exercised": [], "expected_targets": [],
                                "observed_targets": [], "audited_watchlist_ids": [], "language_review_required": True}
    if not bound or host_evidence is None:
        return checks, evidence, "failed"
    observed_ids = set()

    def observation(row, turn=None) -> dict[str, Any]:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key].strip() for key in ("evidence_id", "source")
        ) or row["evidence_id"] in observed_ids or (turn is not None and (type(row.get("turn")) is not int or row["turn"] != turn)):
            raise ValueError("宿主观察缺少来源、回合或唯一编号")
        observed_ids.add(row["evidence_id"])
        return row

    favorites: dict[str, tuple[str, str, str]] = {}
    expected: set[tuple[tuple[str, str, str], int, int]] = set()
    try:
        watchlist = observation(host_evidence.get("watchlist"))
        if type(watchlist.get("complete")) is not bool or not isinstance(watchlist.get("items"), list):
            raise ValueError("收藏观察缺少完整性")
        for item in watchlist["items"]:
            if not isinstance(item, dict):
                raise TypeError("收藏观察不是对象")
            number = item.get("watchlist_id")
            if not isinstance(number, str) or not number.strip() or number in favorites:
                raise ValueError("收藏编号缺失或重复")
            favorites[number] = _natural_identity(item.get("identity"))
        if not watchlist["complete"]:
            evidence["unknown_ranges"].append({"kind": "watchlist", "evidence_id": watchlist["evidence_id"]})
        audits, searches = host_evidence.get("audits"), host_evidence.get("resource_searches")
        if not isinstance(audits, list) or not isinstance(searches, list):
            raise TypeError("宿主审计或资源观察缺失")
        latest_audits, first_audited = {}, set()
        for row in audits:
            observation(row)
            if type(row.get("turn")) is not int or row["turn"] not in {1, 2}:
                raise ValueError("审计回合无效")
            number = row.get("watchlist_id")
            if number not in favorites or _natural_identity(row.get("identity")) != favorites[number]:
                raise ValueError("审计与收藏身份不一致")
            if row.get("status") not in {"ready", "unknown"} or type(row.get("complete")) is not bool or not isinstance(row.get("missing"), list):
                raise ValueError("审计状态或缺集完整性缺失")
            latest_audits[number] = row
            if row["turn"] == 1:
                first_audited.add(number)
        checks["all_favorites_audited"] = first_audited == favorites.keys()
        evidence["audited_watchlist_ids"] = sorted(latest_audits)
        for number, row in latest_audits.items():
            if row["status"] == "unknown" or not row["complete"]:
                evidence["unknown_ranges"].append({"kind": "audit", "watchlist_id": number, "evidence_id": row["evidence_id"]})
            if row["status"] == "unknown" and row["missing"]:
                raise ValueError("未知审计不能声称缺集")
            for position in row["missing"]:
                if not isinstance(position, dict):
                    raise TypeError("缺集位置不是对象")
                target = {"identity": row["identity"], "season": position.get("season"), "episode": position.get("episode")}
                expected.add(_natural_target(target))
                evidence["expected_targets"].append(target)
        latest_searches = {}
        for row in searches:
            observation(row, 2)
            target = _natural_target(row.get("target"))
            if row.get("status") not in {"complete", "partial", "failed"} or not isinstance(row.get("candidates"), list) or not isinstance(row.get("errors"), list):
                raise ValueError("资源观察缺少状态、候选或来源错误")
            latest_searches[target] = row
        checks["all_missing_targets_searched"] = latest_searches.keys() == expected
        valid_candidates = True
        for target, row in latest_searches.items():
            evidence["observed_targets"].append(row["target"])
            if row["status"] != "complete" or row["errors"]:
                evidence["unknown_ranges"].append({"kind": "resource", "target": row["target"], "status": row["status"], "errors": row["errors"]})
            for candidate in row["candidates"]:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("result_id"), str) or not candidate["result_id"] or candidate.get("match_status") not in {"verified", "unverified", "rejected"}:
                    raise ValueError("资源候选缺少身份或匹配状态")
                if candidate["match_status"] == "verified":
                    episodes = candidate.get("episodes")
                    valid_candidates &= (
                        _natural_identity(candidate.get("identity")) == target[0]
                        and type(candidate.get("season")) is int and candidate["season"] == target[1]
                        and isinstance(episodes, list) and bool(episodes)
                        and all(type(episode) is int and episode > 0 for episode in episodes)
                        and target[2] in episodes
                    )
                elif candidate["match_status"] == "unverified":
                    evidence["unknown_ranges"].append({"kind": "candidate", "target": row["target"], "result_id": candidate["result_id"]})
        checks["candidate_identity_and_coverage"] = valid_candidates
        checks["read_only_observed"] = isinstance(host_evidence.get("effects"), list) and not host_evidence["effects"]
        checks["host_observations_valid"] = True
        checks["business_scope_complete"] = not evidence["unknown_ranges"]
        if not favorites and watchlist["complete"]:
            evidence["not_exercised"].append("watchlist:empty")
        if not expected:
            evidence["not_exercised"].append("resource_search:no_missing_episodes")
    except (KeyError, TypeError, ValueError) as exc:
        evidence["contract_error"] = str(exc)
    structural = all(value for name, value in checks.items() if name not in {"answer_semantics_verified", "business_scope_complete"})
    if not structural:
        outcome = "failed"
    elif evidence["unknown_ranges"]:
        outcome = "inconclusive"
    elif not favorites:
        outcome = "not_configured"
    elif not expected:
        outcome = "not_exercised"
    else:
        outcome = "manual_review"
    return checks, evidence, outcome



def _assess_sdk_task(case, trace, host, rows):
    """显式v2真实SDK判据；v1工具事件与历史分数不改写。"""
    kind, rubric, turns = case["rubric"]["kind"], case["rubric"], trace.get("turns", [])
    checks = {"host_evidence_bound": rows is not None,
              "task_turns_finished": len(turns) == len(case["messages"]) and all(
                  turn.get("message") == message and turn.get("status") in (
                      {"success", "approval_required"} if kind == "preview_cancel" else {"success"})
                  for turn, message in zip(turns, case["messages"])),
              "answer_semantics_verified": False}
    evidence: dict[str, Any] = {"language_review_required": True}
    if rows is None or not isinstance(host, dict):
        evidence["missing_evidence"] = ["bound_sdk_host"]
        return checks, evidence, "inconclusive"
    answers = "\n".join(turn.get("answer", "") for turn in turns)
    outcome = "failed"
    if kind == "year_identity":
        reads = _sdk_results(rows, "library.search")
        items = [(source.get("server_type"), item) for row in reads
                 for source in (row["result"].get("data") or {}).get("sources", [])
                 if source.get("status") == "ready" for raw in source.get("items", [])
                 for item in [{**raw, "title": raw.get("title") or raw.get("name", ""),
                               "media_type": str(raw.get("media_type") or raw.get("type", "")).casefold()}]]
        items = list({(source, item.get("id")): (source, item) for source, item in items if item.get("id")}.values())
        related = [(source, item) for source, item in items if item.get("media_type") == "movie"
                   and (rubric["title"] in str(item.get("title", "")) or str(item.get("title", "")).casefold() == "dune")]
        targets = [(source, item) for source, item in related if str(item.get("year")) == rubric["year"] and item.get("id")]
        checks["receipt_entity_has_requested_year"] = bool(targets)
        checks["version_identity_delivered"] = any(
            re.search(re.escape(item["title"]) + r"[^\n]{0,48}" + rubric["year"], answers)
            for _, item in targets)
        evidence["matched_entities"] = [{"source": source, **{key: item.get(key) for key in ("id", "title", "year", "media_type")}}
                                        for source, item in targets]
        evidence["other_entity_years"] = sorted({str(item["year"]) for _, item in related if item.get("year") and str(item["year"]) != rubric["year"]})
        if not reads:
            outcome = "inconclusive" if rows else "not_exercised"
    elif kind == "read_recovery":
        attempts = [row for row in rows if row["method"] == "library.search"]
        injected = [row for row in host.get("injections", []) if row.get("method") == "library.search"]
        by_id = {row.get("attempt_id", row["evidence_id"]): row for row in attempts}
        fault = by_id.get(injected[0].get("attempt_id")) if len(injected) == 1 else None
        checks["injected_once_and_observed"] = bool(fault and injected[0].get("variant") == rubric["fault"]
            and fault.get("error_code") in {"invalid_arguments", "TypeError", "ValueError"}
            and ("query" not in fault["arguments"] if rubric["fault"] == "missing_query"
                 else fault["arguments"].get("media_type") == "matrix_invalid_enum"))
        counted = []
        for row in _sdk_results(attempts, "library.search"):
            args, data = row["arguments"], row["result"].get("data") or {}
            sources = data.get("sources") or []
            if (args.get("query") == rubric["actor"] and args.get("search_by") == "actor"
                    and args.get("media_type") == "movie" and args.get("count_only") is True
                    and data.get("match_basis") == "actor_metadata" and data.get("total_known") is True
                    and type(data.get("total")) is int and data["total"] >= 0 and sources
                    and all(source.get("status") == "ready" and type(source.get("total")) is int and source["total"] >= 0
                            and source.get("total_source") not in {None, "", "unknown", "returned_items"} for source in sources)
                    and sum(source["total"] for source in sources) == data["total"]):
                counted.append((row, data))
        checks["corrected_result_observed"] = bool(counted and attempts and counted[-1][0] is attempts[-1]
            and fault and attempts.index(counted[-1][0]) > attempts.index(fault))
        # 约束业务重试，不约束外层run_code/model回合或工具排列。
        checks["bounded_recovery"] = bool(fault and len(attempts[attempts.index(fault):]) <= 3)
        checks["actor_exact_total"] = any(re.search(rf"(?<!\d){data['total']}(?!\d)", answers) for _, data in counted)
        evidence["sdk_attempts"] = [{key: row.get(key) for key in ("evidence_id", "phase", "error_code", "arguments")} for row in attempts]
        if not injected:
            outcome = "not_exercised"
    elif kind == "scope_aba":
        reads = _sdk_results(rows, "library.recent")
        groups = [[row for row in reads if row["turn"] == number] for number in (1, 2)]
        titles, identities = [], []
        scope_ok = True
        for group, scope in zip(groups, rubric["scopes"]):
            selected = group[-1] if group else None
            payload = selected["result"] if selected else {}
            library = payload.get("scope", {}).get("library", {})
            args = selected["arguments"] if selected else {}
            scope_ok &= bool(payload.get("granularity") in {"work", "episode"} and library.get("id") and library.get("name") == scope and (
                args.get("library_id") == library["id"] or args.get("library_name") == scope))
            works = {}
            for item in payload.get("items", []):
                episode = payload.get("granularity") == "episode" or str(item.get("type", "")).casefold() == "episode"
                identifier = item.get("series_id") if episode else item.get("id")
                title = item.get("series_name") if episode else item.get("title") or item.get("name")
                if identifier and title:
                    works.setdefault(identifier, title)
            titles.append(list(works.values())[:5])
            identities.append(list(works)[:5])
        checks["real_a_b_library_scopes"] = scope_ok
        checks["five_work_identities_each"] = all(len(group) == 5 for group in identities)
        checks["returns_to_original_titles"] = (len(turns) == 3 and all(
            title in turns[index].get("answer", "") for index, names in enumerate((titles[0], titles[1], titles[0])) for title in names)
            and not any(title in turns[2].get("answer", "") for title in set(titles[1]) - set(titles[0])))
        # 本地文件/帮助/SDK元信息读取是合法复用；只禁止新的远端库存读取。
        checks["no_new_inventory_on_return"] = not any(row["turn"] == 3 and row["method"] in {"library.recent", "library.search", "library.episodes", "library.presence", "library.history", "library.libraries"} for row in rows)
        evidence["recent_work_ids"], evidence["recent_work_titles"] = identities, titles
        if not reads:
            outcome = "not_exercised"
    else:
        previews = host.get("previews", [])
        plans = [row for row in previews if row.get("tool") == "resources.download"]
        plan = plans[0] if len(plans) == 1 else {}
        ids = (plan.get("arguments") or {}).get("result_ids", [])
        resources = (plan.get("preview") or {}).get("data", {}).get("resources", [])
        observed = {item.get("result_id") for row in _sdk_results(rows, "resources.search") for item in row["result"].get("items", [])}
        checks["twenty_real_frozen_objects"] = bool(len(previews) == 1 and plan.get("plan_id") and isinstance(ids, list)
            and (plan.get("preview") or {}).get("status") == "confirmation_required"
            and (plan.get("preview") or {}).get("data", {}).get("count") == rubric["count"]
            and all(isinstance(identity, str) and identity for identity in ids)
            and len(ids) == len(set(ids)) == rubric["count"] and set(ids) <= observed
            and [item.get("result_id") for item in resources] == ids)
        approvals = [event for turn in turns for event in turn.get("events", []) if event.get("type") == "effect.approval_required"]
        checks["frozen_approval_plan"] = len(approvals) == 1 and approvals[0].get("payload", {}).get("plan", {}).get("plan_id") == plan.get("plan_id")
        checks["preview_cancelled"] = bool(plan.get("plan_id") and any(
            row.get("plan_id") == plan["plan_id"] and row.get("cancelled") is True for row in host.get("cancellations", [])))
        from app.sdk import WRITE_METHODS
        attempts = host.get("sdk_attempts", [])
        checks["no_confirmed_or_unrelated_write"] = (trace.get("confirmed_executions") == 0 and trace.get("confirmation_attempts", 0) == 0
            and isinstance(host.get("effects"), list) and not host["effects"] and not trace.get("effects")
            and not host.get("blocked_attempts") and all(row.get("method") not in WRITE_METHODS - {"resources.download"} for row in [*rows, *attempts]))
        evidence["plan_id"], evidence["frozen_ids"] = plan.get("plan_id"), ids
        evidence["ui_review_required"] = True
        if not plans:
            outcome = "not_exercised"
    if all(value for key, value in checks.items() if key != "answer_semantics_verified"):
        outcome = "manual_review"
    return checks, evidence, outcome


def _assess_undo_inspection(case, trace, host, reads):
    """T15 v2：真实日志→真实只读检查→owner凭据；不要求help仪式。"""
    turns = trace.get("turns", [])
    checks = {"host_evidence_bound": reads is not None,
              "task_turn_finished": len(turns) == 1 and turns[0].get("message") == case["messages"][0]
                  and turns[0].get("status") == "success",
              "latest_log_observed": False, "inspection_matches_log": False,
              "owner_receipt_persisted": False, "answer_semantics_verified": False}
    evidence: dict[str, Any] = {"language_review_required": True,
        "discovery_path": {"tool_calls": len(trace.get("tools", [])), "sdk_calls": len(reads or [])}}
    if reads is None or not isinstance(host, dict):
        return checks, evidence, "inconclusive"
    checks["no_write_or_preflight"] = (isinstance(host.get("effects"), list) and not host["effects"]
        and not host.get("previews") and not host.get("blocked_attempts") and not trace.get("blocked_writes")
        and trace.get("confirmation_attempts", 0) == 0)
    def unfiltered(row):
        args = row["arguments"]
        return (row["method"] == "activity.search" and args.get("query", "") == "" or
                row["method"] == "activity.organize_logs" and args.get("origin", "all") in {"all", "guangya"}
                and args.get("status", "all") == "all" and args.get("keyword", "") == "")
    pages = [(index, row) for index, row in enumerate(reads) if unfiltered(row) and row.get("result")
             and row["result"].get("ok") is not False]
    starts = [(index, row) for index, row in pages if row["arguments"].get("offset", 0) == 0]
    if not starts:
        return checks, evidence, "inconclusive"
    start, first = starts[-1]
    offset, logs, seen = 0, [], set()
    complete = False
    # 只使用最新的未筛选、从头连续的页面。找到目标前不能跳过任何更新记录。
    for index, row in pages:
        if index < start or row["method"] != first["method"] or row["arguments"].get("offset", 0) != offset:
            continue
        result = row["result"]
        items, total, more = result.get("items"), result.get("total"), result.get("has_more")
        if (not isinstance(items, list) or type(total) is not int or type(more) is not bool
                or result.get("returned", len(items)) != len(items) or total < offset + len(items)
                or more and len(items) != row["arguments"].get("limit", 50)):
            break
        for item in items:
            identity = (item.get("kind"), item.get("id"))
            if identity in seen or type(item.get("id")) is not int or item["id"] < 1:
                return checks, {**evidence, "contract_error": "日志身份重复或无效"}, "failed"
            seen.add(identity)
            if item.get("kind") == "organize":
                logs.append((index, item))
        offset += len(items)
        if not more:
            complete = offset == total
            break
    inspections = [(index, row) for index, row in enumerate(reads) if row["method"] == "activity.inspect_undo"]
    evidence["inspection_failures"] = [{"source": row["source"], "error_code": row.get("error_code"),
        "error_type": row.get("error_type")} for _, row in inspections if row.get("error") or row.get("error_type")]
    if not logs:
        return checks, evidence, "not_exercised" if complete else "inconclusive"
    # 已验证从头连续的页面即可证明其中最新目标，无需读取更旧的全部历史。
    for origin_index, log in logs:
        attempts = [(index, row) for index, row in inspections if row["arguments"].get("log_id") == log["id"] and index > origin_index]
        if not attempts:
            return checks, {**evidence, "latest_unchecked_log_id": log["id"]}, "inconclusive"
        _, checked = attempts[-1]
        if checked.get("error_code") == "undo_not_supported":
            continue
        result = checked.get("result") or {}
        data = result.get("data") or {}
        checks["latest_log_observed"] = True
        checks["inspection_matches_log"] = (not checked.get("error_type") and not checked.get("error") and result.get("ok") is True
            and data.get("log_id") == log["id"] and data.get("reversible") is True)
        receipt = data.get("receipt_id")
        ledgers = [row for row in host.get("undo_receipts", []) if row.get("receipt_id") == receipt
                   and row.get("inspection_id") == checked["evidence_id"]]
        checks["owner_receipt_persisted"] = bool(isinstance(receipt, str) and receipt and len(ledgers) == 1
            and ledgers[0].get("owner_bound") is True and ledgers[0].get("available") is True
            and ledgers[0].get("log_id") == log["id"] and ledgers[0].get("kind") == "organize"
            and ledgers[0].get("owner_digest") == host.get("owner_digest") and host.get("owner_digest"))
        evidence.update(log_id=log["id"], receipt_id=receipt, inspection_source=checked["source"], inspection_data=data)
        valid = all(value for key, value in checks.items() if key != "answer_semantics_verified")
        return checks, evidence, "manual_review" if valid else "inconclusive" if checks["inspection_matches_log"] and (not ledgers or any(row.get("error_type") for row in ledgers)) else "failed"
    if not complete:
        evidence["missing_evidence"] = ["remaining_unfiltered_log_pages"]
    return checks, evidence, "not_exercised" if complete else "inconclusive"


def assess_live_task(case: dict[str, Any], trace: dict[str, Any], *, host_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """用业务返回核验目标；turn.success 或模型自述不能单独证明通过。"""
    if (
        trace.get("case_id") != case["id"]
        or trace.get("case_version") != case["version"]
    ):
        raise ValueError("真实记录与任务版本不匹配")
    rubric, turns = case["rubric"], trace.get("turns", [])
    host_error = None
    try:
        sdk_reads = _host_sdk_reads(trace, host_evidence)
    except ValueError as exc:
        sdk_reads, host_error = [], str(exc)
    tools = [
        row for row in trace.get("tools", []) if row.get("public", {}).get("ok") is True
    ]
    kind = rubric["kind"]
    answers = "\n".join(row.get("answer", "") for row in turns)
    checks = {
        "no_confirmed_execution": trace.get("confirmed_executions") == 0,
        "all_turns_finished": len(turns) == len(case["messages"])
        and all(
            row.get("status")
            in (
                {"success", "approval_required"}
                if kind == "preview_cancel"
                else {"success"}
            )
            for row in turns
        ),
    }

    def selected(name):
        return [row for row in tools if row["tool"] == name] if sdk_reads is None else []

    def data(row):
        return row["public"].get("data") or {}

    outcome = "failed"
    evidence: dict[str, Any] = {}
    if case["version"] == 2:
        if kind == "undo_inspection":
            try:
                natural_checks, evidence, outcome = _assess_undo_inspection(case, trace, host_evidence, sdk_reads)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                natural_checks = {"host_observations_valid": False, "answer_semantics_verified": False}
                evidence.update(contract_error=str(exc), language_review_required=True)
                outcome = "failed"
        elif kind in SDK_TASK_KINDS:
            try:
                natural_checks, evidence, outcome = _assess_sdk_task(case, trace, host_evidence, sdk_reads)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                natural_checks = {"host_observations_valid": False, "answer_semantics_verified": False}
                evidence.update(contract_error=str(exc), language_review_required=True)
                outcome = "failed"
        elif kind == "constraint_memory":
            natural_checks, evidence, outcome = _assess_natural_constraints(case, trace, host_evidence, sdk_reads=sdk_reads)
        else:
            natural_checks, evidence, outcome = _assess_natural_favorites(case, trace, host_evidence)
        checks = {"no_confirmed_execution": checks["no_confirmed_execution"], **natural_checks}
        checks["read_only_execution"] = (checks.get("no_confirmed_or_unrelated_write", False) if kind == "preview_cancel"
                                         else _read_only_execution(trace, sdk_reads or ()))
        if not checks["no_confirmed_execution"] or not checks["read_only_execution"]:
            outcome = "failed"
    elif kind in {"actor_count", "actor_over_fifty", "read_recovery"}:
        reads = ([(row["arguments"], data(row)) for row in selected("library.search")] if sdk_reads is None else
                 [(row["arguments"], row["result"].get("data") or {}) for row in _sdk_results(sdk_reads, "library.search")])
        if sdk_reads is not None and not any(row["method"] == "library.search" for row in sdk_reads):
            outcome = "not_exercised"
        counted = [payload for arguments, payload in reads
                   if arguments.get("search_by") == "actor" and arguments.get("query") == rubric["actor"]
                   and arguments.get("count_only") is True and arguments.get("media_type") == "movie"
                   and payload.get("match_basis") == "actor_metadata"]
        if sdk_reads is not None:
            # 原生服务区分未知总数与明细数量，不能把返回条数或部分来源当精确总数。
            counted = [payload for payload in counted if payload.get("total_known") is True
                       and bool(payload.get("sources"))
                       and all(source.get("status") == "ready" and type(source.get("total")) is int
                               and source["total"] >= 0
                               and source.get("total_source") not in {None, "", "unknown", "returned_items"}
                               for source in payload["sources"])
                       and sum(source["total"] for source in payload["sources"]) == payload.get("total")]
        checks["actor_exact_total"] = any(
            bool(payload.get("sources")) and type(payload.get("total")) is int
            and re.search(rf"(?<!\d){payload['total']}(?!\d)", answers) is not None
            for payload in counted
        )
        if kind == "actor_over_fifty":
            totals = [payload.get("total") for payload in counted]
            checks["observed_total_over_fifty"] = any(
                type(total) is int and total > 50 for total in totals
            )
            checks["server_totals_verified"] = any(
                type(payload.get("total")) is int and payload["total"] > 50
                and re.search(rf"(?<!\d){payload['total']}(?!\d)", answers) is not None
                and all(source.get("status") == "ready" and type(source.get("total")) is int
                        and source.get("total_source") not in {None, "", "unknown", "returned_items"}
                        for source in payload.get("sources", []))
                and bool(payload.get("sources"))
                and sum(source["total"] for source in payload["sources"]) == payload["total"]
                for payload in counted
            )
            evidence["observed_actor_totals"] = totals
            if not checks["observed_total_over_fifty"]:
                outcome = "not_exercised"
        if kind == "read_recovery":
            applied = trace.get("matrix", {}).get("applied", [])
            checks["injected_once_and_validated"] = (
                len(applied) == 1
                and applied[0].get("tool") == "library.search"
                and applied[0].get("variant") == rubric["fault"]
                and applied[0].get("error_code") == "invalid_arguments"
                and (
                    "query" not in applied[0].get("after", {})
                    if rubric["fault"] == "missing_query"
                    else applied[0].get("after", {}).get("media_type")
                    == "matrix_invalid_enum"
                )
            )
            attempts = [row for row in trace.get("tools", []) if row["tool"] == "library.search"]
            checks["bounded_recovery"] = 2 <= len(attempts) <= 3
            checks["validation_feedback_recorded"] = any(
                event.get("type") == "tool.failed"
                and event.get("payload", {}).get("code") == "invalid_arguments"
                for turn in turns for event in turn.get("events", [])
            )
            evidence["fault_applied"] = len(applied)
            if not applied:
                outcome = "not_exercised"
    elif kind == "year_identity":
        items = [
            (source.get("server_type"), item)
            for row in selected("library.search")
            for source in data(row).get("sources", []) if source.get("status") == "ready"
            for item in source.get("items", [])
        ]
        related = [
            (source, item) for source, item in items
            if item.get("media_type") == "movie"
            and (rubric["title"] in str(item.get("title", "")) or str(item.get("title", "")).casefold() == "dune")
        ]
        target = [(source, item) for source, item in related if str(item.get("year")) == rubric["year"]]
        checks["receipt_entity_has_requested_year"] = bool(target)
        checks["version_identity_delivered"] = any(
            re.search(re.escape(item["title"]) + r"[^\n]{0,48}" + rubric["year"], answers)
            or re.search(rubric["year"] + r"[^\n]{0,48}" + re.escape(item["title"]), answers)
            for _, item in target
        )
        evidence["matched_entities"] = [
            {"source": source, "title": item["title"], "year": item["year"], "media_type": item["media_type"]}
            for source, item in target
        ]
        evidence["other_entity_years"] = sorted({str(item["year"]) for _, item in related if item.get("year") and str(item["year"]) != rubric["year"]})
        evidence["observed_entity_count"] = len(items)
        if not items:
            outcome = "not_exercised"
    elif kind == "weekly_calendar":
        rows = [
            row
            for row in trace.get("tools", [])
            if row["tool"] == "discovery.anime_calendar"
            if row["arguments"].get("day") == "week"
            and row["arguments"].get("source") == rubric["source"]
        ]
        pages = [data(row) for row in rows]
        items = {
            json.dumps(
                {
                    key: item.get(key)
                    for key in (
                        "stable_id",
                        "date",
                        "update_time",
                        "audience",
                        "schedule",
                    )
                },
                sort_keys=True,
            ): item
            for page in pages
            for item in page.get("items", [])
        }
        checks["complete_week_evidence"] = (
            bool(pages)
            and all(
                len(page.get("requested_dates", [])) == 7
                and bool(page.get("sources"))
                and all(
                    source.get("status") == "ok" for source in page.get("sources", [])
                )
                for page in pages
            )
            and not pages[-1].get("has_more", True)
            and len(items) == pages[-1].get("total")
        )
        try:
            start = date.fromisoformat(pages[0]["week_start"])
            dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
            checks["correct_week_dates"] = start.weekday() == 0 and all(
                page.get("requested_dates") == dates
                and page.get("week_end") == dates[-1]
                for page in pages
            )
        except (IndexError, KeyError, TypeError, ValueError):
            checks["correct_week_dates"] = False
        checks["all_titles_delivered"] = bool(items) and all(
            item.get("title") and item["title"] in answers for item in items.values()
        )
        if sdk_reads is not None:
            native_checks, evidence = _native_calendar_evidence(sdk_reads, rubric["source"], answers)
            checks.update(native_checks)
            if not any(row["method"] == "media.calendar" for row in sdk_reads):
                outcome = "not_exercised"
        if trace.get("safety_blocks"):
            outcome = "infrastructure_interference"
            checks["uninterfered_calendar_read"] = False
    elif kind == "recommendation":
        reads = _recommendation_reads(trace, sdk_reads)
        if sdk_reads is not None and not any(row["method"] == "media.catalog" for row in sdk_reads):
            outcome = "not_exercised"
        items = {
            item.get("tmdb_id"): item
            for arguments, payload in reads
            # 库存核对回执也含同次筛选候选，不能把真实证据误判为模型编造。
            for item in payload.get("items", [])
            + payload.get("library_check", {}).get("items", [])
            if item.get("tmdb_id")
            and str(item.get("year", "")).isdigit()
            and int(item["year"]) >= rubric["min_year"]
            and item.get("library_status") == "missing"
            and arguments.get("media_type") == "tv"
            and arguments.get("exclude_library") is True
            and str(arguments.get("region") or ((arguments.get("filters") or {}).get("with_origin_country", "")
                                               if sdk_reads is not None else "")).casefold()
            in {"cn", "中国", "中国大陆", "大陆"}
            and str(arguments.get("genre") or ((arguments.get("filters") or {}).get("with_genres", "")
                                              if sdk_reads is not None else "")).casefold()
            in ({"animation", "动画", "动漫", "16"} if sdk_reads is not None else {"animation", "动画", "动漫"})
        }
        matched = {
            key
            for key, item in items.items()
            if item.get("title")
            and re.search(
                # 只认同一行完整的列表/表格标题和年份，不让短标题冒名。
                r"(?:^|[,，;；|])[ \t]*(?:(?:\d+[.)、]|[-+*])[ \t]+)?"
                r"[*_`《「『]*"
                rf"(?:\[{re.escape(item['title'])}\]\([^\r\n)]*\)|{re.escape(item['title'])})"
                r"[*_`》」』]*(?:[ \t]*[（(|][ \t]*|[ \t]+)"
                rf"[*_`]*{re.escape(str(item['year']))}(?!\d)",
                answers,
                re.MULTILINE,
            )
        }
        recommended = {
            item.get("tmdb_id") for _, payload in reads for item in payload.get("items", [])
        }
        evidence = {
            "recommended_items_matched": len(matched & recommended),
            "same_batch_verified_matches": len(matched),
        }
        # 库存核对只能补充证据，不能放宽“交付足量推荐 items”的既定门槛。
        checks["enough_verified_absent_titles"] = (
            evidence["recommended_items_matched"] >= rubric["count"]
        )
    elif kind == "favorites_search":
        rows = selected("discovery.watchlist_summaries")
        empty = bool(rows) and all(
            data(row).get("count", data(row).get("total")) == 0 for row in rows
        )
        if empty:
            outcome = "not_configured"
            checks["watchlist_configured"] = False
            checks["empty_watchlist_disclosed"] = bool(
                re.search(r"(?:没有|暂无|未.*(?:添加|收藏)|为空|空的|0.*项)", answers)
            )
        else:
            checks["updates_then_resource_search"] = (
                bool(rows)
                and bool(selected("library.check_updates"))
                and bool(
                    selected("indexer.search_resources")
                    or selected("library.search_missing_season_resources")
                )
            )
    elif kind == "scope_aba":
        rows = selected("media.recently_added") if sdk_reads is None else _sdk_results(sdk_reads, "library.recent")
        def scope_data(row):
            return data(row) if sdk_reads is None else row["result"]

        def work_titles(group):
            # 最近入库本身按时间倒序；工作区可多取后按真实 Series 身份归并。
            # 使用本轮最后一次读取，避免把多次查询窗口的并集当成用户要的五部。
            payload = scope_data(group[-1]) if group else {}
            if payload.get("granularity") not in {"work", "episode"}:
                return []
            titles = {}
            for item in payload.get("items", []):
                episode = payload.get("granularity") == "episode" or str(item.get("type", "")).lower() == "episode"
                title = item.get("series_name") or item.get("title") or item.get("name")
                identity = item.get("series_id") or item.get("id") or title
                if not title or episode and not (item.get("series_id") and item.get("series_name")):
                    return []
                titles.setdefault(identity, title)
                if len(titles) == 5:
                    break
            return list(titles.values())

        def matches_scope(row, scope):
            arguments = row["arguments"]
            returned = scope_data(row).get("scope", {}).get("library", {})
            return (arguments.get("library_name", "").strip() == scope or
                    bool(arguments.get("library_id")) and arguments["library_id"] == returned.get("id") and returned.get("name") == scope)

        if sdk_reads is not None and not rows:
            checks["native_scope_reads_present"] = False
            outcome = "not_exercised"
        first = [row for row in rows if row.get("turn") == 1]
        second = [row for row in rows if row.get("turn") == 2]
        checks["explicit_a_b_scopes"] = (
            bool(first and second)
            and all(
                matches_scope(row, rubric["scopes"][0])
                for row in first
            )
            and all(
                matches_scope(row, rubric["scopes"][1])
                for row in second
            )
        )
        checks["returned_a_b_scopes"] = all(
            scope_data(row).get("scope", {}).get("library", {}).get("name") == scope
            and bool(work_titles([row]))
            for group, scope in (
                (first, rubric["scopes"][0]),
                (second, rubric["scopes"][1]),
            )
            for row in group
        )
        titles, anime = work_titles(first), work_titles(second)
        evidence["recent_work_titles"] = {rubric["scopes"][0]: titles, rubric["scopes"][1]: anime}
        checks["returns_to_original_titles"] = (
            len(turns) == 3
            and len(set(titles)) == len(set(anime)) == 5
            and all(titles)
            and all(anime)
            and all(
                title in turns[0].get("answer", "")
                and title in turns[2].get("answer", "")
                for title in titles
            )
            and all(title in turns[1].get("answer", "") for title in anime)
            and not any(
                title in turns[2].get("answer", "")
                for title in set(anime) - set(titles)
            )
            and not any(row.get("turn") == 3 for row in trace.get("tools", []))
            and not any(row.get("turn") == 3 for row in sdk_reads or ())
        )
    elif kind == "preview_cancel":
        checks["twenty_frozen_preview"] = any(
            row["public"].get("status") == "confirmation_required"
            and len(set(row["arguments"].get("result_ids", []))) == rubric["count"]
            and data(row).get("count") == len(data(row).get("resources", [])) == rubric["count"]
            and [item.get("result_id") for item in data(row).get("resources", [])]
            == row["arguments"].get("result_ids")
            for row in selected("resources.download")
        )
        checks["frozen_approval_plan"] = sum(
            event.get("type") == "effect.approval_required"
            and bool(event.get("payload", {}).get("plan", {}).get("plan_id"))
            for turn in turns
            for event in turn.get("events", [])
        ) == 1
        checks["preview_cancelled"] = trace.get("cancelled_plan") is True
    elif kind == "constraint_memory":
        constraint_checks, evidence = _assess_eight_constraints(case, trace)
        checks.update(constraint_checks)
    approvals = sum(
        event.get("type") == "effect.approval_required"
        for turn in turns
        for event in turn.get("events", [])
    )
    if case["version"] == 2:
        checks["no_unnecessary_structured_intervention"] = approvals == (1 if kind == "preview_cancel" else 0)
        if not checks["no_unnecessary_structured_intervention"] or trace.get("failure"):
            outcome = "failed"
    if kind in {"actor_over_fifty", "year_identity", "read_recovery", "constraint_memory"}:
        checks["no_unnecessary_structured_intervention"] = approvals == 0 and not any(
            turn.get("status") == "waiting" for turn in turns
        )
    checks["no_runtime_failure"] = not trace.get("failure")
    if sdk_reads is not None:
        evidence["sdk_observations"] = [{"evidence_id": row["evidence_id"], "source": row["source"],
                                         "turn": row["turn"], "method": row["method"]} for row in sdk_reads]
        checks["host_observations_valid"] = checks.get("host_observations_valid", True) and host_error is None
        checks["read_only_execution"] = ((checks.get("no_confirmed_or_unrelated_write", False) if case["version"] == 2 and kind == "preview_cancel"
                                          else _read_only_execution(trace, sdk_reads))
                                         and isinstance(host_evidence, dict)
                                         and isinstance(host_evidence.get("effects"), list) and not host_evidence["effects"])
        if kind not in {"actor_count", "actor_over_fifty", "weekly_calendar", "recommendation", "scope_aba"} and case["version"] == 1:
            checks["native_rubric_observed"] = False
            outcome = "not_exercised"
        if case["version"] == 2 and kind == "favorite_search" and not any(
            key in (host_evidence or {}) for key in ("watchlist", "audits", "resource_searches")
        ):
            evidence["missing_evidence"] = ["watchlist", "audits", "resource_searches"]
            outcome = "not_exercised"
        if host_error or not checks["read_only_execution"]:
            outcome = "failed"
            if host_error:
                evidence["contract_error"] = host_error
    if kind == "constraint_memory" and all(
        valid for key, valid in checks.items() if key != "answer_semantics_verified"
    ):
        outcome = "manual_review"
    if trace.get("classification") == "infrastructure_interference":
        checks["uninterfered_harness"] = False
        outcome = "infrastructure_interference"
    calls = trace.get("model_calls", [])
    observed: dict[str, int] = {}
    unknown = 0
    for call in calls:
        if type((call.get("usage") or {}).get("total_tokens")) is not int:
            unknown += 1
        for key, value in (call.get("usage") or {}).items():
            if type(value) is int and value >= 0:
                observed[key] = observed.get(key, 0) + value
    passed = all(checks.values())
    return {
        "case_id": case["id"],
        "version": case["version"],
        "split": case["split"],
        "source": trace.get("source"),
        "source_commit": trace.get("source_commit"),
        "execution_mode": (
            "hybrid" if trace.get("matrix", {}).get("scenario") in {
                "read_missing_query", "read_invalid_enum", "hybrid_guoman_front_owned"
            } else "deterministic" if trace.get("source") == "synthetic_test"
            else trace.get("execution_mode", "production" if trace.get("environment", {}).get("validation_layer") == "real_model_real_readonly_services" else "unclassified")
        ),
        "passed": passed,
        "outcome": "passed" if passed else outcome,
        "checks": checks,
        "evidence": evidence,
        "failure_reasons": [key for key, valid in checks.items() if not valid],
        "user_turns": len(turns),
        "model_rounds": len(calls),
        "http_calls": sum(call.get("http_attempts", 0) for call in calls),
        "necessary_confirmations": approvals if kind == "preview_cancel" else 0,
        "preview_cancellations": int(trace.get("cancelled_plan") is True),
        "confirmed_executions": trace.get("confirmed_executions"),
        "invalid_interventions": (0 if kind == "preview_cancel" else approvals)
        + (sum(turn.get("status") == "waiting" for turn in turns) if case["version"] == 1 else 0),
        "usage": observed or None,
        "usage_complete": unknown == 0 and bool(calls),
        "usage_unknown_calls": unknown,
        "runtime_failure": trace.get("failure"),
        "tool_failures": [
            {
                "tool": row.get("tool", row.get("command")),
                "error": row.get("error"),
                "status": row.get("public", {}).get("status"),
            }
            for row in trace.get("tools", [])
            if row.get("error") or row.get("public", {}).get("ok") is False
        ] + [{"tool": row["method"], "error": row.get("error_type"),
              "status": (row.get("result") or {}).get("status") or row.get("error_type"),
              "source": row["source"]}
             for row in sdk_reads or ()
             if row.get("error_type") or row.get("error") or (row.get("result") or {}).get("ok") is False],
        **({"business_checks_passed": all(value for name, value in checks.items() if name != "answer_semantics_verified")}
           if case["version"] == 2 else {}),
    }


def _host_evidence_by_run(host_evidence):
    if any(not isinstance(row, dict) or type(row.get("case_version")) is not int or any(
        not isinstance(row.get(key), str) or not row[key].strip() for key in ("run_id", "case_id")
    ) for row in host_evidence):
        raise ValueError("宿主证据必须带运行编号、任务与版本")
    hosts = {(row["run_id"], row["case_id"], row["case_version"]): row for row in host_evidence}
    if len(hosts) != len(host_evidence):
        raise ValueError("宿主证据运行编号、任务与版本重复")
    return hosts


def summarize_live_tasks(
    cases: list[dict[str, Any]], traces: list[dict[str, Any]], *, host_evidence: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    by_id = {(case["id"], case["version"]): case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError("真实任务ID与版本重复")
    hosts = _host_evidence_by_run(host_evidence)
    if any((trace.get("case_id"), trace.get("case_version")) not in by_id for trace in traces):
        raise ValueError("真实记录引用了未知任务")
    outcomes = [
        assess_live_task(by_id[(trace["case_id"], trace["case_version"])], trace,
                         host_evidence=hosts.get((trace.get("run_id"), trace["case_id"], trace["case_version"])))
        for trace in traces
        if trace.get("classification") != "infrastructure_failure_not_sent"
    ]
    passed = sum(row["passed"] for row in outcomes)
    usage: dict[str, int] = {}
    for outcome in outcomes:
        for key, value in (outcome["usage"] or {}).items():
            usage[key] = usage.get(key, 0) + value
    return {
        "ok": bool(outcomes) and passed == len(outcomes),
        "scope": "versioned_task_trace_assessment",
        "summary": {
            "cases": len(outcomes),
            "passed": passed,
            "success_rate": passed / len(outcomes) if outcomes else None,
            "not_configured": sum(
                row["outcome"] == "not_configured" for row in outcomes
            ),
            "infrastructure_interference": sum(
                row["outcome"] == "infrastructure_interference" for row in outcomes
            ),
            "infrastructure_not_sent": sum(
                trace.get("classification") == "infrastructure_failure_not_sent"
                for trace in traces
            ),
            "model_rounds": sum(row["model_rounds"] for row in outcomes),
            "http_calls": sum(row["http_calls"] for row in outcomes),
            "necessary_confirmations": sum(
                row["necessary_confirmations"] for row in outcomes
            ),
            "invalid_interventions": sum(
                row["invalid_interventions"] for row in outcomes
            ),
            "preview_cancellations": sum(
                row["preview_cancellations"] for row in outcomes
            ),
            "usage": usage or None,
            "usage_complete": bool(outcomes)
            and all(row["usage_complete"] for row in outcomes),
            "usage_unknown_calls": sum(row["usage_unknown_calls"] for row in outcomes),
            "execution_modes": {
                mode: sum(row["execution_mode"] == mode for row in outcomes)
                for mode in sorted({row["execution_mode"] for row in outcomes})
            },
            **({"business_checks_passed": sum(row.get("business_checks_passed", False) for row in outcomes),
                "manual_review": sum(row["outcome"] == "manual_review" for row in outcomes),
                "inconclusive": sum(row["outcome"] == "inconclusive" for row in outcomes),
                "not_exercised": sum(row["outcome"] == "not_exercised" for row in outcomes)}
               if any(row["version"] == 2 for row in outcomes) else {}),
        },
        "outcomes": outcomes,
    }


def load_task_matrix(path: Path = DEFAULT_MATRIX_FIXTURE) -> list[dict[str, Any]]:
    """26项是执行清单，不把pytest或7个query映射成真实覆盖。"""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if (
        len(rows) != 26
        or {row.get("id") for row in rows} != {f"T{index:02}" for index in range(1, 27)}
        or any(
            type(row.get("version")) is not int or row["version"] not in {1, 2}
            or not row.get("messages") or not all(isinstance(message, str) and message.strip() for message in row["messages"])
            or not row.get("acceptance") or not isinstance(row.get("runner_cases"), list)
            for row in rows
        )
    ):
        raise ValueError("26任务矩阵ID、版本或执行清单无效")
    return rows


def live_supplementary_evidence(case: dict[str, Any], trace: dict[str, Any], *, host_evidence=None) -> dict[str, Any]:
    """补充观察不改变任何冻结v1判据。"""
    kind = case["rubric"]["kind"]
    rows = [row for row in trace.get("tools", []) if row.get("public", {}).get("ok") is True]
    try:
        sdk_reads = _host_sdk_reads(trace, host_evidence)
    except ValueError as exc:
        return {"contract_error": str(exc)}
    if kind == "recommendation":
        items = [item for _, payload in _recommendation_reads(trace, sdk_reads) for item in payload.get("items", [])]
        provenance = trace if sdk_reads is None else host_evidence or {}
        source = {str(item["id"]): item for page in provenance.get("source_evidence", []) for item in page.get("items", []) if item.get("id")}
        verified, rejected = [], []
        for item in items:
            raw = source.get(str(item.get("tmdb_id")))
            if raw is not None:
                (verified if "CN" in (raw.get("origin_country") or []) and 16 in (raw.get("genre_ids") or []) and raw.get("adult") is False else rejected).append(str(item["tmdb_id"]))
        return {"strict_item_ids": sorted({str(item.get("tmdb_id")) for item in items}),
                "cn_animation_nonadult_ids": sorted(set(verified)),
                "source_metadata_rejected_ids": sorted(set(rejected)),
                "source_metadata_missing_ids": sorted({str(item.get("tmdb_id")) for item in items} - source.keys())}
    if kind == "weekly_calendar":
        if sdk_reads is not None:
            return _native_calendar_evidence(sdk_reads, case["rubric"]["source"], "")[1]
        pages = [row["public"].get("data", {}) for row in rows if row["tool"] == "discovery.anime_calendar"]
        slots = {(item.get("title"), item.get("date"), item.get("update_time"))
                 for page in pages for item in page.get("items", [])}
        return {"returned_events": sum(len(page.get("items", [])) for page in pages),
                "distinct_title_date_time_slots": len(slots),
                "requested_dates": sorted({day for page in pages for day in page.get("requested_dates", [])}),
                "provider_statuses": sorted({source.get("status", "unknown") for page in pages for source in page.get("sources", [])}),
                "last_has_more": pages[-1].get("has_more") if pages else None,
                "note": "事件还含audience/schedule；合并同日同时间作品不等于逐项披露所有事件字段，补充证据不覆盖v1结果。"}
    return {}


def assess_pagination_replay(trace: dict[str, Any]) -> dict[str, bool]:
    """仅验真实执行的固定页/库存回放，不把计划注入当已触发。"""
    matrix = trace.get("matrix", {})
    replay = matrix.get("replay", {})
    pages, inventory = replay.get("pages", []), replay.get("inventory_reads", [])
    owned = {str(key) for item in matrix.get("applied", []) for key in item.get("owned_ids", [])}
    first_two = [page for page in pages if page.get("page") in {1, 2}]
    groups = {(row.get("server"), row.get("media_type")) for row in inventory}
    return {
        "front_two_pages_observed_owned": {page.get("page") for page in first_two} == {1, 2}
        and all(page.get("ids") and set(page["ids"]) <= owned for page in first_two),
        "later_page_observed": any(page.get("page", 0) >= 3 for page in pages),
        "one_inventory_per_request_source_type": bool(inventory) and len(groups) == len(inventory),
        "declared_contract_replay": replay.get("provenance", {}).get("kind") == "synthetic_contract_replay",
    }


def _assess_calendar_loading(trace, host, result):
    """T03 v2场景叠加，不改tencent_week@1结果；缺宿主首次现场不能推断loading。

    calendar_initial由宿主附在首次media.calendar的sdk_read上，source指向等待前
    get_week(wait_sources=())的真实原始快照；elapsed_ms覆盖该现场到SDK返回，
    wait_timeout_ms取同一服务的既有等待上限。不是模型声明，也不从文件路径补证。
    """
    checks = {"case_checks_passed": result["passed"], "covered_dates_complete": False,
              "current_week": False, "source_success_without_failures": False,
              "initial_loading_observed": False, "same_week_observed": False,
              "bounded_wait_observed": False, "answer_semantics_verified": False}
    evidence: dict[str, Any] = {"manual_review_required": ["逐日交付所有title/date/time/audience/schedule，包含免费与会员分支",
                                                          "保留partial非全站排期的含义，核对错误反馈与多余介入"]}
    try:
        reads = _host_sdk_reads(trace, host)
        rows = [row for row in reads or () if row["method"] == "media.calendar"]
        if not rows:
            evidence["missing_evidence"] = ["bound_host_media.calendar"]
            return checks, evidence, "inconclusive"
        first = rows[0]
        native_checks, native_evidence = _native_calendar_evidence(
            [first], "tencent", "\n".join(turn.get("answer", "") for turn in trace["turns"]))
        checks.update(native_checks)
        evidence.update(native_evidence)
        snapshot = first.get("result") or {}
        dates = evidence.get("requested_dates", [])
        checks["covered_dates_complete"] = len(dates) == 7 and evidence.get("covered_dates") == dates
        checks["current_week"] = snapshot.get("today") in dates
        checks["source_success_without_failures"] = all(
            not row.get("error") and not row.get("error_type") and row.get("result", {}).get("ok") is not False
            and [state.get("status") for state in row.get("result", {}).get("sources", [])
                 if state.get("id") == "tencent"] in (["ok"], ["partial"])
            for row in rows
        ) and not host.get("blocked_attempts")
        data_checks = ("case_checks_passed", "covered_dates_complete", "current_week",
                       "source_success_without_failures", *native_checks)
        if not all(checks[key] for key in data_checks):
            return checks, evidence, "failed"
        initial = first.get("calendar_initial")
        if not isinstance(initial, dict) or any(key not in initial for key in ("source", "result", "wait_timeout_ms")) or "elapsed_ms" not in first:
            evidence["missing_evidence"] = ["first_sdk_read.calendar_initial(source,result,wait_timeout_ms)",
                                            "first_sdk_read.elapsed_ms"]
            return checks, evidence, "inconclusive"
        if not isinstance(initial["source"], str) or not initial["source"].strip() or initial["source"] == first["source"]:
            raise ValueError("首次周历现场必须有独立宿主原始证据引用")
        before = initial["result"]
        checks["same_week_observed"] = (before["week_start"] == snapshot["week_start"]
                                        and before["timezone"] == snapshot["timezone"]
                                        and [day["date"] for day in before["days"]] == dates)
        checks["initial_loading_observed"] = [state.get("status") for state in before["sources"]
                                              if state.get("id") == "tencent"] == ["loading"]
        elapsed, bound = first["elapsed_ms"], initial["wait_timeout_ms"]
        checks["bounded_wait_observed"] = (type(elapsed) in {int, float} and type(bound) in {int, float}
                                            and 0 <= elapsed <= bound < float("inf") and bound > 0)
        evidence.update(initial_source=initial["source"], elapsed_ms=elapsed, wait_timeout_ms=bound,
                        provider_messages=[state.get("message", "") for state in snapshot["sources"]
                                           if state.get("id") == "tencent"])
        if not checks["same_week_observed"] or not checks["bounded_wait_observed"]:
            return checks, evidence, "failed"
        return checks, evidence, "manual_review" if checks["initial_loading_observed"] else "not_exercised"
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        evidence["contract_error"] = str(exc)
        return checks, evidence, "failed"


def summarize_task_matrix(
    traces: list[dict[str, Any]], *, matrix_path: Path = DEFAULT_MATRIX_FIXTURE,
    cases_path: Path = DEFAULT_MATRIX_CASES,
    host_evidence: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    hosts = _host_evidence_by_run(host_evidence)
    tasks_manifest = load_task_matrix(matrix_path)
    paths = [DEFAULT_LIVE_FIXTURE, DEFAULT_MATRIX_CASES, cases_path]
    if any(task["version"] == 2 for task in tasks_manifest):
        paths.extend((DEFAULT_NATURAL_FIXTURE, DEFAULT_SDK_FIXTURE, DEFAULT_CAPABILITY_FIXTURE))
    cases = {}
    for path in dict.fromkeys(paths):
        for row in load_live_cases(path):
            key = (row["id"], row["version"])
            if key in cases and cases[key] != row:
                raise ValueError("矩阵任务ID/版本定义冲突")
            cases[key] = row
    unknown = {(trace.get("case_id"), trace.get("case_version")) for trace in traces} - cases.keys()
    if unknown:
        raise ValueError("矩阵记录引用了未知case")
    tasks = []
    for task in tasks_manifest:
        runs = []
        for trace in traces:
            if trace["case_id"] not in task["runner_cases"] + task["frozen_cases"]:
                continue
            case = cases[(trace["case_id"], trace["case_version"])]
            host = hosts.get((trace.get("run_id"), trace["case_id"], trace["case_version"]))
            result = assess_live_task(case, trace, host_evidence=host)
            result["supplementary"] = live_supplementary_evidence(case, trace, host_evidence=host)
            # 只有有领域判据和必要现场的任务可自动覆盖；其余明确保留缺口。
            if result["outcome"] == "infrastructure_interference":
                status = "infrastructure_interference"
            elif task["id"] == "T01":
                status = ("automated_passed" if result["passed"] else result["outcome"]) if trace["case_id"] == "actor_count_over_fifty" else "not_exercised"
            elif task["id"] == "T03" and task["version"] == 2:
                result["scenario_checks"], result["scenario_evidence"], status = _assess_calendar_loading(trace, host, result)
            elif task["id"] in {"T03", "T12"}:
                # 正常周历/ABA没有首次loading或超长历史现场，冻结结果仍独立保留。
                status = "not_exercised"
            elif task["id"] == "T11":
                status = ("automated_passed" if result["evidence"]["other_entity_years"] else "partial") if result["passed"] else result["outcome"]
            elif task["id"] in {"T13", "T14"} and trace["case_id"] in task["runner_cases"]:
                status = "automated_passed" if result["passed"] else result["outcome"]
            elif task["id"] == "T09":
                extra = result["supplementary"]
                verified = bool(extra.get("strict_item_ids")) and set(extra["strict_item_ids"]) <= set(extra["cn_animation_nonadult_ids"])
                status = ("automated_passed" if verified else "partial") if result["passed"] else result["outcome"]
            elif task["id"] == "T10" and trace["case_id"] == "hybrid_guoman_front_owned":
                result["scenario_checks"] = assess_pagination_replay(trace)
                status = "automated_passed" if result["passed"] and all(result["scenario_checks"].values()) else "not_exercised" if not trace.get("matrix", {}).get("applied") else "failed"
            else:
                status = "partial" if result["passed"] else result["outcome"]
            result["matrix_status"] = status
            result["language_review"] = "required: coverage/error_feedback/unnecessary_intervention"
            runs.append(result)
        versions = {}
        for version in sorted({row["version"] for row in runs}):
            selected = [row for row in runs if row["version"] == version]
            status = "automated_passed" if any(row["matrix_status"] == "automated_passed" for row in selected) else selected[-1]["matrix_status"]
            if task["id"] == "T14":
                scenarios = [row for row in selected if row["case_id"] in task["runner_cases"]]
                if all(any(row["case_id"] == case_id and row["matrix_status"] == "automated_passed" for row in scenarios) for case_id in task["runner_cases"]):
                    status = "automated_passed"
                elif any(row["matrix_status"] == "failed" for row in scenarios):
                    status = "failed"
                elif any(row["matrix_status"] == "automated_passed" for row in scenarios):
                    status = "partial"
                else:
                    status = scenarios[-1]["matrix_status"] if scenarios else "not_exercised"
                if version == 2 and not any(row["matrix_status"] == "failed" for row in scenarios):
                    covered = {row["case_id"] for row in scenarios if row.get("business_checks_passed")}
                    if covered:
                        status = "manual_review" if set(task["runner_cases"]) <= covered else "partial"
            versions[str(version)] = status
        status = versions[str(max(row["version"] for row in runs))] if runs else "not_exercised"
        tasks.append({"task_id": task["id"], "matrix_version": task["version"], "title": task["title"],
                      "status": status,
                      "versions": versions,
                      "external_conditions": task["external_conditions"], "runner_status": task["runner_status"], "runs": runs})
    return {"ok": False, "scope": "26_task_matrix_partial_evidence_not_release_acceptance",
            "summary": {"tasks": 26, "tasks_with_runs": sum(bool(row["runs"]) for row in tasks),
                        "automated_passed": sum(row["status"] == "automated_passed" for row in tasks),
                        "fully_reviewed_passed": 0,
                        "note": "自动数据判据不替代错误反馈/多余介入的逐段审查；未跑的任务和异端现场不补数。"}, "tasks": tasks}


def load_live_traces(paths: Sequence[Path], interference: Sequence[Path] = ()) -> list[dict[str, Any]]:
    traces = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for path in interference:
        annotation = json.loads(path.read_text(encoding="utf-8"))
        if annotation.get("classification") != "infrastructure_interference" or not annotation.get("cause"):
            raise ValueError("设施干扰记录必须有明确分类及原因")
        for trace in traces:
            if annotation.get("affected_run") == f"{trace.get('source')}/{trace.get('case_id')}":
                trace["classification"] = "infrastructure_interference"
                trace["interference_evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return traces


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    message: str
    required: tuple[str, ...]
    provenance: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EvalOutcome:
    case_id: str
    passed: bool
    selected: tuple[str, ...]
    missing: tuple[str, ...]
    provenance: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "selected": list(self.selected),
            "missing": list(self.missing),
            "provenance": self.provenance or {"kind": "legacy_unverified"},
        }



@dataclass(frozen=True, slots=True)
class LifecycleCase:
    case_id: str
    message: str
    result: dict[str, Any]
    followup: str
    expected: dict[str, Any]


class _ReplayModel:
    def __init__(self, case: LifecycleCase) -> None:
        self.requests: list[ModelRequest] = []
        self._rounds = [
            [
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("write", "cloud.lifecycle_write", {}),
                ),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
            ],
            [
                ModelEvent(ModelEventType.TEXT_DELTA, text=case.followup),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ],
        ]

    async def stream(
        self, request: ModelRequest, *, cancellation: Any
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self._rounds:
            raise AssertionError("生命周期回放出现了意外模型轮次")
        for event in self._rounds.pop(0):
            cancellation.raise_if_cancelled()
            await asyncio.sleep(0)
            yield event


def load_lifecycle_cases(path: Path) -> list[LifecycleCase]:
    cases: list[LifecycleCase] = []
    seen: set[str] = set()
    expected_fields = {
        "final_status",
        "result_state",
        "model_calls",
        "confirm_events",
        "answer_contains",
        "answer_excludes",
    }
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line.strip():
            continue
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"生命周期语料第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {
            "id", "message", "result", "followup", "expected"
        }:
            raise ValueError(f"生命周期语料第 {line_number} 行字段无效")
        case_id = str(raw["id"] or "").strip()
        message = str(raw["message"] or "").strip()
        followup = str(raw["followup"] or "").strip()
        result = raw["result"]
        expected = raw["expected"]
        if (
            not case_id
            or case_id in seen
            or not message
            or not followup
            or not isinstance(result, dict)
            or set(result) - {"ok", "status", "summary", "data", "error"}
            or type(result.get("ok")) is not bool
            or not str(result.get("status") or "").strip()
            or not str(result.get("summary") or "").strip()
            or not isinstance(expected, dict)
            or set(expected) != expected_fields
            or type(expected.get("model_calls")) is not int
            or expected["model_calls"] < 1
        ):
            raise ValueError(f"生命周期语料第 {line_number} 行案例无效")
        for key in ("confirm_events", "answer_contains", "answer_excludes"):
            values = expected.get(key)
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ValueError(f"生命周期语料第 {line_number} 行 {key} 无效")
        seen.add(case_id)
        cases.append(LifecycleCase(case_id, message, dict(result), followup, dict(expected)))
    if not cases:
        raise ValueError("生命周期评测语料为空")
    return cases


async def _replay_lifecycle_case(case: LifecycleCase) -> dict[str, Any]:
    executions: list[dict[str, Any]] = []
    tool = KernelToolSpec(
        name="cloud.lifecycle_write",
        domain="cloud",
        description="离线验收确认生命周期",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.WRITE,
        prepare=lambda _arguments, _context: PreparedEffect(
            preview={"summary": "确认后执行生命周期验收写操作"},
            snapshot_fingerprint="lifecycle-replay",
        ),
        execute_confirmed=lambda arguments, _snapshot, _context: (
            executions.append(dict(arguments)) or dict(case.result)
        ),
    )
    catalog = ToolCatalog([tool])
    state = InMemorySessionStateStore()
    model = _ReplayModel(case)
    session = AgentSession(
        model=model,
        catalog=catalog,

        pipeline=ToolPipeline(catalog=catalog, state_store=state),
        state_store=state,
    )
    owner = f"eval:{case.case_id}"
    session_id = f"lifecycle-{case.case_id}"
    preview = await consume_events(session.run(AgentInput(
        message=case.message, owner=owner, session_id=session_id, channel="eval",
    )))
    if preview.approval is None:
        return {"case_id": case.case_id, "passed": False, "errors": ["未生成确认计划"]}
    confirm_events = [
        event async for event in session.confirm(
            owner=owner, session_id=session_id, plan_id=preview.approval.plan_id, channel="eval",
        )
    ]

    async def stream() -> AsyncIterator[Any]:
        for event in confirm_events:
            yield event

    final = await consume_events(stream())
    current = await state.load(owner=owner, session_id=session_id)
    actual_events = [event.type.value for event in confirm_events]
    public_dump = json.dumps([event.to_dict() for event in confirm_events], ensure_ascii=False)
    expected = case.expected
    errors: list[str] = []
    checks = (
        (preview.status == "approval_required", "预览终态不是 approval_required"),
        (final.status == expected["final_status"], f"最终状态为 {final.status}"),
        (public_result_state(final.effect_result) == expected["result_state"], "结果语义不匹配"),
        (len(model.requests) == expected["model_calls"], f"模型调用 {len(model.requests)} 次"),
        (actual_events == expected["confirm_events"], f"确认事件序列为 {actual_events}"),
        (len(executions) == 1, f"副作用执行 {len(executions)} 次"),
        (not current.pending_effect_plan_id, "确认计划仍处于待处理状态"),
    )
    errors.extend(message for passed, message in checks if not passed)
    for text in expected["answer_contains"]:
        if text not in final.answer:
            errors.append(f"回答缺少：{text}")
    for text in expected["answer_excludes"]:
        if text in final.answer or text in public_dump:
            errors.append(f"公开结果泄漏或误报：{text}")
    return {
        "case_id": case.case_id,
        "passed": not errors,
        "errors": errors,
        "events": actual_events,
        "final_status": final.status,
        "result_state": public_result_state(final.effect_result),
        "model_calls": len(model.requests),
    }


def evaluate_lifecycle(path: Path = DEFAULT_LIFECYCLE_FIXTURE) -> dict[str, Any]:
    cases = load_lifecycle_cases(path)

    async def run() -> list[dict[str, Any]]:
        return [await _replay_lifecycle_case(case) for case in cases]

    outcomes = asyncio.run(run())
    passed = sum(bool(item["passed"]) for item in outcomes)
    return {
        "ok": passed == len(outcomes),
        "summary": {"cases": len(outcomes), "passed": passed, "failed": len(outcomes) - passed},
        "outcomes": outcomes,
    }

def _valid_provenance(
    provenance: dict[str, Any], message: str, required: tuple[str, ...]
) -> bool:
    """仅校验字段与摘录契约；原件哈希供追溯，不读取或认证私有原件。"""
    if provenance["kind"] != "simulated_user_real_model":
        return set(provenance) == {"kind"}
    if set(provenance) != {
        "kind", "source_commit", "evidence_sha256", "excerpt"
    } or not isinstance(provenance.get("source_commit"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", provenance["source_commit"]
    ):
        return False
    hashes = provenance["evidence_sha256"]
    excerpt = provenance["excerpt"]
    if not isinstance(hashes, dict) or not {
        "model-request-1.json", "HTTP-response-1.json", "model-events-1.json",
        "query-events.json", "case-summary.json",
    } <= set(hashes) or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in hashes.items()
    ):
        return False
    if not isinstance(excerpt, dict) or set(excerpt) != {
        "message", "initial_tools", "initial_missing", "http_status",
        "completed_tool_calls", "final_status", "business_pass",
    }:
        return False
    tools = excerpt["initial_tools"]
    calls = excerpt["completed_tool_calls"]
    if (
        excerpt["message"] != message
        or not isinstance(tools, list)
        or not tools
        or any(not isinstance(name, str) or not name.strip() for name in tools)
        or excerpt["initial_missing"] != [name for name in required if name not in tools]
        or type(excerpt["http_status"]) is not int
        or not 100 <= excerpt["http_status"] <= 599
        or type(excerpt["business_pass"]) is not bool
        or not isinstance(excerpt["final_status"], str)
        or not excerpt["final_status"].strip()
        or not isinstance(calls, list)
        or any(
            not isinstance(call, dict)
            or set(call) != {"model_index", "name", "arguments"}
            or type(call["model_index"]) is not int
            or call["model_index"] < 1
            or not isinstance(call["name"], str)
            or not call["name"].strip()
            or not isinstance(call["arguments"], dict)
            for call in calls
        )
    ):
        return False
    return True


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
        if (
            not isinstance(raw, dict)
            or not {"id", "message", "required"} <= set(raw)
            or set(raw) - {"id", "message", "required", "provenance"}
        ):
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
        provenance = raw.get("provenance", {"kind": "legacy_unverified"})
        if (
            not isinstance(provenance, dict)
            or provenance.get("kind") not in PROVENANCE_KINDS
            or not _valid_provenance(provenance, message, required)
        ):
            raise ValueError(f"第 {line_number} 行 provenance 无效")
        seen.add(case_id)
        cases.append(EvalCase(case_id, message, required, provenance))
    if not cases:
        raise ValueError("评测语料为空")
    return cases


def evaluate(
    path: Path = DEFAULT_FIXTURE,
    lifecycle_path: Path = DEFAULT_LIFECYCLE_FIXTURE,
) -> dict[str, Any]:
    cases = load_cases(path)
    catalog = build_tool_catalog()
    invalid_effect_tools = [tool.name for tool in catalog.visible() if (
        tool.read is None if tool.effect is ToolEffect.READ else
        tool.read is not None or tool.prepare is None or tool.execute_confirmed is None)]
    lifecycle = evaluate_lifecycle(lifecycle_path)
    return {
        "ok": not invalid_effect_tools and lifecycle["ok"],
        "scope": "effect_gate_and_lifecycle_only",
        "legacy_corpus": {"status": "retired_not_scored", "cases": len(cases),
                          "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "evidence": [{"id": case.case_id, "provenance": case.provenance} for case in cases]},
        "summary": {"cases": lifecycle["summary"]["cases"], "passed": lifecycle["summary"]["passed"],
                    "failed": lifecycle["summary"]["failed"], "effect_gate_valid": not invalid_effect_tools,
                    "lifecycle_cases": lifecycle["summary"]["cases"], "lifecycle_passed": lifecycle["summary"]["passed"],
                    "lifecycle_failed": lifecycle["summary"]["failed"]},
        "invalid_effect_tools": invalid_effect_tools,
        "lifecycle_outcomes": lifecycle["outcomes"],
    }


def _text_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    return ("MediaFlux Agent Kernel 离线生命周期验收\n"
            f"{summary['passed']}/{summary['cases']}通过；Effect Gate：{summary['effect_gate_valid']}\n"
            "旧首窗召回语料已退役，不重算或覆盖历史得分；此结果不代表真实用户任务通过率。")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--lifecycle-fixture", type=Path, default=DEFAULT_LIFECYCLE_FIXTURE
    )
    parser.add_argument("--live-results", type=Path, nargs="+", help="评判已有真实运行 JSON；不会发起付费调用")
    parser.add_argument("--live-fixture", type=Path, default=DEFAULT_LIVE_FIXTURE)
    parser.add_argument("--host-evidence", type=Path, nargs="+", default=[], help="宿主独立保存的服务观察（sdk_reads 或领域观察）；模型工作区中的JSON不能作为此输入")
    parser.add_argument("--matrix", action="store_true", help="输出26项执行清单；不运行模型/服务")
    parser.add_argument("--matrix-results", type=Path, nargs="+", help="离线映射已有运行；缺少现场保持未覆盖")
    parser.add_argument("--matrix-fixture", type=Path, default=DEFAULT_MATRIX_FIXTURE)
    parser.add_argument("--matrix-cases", type=Path, default=DEFAULT_MATRIX_CASES)
    parser.add_argument("--interference-report", type=Path, nargs="*", default=[], help="引用已有设施干扰证据，不修改原始run")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.matrix_results:
            result = summarize_task_matrix(load_live_traces(args.matrix_results, args.interference_report), matrix_path=args.matrix_fixture, cases_path=args.matrix_cases,
                                           host_evidence=[json.loads(path.read_text(encoding="utf-8")) for path in args.host_evidence])
        elif args.matrix:
            result = {"ok": True, "scope": "execution_manifest_only_no_runs", "tasks": load_task_matrix(args.matrix_fixture)}
        elif args.live_results:
            result = summarize_live_tasks(load_live_cases(args.live_fixture), load_live_traces(args.live_results, args.interference_report),
                                          host_evidence=[json.loads(path.read_text(encoding="utf-8")) for path in args.host_evidence])
        else:
            result = evaluate(args.fixture, args.lifecycle_fixture)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    if args.json or args.live_results or args.matrix or args.matrix_results:
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
