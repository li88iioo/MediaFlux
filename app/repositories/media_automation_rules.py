"""轻量主动通知规则；只保存调度状态，投递仍由统一 Telegram outbox 负责。"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, overload

from app import database as db
from app.modules.process_lock import CrossProcessLock

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


def _finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("主动规则设置含非有限数值")
    return number


def _decode_settings(value: object) -> dict[str, Any] | None:
    """读取和领取共用有界解码；坏历史值可查看修复，但不能成为可执行默认值。"""
    if not isinstance(value, str) or len(value) > 16_384:
        return None
    try:
        if len(value.encode("utf-8")) > 16_384:
            return None
        settings = json.loads(
            value, parse_float=_finite_json_number, parse_constant=_finite_json_number
        )
    except (ValueError, TypeError, RecursionError):
        return None
    return settings if isinstance(settings, dict) else None


@overload
def _row(row: None) -> None: ...


@overload
def _row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]: ...


def _row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    settings = _decode_settings(item.pop("settings_json"))
    item["settings"] = settings if settings is not None else {}
    if settings is None:
        item["settings_error"] = "主动规则设置已损坏，请编辑或删除该规则"
    item["enabled"] = bool(item["enabled"])
    return item


def list_rules(owner_digest: str, *, kind: str = "") -> list[dict[str, Any]]:
    if kind and kind not in KINDS:
        raise ValueError("主动规则类型无效")
    where = "owner_digest=?"
    params = [owner_digest]
    if kind:
        where += " AND kind=?"
        params.append(kind)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM media_automation_rules WHERE {where} ORDER BY created_at,id",
            params,
        ).fetchall()
    return [_row(row) for row in rows]


def _find_activity_follow_rule(conn, owner_digest: str, target: dict[str, Any]):
    """同一身份查找用于预检和事务内去重，不受展示窗口限制。"""

    def matches(value):
        settings = _decode_settings(value)
        return settings is not None and settings.get("target") == target

    conn.create_function(
        "mediaflux_rule_target_matches", 1, matches, deterministic=True
    )
    return _row(
        conn.execute(
            "SELECT * FROM media_automation_rules WHERE owner_digest=? "
            "AND kind='activity_follow' AND mediaflux_rule_target_matches(settings_json)=1 "
            "ORDER BY created_at,id LIMIT 1",
            (owner_digest,),
        ).fetchone()
    )


def find_activity_follow_rule(
    owner_digest: str, target: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        raise ValueError("活动跟踪身份无效")
    with db.get_conn() as conn:
        return _find_activity_follow_rule(conn, owner_digest, target)


def _read_rule(conn, owner_digest: str, rule_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM media_automation_rules WHERE owner_digest=? AND id=?",
            (owner_digest, rule_id),
        ).fetchone()
    )


def get_rule(owner_digest: str, rule_id: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        return _read_rule(conn, owner_digest, rule_id)


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
        settings,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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
            if (
                kind == "activity_follow"
                and isinstance(settings.get("target"), dict)
                and _find_activity_follow_rule(conn, owner_digest, settings["target"])
                is not None
            ):
                # 两个确认可能都预检到不存在；写锁内再次核对，后来的确认不能新建副本。
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
        committed = _read_rule(conn, owner_digest, rule_id)
    return committed


def delete_rule(owner_digest: str, rule_id: str, *, expected_revision: int) -> bool:
    with publication_guard(), db.get_conn() as conn:
        return (
            conn.execute(
                "DELETE FROM media_automation_rules WHERE id=? AND owner_digest=? AND revision=?",
                (rule_id, owner_digest, expected_revision),
            ).rowcount
            == 1
        )


def _schedule_time_key(value: object) -> str | None:
    """ISO 历史格式按同一瞬间比较；保留微秒，坏日期不执行也不覆写。"""
    try:
        instant = datetime.fromisoformat(str(value))
        return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def claim_due_rules(
    now: datetime | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    clock = now or datetime.now().astimezone()
    stamp = _schedule_time_key(clock.isoformat())
    if stamp is None:
        raise ValueError("主动规则领取时间无效")
    until = (clock + timedelta(minutes=5)).isoformat()
    claimed = []
    with publication_guard(), db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.create_function(
            "mediaflux_rule_time", 1, _schedule_time_key, deterministic=True
        )
        conn.create_function(
            "mediaflux_rule_settings_valid",
            1,
            lambda value: _decode_settings(value) is not None,
            deterministic=True,
        )
        rows = conn.execute(
            "SELECT * FROM media_automation_rules WHERE enabled=1 AND mediaflux_rule_time(next_run_at)<=? "
            "AND (lease_until='' OR mediaflux_rule_time(lease_until)<=?) "
            "AND mediaflux_rule_settings_valid(settings_json)=1 "
            "ORDER BY mediaflux_rule_time(next_run_at),id LIMIT ?",
            (stamp, stamp, max(1, min(limit, 100))),
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
