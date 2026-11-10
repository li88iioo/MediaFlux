"""已退役 Provider 的存量计划读取、隐私清理和中断恢复；不再创建或执行计划。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from app.agent.errors import AgentToolError
from app.modules.web_secret import get_web_secret
from app import database as db

_PLAN_RE = re.compile(r"^PP-[0-9A-F]{24}$")


def _digest(value: str, *, domain: bytes, required: bool = True) -> str:
    normalized = (value or "").strip()
    if required and not normalized:
        raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
    if len(normalized) > 512:
        raise AgentToolError("当前 Agent 身份无效", code="identity_required")
    secret = (get_web_secret() or "")
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


def _decode_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
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


def _invalidate_provider_plans(
    *,
    owner_digest: str,
    session_digest: str | None = None,
) -> dict[str, int]:
    stamp = db.now()
    scope = "owner_digest=?"
    scope_values: tuple[str, ...] = (owner_digest,)
    if session_digest is not None:
        scope += " AND session_digest=?"
        scope_values += (session_digest,)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='agent_provider_plans'"
        ).fetchone()
        if table_exists is None:
            # 独立工具测试、首次迁移前或降级数据库中没有 Provider 状态，
            # 等价于当前 owner 没有可撤销计划，不能因此阻断整段会话重置。
            return {"scrubbed_running": 0, "deleted": 0}
        running = conn.execute(
            "UPDATE agent_provider_plans SET "
            "owner_digest=lower(hex(randomblob(32))),"
            "session_digest=lower(hex(randomblob(32))),provider='',profile_ref='',"
            "operation='',risk='write',arguments_json='{}',target_snapshot_json='{}',"
            "result_json='{}',context_fingerprint='',summary='',"
            "error_code='session_reset_pending',updated_at=? "
            f"WHERE {scope} AND status='running'",
            (stamp, *scope_values),
        )
        removed = conn.execute(
            f"DELETE FROM agent_provider_plans WHERE {scope} AND status<>'running'",
            scope_values,
        )
    scrubbed = max(0, (running.rowcount or 0))
    deleted = max(0, (removed.rowcount or 0))
    return {"scrubbed_running": scrubbed, "deleted": deleted}


def invalidate_provider_plans_for_owner(*, owner: str) -> dict[str, int]:
    """撤销 owner 的短期 Provider 状态；运行中计划仅脱敏后等待执行者收尾。"""
    owner_digest = _digest(owner, domain=b"mediaflux-agent-provider-plan-owner:v1")
    return _invalidate_provider_plans(owner_digest=owner_digest)


def invalidate_provider_plans_for_session(
    *,
    owner: str,
    session_id: str,
) -> dict[str, int]:
    """仅撤销指定 owner/session 的 Provider 短期写计划。"""
    owner_digest, session_digest = _principal(owner, session_id)
    return _invalidate_provider_plans(
        owner_digest=owner_digest,
        session_digest=session_digest,
    )


def get_provider_plan(*, owner: str, session_id: str, plan_ref: str) -> dict[str, Any]:
    owner_digest, session_digest = _principal(owner, session_id)
    normalized = _plan_id(plan_ref)
    stamp = db.now()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_provider_plans WHERE owner_digest=? "
            "AND session_digest=? AND plan_id=?",
            (owner_digest, session_digest, normalized),
        ).fetchone()
        if row is None:
            raise AgentToolError("未找到对应的 Provider 写计划", code="plan_not_found")
        if str(row["status"]) == "prepared":
            conn.execute(
                "UPDATE agent_provider_plans SET status='stale',summary='旧 Provider 网关已退役，请通过原生入口重新预检',"
                "error_code='provider_retired',finished_at=?,updated_at=? WHERE plan_id=?",
                (stamp, stamp, normalized),
            )
            row = conn.execute(
                "SELECT * FROM agent_provider_plans WHERE plan_id=?", (normalized,)
            ).fetchone()
    return _internal_row(row)


def _recover_after_restart(conn, timestamp: str) -> None:
    """只在无存活旧 writer 时恢复 running；prepared 统一失效，保留原始快照和回执。"""
    conn.execute(
        "UPDATE agent_action_history SET status='outcome_unknown',ok=0,"
        "summary='Agent 受确认动作：结果待核对',"
        "error_code='execution_interrupted',finished_at=?,elapsed_ms=0 "
        "WHERE status='executing' AND tool_name<>'provider.change.execute'",
        (timestamp,),
    )
    from app.modules.process_lock import CrossProcessLock

    recovery_lock = CrossProcessLock(
        "agent-provider-write", directory=db.resolve_db_path().parent
    )
    if recovery_lock.acquire(blocking=False):
        try:
            db.recover_interrupted_provider_plans(conn, timestamp=timestamp)
        finally:
            recovery_lock.release()

    conn.execute(
        "UPDATE agent_provider_plans SET status='stale',"
        "summary='旧 Provider 网关已退役，请通过原生入口重新预检',error_code='provider_retired',"
        "finished_at=COALESCE(finished_at,?),updated_at=? "
        "WHERE status='prepared'",
        (timestamp, timestamp),
    )
