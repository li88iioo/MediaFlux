"""RSS 订阅的受控管理动作：精确 ID、原子预检快照、一次性确认。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import get_logger
from app.modules.rss import rss_subscription_refresh_revision, validate_rss_source_urls
from app.modules.rss_subscription_config import (
    RSSSubscriptionConfigError,
    normalize_rss_subscription_create,
    normalize_rss_subscription_update,
    wake_rss_scheduler,
)

logger = get_logger(__name__)
_MAX_SAFE_ID = 2_147_483_647


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_subscription_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_SAFE_ID
    ):
        raise AgentToolError("subscription_id 必须是正整数")
    return value


_RSS_CONFIG_KEYS = frozenset(
    {
        "name",
        "urls",
        "exclude_keywords",
        "action",
        "enabled",
        "refresh_interval_minutes",
        "download_method",
        "media_tmdb_id",
        "media_default_season",
        "skip_existing_episodes",
    }
)
_RSS_UPDATE_KEYS = _RSS_CONFIG_KEYS
_RSS_INTERNAL_CREATE_KEYS = _RSS_CONFIG_KEYS | {
    "refresh_cron",
    "parser",
    "qb_save_path",
    "gy_target_dir",
    "gy_target_dir_name",
}


def _agent_urls(value: Any, *, allow_normalized: bool) -> str:
    if isinstance(value, list):
        raw_urls = value
    elif allow_normalized and isinstance(value, str):
        raw_urls = [line.strip() for line in value.splitlines() if line.strip()]
    else:
        raise AgentToolError("urls 必须包含 1 到 8 个订阅地址")
    if not 1 <= len(raw_urls) <= 8:
        raise AgentToolError("urls 必须包含 1 到 8 个订阅地址")
    normalized_urls: list[str] = []
    for raw_url in raw_urls:
        if (
            not isinstance(raw_url, str)
            or not raw_url.strip()
            or len(raw_url.strip()) > 1000
        ):
            raise AgentToolError("每个 RSS 订阅地址必须是长度不超过 1000 的非空字符串")
        normalized_urls.append(raw_url.strip())
    try:
        return validate_rss_source_urls("\n".join(normalized_urls))
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc


def _agent_config_payload(arguments: dict[str, Any], *, create: bool) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    normalized_create = create and isinstance(arguments.get("urls"), str)
    allowed = (
        _RSS_INTERNAL_CREATE_KEYS
        if normalized_create
        else _RSS_CONFIG_KEYS
        if create
        else _RSS_UPDATE_KEYS | {"subscription_id"}
    )
    unknown = set(arguments) - allowed
    if unknown:
        raise AgentToolError("RSS 订阅配置包含未支持字段")
    if create and not {"name", "urls"}.issubset(arguments):
        raise AgentToolError("创建 RSS 订阅需要 name 和 urls")
    if not create and set(arguments) == {"subscription_id"}:
        raise AgentToolError("至少需要修改一个 RSS 订阅字段")

    payload = {
        key: value for key, value in arguments.items() if key != "subscription_id"
    }
    if normalized_create:
        if any(
            str(payload.get(key) or "").strip()
            for key in ("qb_save_path", "gy_target_dir", "gy_target_dir_name")
        ):
            raise AgentToolError("Agent 不接受任意下载路径或云端目录标识")
        # ToolRegistry 会在预检和确认阶段各规范化一次。数据库字段使用
        # 0/1，而公开工具契约使用 JSON boolean，因此在第二次规范化前
        # 恢复为布尔值，保证 validator 幂等且不放宽外部输入类型。
        for key in ("enabled", "skip_existing_episodes"):
            if key in payload and payload[key] in (0, 1):
                payload[key] = bool(payload[key])
        payload["urls"] = _agent_urls(payload.get("urls"), allow_normalized=True)
        return payload
    if "urls" in payload:
        payload["urls"] = _agent_urls(payload["urls"], allow_normalized=not create)
    return payload


def rss_create_subscription_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = _agent_config_payload(arguments, create=True)
    try:
        fields = normalize_rss_subscription_create(payload, allow_target_paths=False)
    except RSSSubscriptionConfigError as exc:
        raise AgentToolError(str(exc)) from exc
    return fields


def rss_update_subscription_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = _agent_config_payload(arguments, create=False)
    return {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id")),
        **payload,
    }


def rss_delete_subscription_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("rss.delete_subscription 只接受 subscription_id 参数")
    return {
        "subscription_id": _strict_subscription_id(arguments.get("subscription_id"))
    }


def _snapshot(row: Any, *, operation: str, entry_count: int = 0) -> dict[str, Any]:
    if row is None:
        return {
            "operation": operation,
            "exists": False,
            "subscription_id": 0,
            "revision": "missing",
            "enabled": False,
            "refresh_interval_minutes": 0,
            "entry_count": 0,
        }
    return {
        "operation": operation,
        "exists": True,
        "subscription_id": int(row["id"]),
        # revision 是敏感配置的不可逆摘要；确认响应永远不会返回它。
        "revision": rss_subscription_refresh_revision(row),
        "enabled": bool(row["enabled"]),
        "refresh_interval_minutes": max(0, int(row["refresh_interval_minutes"] or 0)),
        "entry_count": max(0, int(entry_count or 0)),
    }


def _capture(subscription_id: int, *, operation: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM rss_items WHERE id=?", (subscription_id,)
        ).fetchone()
        count = 0
        if row is not None and operation == "delete":
            count_row = conn.execute(
                "SELECT COUNT(*) AS total FROM rss_entries WHERE rss_item_id=?",
                (subscription_id,),
            ).fetchone()
            count = int((count_row["total"] if count_row else 0) or 0)
        return _snapshot(row, operation=operation, entry_count=count)


def _fingerprint(state: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _missing() -> AgentToolError:
    return AgentToolError("未找到指定的 RSS 订阅", code="precondition_failed")


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _config_revision(row: Any) -> str:
    fields = (
        "id",
        "name",
        "enabled",
        "refresh_cron",
        "refresh_interval_minutes",
        "urls",
        "parser",
        "exclude_keywords",
        "action",
        "download_method",
        "qb_save_path",
        "gy_target_dir",
        "gy_target_dir_name",
        "media_tmdb_id",
        "media_default_season",
        "skip_existing_episodes",
        "updated_at",
    )
    return _fingerprint({key: _row_value(row, key) for key in fields})


def _changed_config_fields(current: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """只保留真实变化，避免同一配置动作生成无效确认票据。"""
    changed: dict[str, Any] = {}
    for key, value in fields.items():
        try:
            current_value = current[key]
        except (KeyError, TypeError, IndexError):
            current_value = None
        if current_value != value:
            changed[key] = value
    return changed


def _safe_config_summary(fields: dict[str, Any]) -> dict[str, Any]:
    urls = str(fields.get("urls") or "")
    return {
        "name": str(fields.get("name") or "")[:160],
        "url_count": len([line for line in urls.splitlines() if line.strip()]),
        "action": str(fields.get("action") or "subscribe"),
        "enabled": bool(fields.get("enabled", 1)),
        "refresh_interval_minutes": int(fields.get("refresh_interval_minutes") or 0),
        "download_method": str(fields.get("download_method") or ""),
        "media_tmdb_id": str(fields.get("media_tmdb_id") or ""),
        "media_default_season": int(fields.get("media_default_season", 1)),
        "skip_existing_episodes": bool(fields.get("skip_existing_episodes", 0)),
    }


def _prepare_create(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    fields = dict(arguments)
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将创建 1 个 RSS 订阅",
        data={
            "operation": "create",
            "affected": 1,
            **_safe_config_summary(fields),
            "effects": [
                "保存订阅配置并让调度器重新加载。",
                "不会立即刷新订阅，也不会自动提交历史条目。",
            ],
        },
        evidence=[
            Evidence(
                "rss_configuration",
                "已校验订阅配置；确认页不返回订阅地址、下载路径或凭据。",
                _now(),
            )
        ],
        suggestions=["确认票据只可使用一次；Agent 不接受任意下载路径或云端目录标识。"],
    )
    return preview, _fingerprint({"operation": "create", "fields": fields})


def _prepare_update(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    subscription_id = int(arguments["subscription_id"])
    current = db.get_rss_subscription(subscription_id)
    if current is None:
        raise _missing()
    payload = {
        key: value for key, value in arguments.items() if key != "subscription_id"
    }
    try:
        fields = normalize_rss_subscription_update(
            payload,
            current=current,
            allow_target_paths=False,
        )
    except RSSSubscriptionConfigError as exc:
        raise AgentToolError(str(exc)) from exc
    fields = _changed_config_fields(current, fields)
    if not fields:
        raise AgentToolError("RSS 订阅配置没有变化", code="precondition_failed")
    context = {
        "operation": "update",
        "subscription_id": subscription_id,
        "revision": _config_revision(current),
        "fields": fields,
    }
    preview_data: dict[str, Any] = {
        "operation": "update",
        "subscription_id": subscription_id,
        "affected": 1,
        "changed_fields": sorted(fields),
        "effects": [
            "保存指定配置并让调度器重新加载。",
            "不会立即刷新订阅或创建下载任务。",
        ],
    }
    if "name" in fields:
        preview_data["name"] = str(fields["name"])[:160]
    if "urls" in fields:
        preview_data["url_count"] = len(
            [line for line in str(fields["urls"]).splitlines() if line.strip()]
        )
    for key in (
        "action",
        "enabled",
        "refresh_interval_minutes",
        "download_method",
        "media_tmdb_id",
        "media_default_season",
        "skip_existing_episodes",
    ):
        if key in fields:
            preview_data[key] = fields[key]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将更新 1 个 RSS 订阅",
        data=preview_data,
        evidence=[
            Evidence(
                "rss_database",
                "已生成订阅配置快照；确认页不返回订阅地址、下载路径或凭据。",
                _now(),
            )
        ],
        suggestions=["订阅配置变化后，当前确认票据会自动失效。"],
    ), _fingerprint(context)


def _confirmed_create(arguments: dict[str, Any], expected_context: str) -> ToolResult:
    fields = dict(arguments)
    actual_context = _fingerprint({"operation": "create", "fields": fields})
    if not secrets.compare_digest(actual_context, str(expected_context or "")):
        raise AgentToolError("确认上下文与订阅配置不一致", code="confirmation_invalid")
    try:
        subscription_id = db.add_rss_subscription(**fields)
    except (TypeError, ValueError) as exc:
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="RSS 订阅创建失败",
            error=str(exc)[:240],
        )
    persisted = db.get_rss_subscription(subscription_id)
    expected_summary = _safe_config_summary(fields)
    persisted_summary = (
        _safe_config_summary(dict(persisted)) if persisted is not None else None
    )
    if persisted_summary != expected_summary:
        return ToolResult(
            ok=False,
            status="outcome_unknown",
            summary="RSS 订阅写入结果无法核验",
            data={
                "operation": "create",
                "subscription_number": subscription_id,
                "affected": 1,
                "verified": False,
            },
            error="数据库写入已返回编号，但读取结果与确认配置不一致；请刷新 RSS 页面核对后再决定是否重试。",
            suggestions=["先查看 RSS 订阅列表，避免重复创建。"],
        )
    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"RSS 订阅 #{subscription_id} 已创建并核验",
        data={
            "operation": "create",
            "subscription_id": subscription_id,
            "subscription_number": subscription_id,
            "affected": 1,
            "verified": True,
            "runtime_refreshed": runtime_refreshed,
            **persisted_summary,
        },
        evidence=[
            Evidence(
                "rss_database",
                "已使用一次性确认票据保存订阅配置；未立即刷新或创建下载任务。",
                _now(),
            )
        ],
        suggestions=(
            ["订阅已保存，调度器已重新加载。"]
            if runtime_refreshed
            else ["订阅已保存；请重启 MediaFlux 使当前进程重新加载调度。"]
        ),
    )


def _confirmed_config_update(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    payload = {
        key: value for key, value in arguments.items() if key != "subscription_id"
    }
    with db.get_conn() as conn:
        # revision 校验、主表更新和媒体绑定更新必须共享一个写事务，
        # 避免确认通过后再被并发 Web/API 配置覆盖。
        conn.execute("BEGIN IMMEDIATE")
        current = db.get_rss_subscription(subscription_id, connection=conn)
        if current is None:
            return ToolResult(
                ok=False,
                status="conflict",
                summary="RSS 订阅已不存在，请重新检查",
                error="确认快照已失效。",
            )
        try:
            fields = normalize_rss_subscription_update(
                payload,
                current=current,
                allow_target_paths=False,
            )
        except RSSSubscriptionConfigError as exc:
            raise AgentToolError(str(exc)) from exc
        fields = _changed_config_fields(current, fields)
        if not fields:
            raise AgentToolError("RSS 订阅配置没有变化", code="precondition_failed")
        context = {
            "operation": "update",
            "subscription_id": subscription_id,
            "revision": _config_revision(current),
            "fields": fields,
        }
        if not secrets.compare_digest(
            _fingerprint(context), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="RSS 订阅配置已变化，请重新预检",
                error="确认快照已失效。",
            )
        db.update_rss_subscription(subscription_id, fields, connection=conn)
    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="RSS 订阅配置已更新",
        data={
            "operation": "update",
            "subscription_id": subscription_id,
            "affected": 1,
            "changed_fields": sorted(fields),
            "changed_field_count": len(fields),
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[
            Evidence(
                "rss_database",
                "已使用一次性确认票据更新订阅配置；未立即刷新或创建下载任务。",
                _now(),
            )
        ],
        suggestions=(
            ["订阅配置已生效。"]
            if runtime_refreshed
            else ["配置已保存；请重启 MediaFlux 使当前进程重新加载调度。"]
        ),
    )


def _prepare_delete(arguments: dict[str, int]) -> tuple[ToolResult, str]:
    state = _capture(arguments["subscription_id"], operation="delete")
    if not state["exists"]:
        raise _missing()
    preview = ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将永久删除 1 个 RSS 订阅",
        data={
            "operation": "delete",
            "subscription_id": arguments["subscription_id"],
            "affected": 1,
            "deleted_entries": state["entry_count"],
            "effects": [
                "删除订阅配置及其本地条目记录。",
                "不会删除已经创建的下载任务或已下载文件。",
            ],
        },
        evidence=[
            Evidence(
                "rss_database",
                "仅统计本地关联条目数量；未返回订阅名称、地址、过滤词、条目标题或凭据。",
                _now(),
            )
        ],
        suggestions=["删除不可撤销；如只想停止定时刷新，建议改为停用订阅。"],
    )
    return preview, _fingerprint(state)


def _reload_scheduler() -> bool:
    return wake_rss_scheduler()


def _confirmed_delete(arguments: dict[str, int], expected_context: str) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM rss_items WHERE id=?", (subscription_id,)
        ).fetchone()
        count_row = conn.execute(
            "SELECT COUNT(*) AS total FROM rss_entries WHERE rss_item_id=?",
            (subscription_id,),
        ).fetchone()
        entry_count = int((count_row["total"] if count_row else 0) or 0)
        state = _snapshot(row, operation="delete", entry_count=entry_count)
        if not state["exists"] or not secrets.compare_digest(
            _fingerprint(state), str(expected_context or "")
        ):
            return ToolResult(
                ok=False,
                status="conflict",
                summary="RSS 订阅或关联条目已变化，请重新预检",
                error="确认快照已失效。",
            )
        conn.execute("DELETE FROM rss_entries WHERE rss_item_id=?", (subscription_id,))
        conn.execute("DELETE FROM rss_items WHERE id=?", (subscription_id,))

    runtime_refreshed = _reload_scheduler()
    return ToolResult(
        ok=True,
        status="completed",
        summary="RSS 订阅已删除",
        data={
            "operation": "delete",
            "affected": 1,
            "deleted_entries": entry_count,
            "runtime_refreshed": runtime_refreshed,
        },
        evidence=[
            Evidence(
                "rss_database",
                "已删除订阅与本地条目记录；未操作下载器，也未删除已下载文件。",
                _now(),
            )
        ],
        suggestions=[
            "删除已完成；已有下载任务和文件不会受影响。"
            if runtime_refreshed
            else "删除已完成；请重启 MediaFlux 刷新当前进程中的调度状态。"
        ],
    )


def prepare_create_rss_subscription(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    return _prepare_create(arguments)


def create_rss_subscription_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    return _confirmed_create(arguments, expected_context)


def prepare_update_rss_subscription(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    return _prepare_update(arguments)


def update_rss_subscription_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    return _confirmed_config_update(arguments, expected_context)


def prepare_delete_rss_subscription(
    arguments: dict[str, int],
) -> tuple[ToolResult, str]:
    return _prepare_delete(arguments)


def delete_rss_subscription_confirmed(
    arguments: dict[str, int], expected_context: str
) -> ToolResult:
    return _confirmed_delete(arguments, expected_context)
