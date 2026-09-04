"""网盘整理 Cron 调度器。

调度线程只负责计算到期时间并调用 ``OrganizeTaskManager.start``；实际整理、
多源聚合和互斥全部复用既有任务管理器，避免产生第二条执行路径。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from croniter import croniter

from app.config import get, get_bool
from app.logger import get_logger, log_throttled
from app.modules.organize import OrganizeRules
from app.modules.organize_sources import normalize_organize_sources

logger = get_logger(__name__)


class OrganizeScheduler:
    """进程内单线程整理 Cron 调度器。"""

    def __init__(
        self,
        *,
        manager=None,
        get_value: Callable[[str, str], str] = get,
        get_flag: Callable[[str, bool], bool] = get_bool,
        now: Callable[[], datetime] = datetime.now,
        check_interval: float = 10.0,
    ) -> None:
        if manager is None:
            from app.modules.organize_tasks import get_organize_manager

            manager = get_organize_manager()
        self._manager = manager
        self._get_value = get_value
        self._get_flag = get_flag
        self._now = now
        self._check_interval = max(0.05, float(check_interval))
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loaded_cron = ""
        self._next_run: datetime | None = None
        self._active_task_id = ""
        self._last_result: dict = {}
        self._config_error = ""

    def start(self) -> None:
        """启动唯一 daemon 调度线程；重复调用安全。"""
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="organize-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info("网盘整理调度器已启动")

    def stop(self) -> bool:
        """停止调度线程并等待退出；返回是否已安全收敛。"""
        with self._state_lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._check_interval + 1.0))
        stopped = thread is None or not thread.is_alive()
        with self._state_lock:
            if self._thread is thread and stopped:
                self._thread = None
        return stopped

    def reload(self) -> None:
        """清除已计算时间并立即唤醒线程重新读取配置。"""
        with self._state_lock:
            self._loaded_cron = ""
            self._next_run = None
            self._config_error = ""
        self._wake_event.set()

    @staticmethod
    def validate_cron(expr: str) -> bool:
        """只接受标准 5 段 cron（分 时 日 月 周）。"""
        text = str(expr or "").strip()
        if len(text.split()) != 5:
            return False
        try:
            croniter(text, datetime.now()).get_next(datetime)
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def status(self) -> dict:
        """返回启用状态、下次运行和最近一次 Cron 结果。"""
        self._refresh_active_result()
        enabled = self._get_flag("GY_ORGANIZE_SCHEDULE_ENABLED", False)
        cron_expr = self._cron_expr()
        with self._state_lock:
            next_run = self._next_run
            last_result = dict(self._last_result)
            error = self._config_error
        return {
            "enabled": enabled,
            "cron": cron_expr,
            "cron_valid": self.validate_cron(cron_expr),
            "config_error": error,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "",
            "last_result": last_result,
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                log_throttled(
                    logger, logging.ERROR, f"organize-scheduler:{type(exc).__name__}",
                    "网盘整理调度检查失败 type=%s", type(exc).__name__,
                )
            self._wake_event.wait(timeout=self._check_interval)
            self._wake_event.clear()

    def _tick(self) -> None:
        self._refresh_active_result()
        if not self._get_flag("GY_ORGANIZE_SCHEDULE_ENABLED", False):
            with self._state_lock:
                self._loaded_cron = ""
                self._next_run = None
                self._config_error = ""
            return

        cron_expr = self._cron_expr()
        sources = self._configured_sources()
        target_dir_id = str(self._get_value("GY_ORGANIZE_TARGET_DIR", "0") or "0").strip()
        error = self._validate_config(cron_expr, sources, target_dir_id)
        if error:
            with self._state_lock:
                self._loaded_cron = cron_expr
                self._next_run = None
                self._config_error = error
            return

        now = self._now()
        with self._state_lock:
            self._config_error = ""
            if self._next_run is None or self._loaded_cron != cron_expr:
                self._next_run = croniter(cron_expr, now).get_next(datetime)
                self._loaded_cron = cron_expr
            due = bool(self._next_run and now >= self._next_run)
            if due:
                self._next_run = croniter(cron_expr, now).get_next(datetime)
        if due:
            self._start_due_task(sources, target_dir_id)

    def _start_due_task(self, sources: list[dict[str, str]], target_dir_id: str) -> None:
        started_at = self._now().strftime("%Y-%m-%d %H:%M:%S")
        rules = OrganizeRules.from_config(target_dir_id=target_dir_id)
        result = self._manager.start(sources, rules, trigger_type="cron")
        if result.get("ok"):
            task_id = str(result.get("task_id") or "")
            record = {
                "task_id": task_id,
                "trigger_type": "cron",
                "outcome": "started",
                "message": str(result.get("message") or "整理任务已启动"),
                "started_at": started_at,
                "finished_at": "",
                "source_count": len(sources),
                "stats": {},
            }
            with self._state_lock:
                self._active_task_id = task_id
                self._last_result = record
            return

        with self._state_lock:
            self._active_task_id = ""
            self._last_result = {
                "task_id": "",
                "trigger_type": "cron",
                "outcome": "skipped",
                "message": str(result.get("error") or "整理任务未启动"),
                "started_at": started_at,
                "finished_at": started_at,
                "source_count": len(sources),
                "stats": {},
            }

    def _refresh_active_result(self) -> None:
        with self._state_lock:
            task_id = self._active_task_id
        if not task_id:
            return
        task = self._manager.task_status()
        if str(task.get("id") or "") != task_id:
            lookup = getattr(self._manager, "task_result", None)
            task = lookup(task_id) if callable(lookup) else None
            if not task:
                return
        outcome = str(task.get("status") or "")
        if outcome not in {"completed", "partial", "failed", "stopped"}:
            return
        with self._state_lock:
            if self._active_task_id != task_id:
                return
            self._last_result.update({
                "outcome": outcome,
                "message": str(task.get("error") or task.get("message") or ""),
                "finished_at": str(task.get("finished_at") or ""),
                "stats": dict(task.get("stats") or {}),
            })
            self._active_task_id = ""

    def _cron_expr(self) -> str:
        return str(self._get_value("GY_ORGANIZE_SCHEDULE_CRON", "0 4 * * *") or "").strip()

    def _configured_sources(self) -> list[dict[str, str]]:
        sources, error = normalize_organize_sources(
            self._get_value("GY_ORGANIZE_SOURCE_DIRS", ""),
        )
        if error:
            log_throttled(
                logger, logging.WARNING, f"organize-sources:{error}",
                "读取定时整理来源失败: %s", error,
            )
        return sources

    @classmethod
    def _validate_config(
        cls,
        cron_expr: str,
        sources: list[dict[str, str]],
        target_dir_id: str,
    ) -> str:
        if not cls.validate_cron(cron_expr):
            return "cron 表达式无效，需使用 5 段格式：分 时 日 月 周"
        if not sources:
            return "未配置网盘整理源目录"
        if not target_dir_id or target_dir_id == "0":
            return "未配置网盘整理目标目录"
        return ""


_scheduler = OrganizeScheduler()


def get_organize_scheduler() -> OrganizeScheduler:
    return _scheduler
