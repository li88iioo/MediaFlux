"""整理操作历史与业务前像，单一事务恢复身份、位置和成员目标快照。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.logger import get_logger

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
    return {"version": 1, "log": {key: log[key] for key in _BUSINESS_LOG_FIELDS}, "items": clean_items}


def capture_organize_business_snapshot(log_id: int) -> dict[str, Any]:
    """同一 SQLite 读快照中取得前像；不要把临时 busy 状态当成待恢复身份。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        log = conn.execute(
            f"SELECT {','.join(_BUSINESS_LOG_FIELDS)} FROM organize_log WHERE id=?", (log_id,),
        ).fetchone()
        if log is None:
            raise LookupError("整理日志不存在")
        items = conn.execute(
            "SELECT id,file_id,target_parent_id,target_name FROM organize_log_items "
            "WHERE log_id=? ORDER BY id", (log_id,),
        ).fetchall()
        return {"version": 1, "log": dict(log), "items": [dict(row) for row in items]}


def restore_organize_business_snapshot(log_id: int, snapshot: dict, **log_fields) -> bool:
    """文件补偿全部成功后原子恢复业务前像；任何成员失配时整笔拒绝。"""
    state = _validated_snapshot(snapshot)
    if set(log_fields) - _FINISH_FIELDS:
        raise ValueError("不允许用收尾字段覆盖业务前像")
    database = db
    with database.get_conn() as conn:
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
        timestamp = database.now()
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


# 在函数定义后绑定门面，兼容 repository-first 导入；运行期始终使用同一连接/时钟所有者。
from app import database as db  # noqa: E402
from app.repositories import telegram_notifications  # noqa: E402
