"""轻量主动通知规则；只保存调度状态，投递仍由统一 Telegram outbox 负责。"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from app import database as db
from app.modules.process_lock import CrossProcessLock

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_automation_rules (
    id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_automation_rules_due
ON media_automation_rules(enabled,next_run_at,lease_until);
CREATE INDEX IF NOT EXISTS idx_media_automation_rules_owner
ON media_automation_rules(owner_digest,id);
"""
KINDS = frozenset({"daily_summary", "activity_follow"})
_PUBLICATION_LOCK = CrossProcessLock("media-automation-publication")


@contextmanager
def publication_guard():
    """短临界区覆盖取消/编辑与 outbox 接纳；不包围网络读取或模型调用。"""
    if not _PUBLICATION_LOCK.acquire():
        raise RuntimeError("主动规则正在被其它操作更新")
    try:
        yield
    finally:
        _PUBLICATION_LOCK.release()


def ensure_schema() -> None:
    with db.get_conn() as conn:
        conn.executescript(SCHEMA)


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["settings"] = json.loads(item.pop("settings_json"))
    item["enabled"] = bool(item["enabled"])
    return item


def list_rules(owner_digest: str) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM media_automation_rules WHERE owner_digest=? ORDER BY created_at,id LIMIT 100",
            (owner_digest,),
        ).fetchall()
    return [_row(row) for row in rows]


def get_rule(owner_digest: str, rule_id: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM media_automation_rules WHERE owner_digest=? AND id=?",
                (owner_digest, rule_id),
            ).fetchone()
        )


def save_rule(
    owner_digest: str,
    *,
    kind: str,
    settings: dict[str, Any],
    enabled: bool,
    next_run_at: str,
    rule_id: str = "",
    expected_revision: int = 0,
) -> dict[str, Any] | None:
    """CAS 保存；返回 None 表示已被别的确认或 Web 操作修改。"""
    if not owner_digest or kind not in KINDS or not isinstance(settings, dict):
        raise ValueError("主动规则身份或类型无效")
    if not isinstance(enabled, bool):
        raise TypeError("enabled 必须是布尔值")
    encoded = json.dumps(
        settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode()) > 16_384:
        raise ValueError("主动规则设置过大")
    datetime.fromisoformat(next_run_at)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with publication_guard(), db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not rule_id:
            if expected_revision:
                return None
            rule_id = "auto_" + secrets.token_urlsafe(18)
            conn.execute(
                "INSERT INTO media_automation_rules(id,owner_digest,kind,settings_json,enabled,"
                "next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    rule_id,
                    owner_digest,
                    kind,
                    encoded,
                    int(enabled),
                    next_run_at,
                    now,
                    now,
                ),
            )
        else:
            changed = conn.execute(
                "UPDATE media_automation_rules SET settings_json=?,enabled=?,revision=revision+1,"
                "next_run_at=?,lease_token='',lease_until='',updated_at=? "
                "WHERE id=? AND owner_digest=? AND kind=? AND revision=?",
                (
                    encoded,
                    int(enabled),
                    next_run_at,
                    now,
                    rule_id,
                    owner_digest,
                    kind,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                return None
    return get_rule(owner_digest, rule_id)


def delete_rule(owner_digest: str, rule_id: str, *, expected_revision: int) -> bool:
    with publication_guard(), db.get_conn() as conn:
        return (
            conn.execute(
                "DELETE FROM media_automation_rules WHERE id=? AND owner_digest=? AND revision=?",
                (rule_id, owner_digest, expected_revision),
            ).rowcount
            == 1
        )


def claim_due_rules(
    now: datetime | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    clock = now or datetime.now().astimezone()
    stamp = clock.isoformat(timespec="seconds")
    until = (clock + timedelta(minutes=5)).isoformat(timespec="seconds")
    claimed = []
    with publication_guard(), db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM media_automation_rules WHERE enabled=1 AND next_run_at<=? "
            "AND (lease_until='' OR lease_until<=?) ORDER BY next_run_at LIMIT ?",
            (stamp, stamp, max(1, min(int(limit), 100))),
        ).fetchall()
        for row in rows:
            token = secrets.token_urlsafe(18)
            conn.execute(
                "UPDATE media_automation_rules SET lease_token=?,lease_until=? WHERE id=?",
                (token, until, row["id"]),
            )
            item = _row(row)
            item["lease_token"] = token
            claimed.append(item)
    return claimed


def finish_rule(
    rule_id: str, lease_token: str, next_run_at: str, *, disable: bool = False
) -> bool:
    """只允许当前领取者推进调度；取消/编辑会使在途领取者失去发布权。"""
    with db.get_conn() as conn:
        return (
            conn.execute(
                "UPDATE media_automation_rules SET next_run_at=?,enabled=?,lease_token='',lease_until='' "
                "WHERE id=? AND lease_token=? AND enabled=1",
                (next_run_at, int(not disable), rule_id, lease_token),
            ).rowcount
            == 1
        )


def owns_lease(rule_id: str, lease_token: str) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM media_automation_rules WHERE id=? AND lease_token=? AND enabled=1",
            (rule_id, lease_token),
        ).fetchone()
    return row is not None
