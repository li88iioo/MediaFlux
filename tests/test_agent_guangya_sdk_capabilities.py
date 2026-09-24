"""光鸭 SDK P1-P3 能力的领域动作、确认边界与持久任务测试。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import httpx

from app.agent import guangya_account_actions as account_actions
from app.agent import guangya_recycle_actions as recycle_actions
from app.agent import guangya_share_actions as share_actions
from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.models import ToolContext
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

        self.assertEqual(client.copy(["file-1", "file-1"], "target"), "copy-task")
        recycle = client.list_recycle(max_items=10)
        self.assertEqual([(item.file_id, item.name) for item in recycle], [("trash-1", "旧文件.mkv")])
        self.assertEqual(client.restore_from_recycle(["trash-1"]), "restore-task")
        self.assertEqual(client.clear_recycle_bin(), "clear-task")
        self.assertEqual(client.task_status("restore-task")["data"]["status"], "completed")
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
        self.assertTrue(client.delete_user_shares(["share-1"]))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.mkv"
            path.write_bytes(b"sample")
            response = client.upload_local_file(path, parent_id="target")
        self.assertEqual(response["data"]["taskId"], "upload-task")
        share_call = next(item for item in raw.calls if item[0] == "share_create")
        self.assertFalse(share_call[2]["auto_fill_code"])

    def test_agent_capability_summary_keeps_local_upload_disabled(self) -> None:
        result = workspace_actions.summarize_guangya_capabilities({})

        self.assertNotIn("local_upload", result.data["write_operations"])
        self.assertEqual(result.data["agent_disabled_operations"], ["local_upload"])


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
                    result = _Client(raw).upload_local_file(self.path)
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
        result = _Client(raw).upload_local_file(self.path)
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
                result = _Client(raw).upload_local_file(self.path)
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
        result = _Client(raw).upload_local_file(
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


class GuangYaRecycleActionTests(unittest.TestCase):
    def test_list_restore_and_clear_use_frozen_private_snapshots(self) -> None:
        client = _RecycleClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(
            recycle_actions, "GuangYaClient", return_value=client
        ):
            listed = recycle_actions.list_guangya_recycle(
                {"page": 1, "page_size": 50}, context
            )
            self.assertNotIn("trash-1", str(listed.to_dict()))
            collection = listed.references[0].value
            arguments = {"guangya_recycle_items": collection, "indices": [1]}
            preview, fingerprint = recycle_actions.prepare_restore_guangya_recycle(
                arguments, context
            )
            self.assertEqual(preview.data["count"], 1)
            restored = recycle_actions.execute_restore_guangya_recycle(
                arguments, fingerprint, context
            )
            self.assertEqual(restored.status, "accepted")
            self.assertFalse(restored.data["verified"])
            self.assertTrue(restored.data["verification_pending"])
            self.assertEqual(restored.references[0].kind, "guangya_task")

            client.items = [
                GuangYaFile("trash-2", "待清空", True, parent_id="0", etag="dir")
            ]
            clear_preview, clear_fingerprint = (
                recycle_actions.prepare_clear_guangya_recycle({}, context)
            )
            self.assertTrue(clear_preview.data["irreversible"])
            cleared = recycle_actions.execute_clear_guangya_recycle(
                {}, clear_fingerprint, context
            )
            self.assertEqual(cleared.status, "accepted")
            self.assertFalse(cleared.data["verified"])
            self.assertTrue(cleared.data["verification_pending"])
            self.assertEqual(cleared.references[0].kind, "guangya_task")

            status = recycle_actions.query_guangya_task_status(
                {
                    "guangya_task": {
                        "task_id": "clear-task",
                        "operation": "recycle_clear",
                    }
                },
                context,
            )
            self.assertEqual(status.status, "completed")

    def test_clear_fingerprint_is_stable_when_provider_reorders_items(self) -> None:
        client = _RecycleClient()
        client.items = [
            GuangYaFile("trash-2", "第二项", False, size=2, parent_id="0"),
            GuangYaFile("trash-1", "第一项", False, size=1, parent_id="0"),
        ]
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(
            recycle_actions, "GuangYaClient", return_value=client
        ):
            _preview, fingerprint = recycle_actions.prepare_clear_guangya_recycle(
                {}, context
            )
            client.items.reverse()
            _safe, reordered_fingerprint = recycle_actions._clear_snapshot()
        self.assertEqual(reordered_fingerprint, fingerprint)


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
            account_actions, "GuangYaClient", return_value=_AccountClient()
        ):
            result = account_actions.get_guangya_account_status({})
        self.assertEqual(result.data["masked_phone"], "138****8000")
        self.assertEqual(result.data["masked_email"], "a***@example.com")
        self.assertEqual(result.data["storage"]["available_bytes"], 750)
        rendered = str(result.to_dict())
        self.assertNotIn("must-not-leak", rendered)
        self.assertNotIn("13800138000", rendered)


class _ShareClient:
    def __init__(self) -> None:
        self.logged_in = True
        self.credential_generation = 9
        self.shares = [
            {
                "shareId": "share-1",
                "title": "动画",
                "status": "active",
                "fileCount": 1,
            }
        ]
        self.file = GuangYaFile(
            "file-1", "动画", True, parent_id="0", etag="etag-1"
        )
        self.create_response = {
            "code": 200,
            "data": {"shareId": "created-share", "accessCode": "AB12"},
        }

    def list_user_shares(self, **_kwargs):
        return deepcopy(self.shares)

    def delete_user_shares(self, share_ids):
        selected = {str(item) for item in share_ids}
        self.shares = [
            item for item in self.shares if str(item.get("shareId")) not in selected
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


class GuangYaShareActionTests(unittest.TestCase):
    def test_create_returns_link_only_to_public_result_and_revoke_revalidates(self) -> None:
        client = _ShareClient()
        context = ToolContext(owner="owner", session_id="session")
        observation_ref = "OBS" + "A" * 32
        object_ref = "OBJ" + "B" * 24
        observation = {"credential_generation": 9}
        entry = {
            "file_id": "file-1",
            "parent_id": "0",
            "name": "动画",
            "is_dir": True,
            "size": 0,
            "etag": "etag-1",
            "updated_at": 0,
        }
        normalized = share_actions.guangya_share_create_arguments(
            {
                "observation_ref": observation_ref,
                "object_refs": [object_ref],
                "expires_days": 7,
                "auto_access_code": True,
                "allow_download": True,
            }
        )
        with (
            mock.patch.object(share_actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                share_actions,
                "load_directory_observation",
                return_value=observation,
            ),
            mock.patch.object(
                share_actions,
                "observation_entry_map",
                return_value={object_ref: entry},
            ),
            mock.patch.object(share_actions.time, "sleep", return_value=None),
        ):
            preview, fingerprint = share_actions.prepare_create_guangya_share(
                normalized, context
            )
            self.assertNotIn("file-1", str(preview.to_dict()))
            created = share_actions.execute_create_guangya_share(
                normalized, fingerprint, context
            )
            self.assertEqual(created.data["access_code"], "AB12")
            self.assertNotEqual(created.data["access_code"], "200")
            self.assertIn("created-share", created.data["share_url"])
            self.assertNotIn("share_url", created.model_data)

            listed = share_actions.list_guangya_user_shares(
                {"page": 1, "page_size": 50}, context
            )
            collection = listed.references[0].value
            revoke_args = {"guangya_shares": collection, "indices": [1]}
            revoke_preview, revoke_fingerprint = (
                share_actions.prepare_revoke_guangya_shares(revoke_args, context)
            )
            self.assertEqual(revoke_preview.data["count"], 1)
            revoked = share_actions.execute_revoke_guangya_shares(
                revoke_args, revoke_fingerprint, context
            )
            self.assertTrue(revoked.data["verified"])

    def test_create_share_does_not_expose_untrusted_provider_url_or_code(self) -> None:
        client = _ShareClient()
        client.create_response = {
            "code": 200,
            "data": {
                "shareId": "valid_share_1234",
                "shareUrl": "https://attacker.invalid/steal",
                "accessCode": "<script>alert(1)</script>",
            },
        }
        context = ToolContext(owner="owner", session_id="session")
        observation_ref = "OBS" + "A" * 32
        object_ref = "OBJ" + "B" * 24
        normalized = share_actions.guangya_share_create_arguments(
            {
                "observation_ref": observation_ref,
                "object_refs": [object_ref],
            }
        )
        entry = {
            "file_id": "file-1",
            "parent_id": "0",
            "name": "动画",
            "is_dir": True,
            "size": 0,
            "etag": "etag-1",
            "updated_at": 0,
        }
        with (
            mock.patch.object(share_actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                share_actions,
                "load_directory_observation",
                return_value={"credential_generation": 9},
            ),
            mock.patch.object(
                share_actions,
                "observation_entry_map",
                return_value={object_ref: entry},
            ),
        ):
            _preview, fingerprint = share_actions.prepare_create_guangya_share(
                normalized, context
            )
            created = share_actions.execute_create_guangya_share(
                normalized, fingerprint, context
            )

        self.assertEqual(
            created.data["share_url"],
            "https://www.guangyapan.com/s/valid_share_1234#/share",
        )
        self.assertEqual(created.data["access_code"], "")
        self.assertNotIn("attacker.invalid", str(created.to_dict()))
        self.assertNotIn("<script>", str(created.to_dict()))

    def test_create_share_without_verifiable_handle_is_reported_as_accepted(self) -> None:
        client = _ShareClient()
        client.create_response = {"code": 200, "msg": "操作成功"}
        context = ToolContext(owner="owner", session_id="session")
        observation_ref = "OBS" + "A" * 32
        object_ref = "OBJ" + "B" * 24
        normalized = share_actions.guangya_share_create_arguments(
            {
                "observation_ref": observation_ref,
                "object_refs": [object_ref],
            }
        )
        entry = {
            "file_id": "file-1",
            "parent_id": "0",
            "name": "动画",
            "is_dir": True,
            "size": 0,
            "etag": "etag-1",
            "updated_at": 0,
        }
        with (
            mock.patch.object(share_actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                share_actions,
                "load_directory_observation",
                return_value={"credential_generation": 9},
            ),
            mock.patch.object(
                share_actions,
                "observation_entry_map",
                return_value={object_ref: entry},
            ),
        ):
            _preview, fingerprint = share_actions.prepare_create_guangya_share(
                normalized, context
            )
            created = share_actions.execute_create_guangya_share(
                normalized, fingerprint, context
            )

        self.assertEqual(created.status, "accepted")
        self.assertTrue(created.data["verification_pending"])
        self.assertFalse(created.model_data["share_created"])



if __name__ == "__main__":
    unittest.main()
