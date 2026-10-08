"""文件名识别的 Agent 薄动作适配。

inspect 只调用内置解析器读取文本证据；教学 preview 核对草稿，save 仍须确认。
两类预览均不操作样本文件，只有明确确认的教学保存才持久化规则。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.agent.public_safety import sanitize_public_text, sanitize_resource_title
from app.modules.recognition import formats
from app.modules.scraper import extract_recognition_context, parse_release_position
from app.sensitive_data import contains_sensitive_credential

_AGENT_FIELDS = {"draft", "examples", "filenames"}


def inspect_filenames_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(arguments, dict)
        or set(arguments) - {"filenames", "parent_path"}
        or not isinstance(arguments.get("filenames"), list)
        or not 1 <= len(arguments["filenames"]) <= 100
    ):
        raise AgentToolError("请提供 1–100 个真实文件名，可附同一父目录文本；不需要模板或标注答案")

    filenames = arguments["filenames"]
    parent = arguments.get("parent_path", "")
    for value, limit in [(parent, 4096), *((name, 1024) for name in filenames)]:
        if (
            not isinstance(value, str)
            or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or contains_sensitive_credential(value)
        ):
            raise AgentToolError("文件名或父目录必须是限长文本，不能包含控制字符或凭据")
    if any(not name.strip() or "/" in name or "\\" in name for name in filenames):
        raise AgentToolError("请分别提供不含路径的文件名，目录上下文放在 parent_path")
    return {"filenames": list(filenames), "parent_path": parent}


def inspect_filenames(arguments: dict[str, Any]) -> ToolResult:
    args = inspect_filenames_arguments(arguments)
    rows = []
    for index, filename in enumerate(args["filenames"], 1):
        context = extract_recognition_context(filename, args["parent_path"])
        position = parse_release_position(filename)
        # 不把目录或已存格式的上下文推断冒充文件名自身的显式位置。
        rows.append({
            "index": index,
            "filename": filename,
            "normalized_title": sanitize_resource_title(context.normalized_title),
            "filename_title": sanitize_resource_title(context.filename_title),
            "folder_title": sanitize_resource_title(context.folder_title),
            "filename_year": context.filename_year,
            "folder_year": context.folder_year,
            "title_variants": [
                title for value in context.title_variants
                if (title := sanitize_resource_title(value))
            ],
            "release_position": position,
            "context_position": {"season": context.season, "episode": context.episode},
            "cleaned_components": {
                key: [text for value in values if (text := sanitize_resource_title(value))]
                for key, values in context.cleaned_components.items()
            },
            "unresolved_fields": [
                name for name, value in (
                    ("title", context.normalized_title if context.title_variants else ""),
                    ("season", context.season),
                    ("episode", context.episode),
                ) if value is None or value == ""
            ],
        })
    return ToolResult(
        ok=True,
        status="preview",
        summary=f"已用内置解析器只读解析 {len(rows)} 个文件名样本，未保存规则或操作文件",
        data={
            "total": len(rows),
            "rows": rows,
            "parent_context_supplied": bool(args["parent_path"].strip()),
            "tmdb_verified": False,
            "effects": [
                "仅解析用户提供的文本，不读取样本文件、不检查目录存在性，不改名、移动或保存规则。",
                "发布组季号和集号不等于 TMDB 标准季集映射；未查询或绑定 TMDB。",
                "标题与变体是解析候选，不是已核验作品或官方译名；清洗字段保留核心分类，不代表全部应删除。",
                "无法解析的季集保持空值，不补成第一季；目录上下文只反映用户提供的文本。",
            ],
        },
        evidence=[Evidence(
            source="builtin_filename_parser",
            description="实际执行现有文件名清洗/上下文解析与发布位置解析；没有使用模型猜测或教学模板。",
            collected_at=datetime.now(timezone.utc).isoformat(),
        )],
    )



def _safe(value: object, fallback: str, limit: int = 240) -> str:
    return sanitize_public_text(value, limit=limit) or fallback


def teaching_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("发布格式教学参数必须是对象")
    if set(arguments) - _AGENT_FIELDS:
        raise AgentToolError(
            "请只提供格式草稿、标注样本和待预览文件；确认信息由系统处理"
        )
    try:
        return formats.normalize_request(arguments)
    except (TypeError, ValueError) as exc:
        raise AgentToolError(_safe(str(exc), "发布格式教学参数无效")) from exc


def _resource(row: dict[str, Any]) -> dict[str, Any]:
    def point(value: dict[str, Any]) -> tuple[dict[str, Any], str]:
        title = sanitize_resource_title(value["title"], limit=180) or "未识别标题"
        season, episode = value["season"], value["episode"]
        if season is not None and episode is not None:
            label = f"第{season}季第{episode}集"
        elif episode is not None:
            label = f"第{episode}集"
        elif season is not None:
            label = f"第{season}季，集号未识别"
        else:
            label = "季集未识别"
        return {"title": title, "season": season, "episode": episode}, label

    filename = sanitize_resource_title(row["filename"], limit=255) or "未命名样本文件"
    before, before_label = point(row["before"])
    after, after_label = point(row["after"])
    return {
        # 各字段已经净化且有界；不再用工具名过滤器重扫文件名，或截掉末尾的集号。
        "title": (f"季集 {before_label} → {after_label}；标题《{before['title']}》→《{after['title']}》；"
                  f"样本文件「{filename}」"),
        "filename": filename,
        "before": before,
        "after": after,
        "status": _safe(row["status"], "未确定", 40),
        "passed": bool(row["passed"]),
    }


def _public_preview(preview: dict[str, Any], confirmation: bool = False) -> ToolResult:
    summary, examples, rows = preview["summary"], preview["examples"], preview["rows"]
    batch_total, sample_count = summary["total"], len(examples)
    display_total = batch_total or sample_count
    matched, unmatched = summary["matched"], summary["unmatched"]
    blocked, conflicts = summary["blocked"], summary["conflicts"]
    review_required = unmatched + blocked + conflicts
    scope = "仅此目录" if preview["draft"]["scope"] == "directory" else "跨目录发布范围"
    batch_effect = (f"批量核对 {batch_total} 个文件：命中 {matched} 个，未匹配 {unmatched} 个，特别篇/受保护 {blocked} 个，格式冲突 {conflicts} 个；这些文件仍走原有识别流程。"
                    if batch_total else
                    f"已核对 {sample_count} 个标注样本；未提供额外批量文件，特别篇/受保护 {blocked} 个、格式冲突 {conflicts} 个仍走原有识别流程。")
    data = {
        "resources": [_resource(row) for row in examples[:8]],
        "effects": [f"适用范围：{scope}；只影响以后识别。", batch_effect, "不移动文件、不绑定 TMDB、不偏移季集编号。"],
        "total": display_total, "count": display_total, "batch_total": batch_total,
        "sample_count": sample_count, "matched": matched, "unmatched": unmatched,
        "review_required": review_required, "can_save": preview["can_save"],
        "summary": {**summary, "total": display_total, "batch_total": batch_total, "sample_count": sample_count},
    }
    if not preview["can_save"]:
        suggestions = [_safe(item, "请核对样本和适用范围") for item in preview["warnings"][:4]] or ["请核对样本、标题、季集和适用范围后重新预览。"]
        status, text = "attention", (f"发布格式预览完成，但有 {review_required} 个批量结果需人工核对"
                                     if review_required else "发布格式预览完成，但标注样本尚未通过保存前置校验")
    elif confirmation:
        suggestions = ["请核对样本前后标题与季集；确认后才会保存规则。"]
        status, text = "confirmation_required", (f"确认后保存发布格式教学规则，批量核对 {batch_total} 个文件"
                                                  if batch_total else f"确认后保存发布格式教学规则，已核对 {sample_count} 个标注样本")
    else:
        suggestions = ["这是只读预览；如需记住此格式，请另行请求保存并确认。"]
        status, text = "preview", (f"发布格式预览完成：命中 {matched} 个，未匹配 {unmatched} 个"
                                    if batch_total else f"发布格式预览完成：已核对 {sample_count} 个标注样本")
    return ToolResult(ok=True, status=status, summary=text, data=data, model_data={
        "draft": preview["draft"], "examples": examples, "rows": rows,
        "summary": summary, "warnings": preview["warnings"], "can_save": preview["can_save"],
    }, suggestions=suggestions)


def preview_release_format(arguments: dict[str, Any]) -> ToolResult:
    return _public_preview(formats.preview(teaching_arguments(arguments)))


def prepare_release_format(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    preview = formats.preview(teaching_arguments(arguments))
    if not preview["can_save"]:
        raise AgentToolError(_safe(preview["warnings"][0] if preview["warnings"] else "发布格式预览未通过，不能创建保存确认", "发布格式预览未通过，不能创建保存确认"), code="precondition_failed")
    token = preview["preview_token"]
    if not isinstance(token, str) or not token:
        raise AgentToolError("发布格式预览未生成有效确认票据", code="precondition_failed")
    return _public_preview(preview, True), token


def save_release_format_confirmed(arguments: dict[str, Any], token: str) -> ToolResult:
    normalized = teaching_arguments(arguments)
    if not isinstance(token, str) or not token:
        raise AgentToolError("发布格式确认票据无效，请重新预检", code="confirmation_stale")
    try:
        item, created = formats.save({**normalized, "confirmed": True, "preview_token": token})
    except formats.FormatConflict as exc:
        raise AgentToolError(_safe(str(exc), "发布格式预览已失效，请重新预览"), code="confirmation_stale") from exc
    except (TypeError, ValueError) as exc:
        raise AgentToolError(_safe(str(exc), "发布格式确认无法执行"), code="precondition_failed") from exc
    disabled = bool(item["disabled"])
    enabled, duplicate = not disabled, not created
    if created:
        text = "发布格式教学规则已保存并启用" if enabled else "发布格式教学规则已保存，但当前未启用"
    elif disabled:
        text = "相同发布格式规则已存在且已停用，本次未重新启用"
    else:
        text = "相同发布格式规则已存在且保持启用，未重复创建"
    data = {"rule_type": "release_format", "rule_id": int(item["id"]), "resources": [], "effects": ["只保存以后识别使用的字段规则。", "不移动文件、不绑定 TMDB、不偏移季集编号。"],
            "total": 1, "count": 1, "created": bool(created), "duplicate": duplicate,
            "enabled": enabled, "review_required": 0,
            "summary": {"created": bool(created), "duplicate": duplicate, "enabled": enabled}}
    return ToolResult(True, "success", f"{text}（发布格式规则 #{item["id"]}）", data=data,
                      model_data={"draft": normalized["draft"], "examples": normalized["examples"], **data})
