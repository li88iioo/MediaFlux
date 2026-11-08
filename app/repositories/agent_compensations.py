"""补偿凭证账本：宿主持有私有快照，owner、时效和 CAS 约束一次性执行。"""

from __future__ import annotations

import json
import time
import uuid

from app import database as db


def create(key: str, owner_digest: str, *, payload: dict) -> str:
    # receipt_id 属于账本行，不在私有业务快照内复制一份。
    encoded = json.dumps({name: value for name, value in payload.items() if name != "receipt_id"},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    instant = time.time()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM agent_compensations WHERE updated_at < datetime('now','localtime','-30 days') AND state!='executing'"
        )
        previous = conn.execute(
            "SELECT receipt_id,state,expires_at FROM agent_compensations "
            "WHERE owner_digest=? AND payload_json=? ORDER BY rowid DESC LIMIT 1",
            (owner_digest, encoded),
        ).fetchone()
        # 复检不续期，也不为同一快照重新签发执行中/已消费/未知凭证。
        if previous and (previous["state"] != "available" or previous["expires_at"] > instant):
            return previous["receipt_id"]
        if conn.execute("SELECT 1 FROM agent_compensations WHERE receipt_id=?", (key,)).fetchone():
            key = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO agent_compensations(receipt_id,owner_digest,state,updated_at,payload_json,expires_at) "
            "VALUES(?,?,'available',?,?,?)", (key, owner_digest, db.now(), encoded, instant + 86400),
        )
    return key


def get(key: str, owner_digest: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT state,payload_json,expires_at FROM agent_compensations WHERE receipt_id=? AND owner_digest=?",
            (key, owner_digest),
        ).fetchone()
    if row is None:
        return None
    return {"state": row["state"], "expires_at": row["expires_at"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else None}


def state(key: str, owner_digest: str) -> str:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT state FROM agent_compensations WHERE receipt_id=? AND owner_digest=?",
            (key, owner_digest),
        ).fetchone()
    return row["state"] if row else "missing"


def claim(key: str, owner_digest: str) -> bool:
    with db.get_conn() as conn:
        return (
            conn.execute(
                "UPDATE agent_compensations SET state='executing',updated_at=? WHERE receipt_id=? AND owner_digest=? AND state='available' AND expires_at>? AND payload_json<>''",
                (db.now(), key, owner_digest, time.time()),
            ).rowcount
            == 1
        )


def release_unaccepted(key: str, owner_digest: str) -> bool:
    """仅供执行器明确未受理的操作归还占用，不复活任何已结算凭证。"""
    with db.get_conn() as conn:
        return conn.execute(
            "UPDATE agent_compensations SET state='available',updated_at=? "
            "WHERE receipt_id=? AND owner_digest=? AND state='executing'",
            (db.now(), key, owner_digest),
        ).rowcount == 1


def finish(key: str, owner_digest: str, *, completed: bool) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_compensations SET state=?,updated_at=? WHERE receipt_id=? AND owner_digest=? AND state='executing'",
            (
                "completed" if completed else "outcome_unknown",
                db.now(),
                key,
                owner_digest,
            ),
        )
