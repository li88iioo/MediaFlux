"""整理完成后的媒体规格后台补全。

前台整理只登记探测失败项；本 worker 延迟、低并发重试 ffprobe。只有在
云端快照、目标名称和伴随文件全部复核通过后才短暂获取整理写锁完成改名，
随后触发一次 STRM 精准同步。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import fields

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.logger import get_logger
from app.modules.organize_postprocess import companion_target_name
from app.modules.process_lock import CrossProcessLock

logger = get_logger(__name__)


class _ProbeCompletionCancelled(RuntimeError):
    """快照或业务状态已变化，任务必须停止且不重试。"""


class _ProbeCompletionUnavailable(RuntimeError):
    """本次仍未取得媒体规格，可按有限退避重试。"""


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
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout or 0.1)))
        stopped = not thread or not thread.is_alive()
        if stopped:
            self._thread = None
            client = self._client
            self._client = None
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        else:
            logger.warning("整理媒体规格后台补全器未能在关闭超时内结束")
        return stopped

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        return {
            **db.count_organize_probe_jobs(),
            "worker_running": bool(self._thread and self._thread.is_alive()),
            "current_job_id": int(self._current_job_id),
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
            recovered = db.recover_stale_organize_probe_jobs(force=True)
            if recovered:
                logger.warning("已恢复中断的媒体规格补全任务 count=%s", recovered)
            while not self._stop_event.is_set():
                try:
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
        if str(remote.file_id or "") != str(item.get("file_id") or ""):
            raise _ProbeCompletionCancelled(f"云端文件身份不一致: {label}")
        expected_parent = str(item.get("current_parent_id") or "")
        expected_name = str(item.get("current_name") or "")
        if expected_parent and str(remote.parent_id or "") != expected_parent:
            raise _ProbeCompletionCancelled(f"文件位置已被外部修改: {label}")
        if expected_name and str(remote.name or "") != expected_name:
            raise _ProbeCompletionCancelled(f"文件名已被外部修改: {label}")
        expected_size = int(item.get("size") or 0)
        if expected_size and int(remote.size or 0) and int(remote.size or 0) != expected_size:
            raise _ProbeCompletionCancelled(f"文件大小已变化: {label}")
        expected_etag = str(item.get("etag") or "")
        if expected_etag and str(remote.etag or "") and str(remote.etag) != expected_etag:
            raise _ProbeCompletionCancelled(f"文件校验值已变化: {label}")
        return remote

    @staticmethod
    def _rules_from_job(job: dict):
        from app.modules.organize import OrganizeRules, enforce_fixed_organize_rules

        try:
            raw = json.loads(str(job.get("rules_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        allowed = {item.name for item in fields(OrganizeRules)}
        values = {key: value for key, value in raw.items() if key in allowed}
        return enforce_fixed_organize_rules(OrganizeRules(**values))

    def _desired_plan(
        self, job: dict, log: dict, video: dict, remote: GuangYaFile, profile,
    ):
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
        organizer = Organizer(client=self._runtime_client(), scraper=object())
        rules = self._rules_from_job(job)
        organizer._apply_media_profile_to_move_plan(
            plan, remote, rules, match,
            {"season": log.get("season"), "episode": log.get("episode")},
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
    ) -> list[dict]:
        client = self._runtime_client()
        if not self._acquire_write_lock():
            raise InterruptedError("服务正在停止")
        journal: list[tuple[str, str, str]] = []
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
                if entry.is_dir or str(entry.file_id) in allowed_ids:
                    continue
                if str(entry.name or "").casefold() in set(target_names):
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
                if client.rename(file_id, target_name) is False:
                    raise RuntimeError(f"云端改名失败: {current_name}")
                journal.append((file_id, target_name, current_name))

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
            ):
                raise _ProbeCompletionCancelled("整理日志状态已变化")
            return changes
        except Exception:
            rollback_errors: list[str] = []
            for file_id, current_name, old_name in reversed(journal):
                try:
                    if client.rename(file_id, old_name) is False:
                        raise RuntimeError("provider returned false")
                except Exception as exc:
                    rollback_errors.append(f"{file_id}:{type(exc).__name__}")
            if rollback_errors:
                raise RuntimeError(
                    "媒体规格补全失败且部分文件名回滚失败: " + ",".join(rollback_errors)
                )
            raise
        finally:
            self._organize_write_lock.release()

    def _execute_job(self, job: dict) -> None:
        log = self._row_dict(db.get_organize_log(int(job["organize_log_id"])))
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
        desired_name = str(plan.new_name or remote.name)
        if desired_name == str(video.get("current_name") or ""):
            return
        changes = self._apply_remote_rename(
            job=job, log=log, items=items, video=video, desired_name=desired_name,
        )
        if rules.link_strm and changes:
            from app.modules.organize import Organizer

            Organizer._post_organize_link(
                {"moved": 1, "failed": 0, "strm_changes": changes},
                rules,
                force_incremental=True,
            )
        logger.debug(
            "媒体规格后台补全完成 log=%s file=%s renamed=%s",
            log.get("id"), video.get("file_id"), desired_name,
        )

    def _process_one(self) -> bool:
        jobs = db.claim_due_organize_probe_jobs(
            owner=self._owner, lease_seconds=1800, limit=1,
        )
        if not jobs:
            return False
        job = jobs[0]
        job_id = int(job["id"])
        with self._state_lock:
            self._current_job_id = job_id
        try:
            self._execute_job(job)
            db.complete_organize_probe_job(job_id, owner=self._owner)
        except _ProbeCompletionCancelled as exc:
            db.cancel_organize_probe_job(job_id, owner=self._owner, reason=exc)
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
            logger.log(
                logging.WARNING if status == "failed" else logging.DEBUG,
                "媒体规格补全处理结果 job=%s status=%s",
                job_id,
                status,
            )
        except Exception as exc:
            status = db.fail_or_retry_organize_probe_job(
                job_id, owner=self._owner, error_type=type(exc).__name__, error=exc,
                base_backoff_seconds=600,
            )
            logger.warning(
                "媒体规格后台补全失败 job=%s status=%s type=%s",
                job_id, status, type(exc).__name__,
            )
        finally:
            with self._state_lock:
                self._current_job_id = 0
        return True


_worker = OrganizeProbeWorker()


def get_organize_probe_worker() -> OrganizeProbeWorker:
    return _worker
