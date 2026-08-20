"""识别预处理规则：在 TMDB 搜索前清洗标题，并安全调整季集位置。"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.database import get_conn, now, resolve_db_path
from app.modules.tmdb_regex_rules import validate_safe_regex

MAX_RULES = 200
MAX_PATTERN_LENGTH = 500
_MAX_NAME_LENGTH = 120
_ALLOWED_MATCHERS = {"text", "regex"}
_ALLOWED_SCOPES = {"filename", "parent", "both"}
_ALLOWED_ACTIONS = {
    "delete", "replace", "season_override", "season_offset", "episode_offset",
}
_cache_lock = threading.RLock()
_active_cache: tuple[str, list[dict[str, Any]]] | None = None

# 默认启用项只处理无歧义的字符归一化、隐藏控制符和明确技术规格。
# 作品别名、发行版本、配音标签和季集修正只提供停用模板，避免全局误伤。
BUILTIN_RULES: tuple[dict[str, Any], ...] = (
    {
        "builtin_key": "P010-unicode-invisible-separator",
        "name": "清理不可见分隔符",
        "matcher_type": "regex",
        "pattern": r"[\uFEFF\u200B-\u200D\u2060]",
        "scope": "both",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 1000,
        "disabled": False,
    },
    {
        "builtin_key": "P011-unicode-wide-space",
        "name": "统一全角与不换行空格",
        "matcher_type": "regex",
        "pattern": r"[\u00A0\u3000]+",
        "scope": "both",
        "action": "replace",
        "replacement": " ",
        "numeric_value": None,
        "priority": 990,
        "disabled": False,
    },
    {
        "builtin_key": "P012-ascii-control-to-space",
        "name": "控制字符转普通空格",
        "matcher_type": "regex",
        "pattern": r"[\u0000-\u001F\u007F]+",
        "scope": "both",
        "action": "replace",
        "replacement": " ",
        "numeric_value": None,
        "priority": 985,
        "disabled": False,
    },
    {
        "builtin_key": "P013-unicode-extra-format-controls",
        "name": "清理扩展 Unicode 格式控制符",
        "matcher_type": "regex",
        "pattern": r"[\u00AD\u034F\u061C\u180E\u200E\u200F\u202A-\u202E\u2061-\u2064\u2066-\u206F\uFE00-\uFE0F]",
        "scope": "both",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 980,
        "disabled": False,
    },
    {
        "builtin_key": "P014-unicode-other-space",
        "name": "统一其他 Unicode 空白",
        "matcher_type": "regex",
        "pattern": r"[\u2000-\u200A\u2028\u2029\u202F\u205F]+",
        "scope": "both",
        "action": "replace",
        "replacement": " ",
        "numeric_value": None,
        "priority": 975,
        "disabled": False,
    },
    {
        "builtin_key": "P015-fullwidth-square-open",
        "name": "统一全角左方括号",
        "matcher_type": "text",
        "pattern": "［",
        "scope": "both",
        "action": "replace",
        "replacement": "[",
        "numeric_value": None,
        "priority": 970,
        "disabled": False,
    },
    {
        "builtin_key": "P016-fullwidth-square-close",
        "name": "统一全角右方括号",
        "matcher_type": "text",
        "pattern": "］",
        "scope": "both",
        "action": "replace",
        "replacement": "]",
        "numeric_value": None,
        "priority": 969,
        "disabled": False,
    },
    {
        "builtin_key": "P017-fullwidth-paren-open",
        "name": "统一全角左圆括号",
        "matcher_type": "text",
        "pattern": "（",
        "scope": "both",
        "action": "replace",
        "replacement": "(",
        "numeric_value": None,
        "priority": 968,
        "disabled": False,
    },
    {
        "builtin_key": "P018-fullwidth-paren-close",
        "name": "统一全角右圆括号",
        "matcher_type": "text",
        "pattern": "）",
        "scope": "both",
        "action": "replace",
        "replacement": ")",
        "numeric_value": None,
        "priority": 967,
        "disabled": False,
    },
    {
        "builtin_key": "P019-unicode-dot-separator",
        "name": "统一兼容句点",
        "matcher_type": "regex",
        "pattern": r"[．｡]",
        "scope": "filename",
        "action": "replace",
        "replacement": ".",
        "numeric_value": None,
        "priority": 960,
        "disabled": False,
    },
    {
        "builtin_key": "P020-unicode-hyphen-separator",
        "name": "统一 Unicode 连字符",
        "matcher_type": "regex",
        "pattern": r"[\u2010-\u2015\u2212\uFF0D]",
        "scope": "both",
        "action": "replace",
        "replacement": "-",
        "numeric_value": None,
        "priority": 955,
        "disabled": False,
    },
    {
        "builtin_key": "X040-title-alias-rewrite-template",
        "name": "模板：作品别名定向替换",
        "matcher_type": "text",
        "pattern": "__旧别名或错误译名__",
        "scope": "both",
        "action": "replace",
        "replacement": "__官方名或稳定检索名__",
        "numeric_value": None,
        "priority": 700,
        "disabled": True,
    },
    {
        "builtin_key": "P080-bracket-language-display-only",
        "name": "清理独立语言与字幕标签",
        "matcher_type": "regex",
        "pattern": r"(?i)[\[【(（]\s*(?:(?:JPN|JP|日语|日文|ENG|EN|英语|英字)|(?:中文字幕|内封(?:简繁)?字幕|简繁字幕|繁简字幕|双语字幕))\s*[\]】)）]",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 650,
        "disabled": False,
    },
    {
        "builtin_key": "P090-dolby-vision-release-tag",
        "name": "清理 Dolby Vision 发布规格",
        "matcher_type": "regex",
        "pattern": r"(?i)(?<![a-z0-9])(?:dovi|dolby[ ._-]?vision)(?![a-z0-9])",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 645,
        "disabled": False,
    },
    {
        "builtin_key": "P091-dtsx-release-tag",
        "name": "清理 DTS:X 发布规格",
        "matcher_type": "regex",
        "pattern": r"(?i)(?<![a-z0-9])dts[ .:_-]?x(?![a-z0-9])",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 640,
        "disabled": False,
    },
    {
        "builtin_key": "X050-edition-tag-delete-template",
        "name": "模板：定向删除发行版本标签",
        "matcher_type": "regex",
        "pattern": r"(?i)[\[【(（]\s*(?:proper|repack|rerip|limited|extended|unrated|theatrical|director'?s[ ._-]?cut)\s*[\]】)）]",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 630,
        "disabled": True,
    },
    {
        "builtin_key": "X060-dub-audio-tag-delete-template",
        "name": "模板：定向删除配音标签",
        "matcher_type": "regex",
        "pattern": r"[\[【(（]\s*(?:国语配音|國語配音|粤语配音|粵語配音|台配|港配|dub(?:bed)?|dual[ ._-]?audio)\s*[\]】)）]",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 620,
        "disabled": True,
    },
    {
        "builtin_key": "X070-release-group-prefix-template",
        "name": "模板：特定发布组前缀",
        "matcher_type": "regex",
        "pattern": r"(?i)^\s*[\[【]\s*__已核验发布组或来源__\s*[\]】]\s*",
        "scope": "filename",
        "action": "delete",
        "replacement": "",
        "numeric_value": None,
        "priority": 610,
        "disabled": True,
    },
    {
        "builtin_key": "X010-season-override-template",
        "name": "模板：指定作品季号覆盖",
        "matcher_type": "text",
        "pattern": "__请改成作品或来源特征__",
        "scope": "both",
        "action": "season_override",
        "replacement": "",
        "numeric_value": 2,
        "priority": 400,
        "disabled": True,
    },
    {
        "builtin_key": "X020-season-offset-template",
        "name": "模板：季号偏移",
        "matcher_type": "text",
        "pattern": "__请改成作品或来源特征__",
        "scope": "both",
        "action": "season_offset",
        "replacement": "",
        "numeric_value": 1,
        "priority": 390,
        "disabled": True,
    },
    {
        "builtin_key": "X030-episode-offset-template",
        "name": "模板：集数偏移",
        "matcher_type": "text",
        "pattern": "__请改成作品或来源特征__",
        "scope": "both",
        "action": "episode_offset",
        "replacement": "",
        "numeric_value": 1,
        "priority": 380,
        "disabled": True,
    },
)


@dataclass(frozen=True)
class PreprocessResult:
    filename: str
    parent_path: str
    season: int | None
    episode: int | None
    applied_rules: list[dict[str, Any]] = field(default_factory=list)


def _integer(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@lru_cache(maxsize=512)
def _compile_regex(pattern: str) -> re.Pattern[str]:
    """缓存安全校验和编译结果，避免逐文件重复解析同一表达式。"""
    return validate_safe_regex(pattern)


def normalize_rule(data: dict[str, Any], *, allow_builtin_key: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("规则必须是对象")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("规则名称不能为空")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"规则名称不能超过 {_MAX_NAME_LENGTH} 个字符")

    matcher_type = str(data.get("matcher_type") or "text").strip().lower()
    if matcher_type not in _ALLOWED_MATCHERS:
        raise ValueError("匹配方式必须是 text 或 regex")
    pattern = str(data.get("pattern") or "")
    if not pattern:
        raise ValueError("匹配内容不能为空")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"匹配内容不能超过 {MAX_PATTERN_LENGTH} 个字符")
    if matcher_type == "regex":
        _compile_regex(pattern)

    scope = str(data.get("scope") or "filename").strip().lower()
    if scope not in _ALLOWED_SCOPES:
        raise ValueError("匹配范围必须是 filename、parent 或 both")
    action = str(data.get("action") or "delete").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("不支持的预处理动作")

    replacement = str(data.get("replacement") or "")
    numeric_value = None
    if action == "season_override":
        numeric_value = _integer(data.get("numeric_value"), "季号", 0, 999)
    elif action in {"season_offset", "episode_offset"}:
        numeric_value = _integer(data.get("numeric_value"), "偏移量", -999, 999)
    elif action == "delete":
        replacement = ""
    if len(replacement) > 500:
        raise ValueError("替换内容不能超过 500 个字符")

    result = {
        "name": name,
        "matcher_type": matcher_type,
        "pattern": pattern,
        "scope": scope,
        "action": action,
        "replacement": replacement,
        "numeric_value": numeric_value,
        "priority": _integer(data.get("priority", 0), "优先级", -10_000, 10_000),
        "disabled": _as_bool(data.get("disabled", False)),
        "builtin_key": "",
    }
    if allow_builtin_key:
        result["builtin_key"] = str(data.get("builtin_key") or "").strip()[:120]
    return result


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "matcher_type": str(row["matcher_type"]),
        "pattern": str(row["pattern"]),
        "scope": str(row["scope"]),
        "action": str(row["action"]),
        "replacement": str(row["replacement"] or ""),
        "numeric_value": row["numeric_value"],
        "priority": int(row["priority"]),
        "disabled": bool(row["disabled"]),
        "builtin_key": str(row["builtin_key"] or ""),
        "builtin": bool(row["builtin_key"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _invalidate_cache() -> None:
    global _active_cache
    with _cache_lock:
        _active_cache = None
        _compile_regex.cache_clear()


def invalidate_active_cache() -> None:
    """清除当前进程的启用规则缓存，供受控配置写入后调用。"""
    _invalidate_cache()


def ensure_builtin_rules() -> None:
    timestamp = now()
    try:
        with get_conn() as conn:
            for source in BUILTIN_RULES:
                data = normalize_rule(dict(source), allow_builtin_key=True)
                conn.execute(
                    "INSERT OR IGNORE INTO recognition_preprocess_rules "
                    "(name,matcher_type,pattern,scope,action,replacement,numeric_value,priority,disabled,builtin_key,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        data["name"], data["matcher_type"], data["pattern"], data["scope"],
                        data["action"], data["replacement"], data["numeric_value"],
                        data["priority"], int(data["disabled"]), data["builtin_key"],
                        timestamp, timestamp,
                    ),
                )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


def restore_builtin_rules() -> list[dict[str, Any]]:
    timestamp = now()
    with get_conn() as conn:
        for source in BUILTIN_RULES:
            data = normalize_rule(dict(source), allow_builtin_key=True)
            conn.execute(
                "INSERT INTO recognition_preprocess_rules "
                "(name,matcher_type,pattern,scope,action,replacement,numeric_value,priority,disabled,builtin_key,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(builtin_key) WHERE builtin_key <> '' DO UPDATE SET "
                "name=excluded.name,matcher_type=excluded.matcher_type,pattern=excluded.pattern,"
                "scope=excluded.scope,action=excluded.action,replacement=excluded.replacement,"
                "numeric_value=excluded.numeric_value,priority=excluded.priority,"
                "disabled=excluded.disabled,updated_at=excluded.updated_at",
                (
                    data["name"], data["matcher_type"], data["pattern"], data["scope"],
                    data["action"], data["replacement"], data["numeric_value"],
                    data["priority"], int(data["disabled"]), data["builtin_key"],
                    timestamp, timestamp,
                ),
            )
    _invalidate_cache()
    return list_rules()


def list_rules(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    global _active_cache
    cache_key = str(resolve_db_path())
    if enabled_only:
        with _cache_lock:
            if _active_cache is not None and _active_cache[0] == cache_key:
                return [dict(item) for item in _active_cache[1]]
    ensure_builtin_rules()
    where = "WHERE disabled=0" if enabled_only else ""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM recognition_preprocess_rules {where} ORDER BY priority DESC,id ASC"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    result = [_row(item) for item in rows]
    if enabled_only:
        with _cache_lock:
            _active_cache = (cache_key, [dict(item) for item in result])
    return result


def get_rule(rule_id: int) -> dict[str, Any] | None:
    ensure_builtin_rules()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recognition_preprocess_rules WHERE id=?", (int(rule_id),)
        ).fetchone()
    return _row(row) if row else None


def create_rule(data: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_rule(data)
    ensure_builtin_rules()
    timestamp = now()
    with get_conn() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM recognition_preprocess_rules").fetchone()[0])
        if count >= MAX_RULES:
            raise ValueError(f"识别预处理规则最多 {MAX_RULES} 条")
        cursor = conn.execute(
            "INSERT INTO recognition_preprocess_rules "
            "(name,matcher_type,pattern,scope,action,replacement,numeric_value,priority,disabled,builtin_key,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'',?,?)",
            (
                normalized["name"], normalized["matcher_type"], normalized["pattern"],
                normalized["scope"], normalized["action"], normalized["replacement"],
                normalized["numeric_value"], normalized["priority"], int(normalized["disabled"]),
                timestamp, timestamp,
            ),
        )
        rule_id = int(cursor.lastrowid)
    _invalidate_cache()
    return get_rule(rule_id) or {}


def update_rule(rule_id: int, data: dict[str, Any]) -> dict[str, Any]:
    current = get_rule(rule_id)
    if current is None:
        raise ValueError("识别预处理规则不存在")
    normalized = normalize_rule(data)
    timestamp = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE recognition_preprocess_rules SET name=?,matcher_type=?,pattern=?,scope=?,"
            "action=?,replacement=?,numeric_value=?,priority=?,disabled=?,updated_at=? WHERE id=?",
            (
                normalized["name"], normalized["matcher_type"], normalized["pattern"],
                normalized["scope"], normalized["action"], normalized["replacement"],
                normalized["numeric_value"], normalized["priority"], int(normalized["disabled"]),
                timestamp, int(rule_id),
            ),
        )
    _invalidate_cache()
    return get_rule(rule_id) or {}


def delete_rule(rule_id: int) -> bool:
    current = get_rule(rule_id)
    if current is None:
        return False
    if current["builtin"]:
        raise ValueError("推荐规则不能删除，可停用或恢复推荐值")
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM recognition_preprocess_rules WHERE id=?", (int(rule_id),)
        )
    deleted = cursor.rowcount > 0
    if deleted:
        _invalidate_cache()
    return deleted


def _match_and_replace(subject: str, rule: dict[str, Any]) -> tuple[bool, str]:
    if rule["matcher_type"] == "regex":
        regex = _compile_regex(str(rule["pattern"]))
        matched = bool(regex.search(subject))
        if not matched or rule["action"] not in {"delete", "replace"}:
            return matched, subject
        return True, regex.sub(rule["replacement"], subject)
    pattern = str(rule["pattern"])
    matched = pattern.casefold() in subject.casefold()
    if not matched or rule["action"] not in {"delete", "replace"}:
        return matched, subject
    return True, re.sub(re.escape(pattern), lambda _: rule["replacement"], subject, flags=re.IGNORECASE)


def apply_rules(
    filename: str,
    parent_path: str = "",
    *,
    season: int | None = None,
    episode: int | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> PreprocessResult:
    current_filename = str(filename or "")
    current_parent = str(parent_path or "")
    current_season = season
    current_episode = episode
    trace: list[dict[str, Any]] = []
    active_rules = rules if rules is not None else list_rules(enabled_only=True)
    for raw_rule in active_rules:
        rule = normalize_rule(raw_rule, allow_builtin_key=True)
        if rule["disabled"]:
            continue
        targets = (
            ("filename",) if rule["scope"] == "filename"
            else ("parent",) if rule["scope"] == "parent"
            else ("filename", "parent")
        )
        before_filename, before_parent = current_filename, current_parent
        matched = False
        for target in targets:
            subject = current_filename if target == "filename" else current_parent
            target_matched, updated = _match_and_replace(subject, rule)
            matched = matched or target_matched
            if target == "filename":
                current_filename = updated
            else:
                current_parent = updated
        if not matched:
            continue
        before_season, before_episode = current_season, current_episode
        value = rule["numeric_value"]
        if rule["action"] == "season_override":
            current_season = value
        elif rule["action"] == "season_offset" and current_season is not None:
            current_season = max(0, min(999, current_season + int(value)))
        elif rule["action"] == "episode_offset" and current_episode is not None:
            current_episode = max(0, min(999, current_episode + int(value)))
        trace.append({
            "id": raw_rule.get("id"), "name": rule["name"], "action": rule["action"],
            "builtin": bool(raw_rule.get("builtin_key")),
            "filename_before": before_filename, "filename_after": current_filename,
            "parent_before": before_parent, "parent_after": current_parent,
            "season_before": before_season, "season_after": current_season,
            "episode_before": before_episode, "episode_after": current_episode,
        })
    return PreprocessResult(
        filename=current_filename,
        parent_path=current_parent,
        season=current_season,
        episode=current_episode,
        applied_rules=trace,
    )


def preview_rules(data: dict[str, Any]) -> dict[str, Any]:
    filename = str(data.get("filename") or "")
    parent_path = str(data.get("parent_path") or "")
    if not filename:
        raise ValueError("请输入样例文件名")
    season = data.get("season")
    episode = data.get("episode")
    season = None if season in (None, "") else _integer(season, "原季号", 0, 999)
    episode = None if episode in (None, "") else _integer(episode, "原集数", 0, 999)
    draft = data.get("rule")
    rules = None
    if draft is not None:
        preview_rule = normalize_rule(draft, allow_builtin_key=True)
        preview_rule["disabled"] = False
        rules = [preview_rule]
    result = apply_rules(filename, parent_path, season=season, episode=episode, rules=rules)
    return {
        "filename_before": filename, "filename_after": result.filename,
        "parent_path_before": parent_path, "parent_path_after": result.parent_path,
        "season_before": season, "season_after": result.season,
        "episode_before": episode, "episode_after": result.episode,
        "matched": bool(result.applied_rules), "applied_rules": result.applied_rules,
    }
