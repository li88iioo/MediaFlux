"""STRM 失败账本的安全只读分诊。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def strm_failure_triage_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("strm.triage_failures 不接受参数")
    return {}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _empty_failures() -> dict[str, Any]:
    return {
        "total": 0,
        "open": 0,
        "retrying": 0,
        "resolved": 0,
        "active_repeated": 0,
        "active_retried": 0,
        "by_action": {
            "generate": {"total": 0, "open": 0, "retrying": 0, "resolved": 0},
            "metadata": {"total": 0, "open": 0, "retrying": 0, "resolved": 0},
        },
    }


def _empty_data() -> dict[str, Any]:
    return {
        "probe_mode": "database",
        "network_accessed": False,
        "filesystem_accessed": False,
        "failures": _empty_failures(),
    }


def triage_strm_failures(_arguments: dict[str, Any]) -> ToolResult:
    try:
        raw = db.get_strm_failure_triage_summary()
    except Exception as exc:
        logger.warning("Agent STRM 失败分诊不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取 STRM 失败汇总",
            data=_empty_data(),
            evidence=[Evidence(
                "sqlite:strm_failures",
                "尝试读取 STRM 失败账本的固定聚合；未探测业务媒体文件系统、未访问网络，也未执行重试。",
                _now(),
            )],
            suggestions=["请检查本地数据库状态后重试。"],
            error="STRM 失败分诊当前不可用。",
        )

    raw = raw if isinstance(raw, dict) else {}
    raw_by_action = raw.get("by_action") if isinstance(raw.get("by_action"), dict) else {}
    failures = _empty_failures()
    for key in ("total", "open", "retrying", "resolved", "active_repeated", "active_retried"):
        failures[key] = _count(raw.get(key))
    for action in ("generate", "metadata"):
        action_raw = raw_by_action.get(action) if isinstance(raw_by_action.get(action), dict) else {}
        for key in ("total", "open", "retrying", "resolved"):
            failures["by_action"][action][key] = _count(action_raw.get(key))

    if failures["open"]:
        status = "attention"
        summary = f"STRM 当前有 {failures['open']} 条失败记录待处理"
        ok = False
    elif failures["retrying"]:
        status = "running"
        summary = f"STRM 当前有 {failures['retrying']} 条失败记录正在重试"
        ok = True
    else:
        status = "healthy"
        summary = "STRM 当前没有未关闭的失败记录"
        ok = True

    suggestions: list[str] = []
    if failures["by_action"]["generate"]["open"]:
        suggestions.append("存在 STRM 生成失败；请在管理页面核对来源对象与输出配置后再选择性重试。")
    if failures["by_action"]["metadata"]["open"]:
        suggestions.append("存在元数据处理失败；请先核对元数据服务与目录权限，再选择性重试。")
    if failures["active_repeated"]:
        suggestions.append("存在重复失败记录，建议先处理根因，避免连续无效重试。")
    if failures["retrying"] and not failures["open"]:
        suggestions.append("失败记录正在重试，请稍后再次查看分诊结果。")

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "probe_mode": "database",
            "network_accessed": False,
            "filesystem_accessed": False,
            "failures": failures,
        },
        evidence=[Evidence(
            "sqlite:strm_failures",
            "仅统计 STRM 失败状态与动作类别；未读取或返回来源、对象、文件、路径及错误正文，未访问网络或执行重试。",
            _now(),
        )],
        suggestions=suggestions,
    )
