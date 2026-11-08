"""Single signed-session task: strict proposals, exact CAS and evidence reduction.

Task declarations are data, never write authorization or a second execution loop.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.sensitive_data import contains_sensitive_credential

COMPONENTS = frozenset({"tmdb", "qbittorrent"})
IDENTITY = ("task_id", "epoch", "revision")
# 历史TaskPatch的字段合同继续由同一领域校验维护。
_OPERATION_FIELDS = {
    "create": ("source", "root_goal", "constraints", "obligations"),
    "checkpoint": ("evidence_ids", "blockers"),
    "revise": ("source", "root_goal", "constraints", "obligations"),
    "cancel": ("source",),
}


def classify_public_task(task: Any = None) -> str:
    """Classify only the public consumer shape, not evidence or authorization."""
    if task is None:
        return "untracked"
    if not isinstance(task, dict):
        return "unknown"

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value)

    if (
        type(task.get("v")) is not int or task["v"] != 1
        or not text(task.get("task_id"))
        or any(type(task.get(k)) is not int or task[k] <= 0 for k in ("epoch", "revision"))
        or task.get("acceptance_scope") != "declared_obligations"
        or task.get("status") not in ("partial", "blocked", "complete", "cancelled")
        or not text(task.get("root_goal"))
    ):
        return "unknown"
    obligations = task.get("obligations")
    remaining = task.get("remaining_obligation_ids")
    blockers = task.get("blockers")
    if not isinstance(obligations, list) or not obligations:
        return "unknown"
    ids: set[str] = set()
    pending: set[str] = set()
    for item in obligations:
        if (not isinstance(item, dict) or not text(item.get("id"))
            or not text(item.get("description")) or item.get("required") is not True
            or item.get("state") not in ("pending", "blocked", "verified")):
            return "unknown"
        if item["id"] in ids:
            return "unknown"
        ids.add(item["id"])
        if item["state"] != "verified":
            pending.add(item["id"])
    if (not isinstance(remaining, list) or not all(isinstance(v, str) for v in remaining)
        or len(set(remaining)) != len(remaining) or set(remaining) != pending
        or not isinstance(blockers, list)
        or any(not isinstance(b, dict) or not text(b.get("text")) for b in blockers)):
        return "unknown"
    if task["status"] == "complete" and (remaining or blockers):
        return "unknown"
    return task["status"]


class TaskConflict(ValueError):
    pass


def _object(value: Any, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid task fields")
    return value


def _text(value: Any, maximum: int = 512) -> str:
    # 任务声明可引用用户的路径/链接和多行原话；不是对外工具摘要。
    # source是否来自获准用户仍由proposal核对，文字不产生文件操作授权。
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError("invalid task text")
    if (contains_sensitive_credential(value)
            or any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value)):
        raise ValueError("unsafe task text")
    return value


def _array(value: Any, maximum: int, minimum: int = 0) -> list:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError("invalid task array")
    if len({json.dumps(v, sort_keys=True, ensure_ascii=False) for v in value}) != len(
        value
    ):
        raise ValueError("duplicate task item")
    return value


def identity(task: dict | None) -> dict | None:
    return {key: task[key] for key in IDENTITY} if task else None


def validate_proposal(value: dict) -> dict:
    operation = value.get("operation")
    if operation not in _OPERATION_FIELDS:
        raise ValueError("invalid task operation")
    optional = {"read_evidence"} if operation == "checkpoint" else set()
    _object(value, {"operation", *_OPERATION_FIELDS[operation], *(optional & value.keys())})
    if "source" in value:
        _text(value["source"], 2048)
    if operation in {"create", "revise"}:
        _text(value["root_goal"], 2048)
        for constraint in _array(value["constraints"], 8):
            _text(constraint)
        obligations = _array(value["obligations"], 16, 1)
        ids = []
        for obligation in obligations:
            _object(obligation, {"id", "description", "required", "acceptance"})
            ids.append(_text(obligation["id"], 64))
            _text(obligation["description"])
            if obligation["required"] is not True:
                raise ValueError("all M1 obligations must be required")
            acceptance = obligation["acceptance"]
            if type(acceptance) is not dict:
                raise ValueError("invalid acceptance")
            kind = _text(acceptance.get("kind"), 64)
            if kind == "observed_component":
                _object(acceptance, {"kind", "component"})
                if acceptance["component"] not in COMPONENTS:
                    raise ValueError("unsupported component verifier")
            else:
                _object(acceptance, {"kind", "criterion"})
                _text(acceptance["criterion"])
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate obligation id")
    if operation == "checkpoint":
        links = value.get("read_evidence", {})
        if type(links) is not dict or len(links) > 16:
            raise ValueError("invalid read evidence links")
        for obligation_id, evidence_id in links.items():
            _text(obligation_id, 64)
            _text(evidence_id, 64)
        for evidence in _array(value["evidence_ids"], 32):
            _text(evidence, 64)
        for blocker in _array(value["blockers"], 16):
            _text(blocker)
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class TaskPatch:
    expected: dict | None
    operation: str
    payload: dict
    source_request_id: str
    source: str
    new_id: str = ""
    source_kind: str = "current_user"


def proposal(arguments: dict, context: Any) -> TaskPatch:
    args = validate_proposal(arguments)
    _text(context.request_id, 64)
    source = args.get("source", "")
    if source and source not in context.user_source:
        history = getattr(context, "user_history", ()) if args["operation"] == "create" else ()
        if not any(source in text for text in history):
            raise ValueError("task source must be current admitted user text or verified history for create")
    return TaskPatch(
        identity(context.current_task),
        args["operation"],
        args,
        context.request_id,
        source,
        secrets.token_urlsafe(18),
        "current_user" if source in context.user_source else "historical_user_quote",
    )


def tracks_read(task: dict | None, tool: str) -> bool:
    return bool(task and task["status"] not in {"complete", "cancelled"}
                and not tool.startswith("agent.") and any(
                    o["acceptance"]["kind"] == "observed_read" and o["state"] != "verified"
                    for o in task["obligations"]))


def configuration_observations(value: Mapping[str, Any] | None, arguments: dict) -> dict:
    """SDK读回执沿用原配置凭证合同；配置不完整也是有效观察。"""
    selected = ({arguments["component"]} if "component" in arguments else COMPONENTS) & COMPONENTS
    observations: dict[str, dict | None] = {component: None for component in sorted(selected)}
    if not value or not isinstance(value.get("data"), dict):
        return observations
    data = value["data"]
    if data.get("probe_mode") != "configuration_only" or data.get("network_accessed") is not False:
        return observations
    if "component" in arguments:
        if value.get("ok") is not True or value.get("status") != data.get("status") or data.get("component") not in selected:
            return observations
        candidates = [data]
    else:
        if value.get("status") not in {"healthy", "degraded", "attention"} or not isinstance(data.get("components"), list):
            return observations
        candidates = [{**item, "component": item.get("name")} for item in data["components"] if isinstance(item, dict)]
    for item in candidates:
        component = item.get("component")
        if (isinstance(component, str) and component in selected and item.get("status") in {"ready", "incomplete", "not_configured"}
                and type(item.get("enabled")) is bool):
            observations[component] = {"component": component, "status": item["status"], "enabled": item["enabled"],
                "probe_mode": "configuration_only", "network_accessed": False}
    return observations


def read_observations(task: dict | None, tool: str, arguments: dict, result: Mapping[str, Any] | None) -> dict:
    """记录实际成功READ回执，不要求模型提前编写调用，也不将探索失败变成整项任务的永久阻塞。"""
    configuration = configuration_observations(result, arguments) if tool in {"diagnostics.configuration", "diagnostics.component"} else {}
    if not tracks_read(task, tool) or not result or result.get("ok") is not True:
        return configuration
    from app.agent.public_safety import sanitize_public_text
    from app.agent.public_view import public_result_state
    if (str(result.get("status", "success")).strip().casefold() not in {
            "ok", "success", "completed", "healthy", "ready", "found", "not_found", "empty"}
            or public_result_state(result) != "success"):
        return configuration
    encoded = json.dumps([tool, arguments], sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    key = "read:" + hashlib.sha256(encoded.encode()).hexdigest()
    return {**configuration, key: {"query_key": key, "status": "success",
                  "summary": sanitize_public_text(result.get("summary") or tool, limit=240) or tool}}


def _read_key(key: str) -> bool:
    return (isinstance(key, str) and len(key) == 69 and key.startswith("read:")
            and all(c in "0123456789abcdef" for c in key[5:]))


def _fact_key(fact: dict) -> str:
    return fact["component"] if fact["verifier_version"] == "configuration-v1" else fact["query_key"]


def _matches(fact: dict, acceptance: dict) -> bool:
    if acceptance["kind"] == "observed_component":
        return fact["verifier_version"] == "configuration-v1" and fact["component"] == acceptance["component"]
    return acceptance["kind"] == "observed_read" and fact["verifier_version"] == "read-v1"


def observation_patch(
    task: dict | None,
    *,
    owner: str,
    session_id: str,
    tool: str,
    call_id: str,
    observations: dict[str, dict | None] | None,
) -> TaskPatch | None:
    if not task or task["status"] in {"complete", "cancelled"} or not observations:
        return None
    facts = {}
    for component, fact in observations.items():
        configuration = component in COMPONENTS
        if not configuration and (not _read_key(component) or not tracks_read(task, tool)):
            continue
        selector = "component" if configuration else "query_key"
        facts[component] = None if fact is None else {
            **fact,
            "evidence_id": secrets.token_urlsafe(18),
            "task_id": task["task_id"], "epoch": task["epoch"],
            "tool": tool, "call_id": call_id,
            "verifier_version": "configuration-v1" if configuration else "read-v1", "observed_at": time.time(),
            "scope": {"owner": owner, "session_id": session_id, "task_id": task["task_id"],
                      "epoch": task["epoch"], selector: component},
        }
    return TaskPatch(identity(task), "observe", {"observations": facts}, "", "") if facts else None


def valid_fact(fact: dict, task: dict, *, owner: str, session_id: str) -> bool:
    if type(fact) is not dict or fact.get("verifier_version") not in {"configuration-v1", "read-v1"}:
        return False
    configuration = fact["verifier_version"] == "configuration-v1"
    selector = "component" if configuration else "query_key"
    keys = {selector, "status", "evidence_id", "task_id", "epoch", "tool", "call_id",
            "verifier_version", "observed_at", "scope"}
    keys |= {"enabled", "probe_mode", "network_accessed"} if configuration else {"summary"}
    if set(fact) != keys:
        return False
    for key in ("evidence_id", "task_id", "tool", "call_id", "verifier_version"):
        if type(fact[key]) is not str or not 1 <= len(fact[key]) <= 64:
            return False
    if configuration:
        valid_payload = (fact["component"] in COMPONENTS
                         and fact["status"] in {"ready", "incomplete", "not_configured"}
                         and type(fact["enabled"]) is bool and fact["network_accessed"] is False
                         and fact["probe_mode"] == "configuration_only")
    else:
        valid_payload = (_read_key(fact["query_key"]) and fact["status"] == "success"
                         and not fact["tool"].startswith("agent.")
                         and type(fact["summary"]) is str and 0 < len(fact["summary"]) <= 240)
    return (valid_payload and type(fact["observed_at"]) in (float, int)
            and type(fact["epoch"]) is int and fact["epoch"] == task["epoch"]
            and fact["task_id"] == task["task_id"]
            and fact["scope"] == {"owner": owner, "session_id": session_id,
                                  "task_id": task["task_id"], "epoch": task["epoch"],
                                  selector: fact[selector]})


def reduce_task(
    current: dict | None, patch: TaskPatch, *, owner: str, session_id: str
) -> dict:
    if identity(current) != patch.expected:
        raise TaskConflict("task revision changed")
    operation, args = patch.operation, patch.payload
    checkpoint_evidence: dict[str, str] = {}
    read_links: dict[str, str] = {}
    if operation == "create":
        if current is not None and current["status"] not in {"complete", "cancelled"}:
            raise TaskConflict("finish or cancel existing task before create")
        task: dict = {"v": 1, "task_id": patch.new_id, "epoch": 1, "revision": 0}
    else:
        if current is None or current["status"] == "cancelled":
            raise TaskConflict("task is not active")
        task = deepcopy(current)
    if operation in {"create", "revise"}:
        validate_proposal(args)
        if operation == "revise":
            task["epoch"] += 1
        task.update(
            source_request_id=patch.source_request_id,
            source_quote=patch.source,
            source_kind=patch.source_kind,
            root_goal=args["root_goal"],
            constraints=deepcopy(args["constraints"]),
            verified_facts=[],
            blockers=[],
            status="partial",
            obligations=[
                {**deepcopy(o), "state": "pending", "evidence_ids": []}
                for o in args["obligations"]
            ],
        )
    elif operation == "cancel":
        task["status"] = "cancelled"
    elif operation == "observe":
        for component, fact in args["observations"].items():
            if component not in COMPONENTS and not _read_key(component):
                raise ValueError("unsupported observation component")
            marker = {"origin": "query", "component": component, "epoch": task["epoch"],
                      "text": "query:" + component}
            if fact is None:
                if _read_key(component):
                    raise ValueError("failed READ is not observation evidence")
                if marker not in task["blockers"]:
                    task["blockers"].append(marker)
            else:
                if not valid_fact(fact, task, owner=owner, session_id=session_id) or _fact_key(fact) != component:
                    raise ValueError("invalid observation evidence")
                task["verified_facts"].append(deepcopy(fact))
                task["blockers"] = [b for b in task["blockers"] if not (
                    b["origin"] == "query" and b["component"] == component and b["epoch"] == task["epoch"])]
    elif operation == "checkpoint":
        validate_proposal(args)
        facts = {f["evidence_id"]: f for f in task["verified_facts"]}
        read_links = args.get("read_evidence", {})
        obligations = {o["id"]: o for o in task["obligations"]}
        for oid in read_links:
            if oid not in obligations or obligations[oid]["acceptance"]["kind"] != "observed_read":
                raise ValueError("read evidence must name a declared READ obligation")
        for evidence_id in {*args["evidence_ids"], *read_links.values()}:
            fact = facts.get(evidence_id)
            if not fact or not valid_fact(
                fact, task, owner=owner, session_id=session_id
            ):
                raise ValueError("invalid evidence scope")
        checkpoint_evidence = {
            fact["component"]: fact["evidence_id"]
            for fact in task["verified_facts"]
            if fact["verifier_version"] == "configuration-v1" and fact["evidence_id"] in args["evidence_ids"]
        }
        task["blockers"] = [
            b for b in task["blockers"] if b["origin"] == "query"
        ] + [{"origin": "reported", "text": text} for text in args["blockers"]]
    else:
        raise ValueError("invalid patch")
    if task["status"] != "cancelled":
        facts = {f["evidence_id"]: f for f in task["verified_facts"]}
        for obligation in task["obligations"]:
            for evidence_id in obligation["evidence_ids"]:
                fact = facts.get(evidence_id)
                if (
                    not fact
                    or not valid_fact(fact, task, owner=owner, session_id=session_id)
                    or not _matches(fact, obligation["acceptance"])
                ):
                    raise ValueError("invalid linked evidence")
            if evidence_id := read_links.get(obligation["id"]):
                if not _matches(facts[evidence_id], obligation["acceptance"]):
                    raise ValueError("read obligation requires actual READ evidence")
                obligation["evidence_ids"] = [evidence_id]
            # 先完整校验旧关联，再按登记顺序规范化；不能自动关联新观察。
            existing = set(obligation["evidence_ids"])
            obligation["evidence_ids"] = [eid for eid in facts if eid in existing][-1:]
            if obligation["acceptance"]["kind"] == "observed_component":
                evidence_id = checkpoint_evidence.get(obligation["acceptance"]["component"])
                if evidence_id:
                    obligation["evidence_ids"] = [evidence_id]
            obligation["state"] = (
                "blocked"
                if obligation["acceptance"]["kind"] not in {"observed_component", "observed_read"}
                else "verified"
                if obligation["evidence_ids"]
                else "pending"
            )
        task["status"] = (
            "blocked"
            if task["blockers"]
            or any(o["state"] == "blocked" for o in task["obligations"])
            else "complete"
            if all(o["state"] == "verified" for o in task["obligations"])
            else "partial"
        )
        # 状态快照不是审计日志：保留所有义务引用和每个组件最新登记的观察。
        linked = {eid for o in task["obligations"] for eid in o["evidence_ids"]}
        latest = {_fact_key(f): f["evidence_id"] for f in task["verified_facts"]}
        task["verified_facts"] = [
            f for f in task["verified_facts"]
            if f["evidence_id"] in linked or f["evidence_id"] == latest[_fact_key(f)]
        ]
    task["revision"] += 1
    _array(task["blockers"], 16)
    # 清单不是审计日志：按数量和字节统一淘汰最早的未关联回执，不能丢掉完成依据。
    linked = {eid for o in task["obligations"] for eid in o["evidence_ids"]}
    while (len(task["verified_facts"]) > 32
           or len(json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode()) > 16 * 1024):
        oldest = next((f for f in task["verified_facts"] if f["evidence_id"] not in linked), None)
        if oldest is None:
            raise ValueError("task capacity exceeded")
        task["verified_facts"].remove(oldest)
    return task


def task_projection(task: Mapping | None) -> dict | None:
    if task is None:
        return None
    result = deepcopy(dict(task))
    for fact in result["verified_facts"]:
        fact.pop("scope", None)
        fact.pop("call_id", None)
    result["acceptance_scope"] = "declared_obligations"
    result["remaining_obligation_ids"] = [
        o["id"] for o in result["obligations"] if o["state"] != "verified"
    ]
    linked = {eid for o in result["obligations"] for eid in o["evidence_ids"]}
    result["unassociated_evidence_ids"] = [
        f["evidence_id"]
        for f in result["verified_facts"]
        if f["evidence_id"] not in linked
    ]
    return result
