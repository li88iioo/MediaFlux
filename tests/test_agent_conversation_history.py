"""Media Agent 安全会话摘要持久化与 API 回归测试。"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.conversation_history import SQLiteAgentConversationHistoryRepository
from app.agent.operation_coordinator import reset_agent_operation_state_for_tests
from app.agent.registry import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.config import web_credentials
from app.main import create_app
from app.routes.agent_api import _public_session_projection
from tests.support import IsolatedDatabaseTestCase


SESSION_A = "agent_session_history_0001"
SESSION_B = "agent_session_history_0002"


def _response(*, summary: str = "媒体库检查完成") -> dict:
    return {
        "request_id": "request-secret-not-persisted",
        "mode": "tool_result",
        "tool_call": {
            "name": "library.audit_episodes",
            "arguments": {"path": "/media/private", "token": "do-not-store"},
            "elapsed_ms": 12,
        },
        "confirmation_id": "confirmation-secret-not-persisted",
        "result": {
            "ok": True,
            "status": "success",
            "summary": summary,
            "error": "",
            "suggestions": ["继续检查更新", {"secret": "do-not-store"}],
            "data": {
                "title": "九门",
                "original_title": "Jiu Men",
                "year": "2026",
                "media_type": "movie",
                "magnet": "magnet:?xt=urn:btih:secret",
                "result_id": "private-result-id",
                "path": "/media/private",
            },
        },
    }


class AgentConversationHistoryRepositoryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_conversation_messages")
            conn.execute("DELETE FROM agent_conversations")
            conn.execute("DELETE FROM agent_conversation_epochs")
        self.repository = SQLiteAgentConversationHistoryRepository(
            secret_provider=lambda: "history-test-secret",
            max_sessions=2,
            max_messages=4,
        )

    def test_history_is_owner_scoped_signed_and_uses_safe_projection(self):
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="检查《黑镜》有没有缺集",
            response=_response(),
        )

        sessions = self.repository.list_sessions(principal="browser-principal-a")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], SESSION_A)
        self.assertEqual(sessions[0]["message_count"], 2)
        self.assertEqual(
            self.repository.list_sessions(principal="browser-principal-b"), []
        )

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertIsNotNone(history)
        self.assertEqual([item["role"] for item in history["messages"]], ["user", "assistant"])
        assistant = history["messages"][1]["data"]
        self.assertEqual(assistant["tool_name"], "library.audit_episodes")
        self.assertEqual(assistant["summary"], "媒体库检查完成")
        self.assertEqual(assistant["suggestions"], ["继续检查更新"])
        self.assertEqual(assistant["media_context"], {
            "title": "九门",
            "original_title": "Jiu Men",
            "year": "2026",
            "media_type": "tv",
        })
        self.assertNotIn("arguments", assistant)
        self.assertNotIn("confirmation_id", assistant)

        with db.get_conn() as conn:
            raw = "\n".join(
                str(row["payload"])
                for row in conn.execute(
                    "SELECT payload FROM agent_conversation_messages ORDER BY id"
                ).fetchall()
            )
            principals = "\n".join(
                str(row["principal_digest"])
                for row in conn.execute(
                    "SELECT principal_digest FROM agent_conversations"
                ).fetchall()
            )
        self.assertNotIn("browser-principal-a", principals)
        for forbidden in (
            "confirmation-secret-not-persisted",
            "request-secret-not-persisted",
            "magnet:?",
            "/media/private",
            "do-not-store",
            "private-result-id",
            '"arguments"',
        ):
            self.assertNotIn(forbidden, raw)

    def test_multiline_narrative_is_persisted_and_restored_safely(self):
        response = _response(summary="底层工具摘要")
        response["presentation"] = {
            "source": "llm",
            "kind": "narrative",
            "narrative": "第一段直接回答。\n\n- 第二段保留换行",
            "guidance": [],
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="第一行问题\n第二行补充",
            response=response,
        )

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(history["title"], "第一行问题 第二行补充")
        self.assertEqual(
            history["messages"][0]["data"]["text"],
            "第一行问题\n第二行补充",
        )
        assistant = history["messages"][1]["data"]
        self.assertEqual(
            assistant["narrative"],
            "第一段直接回答。\n\n- 第二段保留换行",
        )
        projected = _public_session_projection(history)
        restored_narrative = projected["messages"][1]["data"]["narrative"]
        self.assertIn("第一段直接回答。", restored_narrative)
        self.assertIn("\n- 第二段保留换行", restored_narrative)
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertIn("第一段直接回答", context[-1]["text"])
        self.assertNotIn("底层工具摘要", context[-1]["text"])

    def test_oversized_multibyte_narrative_is_trimmed_without_losing_turn(self):
        response = _response(summary="摘" * 600)
        response["result"]["suggestions"] = ["建" * 180 for _ in range(4)]
        response["presentation"] = {
            "source": "llm",
            "kind": "narrative",
            "narrative": "答" * 1200,
            "guidance": [],
        }

        self.assertTrue(self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="保存这轮结果",
            response=response,
        ))

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        assistant = history["messages"][1]["data"]
        self.assertEqual(assistant["summary"], "摘" * 600)
        self.assertTrue(assistant.get("narrative"))
        self.assertTrue(assistant["suggestions"])
        self.assertLess(len(assistant["narrative"]), 1200)
        self.assertLess(len(assistant["suggestions"]), 4)
        with db.get_conn() as conn:
            payload = str(conn.execute(
                "SELECT payload FROM agent_conversation_messages "
                "WHERE role='assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload"])
        self.assertLessEqual(len(payload.encode("utf-8")), self.repository.max_payload_bytes)

    def test_plain_urls_in_narrative_never_reach_storage_or_llm_context(self):
        for index, unsafe_uri in enumerate((
            "https://intranet.example/a",
            "ftp://private-host/a",
            "file:///tmp/private-a",
        )):
            with self.subTest(uri=unsafe_uri):
                session_id = f"agent_url_history_{index:02d}"
                response = _response(summary="已完成安全检查")
                response["presentation"] = {
                    "source": "llm",
                    "kind": "narrative",
                    "narrative": f"详情位于 {unsafe_uri}",
                    "guidance": [],
                }
                self.repository.append_query_turn(
                    principal="browser-principal-a",
                    session_id=session_id,
                    message="给我安全结果",
                    response=response,
                )
                history = self.repository.get_session(
                    principal="browser-principal-a", session_id=session_id
                )
                projected = _public_session_projection(history)
                context = self.repository.get_llm_context(
                    principal="browser-principal-a", session_id=session_id
                )
                with db.get_conn() as conn:
                    raw = "\n".join(
                        str(row["payload"])
                        for row in conn.execute(
                            "SELECT payload FROM agent_conversation_messages "
                            "WHERE conversation_id=("
                            "SELECT id FROM agent_conversations "
                            "WHERE session_id=? ORDER BY id DESC LIMIT 1)",
                            (session_id,),
                        ).fetchall()
                    )
                combined = "\n".join((
                    raw,
                    json.dumps(history, ensure_ascii=False),
                    json.dumps(projected, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False),
                ))
                self.assertNotIn(unsafe_uri, combined)
                self.assertIn("[已隐藏敏感详情]", combined)

    def test_sensitive_narrative_is_redacted_from_storage_public_projection_and_context(self):
        response = _response(summary="已完成安全检查")
        response["presentation"] = {
            "source": "llm",
            "kind": "narrative",
            "narrative": (
                "token=super-secret https://private.invalid /private/path "
                "magnet:?xt=urn:btih:private"
            ),
            "guidance": [],
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="给我安全结果",
            response=response,
        )

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        projected = _public_session_projection(history)
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        with db.get_conn() as conn:
            raw = "\n".join(
                str(row["payload"])
                for row in conn.execute(
                    "SELECT payload FROM agent_conversation_messages ORDER BY id"
                ).fetchall()
            )
        combined = "\n".join((
            raw,
            json.dumps(projected, ensure_ascii=False),
            json.dumps(context, ensure_ascii=False),
        ))
        for forbidden in (
            "super-secret",
            "private.invalid",
            "/private/path",
            "magnet:?",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn("[已隐藏敏感详情]", combined)

    def test_usage_is_signed_and_stored_but_not_sent_back_to_llm(self):
        response = _response()
        response["llm_usage"] = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "cached_tokens": 8,
            "reasoning_tokens": 4,
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="检查媒体库",
            response=response,
        )
        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(history["messages"][-1]["data"]["usage"], response["llm_usage"])
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertNotIn("usage", context[-1])

    def test_invalid_or_missing_usage_is_omitted(self):
        for usage in (
            None,
            {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 4},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "secret": 9},
        ):
            with self.subTest(usage=usage):
                response = _response()
                if usage is not None:
                    response["llm_usage"] = usage
                projection = self.repository._assistant_projection(response)
                self.assertNotIn("usage", projection)

    def test_only_allowlisted_context_domain_survives_signed_history_projection(self):
        response = {
            "mode": "clarification",
            "tool_call": None,
            "context_domain": "rss",
            "result": {
                "ok": True,
                "status": "clarification_required",
                "summary": "有多个订阅，请选择一个。",
                "suggestions": ["刷新 Private RSS 订阅。"],
            },
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="刷新一个 RSS 订阅",
            response=response,
        )
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(context[-1]["context_domain"], "rss")

        response["context_domain"] = "downloads"
        projection = self.repository._assistant_projection(response)
        self.assertNotIn("context_domain", projection)

        response["context_domains"] = [
            "rss", "media_subscription", "downloads", "rss"
        ]
        projection = self.repository._assistant_projection(response)
        self.assertEqual(
            projection["context_domains"], ["media_subscription", "rss"]
        )

    def test_empty_media_result_keeps_tentative_context_and_pending_selection(self):
        empty_response = {
            "mode": "tool_result",
            "tool_call": {
                "name": "library.search",
                "arguments": {"query": "沙丘2", "limit": 8},
            },
            "result": {
                "ok": True,
                "status": "empty",
                "summary": "媒体库中没有找到匹配内容",
                "data": {"query": "沙丘2", "total": 0},
                "suggestions": [],
            },
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="媒体库里有《沙丘2》吗",
            response=empty_response,
        )
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(context[-1]["tentative_media_context"], {"title": "沙丘2"})
        self.assertNotIn("media_context", context[-1])

        selection_response = {
            "mode": "tool_result",
            "tool_call": {"name": "indexer.submit_resource", "arguments": {}},
            "result": {
                "ok": False,
                "status": "selection_required",
                "summary": "请选择下载目标",
                "data": {"pending_selection": {"position": 2}},
                "suggestions": [],
            },
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="下载第二个",
            response=selection_response,
        )
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(context[-1]["pending_selection"], {"position": 2})

        subscription_response = {
            "mode": "tool_result",
            "tool_call": {
                "name": "discovery.search",
                "arguments": {"query": "庆余年", "limit": 20},
            },
            "result": {
                "ok": True,
                "status": "completed",
                "summary": "找到候选",
                "data": {
                    "query": "庆余年",
                    "items": [],
                    "pending_subscription": {"season": 2},
                },
                "suggestions": ["订阅第 2 个的第 2 季"],
            },
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="订阅《庆余年》第 2 季",
            response=subscription_response,
        )
        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(context[-1]["pending_subscription"], {"season": 2})

    def test_media_context_preserves_stable_ids_and_uses_later_valid_coordinates(self):
        response = {
            "mode": "tool_result",
            "tool_call": {
                "name": "library.search_missing_episode_resources",
                "arguments": {
                    "title": "九门",
                    "season": 2,
                    "target_episode": 3,
                },
            },
            "result": {
                "ok": True,
                "status": "success",
                "summary": "找到缺集资源",
                "data": {
                    "title": "九门",
                    "original_title": "Jiu Men",
                    "year": "2026",
                    "media_type": "tv",
                    "tmdb_id": "invalid",
                    "episode": "invalid",
                    "verification": {
                        "tmdb_id": "987654",
                        "bangumi_id": "3210",
                        "douban_id": "654321",
                        "season": 2,
                        "episode": 3,
                    },
                },
                "suggestions": ["下载第 1 个"],
            },
        }
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="搜索《九门》第 2 季第 3 集",
            response=response,
        )

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        expected = {
            "title": "九门",
            "original_title": "Jiu Men",
            "year": "2026",
            "media_type": "tv",
            "tmdb_id": "987654",
            "bangumi_id": "3210",
            "douban_id": "654321",
            "season": 2,
            "episode": 3,
        }
        self.assertEqual(history["messages"][1]["data"]["media_context"], expected)

        context = self.repository.get_llm_context(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(context[-1]["media_context"], expected)

    def test_tampered_payload_fails_closed_and_delete_is_owner_scoped(self):
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="检查项目配置",
            response=_response(),
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,payload FROM agent_conversation_messages "
                "WHERE role='assistant' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            envelope = json.loads(row["payload"])
            envelope["data"]["summary"] = "被篡改"
            conn.execute(
                "UPDATE agent_conversation_messages SET payload=? WHERE id=?",
                (json.dumps(envelope, ensure_ascii=False), int(row["id"])),
            )

        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(len(history["messages"]), 1)
        self.assertEqual(history["messages"][0]["role"], "user")
        self.assertFalse(
            self.repository.delete_session(
                principal="browser-principal-b", session_id=SESSION_A
            )
        )
        self.assertTrue(
            self.repository.delete_session(
                principal="browser-principal-a", session_id=SESSION_A
            )
        )
        self.assertIsNone(
            self.repository.get_session(
                principal="browser-principal-a", session_id=SESSION_A
            )
        )

    def test_natural_language_slashes_are_not_mistaken_for_file_paths(self):
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="检查电影/剧集更新，并核对 S01/S02 是否完整",
            response=_response(),
        )

        detail = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertIsNotNone(detail)
        self.assertEqual(
            detail["messages"][0]["data"]["text"],
            "检查电影/剧集更新，并核对 S01/S02 是否完整",
        )

    def test_sensitive_user_input_is_never_persisted(self):
        sensitive_messages = (
            "检查 /media/private/secret.mkv",
            "检查路径:/media/private/secret.mkv",
            "路径：/media/private/secret.mkv",
            "检查 file:///media/private/secret.mkv",
            "检查 %2Fmedia%2Fprivate%2Fsecret.mkv",
            r"检查 C:\private\secret.mkv",
            "下载 magnet:?xt=urn:btih:private",
            "Cookie: sessionid=private",
            "set-cookie: session=private",
            "登录口令 correcthorsebatterystaple",
            "服务器密码 my-secret-password",
            "pass=correcthorsebatterystaple",
            "https://example.test/api?pass=correcthorsebatterystaple",
            "private share url https://host.example/s/abc?pwd=xxxx",
        )
        for index, message in enumerate(sensitive_messages):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.repository.append_query_turn(
                    principal="browser-principal-a",
                    session_id=f"agent_sensitive_session_{index:02d}",
                    message=message,
                    response=_response(),
                )
        self.assertEqual(
            self.repository.list_sessions(principal="browser-principal-a"), []
        )

    def test_delete_epoch_blocks_late_append_but_allows_new_requests(self):
        generation = self.repository.session_generation(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertFalse(
            self.repository.delete_session(
                principal="browser-principal-a", session_id=SESSION_A
            )
        )
        self.assertFalse(
            self.repository.append_query_turn(
                principal="browser-principal-a",
                session_id=SESSION_A,
                message="删除前启动的迟到请求",
                response=_response(),
                expected_generation=generation,
            )
        )
        self.assertIsNone(
            self.repository.get_session(
                principal="browser-principal-a", session_id=SESSION_A
            )
        )

        next_generation = self.repository.session_generation(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertNotEqual(generation, next_generation)
        self.assertTrue(
            self.repository.append_query_turn(
                principal="browser-principal-a",
                session_id=SESSION_A,
                message="删除后开始的新请求",
                response=_response(),
                expected_generation=next_generation,
            )
        )

    def test_pruned_epoch_never_reuses_deleted_generation(self):
        repository = SQLiteAgentConversationHistoryRepository(
            secret_provider=lambda: "history-test-secret",
            retention_days=1,
        )
        generation = repository.session_generation(
            principal="browser-principal-a", session_id=SESSION_A
        )
        repository.delete_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_conversation_epochs SET updated_at='2000-01-01 00:00:00'"
            )
        repository.list_sessions(principal="browser-principal-a")
        self.assertFalse(
            repository.append_query_turn(
                principal="browser-principal-a",
                session_id=SESSION_A,
                message="极慢请求不得复活历史",
                response=_response(),
                expected_generation=generation,
            )
        )

    def test_sensitive_assistant_output_is_replaced(self):
        self.repository.append_query_turn(
            principal="browser-principal-a",
            session_id=SESSION_A,
            message="检查媒体摘要",
            response=_response(summary="结果位于 file:///media/private/secret.mkv"),
        )
        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_A
        )
        self.assertEqual(history["messages"][1]["data"]["summary"], "[已隐藏敏感详情]")

    def test_expired_sessions_are_pruned_on_read_without_new_writes(self):
        repository = SQLiteAgentConversationHistoryRepository(
            secret_provider=lambda: "history-test-secret",
            retention_days=1,
        )
        repository.append_query_turn(
            principal="expired-principal",
            session_id="agent_expired_session_0001",
            message="检查旧会话",
            response=_response(),
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_conversations SET updated_at='2000-01-01 00:00:00'"
            )
        self.assertEqual(repository.list_sessions(principal="expired-principal"), [])
        self.assertIsNone(repository.get_session(
            principal="expired-principal", session_id="agent_expired_session_0001"
        ))

    def test_repository_prunes_expired_and_global_excess_sessions(self):
        repository = SQLiteAgentConversationHistoryRepository(
            secret_provider=lambda: "history-test-secret",
            max_sessions=100,
            max_messages=4,
            retention_days=1,
            max_total_sessions=10,
        )
        repository.append_query_turn(
            principal="expired-principal",
            session_id="agent_expired_session_0001",
            message="检查旧会话",
            response=_response(),
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_conversations SET updated_at='2000-01-01 00:00:00'"
            )

        for index in range(11):
            repository.append_query_turn(
                principal=f"principal-{index}",
                session_id=f"agent_global_session_{index:04d}",
                message=f"检查会话 {index}",
                response=_response(summary=f"结果 {index}"),
            )

        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT session_id FROM agent_conversations ORDER BY id"
            ).fetchall()
        session_ids = {str(row["session_id"]) for row in rows}
        self.assertEqual(len(session_ids), 10)
        self.assertNotIn("agent_expired_session_0001", session_ids)
        self.assertNotIn("agent_global_session_0000", session_ids)
        self.assertIn("agent_global_session_0010", session_ids)

    def test_repository_enforces_session_and_message_bounds(self):
        for session_id in (SESSION_A, SESSION_B, "agent_session_history_0003"):
            self.repository.append_query_turn(
                principal="browser-principal-a",
                session_id=session_id,
                message=f"查询 {session_id}",
                response=_response(),
            )
        sessions = self.repository.list_sessions(principal="browser-principal-a")
        self.assertEqual(len(sessions), 2)
        self.assertNotIn(SESSION_A, {item["session_id"] for item in sessions})

        for index in range(3):
            self.repository.append_query_turn(
                principal="browser-principal-a",
                session_id=SESSION_B,
                message=f"继续查询 {index}",
                response=_response(summary=f"结果 {index}"),
            )
        history = self.repository.get_session(
            principal="browser-principal-a", session_id=SESSION_B
        )
        self.assertEqual(len(history["messages"]), 4)
        self.assertEqual(history["message_count"], 4)


class _FakeAgentService:
    def __init__(self) -> None:
        self.query_owners: list[str] = []
        self.reset_owners: list[str] = []
        self.invoke_calls: list[tuple[str, dict, str]] = []
        self.confirm_calls: list[tuple[str, str]] = []
        self.discard_calls: list[tuple[str, str]] = []
        self.prepare_calls: list[tuple[str, dict, str, int | None]] = []
        self.confirm_response: dict = _response(summary="受控操作执行完成")
        self.confirm_hook = None
        self.prepare_hook = None
        self.reset_error: AgentToolError | None = None
        self.confirmation_epoch = 0

    def query(self, _message: str, *, owner: str, **_kwargs):
        self.query_owners.append(owner)
        return _response()

    def has_tool(self, tool_name: str) -> bool:
        return bool(str(tool_name or "").strip())

    def invoke(self, tool_name: str, arguments: dict, *, owner: str = "", **_kwargs):
        self.invoke_calls.append((tool_name, dict(arguments), owner))
        return _response()

    def begin_query_confirmation_epoch(self, *, owner: str) -> int:
        del owner
        self.confirmation_epoch += 1
        return self.confirmation_epoch

    def prepare(
        self,
        tool_name: str,
        arguments: dict,
        *,
        owner: str,
        expected_owner_generation: int | None = None,
        **_kwargs,
    ) -> dict:
        self.prepare_calls.append(
            (tool_name, dict(arguments), owner, expected_owner_generation)
        )
        if self.prepare_hook is not None:
            self.prepare_hook()
        return {
            "mode": "confirmation_required",
            "confirmation": {"confirmation_id": "prepared-confirmation-123456"},
            "result": {
                "ok": True,
                "status": "confirmation_required",
                "summary": "等待确认",
                "suggestions": [],
                "evidence": [],
            },
        }

    def confirm(self, confirmation_id: str, *, owner: str, **_kwargs):
        self.confirm_calls.append((confirmation_id, owner))
        if self.confirm_hook is not None:
            self.confirm_hook()
        return self.confirm_response

    def discard_confirmation(self, confirmation_id: str, *, owner: str):
        self.discard_calls.append((confirmation_id, owner))
        return True

    def reset_session(self, *, owner: str):
        self.reset_owners.append(owner)
        self.confirmation_epoch += 1
        if self.reset_error is not None:
            raise self.reset_error
        return {"reset": True}


class AgentConversationHistoryApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        agent_rate_limiter.reset()
        reset_agent_operation_state_for_tests()
        self.client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        self.service = _FakeAgentService()

    def tearDown(self) -> None:
        self.client.close()
        agent_rate_limiter.reset()
        reset_agent_operation_state_for_tests()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(
            r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html
        ) or re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not matched:
            raise AssertionError("页面未输出 CSRF Token")
        return matched.group(1)

    def _login(self, client: TestClient | None = None) -> str:
        target = client or self.client
        login_page = target.get("/login")
        username, password = web_credentials()
        response = target.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": self._token(login_page.text),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(target.get("/agent").text)

    def test_query_list_restore_and_delete_session(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "检查《黑镜》有没有缺集", "session_id": SESSION_A},
            )
            listing = self.client.get("/api/agent/sessions")
            detail = self.client.get(f"/api/agent/sessions/{SESSION_A}")
            deleted = self.client.delete(
                f"/api/agent/sessions/{SESSION_A}", headers=headers
            )
            missing = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["sessions"][0]["session_id"], SESSION_A)
        self.assertEqual(detail.status_code, 200, detail.text)
        raw_detail = detail.text
        for forbidden in (
            "confirmation-secret-not-persisted",
            "request-secret-not-persisted",
            "magnet:?",
            "/media/private",
            "private-result-id",
            '"arguments"',
        ):
            self.assertNotIn(forbidden, raw_detail)
        self.assertTrue(deleted.json()["deleted"])
        self.assertTrue(deleted.json()["reset"]["reset"])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(len(self.service.query_owners), 1)
        self.assertEqual(len(self.service.reset_owners), 1)
        self.assertTrue(self.service.query_owners[0].startswith("web:v1:"))
        self.assertEqual(self.service.reset_owners[0], self.service.query_owners[0])

    def test_discard_confirmation_returns_minimal_contract_without_archiving(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            seeded = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "检查下载队列", "session_id": SESSION_A},
            )
            history_before = self.client.get(f"/api/agent/sessions/{SESSION_A}")
            response = self.client.post(
                "/api/agent/actions/confirm/discard",
                headers=headers,
                json={
                    "confirmation_id": "confirmation-token-123456",
                    "session_id": SESSION_A,
                },
            )
            history_after = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(seeded.status_code, 200, seeded.text)
        self.assertEqual(history_before.status_code, 200, history_before.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"discarded": True})
        self.assertEqual(len(self.service.discard_calls), 1)
        confirmation_id, owner = self.service.discard_calls[0]
        self.assertEqual(confirmation_id, "confirmation-token-123456")
        self.assertTrue(owner.startswith("web:v1:"))
        self.assertEqual(history_after.status_code, 200, history_after.text)
        self.assertEqual(history_after.json(), history_before.json())

    def test_direct_read_tool_with_session_is_archived_and_restorable(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/tools/library.check_updates",
                headers=headers,
                json={
                    "arguments": {"query": "黑镜", "media_type": "tv"},
                    "session_id": SESSION_A,
                },
            )
            detail = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(detail.status_code, 200, detail.text)
        messages = detail.json()["session"]["messages"]
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["data"]["text"], "执行只读检查 · 媒体更新检查")
        self.assertNotIn("tool_name", messages[1]["data"])
        self.assertEqual(messages[1]["data"]["tool_label"], "剧集完整性检查")
        self.assertNotIn("library.audit_episodes", detail.text)
        self.assertEqual(self.service.invoke_calls[0][:2], (
            "library.check_updates", {"query": "黑镜", "media_type": "tv"},
        ))


    def test_confirmed_action_with_session_is_archived_as_safe_outcome(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        self.service.confirm_response = {
            "request_id": "confirm-request-secret",
            "mode": "confirmed_action",
            "tool_call": {
                "name": "indexer.submit_resource",
                "arguments": {"result_id": "private-result-id", "target": "qb"},
            },
            "confirmation": {"confirmation_id": "do-not-store-confirmation"},
            "result": {
                "ok": True,
                "status": "accepted",
                "summary": "下载任务已提交",
                "error": "",
                "suggestions": ["稍后检查下载状态"],
                "data": {"magnet": "magnet:?xt=urn:btih:secret"},
            },
        }
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={
                    "confirmation_id": "confirmation-token-123456",
                    "session_id": SESSION_A,
                },
            )
            detail = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(detail.status_code, 200, detail.text)
        messages = detail.json()["session"]["messages"]
        self.assertEqual(messages[0]["data"]["text"], "确认并执行 · 资源下载提交")
        self.assertEqual(messages[1]["data"]["summary"], "下载任务已提交")
        self.assertEqual(messages[1]["data"]["status"], "accepted")
        self.assertEqual(messages[1]["data"]["tool_label"], "资源下载提交")
        self.assertNotIn("tool_name", messages[1]["data"])
        for forbidden in (
            "confirmation-token-123456",
            "do-not-store-confirmation",
            "private-result-id",
            "magnet:?",
            '"arguments"',
        ):
            self.assertNotIn(forbidden, detail.text)

    def test_confirmed_business_conflict_is_archived_without_changing_status_code(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        self.service.confirm_response = {
            "mode": "confirmed_action",
            "tool_call": {"name": "config.set_feature_state"},
            "result": {
                "ok": False,
                "status": "no_changes",
                "summary": "配置已经处于目标状态",
                "error": "",
                "suggestions": [],
                "data": {},
            },
        }
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={
                    "confirmation_id": "confirmation-token-123456",
                    "session_id": SESSION_A,
                },
            )
            detail = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(response.status_code, 409, response.text)
        messages = detail.json()["session"]["messages"]
        self.assertEqual(messages[0]["data"]["text"], "确认并执行 · 功能开关修改")
        self.assertFalse(messages[1]["data"]["ok"])
        self.assertEqual(messages[1]["data"]["status"], "no_changes")
        self.assertEqual(messages[1]["data"]["tool_label"], "功能开关修改")
        self.assertNotIn("tool_name", messages[1]["data"])

    def test_confirm_without_session_is_rejected_without_history(self):
        csrf = self._login()
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"confirmation_id": "confirmation-token-123456"},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/api/agent/sessions").json()["sessions"], [])

    def test_late_confirm_cannot_recreate_deleted_session(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        repository = SQLiteAgentConversationHistoryRepository()
        self.service.confirm_hook = lambda: repository.delete_session(
            principal=csrf, session_id=SESSION_A
        )
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={
                    "confirmation_id": "confirmation-token-123456",
                    "session_id": SESSION_A,
                },
            )
            missing = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_reset_waits_for_confirmed_write_to_leave_owner_window(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        confirm_started = threading.Event()
        release_confirm = threading.Event()
        reset_completed = threading.Event()

        def block_confirm() -> None:
            confirm_started.set()
            if not release_confirm.wait(timeout=3):
                raise TimeoutError("测试未释放确认操作")

        self.service.confirm_hook = block_confirm
        reset_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        reset_client.cookies.update(self.client.cookies)
        try:
            with patch(
                "app.routes.agent_api.get_agent_service", return_value=self.service
            ), ThreadPoolExecutor(max_workers=2) as pool:
                confirm_future = pool.submit(
                    self.client.post,
                    "/api/agent/actions/confirm",
                    headers=headers,
                    json={
                        "confirmation_id": "confirmation-token-123456",
                        "session_id": SESSION_A,
                    },
                )
                self.assertTrue(confirm_started.wait(timeout=2))

                def reset_session():
                    response = reset_client.post(
                        "/api/agent/session/reset",
                        headers=headers,
                        json={"session_id": SESSION_A},
                    )
                    reset_completed.set()
                    return response

                reset_future = pool.submit(reset_session)
                self.assertFalse(reset_completed.wait(timeout=0.1))
                release_confirm.set()
                confirmed = confirm_future.result(timeout=3)
                reset = reset_future.result(timeout=3)
        finally:
            release_confirm.set()
            reset_client.close()

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertTrue(reset_completed.is_set())
        self.assertEqual(len(self.service.confirm_calls), 1)
        self.assertEqual(len(self.service.reset_owners), 1)

    def test_delete_waits_for_confirmed_write_then_removes_archive(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        confirm_started = threading.Event()
        release_confirm = threading.Event()
        delete_completed = threading.Event()

        def block_confirm() -> None:
            confirm_started.set()
            if not release_confirm.wait(timeout=3):
                raise TimeoutError("测试未释放确认操作")

        self.service.confirm_hook = block_confirm
        delete_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        delete_client.cookies.update(self.client.cookies)
        try:
            with patch(
                "app.routes.agent_api.get_agent_service", return_value=self.service
            ), ThreadPoolExecutor(max_workers=2) as pool:
                confirm_future = pool.submit(
                    self.client.post,
                    "/api/agent/actions/confirm",
                    headers=headers,
                    json={
                        "confirmation_id": "confirmation-token-123456",
                        "session_id": SESSION_A,
                    },
                )
                self.assertTrue(confirm_started.wait(timeout=2))

                def delete_session():
                    response = delete_client.delete(
                        f"/api/agent/sessions/{SESSION_A}", headers=headers
                    )
                    delete_completed.set()
                    return response

                delete_future = pool.submit(delete_session)
                self.assertFalse(delete_completed.wait(timeout=0.1))
                release_confirm.set()
                confirmed = confirm_future.result(timeout=3)
                deleted = delete_future.result(timeout=3)
        finally:
            release_confirm.set()
            delete_client.close()

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])
        self.assertTrue(delete_completed.is_set())
        self.assertEqual(
            self.client.get(f"/api/agent/sessions/{SESSION_A}").status_code, 404
        )

    def test_reset_supersedes_inflight_prepare_without_returning_ticket(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        prepare_started = threading.Event()
        release_prepare = threading.Event()

        def block_prepare() -> None:
            prepare_started.set()
            if not release_prepare.wait(timeout=3):
                raise TimeoutError("测试未释放预检操作")

        self.service.prepare_hook = block_prepare
        reset_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        reset_client.cookies.update(self.client.cookies)
        try:
            with patch(
                "app.routes.agent_api.get_agent_service", return_value=self.service
            ), ThreadPoolExecutor(max_workers=1) as pool:
                prepare_future = pool.submit(
                    self.client.post,
                    "/api/agent/actions/write.test/prepare",
                    headers=headers,
                    json={"arguments": {}, "session_id": SESSION_A},
                )
                self.assertTrue(prepare_started.wait(timeout=2))
                reset = reset_client.post(
                    "/api/agent/session/reset",
                    headers=headers,
                    json={"session_id": SESSION_A},
                )
                self.assertEqual(reset.status_code, 200, reset.text)
                release_prepare.set()
                prepared = prepare_future.result(timeout=3)
        finally:
            release_prepare.set()
            reset_client.close()

        self.assertEqual(prepared.status_code, 409, prepared.text)
        self.assertNotIn("confirmation", prepared.json())
        self.assertEqual(len(self.service.prepare_calls), 1)
        self.assertEqual(self.service.prepare_calls[0][3], 1)

    def test_delete_removes_archive_even_when_runtime_reset_fails(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        self.service.reset_error = AgentToolError(
            "runtime reset failed", code="confirmation_invalid"
        )
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "检查项目配置", "session_id": SESSION_A},
            )
            deleted = self.client.delete(
                f"/api/agent/sessions/{SESSION_A}", headers=headers
            )
            missing = self.client.get(f"/api/agent/sessions/{SESSION_A}")

        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])
        self.assertFalse(deleted.json()["reset"]["reset"])
        self.assertNotIn("runtime reset failed", deleted.text)
        self.assertEqual(missing.status_code, 404)

    def test_sensitive_query_succeeds_without_archiving_private_input(self):
        csrf = self._login()
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查 /media/private/secret.mkv",
                    "session_id": SESSION_A,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        listing = self.client.get("/api/agent/sessions")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["sessions"], [])

    def test_session_history_isolated_between_login_sessions(self):
        csrf = self._login()
        with patch(
            "app.routes.agent_api.get_agent_service", return_value=self.service
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"message": "检查项目配置", "session_id": SESSION_A},
            )
        self.assertEqual(response.status_code, 200, response.text)

        other = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        try:
            self._login(other)
            listing = other.get("/api/agent/sessions")
            detail = other.get(f"/api/agent/sessions/{SESSION_A}")
        finally:
            other.close()
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["sessions"], [])
        self.assertEqual(detail.status_code, 404)


if __name__ == "__main__":
    import unittest

    unittest.main()
