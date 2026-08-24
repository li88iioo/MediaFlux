from __future__ import annotations

import errno
import importlib.util
import json
import os
import sys
import uuid
import re
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

import app.clients.guangya as guangya_module
from app.clients.guangya import GuangYaClient, _to_file
from app.config import web_credentials
from app.main import create_app


SAFE_TOKEN_KEYS = {
    "has_access_token",
    "has_refresh_token",
    "expires_at",
    "valid",
    "access_token_masked",
    "refresh_token_masked",
}


def _load_isolated_guangya_module():
    alias = f"tests._isolated_guangya_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(alias, guangya_module.__file__)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建光鸭客户端隔离模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(alias, None)
        raise
    return alias, module


class _RotatingRawClient:
    def __init__(self, access_token=None, refresh_token=None, device_id=None):
        self.token = access_token or ""
        self.refresh_token_value = refresh_token or ""
        self.device_id = device_id or "device-generated"
        self.token_expires_at = None
        self.refresh_calls: list[str | None] = []

    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        self.token = "rotated-access-9999"
        self.refresh_token_value = "rotated-refresh-8888"
        self.token_expires_at = 1_900_000_000
        return {
            "access_token": self.token,
            "refresh_token": self.refresh_token_value,
            "expires_in": 3600,
        }

    def fs_files(self, **_kwargs):
        return {"data": {"list": []}}


class _PagedDirectoryRawClient(_RotatingRawClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fs_calls: list[dict] = []
        self.create_calls: list[dict] = []

    def fs_files(self, **kwargs):
        self.fs_calls.append(dict(kwargs))
        page = int(kwargs.get("page", 0))
        if page == 0:
            items = [
                {
                    "fileId": f"dir-{index}",
                    "fileName": f"Media {index}",
                    "resType": 2,
                }
                for index in range(200)
            ]
        elif page == 1:
            items = [
                {
                    "fileId": "canonical",
                    "fileName": "Existing Show (2021) {tmdb-113256}",
                    "resType": 2,
                }
            ]
        else:
            items = []
        return {"data": {"list": items}}

    def fs_create_dir(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        return {"data": {"fileId": "created-safe"}}


class _VersionedDeleteRawClient(_RotatingRawClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.detail = {
            "fileId": "empty-dir",
            "fileName": "Empty",
            "resType": 2,
            "etag": "version-1",
            "utime": 123,
        }
        self.children: list[dict] = []
        self.deleted: list[list[str]] = []

    def fs_detail(self, _file_id):
        return {"data": {"fileInfo": dict(self.detail)}}

    def fs_files(self, **_kwargs):
        return {"data": {"list": list(self.children)}}

    def fs_delete(self, file_ids):
        raise AssertionError("安全空目录清理不得回退到无条件 fs_delete")

    def fs_delete_empty(
        self, file_id, *, expected_etag=None, expected_updated_at=None
    ):
        self.deleted.append([str(file_id)])
        self.delete_preconditions = {
            "expected_etag": expected_etag,
            "expected_updated_at": expected_updated_at,
        }
        return {"code": 0}


class _NoAtomicEmptyDeleteRawClient(_RotatingRawClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fs_delete_calls: list[list[str]] = []
        self.detail_calls = 0
        self.list_calls = 0
        self.children_by_call: list[list[dict]] = []
        self.detail = {
            "fileId": "empty-dir", "fileName": "Empty", "resType": 2,
            "etag": "version-1", "utime": 123,
        }

    def fs_detail(self, _file_id):
        self.detail_calls += 1
        return {"data": {"fileInfo": dict(self.detail)}}

    def fs_files(self, **_kwargs):
        self.list_calls += 1
        index = self.list_calls - 1
        children = (
            self.children_by_call[index]
            if index < len(self.children_by_call)
            else []
        )
        return {"data": {"list": list(children)}}

    def fs_delete(self, file_ids):
        self.fs_delete_calls.append([str(item) for item in file_ids])
        return {"code": 0}


class _NonRotatingRawClient(_RotatingRawClient):
    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        return {"code": 401, "message": "refresh rejected"}


class _EchoExistingRawClient(_RotatingRawClient):
    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        return {
            "access_token": self.token,
            "refresh_token": self.refresh_token_value,
        }


class _InPlaceNoneRawClient(_RotatingRawClient):
    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        self.token = "in-place-access-5555"
        self.refresh_token_value = "in-place-refresh-6666"
        self.token_expires_at = 1_900_000_100
        return None


class _NestedRefreshRawClient(_RotatingRawClient):
    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        return {
            "data": {
                "access_token": "nested-access-7777",
                "refresh_token": "nested-refresh-8888",
                "expires_at": 1_900_000_200,
            }
        }


class _PartialRotationRawClient(_RotatingRawClient):
    """模拟供应商只轮换 refresh/expiry，却没有签发新 access token。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = SimpleNamespace(
            headers={"authorization": f"Bearer {self.token}"}
        )

    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        self.refresh_token_value = "partial-refresh-0000"
        self.token_expires_at = 1_900_000_400
        self._client.headers["authorization"] = "Bearer partial-stale-header"
        return {
            "refresh_token": self.refresh_token_value,
            "expires_in": 3600,
        }


class _ValidateRefreshAttemptRawClient(_RotatingRawClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fs_calls = 0

    def fs_files(self, **_kwargs):
        self.fs_calls += 1
        self.refresh_token()
        return {"data": {"list": []}}


class _BlockingRawClient(_RotatingRawClient):
    refresh_started = threading.Event()
    allow_refresh = threading.Event()

    def refresh_token(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        self.refresh_started.set()
        if not self.allow_refresh.wait(timeout=2):
            raise RuntimeError("test refresh timeout")
        self.token = "blocking-access-1212"
        self.refresh_token_value = "blocking-refresh-3434"
        self.token_expires_at = 1_900_000_300
        return {"access_token": self.token, "refresh_token": self.refresh_token_value}


class _LoginMockRawClient(_RotatingRawClient):
    def login_sms_verify(self, verification_id, verification_code):
        return {"verification_token": "mock-vtoken-123"}

    def login_sms_signin(self, verification_code, verification_token, username, captcha_token):
        self.token = "new-login-access-1111"
        self.refresh_token_value = "new-login-refresh-2222"
        self.token_expires_at = 1_900_000_500
        return {"success": True}


class GuangYaTokenClientTests(unittest.TestCase):
    def _token_file(self, directory: str, **overrides) -> Path:
        payload = {
            "access_token": "access-secret-7890",
            "refresh_token": "refresh-secret-4321",
            "device_id": "device-1",
            "expires_at": time() + 3600,
        }
        payload.update(overrides)
        token_file = Path(directory) / "guangya_token.json"
        token_file.write_text(json.dumps(payload), encoding="utf-8")
        return token_file

    def test_file_payload_keeps_sortable_time_type_and_size_fields(self):
        item = _to_file({
            "fileId": "video-1",
            "fileName": "Example.Show.S01E01.mkv",
            "resType": 1,
            "fileSize": 734003200,
            "ctime": 1_700_000_000,
            "utime": 1_700_000_100,
            "mineType": "video/x-matroska",
            "ext": ".MKV",
        }, "parent")

        self.assertFalse(item.is_dir)
        self.assertEqual(item.size, 734003200)
        self.assertEqual(item.created_at, 1_700_000_000)
        self.assertEqual(item.updated_at, 1_700_000_100)
        self.assertEqual(item.mime_type, "video/x-matroska")
        self.assertEqual(item.extension, "mkv")
        self.assertEqual(item.parent_id, "parent")

    def test_list_dir_reads_all_pages_instead_of_silently_stopping_at_fifty(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_PagedDirectoryRawClient,
            ):
                client = GuangYaClient(token_file=token_file)
                files = client.list_dir("anime-root")
                raw = client.raw

        self.assertEqual(len(files), 201)
        self.assertEqual(files[-1].file_id, "canonical")
        self.assertEqual(
            [(call["page"], call["page_size"]) for call in raw.fs_calls],
            [(0, 200), (1, 200)],
        )
        self.assertTrue(all(call["parent_id"] == "anime-root" for call in raw.fs_calls))

    def test_iter_dir_does_not_prefetch_following_pages_when_consumer_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_PagedDirectoryRawClient,
            ):
                client = GuangYaClient(token_file=token_file)
                iterator = client.iter_dir("anime-root")
                first = next(iterator)
                iterator.close()
                raw = client.raw

        self.assertEqual(first.file_id, "dir-0")
        self.assertEqual(
            [(call["page"], call["page_size"]) for call in raw.fs_calls],
            [(0, 200)],
        )

    def test_create_dir_forbids_provider_side_duplicate_name_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_PagedDirectoryRawClient,
            ):
                client = GuangYaClient(token_file=token_file)
                file_id = client.create_dir("Existing Show", "anime-root")
                raw = client.raw

        self.assertEqual(file_id, "created-safe")
        self.assertEqual(raw.create_calls, [{
            "dir_name": "Existing Show",
            "parent_id": "anime-root",
            "fail_if_name_exist": True,
        }])

    def test_token_status_returns_masked_metadata_without_token_values(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                status = GuangYaClient(token_file=token_file).token_status()

        self.assertEqual(set(status), SAFE_TOKEN_KEYS)
        self.assertTrue(status["has_access_token"])
        self.assertTrue(status["has_refresh_token"])
        self.assertTrue(status["valid"])
        self.assertEqual(status["access_token_masked"], "••••7890")
        self.assertEqual(status["refresh_token_masked"], "••••4321")
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("access-secret-7890", serialized)
        self.assertNotIn("refresh-secret-4321", serialized)

    def test_delete_empty_directory_requires_stable_version_and_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_VersionedDeleteRawClient):
                client = GuangYaClient(token_file=token_file)
                raw = client._raw

                self.assertTrue(client.delete_empty_directory(
                    "empty-dir", expected_etag="version-1", expected_updated_at=123,
                ))
                self.assertEqual(raw.deleted, [["empty-dir"]])
                self.assertEqual(raw.delete_preconditions, {
                    "expected_etag": "version-1",
                    "expected_updated_at": 123,
                })

                raw.deleted.clear()
                raw.detail["etag"] = "version-2"
                with self.assertRaisesRegex(RuntimeError, "版本已变化"):
                    client.delete_empty_directory(
                        "empty-dir", expected_etag="version-1", expected_updated_at=123,
                    )
                self.assertEqual(raw.deleted, [])

                raw.detail["etag"] = "version-1"
                raw.children = [{"fileId": "child", "fileName": "child.mkv", "resType": 1}]
                with self.assertRaisesRegex(RuntimeError, "已包含内容"):
                    client.delete_empty_directory(
                        "empty-dir", expected_etag="version-1", expected_updated_at=123,
                    )
                self.assertEqual(raw.deleted, [])

                raw.children = []
                with self.assertRaisesRegex(RuntimeError, "缺少可验证"):
                    client.delete_empty_directory("empty-dir")
                self.assertEqual(raw.deleted, [])

    def test_delete_empty_directory_uses_guarded_recheck_without_atomic_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_NoAtomicEmptyDeleteRawClient,
            ):
                client = GuangYaClient(token_file=token_file)
                raw = client._raw

                self.assertFalse(client.supports_atomic_empty_directory_delete)
                self.assertTrue(client.supports_guarded_empty_directory_delete)
                self.assertTrue(client.delete_empty_directory(
                    "empty-dir",
                    expected_etag="version-1",
                    expected_updated_at=123,
                ))

            self.assertEqual(raw.detail_calls, 2)
            self.assertEqual(raw.list_calls, 2)
            self.assertEqual(raw.fs_delete_calls, [["empty-dir"]])

    def test_guarded_empty_directory_recheck_rejects_new_child(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_NoAtomicEmptyDeleteRawClient,
            ):
                client = GuangYaClient(token_file=token_file)
                raw = client._raw
                raw.children_by_call = [[], [{
                    "fileId": "late-child",
                    "fileName": "late.mkv",
                    "resType": 1,
                }]]

                with self.assertRaisesRegex(RuntimeError, "已包含内容"):
                    client.delete_empty_directory(
                        "empty-dir",
                        expected_etag="version-1",
                        expected_updated_at=123,
                    )

            self.assertEqual(raw.detail_calls, 2)
            self.assertEqual(raw.list_calls, 2)
            self.assertEqual(raw.fs_delete_calls, [])

    def test_delete_empty_directory_rejects_provider_failure_response(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_VersionedDeleteRawClient):
                client = GuangYaClient(token_file=token_file)
                raw = client._raw
                raw.fs_delete_empty = Mock(return_value={
                    "code": 409,
                    "message": "version conflict",
                })

                with self.assertRaisesRegex(RuntimeError, "version conflict"):
                    client.delete_empty_directory(
                        "empty-dir",
                        expected_etag="version-1",
                        expected_updated_at=123,
                    )

                raw.fs_delete_empty.assert_called_once()

    def test_client_startup_cleans_token_and_generation_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            generation_file = token_file.with_name(f".{token_file.name}.generation")
            token_temp = token_file.parent / f".{token_file.name}.stale.tmp"
            generation_temp = generation_file.parent / f".{generation_file.name}.stale.tmp"
            token_temp.write_text("stale-token", encoding="utf-8")
            generation_temp.write_text("stale-generation", encoding="utf-8")

            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                GuangYaClient(token_file=token_file)

            self.assertFalse(token_temp.exists())
            self.assertFalse(generation_temp.exists())

    def test_client_load_waits_for_credential_writer_before_cleaning_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            generation_file = token_file.with_name(f".{token_file.name}.generation")
            active_temp = token_file.parent / f".{token_file.name}.active.tmp"
            active_temp.write_text("writer-active", encoding="utf-8")
            lock = guangya_module._shared_token_process_lock(token_file)
            started = threading.Event()
            finished = threading.Event()
            result = {}

            def construct_client():
                started.set()
                with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                    result["client"] = GuangYaClient(token_file=token_file)
                finished.set()

            lock.acquire()
            thread = threading.Thread(target=construct_client)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                self.assertFalse(finished.wait(timeout=0.05))
                self.assertTrue(active_temp.exists())
                payload = json.loads(token_file.read_text(encoding="utf-8"))
                payload["access_token"] = "writer-access-2468"
                guangya_module._atomic_write_text(
                    token_file, json.dumps(payload, ensure_ascii=False)
                )
                guangya_module._advance_token_generation(token_file)
            finally:
                lock.release()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertFalse(active_temp.exists())
            self.assertEqual(result["client"]._raw.token, "writer-access-2468")
            self.assertEqual(
                result["client"].credential_generation,
                int(generation_file.read_text(encoding="utf-8")),
            )

    def test_refresh_now_forces_refresh_and_atomically_persists_rotated_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            original_replace = Path.replace
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                with patch.object(
                    Path,
                    "replace",
                    autospec=True,
                    side_effect=lambda source, target: original_replace(source, target),
                ) as replace:
                    status = client.refresh_now()

            persisted = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(client._raw.refresh_calls, ["refresh-secret-4321"])
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(persisted["access_token"], "rotated-access-9999")
            self.assertEqual(persisted["refresh_token"], "rotated-refresh-8888")
            self.assertEqual(status["access_token_masked"], "••••9999")
            self.assertTrue(status["valid"])
            self.assertNotIn("rotated-access-9999", json.dumps(status, ensure_ascii=False))

    def test_successful_refresh_preserves_login_generation_and_stales_old_client(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                refresher = GuangYaClient(token_file=token_file)
                old_client = GuangYaClient(token_file=token_file)
                before = refresher.credential_generation

                refresher.refresh_now()

                self.assertEqual(refresher.credential_generation, before)
                self.assertFalse(old_client.logged_in)

    def test_persisted_generation_invalidates_client_from_isolated_module_state(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            stale_alias, stale_module = _load_isolated_guangya_module()
            writer_alias, writer_module = _load_isolated_guangya_module()
            try:
                stale_module._load_raw = lambda: _RotatingRawClient
                writer_module._load_raw = lambda: _RotatingRawClient
                stale = stale_module.GuangYaClient(token_file=token_file)
                writer = writer_module.GuangYaClient(token_file=token_file)
                stale_raw = stale._raw
                before = stale.credential_generation

                writer.refresh_now()
                persisted = token_file.read_text(encoding="utf-8")

                self.assertEqual(stale.credential_generation, before)
                self.assertFalse(stale.logged_in)
                with self.assertRaisesRegex(RuntimeError, "凭证已变化|已撤销|重新加载"):
                    stale_raw.refresh_token()
                self.assertEqual(stale_raw.refresh_calls, [])
                self.assertEqual(token_file.read_text(encoding="utf-8"), persisted)
            finally:
                sys.modules.pop(stale_alias, None)
                sys.modules.pop(writer_alias, None)

    def test_refresh_now_rejects_response_without_new_access_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            original_payload = token_file.read_text(encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_NonRotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                with self.assertRaisesRegex(RuntimeError, "未返回新的 access token"):
                    client.refresh_now()

            self.assertEqual(client._raw.refresh_calls, ["refresh-secret-4321"])
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_refresh_now_rejects_echoed_existing_credentials_without_change(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            original_payload = token_file.read_text(encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_EchoExistingRawClient):
                client = GuangYaClient(token_file=token_file)
                with self.assertRaisesRegex(RuntimeError, "未返回新的 access token"):
                    client.refresh_now()

            self.assertEqual(client._raw.token, "access-secret-7890")
            self.assertEqual(client._raw.refresh_token_value, "refresh-secret-4321")
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_refresh_now_rejects_partial_rotation_and_restores_memory_header_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            original_expiry = time() + 7200
            token_file = self._token_file(directory, expires_at=original_expiry)
            original_payload = token_file.read_text(encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_PartialRotationRawClient):
                client = GuangYaClient(token_file=token_file)
                with self.assertRaisesRegex(RuntimeError, "未返回新的 access token"):
                    client.refresh_now()

            self.assertEqual(client._raw.token, "access-secret-7890")
            self.assertEqual(client._raw.refresh_token_value, "refresh-secret-4321")
            self.assertAlmostEqual(client._raw.token_expires_at, original_expiry, delta=1)
            self.assertEqual(
                client._raw._client.headers["authorization"],
                "Bearer access-secret-7890",
            )
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_refresh_now_accepts_sdk_in_place_update_with_none_response(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_InPlaceNoneRawClient):
                client = GuangYaClient(token_file=token_file)
                status = client.refresh_now()

            persisted = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["access_token"], "in-place-access-5555")
            self.assertEqual(persisted["refresh_token"], "in-place-refresh-6666")
            self.assertEqual(status["access_token_masked"], "••••5555")

    def test_refresh_now_applies_nested_sdk_token_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_NestedRefreshRawClient):
                client = GuangYaClient(token_file=token_file)
                status = client.refresh_now()

            persisted = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(client._raw.token, "nested-access-7777")
            self.assertEqual(client._raw.refresh_token_value, "nested-refresh-8888")
            self.assertEqual(persisted["expires_at"], 1_900_000_200)
            self.assertEqual(status["refresh_token_masked"], "••••8888")

    def test_stale_raw_refresh_hook_cannot_return_or_persist_revoked_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                stale_client = GuangYaClient(token_file=token_file)
                rotating_client = GuangYaClient(token_file=token_file)
                stale_raw = stale_client._raw

                rotating_client.refresh_now()
                persisted_after_rotation = token_file.read_text(encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, "凭证已变化"):
                    stale_raw.refresh_token()

            self.assertEqual(stale_raw.refresh_calls, [])
            self.assertEqual(
                token_file.read_text(encoding="utf-8"),
                persisted_after_rotation,
            )

    def test_waiting_refresh_cannot_resurrect_credentials_cleared_by_other_client(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            errors: list[Exception] = []
            completed = threading.Event()
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                refresher = GuangYaClient(token_file=token_file)
                clearer = GuangYaClient(token_file=token_file)

                def run_refresh():
                    try:
                        refresher.refresh_now()
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)
                    finally:
                        completed.set()

                with refresher._token_lock:
                    thread = threading.Thread(target=run_refresh)
                    thread.start()
                    clearer.clear_tokens()
                    self.assertFalse(completed.wait(timeout=0.05))
                thread.join(timeout=2)

            self.assertTrue(completed.is_set())
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), "已撤销|重新登录")
            self.assertFalse(token_file.exists())
            self.assertIsNone(refresher._raw)

    def test_validate_rejects_expired_token_without_refresh_or_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() - 60)
            original_payload = token_file.read_text(encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                valid = client.validate()

            self.assertFalse(valid)
            self.assertEqual(client._raw.refresh_calls, [])
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_validate_blocks_sdk_internal_refresh_and_does_not_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            original_payload = token_file.read_text(encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_ValidateRefreshAttemptRawClient):
                client = GuangYaClient(token_file=token_file)
                valid = client.validate()

            self.assertFalse(valid)
            self.assertEqual(client._raw.fs_calls, 1)
            self.assertEqual(client._raw.refresh_calls, [])
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_token_status_rejects_non_numeric_type_and_out_of_range_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                for unsafe in ("1900000000", 1_500_000_000, 4_200_000_000, True):
                    with self.subTest(unsafe=unsafe):
                        client._raw.token_expires_at = unsafe
                        self.assertIsNone(client.token_status()["expires_at"])

    def test_second_instance_reloads_rotated_token_without_refreshing_again(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() + 7200)
            original_replace = Path.replace
            sources = []

            def capture_replace(source, target):
                sources.append(source.name)
                return original_replace(source, target)

            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                first = GuangYaClient(token_file=token_file)
                second = GuangYaClient(token_file=token_file)
                with patch.object(Path, "replace", autospec=True, side_effect=capture_replace):
                    first.refresh_now()
                    second.refresh_now()

            self.assertEqual(len(sources), 1)
            self.assertTrue(all(name.endswith(".tmp") for name in sources))
            self.assertTrue(second.token_status(valid=True)["has_access_token"])
            self.assertEqual(second._raw.refresh_calls, [])

    def test_refresh_and_clear_share_real_path_lock_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_dir = root / "real"
            real_dir.mkdir()
            alias_dir = root / "alias"
            try:
                alias_dir.symlink_to(real_dir, target_is_directory=True)
                if alias_dir.resolve() != real_dir.resolve():
                    self.skipTest("filesystem does not resolve directory symlinks to target")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            token_file = self._token_file(str(real_dir), expires_at=time() + 7200)
            _BlockingRawClient.refresh_started = threading.Event()
            _BlockingRawClient.allow_refresh = threading.Event()
            errors = []
            clear_done = threading.Event()

            with patch("app.clients.guangya._load_raw", return_value=_BlockingRawClient):
                refresher = GuangYaClient(token_file=token_file)
                clearer = GuangYaClient(token_file=alias_dir / token_file.name)

                def run_refresh():
                    try:
                        refresher.refresh_now()
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                def run_clear():
                    try:
                        clearer.clear_tokens()
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)
                    finally:
                        clear_done.set()

                refresh_thread = threading.Thread(target=run_refresh)
                clear_thread = threading.Thread(target=run_clear)
                refresh_thread.start()
                self.assertTrue(_BlockingRawClient.refresh_started.wait(timeout=1))
                clear_thread.start()
                try:
                    self.assertFalse(clear_done.wait(timeout=0.05))
                finally:
                    _BlockingRawClient.allow_refresh.set()
                    refresh_thread.join(timeout=2)
                    clear_thread.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertTrue(clear_done.is_set())
            self.assertFalse(token_file.exists())

    def test_clear_tokens_removes_persisted_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                status = client.clear_tokens()

            self.assertFalse(token_file.exists())
            self.assertIsNone(client._raw)
            self.assertEqual(set(status), SAFE_TOKEN_KEYS)
            self.assertFalse(status["has_access_token"])
            self.assertFalse(status["has_refresh_token"])
            self.assertFalse(status["valid"])

    def test_clear_tokens_revokes_other_loaded_instances_for_same_path(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                first = GuangYaClient(token_file=token_file)
                second = GuangYaClient(token_file=token_file)
                first.clear_tokens()

                self.assertFalse(second.logged_in)
                with self.assertRaisesRegex(RuntimeError, "已撤销|未登录"):
                    second.list_dir("0")

    def test_load_and_clear_remove_orphan_atomic_files_and_enforce_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            token_file.chmod(0o644)
            orphan = token_file.parent / f".{token_file.name}.orphan.tmp"
            orphan.write_text("secret-token", encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                self.assertFalse(orphan.exists())
                if os.name != "nt":
                    self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
                second_orphan = token_file.parent / f".{token_file.name}.second.tmp"
                second_orphan.write_text("secret-token", encoding="utf-8")
                client.clear_tokens()
            self.assertFalse(second_orphan.exists())

    @unittest.skipIf(os.name == "nt", "POSIX fchmod 挂载兼容合同")
    def test_load_private_token_survives_mount_fchmod_eperm(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory)
            token_file.chmod(0o600)
            with patch(
                "app.private_files.os.fchmod",
                side_effect=PermissionError(errno.EPERM, "operation not permitted"),
            ), patch(
                "app.clients.guangya._load_raw",
                return_value=_RotatingRawClient,
            ):
                client = GuangYaClient(token_file=token_file)

            self.assertTrue(client.logged_in)
            self.assertEqual(client.raw.token, "access-secret-7890")

    def test_login_replaces_existing_token_without_revocation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = self._token_file(directory, expires_at=time() - 3600)
            with patch("app.clients.guangya._load_raw", return_value=_LoginMockRawClient):
                client = GuangYaClient(token_file=token_file)
                self.assertFalse(client.validate())
                ok = client.login("13800138000", "123456", verification_id="vid-1", captcha_token="cap-1")
                self.assertTrue(ok)
                self.assertTrue(client.logged_in)
                self.assertEqual(client._raw.token, "new-login-access-1111")
                status = client.token_status()
                self.assertTrue(status["valid"])
                self.assertEqual(status["access_token_masked"], "••••1111")


class _ApiTokenClient:
    instances: list["_ApiTokenClient"] = []

    def __init__(self):
        self.calls: list[str] = []
        self.present = True
        self.__class__.instances.append(self)

    @property
    def logged_in(self):
        return self.present

    def _status(self, valid=True):
        return {
            "has_access_token": self.present,
            "has_refresh_token": self.present,
            "expires_at": 1_900_000_000 if self.present else None,
            "valid": bool(valid and self.present),
            "access_token_masked": "••••7890" if self.present else "",
            "refresh_token_masked": "••••4321" if self.present else "",
            "access_token": "must-never-leak",
            "refresh_token": "must-never-leak-either",
            "sdk_payload": {"access_token": "nested-leak"},
        }

    def token_status(self, *, valid=None):
        self.calls.append("status")
        return self._status(valid=True if valid is None else valid)

    def refresh_now(self):
        self.calls.append("refresh")
        return self._status(valid=True)

    def validate(self):
        self.calls.append("validate")
        return False

    def clear_tokens(self):
        self.calls.append("clear")
        self.present = False
        return self._status(valid=False)


class _UnsafeMaskedApiTokenClient(_ApiTokenClient):
    def _status(self, valid=True):
        status = super()._status(valid=valid)
        status["access_token_masked"] = "must-never-leak-from-mask-field"
        status["refresh_token_masked"] = "must-never-leak-refresh-mask"
        status["expires_at"] = "1900000000"
        return status


class GuangYaTokenApiTests(InitializedWebTestCase):
    def setUp(self):
        _ApiTokenClient.instances.clear()
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        login_page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(login_page.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/guangya")
        self.headers = {"X-CSRF-Token": self._csrf(page.text)}

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def assert_safe_response(self, response, *, valid: bool):
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), SAFE_TOKEN_KEYS)
        self.assertEqual(payload["valid"], valid)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("must-never-leak", serialized)
        self.assertNotIn("nested-leak", serialized)
        return payload

    def test_capabilities_api_exposes_runtime_readiness(self):
        scheduler = type("Scheduler", (), {"validate_config": lambda self, auto_only=False: "STRM 未配置"})()
        with patch("app.routes.guangya_api.get_scheduler", return_value=scheduler):
            response = self.client.get("/api/guangya/capabilities")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {"sdk_available", "proxy_enabled", "strm_configured", "strm_error"},
        )
        self.assertTrue(payload["sdk_available"])
        self.assertTrue(payload["proxy_enabled"])
        self.assertFalse(payload["strm_configured"])
        self.assertEqual(payload["strm_error"], "STRM 未配置")

    def test_validate_api_re_masks_untrusted_mask_fields(self):
        with patch("app.routes.guangya_api.GuangYaClient", _UnsafeMaskedApiTokenClient):
            response = self.client.post(
                "/api/guangya/token/validate", headers=self.headers
            )

        payload = self.assert_safe_response(response, valid=False)
        self.assertEqual(payload["access_token_masked"], "••••ield")
        self.assertEqual(payload["refresh_token_masked"], "••••mask")
        self.assertIsNone(payload["expires_at"])

    def test_refresh_api_uses_explicit_refresh_operation_and_redacts_response(self):
        with patch("app.routes.guangya_api.GuangYaClient", _ApiTokenClient):
            response = self.client.post("/api/guangya/token/refresh", headers=self.headers)

        self.assert_safe_response(response, valid=True)
        self.assertEqual(_ApiTokenClient.instances[-1].calls, ["refresh"])

    def test_refresh_api_rejects_partial_rotation_without_persisting_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "guangya_token.json"
            original_payload = json.dumps({
                "access_token": "access-secret-7890",
                "refresh_token": "refresh-secret-4321",
                "device_id": "device-1",
                "expires_at": time() + 7200,
            })
            token_file.write_text(original_payload, encoding="utf-8")
            with patch("app.clients.guangya._load_raw", return_value=_PartialRotationRawClient):
                client = GuangYaClient(token_file=token_file)
                with patch("app.routes.guangya_api.GuangYaClient", return_value=client):
                    response = self.client.post(
                        "/api/guangya/token/refresh", headers=self.headers
                    )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(token_file.read_text(encoding="utf-8"), original_payload)

    def test_validate_api_uses_read_only_validation_result(self):
        with patch("app.routes.guangya_api.GuangYaClient", _ApiTokenClient):
            response = self.client.post("/api/guangya/token/validate", headers=self.headers)

        self.assert_safe_response(response, valid=False)
        self.assertEqual(_ApiTokenClient.instances[-1].calls, ["validate", "status"])

    def test_clear_api_removes_tokens_and_returns_empty_safe_status(self):
        with patch("app.routes.guangya_api.GuangYaClient", _ApiTokenClient), patch(
            "app.modules.media_proxy.clear_signed_url_cache"
        ) as clear_cache:
            response = self.client.post("/api/guangya/token/clear", headers=self.headers)

        self.assert_safe_response(response, valid=False)
        self.assertFalse(response.json()["has_access_token"])
        self.assertEqual(_ApiTokenClient.instances[-1].calls, ["clear"])
        clear_cache.assert_called_once_with()

class GuangYaTokenUiTests(unittest.TestCase):
    def test_token_controls_have_fixed_loading_dimensions_and_stable_status_region(self):
        template = Path("app/templates/guangya.html").read_text(encoding="utf-8")

        for button_id in ("gyRefreshTokenBtn", "gyValidateTokenBtn", "gyClearTokenBtn"):
            self.assertIn(f'id="{button_id}"', template)
        self.assertIn(".gy-token-action", template)
        self.assertIn("gyApi('/token/validate',{method:'POST'})", template)
        self.assertNotIn("gyApi('/status')", template)
        self.assertIn("min-width:", template)
        self.assertIn("height:", template)
        self.assertNotIn('id="gyTokenStatus"', template)
        self.assertNotIn('id="gyAccessToken"', template)
        self.assertNotIn('id="gyRefreshToken"', template)
        self.assertNotIn('id="gyExpiresAt"', template)
        self.assertIn('id="gyCapabilityBadge"', template)
        self.assertLess(
            template.index('class="gy-token-header"'),
            template.index('id="gyCapabilityBadge"'),
        )
        self.assertIn("min-height:", template)
        self.assertIn("/token/refresh", template)
        self.assertIn("/token/validate", template)
        self.assertIn("/token/clear", template)
        self.assertIn("setGyTokenButtonLoading", template)
        self.assertIn("function setGyTokenControlsBusy", template)
        self.assertIn("document.querySelectorAll('.gy-token-action')", template)
        self.assertIn("setGyTokenControlsBusy(true", template)
        self.assertIn("setGyTokenControlsBusy(false", template)


if __name__ == "__main__":
    unittest.main()


class GuangYaReadRetryPolicyTests(unittest.TestCase):
    class _TransientError(RuntimeError):
        pass

    def test_read_operation_retries_once_after_transient_network_error(self):
        client = object.__new__(GuangYaClient)
        calls = []

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise self._TransientError("temporary")
            return {"ok": True}

        with patch.object(client, "_read_retryable", return_value=True), patch(
            "app.clients.guangya.sleep"
        ) as sleep_mock:
            result = client._call_read("unit_read", operation)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.15)

    def test_httpx_transport_error_is_retryable_through_wrapped_cause(self):
        request = httpx.Request("POST", "https://example.invalid/read")
        transport = httpx.ReadError("connection reset", request=request)
        wrapped = RuntimeError("sdk wrapper")
        wrapped.__cause__ = transport

        self.assertTrue(GuangYaClient._read_retryable(wrapped))

    def test_read_401_forces_refresh_before_single_retry(self):
        client = object.__new__(GuangYaClient)
        calls = []
        error = RuntimeError("unauthorized")
        error.response = SimpleNamespace(status_code=401)

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise error
            return "ok"

        client._token_lock = threading.RLock()
        client._raw = SimpleNamespace(token="old-access")
        with patch.object(client, "refresh_now", return_value={}) as refresh:
            self.assertEqual(client._call_read("unit_read", operation), "ok")

        refresh.assert_called_once_with()
        self.assertEqual(len(calls), 2)

    def test_concurrent_401_responses_share_one_refresh(self):
        client = object.__new__(GuangYaClient)
        client._token_lock = threading.RLock()
        client._raw = SimpleNamespace(token="old-access")
        refresh_calls = 0
        request_barrier = threading.Barrier(2)
        first_attempts: dict[int, bool] = {}
        lock = threading.Lock()

        def refresh_now():
            nonlocal refresh_calls
            refresh_calls += 1
            client._raw.token = "new-access"
            return {}

        def operation():
            identity = threading.get_ident()
            with lock:
                first = not first_attempts.get(identity, False)
                first_attempts[identity] = True
            if first:
                request_barrier.wait(timeout=2)
                error = RuntimeError("unauthorized")
                error.response = SimpleNamespace(status_code=401)
                raise error
            return "ok"

        results = []
        errors = []
        with patch.object(client, "refresh_now", side_effect=refresh_now):
            def run():
                try:
                    results.append(client._call_read("unit_read", operation))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(refresh_calls, 1)

    def test_download_url_forwards_bounded_transport_timeout(self):
        client = object.__new__(GuangYaClient)
        client._raw = Mock()
        client._raw.request.return_value = Mock(
            json=Mock(return_value={"data": {"signedURL": "https://example/file"}})
        )

        with patch.object(client, "_invalidate_if_stale", return_value=False):
            result = client.get_download_url("file-id", timeout=0.25)

        self.assertEqual(result, "https://example/file")
        _args, kwargs = client._raw.request.call_args
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["json"], {"fileId": "file-id"})
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 0.25)

    def test_download_url_can_propagate_transport_timeout_for_probe_classification(self):
        client = object.__new__(GuangYaClient)
        request = httpx.Request("POST", "https://example.invalid/download")
        timeout = httpx.ReadTimeout("timed out", request=request)

        with patch.object(client, "_call_read", side_effect=timeout):
            self.assertIsNone(client.get_download_url("file-id", timeout=0.25))
        with patch.object(client, "_call_read", side_effect=timeout):
            with self.assertRaises(TimeoutError):
                client.get_download_url(
                    "file-id", timeout=0.25, raise_timeout=True,
                )

    def test_bounded_read_does_not_start_unbounded_refresh_on_401(self):
        client = object.__new__(GuangYaClient)
        error = RuntimeError("unauthorized")
        error.response = SimpleNamespace(status_code=401)
        operation = Mock(side_effect=error)

        with patch.object(client, "refresh_now") as refresh:
            with self.assertRaises(RuntimeError):
                client._call_read("unit_read", operation, deadline=monotonic() + 1)

        refresh.assert_not_called()
        operation.assert_called_once_with()

    def test_non_idempotent_sdk_request_cannot_see_refresh_token_for_auto_replay(self):
        client = object.__new__(GuangYaClient)
        client._token_lock = threading.RLock()

        class Raw:
            token = "access"
            token_expires_at = None
            refresh_token_value = "refresh-secret"

            def refresh_token(self, refresh_token=None):
                return {"access_token": "new-access"}

            def request(self, url, method="GET", **kwargs):
                return self.refresh_token_value

        raw = Raw()
        client._raw = raw
        client._install_request_retry_policy(raw)

        observed = raw.request("https://example.invalid/write", "POST")

        self.assertIsNone(observed)
        self.assertEqual(raw.refresh_token_value, "refresh-secret")
