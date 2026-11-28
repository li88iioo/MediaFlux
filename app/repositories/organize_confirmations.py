"""整理候选确认、只读复核与 Telegram 确认投递的持久化。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import datetime, timedelta

from app import database as db
from app.repositories import telegram_notifications

# 重试确认绑定原候选的 payload 键；仓储与确认服务共用。
RETRY_SELECTED_INDEX_KEY = "_retry_selected_index"
MANUAL_SEASON_CORRECTION_KEY = "_manual_season_correction"
MAX_REVIEW_FILES = 80
_MAX_CORRECTION_LOGS = 50

# review_status / review_result_json 是每组的“建议槽”，Agent 复核与人工审阅共用：
# - Agent：waiting → pending → running → approved / abstained / failed；
# - 人工保存选择直接写 approved，review_result_json.source='human'，不认领、不执行；
# - 模型结果只在对应领取仍为 running 时写回，迟到结果不能覆盖新领取或人工选择。
# 真正执行的候选只看 selected_index 与 status，建议槽不代表已确认。


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
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        timestamp = db.now()
        # 日志上界随重试继承。原地纠正沿用日志 ID，每张新卡单独记录操作步骤上界。
        payload = dict(payload)
        if "_organize_log_upper_bound" not in payload:
            payload["_organize_log_upper_bound"] = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM organize_log"
            ).fetchone()[0]
        payload["_organize_step_upper_bound"] = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM organize_operation_steps"
        ).fetchone()[0]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        _require_no_running_correction(conn, encoded)
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


def _log_confirmation_owners(
    conn: sqlite3.Connection, log_ids: list[int], *, minimum_confirmation_id: int = 0,
) -> dict[int, sqlite3.Row]:
    if not log_ids:
        return {}
    rows = conn.execute(
        """
        WITH logs AS (
            SELECT id,file_id,updated_at FROM organize_log
            WHERE id IN (SELECT value FROM json_each(?))
              AND source='guangya' AND status='manual' AND file_id<>''
        ), latest AS (
            SELECT CAST(json_extract(CASE WHEN f.type='object' THEN f.value ELSE '{}' END,'$.file_id') AS TEXT)
                   AS file_id,MAX(c.id) AS confirmation_id
            FROM organize_confirmations c
            JOIN json_each(CASE WHEN json_valid(c.payload_json)
                               THEN c.payload_json ELSE '{}' END,'$.files') f
            WHERE c.id>=?
              AND COALESCE(json_extract(CASE WHEN json_valid(c.payload_json)
                                       THEN c.payload_json ELSE '{}' END,'$.kind'),'guangya')='guangya'
              AND CAST(json_extract(CASE WHEN f.type='object' THEN f.value ELSE '{}' END,'$.file_id') AS TEXT)
                  IN (SELECT file_id FROM logs)
            GROUP BY 1
        )
        SELECT l.id AS log_id,c.token,c.status,c.expires_at,
               CASE WHEN json_valid(c.payload_json) THEN json_type(c.payload_json,?) IS NOT NULL ELSE 0 END
                   AS manual_season_correction,
               NOT EXISTS (
                   SELECT 1 FROM organize_log newer
                   WHERE newer.source='guangya' AND newer.file_id=l.file_id AND newer.id>l.id
               ) AS latest_log
        FROM logs l
        JOIN latest ON latest.file_id=l.file_id
        JOIN organize_confirmations c ON c.id=latest.confirmation_id
        WHERE CASE WHEN json_valid(c.payload_json) THEN
              ((json_type(c.payload_json,'$._organize_log_upper_bound')='integer'
                AND l.id<=json_extract(c.payload_json,'$._organize_log_upper_bound') AND l.updated_at<=c.created_at)
               OR (json_type(c.payload_json,'$._organize_log_upper_bound') IS NULL AND l.updated_at<c.created_at))
          ELSE 0 END
        """, (json.dumps(log_ids), minimum_confirmation_id, f"$.{MANUAL_SEASON_CORRECTION_KEY}"),
    )
    return {row["log_id"]: row for row in rows}


def _pending_log_owners(conn: sqlite3.Connection, log_ids: list[int], timestamp: str) -> dict[int, str]:
    return {log_id: row["token"] for log_id, row in _log_confirmation_owners(conn, log_ids).items()
            if row["status"] == "pending" and row["expires_at"] > timestamp}


def pending_confirmation_tokens_by_log(log_ids: Iterable[int]) -> dict[int, str]:
    """按冻结日志范围定位最新确认单，禁止从已入队项回退到旧待确认单。"""
    ids = list(dict.fromkeys(log_ids))
    if not ids:
        return {}
    with db.get_conn() as conn:
        return _pending_log_owners(conn, ids, db.now())


def correction_log_confirmation(
    log_id: int, *, expected_token: str = "", conn: sqlite3.Connection | None = None,
) -> sqlite3.Row | None:
    """返回原日志在冻结范围内的最新确认及其状态。"""
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        minimum_id = 0
        if expected_token:
            expected = connection.execute(
                "SELECT id FROM organize_confirmations WHERE token=?", (expected_token,),
            ).fetchone()
            if expected is None:
                return None
            minimum_id = expected["id"]
        return _log_confirmation_owners(connection, [log_id], minimum_confirmation_id=minimum_id).get(log_id)


def _matches_pending_member(item: sqlite3.Row, snapshot: dict) -> bool:
    parent_id, name = snapshot.get("parent_id"), snapshot.get("name")
    size, etag = snapshot.get("size"), snapshot.get("etag")
    return (
        isinstance(parent_id, str) and bool(parent_id.strip())
        and isinstance(name, str) and bool(name.strip())
        and item["original_parent_id"] == item["current_parent_id"] == parent_id
        and item["original_name"] == item["current_name"] == name
        and (size is None or (type(size) is int and size == item["size"]))
        and (not etag or (isinstance(etag, str) and etag.lower() == str(item["etag"] or "").lower()))
    )


def _pending_reorganize_log_ids(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], timestamp: str,
) -> dict[str, list[int]]:
    """整组文件及伴随物都与最新的原日志快照一致时才交给纠正识别。"""
    groups: dict[str, tuple[str, list[str], dict[str, dict]]] = {}
    video_count = 0
    for row in rows:
        if row["status"] != "pending" or row["expires_at"] <= timestamp:
            continue
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        source_id = payload.get("source_dir_id")
        files, companions = payload.get("files"), payload.get("companions", [])
        if not isinstance(source_id, str) or not source_id or not isinstance(files, list) or not files:
            continue
        if not isinstance(companions, list):
            continue
        video_count += len(files)
        if video_count > _MAX_CORRECTION_LOGS:
            return {}
        snapshots = {
            item["file_id"]: item for item in (*files, *companions)
            if isinstance(item, dict) and isinstance(item.get("file_id"), str) and item["file_id"].strip()
        }
        if len(snapshots) != len(files) + len(companions):
            continue
        groups[row["token"]] = (source_id, [item["file_id"] for item in files], snapshots)
    if not groups:
        return {}
    primary_ids = list({file_id for _, files, _ in groups.values() for file_id in files})
    logs = {row["file_id"]: row for row in conn.execute(
        "SELECT l.id,l.file_id,l.source_dir_id FROM json_each(?) f JOIN organize_log l ON l.id=("
        "SELECT id FROM organize_log WHERE source='guangya' AND file_id=f.value ORDER BY id DESC LIMIT 1) "
        "WHERE l.status='manual' AND COALESCE(l.legacy_incomplete,0)=0",
        (json.dumps(primary_ids),),
    )}
    owners = _pending_log_owners(conn, [row["id"] for row in logs.values()], timestamp)
    if not owners:
        return {}
    members: dict[int, list[sqlite3.Row]] = {}
    for item in conn.execute(
        "SELECT * FROM organize_log_items WHERE log_id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(owners)),),
    ):
        members.setdefault(item["log_id"], []).append(item)
    result = {}
    for token, (source_id, file_ids, snapshots) in groups.items():
        group_logs = [logs[file_id] for file_id in file_ids if file_id in logs]
        if len(group_logs) != len(file_ids) or any(
            owners.get(log["id"]) != token or log["source_dir_id"] != source_id for log in group_logs
        ):
            continue
        items = [item for log in group_logs for item in members.get(log["id"], ())]
        if len(items) != len(snapshots) or {item["file_id"] for item in items} != snapshots.keys():
            continue
        videos = {(item["log_id"], item["file_id"]) for item in items if item["role"] == "video"}
        if videos != {(log["id"], log["file_id"]) for log in group_logs}:
            continue
        if all(_matches_pending_member(item, snapshots[item["file_id"]]) for item in items):
            result[token] = sorted(log["id"] for log in group_logs)
    return result


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
    include_correction_logs: bool = False,
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
        log_ids: dict[str, list[int]] = {}
        if (
            include_correction_logs and tokens is not None
            and len(tokens) <= _MAX_CORRECTION_LOGS and counts.get("pending")
        ):
            # 按整次选择计数，跨页同样受纠正入口的条数限制。
            video_count = conn.execute(
                "SELECT COALESCE(SUM(json_array_length(payload_json,'$.files')),0)" + where
                + " AND status='pending' AND expires_at>?", [*values, timestamp],
            ).fetchone()[0]
            if 0 < video_count <= _MAX_CORRECTION_LOGS:
                log_ids = _pending_reorganize_log_ids(conn, rows, timestamp)
        finished = json.dumps([row["token"] for row in rows if row["status"] != "pending"])
        for token, log_id in conn.execute("""
            WITH cards AS (
                SELECT token, status, CASE WHEN json_valid(result_json) THEN result_json ELSE '{}' END AS receipt
                FROM organize_confirmations WHERE token IN (SELECT value FROM json_each(?))
            )
            SELECT c.token, l.id FROM cards c JOIN organize_log l ON l.operation_token='recognition-confirm:'||c.token
            WHERE l.operation_token!=''
            UNION
            SELECT c.token, l.id FROM cards c, json_each(c.receipt, '$.organize_log_ids') i
            JOIN organize_log l ON l.id=i.value AND l.source='guangya'
            WHERE c.status='completed' AND json_extract(c.receipt,'$.resolution')='reorganized'
              AND json_type(c.receipt,'$.organize_log_ids')='array' AND i.type='integer'
            ORDER BY 2
        """, (finished,)):
            log_ids.setdefault(token, []).append(log_id)
    return {"rows": rows, "tokens": [row["token"] for row in selected],
            "total": total, "counts": counts, "log_ids": log_ids}


def can_discover_candidates(payload: dict) -> bool:
    """无候选时，只允许用户为普通光鸭媒体请求 TMDB 补搜。"""
    files = payload.get("files")
    rules = payload.get("rules", {})
    return (
        (payload.get("kind") or "guangya") == "guangya"
        and not payload.get("candidates") and RETRY_SELECTED_INDEX_KEY not in payload
        and MANUAL_SEASON_CORRECTION_KEY not in payload
        and isinstance(rules, dict) and not rules.get("nsfw_exclusive")
        and isinstance(files, list) and 0 < len(files) <= MAX_REVIEW_FILES
        and all(isinstance(item, dict) and isinstance(item.get("name"), str)
                and item["name"].strip() for item in files)
    )


def request_organize_confirmation_reviews(tokens: list[str], *, chat_id: str | None = None) -> dict:
    """只读复核显式请求；切换模式与自动写入竞争同一数据库事务。"""
    counts = {"requested": 0, "unchanged": 0, "unavailable": 0}
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        timestamp = db.now()
        for token in tokens:
            row = conn.execute("SELECT * FROM organize_confirmations WHERE token=?", (token,)).fetchone()
            if (row is None or (chat_id is not None and row["chat_id"] != chat_id)
                    or row["status"] != "pending" or row["expires_at"] <= timestamp):
                counts["unavailable"] += 1
                continue
            payload = json.loads(row["payload_json"])
            discover = not payload.get("candidates")
            if discover and not can_discover_candidates(payload):
                counts["unavailable"] += 1
                continue
            active = row["review_status"] in {"pending", "running", "approved"}
            if active and payload.get("review_only") and (not discover or payload.get("discover_candidates")):
                counts["unchanged"] += 1
                continue
            payload["review_only"] = True
            if discover:
                payload["discover_candidates"] = True
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


def activate_organize_confirmation_reviews(tokens: list[str]) -> int:
    """通知完成后只激活建卡时已授权主动复核的组。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status='pending',updated_at=? "
            "WHERE token IN (SELECT value FROM json_each(?)) AND status='pending' AND review_status='waiting' "
            "AND expires_at>?",
            (timestamp, json.dumps(tokens), timestamp),
        )
        return cursor.rowcount


def recover_interrupted_organize_confirmation_reviews() -> tuple[int, list[sqlite3.Row]]:
    """恢复未决复核，回读仍待人工的中断回执以补发原消息。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        timestamp = db.now()
        recovered = conn.execute("""
            UPDATE organize_confirmations
            SET review_status=CASE WHEN review_attempts>=3 THEN 'failed' ELSE 'pending' END,
                review_result_json=CASE WHEN review_attempts>=3 THEN :result ELSE review_result_json END,
                review_completed_at=CASE WHEN review_attempts>=3 THEN :now END, updated_at=:now
            WHERE review_status='running' AND status='pending'
        """, {"now": timestamp, "result": json.dumps({
            "reason_code": "repeated_interruption", "summary": "Agent 复核连续中断，已保留人工确认",
        }, ensure_ascii=False)}).rowcount
        failed = conn.execute("""
            SELECT * FROM organize_confirmations WHERE status='pending' AND expires_at>?
              AND review_status='failed' AND json_valid(review_result_json)
              AND json_extract(review_result_json,'$.reason_code')='repeated_interruption'
        """, (timestamp,)).fetchall()
        return recovered, failed


def claim_next_organize_confirmation_review() -> sqlite3.Row | None:
    """原子领取最早的待确认识别复核；只读模型调用不占用整理写队列。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        timestamp = db.now()
        row = conn.execute(
            "SELECT id,review_started_at FROM organize_confirmations "
            "WHERE review_status='pending' AND status='pending' "
            "AND review_attempts<3 AND expires_at>? "
            "ORDER BY id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        # 保留上次时间，重启或时钟回拨后仍能生成严格更新的领取标记。
        started = datetime.now()
        if previous := row["review_started_at"]:
            started = max(started, datetime.fromisoformat(previous) + timedelta(microseconds=1))
        return conn.execute(
            "UPDATE organize_confirmations SET review_status='running',"
            "review_attempts=review_attempts+1,review_started_at=?,"
            "review_completed_at=NULL,updated_at=? "
            "WHERE id=? RETURNING *",
            (started.isoformat(sep=" ", timespec="microseconds"), timestamp, row["id"]),
        ).fetchone()


def save_organize_confirmation_review(
    claim: sqlite3.Row,
    *,
    status: str,
    result: dict,
    discovered_candidates: list[dict] | None = None,
) -> sqlite3.Row | None:
    """仅当前领取可暂存审计或保存终态；补搜候选与建议同时写入。"""
    if status not in {"running", "approved", "abstained", "failed", "cancelled"}:
        raise ValueError("Agent 复核状态无效")
    candidate_update = ""
    discovery_guard = ""
    bindings = {
        "status": status, "result": json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
        "token": claim["token"], "started_at": claim["review_started_at"],
    }
    if discovered_candidates:
        # 补搜结果与建议同时可见，永远不能覆盖已有候选或复活已处理/过期的卡。
        index = result.get("candidate_index")
        if status != "approved" or type(index) is not int or not 0 <= index < len(discovered_candidates):
            raise ValueError("补搜作品与复核建议不一致")
        candidate_update = ",payload_json=json_set(payload_json,'$.candidates',json(:candidates))"
        bindings["candidates"] = json.dumps(discovered_candidates, ensure_ascii=False)
        discovery_guard = (
            " AND json_extract(payload_json,'$.review_only')=1"
            " AND json_extract(payload_json,'$.discover_candidates')=1"
            " AND COALESCE(json_array_length(payload_json,'$.candidates'),0)=0"
        )
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        bindings["now"] = db.now()
        return conn.execute(
            "UPDATE organize_confirmations SET review_status=:status,review_result_json=:result,"
            "review_completed_at=CASE WHEN :status!='running' THEN :now END,"
            f"updated_at=:now{candidate_update} WHERE token=:token AND status='pending' AND expires_at>:now "
            f"AND review_status='running' AND review_started_at=:started_at{discovery_guard} RETURNING *", bindings,
        ).fetchone()


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
    rows: Iterable[sqlite3.Row],
) -> int:
    """只标记已读到的终态，新回执留给下一次收口。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        cursor = conn.executemany(
            "UPDATE organize_confirmations SET rollup_applied=1,updated_at=? "
            "WHERE id=? AND status=? AND rollup_applied=0",
            ((timestamp, row["id"], row["status"]) for row in rows),
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


def _require_no_running_correction(conn: sqlite3.Connection, payload_json: str) -> None:
    payload = "CASE WHEN json_valid(?) THEN ? ELSE '{}' END"
    row = conn.execute(
        f"SELECT 1 FROM json_each({payload},'$.files') f "
        "JOIN organize_log l ON l.file_id=json_extract(CASE WHEN f.type='object' THEN f.value ELSE '{}' END,'$.file_id') "
        "WHERE l.source='guangya' AND l.status='reorganizing' "
        f"AND COALESCE(json_extract({payload},'$.kind'),'guangya')='guangya' LIMIT 1",
        (payload_json, payload_json, payload_json, payload_json),
    ).fetchone()
    if row is not None:
        raise ValueError("本组日志正在纠正，请等待操作结束后刷新")


def claim_organize_confirmation(
    token: str, *, chat_id: str, selected_index: int, actor: str = "human",
    event_json: str = "", message_id: int | None = None, explicit_approval: bool = False,
    expected_snapshot: tuple[str, str] | None = None, expected_review_started_at: str | None = None,
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
            _require_no_running_correction(conn, row["payload_json"])
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
                "AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,?) IS NULL ELSE 1 END "
                "AND (?<>'agent' OR ? OR (review_status='running' AND review_started_at=? "
                "AND COALESCE(json_extract(payload_json,'$.review_only'),0)=0))",
                (
                    selected_index, explicit_approval, queued_at, normalized_actor, f"queue-{int(row['id']):06d}",
                    normalized_actor, normalized_actor,
                    normalized_actor, timestamp,
                    normalized_actor, timestamp,
                    timestamp, int(row["id"]), f"$.{MANUAL_SEASON_CORRECTION_KEY}",
                    normalized_actor, explicit_approval, expected_review_started_at,
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
            _require_no_running_correction(conn, row["payload_json"])
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
    enqueue_delivery: bool = True,
    season_correction_expires_at: str = "",
) -> None:
    """原子保存失败状态或回到待人工选季，并登记同一条回执。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if season_correction_expires_at:
            # 写前预演停在未知季号时，保留作品身份和原日志边界，撤销所有候选执行与复核入口。
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='pending',selected_index=NULL,"
                "payload_json=json_remove(json_set(payload_json,?,"
                "json_extract(payload_json,'$.candidates[' || selected_index || ']'),"
                "'$.candidates',json('[]')),'$._explicit_approval'),"
                "review_status='',review_result_json='',review_started_at=NULL,review_completed_at=NULL,"
                "queued_at=NULL,task_id='',result_json='',error=?,completed_at=NULL,rollup_applied=0,"
                "expires_at=?,updated_at=? "
                "WHERE token=? AND status='running'",
                (
                    f"$.{MANUAL_SEASON_CORRECTION_KEY}",
                    (error or ""),
                    season_correction_expires_at,
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


# ===== 调用方事务内的确认结算（下载重整、手动整理、重启恢复）=====
def expire_relocated_confirmation_conn(conn: sqlite3.Connection, confirmation_id: int, *, timestamp: str) -> None:
    """源目录已被重新整理接管时，结束仍待确认的旧候选。"""
    conn.execute(
        "UPDATE organize_confirmations SET status='expired',selected_index=NULL,queued_at=NULL,task_id='',result_json=?,error=?,"
        "review_status='cancelled',review_completed_at=?,completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
        (json.dumps({'resolution': 'source_relocated'}), '目录已移动，原确认已由重新整理接管',
         timestamp, timestamp, timestamp, confirmation_id),
    )


def complete_reorganized_confirmation_conn(
    conn: sqlite3.Connection, confirmation_id: int, *, expected_status: str, stats: dict, timestamp: str,
) -> None:
    """人工整理已覆盖同一组文件时，按完整审计结果把确认收口为已完成。"""
    conn.execute(
        "UPDATE organize_confirmations SET status='completed',result_json=?,error='',"
        "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
        "THEN 'cancelled' ELSE review_status END,"
        "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
        "THEN ? ELSE review_completed_at END,"
        "completed_at=?,updated_at=?,rollup_applied=0 WHERE id=? AND status=?",
        (json.dumps(stats, ensure_ascii=False), timestamp, timestamp, timestamp,
         confirmation_id, expected_status),
    )


def fail_interrupted_running_confirmations_conn(conn: sqlite3.Connection, *, timestamp: str) -> None:
    """启动恢复：上次进程执行中的确认一律标记失败，等待重新整理。"""
    conn.execute(
        "UPDATE organize_confirmations SET status='failed',"
        "error=CASE WHEN COALESCE(error,'')='' "
        "THEN '上次进程在 Telegram 确认整理执行期间中断，请重新执行整理' "
        "ELSE error END,completed_at=COALESCE(completed_at,?),"
        "rollup_applied=0,updated_at=? WHERE status='running'",
        (timestamp, timestamp),
    )


def set_confirmation_downstream(
    token: str, downstream: dict, *, strm_run_id: int | None = None,
) -> bool:
    """原子保存回执并拒绝较旧STRM run；无run的旧调用保持原行为。"""
    stored = "CASE WHEN json_valid(result_json) THEN result_json ELSE '{}' END"
    result = f"json_set({stored},'$._downstream',json(?))"
    values: list = [json.dumps(downstream, ensure_ascii=False)]
    guard = ""
    if strm_run_id is not None:
        result = f"json_set({result},'$._downstream_run_id',?)"
        values.append(strm_run_id)
        guard = f" AND COALESCE(CAST(json_extract({stored},'$._downstream_run_id') AS INTEGER),0)<=?"
    values.extend((db.now(), token))
    if strm_run_id is not None:
        values.append(strm_run_id)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE organize_confirmations SET result_json={result},updated_at=? WHERE token=?{guard}",
            values,
        )
        return cursor.rowcount > 0


def set_confirmation_strm_handoff(
    token: str, handoff: dict, downstream: dict, *, warnings: list | None = None,
) -> None:
    """只补交接字段，不覆盖已异步到达的 STRM/刷新终态或整理执行事实。"""
    result = (
        "json_insert(json_set(CASE WHEN json_valid(result_json) THEN result_json ELSE '{}' END,"
        "'$.strm',json(?)),'$._downstream',json(?))"
    )
    values: list = [json.dumps(handoff, ensure_ascii=False), json.dumps(downstream, ensure_ascii=False)]
    if warnings is not None:
        result = f"json_set({result},'$.warnings',json(?))"
        values.append(json.dumps(warnings, ensure_ascii=False))
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE organize_confirmations SET result_json={result},updated_at=? WHERE token=? AND status='completed'",
            (*values, db.now(), token),
        )
