"""识别规则的精确单条启停动作。

该模块只允许按公开规则类型和正整数 ID 切换 enabled 状态。它不会返回规则名、
匹配表达式、TMDB ID、别名、证据或其它业务内容，也不支持批量修改。
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Mapping

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.modules import recognition_knowledge, recognition_preprocess_rules

_RULE_TYPES = {
    "preprocess_rule": "识别预处理规则",
    "tmdb_regex_rule": "TMDB 正则规则",
    "knowledge_entry": "识别知识条目",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_rule_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AgentToolError("rule_id 必须是正整数")
    if value > 2_147_483_647:
        raise AgentToolError("rule_id 超出允许范围")
    return value


def recognition_rule_enabled_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"rule_type", "rule_id", "enabled"}:
        raise AgentToolError(
            "recognition.set_rule_enabled 只接受 rule_type、rule_id 和 enabled 参数"
        )
    rule_type = str(arguments.get("rule_type") or "").strip()
    if rule_type not in _RULE_TYPES:
        raise AgentToolError("rule_type 不受支持")
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {
        "rule_type": rule_type,
        "rule_id": _strict_rule_id(arguments.get("rule_id")),
        "enabled": enabled,
    }


def _ensure_storage(rule_type: str) -> None:
    # 两个 ensure 函数内部可能开启事务，必须在确认事务之前调用。
    if rule_type == "preprocess_rule":
        recognition_preprocess_rules.ensure_builtin_rules()
    elif rule_type == "knowledge_entry":
        recognition_knowledge.ensure_seed_knowledge()


def _fetch_row(conn: Any, rule_type: str, rule_id: int) -> Any | None:
    if rule_type == "preprocess_rule":
        return conn.execute(
            "SELECT * FROM recognition_preprocess_rules WHERE id=?", (rule_id,)
        ).fetchone()
    if rule_type == "tmdb_regex_rule":
        return conn.execute(
            "SELECT * FROM tmdb_regex_rules WHERE id=?", (rule_id,)
        ).fetchone()
    if rule_type == "knowledge_entry":
        return conn.execute(
            "SELECT * FROM recognition_knowledge WHERE id=?", (rule_id,)
        ).fetchone()
    raise AgentToolError("rule_type 不受支持")


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _digest_fields(row: Any, fields: tuple[str, ...]) -> str:
    payload = {field: _row_value(row, field) for field in fields}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _snapshot(row: Any, *, rule_type: str, rule_id: int) -> dict[str, Any]:
    if rule_type == "preprocess_rule":
        identity_fields = (
            "name", "matcher_type", "pattern", "scope", "action", "replacement",
            "numeric_value", "priority", "builtin_key", "created_at",
        )
    elif rule_type == "tmdb_regex_rule":
        identity_fields = (
            "name", "pattern", "match_target", "tmdb_id", "media_type",
            "season_override", "priority", "created_at",
        )
    else:
        # hit/success/conflict 计数和 updated_at 会被正常识别流程更新，不纳入快照，
        # 避免用户确认期间因无关统计变化产生虚假冲突。
        identity_fields = (
            "knowledge_key", "knowledge_type", "canonical_value", "normalized_value",
            "aliases_json", "source", "confidence", "user_modified", "seed_revision",
            "evidence_json", "created_at",
        )
    return {
        "rule_type": rule_type,
        "rule_id": rule_id,
        "enabled": not bool(int(_row_value(row, "disabled", 0) or 0)),
        "identity_sha256": _digest_fields(row, identity_fields),
    }


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _capture(rule_type: str, rule_id: int) -> dict[str, Any]:
    _ensure_storage(rule_type)
    with db.get_conn() as conn:
        row = _fetch_row(conn, rule_type, rule_id)
    if row is None:
        raise AgentToolError("指定的识别规则不存在", code="precondition_failed")
    return _snapshot(row, rule_type=rule_type, rule_id=rule_id)


def prepare_set_recognition_rule_enabled(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    normalized = recognition_rule_enabled_arguments(arguments)
    rule_type = str(normalized["rule_type"])
    rule_id = int(normalized["rule_id"])
    requested = bool(normalized["enabled"])
    snapshot = _capture(rule_type, rule_id)
    current = bool(snapshot["enabled"])
    if current == requested:
        raise AgentToolError("该识别规则已经处于目标状态", code="precondition_failed")

    label = _RULE_TYPES[rule_type]
    operation = "启用" if requested else "停用"
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{operation}{label} #{rule_id}",
        data={
            "operation": "enable" if requested else "disable",
            "rule_type": rule_type,
            "rule_id": rule_id,
            "current_enabled": current,
            "requested_enabled": requested,
            "affected": 1,
            "effects": [
                f"只会{operation}这一条{label}。",
                "不会修改规则内容、匹配表达式、TMDB 映射、别名或优先级。",
                "不会批量影响其它识别规则。",
            ],
        },
        evidence=[Evidence(
            source="recognition_configuration",
            description="仅核对目标规则的类型、编号、启停状态和私有快照；未返回规则内容。",
            collected_at=_now(),
        )],
        suggestions=["该操作可通过相反的启停命令恢复。"],
    )
    return preview, _fingerprint(snapshot)


def _update_enabled(conn: Any, *, rule_type: str, rule_id: int, enabled: bool) -> None:
    disabled = 0 if enabled else 1
    timestamp = db.now()
    if rule_type == "preprocess_rule":
        cursor = conn.execute(
            "UPDATE recognition_preprocess_rules SET disabled=?,updated_at=? WHERE id=?",
            (disabled, timestamp, rule_id),
        )
    elif rule_type == "tmdb_regex_rule":
        cursor = conn.execute(
            "UPDATE tmdb_regex_rules SET disabled=?,updated_at=? WHERE id=?",
            (disabled, timestamp, rule_id),
        )
    elif rule_type == "knowledge_entry":
        cursor = conn.execute(
            "UPDATE recognition_knowledge "
            "SET disabled=?,user_modified=1,updated_at=? WHERE id=?",
            (disabled, timestamp, rule_id),
        )
    else:  # pragma: no cover - validator guarantees the enum
        raise AgentToolError("rule_type 不受支持")
    if cursor.rowcount != 1:
        raise AgentToolError("指定的识别规则不存在", code="precondition_failed")


def _invalidate_runtime(rule_type: str) -> None:
    if rule_type == "preprocess_rule":
        recognition_preprocess_rules.invalidate_active_cache()
    elif rule_type == "knowledge_entry":
        recognition_knowledge.invalidate_active_cache()


def set_recognition_rule_enabled_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = recognition_rule_enabled_arguments(arguments)
    rule_type = str(normalized["rule_type"])
    rule_id = int(normalized["rule_id"])
    requested = bool(normalized["enabled"])
    _ensure_storage(rule_type)

    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch_row(conn, rule_type, rule_id)
        if row is None:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="识别规则已不存在，请重新检查",
                error="确认快照已失效。",
            )
        snapshot = _snapshot(row, rule_type=rule_type, rule_id=rule_id)
        if not secrets.compare_digest(
            _fingerprint(snapshot), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="识别规则已被其他操作修改，请重新预检",
                error="确认快照已失效。",
            )
        if bool(snapshot["enabled"]) == requested:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="识别规则状态已经变化，请重新预检",
                error="确认快照已失效。",
            )
        _update_enabled(
            conn,
            rule_type=rule_type,
            rule_id=rule_id,
            enabled=requested,
        )

    _invalidate_runtime(rule_type)
    label = _RULE_TYPES[rule_type]
    operation = "启用" if requested else "停用"
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"{label} #{rule_id} 已{operation}",
        data={
            "operation": "enable" if requested else "disable",
            "rule_type": rule_type,
            "rule_id": rule_id,
            "enabled": requested,
            "affected": 1,
        },
        evidence=[Evidence(
            source="recognition_configuration",
            description="只更新了目标规则的启停状态，并刷新当前进程相关缓存。",
            collected_at=_now(),
        )],
        suggestions=["如需恢复，可对同一规则编号执行相反的启停操作。"],
    )
