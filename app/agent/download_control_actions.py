"""qBittorrent 单任务受控操作：精确定位、只读预检、一次性确认。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
import unicodedata
from typing import Any

from app import config
from app.agent.download_actions import _bounded_progress, _safe_state, _safe_title
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.clients.qbittorrent import (
    QBittorrentClient,
    TorrentTask,
    close_qbittorrent_client,
)
from app.logger import get_logger

logger = get_logger(__name__)

_MAX_TASK_NAME = 240
_ACTIONS = {
    "pause": {
        "verb": "暂停",
        "completed_summary": "下载任务已暂停",
        "effect": "停止该任务的继续下载或做种；已下载文件不会被删除。",
    },
    "resume": {
        "verb": "恢复",
        "completed_summary": "下载任务已恢复",
        "effect": "允许该任务继续下载或做种；实际速度由下载器与网络决定。",
    },
    "delete": {
        "verb": "移除",
        "completed_summary": "下载任务已从 qBittorrent 移除",
        "effect": "只移除 qBittorrent 任务，绝不会删除已经下载的文件。",
    },
}
_PAUSED_STATES = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).strip()
    return text


def download_task_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"task_name"}:
        raise AgentToolError("下载任务操作只接受 task_name 参数")
    task_name = _normalize_name(arguments.get("task_name"))
    if not task_name:
        raise AgentToolError("task_name 不能为空")
    if len(task_name) > _MAX_TASK_NAME:
        raise AgentToolError(f"task_name 最多 {_MAX_TASK_NAME} 个字符")
    if _safe_title(task_name) == "下载任务":
        raise AgentToolError("任务名称包含链接、路径或敏感内容，不能由 Agent 操作")
    return {"task_name": task_name}


def _client() -> QBittorrentClient:
    url = str(config.get("QB_URL", "") or "").strip()
    username = str(config.get("QB_USERNAME", "") or "").strip()
    password = str(config.get("QB_PASSWORD", "") or "")
    api_key = str(config.get("QB_API_KEY", "") or "").strip()
    if not url:
        raise AgentToolError("qBittorrent 尚未配置", code="precondition_failed")
    if not api_key and not (username and password):
        raise AgentToolError("qBittorrent 认证配置不完整", code="precondition_failed")
    return QBittorrentClient(
        url=url,
        username=username,
        password=password,
        api_key=api_key,
        timeout=8,
    )


def _task_snapshot(task: TorrentTask, *, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "hash": str(task.hash or "").strip().casefold(),
        "name": _normalize_name(task.name),
        "state": _safe_state(task.state),
        "progress_percent": _bounded_progress(task.progress),
    }


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _capture(arguments: dict[str, str], *, action: str) -> tuple[QBittorrentClient, TorrentTask, dict[str, Any]]:
    client = None
    try:
        client = _client()
        tasks = client.list_torrents()
        requested = _normalize_name(arguments["task_name"]).casefold()
        matches = [
            task for task in tasks
            if _normalize_name(task.name).casefold() == requested
        ]
        if not matches:
            raise AgentToolError(
                "没有找到名称完全匹配的下载任务",
                code="precondition_failed",
            )
        if len(matches) != 1:
            raise AgentToolError(
                "存在多个同名下载任务，请在下载任务页操作",
                code="precondition_failed",
            )
        task = matches[0]
        state = _safe_state(task.state)
        if action == "pause" and state in _PAUSED_STATES:
            raise AgentToolError("该下载任务已经暂停", code="precondition_failed")
        if action == "resume" and state not in _PAUSED_STATES:
            raise AgentToolError(
                "该下载任务当前不是暂停状态",
                code="precondition_failed",
            )
        return client, task, _task_snapshot(task, action=action)
    except AgentToolError:
        close_qbittorrent_client(client)
        raise
    except Exception as exc:
        close_qbittorrent_client(client)
        logger.warning(
            "Agent qB 任务预检失败 action=%s type=%s",
            action,
            type(exc).__name__,
        )
        raise AgentToolError(
            "暂时无法读取 qBittorrent 队列",
            code="precondition_failed",
        ) from exc


def _preview(snapshot: dict[str, Any], *, action: str) -> ToolResult:
    copy = _ACTIONS[action]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{copy['verb']} 1 个名称完全匹配的下载任务",
        data={
            "operation": action,
            "affected": 1,
            "task": _safe_title(snapshot["name"]),
            "state": snapshot["state"],
            "progress_percent": snapshot["progress_percent"],
            "effects": [copy["effect"]],
        },
        evidence=[Evidence(
            "qbittorrent",
            "仅核对实时任务名称、状态与进度；未返回任务 hash、保存路径、内容路径或认证信息。",
            _now(),
        )],
        suggestions=["确认票据只可使用一次；任务状态变化后需要重新预检。"],
    )


def _prepare(arguments: dict[str, str], *, action: str) -> tuple[ToolResult, str]:
    client, _task, snapshot = _capture(arguments, action=action)
    try:
        return _preview(snapshot, action=action), _fingerprint(snapshot)
    finally:
        close_qbittorrent_client(client)


def _confirmed(arguments: dict[str, str], expected_context: str, *, action: str) -> ToolResult:
    try:
        client, task, snapshot = _capture(arguments, action=action)
    except AgentToolError as exc:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="下载任务状态已变化，请重新预检",
            error=exc.safe_message,
        )
    try:
        if not secrets.compare_digest(
            _fingerprint(snapshot),
            str(expected_context or ""),
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="下载任务状态已变化，请重新预检",
                error="确认快照已失效。",
            )

        try:
            if action == "pause":
                client.pause_torrents(task.hash)
            elif action == "resume":
                client.resume_torrents(task.hash)
            else:
                # 安全边界：Agent 删除永远只移除任务，不删除数据文件。
                client.delete_torrents(task.hash, delete_files=False)
        except Exception as exc:
            logger.warning(
                "Agent qB 任务操作失败 action=%s type=%s",
                action,
                type(exc).__name__,
            )
            return ToolResult(
                ok=False,
                status="unavailable",
                summary="qBittorrent 暂时无法完成该操作",
                error="下载器操作失败，请稍后重试。",
            )

        copy = _ACTIONS[action]
        return ToolResult(
            ok=True,
            status="completed",
            summary=copy["completed_summary"],
            data={
                "operation": action,
                "affected": 1,
                "delete_files": False if action == "delete" else None,
            },
            evidence=[Evidence(
                "qbittorrent",
                "已使用一次性确认票据执行单任务操作；审计结果不包含 hash、路径或凭据。",
                _now(),
            )],
            suggestions=["可再次询问：检查下载队列状态。"],
        )
    finally:
        close_qbittorrent_client(client)

def prepare_pause_download_task(arguments: dict[str, str]) -> tuple[ToolResult, str]:
    return _prepare(arguments, action="pause")


def prepare_resume_download_task(arguments: dict[str, str]) -> tuple[ToolResult, str]:
    return _prepare(arguments, action="resume")


def prepare_delete_download_task(arguments: dict[str, str]) -> tuple[ToolResult, str]:
    return _prepare(arguments, action="delete")


def pause_download_task_confirmed(arguments: dict[str, str], expected_context: str) -> ToolResult:
    return _confirmed(arguments, expected_context, action="pause")


def resume_download_task_confirmed(arguments: dict[str, str], expected_context: str) -> ToolResult:
    return _confirmed(arguments, expected_context, action="resume")


def delete_download_task_confirmed(arguments: dict[str, str], expected_context: str) -> ToolResult:
    return _confirmed(arguments, expected_context, action="delete")
