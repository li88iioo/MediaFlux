"""owner 私有 Skills 修订；复用主 SQLite 事务，不依赖 Agent 运行时。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from typing import Any

from app import database as db

_METADATA = "revision,name,description,restored_from,created_at"


class SkillRevisionConflict(ValueError):
    """调用者读取的版本已经过期，需重新读取后再提交。"""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"Skill revision changed; current_revision={current_revision}")


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError("Skill name 必须为 1–64 位小写字母、数字、下划线或连字符")
    return value


def _revision(value: int, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Skill revision 必须是 >= {minimum} 的整数")
    return value


def list_skills(*, owner: str, name: str | None = None) -> list[dict[str, Any]]:
    """默认列每个 Skill 的最新版元数据；指定 name 列其修订历史，均不含正文。"""
    owner_digest = db.native_agent_owner_digest(owner)
    with db.get_conn() as conn:
        if name is not None:
            rows = conn.execute(
                f"SELECT {_METADATA} FROM agent_skill_revisions "
                "WHERE owner_digest=? AND name=? ORDER BY revision DESC",
                (owner_digest, _name(name)),
            )
        else:
            rows = conn.execute(
                f"SELECT {_METADATA} FROM agent_skill_revisions WHERE revision IN ("
                "SELECT MAX(revision) FROM agent_skill_revisions "
                "WHERE owner_digest=? GROUP BY name) ORDER BY name",
                (owner_digest,),
            )
        return [dict(row) for row in rows]


def _get(conn: sqlite3.Connection, owner_digest: str, name: str,
         revision: int | None) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_METADATA},content FROM agent_skill_revisions "
        "WHERE owner_digest=? AND name=?" + (" AND revision=?" if revision is not None else "")
        + " ORDER BY revision DESC LIMIT 1",
        (owner_digest, name) + ((revision,) if revision is not None else ()),
    ).fetchone()
    return dict(row) if row is not None else None


def get_skill(*, owner: str, name: str, revision: int | None = None) -> dict[str, Any] | None:
    """读取完整 SKILL.md；未指定 revision 时取最新版，越 owner/名称查询返回 None。"""
    if revision is not None:
        _revision(revision)
    with db.get_conn() as conn:
        return _get(conn, db.native_agent_owner_digest(owner), _name(name), revision)


def _append(conn: sqlite3.Connection, *, owner_digest: str, name: str,
            description: str, content: str, expected_revision: int,
            publication_check: Callable[[sqlite3.Connection], None] | None,
            restored_from: int | None = None) -> dict[str, Any]:
    _revision(expected_revision, minimum=0)
    if publication_check is not None:
        publication_check(conn)
    current = conn.execute(
        "SELECT COALESCE(MAX(revision),0) FROM agent_skill_revisions "
        "WHERE owner_digest=? AND name=?", (owner_digest, name),
    ).fetchone()[0]
    if current != expected_revision:
        raise SkillRevisionConflict(current)
    created_at = db.now()
    cursor = conn.execute(
        "INSERT INTO agent_skill_revisions("
        "owner_digest,name,description,content,restored_from,created_at) VALUES(?,?,?,?,?,?)",
        (owner_digest, name, description, content, restored_from, created_at),
    )
    return {"revision": cursor.lastrowid, "name": name, "description": description,
            "content": content, "restored_from": restored_from, "created_at": created_at}


def save_skill(*, owner: str, name: str, description: str, content: str,
               expected_revision: int,
               publication_check: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, Any]:
    """追加完整修订；新建 expected_revision=0，其余传当前 revision。

    publication_check(conn) 在同一写事务内执行，宿主拒绝发布时须抛异常。
    revision 是数据库分配的全局递增 ID，不是单个 Skill 的连续序号。
    """
    owner_digest, name = db.native_agent_owner_digest(owner), _name(name)
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skill description 不能为空")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Skill content 必须是非空的 SKILL.md 正文")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _append(conn, owner_digest=owner_digest, name=name, description=description,
                       content=content, expected_revision=expected_revision,
                       publication_check=publication_check)


def restore_skill(*, owner: str, name: str, revision: int, expected_revision: int,
                  publication_check: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, Any]:
    """以历史完整内容追加新版；不删除中间历史，也不把当前指针退回旧 revision。"""
    owner_digest, name = db.native_agent_owner_digest(owner), _name(name)
    _revision(revision)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = _get(conn, owner_digest, name, revision)
        if source is None:
            raise LookupError("Skill revision 不存在")
        return _append(conn, owner_digest=owner_digest, name=name,
                       description=source["description"], content=source["content"],
                       expected_revision=expected_revision, publication_check=publication_check,
                       restored_from=revision)
