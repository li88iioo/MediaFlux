"""整理候选确认、只读复核与 Telegram 确认投递的持久化。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app import database as db
from app.repositories import telegram_notifications

# 重试确认绑定原候选的 payload 键；仓储与确认服务共用。
RETRY_SELECTED_INDEX_KEY = "_retry_selected_index"


def create_organize_confirmation(
    *,
    token: str,
    fingerprint: str,
    chat_id: str,
    source_name: str,
    directory_path: str,
    payload: dict,
    expires_at: str,
    organize_task_id: str = "",
    review_requested: bool = False,
    review_ready: bool = False,
) -> int:
    """持久化一组 Telegram 整理候选，并使同指纹旧按钮失效。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 冻结确认创建时的日志范围，秒级时间戳不能区分同秒的新扫描记录。
        payload = dict(payload)
        if "_organize_log_upper_bound" not in payload:
            payload["_organize_log_upper_bound"] = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM organize_log"
            ).fetchone()[0]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE organize_confirmations SET status='expired',"
            "result_json=?,error='新的候选卡已替代本次确认',"
            "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN 'cancelled' ELSE review_status END,"
            "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN ? ELSE review_completed_at END,"
            "completed_at=COALESCE(completed_at,?),rollup_applied=0,updated_at=? "
            "WHERE fingerprint=? AND status='pending'",
            (
                json.dumps({"resolution": "superseded"}, ensure_ascii=False),
                timestamp,
                timestamp,
                timestamp,
                fingerprint,
            ),
        )
        cursor = conn.execute(
            "INSERT INTO organize_confirmations("
            "token,fingerprint,chat_id,source_name,directory_path,payload_json,status,"
            "organize_task_id,rollup_applied,review_status,expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,'pending',?,0,?,?,?,?)",
            (
                token,
                fingerprint,
                (chat_id or ""),
                (source_name or ""),
                (directory_path or ""),
                encoded,
                (organize_task_id or ""),
                ("pending" if review_ready else "waiting") if review_requested else "",
                expires_at,
                timestamp,
                timestamp,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def get_organize_confirmation(token: str) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            ((token or ""),),
        ).fetchone()


def query_organize_confirmations(
    *, batch_id: str = "", task_id: str = "", source: str = "", reason: str = "",
    keyword: str = "", status: str = "pending", tokens: list[str] | None = None,
    chat_id: str | None = None, page: int = 1, page_size: int = 20, selection_limit: int = 1000,
) -> dict:
    """Web、TG 和 SDK 使用同一持久候选集合；筛选值始终为绑定参数。"""
    clauses = ["json_valid(payload_json)", "COALESCE(json_extract(payload_json,'$.kind'),'guangya')='guangya'"]
    values: list[object] = []
    timestamp = db.now()
    for column, value in (("organize_task_id", task_id), ("source_name", source),
                          ("json_extract(payload_json,'$._confirmation_batch')", batch_id)):
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    if chat_id is not None:
        clauses.append("chat_id=?")
        values.append(chat_id)
    if status == "expired":
        clauses.append("(status='expired' OR (status='pending' AND expires_at<=?))")
        values.append(timestamp)
    elif status != "all":
        clauses.append("status=?")
        values.append(status)
        if status == "pending":
            clauses.append("expires_at>?")
            values.append(timestamp)
    for expression, text in (("COALESCE(json_extract(payload_json,'$.reason'),'')", reason),
                             ("directory_path || ' ' || source_name || ' ' || "
                              "COALESCE(json_extract(payload_json,'$.identity'),'')", keyword)):
        if text:
            clauses.append(f"instr(lower({expression}),lower(?))>0")
            values.append(text)
    if tokens is not None:
        if not tokens:
            return {"rows": [], "tokens": [], "total": 0, "counts": {}, "log_ids": {}}
        clauses.append("token IN (" + ",".join("?" for _ in tokens) + ")")
        values.extend(tokens)
    elif not batch_id:
        clauses.append("NOT EXISTS (SELECT 1 FROM organize_confirmations newer "
                       "WHERE newer.fingerprint=organize_confirmations.fingerprint "
                       "AND newer.id>organize_confirmations.id)")
    where = " FROM organize_confirmations WHERE " + " AND ".join(clauses)
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        counts = dict(conn.execute(
            "SELECT CASE WHEN status='pending' AND expires_at<=? THEN 'expired' ELSE status END AS state,"
            "COUNT(*)" + where + " GROUP BY state", [timestamp, *values],
        ).fetchall())
        total = sum(counts.values())
        rows = conn.execute(
            "SELECT *" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            [*values, page_size, min((page - 1) * page_size, total)],
        ).fetchall()
        selected = conn.execute(
            "SELECT token" + where + " ORDER BY id DESC LIMIT ?", [*values, selection_limit],
        ).fetchall()
        # 一次查询本页的精确执行关联；不按标题猜测，也不为每条确认单分别查日志。
        log_ids: dict[str, list[int]] = {}
        if rows:
            operations = {f"recognition-confirm:{row['token']}": row["token"] for row in rows}
            for log in conn.execute(
                "SELECT id,operation_token FROM organize_log WHERE operation_token!='' AND operation_token IN ("
                + ",".join("?" for _ in operations) + ") ORDER BY id", list(operations),
            ):
                log_ids.setdefault(operations[log["operation_token"]], []).append(log["id"])
    return {"rows": rows, "tokens": [row["token"] for row in selected],
            "total": total, "counts": counts, "log_ids": log_ids}


def request_organize_confirmation_reviews(tokens: list[str], *, chat_id: str | None = None) -> dict:
    """只读复核显式请求；切换模式与自动写入竞争同一数据库事务。"""
    counts = {"requested": 0, "unchanged": 0, "unavailable": 0}
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for token in tokens:
            row = conn.execute("SELECT * FROM organize_confirmations WHERE token=?", (token,)).fetchone()
            if (row is None or (chat_id is not None and row["chat_id"] != chat_id)
                    or row["status"] != "pending" or row["expires_at"] <= timestamp):
                counts["unavailable"] += 1
                continue
            payload = json.loads(row["payload_json"])
            if not payload.get("candidates"):
                counts["unavailable"] += 1
                continue
            payload["review_only"] = True
            active = row["review_status"] in {"pending", "running", "approved"}
            conn.execute(
                "UPDATE organize_confirmations SET payload_json=?,review_status=?,"
                "review_attempts=?,updated_at=? WHERE token=?",
                (json.dumps(payload, ensure_ascii=False), row["review_status"] if active else "pending",
                 row["review_attempts"] if active else 0, timestamp, token),
            )
            counts["unchanged" if active else "requested"] += 1
    return counts


def save_organize_confirmation_selection(token: str, candidate_index: int, *, chat_id: str | None = None) -> None:
    """保存人工审阅草稿，不认领、不执行；迟到的模型结果不得覆盖人工选择。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM organize_confirmations WHERE token=?", (token,)).fetchone()
        if row is None or (chat_id is not None and row["chat_id"] != chat_id):
            raise ValueError("确认操作不存在或已失效")
        if row["status"] != "pending" or row["expires_at"] <= db.now():
            raise ValueError("该确认已处理或过期，请刷新列表")
        payload = json.loads(row["payload_json"])
        if type(candidate_index) is not int or not 0 <= candidate_index < len(payload.get("candidates") or []):
            raise ValueError("候选参数无效")
        retry_index = payload.get(RETRY_SELECTED_INDEX_KEY)
        if retry_index is not None and retry_index != candidate_index:
            raise ValueError("该重试确认已绑定其他候选")
        payload["review_only"] = True
        try:
            previous_review = json.loads(row["review_result_json"] or "{}")
        except ValueError:
            previous_review = {}
        evidence = previous_review.get("evidence", []) if isinstance(previous_review, dict) else []
        conn.execute(
            "UPDATE organize_confirmations SET payload_json=?,review_status='approved',"
            "review_result_json=?,review_completed_at=?,updated_at=? WHERE token=?",
            (json.dumps(payload, ensure_ascii=False), json.dumps({"candidate_index": candidate_index,
             "source": "human", "summary": "人工选择，尚未执行", "reason_code": "human_selected", "evidence": evidence}, ensure_ascii=False),
             db.now(), db.now(), token),
        )


def activate_organize_confirmation_review(token: str) -> bool:
    """人工候选卡已可靠入队后，才允许后台 Agent 领取复核。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status='pending',updated_at=? "
            "WHERE token=? AND status='pending' AND review_status='waiting' "
            "AND expires_at>?",
            (timestamp, (token or ""), timestamp),
        )
        return cursor.rowcount == 1


def recover_interrupted_organize_confirmation_reviews() -> int:
    """进程重启后恢复未决复核；连续中断三次后永久回退人工。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        failed = conn.execute(
            "UPDATE organize_confirmations SET review_status='failed',"
            "review_result_json=?,review_completed_at=?,updated_at=? "
            "WHERE review_status='running' AND status='pending' "
            "AND review_attempts>=3",
            (
                json.dumps(
                    {
                        "reason_code": "repeated_interruption",
                        "summary": "Agent 复核连续中断，已保留人工确认",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timestamp,
                timestamp,
            ),
        )
        recovered = conn.execute(
            "UPDATE organize_confirmations SET review_status='pending',"
            "review_started_at=NULL,updated_at=? "
            "WHERE review_status='running' AND status='pending' "
            "AND review_attempts<3",
            (timestamp,),
        )
        return failed.rowcount + recovered.rowcount


def claim_next_organize_confirmation_review() -> sqlite3.Row | None:
    """原子领取最早的待确认识别复核；只读模型调用不占用整理写队列。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM organize_confirmations "
            "WHERE review_status='pending' AND status='pending' "
            "AND review_attempts<3 AND expires_at>? "
            "ORDER BY id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status='running',"
            "review_attempts=review_attempts+1,review_started_at=?,"
            "review_completed_at=NULL,updated_at=? "
            "WHERE id=? AND review_status='pending' AND status='pending'",
            (timestamp, timestamp, int(row["id"])),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE id=?",
            (int(row["id"]),),
        ).fetchone()


def stage_organize_confirmation_review_result(
    token: str, result: dict | None = None
) -> bool:
    """在 Agent 竞争执行所有权前保存最小审计，不改变确认状态。"""
    encoded = json.dumps(
        result or {}, ensure_ascii=False, separators=(",", ":"), default=str
    )
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_result_json=?,updated_at=? "
            "WHERE token=? AND review_status='running' AND status='pending'",
            (encoded, db.now(), (token or "")),
        )
        return cursor.rowcount == 1


def complete_organize_confirmation_review(
    token: str,
    *,
    status: str,
    result: dict | None = None,
) -> bool:
    """保存最小复核回执；模型上下文与工具原始载荷不会持久化。"""
    normalized_status = (status or "").strip().lower()
    if normalized_status not in {
        "approved", "abstained", "failed", "cancelled"
    }:
        raise ValueError("Agent 复核状态无效")
    timestamp = db.now()
    encoded = json.dumps(
        result or {}, ensure_ascii=False, separators=(",", ":"), default=str
    )
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status=?,review_result_json=?,"
            "review_completed_at=?,updated_at=? "
            "WHERE token=? AND review_status='running'",
            (
                normalized_status,
                encoded,
                timestamp,
                timestamp,
                (token or ""),
            ),
        )
        return cursor.rowcount == 1


def list_organize_confirmations_for_task(
    organize_task_id: str, *, chat_id: str = ""
) -> list[sqlite3.Row]:
    """返回同一整理汇总关联的全部确认记录。"""
    task_id = (organize_task_id or "").strip()
    if not task_id:
        return []
    params: list[object] = [task_id]
    chat_clause = ""
    if (chat_id or "").strip():
        chat_clause = " AND chat_id=?"
        params.append((chat_id or "").strip())
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE organize_task_id=?"
            + chat_clause
            + " ORDER BY id ASC",
            params,
        ).fetchall()


def list_unapplied_organize_confirmation_tasks(limit: int = 50) -> list[sqlite3.Row]:
    """列出有终态但尚未回写原汇总的父任务，供启动恢复与定时收口。"""
    resolved_limit = max(1, min((limit or 50), 500))
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT organize_task_id,chat_id,MIN(id) AS first_id "
            "FROM organize_confirmations WHERE organize_task_id<>'' "
            "AND rollup_applied=0 AND status IN ('completed','failed','expired','cancelled') "
            "AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.organize_rollup')='object' ELSE 0 END "
            "GROUP BY organize_task_id,chat_id ORDER BY first_id ASC LIMIT ?",
            (resolved_limit,),
        ).fetchall()


def mark_organize_confirmation_rollup_applied(
    organize_task_id: str, *, chat_id: str = ""
) -> int:
    """标记父任务当前全部确认记录已经投影到整理汇总。"""
    task_id = (organize_task_id or "").strip()
    if not task_id:
        return 0
    params: list[object] = [db.now(), task_id]
    chat_clause = ""
    if (chat_id or "").strip():
        chat_clause = " AND chat_id=?"
        params.append((chat_id or "").strip())
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET rollup_applied=1,updated_at=? "
            "WHERE organize_task_id=?" + chat_clause,
            params,
        )
        return cursor.rowcount


def bind_organize_confirmation_message(
    token: str, *, chat_id: str, message_id: int | str
) -> None:
    """把确认卡位置写入已有 payload，供后台任务可靠更新同一条消息。"""
    try:
        resolved_message_id = int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("确认消息参数无效") from exc
    if resolved_message_id <= 0:
        raise ValueError("确认消息参数无效")
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id,payload_json FROM organize_confirmations WHERE token=?",
            ((token or ""),),
        ).fetchone()
        if row is None:
            raise ValueError("确认操作不存在或已失效")
        expected_chat = str(row["chat_id"] or "")
        if expected_chat and expected_chat != (chat_id or ""):
            raise ValueError("确认操作不存在或已失效")
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError) as exc:
            raise ValueError("确认任务数据损坏，请重新执行整理") from exc
        if not isinstance(payload, dict):
            raise ValueError("确认任务数据损坏，请重新执行整理")
        payload["_telegram_message_id"] = resolved_message_id
        conn.execute(
            "UPDATE organize_confirmations SET payload_json=?,updated_at=? WHERE token=?",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                db.now(),
                (token or ""),
            ),
        )


def _pending_confirmation_row(conn: sqlite3.Connection, token: str, chat_id: str) -> sqlite3.Row:
    """读取仍待处理且属于该会话的确认单；不存在、越权与已处理统一拒绝。"""
    row = conn.execute(
        "SELECT * FROM organize_confirmations WHERE token=?",
        ((token or ""),),
    ).fetchone()
    if row is None:
        raise ValueError("确认操作不存在或已失效")
    expected_chat = str(row["chat_id"] or "")
    if expected_chat and expected_chat != (chat_id or ""):
        raise ValueError("确认操作不存在或已失效")
    if str(row["status"] or "") != "pending":
        raise ValueError("该确认操作已处理")
    return row


def claim_organize_confirmation(
    token: str, *, chat_id: str, selected_index: int, actor: str = "human",
    event_json: str = "", message_id: int | None = None, explicit_approval: bool = False,
    expected_snapshot: tuple[str, str] | None = None,
) -> sqlite3.Row:
    """原子保存确认选择与接纳回执；过期、越权和重放统一拒绝。"""
    normalized_actor = (actor or "human").strip().lower()
    if normalized_actor not in {"human", "agent"}:
        raise ValueError("确认执行者无效")
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 入队顺序必须反映按钮实际点击顺序。全局 db.now() 只精确到秒，
        # 连续点击会退化成记录创建顺序，因此仅为队列时间保留微秒。
        queued_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        row = _pending_confirmation_row(conn, token, chat_id)
        if expected_snapshot is not None and (row["payload_json"], row["review_result_json"]) != expected_snapshot:
            raise ValueError("确认材料已变化，请重新预览")
        expired = str(row["expires_at"] or "") <= timestamp
        if expired:
            conn.execute(
                "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
                (
                    json.dumps({"resolution": "expired"}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            claimed = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='queued',selected_index=?,"
                "payload_json=CASE WHEN ? THEN json_set(payload_json,'$._explicit_approval',json('true')) ELSE payload_json END,"
                "queued_at=?,confirmation_actor=?,task_id=?,error='',"
                "review_status=CASE "
                "WHEN ?='agent' THEN 'approved' "
                "WHEN ?='human' AND review_status IN ('waiting','pending','running') THEN 'cancelled' "
                "ELSE review_status END,"
                "review_completed_at=CASE "
                "WHEN ?='agent' THEN ? "
                "WHEN ?='human' AND review_status IN ('waiting','pending','running') THEN ? "
                "ELSE review_completed_at END,updated_at=? "
                "WHERE id=? AND status='pending' "
                "AND (?<>'agent' OR ? OR (review_status='running' "
                "AND COALESCE(json_extract(payload_json,'$.review_only'),0)=0))",
                (
                    selected_index, explicit_approval, queued_at, normalized_actor, f"queue-{int(row['id']):06d}",
                    normalized_actor, normalized_actor,
                    normalized_actor, timestamp,
                    normalized_actor, timestamp,
                    timestamp, int(row["id"]), normalized_actor, explicit_approval,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("该确认操作已处理")
            if event_json:
                telegram_notifications._enqueue_confirmation_delivery(
                    conn,
                    token=token,
                    event_json=event_json,
                    chat_id=chat_id,
                    message_id=message_id,
                    timestamp=timestamp,
                )
            claimed = conn.execute(
                "SELECT * FROM organize_confirmations WHERE id=?", (int(row["id"]),)
            ).fetchone()
    if expired:
        raise ValueError("确认操作已过期，请重新执行整理")
    assert claimed is not None
    return claimed


def cancel_organize_confirmation(
    token: str,
    *,
    chat_id: str,
    event_json: str = "",
    message_id: int | None = None,
    enqueue_delivery: bool = True,
    resolution: str = "cancelled",
) -> sqlite3.Row:
    """原子取消待确认任务，并可在同一事务写入终态回执。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _pending_confirmation_row(conn, token, chat_id)
        if str(row["expires_at"] or "") <= timestamp:
            conn.execute(
                "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
                (
                    json.dumps({"resolution": "expired"}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            cancelled = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='cancelled',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (
                    json.dumps(
                        {
                            "cancelled": True,
                            "resolution": (resolution or "cancelled"),
                        },
                        ensure_ascii=False,
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("该确认操作已处理")
            cancelled = conn.execute(
                "SELECT * FROM organize_confirmations WHERE id=?", (int(row["id"]),)
            ).fetchone()
            if enqueue_delivery and (event_json or ""):
                telegram_notifications._enqueue_confirmation_delivery(
                    conn,
                    token=token,
                    event_json=event_json,
                    chat_id=str(row["chat_id"] or "") or (chat_id or ""),
                    message_id=message_id,
                    timestamp=timestamp,
                )
    if cancelled is None:
        raise ValueError("确认操作已过期，请重新执行整理")
    return cancelled


def list_due_pending_organize_confirmations(
    *, token: str = "", limit: int = 100
) -> list[sqlite3.Row]:
    """列出到期但尚未选择的候选；终态与回执由单条原子接口提交。"""
    timestamp = db.now()
    resolved_limit = max(1, min((limit or 100), 500))
    with db.get_conn() as conn:
        params: list[object] = [timestamp]
        token_clause = ""
        if (token or "").strip():
            token_clause = " AND token=?"
            params.append((token or "").strip())
        params.append(resolved_limit)
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE status='pending' "
            "AND expires_at<=?"
            + token_clause
            + " ORDER BY expires_at ASC,id ASC LIMIT ?",
            params,
        ).fetchall()


def expire_organize_confirmation_with_delivery(
    token: str,
    *,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    enqueue_delivery: bool = True,
) -> sqlite3.Row | None:
    """原子失效一张到期候选卡，并写入统一通知中心的事务桥。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
            "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
            "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN 'cancelled' ELSE review_status END,"
            "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN ? ELSE review_completed_at END,"
            "completed_at=?,rollup_applied=0,updated_at=? "
            "WHERE token=? AND status='pending' AND expires_at<=?",
            (
                json.dumps({"resolution": "expired"}, ensure_ascii=False),
                timestamp,
                timestamp,
                timestamp,
                (token or ""),
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            return None
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            ((token or ""),),
        ).fetchone()


def get_next_queued_organize_confirmation() -> sqlite3.Row | None:
    """返回最早的已确认 Telegram 整理任务；入队后不再按候选 TTL 失效。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE status='queued' "
            "ORDER BY COALESCE(queued_at,created_at) ASC,id ASC LIMIT 1"
        ).fetchone()


def get_organize_confirmation_queue_position(row_id: int) -> int:
    """按用户点击入队时间返回前方任务数量，并计入当前运行项。"""
    with db.get_conn() as conn:
        target = conn.execute(
            "SELECT id,COALESCE(queued_at,created_at) AS queue_time "
            "FROM organize_confirmations WHERE id=?",
            (row_id,),
        ).fetchone()
        if target is None:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_confirmations "
            "WHERE id<>? AND (status='running' OR (status='queued' AND ("
            "COALESCE(queued_at,created_at)<? OR "
            "(COALESCE(queued_at,created_at)=? AND id<?))))",
            (
                row_id,
                str(target["queue_time"] or ""),
                str(target["queue_time"] or ""),
                row_id,
            ),
        ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def claim_queued_organize_confirmation(token: str) -> sqlite3.Row | None:
    """原子领取一个已排队确认；已选择任务不再因等待跨过 TTL 而失效。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='running',error='',updated_at=? "
            "WHERE token=? AND status='queued'",
            (timestamp, (token or "")),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            ((token or ""),),
        ).fetchone()


def requeue_organize_confirmation(token: str, error: str = "") -> None:
    """统一整理锁繁忙时保留用户选择并放回 FIFO 队列。"""
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE organize_confirmations SET status='queued',error=?,updated_at=? "
            "WHERE token=? AND status='running'",
            ((error or ""), db.now(), (token or "")),
        )


def update_organize_confirmation(token: str, **fields) -> None:
    allowed = {
        "status", "task_id", "result_json", "error", "completed_at",
        "review_status", "review_result_json", "review_started_at",
        "review_completed_at", "confirmation_actor",
    }
    sets: list[str] = []
    values: list[object] = []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([db.now(), (token or "")])
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE organize_confirmations SET {','.join(sets)} WHERE token=?",
            values,
        )


def complete_organize_confirmation_with_delivery(
    token: str,
    *,
    result_json: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    enqueue_delivery: bool = True,
) -> None:
    """原子保存成功终态、下载收尾意图与可选回执。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='completed',result_json=?,error='',"
            "completed_at=?,rollup_applied=0,updated_at=? "
            "WHERE token=? AND status='running'",
            ((result_json or "{}"), timestamp, timestamp, (token or "")),
        )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        # 收尾意图与确认终态同事务落盘；即使回执静默或随后进程中断，
        # 下载隔离目录也能由后台安全恢复，而不是永久留下旧待处理状态。
        from app.repositories.download_staging_reconcile import (
            enqueue_confirmation_cleanup,
        )
        from app.repositories.organize_history import settle_completed_confirmation_logs

        settle_completed_confirmation_logs(conn, token=token)
        enqueue_confirmation_cleanup(conn, token=token, timestamp=timestamp)
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )


def fail_organize_confirmation_with_delivery(
    token: str,
    *,
    error: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    retryable: bool,
    enqueue_delivery: bool = True,
    retry_expires_at: str = "",
) -> None:
    """原子保存失败/可重试状态与待投递回执。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if retryable:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='pending',selected_index=NULL,"
                "queued_at=NULL,task_id='',error=?,completed_at=NULL,rollup_applied=0,"
                "expires_at=CASE WHEN ?<>'' THEN ? ELSE expires_at END,updated_at=? "
                "WHERE token=? AND status IN ('queued','running')",
                (
                    (error or ""),
                    (retry_expires_at or ""),
                    (retry_expires_at or ""),
                    timestamp,
                    (token or ""),
                ),
            )
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='failed',error=?,completed_at=?,"
                "rollup_applied=0,updated_at=? "
                "WHERE token=? AND status IN ('queued','running')",
                ((error or ""), timestamp, timestamp, (token or "")),
            )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )


def claim_due_organize_confirmation_delivery(
    *,
    current_time: str,
    stale_before: str,
    token: str = "",
) -> sqlite3.Row | None:
    """原子领取一条到期回执；租约代数防止迟到 worker 覆盖新结果。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        params: list[object] = [current_time, stale_before]
        token_clause = ""
        if (token or ""):
            token_clause = " AND confirmation_token=?"
            params.append(token)
        row = conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE (("
            "status IN ('pending','retry_wait') AND next_attempt_at<=?) OR "
            "(status='sending' AND updated_at<=?))"
            + token_clause
            + " ORDER BY next_attempt_at ASC,id ASC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='sending',"
            "lease_generation=lease_generation+1,updated_at=? WHERE id=? AND "
            "lease_generation=? AND status=?",
            (
                current_time,
                int(row["id"]),
                int(row["lease_generation"]),
                str(row["status"]),
            ),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE id=?",
            (int(row["id"]),),
        ).fetchone()


def complete_organize_confirmation_delivery(
    delivery_id: int, *, expected_lease_generation: int, sent_at: str
) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='sent',sent_at=?,"
            "last_error='',updated_at=? WHERE id=? AND status='sending' AND lease_generation=?",
            (
                sent_at,
                sent_at,
                delivery_id,
                expected_lease_generation,
            ),
        )
        return cursor.rowcount == 1


def retry_organize_confirmation_delivery(
    delivery_id: int,
    *,
    expected_lease_generation: int,
    next_attempt_at: str,
    error: str,
) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='retry_wait',"
            "attempts=attempts+1,next_attempt_at=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                next_attempt_at,
                (error or "DeliveryFailed")[:500],
                db.now(),
                delivery_id,
                expected_lease_generation,
            ),
        )
        return cursor.rowcount == 1


def get_organize_confirmation_delivery(token: str) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE confirmation_token=?",
            ((token or ""),),
        ).fetchone()
