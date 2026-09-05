"""回退凭证的一次性状态；私有操作载荷由 Kernel 加密引用持久化。"""

from __future__ import annotations

from app import database as db


def create(key: str, owner_digest: str) -> None:
    with db.get_conn() as conn:
        # 引用最多有效一天；保留 30 天状态后可清理，不让凭证表无限膨胀。
        conn.execute(
            "DELETE FROM agent_compensations WHERE updated_at < datetime('now','localtime','-30 days') AND state!='executing'"
        )
        conn.execute(
            "INSERT OR IGNORE INTO agent_compensations(receipt_id,owner_digest,state,updated_at) VALUES(?,?,'available',?)",
            (key, owner_digest, db.now()),
        )


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
                "UPDATE agent_compensations SET state='executing',updated_at=? WHERE receipt_id=? AND owner_digest=? AND state='available'",
                (db.now(), key, owner_digest),
            ).rowcount
            == 1
        )


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
