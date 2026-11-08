"""Agent Kernel 的统一公开文本投影。

模型上下文可以保留结构化工具结果，但 Web/TG 历史只能消费这里生成的
紧凑公开摘要，避免把内部 JSON、引用和值域细节直接暴露给用户。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.agent.model_context_budget import decode_tool_content
from app.agent.public_safety import (
    public_tool_label,
    replace_internal_identifiers,
    sanitize_public_text,
    sanitize_resource_title,
)
from app.sensitive_data import redact_sensitive_text

_CONFIRMED_RESULT_MARKER = "已确认操作的可信系统结果（不是待执行计划）："
_TARGET_LABELS = {
    "guangya": "光鸭云盘",
    "qb": "qBittorrent",
    "qbittorrent": "qBittorrent",
    "cloud": "云盘",
    "local": "本地",
}
_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("total", "请求"),
    ("succeeded", "已受理"),
    ("created", "已创建"),
    ("updated", "已更新"),
    ("review_required", "待复核"),
    ("duplicate", "已存在"),
    ("failed", "未完成"),
    ("skipped", "已跳过"),
)
_SUBMITTED_RESULT_STATUSES = frozenset({"accepted", "submitted"})
_PENDING_RESULT_STATUSES = frozenset(
    {"pending", "queued", "running", "in_progress", "retry_wait"}
)
_FS_CHANGE_ACTION_LABELS = {
    "rename": "改名",
    "move": "移动",
    "relocate": "清洗并移动",
    "copy": "复制",
    "trash": "移入回收站",
    "create_directory": "创建目录",
}
_FS_CHANGE_STATUS_LABELS = {
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "blocked": "受阻",
    "not_started": "未开始",
    "unknown": "已发起但待核验，不建议盲目重试",
}
_FS_CHANGE_REASON_LABELS = {
    "precondition_failed": "前置条件未满足",
    "dependency_failed": "依赖项未完成",
    "write_rejected": "写入被拒绝",
    "verification_pending": "写后核验未完成",
    "write_outcome_unknown": "写入结果未知",
    "execution_error": "执行异常",
    "audit_unavailable": "审计记录不可用",
    "cancelled": "已取消",
}
_MAX_FS_CHANGE_OPERATION_ITEMS = 400
_MAX_PUBLIC_FS_CHANGE_ITEMS = 8
_WARNING_RESULT_STATUSES = frozenset(
    {
        "partial",
        "degraded",
        "incomplete",
        "attention",
        "inconclusive",
        "outcome_unknown",
        "manual_review",
        "stopped",
        "cancelled",
        "not_started",
        "unknown",
    }
)


def _safe(value: object, *, limit: int = 600) -> str:
    return sanitize_public_text(value, limit=limit)


def _int_value(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def public_result_state(result: Mapping[str, Any] | None) -> str:
    """返回入口共用的结果语义，避免各处各判一套状态。"""
    value = result or {}
    status = str(value.get("status") or "").strip().casefold()
    data = value.get("data")
    background = data.get("background_job") if isinstance(data, Mapping) else None
    background_status = (
        str(background.get("status") or "").strip().casefold()
        if isinstance(background, Mapping)
        else ""
    )
    statuses = {status, background_status} - {""}
    if statuses & _WARNING_RESULT_STATUSES:
        return "warning"
    if value.get("ok") is False or status in {"failed", "error"}:
        return "failed"
    if statuses & _PENDING_RESULT_STATUSES:
        return "pending"
    if statuses & _SUBMITTED_RESULT_STATUSES:
        return "submitted"
    return "success"


def _result_icon(result: Mapping[str, Any]) -> str:
    return {
        "submitted": "📤",
        "pending": "⏳",
        "warning": "⚠️",
        "failed": "❌",
        "success": "✅",
    }[public_result_state(result)]


def _failed_item_errors(data: Mapping[str, Any]) -> list[str]:
    items = data.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    errors: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("ok") is not False:
            continue
        error = _safe(item.get("error"), limit=180)
        if error and error not in errors:
            errors.append(error)
        if len(errors) >= 3:
            break
    return errors


def candidate_result_lines(data: Mapping[str, Any]) -> list[str]:
    if data.get("source_type") != "resource_candidates" or not isinstance(data.get("items"), list):
        return []
    labels = {"submitted": "已提交", "duplicate": "已存在，未重复添加", "failed": "提交失败",
              "partial": "部分目标成功", "manual_review": "结果未知，请先核验"}
    lines = []
    for item in data["items"]:
        if not isinstance(item, Mapping):
            continue
        position = item.get("position")
        prefix = f"#{position} · " if type(position) is int else ""
        title = sanitize_resource_title(item.get("title"), limit=160) or "资源"
        status = "duplicate" if item.get("duplicate") else str(item.get("status") or "")
        text = f"{prefix}{title}：{labels.get(status, '结果未知，请先核验')}"
        target = _TARGET_LABELS.get(str(item.get("target") or ""), "两个目标" if item.get("target") == "both" else "")
        if target:
            text += f" · {target}"
        request_id = item.get("request_id")
        if type(request_id) is int and request_id > 0:
            text += f" · 下载请求 #{request_id}"
        for key, label in (("succeeded", "已提交"), ("failed", "失败目标")):
            if isinstance(item.get(key), list) and item[key]:
                names = [_TARGET_LABELS[name] for name in item[key] if isinstance(name, str) and name in _TARGET_LABELS]
                if names:
                    text += f" · {label}：{'、'.join(names)}"
        lines.append(text)
    return lines


def _operation_item_status(item: Mapping[str, Any]) -> str:
    status = str(item.get("status") or "")
    raw_actions = item.get("completed_actions")
    operation = item.get("operation")
    relocate_partial_actions = (
        status == "partial"
        and operation == "relocate"
        and raw_actions in (["move"], ["rename"])
    )
    if relocate_partial_actions and raw_actions == ["move"]:
        detail = "已移动；改名未完成"
    elif relocate_partial_actions:
        detail = "已改名；移动未完成"
    else:
        detail = _FS_CHANGE_STATUS_LABELS.get(status, "结果未知")
    if status != "completed" and not relocate_partial_actions:
        if isinstance(raw_actions, list):
            actions = dict.fromkeys(
                action for action in raw_actions
                if isinstance(action, str) and action in _FS_CHANGE_ACTION_LABELS
            )
            action_text = "、".join(_FS_CHANGE_ACTION_LABELS[action] for action in actions)
            action_text = action_text or ("无" if not raw_actions else "未知")
        else:
            action_text = "未知"
        detail += f"；已确认动作：{action_text}"
    reason = _FS_CHANGE_REASON_LABELS.get(str(item.get("reason") or ""))
    return f"{detail}；原因：{reason}" if reason else detail


def _operation_items_lines(
    data: Mapping[str, Any], *, result_status: str = ""
) -> list[str] | None:
    """渲染通用文件变更回执；None 表示旧结果没有该 DTO。"""
    if "operation_items" not in data:
        return None
    raw_items = data.get("operation_items")
    if not isinstance(raw_items, list):
        return ["- 计划结果：未知（逐项结果格式无效，不能确认完成情况）"]
    stats = data.get("stats")
    total = _int_value(data.get("total"))
    if total is None and isinstance(stats, Mapping):
        total = _int_value(stats.get("total"))
    items = raw_items[:_MAX_FS_CHANGE_OPERATION_ITEMS]
    completed = sum(
        isinstance(item, Mapping) and item.get("status") == "completed"
        for item in items
    )
    count = len(raw_items)
    if total is None:
        summary = f"- 计划结果：已确认完成 {completed} 项；计划总数未知，整体状态未知。"
    elif count == total and count <= _MAX_FS_CHANGE_OPERATION_ITEMS:
        summary = f"- 计划结果：已完成 {completed}/总 {total} 项"
    elif count < total:
        summary = (
            f"- 计划结果：已确认完成 {completed}/总 {total} 项；另有 "
            f"{total - len(items)} 项结果未核对，状态未知。"
        )
    else:
        summary = (
            f"- 计划结果：已确认完成 {completed}/总 {total} 项；"
            "逐项结果数量与计划不一致，汇总未知。"
        )

    remaining = [
        item for item in items
        if not isinstance(item, Mapping) or item.get("status") != "completed"
    ]
    all_completed = total is not None and count == total and completed == total
    completed_items = [
        item for item in items
        if isinstance(item, Mapping) and item.get("status") == "completed"
    ]
    prioritized = remaining
    if result_status == "partial" and total is not None and total <= _MAX_PUBLIC_FS_CHANGE_ITEMS and not all_completed:
        prioritized = [*remaining, *completed_items]
    shown = prioritized[:_MAX_PUBLIC_FS_CHANGE_ITEMS]
    lines = [summary]
    for raw_item in shown:
        item = raw_item if isinstance(raw_item, Mapping) else {}
        position = item.get("position")
        position = f"#{position}" if type(position) is int and position > 0 else "#?"
        label = sanitize_resource_title(item.get("label"), limit=180) or "文件变更项"
        lines.append(f"- {position} · {label}：{_operation_item_status(item)}")

    omitted = len(prioritized) - len(shown) + max(0, len(raw_items) - len(items))
    if omitted:
        lines.append(f"- 另有 {omitted} 项剩余或未知结果未展示。")
    return lines


def format_public_result(
    value: Mapping[str, Any] | None,
    *,
    fallback: str = "操作已结束。",
) -> str:
    """把公开 ToolResult 压缩为适合 Web/TG 展示的 Markdown。"""
    result = dict(value or {})
    summary = (_safe(result.get("summary"), limit=700)
               or _safe(result.get("message"), limit=700)
               or _safe(fallback, limit=700) or "操作已结束。")
    state = public_result_state(result)
    lines = [f"{_result_icon(result)} {summary}"]
    if state == "submitted":
        lines.append("- 状态：请求已提交，后台任务尚未完成；可以继续查询进度。")
    elif state == "pending":
        lines.append("- 状态：后台任务尚未完成")
    data = result.get("data")
    if isinstance(data, Mapping):
        operation_item_lines = _operation_items_lines(
            data, result_status=str(result.get("status") or "")
        )
        target = _safe(data.get("target"), limit=40).lower()
        if target:
            lines.append(f"- 目标：{_TARGET_LABELS.get(target, target)}")
        for key, label in _COUNT_FIELDS:
            if key == "total" and operation_item_lines is not None:
                continue
            count = _int_value(data.get(key))
            if count is not None:
                lines.append(f"- {label}：{count} 项")
        operation_ref = str(data.get("operation_ref") or "").strip().upper()
        if re.fullmatch(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}", operation_ref):
            lines.append(f"- 操作编号：{operation_ref}")
        stats = data.get("stats")
        if isinstance(stats, Mapping):
            counts = [f"{label} {count} 项" for key, label in (("renamed", "改名"), ("moved", "移动"), ("relocated", "清洗并移动"), ("copied", "复制"), ("created", "创建"), ("trashed", "回收"), ("skipped", "跳过"), ("failed", "失败")) if (count := _int_value(stats.get(key))) is not None and count > 0]
            if counts:
                lines.append("- 变更统计：" + "；".join(counts))
            if stats.get("strm_scope_unknown"):
                lines.append("- 提示：同步范围未能确认，本次未触发 STRM 联动；请核对同步目录。")
        scope_note = _safe(data.get("scope_note"), limit=300)
        if scope_note:
            lines.append(f"- 执行范围：{scope_note}")
        if operation_item_lines is not None:
            lines.extend(operation_item_lines)
        else:
            lines.extend(f"- {line}" for line in candidate_result_lines(data))
            for error in _failed_item_errors(data):
                lines.append(f"- 失败原因：{error}")

    error = _safe(result.get("error"), limit=300)
    if error and error != summary and error not in "\n".join(lines):
        lines.append(f"- 说明：{error}")
    return "\n".join(lines)


def _file_change_evidence(
    result: Mapping[str, Any] | None,
    results: Sequence[tuple[str, Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    """Validate one complete operation snapshot; labels/summary prose are not facts."""
    snapshots = []
    for value in ([result] if result is not None else []) + [v for _, v in results]:
        data = value.get("data")
        if not isinstance(data, Mapping):
            continue
        task = data.get("task")
        dto = data if "operation_items" in data else task
        if not isinstance(dto, Mapping) or "operation_items" not in dto:
            continue
        if any(v.get("truncated") or v.get("has_more") for v in (value, data, dto)):
            return None
        if dto not in snapshots:
            snapshots.append(dto)
    if len(snapshots) != 1:
        return None
    dto = snapshots[0]
    rows = dto.get("operation_items")
    stats = dto.get("stats")
    total = dto.get("total", stats.get("total") if isinstance(stats, Mapping) else None)
    if ("total" in dto and isinstance(stats, Mapping) and "total" in stats
            and (type(stats["total"]) is not int or stats["total"] != total)):
        return None
    if (type(total) is not int or not 0 < total <= _MAX_FS_CHANGE_OPERATION_ITEMS
            or not isinstance(rows, list) or len(rows) != total):
        return None
    positions = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        pos = row.get("position")
        if (type(pos) is not int or not 1 <= pos <= total or pos in positions
                or not isinstance(row.get("operation"), str)
                or row.get("operation") not in _FS_CHANGE_ACTION_LABELS
                or not isinstance(row.get("status"), str)
                or row.get("status") not in _FS_CHANGE_STATUS_LABELS):
            return None
        positions.add(pos)
    counts = {status: sum(row["status"] == status for row in rows) for status in _FS_CHANGE_STATUS_LABELS}
    return {"total": total, "unconfirmed": total - counts["completed"], **counts,
            "reasons": {row["position"]: (_FS_CHANGE_STATUS_LABELS[row["status"]],
                        _FS_CHANGE_REASON_LABELS.get(str(row.get("reason") or ""))) for row in rows}}


def _download_progress_request(
    result: Mapping[str, Any] | None,
    results: Sequence[tuple[str, Mapping[str, Any]]],
) -> int | None:
    """Return a same-request, unambiguous local downloading-stage observation."""
    if result is None or result.get("ok") is not True:
        return None
    data = result.get("data")
    if not isinstance(data, Mapping):
        return None
    number = data.get("request_id") or data.get("request_number")
    if (type(number) is not int or number <= 0
            or data.get("source_type") != "direct_url" or data.get("target") != "qb"
            or data.get("status") != "submitted"
            or public_result_state(result) != "submitted"):
        return None
    snapshots = []
    for _, value in results:
        observed = value.get("data")
        if not isinstance(observed, Mapping):
            continue
        request = observed.get("request")
        activities, stages = observed.get("activities"), observed.get("stages")
        timeline = (observed.get("kind") == "download" and isinstance(activities, list) and isinstance(stages, list)
                    and activities[:1] == [{"kind": "download", "id": number}] and type(activities[0]["id"]) is int)
        if timeline:
            stages = {item.get("stage"): item.get("status") for item in stages
                      if isinstance(item, Mapping) and not item.get("historical")}
            request = {"request_number": number, "status": stages.get("下载请求"),
                       "targets": ["qb"] if "qB 下载" in stages and "光鸭下载" not in stages else [],
                       "qb_status": stages.get("qB 下载"), "organize_status": stages.get("光鸭整理"),
                       "strm_status": stages.get("STRM")}
        if not isinstance(request, Mapping) or request.get("request_number") != number:
            continue
        if (type(request.get("request_number")) is not int
                or value.get("ok") is not True or value.get("status") != ("in_progress" if timeline else "success")
                or any(v.get("truncated") or v.get("has_more") for v in (value, observed, request))):
            return None
        if request not in snapshots:
            snapshots.append(request)
    if len(snapshots) != 1:
        return None
    request = snapshots[0]
    if (request.get("targets") != ["qb"] or request.get("status") != "downloading"
            or request.get("qb_status") != "downloading"
            or request.get("organize_status") != "pending" or request.get("strm_status") != "pending"):
        return None
    return number


# 完成、运行状态和否定判断共享同一组操作词，避免各自漏掉创建/发送等动作。
_OPERATION = r"(?:创建|生成|保存|发送|分享|上传|下载|删除|清空|移动|复制|改名|重命名|归档|入库|同步|启用|停用|修改|更新|订阅|执行|提交|写入|操作|处理|整理)"
_COMPLETION = r"(?:完成|成功|完毕|搞定|好了|就绪)"
_EN_COMPLETION = r"(?:created|generated|saved|sent|shared|uploaded|downloaded|deleted|cleared|moved|copied|renamed|archived|synced|enabled|disabled|updated|subscribed|executed)"
_COMPLETION_CLAIM = re.compile(
    rf"^(?:任务)?已(?:经)?({_COMPLETION})(?=[，。！？,.!]|$)|"
    rf"(?:全部|所有|一切|整批|都).{{0,12}}({_COMPLETION})|"
    rf"{_OPERATION}(?:步骤)?(?:\s+[A-Za-z0-9#-]+)?\s*(?:已(?:经)?)?(?:成功)?({_COMPLETION})|"
    rf"已(?:经)?(?:成功)?({_OPERATION})|(?<![\w.-])({_EN_COMPLETION})(?![\w-]|\.\w)|"
    r"\b(?:all (?:writes|tasks).{0,12}|the write is )(finished|done|completed?)\b|"
    r"\b(nothing remains to do)\b",
    re.IGNORECASE,
)
_STATE_CLAIM = re.compile(
    rf"请求\s*#?\d+|(?:已|正在|仍|当前|目前|状态|处于)[^，,；;。.!?]{{0,12}}(?:受理|{_OPERATION})|"
    r"(?:文件变更|第\d+项)|(?:完成(?:了)?|剩余|还剩)\s*\d+\s*(?:项|个)|"
    r"\b(?:request\s*#?\d+|submitted|downloading|executing)\b", re.IGNORECASE,
)
_REPLAY_CLAIM = re.compile(r"(?:允许|可以|无需确认|不必确认).{0,8}(?:重放|重新提交|自动执行)|\b(?:replay|resubmit) (?:automatically|without confirmation)\b", re.IGNORECASE)
_EXPLANATION = re.compile(r"说明|意味着|原因|建议|如果|若|需要|应当|不应|不能把|不代表|不能确认|无法确认|不同阶段|尚未|未能|没有|\b(?:means|recommend|suggest|if|should|need)\b", re.IGNORECASE)
_NEGATED_COMPLETION = re.compile(
    rf"(?:(?:尚不能|不能|无法|尚未|未能|未|没有)(?:确认|证明)?(?:已(?:经)?)?|不代表(?:已(?:经)?)?)"
    rf"(?:全部|完全)?(?:{_OPERATION})?$|(?:不能|无法|尚未|未能|没有)(?:确认|证明).*$|不代表.*$|"
    r"\b(?:not|never)(?: yet| fully| been| already)*\s*$", re.IGNORECASE,
)
_CONDITIONAL_COMPLETION = re.compile(
    r"^\s*(?:如果|若|假如|一旦|假设|只有|(?:if|when|once)\b)|"
    r"\b(?:will|would|can|could|should|must|may|might)(?: already)?(?: be| have been)\s*$",
    re.IGNORECASE,
)


def _asserted_completion(clause: str) -> bool:
    for match in _COMPLETION_CLAIM.finditer(clause):
        # 最后命中的捕获组就是动作/完成谓词，不再逐词二次扫描同一匹配。
        position = match.start(match.lastindex or 0)
        prefix = re.split(r"[，,；;。.!?]|但是|但|然而|不过|却|\b(?:but|however)\b", clause[:position], flags=re.IGNORECASE)[-1]
        if not (_NEGATED_COMPLETION.search(prefix) or _CONDITIONAL_COMPLETION.search(prefix)):
            return True
    return False

_FILE_COUNTS = {
    "total": re.compile(r"(?:总共|总计|合计|一共|共有|共)\s*(\d+)\s*(?:项|个)"),
    "completed": re.compile(r"(?:已(?:经)?完成(?:了)?|完成了)\s*(\d+)\s*(?:项|个)|(\d+)\s*(?:项|个)(?:已(?:经)?)完成"),
    "unconfirmed": re.compile(r"(\d+)\s*(?:项|个)(?:仍)?未确认完成"),
    **{status: re.compile(r"(\d+)\s*(?:项|个)" + label) for status, label in (
        ("failed", "失败"), ("blocked", "受阻"), ("not_started", "未开始"), ("unknown", "结果未知"))},
}


def _file_change_clause(clause: str, evidence: Mapping[str, Any] | None) -> bool:
    if evidence is None or re.match(r"(?:标签|摘要|引用|引文|summary|label)\s*[:：]", clause, re.I):
        return False
    # 明细事实不能推出整批成功；总任务状态仍以对应回执为准。
    if re.search(r"全部|所有|整批|(?:都|均).{0,12}(?:完成|成功|好了)|故.*完成", clause):
        return False
    position = re.fullmatch(r"第(\d+)项(?:因|由于)?(.*?)(?:而)?(受阻|失败|未开始)[。！]?", clause)
    if position:
        state, reason = evidence["reasons"].get(int(position[1]), (None, None))
        return bool(reason and reason in position[2] and state == position[3]
                    and not any(label in position[2] and label != reason for label in _FS_CHANGE_REASON_LABELS.values()))
    covered = []
    for field, pattern in _FILE_COUNTS.items():
        for match in pattern.finditer(clause):
            value = next(value for value in match.groups() if value is not None)
            if int(value) != evidence[field]:
                return False
            covered.append(match.span())
    return bool(covered) and all(any(start <= match.start() and match.end() <= end for start, end in covered)
                                for match in re.finditer(r"\d+", clause))


def _without_internal_receipts(text: str, *, keep_incomplete_prefix: bool = True) -> str:
    while _CONFIRMED_RESULT_MARKER in text:
        prefix, _, suffix = text.partition(_CONFIRMED_RESULT_MARKER)
        try:
            _, end = json.JSONDecoder().raw_decode(suffix.lstrip())
        except ValueError:
            return prefix if keep_incomplete_prefix else ""
        text = prefix + suffix.lstrip()[end:]
    return text


def _model_analysis(content, result, results, *, write_attempted=False, has_errors=False) -> str:
    completed = result is not None and public_result_state(result) == "success" and not has_errors
    text = _without_internal_receipts(str(content or "").replace("\x00", ""), keep_incomplete_prefix=not completed)
    # 正常成功答复本来就保留完整正文（含用户可见媒体路径/链接），不因修复降级投影而回归。
    if completed:
        return text.strip()
    file_evidence = _file_change_evidence(result, results)
    request = _download_progress_request(result, results)
    locked = write_attempted or result is not None
    kept = []
    # 句末闭括号/引号/Markdown闭标记随所属句处理，不留下孤立的格式标记。
    for raw in re.findall(r"[^\n]+?(?:[。！？][）)\]”’\"*`_]*|[.!?][）)\]”’\"*`_]*(?=\s|$)|(?=\n|$))|\n", text):
        if not raw:
            continue
        if not raw.strip():
            kept.append(raw)
            continue
        clause = raw.strip()
        if (clause.count("`") % 2 or re.fullmatch(r"(?:已开始处理|正在整理结果|处理中)[。.!…]*", clause)
                or re.search(r"忽略(?:此前|之前|以上)结果", clause)):
            continue
        safe = sanitize_public_text(clause, limit=max(1, len(clause) * 3), preserve_format=True)
        # 展示保留排版，判定必须使用相同安全边界的解码/NFKC文本，不能被全角或转义绕过。
        canonical = replace_internal_identifiers(safe)
        if (not canonical or canonical.strip(" #*`，。,.!?！？…") in {"内部检查", "内部状态"}
                or _REPLAY_CLAIM.search(canonical)):
            continue
        semantic = re.sub(r"《[^》]*》|`[^`\n]+\.[A-Za-z0-9]{1,12}`|[*`]", "", canonical).lstrip("#- ")
        proven = _file_change_clause(semantic, file_evidence)
        # 绑定同一请求的实际下载阶段；普通解释不必与某一句模板逐字相等。
        progress = bool(request and re.search(rf"请求\s*#?{request}(?!\d)", semantic)
                        and re.search(r"正在下载|下载中|下载阶段", semantic)
                        and not re.search(r"删除|清空|改名|移动|复制", semantic)
                        and re.findall(r"\d+", semantic) == [str(request)])
        unverified_state = (locked and _STATE_CLAIM.search(semantic) and not (proven or progress)
                            and (not _EXPLANATION.search(semantic)
                                 or re.search(r"请求\s*#?\d+|文件变更|第\d+项", semantic)))
        if unverified_state or (_asserted_completion(semantic) and not proven):
            continue
        kept.append(raw if safe == clause else raw.replace(clause, safe))
    return "".join(kept).strip()


def format_partial_progress(
    reason: str, results: Sequence[tuple[str, Mapping[str, Any]]], *,
    confirmed_result: Mapping[str, Any] | None = None, write_attempted: bool = False,
    current_errors: Iterable[object] | None = None, analysis_text: str = "",
) -> str:
    """统一降级投影：查询交付已有内容，实际涉及写入才显示执行警示。"""
    lines = ["部分完成：" + _safe(reason, limit=300)]
    pending_errors = tuple(current_errors) if current_errors is not None else ()
    errors = list(dict.fromkeys(
        text for error in pending_errors if (text := _safe(error, limit=200))
    ))
    analysis = _model_analysis(analysis_text, confirmed_result, results,
        write_attempted=write_attempted, has_errors=bool(pending_errors))
    # 同一规范比较整行；只移除重复状态，保留同一行附带的其他事实。
    canonical_status = replace_internal_identifiers(lines[0])
    analysis = "\n".join(
        line for line in analysis.splitlines() if replace_internal_identifiers(line) != canonical_status
    ).strip()
    # 工作区回执证明命令结束，不证明业务查询完成；原输出仍留在模型历史和执行记录中。
    results = [(label, result) for label, result in results
               if result.get("_tool_name") != "agent.run_code" or result.get("ok") is False]
    if write_attempted or confirmed_result is not None:
        if confirmed_result is not None:
            lines.extend(("", format_public_result(confirmed_result)))
        if analysis:
            lines.extend(("", analysis))
        elif results:
            lines.append("\n本轮工具核对结果（不代表后续操作已完成）：")
            for label, result in results[-8:]:
                lines.append(f"• {_safe(label, limit=100)}：\n{format_public_result(result)}")
            if len(results) > 8:
                lines.append(f"以上为最近 8 项；本轮共记录 {len(results)} 项工具结果。")
        if errors:
            lines.append("\n未完成的查询：" + "；".join(errors))
        lines.append("\n后续写操作尚未执行；已确认操作以以上回执为准。" if confirmed_result is not None
                     else "\n本轮未执行新的写操作；检查或搜索成功不等于变更已提交。")
        lines.append("可稍后继续处理未完成部分；新的写操作仍需确认，不会自动重放。")
        return "\n".join(lines)
    if analysis:
        lines.extend(("", analysis))
    items: dict[tuple[str, str, str], str] = {}
    summaries = []
    # 投影调用方 ledger 的当前错误；历史抑制只信任其可靠清错类别，不在这里推断恢复。
    for _label, result in results:
        if result.get("ok") is False or result.get("status") in {"rate_limited", "failed", "error"}:
            # 标记不含按工具名清除的参数错误及未登记失败；它们与确认/写入分支仍展示历史。
            if current_errors is not None and result.get("_recovery_tracked"):
                continue
            error = _safe(result.get("error") or result.get("summary"), limit=200)
            if error and error not in errors:
                errors.append(error)
            continue
        data = result.get("data")
        rows = data.get("items", []) if isinstance(data, Mapping) else []
        for item in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(item, Mapping):
                continue
            title = _safe(item.get("title") or item.get("series_name") or item.get("name"), limit=160)
            if not title:
                continue
            year = _safe(item.get("year"), limit=8)
            media_types: dict[object, str] = {"movie": "电影", "Movie": "电影", "tv": "剧集", "Series": "剧集", "Episode": "单集"}
            media_type = media_types.get(item.get("media_type") or item.get("type"), "")
            if media_type == "单集":
                season, episode = _int_value(item.get("season_number")), _int_value(item.get("episode_number"))
                position = ((f"S{season:02}" if season is not None and season >= 0 else "") + f"E{episode:02}"
                            if episode is not None and episode >= 0 else _safe(item.get("name"), limit=160))
                if position and position != title:
                    title += " · " + position
            key = (title, year, media_type)
            overview = _safe(item.get("overview"), limit=180)
            suffix = " · ".join(value for value in (year, media_type) if value)
            items.setdefault(key, title + (f"（{suffix}）" if suffix else "") + (f"：{overview}" if overview else ""))
        summary = _safe(result.get("summary"), limit=300)
        if summary and summary not in summaries:
            summaries.append(summary)
    if items and (not analysis or not any(title in analysis for title, _year, _kind in items)):
        lines.append("\n已取得的部分内容（尚未完成全部筛选/核对）：")
        lines.extend(f"{index}. {text}" for index, text in enumerate(list(items.values())[:12], 1))
        if len(items) > 12:
            lines.append(f"本轮共取得 {len(items)} 项去重内容，此处展示前 12 项。")
    elif not items:
        # 只逐条去重已被分析覆盖的公开摘要，未提及的成功事实仍需交付。
        canonical_analysis = replace_internal_identifiers(analysis)
        summaries = [text for text in summaries if text not in canonical_analysis]
        if summaries:
            lines.append("\n已完成的查询：")
            lines.extend(f"• {text}" for text in summaries[-8:])
        elif not analysis:
            lines.append("\n本轮尚未形成可交付的答复。")
    # 状态行仍在最前；只移除末尾实际展示的重复详情，未展示的首要阻塞留在首行。
    for error in errors[-3:]:
        if error != lines[0].removeprefix("部分完成："):
            lines[0] = lines[0].replace(error, "").rstrip(" ：:；;")
    if errors:
        lines.append("\n未完成的查询：" + "；".join(errors[-3:]))
    lines.append("\n未完成的查询不能据此判断没有内容。")
    return "\n".join(lines)


def sanitize_confirmed_answer(
    content: object, result: Mapping[str, Any] | None = None, *,
    results: Sequence[tuple[str, Mapping[str, Any]]] = (),
) -> str:
    """状态来自真实回执，正文只做句级清理；无正文才用工具摘要兜底。"""
    text = str(content or "").replace("\x00", "").strip()
    if result is None:
        return "⚠️ 执行结果尚未确认，请先查询实际业务状态，勿直接重复提交。" if _CONFIRMED_RESULT_MARKER in text else text
    receipt = format_public_result(result)
    while text.startswith(receipt):
        text = text[len(receipt):].lstrip()
    analysis = _model_analysis(text, result, results)
    if public_result_state(result) == "success":
        return analysis or receipt
    if analysis:
        receipt += "\n\n" + analysis
    followup = results if not analysis or any(value.get("ok") is False for _, value in results) else ()
    if followup:
        receipt += "\n\n后续核验（不改变上述操作状态）：\n" + "\n".join(
            f"• {_safe(label, limit=100)}：\n{format_public_result(value)}" for label, value in followup[-8:]
        )
    return receipt


def public_activity(value: object) -> dict[str, str] | None:
    """过程区只接受宿主产生的简短观察；不传播参数、脚本或 Provider 私有块。"""
    if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
        return None
    identity = value["id"][:200]
    if value.get("kind") == "reasoning":
        text = redact_sensitive_text(str(value.get("text") or ""))[:8000]
        return {"id": identity, "kind": "reasoning", "text": text} if text else None
    if value.get("kind") != "tool" or value.get("status") not in {
        "running", "success", "partial", "failed", "approval_required", "cancelled",
    }:
        return None
    return {"id": identity, "kind": "tool", "status": value["status"],
            "method": str(value.get("method") or "")[:100],
            "label": sanitize_public_text(value.get("label", ""), limit=100) or "查询项目服务",
            "summary": sanitize_public_text(value.get("summary", ""), limit=300)}


def public_activities(values: object) -> list[dict[str, str]]:
    """有界展示快照，不占模型历史；旧消息没有活动时保持为空。"""
    if not isinstance(values, list):
        return []
    result, remaining = [], 24000
    for value in reversed(values[-80:]):
        item = public_activity(value)
        if item is None:
            continue
        remaining -= sum(len(text) for text in item.values())
        if remaining < 0:
            break
        result.append(item)
    return result[::-1]


def public_active_turn_events(
    events: Sequence[Mapping[str, Any]], active_turn: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """从原journal恢复当前轮次的公开显示事件，不发布工具参数或隐藏正文。"""
    current = [event for event in events if event.get("request_id") == active_turn.get("request_id")
               and event.get("turn_id") == active_turn.get("turn_id")]
    stream_enabled = any(event.get("type") == "turn.started"
                         and event.get("payload", {}).get("stream_display_enabled") is True for event in current)
    visible = []
    for event in current:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "turn.started":
            public = {"stream_display_enabled": stream_enabled, "kind": payload.get("kind", "query")}
        elif kind == "model.started":
            public = {"round": payload.get("round", 1)}
        elif kind == "model.delta" and stream_enabled:
            public = {"round": payload.get("round", 1), "delta": payload.get("delta", "")}
        elif kind == "model.reasoning_summary.delta":
            public = {"round": payload.get("round", 1), "summary_id": payload.get("summary_id", ""),
                      "delta": redact_sensitive_text(str(payload.get("delta") or ""))[:8000]}
        elif kind in {"model.tool_call", "tool.completed", "tool.failed"}:
            name = payload.get("tool")
            if name in {"agent.capabilities", "agent.read_result"}:
                continue
            public = {"call_id": payload.get("call_id", ""), "label": public_tool_label(name)}
            result = payload.get("result")
            if isinstance(result, Mapping):
                summary = next((result[key] for key in ("summary", "message", "title", "status")
                                if isinstance(result.get(key), str) and result[key].strip()), "")
                public["result"] = {"summary": sanitize_public_text(summary, preserve_format=True)}
        elif payload.get("activity"):
            public = {}
        else:
            continue
        if payload.get("activity"):
            activity = public_activity(payload["activity"])
            if activity:
                public["activity"] = activity
        visible.append({**{key: event[key] for key in ("event_id", "sequence", "turn_id", "request_id") if key in event},
                        "type": kind, "payload": public})
    return visible


def public_conversation_messages(
    conversation: Sequence[Mapping[str, Any] | object],
    *, candidate_view: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """投影可公开恢复的对话，过滤工具中间回合和空助手消息。"""
    messages: list[dict[str, Any]] = []
    pending_tools: list[str] = []
    pending_candidate = False
    # 只有同一确认计划的最终答复才能取代中间回执；不能按相似文本跨任务折叠。
    final_effects = {
        str(item["effect_plan_id"]): index
        for index, item in enumerate(conversation)
        if isinstance(item, Mapping) and item.get("role") == "assistant"
        and item.get("effect_plan_id") and not item.get("tool_name")
        and not item.get("tool_calls") and str(item.get("content") or "").strip()
    }
    # 旧会话尚未保存plan关联：仅迁移明确的内部确认回执及同用户回合后续答复。
    # 普通工具摘要/跨用户消息绝不靠文字相似度合并，原始审计记录不修改。
    legacy_finals: dict[int, tuple[int, Mapping[str, Any]]] = {}
    legacy_receipt = None
    for index, item in enumerate(conversation):
        if not isinstance(item, Mapping):
            continue
        if item.get("role") == "user":
            legacy_receipt = None
        elif item.get("role") == "assistant" and not item.get("tool_calls"):
            if item.get("tool_name"):
                legacy_receipt = None
                _, marker, payload = str(item.get("content") or "").partition(_CONFIRMED_RESULT_MARKER)
                if marker and not item.get("effect_plan_id"):
                    try:
                        result, _ = json.JSONDecoder().raw_decode(payload.lstrip())
                        if isinstance(result, Mapping):
                            legacy_receipt = (index, result)
                    except ValueError:
                        pass
            elif legacy_receipt and str(item.get("content") or "").strip():
                legacy_finals[index] = legacy_receipt
                legacy_receipt = None
    legacy_receipt_indexes = {index for index, _ in legacy_finals.values()}
    # 候选卡只展示最后一次确认结果。同一批候选可以分次提交，不能按 ref
    # 隐藏全部历史回执；旧历史没有 plan ID，以最后一条同引用、同公开文本
    # 的回执定位卡片实际替代的消息，相同文本也只能折叠一次。
    folded_result_index = -1
    last_result = candidate_view.get("last_result") if candidate_view else None
    last_text = last_result.get("text") if isinstance(last_result, Mapping) else None
    if candidate_view is not None and isinstance(last_text, str) and last_text.strip():
        for index in range(len(conversation) - 1, -1, -1):
            item = conversation[index]
            if (
                isinstance(item, Mapping)
                and item.get("role") == "assistant"
                and item.get("candidate_result_ref") == candidate_view.get("ref")
                and str(item.get("public_content") or "").strip() == last_text.strip()
            ):
                folded_result_index = index
                break

    candidate_followup = ""
    if folded_result_index >= 0:
        row = conversation[folded_result_index]
        assert isinstance(row, Mapping)
        assert isinstance(last_text, str)
        final_index = final_effects.get(str(row.get("effect_plan_id") or ""), -1)
        if final_index < 0:
            final_index = next((i for i, (receipt_index, _) in legacy_finals.items() if receipt_index == folded_result_index), -1)
        if final_index >= 0:
            final_row = conversation[final_index]
            assert isinstance(final_row, Mapping)
            content = str(final_row.get("content") or "").strip()
            if final_index in legacy_finals:
                content = sanitize_confirmed_answer(content, legacy_finals[final_index][1])
            receipt = last_text.strip()
            before, matched, after = content.partition(receipt)
            if matched and (not before or before.endswith("\n\n")) and (not after or after.startswith("\n\n")):
                folded_result_index = final_index
                candidate_followup = "\n\n".join(part.strip() for part in (before, after) if part.strip())

    def remember_tool(value: object) -> None:
        name = str(value or "").strip()
        if name and name not in pending_tools:
            pending_tools.append(name)

    for index, item in enumerate(conversation):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip()
        if role == "user":
            if pending_candidate and candidate_view:
                messages.append({"role": "assistant", "content": "资源候选", "candidate_view": dict(candidate_view)})
            pending_candidate = False
            pending_tools.clear()
            content = str(item.get("content") or "").strip()
            if content:
                messages.append({"role": "user", "content": content})
            continue
        if role == "tool":
            if candidate_view:
                payload = decode_tool_content(str(item.get("content") or ""))
                if isinstance(payload.get("candidate_view"), dict) and payload["candidate_view"].get("ref") == candidate_view.get("ref"):
                    pending_candidate = True
                refs = payload.get("opaque_refs", [])
                if isinstance(refs, list) and any(
                    isinstance(ref, dict) and ref.get("kind") == "resource_candidates"
                    and ref.get("ref") == candidate_view.get("ref") for ref in refs
                ):
                    pending_candidate = True
            remember_tool(item.get("tool_name"))
            continue
        if role != "assistant":
            continue
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, Sequence) and not isinstance(
            tool_calls, (str, bytes, bytearray)
        ):
            for raw_call in tool_calls:
                if isinstance(raw_call, Mapping):
                    remember_tool(raw_call.get("name"))
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name:
            remember_tool(tool_name)
            if index in legacy_receipt_indexes or final_effects.get(str(item.get("effect_plan_id") or ""), -1) > index:
                continue
            content = str(item.get("public_content") or "").strip()
            if not content:
                # tool_name is persisted server metadata; only this receipt row
                # may migrate legacy embedded JSON, never arbitrary model text.
                raw = str(item.get("content") or "")
                _, marker, payload = raw.partition(_CONFIRMED_RESULT_MARKER)
                result = None
                if marker:
                    try:
                        result, _ = json.JSONDecoder().raw_decode(payload.lstrip())
                    except ValueError:
                        pass
                content = sanitize_confirmed_answer(raw, result if isinstance(result, Mapping) else None)
        else:
            content = str(item.get("content") or "").strip()
            if index in legacy_finals:
                content = sanitize_confirmed_answer(content, legacy_finals[index][1])
        if content:
            message: dict[str, Any] = {"role": "assistant", "content": content}
            activities = public_activities(item.get("activities"))
            if activities:
                message["activities"] = activities
            if pending_tools:
                message["tools"] = list(pending_tools)
                message["tool_labels"] = [
                    public_tool_label(tool_name) for tool_name in pending_tools
                ]
            if candidate_view and index == folded_result_index:
                message["candidate_result_ref"] = candidate_view["ref"]
                if candidate_followup:
                    message["candidate_followup"] = candidate_followup
            if pending_candidate and candidate_view:
                message["candidate_view"] = dict(candidate_view)
                pending_candidate = False
            messages.append(message)
            pending_tools.clear()
    if pending_candidate and candidate_view:
        messages.append({"role": "assistant", "content": "资源候选", "candidate_view": dict(candidate_view)})
    return messages
