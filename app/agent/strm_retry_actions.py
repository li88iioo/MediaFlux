"""Media Agent 的 STRM 失败项受控重试动作。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any

from app import config, database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)

_ALLOWED_SCOPES = {"all", "generate", "metadata"}
_MAX_RETRY_ITEMS = 100
_STRM_CONFIRMATION_KEYS = (
    "GY_STRM_SOURCE_DIRS",
    "GY_STRM_BASE_URL",
    "STRM_ROOT",
    "STRM_VIDEO_EXTS",
    "STRM_METADATA_ENABLED",
    "STRM_METADATA_EXTS",
    "STRM_SKIP_THRESHOLD_MB",
    "STRM_NOTIFY_ENABLED",
    "GY_ORGANIZE_STRM_DETAIL_NOTIFY",
)
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def strm_failure_retry_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"scope"}:
        raise AgentToolError("strm.retry_failures 只接受 scope 参数")
    scope = arguments.get("scope", "all")
    if not isinstance(scope, str) or scope != scope.strip() or scope not in _ALLOWED_SCOPES:
        raise AgentToolError("scope 必须是 all、generate 或 metadata")
    return {"scope": scope}


def _snapshot(scope: str) -> list[dict[str, Any]]:
    action = "" if scope == "all" else scope
    rows = db.get_strm_failure_retry_snapshot(action=action, limit=_MAX_RETRY_ITEMS + 1)
    return [
        {
            "id": int(row["id"]),
            "action": str(row["action"] or ""),
            "failure_count": int(row["failure_count"] or 0),
            "retry_count": int(row["retry_count"] or 0),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
        if str(row["action"] or "") in {"generate", "metadata"}
    ]


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.modules.strm import capture_strm_retry_runtime_config

    scope = arguments["scope"]
    failures = _snapshot(scope)
    config_payload = {key: config.get(key, "") for key in _STRM_CONFIRMATION_KEYS}
    runtime_config, config_error = capture_strm_retry_runtime_config()
    payload = {
        "scope": scope,
        "failures": failures,
        "config_sha256": _stable_hash(config_payload),
        "runtime_sha256": _stable_hash(runtime_config),
        "config_error": str(config_error or ""),
    }
    return {
        "scope": scope,
        "failures": failures,
        "runtime_config": runtime_config,
        "config_error": str(config_error or ""),
        "fingerprint": _stable_hash(payload),
    }


def _breakdown(failures: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "generate": sum(item["action"] == "generate" for item in failures),
        "metadata": sum(item["action"] == "metadata" for item in failures),
    }


def _preview_strm_failure_retry(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """仅查询失败账本并生成安全摘要，不 claim、不访问云盘。"""
    failures = state["failures"]
    count = len(failures)
    if state["config_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 重试配置当前不可用",
            error="请检查 STRM 配置后重新预检。",
            suggestions=["可询问：为什么 STRM 配置不可用？"],
        )
    if count == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前范围没有可重试的 STRM 失败记录",
            error="没有可重试的失败记录。",
            suggestions=["可先询问：查看 STRM 失败状态。"],
        )
    if count > _MAX_RETRY_ITEMS:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="当前范围的失败记录超过单次安全上限",
            error="待重试记录过多，请缩小重试范围。",
            suggestions=["请改为只重试 STRM 生成失败，或只重试 STRM 元数据失败。"],
        )

    counts = _breakdown(failures)
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将重试 {count} 条 STRM 失败记录",
        data={
            "action": "strm.retry_failures",
            "scope": state["scope"],
            "selected_count": count,
            "by_action": counts,
            "effects": [
                "重新定位当前失败项对应的云端对象",
                "按现有 STRM 配置重新生成链接或伴随元数据",
                "更新本地失败账本中的重试状态与聚合计数",
            ],
            "limits": {"maximum_items": _MAX_RETRY_ITEMS},
        },
        evidence=[Evidence(
            "sqlite:strm_failures",
            "仅读取失败项的内部标识、动作类型、聚合版本计数与更新时间用于确认绑定；未返回明细、未访问网络、未扫描云盘、未执行重试。",
            _now(),
        )],
        suggestions=["确认前请先处理重复失败的根因，避免连续无效重试。"],
    )


def prepare_strm_failure_retry(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview_strm_failure_retry(arguments, state), str(state["fingerprint"])


def _safe_count(raw: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(raw.get(key, 0) or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _retry_strm_failure_records_state(state: dict[str, Any]) -> ToolResult:
    if state.get("config_error") or not isinstance(state.get("runtime_config"), dict):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="STRM 重试配置当前不可用",
            error="请检查 STRM 配置后重新预检。",
            suggestions=["可询问：为什么 STRM 配置不可用？"],
        )
    failures = state.get("failures") if isinstance(state.get("failures"), list) else []
    if not failures or len(failures) > _MAX_RETRY_ITEMS:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="待重试记录已变化，请重新预检",
            error="重试范围已变化。",
        )
    ids = [int(item["id"]) for item in failures]

    from app.modules.strm import retry_strm_failures

    raw = retry_strm_failures(
        ids,
        "agent",
        runtime_config=state.get("runtime_config") or {},
    )
    raw = raw if isinstance(raw, dict) else {}
    counts = {
        key: _safe_count(raw, key)
        for key in (
            "requested", "matched", "resolved", "failed", "missing", "stale",
            "deferred",
        )
    }
    if not bool(raw.get("ok")):
        busy = "正在运行" in str(raw.get("error") or "")
        return ToolResult(
            ok=False,
            status="conflict" if busy else "not_configured",
            summary="STRM 重试未执行" if busy else "STRM 重试配置当前不可用",
            data=counts,
            error="STRM 同步或重试任务正在运行。" if busy else "请检查 STRM 配置后重新预检。",
            suggestions=["可询问：查看 STRM 同步进度。"] if busy else ["可询问：为什么 STRM 配置不可用？"],
        )
    if counts["requested"] > 0 and counts["matched"] == 0:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="失败项已被其他任务处理，请重新查看",
            data={**counts, "scope": state["scope"]},
            error="确认后的失败项状态已变化，本次未执行重试。",
            suggestions=["可再次询问：查看 STRM 失败状态。"],
        )

    logger.info(
        "Agent STRM 失败重试完成 scope=%s requested=%s matched=%s resolved=%s failed=%s",
        state["scope"], counts["requested"], counts["matched"],
        counts["resolved"], counts["failed"],
    )
    summary = f"STRM 重试完成：已解决 {counts['resolved']} 条"
    if counts["failed"]:
        summary += f"，仍有 {counts['failed']} 条未解决"
    if counts["deferred"]:
        summary += f"，{counts['deferred']} 条因扫描未完成已保留待重试"
    changed = max(0, counts["requested"] - counts["matched"])
    if changed:
        summary += f"，{changed} 条状态已变化未重试"
    return ToolResult(
        ok=True,
        status="partial" if counts["failed"] or counts["deferred"] or changed else "completed",
        summary=summary,
        data={**counts, "scope": state["scope"]},
        evidence=[Evidence(
            "strm_retry",
            "已通过一次性确认票据重试冻结的失败项集合；仅返回聚合计数，不返回来源、对象、文件、路径或错误正文。",
            _now(),
        )],
        suggestions=["可再次询问：查看 STRM 失败状态。"],
    )


def retry_strm_failure_records_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError(
            "STRM 失败项或运行配置已变化，请重新预检",
            code="confirmation_stale",
        )
    return _retry_strm_failure_records_state(state)
