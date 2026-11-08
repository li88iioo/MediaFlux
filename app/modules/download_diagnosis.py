"""qBittorrent 实时队列的公共只读诊断；SDK 与工作区共用同一快照规则。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app import config
from app.clients.qbittorrent import TorrentTask, TransferInfo, close_qbittorrent_client
from app.logger import get_logger
from app.modules.qb_control import configured_qb_client
from app.repositories.download_requests import safe_download_text

logger = get_logger(__name__)
_MAX_ATTENTION_TASKS = 20
_ALLOWED_STATES = {
    "downloading", "stalledDL", "stalledUP", "uploading", "pausedDL", "pausedUP",
    "queuedDL", "queuedUP", "checkingDL", "checkingUP", "forcedDL", "forcedUP",
    "metaDL", "forcedMetaDL", "error", "missingFiles", "moving", "allocating",
    "checkingResumeData", "stoppedDL", "stoppedUP", "unknown",
}


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
        return (
            "attention",
            ["zero_download_speed"],
            "当前快照下载速度为零；尚不能判定持续停滞。",
        )
    if state in {"downloading", "forcedDL"} and progress < 100:
        return "downloading", [], "任务正在下载。"
    if progress >= 100 or state in {
        "uploading",
        "stalledUP",
        "forcedUP",
        "queuedUP",
        "pausedUP",
        "stoppedUP",
    }:
        return "completed", [], "任务已完成下载。"
    if state == "unknown":
        return "other", ["unknown_state"], "下载器返回了暂未识别的任务状态。"
    return "other", ["inconsistent_state"], "任务状态与进度组合需要进一步核对。"


def _task_projection(
    task: TorrentTask, kind: str, reasons: list[str], assessment: str
) -> dict[str, Any]:
    eta = _bounded_int(task.eta, maximum=2_147_483_647)
    return {
        "hash": task.hash,
        "name": safe_download_text(task.name),
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
    raw_status = (info.connection_status or "").strip().casefold()
    status = (
        raw_status
        if raw_status in {"connected", "disconnected", "firewalled"}
        else "unknown"
    )
    return {
        "api_state": "reachable",
        "transfer_status": status,
        "download_speed_bytes_per_sec": _bounded_int(info.dl_info_speed),
        "upload_speed_bytes_per_sec": _bounded_int(info.up_info_speed),
        "downloaded_bytes_total": _bounded_int(info.dl_info_data),
        "uploaded_bytes_total": _bounded_int(info.up_info_data),
        "dht_nodes": _bounded_int(info.dht_nodes, maximum=10_000_000),
    }


def _result(status: str, summary: str, data: dict[str, Any],
            suggestions: list[str], error: str = "") -> dict[str, Any]:
    configuration = status in {"not_configured", "incomplete"}
    return {
        "ok": status not in {"unavailable", "partial"},
        "status": status, "summary": summary, "data": data,
        "partial": status == "partial",
        "evidence": [{
            "source": "configuration" if configuration else "qbittorrent",
            "description": ("仅检查 qBittorrent 配置完整性，未公开配置值。" if configuration else
                            "尝试读取 qBittorrent 实时任务与传输摘要；成功范围见 queue_state 与 connection；未执行写操作。"),
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }],
        "suggestions": suggestions, "error": error,
    }


def diagnose_download_queue() -> dict[str, Any]:
    """读取队列与传输两个独立事实；部分失败保留已读快照，失败不能证明队列为空。"""
    values = config.get_many(("QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY"))
    readiness = "ready"
    if not values["QB_URL"].strip():
        readiness = "not_configured"
    elif not values["QB_API_KEY"].strip() and not (values["QB_USERNAME"].strip() and values["QB_PASSWORD"]):
        readiness = "incomplete"
    data: dict[str, Any] = {
        "connection": {"api_state": readiness}, "queue_state": readiness,
        "summary": {}, "attention_tasks": [], "attention_truncated": False,
        "unavailable_sources": [],
    }
    if readiness != "ready":
        summary, suggestion = (
            ("qBittorrent 尚未配置", "请先在设置页配置 qBittorrent 地址与认证信息。")
            if readiness == "not_configured" else
            ("qBittorrent 认证配置不完整", "请补充 API Key，或同时配置用户名与密码。")
        )
        return _result(readiness, summary, data, [suggestion])

    snapshots: dict[str, Any] = {}
    client = None
    try:
        client = configured_qb_client(values)
        for source, read in (("queue", client.list_torrents), ("transfer", client.get_transfer_info)):
            try:
                snapshots[source] = read()
            except Exception as exc:
                logger.warning("下载队列诊断读取失败 source=%s type=%s", source, type(exc).__name__)
    except Exception as exc:
        logger.warning("下载队列诊断连接失败 type=%s", type(exc).__name__)
    finally:
        close_qbittorrent_client(client)

    missing = [source for source in ("queue", "transfer") if source not in snapshots]
    data["unavailable_sources"] = missing
    data["queue_state"] = "unavailable" if "queue" in missing else "reachable"
    data["connection"] = (_connection_projection(snapshots["transfer"])
                          if "transfer" in snapshots else {"api_state": "unavailable"})
    if not snapshots:
        return _result("unavailable", "暂时无法读取 qBittorrent 队列", data,
                       ["请检查 qBittorrent 服务、认证信息和网络后重试。"], "下载器当前不可用。")

    counts = data["summary"]
    attention_total = 0
    if "queue" in snapshots:
        counts.update(dict.fromkeys((
            "total", "downloading", "completed", "stalled", "queued", "paused", "checking",
            "metadata", "processing", "failed", "attention", "other", "suspected_stuck",
        ), 0))
        for task in snapshots["queue"]:
            kind, reasons, assessment = _classify(task)
            counts["total"] += 1
            counts[kind] += 1
            if kind == "stalled":
                counts["suspected_stuck"] += 1
            if kind in {"failed", "stalled", "attention", "other"}:
                attention_total += 1
                if len(data["attention_tasks"]) < _MAX_ATTENTION_TASKS:
                    data["attention_tasks"].append(_task_projection(task, kind, reasons, assessment))
    data["attention_truncated"] = attention_total > _MAX_ATTENTION_TASKS
    transfer_attention = data["connection"].get("transfer_status") in {"disconnected", "firewalled", "unknown"}
    if missing:
        status, summary = "partial", "qBittorrent 诊断仅部分可用"
    elif attention_total:
        status, summary = "attention", f"下载队列有 {attention_total} 项需要关注"
    elif transfer_attention:
        status, summary = "attention", "qBittorrent API 可访问，但传输连接状态需要关注"
    else:
        status, summary = "healthy", f"下载队列状态正常，共 {counts['total']} 项任务"
    suggestions: list[str] = []
    if counts.get("failed"):
        suggestions.append("请在下载任务页检查失败或文件缺失的任务。")
    if counts.get("stalled") or counts.get("attention"):
        suggestions.append("请结合 Tracker、连接数和后续快照确认任务是否持续停滞。")
    if transfer_attention:
        suggestions.append("qBittorrent API 可访问，但传输连接受限或断开，请检查网络与监听状态。")
    if missing:
        suggestions.append("部分实时信息读取失败，请检查 qBittorrent 后重试；未读到的队列不能视为空队列。")
    return _result(status, summary, data, suggestions, "部分诊断信息不可用。" if missing else "")
