from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.provider_model import ProviderSettings
from app.modules import telegram_topic_routing as routing
from app.modules.telegram_topic_routing import (
    auto_name_private_topic,
    record_private_topic_service_message,
    set_topic_mode,
)
from tests.support import IsolatedDatabaseTestCase


class _FakeBot:
    def __init__(self):
        self.edits = []

    def edit_forum_topic(self, **kwargs):
        self.edits.append(kwargs)
        return True


class _FakeAdapter:
    def __init__(self, events=(), error: Exception | None = None, before_output=None):
        self.events = list(events)
        self.error = error
        self.before_output = before_output
        self.requests = []

    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        if self.before_output is not None:
            self.before_output()
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


def _event_message(
    *,
    chat_type="private",
    chat_id=501,
    thread_id=901,
    created=None,
    edited=None,
    closed=None,
    reopened=None,
    sender_id=501,
    sender_is_bot=False,
):
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id),
        message_thread_id=thread_id,
        is_topic_message=True,
        forum_topic_created=created,
        forum_topic_edited=edited,
        forum_topic_closed=closed,
        forum_topic_reopened=reopened,
        from_user=SimpleNamespace(id=sender_id, is_bot=sender_is_bot),
    )


class TelegramTopicNamingTests(IsolatedDatabaseTestCase):
    owner = "tg:v1:501\x1f501"
    chat_id = 501

    def setUp(self):
        super().setUp()
        set_topic_mode(self.owner, True)

    def _record_implicit(self, thread_id=901):
        message = _event_message(
            thread_id=thread_id,
            created=SimpleNamespace(is_name_implicit=True),
        )
        self.assertTrue(record_private_topic_service_message(self.owner, message))
        return routing._topic_naming_key(self.owner, self.chat_id, thread_id)

    def _patch_model(self, adapter):
        from app.agent.kernel import provider_model

        settings = ProviderSettings(
            api_url="https://llm.example/v1",
            model="configured-model",
            api_key="test-key",
            timeout_seconds=10,
        )
        return (
            patch.object(ProviderSettings, "from_config", return_value=settings),
            patch.object(provider_model, "OpenAICompatibleModelAdapter", return_value=adapter),
        )

    def _run_name(
        self, bot, thread_id, message, adapter, *, topic_open=True, chat_type="private"
    ):
        settings_patch, adapter_patch = self._patch_model(adapter)
        with settings_patch, adapter_patch:
            return asyncio.run(
                auto_name_private_topic(
                    bot,
                    owner=self.owner,
                    session_id=f"session-{thread_id}",
                    chat_id=self.chat_id,
                    chat_type=chat_type,
                    thread_id=thread_id,
                    topic_open=topic_open,
                    first_user_message=message,
                )
            )

    def test_implicit_topic_is_named_once_with_override_and_redacted_input(self):
        thread_id = 901
        self._record_implicit(thread_id)
        bot = _FakeBot()
        adapter = _FakeAdapter(
            events=[
                ModelEvent(ModelEventType.TEXT_DELTA, text="电影推荐与筛选"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ]
        )
        with (
            patch(
                "app.modules.telegram_model_preferences.get_telegram_model_preference",
                return_value="session-override",
            ),
            patch.object(
                routing,
                "_set_topic_naming_status",
                wraps=routing._set_topic_naming_status,
            ) as status_write,
        ):
            self.assertTrue(
                self._run_name(
                    bot,
                    thread_id,
                    "想找一部电影，API_KEY=top-secret magnet:?xt=urn:btih:abcdef",
                    adapter,
                )
            )
            self.assertEqual(status_write.call_count, 2)
            self.assertFalse(
                self._run_name(
                    bot, thread_id, "后续消息不能触发重命名", adapter
                )
            )
            self.assertEqual(status_write.call_count, 2)

        self.assertEqual(len(adapter.requests), 1)
        request = adapter.requests[0]
        self.assertEqual(request.model, "session-override")
        self.assertEqual(request.tools, ())
        self.assertEqual(request.max_output_tokens, 96)
        self.assertNotIn("top-secret", request.messages[0].content)
        self.assertNotIn("magnet:", request.messages[0].content)
        self.assertEqual(
            bot.edits,
            [{"chat_id": self.chat_id, "message_thread_id": thread_id, "name": "电影推荐与筛选"}],
        )
        self.assertEqual(routing._topic_naming_status(routing._topic_naming_key(self.owner, self.chat_id, thread_id)), "auto")

    def test_manual_rename_during_model_generation_prevents_telegram_write(self):
        thread_id = 902
        key = self._record_implicit(thread_id)
        bot = _FakeBot()

        def record_manual_rename():
            service_message = _event_message(
                thread_id=thread_id,
                edited=SimpleNamespace(name="我的手动标题"),
                sender_id=501,
            )
            self.assertTrue(
                record_private_topic_service_message(
                    self.owner, service_message, bot_user_id=999
                )
            )

        adapter = _FakeAdapter(
            events=[
                ModelEvent(ModelEventType.TEXT_DELTA, text="模型生成的标题"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ],
            before_output=record_manual_rename,
        )
        self.assertFalse(self._run_name(bot, thread_id, "帮我找电影", adapter))
        self.assertEqual(bot.edits, [])
        self.assertEqual(routing._topic_naming_status(key), "manual")

    def test_empty_overlong_promise_and_provider_failure_never_edit_topic(self):
        outputs = [
            "",
            "x" * 129,
            "我会替你完成下载",
            "标题 API_KEY=top-secret",
            "标题\x00注入",
            RuntimeError("provider unavailable"),
        ]
        for index, output in enumerate(outputs):
            with self.subTest(index=index):
                thread_id = 910 + index
                self._record_implicit(thread_id)
                bot = _FakeBot()
                if isinstance(output, Exception):
                    adapter = _FakeAdapter(error=output)
                else:
                    adapter = _FakeAdapter(
                        events=[
                            ModelEvent(ModelEventType.TEXT_DELTA, text=output),
                            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                        ]
                    )
                self.assertFalse(self._run_name(bot, thread_id, "帮我查一部电影", adapter))
                self.assertEqual(bot.edits, [])

    def test_missing_creation_thread_closed_and_non_private_topics_skip_model(self):
        cases = [
            (920, None, True, "private", "有 thread 但没有创建记录"),
            (921, True, False, "private", "调用方确认已关闭 topic"),
            (922, True, True, "group", "群组话题"),
        ]
        for thread_id, record_created, topic_open, chat_type, label in cases:
            with self.subTest(label=label):
                if record_created:
                    self._record_implicit(thread_id)
                bot = _FakeBot()
                adapter = _FakeAdapter(
                    events=[ModelEvent(ModelEventType.TEXT_DELTA, text="不应出现")]
                )
                self.assertFalse(
                    self._run_name(
                        bot,
                        thread_id,
                        "帮我查一部电影",
                        adapter,
                        topic_open=topic_open,
                        chat_type=chat_type,
                    )
                )
                self.assertEqual(adapter.requests, [])
                self.assertEqual(bot.edits, [])

        self._record_implicit(923)
        self.assertTrue(
            record_private_topic_service_message(
                self.owner, _event_message(thread_id=923, closed=SimpleNamespace())
            )
        )
        bot = _FakeBot()
        adapter = _FakeAdapter()
        self.assertFalse(self._run_name(bot, 923, "不应命名", adapter))
        self.assertEqual(adapter.requests, [])

        bot = _FakeBot()
        adapter = _FakeAdapter()
        self.assertFalse(
            self._run_name(bot, None, "帮我查一部电影", adapter)
        )
        self.assertEqual(adapter.requests, [])

    def test_explicit_creation_title_and_unverified_existing_topic_are_never_eligible(self):
        explicit = _event_message(
            thread_id=930,
            created=SimpleNamespace(is_name_implicit=False),
        )
        self.assertTrue(record_private_topic_service_message(self.owner, explicit))
        self.assertEqual(
            routing._topic_naming_status(routing._topic_naming_key(self.owner, self.chat_id, 930)),
            "ineligible",
        )
        bot = _FakeBot()
        adapter = _FakeAdapter()
        self.assertFalse(self._run_name(bot, 930, "对话", adapter))
        self.assertFalse(self._run_name(bot, 931, "旧话题", adapter))
        self.assertEqual(adapter.requests, [])
        self.assertEqual(bot.edits, [])

    def test_close_reopen_and_manual_rename_are_persisted(self):
        thread_id = 940
        key = self._record_implicit(thread_id)
        closed = _event_message(thread_id=thread_id, closed=SimpleNamespace())
        self.assertTrue(record_private_topic_service_message(self.owner, closed))
        self.assertEqual(routing._topic_naming_status(key), "closed:eligible")

        bot = _FakeBot()
        adapter = _FakeAdapter()
        self.assertFalse(self._run_name(bot, thread_id, "不应命名", adapter, topic_open=False))
        self.assertEqual(adapter.requests, [])

        reopened = _event_message(thread_id=thread_id, reopened=SimpleNamespace())
        self.assertTrue(record_private_topic_service_message(self.owner, reopened))
        self.assertEqual(routing._topic_naming_status(key), "eligible")

        manual = _event_message(
            thread_id=thread_id,
            edited=SimpleNamespace(name="手动标题"),
            sender_id=501,
        )
        self.assertTrue(record_private_topic_service_message(self.owner, manual, bot_user_id=999))
        self.assertEqual(routing._topic_naming_status(key), "manual")

    def test_bot_rename_event_does_not_mark_manual(self):
        thread_id = 950
        key = self._record_implicit(thread_id)
        event = _event_message(
            thread_id=thread_id,
            edited=SimpleNamespace(name="自动标题"),
            sender_id=999,
            sender_is_bot=True,
        )
        self.assertTrue(record_private_topic_service_message(self.owner, event, bot_user_id=999))
        self.assertEqual(routing._topic_naming_status(key), "eligible")

    def test_only_complete_stop_without_tool_calls_can_be_used_as_title(self):
        for index, finish_reason in enumerate(("length", "tool_calls", "tool_use", "")):
            with self.subTest(finish_reason=finish_reason):
                thread_id = 960 + index
                self._record_implicit(thread_id)
                bot = _FakeBot()
                events = [ModelEvent(ModelEventType.TEXT_DELTA, text="不可用标题")]
                if finish_reason == "tool_calls":
                    events.append(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED))
                events.append(
                    ModelEvent(ModelEventType.FINISH, finish_reason=finish_reason)
                )
                adapter = _FakeAdapter(events=events)
                self.assertFalse(self._run_name(bot, thread_id, "普通请求", adapter))
                self.assertEqual(bot.edits, [])

    def test_service_events_outside_private_topics_are_ignored(self):
        group_event = _event_message(
            chat_type="supergroup",
            created=SimpleNamespace(is_name_implicit=True),
        )
        self.assertFalse(record_private_topic_service_message(self.owner, group_event))
        no_thread = _event_message(
            thread_id=None,
            created=SimpleNamespace(is_name_implicit=True),
        )
        self.assertFalse(record_private_topic_service_message(self.owner, no_thread))
