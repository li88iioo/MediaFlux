"""RSS 条目与投递的公共预检、原子执行和回执；Web/SDK 共享既有引擎。"""

from __future__ import annotations

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from app import database as db
from app.logger import get_logger
from app.modules.rss import (
    RSSEngine,
    rss_entry_delivery,
    rss_subscription_refresh_revision,
)
from app.modules.rss_subscriptions import RSSSubscriptionError, subscription_id
from app.modules.web_secret import signed_context_fingerprint
from app.sensitive_data import redact_sensitive_text

logger = get_logger(__name__)
_SAFE_FAILURE_LABELS = {
    "": "",
    "unknown_failure": "未分类失败",
    "submission_outcome_unknown": "提交结果待确认",
    "qb_outcome_unknown": "qBittorrent 提交结果待确认",
    "guangya_outcome_unknown": "光鸭提交结果待确认",
    "download_failed": "下载失败",
    "invalid_payload": "条目数据无效",
    "qb_auth_failed": "qBittorrent 认证失败",
    "qb_rate_limited": "qBittorrent 请求受限",
    "qb_unavailable": "qBittorrent 暂不可用",
    "qb_submit_failed": "qBittorrent 提交失败",
    "guangya_submit_failed": "光鸭提交失败",
    "guangya_manifest_unavailable": "光鸭暂未取得文件清单",
    "guangya_unavailable": "光鸭解析服务暂不可用",
    "guangya_rate_limited": "光鸭请求受限",
    "guangya_auth_failed": "光鸭认证失败",
    "guangya_credentials_changed": "光鸭登录凭据已变化，请重新预检",
}


def _ids(value):
    if not isinstance(value, list) or not value:
        raise RSSSubscriptionError("ID 必须是非空正整数数组")
    ids = [subscription_id(item) for item in value]
    if len(set(ids)) != len(ids):
        raise RSSSubscriptionError("ID 不允许重复")
    return ids


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise RSSSubscriptionError("分页范围必须是有效整数")
    return value


def normalize(operation: str, arguments: dict) -> dict:
    values = dict(arguments)
    if operation == "mark":
        if (
            set(values) != {"entry_ids", "processed"}
            or type(values["processed"]) is not bool
        ):
            raise RSSSubscriptionError("必须提供 entry_ids 和布尔 processed")
        return {
            "entry_ids": _ids(values["entry_ids"]),
            "processed": values["processed"],
        }
    if operation == "refresh":
        values = {key: val for key, val in values.items() if val is not None}
        if values == {"scope": "all_configured"}:
            return values
        if set(values) != {"subscription_ids"}:
            raise RSSSubscriptionError(
                "请选择 subscription_ids 或 scope=all_configured"
            )
        return {"subscription_ids": _ids(values["subscription_ids"])}
    if operation not in {"download", "retry"} or set(values) - {
        "entry_ids",
        "limit",
        "subscription_id",
    }:
        raise RSSSubscriptionError("RSS 投递参数无效")
    values = {key: val for key, val in values.items() if val is not None}
    if "entry_ids" in values:
        if set(values) != {"entry_ids"}:
            raise RSSSubscriptionError(
                "选定 entry_ids 不能同时指定 limit 或 subscription_id"
            )
        return {"entry_ids": _ids(values["entry_ids"])}
    values["limit"] = _integer(values.get("limit", 10))
    if "subscription_id" in values:
        values["subscription_id"] = subscription_id(values["subscription_id"])
    return values


def entries(
    subscription_id=None, status="pending", limit=100, offset=0, query=""
) -> dict:
    if subscription_id is not None:
        from app.modules.rss_subscriptions import subscription_id as validate_id

        subscription_id = validate_id(subscription_id)
    if status not in {
        "all",
        "pending",
        "submitting",
        "downloaded",
        "failed",
        "skipped",
    }:
        raise RSSSubscriptionError("status 无效")
    _integer(limit)
    _integer(offset, 0)
    if not isinstance(query, str):
        raise RSSSubscriptionError("query 必须是字符串")
    page = db.list_rss_entries(
        sub_id=subscription_id,
        status=None if status == "all" else status,
        limit=limit,
        offset=offset,
        keyword=query,
        order="received_desc",
    )
    items = [
        {
            "id": int(row["id"]),
            "subscription_id": int(row["rss_item_id"]),
            "subscription_name": redact_sensitive_text(str(row["sub_name"] or "")),
            "title": redact_sensitive_text(str(row["title"] or "")),
            "status": str(row["status"]),
            "processed": bool(row["processed"]),
            "published_at": row["pub_date"],
            "created_at": row["created_at"],
            "season": row["media_season"],
            "episode": row["media_episode"],
            "skip_reason": redact_sensitive_text(str(row["skip_reason"] or "")),
            "failure_code": str(row["failure_code"] or ""),
            "failure_label": _SAFE_FAILURE_LABELS.get(
                str(row["failure_code"] or ""), "未分类失败"
            ),
            "failure_retryable": bool(row["failure_retryable"]),
            "delivery": rss_entry_delivery(row),
        }
        for row in page.items
    ]
    has_more = offset + len(items) < page.total
    return {
        "ok": True,
        "status": "completed" if items else "empty",
        "data": {
            "items": items,
            "total": page.total,
            "offset": offset,
            "has_more": has_more,
            "next": {
                "subscription_id": subscription_id,
                "status": status,
                "limit": limit,
                "offset": offset + len(items),
                "query": query,
            }
            if has_more
            else None,
        },
    }


def _capture(operation: str, arguments: dict) -> dict:
    if operation == "refresh":
        rows = (
            db.list_rss_subscriptions()
            if arguments.get("scope")
            else [db.get_rss_subscription(i) for i in arguments["subscription_ids"]]
        )
        if not rows or any(
            row is None or not str(row["urls"] or "").strip() for row in rows
        ):
            raise RSSSubscriptionError(
                "部分 RSS 订阅不存在或未配置地址", code="precondition_failed"
            )
        return {
            "states": [
                {
                    "subscription_id": int(row["id"]),
                    "name": redact_sensitive_text(str(row["name"])),
                    "revision": rss_subscription_refresh_revision(row),
                }
                for row in rows
                if row is not None
            ]
        }
    if operation == "mark":
        by_id = db.get_rss_entries_by_ids(arguments["entry_ids"])
        rows = [by_id.get(i) for i in arguments["entry_ids"]]
        eligible = (
            {"pending", "failed", "skipped"}
            if arguments["processed"]
            else {"failed", "skipped"}
        )
        if any(row is None or str(row["status"]) not in eligible for row in rows):
            raise RSSSubscriptionError("条目不存在、正在提交或已下载", code="conflict")
        return {
            "rows": [
                {
                    "id": int(row["id"]),
                    "status": str(row["status"]),
                    "processed": bool(row["processed"]),
                    "created_at": str(row["created_at"] or ""),
                    "failure_code": str(row["failure_code"] or ""),
                    "failure_retryable": bool(row["failure_retryable"]),
                }
                for row in rows
                if row is not None
            ],
            "processed": arguments["processed"],
        }
    from app.modules.offline import OfflineRules
    from app.modules.rss import capture_rss_qb_runtime_config
    from app.modules.download_dispatcher import guangya_download_credential_generation

    runtime, config_error = capture_rss_qb_runtime_config()
    query = (
        db.get_pending_rss_snapshot
        if operation == "download"
        else db.get_retryable_failed_rss_snapshot
    )
    selected = arguments.get("entry_ids")
    rows = query(
        default_method=runtime.get("default_method", "qb"),
        limit=None if selected else arguments["limit"] + 1,
        entry_ids=selected,
        subscription_id=arguments.get("subscription_id"),
    )
    if selected and len(rows) != len(selected):
        raise RSSSubscriptionError("所选 RSS 条目不是完整的可投递集合", code="conflict")
    has_more = not selected and len(rows) > arguments["limit"]
    rows = rows if selected else rows[: arguments["limit"]]
    missing_source = [str(row["id"]) for row in rows
                      if not row["download_request_key"] and not RSSEngine._entry_torrent_url(row)]
    if missing_source:
        raise RSSSubscriptionError(
            f"RSS 条目 {', '.join(missing_source)} 缺少下载链接，请刷新订阅源或调整所选条目",
            code="missing_torrent_url",
        )
    snapshot = (
        db.rss_pending_entry_snapshot
        if operation == "download"
        else db.rss_retry_entry_snapshot
    )
    items = [snapshot(row) for row in rows]
    targets = sorted(
        {
            str(
                row["download_backend"]
                or row["download_method"]
                or runtime.get("default_method")
                or "qb"
            )
            .strip()
            .lower()
            for row in rows
        }
    )
    if "qb" not in targets:
        config_error = ""
        runtime = {"default_method": runtime.get("default_method") or "qb"}
    credentials = {}
    if "guangya" in targets:
        try:
            credentials["guangya_credential_generation"] = guangya_download_credential_generation()
        except ValueError as exc:
            raise RSSSubscriptionError("光鸭登录状态不可用，请重新登录后预检", code="not_configured") from exc
    return {
        "entries": items,
        "runtime_config": runtime,
        "config_error": config_error,
        "cloud_rules": asdict(OfflineRules.from_config())
        if "guangya" in targets
        else {},
        **credentials,
        "target": "both"
        if len(targets) > 1
        else "qbittorrent"
        if targets == ["qb"]
        else "guangya",
        "has_more": bool(has_more),
    }


def _fingerprint(operation, arguments, state):
    return signed_context_fingerprint(
        {"arguments": arguments, "state": state}, domain="rss-" + operation
    )


def prepare(operation: str, arguments: dict) -> tuple[dict, str]:
    arguments = normalize(operation, arguments)
    state = _capture(operation, arguments)
    fingerprint = _fingerprint(operation, arguments, state)
    if operation in {"download", "retry"}:
        count = len(state["entries"])
        if not count:
            return {
                "ok": True,
                "status": "no_changes",
                "summary": "当前没有可安全投递的 RSS 条目",
            }, fingerprint
        if state["config_error"]:
            return {
                "ok": False,
                "status": "not_configured",
                "summary": "qBittorrent 配置不可用",
            }, fingerprint
        data = {
            "selected_count": count,
            "target": state["target"],
            "has_more": state["has_more"],
            "entry_ids": [row["id"] for row in state["entries"]],
        }
    elif operation == "refresh":
        data = {
            "subscription_count": len(state["states"]),
            "subscriptions": state["states"],
            "scope": arguments.get("scope", "selected"),
        }
    else:
        data = {
            "selected_count": len(state["rows"]),
            "processed": arguments["processed"],
            "entry_ids": arguments["entry_ids"],
        }
    return {
        "ok": True,
        "status": "confirmation_required",
        "summary": "确认后执行 RSS " + operation,
        "data": data,
    }, fingerprint


def execute(
    operation: str,
    arguments: dict,
    expected_context: str | None = None,
    *,
    check_cancelled=None,
) -> dict:
    if check_cancelled:
        check_cancelled()
    arguments = normalize(operation, arguments)
    try:
        state = _capture(operation, arguments)
    except RSSSubscriptionError as exc:
        if expected_context is not None:
            raise RSSSubscriptionError(str(exc), code="confirmation_stale") from exc
        raise
    if expected_context is not None and not secrets.compare_digest(
        _fingerprint(operation, arguments, state), expected_context
    ):
        raise RSSSubscriptionError(
            "RSS 条目或订阅/目标配置已变化，请重新预检", code="confirmation_stale"
        )
    if check_cancelled:
        check_cancelled()
    if operation == "mark":
        updated = db.update_rss_entries_processed_snapshot(
            state["rows"], arguments["processed"]
        )
        if updated != len(state["rows"]):
            raise RSSSubscriptionError(
                "RSS 条目标记发生并发冲突", code="confirmation_stale"
            )
        return {
            "ok": True,
            "status": "completed",
            "summary": f"已标记 {updated} 个 RSS 条目",
            "data": {"affected": updated, "processed": arguments["processed"]},
        }
    if operation == "refresh":
        run_id = db.add_task_run("rss_refresh", "manual")
        try:
            result = _refresh(state)
            result["data"]["run_id"] = run_id
            db.finish_task_run(
                run_id, result["status"], result=json.dumps(result, ensure_ascii=False)
            )
            return result
        except Exception:
            db.finish_task_run(run_id, "outcome_unknown", error="刷新结果无法确认")
            raise
    if not state["entries"]:
        return {
            "ok": True,
            "status": "no_changes",
            "summary": "当前没有可安全投递的 RSS 条目",
        }
    if state["config_error"]:
        raise RSSSubscriptionError("下载器配置不可用", code="not_configured")
    raw = RSSEngine().submit_snapshot(
        state["entries"],
        state["runtime_config"],
        claim=db.claim_pending_rss_entries
        if operation == "download"
        else db.claim_retryable_failed_rss_entries,
        check_cancelled=check_cancelled,
        guangya_credential_generation=state.get("guangya_credential_generation"),
    )
    return submission_result(
        raw,
        kind="retry"
        if operation == "retry"
        else "entry"
        if arguments.get("entry_ids")
        else "download",
        target=state["target"],
    )


def refresh_run(run_id: int) -> dict:
    row = db.get_task_run(subscription_id(run_id))
    if row is None or row["task_name"] != "rss_refresh":
        return {"ok": False, "status": "not_found", "data": {"run_id": run_id}}
    result = json.loads(row["result"] or "{}")
    return result or {"ok": True, "status": row["status"], "data": {"run_id": run_id}}


def submission_result(raw: dict[str, Any], *, kind: str, target: str) -> dict:
    requested = max(0, int(raw.get("requested") or 0))
    claimed = max(0, int(raw.get("claimed") or 0))
    retry, exact = (kind == "retry", kind == "entry")
    target_label = {
        "qbittorrent": "qBittorrent",
        "guangya": "光鸭",
        "both": "qBittorrent / 光鸭",
    }[target]
    if raw.get("conflict") or claimed != requested:
        if exact:
            raise RSSSubscriptionError(
                "RSS 条目在提交前发生变化，请重新预检", code="confirmation_stale"
            )
        return {
            "ok": False,
            "status": "conflict",
            "summary": "可重试 RSS 失败条目已变化，本次未提交"
            if retry
            else "待处理 RSS 条目已变化，本次未提交",
            "data": {
                "target": target,
                "requested": requested,
                "claimed": 0,
                "submitted": 0,
                "failed": 0,
            },
            "error": "请重新预检后再确认。",
        }
    submitted = max(0, int(raw.get("submitted") or 0))
    failed = max(0, int(raw.get("failed") or 0))
    unknown = min(failed, max(0, int(raw.get("outcome_unknown") or 0)))
    confirmed_failed = failed - unknown
    pending = max(0, int(raw.get("pending") or 0))
    cancelled = max(0, int(raw.get("cancelled") or 0))
    subject = "RSS 失败条目" if retry else "RSS 条目"
    if pending:
        pending_only = submitted == 0 and failed == 0
        status = "in_progress" if pending_only else "partial"
        ok = pending_only or submitted > 0
        summary = f"{subject}{('重试' if retry else '提交')}状态：成功 {submitted}，提交中 {pending}，失败 {confirmed_failed}，待核对 {unknown}"
    elif unknown:
        status = (
            "partial"
            if submitted or (confirmed_failed and (not exact))
            else "review_required"
        )
        ok = submitted > 0
        heading = "RSS 提交结果" if exact else f"{subject}提交结果"
        failure = failed - unknown if exact else confirmed_failed
        label = "失败" if exact else "确认失败"
        summary = f"{heading}：成功 {submitted}，待核对 {unknown}，{label} {failure}"
    elif failed:
        status, ok = ("partial", True) if submitted else ("failed", False)
        summary = (
            f"{subject}部分{('重试' if retry else '提交')}完成：成功 {submitted}，失败 {failed}"
            if submitted
            else f"本次 {failed} 个 {subject}{('仍未成功提交' if retry else '均未成功提交')}"
        )
    else:
        status, ok = ("completed", True)
        summary = (
            f"已成功重试 {submitted} 个 RSS 失败条目"
            if retry
            else f"已向 {target_label} 提交 {submitted} 个 RSS 条目"
        )
    if cancelled:
        if not (pending or failed):
            status, ok = ("partial", True) if submitted else ("cancelled", False)
        summary += f"；另有 {cancelled} 项尚未提交，已按取消停止"
    if unknown:
        suggestions = (
            ["请先核对对应下载器中是否已存在对应任务，勿直接重复提交。"]
            if retry
            else [f"请先在 {target_label} 中核对待确认任务，勿直接重复提交。"]
            if exact
            else [f"请先核对 {target_label} 中是否已存在对应任务，勿直接重复提交。"]
        )
        error = (
            "部分提交结果未知。"
            if exact
            else f"部分提交结果未知，请先核对{('对应下载器' if retry else ' ' + target_label)}，勿直接重试。"
        )
    else:
        suggestions = (
            ["请重新诊断 RSS 失败状态后再决定下一步。"]
            if failed and retry
            else ["请在 RSS 订阅页和下载任务页核对失败项。"]
            if failed and kind == "download"
            else []
        )
        error = (
            f"{subject}{('重试' if retry else '提交')}未全部成功。" if failed else ""
        )
    return {
        "ok": ok,
        "status": status,
        "summary": summary,
        "data": {
            "target": target,
            "requested": requested,
            "claimed": claimed,
            "submitted": submitted,
            "failed": failed,
            **({"outcome_unknown": unknown} if unknown else {}),
            **({"pending": pending} if pending else {}),
            **({"cancelled": cancelled} if cancelled else {}),
        },
        "suggestions": suggestions,
        "error": error,
    }


def _refresh(state: dict[str, Any]) -> dict:
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
        except Exception as exc:  # noqa: BLE001 -- 单个源异常记录为失败，保留同批其他刷新结果
            logger.warning(
                "RSS 批量刷新异常 subscription_id=%s type=%s",
                int(item["subscription_id"]),
                type(exc).__name__,
            )
            return {"error": "RSS 刷新异常"}

    for offset in range(0, len(states), 32):
        batch = states[offset : offset + 32]
        with ThreadPoolExecutor(
            max_workers=min(4, len(batch)), thread_name_prefix="rss-refresh"
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
        "failed"
        if succeeded == 0
        else "partial"
        if failed > 0 or has_partial
        else "completed"
    )
    return {
        "ok": succeeded > 0,
        "status": status,
        "summary": f"已刷新 {succeeded}/{requested} 个 RSS 订阅：拉取 {totals['total']}，新增 {totals['new']}，排除 {totals['skipped']}"
        + (
            f"；{partial_subscriptions} 个订阅部分完成，暂不可用源 {failed_sources}"
            if has_partial
            else ""
        ),
        "data": {
            "requested": requested,
            "refreshed": succeeded,
            "failed": failed,
            "partial_subscriptions": partial_subscriptions,
            "failed_sources": failed_sources,
            **totals,
            "subscriptions": results,
            "subscriptions_truncated": False,
        },
        "suggestions": (["失败或冲突的订阅可以稍后单独重试。"] if failed else [])
        + (["其余订阅源已处理；请稍后核对暂不可用源。"] if has_partial else []),
        "error": "部分订阅未刷新成功。"
        if failed
        else "部分订阅源暂不可用。"
        if has_partial
        else "",
    }
