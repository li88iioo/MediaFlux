"""统一整理的串行云端写入阶段。"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from inspect import getattr_static
from typing import TYPE_CHECKING, Any, Callable, Mapping

from app import database as db
from app.clients.guangya import GuangYaFile
from app.logger import get_logger
from app.modules.directory_scrape_errors import safe_organize_failure
from app.modules.guangya_compensation import restore_guangya_file
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
    record_blocked_delete,
)
from app.modules.organize_postprocess import (
    companion_target_name,
    media_notification_item,
    media_role,
    normalized_stem,
    replacement_delete_block_reason,
    resolved_plan_position,
)
from app.modules.organize_runtime import OrganizeTaskRuntime
from app.modules.scraper import MatchResult

if TYPE_CHECKING:
    from app.modules.organize import OrganizePlan, Organizer, OrganizeRules


logger = get_logger(__name__)


def _optional_guard_method(guard, name: str):
    # 与其它可选能力一样只采用真实声明的方法，不触发动态__getattr__/Mock补造能力。
    try:
        getattr_static(guard, name)
    except AttributeError:
        return None
    method = getattr(guard, name, None)
    return method if callable(method) else None


def _release_parse_diagnostic(match) -> dict | None:
    """提取解析管道留下的结构化证据，供每条整理审计复用。"""
    metadata = getattr(match, "metadata", None) or {}
    diagnostic = metadata.get("release_parse") if isinstance(metadata, dict) else None
    return dict(diagnostic) if isinstance(diagnostic, dict) and diagnostic else None


def execute_organize_plans(
    organizer: Organizer,
    plans: list[OrganizePlan],
    rules: OrganizeRules,
    stats: dict,
    companion_files: dict[str, list[GuangYaFile]],
    subtitle_plans_by_video: dict[str, list] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    source_dir_id: str = "",
    on_progress: Callable[[int, int], None] | None = None,
    operation_token: str = "",
    notification_context: dict | None = None,
    task_runtime: OrganizeTaskRuntime | None = None,
    source_snapshots: Mapping[str, GuangYaFile] | None = None,
) -> None:
    operation_token = (operation_token or "").strip()
    from app.modules.organize_probe_notifications import apply_notification_context

    apply_notification_context(stats, notification_context)

    def write_plan_audit(plan, match, destination, status, outcome, items):
        payload = {
            "source_dir_id": source_dir_id,
            "original_parent_id": plan.original_parent_id,
            "original_name": plan.original_name,
            "media_type": match.media_type,
            "provider": organizer._match_provider(match),
            "external_id": organizer._match_external_id(match),
            "title": match.title,
            "year": match.year,
            "release_parse": _release_parse_diagnostic(match),
            **outcome,
        }
        if operation_token:
            payload["operation_token"] = operation_token
        return organizer._write_organize_audit(
            ("guangya", plan.original_path, destination, plan.file_id, status, match.tmdb_id),
            payload, items,
        )

    subtitle_plans_by_video = subtitle_plans_by_video or {}
    moved_companions: set[str] = set()
    directory_stats: dict[str, dict[str, Any]] = {}
    runtime = task_runtime or OrganizeTaskRuntime()
    target_episode_inventory_cache = runtime.target_episode_inventory_cache
    # 光鸭目录创建后列表可能短时间仍返回旧数据。同一批整理必须复用
    # 已解析/创建的每一级目录，避免每个剧集重复创建同名媒体目录。
    directory_chain_cache = runtime.directory_chain_cache

    def read_target_revision(target_id: str):
        """读取目录轻量版本；缺少版本字段时返回 None 并走严格刷新。"""
        started = time.monotonic()
        try:
            detail = organizer.client.file_info(target_id)
        except Exception as exc:  # 版本探测失败不能放宽冲突校验
            logger.debug(
                "目标目录版本读取失败 target=%s type=%s",
                target_id, type(exc).__name__,
            )
            detail = None
        stats["target_revision_checks"] = int(
            stats.get("target_revision_checks", 0) or 0
        ) + 1
        stats["target_revision_check_elapsed_seconds"] = round(
            float(stats.get("target_revision_check_elapsed_seconds", 0.0) or 0.0)
            + max(0.0, time.monotonic() - started),
            3,
        )
        return runtime.inventory_revision(detail)

    def checkpoint_target_revision(target_id: str) -> None:
        """成功写入后记录目录新版本；失败或字段缺失则让下一计划严格刷新。"""
        revision = read_target_revision(target_id)
        runtime.set_inventory_revision(target_id, revision)
        if revision is None:
            stats["target_revision_checkpoint_fallbacks"] = int(
                stats.get("target_revision_checkpoint_fallbacks", 0) or 0
            ) + 1
        else:
            stats["target_revision_checkpoints"] = int(
                stats.get("target_revision_checkpoints", 0) or 0
            ) + 1
    source_group_rows = {
        f"{str(row.get('id') or '')}\x1f{str(row.get('path') or '')}": row
        for row in (stats.get("source_groups") or [])
        if isinstance(row, dict)
    }
    source_group_paths: dict[str, set[str]] = {}
    for plan in plans:
        group_key = (
            f"{(plan.source_group_id or source_dir_id)}\x1f"
            f"{(plan.source_group_path or '__root__')}"
        )
        source_group_paths.setdefault(group_key, set()).add(plan.original_path or "/")
        current = directory_stats.setdefault(plan.original_path or "/", {
            "total": 0, "moved": 0, "metadata_moved": 0,
            "skipped": 0, "need_confirm": 0, "failed": 0,
            "confirmations": [], "skip_reasons": [],
        })
        current["total"] += 1
        if plan.action != "move":
            if plan.match and plan.match.need_confirm:
                current["need_confirm"] += 1
                summary = organizer._confirmation_summary(plan.match)
                if summary and summary not in current["confirmations"] and len(current["confirmations"]) < 3:
                    current["confirmations"].append(summary)
            else:
                current["skipped"] += 1
                organizer._append_reason(
                    current, "skip_reasons",
                    plan.note or plan.conflict_note or (plan.match.error if plan.match else "")
                    or "未进入整理执行",
                )

    def record_runtime_skip(plan, target_parent_id: str, reason: str) -> None:
        stats["skipped"] += 1
        current = directory_stats[plan.original_path or "/"]
        current["skipped"] += 1
        organizer._append_reason(stats, "skip_reasons", reason)
        organizer._append_reason(current, "skip_reasons", reason)
        match = plan.match or MatchResult()
        parsed = organizer._parse_media_fields(plan.original_name)
        position_season, position_episode = resolved_plan_position(plan, parsed)
        target_name = plan.new_name or plan.original_name
        companions = organizer._companions_for_plan(
            plan, companion_files.get(plan.original_path, [])
        )
        write_plan_audit(
            plan, match, plan.target_path + "/" + target_name, "skipped",
            {
                "current_parent_id": plan.original_parent_id,
                "current_name": plan.original_name,
                "target_parent_id": target_parent_id,
                "season": position_season,
                "episode": position_episode,
                "error": reason,
                "legacy_incomplete": False,
            },
            [{
                "file_id": plan.file_id, "role": "video",
                "original_parent_id": plan.original_parent_id,
                "original_name": plan.original_name,
                "current_parent_id": plan.original_parent_id,
                "current_name": plan.original_name,
                "target_parent_id": target_parent_id,
                "target_name": target_name, "size": plan.size, "etag": plan.etag,
                "status": "skipped", "error": reason,
            }, *[{
                "file_id": item.file_id, "role": media_role(item.name),
                "original_parent_id": item.parent_id or plan.original_parent_id,
                "original_name": item.name,
                "current_parent_id": item.parent_id or plan.original_parent_id,
                "current_name": item.name,
                "target_parent_id": target_parent_id,
                "target_name": item.name, "size": item.size, "etag": item.etag,
                "status": "skipped", "error": reason,
            } for item in companions]],
        )

    active_group_key = ""
    completed_group_keys: set[str] = set()
    halted_group_reasons: dict[str, str] = {}
    plan_total = len(plans)
    for plan_index, p in enumerate(plans, start=1):
        group_key = (
            f"{(p.source_group_id or source_dir_id)}\x1f"
            f"{(p.source_group_path or '__root__')}"
        )
        if group_key != active_group_key:
            if active_group_key and active_group_key not in completed_group_keys:
                previous = source_group_rows.get(active_group_key)
                if previous is not None and previous.get("status") == "running":
                    previous["status"] = "completed"
                completed_group_keys.add(active_group_key)
                stats["source_groups_completed"] = len(completed_group_keys)
            active_group_key = group_key
            current_group = source_group_rows.get(group_key)
            if current_group is not None:
                current_group["status"] = "running"
                stats["current_source_group"] = str(
                    current_group.get("name") or current_group.get("path") or ""
                )
        if on_progress is not None:
            # 进度投影属于观测能力，任何异常都不得影响云盘写入序列。
            try:
                on_progress(plan_index, plan_total)
            except Exception:
                logger.debug("整理进度回调失败", exc_info=True)
        if cancel_event and cancel_event.is_set():
            stats["stopped"] = 1
            break
        if group_key in halted_group_reasons:
            # 云端写入不是可回滚的数据库事务。同一媒体组一旦出现不可恢复
            # 写入失败，继续处理余下文件可能形成半季、重复版本或伴随文件错位。
            # 因此只停止当前组，记录余下计划为安全跳过；外层目录组流水线
            # 仍会继续下一个媒体组，实现 fail-fast 与组间失败隔离兼得。
            failure_reason = halted_group_reasons[group_key]
            skip_reason = f"同媒体目录前序写入失败，已停止本组剩余操作：{failure_reason}"
            stats["skipped"] += 1
            current_directory = directory_stats[p.original_path or "/"]
            current_directory["skipped"] += 1
            organizer._append_reason(current_directory, "skip_reasons", skip_reason)
            match = p.match or MatchResult()
            parsed = organizer._parse_media_fields(p.original_name)
            position_season, position_episode = resolved_plan_position(p, parsed)
            write_plan_audit(
                p, match, p.target_path, "skipped",
                {
                    "current_parent_id": p.original_parent_id,
                    "current_name": p.original_name,
                    "season": position_season,
                    "episode": position_episode,
                    "error": skip_reason,
                    "legacy_incomplete": False,
                },
                [{
                    "file_id": p.file_id, "role": "video",
                    "original_parent_id": p.original_parent_id,
                    "original_name": p.original_name,
                    "current_parent_id": p.original_parent_id,
                    "current_name": p.original_name,
                    "target_name": p.new_name,
                    "size": p.size, "etag": p.etag,
                    "status": "skipped", "error": skip_reason,
                }],
            )
            continue
        if p.action != "move":
            match = p.match or MatchResult()
            audit_status = "manual" if match.need_confirm else "skipped"
            parsed = organizer._parse_media_fields(p.original_name)
            position_season, position_episode = resolved_plan_position(p, parsed)
            companions = organizer._companions_for_plan(
                p, companion_files.get(p.original_path, [])
            )
            try:
                write_plan_audit(
                    p, match, "", audit_status,
                    {
                        "current_parent_id": p.original_parent_id,
                        "current_name": p.original_name,
                        "season": position_season,
                        "episode": position_episode,
                        "error": p.note or match.error or "未进入整理执行",
                        "legacy_incomplete": False,
                    },
                    [{
                        "file_id": p.file_id, "role": "video",
                        "original_parent_id": p.original_parent_id,
                        "original_name": p.original_name,
                        "current_parent_id": p.original_parent_id,
                        "current_name": p.original_name, "size": p.size, "etag": p.etag,
                        "status": audit_status, "error": p.note or match.error,
                    }, *[{
                        "file_id": item.file_id, "role": media_role(item.name),
                        "original_parent_id": item.parent_id or p.original_parent_id,
                        "original_name": item.name,
                        "current_parent_id": item.parent_id or p.original_parent_id,
                        "current_name": item.name, "size": item.size, "etag": item.etag,
                        "status": audit_status, "error": p.note or match.error,
                    } for item in companions]],
                )
            except Exception:
                logger.exception("写入跳过整理审计失败 file=%s", p.original_name)
                raise
            continue
        delete_audit_id = None
        existing = None
        replacement_backup_name = ""
        existing_original_name = ""
        target_id: str = ""
        rollback_incomplete = False
        companion_rollback_errors: dict[str, str] = {}
        moved_metadata: list[tuple[GuangYaFile, str]] = []
        replacement_deleted = False
        companion_key = f"{p.original_path}:{normalized_stem(p.original_name)}"
        candidates = companion_files.get(p.original_path, [])
        non_subtitles = [
            item for item in candidates if media_role(item.name) != "subtitle"
        ]
        planned_subtitles = subtitle_plans_by_video.get(p.file_id, [])
        subtitle_plan_by_id = {
            item.file.file_id: item for item in planned_subtitles
        }
        companions = [] if companion_key in moved_companions else [
            *organizer._companions_for_plan(p, non_subtitles),
            *(item.file for item in planned_subtitles),
        ]
        try:
            write_guard = getattr(organizer, "_before_plan_write", None)
            if write_guard is not None:
                write_guard(p, "prepare")
            video_original = (
                source_snapshots[p.file_id] if source_snapshots is not None else GuangYaFile(
                    p.file_id, p.original_name, False, p.size, p.etag, p.original_parent_id,
                )
            )
            if source_snapshots is not None:
                companions = [source_snapshots[item.file_id] for item in companions]
            organizer._verify_remote_snapshot(video_original, role="待整理视频")
            for item in companions:
                organizer._verify_remote_snapshot(
                    GuangYaFile(
                        item.file_id, item.name, False, item.size, item.etag,
                        item.parent_id or p.original_parent_id,
                    ),
                    role="伴随文件",
                )
            target_id = organizer._ensure_dir_chain(
                rules.target_dir_id,
                p.target_path,
                directory_chain_cache,
            )
            if (
                p.original_parent_id
                and target_id
                and p.original_parent_id == target_id
            ):
                reason = "文件已位于目标目录，未执行重复移动、覆盖或回收"
                p.conflict_decision = "already_organized"
                p.conflict_note = reason
                record_runtime_skip(p, target_id, reason)
                continue

            runtime.remember_target_path(
                (rules.target_dir_id or ""), p.target_path, target_id,
            )
            target_snapshot = runtime.load_target_inventory(
                target_id, list_files=organizer.client.list_dir,
                read_revision=read_target_revision, stats=stats,
            )
            target_files = target_snapshot.files
            evidence_names = target_snapshot.evidence_names
            probe_batches, probe_hits = organizer._prime_existing_variant_cache(
                target_files, rules, evidence_names
            )
            stats["target_probe_cache_batches"] = int(
                stats.get("target_probe_cache_batches", 0) or 0
            ) + probe_batches
            stats["target_probe_cache_hits"] = int(
                stats.get("target_probe_cache_hits", 0) or 0
            ) + probe_hits
            existing, conflict_decision, conflict_note = organizer._resolve_variant_conflict(
                p, target_files, rules, evidence_names
            )
            p.conflict_decision = conflict_decision
            p.conflict_note = conflict_note
            existing_original_name = existing.name if existing else ""
            video_target_name = p.new_name if rules.rename_enabled and p.new_name else p.original_name
            companion_targets = []
            for item in companions:
                target_name = item.name
                if rules.rename_enabled:
                    subtitle_plan = subtitle_plan_by_id.get(item.file_id)
                    target_name = (
                        subtitle_plan.target_name(video_target_name) if subtitle_plan is not None
                        else companion_target_name(p.original_name, video_target_name, item.name)
                    )
                companion_targets.append((item, target_name))
            # 可选的授权范围在真实库存仲裁后再校验，且必须早于旧文件备份、移动或回收。
            if write_guard is not None:
                bind_target = _optional_guard_method(write_guard, "bind_target_context")
                if callable(bind_target):
                    bind_target(p, target_id, companions=companions,
                                target_names=[video_target_name, *(name for _, name in companion_targets)])
                write_guard(p, "conflict", target_files=target_files)
            if existing and conflict_decision != "replace":
                record_runtime_skip(
                    p, target_id, conflict_note or "目标存在同媒体文件，按冲突策略跳过",
                )
                continue
            actual_name = p.original_name
            write_journal: list[tuple[GuangYaFile, str]] = []
            try:
                if existing:
                    organizer._verify_remote_snapshot(existing, role="待替换旧文件")
                    # 先登记备份意图；rename 响应丢失也必须回读云端并补偿。
                    replacement_backup_name = (
                        f"{existing.name}.mediaflux-backup-{existing.file_id[-8:]}"
                    )
                    write_journal.append((
                        replace(existing, name=existing_original_name, parent_id=target_id), "replacement",
                    ))
                    organizer.client.rename(existing.file_id, replacement_backup_name)
                    existing.name = replacement_backup_name
                    evidence_names[existing.file_id] = replacement_backup_name
                    stats["conflict"] += 1
                # 目标目录创建和冲突判断也可能耗时；在真正移动前再次缩小
                # 外部改名/移动造成的 TOCTOU 窗口。
                organizer._verify_remote_snapshot(video_original, role="待整理视频")
                if write_guard is not None:
                    write_guard(p, "commit")
                if p.original_parent_id:
                    write_journal.append((video_original, "video"))
                organizer.client.move([p.file_id], target_id)
                if video_target_name != p.original_name:
                    organizer.client.rename(p.file_id, video_target_name)
                    actual_name = video_target_name
                    stats["renamed"] += 1
                for item, target_name in companion_targets:
                    organizer._verify_remote_snapshot(
                        GuangYaFile(
                            item.file_id, item.name, False, item.size, item.etag,
                            item.parent_id or p.original_parent_id,
                        ),
                        role="伴随文件",
                    )
                    companion_guard = _optional_guard_method(write_guard, "before_companion_write")
                    if callable(companion_guard):
                        companion_guard(p, item, target_id, target_name)
                    write_journal.append((
                        replace(item, parent_id=item.parent_id or p.original_parent_id), "companion",
                    ))
                    organizer.client.move([item.file_id], target_id)
                    if target_name != item.name:
                        organizer.client.rename(item.file_id, target_name)
                    moved_metadata.append((item, target_name))
            except Exception:
                # 所有写意图都先记录原快照；无论响应是否收到，按逆序回读并补偿。
                for item, role in reversed(write_journal):
                    try:
                        restore_guangya_file(organizer.client, item)
                    except Exception as rollback_exc:
                        rollback_incomplete = True
                        if role == "companion":
                            companion_rollback_errors[item.file_id] = safe_organize_failure(rollback_exc)
                        logger.error(
                            "回滚整理文件失败 file=%s role=%s type=%s",
                            item.name, role, type(rollback_exc).__name__,
                        )
                    else:
                        if role == "video" and actual_name != p.original_name and stats.get("renamed", 0) > 0:
                            stats["renamed"] -= 1
                        elif role == "replacement":
                            replacement_backup_name = ""
                raise
            if existing and replacement_backup_name:
                candidate = DeleteCandidate(
                    existing.file_id, replacement_backup_name, target_id,
                    existing.size, existing.etag,
                )
                replacement = DeleteCandidate(
                    p.file_id, actual_name, target_id, p.size, p.etag,
                )
                if not rules.recycle_replaced_enabled:
                    delete_audit_id = record_blocked_delete(
                        trigger="replacement",
                        reason="同版本替换后移入光鸭回收站开关已关闭；旧文件保留为备份名",
                        candidate=candidate, replacement=replacement,
                    )
                else:
                    refreshed = None
                    try:
                        refreshed, old_detail, new_detail = (
                            organizer._replacement_verification_snapshot(
                                target_id, existing.file_id, p.file_id
                            )
                        )
                        block_reason = replacement_delete_block_reason(
                            expected_old=GuangYaFile(
                                existing.file_id, replacement_backup_name, False,
                                existing.size, existing.etag, target_id,
                            ),
                            expected_new=GuangYaFile(
                                p.file_id, actual_name, False, p.size, p.etag, target_id,
                            ),
                            old_detail=old_detail, new_detail=new_detail,
                            target_files=refreshed,
                            scan_errors=list(stats.get("scan_errors") or []),
                            move_succeeded=True,
                        )
                    except Exception as verify_exc:
                        logger.warning(
                            "替换删除验证失败 type=%s",
                            type(verify_exc).__name__,
                        )
                        block_reason = "扫描错误，禁止将旧文件移入回收站"
                    if refreshed is not None:
                        target_snapshot = runtime.store_inventory(
                            target_id, refreshed,
                            {
                                item.file_id: item.name
                                for item in refreshed
                                if not item.is_dir
                            },
                        )
                        target_files = target_snapshot.files
                        evidence_names = target_snapshot.evidence_names
                    if block_reason:
                        delete_audit_id = record_blocked_delete(
                            trigger="replacement", reason=block_reason,
                            candidate=candidate, replacement=replacement,
                        )
                    else:
                        try:
                            result = execute_recycle_bin_delete(
                                organizer.client, trigger="replacement",
                                reason=conflict_note or "同版本新文件胜出",
                                candidate=candidate, replacement=replacement,
                                safe_failure_message="光鸭回收站操作失败",
                            )
                            delete_audit_id = result["audit_id"]
                            replacement_deleted = True
                            target_files[:] = [
                                item for item in target_files
                                if item.file_id != existing.file_id
                            ]
                            evidence_names.pop(existing.file_id, None)
                        except Exception as delete_exc:
                            logger.error(
                                "新文件已就位，但旧文件回收站操作失败 file=%s type=%s",
                                replacement_backup_name, type(delete_exc).__name__,
                            )
                            stats["replacement_cleanup_failed"] = int(
                                stats.get("replacement_cleanup_failed", 0) or 0
                            ) + 1
            if not any(item.file_id == p.file_id for item in target_files):
                target_files.append(GuangYaFile(
                    file_id=p.file_id,
                    name=actual_name,
                    is_dir=False,
                    size=p.size,
                    etag=p.etag,
                    parent_id=target_id,
                ))
            evidence_names[p.file_id] = f"{p.original_name} {actual_name}"
            for item, target_name in moved_metadata:
                if not any(existing_item.file_id == item.file_id for existing_item in target_files):
                    target_files.append(GuangYaFile(
                        file_id=item.file_id,
                        name=target_name,
                        is_dir=False,
                        size=item.size,
                        etag=item.etag,
                        parent_id=target_id,
                    ))
                evidence_names[item.file_id] = target_name
            runtime.note_inventory_write(target_id)
            checkpoint_target_revision(target_id)
            if companions:
                stats["metadata_moved"] += len(companions)
                stats["subtitle_moved"] += sum(
                    1 for item in companions if media_role(item.name) == "subtitle"
                )
                directory_stats[p.original_path or "/"]["metadata_moved"] += len(companions)
            moved_companions.add(companion_key)
            stats["moved"] += 1
            directory_stats[p.original_path or "/"]["moved"] += 1
            parsed = organizer._parse_media_fields(p.original_name)
            position_season, position_episode = resolved_plan_position(p, parsed)
            season_inventory = None
            assert p.match is not None
            if p.match.media_type == "tv" and position_season is not None:
                inventory_key = (target_id, position_season)
                season_inventory = target_episode_inventory_cache.setdefault(
                    inventory_key, []
                )
                season_inventory[:] = organizer._season_episode_inventory(
                    target_files,
                    rules,
                    season=position_season,
                ) or []
            stats.setdefault("media_items", []).append(
                media_notification_item(
                    p,
                    actual_name,
                    parsed,
                    season_present_episodes=season_inventory,
                )
            )
            log_id = None
            try:
                log_id = write_plan_audit(
                    p, p.match, p.target_path + "/" + actual_name, "success",
                    {
                        "current_parent_id": target_id,
                        "current_name": actual_name,
                        "target_parent_id": target_id,
                        "season": position_season,
                        "episode": position_episode,
                        "legacy_incomplete": False,
                    },
                    [{
                        "file_id": p.file_id, "role": "video",
                        "original_parent_id": p.original_parent_id,
                        "original_name": p.original_name, "current_parent_id": target_id,
                        "current_name": actual_name, "target_parent_id": target_id,
                        "target_name": actual_name, "size": p.size, "etag": p.etag,
                        "status": "success",
                    }, *[{
                        "file_id": item.file_id, "role": media_role(item.name),
                        "original_parent_id": item.parent_id or p.original_parent_id,
                        "original_name": item.name, "current_parent_id": target_id,
                        "current_name": target_name, "target_parent_id": target_id,
                        "target_name": target_name, "size": item.size, "etag": item.etag,
                        "status": "success",
                    } for item, target_name in moved_metadata]],
                )
                if delete_audit_id and isinstance(log_id, int):
                    db.update_organize_delete_audit(
                        delete_audit_id, organize_log_id=log_id
                    )
                if p.media_probe_pending and isinstance(log_id, int):
                    try:
                        from app.modules.organize import organize_rules_snapshot

                        db.enqueue_organize_probe_completion(
                            log_id,
                            source_id=(rules.target_dir_id or ""),
                            rel_dir=(p.target_path or ""),
                            rules=organize_rules_snapshot(rules),
                            notification_context=notification_context,
                            delay_seconds=130,
                            max_attempts=2,
                        )
                        stats["media_probe_background_queued"] = int(
                            stats.get("media_probe_background_queued", 0) or 0
                        ) + 1
                        from app.modules.organize_probe_worker import (
                            get_organize_probe_worker,
                        )
                        get_organize_probe_worker().wake()
                    except Exception as queue_exc:
                        stats["media_probe_background_queue_failed"] = int(
                            stats.get("media_probe_background_queue_failed", 0) or 0
                        ) + 1
                        logger.warning(
                            "媒体规格后台补全入队失败 file=%s type=%s",
                            p.file_id, type(queue_exc).__name__,
                        )
                stats.setdefault("strm_changes", []).append({
                    "source_id": rules.target_dir_id,
                    "kind": "video",
                    "action": "upsert",
                    "file_id": p.file_id,
                    "rel_dir": (p.target_path or ""),
                    "name": actual_name,
                    "etag": (p.etag or ""),
                    "size": (p.size or 0),
                    "parent_id": target_id,
                })
                for item, target_name in moved_metadata:
                    stats["strm_changes"].append({
                        "source_id": rules.target_dir_id,
                        "kind": "metadata",
                        "action": "upsert",
                        "file_id": item.file_id,
                        "rel_dir": (p.target_path or ""),
                        "name": target_name,
                        "etag": (item.etag or ""),
                        "size": (item.size or 0),
                        "parent_id": target_id,
                    })
                if existing and replacement_deleted:
                    stats["strm_changes"].append({
                        "source_id": rules.target_dir_id,
                        "kind": "video",
                        "action": "remove",
                        "file_id": existing.file_id,
                    })
                elif existing:
                    # 旧文件仍以备份名留在云端；只有全量扫描才能安全判断
                    # 旧索引、备份文件和新赢家之间的最终状态。
                    stats["strm_force_full"] = True
            except Exception as audit_exc:
                audit_log_id = getattr(audit_exc, "log_id", None)
                if isinstance(audit_log_id, int):
                    log_id = audit_log_id
                # 云盘移动已经成功，任何审计落库异常都不能再记作移动
                # 失败或触发重试，否则可能重复整理同一文件。
                stats["audit_failures"] = int(stats.get("audit_failures", 0) or 0) + 1
                stats["strm_force_full"] = True
                if isinstance(log_id, int):
                    try:
                        db.update_organize_log(
                            log_id,
                            legacy_incomplete=True,
                            error="云盘整理成功，但操作明细写入失败，请人工核对",
                        )
                    except Exception as mark_exc:
                        logger.error(
                            "标记整理审计不完整失败 file=%s type=%s",
                            p.original_name, type(mark_exc).__name__,
                        )
                logger.error(
                    "整理成功但审计写入失败 file=%s type=%s",
                    p.original_name, type(audit_exc).__name__,
                )

        except Exception as e:
            if target_id:
                runtime.invalidate_inventory(target_id)
            failure_message = safe_organize_failure(e)
            if existing and rollback_incomplete:
                failure_message += "；替换回滚未完成，请人工核验新旧文件"
            logger.error(
                "文件整理失败 file=%s type=%s",
                p.original_name, type(e).__name__,
            )
            if existing and not delete_audit_id:
                delete_audit_id = record_blocked_delete(
                    trigger="replacement",
                    reason=f"移动失败，禁止将旧文件移入回收站：{failure_message}",
                    candidate=DeleteCandidate(
                        existing.file_id,
                        replacement_backup_name or existing_original_name,
                        target_id or existing.parent_id,
                        existing.size, existing.etag,
                    ),
                    replacement=DeleteCandidate(
                        p.file_id, p.new_name or p.original_name,
                        target_id, p.size, p.etag,
                    ),
                )
            stats["failed"] += 1
            directory_stats[p.original_path or "/"]["failed"] += 1
            halted_group_reasons[group_key] = failure_message
            current_group = source_group_rows.get(group_key)
            if current_group is not None:
                current_group["write_halted"] = True
                current_group["error"] = failure_message
            parsed = organizer._parse_media_fields(p.original_name)
            position_season, position_episode = resolved_plan_position(p, parsed)
            failed_log_id = write_plan_audit(
                p, p.match, p.target_path, "failed",
                {
                    "current_parent_id": "" if rollback_incomplete else p.original_parent_id,
                    "current_name": "" if rollback_incomplete else p.original_name,
                    "season": position_season,
                    "episode": position_episode,
                    "error": failure_message,
                    "legacy_incomplete": rollback_incomplete,
                },
                [{
                    "file_id": p.file_id, "role": "video",
                    "original_parent_id": p.original_parent_id,
                    "original_name": p.original_name,
                    "current_parent_id": "" if rollback_incomplete else p.original_parent_id,
                    "current_name": "" if rollback_incomplete else p.original_name,
                    "target_name": p.new_name,
                    "size": p.size, "etag": p.etag,
                    "status": "failed", "error": failure_message,
                }, *[{
                    "file_id": item.file_id, "role": media_role(item.name),
                    "original_parent_id": item.parent_id or p.original_parent_id,
                    "original_name": item.name,
                    "current_parent_id": (
                        "" if str(item.file_id) in companion_rollback_errors
                        else item.parent_id or p.original_parent_id
                    ),
                    "current_name": (
                        "" if str(item.file_id) in companion_rollback_errors else item.name
                    ),
                    "target_parent_id": target_id,
                    "target_name": next((
                        name for moved_item, name in moved_metadata
                        if moved_item.file_id == item.file_id
                    ), item.name),
                    "size": item.size, "etag": item.etag,
                    "status": "failed",
                    "error": (
                        f"{failure_message}；伴随文件回滚失败："
                        f"{companion_rollback_errors[str(item.file_id)]}"
                        if str(item.file_id) in companion_rollback_errors
                        else failure_message
                    ),
                } for item in companions]],
            )
            if delete_audit_id and isinstance(failed_log_id, int):
                db.update_organize_delete_audit(
                    delete_audit_id, organize_log_id=failed_log_id
                )
    if active_group_key and active_group_key not in completed_group_keys:
        completed_group_keys.add(active_group_key)
    for group_key, row in source_group_rows.items():
        totals = {
            "moved": 0, "metadata_moved": 0, "skipped": 0,
            "need_confirm": 0, "failed": 0,
        }
        for path in source_group_paths.get(group_key, set()):
            current = directory_stats.get(path, {})
            for key in totals:
                totals[key] += int(current.get(key, 0) or 0)
        row.update(totals)
        if stats.get("stopped") and row.get("status") == "running":
            row["status"] = "stopped"
        elif stats.get("stopped") and row.get("status") == "planned":
            row["status"] = "pending"
        elif totals["failed"]:
            row["status"] = "partial"
        elif totals["need_confirm"]:
            row["status"] = "attention"
        elif row.get("status") in {"planned", "running"}:
            row["status"] = "completed"
    stats["source_groups_completed"] = sum(
        1 for row in source_group_rows.values()
        if row.get("status") in {"completed", "partial", "attention"}
    )
    stats["current_source_group"] = ""
    stats["directories"] = directory_stats
