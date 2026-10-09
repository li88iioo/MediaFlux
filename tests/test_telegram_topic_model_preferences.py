"""Telegram topic 路由与会话模型偏好使用隔离 KV 的传输测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.agent.model_catalog import fetch_ai_models, normalize_provider_model_id
from app.bot import handlers
from app.modules import telegram_model_preferences as model_preferences
from app.modules import telegram_topic_routing as topic_routing
from app.modules.telegram_topic_routing import (
    bind_download_request_thread,
    create_session_callback_route,
    download_request_thread,
    resolve_session_callback_route,
    set_topic_mode,
    telegram_session_scope,
    topic_mode_enabled,
)
from app.notifier import NotificationEvent, send_event_result


class _TelegramBot:
    def __init__(self):
        self.sent = []
        self.replies = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=81)

    def reply_to(self, source, text, **kwargs):
        self.replies.append((source, text, kwargs))
        return SimpleNamespace(message_id=82)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Rows(list):
    def fetchall(self):
        return self


class _Connection:
    def __init__(self, values, updated_at):
        self.values = values
        self.updated_at = updated_at
        self.select_limits = []

    def execute(self, sql, args):
        if sql.startswith("INSERT OR IGNORE INTO settings_kv"):
            key, value, updated = args
            if key not in self.values:
                self.values[key] = value
                self.updated_at[key] = updated
        elif sql.startswith("SELECT key FROM settings_kv"):
            lower, upper, cutoff, limit = args
            self.select_limits.append(limit)
            keys = sorted(
                key
                for key in self.values
                if lower <= key < upper
                and self.updated_at.get(key, "") <= cutoff
            )[:limit]
            return _Rows({"key": key} for key in keys)
        elif sql.startswith("DELETE FROM settings_kv"):
            self.values.pop(args[0], None)
            self.updated_at.pop(args[0], None)

    def executemany(self, sql, rows):
        for args in rows:
            self.execute(sql, args)


class TelegramTopicAndModelPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {}
        self.updated_at = {}
        self.owner = "tg:v1:-100123\x1f7"
        self._kv_get = patch.object(
            db, "kv_get", side_effect=lambda key, default="": self.values.get(key, default)
        )
        def save_value(key, value):
            self.values[key] = value
            self.updated_at[key] = db.now()

        self._kv_set = patch.object(db, "kv_set", side_effect=save_value)
        self._get_conn = patch.object(
            db,
            "get_conn",
            side_effect=lambda: _ConnectionContext(
                _Connection(self.values, self.updated_at)
            ),
        )
        self._kv_get.start()
        self._kv_set.start()
        self._get_conn.start()
        self.addCleanup(self._kv_get.stop)
        self.addCleanup(self._kv_set.stop)
        self.addCleanup(self._get_conn.stop)

    def test_topic_mode_is_persisted_per_owner_and_thread_routes_are_isolated(self) -> None:
        self.assertFalse(topic_mode_enabled(self.owner))
        self.assertEqual(telegram_session_scope(self.owner, 41), self.owner)
        set_topic_mode(self.owner, True)
        self.assertTrue(topic_mode_enabled(self.owner))
        first_scope = telegram_session_scope(self.owner, 41)
        second_scope = telegram_session_scope(self.owner, 42)
        self.assertNotEqual(first_scope, self.owner)
        self.assertNotEqual(first_scope, second_scope)
        set_topic_mode(self.owner, False)
        self.assertFalse(topic_mode_enabled(self.owner))
        self.assertEqual(telegram_session_scope(self.owner, 41), self.owner)

    def test_expired_random_callback_routes_are_pruned_in_bounded_batches(self) -> None:
        old_session_token = create_session_callback_route(
            self.owner, "old-session", 41
        )
        live_session_token = create_session_callback_route(
            self.owner, "live-session", 41
        )
        session_prefix = (
            "telegram_callback_route:v1:"
            + topic_routing._digest(self.owner)
            + ":"
        )
        old_session_key = session_prefix + old_session_token
        live_session_key = session_prefix + live_session_token

        with self._channel():
            old_model_token = model_preferences.create_model_callback(
                self.owner, "old-model", 41, action="select", model_id="model-a"
            )
            live_model_token = model_preferences.create_model_callback(
                self.owner, "live-model", 41, action="select", model_id="model-b"
            )
        old_model_key = model_preferences._callback_key(
            self.owner, old_model_token
        )
        live_model_key = model_preferences._callback_key(
            self.owner, live_model_token
        )
        self.updated_at[old_session_key] = "2000-01-01 00:00:00"
        self.updated_at[old_model_key] = "2000-01-01 00:00:00"
        self.values["unrelated:setting"] = "keep"
        self.updated_at["unrelated:setting"] = "2000-01-01 00:00:00"

        removed = topic_routing._maybe_prune_expired_routes(force=True)

        self.assertEqual(removed, 2)
        self.assertNotIn(old_session_key, self.values)
        self.assertNotIn(old_model_key, self.values)
        self.assertIn(live_session_key, self.values)
        self.assertIn(live_model_key, self.values)
        self.assertEqual(self.values["unrelated:setting"], "keep")

    def test_callback_resolvers_reject_malformed_expiry_values(self) -> None:
        bad_expiries = ("not-a-time", [], float("nan"), float("inf"))
        session_token = create_session_callback_route(
            self.owner, "session", 41
        )
        session_key = (
            "telegram_callback_route:v1:"
            + topic_routing._digest(self.owner)
            + ":"
            + session_token
        )
        with self._channel():
            model_token = model_preferences.create_model_callback(
                self.owner, "session", 41, action="select", model_id="model-a"
            )
        model_key = model_preferences._callback_key(self.owner, model_token)

        for bad_expiry in bad_expiries:
            with self.subTest(expiry=repr(bad_expiry)):
                for key in (session_key, model_key):
                    route = json.loads(self.values[key])
                    route["expires_at"] = bad_expiry
                    self.values[key] = json.dumps(route)
                with self._channel():
                    self.assertEqual(
                        resolve_session_callback_route(
                            self.owner, session_token, 41
                        ),
                        "",
                    )
                    self.assertIsNone(
                        model_preferences.resolve_model_callback(
                            self.owner, model_token, 41
                        )
                    )

    def test_agent_callback_route_is_bound_to_original_thread_and_session(self) -> None:
        route = create_session_callback_route(self.owner, "tg_topic_session", 41)
        self.assertEqual(
            resolve_session_callback_route(self.owner, route, 41),
            "tg_topic_session",
        )
        self.assertEqual(resolve_session_callback_route(self.owner, route, 42), "")

    def test_download_receipt_route_keeps_first_thread_until_confirmed_route_updates(self) -> None:
        bind_download_request_thread(17, 41, only_if_unbound=True)
        bind_download_request_thread(17, 42, only_if_unbound=True)
        self.assertEqual(download_request_thread(17), 41)
        bind_download_request_thread(17, 42)
        self.assertEqual(download_request_thread(17), 42)

    def _channel(self, *, url="https://provider.example/v1", protocol="responses", key="secret-one"):
        values = {
            "AGENT_LLM_API_URL": url,
            "AGENT_LLM_PROTOCOL": protocol,
            "AGENT_LLM_API_KEY": key,
        }
        return patch.object(
            model_preferences.config,
            "get",
            side_effect=lambda name, default="": values.get(name, default),
        )

    def test_model_preference_is_session_and_channel_bound_without_storing_key(self) -> None:
        with self._channel():
            model_preferences.set_telegram_model_preference(
                self.owner, "session-a", "model-a"
            )
            self.assertEqual(
                model_preferences.get_telegram_model_preference(
                    self.owner, "session-a"
                ),
                "model-a",
            )
            self.assertEqual(
                model_preferences.get_telegram_model_preference(
                    self.owner, "session-b"
                ),
                "",
            )
            stored_value = next(
                value for key, value in self.values.items()
                if key.startswith("telegram_model_preference:")
            )
            self.assertNotIn("secret-one", stored_value)
            stored = json.loads(stored_value)
            self.assertEqual(stored["model_id"], "model-a")
            self.assertNotEqual(stored["channel"], "secret-one")

        with self._channel(key="secret-two"):
            self.assertEqual(
                model_preferences.get_telegram_model_preference(
                    self.owner, "session-a"
                ),
                "",
                "credential change invalidates a stale selection",
            )

    def test_model_catalog_resolves_auto_protocol_before_building_headers(self) -> None:
        resolver_calls = []
        header_calls = []
        client_instances = []

        provider_module = ModuleType("app.clients.openai_compatible")

        def resolve_protocol(protocol, base_url):
            resolver_calls.append((protocol, base_url))
            return "anthropic_messages"

        def provider_headers(protocol, api_key, *, include_content_type):
            header_calls.append((protocol, api_key, include_content_type))
            return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}

        provider_module.normalize_provider_location = lambda base_url, **_kwargs: SimpleNamespace(
            host="api.anthropic.com", models_url=base_url.rstrip("/") + "/models"
        )
        provider_module.resolve_protocol = resolve_protocol
        provider_module.provider_headers = provider_headers

        http_module = ModuleType("app.indexers.http")

        class FakeHttpClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.get_calls = []
                client_instances.append(self)

            async def get(self, url, *, headers, max_redirects):
                self.get_calls.append((url, headers, max_redirects))
                return SimpleNamespace(
                    status_code=200,
                    text='{"data":[{"id":"model-a"},{"id":"model-a"}]}',
                )

            async def aclose(self):
                return None

        http_module.FixedHostHttpClient = FakeHttpClient
        clients_package = ModuleType("app.clients")
        clients_package.__path__ = []
        indexers_package = ModuleType("app.indexers")
        indexers_package.__path__ = []
        modules = {
            "app.clients": clients_package,
            "app.clients.openai_compatible": provider_module,
            "app.indexers": indexers_package,
            "app.indexers.http": http_module,
        }
        with patch.dict(sys.modules, modules):
            models = asyncio.run(
                fetch_ai_models(
                    base_url="https://api.anthropic.com/v1",
                    api_key="test-key",
                    protocol="auto",
                    timeout_seconds=12,
                )
            )

        self.assertEqual(models, ["model-a"])
        self.assertEqual(
            resolver_calls, [("auto", "https://api.anthropic.com/v1")]
        )
        self.assertEqual(
            header_calls, [("anthropic_messages", "test-key", False)]
        )
        self.assertEqual(client_instances[0].kwargs["timeout_seconds"], 12)
        self.assertEqual(
            client_instances[0].get_calls[0][1]["x-api-key"], "test-key"
        )

    def test_catalog_and_preference_share_strict_model_id_validation(self) -> None:
        for invalid in ("", "\nmodel", "model\tname", "\x85model", "line\u2028break", "x" * 201):
            with self.subTest(invalid=repr(invalid)):
                self.assertEqual(normalize_provider_model_id(invalid), "")
                with self._channel(), self.assertRaises(ValueError):
                    model_preferences.set_telegram_model_preference(
                        self.owner, "session-a", invalid
                    )
        self.assertEqual(normalize_provider_model_id("  model-a  "), "model-a")

    def test_model_cancel_callback_is_bound_to_the_original_topic(self) -> None:
        with self._channel():
            token = model_preferences.create_model_callback(self.owner, "session-topic", 41, action="cancel")
            route = model_preferences.resolve_model_callback(self.owner, token, 41)
            self.assertEqual(route["action"], "cancel")
            self.assertEqual(route["session_id"], "session-topic")
            self.assertIsNone(model_preferences.resolve_model_callback(self.owner, token, 42))

    def test_model_callback_cannot_cross_topic_or_provider_channel(self) -> None:
        with self._channel():
            token = model_preferences.create_model_callback(
                self.owner,
                "session-topic",
                41,
                action="select",
                model_id="model-a",
            )
            route = model_preferences.resolve_model_callback(self.owner, token, 41)
            self.assertEqual(route["session_id"], "session-topic")
            self.assertEqual(route["model_id"], "model-a")
            self.assertIsNone(
                model_preferences.resolve_model_callback(self.owner, token, 42)
            )

        with self._channel(key="rotated-key"):
            self.assertIsNone(
                model_preferences.resolve_model_callback(self.owner, token, 41)
            )

    def test_download_notification_dispatch_uses_persisted_topic_route(self) -> None:
        from app.modules import telegram_notification_center as center
        from app.notifier import TelegramSendResult

        event = NotificationEvent("下载完成")
        item = {
            "id": 9,
            "lease_generation": 2,
            "revision": 3,
            "topic": "download",
            "thread_key": "download:17",
            "event_key": "download:17:complete",
            "event_json": "{}",
            "chat_id": "100",
            "message_id": 0,
        }
        with (
            patch.object(center, "_allows_dispatch", return_value=True),
            patch.object(center, "deserialize_notification_event", return_value=event),
            patch(
                "app.modules.telegram_download_lifecycle.download_notification_obsolescence",
                return_value="",
            ),
            patch(
                "app.modules.telegram_topic_routing.download_request_thread",
                return_value=41,
            ),
            patch.object(
                center, "send_event_result",
                return_value=TelegramSendResult(ok=True, message_id=81),
            ) as send,
            patch.object(center, "complete_notification", return_value=True),
        ):
            self.assertTrue(center._dispatch_item(item))

        send.assert_called_once_with(
            event, chat_id="100", message_thread_id=41
        )

    def test_reply_and_async_notification_keep_source_topic(self) -> None:
        bot = _TelegramBot()
        source = SimpleNamespace(
            chat=SimpleNamespace(id=100),
            message_id=27,
            message_thread_id=41,
        )
        handlers._reply_to_source(bot, source, "同一话题回复")
        self.assertEqual(bot.replies[0][2]["message_thread_id"], 41)

        with patch("app.notifier.get_bot", return_value=bot):
            outcome = send_event_result(
                NotificationEvent("异步完成"),
                chat_id="100",
                message_thread_id=41,
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(bot.sent[0][2]["message_thread_id"], 41)
