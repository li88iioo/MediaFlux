"""确认后可靠收尾：仅重试只读/锁忙，绝不重跑确认或结果未知的删除。"""
from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import replace

from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger, log_throttled
from app.modules.download_staging_cleanup import cleanup_report, merge_cleanup_report
from app.modules.organize import OrganizeRules
from app.modules.organize_tasks import OrganizeTaskManager, get_organize_manager
from app.repositories.download_staging import (
    complete_staging_confirmation_phase,
    same_staging_identity,
)
from app.repositories.download_staging_reconcile import (
    defer_cleanup_intent,
    discover_legacy_confirmation_cleanup,
    finish_cleanup_intent,
    list_due_cleanup_intents,
    validate_cleanup_intent,
)

logger = get_logger(__name__)
_BATCH_SIZE = 10


def _require_writer_lock(writer_lock) -> None:
    # CrossProcessLock 允许跨线程释放，没有 public locked()；只验证持有状态，
    # 不获取第二把锁。两条生产入口仅在 manager.start_operation 回调内调用。
    if (getattr(writer_lock, '_name', '') != 'guangya-organize'
            or getattr(writer_lock, '_handle', None) is None
            or not writer_lock._thread_lock.locked()):
        raise RuntimeError('确认收尾只能在 guangya-organize 写锁内执行')


def reconcile_with_client(client=None, *, rules: OrganizeRules | None = None, limit: int = _BATCH_SIZE,
                          confirmation_token: str = '', writer_lock=None) -> dict:
    """持锁后重新查询 due 队列并 CAS；未传 client 时在首个获准清理处懒加载。"""
    _require_writer_lock(writer_lock)
    aggregate = cleanup_report()
    aggregate['processed'] = 0
    # 不沿用调度前的队列快照；同进程/跨进程重复 tick 均以锁内最新状态为准。
    with ExitStack() as clients:
        for job in list_due_cleanup_intents(limit=limit, confirmation_token=confirmation_token):
            if not defer_cleanup_intent(job, error='收尾尝试已排定；中断后按持久退避时间复核'):
                continue
            aggregate['processed'] += 1
            try:
                row, reason = validate_cleanup_intent(job)
                if row is None:
                    finish_cleanup_intent(job, status='retry' if reason == 'waiting' else 'blocked', error=(
                        '仍有同源媒体等待确认，稍后复核' if reason == 'waiting' else reason
                    ))
                    continue
                if row['gy_staging_cleanup_status'] in ('completed', 'skipped'):
                    finish_cleanup_intent(
                        job, status='completed' if row['gy_staging_cleanup_status'] == 'completed' else 'retained',
                        error=str(row.get('gy_staging_cleanup_error') or ''),
                    )
                    continue
                if not complete_staging_confirmation_phase(row):
                    finish_cleanup_intent(job, status='blocked', error='下载或确认阶段在收尾准备时变化，已停止')
                    continue
                current_rules = rules or OrganizeRules.from_config().for_source(str(row['gy_target_dir']))
                if row.pop('_cleanup_disabled', False):
                    current_rules = replace(current_rules, clean_empty=False)
                if current_rules.clean_empty and client is None:
                    client = GuangYaClient()
                    clients.callback(close_guangya_client, client)
                # 所有永久来源/目标保护、版本、空树、请求 CAS、删除审计继续复用原实现。
                report = OrganizeTaskManager._cleanup_download_staging(
                    client, [int(row['id'])],
                    [{'id': str(row['gy_target_dir']), 'name': str(row['gy_staging_name'])}],
                    rules=current_rules,
                    expected_identities={int(row['id']): {**row, '_confirmation_id': int(job['confirmation_id'])}},
                )
                merge_cleanup_report(aggregate, report)
                latest = download_requests_repo.get_download_request(int(row['id']))
                if latest is None or not same_staging_identity(dict(latest), row):
                    state = 'blocked'
                elif latest['gy_staging_cleanup_status'] == 'completed':
                    state = 'completed'
                elif latest['gy_staging_cleanup_status'] == 'failed' or report.get('_blocked'):
                    state = 'blocked'
                elif report.get('_retryable') or (report.get('scan_failures') and not report.get('_blocked')):
                    state = 'retry'
                elif report.get('not_empty') or latest['gy_staging_cleanup_status'] == 'skipped':
                    state = 'retained'
                else:
                    state = 'blocked'  # 未得到明确可重试/策略/残留结论，不猜测成功。
                finish_cleanup_intent(job, status=state, error='；'.join(report.get('reasons') or []))
            except Exception as exc:  # noqa: BLE001 - DB/provider 边界：持久退避仍先重验未知写护栏。
                # 这里的 retry 只意味着再次验证；若删除已开始，下载 failed/审计护栏会挡住重放。
                finish_cleanup_intent(job, status='retry', error='收尾读取或记录失败，将退避复核；未知写入不会重放')
                logger.warning('下载确认收尾暂不可用 request=%s type=%s', job['request_id'], type(exc).__name__)
    return aggregate


def schedule_staging_reconciliation() -> int:
    """Tracker 的真实周期入口；active downloads 为空也运行，锁忙不驻留内存队列。"""
    try:
        discover_legacy_confirmation_cleanup()
    except Exception as exc:  # noqa: BLE001 - 旧数据扫描失败不能饿死已持久化的收尾。
        log_throttled(
            logger, logging.WARNING, f'staging-discovery:{type(exc).__name__}',
            '旧下载确认发现暂不可用 type=%s', type(exc).__name__, interval_seconds=300.0,
        )
    due = list_due_cleanup_intents(limit=_BATCH_SIZE)
    if not due:
        return 0
    manager = get_organize_manager()

    def reconcile() -> dict:
        _require_writer_lock(manager._lock)
        # 必须在锁内重读，其他 worker 可能已经处理了锁外看到的任务。
        if not list_due_cleanup_intents(limit=1):
            return {'ok': True, 'stats': {'processed': 0}}
        try:
            report = reconcile_with_client(writer_lock=manager._lock)
            return {'ok': True, 'stats': report}
        except Exception:
            for job in list_due_cleanup_intents(limit=_BATCH_SIZE):
                defer_cleanup_intent(job, error='云盘客户端或收尾服务暂不可用，将退避复核')
            raise

    try:
        result = manager.start_operation(
            '复核下载确认收尾', '持久确认收尾队列', reconcile,
            queue_if_busy=False, dedupe_key='download-staging-reconcile',
        )
    except Exception:
        for job in due:
            defer_cleanup_intent(job, error='整理维护入口暂不可用，将退避重试')
        raise
    if not result.get('ok'):
        for job in due:
            defer_cleanup_intent(job, error='整理写锁繁忙，将退避重试')
        return 0
    return len(due)
