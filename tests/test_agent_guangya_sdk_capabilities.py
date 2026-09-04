"""光鸭 SDK P1-P3 能力的领域动作、确认边界与持久任务测试。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from app.agent import guangya_account_actions as account_actions
from app.agent import guangya_recycle_actions as recycle_actions
from app.agent import guangya_share_actions as share_actions
from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.models import ToolContext
from app.clients.guangya import GuangYaClient, GuangYaFile


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

    def user_info(self):
        return {
            "data": {
                "nickname": "测试用户",
                "phone": "13800138000",
                "storage": {"totalSpace": 1000, "usedSpace": 250},
            }
        }

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

    def file_upload(self, file_path, **kwargs):
        self.calls.append(("upload", str(file_path), dict(kwargs)))
        return {"msg": "文件上传中", "data": {"taskId": "upload-task"}}


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
        with (
            mock.patch.object(recycle_actions, "GuangYaClient", return_value=client),
            mock.patch.object(recycle_actions.time, "sleep", return_value=None),
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
            self.assertTrue(restored.data["verified"])
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
            self.assertTrue(cleared.data["verified"])

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
                "totalSpace": 1000,
                "usedSpace": 250,
                "token": "must-not-leak",
                "userId": "must-not-leak",
            }
        }

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
