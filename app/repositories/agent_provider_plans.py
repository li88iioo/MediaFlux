"""持久化、owner/session 隔离的 Agent Provider 写计划。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from app.agent.registry import AgentToolError
from app.modules.web_secret import get_web_secret

if TYPE_CHECKING:
    from types import ModuleType

_PLAN_RE = re.compile(r"^PP-[0-9A-F]{24}$")
_ALLOWED_RISKS = {"low_write", "write", "danger"}
_TERMINAL_STATUSES = {"succeeded", "failed", "stale", "outcome_unknown"}
_MAX_JSON_BYTES = 65_536
_MAX_SUMMARY_LENGTH = 240
_MAX_ERROR_CODE_LENGTH = 80
_MAX_HISTORY_PER_PRINCIPAL = 64


def _database() -> ModuleType:
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()


def _digest(value: str, *, domain: bytes, required: bool = True) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
    if len(normalized) > 512:
        raise AgentToolError("当前 Agent 身份无效", code="identity_required")
    secret = str(get_web_secret() or "")
    if not secret:
        raise AgentToolError("Agent 身份隔离密钥不可用", code="identity_required")
    return hmac.new(
        secret.encode("utf-8"),
        domain + b"\0" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _principal(owner: str, session_id: str) -> tuple[str, str]:
    return (
        _digest(owner, domain=b"mediaflux-agent-provider-plan-owner:v1"),
        _digest(
            session_id,
            domain=b"mediaflux-agent-provider-plan-session:v1",
            required=False,
        ),
    )


def _plan_id(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if not _PLAN_RE.fullmatch(normalized):
        raise AgentToolError("Provider 写计划编号无效", code="plan_not_found")
    return normalized


def _json_object(value: object, *, field: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{field}必须是对象")
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field}过大")
    return raw


def _decode_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _public_row(row: Any) -> dict[str, Any]:
    return {
        "plan_ref": str(row["plan_id"]),
        "provider": str(row["provider"]),
        "profile_ref": str(row["profile_ref"]),
        "operation": str(row["operation"]),
        "risk": str(row["risk"]),
        "status": str(row["status"]),
        "target_snapshot": _decode_object(row["target_snapshot_json"]),
        "result": _decode_object(row["result_json"]),
        "summary": str(row["summary"] or ""),
        "error_code": str(row["error_code"] or ""),
        "attempts": int(row["attempts"] or 0),
        "expires_at": float(row["expires_at"] or 0),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
    }


def _internal_row(row: Any) -> dict[str, Any]:
    result = _public_row(row)
    result["arguments"] = _decode_object(row["arguments_json"])
    result["context_fingerprint"] = str(row["context_fingerprint"] or "")
    return result


def _trim_history(conn: Any, *, owner_digest: str, session_digest: str) -> None:
    rows = conn.execute(
        "SELECT plan_id FROM agent_provider_plans "
        "WHERE owner_digest=? AND session_digest=? "
        "AND status IN ('succeeded','failed','stale','outcome_unknown') "
        "ORDER BY updated_at DESC,plan_id DESC LIMIT -1 OFFSET ?",
        (owner_digest, session_digest, _MAX_HISTORY_PER_PRINCIPAL),
    ).fetchall()
    if rows:
        conn.executemany(
            "DELETE FROM agent_provider_plans WHERE owner_digest=? "
            "AND session_digest=? AND plan_id=?",
            [
                (owner_digest, session_digest, str(row["plan_id"]))
                for row in rows
            ],
        )


def create_provider_plan(
    *,
    owner: str,
    session_id: str,
    provider: str,
    profile_ref: str,
    operation: str,
    risk: str,
    arguments: dict[str, Any],
    target_snapshot: dict[str, Any],
    context_fingerprint: str,
    summary: str = "",
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    owner_digest, session_digest = _principal(owner, session_id)
    normalized_risk = str(risk or "").strip().casefold()
    if normalized_risk not in _ALLOWED_RISKS:
        raise ValueError("Provider 写计划风险等级无效")
    plan_ref = f"PP-{secrets.token_hex(12).upper()}"
    stamp = now()
    expiry = time.time() + max(30, min(int(ttl_seconds), 900))
    with get_conn() as conn:
        conn.execute(
            "UPDATE agent_provider_plans SET status='stale',summary='写计划已过期',"
            "error_code='plan_expired',finished_at=COALESCE(finished_at,?),updated_at=? "
            "WHERE owner_digest=? AND session_digest=? AND status='prepared' "
            "AND expires_at<=?",
            (stamp, stamp, owner_digest, session_digest, time.time()),
        )
        conn.execute(
            "INSERT INTO agent_provider_plans("
            "plan_id,owner_digest,session_digest,provider,profile_ref,operation,risk,"
            "status,arguments_json,target_snapshot_json,context_fingerprint,result_json,"
            "summary,error_code,attempts,expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'prepared',?,?,?,'{}',?, '',0,?,?,?)",
            (
                plan_ref,
                owner_digest,
                session_digest,
                str(provider),
                str(profile_ref),
                str(operation),
                normalized_risk,
                _json_object(arguments, field="arguments"),
                _json_object(target_snapshot, field="target_snapshot"),
                str(context_fingerprint),
                " ".join(str(summary or "").split())[:_MAX_SUMMARY_LENGTH],
                expiry,
                stamp,
                stamp,
            ),
        )
        _trim_history(
            conn,
            owner_digest=owner_digest,
            session_digest=session_digest,
        )
        row = conn.execute(
            "SELECT * FROM agent_provider_plans WHERE plan_id=?",
            (plan_ref,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Provider 写计划创建失败")
    return _internal_row(row)


def get_provider_plan(
    *, owner: str, session_id: str, plan_ref: str
) -> dict[str, Any]:
    owner_digest, session_digest = _principal(owner, session_id)
    normalized = _plan_id(plan_ref)
    stamp = now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_provider_plans WHERE owner_digest=? "
            "AND session_digest=? AND plan_id=?",
            (owner_digest, session_digest, normalized),
        ).fetchone()
        if row is None:
            raise AgentToolError("未找到对应的 Provider 写计划", code="plan_not_found")
        if str(row["status"]) == "prepared" and float(row["expires_at"] or 0) <= time.time():
            conn.execute(
                "UPDATE agent_provider_plans SET status='stale',summary='写计划已过期',"
                "error_code='plan_expired',finished_at=?,updated_at=? WHERE plan_id=?",
                (stamp, stamp, normalized),
            )
            row = conn.execute(
                "SELECT * FROM agent_provider_plans WHERE plan_id=?", (normalized,)
            ).fetchone()
    return _internal_row(row)


def claim_provider_plan(
    *,
    owner: str,
    session_id: str,
    plan_ref: str,
    expected_context: str,
) -> dict[str, Any]:
    owner_digest, session_digest = _principal(owner, session_id)
    normalized = _plan_id(plan_ref)
    stamp = now()
    current_time = time.time()
    deferred_error: AgentToolError | None = None
    claimed = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_provider_plans WHERE owner_digest=? "
            "AND session_digest=? AND plan_id=?",
            (owner_digest, session_digest, normalized),
        ).fetchone()
        if row is None:
            raise AgentToolError("未找到对应的 Provider 写计划", code="plan_not_found")
        status = str(row["status"])
        if status != "prepared":
            if status == "succeeded":
                raise AgentToolError("该 Provider 写计划已经执行", code="already_executed")
            if status == "running":
                raise AgentToolError("该 Provider 写计划正在执行", code="already_running")
            if status == "outcome_unknown":
                raise AgentToolError("上次执行结果未知，请先人工核对", code="outcome_unknown")
            raise AgentToolError("Provider 写计划已失效，请重新预检", code="confirmation_stale")
        if float(row["expires_at"] or 0) <= current_time:
            conn.execute(
                "UPDATE agent_provider_plans SET status='stale',summary='写计划已过期',"
                "error_code='plan_expired',finished_at=?,updated_at=? WHERE plan_id=?",
                (stamp, stamp, normalized),
            )
            deferred_error = AgentToolError(
                "Provider 写计划已过期，请重新预检", code="confirmation_stale"
            )
        else:
            stored_context = str(row["context_fingerprint"] or "")
            if not stored_context or not hmac.compare_digest(
                stored_context, str(expected_context or "")
            ):
                conn.execute(
                    "UPDATE agent_provider_plans SET status='stale',"
                    "summary='确认上下文已变化',error_code='confirmation_stale',"
                    "finished_at=?,updated_at=? WHERE plan_id=?",
                    (stamp, stamp, normalized),
                )
                deferred_error = AgentToolError(
                    "相关状态已变化，请重新预检", code="confirmation_stale"
                )
            else:
                updated = conn.execute(
                    "UPDATE agent_provider_plans SET status='running',attempts=attempts+1,"
                    "started_at=?,updated_at=? WHERE owner_digest=? AND session_digest=? "
                    "AND plan_id=? AND status='prepared'",
                    (stamp, stamp, owner_digest, session_digest, normalized),
                ).rowcount
                if updated != 1:
                    raise AgentToolError(
                        "Provider 写计划已被处理", code="already_running"
                    )
                claimed = conn.execute(
                    "SELECT * FROM agent_provider_plans WHERE plan_id=?", (normalized,)
                ).fetchone()
    if deferred_error is not None:
        raise deferred_error
    if claimed is None:
        raise AgentToolError("Provider 写计划认领失败", code="outcome_unknown")
    return _internal_row(claimed)


def finish_provider_plan(
    *,
    plan_ref: str,
    status: str,
    result: dict[str, Any],
    summary: str,
    error_code: str = "",
) -> None:
    normalized = _plan_id(plan_ref)
    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in _TERMINAL_STATUSES:
        raise ValueError("Provider 写计划终态无效")
    stamp = now()
    safe_summary = " ".join(str(summary or "").split())[:_MAX_SUMMARY_LENGTH]
    safe_error = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_code or ""))[
        :_MAX_ERROR_CODE_LENGTH
    ]
    with get_conn() as conn:
        updated = conn.execute(
            "UPDATE agent_provider_plans SET status=?,result_json=?,summary=?,"
            "error_code=?,finished_at=?,updated_at=? WHERE plan_id=? AND status='running'",
            (
                normalized_status,
                _json_object(result, field="result"),
                safe_summary,
                safe_error,
                stamp,
                stamp,
                normalized,
            ),
        ).rowcount
        if updated != 1:
            raise AgentToolError("Provider 写计划状态已变化", code="outcome_unknown")
