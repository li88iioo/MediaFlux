from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.config import web_credentials
from tests.support import IsolatedDatabaseTestCase


SHARE_URL = "https://www.guangyapan.com/s/demo123?code=2468"
SECRET_TOKEN = "share-access-token-must-stay-private"
SIGNED_URL = "https://download.invalid/file?sign=private-signature"


def inspected_payload(count: int = 3) -> dict:
    return {
        "share_id": "demo123",
        "access_token": SECRET_TOKEN,
        "files": [
            {
                "id": f"file-{index}",
                "name": f"Episode {index:02d}.mkv",
                "is_dir": False,
                "size": index * 1024,
                "download_url": SIGNED_URL,
            }
            for index in range(1, count + 1)
        ],
    }


class FakeShareClient:
    def __init__(self, inspected: dict | None = None) -> None:
        self.logged_in = True
        self.inspected = inspected or inspected_payload()
        self.inspect_calls: list[str] = []
        self.restore_calls: list[tuple[str, list[str], str]] = []
        self.create_dir_calls: list[tuple[str, str]] = []
        self.close_calls = 0

    def inspect_share(self, share_url: str) -> dict:
        self.inspect_calls.append(share_url)
        return self.inspected

    def create_dir(self, name: str, parent_id: str = "0") -> str:
        self.create_dir_calls.append((name, parent_id))
        return f"staging-{parent_id}"

    def restore_share(self, access_token: str, file_ids: list[str], target_dir_id: str) -> dict:
        self.restore_calls.append((access_token, list(file_ids), target_dir_id))
        return {"success": True, "count": len(file_ids)}

    def list_dir(self, parent_id: str = "0"):
        return [
            SimpleNamespace(file_id="dir-private-1", name="电影", is_dir=True, size=0),
            SimpleNamespace(file_id="not-a-dir", name="readme.txt", is_dir=False, size=10),
        ]

    def close(self) -> None:
        self.close_calls += 1


class FakeMarkup:
    def __init__(self, row_width: int = 2) -> None:
        self.row_width = row_width
        self.buttons = []

    def add(self, *buttons) -> None:
        self.buttons.extend(buttons)


FAKE_TELEBOT = SimpleNamespace(types=SimpleNamespace(
    InlineKeyboardMarkup=FakeMarkup,
    InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
        text=text, callback_data=callback_data,
    ),
))


class GuangYaShareRoutingTests(unittest.TestCase):
    def test_guangya_share_url_routes_before_generic_http(self):
        from app.modules.download_dispatcher import route_download_url

        self.assertEqual(route_download_url(SHARE_URL), "guangya_share")
        self.assertEqual(route_download_url("https://example.invalid/archive.iso"), "http")
        self.assertEqual(route_download_url("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"), "magnet")

    def test_guangya_detector_rejects_lookalike_hosts_and_non_share_paths(self):
        from app.modules.download_dispatcher import is_guangya_share_url

        self.assertTrue(is_guangya_share_url("https://guangyapan.com/share/ABC123"))
        self.assertTrue(is_guangya_share_url("https://www.guangyapan.com/s/ABC123?pwd=1"))
        self.assertTrue(is_guangya_share_url("https://www.guangyapan.com/S/ABC123?code=1"))
        self.assertFalse(is_guangya_share_url("https://guangyapan.com.evil.invalid/s/ABC123"))
        self.assertFalse(is_guangya_share_url("https://www.guangyapan.com/download/ABC123"))


class GuangYaRestoreResultTests(unittest.TestCase):
    @staticmethod
    def _client_with_response(response):
        from app.clients.guangya import GuangYaClient

        class Raw:
            def share_restore(self, **_kwargs):
                return response

        client = GuangYaClient.__new__(GuangYaClient)
        client._raw = Raw()
        client._last_persisted_token = ""
        return client

    def test_restore_classifies_explicit_failure_as_retry_safe(self):
        result = self._client_with_response(
            {"code": 500, "msg": "provider rejected"}
        ).restore_share("share-token", ["file-1"], "target")

        self.assertFalse(result["success"])
        self.assertTrue(result["retry_safe"])

    def test_restore_classifies_ambiguous_response_as_manual_review(self):
        result = self._client_with_response({}).restore_share(
            "share-token", ["file-1"], "target"
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["retry_safe"])

    def test_restore_accepts_explicit_success_code(self):
        result = self._client_with_response({"code": 0}).restore_share(
            "share-token", ["file-1"], "target"
        )

        self.assertTrue(result["success"])


class ShareTransferPreviewStoreTests(unittest.TestCase):
    def test_store_is_bounded_expires_after_fifteen_minutes_and_isolates_chat_user(self):
        from app.modules.share_transfer import ShareTransferPreviewStore

        now = [100.0]
        tokens = iter(("preview-one", "preview-two", "preview-three"))
        store = ShareTransferPreviewStore(
            ttl_seconds=15 * 60,
            max_entries=2,
            clock=lambda: now[0],
            token_factory=lambda: next(tokens),
        )
        first = store.create(inspected_payload(1), chat_id="chat-a", user_id="user-a")
        second = store.create(inspected_payload(1), chat_id="chat-a", user_id="user-a")
        third = store.create(inspected_payload(1), chat_id="chat-a", user_id="user-a")

        self.assertEqual((first, second, third), ("preview-one", "preview-two", "preview-three"))
        self.assertEqual(store.entry_count, 2)
        with self.assertRaisesRegex(ValueError, "过期|无效"):
            store.snapshot(first, chat_id="chat-a", user_id="user-a")
        with self.assertRaisesRegex(ValueError, "过期|无效"):
            store.snapshot(second, chat_id="chat-a", user_id="user-b")
        with self.assertRaisesRegex(ValueError, "过期|无效"):
            store.snapshot(second, chat_id="chat-b", user_id="user-a")

        now[0] += 15 * 60 + 0.1
        with self.assertRaisesRegex(ValueError, "过期|无效"):
            store.snapshot(third, chat_id="chat-a", user_id="user-a")
        self.assertEqual(store.entry_count, 0)

    def test_public_snapshot_strips_share_token_signed_url_and_unknown_fields(self):
        from app.modules.share_transfer import ShareTransferPreviewStore

        store = ShareTransferPreviewStore(token_factory=lambda: "safe-preview")
        preview_id = store.create(inspected_payload(1), chat_id="chat", user_id="user")
        snapshot = store.snapshot(preview_id, chat_id="chat", user_id="user")
        rendered = repr(snapshot)

        self.assertNotIn(SECRET_TOKEN, rendered)
        self.assertNotIn(SIGNED_URL, rendered)
        self.assertEqual(set(snapshot["files"][0]), {"id", "name", "is_dir", "size"})
        self.assertEqual(snapshot["selected_ids"], ["file-1"])

    def test_selection_operations_revalidate_membership_and_support_all_none_toggle(self):
        from app.modules.share_transfer import ShareTransferPreviewStore

        store = ShareTransferPreviewStore(token_factory=lambda: "selection-preview")
        preview_id = store.create(inspected_payload(3), chat_id="chat", user_id="user")
        store.select_none(preview_id, chat_id="chat", user_id="user")
        self.assertEqual(store.snapshot(preview_id, "chat", "user")["selected_ids"], [])
        store.toggle(preview_id, "file-2", chat_id="chat", user_id="user")
        self.assertEqual(store.snapshot(preview_id, "chat", "user")["selected_ids"], ["file-2"])
        store.select_all(preview_id, chat_id="chat", user_id="user")
        self.assertEqual(
            store.snapshot(preview_id, "chat", "user")["selected_ids"],
            ["file-1", "file-2", "file-3"],
        )
        with self.assertRaisesRegex(ValueError, "不属于"):
            store.toggle(preview_id, "foreign-file", chat_id="chat", user_id="user")


class ShareTransferRequestTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        from app.modules.share_transfer import ShareTransferPreviewStore

        # IsolatedDatabaseTestCase 按类复用同一个临时库；每个用例仍需清理自身状态。
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
        self.store = ShareTransferPreviewStore(token_factory=lambda: "request-preview")
        self.client = FakeShareClient()
        self.preview_id = self.store.create(
            self.client.inspected,
            chat_id="chat-100",
            user_id="user-200",
        )

    def test_selected_ids_are_revalidated_and_duplicate_request_is_idempotent(self):
        from app.modules.share_transfer import create_share_request

        tracker = Mock()
        with patch("app.modules.share_transfer.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "archive-target",
        }.get(key, default)), patch(
            "app.modules.download_tracker.get_download_tracker", return_value=tracker,
        ):
            first = create_share_request(
                self.preview_id,
                ["file-2", "file-1", "file-2"],
                "dir-target",
                "chat-100",
                user_id="user-200",
                target_name="电视剧",
                client=self.client,
                store=self.store,
                tracker_chat_id="chat-100",
            )
            second = create_share_request(
                self.preview_id,
                ["file-1", "file-2"],
                "dir-target",
                "chat-100",
                user_id="user-200",
                target_name="电视剧",
                client=self.client,
                store=self.store,
                tracker_chat_id="chat-100",
            )

        self.assertTrue(first["success"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(self.client.create_dir_calls), 1)
        staging_name, staging_parent = self.client.create_dir_calls[0]
        self.assertTrue(staging_name.startswith(
            f"MediaFlux-share-{first['request_id']}-"
        ))
        self.assertEqual(staging_parent, "dir-target")
        self.assertEqual(self.client.restore_calls, [
            (SECRET_TOKEN, ["file-1", "file-2"], "staging-dir-target"),
        ])
        tracker.reload.assert_called_once_with()

        row = db.get_download_request(first["request_id"])
        self.assertIsNotNone(row)
        self.assertRegex(str(row["request_key"]), r"^[0-9a-f]{64}$")
        self.assertEqual(row["kind"], "guangya_share")
        self.assertEqual(row["source_value"], "")
        self.assertEqual(row["chat_id"], "chat-100")
        self.assertEqual(row["gy_status"], "completed")
        self.assertEqual(row["gy_target_dir"], "staging-dir-target")
        self.assertEqual(row["gy_isolated"], 1)
        self.assertEqual(row["gy_staging_parent_dir"], "dir-target")
        self.assertEqual(row["gy_staging_cleanup_status"], "pending")
        persisted = repr(dict(row)) + repr([dict(item) for item in db.list_download_logs(source="guangya_share")])
        self.assertNotIn(SECRET_TOKEN, persisted)
        self.assertNotIn(SHARE_URL, persisted)
        self.assertNotIn(SIGNED_URL, persisted)


    def test_existing_deterministic_staging_is_reused_after_pre_persist_crash(self):
        from app.modules.share_transfer import create_share_request

        self.client.list_dir = Mock(return_value=[
            SimpleNamespace(
                file_id="staging-recovered", name="MediaFlux-share-recovered",
                is_dir=True, size=0,
            )
        ])
        tracker = Mock()
        with patch("app.modules.share_transfer.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "archive-target",
        }.get(key, default)), patch(
            "app.modules.share_transfer._share_staging_name",
            return_value="MediaFlux-share-recovered",
        ), patch(
            "app.modules.download_tracker.get_download_tracker", return_value=tracker,
        ):
            result = create_share_request(
                self.preview_id, ["file-1"], "dir-target", "chat-100",
                user_id="user-200", target_name="电视剧", client=self.client,
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertTrue(result["success"])
        self.assertEqual(self.client.create_dir_calls, [])
        self.assertEqual(self.client.restore_calls, [
            (SECRET_TOKEN, ["file-1"], "staging-recovered"),
        ])
        row = db.get_download_request(result["request_id"])
        self.assertEqual(row["gy_target_dir"], "staging-recovered")
        self.assertEqual(row["gy_staging_name"], "MediaFlux-share-recovered")

    def test_create_error_recovers_preallocated_staging_by_exact_name(self):
        from app.modules.share_transfer import create_share_request

        state = {"created": False}
        staging_name = "MediaFlux-share-recovery-a1b2c3d4"

        def list_dir(parent_id="0"):
            if state["created"]:
                return [SimpleNamespace(
                    file_id="staging-after-error", name=staging_name,
                    is_dir=True, size=0,
                )]
            return []

        def create_dir(name, parent_id="0"):
            state["created"] = True
            raise TimeoutError("response lost after create")

        self.client.list_dir = Mock(side_effect=list_dir)
        self.client.create_dir = Mock(side_effect=create_dir)
        tracker = Mock()
        with patch("app.modules.share_transfer.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "archive-target",
        }.get(key, default)), patch(
            "app.modules.share_transfer._share_staging_name", return_value=staging_name,
        ), patch(
            "app.modules.download_tracker.get_download_tracker", return_value=tracker,
        ):
            result = create_share_request(
                self.preview_id, ["file-1"], "dir-target", "chat-100",
                user_id="user-200", target_name="电视剧", client=self.client,
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertTrue(result["success"])
        self.client.create_dir.assert_called_once_with(staging_name, "dir-target")
        self.assertEqual(self.client.restore_calls, [
            (SECRET_TOKEN, ["file-1"], "staging-after-error"),
        ])

    def test_multiple_preallocated_staging_matches_fail_closed(self):
        from app.modules.share_transfer import create_share_request

        staging_name = "MediaFlux-share-duplicate-a1b2c3d4"
        self.client.list_dir = Mock(return_value=[
            SimpleNamespace(file_id="staging-a", name=staging_name, is_dir=True, size=0),
            SimpleNamespace(file_id="staging-b", name=staging_name, is_dir=True, size=0),
        ])
        with patch("app.modules.share_transfer.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "archive-target",
        }.get(key, default)), patch(
            "app.modules.share_transfer._share_staging_name", return_value=staging_name,
        ):
            result = create_share_request(
                self.preview_id, ["file-1"], "dir-target", "chat-100",
                user_id="user-200", target_name="电视剧", client=self.client,
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertFalse(result["success"])
        self.assertIn("多个同名", result["error"])
        self.assertEqual(self.client.restore_calls, [])
        row = db.get_download_request(result["request_id"])
        self.assertEqual(row["status"], "failed")

    def test_foreign_selected_id_fails_before_cloud_write_or_persistence(self):
        from app.modules.share_transfer import create_share_request

        with self.assertRaisesRegex(ValueError, "不属于"):
            create_share_request(
                self.preview_id,
                ["file-1", "foreign-file"],
                "dir-target",
                "chat-100",
                user_id="user-200",
                client=self.client,
                store=self.store,
            )
        self.assertEqual(self.client.restore_calls, [])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 0)

    def test_pending_duplicate_returns_existing_request_without_second_cloud_write(self):
        from app.modules.share_transfer import create_share_request, share_request_key

        key = share_request_key(
            "demo123", ["file-1"], "dir-target", "chat-100", "user-200",
        )
        existing_id, created = db.create_share_transfer_request(
            key, title="existing", chat_id="chat-100", origin="telegram",
        )
        self.assertTrue(created)

        result = create_share_request(
            self.preview_id, ["file-1"], "dir-target", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["request_id"], existing_id)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.client.restore_calls, [])

    def test_cloud_failure_does_not_persist_or_return_upstream_signed_url(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(return_value={
            "success": False, "error": f"upstream failed: {SIGNED_URL}",
        })
        result = create_share_request(
            self.preview_id, ["file-3"], "dir-failure", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )

        self.assertFalse(result["success"])
        self.assertNotIn(SIGNED_URL, repr(result))
        row = db.get_download_request(result["request_id"])
        persisted = repr(dict(row)) + repr([
            dict(item) for item in db.list_download_logs(source="guangya_share")
        ])
        self.assertNotIn(SIGNED_URL, persisted)

    def test_failed_request_can_be_explicitly_retried_once_without_new_request(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(side_effect=[
            {"success": False, "retry_safe": True},
            {"success": True, "task_id": "retry-ok"},
        ])
        first = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )
        second = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )
        third = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )

        self.assertFalse(first["success"])
        self.assertTrue(second["success"])
        self.assertTrue(second["retried"])
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(third["duplicate"])
        self.assertEqual(self.client.restore_share.call_count, 2)

    def test_indeterminate_failure_never_retries_cloud_write(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(side_effect=[
            TimeoutError("provider response lost"),
            {"success": True},
        ])
        first = create_share_request(
            self.preview_id, ["file-1"], "dir-uncertain", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )
        second = create_share_request(
            self.preview_id, ["file-1"], "dir-uncertain", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )

        self.assertFalse(first["success"])
        self.assertEqual(first["status"], "manual_review")
        self.assertIn("核对", first["error"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["status"], "manual_review")
        self.assertEqual(self.client.restore_share.call_count, 1)

    def test_second_explicit_failure_cannot_be_retried_again(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(side_effect=[
            {"success": False, "retry_safe": True},
            {"success": False, "retry_safe": True},
            {"success": True},
        ])
        first = create_share_request(
            self.preview_id, ["file-2"], "dir-retry-limit", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )
        second = create_share_request(
            self.preview_id, ["file-2"], "dir-retry-limit", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )
        third = create_share_request(
            self.preview_id, ["file-2"], "dir-retry-limit", "chat-100",
            user_id="user-200", client=self.client, store=self.store,
        )

        self.assertFalse(first["success"])
        self.assertTrue(second["retried"])
        self.assertFalse(second["success"])
        self.assertTrue(third["duplicate"])
        self.assertEqual(self.client.restore_share.call_count, 2)

    def test_success_without_configured_follow_up_does_not_wake_tracker(self):
        from app.modules.share_transfer import create_share_request

        tracker = Mock()
        with patch("app.modules.share_transfer.get", return_value=""), patch(
            "app.modules.download_tracker.get_download_tracker", return_value=tracker,
        ):
            result = create_share_request(
                self.preview_id,
                ["file-1"],
                "dir-target",
                "chat-100",
                user_id="user-200",
                client=self.client,
                store=self.store,
            )
        self.assertTrue(result["success"])
        self.assertEqual(self.client.create_dir_calls, [])
        self.assertEqual(self.client.restore_calls[-1][2], "dir-target")
        tracker.reload.assert_not_called()


class TelegramShareViewTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.modules.share_transfer import ShareTransferPreviewStore

        counter = iter(f"opaque-{index:03d}" for index in range(200))
        self.store = ShareTransferPreviewStore(
            token_factory=lambda: next(counter),
            action_token_factory=lambda: next(counter),
        )
        self.preview_id = self.store.create(
            inspected_payload(11),
            chat_id="chat",
            user_id="user",
        )

    def test_file_view_has_pagination_selection_target_confirm_cancel_and_opaque_callbacks(self):
        from app.bot.handlers import _share_selection_view

        text, markup = _share_selection_view(
            FAKE_TELEBOT,
            self.preview_id,
            chat_id="chat",
            user_id="user",
            page=0,
            store=self.store,
        )
        labels = [button.text for button in markup.buttons]
        callbacks = [button.callback_data for button in markup.buttons]

        self.assertIn("全选", labels)
        self.assertIn("全不选", labels)
        self.assertTrue(any("目标目录" in label for label in labels))
        self.assertIn("确认转存", labels)
        self.assertIn("取消", labels)
        self.assertIn("下一页", labels)
        self.assertIn("1/2", text)
        for callback in callbacks:
            self.assertRegex(callback, r"^gys:[A-Za-z0-9_-]{8,}$")
            self.assertNotIn("file-", callback)
            self.assertNotIn("dir-", callback)
            self.assertNotIn(SECRET_TOKEN, callback)
            self.assertNotIn("guangyapan", callback)

    def test_target_directory_view_lists_only_directories_and_callbacks_stay_opaque(self):
        from app.bot.handlers import _share_target_view

        client = FakeShareClient()
        text, markup = _share_target_view(
            FAKE_TELEBOT,
            self.preview_id,
            chat_id="chat",
            user_id="user",
            parent_id="0",
            parent_name="根目录",
            page=0,
            client=client,
            store=self.store,
        )
        labels = [button.text for button in markup.buttons]
        callbacks = [button.callback_data for button in markup.buttons]

        self.assertIn("选择当前目录", labels)
        self.assertIn("电影", labels)
        self.assertNotIn("readme.txt", labels)
        self.assertIn("目标目录", text)
        self.assertTrue(all(re.fullmatch(r"gys:[A-Za-z0-9_-]{8,}", value) for value in callbacks))
        self.assertTrue(all("dir-private-1" not in value for value in callbacks))
        self.assertEqual(client.close_calls, 0)

    def test_target_directory_view_closes_owned_client_when_rendering_fails(self):
        from app.bot.handlers import _share_target_view

        client = FakeShareClient()
        client.logged_in = False
        with patch("app.clients.guangya.GuangYaClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "未登录"):
                _share_target_view(
                    FAKE_TELEBOT,
                    self.preview_id,
                    chat_id="chat",
                    user_id="user",
                    store=self.store,
                )

        self.assertEqual(client.close_calls, 1)

    def test_telegram_share_inspection_closes_short_lived_client_on_success_and_failure(self):
        from app.bot.handlers import _inspect_telegram_share

        message = SimpleNamespace(
            chat=SimpleNamespace(id="chat"),
            from_user=SimpleNamespace(id="user"),
        )
        bot = Mock()
        successful = FakeShareClient()
        failed = FakeShareClient()
        failed.inspect_share = Mock(side_effect=RuntimeError("upstream failed"))

        with patch(
            "app.modules.share_transfer.get_share_transfer_store", return_value=self.store,
        ), patch(
            "app.modules.share_transfer.GuangYaClient", side_effect=[successful, failed],
        ):
            _inspect_telegram_share(bot, message, SHARE_URL, FAKE_TELEBOT)
            _inspect_telegram_share(bot, message, SHARE_URL, FAKE_TELEBOT)

        self.assertEqual(successful.close_calls, 1)
        self.assertEqual(failed.close_calls, 1)
        self.assertEqual(bot.reply_to.call_count, 2)
        self.assertIn("解析失败", bot.reply_to.call_args.args[1])


class ShareApiSecurityTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        from app.main import create_app

        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self._login(self.client)
        page = self.client.get("/guangya/more")
        self.headers = {"X-CSRF-Token": self._csrf(page.text)}

    @staticmethod
    def _csrf(html_text: str) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', html_text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', html_text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    @classmethod
    def _login(cls, client: TestClient) -> None:
        login_page = client.get("/login")
        username, password = web_credentials()
        response = client.post(
            "/login",
            data={
                "csrf_token": cls._csrf(login_page.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        if response.status_code != 302:
            raise AssertionError(f"登录失败: {response.status_code}")

    def test_web_inspect_does_not_echo_upstream_exception_with_share_code(self):
        fake = FakeShareClient()
        fake.inspect_share = Mock(side_effect=ValueError(f"invalid url: {SHARE_URL}"))
        with patch("app.routes.share_api.GuangYaClient", return_value=fake):
            response = self.client.post(
                "/api/share/inspect", headers=self.headers, json={"url": SHARE_URL},
            )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(SHARE_URL, response.text)
        self.assertNotIn("2468", response.text)

    def test_web_share_api_keeps_auth_csrf_session_isolation_and_secret_redaction(self):
        fake = FakeShareClient()
        with patch("app.routes.share_api.GuangYaClient", return_value=fake):
            no_csrf = self.client.post("/api/share/inspect", json={"url": SHARE_URL})
            inspected = self.client.post(
                "/api/share/inspect",
                headers=self.headers,
                json={"url": SHARE_URL},
            )

        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(inspected.status_code, 200)
        payload = inspected.json()
        self.assertNotIn(SECRET_TOKEN, repr(payload))
        self.assertNotIn(SIGNED_URL, repr(payload))
        self.assertNotIn(SHARE_URL, repr(payload))

        other = TestClient(self.client.app, raise_server_exceptions=False)
        self._login(other)
        other_page = other.get("/guangya/more")
        with patch("app.routes.share_api.GuangYaClient", return_value=fake):
            cross_session = other.post(
                "/api/share/restore",
                headers={"X-CSRF-Token": self._csrf(other_page.text)},
                json={
                    "preview_id": payload["preview_id"],
                    "file_ids": ["file-1"],
                    "target_dir_id": "dir-target",
                },
            )
            restored = self.client.post(
                "/api/share/restore",
                headers=self.headers,
                json={
                    "preview_id": payload["preview_id"],
                    "file_ids": ["file-1"],
                    "target_dir_id": "dir-target",
                    "target_dir_name": "电影",
                },
            )

        self.assertIn(cross_session.status_code, {400, 410})
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["success"])
        self.assertNotIn(SECRET_TOKEN, restored.text)
        self.assertNotIn(SIGNED_URL, restored.text)

        request_id = restored.json()["request_id"]
        own_status = self.client.get(f"/api/share/requests/{request_id}")
        cross_status = other.get(f"/api/share/requests/{request_id}")
        self.assertEqual(own_status.status_code, 200)
        self.assertTrue(own_status.json()["terminal"])
        self.assertTrue(own_status.json()["success"])
        self.assertEqual(cross_status.status_code, 404)
        self.assertNotIn(SECRET_TOKEN, own_status.text)
        self.assertNotIn(SIGNED_URL, own_status.text)


if __name__ == "__main__":
    unittest.main()
