"""Web 与 Agent 共用的 STRM 伴随元数据控制，绝不删除落盘文件。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

from app import config
from app.repositories import strm as repository

_POLICY_KEY = "STRM_METADATA_ENABLED"
_PREVIEW_TTL = 600


def metadata_status() -> dict[str, Any]:
    from app.modules.strm_metadata_worker import get_strm_metadata_worker

    raw = get_strm_metadata_worker().status()
    counts = {
        name: max(0, int(raw.get(name) or 0))
        for name in (
            "queued",
            "retry_wait",
            "running",
            "completed",
            "failed",
            "cancelled",
            "pending",
            "total",
        )
    }
    enabled = bool(raw.get("enabled"))
    worker = bool(raw.get("worker_running"))
    consumer = bool(raw.get("consumer_active"))
    breaker = max(0, int(raw.get("breaker_seconds") or 0))
    active = counts["running"] > 0
    if not enabled:
        state = "draining" if active else "paused"
    elif breaker:
        state = "backoff"
    elif active:
        state = "processing"
    elif not worker or not consumer:
        state = "waiting_worker"
    else:
        state = "waiting"
    return {
        **counts,
        "sampled_at": datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "cancellable": counts["queued"] + counts["retry_wait"],
        "enabled": enabled,
        "worker_running": worker,
        "worker_scope": "current_process",
        "consumer_active": consumer,
        "breaker_seconds": breaker,
        "state": state,
        "scope": "同步云盘中的 NFO、字幕、海报等伴随文件，不是查询演员资料或控制 Jellyfin 刮削。",
        "semantics": "running 为查询瞬间正在处理数量，不代表线程不存在或不会自动领取；线程状态仅代表本进程。关闭后不再领取，当前任务可继续完成；后续启动的扫描不再新增，已运行的扫描可能沿用旧配置继续入队。",
    }


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _signed_preview(payload: dict[str, Any]) -> str:
    from app.modules.web_secret import get_web_secret

    body = base64.urlsafe_b64encode(_encode(payload).encode()).decode().rstrip("=")
    signature = hmac.new(
        get_web_secret().encode(), ("strm-metadata:" + body).encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def _read_preview(token: str, owner: str, operation: str) -> dict[str, Any]:
    from app.modules.web_secret import get_web_secret

    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("元数据确认凭证无效，请重新预览")
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(
            get_web_secret().encode(),
            ("strm-metadata:" + body).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if (
            payload["owner"] != owner
            or payload["operation"] != operation
            or time.time() > payload["expires_at"]
        ):
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("元数据确认凭证无效或已过期，请重新预览") from exc


def prepare_backlog_cancel(owner: str) -> tuple[dict[str, Any], str]:
    snapshot = repository.capture_strm_metadata_backlog()
    if not snapshot["count"]:
        raise ValueError("当前没有可取消的排队或等待重试任务")
    view = {
        "count": snapshot["count"],
        "queued": snapshot["counts"]["queued"],
        "retry_wait": snapshot["counts"]["retry_wait"],
        "enabled": config.get_bool(_POLICY_KEY, False),
        "effects": [
            "仅将本次预览中的排队／等待重试任务标记为已取消，保留取消记录。",
            "不启动同步，不改变伴随同步开关，不中断正在处理的任务。",
            "不删除任何已落盘的 NFO、字幕、海报、STRM 或媒体文件，不影响 Jellyfin／Emby 已补全的信息。",
            "预览之后新增任务不在本次范围；以后重新开启并扫描可能重新产生待办。",
        ],
    }
    token = _signed_preview(
        {
            "operation": "cancel",
            "owner": owner,
            "snapshot": snapshot,
            "expires_at": int(time.time()) + _PREVIEW_TTL,
        }
    )
    return view, token


def cancel_backlog_confirmed(token: str, owner: str) -> dict[str, Any]:
    payload = _read_preview(token, owner, "cancel")
    count = repository.cancel_strm_metadata_backlog(payload["snapshot"])
    return {
        "cancelled": count,
        "enabled": config.get_bool(_POLICY_KEY, False),
        "files_deleted": 0,
        "summary": f"已取消 {count} 项伴随元数据待办；同步开关未改变，未删除任何文件。",
    }


def _policy_snapshot() -> tuple[dict[str, Any], bytes | None]:
    raw = config.ENV_FILE.read_bytes() if config.ENV_FILE.exists() else None
    return {
        "enabled": config.get_bool(_POLICY_KEY, False),
        "config_digest": hashlib.sha256(raw or b"").hexdigest(),
    }, raw


def prepare_policy(enabled: bool, owner: str) -> tuple[dict[str, Any], str]:
    if type(enabled) is not bool:
        raise ValueError("enabled 必须为布尔值")
    if config.has_external_override(_POLICY_KEY):
        raise ValueError("伴随同步由部署环境控制，不能在此覆盖")
    if enabled:
        from app.modules.strm import DEFAULT_METADATA_EXTS, parse_strm_extensions, validate_strm_extensions

        validate_strm_extensions(config.get("STRM_VIDEO_EXTS", ""), parse_strm_extensions(
            config.get("STRM_METADATA_EXTS", ""), defaults=DEFAULT_METADATA_EXTS,
        ))
    snapshot, _ = _policy_snapshot()
    view = {
        "before": snapshot["enabled"],
        "enabled": enabled,
        "effects": [
            "开启后消费者可继续历史积压，后续扫描可登记 NFO、字幕、海报同步任务。"
            if enabled
            else "关闭后不再领取后续任务；后续启动的同步不再新增伴随任务，但已运行的扫描可能沿用旧配置继续入队。已开始的下载可完成，积压保留。",
            "不删除任何落盘文件，不控制 Jellyfin／Emby 自身的刮削。",
        ],
    }
    return view, _signed_preview(
        {
            "operation": "policy",
            "owner": owner,
            "snapshot": snapshot,
            "enabled": enabled,
            "expires_at": int(time.time()) + _PREVIEW_TTL,
        }
    )


def set_policy_confirmed(token: str, owner: str) -> dict[str, Any]:
    payload = _read_preview(token, owner, "policy")
    snapshot, raw = _policy_snapshot()
    if snapshot != payload["snapshot"]:
        raise ValueError("同步配置已变化，请重新预览")
    enabled = payload["enabled"]
    try:
        config.update_runtime_env_file(
            config.ENV_FILE, {_POLICY_KEY: "true" if enabled else "false"}, expected=raw
        )
    except (
        config.ConcurrentConfigUpdateError,
        config.ExternalConfigOverrideError,
    ) as exc:
        raise ValueError("同步配置已变化或由部署环境控制，请重新预览") from exc
    from app.modules.strm_metadata_worker import get_strm_metadata_worker

    get_strm_metadata_worker().wake()
    actual = config.get_bool(_POLICY_KEY, False)
    verified = actual == enabled
    summary = (
        "伴随元数据同步已开启，历史积压可继续处理。"
        if enabled
        else "伴随元数据同步已关闭，保留积压；已开始任务可完成，已运行的扫描可能沿用旧配置继续入队。"
    )
    if not verified:
        summary = (
            "同步开关写入已提交，但当前运行态未与目标一致；请刷新核实，不要重复提交。"
        )
    return {"enabled": actual, "verified": verified, "summary": summary}
