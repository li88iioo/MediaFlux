"""整理操作历史与业务前像，单一事务恢复身份、位置和成员目标快照。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from typing import Any

from app.logger import get_logger
from app.repositories import recognition
from app import database as db
from app.repositories import telegram_notifications

logger = get_logger(__name__)

_BUSINESS_LOG_FIELDS = (
    "tmdb_id", "provider", "external_id", "media_type", "title", "year",
    "season", "episode", "new_path", "target_parent_id", "release_parse_json",
)
_FINISH_FIELDS = {"status", "operation_type", "current_parent_id", "current_name", "error"}


def active_organize_confirmation_refs(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    """在调用方事务内读取仍可执行的确认引用，供日志两入口和页面共用。"""
    files, tasks, tokens = set(), set(), set()
    for ref in conn.execute(
        "SELECT token,payload_json FROM organize_confirmations WHERE status IN ('pending','queued','running')"
    ):
        tokens.add("recognition-confirm:" + ref["token"])
        try:
            payload = json.loads(ref["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        tasks.add(str(payload.get("local_task_id") or ""))
        members = payload.get("files")
        if isinstance(members, list):
            files.update(str(item.get("file_id") or "") for item in members if isinstance(item, dict))
    files.discard("")
    tasks.discard("")
    return files, tasks, tokens


def settle_completed_confirmation_logs(conn: sqlite3.Connection, *, token: str = "") -> int:
    """确认终态与原待确认日志一起结算；启动恢复也使用同一规则。"""
    rows = conn.execute(
        """
        WITH matches AS (
            SELECT l.id AS log_id,l.file_id,l.updated_at AS log_updated_at,c.*,
                   ROW_NUMBER() OVER (PARTITION BY l.id ORDER BY c.id DESC) AS latest
            FROM organize_log l JOIN organize_confirmations c
            JOIN json_each(CASE WHEN json_valid(c.payload_json)
                           THEN c.payload_json ELSE '{}' END,'$.files') f
              ON json_extract(CASE WHEN f.type='object' THEN f.value ELSE '{}' END,'$.file_id')=l.file_id
            WHERE l.source='guangya' AND l.file_id<>'' AND (
                l.status='manual' OR (l.status='skipped' AND (
                    instr(COALESCE(l.error,''),'人工确认')>0 OR instr(COALESCE(l.error,''),'待确认')>0)))
              AND COALESCE(json_extract(CASE WHEN json_valid(c.payload_json)
                                        THEN c.payload_json ELSE '{}' END,'$.kind'),'guangya')='guangya'
        )
        SELECT * FROM matches WHERE latest=1 AND status='completed'
          AND (?='' OR token=?) AND (
              (json_type(payload_json,'$._organize_log_upper_bound')='integer'
               AND log_id<=json_extract(payload_json,'$._organize_log_upper_bound') AND log_updated_at<=created_at)
              OR (json_type(payload_json,'$._organize_log_upper_bound') IS NULL AND log_updated_at<created_at))
        """, (token, token),
    ).fetchall()
    if not rows:
        return 0
    active_files, _, _ = active_organize_confirmation_refs(conn)
    settled = 0
    for row in rows:
        if row["file_id"] in active_files:
            continue
        receipt = conn.execute(
            "SELECT status FROM organize_log WHERE source='guangya' AND file_id=? AND id>? "
            "AND operation_token=? ORDER BY id DESC LIMIT 1",
            (row["file_id"], row["log_id"], "recognition-confirm:" + row["token"]),
        ).fetchone()
        status, reason = "confirmed", ""
        if receipt is None:
            # 旧版本的执行日志可能已清理。仅凭完整、无异常的组结果恢复跳过项，
            # 不把未知结果猜成成功，也不改写重新扫描后产生的新待确认记录。
            try:
                stats = json.loads(row["result_json"])
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(stats, dict) or not (
                all(type(stats.get(key)) is int for key in ("total", "moved", "skipped", "failed", "need_confirm"))
                and stats["skipped"] > 0
                and stats.get("failed") == 0 and stats.get("need_confirm") == 0
                and not any(stats.get(key) for key in ("stopped", "conflict", "rename_failed"))
                and stats.get("total") == len(payload["files"])
                and stats["total"] == stats.get("moved", 0) + stats["skipped"]
            ):
                continue
            members = {str(item.get("file_id") or "") for item in payload["files"] if isinstance(item, dict)}
            moved = {r["file_id"] for r in conn.execute(
                "SELECT DISTINCT file_id FROM organize_log WHERE source='guangya' AND status='confirmed' "
                "AND file_id IN (SELECT value FROM json_each(?)) AND created_at<? AND updated_at=?",
                (json.dumps(sorted(members)), row["created_at"], row["completed_at"]),
            )}
            # 混合组必须保留入库成员的结算证据；组计数本身不能推断哪一项跳过。
            if len(moved) != stats["moved"] or len(members - moved) != stats["skipped"] or row["file_id"] in moved:
                continue
            status = "skipped"
            reason = f"确认处理已结束；本组入库 {stats['moved']} 个、跳过 {stats['skipped']} 个"
        elif receipt["status"] not in {"success", "skipped", "failed"}:
            continue
        stamp = row["completed_at"] or row["updated_at"]
        conn.execute(
            "UPDATE organize_log SET status=?,error=?,confirmation_actor=?,version=version+1,updated_at=? WHERE id=?",
            (status, reason, row["confirmation_actor"], stamp, row["log_id"]),
        )
        conn.execute(
            "UPDATE organize_log_items SET status=?,error=?,updated_at=? "
            "WHERE log_id=? AND status IN ('manual','skipped')",
            (status, reason, stamp, row["log_id"]),
        )
        settled += 1
    return settled


def reconcile_reorganized_confirmations(
    source_ids: list[str] | None = None, *, file_ids: list[str] | None = None,
    conn=None, recovery: dict | None = None,
) -> list[str]:
    """以成功重整快照关闭旧确认，同事务登记收尾和回执，并补齐旧重整终态。"""
    if source_ids == [] or file_ids == []:
        return []
    from app.modules.organize_confirmations import enqueue_reorganized_confirmation_delivery

    from app.repositories.download_staging import (
        _RELOCATED_CONFIRMATION_SQL, list_staging_cleanup_requests, requests_for_download_confirmation,
    )
    from app.repositories.download_staging_reconcile import (
        _eligible, _object, _recover_reorganized_request, _success, enqueue_confirmation_cleanup,
    )

    resolved: list[str] = []
    clauses = ["json_valid(c.payload_json)",
               "COALESCE(json_extract(c.payload_json,'$.kind'),'guangya')='guangya'",
               "NOT " + _RELOCATED_CONFIRMATION_SQL,
               "NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
               "WHERE newer.fingerprint=c.fingerprint AND newer.id>c.id)"]
    params: list = []
    if source_ids is None:
        # 实时每个视频只核对相关 pending；启动恢复只扫描一次未决候选，不重扫历史回执。
        clauses.append("c.status='pending'")
    else:
        clauses.append("(c.status IN ('pending','failed','expired','cancelled') OR "
                       "(c.status='completed' AND CASE WHEN json_valid(c.result_json) THEN "
                       "json_extract(c.result_json,'$.resolution') END='reorganized'))")
    if source_ids is not None:
        clauses.append("CAST(json_extract(c.payload_json,'$.source_dir_id') AS TEXT) IN ("
                       + ",".join("?" for _ in source_ids) + ")")
        params.extend(source_ids)
    if file_ids is not None:
        clauses.append("EXISTS (SELECT 1 FROM json_each(c.payload_json,'$.files') f "
                       "WHERE json_extract(CASE WHEN f.type='object' THEN f.value ELSE '{}' END,'$.file_id') "
                       "IN (SELECT value FROM json_each(?)))")
        params.append(json.dumps(file_ids))
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        if conn is None:
            connection.execute('BEGIN IMMEDIATE')
        if recovery is not None:
            _recover_reorganized_request(connection, recovery, source_ids=source_ids or [])
        confirmations = connection.execute(
            "SELECT c.* FROM organize_confirmations c WHERE " + " AND ".join(clauses), params,
        ).fetchall()
        for confirmation in confirmations:
            if confirmation['status'] == 'completed':
                # 已提交的成功不重写历史，也不再触碰可能已清理的目录。
                if _success(confirmation['result_json']):
                    enqueue_reorganized_confirmation_delivery(
                        connection, confirmation, timestamp=db.now(), repair_existing=True,
                    )
                continue
            payload = _object(confirmation['payload_json'])
            source_id = str(payload.get('source_dir_id') or '')
            try:
                owners = requests_for_download_confirmation(payload, strict=True, conn=connection)
            except ValueError:
                # 显式下载关联损坏、多个隔离 owner 或任务换绑均不能降级为普通来源。
                continue
            files = payload.get('files')
            companions = payload.get('companions', [])
            if ((owners and not _eligible(owners[0]))
                    or (not owners and confirmation['status'] != 'pending')
                    or not isinstance(files, list) or not files
                    or not isinstance(companions, list)):
                continue
            companion_evidence = {}
            companion_ids = [str(item.get('file_id') or '') for item in companions if isinstance(item, dict)]
            if companion_ids:
                # 伴随文件可能属于多个视频日志：整包只扫描一次明细，再走唯一键取快照。
                marks = ','.join('?' for _ in companion_ids)
                companion_evidence = {row['file_id']: row for row in connection.execute(
                    "WITH latest AS (SELECT file_id,MAX(log_id) AS log_id FROM organize_log_items "
                    f"WHERE file_id IN ({marks}) GROUP BY file_id) "
                    "SELECT i.*,l.status AS log_status,l.operation_type,l.source_dir_id,l.legacy_incomplete,"
                    "l.created_at AS log_created_at,l.operation_token AS log_operation_token,"
                    "(SELECT MAX(id) FROM organize_log WHERE source='guangya' AND file_id=i.file_id) AS primary_log_id "
                    "FROM latest JOIN organize_log_items i ON i.log_id=latest.log_id AND i.file_id=latest.file_id "
                    "JOIN organize_log l ON l.id=i.log_id WHERE l.source='guangya'", companion_ids,
                )}
            log_ids: set[int] = set()
            for index, item in enumerate([*files, *companions]):
                if not isinstance(item, dict) or not item.get('file_id') or not item.get('parent_id'):
                    break
                if index < len(files):
                    # 先用主日志 file_id 索引定位最新操作；缺明细也不能回退到旧成功。
                    evidence = connection.execute(
                        "SELECT i.*,l.status AS log_status,l.operation_type,l.source_dir_id,l.legacy_incomplete,"
                        "l.created_at AS log_created_at,l.operation_token AS log_operation_token,"
                        "l.id AS primary_log_id FROM organize_log l LEFT JOIN organize_log_items i "
                        "ON i.log_id=l.id AND i.file_id=l.file_id "
                        "WHERE l.source='guangya' AND l.file_id=? ORDER BY l.id DESC LIMIT 1", (str(item['file_id']),),
                    ).fetchone()
                else:
                    evidence = companion_evidence.get(str(item['file_id']))
                if (evidence is None or evidence['log_status'] != 'success'
                        or evidence['legacy_incomplete'] or evidence['log_id'] is None
                        or int(evidence['primary_log_id'] or 0) > int(evidence['log_id'])
                        or evidence['operation_type'] not in ('organize', 'reorganize') or evidence['status'] != 'success'
                        or (str(evidence['source_dir_id']) != source_id and (
                            owners or str(evidence['source_dir_id']) != str(item['parent_id'])))
                        or str(evidence['original_parent_id']) != str(item['parent_id'])
                        or not evidence['target_parent_id']
                        or evidence['current_parent_id'] != evidence['target_parent_id']
                        or evidence['current_parent_id'] == evidence['original_parent_id']
                        or evidence['current_name'] != evidence['target_name']
                        or (item.get('size') is not None and item['size'] != evidence['size'])
                        or (item.get('etag') and str(item['etag']).lower() != str(evidence['etag']).lower())):
                    break
                if (not owners and str(evidence['source_dir_id']) != source_id
                        and list_staging_cleanup_requests(source_id=str(evidence['source_dir_id']), conn=connection)):
                    # 目录刮削入口本身属于隔离下载时，不能借更大的旧扫描根绕过下载归属。
                    break
                # 新审计以创建时冻结的 ID 上界区分同秒先后；原地重整使用本次成功步骤。
                upper_bound = payload.get('_organize_log_upper_bound')
                new_log = (int(evidence['log_id']) > upper_bound if type(upper_bound) is int
                           else str(evidence['log_created_at']) > confirmation['created_at'])
                if new_log:
                    if str(evidence['log_created_at']) < confirmation['created_at']:
                        break
                else:
                    step = connection.execute(
                        "SELECT finished_at FROM organize_operation_steps WHERE log_id=? AND operation_token=? "
                        "AND file_id=? AND action='move_rename' AND status='success' "
                        "AND to_parent_id=? AND to_name=? ORDER BY id DESC LIMIT 1",
                        (evidence['log_id'], evidence['log_operation_token'], str(item['file_id']),
                         evidence['current_parent_id'], evidence['current_name']),
                    ).fetchone()
                    if (evidence['operation_type'] != 'reorganize' or step is None
                            or str(step['finished_at'] or '') <= confirmation['created_at']):
                        break
                log_ids.add(int(evidence['log_id']))
            else:
                # 同组字幕等伴随文件也必须落稳，不能只凭视频的成功记录释放目录。
                placeholders = ','.join('?' for _ in log_ids)
                unsafe = connection.execute(
                    "SELECT 1 FROM organize_log_items WHERE log_id IN (" + placeholders + ") "
                    "AND (status!='success' OR COALESCE(target_parent_id,'')='' "
                    "OR COALESCE(current_parent_id,'')!=target_parent_id "
                    "OR COALESCE(current_name,'')!=COALESCE(target_name,'')) LIMIT 1",
                    tuple(log_ids),
                ).fetchone()
                if unsafe:
                    continue
                timestamp = db.now()
                stats = {
                    'moved': len(files), 'failed': 0, 'need_confirm': 0,
                    'resolution': 'reorganized', 'organize_log_ids': sorted(log_ids),
                    'previous_confirmation': {
                        'status': confirmation['status'], 'error': confirmation['error'],
                        'result': _object(confirmation['result_json']),
                    },
                }
                connection.execute(
                    "UPDATE organize_confirmations SET status='completed',result_json=?,error='',"
                    "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                    "THEN 'cancelled' ELSE review_status END,"
                    "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                    "THEN ? ELSE review_completed_at END,"
                    "completed_at=?,updated_at=?,rollup_applied=0 WHERE id=? AND status=?",
                    (json.dumps(stats, ensure_ascii=False), timestamp, timestamp, timestamp,
                     int(confirmation['id']), confirmation['status']),
                )
                token = str(confirmation['token'])
                enqueue_confirmation_cleanup(connection, token=token, timestamp=timestamp)
                completed = connection.execute(
                    'SELECT * FROM organize_confirmations WHERE id=?', (int(confirmation['id']),),
                ).fetchone()
                assert completed is not None  # 同一写事务内刚刚更新的确认必定存在。
                enqueue_reorganized_confirmation_delivery(connection, completed, timestamp=timestamp)
                resolved.append(token)
    return resolved


def list_related_organize_logs(log_id: int) -> list[sqlite3.Row]:
    """同一云端主文件的其它历史记录；只提供导航，不替换任一历史快照。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT other.id,other.status,other.updated_at FROM organize_log AS current "
            "JOIN organize_log AS other ON other.source=current.source AND other.file_id=current.file_id "
            "WHERE current.id=? AND current.file_id!='' AND other.id!=current.id "
            "ORDER BY other.updated_at DESC,other.id DESC LIMIT 5", (log_id,),
        ).fetchall()


def list_organize_operation_steps(log_id: int, limit: int = 300) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_operation_steps WHERE log_id=? ORDER BY id DESC LIMIT ?",
            (log_id, max(1, min((limit or 300), 1000))),
        ).fetchall()


def add_organize_operation_step(
    log_id: int,
    operation_token: str,
    step_index: int,
    action: str,
    **fields,
) -> int:
    timestamp = db.now()
    before = fields.get("state_before")
    before_json = (
        json.dumps(_validated_snapshot(before), ensure_ascii=False, separators=(",", ":"))
        if before is not None else ""
    )
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO organize_operation_steps(log_id,operation_token,step_index,action,file_id,"
            "from_parent_id,from_name,to_parent_id,to_name,status,error,started_at,finished_at,state_before_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                log_id,
                operation_token,
                step_index,
                action,
                str(fields.get("file_id") or ""),
                str(fields.get("from_parent_id") or ""),
                str(fields.get("from_name") or ""),
                str(fields.get("to_parent_id") or ""),
                str(fields.get("to_name") or ""),
                str(fields.get("status") or "pending"),
                str(fields.get("error") or ""),
                fields.get("started_at") or timestamp,
                fields.get("finished_at"),
                before_json,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def finish_organize_operation_step(step_id: int, status: str, error: str = "") -> bool:
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_operation_steps SET status=?,error=?,finished_at=? WHERE id=?",
            (status, error, db.now(), step_id),
        )
        return cur.rowcount == 1


def begin_return_compensation(log_id: int, token: str, step_ids: list[int]) -> None:
    """先撤销成功步骤的提交资格，再允许反向移动；不可记账时不启动补偿。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.executemany(
            "UPDATE organize_operation_steps SET status='running',finished_at=NULL,"
            "error='送回操作正在补偿，结果待核验' WHERE id=? AND log_id=? AND operation_token=? "
            "AND EXISTS(SELECT 1 FROM organize_log WHERE id=? AND operation_token=? AND status='returning')",
            [(step_id, log_id, token, log_id, token) for step_id in step_ids],
        )
        if updated.rowcount != len(step_ids):
            raise RuntimeError("送回操作身份已变化，未执行补偿")


def _recover_return_receipts_conn(conn, timestamp: str, *, limit: int) -> int:
    """仅按原 token 的完整已提交步骤补记回执；不执行云端写入或后处理。"""
    logs = conn.execute(
        "SELECT * FROM organize_log WHERE status='returning' ORDER BY id LIMIT ?", (limit,),
    ).fetchall()
    for log in logs:
        items = conn.execute("SELECT * FROM organize_log_items WHERE log_id=?", (log["id"],)).fetchall()
        steps = conn.execute(
            "SELECT * FROM organize_operation_steps WHERE log_id=? AND operation_token=? "
            "AND action='return_to_source'", (log["id"], log["operation_token"]),
        ).fetchall()
        by_file = {step["file_id"]: step for step in steps}
        video = next((item for item in items if item["file_id"] == log["file_id"]), None)
        complete = video is not None and len(items) == len(steps) == len(by_file) and all(
            (step := by_file.get(item["file_id"])) is not None
            and item["status"] == step["status"] == "success"
            and (item["current_parent_id"], item["current_name"]) == (step["to_parent_id"], step["to_name"])
            for item in items
        )
        notice = (
            "文件送回步骤已完成，终态已补记；空目录清理、STRM与媒体库刷新未执行，请按需重新同步"
            if complete else "送回操作已中断，步骤或补偿结果不完整；执行前必须重新核验全部云端快照"
        )
        conn.execute(
            "UPDATE organize_log SET status=?,current_parent_id=?,current_name=?,operation_type=?,"
            "error=?,version=version+1,updated_at=? WHERE id=?",
            ("reverted" if complete else "interrupted",
             video["current_parent_id"] if complete and video is not None else log["current_parent_id"],
             video["current_name"] if complete and video is not None else log["current_name"],
             "return_to_source" if complete else log["operation_type"], notice, timestamp, log["id"]),
        )
        if complete:
            recognition.sync_manual_episode_confirmation(conn, log["id"], revoke=True)
        if not complete:
            conn.execute(
                "UPDATE organize_operation_steps SET status='interrupted',error=?,finished_at=? "
                "WHERE log_id=? AND operation_token=? AND status='running'",
                (notice, timestamp, log["id"], log["operation_token"]),
            )
    return len(logs)


def recover_return_receipts() -> int:
    """调用方须持有整理写锁；既有 dispatcher 每周期最多补记一条。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _recover_return_receipts_conn(conn, db.now(), limit=1)


def _validated_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        raise ValueError("整理业务前像格式无效")
    log = value.get("log")
    items = value.get("items")
    if not isinstance(log, dict) or not isinstance(items, list):
        raise ValueError("整理业务前像缺少日志或成员状态")
    if any(key not in log for key in _BUSINESS_LOG_FIELDS):
        raise ValueError("整理业务前像字段不完整，不能猜测旧媒体身份")
    clean_items = []
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict) or not all(key in item for key in ("id", "file_id", "target_parent_id", "target_name")):
            raise ValueError("整理成员前像字段不完整")
        item_id = int(item["id"])
        file_id = str(item["file_id"] or "")
        if item_id <= 0 or not file_id or item_id in seen_ids:
            raise ValueError("整理成员前像 ID 无效或重复")
        seen_ids.add(item_id)
        clean_items.append({"id": item_id, "file_id": file_id,
                            "target_parent_id": item["target_parent_id"], "target_name": item["target_name"]})
    clean_log = {key: log[key] for key in _BUSINESS_LOG_FIELDS}
    if "confirmation_actor" in log:
        clean_log["confirmation_actor"] = log["confirmation_actor"]
    state: dict[str, Any] = {"version": 1, "log": clean_log, "items": clean_items}
    if "manual_episode_confirmation" in value:
        learning = value["manual_episode_confirmation"]
        if learning is not None and not isinstance(learning, dict):
            raise ValueError("人工学习前像格式无效")
        state["manual_episode_confirmation"] = learning
    return state


def capture_organize_business_snapshot(log_id: int) -> dict[str, Any]:
    """同一 SQLite 读快照中取得前像；不要把临时 busy 状态当成待恢复身份。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        log = conn.execute(
            f"SELECT {','.join(_BUSINESS_LOG_FIELDS)},confirmation_actor FROM organize_log WHERE id=?", (log_id,),
        ).fetchone()
        if log is None:
            raise LookupError("整理日志不存在")
        items = conn.execute(
            "SELECT id,file_id,target_parent_id,target_name FROM organize_log_items "
            "WHERE log_id=? ORDER BY id", (log_id,),
        ).fetchall()
        learning = conn.execute(
            "SELECT c.* FROM manual_episode_confirmations c JOIN organize_log l "
            "ON l.source=c.source AND l.file_id=c.file_id WHERE l.id=?", (log_id,),
        ).fetchone()
        return {"version": 1, "log": dict(log), "items": [dict(row) for row in items],
                "manual_episode_confirmation": dict(learning) if learning is not None else None}


def restore_organize_business_snapshot(log_id: int, snapshot: dict, **log_fields) -> bool:
    """文件补偿全部成功后原子恢复业务前像；任何成员失配时整笔拒绝。"""
    state = _validated_snapshot(snapshot)
    if set(log_fields) - _FINISH_FIELDS:
        raise ValueError("不允许用收尾字段覆盖业务前像")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT id FROM organize_log WHERE id=?", (log_id,)).fetchone() is None:
            raise LookupError("整理日志不存在")
        members = conn.execute(
            "SELECT id,file_id FROM organize_log_items WHERE log_id=?", (log_id,),
        ).fetchall()
        actual = {(int(row["id"]), str(row["file_id"])) for row in members}
        expected = {(item["id"], item["file_id"]) for item in state["items"]}
        if actual != expected:
            raise ValueError("媒体组成员已变化，业务前像不能安全应用")
        timestamp = db.now()
        values = {**state["log"], **log_fields}
        assignments = ','.join(f'{key}=?' for key in values)
        conn.execute(
            f"UPDATE organize_log SET {assignments},version=version+1,updated_at=? WHERE id=?",
            [*values.values(), timestamp, log_id],
        )
        for item in state["items"]:
            conn.execute(
                "UPDATE organize_log_items SET target_parent_id=?,target_name=?,updated_at=? "
                "WHERE id=? AND log_id=? AND file_id=?",
                (item["target_parent_id"], item["target_name"], timestamp, item["id"], log_id, item["file_id"]),
            )
        if "manual_episode_confirmation" in state:
            recognition.restore_manual_episode_confirmation(conn, log_id, state["manual_episode_confirmation"])
        else:
            # 旧前像没有独立学习记录；沿用可验证的旧状态，不猜测已删除的经验。
            recognition.sync_manual_episode_confirmation(conn, log_id, revoke=log_fields.get("status") == "reverted")
    return True


def list_latest_reversible_organize_steps(log_id: int) -> list[sqlite3.Row]:
    """回退使用完整操作集，不复用日志页最近300步的展示窗口。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        latest = conn.execute(
            "SELECT operation_token FROM organize_operation_steps "
            "WHERE log_id=? AND status='success' AND action='move_rename' "
            "ORDER BY id DESC LIMIT 1", (log_id,),
        ).fetchone()
        if latest is None:
            return []
        return conn.execute(
            "SELECT * FROM organize_operation_steps WHERE log_id=? AND operation_token=? "
            "AND status='success' AND action='move_rename' ORDER BY id DESC",
            (log_id, latest["operation_token"]),
        ).fetchall()


def list_pending_organize_probe_steps(
    log_id: int, job_id: int, *, include_succeeded: bool = False,
) -> list[sqlite3.Row]:
    """完整恢复写意图；收尾可带成功步骤，作为跨进程的已提交事实。"""
    statuses = ("running", "interrupted", "success") if include_succeeded else ("running", "interrupted")
    placeholders = ",".join("?" for _ in statuses)
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_operation_steps WHERE log_id=? "
            "AND operation_token GLOB ? AND action='probe_rename' "
            f"AND status IN ({placeholders}) ORDER BY id DESC",
            (log_id, f"probe:{job_id}:*", *statuses),
        ).fetchall()


def _settle_organize_task_run_conn(conn, run_id: int, result: dict, timestamp: str) -> dict:
    """只结算已执行的整理回执；单请求失败不回滚其它请求或重放云端动作。"""
    from app.repositories.download_requests import (
        _GUANGYA_FOLLOWUP_ALLOWED_SQL,
        _update_download_request_conn,
    )
    from app.repositories.media_subscriptions import (
        _sync_media_download_admissions_conn,
    )

    row = conn.execute(
        "SELECT status,result FROM task_runs WHERE id=? AND task_name='guangya_organize'",
        (run_id,),
    ).fetchone()
    if row is None:
        raise LookupError("整理运行记录不存在")
    stored = json.loads(row["result"] or "{}")
    if stored.get("task_id") != result.get("task_id"):
        raise ValueError("整理运行身份已变化")
    if row["status"] != "running" and not stored.get("request_settlement"):
        return stored
    # 另一个进程可能已结算其中一部分，旧内存回执不能复活已移除的待结算项。
    result = dict(stored if stored.get("request_settlement") else result)
    settlement = dict(result["request_settlement"])
    pending = {}
    for request_id, original_fields in settlement["requests"].items():
        conn.execute("SAVEPOINT organize_request_settlement")
        try:
            request = conn.execute(
                "SELECT * FROM download_requests WHERE id=? AND "
                + _GUANGYA_FOLLOWUP_ALLOWED_SQL, (int(request_id),),
            ).fetchone()
            if (request is not None and request["organize_task_id"] == result["task_id"]
                    and request["organize_run_id"] == run_id):
                fields = dict(original_fields)
                generation = fields.pop("_strm_generation", 0)
                if (request["strm_generation"] != generation
                        or request["strm_status"] not in {"pending", "failed"}):
                    fields = {key: value for key, value in fields.items() if not key.startswith("strm_")}
                if "organize_started" not in fields:
                    fields["organize_started"] = 1
                _update_download_request_conn(conn, int(request_id), fields, timestamp)
                _sync_media_download_admissions_conn(conn, int(request_id), timestamp)
        except Exception as exc:
            conn.execute("ROLLBACK TO organize_request_settlement")
            pending[str(request_id)] = original_fields
            logger.warning("整理请求结算待恢复 request=%s type=%s", request_id, type(exc).__name__)
        finally:
            conn.execute("RELEASE organize_request_settlement")
    result.update({key: settlement[key] for key in ("status", "error", "message")})
    if pending:
        settlement["requests"] = pending
        result["request_settlement"] = settlement
        result.update(status="partial", message="云端整理已结束，关联请求仍待结算",
                      error="部分关联请求结算未完成；恢复时仅补结算，勿重复整理")
    else:
        result.pop("request_settlement", None)
    conn.execute(
        "UPDATE task_runs SET status=?,finished_at=?,result=?,error=? WHERE id=?",
        ({"completed": "success", "stopped": "skipped"}.get(result["status"], result["status"]),
         timestamp, json.dumps(result, ensure_ascii=False, default=str),
         result.get("persistence_error") or result["error"], run_id),
    )
    return result


def settle_organize_task_run(run_id: int, result: dict) -> dict:
    """任务回执、关联请求及订阅准入在同一提交点可见。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _settle_organize_task_run_conn(conn, run_id, result, db.now())


def next_unsettled_organize_task_run(after_id: int = 0) -> sqlite3.Row | None:
    """既有 dispatcher 每周期取一个回执，轮转而非只看近期运行记录。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id,result FROM task_runs WHERE task_name='guangya_organize' AND status='partial' "
            "AND json_valid(result) AND json_type(result,'$.request_settlement')='object' "
            "ORDER BY (id<=?),id LIMIT 1", (after_id,),
        ).fetchone()


def _recover_after_restart(conn, timestamp: str) -> None:
    """恢复整理确认、写审计与版本化运行结果；回执使用同一事务。"""
    reconcile_reorganized_confirmations(conn=conn)
    settle_completed_confirmation_logs(conn)
    _recover_return_receipts_conn(conn, timestamp, limit=-1)
    for run in conn.execute(
        "SELECT id,result FROM task_runs WHERE task_name='guangya_organize' AND status='partial' "
        "AND json_valid(result) AND json_type(result,'$.request_settlement')='object'"
    ).fetchall():
        _settle_organize_task_run_conn(conn, int(run["id"]), json.loads(run["result"]), timestamp)
    interrupted_confirmations = conn.execute(
        "SELECT token,chat_id,directory_path,payload_json FROM organize_confirmations "
        "WHERE status='running'"
    ).fetchall()
    for interrupted in interrupted_confirmations:
        try:
            stored_payload = json.loads(
                str(interrupted["payload_json"] or "{}")
            )
        except (TypeError, ValueError):
            stored_payload = {}
        if not isinstance(stored_payload, dict):
            stored_payload = {}
        try:
            message_id = int(stored_payload.get("_telegram_message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        event_json = json.dumps(
            {
                "title": "❌ Telegram 确认整理已中断",
                "fields": [["目录", str(interrupted["directory_path"] or "/")]],
                "lines": [],
                "image_url": "",
                "footer": "上次进程在执行期间中断，结果未确认。请重新执行整理生成新候选。",
                "actions": [],
                "layout": "relaxed",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        telegram_notifications._enqueue_confirmation_delivery(
            conn, token=str(interrupted["token"] or ""), event_json=event_json,
            chat_id=str(interrupted["chat_id"] or ""), message_id=message_id or None,
            timestamp=timestamp,
        )

    conn.execute(
        "UPDATE organize_confirmations SET status='failed',"
        "error=CASE WHEN COALESCE(error,'')='' "
        "THEN '上次进程在 Telegram 确认整理执行期间中断，请重新执行整理' "
        "ELSE error END,completed_at=COALESCE(completed_at,?),"
        "rollup_applied=0,updated_at=? WHERE status='running'",
        (timestamp, timestamp),
    )
    # pending 候选由 Telegram 确认调度器统一过期并投递终态卡片；
    # 这里提前改状态会让卡片与父任务汇总失去主动收口机会。
    conn.execute(
        "UPDATE organize_log SET status='interrupted',error=CASE "
        "WHEN COALESCE(error,'')='' THEN '上次进程在云端写操作期间中断，必须重新核验快照' "
        "ELSE error END,updated_at=? "
        "WHERE status IN ('reorganizing','returning','reverting','deleting')",
        (timestamp,),
    )
    conn.execute(
        "UPDATE organize_operation_steps SET status='interrupted',"
        "error=CASE WHEN COALESCE(error,'')='' THEN '进程中断，步骤结果需要人工核验' ELSE error END,"
        "finished_at=COALESCE(finished_at,?) WHERE status='running'",
        (timestamp,),
    )
    interrupted_organize_runs = conn.execute(
        "SELECT id,result,error FROM task_runs "
        "WHERE task_name='guangya_organize' AND status='running'"
    ).fetchall()
    if interrupted_organize_runs:
        # 局部导入避免底层数据库模块在正常路径依赖整理实现；恢复时仍
        # 复用唯一的版本化协议，禁止重新落回自由格式 result。
        from app.modules.organize_results import read_organize_result

        interrupted_message = "上次进程在整理任务运行期间中断"
        for interrupted_run in interrupted_organize_runs:
            try:
                raw_result = json.loads(str(interrupted_run["result"] or "{}"))
            except (TypeError, ValueError):
                raw_result = {}
            normalized_result = read_organize_result(raw_result)
            previous_error = str(
                normalized_result.get("error") or interrupted_run["error"] or ""
            ).strip()
            if previous_error and previous_error != interrupted_message:
                normalized_result["previous_error"] = previous_error
            normalized_result["status"] = "failed"
            normalized_result["error"] = interrupted_message
            conn.execute(
                "UPDATE task_runs SET status='failed',"
                "finished_at=COALESCE(finished_at,?),result=?,"
                "error=? "
                "WHERE id=? AND status='running'",
                (
                    timestamp,
                    json.dumps(
                        normalized_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    interrupted_message,
                    int(interrupted_run["id"]),
                ),
            )
    conn.execute(
        "UPDATE organize_delete_audit SET status='interrupted',"
        "error='上次进程在光鸭 provider 调用期间中断，结果未知，需人工核验',"
        "provider_result='光鸭回收站结果未知，需人工核验',updated_at=? "
        "WHERE status='pending'",
        (timestamp,),
    )
