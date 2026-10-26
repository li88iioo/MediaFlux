"""下载隔离目录的只读定位与身份条件更新，不改变全局活动目录保护规则。"""
from __future__ import annotations

import json
import re
from contextlib import nullcontext

from app import database as db

# 清理只读业务身份，避免列表查询载入磁力、种子内容和通知凭据。
_IDENTITY_KEYS = (
    'gy_target_dir', 'gy_staging_parent_dir', 'gy_staging_name',
    'organize_task_id', 'gy_task_id', 'gy_task_ids', 'targets',
)
_COLUMNS = (
    'id,status,targets,gy_status,gy_isolated,gy_target_dir,gy_target_name,'
    'gy_staging_parent_dir,gy_staging_name,gy_staging_cleanup_status,'
    'gy_staging_cleanup_error,organize_started,organize_status,organize_task_id,'
    'gy_task_id,gy_task_ids,attention_cleared_at'
)

# 仅由原子来源交接写入；取消/过期本身不意味着可以忽略未完成的文件。
_RELOCATED_CONFIRMATION_SQL = (
    "(c.status='expired' AND CASE WHEN json_valid(c.result_json) THEN "
    "COALESCE(json_extract(c.result_json,'$.resolution'),'')='source_relocated' ELSE 0 END)"
)


def list_staging_cleanup_requests(*, source_id: str = '', limit: int = 1000,
                                  active_only: bool = False, conn=None) -> list[dict]:
    """读取有界隔离请求；超限让调用方停止，而不是静默漏掉保护对象。"""
    where = "gy_isolated=1 AND TRIM(COALESCE(gy_target_dir,'')) NOT IN ('','0')"
    params: list[object] = []
    if source_id:
        where += ' AND gy_target_dir=?'
        params.append(source_id)
    elif not active_only:
        where += " AND COALESCE(gy_staging_cleanup_status,'')!='completed'"
    if active_only:
        # 手动删除只保护仍可能写入的任务；自动清理仍沿用原有更严格的保留策略。
        where += (
            " AND (gy_status IN ('submitting','submitted','downloading','outcome_unknown','manual_review') "
            "OR (gy_status='completed' AND ("
            "organize_status IN ('pending','settling','queued','starting','running') "
            "OR (organize_started>=0 AND COALESCE(organize_status,'')='') "
            "OR EXISTS (SELECT 1 FROM organize_confirmations c WHERE c.status IN ('queued','running') "
            "AND CASE WHEN json_valid(c.payload_json) THEN "
            "CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT) END=download_requests.gy_target_dir))))"
        )
    fetch_limit = max(1, min(limit, 1000)) + 1
    params.append(fetch_limit)
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='download_requests'").fetchone() is None:
            return []  # 与全局staging保护查询保持初始化前的兼容行为。
        rows = connection.execute(
            f'SELECT {_COLUMNS} FROM download_requests WHERE {where} ORDER BY id LIMIT ?', params
        ).fetchall()
    if len(rows) > fetch_limit - 1:
        raise RuntimeError('待复核下载目录超过安全上限，请缩小清理范围')
    return [dict(row) for row in rows]


def _unsafe_confirmation_stats(encoded: str | None) -> bool:
    try:
        stats = json.loads(encoded or '{}')
        return (
            not isinstance(stats, dict)
            or stats.get('scan_complete') is False
            or any(stats.get(key) for key in (
                'failed', 'need_confirm', 'stopped', 'scan_errors', 'scan_limited', 'audit_failures',
                'replacement_cleanup_failed', 'empty_dir_cleanup_failed', 'source_dir_cleanup_failed',
            ))
        )
    except (TypeError, ValueError):
        return True


def unresolved_staging_confirmations(source_id: str) -> int:
    """完成卡仍含失败也阻止删除；旧卡按source ID匹配，同指纹只看最新。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT c.status,c.result_json FROM organize_confirmations c WHERE "
            f"NOT {_RELOCATED_CONFIRMATION_SQL} AND "
            "NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
            "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id) AND "
            "CASE WHEN json_valid(c.payload_json) THEN "
            "CAST(COALESCE(json_extract(c.payload_json,'$.source_dir_id'),'') AS TEXT)=? "
            "ELSE c.status NOT IN ('completed','cancelled','expired') END", (source_id,),
        ).fetchall()
    return sum(
        _unsafe_confirmation_stats(row['result_json']) if row['status'] == 'completed'
        else True
        for row in rows
    )


def is_current_staging_confirmation(confirmation_id: int, source_id: str, *, conn=None) -> bool:
    """持久收尾授权只能使用原卡；新成功卡也不能替代旧意图的授权身份。"""
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        row = connection.execute(
            "SELECT c.status,c.result_json FROM organize_confirmations c WHERE c.id=? "
            "AND CASE WHEN json_valid(c.payload_json) THEN "
            "CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT)=? ELSE 0 END "
            "AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
            "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)", (confirmation_id, source_id),
        ).fetchone()
    return row is not None and row['status'] == 'completed' and not _unsafe_confirmation_stats(row['result_json'])


def staging_identity_snapshot(row: dict) -> dict:
    """只持久化清理授权需要的原始身份，不带下载凭据或用户私密载荷。"""
    return {key: row.get(key) for key in ('id', 'gy_isolated', *_IDENTITY_KEYS)}


def same_staging_identity(current: dict, expected: dict) -> bool:
    return (
        int(current.get('id') or 0) == int(expected.get('id') or 0)
        and int(current.get('gy_isolated') or 0) == 1
        and all(str(current.get(key) or '') == str(expected.get(key) or '') for key in _IDENTITY_KEYS)
    )


def policy_skip_has_evidence(expected: dict, *, conn) -> bool:
    """策略保留白名单：无失败初态，或旧告警中的每个文件都有完整成功确认。"""
    error = str(expected.get('gy_staging_cleanup_error') or '').strip()
    existing_policy = expected.get('gy_staging_cleanup_status') == 'skipped' and error.startswith(
        '按策略保留隔离目录（clean_empty 已关闭）',
    )
    legacy = re.fullmatch(r'隔离目录仍有 ([1-9][0-9]*) 项未整理或未识别：(.+)', error)
    if error and not existing_policy and legacy is None:
        return False
    names: list[str] = []
    if legacy:
        count = int(legacy.group(1))
        names = [legacy.group(2)] if count == 1 else legacy.group(2).split('、')
        # 旧代码超过五项会截断预览；不能凭不完整列表解除告警。
        if count > 5 or len(names) != count or len(set(names)) != count:
            return False
    confirmations = conn.execute(
        "SELECT c.payload_json,c.result_json FROM organize_confirmations c WHERE c.status='completed' "
        "AND CASE WHEN json_valid(c.payload_json) THEN "
        "CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT)=? ELSE 0 END "
        "AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
        "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)", (str(expected.get('gy_target_dir') or ''),),
    ).fetchall()
    confirmed_names: set[str] = set()
    has_success = False
    for confirmation in confirmations:
        if _unsafe_confirmation_stats(confirmation['result_json']):
            continue
        try:
            payload = json.loads(confirmation['payload_json'])
            result = json.loads(confirmation['result_json'] or '{}')
            files = payload.get('files')
            moved = result.get('moved')
            if type(moved) is not int or moved <= 0:
                continue
            has_success = True
            if not isinstance(files, list) or not files or moved < len(files):
                continue
            confirmed_names.update(str(item['name']) for item in files
                                   if isinstance(item, dict) and item.get('file_id') and item.get('name'))
        except (TypeError, ValueError, AttributeError):
            continue
    return has_success and set(names) <= confirmed_names


def update_staging_cleanup(expected: dict, *, status: str, error: str = '') -> bool:
    """CAS保留原隔离身份/后端绑定与清理阶段，旧回调不能改写重绑定后的请求。"""
    if status not in ('retained', 'failed', 'completed', 'skipped'):
        raise ValueError('非法隔离目录清理状态')
    where = ' AND '.join(f"COALESCE({key},'')=?" for key in _IDENTITY_KEYS)
    with db.get_conn() as conn:
        if status == 'skipped':
            if (expected.get('gy_staging_cleanup_status') not in ('', None, 'pending', 'retained', 'skipped')
                    or expected.get('organize_status') != 'completed'):
                return False
            where += " AND organize_status='completed'"
            # 策略跳过不等于隐藏：必须在同一事务内确认成功证据与旧告警仍有效。
            conn.execute('BEGIN IMMEDIATE')
            if expected.get('_confirmation_id') and not is_current_staging_confirmation(
                expected['_confirmation_id'], str(expected.get('gy_target_dir') or ''), conn=conn,
            ):
                return False
            summary = staging_confirmation_summary(str(expected.get('gy_target_dir') or ''), conn=conn)
            if not summary['total'] or summary['completed'] != summary['total']:
                return False
            if not policy_skip_has_evidence(expected, conn=conn):
                return False
            owners = list_staging_cleanup_requests(source_id=str(expected.get('gy_target_dir') or ''), conn=conn)
            if len(owners) != 1 or not same_staging_identity(owners[0], expected):
                return False
            current = conn.execute(
                'SELECT gy_staging_cleanup_error FROM download_requests WHERE id=?', (int(expected['id']),),
            ).fetchone()
            if current is None or str(current['gy_staging_cleanup_error'] or '') != str(expected.get('gy_staging_cleanup_error') or ''):
                return False
            unknown = conn.execute(
                "SELECT 1 FROM organize_delete_audit WHERE trigger='download_staging_cleanup' "
                "AND status IN ('pending','failed') AND (file_id=? OR parent_id=?) LIMIT 1",
                (str(expected.get('gy_target_dir') or ''), str(expected.get('gy_target_dir') or '')),
            ).fetchone()
            if unknown:
                return False
        cur = conn.execute(
            'UPDATE download_requests SET gy_staging_cleanup_status=?,gy_staging_cleanup_error=?,updated_at=? '
            "WHERE id=? AND gy_isolated=1 AND gy_status='completed' "
            "AND status NOT IN ('cancelled','resubmitted','failed') "
            "AND COALESCE(organize_status,'') NOT IN ('resubmitted','cleared','failed','stopped') "
            "AND COALESCE(attention_cleared_at,'')='' "
            "AND COALESCE(gy_staging_cleanup_status,'')=? AND " + where,
            [status, (error or '')[:500], db.now(), int(expected['id']),
             str(expected.get('gy_staging_cleanup_status') or ''),
             *(str(expected.get(key) or '') for key in _IDENTITY_KEYS)],
        )
    return cur.rowcount == 1


def staging_confirmation_summary(source_id: str, *, conn=None) -> dict[str, int]:
    """同指纹只看最新确认，历史失败/被替代卡不能永远冻结已完成请求。"""
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        rows = connection.execute(
            "SELECT c.status,c.result_json FROM organize_confirmations c "
            f"WHERE NOT {_RELOCATED_CONFIRMATION_SQL} AND json_valid(c.payload_json) "
            "AND CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT)=? "
            "AND NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
            "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)", (source_id,),
        ).fetchall()
    result = {'total': len(rows), 'completed': 0, 'pending': 0, 'failed': 0}
    for row in rows:
        if row['status'] == 'completed':
            result['failed' if _unsafe_confirmation_stats(row['result_json']) else 'completed'] += 1
        elif row['status'] in ('pending', 'queued', 'running'):
            result['pending'] += 1
        else:
            # 取消、过期或失败不等于文件已成功入库，不冒报完成。
            result['failed'] += 1
    return result


def requeue_relocated_staging(expected: dict, *, parent_id: str, nsfw: bool) -> bool:
    """持有整理写锁时交接已移动来源；旧确认失效与重新落稳入队必须同事务。"""
    from app.repositories.download_requests import _update_download_request_conn

    source_id = str(expected.get('gy_target_dir') or '')
    if not parent_id or parent_id in ('0', source_id, expected.get('gy_staging_parent_dir')):
        return False
    with db.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        owners = list_staging_cleanup_requests(source_id=source_id, conn=conn)
        if len(owners) != 1 or not same_staging_identity(owners[0], expected):
            return False
        row = owners[0]
        if (row['gy_status'] != 'completed' or row['status'] not in ('completed', 'submitted', 'downloading', 'manual_review')
                or row['targets'] not in ('guangya', 'both') or row['organize_status'] != 'requires_manual'
                or int(row['organize_started'] or 0) < 0 or row['attention_cleared_at']
                or (row['gy_staging_cleanup_status'] or '') not in ('', 'pending', 'retained')):
            return False
        if conn.execute(
            "SELECT 1 FROM organize_delete_audit WHERE status IN ('pending','failed') "
            "AND (file_id=? OR parent_id=?) LIMIT 1", (source_id, source_id),
        ).fetchone() or conn.execute(
            "SELECT 1 FROM download_staging_reconcile WHERE request_id=? AND status='blocked' LIMIT 1",
            (row['id'],),
        ).fetchone():
            return False
        cards = conn.execute(
            "SELECT c.* FROM organize_confirmations c WHERE CASE WHEN json_valid(c.payload_json) THEN "
            "CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT)=? "
            "ELSE c.organize_task_id=? END", (source_id, row['organize_task_id']),
        ).fetchall()
        for card in cards:
            if card['status'] == 'pending':
                try:
                    if not requests_for_download_confirmation(json.loads(card['payload_json']), strict=True, conn=conn):
                        return False
                except (ValueError, TypeError, AttributeError):
                    return False
                continue
            if card['status'] == 'completed' and not _unsafe_confirmation_stats(card['result_json']):
                continue
            try:
                superseded = card['status'] == 'expired' and json.loads(card['result_json'] or '{}').get('resolution') in ('superseded', 'source_relocated')
            except (ValueError, AttributeError):
                superseded = False
            if not superseded:
                return False  # 不抢占已入队/执行中/失败未知或用户已取消的确认。
        timestamp = db.now()
        for card in cards:
            if card['status'] == 'pending':
                conn.execute(
                    "UPDATE organize_confirmations SET status='expired',selected_index=NULL,queued_at=NULL,task_id='',result_json=?,error=?,"
                    "review_status='cancelled',review_completed_at=?,completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
                    (json.dumps({'resolution': 'source_relocated'}), '目录已移动，原确认已由重新整理接管',
                     timestamp, timestamp, timestamp, card['id']),
                )
        _update_download_request_conn(
            conn, int(row['id']), dict(
                gy_staging_parent_dir=parent_id, gy_requested_target_dir=parent_id, gy_requested_nsfw=int(nsfw),
                # 已完成下载的重新整理只处理剩余文件，原整包数量不再是剩余目录门槛。
                gy_expected_file_count=0, gy_settle_snapshot='', gy_settle_stable_count=0,
                gy_settle_observed_file_count=0, gy_settle_attempts=0,
                gy_staging_cleanup_status='pending', gy_staging_cleanup_error='',
                organize_started=0, organize_status='settling', organize_task_id='', organize_run_id=None,
                organize_attempts=0, organize_next_retry_at=None, organize_finished_at=None,
                organize_error='目录来源已更新，等待重新核验云端文件落稳', error='',
                strm_run_id=None, strm_status='', strm_error='', strm_finished_at=None,
            ), timestamp,
        )
    return True


def requests_for_download_confirmation(payload: dict, *, strict: bool = False, conn=None) -> list[dict]:
    """新卡按业务关联校验，旧卡只接受唯一且精确绑定的隔离source ID。"""
    source_id = str(payload.get('source_dir_id') or '').strip()
    if source_id in ('', '0'):
        if strict and 'download_request_ids' in payload:
            raise ValueError('原下载请求的来源关联无效')
        return []
    owners = list_staging_cleanup_requests(source_id=source_id, conn=conn)
    if len(owners) != 1:
        if strict and (owners or 'download_request_ids' in payload):
            raise ValueError('原下载请求关联缺失或不唯一，请重新核对整理任务')
        return []
    row = owners[0]
    if 'download_request_ids' in payload:
        ids = payload['download_request_ids']
        if (
            not isinstance(ids, list) or not ids or len(ids) > 100
            or any(type(value) is not int or value <= 0 for value in ids)
            or int(row['id']) not in ids
        ):
            if strict:
                raise ValueError('原下载请求关联已变化，请重新核对整理任务')
            return []
    task_id = str(payload.get('organize_task_id') or '').strip()
    if task_id and task_id != str(row.get('organize_task_id') or ''):
        if strict:
            raise ValueError('原下载整理任务关联已变化，不能降级为普通整理')
        return []
    return [row]


def complete_staging_confirmation_phase(expected: dict) -> bool:
    """所有最新确认成功后，条件更新原下载整理阶段；不覆盖其它失败/新任务。"""
    where = ' AND '.join(f"COALESCE({key},'')=?" for key in _IDENTITY_KEYS)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        summary = staging_confirmation_summary(str(expected.get('gy_target_dir') or ''), conn=conn)
        if not summary['total'] or summary['completed'] != summary['total']:
            return False
        cur = conn.execute(
            "UPDATE download_requests SET organize_status='completed',organize_error='',"
            "organize_finished_at=?,updated_at=? WHERE id=? AND gy_isolated=1 AND gy_status='completed' "
            "AND status NOT IN ('cancelled','resubmitted','failed') "
            "AND organize_status IN ('completed','requires_manual') "
            "AND COALESCE(attention_cleared_at,'')='' AND " + where,
            [db.now(), db.now(), int(expected['id']),
             *(str(expected.get(key) or '') for key in _IDENTITY_KEYS)],
        )
    return cur.rowcount == 1
