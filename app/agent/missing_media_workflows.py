"""缺集补库流程的安全持久化与用户可读投影。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import hmac
import re
import secrets
from typing import Any, Callable, Protocol
import unicodedata

from app import database as db
from app.agent.models import ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.modules.web_secret import get_web_secret

_SOURCE_TOOLS = {
    "library.search_missing_episode_resources",
    "library.search_missing_season_resources",
}
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
_TERMINAL_STATES = {"visible", "attention", "stale", "cancelled"}
_PRUNABLE_STATES = {"visible", "stale", "cancelled"}
_TARGETS = {"qb", "guangya", "both"}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,80}$")
_MAX_OWNER_LENGTH = 512
_MAX_WORKFLOWS_PER_OWNER = 50

_STATE_LABELS = {
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
    def capture_search(self, *, owner: str, tool_name: str, result: ToolResult) -> str | None: ...

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

    def finish_verification(self, *, request_id: int, status: str, result: str) -> bool: ...

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

    def capture_search(self, *, owner: str, tool_name: str, result: ToolResult) -> str | None:
        source_tool = str(tool_name or "").strip()
        if source_tool not in _SOURCE_TOOLS:
            return None
        owner_digest = self._owner_digest(owner)
        parsed = _search_projection(source_tool, result)
        if parsed is None:
            return None
        workflow_id = secrets.token_urlsafe(18)
        now = db.now()
        state = _rollup_state(tuple(item["state"] for item in parsed["items"]))
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 新检查会使相同目标的旧未完成流程失效；旧候选句柄不可跨搜索复用。
            stale_rows = conn.execute(
                "SELECT workflow_id FROM agent_missing_media_workflows "
                "WHERE owner_digest=? AND title=? AND tmdb_id=? AND season=? "
                "AND state NOT IN ('visible','attention','stale','cancelled')",
                (
                    owner_digest,
                    parsed["title"],
                    parsed["tmdb_id"],
                    parsed["season"],
                ),
            ).fetchall()
            stale_ids = [str(row["workflow_id"]) for row in stale_rows]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"UPDATE agent_missing_media_workflows SET state='stale',"
                    f"revision=revision+1,updated_at=? WHERE workflow_id IN ({placeholders})",
                    (now, *stale_ids),
                )
                conn.execute(
                    f"UPDATE agent_missing_media_workflow_items SET state='stale',"
                    f"revision=revision+1,updated_at=? WHERE workflow_id IN ({placeholders}) "
                    "AND state NOT IN ('visible','attention','cancelled')",
                    (now, *stale_ids),
                )
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
        safe_target = str(target or "").strip().lower()
        if safe_verification is None or not safe_candidate or safe_target not in _TARGETS:
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
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE agent_missing_media_workflow_items SET state=?,"
                "download_request_id=?,revision=revision+1,last_error_code='',updated_at=? "
                "WHERE item_id=? AND workflow_id=? AND revision=? "
                "AND state='confirmation_required'",
                (
                    next_state,
                    request,
                    now,
                    ref.item_id,
                    ref.workflow_id,
                    ref.revision,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._refresh_workflow(conn, workflow_id=ref.workflow_id, updated_at=now)
            return True

    def finish_verification(self, *, request_id: int, status: str, result: str) -> bool:
        request = _positive_int(request_id, maximum=2_147_483_647)
        safe_status = str(status or "").strip().lower()
        safe_result = str(result or "").strip().lower()
        if request is None or safe_status not in {"visible", "attention"}:
            return False
        next_state = "visible" if safe_status == "visible" and safe_result == "visible" else "attention"
        error_code = "" if next_state == "visible" else "verification_attention"
        now = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT workflow_id FROM agent_missing_media_workflow_items "
                "WHERE download_request_id=? AND state IN "
                "('submitted','verification_pending','attention')",
                (request,),
            ).fetchall()
            if not rows:
                return False
            cursor = conn.execute(
                "UPDATE agent_missing_media_workflow_items SET state=?,"
                "last_error_code=?,revision=revision+1,updated_at=? "
                "WHERE download_request_id=? AND state IN "
                "('submitted','verification_pending','attention')",
                (next_state, error_code, now, request),
            )
            for workflow_id in {str(row["workflow_id"]) for row in rows}:
                self._refresh_workflow(conn, workflow_id=workflow_id, updated_at=now)
            return cursor.rowcount > 0

    def release_confirmation(
        self, *, owner: str, workflow_ref: dict[str, Any]
    ) -> bool:
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

    def list_for_owner(self, *, owner: str, limit: int = 10) -> tuple[Any, ...]:
        owner_digest = self._owner_digest(owner)
        safe_limit = max(1, min(int(limit), 30))
        with db.get_conn() as conn:
            workflows = conn.execute(
                "SELECT workflow_id FROM agent_missing_media_workflows "
                "WHERE owner_digest=? ORDER BY updated_at DESC,workflow_id LIMIT ?",
                (owner_digest, safe_limit),
            ).fetchall()
            workflow_ids = tuple(str(row["workflow_id"]) for row in workflows)
            if not workflow_ids:
                return ()
            placeholders = ",".join("?" for _ in workflow_ids)
            return tuple(conn.execute(
                "SELECT w.workflow_id,w.source_tool,w.title,w.tmdb_id,w.season,w.as_of,"
                "w.state,w.revision,w.created_at,w.updated_at,"
                "i.item_id,i.episode,i.state AS item_state,i.target,"
                "i.download_request_id,i.last_error_code,i.updated_at AS item_updated_at "
                "FROM agent_missing_media_workflows w "
                "JOIN agent_missing_media_workflow_items i ON i.workflow_id=w.workflow_id "
                f"WHERE w.owner_digest=? AND w.workflow_id IN ({placeholders}) "
                "ORDER BY w.updated_at DESC,w.workflow_id,i.episode ASC",
                (owner_digest, *workflow_ids),
            ).fetchall())
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
        overflow = max(0, int(row["total"] if row is not None else 0) - _MAX_WORKFLOWS_PER_OWNER)
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
        normalized = str(owner or "").strip()
        if not normalized or len(normalized) > _MAX_OWNER_LENGTH:
            raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
        secret = str(self._secret_provider() or "")
        if not secret:
            raise AgentToolError("Agent 身份隔离密钥不可用", code="identity_required")
        return hmac.new(
            secret.encode("utf-8"),
            b"mediaflux-agent-missing-workflow:v1\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def missing_workflow_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    extra = set(arguments) - {"limit"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    limit = arguments.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise AgentToolError("补库流程条数必须为 1 到 30 的整数")
    return {"limit": limit}


def list_missing_workflows(
    arguments: dict[str, Any],
    context: ToolContext,
    *,
    repository: MissingMediaWorkflowRepository | None = None,
) -> ToolResult:
    repo = repository or SQLiteMissingMediaWorkflowRepository()
    rows = repo.list_for_owner(owner=context.owner, limit=int(arguments["limit"]))
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        workflow_id = str(row["workflow_id"])
        workflow = grouped.setdefault(workflow_id, {
            "title": _safe_text(row["title"], 120),
            "season": _positive_int(row["season"], maximum=100) or 1,
            "state": str(row["state"] or "attention"),
            "state_label": _STATE_LABELS.get(str(row["state"] or ""), "需要人工关注"),
            "updated_at": _safe_timestamp(row["updated_at"]),
            "items": [],
        })
        item_state = str(row["item_state"] or "attention")
        workflow["items"].append({
            "episode": _positive_int(row["episode"], maximum=1000) or 1,
            "state": item_state,
            "state_label": _STATE_LABELS.get(item_state, "需要人工关注"),
            "target": str(row["target"] or "") if str(row["target"] or "") in _TARGETS else "",
            "has_download_task": bool(row["download_request_id"]),
            "updated_at": _safe_timestamp(row["item_updated_at"]),
        })
    workflows = list(grouped.values())[: int(arguments["limit"])]
    attention = sum(
        1 for workflow in workflows
        if workflow["state"] in {
            "attention",
            "search_ready",
            "selection_required",
            "confirmation_required",
            "stale",
        }
    )
    if not workflows:
        return ToolResult(
            True,
            "empty",
            "当前没有补库流程",
            data={"total": 0, "attention": 0, "workflows": []},
            suggestions=["可以问：检查媒体库有没有缺集。"],
        )
    suggestions: list[str] = []
    for workflow in workflows:
        for item in workflow["items"]:
            if item["state"] in {"search_ready", "stale", "attention"}:
                suggestions.append(
                    f"重新搜索《{workflow['title']}》第 {workflow['season']} 季第 {item['episode']} 集资源"
                )
                break
        if len(suggestions) >= 3:
            break
    if any(workflow["state"] in {"submitted", "verification_pending"} for workflow in workflows):
        suggestions.append("查看刚才下载到哪了")
    return ToolResult(
        True,
        "attention" if attention else "healthy",
        f"共有 {len(workflows)} 个补库流程，{attention} 个需要你处理",
        data={"total": len(workflows), "attention": attention, "workflows": workflows},
        suggestions=suggestions[:4],
    )


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


def is_missing_workflow_status_message(message: str) -> bool:
    normalized = "".join(str(message or "").strip().casefold().split())
    if not normalized:
        return False
    scope = any(token in normalized for token in ("补库", "缺集任务", "缺集下载", "入库复核"))
    status = any(token in normalized for token in ("状态", "进度", "到哪", "完成", "情况", "结果"))
    return scope and status


def _search_projection(tool_name: str, result: ToolResult) -> dict[str, Any] | None:
    data = result.data if isinstance(result.data, dict) else {}
    verification = data.get("verification")
    if not isinstance(verification, dict) or verification.get("verified_missing") is not True:
        return None
    base = _base_verification_projection(verification)
    if base is None:
        return None
    items: list[dict[str, Any]] = []
    if tool_name == "library.search_missing_episode_resources":
        episode = _positive_int(verification.get("episode"), maximum=1000)
        if episode is None:
            return None
        items.append({
            "episode": episode,
            "state": "selection_required" if _search_has_candidate(data.get("search")) else "search_ready",
        })
    else:
        episodes = data.get("episodes")
        if not isinstance(episodes, list):
            return None
        for raw in episodes[:1000]:
            if not isinstance(raw, dict):
                continue
            episode = _positive_int(raw.get("episode"), maximum=1000)
            if episode is None or any(item["episode"] == episode for item in items):
                continue
            items.append({
                "episode": episode,
                "state": "selection_required" if _search_has_candidate(raw.get("search")) else "search_ready",
            })
    if not items:
        return None
    return {**base, "items": items}


def _search_has_candidate(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    recommendation = value.get("recommendation")
    if not isinstance(recommendation, dict):
        return False
    selected = recommendation.get("selected")
    if isinstance(selected, dict) and _safe_text(selected.get("title"), 300):
        return True
    alternatives = recommendation.get("alternatives")
    return bool(
        isinstance(alternatives, list)
        and any(isinstance(item, dict) and _safe_text(item.get("title"), 300) for item in alternatives)
    )


def _base_verification_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    title = _safe_text(value.get("title"), 120)
    tmdb_id = str(value.get("tmdb_id") or "").strip()
    season = _positive_int(value.get("season"), maximum=100)
    as_of = _safe_date(value.get("as_of"))
    if not title or not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10:
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
    if not isinstance(value, dict) or set(value) != {"workflow_id", "item_id", "revision"}:
        return None
    workflow_id = str(value.get("workflow_id") or "").strip()
    item_id = str(value.get("item_id") or "").strip()
    revision = _positive_int(value.get("revision"), maximum=2_147_483_647)
    if not _ID_PATTERN.fullmatch(workflow_id) or not _ID_PATTERN.fullmatch(item_id) or revision is None:
        return None
    return {"workflow_id": workflow_id, "item_id": item_id, "revision": revision}


def _rollup_state(states: tuple[str, ...]) -> str:
    normalized = tuple(state for state in states if state in _STATES)
    if not normalized:
        return "attention"
    if all(state == "visible" for state in normalized):
        return "visible"
    if all(state in _TERMINAL_STATES for state in normalized):
        return "attention" if "attention" in normalized else normalized[0]
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
    return parsed.isoformat() if parsed <= date.today() else ""


def _safe_timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat(timespec="seconds")


def safe_workflow_error_code(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _ERROR_CODE_PATTERN.fullmatch(normalized) else ""
