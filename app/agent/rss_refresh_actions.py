"""Media Agent 的单订阅 RSS 受控刷新动作。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import secrets
import threading
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.rss import rss_subscription_refresh_revision

logger = get_logger(__name__)
_CONFIRMATION_STATE = threading.local()
_BULK_EXPLICIT_LIMIT = 32
_BULK_DISPLAY_LIMIT = 100
_BULK_MAX_WORKERS = 4


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rss_refresh_subscription_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("rss.refresh_subscription 只接受 subscription_id 参数")
    subscription_id = arguments.get("subscription_id")
    if isinstance(subscription_id, bool) or not isinstance(subscription_id, int) or subscription_id <= 0:
        raise AgentToolError("subscription_id 必须是正整数")
    return {"subscription_id": subscription_id}


def _capture_row(subscription_id: int, row: Any | None) -> dict[str, Any]:
    if not row:
        return {
            "subscription_id": subscription_id,
            "exists": False,
            "has_urls": False,
            "revision": "missing",
        }
    has_urls = any(str(item).strip() for item in str(row["urls"] or "").splitlines())
    return {
        "subscription_id": subscription_id,
        "name": str(row["name"] or "").strip()[:120],
        "exists": True,
        "has_urls": has_urls,
        "revision": rss_subscription_refresh_revision(row),
    }


def _capture(arguments: dict[str, int]) -> dict[str, Any]:
    subscription_id = arguments["subscription_id"]
    return _capture_row(subscription_id, db.get_rss_subscription(subscription_id))


def preview_rss_subscription_refresh(arguments: dict[str, int]) -> ToolResult:
    """只读确认订阅存在且可刷新；不访问订阅源。"""
    _CONFIRMATION_STATE.preview = None
    state = _capture(arguments)
    if not state["exists"]:
        return ToolResult(
            ok=False,
            status="not_found",
            summary="未找到指定的 RSS 订阅",
            error="请核对订阅 ID 后重试。",
        )
    if not state["has_urls"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="该 RSS 订阅尚未配置可用地址",
            error="请先在 RSS 订阅页补充订阅地址。",
        )

    _CONFIRMATION_STATE.preview = state
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将刷新 1 个 RSS 订阅",
        data={
            "action": "rss.refresh_subscription",
            "subscription_id": arguments["subscription_id"],
            "effects": [
                "访问该订阅当前配置的 RSS 地址。",
                "去重写入新条目并更新最后刷新时间。",
                "不会自动提交下载任务。",
            ],
        },
        evidence=[Evidence(
            "rss_database",
            "仅核对本地订阅配置；未访问订阅源、未写入条目、未触发下载。",
            _now(),
        )],
        suggestions=["确认前请核对订阅 ID；确认后仅执行一次刷新。"],
    )


def rss_refresh_subscription_confirmation_context(arguments: dict[str, int]) -> str:
    state = getattr(_CONFIRMATION_STATE, "preview", None)
    _CONFIRMATION_STATE.preview = None
    if not isinstance(state, dict) or state.get("subscription_id") != arguments["subscription_id"]:
        state = _capture(arguments)
    _CONFIRMATION_STATE.pending = state
    return str(state["revision"])


def refresh_rss_subscription(arguments: dict[str, int]) -> ToolResult:
    state = getattr(_CONFIRMATION_STATE, "pending", None)
    _CONFIRMATION_STATE.pending = None
    subscription_id = arguments["subscription_id"]
    if (
        not isinstance(state, dict)
        or state.get("subscription_id") != subscription_id
        or not state.get("exists")
        or not state.get("has_urls")
        or not state.get("revision")
    ):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 刷新确认上下文已失效",
            error="请重新预检后再确认。",
        )

    from app.modules.rss import RSSEngine

    raw = RSSEngine().refresh(subscription_id, expected_revision=str(state["revision"]))
    if raw.get("busy"):
        return ToolResult(
            ok=False,
            status="busy",
            summary="该 RSS 订阅正在刷新",
            error="请等待当前刷新完成后重试。",
        )
    if raw.get("conflict"):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 订阅配置已变化，本次未刷新",
            error="请重新预检后再确认。",
        )
    if raw.get("error"):
        return ToolResult(
            ok=False,
            status="failed",
            summary="RSS 订阅刷新未完成",
            error="请检查订阅配置或稍后重试。",
        )

    total = max(0, int(raw.get("total") or 0))
    new = max(0, int(raw.get("new") or 0))
    skipped = max(0, int(raw.get("skipped") or 0))
    partial = bool(raw.get("partial"))
    failed_sources = max(0, int(raw.get("failed_sources") or 0))
    logger.info(
        "Agent RSS 刷新完成 subscription_id=%s total=%s new=%s skipped=%s "
        "partial=%s failed_sources=%s",
        subscription_id,
        total,
        new,
        skipped,
        partial,
        failed_sources,
    )
    data = {
        "subscription_id": subscription_id,
        "total": total,
        "new": new,
        "skipped": skipped,
    }
    if partial:
        data.update({"partial": True, "failed_sources": failed_sources})
    return ToolResult(
        ok=True,
        status="partial" if partial else "completed",
        summary=(
            f"RSS 订阅刷新部分完成：拉取 {total}，新增 {new}，排除 {skipped}，"
            f"暂不可用源 {failed_sources}"
            if partial else
            f"RSS 订阅刷新完成：拉取 {total}，新增 {new}，排除 {skipped}"
        ),
        data=data,
        evidence=[Evidence(
            "rss_refresh",
            "已按确认时绑定的订阅配置执行一次刷新；响应仅包含聚合计数。",
            _now(),
        )],
        suggestions=(
            ["其余订阅源已处理；请稍后核对暂不可用源。"]
            if partial else []
        ),
    )

def rss_refresh_subscriptions_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) == {"scope"}:
        if arguments.get("scope") != "all_enabled":
            raise AgentToolError("scope 只支持 all_enabled")
        return {"scope": "all_enabled"}
    if set(arguments) != {"subscription_ids"}:
        raise AgentToolError(
            "rss.refresh_subscriptions 只接受 subscription_ids 或 all_enabled scope"
        )
    raw_ids = arguments.get("subscription_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise AgentToolError("subscription_ids 必须是非空正整数数组")
    if len(raw_ids) > _BULK_EXPLICIT_LIMIT:
        raise AgentToolError(f"显式选择单次最多刷新 {_BULK_EXPLICIT_LIMIT} 个 RSS 订阅")
    ids: list[int] = []
    for value in raw_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AgentToolError("subscription_ids 必须是非空正整数数组")
        if value in ids:
            raise AgentToolError("subscription_ids 不允许重复")
        ids.append(value)
    return {"subscription_ids": ids}


def _capture_many(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("scope") == "all_enabled":
        states = [
            _capture_row(int(row["id"]), row)
            for row in db.list_enabled_rss_subscriptions()
        ]
        scope = "all_enabled"
    else:
        states = [_capture({"subscription_id": item}) for item in arguments["subscription_ids"]]
        scope = "selected"
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "scope": scope,
                "subscriptions": [
                    {"id": item["subscription_id"], "revision": item.get("revision")}
                    for item in states
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"scope": scope, "states": states, "fingerprint": fingerprint}


def _preview_rss_subscriptions_state(state: dict[str, Any]) -> ToolResult:
    """只读预检一组订阅；确认后才会依次访问订阅源。"""
    if not state["states"]:
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="当前没有已启用的 RSS 订阅",
            error="请先启用至少一个 RSS 订阅。",
        )
    missing = [item["subscription_id"] for item in state["states"] if not item["exists"]]
    unconfigured = [
        item["subscription_id"]
        for item in state["states"]
        if item["exists"] and not item["has_urls"]
    ]
    if missing or unconfigured:
        details: list[str] = []
        if missing:
            details.append("不存在：" + "、".join(map(str, missing)))
        if unconfigured:
            details.append("未配置地址：" + "、".join(map(str, unconfigured)))
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="部分 RSS 订阅暂时不能刷新",
            error="；".join(details),
        )

    names = [item.get("name") or f"#{item['subscription_id']}" for item in state["states"]]
    displayed_names = names[:12]
    name_summary = "、".join(displayed_names)
    if len(names) > len(displayed_names):
        name_summary += f" 等 {len(names)} 个订阅"
    displayed_states = state["states"][:_BULK_DISPLAY_LIMIT]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将刷新 {len(names)} 个 RSS 订阅：{name_summary}",
        data={
            "action": "rss.refresh_subscriptions",
            "scope": state["scope"],
            "subscription_count": len(names),
            "subscriptions": [
                {"subscription_id": item["subscription_id"], "name": item.get("name") or ""}
                for item in displayed_states
            ],
            "subscriptions_truncated": len(state["states"]) > len(displayed_states),
            "effects": [
                "依次访问这些订阅当前配置的 RSS 地址。",
                "去重写入新条目并更新各订阅最后刷新时间。",
                "不会自动创建下载任务。",
            ],
        },
        evidence=[Evidence(
            "rss_database",
            "仅核对本地订阅名称和可刷新状态；尚未访问订阅源。",
            _now(),
        )],
    )


def preview_rss_subscriptions_refresh(arguments: dict[str, Any]) -> ToolResult:
    return _preview_rss_subscriptions_state(_capture_many(arguments))


def prepare_rss_subscriptions_refresh(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """生成完整快照；all_enabled 不把全部内部 ID 写入确认参数。"""
    state = _capture_many(arguments)
    return _preview_rss_subscriptions_state(state), str(state["fingerprint"])


def rss_refresh_subscriptions_confirmation_context(arguments: dict[str, Any]) -> str:
    """兼容旧调用方；新工具注册使用原子 confirmation_preparer。"""
    return str(_capture_many(arguments)["fingerprint"])


def _refresh_rss_subscriptions_state(state: dict[str, Any]) -> ToolResult:
    from app.modules.rss import RSSEngine

    states = list(state["states"])
    results: list[dict[str, Any]] = []
    totals = {"total": 0, "new": 0, "skipped": 0}
    succeeded = 0
    partial_subscriptions = 0
    failed_sources = 0

    def refresh_one(item: dict[str, Any]) -> dict[str, Any]:
        try:
            return RSSEngine().refresh(
                int(item["subscription_id"]),
                expected_revision=str(item.get("revision") or ""),
            )
        except Exception as exc:
            logger.warning(
                "Agent RSS 批量刷新异常 subscription_id=%s type=%s",
                int(item["subscription_id"]),
                type(exc).__name__,
            )
            return {"error": "RSS 刷新异常"}

    for offset in range(0, len(states), _BULK_EXPLICIT_LIMIT):
        batch = states[offset : offset + _BULK_EXPLICIT_LIMIT]
        with ThreadPoolExecutor(
            max_workers=min(_BULK_MAX_WORKERS, len(batch)),
            thread_name_prefix="agent-rss-refresh",
        ) as executor:
            futures = [executor.submit(refresh_one, item) for item in batch]
            batch_results = [future.result() for future in futures]
        for item, raw in zip(batch, batch_results):
            if raw.get("busy"):
                status = "busy"
            elif raw.get("conflict"):
                status = "conflict"
            elif raw.get("error"):
                status = "failed"
            else:
                is_partial = bool(raw.get("partial"))
                status = "partial" if is_partial else "completed"
                succeeded += 1
                totals["total"] += max(0, int(raw.get("total") or 0))
                totals["new"] += max(0, int(raw.get("new") or 0))
                totals["skipped"] += max(0, int(raw.get("skipped") or 0))
                if is_partial:
                    partial_subscriptions += 1
                    failed_sources += max(0, int(raw.get("failed_sources") or 0))
            result_row = {
                "subscription_id": int(item["subscription_id"]),
                "name": str(item.get("name") or ""),
                "status": status,
            }
            if status == "partial":
                result_row["failed_sources"] = max(
                    0, int(raw.get("failed_sources") or 0)
                )
            results.append(result_row)

    requested = len(states)
    failed = requested - succeeded
    has_partial = partial_subscriptions > 0
    status = (
        "failed" if succeeded == 0 else
        "partial" if failed > 0 or has_partial else
        "completed"
    )
    return ToolResult(
        ok=succeeded > 0,
        status=status,
        summary=(
            f"已刷新 {succeeded}/{requested} 个 RSS 订阅：拉取 {totals['total']}，"
            f"新增 {totals['new']}，排除 {totals['skipped']}"
            + (
                f"；{partial_subscriptions} 个订阅部分完成，暂不可用源 {failed_sources}"
                if has_partial else ""
            )
        ),
        data={
            "requested": requested,
            "refreshed": succeeded,
            "failed": failed,
            "partial_subscriptions": partial_subscriptions,
            "failed_sources": failed_sources,
            **totals,
            "subscriptions": results[:_BULK_DISPLAY_LIMIT],
            "subscriptions_truncated": len(results) > _BULK_DISPLAY_LIMIT,
        },
        evidence=[Evidence(
            "rss_refresh",
            "已按确认时绑定的订阅配置逐个刷新；响应仅包含名称、状态和聚合计数。",
            _now(),
        )],
        suggestions=(
            ["失败或冲突的订阅可以稍后单独重试。"] if failed else []
        ) + (
            ["其余订阅源已处理；请稍后核对暂不可用源。"]
            if has_partial else []
        ),
        error=(
            "部分订阅未刷新成功。" if failed else
            "部分订阅源暂不可用。" if has_partial else ""
        ),
    )


def refresh_rss_subscriptions(arguments: dict[str, Any]) -> ToolResult:
    """兼容内部调用；注册表仍会阻止绕过确认直接执行。"""
    return _refresh_rss_subscriptions_state(_capture_many(arguments))


def refresh_rss_subscriptions_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    current = _capture_many(arguments)
    if not secrets.compare_digest(
        str(current["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError("相关 RSS 订阅配置已变化，请重新预检", code="confirmation_stale")
    return _refresh_rss_subscriptions_state(current)
