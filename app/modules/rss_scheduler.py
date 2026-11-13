"""RSS 订阅周期调度器。"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

from app import database as db
from app.repositories import rss as rss_repo
from app.logger import get_logger, log_throttled
from app.modules.rss import RSS_FAILURE_LABELS, RSSEngine, rss_subscription_refresh_revision
from app.sensitive_data import redact_sensitive_text
from app.notifier import NOTIFICATION_SECTION_BREAK, NotificationEvent

logger = get_logger(__name__)
_MAX_CONCURRENT_REFRESHES = 4
_ALERT_KEY_PREFIX = "rss.scheduler.alert_signature."
_ADMISSION_CURSOR_KEY = "rss.scheduler.last_admitted_id"


class RSSScheduler:
    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_ids: set[int] = set()
        self._workers: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._accepting = True
        self._cleanup_interval_seconds = 3600
        self._last_cleanup_at = 0.0
        self._alert_signatures: dict[int, tuple[object, ...]] = {}

    @staticmethod
    def _alert_key(sub_id: int) -> str:
        return f"{_ALERT_KEY_PREFIX}{sub_id}"

    def _persisted_alert(self, sub_id: int) -> dict | None:
        raw = db.kv_get(self._alert_key(sub_id), "")
        if not raw:
            return None
        value = json.loads(raw)
        # 历史列表只归一化为“已接纳”，不重放已发告警；恢复后进入同一新状态机。
        if isinstance(value, list):
            return {"signature": value, "accepted": True}
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("signature"), list)
            or not isinstance(value.get("accepted"), bool)
            or not isinstance(value.get("event_key"), str)
            or not value["event_key"].startswith(f"rss-alert:{sub_id}:")
        ):
            raise ValueError("RSS 告警持久状态无效，已停止重放")
        return value

    def _save_alert(self, sub_id: int, alert: dict) -> None:
        db.kv_set(
            self._alert_key(sub_id),
            json.dumps(alert, ensure_ascii=False, separators=(",", ":")),
        )

    def _notify_issue(
        self, sub_id: int, code: str, fields: list[tuple[str, object]], *,
        lines: tuple[str, ...] = (), reason_codes: tuple[str, ...] = (),
    ) -> None:
        signature = (code, *(str(value) for _label, value in fields), *reason_codes)
        with self._lock:
            if self._alert_signatures.get(sub_id) == signature:
                return
        try:
            alert: dict[str, Any] | None = self._persisted_alert(sub_id)
            same_issue = alert is not None and tuple(alert["signature"]) == signature
            if same_issue and alert["accepted"]:
                with self._lock:
                    self._alert_signatures[sub_id] = signature
                return
            if not same_issue:
                # 先持久化本次故障身份再入 outbox；重启/ACK 失败均复用该 key。
                # 恢复或问题变化后生成新身份，避免旧一次性事件永久吞掉复发。
                alert = {
                    "version": 1, "signature": list(signature), "accepted": False,
                    "event_key": f"rss-alert:{sub_id}:{uuid.uuid4().hex}",
                }
                self._save_alert(sub_id, alert)
            assert alert is not None
            subscription = rss_repo.get_rss_subscription(sub_id)
        except Exception as exc:
            logger.warning(
                "RSS 周期告警状态读取异常 sub#%s type=%s",
                sub_id,
                type(exc).__name__,
            )
            return
        name = redact_sensitive_text(subscription["name"] or f"订阅 #{sub_id}") if subscription else f"订阅 #{sub_id}"
        try:
            from app.modules.telegram_notification_center import (
                publish_notification_event,
            )
            from app.modules.telegram_notification_policy import (
                NotificationImportance,
                NotificationTopic,
            )

            accepted = bool(publish_notification_event(
                alert["event_key"],
                NotificationEvent(
                    "⚠️ RSS 周期任务需要处理",
                    fields=(
                        ("订阅", name),
                        NOTIFICATION_SECTION_BREAK,
                        *tuple(fields),
                    ),
                    lines=lines,
                    footer="相同问题只提醒一次；原因变化或恢复后再次发生会重新提醒。",
                    layout="relaxed",
                ),
                topic=NotificationTopic.RSS,
                importance=NotificationImportance.ERROR,
            ))
        except Exception as exc:
            logger.warning(
                "RSS 周期告警发送异常 sub#%s type=%s",
                sub_id,
                type(exc).__name__,
            )
            return
        if not accepted:
            return
        try:
            self._save_alert(sub_id, {**alert, "accepted": True})
        except Exception as exc:
            # 已入 outbox 但 ACK 未落盘时不要假装完成；下轮复用同一事件收敛。
            logger.warning(
                "RSS 周期告警状态保存异常 sub#%s type=%s",
                sub_id,
                type(exc).__name__,
            )
            return
        with self._lock:
            self._alert_signatures[sub_id] = signature

    def _clear_issue(self, sub_id: int) -> None:
        with self._lock:
            had_issue = sub_id in self._alert_signatures
        try:
            # pending、历史、损坏状态都可在业务明确恢复后清除；不删除通知审计。
            had_issue = bool(db.kv_get(self._alert_key(sub_id), "")) or had_issue
            if not had_issue:
                return
            db.kv_set(self._alert_key(sub_id), "")
        except Exception as exc:
            logger.warning(
                "RSS 周期告警状态清理异常 sub#%s type=%s",
                sub_id,
                type(exc).__name__,
            )
            return
        with self._lock:
            self._alert_signatures.pop(sub_id, None)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._accepting = True
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="rss-scheduler", daemon=True,
            )
            self._thread.start()
        logger.info("RSS 调度器已启动")

    def stop(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._accepting = False
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if not thread or not thread.is_alive():
            self._thread = None
        with self._lock:
            workers = tuple(self._workers.values())
        for worker in workers:
            if worker is threading.current_thread() or not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            alive = any(worker.is_alive() for worker in self._workers.values())
        return not alive and (self._thread is None or not self._thread.is_alive())

    def reload(self) -> None:
        if not self._stop_event.is_set():
            self._wake_event.set()

    def run_due(self) -> int:
        if self._stop_event.is_set():
            return 0
        self._run_cleanup_if_due()
        recovered = rss_repo.recover_stale_submitting_rss_entries(stale_minutes=15)
        if recovered:
            logger.warning(
                "已将 %s 条提交结果未知的 RSS 条目转为人工核对状态", recovered
            )
        try:
            cursor = max(0, int(db.kv_get(_ADMISSION_CURSOR_KEY, "0") or 0))
        except (TypeError, ValueError):
            cursor = 0
        due = rss_repo.list_due_rss_subscriptions()
        # 失败刷新不应伪造 last_refreshed_at；独立记录最后一次真实准入，
        # 让超过并发窗口的订阅也有执行机会，重启后继续而非总从最小 ID 开始。
        ordered = [row for row in due if int(row["id"]) > cursor]
        ordered.extend(row for row in due if int(row["id"]) <= cursor)
        count = 0
        for row in ordered:
            sub_id = int(row["id"])
            worker = threading.Thread(
                target=self._execute,
                args=(
                    sub_id,
                    str(row["action"] or "subscribe"),
                    rss_subscription_refresh_revision(row),
                ),
                name=f"rss-refresh-{sub_id}",
                daemon=True,
            )
            with self._lock:
                # stop() 与任务准入共用一把锁，禁止关机窗口再启动新 worker。
                if not self._accepting or self._stop_event.is_set():
                    break
                if len(self._running_ids) >= _MAX_CONCURRENT_REFRESHES:
                    break
                if sub_id in self._running_ids:
                    continue
                self._running_ids.add(sub_id)
                self._workers[sub_id] = worker
                try:
                    worker.start()
                except Exception:
                    self._running_ids.discard(sub_id)
                    self._workers.pop(sub_id, None)
                    raise
                db.kv_set(_ADMISSION_CURSOR_KEY, str(sub_id))
            count += 1
        return count

    def _run_cleanup_if_due(self) -> int:
        current = time.monotonic()
        if self._last_cleanup_at and current - self._last_cleanup_at < self._cleanup_interval_seconds:
            return 0
        deleted = rss_repo.purge_processed_rss_entries(retention_days=7)
        self._last_cleanup_at = current
        if deleted:
            logger.info(f"已清理 {deleted} 条超过 7 天的已处理 RSS 条目")
        return deleted

    def _execute(self, sub_id: int, action: str, expected_revision: str = "") -> None:
        try:
            engine = RSSEngine()
            result = (
                engine.auto_download(sub_id, expected_revision=expected_revision)
                if action == "download"
                else engine.refresh(sub_id, expected_revision=expected_revision)
            )
            if result.get("busy"):
                logger.debug("RSS 周期刷新跳过 sub#%s", sub_id)
            elif result.get("conflict"):
                logger.debug("RSS 周期刷新取消 sub#%s", sub_id)
            elif result.get("error"):
                logger.warning("RSS 周期任务失败 sub#%s", sub_id)
                self._notify_issue(
                    sub_id,
                    str(result.get("error_code") or "error"),
                    [("状态", "刷新失败，请检查订阅源")],
                )
            elif not (result.get("failed") or result.get("outcome_unknown_count")) and (
                result.get("partial") or (result.get("refresh") or {}).get("partial")
            ):
                logger.warning("RSS 周期刷新部分完成 sub#%s", sub_id)
                self._notify_issue(sub_id, "partial_refresh", [("状态", "部分订阅源刷新失败")])
            else:
                failures = {item["id"]: item for item in result.get("failure_details") or []}
                failed = int(result.get("failed") or 0)
                unknown = int(result.get("outcome_unknown_count") or 0)
                summary = rss_repo.get_rss_failure_summary(
                    sub_id, entry_ids=tuple(failures) if failed or unknown else None,
                )
                # 本轮有失败时只说明本轮，空轮次则继续提醒尚未处理的终态失败。
                # 并发冲突等没有持久失败记录时仍保留实际回执，不能宣布成功。
                if not (failed or unknown):
                    failed, unknown = summary["failed_count"], summary["outcome_unknown_count"]
                if failed or unknown:
                    logger.warning(
                        "RSS 周期下载%s sub#%s outcome_unknown=%s failed=%s",
                        "结果待核对" if unknown else "部分失败", sub_id, unknown, failed,
                    )
                    fields: list[tuple[str, object]] = [
                        ("状态", "提交结果待人工核对，请勿重复提交" if unknown else "周期下载部分失败"),
                    ]
                    if unknown:
                        fields.append(("待核对", unknown))
                    # failed 包含未知结果，展示时分开，避免将待核对误称为确定失败。
                    fields.append(("失败", str(max(0, failed - unknown))))
                    fields.extend(("原因", f"{RSS_FAILURE_LABELS.get(group['code']) or '未分类失败'} · {group['count']} 条")
                                  for group in summary["reasons"])
                    if failed > summary["failed_count"]:
                        fields.append(("原因", f"未取得条目详情，请查看下载中心 · {failed - summary['failed_count']} 条"))
                    lines = []
                    for entry in summary["entries"]:
                        title = " ".join(redact_sensitive_text(entry["title"]).split())[:160]
                        detail = failures.get(entry["id"], {}).get("error")
                        reason = " ".join(redact_sensitive_text(detail).split())[:200] if detail else (
                            RSS_FAILURE_LABELS.get(entry["failure_code"]) or "未分类失败"
                        )
                        lines.append(f"• #{entry['id']} {title}：{reason}")
                    remaining = failed - len(lines)
                    if remaining > 0:
                        lines.append(f"另有 {remaining} 条未展开。")
                    lines.append("请在下载中心查看详情并处理；待核对条目请先确认下载器任务，勿直接重投。" if unknown
                                 else "请在下载中心查看失败详情，处理原因后再重试。")
                    self._notify_issue(
                        sub_id, "outcome_unknown" if unknown else "partial_failure", fields, lines=tuple(lines),
                        reason_codes=tuple(group["code"] for group in summary["reasons"]),
                    )
                else:
                    self._clear_issue(sub_id)
        except Exception as exc:
            logger.warning("RSS 周期任务异常 sub#%s type=%s", sub_id, type(exc).__name__)
            self._notify_issue(
                sub_id,
                f"exception:{type(exc).__name__}",
                [("状态", "周期任务异常，请检查运行日志")],
            )
        finally:
            with self._lock:
                self._running_ids.discard(sub_id)
                current = self._workers.get(sub_id)
                if current is threading.current_thread():
                    self._workers.pop(sub_id, None)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception as exc:
                log_throttled(
                    logger, logging.WARNING, f"rss-scheduler:{type(exc).__name__}",
                    "RSS 调度检查失败 type=%s", type(exc).__name__,
                )
            self._wake_event.wait(timeout=30)
            self._wake_event.clear()


_scheduler = RSSScheduler()


def get_rss_scheduler() -> RSSScheduler:
    return _scheduler
