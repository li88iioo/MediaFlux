"""Agent 对话历史的最小、安全 SQLite 持久化。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import unquote
from typing import Any, Callable

from app import database as db
from app.agent.conversation_summary import (
    normalize_conversation_summary,
    render_conversation_summary,
)
from app.modules.web_secret import get_web_secret
from app.agent.media_case import media_case_stage_for_tool, normalize_media_case_stage
from app.sensitive_data import contains_sensitive_credential

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SCHEMA_VERSION = 1
_SUMMARY_STORAGE_VERSION = 1
_UNSAFE_HISTORY_DETAIL = "[已隐藏敏感详情]"
_SAFE_CONTEXT_DOMAINS = frozenset({"rss"})
_SAFE_CONTEXT_TOPICS = frozenset({"rss", "media_subscription"})
_MEDIA_CONTEXT_TOOL_TYPES: dict[str, str] = {
    "library.search": "",
    "library.count_series_episodes": "tv",
    "library.audit_episodes": "tv",
    "library.audit_library_episodes": "tv",
    "library.check_updates": "",
    "library.search_missing_episode_resources": "tv",
    "library.search_missing_season_resources": "tv",
    "media.subscription_updates": "",
    "discovery.search": "",
    "discovery.recommend": "",
    "discovery.lookup_rating": "",
    "discovery.add_watchlist": "",
    "indexer.search_resources": "",
    "indexer.submit_resource": "tv",
}
_MEDIA_CONTEXT_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_HISTORY_CREDENTIAL_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:pass|passwd|password|pwd|token|secret|api[_ -]?key|auth)\s*[:=]\s*\S+"
    r"|[?&](?:pass|passwd|password|pwd|token|secret|key|auth|code)=([^&\s]+)"
    r"|(?:密码|口令|密钥|令牌|访问码|授权码|提取码)\s+(?:是\s*)?[^\s，。；,;]{4,}"
    r")"
)
_HISTORY_MULTILINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_OUTPUT_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:magnet|ed2k)\s*:\s*"
    r"|\bfile\s*:\s*(?:/{1,3}|\\)"
    r"|\b(?:cookie|set-cookie)\s*[:=]\s*[^\s;,]+"
    r"|\b(?:https?|ftp|file)\s*:\s*//"
    r"|\b[a-z][a-z0-9+.-]{1,20}\s*:\s*//[^\s]*@"
    r"|(?:^|[\s:：(\"'\[\{])/(?!/)[^\s]+"
    r"|(?:^|[\s:：(\"'\[\{])\\\\[^\s]+"
    r"|(?:^|[^A-Za-z0-9])[A-Za-z]:[\\/][^\s]+"
    r")"
)


@dataclass(frozen=True, slots=True)
class ConversationCompactionSnapshot:
    conversation_id: int
    expected_generation: int
    expected_revision: int
    through_message_id: int
    previous_summary: dict[str, Any] | None
    latest_media_context: dict[str, Any] | None
    messages: tuple[dict[str, Any], ...]


class SQLiteAgentConversationHistoryRepository:
    """按登录会话隔离，仅保存不可重放的安全对话投影。"""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] = get_web_secret,
        max_sessions: int = 24,
        max_messages: int = 80,
        max_payload_bytes: int = 4 * 1024,
        retention_days: int = 45,
        max_total_sessions: int = 500,
    ) -> None:
        self._secret_provider = secret_provider
        self.max_sessions = max(1, min(int(max_sessions), 100))
        self.max_messages = max(2, min(int(max_messages), 500))
        self.max_payload_bytes = max(1024, min(int(max_payload_bytes), 64 * 1024))
        self.max_summary_payload_bytes = max(
            8 * 1024, min(self.max_payload_bytes * 4, 64 * 1024)
        )
        self.retention_days = max(1, min(int(retention_days), 365))
        self.max_total_sessions = max(10, min(int(max_total_sessions), 10000))

    def append_query_turn(
        self,
        *,
        principal: str,
        session_id: str,
        message: str,
        response: dict[str, Any],
        expected_generation: int | None = None,
    ) -> bool:
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        raw_user_text = str(message or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if self._contains_unsafe_history_text(raw_user_text):
            raise ValueError("包含敏感详情的 Agent 消息不会写入历史")
        user_text = self._safe_output_text(
            raw_user_text, limit=1000, label="message"
        )
        assistant = self._assistant_projection(response)
        title = self._title(user_text)
        created_at = db.now()
        user_payload = self._encode(
            principal_digest=principal_digest,
            session_id=session_key,
            role="user",
            data={"text": user_text},
        )
        assistant_payload = self._encode_assistant_projection(
            principal_digest=principal_digest,
            session_id=session_key,
            data=assistant,
        )
        with db.get_conn() as conn:
            self._prune_global(conn, now_value=created_at)
            current_generation = self._session_generation_locked(
                conn,
                principal_digest=principal_digest,
                session_id=session_key,
                now_value=created_at,
            )
            if (
                expected_generation is not None
                and int(expected_generation) != current_generation
            ):
                return False
            conn.execute(
                "INSERT OR IGNORE INTO agent_conversations("
                "principal_digest,session_id,title,message_count,created_at,updated_at"
                ") VALUES(?,?,?,0,?,?)",
                (principal_digest, session_key, title, created_at, created_at),
            )
            row = conn.execute(
                "SELECT id FROM agent_conversations WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("Agent 会话创建失败")
            conversation_id = int(row["id"])
            conn.executemany(
                "INSERT INTO agent_conversation_messages(conversation_id,role,payload,created_at) "
                "VALUES(?,?,?,?)",
                (
                    (conversation_id, "user", user_payload, created_at),
                    (conversation_id, "assistant", assistant_payload, created_at),
                ),
            )
            conn.execute(
                "DELETE FROM agent_conversation_messages WHERE conversation_id=? AND id NOT IN ("
                "SELECT id FROM agent_conversation_messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?)",
                (conversation_id, conversation_id, self.max_messages),
            )
            count = int(conn.execute(
                "SELECT COUNT(*) AS value FROM agent_conversation_messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()["value"])
            conn.execute(
                "UPDATE agent_conversations SET title=CASE WHEN title='' THEN ? ELSE title END, "
                "message_count=?, updated_at=? WHERE id=?",
                (title, count, created_at, conversation_id),
            )
            stale = conn.execute(
                "SELECT id FROM agent_conversations WHERE principal_digest=? "
                "ORDER BY updated_at DESC,id DESC LIMIT -1 OFFSET ?",
                (principal_digest, self.max_sessions),
            ).fetchall()
            if stale:
                conn.executemany(
                    "DELETE FROM agent_conversations WHERE id=?",
                    ((int(item["id"]),) for item in stale),
                )
            # 插入后再次收敛全局上限，避免恰好达到上限时新增会话多保留一条。
            self._prune_global(conn, now_value=created_at)
            return True

    def _prune_global(self, conn: Any, *, now_value: str) -> None:
        try:
            current = datetime.strptime(now_value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            current = datetime.now()
        cutoff = (current - timedelta(days=self.retention_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "DELETE FROM agent_conversations WHERE updated_at < ?",
            (cutoff,),
        )
        stale = conn.execute(
            "SELECT id FROM agent_conversations "
            "ORDER BY updated_at DESC,id DESC LIMIT -1 OFFSET ?",
            (self.max_total_sessions,),
        ).fetchall()
        if stale:
            conn.executemany(
                "DELETE FROM agent_conversations WHERE id=?",
                ((int(item["id"]),) for item in stale),
            )
        # epoch 使用随机不可复用值，因此可安全清理旧行；迟到请求即便在清理后
        # 重建 epoch，也不会再次命中删除前捕获的 generation。
        conn.execute(
            "DELETE FROM agent_conversation_epochs WHERE updated_at < ?",
            (cutoff,),
        )
        stale_epochs = conn.execute(
            "SELECT principal_digest,session_id FROM agent_conversation_epochs "
            "ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (max(self.max_total_sessions * 2, 1000),),
        ).fetchall()
        if stale_epochs:
            conn.executemany(
                "DELETE FROM agent_conversation_epochs "
                "WHERE principal_digest=? AND session_id=?",
                (
                    (str(item["principal_digest"]), str(item["session_id"]))
                    for item in stale_epochs
                ),
            )

    def list_sessions(self, *, principal: str, limit: int = 24) -> list[dict[str, Any]]:
        principal_digest = self._principal_digest(principal)
        bounded = max(1, min(int(limit), self.max_sessions))
        with db.get_conn() as conn:
            self._prune_global(conn, now_value=db.now())
            rows = conn.execute(
                "SELECT session_id,title,message_count,created_at,updated_at "
                "FROM agent_conversations WHERE principal_digest=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (principal_digest, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(
        self, *, principal: str, session_id: str, limit: int = 120
    ) -> dict[str, Any] | None:
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        bounded = max(2, min(int(limit), self.max_messages))
        with db.get_conn() as conn:
            self._prune_global(conn, now_value=db.now())
            conversation = conn.execute(
                "SELECT id,session_id,title,message_count,created_at,updated_at "
                "FROM agent_conversations WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if conversation is None:
                return None
            rows = conn.execute(
                "SELECT role,payload,created_at FROM agent_conversation_messages "
                "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (int(conversation["id"]), bounded),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            role = str(row["role"] or "")
            data = self._decode(
                principal_digest=principal_digest,
                session_id=session_key,
                role=role,
                encoded=str(row["payload"] or ""),
            )
            if data is not None:
                messages.append({"role": role, "data": data, "created_at": str(row["created_at"] or "")})
        result = dict(conversation)
        result.pop("id", None)
        result["messages"] = messages
        return result

    def get_llm_context(
        self, *, principal: str, session_id: str, tail_limit: int = 10
    ) -> list[dict[str, Any]]:
        """读取“已验签滚动摘要 + 最近消息”，供 Web 与 Telegram 共用。"""
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        bounded = max(2, min(int(tail_limit), 20))
        with db.get_conn() as conn:
            conversation = conn.execute(
                "SELECT id FROM agent_conversations "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if conversation is None:
                return []
            conversation_id = int(conversation["id"])
            generation_row = conn.execute(
                "SELECT generation FROM agent_conversation_epochs "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            summary_row = conn.execute(
                "SELECT payload,through_message_id,revision "
                "FROM agent_conversation_summaries WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            summary_entry: dict[str, Any] | None = None
            through_message_id = 0
            if summary_row is not None and generation_row is not None:
                summary_data = self._decode(
                    principal_digest=principal_digest,
                    session_id=session_key,
                    role=self._summary_role(
                        conversation_id=conversation_id,
                        generation=int(generation_row["generation"]),
                        through_message_id=int(summary_row["through_message_id"]),
                        revision=int(summary_row["revision"]),
                    ),
                    encoded=str(summary_row["payload"] or ""),
                    max_bytes=self.max_summary_payload_bytes,
                )
                normalized_summary, stored_media_context = (
                    self._stored_summary_projection(summary_data)
                )
                rendered = render_conversation_summary(normalized_summary)
                if rendered:
                    summary_entry = {"role": "summary", "text": rendered}
                    if stored_media_context:
                        summary_entry["media_context"] = stored_media_context
                    through_message_id = int(summary_row["through_message_id"])
            compacted_assistant_rows: list[Any] = []
            if summary_entry is not None and through_message_id > 0:
                # 摘要文本只保存自然语言目标/事实。为让“这部剧”“重试”在
                # 压缩后仍能继承最近一次已核验媒体身份，从已签名历史中回看
                # 少量已压缩 assistant 投影；只提取安全 media_context，不回填正文。
                compacted_assistant_rows = conn.execute(
                    "SELECT id,role,payload,created_at FROM agent_conversation_messages "
                    "WHERE conversation_id=? AND id<=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 64",
                    (conversation_id, through_message_id),
                ).fetchall()
            rows = conn.execute(
                "SELECT id,role,payload,created_at FROM agent_conversation_messages "
                "WHERE conversation_id=? AND id>? ORDER BY id DESC LIMIT ?",
                (conversation_id, through_message_id, bounded),
            ).fetchall()

        if summary_entry is not None and not summary_entry.get("media_context"):
            for row in compacted_assistant_rows:
                entry = self._context_entry_from_row(
                    row,
                    principal_digest=principal_digest,
                    session_id=session_key,
                )
                media_context = entry.get("media_context") if entry else None
                if isinstance(media_context, dict) and media_context:
                    summary_entry["media_context"] = media_context
                    break

        context: list[dict[str, Any]] = []
        if summary_entry is not None:
            context.append(summary_entry)
        for row in reversed(rows):
            entry = self._context_entry_from_row(
                row,
                principal_digest=principal_digest,
                session_id=session_key,
            )
            if entry is not None:
                context.append(entry)
        return context

    def prepare_compaction(
        self,
        *,
        principal: str,
        session_id: str,
        first_trigger_messages: int = 12,
        refresh_messages: int = 8,
        tail_messages: int = 8,
        max_chunk_messages: int = 16,
    ) -> ConversationCompactionSnapshot | None:
        """创建有界摘要快照；慢速 LLM 调用必须在事务外完成。"""
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        first_trigger = max(4, min(int(first_trigger_messages), self.max_messages))
        refresh = max(2, min(int(refresh_messages), 40))
        tail = max(2, min(int(tail_messages), 20))
        chunk_limit = max(2, min(int(max_chunk_messages), 32))
        with db.get_conn() as conn:
            conversation = conn.execute(
                "SELECT id FROM agent_conversations "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if conversation is None:
                return None
            conversation_id = int(conversation["id"])
            generation_row = conn.execute(
                "SELECT generation FROM agent_conversation_epochs "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if generation_row is None:
                return None
            summary_row = conn.execute(
                "SELECT payload,through_message_id,revision "
                "FROM agent_conversation_summaries WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            previous_summary: dict[str, Any] | None = None
            through_message_id = 0
            revision = 0
            if summary_row is not None:
                revision = int(summary_row["revision"])
                through_message_id = int(summary_row["through_message_id"])
                decoded = self._decode(
                    principal_digest=principal_digest,
                    session_id=session_key,
                    role=self._summary_role(
                        conversation_id=conversation_id,
                        generation=int(generation_row["generation"]),
                        through_message_id=through_message_id,
                        revision=revision,
                    ),
                    encoded=str(summary_row["payload"] or ""),
                    max_bytes=self.max_summary_payload_bytes,
                )
                previous_summary, previous_media_context = (
                    self._stored_summary_projection(decoded)
                )
                if previous_summary is None:
                    return None
            else:
                previous_media_context = {}
            rows = conn.execute(
                "SELECT id,role,payload,created_at FROM agent_conversation_messages "
                "WHERE conversation_id=? AND id>? ORDER BY id ASC",
                (conversation_id, through_message_id),
            ).fetchall()

        if previous_summary is None and len(rows) < first_trigger:
            return None
        eligible = list(rows[:-tail]) if len(rows) > tail else []
        if not eligible:
            return None
        if previous_summary is not None and len(eligible) < refresh:
            return None
        eligible = eligible[:chunk_limit]
        messages: list[dict[str, Any]] = []
        latest_media_context = dict(previous_media_context)
        for row in eligible:
            entry = self._context_entry_from_row(
                row,
                principal_digest=principal_digest,
                session_id=session_key,
            )
            if entry is None:
                return None
            messages.append(entry)
            media_context = entry.get("media_context")
            if isinstance(media_context, dict) and media_context:
                latest_media_context = dict(media_context)
        return ConversationCompactionSnapshot(
            conversation_id=conversation_id,
            expected_generation=int(generation_row["generation"]),
            expected_revision=revision,
            through_message_id=int(eligible[-1]["id"]),
            previous_summary=previous_summary,
            latest_media_context=latest_media_context or None,
            messages=tuple(messages),
        )

    def store_compaction_summary(
        self,
        *,
        principal: str,
        session_id: str,
        snapshot: ConversationCompactionSnapshot,
        summary: dict[str, Any],
    ) -> bool:
        """使用 generation + conversation id + revision CAS 保存滚动摘要。"""
        normalized = normalize_conversation_summary(summary)
        if normalized is None:
            return False
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        now_value = db.now()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            generation_row = conn.execute(
                "SELECT generation FROM agent_conversation_epochs "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if (
                generation_row is None
                or int(generation_row["generation"]) != snapshot.expected_generation
            ):
                return False
            conversation = conn.execute(
                "SELECT id FROM agent_conversations "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            ).fetchone()
            if (
                conversation is None
                or int(conversation["id"]) != snapshot.conversation_id
            ):
                return False
            current = conn.execute(
                "SELECT revision,through_message_id,created_at "
                "FROM agent_conversation_summaries WHERE conversation_id=?",
                (snapshot.conversation_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            current_through = int(current["through_message_id"]) if current is not None else 0
            if (
                current_revision != snapshot.expected_revision
                or snapshot.through_message_id <= current_through
            ):
                return False
            next_revision = current_revision + 1
            encoded = self._encode(
                principal_digest=principal_digest,
                session_id=session_key,
                role=self._summary_role(
                    conversation_id=snapshot.conversation_id,
                    generation=snapshot.expected_generation,
                    through_message_id=snapshot.through_message_id,
                    revision=next_revision,
                ),
                data={
                    "storage_version": _SUMMARY_STORAGE_VERSION,
                    "summary": normalized,
                    "media_context": snapshot.latest_media_context or {},
                },
                max_bytes=self.max_summary_payload_bytes,
            )
            created_at = str(current["created_at"]) if current is not None else now_value
            conn.execute(
                "INSERT INTO agent_conversation_summaries("
                "conversation_id,payload,through_message_id,revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "payload=excluded.payload,"
                "through_message_id=excluded.through_message_id,"
                "revision=excluded.revision,updated_at=excluded.updated_at",
                (
                    snapshot.conversation_id,
                    encoded,
                    snapshot.through_message_id,
                    next_revision,
                    created_at,
                    now_value,
                ),
            )
            return True

    def _stored_summary_projection(
        self, value: Any
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """验证当前摘要载荷及其独立媒体身份锚点。"""
        if (
            not isinstance(value, dict)
            or set(value) != {"storage_version", "summary", "media_context"}
            or value.get("storage_version") != _SUMMARY_STORAGE_VERSION
        ):
            return None, {}
        summary = normalize_conversation_summary(value.get("summary"))
        media_context = self._validated_media_context(value.get("media_context"))
        if summary is None or media_context is None:
            return None, {}
        return summary, media_context

    def _validated_media_context(
        self, value: Any
    ) -> dict[str, Any] | None:
        if value in (None, {}):
            return {}
        if not isinstance(value, dict) or not set(value).issubset(
            {
                "title", "original_title", "year", "media_type",
                "tmdb_id", "bangumi_id", "douban_id", "season", "episode",
                "case_stage",
            }
        ):
            return None
        title = self._safe_optional_output_text(value.get("title"), limit=160)
        if not title or title == _UNSAFE_HISTORY_DETAIL:
            return None
        result: dict[str, Any] = {"title": title}
        original_title = self._safe_optional_output_text(
            value.get("original_title"), limit=160
        )
        if original_title and original_title != _UNSAFE_HISTORY_DETAIL:
            result["original_title"] = original_title
        year = str(value.get("year") or "").strip()
        if year:
            if not _MEDIA_CONTEXT_YEAR_RE.fullmatch(year):
                return None
            result["year"] = year
        media_type = str(value.get("media_type") or "").strip().lower()
        if media_type:
            if media_type not in {"movie", "tv"}:
                return None
            result["media_type"] = media_type
        for field, maximum_digits in (("tmdb_id", 10), ("bangumi_id", 10), ("douban_id", 20)):
            identifier = str(value.get(field) or "").strip()
            if identifier:
                if not identifier.isascii() or not identifier.isdigit() or len(identifier) > maximum_digits:
                    return None
                result[field] = identifier
        for field, maximum in (("season", 100), ("episode", 1000)):
            coordinate = value.get(field)
            if coordinate is not None:
                if isinstance(coordinate, bool) or not isinstance(coordinate, int) or not 1 <= coordinate <= maximum:
                    return None
                result[field] = coordinate
        case_stage = normalize_media_case_stage(value.get("case_stage"))
        if value.get("case_stage") not in (None, "") and not case_stage:
            return None
        if case_stage:
            result["case_stage"] = case_stage
        return result

    def _media_context_projection(
        self,
        *,
        tool_name: str,
        data: Any,
        arguments: Any = None,
        ok: bool = True,
        status: str = "",
    ) -> dict[str, Any]:
        """仅保留不可执行、可验签的媒体身份，供自然续问使用。"""
        normalized_tool = str(tool_name or "").strip()
        if normalized_tool not in _MEDIA_CONTEXT_TOOL_TYPES:
            return {}
        safe_data = data if isinstance(data, dict) else {}
        normalized_status = str(status or "").strip().lower()
        # 失败响应即使回显了 query/title，也只是未核验的请求信息，不能污染
        # “这部剧 / 重试 / 下载第 N 集”的结构化上下文。
        if not bool(ok) or normalized_status in {
            "not_found",
            "empty",
            "error",
            "failed",
            "unavailable",
            "clarification_required",
            "cancelled",
            "timeout",
        }:
            return {}
        safe_arguments = arguments if isinstance(arguments, dict) else {}
        verification = safe_data.get("verification")
        safe_verification = verification if isinstance(verification, dict) else {}
        raw_items = safe_data.get("items")
        safe_item = (
            raw_items[0]
            if isinstance(raw_items, list) and len(raw_items) == 1 and isinstance(raw_items[0], dict)
            else {}
        )
        title = self._safe_optional_output_text(
            safe_data.get("title")
            or safe_verification.get("title")
            or safe_item.get("title")
            or safe_data.get("query")
            or safe_arguments.get("title")
            or safe_arguments.get("query"),
            limit=160,
        )
        if not title or title == _UNSAFE_HISTORY_DETAIL:
            return {}
        result: dict[str, Any] = {"title": title}
        original_title = self._safe_optional_output_text(
            safe_data.get("original_title")
            or safe_verification.get("original_title")
            or safe_item.get("original_title")
            or safe_arguments.get("original_title"),
            limit=160,
        )
        if original_title and original_title != _UNSAFE_HISTORY_DETAIL:
            result["original_title"] = original_title
        year = str(
            safe_data.get("year") or safe_verification.get("year")
            or safe_item.get("year") or safe_arguments.get("year") or ""
        ).strip()
        if _MEDIA_CONTEXT_YEAR_RE.fullmatch(year):
            result["year"] = year
        forced_type = _MEDIA_CONTEXT_TOOL_TYPES[normalized_tool]
        media_type = forced_type or str(
            safe_data.get("media_type") or safe_arguments.get("media_type") or ""
        ).strip().lower()
        if media_type in {"movie", "tv"}:
            result["media_type"] = media_type
        sources = (safe_data, safe_verification, safe_item, safe_arguments)
        for field, maximum_digits in (
            ("tmdb_id", 10),
            ("bangumi_id", 10),
            ("douban_id", 20),
        ):
            identifier = next(
                (
                    candidate
                    for source in sources
                    if (
                        candidate := str(source.get(field) or "").strip()
                    ).isascii()
                    and candidate.isdigit()
                    and 1 <= len(candidate) <= maximum_digits
                ),
                "",
            )
            if identifier:
                result[field] = identifier
        coordinate_aliases = {
            "season": ("season",),
            "episode": ("episode", "target_episode"),
        }
        for field, aliases in coordinate_aliases.items():
            maximum = 100 if field == "season" else 1000
            coordinate = next(
                (
                    candidate
                    for source in sources
                    for alias in aliases
                    if isinstance((candidate := source.get(alias)), int)
                    and not isinstance(candidate, bool)
                    and 1 <= candidate <= maximum
                ),
                None,
            )
            if coordinate is not None:
                result[field] = coordinate
        case_stage = media_case_stage_for_tool(normalized_tool)
        if case_stage:
            result["case_stage"] = case_stage
        return result

    def _tentative_media_context_projection(
        self,
        *,
        tool_name: str,
        data: Any,
        arguments: Any = None,
        status: str = "",
    ) -> dict[str, Any]:
        """保留用户明确输入但尚未由当前数据源命中的安全媒体标题。"""
        if str(status or "").strip().lower() not in {"empty", "not_found"}:
            return {}
        return self._media_context_projection(
            tool_name=tool_name,
            data=data,
            arguments=arguments,
            ok=True,
            status="success",
        )

    @staticmethod
    def _validated_pending_selection(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or not set(value).issubset({"position", "target"}):
            return None
        position = value.get("position")
        if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= 20:
            return None
        result: dict[str, Any] = {"position": position}
        target = str(value.get("target") or "").strip().lower()
        if target:
            if target not in {"qb", "guangya", "both"}:
                return None
            result["target"] = target
        return result

    @staticmethod
    def _validated_pending_subscription(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict) or set(value) != {"season"}:
            return None
        season = value.get("season")
        if isinstance(season, bool) or not isinstance(season, int) or not 1 <= season <= 100:
            return None
        return {"season": season}

    def _context_entry_from_row(
        self,
        row: Any,
        *,
        principal_digest: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        role = str(row["role"] or "").strip().lower()
        if role not in {"user", "assistant"}:
            return None
        data = self._decode(
            principal_digest=principal_digest,
            session_id=session_id,
            role=role,
            encoded=str(row["payload"] or ""),
        )
        if data is None:
            return None
        text = (
            data.get("text")
            if role == "user"
            else data.get("narrative") or data.get("summary")
        )
        normalized = " ".join(str(text or "").split()).strip()
        if not normalized or self._contains_unsafe_history_text(normalized):
            return None
        entry: dict[str, Any] = {"role": role, "text": normalized[:600]}
        if role == "assistant":
            tool_name = str(data.get("tool_name") or "").strip()[:120]
            status = str(data.get("status") or "").strip()[:64]
            suggestions = data.get("suggestions")
            media_context = self._media_context_projection(
                tool_name=tool_name,
                data=data.get("media_context"),
            )
            tentative_media_context = self._tentative_media_context_projection(
                tool_name=tool_name,
                data=data.get("tentative_media_context"),
                status="empty",
            )
            pending_selection = self._validated_pending_selection(
                data.get("pending_selection")
            )
            pending_subscription = self._validated_pending_subscription(
                data.get("pending_subscription")
            )
            if tool_name:
                entry["tool_name"] = tool_name
            if media_context:
                entry["media_context"] = media_context
            if tentative_media_context:
                entry["tentative_media_context"] = tentative_media_context
            if pending_selection is not None:
                entry["pending_selection"] = pending_selection
            if pending_subscription is not None:
                entry["pending_subscription"] = pending_subscription
            if status:
                entry["status"] = status
            context_domain = str(data.get("context_domain") or "").strip()
            if context_domain in _SAFE_CONTEXT_DOMAINS:
                entry["context_domain"] = context_domain
            context_domains = data.get("context_domains")
            if isinstance(context_domains, list):
                safe_domains = sorted({
                    str(value or "").strip()
                    for value in context_domains[:4]
                    if str(value or "").strip() in _SAFE_CONTEXT_TOPICS
                })
                if safe_domains:
                    entry["context_domains"] = safe_domains
            if isinstance(suggestions, list):
                entry["suggestions"] = [
                    value
                    for raw in suggestions[:3]
                    if isinstance(raw, str)
                    and (value := " ".join(raw.split()).strip()[:180])
                    and not self._contains_unsafe_history_text(value)
                ]
        return entry

    def session_generation(self, *, principal: str, session_id: str) -> int:
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        now_value = db.now()
        with db.get_conn() as conn:
            self._prune_global(conn, now_value=now_value)
            return self._session_generation_locked(
                conn,
                principal_digest=principal_digest,
                session_id=session_key,
                now_value=now_value,
            )

    def delete_session(self, *, principal: str, session_id: str) -> bool:
        principal_digest = self._principal_digest(principal)
        session_key = self._session_id(session_id)
        now_value = db.now()
        next_generation = self._new_generation()
        with db.get_conn() as conn:
            self._prune_global(conn, now_value=now_value)
            conn.execute(
                "INSERT INTO agent_conversation_epochs("
                "principal_digest,session_id,generation,updated_at"
                ") VALUES(?,?,?,?) "
                "ON CONFLICT(principal_digest,session_id) DO UPDATE SET "
                "generation=excluded.generation,updated_at=excluded.updated_at",
                (principal_digest, session_key, next_generation, now_value),
            )
            cursor = conn.execute(
                "DELETE FROM agent_conversations WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_key),
            )
            return cursor.rowcount > 0

    def _session_generation_locked(
        self,
        conn: Any,
        *,
        principal_digest: str,
        session_id: str,
        now_value: str,
    ) -> int:
        row = conn.execute(
            "SELECT generation FROM agent_conversation_epochs "
            "WHERE principal_digest=? AND session_id=?",
            (principal_digest, session_id),
        ).fetchone()
        if row is None:
            generation = self._new_generation()
            conn.execute(
                "INSERT OR IGNORE INTO agent_conversation_epochs("
                "principal_digest,session_id,generation,updated_at"
                ") VALUES(?,?,?,?)",
                (principal_digest, session_id, generation, now_value),
            )
            row = conn.execute(
                "SELECT generation FROM agent_conversation_epochs "
                "WHERE principal_digest=? AND session_id=?",
                (principal_digest, session_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Agent 会话 epoch 创建失败")
        conn.execute(
            "UPDATE agent_conversation_epochs SET updated_at=? "
            "WHERE principal_digest=? AND session_id=?",
            (now_value, principal_digest, session_id),
        )
        return int(row["generation"])

    @staticmethod
    def _new_generation() -> int:
        return secrets.randbits(63) or 1

    def principal_digest_for_tests(self, principal: str) -> str:
        return self._principal_digest(principal)

    @staticmethod
    def _summary_role(
        *,
        conversation_id: int,
        generation: int,
        through_message_id: int,
        revision: int,
    ) -> str:
        """把摘要绑定到具体会话实例，阻止删除后跨代重放。"""
        return (
            "summary:v2:"
            f"{int(conversation_id)}:{int(generation)}:"
            f"{int(through_message_id)}:{int(revision)}"
        )

    def _principal_digest(self, principal: str) -> str:
        normalized = str(principal or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("Agent 历史主体无效")
        secret = str(self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent 历史指纹密钥不可用")
        return hmac.new(
            secret.encode("utf-8"),
            b"mediaflux-agent-conversation-principal:v1\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _session_id(value: str) -> str:
        normalized = str(value or "").strip()
        if not _SESSION_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Agent 历史 session_id 无效")
        return normalized

    @staticmethod
    def _text(value: Any, *, limit: int, label: str) -> str:
        normalized = " ".join(str(value or "").split()).strip()
        if not normalized:
            raise ValueError(f"Agent 历史 {label} 为空")
        return normalized[:limit]

    @staticmethod
    def _title(message: str) -> str:
        single_line = " ".join(str(message or "").split()).strip()
        return single_line if len(single_line) <= 48 else f"{single_line[:47].rstrip()}…"

    @staticmethod
    def _validated_usage(value: Any) -> dict[str, int] | None:
        """只保存 Provider 明确返回且内部一致的有界 usage。"""
        if not isinstance(value, dict):
            return None
        allowed = {
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        }
        required = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if not required.issubset(value) or not set(value).issubset(allowed):
            return None
        normalized: dict[str, int] = {}
        for key in allowed:
            raw = value.get(key, 0)
            maximum = 4_000_000 if key == "total_tokens" else 2_000_000
            if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= maximum:
                return None
            normalized[key] = raw
        if normalized["total_tokens"] < (
            normalized["prompt_tokens"] + normalized["completion_tokens"]
        ):
            return None
        return normalized

    def _assistant_projection(self, response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ValueError("Agent 历史响应无效")
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        tool_call = response.get("tool_call") if isinstance(response.get("tool_call"), dict) else {}
        suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
        safe_suggestions = [
            self._safe_output_text(item, limit=180, label="suggestion")
            for item in suggestions[:4]
            if isinstance(item, str) and item.strip()
        ]
        tool_name = str(tool_call.get("name") or "")[:120]
        projection: dict[str, Any] = {
            "mode": self._text(response.get("mode") or "read_only", limit=48, label="mode"),
            "tool_name": tool_name,
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or "unknown")[:64],
            "summary": self._safe_output_text(
                result.get("summary") or "Agent 已返回结果", limit=600, label="summary"
            ),
            "error": self._safe_optional_output_text(result.get("error"), limit=300),
            "suggestions": safe_suggestions,
        }
        context_domain = str(response.get("context_domain") or "").strip()
        if context_domain in _SAFE_CONTEXT_DOMAINS:
            projection["context_domain"] = context_domain
        context_domains = response.get("context_domains")
        if isinstance(context_domains, list):
            safe_domains = sorted({
                str(value or "").strip()
                for value in context_domains[:4]
                if str(value or "").strip() in _SAFE_CONTEXT_TOPICS
            })
            if safe_domains:
                projection["context_domains"] = safe_domains
        presentation = (
            response.get("presentation")
            if isinstance(response.get("presentation"), dict)
            else {}
        )
        if (
            presentation.get("source") == "llm"
            and presentation.get("kind") == "narrative"
        ):
            narrative = self._safe_optional_output_text(
                presentation.get("narrative"), limit=1200
            )
            if narrative:
                projection["narrative"] = narrative
        usage = self._validated_usage(response.get("llm_usage"))
        if usage is not None:
            projection["usage"] = usage
        media_context = self._media_context_projection(
            tool_name=tool_name,
            data=result.get("data"),
            arguments=tool_call.get("arguments"),
            ok=bool(result.get("ok")),
            status=str(result.get("status") or ""),
        )
        if media_context:
            projection["media_context"] = media_context
        else:
            tentative_media_context = self._tentative_media_context_projection(
                tool_name=tool_name,
                data=result.get("data"),
                arguments=tool_call.get("arguments"),
                status=str(result.get("status") or ""),
            )
            if tentative_media_context:
                projection["tentative_media_context"] = tentative_media_context
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if (
            tool_name == "indexer.submit_resource"
            and str(result.get("status") or "").strip().lower() == "selection_required"
        ):
            pending_selection = self._validated_pending_selection(
                result_data.get("pending_selection")
            )
            if pending_selection is not None:
                projection["pending_selection"] = pending_selection
        pending_subscription = self._validated_pending_subscription(
            result_data.get("pending_subscription")
        )
        if pending_subscription is not None:
            projection["pending_subscription"] = pending_subscription
        return projection

    @staticmethod
    def _contains_unsafe_history_text(value: str) -> bool:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        # 最多解码两层常见百分号编码；历史是辅助能力，宁可不存也不冒险落盘。
        for _ in range(2):
            decoded = unquote(normalized)
            if decoded == normalized:
                break
            normalized = decoded
        return (
            contains_sensitive_credential(normalized)
            or bool(_HISTORY_CREDENTIAL_RE.search(normalized))
            or bool(_UNSAFE_OUTPUT_RE.search(normalized))
        )

    @staticmethod
    def _normalize_multiline_text(value: Any, *, limit: int) -> str:
        raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        if _HISTORY_MULTILINE_CONTROL_RE.search(raw):
            return ""
        lines: list[str] = []
        previous_blank = False
        for raw_line in raw.split("\n"):
            line = " ".join(raw_line.split()).strip()
            if not line:
                if lines and not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            previous_blank = False
            lines.append(line)
        return "\n".join(lines).strip()[:max(1, int(limit))].rstrip()

    def _safe_output_text(self, value: Any, *, limit: int, label: str) -> str:
        raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if self._contains_unsafe_history_text(raw):
            return _UNSAFE_HISTORY_DETAIL
        normalized = self._normalize_multiline_text(raw, limit=limit)
        if not normalized:
            raise ValueError(f"Agent 历史 {label} 为空")
        return normalized

    def _safe_optional_output_text(self, value: Any, *, limit: int) -> str:
        raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        if self._contains_unsafe_history_text(raw):
            return _UNSAFE_HISTORY_DETAIL
        return self._normalize_multiline_text(raw, limit=limit)

    def _encode_assistant_projection(
        self,
        *,
        principal_digest: str,
        session_id: str,
        data: dict[str, Any],
    ) -> str:
        """按真实 UTF-8 包络大小裁剪可选字段，确保安全历史不会静默丢轮。"""
        projection = dict(data)

        def encode_or_none() -> str | None:
            try:
                return self._encode(
                    principal_digest=principal_digest,
                    session_id=session_id,
                    role="assistant",
                    data=projection,
                )
            except ValueError as exc:
                if str(exc) != "Agent 历史消息过大":
                    raise
                return None

        encoded = encode_or_none()
        if encoded is not None:
            return encoded

        narrative = projection.pop("narrative", None)

        # 先让原有安全投影落入预算：建议从尾部裁剪，摘要最后才缩短。
        suggestions = projection.get("suggestions")
        if isinstance(suggestions, list):
            while suggestions and encode_or_none() is None:
                suggestions = suggestions[:-1]
                projection["suggestions"] = suggestions

        for optional_key in ("error", "usage"):
            if encode_or_none() is not None:
                break
            projection.pop(optional_key, None)

        if encode_or_none() is None:
            summary = str(projection.get("summary") or "Agent 已返回结果")
            low, high, best = 1, len(summary), ""
            while low <= high:
                middle = (low + high) // 2
                projection["summary"] = summary[:middle].rstrip() or "结果"
                if encode_or_none() is not None:
                    best = projection["summary"]
                    low = middle + 1
                else:
                    high = middle - 1
            projection["summary"] = best or "结果已精简"

        # 极端上下文投影仍必须可落盘；这些字段丢失只影响 follow-up 增强，
        # 不包含可重放参数，且下一轮仍可重新查询。
        for optional_key in (
            "tentative_media_context",
            "pending_selection",
            "pending_subscription",
            "media_context",
            "context_domains",
        ):
            if encode_or_none() is not None:
                break
            projection.pop(optional_key, None)

        encoded = encode_or_none()
        if encoded is None:
            projection["tool_name"] = str(projection.get("tool_name") or "")[:32]
            projection["status"] = str(projection.get("status") or "unknown")[:24]
            projection["mode"] = str(projection.get("mode") or "read_only")[:24]
            encoded = encode_or_none()
        if encoded is None:
            # max_payload_bytes 最低为 1024；该固定最小投影应始终可编码。
            projection = {
                "mode": "read_only",
                "tool_name": "",
                "ok": bool(data.get("ok")),
                "status": "truncated",
                "summary": "结果已精简",
                "error": "",
                "suggestions": [],
            }
            encoded = self._encode(
                principal_digest=principal_digest,
                session_id=session_id,
                role="assistant",
                data=projection,
            )

        if not isinstance(narrative, str) or not narrative:
            return encoded

        def fit_narrative_prefix() -> tuple[str, str | None]:
            projection.pop("narrative", None)
            low, high, best = 1, len(narrative), ""
            best_encoded: str | None = None
            while low <= high:
                middle = (low + high) // 2
                candidate = narrative[:middle].rstrip()
                if not candidate:
                    low = middle + 1
                    continue
                projection["narrative"] = candidate
                candidate_encoded = encode_or_none()
                if candidate_encoded is not None:
                    best = candidate
                    best_encoded = candidate_encoded
                    low = middle + 1
                else:
                    high = middle - 1
            if best:
                projection["narrative"] = best
            else:
                projection.pop("narrative", None)
            return best, best_encoded

        best, best_encoded = fit_narrative_prefix()
        if best_encoded is not None:
            return best_encoded

        # 若包络刚好占满，牺牲最后一条建议，为自然叙事至少留出空间。
        suggestions = projection.get("suggestions")
        if isinstance(suggestions, list) and suggestions:
            projection["suggestions"] = suggestions[:-1]
            baseline = encode_or_none()
            if baseline is not None:
                best, best_encoded = fit_narrative_prefix()
                if best_encoded is not None:
                    return best_encoded
                return baseline

        return encoded

    def _encode(
        self,
        *,
        principal_digest: str,
        session_id: str,
        role: str,
        data: dict[str, Any],
        max_bytes: int | None = None,
    ) -> str:
        auth = self._auth_tag(
            principal_digest=principal_digest, session_id=session_id, role=role, data=data
        )
        encoded = json.dumps(
            {"version": _SCHEMA_VERSION, "data": data, "auth": auth},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_limit = self.max_payload_bytes if max_bytes is None else int(max_bytes)
        if len(encoded.encode("utf-8")) > payload_limit:
            raise ValueError("Agent 历史消息过大")
        return encoded

    def _decode(
        self,
        *,
        principal_digest: str,
        session_id: str,
        role: str,
        encoded: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        payload_limit = self.max_payload_bytes if max_bytes is None else int(max_bytes)
        if not encoded or len(encoded.encode("utf-8")) > payload_limit:
            return None
        try:
            envelope = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"version", "data", "auth"}
            or envelope.get("version") != _SCHEMA_VERSION
            or not isinstance(envelope.get("data"), dict)
            or not isinstance(envelope.get("auth"), str)
        ):
            return None
        expected = self._auth_tag(
            principal_digest=principal_digest,
            session_id=session_id,
            role=role,
            data=envelope["data"],
        )
        if not hmac.compare_digest(str(envelope["auth"]), expected):
            return None
        return envelope["data"]

    def _auth_tag(self, *, principal_digest: str, session_id: str, role: str, data: dict[str, Any]) -> str:
        secret = str(self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent 历史签名密钥不可用")
        canonical = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        message = "\0".join((
            "mediaflux-agent-conversation-record:v1",
            principal_digest,
            session_id,
            role,
            canonical,
        )).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


_repository = SQLiteAgentConversationHistoryRepository()


def get_agent_conversation_history_repository() -> SQLiteAgentConversationHistoryRepository:
    return _repository
