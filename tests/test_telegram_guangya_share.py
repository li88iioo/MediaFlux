from __future__ import annotations

import re
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaClient
from app.config import web_credentials
from tests.support import IsolatedDatabaseTestCase
from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright

SHARE_URL = "https://www.guangyapan.com/s/demo123?code=2468"
HASH_ROUTE_SHARE_URL = (
    "https://www.guangyapan.com/s/"
    "1938060615538466851_aecd7hu726g3whGl#/share"
)
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
        self.credential_generation = 0
        self.inspected = inspected or inspected_payload()
        self.inspect_calls: list[str] = []
        self.restore_calls: list[tuple[str, list[str], str]] = []
        self.create_dir_calls: list[tuple[str, str]] = []
        self.close_calls = 0

    def inspect_share(self, share_url: str) -> dict:
        self.inspect_calls.append(share_url)
        return self.inspected

    def create_dir(self, name: str, parent_id: str = "0", *,
                   before_write: Callable[[], None] | None = None) -> str:
        if before_write is not None:
            before_write()
        self.create_dir_calls.append((name, parent_id))
        return f"staging-{parent_id}"

    def restore_share(self, access_token: str, file_ids: list[str], target_dir_id: str, *, cancel_check=None) -> dict:
        if cancel_check:
            cancel_check()
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
        self.rows = []

    def add(self, *buttons, row_width=None) -> None:
        width = max(1, int(row_width or self.row_width))
        self.buttons.extend(buttons)
        self.rows.extend(
            list(buttons[index:index + width])
            for index in range(0, len(buttons), width)
        )


FAKE_TELEBOT = SimpleNamespace(types=SimpleNamespace(
    InlineKeyboardMarkup=FakeMarkup,
    InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
        text=text, callback_data=callback_data,
    ),
))


class GuangYaShareRoutingTests(unittest.TestCase):
    def test_guangya_share_url_routes_before_generic_http(self):
        from app.modules.download_dispatcher import (
            extract_download_url,
            route_download_url,
        )

        self.assertEqual(route_download_url(SHARE_URL), "guangya_share")
        self.assertEqual(route_download_url(HASH_ROUTE_SHARE_URL), "guangya_share")
        self.assertEqual(
            extract_download_url(f"请解析 {HASH_ROUTE_SHARE_URL}"),
            HASH_ROUTE_SHARE_URL,
        )
        self.assertEqual(route_download_url("https://example.invalid/archive.iso"), "http")
        self.assertEqual(route_download_url("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"), "magnet")

    def test_guangya_detector_rejects_lookalike_hosts_and_non_share_paths(self):
        from app.modules.download_dispatcher import is_guangya_share_url

        self.assertTrue(is_guangya_share_url("https://guangyapan.com/share/ABC123"))
        self.assertTrue(is_guangya_share_url("https://www.guangyapan.com/s/ABC123?pwd=1"))
        self.assertTrue(is_guangya_share_url("https://www.guangyapan.com/S/ABC123?code=1"))
        self.assertTrue(is_guangya_share_url(HASH_ROUTE_SHARE_URL))
        self.assertTrue(is_guangya_share_url("https://www.guangyapan.com/s/ABC-123_def"))
        self.assertFalse(is_guangya_share_url("https://guangyapan.com.evil.invalid/s/ABC123"))
        self.assertFalse(is_guangya_share_url("https://www.guangyapan.com/download/ABC123"))

    def test_guangya_client_keeps_the_complete_hash_route_share_id(self):
        from app.clients.guangya import GuangYaClient

        self.assertEqual(
            GuangYaClient._parse_share(HASH_ROUTE_SHARE_URL),
            ("1938060615538466851_aecd7hu726g3whGl", ""),
        )


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
        for response in (
            {"code": 500, "msg": "provider rejected"},
            {"code": 200, "success": False, "msg": "保存失败"},
            {"code": 0, "data": {"success": False, "msg": "空间不足"}},
            {"code": 200, "msg": "success", "data": {"code": 403, "msg": "无权限"}},
            {"success": True, "data": {"success": False}},
            {"success": True, "data": {"code": 166, "msg": "目录不可用"}},
            {"code": 0, "msg": "转存失败"},
        ):
            with self.subTest(response=response):
                result = self._client_with_response(response).restore_share(
                    "share-token", ["file-1"], "target"
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["retry_safe"])
                self.assertEqual(result["count"], 0)

    def test_restore_classifies_ambiguous_response_as_manual_review(self):
        for response in (None, {}, [], "", {"msg": "pending"}, {"msg": "转存失败"}, {"data": {"code": 0}}):
            with self.subTest(response=response):
                result = self._client_with_response(response).restore_share(
                    "share-token", ["file-1"], "target"
                )
                self.assertFalse(result["success"])
                self.assertFalse(result["retry_safe"])
                self.assertEqual(result["count"], 0)

    def test_restore_accepts_explicit_success_code(self):
        for response in ({"code": 0}, {"code": "200"}, {"success": True}, {"msg": "success"}):
            with self.subTest(response=response):
                result = self._client_with_response(response).restore_share(
                    "share-token", ["file-1"], "target"
                )
                self.assertTrue(result["success"])
                self.assertFalse(result["retry_safe"])
                self.assertEqual(result["count"], 1)

    def test_restore_checks_cancellation_after_raw_initialization(self):
        import asyncio
        from app.agent.kernel.state_types import CancellationToken

        token = CancellationToken()
        raw = Mock()

        class InitializingClient(GuangYaClient):
            @property
            def raw(self):
                token.cancel()
                return raw

        client = InitializingClient.__new__(InitializingClient)
        with self.assertRaises(asyncio.CancelledError):
            client.restore_share(
                "share-token", ["file-1"], "target",
                cancel_check=token.raise_if_cancelled,
            )
        raw.share_restore.assert_not_called()


class ShareTransferPreviewStoreTests(unittest.TestCase):
    def test_inspection_uses_the_saved_share_target_as_the_default(self):
        from app.modules.share_transfer import (
            ShareTransferPreviewStore,
            inspect_share_for_transfer,
        )

        store = ShareTransferPreviewStore(token_factory=lambda: "configured-preview")
        client = FakeShareClient()
        with patch(
            "app.modules.share_transfer.get",
            side_effect=lambda key, default="": {
                "GY_SHARE_TARGET_DIR": "saved-target",
                "GY_SHARE_TARGET_DIR_NAME": "分享转存目录",
            }.get(key, default),
        ):
            preview = inspect_share_for_transfer(
                HASH_ROUTE_SHARE_URL,
                "chat",
                "user",
                client=cast(GuangYaClient, client),
                store=store,
            )

        self.assertEqual(preview["target_id"], "saved-target")
        self.assertEqual(preview["target_name"], "分享转存目录")
        self.assertEqual(client.inspect_calls, [HASH_ROUTE_SHARE_URL])

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
            credential_generation=self.client.credential_generation,
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
                client=cast(GuangYaClient, self.client),
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
                client=cast(GuangYaClient, self.client),
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

        row = download_requests_repo.get_download_request(first["request_id"])
        assert (row) is not None
        self.assertRegex(str(row["request_key"]), r"^[0-9a-f]{64}$")
        self.assertEqual(row["kind"], "guangya_share")
        self.assertEqual(row["source_value"], "")
        self.assertEqual(row["chat_id"], "chat-100")
        self.assertEqual(row["gy_status"], "completed")
        self.assertEqual(row["gy_target_dir"], "staging-dir-target")
        self.assertEqual(row["gy_isolated"], 1)
        self.assertEqual(row["gy_staging_parent_dir"], "dir-target")
        self.assertEqual(row["gy_staging_cleanup_status"], "pending")
        persisted = repr(dict(row)) + repr([dict(item) for item in download_requests_repo.list_download_logs(source="guangya_share")])
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
                user_id="user-200", target_name="电视剧", client=cast(GuangYaClient, self.client),
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertTrue(result["success"])
        self.assertEqual(self.client.create_dir_calls, [])
        self.assertEqual(self.client.restore_calls, [
            (SECRET_TOKEN, ["file-1"], "staging-recovered"),
        ])
        row = download_requests_repo.get_download_request(result["request_id"])
        assert row is not None
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

        def create_dir(name, parent_id="0", *, before_write):
            before_write()
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
                user_id="user-200", target_name="电视剧", client=cast(GuangYaClient, self.client),
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertTrue(result["success"])
        self.client.create_dir.assert_called_once()
        self.assertEqual(self.client.create_dir.call_args.args, (staging_name, "dir-target"))
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
                user_id="user-200", target_name="电视剧", client=cast(GuangYaClient, self.client),
                store=self.store, tracker_chat_id="chat-100",
            )

        self.assertFalse(result["success"])
        self.assertIn("多个同名", result["error"])
        self.assertEqual(self.client.restore_calls, [])
        row = download_requests_repo.get_download_request(result["request_id"])
        assert row is not None
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
                client=cast(GuangYaClient, self.client),
                store=self.store,
            )
        self.assertEqual(self.client.restore_calls, [])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 0)

    def test_submitting_duplicate_returns_existing_request_without_second_cloud_write(self):
        from app.modules.share_transfer import create_share_request, share_request_key

        key = share_request_key(
            "demo123", ["file-1"], "dir-target", "chat-100", "user-200",
        )
        existing_id, created = download_requests_repo.create_share_transfer_request(
            key, title="existing", chat_id="chat-100", origin="telegram",
        )
        self.assertTrue(created)

        result = create_share_request(
            self.preview_id, ["file-1"], "dir-target", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["request_id"], existing_id)
        self.assertEqual(result["status"], "submitting")
        self.assertEqual(self.client.restore_calls, [])

    def test_cloud_failure_does_not_persist_or_return_upstream_signed_url(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(return_value={
            "success": False, "error": f"upstream failed: {SIGNED_URL}",
        })
        result = create_share_request(
            self.preview_id, ["file-3"], "dir-failure", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )

        self.assertFalse(result["success"])
        self.assertNotIn(SIGNED_URL, repr(result))
        row = download_requests_repo.get_download_request(result["request_id"])
        assert row is not None
        persisted = repr(dict(row)) + repr([
            dict(item) for item in download_requests_repo.list_download_logs(source="guangya_share")
        ])
        self.assertNotIn(SIGNED_URL, persisted)

    def test_failed_request_can_be_explicitly_retried_once_without_new_request(self):
        from app.modules.share_transfer import create_share_request

        response = {"code": 0, "data": {"success": False, "msg": "空间不足"}}
        normalizer = GuangYaRestoreResultTests._client_with_response(response)
        self.client.restore_share = Mock(wraps=normalizer.restore_share)
        first = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )
        self.assertEqual(first["status"], "failed")
        failed = download_requests_repo.get_download_request(first["request_id"])
        assert failed is not None
        self.assertEqual(failed["gy_status"], "failed")
        response.clear()
        response["code"] = 0
        second = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )
        third = create_share_request(
            self.preview_id, ["file-1"], "dir-retry", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
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
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )
        second = create_share_request(
            self.preview_id, ["file-1"], "dir-uncertain", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
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
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )
        second = create_share_request(
            self.preview_id, ["file-2"], "dir-retry-limit", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )
        third = create_share_request(
            self.preview_id, ["file-2"], "dir-retry-limit", "chat-100",
            user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
        )

        self.assertFalse(first["success"])
        self.assertTrue(second["retried"])
        self.assertFalse(second["success"])
        self.assertTrue(third["duplicate"])
        self.assertEqual(self.client.restore_share.call_count, 2)

    def test_cleaning_logs_between_failures_does_not_change_single_retry_budget(self):
        from app.modules.share_transfer import create_share_request

        self.client.restore_share = Mock(side_effect=[
            {"success": False, "retry_safe": True},
            {"success": False, "retry_safe": True},
            {"success": True},
        ])

        def submit():
            return create_share_request(
                self.preview_id, ["file-2"], "dir-cleaned-retry", "chat-100",
                user_id="user-200", client=cast(GuangYaClient, self.client), store=self.store,
            )

        first = submit()
        row = download_requests_repo.get_download_request(first["request_id"])
        assert row is not None
        self.assertEqual((row["status"], row["share_retry_count"]), ("failed", 0))
        download_requests_repo.delete_download_logs([row["id"] for row in download_requests_repo.list_download_logs(source="guangya_share")])
        second = submit()
        self.assertTrue(second["retried"])
        self.assertFalse(second["success"])
        self.assertEqual(second["request_id"], first["request_id"])
        download_requests_repo.delete_download_logs([row["id"] for row in download_requests_repo.list_download_logs(source="guangya_share")])
        third = submit()
        self.assertTrue(third["duplicate"])
        self.assertEqual(third["status"], "failed")
        row = download_requests_repo.get_download_request(first["request_id"])
        assert row is not None
        self.assertEqual(row["share_retry_count"], 1)
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
                client=cast(GuangYaClient, self.client),
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
        row_labels = [[button.text for button in row] for row in markup.rows]
        self.assertIn(["全选", "全不选"], row_labels)
        self.assertIn(["确认转存", "取消"], row_labels)
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


class TelegramShareConfirmationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")

    def test_switch_after_telegram_confirmation_stops_background_transfer(self):
        from app.bot.handlers import _handle_share_callback, _inspect_telegram_share
        from app.modules.share_transfer import ShareTransferPreviewStore

        store = ShareTransferPreviewStore()
        before, after = FakeShareClient(), FakeShareClient()
        after.credential_generation = 1
        bot = Mock()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=100), from_user=SimpleNamespace(id=200),
            message_id=5, message_thread_id=42,
        )
        workers = []

        def hold_worker(*, target, args, **_kwargs):
            return SimpleNamespace(start=lambda: workers.append((target, args)))

        with (
            patch("app.modules.share_transfer.get_share_transfer_store", return_value=store),
            patch("app.modules.share_transfer.GuangYaClient", side_effect=[before, after]),
            patch("app.bot.handlers.threading.Thread", side_effect=hold_worker),
        ):
            _inspect_telegram_share(bot, message, SHARE_URL, FAKE_TELEBOT)
            keyboard = bot.reply_to.call_args.kwargs["reply_markup"]
            confirm = next(button for button in keyboard.buttons if button.text == "确认转存")
            _handle_share_callback(
                bot, SimpleNamespace(id="confirm", data=confirm.callback_data, message=message,
                                     from_user=message.from_user), FAKE_TELEBOT,
            )
            self.assertIn("已受理", bot.answer_callback_query.call_args.args[1])
            self.assertEqual(len(workers), 1)
            run, args = workers[0]
            run(*args)

        self.assertFalse(before.restore_calls or after.restore_calls or after.create_dir_calls)
        self.assertIn("重新解析", bot.edit_message_text.call_args.args[0])
        with db.get_conn() as conn:
            row = conn.execute("SELECT status,gy_status FROM download_requests").fetchone()
        self.assertEqual(tuple(row), ("failed", "failed"))

    def test_telegram_timeouts_preserve_the_persisted_transfer_and_its_receipt(self):
        from app.bot.handlers import _handle_share_callback, _share_selection_view
        from app.modules.share_transfer import ShareTransferPreviewStore

        store = ShareTransferPreviewStore()
        client = FakeShareClient()

        def run_worker(*, target, args, **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))

        with (
            patch("app.modules.share_transfer.get_share_transfer_store", return_value=store),
            patch("app.modules.share_transfer.GuangYaClient", return_value=client),
            patch("app.bot.handlers.threading.Thread", side_effect=run_worker),
        ):
            for attempt, failing_text in enumerate(("正在转存", "光鸭分享转存完成"), 1):
                with self.subTest(failing_text=failing_text):
                    chat_id, user_id = 100, 200
                    target_id = str(attempt - 1)
                    preview_id = store.create(client.inspected, chat_id, user_id, target_id=target_id,
                                              credential_generation=client.credential_generation)
                    _, markup = _share_selection_view(
                        FAKE_TELEBOT, preview_id, chat_id=str(chat_id), user_id=str(user_id), store=store,
                    )
                    confirm = next(button for button in markup.buttons if button.text == "确认转存")
                    call = SimpleNamespace(
                        id="callback", data=confirm.callback_data,
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=5, message_thread_id=42),
                        from_user=SimpleNamespace(id=user_id),
                    )
                    bot = Mock()
                    receipts = []

                    def edit(text, *_args, **_kwargs):
                        if text.startswith("正在转存"):
                            with db.get_conn() as conn:
                                status = conn.execute(
                                    "SELECT status FROM download_requests ORDER BY id DESC LIMIT 1"
                                ).fetchone()
                            self.assertIsNotNone(status)
                            self.assertEqual(status["status"], "submitting")
                        if failing_text in text:
                            raise TimeoutError("Telegram timeout")
                        receipts.append(text)

                    bot.edit_message_text.side_effect = edit
                    if attempt == 1:
                        bot.answer_callback_query.side_effect = TimeoutError("Telegram timeout")
                    _handle_share_callback(bot, call, FAKE_TELEBOT)

                    with db.get_conn() as conn:
                        rows = conn.execute("SELECT * FROM download_requests ORDER BY id").fetchall()
                    self.assertEqual(len(rows), attempt)
                    row = rows[-1]
                    self.assertEqual((row["status"], row["gy_status"]), ("completed", "completed"))
                    self.assertEqual(client.restore_calls[-1][-1], target_id)
                    if attempt == 1:
                        self.assertEqual(len(receipts), 1)
                        receipt = receipts[0]
                    else:
                        self.assertEqual(len(receipts), 1)
                        bot.reply_to.assert_called_once()
                        self.assertEqual(bot.reply_to.call_args.kwargs["message_thread_id"], 42)
                        receipt = bot.reply_to.call_args.args[1]
                    self.assertIn("光鸭分享转存完成", receipt)
                    self.assertIn(f"#{row['id']}", receipt)
                    self.assertNotIn("转存失败", receipt)

                    bot.answer_callback_query.side_effect = None
                    _handle_share_callback(bot, call, FAKE_TELEBOT)
                    self.assertIn("下载记录核对", bot.answer_callback_query.call_args.args[1])
                    self.assertEqual(len(client.restore_calls), attempt)


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

    def test_first_previews_from_the_same_browser_cookie_keep_the_same_owner(self):
        # 两个请求从同一份初始 cookie 发出，第二个响应最后写回浏览器。
        sibling = TestClient(self.client.app, raise_server_exceptions=False)
        self.addCleanup(sibling.close)
        sibling.cookies.update(self.client.cookies)
        fake = FakeShareClient()
        with patch("app.routes.share_api.GuangYaClient", return_value=fake):
            first = self.client.post('/api/share/inspect', headers=self.headers, json={'url': SHARE_URL})
            second = sibling.post('/api/share/inspect', headers=self.headers, json={'url': SHARE_URL})
            self.assertEqual((first.status_code, second.status_code), (200, 200))
            restored = sibling.post('/api/share/restore', headers=self.headers, json={
                'preview_id': first.json()['preview_id'], 'file_ids': ['file-1'],
                'target_dir_id': 'dir-target', 'target_dir_name': '电影',
            })
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()['success'])
        self.assertEqual(len(fake.restore_calls), 1)

    def test_web_and_agent_share_preview_cannot_write_after_switching_accounts(self):
        before, after = FakeShareClient(), FakeShareClient()
        after.credential_generation = 1
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]
        with patch("app.routes.share_api.GuangYaClient", side_effect=[before, after, after, after]):
            first = self.client.post("/api/share/inspect", headers=self.headers, json={"url": SHARE_URL})
            self.assertEqual(first.status_code, 200)
            payload = {"preview_id": first.json()["preview_id"], "file_ids": ["file-1"],
                       "target_dir_id": "0", "target_dir_name": "根目录"}
            stale = self.client.post("/api/share/restore", headers=self.headers, json=payload)
            self.assertEqual(stale.status_code, 410, stale.text)
            self.assertIn("重新解析", stale.json()["error"])
            self.assertFalse(before.restore_calls or after.restore_calls)
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], count)

            fresh = self.client.post("/api/share/inspect", headers=self.headers, json={"url": SHARE_URL})
            payload["preview_id"] = fresh.json()["preview_id"]
            submitted = self.client.post("/api/share/restore", headers=self.headers, json=payload)
            self.assertEqual(submitted.status_code, 200, submitted.text)
            self.assertEqual(len(after.restore_calls), 1)

    def test_share_preview_survives_access_token_rotation(self):
        from tests.test_guangya_token_operations import _RotatingRawClient

        restored_with = []

        class Raw(_RotatingRawClient):
            def share_restore(self, **kwargs):
                restored_with.append(self.token)
                return {"code": 0}

        with TemporaryDirectory() as directory:
            token_file = Path(directory) / "token.json"
            token_file.write_text('{"access_token":"access-before","refresh_token":"refresh-before"}', encoding="utf-8")
            with (
                patch("app.clients.guangya._load_raw", return_value=Raw),
                patch("app.routes.share_api.GuangYaClient", side_effect=lambda: GuangYaClient(token_file)),
                patch.object(GuangYaClient, "inspect_share", return_value=inspected_payload(1)),
            ):
                inspected = self.client.post("/api/share/inspect", headers=self.headers, json={"url": SHARE_URL})
                self.assertEqual(inspected.status_code, 200)
                renewing = GuangYaClient(token_file)
                try:
                    renewing.refresh_now()
                finally:
                    renewing.close()
                response = self.client.post("/api/share/restore", headers=self.headers, json={
                    "preview_id": inspected.json()["preview_id"], "file_ids": ["file-1"],
                    "target_dir_id": "0", "target_dir_name": "根目录",
                })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(restored_with, ["rotated-access-9999"])

    def test_web_share_api_keeps_auth_csrf_session_isolation_and_secret_redaction(self):
        fake = FakeShareClient()
        with patch("app.routes.share_api.GuangYaClient", return_value=fake), patch(
            "app.modules.share_transfer.get", side_effect=lambda key, default="": {
                "GY_SHARE_TARGET_DIR": "saved-share-dir", "GY_SHARE_TARGET_DIR_NAME": "分享默认目录",
            }.get(key, default),
        ):
            no_csrf = self.client.post("/api/share/inspect", json={"url": SHARE_URL})
            inspected = self.client.post(
                "/api/share/inspect",
                headers=self.headers,
                json={"url": SHARE_URL},
            )

        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(inspected.status_code, 200)
        payload = inspected.json()
        self.assertEqual((payload["target_id"], payload["target_name"]), ("saved-share-dir", "分享默认目录"))
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

@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class WebShareLifecycleBrowserTests(unittest.TestCase):
    """实际模板、CSS、app.js 与目录选择器；仅接口离线替换。"""

    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox", "--disable-background-networking"]
        )
        cls.addClassCleanup(cls.browser.close)
        from app.web import templates

        cls.root = Path(__file__).resolve().parents[1]
        cls.html = templates.env.from_string(
            '{% extends "base.html" %}{% block content %}{% include "_share_transfer_content.html" %}'
            '{% endblock %}{% block scripts %}{% include "_share_transfer_scripts.html" %}{% endblock %}'
        ).render(
            active="guangya_more", discovery_enabled=False, agent_enabled=False,
            app_version="offline-share", csrf_token=lambda: "offline-csrf",
            static_url=lambda value: "/static/" + value,
            url_for=lambda name: "/" + name.split(".")[-1],
        )

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(3000)
        self.errors, self.unexpected = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.route)
        self.page.add_init_script("""
            window.calls=[];
            window.fetch=(url,options={})=>new Promise((resolve,reject)=>{
                const call={url:String(url),options,resolve,reject}; calls.push(call);
                if(call.url.startsWith('/api/guangya/dirs')){
                    const parent=new URL(call.url,location.origin).searchParams.get('parent_id');
                    const files=parent==='0'?[{file_id:'drive',name:'我的光鸭',is_dir:true}]:
                        parent==='drive'?[{file_id:'folder-a',name:'目录 A',is_dir:true},{file_id:'folder-b',name:'任意目录 B',is_dir:true}]:[];
                    resolve({ok:true,status:200,json:async()=>files});
                }
            });
            window.reply=(index,data,status=200)=>calls[index].resolve({ok:status<400,status,json:async()=>data});
        """)
        self.page.goto("http://offline-share.invalid/")
        self.page.locator("#inspectShareBtn").wait_for()

    def route(self, route):
        from urllib.parse import urlsplit

        url = urlsplit(route.request.url)
        if url.netloc == "offline-share.invalid" and url.path == "/":
            route.fulfill(content_type="text/html", body=self.html)
        elif url.netloc == "offline-share.invalid" and url.path.startswith("/static/"):
            import mimetypes

            path = (self.root / "app" / url.path.lstrip("/")).resolve()
            if path.is_relative_to(self.root / "app/static") and path.is_file():
                route.fulfill(body=path.read_bytes(), content_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            else:
                self.unexpected.append(route.request.url)
                route.abort()
        else:
            self.unexpected.append(route.request.url)
            route.abort()

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])
        paths = self.page.evaluate("calls.map(call=>call.url)")
        self.assertTrue(all(path.startswith(('/api/share/', '/api/guangya/dirs')) for path in paths), paths)

    def ready_preview(self):
        self.inspect("a")

    def inspect(self, suffix, *, long_name=False):
        self.page.locator("#shareUrl").fill("https://share.invalid/" + suffix)
        self.page.locator("#inspectShareBtn").click()
        self.page.evaluate("""({suffix,longName})=>{
            const index=calls.findLastIndex(call=>call.url==='/api/share/inspect');
            reply(index,{preview_id:suffix,share_id:suffix,count:2,files:[
                {id:'file-'+suffix,name:longName?'电影'.repeat(150)+'.mkv':'Video '+suffix,is_dir:false,size:1000},
                {id:'second-'+suffix,name:'第二个视频.mp4',is_dir:false,size:2000},
            ]});
        }""", {"suffix": suffix, "longName": long_name})
        self.page.locator(f".share-file-check[value='file-{suffix}']").wait_for()

    def choose(self, name="目录 A"):
        self.page.locator("#restoreShareBtn").click()
        self.page.locator("#shareDirModal .settings-dir-open").filter(has_text="我的光鸭").click()
        self.page.locator("#shareDirModal .settings-dir-item").filter(has_text=name).locator(".settings-dir-select").click()
        self.page.locator("#appConfirmModal").wait_for(state="visible")
        self.assertEqual(self.page.locator("#shareDirModal").count(), 0)

    def submit(self, name="目录 A"):
        self.choose(name)
        self.page.locator("#appConfirmSubmit").click()
        self.page.wait_for_function("calls.at(-1).url==='/api/share/restore'")
        return self.page.evaluate("calls.length-1")

    def test_cancel_picker_and_confirmation_reopen_with_any_directory(self):
        self.ready_preview()
        self.page.locator("#restoreShareBtn").click()
        self.page.locator("#shareDirModal [data-dir-close]").click()
        self.choose()
        self.page.locator("#appConfirmCancel").focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn').disabled")
        self.page.wait_for_function("document.activeElement.id==='restoreShareBtn'")
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 0)
        pending = self.submit("任意目录 B")
        body = self.page.evaluate("index=>JSON.parse(calls[index].options.body)", pending)
        self.assertEqual((body["target_dir_id"], body["target_dir_name"]), ("folder-b", "任意目录 B"))
        self.page.evaluate("index=>reply(index,{success:true,count:2})", pending)
        self.page.wait_for_function("document.querySelector('#shareStateTag').textContent.includes('转存成功')")

    def test_repeated_save_does_not_duplicate_picker_or_submission(self):
        self.ready_preview()
        self.page.locator("#restoreShareBtn").click()
        self.page.locator("#shareDirModal .settings-dir-select").wait_for()
        self.page.locator("#restoreShareBtn").dispatch_event("click")
        self.assertEqual(self.page.locator("#shareDirModal").count(), 1)
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url.startsWith('/api/guangya/dirs')).length"), 1)
        self.page.locator("#shareDirModal .settings-dir-select").click()
        self.page.locator("#appConfirmSubmit").click()
        self.page.wait_for_function("calls.at(-1).url==='/api/share/restore'")
        self.page.locator("#restoreShareBtn").dispatch_event("click")
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 1)

    def test_link_change_discards_old_picker_and_confirmation(self):
        self.ready_preview()
        self.page.locator("#restoreShareBtn").click()
        self.inspect("b")
        self.assertEqual(self.page.locator("#shareDirModal").count(), 0)
        self.choose()
        self.page.locator("#shareUrl").evaluate("el=>{el.value='https://share.invalid/c';el.dispatchEvent(new Event('input'));}")
        self.page.locator("#appConfirmSubmit").click()
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn .spin')")
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 0)
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())

    def test_selection_change_before_confirmation_never_submits_old_files(self):
        self.ready_preview()
        self.choose()
        self.page.locator(".share-file-check").first.evaluate("el=>{el.checked=false;el.dispatchEvent(new Event('change'));}")
        self.page.locator("#appConfirmSubmit").click()
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn').disabled")
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 0)
        self.assertEqual(self.page.locator(".share-file-check:checked").count(), 1)

    def test_selection_change_during_post_keeps_receipt_and_consumes_preview(self):
        self.ready_preview()
        pending = self.submit()
        self.page.locator(".share-file-check").first.uncheck()
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())
        self.page.evaluate("index=>reply(index,{success:true,count:2,target_dir_name:'目录 A'})", pending)
        self.page.wait_for_function("document.querySelector('#shareStateTag').textContent.includes('转存成功')")
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())
        self.page.locator(".share-file-check").first.check()
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 1)

    def test_cancelled_confirmation_never_steals_focus_from_a_new_dialog(self):
        self.ready_preview()
        self.choose()
        self.page.evaluate("void appConfirm({title:'新的确认',message:'保留当前焦点',trigger:document.querySelector('#inspectShareBtn')})")
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn').disabled")
        self.assertEqual(self.page.locator("#appConfirmTitle").inner_text(), "新的确认")
        self.assertTrue(self.page.evaluate("Boolean(document.activeElement.closest('#appConfirmModal'))"))

    def test_explicit_rejection_releases_submission_for_retry(self):
        self.ready_preview()
        pending = self.submit()
        self.page.evaluate("index=>reply(index,{error:'目标目录不可用'},400)", pending)
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn').disabled")
        self.assertEqual(self.page.locator("#shareMessage").inner_text(), "目标目录不可用")
        self.submit("任意目录 B")
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url==='/api/share/restore').length"), 2)

    def test_lost_reply_then_duplicate_shows_existing_completion(self):
        from playwright.sync_api import expect

        self.ready_preview()
        pending = self.submit()
        self.page.evaluate("index=>calls[index].reject(new TypeError('NetworkError'))", pending)
        expect(self.page.locator("#shareStateTag")).to_have_text("状态待确认")
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())
        self.assertEqual(self.page.get_by_role("link", name="查看下载日志").get_attribute("href"), "/downloads?view=logs")

        self.ready_preview()
        pending = self.submit()
        self.page.evaluate("index=>reply(index,{success:true,duplicate:true,request_id:7,count:0})", pending)
        message = self.page.locator("#shareMessage")
        expect(message).to_contain_text("请求 #7 此前已完成")
        self.assertNotIn("0 项", message.inner_text())

    def test_request_id_in_error_reply_follows_manual_review_status(self):
        from playwright.sync_api import expect

        self.ready_preview()
        self.page.clock.install()
        pending = self.submit()
        self.page.evaluate("index=>reply(index,{error:'需要核对',request_id:7},502)", pending)
        expect(self.page.locator("#shareStateTag")).to_have_text("状态待确认")
        self.page.clock.fast_forward(2000)
        self.page.wait_for_function("calls.at(-1).url==='/api/share/requests/7'")
        self.page.evaluate("reply(calls.length-1,{terminal:true,status:'manual_review',error:'请核对云端结果'})")
        expect(self.page.locator("#shareStateTag")).to_have_text("需要核对")
        expect(self.page.locator("#shareMessage")).to_contain_text("请核对云端结果")
        self.assertTrue(self.page.locator("#restoreShareBtn").is_disabled())

    def test_old_submission_response_does_not_clear_new_preview(self):
        self.ready_preview()
        pending = self.submit()
        self.inspect("b")
        self.page.evaluate("index=>reply(index,{success:true,count:2,target_dir_name:'目录 A'})", pending)
        self.page.wait_for_function("!document.querySelector('#restoreShareBtn').disabled")
        self.assertIn("分享 ID: b", self.page.locator("#shareMessage").inner_text())
        pending = self.submit("任意目录 B")
        self.assertEqual(self.page.evaluate("index=>JSON.parse(calls[index].options.body).preview_id", pending), "b")

    def test_old_status_poll_does_not_overwrite_new_preview(self):
        self.ready_preview()
        self.page.clock.install()
        pending = self.submit()
        self.page.evaluate("index=>reply(index,{accepted:true,request_id:7},202)", pending)
        self.page.clock.fast_forward(2000)
        self.page.wait_for_function("calls.at(-1).url==='/api/share/requests/7'")
        poll = self.page.evaluate("calls.length-1")
        self.inspect("b")
        self.page.evaluate("index=>reply(index,{terminal:true,success:true,target_dir_name:'目录 A'})", poll)
        self.page.clock.fast_forward(10000)
        self.assertIn("分享 ID: b", self.page.locator("#shareMessage").inner_text())
        self.assertEqual(self.page.evaluate("calls.filter(call=>call.url.includes('/requests/')).length"), 1)

    def test_desktop_and_mobile_layout_with_real_css_and_picker(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.inspect(str(width), long_name=True)
                self.assertEqual(self.page.locator(".share-target-card, #saveShareTargetBtn, #shareTargetDir").count(), 0)
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                button = self.page.locator("#restoreShareBtn").bounding_box()
                assert button is not None
                self.assertGreaterEqual(button["height"], 44)
                self.assertLessEqual(button["x"] + button["width"], width)
                evidence = self.root / ".superpowers/tmp"
                evidence.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(evidence / f"share-transfer-{width}.png"))
                self.page.locator("#restoreShareBtn").click()
                self.page.locator("#shareDirModal .settings-dir-select").wait_for()
                self.assertEqual(self.page.locator("#shareDirModal [data-dir-title]").inner_text(), "选择本次转存目录")
                self.assertTrue(self.page.locator("#shareDirModal [data-dir-confirm]").is_hidden())
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                self.page.locator("#shareDirModal").evaluate("""async modal=>{
                    await new Promise(requestAnimationFrame);
                    await Promise.all(modal.getAnimations({subtree:true}).map(animation=>animation.finished.catch(()=>{})));
                }""")
                self.page.screenshot(path=str(evidence / f"share-transfer-picker-{width}.png"))
                self.page.locator("#shareDirModal [data-dir-close]").click()


if __name__ == "__main__":
    unittest.main()
