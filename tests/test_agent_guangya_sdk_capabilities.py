"""光鸭 SDK P1-P3 能力的领域动作、确认边界与持久任务测试。"""

from __future__ import annotations
from typing import Any
from typing import cast

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import httpx

from app.agent.kernel.bootstrap import build_tool_catalog
from app import sdk
from app.clients.guangya import GuangYaShareError, _extract_created_share
from app.modules import guangya_account
from app.modules import guangya_recycle as recycle
from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected


class _RawSdk:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def fs_copy(self, file_ids, parent_id=None):
        self.calls.append(("copy", list(file_ids), parent_id))
        return {"msg": "复制成功", "data": {"taskId": "copy-task"}}

    def fs_recycle_files(self, page=0, page_size=50, **_kwargs):
        self.calls.append(("recycle_list", page, page_size))
        if page:
            return {"data": {"list": []}}
        return {
            "data": {
                "list": [
                    {
                        "fileId": "trash-1",
                        "fileName": "旧文件.mkv",
                        "resType": 1,
                        "fileSize": 123,
                        "parentId": "source",
                        "gcid": "gcid-1",
                    }
                ]
            }
        }

    def fs_recycle(self, file_ids):
        self.calls.append(("restore", list(file_ids)))
        return {"code": 0, "data": {"taskId": "restore-task"}}

    def fs_clear_recycle_bin(self):
        self.calls.append(("clear",))
        return {"msg": "清理成功", "data": {"taskId": "clear-task"}}

    def get_task_status(self, task_id):
        self.calls.append(("task", task_id))
        return {"data": {"status": "completed", "progress": 100}}

    def _account_headers(self):
        return {"x-client-id": "test-client"}

    def request(self, url, method="GET", **kwargs):
        self.calls.append(("request", url, method, kwargs))
        if url == "https://account.guangyapan.com/v1/user/me":
            data = {"nickname": "测试用户"}
        elif url == "https://api.guangyapan.com/assets/v1/get_assets":
            data = {"totalSpaceSize": 1000, "usedSpaceSize": 250}
        else:
            raise AssertionError("unexpected account endpoint")
        return httpx.Response(200, json={"code": 0, "data": data})

    def share_user_list(self, page=0, page_size=50, **_kwargs):
        self.calls.append(("share_list", page, page_size))
        return {
            "data": {
                "list": [
                    {
                        "id": "10001",
                        "shareId": "share-1",
                        "title": "动画",
                        "status": "active",
                        "fileCount": 2,
                    }
                ]
            }
        }

    def share_create(self, file_ids, **kwargs):
        self.calls.append(("share_create", list(file_ids), dict(kwargs)))
        return {
            "code": 200,
            "data": {"shareId": "created-share", "accessCode": "AB12"},
        }

    def share_delete(self, ids):
        self.calls.append(("share_delete", list(ids)))
        return {"msg": "删除成功"}

    def upload_token(self, name, file_size, parent_id=None, md5=None):
        self.calls.append(("upload_token", name, file_size, parent_id, md5))
        return {"msg": "success", "data": {"taskId": "upload-task"}}

    def cdn_upload(self, file_path, token_data, **kwargs):
        self.calls.append(("cdn_upload", str(file_path), token_data, kwargs))
        return '"fixture-etag"'

    def upload_info(self, task_id):
        self.calls.append(("upload_info", task_id))
        return {"msg": "success", "data": {"taskId": task_id}}


class _Client(GuangYaClient):
    def __init__(self, raw: _RawSdk) -> None:
        self._raw = raw

    @property
    def raw(self):
        return self._raw


class GuangYaSdkClientTests(unittest.TestCase):
    def test_p1_p3_sdk_wrappers_are_bounded_and_return_task_ids(self) -> None:
        raw = _RawSdk()
        client = _Client(raw)

        copy_task_id = client.copy(["file-1", "file-1"], "target")
        self.assertEqual(copy_task_id, "copy-task")
        self.assertEqual(client.task_status(copy_task_id)["data"]["status"], "completed")
        self.assertIn(("task", copy_task_id), raw.calls)
        recycle = client.list_recycle(max_items=10)
        self.assertEqual([(item.file_id, item.name) for item in recycle], [("trash-1", "旧文件.mkv")])
        self.assertEqual(client.restore_from_recycle(["trash-1"]), "restore-task")
        self.assertEqual(client.clear_recycle_bin(), "clear-task")
        self.assertEqual(client.account_info()["data"]["nickname"], "测试用户")
        self.assertEqual(client.account_storage_info()["data"]["totalSpaceSize"], 1000)

    def test_p2_share_and_upload_wrappers_do_not_expose_raw_client(self) -> None:
        raw = _RawSdk()
        client = _Client(raw)
        self.assertEqual(len(client.list_user_shares(max_items=10)), 1)
        created = client.create_user_share(
            ["file-1"],
            title="动画",
            code="AB12",
            auto_fill_code=True,
        )
        self.assertEqual(created["data"]["shareId"], "created-share")
        self.assertTrue(client.delete_user_shares(["10001"]))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.mkv"
            path.write_bytes(b"sample")
            response = client.upload_local_file(path, parent_id="target")
        self.assertEqual(response["data"]["taskId"], "upload-task")
        share_call = next(item for item in raw.calls if item[0] == "share_create")
        self.assertFalse(share_call[2]["auto_fill_code"])

    def test_create_share_access_code_is_not_a_provider_error_code(self) -> None:
        response = {"code": 0, "msg": "success", "data": {"shareId": "public-share", "code": "AB12"}}
        raw = _RawSdk()
        with mock.patch.object(raw, "share_create", return_value=response):
            created = _Client(raw).create_user_share(["file-1"], title="Probe")
        self.assertEqual(created, response)
        self.assertEqual(_extract_created_share(created)["access_code"], "AB12")
        for failure in (
            {**response, "code": 10},
            {**response, "data": {**response["data"], "success": False}},
        ):
            with self.subTest(failure=failure), mock.patch.object(raw, "share_create", return_value=failure):
                with self.assertRaises(GuangYaWriteRejected):
                    _Client(raw).create_user_share(["file-1"], title="Probe")

    def test_agent_catalog_keeps_local_upload_disabled(self) -> None:
        cloud = {name for name in sdk.METHODS if name.startswith("cloud.")}
        registered = {spec.name for spec in build_tool_catalog().visible()}
        self.assertIn("cloud.change", cloud & registered)
        self.assertFalse(any("upload" in name for name in cloud | registered))
        self.assertFalse(any(name.startswith("guangya.") for name in registered))
        self.assertFalse(hasattr(sdk.cloud, "upload_local_file"))
        with self.assertRaises(KeyError):
            sdk.prepare_sdk_method("cloud.upload_local_file", {}, owner="sdk-owner")


class GuangYaLocalUploadTests(unittest.TestCase):
    """独立上传链回归；只使用原子方法替身，禁止 high-level 回退和实网。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "sample.mkv"
        self.path.write_bytes(b"x" * (1024 * 1024))
        sleeper = mock.patch("app.clients.guangya.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        network = mock.patch("socket.socket.connect", side_effect=AssertionError("external network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def make_raw(self):
        raw = mock.Mock(spec=["upload_token", "check_can_flash_upload", "cdn_upload", "upload_info"])
        raw.upload_token.return_value = {"msg": "success", "data": {"taskId": "upload-task"}}
        raw.check_can_flash_upload.return_value = {"msg": "success", "canFlashUpload": False}
        raw.cdn_upload.return_value = '"etag"'
        raw.upload_info.return_value = {"msg": "success", "data": {"fileId": "uploaded"}}
        return raw

    def test_flash_top_level_and_wrapped_true_or_false(self):
        for wrapped in (False, True):
            for can_flash in (False, True):
                with self.subTest(wrapped=wrapped, can_flash=can_flash):
                    raw = self.make_raw()
                    payload = {"canFlashUpload": can_flash}
                    raw.check_can_flash_upload.return_value = {"msg": "success", **({"data": payload} if wrapped else payload)}
                    result = _Client(cast(_RawSdk, raw)).upload_local_file(self.path)
                    self.assertEqual(result, raw.upload_info.return_value)
                    raw.upload_token.assert_called_once_with(self.path.name, 1024 * 1024, None)
                    raw.check_can_flash_upload.assert_called_once_with("upload-task", self.path)
                    self.assertEqual(raw.cdn_upload.call_count, 0 if can_flash else 1)
                    raw.upload_info.assert_called_once_with("upload-task")
        self.sleep.assert_not_called()

    def test_small_file_keeps_base64_md5_and_uploads_cdn_without_flash(self):
        from base64 import b64encode
        from hashlib import md5

        for content in (b"", b"sample", b"x" * (1024 * 1024 - 1)):
            with self.subTest(size=len(content)):
                self.path.write_bytes(content)
                raw = self.make_raw()
                _Client(raw).upload_local_file(self.path, parent_id="target")
                raw.upload_token.assert_called_once_with(
                    self.path.name, len(content), "target", md5=b64encode(md5(content).digest()).decode()
                )
                raw.check_can_flash_upload.assert_not_called()
                raw.cdn_upload.assert_called_once_with(
                    self.path, raw.upload_token.return_value["data"],
                    content_type="application/octet-stream", chunk_size=5 * 1024 * 1024,
                )
                raw.upload_info.assert_called_once_with("upload-task")
        self.sleep.assert_not_called()

    def test_observed_flash_success_without_flag_uses_same_task_cdn(self):
        # probe-upload-shape.log 的真实 flash 响应：成功不等于已完成秒传。
        raw = self.make_raw()
        raw.check_can_flash_upload.return_value = {"msg": "success"}
        result = _Client(cast(_RawSdk, raw)).upload_local_file(self.path)
        self.assertEqual(result, raw.upload_info.return_value)
        raw.upload_token.assert_called_once()
        raw.cdn_upload.assert_called_once_with(
            self.path, raw.upload_token.return_value["data"],
            content_type="application/octet-stream", chunk_size=5 * 1024 * 1024,
        )
        self.assertEqual([call[0] for call in raw.mock_calls], [
            "upload_token", "check_can_flash_upload", "cdn_upload", "upload_info",
        ])

    def test_observed_small_file_pending_147_requires_cdn_and_bounded_wait(self):
        self.path.write_bytes(b"sample")
        raw = self.make_raw()
        raw.upload_token.return_value["data"].update({
            "creds": {"accessKeyID": "fixture-key", "secretAccessKey": "fixture-secret", "sessionToken": "fixture-token"},
            "fullEndPoint": "https://cdn.example.invalid", "bucketName": "fixture", "objectPath": "synthetic",
        })
        pending = {"code": 147, "msg": "文件上传中"}
        ready = raw.upload_info.return_value
        raw.upload_info.side_effect = [pending, pending, pending, ready]
        self.assertEqual(_Client(raw).upload_local_file(self.path), ready)
        raw.upload_token.assert_called_once()
        raw.check_can_flash_upload.assert_not_called()
        raw.cdn_upload.assert_called_once()
        self.assertEqual([call[0] for call in raw.mock_calls], [
            "upload_token", "cdn_upload", *(["upload_info"] * 4),
        ])
        self.assertEqual(self.sleep.call_args_list, [mock.call(2)] * 3)
        self.assertEqual(pending, {"code": 147, "msg": "文件上传中"})

    def test_destination_name_and_chunk_bounds(self):
        for name, parent, size, expected_name, expected_parent, expected_size in (
            ("  renamed.mkv  ", "target", 2 * 1024 * 1024, "renamed.mkv", "target", 2 * 1024 * 1024),
            ("  ", "0", 1, "sample.mkv", None, 1024 * 1024),
            ("", "", 128 * 1024 * 1024, "sample.mkv", None, 64 * 1024 * 1024),
        ):
            with self.subTest(parent=parent, size=size):
                raw = self.make_raw()
                _Client(raw).upload_local_file(self.path, name=name, parent_id=parent, chunk_size=size)
                raw.upload_token.assert_called_once_with(expected_name, 1024 * 1024, expected_parent)
                raw.cdn_upload.assert_called_once_with(
                    self.path, raw.upload_token.return_value["data"],
                    content_type="application/octet-stream", chunk_size=expected_size,
                )

    def test_token_rejection_or_missing_task_never_starts_second_task(self):
        for response in (None, {}, {"msg": "success", "data": {}},
                         {"data": {"taskId": "  "}},
                         {"code": 403, "msg": "拒绝", "data": {"taskId": "bad"}},
                         {"code": 0, "data": {"code": 403, "taskId": "bad"}}):
            with self.subTest(response=response):
                raw = self.make_raw()
                raw.upload_token.return_value = response
                with self.assertRaises(GuangYaWriteRejected):
                    _Client(raw).upload_local_file(self.path)
                raw.upload_token.assert_called_once()
                raw.check_can_flash_upload.assert_not_called()
                raw.cdn_upload.assert_not_called()
                raw.upload_info.assert_not_called()

    def test_flash_failure_or_invalid_flag_does_not_start_cdn(self):
        for response in (None, {}, {"msg": "检查失败"},
                         {"code": 403, "canFlashUpload": True},
                         {"data": {"success": False, "canFlashUpload": False}},
                         {"canFlashUpload": "false"}, {"canFlashUpload": None}):
            with self.subTest(response=response):
                raw = self.make_raw()
                raw.check_can_flash_upload.return_value = response
                with self.assertRaises(GuangYaWriteRejected):
                    _Client(raw).upload_local_file(self.path)
                raw.upload_token.assert_called_once()
                raw.cdn_upload.assert_not_called()
                raw.upload_info.assert_not_called()

    def test_cdn_failure_stops_without_polling_or_new_token(self):
        for failure in (RuntimeError("cdn failed"), "", None, {"msg": "上传失败"}):
            with self.subTest(failure=failure):
                raw = self.make_raw()
                if isinstance(failure, Exception):
                    raw.cdn_upload.side_effect = failure
                else:
                    raw.cdn_upload.return_value = failure
                with self.assertRaises(RuntimeError):
                    _Client(raw).upload_local_file(self.path)
                raw.upload_token.assert_called_once()
                raw.cdn_upload.assert_called_once()
                raw.upload_info.assert_not_called()

    def test_processing_is_polled_with_a_bound_and_returned_unchanged(self):
        pending = {"code": 147, "msg": "文件上传中"}
        for finish in (True, False):
            with self.subTest(finish=finish):
                raw = self.make_raw()
                self.sleep.reset_mock()
                ready = raw.upload_info.return_value
                raw.upload_info.side_effect = [pending, ready] if finish else [pending] * 4
                result = _Client(cast(_RawSdk, raw)).upload_local_file(self.path)
                self.assertEqual(result, ready if finish else pending)
                self.assertEqual(raw.upload_info.call_count, 2 if finish else 4)
                self.assertEqual(self.sleep.call_args_list, [mock.call(2)] * (1 if finish else 3))
                raw.upload_token.assert_called_once()

    def test_small_and_flash_processing_are_bounded_and_never_claim_complete(self):
        for small in (True, False):
            with self.subTest(small=small):
                self.path.write_bytes(b"sample" if small else b"x" * (1024 * 1024))
                raw = self.make_raw()
                raw.check_can_flash_upload.return_value = {"canFlashUpload": True}
                pending = {"msg": "文件上传中", "data": {"taskId": "upload-task"}}
                raw.upload_info.return_value = pending
                self.assertEqual(_Client(raw).upload_local_file(self.path), pending)
                self.assertEqual(raw.upload_info.call_args_list, [mock.call("upload-task")] * 4)
                self.assertEqual(raw.cdn_upload.call_count, 1 if small else 0)
        self.assertEqual(self.sleep.call_args_list, [mock.call(2)] * 6)

    def test_upload_info_failure_is_not_masked_by_processing_or_success(self):
        for response in (None, {}, [], {"msg": "文件上传失败"},
                         {"code": 403, "msg": "文件上传中"},
                         {"code": 147, "msg": "success"},
                         {"code": 147, "msg": "文件上传中 "},
                         {"code": 147, "msg": "文件上传中", "error": "rejected"},
                         {"code": 147, "msg": "文件上传中", "data": {"error": "rejected"}},
                         {"code": 147, "msg": "文件上传中", "data": {"errors": ["rejected"]}},
                         {"code": 147, "msg": "文件上传中", "data": {"code": 403}},
                         {"msg": "文件上传中", "data": {"success": False}},
                         {"msg": "文件上传中", "data": {"msg": "上传失败"}},
                         {"code": 0, "data": {"code": 403, "msg": "失败"}}):
            with self.subTest(response=response):
                raw = self.make_raw()
                raw.upload_info.return_value = response
                with self.assertRaises(GuangYaWriteRejected):
                    _Client(raw).upload_local_file(self.path)
                raw.upload_info.assert_called_once_with("upload-task")
                raw.upload_token.assert_called_once()
        self.sleep.assert_not_called()

    def test_real_sdk_primitives_keep_multipart_bytes_and_target(self):
        import json
        from xml.etree.ElementTree import fromstring

        from guangyaclient import GuangyaClient as RawClient

        self.path.write_bytes(b"x" * (2 * 1024 * 1024 + 17))
        requests = []
        parts = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("get_res_center_token"):
                body = json.loads(request.read())
                self.assertEqual(body["parentId"], "synthetic-target")
                self.assertEqual(body["name"], "renamed.mkv")
                self.assertEqual(body["res"]["fileSize"], self.path.stat().st_size)
                return httpx.Response(200, json={"msg": "success", "data": {
                    "taskId": "upload-task", "fullEndPoint": "https://cdn.example.invalid",
                    "bucketName": "fixture", "objectPath": "synthetic",
                    "creds": {"accessKeyID": "fixture-key", "secretAccessKey": "fixture-secret", "sessionToken": "fixture-token"},
                }})
            if request.url.path.endswith("check_can_flash_upload"):
                self.assertEqual(json.loads(request.read())["taskId"], "upload-task")
                return httpx.Response(200, json={"msg": "success"})
            if request.url.path.endswith("get_info_by_task_id"):
                self.assertEqual(len(parts), 3)
                if sum(req.url.path.endswith("get_info_by_task_id") for req in requests) == 1:
                    return httpx.Response(200, json={"code": 147, "msg": "文件上传中"})
                return httpx.Response(200, json={"msg": "success", "data": {"fileId": "uploaded"}})
            self.assertEqual(request.url.host, "cdn.example.invalid")
            if "uploads" in request.url.params:
                return httpx.Response(200, text="<InitiateMultipartUploadResult><UploadId>fixture-upload</UploadId></InitiateMultipartUploadResult>")
            self.assertEqual(request.url.params["uploadId"], "fixture-upload")
            if request.method == "PUT":
                parts.append(request.read())
                self.assertEqual(int(request.url.params["partNumber"]), len(parts))
                return httpx.Response(200, headers={"etag": f'"part-{len(parts)}"'})
            self.assertEqual(len(fromstring(request.read()).findall("Part")), 3)
            return httpx.Response(200, text='<CompleteMultipartUploadResult><ETag>"fixture-etag"</ETag></CompleteMultipartUploadResult>')

        raw = RawClient(access_token="fixture-access", device_id="fixture-device")
        raw.close()
        raw._client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(raw.close)
        result = _Client(cast(_RawSdk, raw)).upload_local_file(
            self.path, name="renamed.mkv", parent_id="synthetic-target", chunk_size=1024 * 1024,
        )
        self.assertEqual(result["data"]["fileId"], "uploaded")
        self.assertEqual([len(part) for part in parts], [1024 * 1024, 1024 * 1024, 17])
        self.assertEqual(b"".join(parts), self.path.read_bytes())
        self.assertEqual(sum(request.url.path.endswith("get_res_center_token") for request in requests), 1)

    def test_invalid_local_input_does_not_allocate_remote_task(self):
        for path, chunk_size in ((self.path.with_name("missing"), 1024 * 1024), (self.path, "invalid")):
            with self.subTest(path=path, chunk_size=chunk_size):
                raw = self.make_raw()
                with self.assertRaises((FileNotFoundError, ValueError)):
                    _Client(raw).upload_local_file(path, chunk_size=chunk_size)
                raw.upload_token.assert_not_called()


class _RecycleClient:
    def __init__(self) -> None:
        self.logged_in = True
        self.credential_generation = 7
        self.items = [
            GuangYaFile(
                "trash-1",
                "旧文件.mkv",
                False,
                size=123,
                etag="gcid-1",
                parent_id="source",
            )
        ]

    def list_recycle(self, **_kwargs):
        return deepcopy(self.items)

    def restore_from_recycle(self, file_ids):
        selected = {str(item) for item in file_ids}
        self.items = [item for item in self.items if item.file_id not in selected]
        return "restore-task"

    def clear_recycle_bin(self):
        self.items = []
        return "clear-task"

    def task_status(self, _task_id):
        return {"data": {"status": "completed", "progress": 100}}

    def close(self):
        return True


class GuangYaRecycleServiceTests(unittest.TestCase):
    def test_restore_and_clear_use_real_ids_and_return_original_task(self):
        client = cast(GuangYaClient, _RecycleClient())
        frozen = recycle.prepare_restore(client, ["trash-1", "trash-1"])
        self.assertEqual([item["file_id"] for item in frozen["items"]], ["trash-1"])
        self.assertEqual(len(client.list_recycle()), 1)
        restored = recycle.restore_prepared(client, frozen)
        self.assertEqual(restored["status"], "accepted")
        self.assertFalse(restored["verified"])
        self.assertTrue(restored["verification_pending"])
        self.assertEqual(restored["task_id"], "restore-task")
        self.assertEqual(client.list_recycle(), [])
        self.assertEqual(recycle.read_task(client, restored["task_id"]), {"status": "completed", "progress": 1.0})

        client = cast(GuangYaClient, _RecycleClient())
        frozen = recycle.prepare_clear(client)
        cleared = recycle.clear_prepared(client, frozen)
        self.assertTrue(cleared["irreversible"])
        self.assertEqual(cleared["status"], "accepted")
        self.assertEqual(cleared["task_id"], "clear-task")
        self.assertFalse(cleared["verified"])
        self.assertEqual(client.list_recycle(), [])

    def test_clear_snapshot_is_stable_when_provider_reorders_items(self):
        fake = _RecycleClient()
        fake.items = [GuangYaFile("trash-2", "第二项", False, size=2, parent_id="0"),
                      GuangYaFile("trash-1", "第一项", False, size=1, parent_id="0")]
        client = cast(GuangYaClient, fake)
        snapshot = recycle.prepare_clear(client)
        fake.items.reverse()
        self.assertEqual(snapshot, recycle.prepare_clear(client))
        self.assertEqual(recycle.clear_prepared(client, snapshot)["status"], "accepted")


class _AccountClient:
    logged_in = True

    def account_info(self):
        return {
            "data": {
                "nickname": "Alice",
                "phone": "13800138000",
                "email": "alice@example.com",
                "token": "must-not-leak",
                "userId": "must-not-leak",
            }
        }

    def account_storage_info(self):
        return {"code": 0, "data": {"totalSpaceSize": 1000, "usedSpaceSize": 250}}

    def close(self):
        return True


class GuangYaAccountActionTests(unittest.TestCase):
    def test_account_projection_masks_identity_and_whitelists_capacity(self) -> None:
        with mock.patch.object(
            guangya_account, "GuangYaClient", return_value=_AccountClient()
        ):
            result = guangya_account.account_status()
        self.assertEqual(result["masked_phone"], "138****8000")
        self.assertEqual(result["masked_email"], "a***@example.com")
        self.assertEqual(result["storage"]["available_bytes"], 750)
        rendered = str(result)
        self.assertNotIn("must-not-leak", rendered)
        self.assertNotIn("13800138000", rendered)


class _ShareClient(GuangYaClient):
    logged_in = True
    credential_generation = 9

    def __init__(self) -> None:
        self.logged_in = True
        self.credential_generation = 9
        self.shares = [
            {
                "id": "10001",
                "shareId": "share-1",
                "title": "动画",
                "status": "active",
                "fileCount": 1,
            }
        ]
        self.file = GuangYaFile(
            "file-1", "动画", True, parent_id="0", etag="etag-1"
        )
        self.create_response: dict[str, Any] = {
            "code": 200,
            "data": {"shareId": "created-share", "accessCode": "AB12"},
        }

    def list_user_shares(self, **_kwargs):
        return deepcopy(self.shares)

    def delete_user_shares(self, share_ids):
        selected = {str(item) for item in share_ids}
        self.shares = [
            item for item in self.shares if str(item.get("id")) not in selected
        ]
        return True

    def file_info(self, file_id):
        return deepcopy(self.file) if str(file_id) == self.file.file_id else None

    def create_user_share(self, file_ids, **_kwargs):
        if file_ids != ["file-1"]:
            raise AssertionError("unexpected file selection")
        return deepcopy(self.create_response)

    def close(self):
        return True


class GuangYaShareServiceTests(unittest.TestCase):
    def test_share_snapshot_requires_management_record_id_not_public_link_id(self):
        client = _ShareClient()
        item = client.user_share_snapshot()["items"][0]
        self.assertEqual(item["share_id"], "10001")
        self.assertEqual(item["title"], "动画")
        del client.shares[0]["id"]
        with self.assertRaises(GuangYaShareError):
            client.user_share_snapshot()

    def test_sdk_creation_and_revoke_use_real_ids(self):
        client = _ShareClient()
        with mock.patch("app.clients.guangya.GuangYaClient", return_value=client):
            listed = sdk.cloud.shares()
            self.assertEqual(listed["items"][0]["share_id"], "10001")
            created = sdk.cloud.share(["file-1"], expires_days=7)
            self.assertEqual(created["access_code"], "AB12")
            self.assertIn("created-share", created["share_url"])
            self.assertEqual(created["status"], "completed")
            self.assertTrue(sdk.cloud.revoke_share(["10001"])["verified"])

    def test_create_share_does_not_expose_untrusted_provider_url_or_code(self):
        client = _ShareClient()
        client.create_response = {"code": 200, "data": {
            "shareId": "valid_share_1234", "shareUrl": "https://attacker.invalid/steal",
            "accessCode": "<script>alert(1)</script>",
        }}
        with mock.patch("app.clients.guangya.GuangYaClient", return_value=client):
            created = sdk.cloud.share(["file-1"])
        self.assertEqual(created["share_url"], "https://www.guangyapan.com/s/valid_share_1234#/share")
        self.assertEqual(created["access_code"], "")
        self.assertNotIn("attacker.invalid", str(created))
        self.assertNotIn("<script>", str(created))

    def test_create_share_without_verifiable_handle_is_reported_as_accepted(self):
        client = _ShareClient()
        client.create_response = {"code": 200, "msg": "操作成功"}
        with mock.patch("app.clients.guangya.GuangYaClient", return_value=client):
            created = sdk.cloud.share(["file-1"])
        self.assertEqual(created["status"], "accepted")
        self.assertTrue(created["verification_pending"])
        self.assertFalse(created["verified"])



if __name__ == "__main__":
    unittest.main()
