"""qBittorrent 下载队列的只读、安全诊断。"""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any
import unicodedata

from app import config, database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.clients.qbittorrent import QBittorrentClient, TorrentTask, TransferInfo
from app.logger import get_logger

logger = get_logger(__name__)

_MAX_ATTENTION_TASKS = 20
_MAX_TITLE_LENGTH = 180
_SENSITIVE_TITLE_PATTERN = re.compile(
    r"(?:magnet:\?|ed2k://|https?://|ftp://|(?:pass(?:word|key)?|secret|token|api[_-]?key|auth(?:orization|key)?|cookie|session|uid)\s*[=:]|authorization\s*:\s*bearer\b)",
    re.IGNORECASE,
)
_ALLOWED_STATES = {
    "downloading", "stalledDL", "stalledUP", "uploading", "pausedDL", "pausedUP",
    "queuedDL", "queuedUP", "checkingDL", "checkingUP", "forcedDL", "forcedUP",
    "metaDL", "forcedMetaDL", "error", "missingFiles", "moving", "allocating",
    "checkingResumeData", "stoppedDL", "stoppedUP", "unknown",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def download_diagnosis_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    extra = set(arguments)
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    return {}


def download_request_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset({"scope", "limit"}):
        raise AgentToolError("下载请求列表参数无效")
    scope = str(arguments.get("scope") or "active").strip().lower()
    if scope not in {"active", "attention", "recent"}:
        raise AgentToolError("scope 仅支持 active、attention 或 recent")
    limit = arguments.get("limit", 12)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise AgentToolError("limit 必须是 1 到 30 的整数")
    return {"scope": scope, "limit": limit}


def _safe_request_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {
        "pending", "submitting", "submitted", "accepted", "queued", "running",
        "downloading", "completed", "success", "failed", "partial",
        "manual_review", "requires_manual", "skipped", "cancelled",
        "outcome_unknown", "not_started",
    } else "unknown"


def _safe_targets(value: Any) -> list[str]:
    raw = str(value or "").strip().lower()
    if raw == "both":
        return ["qb", "guangya"]
    normalized = re.sub(r"[+|/、，\s]+", ",", raw)
    return list(dict.fromkeys(
        item for item in (part.strip() for part in normalized.split(","))
        if item in {"qb", "guangya"}
    ))


def summarize_download_requests(arguments: dict[str, Any]) -> ToolResult:
    normalized = download_request_summaries_arguments(arguments)
    scope = normalized["scope"]
    limit = normalized["limit"]
    if scope == "active":
        rows = db.list_active_download_requests(limit=limit)
    elif scope == "attention":
        rows = db.list_download_requests_requiring_attention(limit=limit, offset=0)
    else:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM download_requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    items = []
    for row in rows:
        items.append({
            "request_number": int(row["id"]),
            "title": _safe_title(row["title"]),
            "kind": str(row["kind"] or "unknown")[:24],
            "targets": _safe_targets(row["targets"]),
            "status": _safe_request_status(row["status"]),
            "qb_status": _safe_request_status(row["qb_status"]),
            "guangya_status": _safe_request_status(row["gy_status"]),
            "organize_status": _safe_request_status(row["organize_status"]),
            "strm_status": _safe_request_status(row["strm_status"]),
            "updated_at": str(row["updated_at"] or "")[:32],
        })
    label = {"active": "进行中", "attention": "待处理", "recent": "最近"}[scope]
    return ToolResult(
        ok=True,
        status="success" if items else "empty",
        summary=f"{label}下载请求 {len(items)} 项",
        data={"scope": scope, "count": len(items), "items": items},
        evidence=[Evidence(
            "download_requests",
            "读取 MediaFlux 下载请求的 qB、光鸭、整理与 STRM 阶段摘要；未返回链接、路径、哈希或云端任务标识。",
            _now(),
        )],
        suggestions=[] if items else ["可切换 active、attention 或 recent 范围查看。"],
    )


def _bounded_int(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    return max(minimum, min(maximum, number))


def _bounded_progress(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(1.0, number)) * 100, 1)


def _safe_title(value: Any) -> str:
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not title
        or _SENSITIVE_TITLE_PATTERN.search(title)
        or title.startswith(("/", "\\"))
        or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", title)
        or re.search(r"(?:^|\s)/(?:[^/\s]+/)+", title)
    ):
        return "下载任务"
    title = "".join(" " if unicodedata.category(char).startswith("C") else char for char in title)
    title = " ".join(title.split())
    if len(title) > _MAX_TITLE_LENGTH:
        title = title[: _MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return title or "下载任务"


def _safe_state(value: Any) -> str:
    state = str(value or "").strip()
    return state if state in _ALLOWED_STATES else "unknown"


def _classify(task: TorrentTask) -> tuple[str, list[str], str]:
    state = _safe_state(task.state)
    progress = _bounded_progress(task.progress)
    speed = _bounded_int(task.dlspeed)

    if state == "missingFiles":
        return "failed", ["missing_files"], "下载器报告任务文件缺失。"
    if state == "error":
        return "failed", ["backend_error"], "下载器报告任务异常。"
    if state in {"checkingDL", "checkingUP", "checkingResumeData"}:
        return "checking", [], "下载器正在校验任务文件。"
    if state in {"metaDL", "forcedMetaDL"}:
        return "metadata", [], "任务正在获取种子元数据。"
    if state in {"moving", "allocating"}:
        return "processing", [], "下载器正在准备或移动任务文件。"
    if state == "stalledDL" and progress < 100:
        reasons = ["stalled_download"]
        if speed == 0:
            reasons.append("zero_download_speed")
        return "stalled", reasons, "当前快照疑似停滞；尚无连续无进度历史证据。"
    if state == "queuedDL" and progress < 100:
        return "queued", [], "任务当前处于下载队列等待状态。"
    if state in {"pausedDL", "stoppedDL"} and progress < 100:
        return "paused", [], "任务当前处于暂停状态。"
    if state in {"downloading", "forcedDL"} and progress < 100 and speed == 0:
        return "attention", ["zero_download_speed"], "当前快照下载速度为零；尚不能判定持续停滞。"
    if state in {"downloading", "forcedDL"} and progress < 100:
        return "downloading", [], "任务正在下载。"
    if progress >= 100 or state in {
        "uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP", "stoppedUP",
    }:
        return "completed", [], "任务已完成下载。"
    if state == "unknown":
        return "other", ["unknown_state"], "下载器返回了暂未识别的任务状态。"
    return "other", ["inconsistent_state"], "任务状态与进度组合需要进一步核对。"


def _task_projection(task: TorrentTask, kind: str, reasons: list[str], assessment: str) -> dict[str, Any]:
    eta = _bounded_int(task.eta, maximum=2_147_483_647)
    return {
        "name": _safe_title(task.name),
        "progress_percent": _bounded_progress(task.progress),
        "state": _safe_state(task.state),
        "status_kind": kind,
        "size_bytes": _bounded_int(task.size),
        "downloaded_bytes": _bounded_int(task.downloaded),
        "download_speed_bytes_per_sec": _bounded_int(task.dlspeed),
        "eta_seconds": eta if eta > 0 else None,
        "reason_codes": reasons,
        "assessment": assessment,
    }


def _connection_projection(info: TransferInfo) -> dict[str, Any]:
    raw_status = str(info.connection_status or "").strip().casefold()
    status = raw_status if raw_status in {"connected", "disconnected", "firewalled"} else "unknown"
    return {
        "api_state": "reachable",
        "transfer_status": status,
        "download_speed_bytes_per_sec": _bounded_int(info.dl_info_speed),
        "upload_speed_bytes_per_sec": _bounded_int(info.up_info_speed),
        "downloaded_bytes_total": _bounded_int(info.dl_info_data),
        "uploaded_bytes_total": _bounded_int(info.up_info_data),
        "dht_nodes": _bounded_int(info.dht_nodes, maximum=10_000_000),
    }


def _configuration_state() -> tuple[str, dict[str, str]]:
    values = {
        "url": str(config.get("QB_URL", "") or "").strip(),
        "username": str(config.get("QB_USERNAME", "") or "").strip(),
        "password": str(config.get("QB_PASSWORD", "") or ""),
        "api_key": str(config.get("QB_API_KEY", "") or "").strip(),
    }
    if not values["url"]:
        return "not_configured", values
    if not values["api_key"] and not (values["username"] and values["password"]):
        return "incomplete", values
    return "ready", values


def diagnose_download_queue(_arguments: dict[str, Any]) -> ToolResult:
    readiness, values = _configuration_state()
    if readiness == "not_configured":
        return ToolResult(
            ok=True,
            status="not_configured",
            summary="qBittorrent 尚未配置",
            data={"connection": {"api_state": "not_configured"}, "summary": {}, "attention_tasks": []},
            evidence=[Evidence("configuration", "仅检查 qBittorrent 配置完整性，未读取配置值。", _now())],
            suggestions=["请先在设置页配置 qBittorrent 地址与认证信息。"],
        )
    if readiness == "incomplete":
        return ToolResult(
            ok=True,
            status="incomplete",
            summary="qBittorrent 认证配置不完整",
            data={"connection": {"api_state": "incomplete"}, "summary": {}, "attention_tasks": []},
            evidence=[Evidence("configuration", "仅检查 qBittorrent 配置完整性，未读取配置值。", _now())],
            suggestions=["请补充 API Key，或同时配置用户名与密码。"],
        )

    try:
        client = QBittorrentClient(
            url=values["url"],
            username=values["username"],
            password=values["password"],
            api_key=values["api_key"],
            timeout=8,
        )
        tasks = client.list_torrents()
        transfer = client.get_transfer_info()
    except Exception as exc:
        logger.warning("Agent 下载队列诊断失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取 qBittorrent 队列",
            data={"connection": {"api_state": "unavailable"}, "summary": {}, "attention_tasks": []},
            evidence=[Evidence("qbittorrent", "尝试读取实时任务与传输摘要，未执行下载任务写操作。", _now())],
            suggestions=["请检查 qBittorrent 服务、认证信息和网络后重试。"],
            error="下载器当前不可用。",
        )

    counts = {
        "total": 0,
        "downloading": 0,
        "completed": 0,
        "stalled": 0,
        "queued": 0,
        "paused": 0,
        "checking": 0,
        "metadata": 0,
        "processing": 0,
        "failed": 0,
        "attention": 0,
        "other": 0,
        "suspected_stuck": 0,
    }
    attention_tasks: list[dict[str, Any]] = []
    attention_total = 0
    for task in tasks:
        kind, reasons, assessment = _classify(task)
        counts["total"] += 1
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "stalled":
            counts["suspected_stuck"] += 1
        if kind in {"failed", "stalled", "attention", "other"}:
            attention_total += 1
            if len(attention_tasks) < _MAX_ATTENTION_TASKS:
                attention_tasks.append(_task_projection(task, kind, reasons, assessment))

    connection = _connection_projection(transfer)
    transfer_attention = connection["transfer_status"] in {"disconnected", "firewalled", "unknown"}
    needs_attention = attention_total > 0 or transfer_attention
    status = "attention" if needs_attention else "healthy"
    if attention_total:
        summary = f"下载队列有 {attention_total} 项需要关注"
    elif transfer_attention:
        summary = "qBittorrent API 可访问，但传输连接状态需要关注"
    else:
        summary = f"下载队列状态正常，共 {counts['total']} 项任务"
    suggestions: list[str] = []
    if counts["failed"]:
        suggestions.append("请在下载任务页检查失败或文件缺失的任务。")
    if counts["stalled"] or counts["attention"]:
        suggestions.append("请结合 Tracker、连接数和后续快照确认任务是否持续停滞。")
    if transfer_attention:
        suggestions.append("qBittorrent API 可访问，但传输连接受限或断开，请检查网络与监听状态。")

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "connection": connection,
            "summary": counts,
            "attention_tasks": attention_tasks,
            "attention_truncated": attention_total > _MAX_ATTENTION_TASKS,
        },
        evidence=[Evidence("qbittorrent", "读取 qBittorrent 实时任务与传输摘要；未执行暂停、恢复、删除或提交。", _now())],
        suggestions=suggestions,
    )
