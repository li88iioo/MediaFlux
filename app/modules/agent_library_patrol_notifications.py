"""Agent 全库缺集巡检结果变化的安全 Telegram 通知。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Any

from app.agent.library_patrol_status import validate_persisted_patrol_projection
from app.config import get
from app.notifier import NotificationAction, NotificationEvent, send_event

_FINGERPRINT_SCHEMA_VERSION = 1
_MAX_PAYLOAD_BYTES = 32_768
_MAX_LINES = 5
_PATROL_SUMMARY_CALLBACK = "agp:summary"
_PATROL_RESOURCES_CALLBACK = "agp:resources"
_ALLOWED_TELEGRAM_ID_RE = re.compile(r"^-?\d{1,24}$")


def _validated_projection(value: Any) -> dict[str, Any]:
    projection = validate_persisted_patrol_projection(value)
    if projection is None or projection["patrol_status"] not in {
        "updates_available", "up_to_date",
    }:
        raise ValueError("巡检通知投影无效")
    return projection


def build_patrol_result_fingerprint(projection: Any) -> str:
    """对业务结果生成稳定指纹，忽略日期、标题和调度器运行噪声。"""
    safe = _validated_projection(projection)
    identities = sorted(
        (
            str(item["tmdb_id"]),
            int(item["season"]),
            int(item["missing_count"]),
            tuple(int(episode) for episode in item["episode_sample"]),
        )
        for item in safe["options"]
    )
    canonical = {
        "schema": _FINGERPRINT_SCHEMA_VERSION,
        "outcome": safe["patrol_status"],
        "updates_available_count": safe["updates_available_count"],
        "missing_episode_count": safe["missing_episode_count"],
        "inconclusive_count": safe["inconclusive_count"],
        "unmapped_series_count": safe["unmapped_series_count"],
        "findings_truncated": safe["findings_truncated"],
        "options": identities,
    }
    raw = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def serialize_patrol_notification_payload(projection: Any) -> str:
    """仅序列化已验证的持久化安全投影。"""
    safe = _validated_projection(projection)
    raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(raw.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("巡检通知投影过大")
    return raw


def load_patrol_notification_payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("巡检通知载荷损坏") from exc
    return _validated_projection(value)


def _episode_text(item: dict[str, Any]) -> str:
    episodes = ", ".join(f"E{episode:02d}" for episode in item["episode_sample"])
    return f"{item['title']} · S{item['season']:02d} · {episodes}"


def build_library_patrol_event(projection: Any) -> NotificationEvent:
    """构造不含服务地址、路径、凭据和原始响应的结构化通知。"""
    safe = _validated_projection(projection)
    if safe["patrol_status"] == "up_to_date":
        return NotificationEvent(
            "Agent 全库缺集巡检已恢复正常",
            fields=(("已核对剧集", str(safe["checked_series_count"])),),
            footer="本次巡检未发现已播缺集。",
        )

    lines = tuple(_episode_text(item) for item in safe["options"][:_MAX_LINES])
    footer_parts = ["可在 Agent 中查询最近巡检结果并继续找资源。"]
    if safe["findings_truncated"] or len(safe["options"]) > _MAX_LINES:
        footer_parts.insert(0, "通知仅展示部分安全候选。")
    return NotificationEvent(
        "Agent 全库缺集巡检发现更新",
        fields=(
            ("已核对剧集", str(safe["checked_series_count"])),
            ("受影响剧集", str(safe["updates_available_count"])),
            ("已播缺集", str(safe["missing_episode_count"])),
        ),
        lines=lines,
        footer=" ".join(footer_parts),
        actions=(
            NotificationAction("查看巡检摘要", _PATROL_SUMMARY_CALLBACK),
            NotificationAction("为缺集找资源", _PATROL_RESOURCES_CALLBACK),
        ),
    )


def _patrol_actions_configured() -> bool:
    enabled = str(get("TG_AGENT_ENABLED", "0") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return False
    chat_id = str(get("TG_CHAT_ID", "") or "").strip()
    if not _ALLOWED_TELEGRAM_ID_RE.fullmatch(chat_id):
        return False
    raw_users = str(get("TG_AGENT_ALLOWED_USER_IDS", "") or "")
    return any(
        _ALLOWED_TELEGRAM_ID_RE.fullmatch(part)
        for part in re.split(r"[,;\s，；]+", raw_users.strip())
        if part
    )


def send_library_patrol_notification(projection: Any) -> bool:
    """发送安全结构化通知；Agent 不可操作时省略误导性的快捷按钮。"""
    event = build_library_patrol_event(projection)
    if event.actions and not _patrol_actions_configured():
        event = replace(event, actions=())
    return bool(send_event(event))
