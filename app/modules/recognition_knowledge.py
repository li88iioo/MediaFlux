"""本地识别知识库：管理发布组与尾部制作组的可增长精确词条。"""
from __future__ import annotations

import json
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any

from app.database import get_conn, now, resolve_db_path
from app.logger import get_logger

logger = get_logger(__name__)

_ALLOWED_TYPES = {"release_group", "release_suffix"}
_ALLOWED_SOURCES = {"builtin", "learned", "user"}
_MAX_VALUE_LENGTH = 160
_MAX_ALIASES = 24
_SEED_PATH = Path(__file__).resolve().parents[1] / "resources" / "recognition_knowledge.json"

_seed_lock = threading.RLock()
_cache_lock = threading.RLock()
_seed_paths: set[str] = set()
_active_cache: tuple[str, dict[str, dict[str, dict[str, Any]]]] | None = None
_builtin_cache: dict[str, dict[str, dict[str, Any]]] | None = None


def _db_key() -> str:
    return str(resolve_db_path().resolve())


def normalize_value(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = " ".join(text.split())
    return text.casefold()


def _decode_json(value: object, fallback: Any) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded


def _row_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    aliases = _decode_json(item.pop("aliases_json", "[]"), [])
    evidence = _decode_json(item.pop("evidence_json", "{}"), {})
    item["aliases"] = [str(value) for value in aliases if str(value or "").strip()]
    item["evidence"] = evidence if isinstance(evidence, dict) else {}
    item["disabled"] = bool(item.get("disabled"))
    item["user_modified"] = bool(item.get("user_modified"))
    item["confidence"] = float(item.get("confidence") or 0)
    return item


def _parse_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError("停用状态必须是布尔值")


def _validate_values(data: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = existing or {}
    knowledge_type = str(data.get("knowledge_type", current.get("knowledge_type", "release_group"))).strip()
    if knowledge_type not in _ALLOWED_TYPES:
        raise ValueError("知识类型无效")
    canonical = unicodedata.normalize(
        "NFKC", str(data.get("canonical_value", current.get("canonical_value", "")))
    ).strip()
    if not canonical or len(canonical) > _MAX_VALUE_LENGTH or "\x00" in canonical:
        raise ValueError("标准名称不能为空且长度不能超过 160")
    aliases_raw = data.get("aliases", current.get("aliases", []))
    if isinstance(aliases_raw, str):
        aliases_raw = [part.strip() for part in aliases_raw.replace("，", ",").split(",")]
    if not isinstance(aliases_raw, list):
        raise ValueError("别名必须是数组或逗号分隔文本")
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in [canonical, *aliases_raw]:
        value = unicodedata.normalize("NFKC", str(raw or "")).strip()
        normalized = normalize_value(value)
        if not value or not normalized or normalized in seen:
            continue
        if len(value) > _MAX_VALUE_LENGTH or "\x00" in value:
            raise ValueError("单个别名长度不能超过 160")
        aliases.append(value)
        seen.add(normalized)
        if len(aliases) > _MAX_ALIASES:
            raise ValueError("别名数量不能超过 24")
    source = str(data.get("source", current.get("source", "user"))).strip()
    if source not in _ALLOWED_SOURCES:
        raise ValueError("知识来源无效")
    try:
        confidence = float(data.get("confidence", current.get("confidence", 1.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("置信度必须是 0 到 1 的数字") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("置信度必须在 0 到 1 之间")
    disabled = _parse_bool(
        data.get("disabled", current.get("disabled", False)),
        default=bool(current.get("disabled", False)),
    )
    key = str(data.get("knowledge_key", current.get("knowledge_key", ""))).strip()
    if not key:
        key = f"{knowledge_type}:{normalize_value(canonical)}"
    if len(key) > 220 or "\x00" in key:
        raise ValueError("知识键无效")
    evidence = data.get("evidence", current.get("evidence", {}))
    if not isinstance(evidence, dict):
        raise ValueError("识别证据必须是对象")
    return {
        "knowledge_key": key,
        "knowledge_type": knowledge_type,
        "canonical_value": canonical,
        "normalized_value": normalize_value(canonical),
        "aliases": aliases,
        "source": source,
        "confidence": round(confidence, 3),
        "disabled": disabled,
        "evidence": evidence,
    }


def _load_seed_payload() -> tuple[int, list[dict[str, Any]]] | None:
    try:
        payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("识别知识种子读取失败 type=%s", type(exc).__name__)
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("识别知识种子 entries 无效")
    return int(payload.get("revision") or 0), [entry for entry in entries if isinstance(entry, dict)]


def _builtin_seed_index() -> dict[str, dict[str, dict[str, Any]]]:
    """返回不依赖数据库的内置只读词库，供纯文件名解析阶段使用。"""
    global _builtin_cache
    with _cache_lock:
        if _builtin_cache is not None:
            return _builtin_cache
        index: dict[str, dict[str, dict[str, Any]]] = {
            knowledge_type: {} for knowledge_type in _ALLOWED_TYPES
        }
        payload = _load_seed_payload()
        if payload is None:
            _builtin_cache = index
            return index
        revision, entries = payload
        for raw in entries:
            item = _validate_values({**raw, "source": "builtin"})
            snapshot = {
                **item,
                "id": 0,
                "hit_count": 0,
                "success_count": 0,
                "conflict_count": 0,
                "user_modified": False,
                "seed_revision": revision,
            }
            for alias in item["aliases"]:
                normalized = normalize_value(alias)
                if normalized:
                    index[item["knowledge_type"]][normalized] = snapshot
        _builtin_cache = index
        return index


def _invalidate_cache() -> None:
    global _active_cache
    with _cache_lock:
        _active_cache = None


def invalidate_active_cache() -> None:
    """清除当前进程的启用规则缓存，供受控配置写入后调用。"""
    _invalidate_cache()


def ensure_seed_knowledge() -> None:
    key = _db_key()
    with _seed_lock:
        if key in _seed_paths:
            return
        payload = _load_seed_payload()
        if payload is None:
            _seed_paths.add(key)
            return
        revision, entries = payload
        timestamp = now()
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                normalized = _validate_values({**raw, "source": "builtin"})
                conn.execute(
                    """
                    INSERT INTO recognition_knowledge(
                        knowledge_key,knowledge_type,canonical_value,normalized_value,
                        aliases_json,source,confidence,disabled,user_modified,seed_revision,
                        evidence_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'builtin',1.0,0,0,?,'{}',?,?)
                    ON CONFLICT(knowledge_key) DO UPDATE SET
                        knowledge_type=excluded.knowledge_type,
                        canonical_value=excluded.canonical_value,
                        normalized_value=excluded.normalized_value,
                        aliases_json=excluded.aliases_json,
                        seed_revision=excluded.seed_revision,
                        updated_at=excluded.updated_at
                    WHERE recognition_knowledge.source='builtin'
                      AND recognition_knowledge.user_modified=0
                      AND recognition_knowledge.seed_revision < excluded.seed_revision
                    """,
                    (
                        normalized["knowledge_key"], normalized["knowledge_type"],
                        normalized["canonical_value"], normalized["normalized_value"],
                        json.dumps(normalized["aliases"], ensure_ascii=False), revision,
                        timestamp, timestamp,
                    ),
                )
        _seed_paths.add(key)
        _invalidate_cache()


def _is_missing_schema_error(exc: sqlite3.OperationalError) -> bool:
    return "no such table: recognition_knowledge" in str(exc).casefold()


def _active_index() -> dict[str, dict[str, dict[str, Any]]]:
    global _active_cache
    key = _db_key()
    try:
        ensure_seed_knowledge()
        with _cache_lock:
            if _active_cache and _active_cache[0] == key:
                return _active_cache[1]
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM recognition_knowledge WHERE disabled=0 ORDER BY id ASC"
                ).fetchall()
            index: dict[str, dict[str, dict[str, Any]]] = {
                knowledge_type: {} for knowledge_type in _ALLOWED_TYPES
            }
            for row in rows:
                item = _row_dict(row)
                for alias in item["aliases"]:
                    normalized = normalize_value(alias)
                    if normalized:
                        index[item["knowledge_type"]][normalized] = item
            _active_cache = (key, index)
            return index
    except sqlite3.OperationalError as exc:
        if not _is_missing_schema_error(exc):
            raise
        logger.debug("识别数据库尚未初始化，纯解析阶段使用内置只读词库")
        return _builtin_seed_index()


def lookup(value: object, knowledge_type: str = "release_group") -> dict[str, Any] | None:
    if knowledge_type not in _ALLOWED_TYPES:
        return None
    normalized = normalize_value(value)
    if not normalized:
        return None
    item = _active_index()[knowledge_type].get(normalized)
    return dict(item) if item else None


def _find_by_normalized(
    conn: Any, knowledge_type: str, normalized_values: set[str],
    *, exclude_id: int | None = None,
) -> dict[str, Any] | None:
    if not normalized_values:
        return None
    rows = conn.execute(
        "SELECT * FROM recognition_knowledge WHERE knowledge_type=? ORDER BY id ASC",
        (knowledge_type,),
    ).fetchall()
    for row in rows:
        if exclude_id is not None and int(row["id"]) == int(exclude_id):
            continue
        item = _row_dict(row)
        values = {normalize_value(item["canonical_value"])}
        values.update(normalize_value(alias) for alias in item["aliases"])
        if normalized_values & values:
            return item
    return None


def lookup_any(value: object, knowledge_type: str = "release_group") -> dict[str, Any] | None:
    """查找启用或停用词条；停用词条用于阻止 AI 绕过人工决定重新学习。"""
    if knowledge_type not in _ALLOWED_TYPES:
        return None
    normalized = normalize_value(value)
    if not normalized:
        return None
    try:
        ensure_seed_knowledge()
        with get_conn() as conn:
            item = _find_by_normalized(conn, knowledge_type, {normalized})
    except sqlite3.OperationalError as exc:
        if not _is_missing_schema_error(exc):
            raise
        item = _builtin_seed_index()[knowledge_type].get(normalized)
    return dict(item) if item else None


def is_known(value: object, knowledge_type: str = "release_group") -> bool:
    return lookup(value, knowledge_type) is not None


def record_hit(entry_id: int, *, success: bool | None = None) -> None:
    fields = ["hit_count=hit_count+1", "updated_at=?"]
    if success is True:
        fields.append("success_count=success_count+1")
    elif success is False:
        fields.append("conflict_count=conflict_count+1")
    try:
        with get_conn() as conn:
            conn.execute(
                f"UPDATE recognition_knowledge SET {','.join(fields)} WHERE id=?",
                (now(), int(entry_id)),
            )
    except Exception as exc:
        logger.debug("识别知识命中统计写入失败 type=%s", type(exc).__name__)


def list_entries(*, keyword: str = "", knowledge_type: str = "", limit: int = 300) -> dict[str, Any]:
    ensure_seed_knowledge()
    keyword = str(keyword or "").strip()[:160]
    knowledge_type = str(knowledge_type or "").strip()
    if knowledge_type and knowledge_type not in _ALLOWED_TYPES:
        raise ValueError("知识类型无效")
    limit = max(1, min(int(limit or 300), 500))
    where: list[str] = []
    values: list[Any] = []
    if knowledge_type:
        where.append("knowledge_type=?")
        values.append(knowledge_type)
    if keyword:
        where.append("(canonical_value LIKE ? OR aliases_json LIKE ?)")
        pattern = f"%{keyword}%"
        values.extend([pattern, pattern])
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM recognition_knowledge{clause} "
            "ORDER BY disabled ASC, source='user' DESC, source='learned' DESC, updated_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        summary_row = conn.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN disabled=0 THEN 1 ELSE 0 END) enabled,
                   SUM(CASE WHEN disabled=1 THEN 1 ELSE 0 END) disabled,
                   SUM(CASE WHEN source='builtin' THEN 1 ELSE 0 END) builtin,
                   SUM(CASE WHEN source='learned' THEN 1 ELSE 0 END) learned,
                   SUM(CASE WHEN source='user' THEN 1 ELSE 0 END) user
            FROM recognition_knowledge
            """
        ).fetchone()
    return {
        "items": [_row_dict(row) for row in rows],
        "summary": {key: int(summary_row[key] or 0) for key in summary_row.keys()},
    }


def get_entry(entry_id: int) -> dict[str, Any] | None:
    ensure_seed_knowledge()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recognition_knowledge WHERE id=?", (int(entry_id),)
        ).fetchone()
    return _row_dict(row) if row else None



def _assert_no_alias_collision(
    knowledge_type: str, aliases: list[str], *, conn: Any, exclude_id: int | None = None
) -> None:
    normalized_aliases = {normalize_value(value) for value in aliases if normalize_value(value)}
    collision = _find_by_normalized(
        conn, knowledge_type, normalized_aliases, exclude_id=exclude_id
    )
    if collision:
        raise ValueError(f"识别名称已被词条「{collision['canonical_value']}」使用")


def create_entry(data: dict[str, Any]) -> dict[str, Any]:
    ensure_seed_knowledge()
    normalized = _validate_values({**data, "source": data.get("source") or "user"})
    timestamp = now()
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _assert_no_alias_collision(
                normalized["knowledge_type"], normalized["aliases"], conn=conn
            )
            cursor = conn.execute(
                """
                INSERT INTO recognition_knowledge(
                    knowledge_key,knowledge_type,canonical_value,normalized_value,
                    aliases_json,source,confidence,disabled,user_modified,seed_revision,
                    evidence_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,1,0,?,?,?)
                """,
                (
                    normalized["knowledge_key"], normalized["knowledge_type"],
                    normalized["canonical_value"], normalized["normalized_value"],
                    json.dumps(normalized["aliases"], ensure_ascii=False),
                    normalized["source"], normalized["confidence"], int(normalized["disabled"]),
                    json.dumps(normalized["evidence"], ensure_ascii=False), timestamp, timestamp,
                ),
            )
            entry_id = int(cursor.lastrowid)
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise ValueError("相同识别知识已经存在") from None
        raise
    _invalidate_cache()
    return get_entry(entry_id) or {}


def update_entry(entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
    ensure_seed_knowledge()
    timestamp = now()
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM recognition_knowledge WHERE id=?", (int(entry_id),)
            ).fetchone()
            if not row:
                raise ValueError("识别知识不存在")
            existing = _row_dict(row)
            normalized = _validate_values(data, existing=existing)
            _assert_no_alias_collision(
                normalized["knowledge_type"], normalized["aliases"],
                conn=conn, exclude_id=int(entry_id),
            )
            conn.execute(
                """
                UPDATE recognition_knowledge SET
                    knowledge_key=?,knowledge_type=?,canonical_value=?,normalized_value=?,
                    aliases_json=?,confidence=?,disabled=?,user_modified=1,evidence_json=?,updated_at=?
                WHERE id=?
                """,
                (
                    normalized["knowledge_key"], normalized["knowledge_type"],
                    normalized["canonical_value"], normalized["normalized_value"],
                    json.dumps(normalized["aliases"], ensure_ascii=False),
                    normalized["confidence"], int(normalized["disabled"]),
                    json.dumps(normalized["evidence"], ensure_ascii=False), timestamp, int(entry_id),
                ),
            )
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise ValueError("相同识别知识已经存在") from None
        raise
    _invalidate_cache()
    return get_entry(entry_id) or {}


def delete_entry(entry_id: int) -> bool:
    existing = get_entry(entry_id)
    if not existing:
        return False
    if existing["source"] == "builtin":
        raise ValueError("内置知识不能删除，可以将其停用")
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM recognition_knowledge WHERE id=?", (int(entry_id),))
    _invalidate_cache()
    return cursor.rowcount == 1


def record_learned_release_group(
    value: str, *, confidence: float, aliases: list[str] | tuple[str, ...] = (),
    evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """记录 AI+TMDB 双重验证样本；两个不同样本后自动启用精确词条。"""
    ensure_seed_knowledge()
    canonical = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not canonical:
        raise ValueError("发布组名称不能为空")
    normalized_value = normalize_value(canonical)
    candidate_aliases = []
    for alias in aliases:
        text = unicodedata.normalize("NFKC", str(alias or "")).strip()
        if text and (
            normalize_value(text) == normalized_value
            or not lookup_any(text, "release_group")
        ):
            candidate_aliases.append(text)
    alias_values = _validate_values({
        "knowledge_type": "release_group",
        "canonical_value": canonical,
        "aliases": candidate_aliases,
        "source": "learned",
        "confidence": confidence,
        "disabled": True,
    })["aliases"]
    key = f"learned-release-group:{normalized_value}"
    incoming = dict(evidence or {})
    sample_key = str(incoming.pop("sample_key", "") or "").strip()[:128]
    if not sample_key:
        raise ValueError("学习样本标识不能为空")
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM recognition_knowledge WHERE knowledge_key=?", (key,)
        ).fetchone()
        current_id = int(row["id"]) if row else None
        existing_match = _find_by_normalized(
            conn, "release_group", {normalized_value}, exclude_id=current_id
        )
        if existing_match:
            # 已存在的启用或停用词条优先；尤其不能绕过用户的停用决定。
            return existing_match
        if row:
            item = _row_dict(row)
            stored_evidence = dict(item.get("evidence") or {})
            samples = [str(value) for value in stored_evidence.get("samples", []) if str(value)]
            is_new_sample = bool(sample_key and sample_key not in samples)
            if is_new_sample:
                samples.append(sample_key)
            stored_evidence.update(incoming)
            stored_evidence["samples"] = samples[-8:]
            merged_aliases = _validate_values({
                "knowledge_type": "release_group",
                "canonical_value": item["canonical_value"],
                "aliases": [*item.get("aliases", []), *alias_values],
                "source": "learned",
                "confidence": max(float(item.get("confidence") or 0), float(confidence)),
                "disabled": bool(item.get("disabled", True)),
            })["aliases"]
            _assert_no_alias_collision(
                "release_group", merged_aliases, conn=conn, exclude_id=int(item["id"])
            )
            success_count = int(item.get("success_count") or 0) + int(is_new_sample)
            hit_count = int(item.get("hit_count") or 0) + 1
            disabled = (
                int(bool(item.get("disabled")))
                if item.get("user_modified")
                else (0 if success_count >= 2 else 1)
            )
            conn.execute(
                """
                UPDATE recognition_knowledge SET aliases_json=?,confidence=?,hit_count=?,success_count=?,
                    disabled=?,evidence_json=?,updated_at=? WHERE id=?
                """,
                (
                    json.dumps(merged_aliases, ensure_ascii=False),
                    max(float(item.get("confidence") or 0), float(confidence)),
                    hit_count, success_count, disabled,
                    json.dumps(stored_evidence, ensure_ascii=False), timestamp, int(item["id"]),
                ),
            )
            entry_id = int(item["id"])
        else:
            _assert_no_alias_collision("release_group", alias_values, conn=conn)
            stored_evidence = {**incoming, "samples": [sample_key] if sample_key else []}
            cursor = conn.execute(
                """
                INSERT INTO recognition_knowledge(
                    knowledge_key,knowledge_type,canonical_value,normalized_value,aliases_json,
                    source,confidence,hit_count,success_count,conflict_count,disabled,user_modified,
                    seed_revision,evidence_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,'learned',?,1,1,0,1,0,0,?,?,?)
                """,
                (
                    key, "release_group", canonical, normalized_value,
                    json.dumps(alias_values, ensure_ascii=False), round(float(confidence), 3),
                    json.dumps(stored_evidence, ensure_ascii=False), timestamp, timestamp,
                ),
            )
            entry_id = int(cursor.lastrowid)
    _invalidate_cache()
    return get_entry(entry_id) or {}


def reset_runtime_state_for_tests() -> None:
    """测试辅助：数据库路径切换后清理模块级缓存。"""
    global _active_cache, _builtin_cache
    with _seed_lock, _cache_lock:
        _seed_paths.clear()
        _active_cache = None
        _builtin_cache = None
