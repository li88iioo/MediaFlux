"""确认收尾持久意图：与确认终态同事务写入，不承担任何 provider I/O。"""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta

from app import database as db
from app.repositories.download_staging import (
    _unsafe_confirmation_stats,
    list_staging_cleanup_requests,
    requests_for_download_confirmation,
    same_staging_identity,
    staging_confirmation_summary,
    staging_identity_snapshot,
)

LEGACY_CURSOR_KEY = 'download_staging_reconcile.legacy_cursor_id'
_MAX_BATCH = 40


def _object(encoded) -> dict:
    try:
        value = json.loads(encoded or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _success(encoded) -> bool:
    stats = _object(encoded)
    moved = stats.get('moved')
    return type(moved) is int and moved > 0 and not _unsafe_confirmation_stats(encoded)


def _eligible(row: dict) -> bool:
    return (
        row.get('gy_status') == 'completed'
        and row.get('status') in ('completed', 'submitted', 'downloading', 'manual_review')
        and row.get('targets') in ('guangya', 'both')
        and int(row.get('gy_isolated') or 0) == 1
        and int(row.get('organize_started') or 0) >= 0
        and row.get('organize_status') in ('completed', 'requires_manual')
        and row.get('gy_staging_cleanup_status') in ('', None, 'pending', 'retained', 'completed', 'skipped')
        and not row.get('attention_cleared_at')
        and str(row.get('gy_target_dir') or '').strip() not in ('', '0')
        and bool(str(row.get('gy_staging_parent_dir') or '').strip())
        and bool(str(row.get('gy_staging_name') or '').strip())
    )


def enqueue_confirmation_cleanup(conn, *, token: str, timestamp: str) -> int | None:
    """主 facade 原子挂钩；仅使用传入连接，调用方提交/回滚，异常不可吞。"""
    confirmation = conn.execute(
        "SELECT c.* FROM organize_confirmations c WHERE c.token=? AND c.status='completed' "
        'AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer '
        'WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)', (token,),
    ).fetchone()
    if confirmation is None or not _success(confirmation['result_json']):
        return None
    payload = _object(confirmation['payload_json'])
    owners = requests_for_download_confirmation(payload, conn=conn)
    if len(owners) != 1 or not _eligible(owners[0]):
        return None
    row = owners[0]
    if row.get('gy_staging_cleanup_status') in ('completed', 'skipped'):
        return None
    identity = staging_identity_snapshot(row)
    # 执行时收窄过的授权不可被后台当前配置放大（含 Agent clean_title）。
    stats = _object(confirmation['result_json'])
    if 'download_staging_identity' in stats:
        before = stats['download_staging_identity']
        if not isinstance(before, dict) or not same_staging_identity(row, before):
            return None
    candidates = payload.get('candidates') or []
    agent_clean = (
        confirmation['confirmation_actor'] == 'agent' and isinstance(candidates, list)
        and any(isinstance(item, dict) and item.get('provider') == 'clean_title' for item in candidates)
    )
    # 早期自动清洗未持久化策略但不曾授权删除；新执行明确记录 cleanup_empty。
    # 不因当前开关开启而给旧成功票据补发删除权限。
    cleanup_policy = stats.get('download_staging_policy') or ('retained' if agent_clean else '')
    identity['_cleanup_disabled'] = (
        cleanup_policy == 'retained'
        or (isinstance(payload.get('rules'), dict) and payload['rules'].get('clean_empty') is False)
    )
    conn.execute(
        'INSERT INTO download_staging_reconcile '
        '(confirmation_id,request_id,identity_json,result_json,status,created_at,updated_at) '
        "VALUES(?,?,?,?,'pending',?,?) ON CONFLICT(confirmation_id) DO NOTHING",
        (int(confirmation['id']), int(row['id']), json.dumps(identity, ensure_ascii=False),
         str(confirmation['result_json']), timestamp, timestamp),
    )
    return int(conn.execute(
        'SELECT id FROM download_staging_reconcile WHERE confirmation_id=?', (int(confirmation['id']),),
    ).fetchone()['id'])


def reorganized_source_evidence(source_id: str, *, conn=None) -> list[dict]:
    """完整来源的最新成员事实；缺明细/新失败绝不回退到旧成功。"""
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT l.id AS log_id,l.version AS log_version,l.file_id AS primary_file_id,"
            "l.source_dir_id,l.status AS log_status,l.operation_type,l.legacy_incomplete,i.* "
            "FROM organize_log l LEFT JOIN organize_log_items i ON i.log_id=l.id "
            "WHERE l.source='guangya' AND l.file_id IN "
            "(SELECT file_id FROM organize_log WHERE source='guangya' AND source_dir_id=?) "
            "AND l.id=(SELECT MAX(newer.id) FROM organize_log newer "
            "WHERE newer.source='guangya' AND newer.file_id=l.file_id) ORDER BY l.id,i.id",
            (source_id,),
        )]
    if not rows or any(
        row['source_dir_id'] != source_id or row['log_status'] != 'success'
        or row['legacy_incomplete'] or row['operation_type'] not in ('organize', 'reorganize')
        or not row['id'] or not row['file_id'] or row['status'] != 'success'
        or not row['original_parent_id'] or not row['target_parent_id']
        or row['current_parent_id'] != row['target_parent_id']
        or row['current_parent_id'] == row['original_parent_id']
        or not row['target_name'] or row['current_name'] != row['target_name']
        or int(row['size'] or 0) <= 0 or not row['etag']
        for row in rows
    ):
        return []
    for log_id in {row['log_id'] for row in rows}:
        videos = [row for row in rows if row['log_id'] == log_id and row['role'] == 'video']
        if len(videos) != 1 or videos[0]['file_id'] != videos[0]['primary_file_id']:
            return []
    return rows


def _recover_reorganized_request(conn, recovery: dict, *, source_ids: list[str]) -> bool:
    """仅接受人工写入后的远端全量核验；请求和成员在同一事务内再次 CAS。"""
    expected = recovery['request']
    source_id = str(expected['gy_target_dir'])
    if source_id not in source_ids:
        return False
    owners = list_staging_cleanup_requests(source_id=source_id, conn=conn)
    if len(owners) != 1 or owners[0] != expected:
        return False
    row = owners[0]
    # 不放宽后台 _eligible：只有此显式纠偏入口可恢复失败，取消/未知删除仍冻结。
    if (row['organize_status'] not in ('failed', 'partial', 'completed', 'requires_manual')
            or (row['organize_status'] not in ('failed', 'partial') and int(row['organize_started']) >= 0)
            or not _eligible({**row, 'organize_started': 1, 'organize_status': 'completed'})):
        return False
    evidence = reorganized_source_evidence(source_id, conn=conn)
    if not evidence or evidence != recovery['members']:
        return False
    confirmations = conn.execute(
        "SELECT c.* FROM organize_confirmations c WHERE json_valid(c.payload_json) "
        "AND CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT)=? "
        "AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
        "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)", (source_id,),
    ).fetchall()
    for confirmation in confirmations:
        if confirmation['status'] in ('queued', 'running'):
            return False
        payload = _object(confirmation['payload_json'])
        bindings = requests_for_download_confirmation(payload, conn=conn)
        files, companions = payload.get('files'), payload.get('companions', [])
        if (len(bindings) != 1 or not same_staging_identity(bindings[0], row)
                or not isinstance(files, list) or not files or not isinstance(companions, list)):
            return False
        for item in [*files, *companions]:
            if not isinstance(item, dict) or not any(
                proof['file_id'] == item.get('file_id')
                and proof['original_parent_id'] == item.get('parent_id')
                and (item.get('size') is None or proof['size'] == item['size'])
                and (not item.get('etag') or str(proof['etag']).lower() == str(item['etag']).lower())
                for proof in evidence
            ):
                return False
    unknown = conn.execute(
        "SELECT 1 FROM organize_delete_audit WHERE trigger='download_staging_cleanup' "
        "AND status IN ('pending','failed') AND (file_id=? OR parent_id=?) LIMIT 1",
        (source_id, source_id),
    ).fetchone()
    if unknown:
        return False
    # BEGIN IMMEDIATE 已锁住读快照；仍明确带上原业务状态，不把历史成功当复活权限。
    changed = conn.execute(
        "UPDATE download_requests SET organize_started=1,organize_status='completed',"
        "organize_error='',organize_finished_at=?,updated_at=? "
        "WHERE id=? AND status=? AND organize_status=? AND organize_started=? "
        "AND organize_task_id=? AND gy_target_dir=? AND COALESCE(attention_cleared_at,'')=''",
        (db.now(), db.now(), int(row['id']), row['status'], row['organize_status'],
         row['organize_started'], row['organize_task_id'], source_id),
    )
    return changed.rowcount == 1


def discover_legacy_confirmation_cleanup(*, limit: int = _MAX_BATCH) -> int:
    """按下载 ID 游标有界补建旧终态意图；不从名称猜关联，也不更新下载终态。"""
    from app.repositories.organize_history import reconcile_reorganized_confirmations

    limit = max(1, min(limit, _MAX_BATCH))
    with db.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        saved = conn.execute('SELECT value FROM settings_kv WHERE key=?', (LEGACY_CURSOR_KEY,)).fetchone()
        try:
            cursor = max(0, int(saved['value'])) if saved else 0
        except (TypeError, ValueError):
            cursor = 0
        rows = conn.execute(
            "SELECT id,gy_target_dir FROM download_requests WHERE id>? AND gy_isolated=1 "
            "AND gy_status='completed' AND status IN ('completed','submitted','downloading','manual_review') "
            "AND organize_status IN ('completed','requires_manual') "
            "AND COALESCE(gy_staging_cleanup_status,'') IN ('','pending','retained') "
            "AND COALESCE(attention_cleared_at,'')='' ORDER BY id LIMIT ?", (cursor, limit),
        ).fetchall()
        created = 0
        timestamp = db.now()
        latest_by_source: dict[str, str] = {}
        source_ids = list(dict.fromkeys(str(row['gy_target_dir']) for row in rows))
        if source_ids:
            created += len(reconcile_reorganized_confirmations(source_ids, conn=conn))
            placeholders = ','.join('?' for _ in source_ids)
            source_expression = (
                "CASE WHEN json_valid(c.payload_json) THEN "
                "CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT) END"
            )
            # 按整数主键倒序流式扫描；status/updated_at 索引会引入整段排序，
            # 使下方的提前结束无法避免旧历史扫描。newer 的指纹索引仍可使用。
            confirmations = conn.execute(
                f"SELECT c.token,{source_expression} AS source_id "
                "FROM organize_confirmations c NOT INDEXED WHERE c.status='completed' "
                f"AND {source_expression} IN ({placeholders}) "
                'AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer '
                'WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id) '
                'ORDER BY c.id DESC', source_ids,
            )
            try:
                for confirmation in confirmations:
                    latest_by_source.setdefault(str(confirmation['source_id']), str(confirmation['token']))
                    # 倒序首条即各来源的最新卡。全部找到便结束游标，不能
                    # 为减少SQL次数而把更早的整段历史继续扫描/传回Python。
                    if len(latest_by_source) == len(source_ids):
                        break
            finally:
                confirmations.close()
        # 只批量化历史查找；新旧确认仍进入同一个终态入队器，重新校验唯一
        # 下载归属与全部最新卡。多请求复用同一来源时也不重复执行相同入队。
        tokens = dict.fromkeys(latest_by_source.get(str(row['gy_target_dir'])) for row in rows)
        for token in tokens:
            if token:
                before = conn.total_changes
                enqueue_confirmation_cleanup(conn, token=token, timestamp=timestamp)
                created += int(conn.total_changes > before)
        db.kv_set_conn(conn, LEGACY_CURSOR_KEY, str(rows[-1]['id'] if rows else 0), timestamp=timestamp)
    return created


def list_due_cleanup_intents(*, limit: int = _MAX_BATCH, confirmation_token: str = '') -> list[dict]:
    params: list = [db.now()]
    where = ''
    if confirmation_token:
        where = ' AND c.token=?'
        params.append(confirmation_token)
    params.append(max(1, min(limit, _MAX_BATCH)))
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT q.* FROM download_staging_reconcile q JOIN organize_confirmations c ON c.id=q.confirmation_id '
            "WHERE q.status IN ('pending','retry') AND q.next_attempt_at<=?" + where +
            ' ORDER BY q.next_attempt_at,q.id LIMIT ?', params,
        ).fetchall()
    return [dict(row) for row in rows]


def validate_cleanup_intent(job: dict) -> tuple[dict | None, str]:
    """重新确认绑定与全部最新确认；返回 waiting 可退避，其余错误必须 fail closed。"""
    with db.get_conn() as conn:
        confirmation = conn.execute(
            'SELECT c.* FROM organize_confirmations c WHERE c.id=? '
            'AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer '
            'WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)', (job['confirmation_id'],),
        ).fetchone()
        if (confirmation is None or confirmation['status'] != 'completed'
                or not _success(job['result_json']) or not _success(confirmation['result_json'])):
            return None, '确认已变化、失败或成功证据不足，停止自动收尾'
        owners = requests_for_download_confirmation(_object(confirmation['payload_json']), conn=conn)
        expected = _object(job['identity_json'])
        if (len(owners) != 1 or int(owners[0]['id']) != int(job['request_id'])
                or not same_staging_identity(owners[0], expected) or not _eligible(owners[0])):
            return None, '下载身份、唯一归属或状态已变化；未知删除、取消及已清除请求不自动重试'
        row = owners[0]
        summary = staging_confirmation_summary(str(row['gy_target_dir']), conn=conn)
        if not summary['total'] or summary['failed']:
            return None, '同源确认存在失败、取消或过期事项，停止自动收尾'
        if summary['pending']:
            return None, 'waiting'
        row['_cleanup_disabled'] = expected.get('_cleanup_disabled') is True
        return row, ''


def _delay(attempt: int) -> int:
    return min(3600, 30 * (2 ** min(max(0, attempt - 1), 7)))


def defer_cleanup_intent(job: dict, *, error: str) -> bool:
    """CAS 退避也用作尝试前检查点；进程中断后无需抢救 running/lease。"""
    attempt = int(job['attempt_count']) + 1
    timestamp = db.now()
    next_at = (datetime.fromisoformat(timestamp) + timedelta(seconds=_delay(attempt))).strftime('%Y-%m-%d %H:%M:%S')
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_staging_reconcile SET status='retry',attempt_count=?,next_attempt_at=?,last_error=?,updated_at=? "
            "WHERE id=? AND status=? AND attempt_count=? AND next_attempt_at=?",
            (attempt, next_at, error[:500], timestamp, int(job['id']), job['status'],
             job['attempt_count'], job['next_attempt_at']),
        )
    if cur.rowcount != 1:
        return False
    job.update(status='retry', attempt_count=attempt, next_attempt_at=next_at)
    return True


def finish_cleanup_intent(job: dict, *, status: str, error: str = '') -> bool:
    if status not in ('retry', 'completed', 'retained', 'blocked'):
        raise ValueError('非法确认收尾状态')
    with db.get_conn() as conn:
        cur = conn.execute(
            'UPDATE download_staging_reconcile SET status=?,last_error=?,updated_at=? '
            "WHERE id=? AND status=? AND attempt_count=? AND next_attempt_at=?",
            (status, error[:500], db.now(), int(job['id']), job['status'],
             job['attempt_count'], job['next_attempt_at']),
        )
    return cur.rowcount == 1
