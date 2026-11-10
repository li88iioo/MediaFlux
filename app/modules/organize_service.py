"""光鸭整理的公共预览、确认快照与调度入口；复用原任务管理器。"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from app import config
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger
from app.modules.organize import Organizer, OrganizeRules, organize_rules_snapshot
from app.modules.organize_sources import (
    configured_organize_sources,
    normalize_organize_source_ids,
)
from app.modules.organize_tasks import _protected_source_ids, get_organize_manager

logger = get_logger(__name__)


class OrganizeServiceError(ValueError):
    def __init__(self, message: str, *, code="invalid_arguments"):
        super().__init__(message)
        self.code = code


def configured_inputs(source_ids=None):
    try:
        sources = configured_organize_sources()
    except ValueError as exc:
        raise OrganizeServiceError(str(exc), code="invalid_configuration") from exc
    if source_ids is not None:
        ids, error = normalize_organize_source_ids(
            source_ids, configured_sources=sources, require_nonempty=True
        )
        if error:
            raise OrganizeServiceError(error)
        sources = [source for source in sources if source["id"] in ids]
    target = (config.get("GY_ORGANIZE_TARGET_DIR", "0") or "0").strip()
    rules = OrganizeRules.from_config(target_dir_id=target)
    error = (
        "未配置光鸭整理源目录"
        if not sources
        else "未配置光鸭整理目标目录"
        if not target or target == "0"
        else ""
    )
    return sources, rules, error


def _credential_generation(client):
    return max(0, int(client.credential_generation or 0))


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _run_fingerprint(sources, rules, client):
    return _fingerprint(
        {
            "sources": sources,
            "rules": organize_rules_snapshot(rules),
            "credential_generation": _credential_generation(client),
        }
    )


def preview_sources(sources, rules, *, max_files=100, client=None) -> dict:
    """Web 与 SDK 共用 dry-run；上限仅约束预览，不缩减随后触发的来源范围。"""
    if type(max_files) is not int or not 1 <= max_files <= 1000:
        raise OrganizeServiceError("预览数量上限必须是 1-1000 的整数")
    aggregate: dict[str, Any] = {"scan_complete": True, "protected_staging_dirs": []}
    plans_out = []
    scan_errors = 0
    remaining = max_files
    protected_staging_ids = download_requests_repo.list_protected_guangya_staging_ids()
    protected = _protected_source_ids(sources, staging_ids=protected_staging_ids)
    for source in sources:
        if remaining <= 0:
            break
        source_rules = rules.for_source(source["id"])
        organizer = Organizer(client=client) if client is not None else Organizer()
        try:
            organizer._validate_target_outside_source(
                source["id"], source_rules.target_dir_id
            )
            plans, stats = organizer.organize(
                source["id"],
                source_rules,
                dry_run=True,
                max_files=remaining,
                post_actions=False,
                protected_source_ids=protected,
                protected_staging_ids=protected_staging_ids,
            )
        finally:
            organizer.close()
        scan_errors += len(stats.get("scan_errors") or [])
        for key, value in stats.items():
            if isinstance(value, int) and not isinstance(value, bool):
                aggregate[key] = aggregate.get(key, 0) + value
        aggregate["scan_complete"] = bool(
            aggregate["scan_complete"] and stats.get("scan_complete", True)
        )
        aggregate["protected_staging_dirs"].extend(stats.get("protected_staging_dirs") or [])
        remaining -= int(stats.get("total", 0))
        for plan in plans:
            plans_out.append(
                {
                    "source_id": source["id"],
                    "source_name": source["name"],
                    **{
                        key: getattr(plan, key)
                        for key in (
                            "action",
                            "original_name",
                            "original_path",
                            "size",
                            "main_category",
                            "region",
                            "new_name",
                            "base_name",
                            "variant_label",
                            "variant_suffix",
                            "conflict_decision",
                            "conflict_note",
                            "target_path",
                            "note",
                        )
                    },
                    "title": plan.match.title if plan.match else "",
                    "year": plan.match.year if plan.match else "",
                    "tmdb_id": plan.match.tmdb_id if plan.match else "",
                    "confidence": plan.match.confidence if plan.match else 0,
                }
            )
    return {
        "stats": aggregate,
        "plans": plans_out,
        "scan_errors": scan_errors,
        "sampled_files": aggregate.get("total", 0),
        "sample_limit": max_files,
        "truncated": remaining <= 0,
    }


def _preview(
    sources, rules, error, client, *, max_files=100, for_confirmation=False
) -> dict:
    if error or not client.logged_in:
        return {
            "ok": False,
            "status": "not_configured",
            "summary": "光鸭整理配置尚不完整",
            "error": error or "光鸭账号尚未连接",
        }
    if get_organize_manager().task_status().get("status") in {"running", "stopping"}:
        return {"ok": False, "status": "conflict", "summary": "光鸭整理任务已在运行"}
    try:
        data = preview_sources(sources, rules, max_files=max_files, client=client)
    except Exception as exc:  # noqa: BLE001 -- 预览失败不得生成写确认，错误正文不对外暴露
        logger.warning("光鸭整理预览失败 type=%s", type(exc).__name__)
        return {
            "ok": False,
            "status": "unavailable",
            "summary": "光鸭整理预览暂时不可用",
        }
    data.update(
        source_count=len(sources),
        sources=sources,
        target_dir_id=rules.target_dir_id,
        effects={
            "scope": "selected_sources",
            "moves_files": True,
            "renames_files": rules.rename_enabled,
            "may_clean_empty_directories": rules.clean_empty,
            "may_recycle_replaced_files": rules.recycle_replaced_enabled,
            "triggers_strm_after_moves": rules.link_strm,
        },
    )
    if data["scan_errors"]:
        return {
            "ok": False,
            "status": "inconclusive",
            "summary": "光鸭整理预览扫描不完整",
            "data": data,
            "error": "部分目录无法读取，本次不会创建执行确认。",
        }
    protected_count = len(data["stats"].get("protected_staging_dirs") or [])
    protected_summary = f"受保护下载目录 {protected_count} 个未扫描" if protected_count else ""
    if protected_count and data["sampled_files"] == 0:
        return {
            "ok": False,
            "status": "inconclusive",
            "summary": f"光鸭整理预览受阻：{protected_summary}",
            "data": data,
            "error": "下载隔离目录仍可能被写入或等待核验；已完成下载的待确认项可从原整理日志查看详情并纠正后重新整理。",
        }
    if for_confirmation and data["sampled_files"] == 0:
        return {
            "ok": True,
            "status": "no_changes",
            "summary": "当前来源中没有可整理的媒体文件",
            "data": data,
        }
    return {
        "ok": True,
        "status": "preview",
        "summary": "只读预览已生成；确认后整理所选来源的全部可整理文件，预览数量不限制执行范围"
        + (f"；{protected_summary}" if protected_summary else ""),
        "data": data,
    }


def preview(source_ids=None, max_files=100) -> dict:
    sources, rules, error = configured_inputs(source_ids)
    client = GuangYaClient()
    try:
        return _preview(sources, rules, error, client, max_files=max_files)
    finally:
        close_guangya_client(client)


def prepare_run(source_ids=None) -> tuple[dict, str]:
    sources, rules, error = configured_inputs(source_ids)
    client = GuangYaClient()
    try:
        return _preview(
            sources, rules, error, client, for_confirmation=True
        ), _run_fingerprint(sources, rules, client)
    finally:
        close_guangya_client(client)


def run(source_ids=None, *, expected_fingerprint=None, check_cancelled=None) -> dict:
    sources, rules, error = configured_inputs(source_ids)
    client = GuangYaClient()
    transferred = False
    try:
        if check_cancelled:
            check_cancelled()
        if expected_fingerprint is not None and not secrets.compare_digest(
            _run_fingerprint(sources, rules, client), expected_fingerprint
        ):
            raise OrganizeServiceError(
                "光鸭整理配置已变化或登录凭据已更新，请重新预检",
                code="confirmation_stale",
            )
        if error or not client.logged_in:
            return {
                "ok": False,
                "status": "not_configured",
                "summary": "光鸭整理配置尚不完整",
                "error": error or "光鸭账号尚未连接",
            }
        if check_cancelled:
            check_cancelled()
        result = get_organize_manager().start(
            sources, rules, trigger_type="manual", client=client,
            expected_credential_generation=_credential_generation(client), take_client_ownership=True,
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "status": "conflict",
                "summary": "光鸭整理任务未启动",
                "error": result.get("error") or "已有整理任务运行中",
            }
        transferred = True
        return {
            "ok": True,
            "status": "accepted",
            "summary": "光鸭整理任务已提交",
            "data": {
                "task_id": result["task_id"],
                "run_id": result["run_id"],
                "trigger_type": "manual",
                "source_count": len(sources),
            },
        }
    finally:
        if not transferred:
            close_guangya_client(client)


def _stop_snapshot():
    task = get_organize_manager().task_status()
    return {
        "task_id": str(task.get("id") or ""),
        "run_id": task.get("run_id") or 0,
        "status": str(task.get("status") or "").strip().lower(),
        "stoppable": task.get("stoppable") is True,
        "started_at": str(task.get("started_at") or ""),
    }


def prepare_stop(run_id=None) -> tuple[dict, dict]:
    task = _stop_snapshot()
    if run_id is not None and (
        type(run_id) is not int or run_id <= 0 or run_id != task["run_id"]
    ):
        raise OrganizeServiceError("指定整理运行已变化", code="confirmation_stale")
    if task["status"] != "running" or not task["stoppable"]:
        return {
            "ok": False,
            "status": "conflict",
            "summary": "当前没有可停止的光鸭整理任务",
        }, task
    return {
        "ok": True,
        "status": "preview",
        "summary": "确认后在安全边界停止；已完成云盘操作不会回滚",
        "data": {"requested": True, "cooperative": True, "run_id": task["run_id"]},
    }, task


def stop(*, expected_snapshot=None, run_id=None, check_cancelled=None) -> dict:
    if check_cancelled:
        check_cancelled()
    if expected_snapshot is not None:
        current = _stop_snapshot()
        if (
            current != expected_snapshot
            or current["status"] != "running"
            or not current["stoppable"]
            or not current["task_id"]
        ):
            raise OrganizeServiceError(
                "整理任务状态已变化，请重新预检", code="confirmation_stale"
            )
        if check_cancelled:
            check_cancelled()
        raw = get_organize_manager().stop(
            expected_task_id=current["task_id"], require_running=True
        )
        if not raw.get("ok"):
            raise OrganizeServiceError(
                "整理任务状态已变化，请重新预检", code="confirmation_stale"
            )
        return {
            "ok": True,
            "status": "accepted",
            "summary": "光鸭整理停止请求已提交",
            "data": {
                "accepted": True,
                "task_id": current["task_id"],
                "run_id": current["run_id"],
            },
        }
    if run_id is not None:
        preview_result, snapshot = prepare_stop(run_id)
        if not preview_result["ok"]:
            return preview_result
        return stop(expected_snapshot=snapshot, check_cancelled=check_cancelled)
    return get_organize_manager().stop()
