"""发布格式教学：受限字段模板、样本回放和唯一解析链使用的规则快照。

模板只提取原始字段，不改文件名、不分配 TMDB 身份、不做集号偏移。
所有数据库写入只发生在显式保存/启停/删除中，解析和预览不会建表。
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import secrets
import sqlite3
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.modules.recognition.cleaner import strip_media_file_suffix
from app.modules.recognition.evaluation import compare_shadow_case
from app.modules.recognition.extractors.deterministic import (
    _extract_episode,
    _extract_explicit_season,
)
from app.modules.special_media import is_special_media_name, is_special_path
from app.modules.web_secret import get_web_secret

MAX_RULES = 128
MAX_EXAMPLES = 8
MAX_FILES = 100
_PREVIEW_EPOCH = secrets.token_hex(16)
_FIELDS = {
    "title": r"[^/\\\r\n]{1,180}?",
    "episode": r"[0-9]{1,4}",
    "season": r"[0-9]{1,2}",
    "version": r"[0-9]{1,2}",
    "resolution": r"(?:480|576|720|1080|2160|4320)[pi]|[48]k",
    "checksum": r"[a-f0-9]{8,64}",
}
_SLOT = re.compile(r"\{([a-z_]+)\}")
_VIDEO_SUFFIX = re.compile(r"(?i)\.(?:mkv|mp4|avi|ts|m2ts|mts|mov|m4v|webm|mpeg|mpg|wmv|flv|vob|tp|f4v|rm|rmvb)$")
# 只保护完整附加内容标签/目录，不把 Interview with... 等正常片名当花絮。
_EXTRA_MARKER = re.compile(
    r"(?i)(?:^|[\[【(/\\])(?:interviews?|访谈|訪談|making[ ._-]+of|"
    r"behind[ ._-]+the[ ._-]+scenes)(?=$|[\]】)/\\])"
)
_cache_lock = threading.RLock()
_cache: tuple[tuple[str, int], tuple[dict, ...]] | None = None
_cache_generation = 0


class FormatConflict(ValueError):
    """预览或规则版本已失效，调用方需要重新读取而非盲目重试。"""


@dataclass(frozen=True)
class CompiledFormat:
    regex: re.Pattern[str]
    prefix: str


def _text(value: object, label: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    value = unicodedata.normalize("NFKC", value).strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label}不能为空或含控制字符，且最多 {maximum} 个字符")
    return value


def _number(value: object, label: str, maximum: int = 9999) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label}必须是 1–{maximum} 的整数")
    return value


def _filename(value: object) -> str:
    value = _text(value, "文件名", 1024)
    if "/" in value or "\\" in value:
        raise ValueError("文件名不能包含目录，请单独填写父目录")
    return value


def _parent(value: object) -> str:
    text = _text(value, "父目录", 4096, required=False).replace("\\", "/")
    return posixpath.normpath(text) if text else ""


@lru_cache(maxsize=256)
def compile_template(template: str) -> CompiledFormat:
    """只有六种固定字段可变，其余字符全部按字面量匹配。"""
    template = _filename(template)
    if not _VIDEO_SUFFIX.search(template):
        raise ValueError("模板须保留视频文件扩展名，如 .mkv；伴随文件不参与教学")
    pieces: list[str] = []
    seen: set[str] = set()
    previous = 0
    prefix = ""
    for slot in _SLOT.finditer(template):
        field = slot.group(1)
        if field not in _FIELDS or field in seen:
            raise ValueError("模板包含未知或重复字段；每种字段只能出现一次")
        literal = template[previous:slot.start()]
        if seen and not literal:
            raise ValueError("两个字段之间必须保留明确的分隔字符")
        if not seen:
            prefix = literal
        pieces.extend((re.escape(literal), f"(?P<{field}>{_FIELDS[field]})"))
        seen.add(field)
        previous = slot.end()
    if not {"title", "episode"} <= seen:
        raise ValueError("模板必须包含 {title} 和 {episode}")
    pieces.append(re.escape(template[previous:]))
    return CompiledFormat(re.compile("".join(pieces), re.IGNORECASE), prefix)


def normalize_draft(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"name", "template", "scope", "parent_path"}:
        raise ValueError("格式草稿字段无效")
    name = _text(value.get("name"), "规则名称", 120)
    template = _text(value.get("template"), "模板", 512)
    compiled = compile_template(template)
    scope = value.get("scope", "directory")
    if not isinstance(scope, str) or scope not in {"directory", "release"}:
        raise ValueError("范围必须是 directory 或 release")
    parent = _parent(value.get("parent_path", ""))
    if scope == "directory" and not parent:
        raise ValueError("仅此目录模式必须填写父目录，且不会递归到子目录")
    if scope == "release":
        if not any(c.isalnum() for c in compiled.prefix):
            raise ValueError("跨目录模板必须保留固定发布前缀，不能只凭任意标题和数字匹配")
        parent = ""
    return {"name": name, "template": template, "scope": scope, "parent_path": parent}


def normalize_request(value: object) -> dict[str, Any]:
    """Web 与 Agent 共用的样本、模板和批量输入校验。"""
    if not isinstance(value, dict) or set(value) - {"draft", "examples", "filenames", "preview_token", "confirmed"}:
        raise ValueError("教学请求字段无效")
    draft = normalize_draft(value.get("draft"))
    raw_examples = value.get("examples")
    if not isinstance(raw_examples, list) or not 2 <= len(raw_examples) <= MAX_EXAMPLES:
        raise ValueError(f"请提供 2–{MAX_EXAMPLES} 个明确标注样本")
    examples = []
    for item in raw_examples:
        if not isinstance(item, dict) or set(item) - {"filename", "title", "episode", "season"}:
            raise ValueError("样本字段无效")
        example: dict[str, Any] = {
            "filename": _filename(item.get("filename")),
            "title": _text(item.get("title"), "样本标题", 180),
            "episode": _number(item.get("episode"), "样本集号"),
        }
        if item.get("season") is not None:
            example["season"] = _number(item["season"], "样本季号", 99)
        if "{season}" in draft["template"] and "season" not in example:
            raise ValueError("模板含 {season} 时，每个样本都需标注原始季号")
        examples.append(example)
    if len({e["filename"] for e in examples}) != len(examples) or len({e["episode"] for e in examples}) < 2:
        raise ValueError("样本必须来自不同文件，并至少覆盖两个不同集号")
    if draft["scope"] == "release" and len({e["title"].casefold() for e in examples}) < 2:
        raise ValueError("跨目录复用至少需要两部不同作品的标注样本")
    filenames = value.get("filenames", [])
    if not isinstance(filenames, list) or len(filenames) > MAX_FILES:
        raise ValueError(f"每次最多预览 {MAX_FILES} 个文件")
    return {"draft": draft, "examples": examples, "filenames": [_filename(f) for f in filenames]}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _signature(draft: dict) -> str:
    return _digest({key: draft[key] for key in ("template", "scope", "parent_path")})


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["examples"] = json.loads(item.pop("examples_json"))
    item["disabled"] = bool(item["disabled"])
    return item


def _read(conn) -> list[dict[str, Any]]:
    return [_row(row) for row in conn.execute("SELECT * FROM recognition_format_rules ORDER BY id")]


def _registry(conn) -> tuple[list[dict], str]:
    rules = _read(conn)
    # AUTOINCREMENT 高水位在删除后仍保留，防止「空库→新增→删除→空库」
    # 让旧预览重新有效；不为确认另建事件表或维护第二套代次。
    sequence = conn.execute(
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='recognition_format_rules'),0)"
    ).fetchone()[0]
    with _cache_lock:
        generation = _cache_generation
    # 恢复旧备份也会失效缓存；即使恢复了相同的行和seq，旧预览仍不能写回。
    return rules, _digest({"rules": rules, "sequence": sequence, "generation": generation, "epoch": _PREVIEW_EPOCH})


def _snapshot() -> tuple[list[dict], str]:
    from app import database as db

    with db.get_conn() as conn:
        conn.execute("BEGIN")
        return _registry(conn)


def list_rules() -> list[dict[str, Any]]:
    from app import database as db

    with db.get_conn() as conn:
        return _read(conn)


def invalidate_cache() -> None:
    global _cache, _cache_generation
    with _cache_lock:
        _cache = None
        _cache_generation += 1


def active_rules() -> tuple[dict, ...]:
    global _cache
    from app import database as db

    path = db.resolve_db_path()
    try:
        inode = path.stat().st_ino
    except FileNotFoundError:
        return ()  # 纯解析不创建尚不存在的数据库。
    key = (str(path), inode)
    with _cache_lock:
        generation = _cache_generation
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    try:
        rules = tuple(rule for rule in list_rules() if not rule["disabled"])
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return ()  # 初始化/迁移前没有教学记录，不在解析链中建表。
    with _cache_lock:
        if generation == _cache_generation:
            _cache = (key, rules)
    return rules


def _extract(filename: str, parent: str, rule: dict) -> dict | None:
    if rule.get("disabled") or (rule["scope"] == "directory" and _parent(parent) != rule["parent_path"]):
        return None
    match = compile_template(rule["template"]).regex.fullmatch(unicodedata.normalize("NFKC", filename).strip())
    if match is None:
        return None
    fields: dict[str, Any] = {key: value.strip() for key, value in match.groupdict().items()}
    for key in ("episode", "season", "version"):
        if key in fields:
            fields[key] = int(fields[key])
            if fields[key] == 0:
                return None
    if not fields["title"]:
        return None
    return fields


def apply_to_surface(surface: dict, filename: str, parent: str, *, rules=None) -> None:
    """为统一 surface 提供字段，不创建第二个匹配/评分/写入入口。"""
    matches = []
    for rule in active_rules() if rules is None else rules:
        fields = _extract(filename, parent, rule)
        if fields is not None:
            matches.append((rule, fields))
    if not matches:
        return
    cleaned = surface["cleaned"] = dict(surface["cleaned"])
    if (surface["season"] == 0 or surface["fractional_position"] is not None
            or is_special_media_name(filename) or is_special_path(parent)
            or _EXTRA_MARKER.search(filename) or _EXTRA_MARKER.search(parent)):
        cleaned["release_format_blocked"] = ["特别篇或伴随内容保持原识别"]
        return
    stem = strip_media_file_suffix(filename)
    strong_episode = _extract_episode(stem)
    strong_season = _extract_explicit_season(stem, episode_context=True)
    if strong_season is None:
        for segment in reversed(_parent(parent).split("/")):
            strong_season = _extract_explicit_season(segment, episode_context=True)
            if strong_season is not None:
                break
    compatible = [(rule, fields) for rule, fields in matches if (
        (strong_episode is None or fields["episode"] == strong_episode)
        and (strong_season is None or fields.get("season", strong_season) == strong_season)
    )]
    if not compatible:
        cleaned["release_format_conflicts"] = [rule["name"] for rule, _ in matches]
        return
    positions = {(fields["title"], fields.get("season", surface["season"]), fields["episode"]) for _, fields in compatible}
    if len(positions) != 1:
        cleaned["release_format_conflicts"] = [rule["name"] for rule, _ in compatible]
        return
    fields = compatible[0][1]
    surface.update(filename_title=fields["title"], release_title_candidates=[fields["title"]],
                   episode=fields["episode"], season=fields.get("season", surface["season"]),
                   format_fields=fields)
    cleaned["release_formats"] = [rule["name"] for rule, _ in compatible]
    for key in ("version", "resolution", "checksum"):
        if key in fields:
            cleaned[f"format_{key}"] = [str(fields[key])]


def _projection(filename: str, parent: str, rules: list[dict]) -> tuple[dict, dict]:
    from app.modules.scraper import _parse_release_core

    context = _parse_release_core(filename, parent, _format_rules=rules).context
    return {"title": context.normalized_title, "season": context.season, "episode": context.episode}, context.cleaned_components


def _preview_row(filename: str, parent: str, before_rules: list[dict], after_rules: list[dict]) -> dict:
    before, _ = _projection(filename, parent, before_rules)
    after, components = _projection(filename, parent, after_rules)
    if _extract(filename, parent, after_rules[-1]) is None:
        status, reason = "unmatched", "不属于该格式或目录，保持原识别流程"
    elif components.get("release_format_conflicts"):
        status, reason = "conflict", "教学格式之间或与明确季集编号冲突，不能自动选取"
    elif components.get("release_format_blocked"):
        status, reason = "blocked", "；".join(components["release_format_blocked"])
    elif components.get("release_formats"):
        status = "unchanged" if before == after else "matched"
        reason = "字段已按模板抽取，仍须通过原有 TMDB 和归档校验"
    else:
        status, reason = "unmatched", "不属于该格式或目录，保持原识别流程"
    return {"filename": filename, "before": before, "after": after, "status": status, "reason": reason}


def _evaluate(request: dict, stored: list[dict]) -> dict:
    draft = request["draft"]
    active = [rule for rule in stored if not rule["disabled"] and rule["signature"] != _signature(draft)]
    experiment = [*active, {**draft, "disabled": False}]
    examples = []
    improved = 0
    for index, sample in enumerate(request["examples"]):
        row = _preview_row(sample["filename"], draft["parent_path"], active, experiment)
        expected = {k: sample[k] for k in ("title", "season", "episode") if k in sample}
        outcomes = compare_shadow_case(case_id=f"teaching:{index}", tags=(), expected=expected,
                                      baseline=row["before"], experiment=row["after"], fields=tuple(expected))
        row["passed"] = row["status"] in {"matched", "unchanged"} and all(o.experiment_category == "matched" for o in outcomes)
        improved += sum(o.baseline_category != "matched" and o.experiment_category == "matched" for o in outcomes)
        examples.append(row)
    regressions = []
    for rule in active:
        for sample in rule["examples"]:
            before, _ = _projection(sample["filename"], rule["parent_path"], active)
            after, components = _projection(sample["filename"], rule["parent_path"], experiment)
            expected = {k: sample[k] for k in ("title", "season", "episode") if k in sample}
            outcomes = compare_shadow_case(case_id=str(rule["id"]), tags=(), expected=expected,
                                          baseline=before, experiment=after, fields=tuple(expected))
            if any(o.regressed for o in outcomes) or components.get("release_format_conflicts"):
                regressions.append({"rule_id": rule["id"], "filename": sample["filename"]})
    rows = [_preview_row(filename, draft["parent_path"], active, experiment) for filename in request["filenames"]]
    summary = {"total": len(rows), "matched": sum(r["status"] in {"matched", "unchanged"} for r in rows),
               "changed": sum(r["before"] != r["after"] for r in rows),
               **{key: sum(r["status"] == key for r in rows) for key in ("unmatched", "blocked", "conflict")},
               "regressions": len(regressions)}
    summary["conflicts"] = summary.pop("conflict")
    warnings = []
    if not all(row["passed"] for row in examples):
        warnings.append("标注样本未全部通过，请核对模板、标题和原始集号；这里不做集号偏移")
    if regressions:
        warnings.append("该模板会使已有教学样本回退，请缩小适用范围")
    if not improved:
        warnings.append("标注样本没有改善，不需要再添加一条重复规则")
    can_save = not warnings and not summary["conflicts"]
    if summary["conflicts"]:
        warnings.append("批量预览存在格式冲突，请先处理冲突")
    return {"draft": draft, "examples": examples, "rows": rows, "summary": summary,
            "regressions": regressions, "warnings": warnings, "can_save": can_save}


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_web_secret(), salt="mediaflux-release-format-preview-v1")


def _receipt(request: dict, result: dict, registry: str) -> dict:
    return {"request": _digest(request), "result": _digest(result), "registry": registry}


def preview(value: object) -> dict:
    request = normalize_request(value)
    stored, registry = _snapshot()
    result = _evaluate(request, stored)
    token = _signer().dumps(_receipt(request, result, registry)) if result["can_save"] else ""
    return {**result, "preview_token": token}


def save(value: Any) -> tuple[dict, bool]:
    from app import database as db

    request = normalize_request(value)
    if value.get("confirmed") is not True:
        raise ValueError("请先预览并明确确认保存；不会执行文件整理")
    token = value.get("preview_token")
    if not isinstance(token, str) or not token or len(token) > 2048:
        raise FormatConflict("请重新预览后保存")
    try:
        receipt = _signer().loads(token, max_age=900)
    except BadSignature as exc:
        raise FormatConflict("预览已失效，请重新预览") from exc
    if not isinstance(receipt, dict) or receipt.get("request") != _digest(request):
        raise FormatConflict("输入已变化，请重新预览")
    stored, registry = _snapshot()
    signature = _signature(request["draft"])
    existing = next((item for item in stored if item["signature"] == signature), None)
    if existing is not None:
        return existing, False  # 重复提交绝不重新启用或覆盖后来编辑的状态。
    if len(stored) >= MAX_RULES:
        raise ValueError(f"最多保存 {MAX_RULES} 条格式，请先清理不再使用的规则")
    result = _evaluate(request, stored)
    if not result["can_save"] or receipt != _receipt(request, result, registry):
        raise FormatConflict("预览结果或规则库已变化，请重新预览")
    draft = request["draft"]
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current, current_registry = _registry(conn)
        duplicate = next((item for item in current if item["signature"] == signature), None)
        if duplicate is not None:
            return duplicate, False
        if current_registry != receipt["registry"]:
            raise FormatConflict("规则库已变化，请重新预览")
        stamp = db.now()
        cursor = conn.execute(
            "INSERT INTO recognition_format_rules(signature,name,template,scope,parent_path,examples_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (signature, draft["name"], draft["template"], draft["scope"], draft["parent_path"],
             json.dumps(request["examples"], ensure_ascii=False), stamp, stamp),
        )
        item = _row(conn.execute("SELECT * FROM recognition_format_rules WHERE id=?", (cursor.lastrowid,)).fetchone())
    invalidate_cache()
    return item, True


def change(rule_id: int, value: object, *, delete: bool = False, expected_snapshot: dict | None = None,
           check_write: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    from app import database as db

    allowed = {"revision"} if delete else {"revision", "disabled"}
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("规则修改字段无效")
    revision = _number(value["revision"], "规则版本", 2**31 - 1)
    if not delete and type(value["disabled"]) is not bool:
        raise ValueError("disabled 必须是布尔值")
    stored, registry = _snapshot()
    item = next((item for item in stored if item["id"] == rule_id), None)
    if item is None or item["revision"] != revision:
        raise FormatConflict("规则已改变或删除，请刷新列表")
    if expected_snapshot is not None and {k: v for k, v in item.items() if k != "updated_at"} != expected_snapshot:
        raise FormatConflict("发布格式规则已变化，请重新预检")
    if not delete and not value["disabled"]:
        request = {"draft": {key: item[key] for key in ("name", "template", "scope", "parent_path")},
                   "examples": item["examples"], "filenames": []}
        result = _evaluate(request, stored)
        if not all(row["passed"] for row in result["examples"]) or result["regressions"]:
            raise FormatConflict("重新启用会与当前规则冲突，请重新教学")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _registry(conn)[1] != registry:
            raise FormatConflict("规则库已变化，请刷新列表")
        if check_write:
            check_write(conn)
        if delete:
            conn.execute("DELETE FROM recognition_format_rules WHERE id=? AND revision=?", (rule_id, revision))
            result = {"deleted": True}
        else:
            conn.execute("UPDATE recognition_format_rules SET disabled=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                         (int(value["disabled"]), db.now(), rule_id, revision))
            result = {"item": _row(conn.execute("SELECT * FROM recognition_format_rules WHERE id=?", (rule_id,)).fetchone())}
    invalidate_cache()
    return result
