"""通过 Telegram SDK 验证种子入口，Telegram 请求使用本地替身。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telebot

from app import config
from app.agent.kernel.adapters import TurnView
from app.agent.rate_limit import AgentRateLimiter
from app.bot import agent_adapter, agent_attachments, handlers
from app.bot.client import TopicRoutingBot
from app.bot.progress import TelegramProgress
from app.modules import agent_files, download_dispatcher, telegram_topic_routing
from tests.support import IsolatedDatabaseTestCase, assert_present

_TORRENT = (
    b"d4:infod6:lengthi4e4:name4:Test12:piece lengthi16384e6:pieces20:"
    + hashlib.sha1(b"test").digest() + b"ee"
)


class TelegramAgentAttachmentTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.settings = {
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "100",
            "TG_AGENT_ENABLED": "1",
            "AGENT_LLM_ENABLED": "1",
        }
        self.enterContext(patch.object(config, "get", side_effect=self._setting))
        self.enterContext(patch.object(handlers, "get", side_effect=self._setting))
        workspace = Path(self.enterContext(TemporaryDirectory(prefix="tg-torrents-")))
        self.enterContext(patch.object(
            agent_files, "get_runtime_paths", return_value=SimpleNamespace(runtime_dir=workspace),
        ))
        limiter = AgentRateLimiter()
        for module in (agent_adapter, agent_attachments):
            self.enterContext(patch.object(module, "agent_rate_limiter", limiter))
        self.enterContext(patch.object(agent_adapter.AGENT_EXECUTOR, "start", return_value=True))
        self.enterContext(patch.object(agent_adapter.AGENT_EXECUTOR, "submit", side_effect=self._submit))
        self.enterContext(patch.object(TelegramProgress, "_typing_heartbeat"))
        self.enterContext(patch.object(telegram_topic_routing, "auto_name_private_topic", new_callable=AsyncMock))
        self.bot = TopicRoutingBot("123456:local-mock-only", parse_mode="HTML", threaded=False)
        self.request = self.enterContext(patch("telebot.apihelper._make_request", side_effect=self._request))
        self.remote_size: int | None = len(_TORRENT)
        self.download = self.enterContext(patch.object(self.bot, "download_file", return_value=_TORRENT))
        self.messages: list[dict] = []
        self.update_id = 1000
        self.owner = agent_adapter.telegram_agent_owner(100, 100)
        telegram_topic_routing.set_topic_mode(self.owner, True)
        self.session = agent_adapter.telegram_agent_session_id(100, 100, 500)
        handlers._register_commands(self.bot, telebot)

    def _setting(self, key, default=""):
        return self.settings.get(key, default)

    @staticmethod
    def _submit(function, *args, **_kwargs):
        function(*args)
        return True

    def _request(self, _token, api_method, *, params, **_kwargs):
        if api_method == "getFile":
            return dict(file_id=params["file_id"], file_unique_id="unique",
                        file_path="documents/mock", file_size=self.remote_size)
        if api_method in {"setMyCommands", "sendChatAction", "editMessageText"}:
            return True
        if api_method == "sendMessage":
            message = self._message(
                2000 + len(self.messages), thread_id=params.get("message_thread_id"), text=params["text"],
                **{"from": {"id": self.bot.bot_id, "first_name": "Bot", "is_bot": True}},
            )
            if "reply_markup" in params:
                message["reply_markup"] = json.loads(params["reply_markup"])
            self.messages.append(message)
            return message
        self.fail(f"Unexpected Telegram request {api_method}")

    @staticmethod
    def _message(message_id, *, thread_id=None, **fields) -> dict:
        message = {
            "message_id": message_id, "date": 1000,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 100, "first_name": "User", "is_bot": False},
            **fields,
        }
        if thread_id is not None:
            message.update(message_thread_id=thread_id, is_topic_message=True)
        return message

    def _dispatch(self, **fields):
        self.update_id += 1
        update = telebot.types.Update.de_json({"update_id": self.update_id, **fields})
        self.bot.process_new_updates([assert_present(update)])

    def _attachment(self, message_id=501, *, size=None, filename="episode.TORRENT",
                    mime="application/octet-stream", **fields):
        self._dispatch(message=self._message(
            message_id, document={
                "file_id": "document", "file_unique_id": "unique",
                "file_name": filename, "mime_type": mime, "file_size": size,
            }, **fields,
        ))

    def _file_requests(self):
        return [request for request in self.request.call_args_list if request.args[1] == "getFile"]

    def test_explicit_torrent_uses_topic_session_and_preserves_original_bytes(self):
        transport = SimpleNamespace(query=AsyncMock(
            return_value=TurnView(self.session, "turn", "request", "success", answer="已收到种子"),
        ))
        with patch.object(agent_adapter, "get_agent_kernel_runtime", return_value=SimpleNamespace(telegram=transport)):
            self._dispatch(message=self._message(500, thread_id=500, forum_topic_created={
                "name": "选集", "icon_color": 0, "is_name_implicit": True,
            }))
            self._attachment(filename="选集<&>.TORRENT", caption="/Agent 只要第 2 集")
            self._attachment(filename="选集<&>.TORRENT", caption="/Agent 只要第 2 集", thread_id=500)

        self.download.assert_called_once_with("documents/mock")
        transport.query.assert_awaited_once()
        envelope = transport.query.await_args.args[0]
        self.assertEqual((envelope.owner, envelope.session_id), (self.owner, self.session))
        self.assertEqual(envelope.delivery_route["message_thread_id"], 500)
        self.assertTrue(envelope.message.startswith("只要第 2 集\n"))
        self.assertIn("mf.cloud.resolve_offline(artifact_id=...)", envelope.message)
        reference = json.loads(envelope.message.splitlines()[-1])
        self.assertEqual((reference["name"], reference["size"]), ("选集<&>.TORRENT", len(_TORRENT)))
        artifact_id = reference["artifact_id"]
        item = download_dispatcher.torrent_artifact_input(artifact_id, owner=self.owner, session_id=self.session)
        self.assertEqual(item.torrent_data, _TORRENT)
        other_session = agent_adapter.telegram_agent_session_id(100, 100, 600)
        with self.assertRaises(FileNotFoundError):
            agent_files.open_file(self.owner, other_session, artifact_id)
        self.assertIn("选集&lt;&amp;&gt;.TORRENT", self.messages[0]["text"])
        self.assertEqual(self.messages[0]["message_thread_id"], 500)

    def test_direct_torrent_keeps_target_picker(self):
        self._dispatch(message=self._message(500, thread_id=500, text="/agent"))
        guide = self.messages[-1]
        self.assertIn("/agent 加处理说明", guide["text"])
        with (
            patch.object(agent_adapter, "get_agent_kernel_runtime") as runtime,
            patch.object(agent_attachments, "store_file") as store,
            patch.object(download_dispatcher, "create_request", wraps=download_dispatcher.create_request) as submit,
            patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": False}),
        ):
            cases = [
                {},
                {"caption": "/agent"},
                {"caption": "只要第 2 集", "reply_to_message": guide},
            ]
            for message_id, fields in enumerate(cases, start=501):
                self._attachment(message_id, thread_id=500, **fields)
            self.settings["TG_AGENT_ENABLED"] = "0"
            self._attachment(504, thread_id=500, caption="/agent 选集")

        runtime.assert_not_called()
        store.assert_not_called()
        self.assertEqual(submit.call_count, len(cases) + 1)
        for submission in submit.call_args_list:
            item = submission.args[0]
            self.assertEqual((item.kind, item.torrent_data), ("torrent", _TORRENT))
        picker = self.messages[-1]
        self.assertIn("选择下载目标", picker["text"])
        labels = [button["text"] for row in picker["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("qBittorrent", labels)
        self.assertEqual(picker["message_thread_id"], 500)

    def test_non_torrent_files_never_fetch_even_with_torrent_mime_or_agent_caption(self):
        cases = [
            ("notes.txt", "application/x-bittorrent"),
            ("movie.torrent.zip", "application/octet-stream"),
            ("manifest.json", "application/json"),
            (None, "application/x-bittorrent"),
        ]
        for message_id, (filename, mime) in enumerate(cases, start=501):
            self._attachment(message_id, filename=filename, mime=mime, caption="/agent 选集")

        self.assertFalse(self._file_requests())
        self.download.assert_not_called()
        self.assertEqual(len(self.messages), len(cases))
        self.assertTrue(all("仅可发送 .torrent 种子文件" in msg["text"] for msg in self.messages))

    def test_invalid_torrent_does_not_run_agent_or_create_download(self):
        self.remote_size = None
        self.download.return_value = b"d4:infod4:name4:Testee"
        with (
            patch.object(agent_adapter, "get_agent_kernel_runtime") as runtime,
            patch.object(download_dispatcher, "create_request") as submit,
        ):
            self._attachment(thread_id=500, caption="/agent 选集")
        runtime.assert_not_called()
        submit.assert_not_called()
        self.assertIn("无法接收种子", self.messages[-1]["text"])

    def test_agent_torrent_checks_reported_and_downloaded_sizes(self):
        limit = download_dispatcher.TORRENT_MAX_BYTES
        with patch.object(agent_attachments, "store_file") as store:
            self._attachment(501, size=limit + 1, caption="/agent 选集")
            self.assertFalse(self._file_requests())
            self.remote_size = limit + 1
            self._attachment(502, caption="/agent 选集")
            self.download.assert_not_called()
            self.remote_size = None
            self.download.return_value = b"a" * (limit + 1)
            self._attachment(503, caption="/agent 选集")
            self.download.return_value = _TORRENT
            self._attachment(504, size=len(_TORRENT) - 1, caption="/agent 选集")
        store.assert_not_called()
        self.assertTrue(all("10 MB" in message["text"] for message in self.messages[:3]))
        self.assertIn("无法接收种子", self.messages[-1]["text"])

    def test_gcid_command_and_callback_have_no_route(self):
        request_count = self.request.call_count
        self._dispatch(message=self._message(501, text="/agent_gcid"))
        self._dispatch(callback_query={
            "id": "callback", "chat_instance": "mock",
            "from": {"id": 100, "first_name": "User", "is_bot": False},
            "message": self._message(502), "data": "agcid:" + "a" * 32 + ":0",
        })
        self.assertEqual(self.request.call_count, request_count)
