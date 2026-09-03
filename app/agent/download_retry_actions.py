"""下载待处理请求的受控重新提交：脱敏预检、状态指纹与一次性确认。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from app import config
from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.modules.download_dispatcher import (
    download_resubmit_capabilities,
    resubmit_download_request,
)

_TARGET_LABELS = {
    "qb": "qBittorrent",
    "guangya": "光鸭",
    "both": "qBittorrent 与光鸭",
}
_RUNTIME_CONFIG_KEYS = (
    "QB_URL",
    "QB_USERNAME",
    "QB_PASSWORD",
    "QB_API_KEY",
    "TG_QB_CATEGORY",
    "TG_QB_SAVE_PATH",
    "RSS_QB_CATEGORY",
    "RSS_QB_SAVE_PATH",
    "OFFLINE_MAGNET_ENABLED",
    "OFFLINE_ED2K_ENABLED",
    "OFFLINE_HTTP_ENABLED",
    "OFFLINE_TARGET_DIR",
    "OFFLINE_SECONDARY_ENABLED",
    "OFFLINE_SECONDARY_DIR",
    "OFFLINE_SECONDARY_KEYWORDS",
    "OFFLINE_EXCLUDE_KEYWORDS",
    "OFFLINE_MIN_FILE_MB",
    "OFFLINE_ALLOWED_EXTS",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def download_retry_submission_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"request_id", "target"}:
        raise AgentToolError("重新提交只接受 request_id 与 target 参数")
    request_id = arguments.get("request_id")
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
    ):
        raise AgentToolError("request_id 必须是大于 0 的整数")
    target = str(arguments.get("target") or "").strip().casefold()
    if target not in _TARGET_LABELS:
        raise AgentToolError("target 仅支持 qb、guangya 或 both")
    return {"request_id": request_id, "target": target}


def _attention_stages(row: Any) -> list[str]:
    stages: list[str] = []
    if str(_row_value(row, "status")).casefold() in {"failed", "manual_review"}:
        stages.append("请求分发")
    if str(_row_value(row, "qb_status")).casefold() in {"failed", "manual_review"}:
        stages.append("qBittorrent")
    if str(_row_value(row, "gy_status")).casefold() in {"failed", "manual_review"}:
        stages.append("光鸭")
    if str(_row_value(row, "local_import_status")).casefold() == "failed":
        stages.append("本地导入")
    organize_started = _safe_int(_row_value(row, "organize_started", 0) or 0)
    if (
        organize_started < 0
        or str(_row_value(row, "organize_status")).casefold() == "failed"
    ):
        stages.append("媒体整理")
    if str(_row_value(row, "strm_status")).casefold() == "failed":
        stages.append("STRM")
    if str(_row_value(row, "gy_staging_cleanup_status")).casefold() in {
        "retained",
        "failed",
    }:
        stages.append("光鸭暂存清理")
    return list(dict.fromkeys(stages))


def _digest(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
    else:
        payload = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    request_id = int(arguments["request_id"])
    target = str(arguments["target"])
    row = db.get_download_request(request_id)
    if row is None:
        raise AgentToolError("下载待处理记录不存在", code="precondition_failed")
    if str(_row_value(row, "attention_cleared_at")).strip():
        raise AgentToolError(
            "该下载记录已经处理，无需重新提交", code="precondition_failed"
        )
    attention_stages = _attention_stages(row)
    if not attention_stages:
        raise AgentToolError("该下载记录当前无需处理", code="precondition_failed")

    capability = download_resubmit_capabilities(row).get(target) or {}
    if not bool(capability.get("enabled")):
        raise AgentToolError(
            str(capability.get("reason") or "当前目标不可重新提交"),
            code="precondition_failed",
        )

    safe_state = {
        "request_id": request_id,
        "target": target,
        "kind": str(_row_value(row, "kind")).strip().casefold(),
        "status": str(_row_value(row, "status")).strip().casefold(),
        "qb_status": str(_row_value(row, "qb_status")).strip().casefold(),
        "gy_status": str(_row_value(row, "gy_status")).strip().casefold(),
        "local_import_status": str(_row_value(row, "local_import_status"))
        .strip()
        .casefold(),
        "organize_started": _safe_int(_row_value(row, "organize_started", 0) or 0),
        "organize_status": str(_row_value(row, "organize_status")).strip().casefold(),
        "strm_status": str(_row_value(row, "strm_status")).strip().casefold(),
        "gy_staging_cleanup_status": str(_row_value(row, "gy_staging_cleanup_status"))
        .strip()
        .casefold(),
        "attention_cleared_at": str(_row_value(row, "attention_cleared_at")).strip(),
        "updated_at": str(_row_value(row, "updated_at")).strip(),
        "attention_stages": attention_stages,
        "capability_enabled": True,
    }
    private_fingerprint_state = {
        **safe_state,
        "request_key_sha256": _digest(_row_value(row, "request_key")),
        "title_sha256": _digest(_row_value(row, "title")),
        "source_value_sha256": _digest(_row_value(row, "source_value")),
        "torrent_data_sha256": _digest(_row_value(row, "torrent_data")),
        "runtime_config_sha256": _fingerprint(
            {key: config.get(key, "") for key in _RUNTIME_CONFIG_KEYS}
        ),
    }
    safe_state["fingerprint"] = _fingerprint(private_fingerprint_state)
    return safe_state


def _preview(state: dict[str, Any]) -> ToolResult:
    target_label = _TARGET_LABELS[state["target"]]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将把下载待处理记录 #{state['request_id']} 重新提交到 {target_label}",
        data={
            "action": "retry_submission",
            "request_id": state["request_id"],
            "target": state["target"],
            "target_label": target_label,
            "kind": state["kind"],
            "status": state["status"],
            "qb_status": state["qb_status"],
            "gy_status": state["gy_status"],
            "attention_stages": list(state["attention_stages"]),
            "effects": [
                "会复制服务端保留的原始下载请求并创建一条新的提交记录。",
                "不会向 Agent、模型或前端返回下载链接、种子内容、保存路径或凭据。",
                "若提交失败，原记录会继续保留在待处理列表中。",
            ],
        },
        evidence=[
            Evidence(
                source="download_request",
                description="仅核对待处理状态与目标可用性；未返回资源地址、种子内容、路径或凭据。",
                collected_at=_now(),
            )
        ],
        suggestions=["确认前请核对目标下载服务是否在线。"],
    )


def prepare_retry_download_submission(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview(state), str(state["fingerprint"])


def retry_download_submission_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = download_retry_submission_arguments(arguments)
    try:
        state = _capture(normalized)
    except AgentToolError:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="下载待处理记录状态已变化，请重新预检",
            error="confirmation_stale",
            suggestions=["重新发起下载请求重投并确认最新状态。"],
        )
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="下载待处理记录或下载服务配置已变化，请重新预检",
            error="confirmation_stale",
            suggestions=["重新发起下载请求重投并确认最新状态。"],
        )

    result = resubmit_download_request(normalized["request_id"], normalized["target"])
    succeeded = len(result.get("succeeded") or [])
    failed = len(result.get("failed") or [])
    duplicate = bool(result.get("duplicate"))
    source_attention_preserved = bool(
        result.get("source_attention_preserved", not result.get("ok"))
    )
    safe_data = {
        "target": normalized["target"],
        "status": str(result.get("status") or "failed"),
        "created": bool(result.get("created")),
        "duplicate": duplicate,
        "succeeded": succeeded,
        "failed": failed,
        "source_attention_preserved": source_attention_preserved,
    }
    target_label = _TARGET_LABELS[normalized["target"]]
    evidence = [
        Evidence(
            source="download_dispatcher",
            description="只记录目标、成功/失败数量与原待处理记录是否保留；未返回资源地址、任务标识、路径或凭据。",
            collected_at=_now(),
        )
    ]
    if duplicate:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="同一资源已有下载请求正在处理，未创建重复任务",
            data=safe_data,
            evidence=evidence,
            error="duplicate_request",
            suggestions=["先检查下载队列或待处理列表，再决定是否重试。"],
        )
    if not bool(result.get("ok")):
        return ToolResult(
            ok=False,
            status="failed",
            summary=f"未能重新提交到 {target_label}，原记录仍保留在待处理列表",
            data=safe_data,
            evidence=evidence,
            error="download_submission_failed",
            suggestions=["检查目标下载服务状态后重新预检。"],
        )
    status = "partial" if failed else "completed"
    summary = (
        f"已部分重新提交到 {target_label}；失败目标仍保留待处理记录"
        if failed
        else f"已重新提交到 {target_label}"
    )
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=safe_data,
        evidence=evidence,
        suggestions=(
            ["可稍后检查下载队列与剩余待处理项。"]
            if failed
            else ["可在下载任务页查看后续状态。"]
        ),
    )
