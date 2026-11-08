"""Agent 下载复核与通知发件箱的数据访问。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from app.modules.media_identity import normalize_episode_verification
from app.modules.web_secret import get_web_secret


def _target_identity(target: dict) -> tuple:
    return tuple(target[key] for key in ("tmdb_id", "season", "episode", "library_name"))


def _job(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    value["targets"] = json.loads(value.pop("targets_json"))
    return value


def enqueue_agent_download_verification(
    request_id: int, *, targets: list[dict], owner: str = "", chat_id: str = "",
    conn: sqlite3.Connection | None = None,
) -> str:
    """在唯一 request job 合并服务端复核意图；有 conn 时不另开事务或提交。"""
    if not isinstance(targets, list) or not targets:
        raise ValueError("复核目标必须是非空列表")
    if not isinstance(owner, str) or len(owner) > 512 or not isinstance(chat_id, str) or len(chat_id) > 32:
        raise ValueError("复核 owner/chat_id 无效")
    incoming: dict[tuple, dict] = {}
    for target in targets:
        normalized = normalize_episode_verification(target)
        if normalized is None:
            raise ValueError("复核目标无效")
        incoming.setdefault(_target_identity(normalized), {**normalized, "result": ""})
    timestamp = db.now()
    with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
        if conn is None:
            connection.execute("BEGIN IMMEDIATE")
        request = connection.execute("SELECT 1 FROM download_requests WHERE id=?", (request_id,)).fetchone()
        if request is None:
            return "not_eligible"
        existing = _job(connection.execute("SELECT * FROM agent_download_verifications WHERE request_id=?", (request_id,)).fetchone())
        if existing is not None:
            if existing["owner"] != owner or (chat_id and chat_id != existing["chat_id"]):
                return "conflict"
            identities = {_target_identity(target) for target in existing["targets"]}
            added = [target for key, target in incoming.items() if key not in identities]
            if not added:
                return "existing"
            merged = [{**target, "result": ""} for target in existing["targets"] + added]
            connection.execute(
                "UPDATE agent_download_verifications SET targets_json=?,status='pending',result='',attempts=0,"
                "lease_generation=lease_generation+1,next_check_at=?,last_checked_at=NULL,updated_at=? WHERE request_id=?",
                (json.dumps(merged, ensure_ascii=False, separators=(",", ":")), timestamp, timestamp, request_id),
            )
            # 一条 outbox 随同一 job 更新；旧发送者不能完成新目标的通知。
            connection.execute(
                "UPDATE agent_download_verification_notification_outbox SET status='discarded',payload_json='',"
                "lease_generation=lease_generation+1,last_error_type='VerificationTargetsChanged',updated_at=? WHERE request_id=?",
                (timestamp, request_id),
            )
        else:
            connection.execute(
                "INSERT INTO agent_download_verifications(request_id,targets_json,owner,chat_id,next_check_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (request_id, json.dumps(list(incoming.values()), ensure_ascii=False, separators=(",", ":")), owner, chat_id,
                 timestamp, timestamp, timestamp),
            )
        return "queued"


def get_agent_download_verification(request_id: int) -> dict | None:
    with db.get_conn() as conn:
        return _job(conn.execute("SELECT * FROM agent_download_verifications WHERE request_id=?", (request_id,)).fetchone())


def claim_due_agent_download_verification(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> dict | None:
    """原子领取一个到期复核任务，避免并发 worker 重复审计。"""
    timestamp = (current_time or db.now())
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_download_verifications SET status='retry_wait',"
                "next_check_at=?,updated_at=? "
                "WHERE status='running' AND updated_at<=?",
                (timestamp, timestamp, stale_before),
            )
        row = conn.execute(
            "SELECT request_id FROM agent_download_verifications "
            "WHERE status IN ('pending','retry_wait') AND next_check_at<=? "
            "ORDER BY next_check_at ASC,request_id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        request_id = int(row["request_id"])
        cur = conn.execute(
            "UPDATE agent_download_verifications SET status='running',"
            "lease_generation=lease_generation+1,updated_at=? "
            "WHERE request_id=? AND status IN ('pending','retry_wait') AND next_check_at<=?",
            (timestamp, request_id, timestamp),
        )
        if cur.rowcount != 1:
            return None
        return _job(conn.execute(
            "SELECT * FROM agent_download_verifications WHERE request_id=?", (request_id,),
        ).fetchone())


def renew_agent_download_verification_lease(
    request_id: int,
    *,
    expected_lease_generation: int,
    renewed_at: str | None = None,
) -> bool:
    """续期正在运行的复核任务；租约变化后旧 worker 不得继续持有。"""
    timestamp = (renewed_at or db.now())
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verifications SET updated_at=? "
            "WHERE request_id=? AND status='running' AND lease_generation=?",
            (
                timestamp,
                request_id,
                max(0, expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def _update_verification(
    conn: sqlite3.Connection, request_id: int, *, status: str, result: str, attempts: int,
    next_check_at: str, expected_lease_generation: int, last_checked_at: str | None,
    targets: list[dict] | None,
) -> bool:
    if status not in {"pending", "running", "retry_wait", "visible", "attention"}:
        raise ValueError("自动复核状态无效")
    if result not in {"", "visible", "missing", "inconclusive"}:
        raise ValueError("自动复核结果无效")
    job = _job(conn.execute(
        "SELECT * FROM agent_download_verifications WHERE request_id=? AND lease_generation=? AND status='running'",
        (request_id, expected_lease_generation),
    ).fetchone())
    if job is None:
        return False
    current = job["targets"]
    if targets is not None:
        outcomes = {_target_identity(target): target.get("result", "") for target in targets}
        if len(targets) != len(current) or set(outcomes) != {_target_identity(target) for target in current}:
            return False
        if any(value not in {"", "visible", "missing", "inconclusive"} for value in outcomes.values()):
            raise ValueError("目标复核结果无效")
        current = [{**target, "result": outcomes[_target_identity(target)]} for target in current]
    if (status == "visible" or result == "visible") and (
        not current or any(target.get("result") != "visible" for target in current)
    ):
        return False
    if status in {"visible", "attention"}:
        current = [{**target, "title": ""} for target in current]
    cur = conn.execute(
        "UPDATE agent_download_verifications SET status=?,targets_json=?,result=?,attempts=?,"
        "next_check_at=?,last_checked_at=?,updated_at=? WHERE request_id=? AND lease_generation=? AND status='running'",
        (status, json.dumps(current, ensure_ascii=False, separators=(",", ":")), result,
         max(0, min(attempts, 100)), next_check_at or db.now(), last_checked_at, db.now(), request_id,
         expected_lease_generation),
    )
    return cur.rowcount == 1


def update_agent_download_verification(
    request_id: int, *, status: str, result: str = "", attempts: int, next_check_at: str,
    expected_lease_generation: int, last_checked_at: str | None = None,
    targets: list[dict] | None = None,
) -> bool:
    """同一租约内写入全部目标结果及汇总，禁止把未核对目标标为 visible。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _update_verification(
            conn, request_id, status=status, result=result, attempts=attempts,
            next_check_at=next_check_at, expected_lease_generation=expected_lease_generation,
            last_checked_at=last_checked_at, targets=targets,
        )


def finish_agent_download_verification(
    request_id: int, *, status: str, result: str, attempts: int, next_check_at: str,
    expected_lease_generation: int, payload_json: str, last_checked_at: str | None = None,
    targets: list[dict] | None = None,
) -> bool:
    """原子发布全部目标终态并更新原 outbox；新增目标不另建通知 tracker。"""
    if status not in {"visible", "attention"}:
        raise ValueError("自动复核终态无效")
    payload = (payload_json or "").strip()
    if not payload:
        raise ValueError("自动复核通知载荷无效")
    timestamp = db.now()
    due_at = next_check_at or timestamp
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _update_verification(
            conn, request_id, status=status, result=result, attempts=attempts,
            next_check_at=due_at, expected_lease_generation=expected_lease_generation,
            last_checked_at=last_checked_at, targets=targets,
        ):
            return False
        conn.execute(
            "INSERT INTO agent_download_verification_notification_outbox("
            "request_id,owner,chat_id,payload_json,status,attempts,lease_generation,"
            "next_attempt_at,last_error_type,sent_at,created_at,updated_at) "
            "SELECT request_id,owner,chat_id,?,'pending',0,0,?,'',NULL,?,? "
            "FROM agent_download_verifications WHERE request_id=? "
            "ON CONFLICT(request_id) DO UPDATE SET payload_json=excluded.payload_json,status='pending',attempts=0,"
            "lease_generation=agent_download_verification_notification_outbox.lease_generation+1,"
            "next_attempt_at=excluded.next_attempt_at,last_error_type='',updated_at=excluded.updated_at",
            (payload, due_at, timestamp, timestamp, request_id),
        )
        return True


def list_agent_download_verification_notifications() -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_download_verification_notification_outbox ORDER BY id"
        ).fetchall()


def claim_due_agent_download_verification_notification(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取一条下载核验通知；generation 隔离过期发送者。"""
    timestamp = (current_time or db.now())
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_download_verification_notification_outbox "
                "SET status='discarded',lease_generation=lease_generation+1,"
                "payload_json='',last_error_type='DeliveryOutcomeUnknown',updated_at=? "
                "WHERE status='sending' AND updated_at<=?",
                (timestamp, stale_before),
            )
        row = conn.execute(
            "SELECT id FROM agent_download_verification_notification_outbox "
            "WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        notification_id = int(row["id"])
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='sending',lease_generation=lease_generation+1,updated_at=? "
            "WHERE id=? AND status IN ('pending','retry_wait')",
            (timestamp, notification_id),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM agent_download_verification_notification_outbox WHERE id=?",
            (notification_id,),
        ).fetchone()


def complete_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    sent_at: str | None = None,
) -> bool:
    timestamp = (sent_at or db.now())
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='sent',payload_json='',sent_at=?,last_error_type='',updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                timestamp, timestamp, notification_id,
                max(0, expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def release_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    next_attempt_at: str | None = None,
) -> bool:
    """无损释放尚未发送的通知租约；关闭 Agent 不消耗重试预算。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='retry_wait',next_attempt_at=?,last_error_type='',updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                (next_attempt_at or timestamp), timestamp, notification_id,
                max(0, expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def retry_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    next_attempt_at: str,
    error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", (error_type or ""))[:80]
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='retry_wait',attempts=MIN(attempts+1,100),"
            "next_attempt_at=?,last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                (next_attempt_at or timestamp), safe_error_type, timestamp,
                notification_id, max(0, expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def discard_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", (error_type or ""))[:80]
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='discarded',payload_json='',last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                safe_error_type, timestamp, notification_id,
                max(0, expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def discard_agent_download_verification_notifications() -> int:
    """通知关闭时丢弃未发送积压，并使正在发送的旧租约失效。"""
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='discarded',payload_json='',"
            "lease_generation=lease_generation+1,updated_at=? "
            "WHERE status IN ('pending','retry_wait','sending')",
            (timestamp,),
        )
        return max(0, cur.rowcount)


def _recover_after_restart(conn, timestamp: str) -> None:
    """启动时收回运行租约；新 generation 阻止旧执行者晚到提交。"""
    conn.execute(
        "UPDATE agent_download_verifications SET status='retry_wait',"
        "lease_generation=lease_generation+1,"
        "next_check_at=?,updated_at=? WHERE status='running'",
        (timestamp, timestamp),
    )


_STATES = {
    "search_ready",
    "selection_required",
    "confirmation_required",
    "submitted",
    "verification_pending",
    "visible",
    "attention",
    "stale",
    "cancelled",
}
_PRUNABLE_STATES = {"visible", "stale", "cancelled"}
_TARGETS = {"qb", "guangya", "both"}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAX_OWNER_LENGTH = 512
_MAX_WORKFLOWS_PER_OWNER = 50

WORKFLOW_STATE_LABELS = {
    "search_ready": "仍缺失，等待重新搜索资源",
    "selection_required": "已找到候选，等待选择",
    "confirmation_required": "已选择资源，等待确认",
    "submitted": "下载任务已提交",
    "verification_pending": "正在等待下载完成并自动复核",
    "visible": "已入库并可见",
    "attention": "需要人工关注",
    "stale": "流程已过期，需要重新检查",
    "cancelled": "已取消",
}


@dataclass(frozen=True)
class MissingWorkflowRef:
    workflow_id: str
    item_id: str
    revision: int


class MissingMediaWorkflowRepository(Protocol):
    def capture_search(
        self, *, owner: str, verification: dict, episodes: list[dict]
    ) -> str | None: ...

    def select_candidate(
        self,
        *,
        owner: str,
        verification: dict[str, Any],
        candidate_title: str,
        target: str,
    ) -> MissingWorkflowRef | None: ...

    def attach_submission(
        self,
        *,
        workflow_ref: dict[str, Any],
        request_id: int,
        verification_enqueued: bool,
    ) -> bool: ...

    def finish_verification(
        self, *, request_id: int, status: str, result: str, targets: list[dict]
    ) -> bool: ...

    def release_confirmation(
        self, *, owner: str, workflow_ref: dict[str, Any]
    ) -> bool: ...

    def reconcile_confirmations(
        self, *, owner: str, active_refs: tuple[dict[str, Any], ...]
    ) -> int: ...

    def list_for_owner(self, *, owner: str, limit: int = 10) -> tuple[Any, ...]: ...


class SQLiteMissingMediaWorkflowRepository:
    """持久保存安全补库状态；绝不保存 result_id、磁力、URL 或本地路径。"""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] = get_web_secret,
    ) -> None:
        self._secret_provider = secret_provider

    def capture_search(
        self, *, owner: str, verification: dict, episodes: list[dict]
    ) -> str | None:
        owner_digest = self._owner_digest(owner)
        base = _base_verification_projection(verification)
        if base is None or verification.get("verified_missing") is not True:
            return None
        items = {episode: {"episode": episode, "state": "selection_required" if item.get("has_candidate") else "search_ready"}
                 for item in episodes if (episode := _positive_int(item.get("episode"), maximum=1000)) is not None}
        if not items:
            return None
        source_tool = "resources.search_missing"
        parsed: dict[str, Any] = {**base, "items": list(items.values())}
        workflow_id = secrets.token_urlsafe(18)
        now = db.now()
        state = _rollup_state(tuple(item["state"] for item in parsed["items"]))
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 仅废弃旧未受理候选；已提交任务独立等待核验，不能随重搜丢失关联。
            stale_rows = conn.execute(
                "SELECT DISTINCT w.workflow_id FROM agent_missing_media_workflows w "
                "JOIN agent_missing_media_workflow_items i ON i.workflow_id=w.workflow_id "
                "WHERE w.owner_digest=? AND w.title=? AND w.tmdb_id=? AND w.season=? "
                "AND i.state IN ('search_ready','selection_required','confirmation_required')",
                (
                    owner_digest,
                    parsed["title"],
                    parsed["tmdb_id"],
                    parsed["season"],
                ),
            ).fetchall()
            for row in stale_rows:
                conn.execute(
                    "UPDATE agent_missing_media_workflow_items SET state='stale',"
                    "revision=revision+1,updated_at=? WHERE workflow_id=? "
                    "AND state IN ('search_ready','selection_required','confirmation_required')",
                    (now, row["workflow_id"]),
                )
                self._refresh_workflow(conn, workflow_id=row["workflow_id"], updated_at=now)
            conn.execute(
                "INSERT INTO agent_missing_media_workflows("
                "workflow_id,owner_digest,source_tool,title,tmdb_id,season,as_of,state,"
                "revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                (
                    workflow_id,
                    owner_digest,
                    source_tool,
                    parsed["title"],
                    parsed["tmdb_id"],
                    parsed["season"],
                    parsed["as_of"],
                    state,
                    now,
                    now,
                ),
            )
            for item in parsed["items"]:
                conn.execute(
                    "INSERT INTO agent_missing_media_workflow_items("
                    "item_id,workflow_id,title,tmdb_id,season,episode,as_of,state,"
                    "revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        secrets.token_urlsafe(18),
                        workflow_id,
                        parsed["title"],
                        parsed["tmdb_id"],
                        parsed["season"],
                        item["episode"],
                        parsed["as_of"],
                        item["state"],
                        now,
                        now,
                    ),
                )
            self._bound_owner_rows(conn, owner_digest=owner_digest)
        return workflow_id

    def select_candidate(
        self,
        *,
        owner: str,
        verification: dict[str, Any],
        candidate_title: str,
        target: str,
    ) -> MissingWorkflowRef | None:
        owner_digest = self._owner_digest(owner)
        safe_verification = _verification_projection(verification)
        safe_candidate = _safe_text(candidate_title, 300)
        safe_target = (target or "").strip().lower()
        if (
            safe_verification is None
            or not safe_candidate
            or safe_target not in _TARGETS
        ):
            return None
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT i.item_id,i.workflow_id,i.revision FROM "
                "agent_missing_media_workflow_items i "
                "JOIN agent_missing_media_workflows w ON w.workflow_id=i.workflow_id "
                "WHERE w.owner_digest=? AND i.title=? AND i.tmdb_id=? AND i.season=? "
                "AND i.episode=? AND i.as_of=? "
                "AND i.state IN ('selection_required','search_ready') "
                "ORDER BY i.id DESC LIMIT 1",
                (
                    owner_digest,
                    safe_verification["title"],
                    safe_verification["tmdb_id"],
                    safe_verification["season"],
                    safe_verification["episode"],
                    safe_verification["as_of"],
                ),
            ).fetchone()
            if row is None:
                return None
            current_revision = int(row["revision"] or 1)
            cursor = conn.execute(
                "UPDATE agent_missing_media_workflow_items SET "
                "state='confirmation_required',candidate_title=?,target=?,"
                "revision=revision+1,last_error_code='',updated_at=? "
                "WHERE item_id=? AND revision=? "
                "AND state IN ('selection_required','search_ready')",
                (
                    safe_candidate,
                    safe_target,
                    now,
                    str(row["item_id"]),
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                return None
            conn.execute(
                "UPDATE agent_missing_media_workflows SET state='confirmation_required',"
                "revision=revision+1,updated_at=? WHERE workflow_id=?",
                (now, str(row["workflow_id"])),
            )
            return MissingWorkflowRef(
                workflow_id=str(row["workflow_id"]),
                item_id=str(row["item_id"]),
                revision=current_revision + 1,
            )

    def attach_submission(
        self,
        *,
        workflow_ref: dict[str, Any],
        request_id: int,
        verification_enqueued: bool,
    ) -> bool:
        ref = _workflow_ref_projection(workflow_ref)
        request = _positive_int(request_id, maximum=2_147_483_647)
        if ref is None or request is None:
            return False
        next_state = "verification_pending" if verification_enqueued else "submitted"
        error_code = ""
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            verified = conn.execute(
                "SELECT status,result,targets_json FROM agent_download_verifications WHERE request_id=?",
                (request,),
            ).fetchone()
            if verified is not None and verified["status"] in {"visible", "attention"}:
                item = conn.execute(
                    "SELECT tmdb_id,season,episode FROM agent_missing_media_workflow_items WHERE item_id=? AND workflow_id=?",
                    (ref.item_id, ref.workflow_id),
                ).fetchone()
                next_state = _verification_item_state(item, json.loads(verified["targets_json"]))
                error_code = "" if next_state == "visible" else "verification_attention"
            cursor = conn.execute(
                "UPDATE agent_missing_media_workflow_items SET state=?,"
                "download_request_id=?,revision=revision+1,last_error_code=?,updated_at=? "
                "WHERE item_id=? AND workflow_id=? AND download_request_id IS NULL "
                # 重搜只修订一次的已冻结候选可接收真实受理回执，不恢复执行授权。
                "AND ((revision=? AND state='confirmation_required') OR "
                "(revision=? AND state='stale' AND candidate_title<>'' AND target<>''))",
                (
                    next_state,
                    request,
                    error_code,
                    now,
                    ref.item_id,
                    ref.workflow_id,
                    ref.revision,
                    ref.revision + 1,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._refresh_workflow(conn, workflow_id=ref.workflow_id, updated_at=now)
            return True

    def finish_verification(self, *, request_id: int, status: str, result: str, targets: list[dict]) -> bool:
        request = _positive_int(request_id, maximum=2_147_483_647)
        safe_status = (status or "").strip().lower()
        safe_result = (result or "").strip().lower()
        if request is None or safe_status not in {"visible", "attention"} or safe_result not in {"", "visible", "missing", "inconclusive"}:
            return False
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT item_id,workflow_id,tmdb_id,season,episode,state,last_error_code FROM agent_missing_media_workflow_items "
                "WHERE download_request_id=? AND state IN "
                "('submitted','verification_pending','attention','visible')",
                (request,),
            ).fetchall()
            if not rows:
                return False
            changed = set()
            for row in rows:
                item_state = _verification_item_state(row, targets)
                error_code = "" if item_state == "visible" else "verification_attention"
                if (row["state"], row["last_error_code"]) == (item_state, error_code):
                    continue
                conn.execute(
                    "UPDATE agent_missing_media_workflow_items SET state=?,last_error_code=?,revision=revision+1,updated_at=? WHERE item_id=?",
                    (item_state, error_code, now, row["item_id"]),
                )
                changed.add(str(row["workflow_id"]))
            for workflow_id in changed:
                self._refresh_workflow(conn, workflow_id=workflow_id, updated_at=now)
            return bool(changed)

    def release_confirmation(self, *, owner: str, workflow_ref: dict[str, Any]) -> bool:
        owner_digest = self._owner_digest(owner)
        ref = _workflow_ref_projection(workflow_ref)
        if ref is None:
            return False
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE agent_missing_media_workflow_items SET "
                "state='selection_required',candidate_title='',target='',"
                "revision=revision+1,last_error_code='',updated_at=? "
                "WHERE item_id=? AND workflow_id=? AND revision=? "
                "AND state='confirmation_required' AND workflow_id IN ("
                "SELECT workflow_id FROM agent_missing_media_workflows WHERE owner_digest=?"
                ")",
                (now, ref.item_id, ref.workflow_id, ref.revision, owner_digest),
            )
            if cursor.rowcount != 1:
                return False
            self._refresh_workflow(conn, workflow_id=ref.workflow_id, updated_at=now)
            return True

    def reconcile_confirmations(
        self, *, owner: str, active_refs: tuple[dict[str, Any], ...]
    ) -> int:
        owner_digest = self._owner_digest(owner)
        active = {
            (ref.workflow_id, ref.item_id, ref.revision)
            for value in active_refs
            if (ref := _workflow_ref_projection(value)) is not None
        }
        now = db.now()
        released = 0
        touched: set[str] = set()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT i.workflow_id,i.item_id,i.revision FROM "
                "agent_missing_media_workflow_items i JOIN agent_missing_media_workflows w "
                "ON w.workflow_id=i.workflow_id WHERE w.owner_digest=? "
                "AND i.state='confirmation_required'",
                (owner_digest,),
            ).fetchall()
            for row in rows:
                key = (
                    str(row["workflow_id"]),
                    str(row["item_id"]),
                    int(row["revision"] or 0),
                )
                if key in active:
                    continue
                cursor = conn.execute(
                    "UPDATE agent_missing_media_workflow_items SET "
                    "state='selection_required',candidate_title='',target='',"
                    "revision=revision+1,last_error_code='',updated_at=? "
                    "WHERE workflow_id=? AND item_id=? AND revision=? "
                    "AND state='confirmation_required'",
                    (now, *key),
                )
                if cursor.rowcount == 1:
                    released += 1
                    touched.add(key[0])
            for workflow_id in touched:
                self._refresh_workflow(conn, workflow_id=workflow_id, updated_at=now)
        return released

    def list_for_owner(self, *, owner: str, limit: int = 10, offset: int = 0, conn: sqlite3.Connection | None = None) -> tuple[Any, ...]:
        owner_digest = self._owner_digest(owner)
        safe_limit = max(1, min(limit, 30))
        with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
            workflows = connection.execute(
                "SELECT workflow_id FROM agent_missing_media_workflows "
                "WHERE owner_digest=? ORDER BY updated_at DESC,workflow_id LIMIT ? OFFSET ?",
                (owner_digest, safe_limit, offset),
            ).fetchall()
            workflow_ids = tuple(str(row["workflow_id"]) for row in workflows)
            if not workflow_ids:
                return ()
            placeholders = ",".join("?" for _ in workflow_ids)
            return tuple(
                connection.execute(
                    "SELECT w.workflow_id,w.source_tool,w.title,w.tmdb_id,w.season,w.as_of,"
                    "w.state,w.revision,w.created_at,w.updated_at,"
                    "i.item_id,i.episode,i.state AS item_state,i.target,"
                    "i.download_request_id,i.last_error_code,i.updated_at AS item_updated_at "
                    "FROM agent_missing_media_workflows w "
                    "JOIN agent_missing_media_workflow_items i ON i.workflow_id=w.workflow_id "
                    f"WHERE w.owner_digest=? AND w.workflow_id IN ({placeholders}) "
                    "ORDER BY w.updated_at DESC,w.workflow_id,i.episode ASC",
                    (owner_digest, *workflow_ids),
                ).fetchall()
            )

    def count_for_owner(self, *, owner: str, conn: sqlite3.Connection | None = None) -> int:
        with (nullcontext(conn) if conn is not None else db.get_conn()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM agent_missing_media_workflows WHERE owner_digest=?",
                                    (self._owner_digest(owner),)).fetchone()[0])

    @staticmethod
    def _refresh_workflow(conn: Any, *, workflow_id: str, updated_at: str) -> None:
        states = tuple(
            str(row["state"])
            for row in conn.execute(
                "SELECT state FROM agent_missing_media_workflow_items "
                "WHERE workflow_id=? ORDER BY episode",
                (workflow_id,),
            ).fetchall()
        )
        if not states:
            return
        conn.execute(
            "UPDATE agent_missing_media_workflows SET state=?,revision=revision+1,updated_at=? "
            "WHERE workflow_id=?",
            (_rollup_state(states), updated_at, workflow_id),
        )

    @staticmethod
    def _bound_owner_rows(conn: Any, *, owner_digest: str) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM agent_missing_media_workflows "
            "WHERE owner_digest=?",
            (owner_digest,),
        ).fetchone()
        overflow = max(
            0, int(row["total"] if row is not None else 0) - _MAX_WORKFLOWS_PER_OWNER
        )
        if overflow <= 0:
            return
        # 身份迁移可能把多个旧 owner 分区合并到同一个 session owner。容量整理只能
        # 淘汰已完成/过期/取消的历史记录，绝不能静默删除仍在执行或需要人工关注的流程。
        placeholders = ",".join("?" for _ in _PRUNABLE_STATES)
        stale = conn.execute(
            "SELECT workflow_id FROM agent_missing_media_workflows "
            f"WHERE owner_digest=? AND state IN ({placeholders}) "
            "ORDER BY updated_at ASC,workflow_id ASC LIMIT ?",
            (owner_digest, *sorted(_PRUNABLE_STATES), overflow),
        ).fetchall()
        if not stale:
            return
        conn.executemany(
            "DELETE FROM agent_missing_media_workflows WHERE workflow_id=?",
            [(str(row["workflow_id"]),) for row in stale],
        )

    def _owner_digest(self, owner: str) -> str:
        normalized = (owner or "").strip()
        if not normalized or len(normalized) > _MAX_OWNER_LENGTH:
            raise ValueError("无法确认当前调用身份")
        secret = (self._secret_provider() or "")
        if not secret:
            raise ValueError("调用身份隔离密钥不可用")
        return hmac.new(
            secret.encode("utf-8"),
            b"mediaflux-agent-missing-workflow:v1\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def workflow_followup_context(
    verification: dict[str, Any] | None,
    workflow_ref: MissingWorkflowRef | None,
) -> dict[str, Any] | None:
    safe_verification = _verification_projection(verification)
    if safe_verification is None:
        return None
    if workflow_ref is None:
        return safe_verification
    return {
        "verification": safe_verification,
        "workflow": {
            "workflow_id": workflow_ref.workflow_id,
            "item_id": workflow_ref.item_id,
            "revision": workflow_ref.revision,
        },
    }


def workflow_ref_from_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _workflow_ref_dict(value.get("workflow"))


def verification_from_followup_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("verification") if "verification" in value else value
    return _verification_projection(source)


def _base_verification_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    title = _safe_text(value.get("title"), 120)
    tmdb_id = str(value.get("tmdb_id") or "").strip()
    season = _positive_int(value.get("season"), maximum=100)
    as_of = _safe_date(value.get("as_of"))
    if (
        not title
        or not tmdb_id.isascii()
        or not tmdb_id.isdigit()
        or not 1 <= len(tmdb_id) <= 10
    ):
        return None
    if season is None or not as_of:
        return None
    projection = {"title": title, "tmdb_id": tmdb_id, "season": season, "as_of": as_of}
    library_name = _safe_text(value.get("library_name"), 80)
    if library_name:
        projection["library_name"] = library_name
    return projection


def _verification_projection(value: Any) -> dict[str, Any] | None:
    base = _base_verification_projection(value)
    if base is None or not isinstance(value, dict):
        return None
    episode = _positive_int(value.get("episode"), maximum=1000)
    if episode is None:
        return None
    return {**base, "episode": episode}


def _workflow_ref_projection(value: Any) -> MissingWorkflowRef | None:
    projected = _workflow_ref_dict(value)
    if projected is None:
        return None
    return MissingWorkflowRef(
        workflow_id=projected["workflow_id"],
        item_id=projected["item_id"],
        revision=projected["revision"],
    )


def _workflow_ref_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "workflow_id",
        "item_id",
        "revision",
    }:
        return None
    workflow_id = str(value.get("workflow_id") or "").strip()
    item_id = str(value.get("item_id") or "").strip()
    revision = _positive_int(value.get("revision"), maximum=2_147_483_647)
    if (
        not _ID_PATTERN.fullmatch(workflow_id)
        or not _ID_PATTERN.fullmatch(item_id)
        or revision is None
    ):
        return None
    return {"workflow_id": workflow_id, "item_id": item_id, "revision": revision}


def _verification_item_state(item: Any, targets: list[dict]) -> str:
    if item is None:
        return "attention"
    identity = (item["tmdb_id"], item["season"], item["episode"])
    matches = [target for target in targets if
               (target["tmdb_id"], target["season"], target["episode"]) == identity]
    # 工作流不区分媒体库；同一集在所有指定库中均已核实，才投影为可见。
    return "visible" if matches and all(target.get("result") == "visible" for target in matches) else "attention"


def _rollup_state(states: tuple[str, ...]) -> str:
    normalized = tuple(state for state in states if state in _STATES)
    if not normalized:
        return "attention"
    if all(state == "visible" for state in normalized):
        return "visible"
    priority = (
        "attention",
        "verification_pending",
        "submitted",
        "confirmation_required",
        "selection_required",
        "search_ready",
        "stale",
        "cancelled",
    )
    return next((state for state in priority if state in normalized), normalized[0])


def _safe_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if not text or any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text[:maximum]


def _positive_int(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 1 <= parsed <= maximum else None


def _safe_date(value: Any) -> str:
    try:
        parsed = date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat() if parsed <= datetime.now().astimezone().date() else ""






# 在函数定义后绑定门面，兼容 repository-first 导入；运行期始终使用同一连接/时钟所有者。
from app import database as db
