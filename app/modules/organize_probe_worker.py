"""整理完成后的媒体规格后台补全。

前台整理只登记探测失败项；本 worker 延迟、低并发重试 ffprobe。只有在
云端快照、目标名称和伴随文件全部复核通过后才短暂获取整理写锁完成改名，
随后触发一次 STRM 精准同步。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile, close_guangya_client
from app.logger import get_logger
from app.modules.guangya_compensation import (
    GuangYaCompensationError,
    restore_guangya_file,
)
from app.modules.organize_postprocess import companion_target_name
from app.modules.process_lock import CrossProcessLock
from app.repositories.organize_probe import normalize_notification_context

logger = get_logger(__name__)


class _ProbeCompletionCancelled(RuntimeError):
    """快照或业务状态已变化，任务必须停止且不重试。"""


class _ProbeCompletionUnavailable(RuntimeError):
    """本次仍未取得媒体规格，可按有限退避重试。"""


class _ProbeCompensationFailed(RuntimeError):
    """本任务写入恢复不确定；停止外部写入，按现有失败次数收束人工核验。"""


class _ProbeHandoffUnavailable(RuntimeError):
    """改名已提交但交接未确认；保留载荷重试，不消耗探测次数。"""


class OrganizeProbeWorker:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._consumer_lock = CrossProcessLock("organize-probe-worker")
        self._organize_write_lock = CrossProcessLock("guangya-organize")
        self._owner = f"organize-probe-{uuid.uuid4().hex[:12]}"
        self._client: GuangYaClient | None = None
        self._current_job_id = 0

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="organize-probe-worker", daemon=True,
            )
            self._thread.start()
        self._wake_event.set()
        logger.info("整理媒体规格后台补全器已启动")

    def stop(self, timeout: float = 30.0) -> bool:
        with self._state_lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout or 0.1)))
        if thread and thread.is_alive():
            logger.warning("整理媒体规格后台补全器未能在关闭超时内结束")
            return False

        with self._state_lock:
            # join 期间允许显式 restart；只清理本轮捕获的旧线程，不能抹掉
            # 新线程句柄或关闭它正在复用的 HTTP Client。
            if self._thread is not thread:
                logger.warning("整理媒体规格后台补全器在关闭期间已重新启动")
                return False
            self._thread = None
            client = self._client
            self._client = None
        close_guangya_client(client)
        return True

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        return {
            **db.count_organize_probe_jobs(),
            "worker_running": bool(self._thread and self._thread.is_alive()),
            "current_job_id": self._current_job_id,
        }

    def _runtime_client(self) -> GuangYaClient:
        if self._client is None:
            self._client = GuangYaClient()
        return self._client

    def _loop(self) -> None:
        owns_lock = False
        try:
            while not self._stop_event.is_set():
                if self._consumer_lock.acquire(blocking=False):
                    owns_lock = True
                    break
                self._stop_event.wait(1.0)
            if not owns_lock:
                return
            recovery_pending = True
            while not self._stop_event.is_set():
                try:
                    if recovery_pending:
                        recovered = db.recover_stale_organize_probe_jobs(force=True)
                        if recovered:
                            logger.warning("已恢复中断的媒体规格补全任务 count=%s", recovered)
                        recovery_pending = False
                    worked = self._process_one()
                except Exception:
                    logger.exception("媒体规格后台补全轮询异常")
                    worked = False
                if worked:
                    self._stop_event.wait(0.5)
                    continue
                self._wake_event.wait(15.0)
                self._wake_event.clear()
        finally:
            if owns_lock:
                self._consumer_lock.release()

    @staticmethod
    def _row_dict(row) -> dict:
        return dict(row) if row is not None else {}

    @staticmethod
    def _verify_snapshot(remote: GuangYaFile | None, item: dict) -> GuangYaFile:
        label = str(item.get("current_name") or item.get("file_id") or "文件")
        if remote is None:
            raise _ProbeCompletionUnavailable(f"暂时无法读取云端文件详情: {label}")
        if (remote.file_id or "") != str(item.get("file_id") or ""):
            raise _ProbeCompletionCancelled(f"云端文件身份不一致: {label}")
        expected_parent = str(item.get("current_parent_id") or "")
        expected_name = str(item.get("current_name") or "")
        if expected_parent and (remote.parent_id or "") != expected_parent:
            raise _ProbeCompletionCancelled(f"文件位置已被外部修改: {label}")
        if expected_name and (remote.name or "") != expected_name:
            raise _ProbeCompletionCancelled(f"文件名已被外部修改: {label}")
        expected_size = int(item.get("size") or 0)
        if expected_size and (remote.size or 0) and (remote.size or 0) != expected_size:
            raise _ProbeCompletionCancelled(f"文件大小已变化: {label}")
        expected_etag = str(item.get("etag") or "")
        if expected_etag and (remote.etag or "") and remote.etag != expected_etag:
            raise _ProbeCompletionCancelled(f"文件校验值已变化: {label}")
        return remote

    @staticmethod
    def _rules_from_job(job: dict):
        from app.modules.organize import restore_organize_rules_snapshot

        try:
            raw = json.loads(str(job.get("rules_json") or "{}"))
        except (TypeError, ValueError):
            raw = {}
        return restore_organize_rules_snapshot(raw)

    def _desired_plan(
        self, job: dict, log: dict, video: dict, remote: GuangYaFile, profile,
    ):
        from app.modules.nsfw import extract_nsfw_part_index
        from app.modules.organize import OrganizePlan, Organizer
        from app.modules.scraper import MatchResult

        match = MatchResult(
            tmdb_id=str(log.get("tmdb_id") or ""),
            title=str(log.get("title") or ""),
            year=str(log.get("year") or ""),
            media_type=str(log.get("media_type") or ""),
            confidence=1.0,
            locked=True,
            status="matched",
            provider=str(log.get("provider") or "tmdb"),
            external_id=str(log.get("external_id") or ""),
        )
        plan = OrganizePlan(
            file_id=str(video.get("file_id") or ""),
            original_name=str(log.get("original_name") or video.get("original_name") or ""),
            original_path=str(log.get("original_path") or ""),
            original_parent_id=str(log.get("original_parent_id") or ""),
            size=int(video.get("size") or 0),
            etag=str(video.get("etag") or ""),
            match=match,
            season=log.get("season"),
            episode=log.get("episode"),
            target_path=str(job.get("rel_dir") or ""),
            action="move",
        )
        organizer = Organizer(client=self._runtime_client(), recognition_enabled=False)
        rules = self._rules_from_job(job)
        # 前台已将 part/CD 统一为归档名中的 CDn；仅恢复已验证的视频名
        # 中的身份，不重新识别标题，也不让规格补全吞掉分片。
        part = (
            extract_nsfw_part_index(remote.name)
            if organizer._match_provider(match) in {"metatube", "clean_title"} else None
        )
        organizer._apply_media_profile_to_move_plan(
            plan, remote, rules, match,
            {"season": log.get("season"), "episode": log.get("episode"), "part": part},
            profile,
        )
        return organizer, rules, plan

    def _acquire_write_lock(self) -> bool:
        while not self._stop_event.is_set():
            if self._organize_write_lock.acquire(blocking=False):
                return True
            self._stop_event.wait(0.25)
        return False

    def _apply_remote_rename(
        self,
        *,
        job: dict,
        log: dict,
        items: list[dict],
        video: dict,
        desired_name: str,
        link_strm: bool = False,
    ) -> list[dict]:
        client = self._runtime_client()
        if not self._acquire_write_lock():
            raise InterruptedError("服务正在停止")
        journal: list[tuple[str, str, str]] = []
        step_ids: dict[str, int] = {}
        operation_token = f"probe:{int(job['id'])}:{uuid.uuid4().hex}"
        committed = False
        changes: list[dict] = []
        try:
            refreshed: dict[str, GuangYaFile] = {}
            for item in items:
                remote = self._verify_snapshot(
                    client.file_info(str(item.get("file_id") or "")), item,
                )
                refreshed[str(item.get("file_id") or "")] = remote

            old_video_name = str(video.get("current_name") or "")
            targets: dict[str, str] = {}
            for item in items:
                file_id = str(item.get("file_id") or "")
                if item.get("role") == "video":
                    target_name = desired_name
                else:
                    target_name = companion_target_name(
                        old_video_name, desired_name, str(item.get("current_name") or ""),
                    )
                targets[file_id] = target_name

            target_names = [name.casefold() for name in targets.values() if name]
            if len(target_names) != len(set(target_names)):
                raise _ProbeCompletionCancelled("媒体规格补全后的目标文件名发生组内冲突")
            parent_id = str(video.get("current_parent_id") or "")
            allowed_ids = set(targets)
            for entry in client.list_dir(parent_id):
                if entry.is_dir or entry.file_id in allowed_ids:
                    continue
                if (entry.name or "").casefold() in set(target_names):
                    raise _ProbeCompletionCancelled(
                        f"目标目录已有同名文件，已停止后台补全: {entry.name}"
                    )

            # 先改伴随文件、最后改主视频；任一步失败都会按相反顺序恢复。
            ordered = [item for item in items if item.get("role") != "video"] + [video]
            for item in ordered:
                file_id = str(item.get("file_id") or "")
                current_name = str(item.get("current_name") or "")
                target_name = targets[file_id]
                if not target_name or target_name == current_name:
                    continue
                # 复用已有步骤表记录写意图；进程中断也不能将自己的改名误认成外部变化。
                step_ids[file_id] = db.add_organize_operation_step(
                    int(log["id"]), operation_token, len(step_ids) + 1, "probe_rename",
                    file_id=file_id, from_parent_id=str(item.get("current_parent_id") or ""),
                    from_name=current_name, to_parent_id=str(item.get("current_parent_id") or ""),
                    to_name=target_name, status="running",
                )
                journal.append((file_id, target_name, current_name))
                if client.rename(file_id, target_name) is False:
                    raise RuntimeError(f"云端改名失败: {current_name}")

            rel_dir = str(job.get("rel_dir") or "")
            item_updates: list[dict] = []
            for item in items:
                file_id = str(item.get("file_id") or "")
                target_name = targets[file_id]
                item_updates.append({
                    "id": int(item["id"]),
                    "expected_name": str(item.get("current_name") or ""),
                    "current_name": target_name,
                    "target_name": target_name,
                })
                changes.append({
                    "source_id": str(job.get("source_id") or ""),
                    "kind": "video" if item.get("role") == "video" else "metadata",
                    "action": "upsert",
                    "file_id": file_id,
                    "rel_dir": rel_dir,
                    "name": target_name,
                    "etag": str(item.get("etag") or ""),
                    "size": int(item.get("size") or 0),
                    "parent_id": str(item.get("current_parent_id") or ""),
                })
            new_path = "/".join(part for part in (rel_dir, desired_name) if part)
            if not db.commit_organize_probe_rename(
                int(log["id"]), current_name=desired_name, new_path=new_path,
                item_updates=item_updates,
                job_id=int(job["id"]), owner=self._owner,
                changes=changes if link_strm else [],
            ):
                raise _ProbeCompletionCancelled("整理日志或任务 lease 状态已变化")
            committed = True
            job["rename_committed"] = True
            # 后续异常必须按提交后交接处理，不能消耗探测次数或重复改名。
            job["pending_strm_changes_json"] = json.dumps(
                changes if link_strm else [], ensure_ascii=False,
            )
            for step_id in step_ids.values():
                if not db.finish_organize_operation_step(step_id, "success"):
                    raise _ProbeHandoffUnavailable("改名已提交，操作步骤收尾待重试")
            return changes
        except Exception as failure:
            if committed:
                raise _ProbeHandoffUnavailable("改名已提交，操作步骤收尾待重试") from failure
            rollback_errors: list[str] = []
            by_file = {str(item["file_id"]): item for item in items}
            for file_id, _current_name, old_name in reversed(journal):
                item = by_file[file_id]
                try:
                    restore_guangya_file(client, GuangYaFile(
                        file_id, old_name, False, int(item.get("size") or 0),
                        str(item.get("etag") or ""), str(item.get("current_parent_id") or ""),
                    ))
                    if not db.finish_organize_operation_step(step_ids[file_id], "rolled_back"):
                        raise RuntimeError("补偿步骤状态未持久化")
                except Exception as exc:
                    db.finish_organize_operation_step(step_ids[file_id], "rollback_failed", "补偿结果需要人工核验")
                    remote = exc.snapshot if isinstance(exc, GuangYaCompensationError) else None
                    db.update_organize_log_item(
                        int(item["id"]), status="rollback_failed",
                        current_parent_id=remote.parent_id if remote else "",
                        current_name=remote.name if remote else "",
                        error="媒体规格补全回滚无法确认，必须人工核验",
                    )
                    rollback_errors.append(f"{file_id}:{type(exc).__name__}")
            if rollback_errors:
                db.update_organize_log(
                    int(log["id"]), status="partial_failed", legacy_incomplete=True,
                    error="媒体规格补全回滚无法确认，必须人工核验",
                )
                raise _ProbeCompensationFailed(
                    "媒体规格补全回滚无法确认，必须人工核验: " + ",".join(rollback_errors)
                )
            raise
        finally:
            self._organize_write_lock.release()

    def _handoff_pending(self, job: dict) -> bool:
        """重启也只依赖持久载荷；缺配置或下游拒绝时绝不能确认丢弃。"""
        try:
            changes = json.loads(job.get("pending_strm_changes_json", "[]"))
            if not isinstance(changes, list) or any(not isinstance(item, dict) for item in changes):
                raise ValueError("规格补全交接载荷格式无效")
            if not changes:
                return False
            from app.modules.organize import Organizer
            from app.modules.organize_probe_notifications import tag_probe_changes

            rules = self._rules_from_job(job)
            context = normalize_notification_context(job.get("notification_context_json"))
            stats = {
                "moved": 1, "failed": 0,
                "strm_changes": tag_probe_changes(changes, context, notify_enabled=rules.notify_enabled),
                "notification_context": context,
                "probe_completion": True,
            }
            Organizer._post_organize_link(stats, rules, force_incremental=True)
            outcome = stats.get("strm")
            if not isinstance(outcome, dict) or outcome.get("ok") is not True:
                raise _ProbeHandoffUnavailable("STRM 尚未持久接管规格补全变化，保留待交接任务")
            return True
        except _ProbeHandoffUnavailable:
            raise
        except Exception as exc:
            raise _ProbeHandoffUnavailable(
                f"规格补全交接暂不可用（{type(exc).__name__}），保留待交接任务"
            ) from exc

    def _recover_unfinished_rename(self, job: dict) -> bool:
        """旧进程的写意图只按已提交 DB 事实收尾，否则冻结人工核验，不猜着续写。"""
        try:
            steps = [dict(row) for row in db.list_pending_organize_probe_steps(
                int(job["organize_log_id"]), int(job["id"]), include_succeeded=True,
            )]
        except sqlite3.Error as exc:
            # 读不到恢复事实不等于尚未提交，数据库故障不消耗探测重试预算。
            raise _ProbeHandoffUnavailable("规格补全恢复步骤暂不可读取") from exc
        if not steps:
            return False
        unresolved = [step for step in steps if step["status"] != "success"]
        # 已完成的历史操作不混入当前未决意图；只有收尾时才读取最近成功操作。
        latest_token = steps[0]["operation_token"]
        steps = unresolved or [step for step in steps if step["operation_token"] == latest_token]
        if not self._acquire_write_lock():
            raise InterruptedError("服务正在停止")
        try:
            try:
                log = self._row_dict(db.get_organize_log(int(job["organize_log_id"])))
                items = {str(row["file_id"]): dict(row) for row in db.list_organize_log_items(int(job["organize_log_id"]))}
            except sqlite3.Error as exc:
                raise _ProbeHandoffUnavailable("规格补全业务快照暂不可读取") from exc
            if log.get("status") == "partial_failed":
                raise _ProbeCompensationFailed("上次规格补全无法确认，必须人工核验")
            if log.get("status") != "success":
                raise _ProbeCompletionCancelled("整理日志状态已变化")
            committed = all(
                (items.get(str(step["file_id"]), {}).get("current_name"),
                 items.get(str(step["file_id"]), {}).get("current_parent_id"))
                == (step["to_name"], step["to_parent_id"])
                for step in steps
            )
            if committed:
                # 同一日志的 probe 队列幂等；成功步骤即跨进程的提交事实。
                # 即使所有步骤已成功，任务 ack 失败也不能回到媒体探测。
                job["rename_committed"] = True
                try:
                    for step in steps:
                        if step["status"] != "success" and not db.finish_organize_operation_step(int(step["id"]), "success"):
                            raise _ProbeHandoffUnavailable("已提交改名的步骤收尾尚未完成")
                except Exception as exc:
                    raise _ProbeHandoffUnavailable("已提交改名的步骤收尾暂不可用") from exc
                return True
            if not unresolved:
                raise _ProbeCompletionCancelled("整理记录已更新，旧规格补全任务不再适用")
            for step in steps:
                item = items.get(str(step["file_id"]), {})
                current = (item.get("current_parent_id"), item.get("current_name"))
                known_positions = (
                    (step["from_parent_id"], step["from_name"]),
                    (step["to_parent_id"], step["to_name"]),
                )
                if all(current) and current not in known_positions:
                    # 后续人工纠偏已保存不同位置；旧意图不能清空新的可信快照。
                    raise _ProbeCompletionCancelled("整理记录已被后续操作更新，旧规格补全任务已取消")
            reason = "上次媒体规格改名期间中断，文件状态无法确认，必须人工核验"
            for step in steps:
                item = items.get(str(step["file_id"]))
                if item is not None:
                    db.update_organize_log_item(int(item["id"]), status="rollback_failed", current_name="", current_parent_id="", error=reason)
                db.finish_organize_operation_step(int(step["id"]), "rollback_failed", reason)
            db.update_organize_log(int(log["id"]), status="partial_failed", legacy_incomplete=True, error=reason)
            raise _ProbeCompensationFailed(reason)
        finally:
            self._organize_write_lock.release()

    def _execute_job(self, job: dict) -> bool:
        rename_committed = self._recover_unfinished_rename(job)
        # 优先补交接：不能依赖探测可用性、云端访问或名称是否仍需变化。
        if self._handoff_pending(job):
            return True
        if rename_committed:
            return False
        log = self._row_dict(db.get_organize_log(int(job["organize_log_id"])))
        if log.get("status") == "partial_failed":
            raise _ProbeCompensationFailed("上次云端写入/补偿未能确认，必须人工核验，不自动续写")
        if not log or str(log.get("status") or "") != "success":
            raise _ProbeCompletionCancelled("整理日志状态已变化")
        if bool(log.get("legacy_incomplete")):
            raise _ProbeCompletionCancelled("整理日志快照不完整")
        items = [self._row_dict(item) for item in db.list_organize_log_items(int(log["id"]))]
        videos = [item for item in items if item.get("role") == "video"]
        if len(videos) != 1:
            raise _ProbeCompletionCancelled("整理日志缺少唯一主视频快照")
        video = videos[0]
        client = self._runtime_client()
        remote = self._verify_snapshot(
            client.file_info(str(video.get("file_id") or "")), video,
        )

        from app.modules.media_probe import probe_media_profile

        profile = probe_media_profile(
            remote, client, enabled=True, timeout=30, cache_only=False,
            cancel_event=self._stop_event,
        )
        if self._stop_event.is_set():
            raise InterruptedError("服务正在停止")
        if profile is None:
            raise _ProbeCompletionUnavailable("媒体规格仍不可用")

        _organizer, rules, plan = self._desired_plan(job, log, video, remote, profile)
        desired_name = (plan.new_name or remote.name)
        if desired_name == str(video.get("current_name") or ""):
            return False
        self._apply_remote_rename(
            job=job, log=log, items=items, video=video, desired_name=desired_name,
            link_strm=rules.link_strm,
        )
        handoff_completed = self._handoff_pending(job)
        logger.debug(
            "媒体规格后台补全完成 log=%s file=%s renamed=%s",
            log.get("id"), video.get("file_id"), desired_name,
        )
        return handoff_completed

    def _process_one(self) -> bool:
        jobs = db.claim_due_organize_probe_jobs(
            owner=self._owner, lease_seconds=1800, limit=1,
        )
        if not jobs:
            return False
        job = jobs[0]
        job_id = int(job["id"])
        notification_error = ""
        with self._state_lock:
            self._current_job_id = job_id
        try:
            handoff_completed = self._execute_job(job)
            if not db.complete_organize_probe_job(
                job_id, owner=self._owner, handoff_completed=handoff_completed,
            ):
                raise _ProbeHandoffUnavailable("任务 lease 已变化，未确认规格补全完成")
            # noop 也是本批次的已提交终态，不能仅在发生 STRM 交接时收尾。
            try:
                from app.modules.organize_probe_notifications import (
                    publish_probe_acknowledged,
                )

                publish_probe_acknowledged(job)
            except Exception as exc:
                logger.warning("规格补全完成通知更新失败 type=%s", type(exc).__name__)
        except _ProbeHandoffUnavailable as exc:
            db.release_organize_probe_job(
                job_id, owner=self._owner, delay_seconds=30, reason=exc,
            )
            notification_error = "后台规格补全交接尚未完成，将保留任务重试。"
        except _ProbeCompletionCancelled as exc:
            if db.cancel_organize_probe_job(job_id, owner=self._owner, reason=exc):
                notification_error = "后台规格补全因身份或快照变化取消，请在 Web 运行记录中复核。"
            logger.debug("媒体规格补全任务已取消 job=%s reason=%s", job_id, exc)
        except InterruptedError as exc:
            db.release_organize_probe_job(
                job_id, owner=self._owner, delay_seconds=30, reason=exc,
            )
        except _ProbeCompletionUnavailable as exc:
            status = db.fail_or_retry_organize_probe_job(
                job_id, owner=self._owner, error_type="ProbeUnavailable", error=exc,
                base_backoff_seconds=600,
            )
            if status == "failed":
                notification_error = "后台规格补全达到重试上限，请在 Web 运行记录中复核。"
            logger.log(
                logging.WARNING if status == "failed" else logging.DEBUG,
                "媒体规格补全处理结果 job=%s status=%s",
                job_id,
                status,
            )
        except Exception as exc:
            if job.get("rename_committed") or job.get("pending_strm_changes_json", "[]") != "[]":
                # 包含下游已接管但本地完成状态写入失败：允许幂等重投，
                # 不能让 max_attempts 将已提交的交接永久终结。
                db.release_organize_probe_job(
                    job_id, owner=self._owner, delay_seconds=30,
                    reason=f"交接完成状态暂不可用（{type(exc).__name__}）",
                )
                return True
            status = db.fail_or_retry_organize_probe_job(
                job_id, owner=self._owner, error_type=type(exc).__name__, error=exc,
                base_backoff_seconds=600,
            )
            if status == "failed":
                notification_error = "后台规格补全失败，请在 Web 运行记录中复核。"
            logger.warning(
                "媒体规格后台补全失败 job=%s status=%s type=%s",
                job_id, status, type(exc).__name__,
            )
        finally:
            if notification_error:
                try:
                    from app.modules.organize_probe_notifications import publish_probe_scope

                    publish_probe_scope(
                        {"probe": True,
                         "context": normalize_notification_context(job.get("notification_context_json")),
                         "notify_override": self._rules_from_job(job).notify_enabled},
                        strm_status="后台规格补全待复核", media_refresh="",
                        partial=True, error=notification_error,
                    )
                except Exception as exc:
                    logger.warning("规格补全异常通知更新失败 type=%s", type(exc).__name__)
            with self._state_lock:
                self._current_job_id = 0
        return True


_worker = OrganizeProbeWorker()


def get_organize_probe_worker() -> OrganizeProbeWorker:
    return _worker
