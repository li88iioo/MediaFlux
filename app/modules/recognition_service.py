"""文件名解析与发布格式教学共用服务；直接使用 Web 格式服务和内置解析器。"""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
import sqlite3
from typing import Any

from app.modules.recognition import formats
from app.modules import recognition_preprocess_rules, tmdb_regex_rules
from app.modules.tmdb_regex_rules import RuleConflict
from app.modules.scraper import extract_recognition_context, parse_release_position
from app.sensitive_data import contains_sensitive_credential


_RULE_LABELS = {"preprocess_rule": "识别预处理规则", "tmdb_regex_rule": "TMDB 正则规则", "release_format": "发布格式教学规则"}


def rules(rule_type: str) -> dict[str, Any]:
    """只读持久化真实规则，不初始化种子、不使用启用缓存或展示编号。"""
    if rule_type == "preprocess_rule":
        items = recognition_preprocess_rules.list_rules(ensure_builtins=False)
    elif rule_type == "tmdb_regex_rule":
        items = tmdb_regex_rules.list_rules()
    elif rule_type == "release_format":
        items = formats.list_rules()
    else:
        raise ValueError("规则类型无效；知识条目请使用识别知识配置")
    return {"rule_type": rule_type, "items": items, "total": len(items)}


def prepare_rule_enabled(rule_type: str, rule_id: int, enabled: bool) -> dict[str, Any]:
    if type(rule_id) is not int or not 1 <= rule_id <= 2_147_483_647 or type(enabled) is not bool:
        raise ValueError("rule_id 必须是正整数，enabled 必须是布尔值")
    item = next((row for row in rules(rule_type)["items"] if row["id"] == rule_id), None)
    if item is None:
        raise ValueError("指定的识别规则不存在")
    if bool(item["disabled"]) == (not enabled):
        raise ValueError("该识别规则已经处于目标状态")
    operation = "启用" if enabled else "停用"
    return {"snapshot": {"rule_type": rule_type, "item": {k: v for k, v in item.items() if k != "updated_at"}},
            "preview": {"ok": True, "status": "confirmation_required", "summary": f"确认后将{operation}{_RULE_LABELS[rule_type]} #{rule_id}",
                        "data": {"rule_type": rule_type, "rule_id": rule_id, "current_enabled": not item["disabled"],
                                 "requested_enabled": enabled, "affected": 1,
                                 "effects": ["只修改这一条规则的启停状态，不修改内容、匹配表达式、TMDB、优先级或媒体。"]}}}


def set_rule_enabled(rule_type: str, rule_id: int, enabled: bool, *, expected_snapshot: dict,
                     check_write: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, Any]:
    """公共规则写入只沿各模块唯一更新入口；格式的注册表复验仍由 change 完成。"""
    item = expected_snapshot.get("item")
    if (rule_type not in _RULE_LABELS or expected_snapshot.get("rule_type") != rule_type or not isinstance(item, dict)
            or type(rule_id) is not int or item.get("id") != rule_id or type(enabled) is not bool
            or item.get("disabled") == (not enabled)):
        raise RuleConflict("确认范围无效，请重新预检")
    try:
        if rule_type == "release_format":
            formats.change(rule_id, {"revision": item["revision"], "disabled": not enabled},
                           expected_snapshot=item, check_write=check_write)
        else:
            module = recognition_preprocess_rules if rule_type == "preprocess_rule" else tmdb_regex_rules
            module.update_rule(rule_id, {"disabled": not enabled}, expected_snapshot=item, check_write=check_write)
    except formats.FormatConflict as exc:
        raise RuleConflict(str(exc)) from exc
    return {"ok": True, "status": "completed", "summary": f"{_RULE_LABELS[rule_type]} #{rule_id} 已{'启用' if enabled else '停用'}",
            "data": {"operation": "enable" if enabled else "disable", "rule_type": rule_type,
                     "rule_id": rule_id, "enabled": enabled, "affected": 1}}


def inspect_filenames_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(arguments, dict)
        or set(arguments) - {"filenames", "parent_path"}
        or not isinstance(arguments.get("filenames"), list)
        or not 1 <= len(arguments["filenames"]) <= 100
    ):
        raise ValueError("请提供 1–100 个真实文件名，可附同一父目录文本；不需要模板或标注答案")

    filenames = arguments["filenames"]
    parent = arguments.get("parent_path", "")
    for value, limit in [(parent, 4096), *((name, 1024) for name in filenames)]:
        if (
            not isinstance(value, str)
            or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or contains_sensitive_credential(value)
        ):
            raise ValueError("文件名或父目录必须是限长文本，不能包含控制字符或凭据")
    if any(not name.strip() or "/" in name or "\\" in name for name in filenames):
        raise ValueError("请分别提供不含路径的文件名，目录上下文放在 parent_path")
    return {"filenames": list(filenames), "parent_path": parent}


def inspect_filenames(filenames: list[str], parent_path: str = "") -> dict[str, Any]:
    """只解析用户文件名与目录文本，原样返回完整证据；展示净化由消费层负责。"""
    args = inspect_filenames_arguments({"filenames": filenames, "parent_path": parent_path})
    rows = []
    for index, filename in enumerate(args["filenames"], 1):
        context = extract_recognition_context(filename, args["parent_path"])
        position = parse_release_position(filename)
        # 不把目录或已存格式的上下文推断冒充文件名自身的显式位置。
        rows.append({
            "index": index,
            "filename": filename,
            "normalized_title": context.normalized_title,
            "filename_title": context.filename_title,
            "folder_title": context.folder_title,
            "filename_year": context.filename_year,
            "folder_year": context.folder_year,
            "title_variants": context.title_variants,
            "release_position": position,
            "context_position": {"season": context.season, "episode": context.episode},
            "cleaned_components": context.cleaned_components,
            "unresolved_fields": [
                name for name, value in (
                    ("title", context.normalized_title if context.title_variants else ""),
                    ("season", context.season),
                    ("episode", context.episode),
                ) if value is None or value == ""
            ],
        })
    return {
        "total": len(rows), "rows": rows,
        "parent_context_supplied": bool(args["parent_path"].strip()), "tmdb_verified": False,
        "effects": [
            "仅解析用户提供的文本，不读取样本文件、不检查目录存在性，不改名、移动或保存规则。",
            "发布组季号和集号不等于 TMDB 标准季集映射；未查询或绑定 TMDB。",
            "标题与变体是解析候选，不是已核验作品或官方译名；清洗字段保留核心分类，不代表全部应删除。",
            "无法解析的季集保持空值，不补成第一季；目录上下文只反映用户提供的文本。",
        ],
        "evidence": [{"source": "builtin_filename_parser",
                      "description": "实际执行现有文件名清洗/上下文解析与发布位置解析；没有使用模型猜测或教学模板。",
                      "collected_at": datetime.now(timezone.utc).isoformat()}],
    }


def teaching_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """教学只接收明确样本；确认信息由执行器持有，不接受调用者注入。"""
    if not isinstance(arguments, dict) or set(arguments) - {"draft", "examples", "filenames"}:
        raise ValueError("请只提供格式草稿、标注样本和待预览文件；确认信息由系统处理")
    if arguments.get("filenames", []) is None:
        arguments = {**arguments, "filenames": []}
    return formats.normalize_request(arguments)


def preview_release_format(draft: dict, examples: list[dict], filenames: list[str] | None = None) -> dict:
    """完整只读预览，不公开 Web 保存凭据，不创建保存确认。"""
    result = formats.preview(teaching_arguments({"draft": draft, "examples": examples,
                                               "filenames": filenames}))
    return {key: value for key, value in result.items() if key != "preview_token"}


def prepare_release_format(arguments: dict[str, Any]) -> dict:
    """复用 Web 预览，将原凭据和规范化请求交给已有 EffectPlan 冻结。"""
    request = teaching_arguments(arguments)
    preview = formats.preview(request)
    if not preview["can_save"]:
        raise ValueError(preview["warnings"][0] if preview["warnings"] else "发布格式预览未通过，不能创建保存确认")
    return {"preview": {key: value for key, value in preview.items() if key != "preview_token"},
            "snapshot": {"request": request, "preview_token": preview["preview_token"]}}


def save_release_format(arguments: dict[str, Any], *, expected_snapshot: dict) -> dict:
    """仅提交既有执行器冻结的 Web 预览；不重建预览、不重新启用重复规则。"""
    request = teaching_arguments(arguments)
    if expected_snapshot.get("request") != request:
        raise formats.FormatConflict("输入已变化，请重新预览")
    item, created = formats.save({**request, "confirmed": True,
                                  "preview_token": expected_snapshot.get("preview_token")})
    enabled = not item["disabled"]
    if created:
        text = "发布格式教学规则已保存并启用" if enabled else "发布格式教学规则已保存，但当前未启用"
    elif not enabled:
        text = "相同发布格式规则已存在且已停用，本次未重新启用"
    else:
        text = "相同发布格式规则已存在且保持启用，未重复创建"
    return {"ok": True, "status": "success", "summary": f"{text}（发布格式规则 #{item['id']}）",
            "data": {"rule_type": "release_format", "rule_id": int(item["id"]),
                     "created": created, "duplicate": not created, "enabled": enabled,
                     "resources": [], "total": 1, "count": 1, "review_required": 0,
                     "effects": ["只保存以后识别使用的字段规则。", "不移动文件、不绑定 TMDB、不偏移季集编号。"],
                     "summary": {"created": created, "duplicate": not created, "enabled": enabled}}}
