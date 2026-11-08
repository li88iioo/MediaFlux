"""RSS 配置、聚合状态及确认快照；Web、SDK 和调度使用相同仓储。"""

from __future__ import annotations

from typing import Any

from app import database as db
from app.logger import get_logger
from app.modules.rss import _release_rss_refresh, _try_acquire_rss_refresh
from app.modules.rss_subscription_config import (
    RSSSubscriptionConfigError,
    normalize_rss_subscription_create,
    normalize_rss_subscription_update,
    wake_rss_scheduler,
)
from app.sensitive_data import redact_sensitive_text

logger = get_logger(__name__)
_STALE_SUBMITTING_MINUTES = 15
_PENDING_BACKLOG_HOURS = 24


class RSSSubscriptionError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


def subscription_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= 2_147_483_647
    ):
        raise RSSSubscriptionError("subscription_id 必须是正整数")
    return value


def _payload(arguments):
    values = dict(arguments)
    if isinstance(values.get("urls"), list):
        if not values["urls"] or any(
            not isinstance(url, str) for url in values["urls"]
        ):
            raise RSSSubscriptionError("urls 必须包含有效订阅地址")
        values["urls"] = "\n".join(values["urls"])
    return values


def _config(row) -> dict[str, Any]:
    values = dict(row)
    return {
        key: values.get(key)
        for key in (
            "name",
            "urls",
            "parser",
            "exclude_keywords",
            "action",
            "enabled",
            "refresh_cron",
            "refresh_interval_minutes",
            "download_method",
            "qb_save_path",
            "gy_target_dir",
            "gy_target_dir_name",
            "media_tmdb_id",
            "media_default_season",
            "skip_existing_episodes",
            "updated_at",
        )
    }


def _public(row):
    value = dict(row)
    for key in (
        "urls",
        "exclude_keywords",
        "qb_save_path",
        "gy_target_dir",
        "gy_target_dir_name",
        "name",
    ):
        if isinstance(value.get(key), str):
            value[key] = redact_sensitive_text(value[key])
    for key in ("enabled", "skip_existing_episodes"):
        value[key] = bool(value.get(key))
    return value


def subscriptions(limit: int = 100, after_id: int | None = None) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RSSSubscriptionError("limit 必须为1到100")
    if after_id is not None:
        subscription_id(after_id)
    try:
        rows = db.list_rss_subscriptions(limit=limit + 1, after_id=after_id)
        has_more = len(rows) > limit
        page = rows[:limit]
        summaries = db.list_rss_subscription_safe_summaries(
            db.now(), limit=limit, after_id=after_id
        )
        states = {item["subscription_number"]: item for item in summaries["items"]}
        items = [{**states.get(row["id"], {}), **_public(row)} for row in page]
        return {
            "ok": True,
            "status": "completed" if summaries["total"] else "not_configured",
            "data": {
                "items": items,
                "total": summaries["total"],
                "has_more": has_more,
                "next": {"limit": limit, "after_id": page[-1]["id"]}
                if has_more
                else None,
            },
        }

    except Exception as exc:  # noqa: BLE001 - 数据库读取失败只返回固定业务错误
        logger.warning("RSS配置读取失败 type=%s", type(exc).__name__)
        return {
            "ok": False,
            "status": "unavailable",
            "data": {"items": []},
            "error": "RSS配置当前不可用",
        }


def subscription(identifier: int) -> dict:
    subscription_id(identifier)
    try:
        row = db.get_rss_subscription(identifier)
        if row is None:
            return {
                "ok": False,
                "status": "not_found",
                "data": {"subscription_id": identifier},
            }
        state = db.get_rss_subscription_safe_summary(identifier, db.now()) or {}
        return {
            "ok": True,
            "status": "attention" if state.get("attention_count") else "healthy",
            "data": {**state, **_public(row)},
        }

    except Exception as exc:  # noqa: BLE001 - 数据库读取失败只返回固定业务错误
        logger.warning("RSS配置读取失败 type=%s", type(exc).__name__)
        return {
            "ok": False,
            "status": "unavailable",
            "data": {"subscription_id": identifier},
            "error": "RSS配置当前不可用",
        }


def activity(limit: int = 100, after_id: int | None = None) -> dict:
    result = subscriptions(limit, after_id)
    if not result["ok"]:
        return result
    page = result["data"]
    return {
        "ok": True,
        "status": "completed",
        "data": {
            "window_hours": 24,
            "downloaded": db.count_rss_downloaded_entries_since(db.now(), hours=24),
            "subscriptions": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "downloaded_last_24h": item["entry_counts"]["downloaded_last_24h"],
                }
                for item in page["items"]
            ],
            "total": page["total"],
            "has_more": page["has_more"],
            "next": page["next"],
        },
    }


def _snapshot(conn, operation, arguments) -> dict[str, Any]:
    if operation == "create":
        fields = normalize_rss_subscription_create(_payload(arguments))
        matches = conn.execute(
            "SELECT id FROM rss_items WHERE name=? AND urls=? ORDER BY id",
            (fields["name"], fields["urls"]),
        ).fetchall()
        return {
            "operation": operation,
            "fields": fields,
            "existing_ids": [row["id"] for row in matches],
        }
    identifier = subscription_id(arguments.get("subscription_id"))
    current = db.get_rss_subscription(identifier, connection=conn)
    if current is None:
        raise RSSSubscriptionError("未找到指定的 RSS 订阅", code="not_found")
    state = {
        "operation": operation,
        "subscription_id": identifier,
        "config": _config(current),
    }
    if operation == "update":
        normalized = normalize_rss_subscription_update(
            _payload({key: value for key, value in arguments.items() if key != "subscription_id"}), current=dict(current)
        )
        fields = {
            key: value for key, value in normalized.items() if current[key] != value
        }
        state["fields"] = fields
    elif operation == "delete":
        state["entry_count"] = conn.execute(
            "SELECT COUNT(*) FROM rss_entries WHERE rss_item_id=?", (identifier,)
        ).fetchone()[0]
    else:
        raise RSSSubscriptionError("RSS 配置操作无效")
    return state


def prepare_change(operation: str, arguments: dict) -> dict:
    try:
        with db.get_conn() as conn:
            snapshot = _snapshot(conn, operation, arguments)
    except RSSSubscriptionConfigError as exc:
        raise RSSSubscriptionError(str(exc)) from exc
    if operation == "update" and not snapshot["fields"]:
        raise RSSSubscriptionError("RSS 订阅配置没有变化", code="precondition_failed")
    public: dict[str, Any] = {"operation": operation, "affected": 1}
    if operation == "create":
        public.update(
            name=redact_sensitive_text(snapshot["fields"]["name"]),
            url_count=len(snapshot["fields"]["urls"].splitlines()),
        )
    else:
        public["subscription_id"] = snapshot["subscription_id"]
        if operation == "update":
            public["changed_fields"] = sorted(snapshot["fields"])
        else:
            public["deleted_entries"] = snapshot["entry_count"]
    public["effects"] = (
        ["保存RSS配置并重新加载调度；不会立即刷新或提交下载。"]
        if operation != "delete"
        else ["永久删除订阅及本地条目；不删除下载任务或文件。"]
    )
    return {
        "snapshot": snapshot,
        "preview": {
            "ok": True,
            "status": "confirmation_required",
            "summary": {
                "create": "确认创建RSS订阅",
                "update": "确认更新RSS订阅",
                "delete": "确认删除RSS订阅",
            }[operation],
            "data": public,
        },
    }


def apply_change(
    operation: str,
    arguments: dict,
    *,
    expected_snapshot: dict | None = None,
    check_cancelled=None,
) -> dict:
    if check_cancelled:
        check_cancelled()
    identifier = (
        subscription_id(arguments.get("subscription_id"))
        if operation != "create"
        else 0
    )
    acquired = operation == "delete" and _try_acquire_rss_refresh(identifier)
    if operation == "delete" and not acquired:
        raise RSSSubscriptionError("RSS订阅正在刷新，请稍后重试", code="busy")
    try:
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                snapshot = _snapshot(conn, operation, arguments)
            except (RSSSubscriptionConfigError, RSSSubscriptionError) as exc:
                if expected_snapshot is not None:
                    return {
                        "ok": False,
                        "status": "conflict",
                        "summary": "RSS配置已变化，请重新预检",
                        "data": {"affected": 0},
                    }
                raise RSSSubscriptionError(str(exc)) from exc
            if expected_snapshot is not None and snapshot != expected_snapshot:
                return {
                    "ok": False,
                    "status": "conflict",
                    "summary": "RSS订阅配置已变化，请重新预检",
                    "data": {"affected": 0},
                }
            if check_cancelled:
                check_cancelled()
            if operation == "create":
                identifier = db.add_rss_subscription(
                    **snapshot["fields"], connection=conn
                )
            elif operation == "update":
                db.update_rss_subscription(
                    identifier, snapshot["fields"], connection=conn
                )
            else:
                if conn.execute(
                    "SELECT 1 FROM rss_entries e WHERE rss_item_id=? AND (e.status='submitting' OR EXISTS (SELECT 1 FROM download_requests r WHERE r.id=e.download_request_id AND r.status IN ('pending','submitting','submitted','downloading','outcome_unknown','manual_review'))) LIMIT 1",
                    (identifier,),
                ).fetchone():
                    raise RSSSubscriptionError(
                        "RSS仍有提交中的条目，不能删除", code="busy"
                    )
                db.delete_rss_subscription(identifier, connection=conn)
        fields = snapshot.get("fields", {})
        try:
            row = db.get_rss_subscription(identifier)
        except Exception:  # noqa: BLE001 - 写后回读失败必须保留unknown，不得提示重放
            return {
                "ok": False,
                "status": "outcome_unknown",
                "summary": "RSS写入结果无法核验",
                "data": {
                    "operation": operation,
                    "subscription_id": identifier,
                    "affected": 1,
                    "verified": False,
                },
                "error": "请先读取订阅列表核实，避免重复执行。",
            }
        verified = (
            row is None
            if operation == "delete"
            else row is not None
            and all(row[key] == value for key, value in fields.items())
        )
        if not verified:
            return {
                "ok": False,
                "status": "outcome_unknown",
                "summary": "RSS写入结果无法核验",
                "data": {
                    "operation": operation,
                    "subscription_id": identifier,
                    "affected": 1,
                    "verified": False,
                },
                "error": "请先读取订阅列表核实，避免重复创建或执行。",
            }
        return {
            "ok": True,
            "status": "completed",
            "summary": "RSS订阅配置已保存并核验"
            if operation != "delete"
            else "RSS订阅已删除",
            "data": {
                "operation": operation,
                "subscription_id": identifier,
                "affected": 1,
                "verified": True,
                "changed_fields": sorted(fields),
                "changed_field_count": len(fields),
                "deleted_entries": snapshot.get("entry_count", 0),
                "runtime_refreshed": wake_rss_scheduler(),
            },
        }
    finally:
        if acquired:
            _release_rss_refresh(identifier)


def _empty_data() -> dict[str, Any]:
    return {
        "thresholds": {
            "stale_submitting_minutes": _STALE_SUBMITTING_MINUTES,
            "pending_backlog_hours": _PENDING_BACKLOG_HOURS,
        },
        "subscriptions": {},
        "entries": {},
        "attention": {
            "total": 0,
            "categories": {},
            "subscriptions": [],
            "truncated": False,
        },
    }


def diagnose(limit: int = 20, offset: int = 0) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RSSSubscriptionError("limit 必须为1到100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise RSSSubscriptionError("offset 必须是非负整数")
    try:
        snapshot = db.now()
        aggregate = db.get_rss_diagnostic_summary(
            snapshot,
            stale_submitting_minutes=_STALE_SUBMITTING_MINUTES,
            pending_backlog_hours=_PENDING_BACKLOG_HOURS,
            attention_limit=limit,
            attention_offset=offset,
        )
    except Exception as exc:  # noqa: BLE001 - 数据库读取失败只返回固定业务错误
        logger.warning("Agent RSS 诊断失败 type=%s", type(exc).__name__)
        return {
            "ok": False,
            "status": "unavailable",
            "summary": "暂时无法读取 RSS 订阅状态",
            "data": _empty_data(),
            "suggestions": ["请检查本地数据库状态后重试。"],
            "error": "RSS 诊断当前不可用。",
        }
    subscriptions = dict(aggregate.get("subscriptions") or {})
    entries = dict(aggregate.get("entries") or {})
    attention_subscriptions = list(aggregate.get("attention_subscriptions") or [])
    categories = {
        "failed_entries": max(0, int(entries.get("failed") or 0)),
        "stale_submitting": max(0, int(entries.get("stale_submitting") or 0)),
        "aged_pending_backlog": max(0, int(entries.get("pending_backlog") or 0)),
        "unknown_or_inconsistent": max(
            0, int(entries.get("unknown_or_inconsistent") or 0)
        ),
        "invalid_last_refreshed_at": max(
            0, int(subscriptions.get("invalid_last_refreshed_at") or 0)
        ),
        "cron_configured_but_not_scheduled": max(
            0, int(subscriptions.get("cron_configured_but_not_scheduled") or 0)
        ),
    }
    attention_total = sum(categories.values())
    total_subscriptions = max(0, int(subscriptions.get("total") or 0))
    enabled_subscriptions = max(0, int(subscriptions.get("enabled") or 0))
    if total_subscriptions == 0:
        status = "not_configured"
        summary = "尚未创建 RSS 订阅"
    elif enabled_subscriptions == 0:
        status = "inactive"
        summary = f"已有 {total_subscriptions} 个 RSS 订阅，但当前均未启用"
    elif attention_total:
        status = "attention"
        summary = f"RSS 状态有 {attention_total} 项需要关注"
    else:
        status = "healthy"
        summary = f"RSS 状态正常，当前启用 {enabled_subscriptions} 个订阅"
    suggestions: list[str] = []
    if categories["failed_entries"]:
        suggestions.append("请在 RSS 订阅页检查失败条目，并确认下载目标可用。")
    if categories["stale_submitting"]:
        suggestions.append(
            "存在长期停留在提交中的条目，请核对下载后端后再决定是否重试。"
        )
    if categories["aged_pending_backlog"]:
        suggestions.append(
            "存在超过 24 小时的待处理条目，请检查自动下载策略或进行人工处理。"
        )
    if categories["unknown_or_inconsistent"]:
        suggestions.append("发现状态不一致的 RSS 条目，建议先在管理页核对。")
    if categories["invalid_last_refreshed_at"]:
        suggestions.append(
            "发现无法解析的上次刷新时间；系统会将其视为到期并在下次刷新后修复。"
        )
    if categories["cron_configured_but_not_scheduled"]:
        suggestions.append(
            "当前 RSS 调度按刷新间隔运行；仅填写 Cron 不会启用自动刷新。"
        )
    if status == "not_configured":
        suggestions.append("可先创建一个 RSS 订阅，再运行诊断。")
    elif status == "inactive":
        suggestions.append("如需自动刷新，请启用至少一个 RSS 订阅。")
    return {
        "ok": True,
        "status": status,
        "summary": summary,
        "data": {
            "thresholds": dict(aggregate.get("thresholds") or {}),
            "subscriptions": subscriptions,
            "entries": entries,
            "attention": {
                "total": attention_total,
                "categories": categories,
                "subscriptions": attention_subscriptions,
                "truncated": bool(aggregate.get("attention_truncated")),
                "has_more": bool(aggregate.get("attention_truncated")),
                "next": {
                    "limit": limit,
                    "offset": offset + len(attention_subscriptions),
                }
                if aggregate.get("attention_truncated")
                else None,
            },
        },
        "suggestions": suggestions,
    }
