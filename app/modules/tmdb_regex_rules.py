"""持久化、校验并匹配有界的 TMDB 正则识别规则。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from re import _constants as re_constants
from re import _parser as re_parser
from typing import Any

from app.database import get_conn, now
from app.logger import get_logger

logger = get_logger(__name__)

MAX_RULES = 200
MAX_PATTERN_LENGTH = 500
_MAX_NAME_LENGTH = 120
_MAX_SUBJECT_LENGTH = 4096
_ALLOWED_TARGETS = {"filename", "parent", "both"}
_ALLOWED_MEDIA_TYPES = {"any", "movie", "tv"}
_REPEAT_OPS = {
    value for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
    if (value := getattr(re_constants, name, None)) is not None
}
_ZERO_WIDTH_OPS = {
    value for name in ("AT", "ASSERT", "ASSERT_NOT")
    if (value := getattr(re_constants, name, None)) is not None
}


def _simple_repeat_domain(tokens) -> tuple[str, object] | None:
    items = list(tokens)
    while (
        len(items) == 1
        and items[0][0] == re_constants.SUBPATTERN
    ):
        items = list(items[0][1][-1])
    if len(items) != 1:
        return None
    operation, argument = items[0]
    if operation == re_constants.ANY:
        return ("any", None)
    if operation == re_constants.LITERAL:
        return ("chars", frozenset({int(argument)}))
    if operation == re_constants.NOT_LITERAL:
        return ("not_chars", frozenset({int(argument)}))
    if operation == re_constants.CATEGORY:
        category = str(argument)
        if "_NOT_" in category:
            return ("not_category", category.replace("_NOT_", "_"))
        return ("category", category)
    if operation != re_constants.IN:
        return None
    negated = bool(argument and argument[0][0] == re_constants.NEGATE)
    entries = argument[1:] if negated else argument
    chars: set[int] = set()
    category = ""
    category_negated = False
    for inner_operation, inner_argument in entries:
        if inner_operation == re_constants.LITERAL:
            chars.add(int(inner_argument))
        elif inner_operation == re_constants.RANGE:
            start, end = inner_argument
            if end - start > 512:
                return ("unknown_chars", None)
            chars.update(range(int(start), int(end) + 1))
        elif inner_operation == re_constants.CATEGORY and not chars and not category:
            category = str(inner_argument)
            category_negated = "_NOT_" in category
            category = category.replace("_NOT_", "_")
        else:
            return ("unknown_chars", None)
    if category:
        return (
            "category" if negated == category_negated else "not_category",
            category,
        )
    return (
        "not_chars" if negated else "chars",
        frozenset(chars),
    )


def _category_contains(category: str, value: int) -> bool:
    char = chr(value)
    if "DIGIT" in category:
        return char.isdigit()
    if "SPACE" in category:
        return char.isspace()
    if "WORD" in category:
        return char.isalnum() or char == "_"
    if "LINEBREAK" in category:
        return char in "\r\n"
    return False


def _repeat_domains_overlap(
    first: tuple[str, object] | None,
    second: tuple[str, object] | None,
) -> bool:
    if (
        (first is not None and first[0] == "unknown_chars")
        or (second is not None and second[0] == "unknown_chars")
    ):
        return True
    if first is None or second is None:
        return False
    if first[0] == "any" or second[0] == "any":
        return True
    if first[0] == second[0] == "not_chars":
        return True
    if first[0] == "not_chars" and second[0] == "chars":
        return bool(second[1] - first[1])
    if first[0] == "chars" and second[0] == "not_chars":
        return bool(first[1] - second[1])
    if first[0] == second[0] == "not_category":
        return True
    if first[0] == "not_category" and second[0] == "category":
        return str(first[1]) != str(second[1])
    if first[0] == "category" and second[0] == "not_category":
        return str(first[1]) != str(second[1])
    if first[0] == "not_category" and second[0] == "chars":
        return any(
            not _category_contains(str(first[1]), value)
            for value in second[1]
        )
    if first[0] == "chars" and second[0] == "not_category":
        return any(
            not _category_contains(str(second[1]), value)
            for value in first[1]
        )
    if first[0].startswith("not_") or second[0].startswith("not_"):
        return True
    if first[0] == second[0] == "chars":
        return bool(first[1] & second[1])
    if first[0] == second[0] == "category":
        left, right = str(first[1]), str(second[1])
        if left == right:
            return True
        return (
            ("DIGIT" in left and "WORD" in right)
            or ("WORD" in left and "DIGIT" in right)
            or ("LINEBREAK" in left and "SPACE" in right)
            or ("SPACE" in left and "LINEBREAK" in right)
        )
    chars, category = (
        (first[1], str(second[1]))
        if first[0] == "chars"
        else (second[1], str(first[1]))
    )
    return any(_category_contains(category, value) for value in chars)


def _validate_parsed_pattern(tokens) -> None:
    previous_repeat: tuple[str, tuple[str, object] | None] | None = None
    for operation, argument in tokens:
        if str(operation).startswith("GROUPREF"):
            raise ValueError("正则安全限制：不允许 backreference 回溯引用")

        if operation in _REPEAT_OPS:
            minimum, maximum, body = argument
            _validate_parsed_pattern(body)
            variable = minimum != maximum
            if variable:
                current = (repr(list(body)), _simple_repeat_domain(body))
                if previous_repeat and (
                    previous_repeat[0] == current[0]
                    or _repeat_domains_overlap(previous_repeat[1], current[1])
                ):
                    raise ValueError(
                        "正则安全限制：不允许相邻模糊量词造成高回溯"
                    )
                previous_repeat = current
            else:
                previous_repeat = None
            continue

        if operation == re_constants.SUBPATTERN:
            _validate_parsed_pattern(argument[-1])
        elif operation == re_constants.BRANCH:
            for branch in argument[1]:
                _validate_parsed_pattern(branch)
        elif operation in {
            value for name in ("ASSERT", "ASSERT_NOT", "ATOMIC_GROUP")
            if (value := getattr(re_constants, name, None)) is not None
        }:
            nested = argument[1] if operation in _ZERO_WIDTH_OPS else argument
            if hasattr(nested, "__iter__"):
                _validate_parsed_pattern(nested)

        if operation not in _ZERO_WIDTH_OPS:
            previous_repeat = None


def _repeat_token(pattern: str, index: int) -> tuple[bool, int]:
    if index >= len(pattern):
        return False, index
    if pattern[index] in "*+":
        return True, index + 1
    if pattern[index] == "?":
        return False, index + 1
    if pattern[index] != "{":
        return False, index
    match = re.match(r"\{(\d+)(?:,(\d*)?)?\}", pattern[index:])
    if not match:
        return False, index
    minimum = int(match.group(1))
    maximum_text = match.group(2)
    if "," not in match.group(0):
        maximum = minimum
    elif maximum_text in (None, ""):
        maximum = None
    else:
        maximum = int(maximum_text)
    return maximum is None or maximum > 1, index + len(match.group(0))


def _validate_pattern_safety(pattern: str) -> None:
    """拒绝常见指数回溯结构；历史规则匹配前也执行。"""
    try:
        _validate_parsed_pattern(re_parser.parse(pattern, re.IGNORECASE))
    except re.error as exc:
        raise ValueError(f"正则表达式无效：{exc}") from exc
    stack: list[dict[str, bool]] = [{"repeat": False, "alternation": False}]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 < len(pattern) and (
                pattern[index + 1].isdigit() or pattern[index + 1] == "g"
            ):
                raise ValueError("正则安全限制：不允许 backreference 回溯引用")
            index += 2
            continue
        if char == "[":
            index += 1
            while index < len(pattern):
                if pattern[index] == "\\":
                    index += 2
                elif pattern[index] == "]":
                    index += 1
                    break
                else:
                    index += 1
            continue
        if char == "(":
            stack.append({"repeat": False, "alternation": False})
            index += 1
            if index < len(pattern) and pattern[index] == "?":
                if pattern.startswith("?P<", index):
                    end = pattern.find(">", index + 3)
                    index = len(pattern) if end < 0 else end + 1
                elif index + 1 < len(pattern) and pattern[index + 1] in ":=!":
                    index += 2
                elif pattern.startswith("?<=", index) or pattern.startswith("?<!", index):
                    index += 3
                else:
                    colon = pattern.find(":", index + 1, min(len(pattern), index + 12))
                    if colon >= 0 and re.fullmatch(
                        r"\?[aiLmsux-]+", pattern[index:colon]
                    ):
                        index = colon + 1
            continue
        if char == ")" and len(stack) > 1:
            group = stack.pop()
            dangerous_outer, end = _repeat_token(pattern, index + 1)
            if dangerous_outer and (group["repeat"] or group["alternation"]):
                raise ValueError("正则安全限制：不允许嵌套量词或重复分支造成高回溯")
            stack[-1]["repeat"] = stack[-1]["repeat"] or group["repeat"] or dangerous_outer
            stack[-1]["alternation"] = (
                stack[-1]["alternation"] or group["alternation"]
            )
            index = end if end > index + 1 else index + 1
            continue
        if char == "|":
            stack[-1]["alternation"] = True
            index += 1
            continue
        repeated, end = _repeat_token(pattern, index)
        if repeated or char == "?":
            stack[-1]["repeat"] = True
            index = end if end > index else index + 1
            continue
        index += 1


def validate_safe_regex(pattern: str) -> re.Pattern[str]:
    """编译并执行项目统一的正则安全检查。"""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("正则表达式不能为空")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"正则表达式不能超过 {MAX_PATTERN_LENGTH} 个字符")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"正则表达式无效：{exc}") from exc
    _validate_pattern_safety(pattern)
    return compiled


@dataclass(frozen=True)
class RuleMatch:
    rule_id: int
    rule_name: str
    tmdb_id: str
    media_type: str
    season_override: int | None
    match_target: str


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _normalize(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("规则必须是对象")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("规则名称不能为空")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"规则名称不能超过 {_MAX_NAME_LENGTH} 个字符")

    pattern_value = data.get("pattern")
    pattern = pattern_value if isinstance(pattern_value, str) else ""
    if not pattern:
        raise ValueError("正则表达式不能为空")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"正则表达式不能超过 {MAX_PATTERN_LENGTH} 个字符")
    validate_safe_regex(pattern)

    match_target = str(data.get("match_target") or "filename").strip().lower()
    if match_target not in _ALLOWED_TARGETS:
        raise ValueError("匹配目标必须是 filename、parent 或 both")

    tmdb_id = str(data.get("tmdb_id") or "").strip()
    if not tmdb_id.isdigit() or len(tmdb_id) > 20:
        raise ValueError("tmdb_id 必须是最多 20 位数字")

    media_type = str(data.get("media_type") or "any").strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise ValueError("媒体类型必须是 any、movie 或 tv")

    season_raw = data.get("season_override")
    season_override = None
    if season_raw not in (None, ""):
        season_override = _integer(season_raw, "季覆盖", minimum=0, maximum=999)
        if media_type == "movie":
            raise ValueError("电影规则不能设置季覆盖")

    priority = _integer(data.get("priority", 0), "优先级", minimum=-10_000, maximum=10_000)
    disabled_raw = data.get("disabled", False)
    if isinstance(disabled_raw, str):
        disabled = disabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        disabled = bool(disabled_raw)
    return {
        "name": name,
        "pattern": pattern,
        "match_target": match_target,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "season_override": season_override,
        "priority": priority,
        "disabled": disabled,
    }


def _serialize(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "pattern": str(row["pattern"]),
        "match_target": str(row["match_target"]),
        "tmdb_id": str(row["tmdb_id"]),
        "media_type": str(row["media_type"]),
        "season_override": (
            int(row["season_override"]) if row["season_override"] is not None else None
        ),
        "priority": int(row["priority"]),
        "disabled": bool(row["disabled"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def create_rule(data: dict) -> dict:
    rule = _normalize(data)
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = int(conn.execute("SELECT COUNT(*) FROM tmdb_regex_rules").fetchone()[0])
        if count >= MAX_RULES:
            raise ValueError(f"TMDB 正则规则最多 {MAX_RULES} 条")
        cursor = conn.execute(
            "INSERT INTO tmdb_regex_rules(name,pattern,match_target,tmdb_id,media_type,"
            "season_override,priority,disabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                rule["name"], rule["pattern"], rule["match_target"], rule["tmdb_id"],
                rule["media_type"], rule["season_override"], rule["priority"],
                1 if rule["disabled"] else 0, timestamp, timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tmdb_regex_rules WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
    created = _serialize(row)
    logger.info(
        "TMDB 强制匹配规则已创建 id=%s name=%s tmdb=%s type=%s priority=%s",
        created["id"], created["name"], created["tmdb_id"],
        created["media_type"], created["priority"],
    )
    return created


def list_rules() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tmdb_regex_rules ORDER BY priority DESC, id ASC"
        ).fetchall()
    return [_serialize(row) for row in rows]


def get_rule(rule_id: int) -> dict | None:
    identifier = _integer(rule_id, "规则 ID", minimum=1, maximum=2_147_483_647)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tmdb_regex_rules WHERE id=?", (identifier,)
        ).fetchone()
    return _serialize(row) if row is not None else None


def update_rule(rule_id: int, data: dict) -> dict:
    identifier = _integer(rule_id, "规则 ID", minimum=1, maximum=2_147_483_647)
    rule = _normalize(data)
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE tmdb_regex_rules SET name=?,pattern=?,match_target=?,tmdb_id=?,"
            "media_type=?,season_override=?,priority=?,disabled=?,updated_at=? WHERE id=?",
            (
                rule["name"], rule["pattern"], rule["match_target"], rule["tmdb_id"],
                rule["media_type"], rule["season_override"], rule["priority"],
                1 if rule["disabled"] else 0, now(), identifier,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("TMDB 正则规则不存在")
        row = conn.execute(
            "SELECT * FROM tmdb_regex_rules WHERE id=?", (identifier,)
        ).fetchone()
    updated = _serialize(row)
    logger.info(
        "TMDB 强制匹配规则已更新 id=%s name=%s tmdb=%s type=%s priority=%s disabled=%s",
        updated["id"], updated["name"], updated["tmdb_id"],
        updated["media_type"], updated["priority"], updated["disabled"],
    )
    return updated


def delete_rule(rule_id: int) -> bool:
    identifier = _integer(rule_id, "规则 ID", minimum=1, maximum=2_147_483_647)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tmdb_regex_rules WHERE id=?", (identifier,)
        ).fetchone()
        cursor = conn.execute("DELETE FROM tmdb_regex_rules WHERE id=?", (identifier,))
    deleted = cursor.rowcount > 0
    if deleted and row is not None:
        snapshot = _serialize(row)
        logger.info(
            "TMDB 强制匹配规则已删除 id=%s name=%s tmdb=%s type=%s",
            snapshot["id"], snapshot["name"], snapshot["tmdb_id"], snapshot["media_type"],
        )
    return deleted


def _subject(match_target: str, filename: str, parent_path: str) -> str:
    filename = str(filename or "")[:_MAX_SUBJECT_LENGTH]
    parent_path = str(parent_path or "")[:_MAX_SUBJECT_LENGTH]
    if match_target == "parent":
        return parent_path
    if match_target == "both":
        return f"{parent_path.rstrip('/\\')}/{filename}"[-_MAX_SUBJECT_LENGTH:]
    return filename


def _matches(rule: dict, filename: str, parent_path: str, media_type: str) -> bool:
    requested_type = "tv" if str(media_type).lower() == "tv" else "movie"
    if rule["media_type"] not in {"any", requested_type}:
        return False
    try:
        _validate_pattern_safety(str(rule["pattern"]))
    except ValueError:
        return False
    return re.search(
        rule["pattern"],
        _subject(rule["match_target"], filename, parent_path),
        re.IGNORECASE,
    ) is not None


def find_tmdb_regex_match(
    filename: str, parent_path: str = "", media_type: str = "movie"
) -> RuleMatch | None:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tmdb_regex_rules WHERE disabled=0 "
                "ORDER BY priority DESC, id ASC"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    for row in rows:
        rule = _serialize(row)
        if _matches(rule, filename, parent_path, media_type):
            resolved_type = (
                "tv" if rule["media_type"] == "tv" else
                "movie" if rule["media_type"] == "movie" else
                ("tv" if str(media_type).lower() == "tv" else "movie")
            )
            return RuleMatch(
                rule_id=rule["id"],
                rule_name=rule["name"],
                tmdb_id=rule["tmdb_id"],
                media_type=resolved_type,
                season_override=rule["season_override"],
                match_target=rule["match_target"],
            )
    return None


def preview_rule(
    data: dict,
    filename: str,
    parent_path: str = "",
    media_type: str = "movie",
) -> dict:
    rule = _normalize(data)
    matched = not rule["disabled"] and _matches(rule, filename, parent_path, media_type)
    return {
        "matched": matched,
        "tmdb_id": rule["tmdb_id"] if matched else "",
        "media_type": rule["media_type"],
        "season_override": rule["season_override"],
        "match_target": rule["match_target"],
        "sample": _subject(rule["match_target"], filename, parent_path),
    }
