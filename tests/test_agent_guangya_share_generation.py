"""分享创建、撤销在确认复核后切换凭据时必须停止写入。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from app.agent import guangya_share_actions as actions
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
from app.clients.guangya import GuangYaFile


class _ShareClient:
    logged_in = True

    def __init__(
        self,
        generation: int,
        *,
        fail_verification: bool = False,
        omit_created_handle: bool = False,
    ) -> None:
        self.credential_generation = generation
        self.file = GuangYaFile(
            "file-1", "测试目录", True, parent_id="0", etag="test-etag"
        )
        self.shares = [
            {"id": "20001", "shareId": "existing-share", "title": "测试分享", "status": "active"}
        ]
        self.writes: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False
        self.fail_verification = fail_verification
        self.omit_created_handle = omit_created_handle

    def file_info(self, file_id):
        return deepcopy(self.file) if file_id == self.file.file_id else None

    def list_user_shares(self, **_kwargs):
        if self.writes and self.fail_verification:
            raise RuntimeError("模拟写后回读暂不可用")
        return deepcopy(self.shares)

    def create_user_share(self, file_ids, **_kwargs):
        self.writes.append(("create", tuple(file_ids)))
        if self.omit_created_handle:
            return {"code": 200, "msg": "操作成功"}
        return {"code": 200, "data": {"shareId": "created-share"}}

    def delete_user_shares(self, share_ids):
        self.writes.append(("revoke", tuple(share_ids)))
        self.shares = [item for item in self.shares if item["id"] not in share_ids]
        return True

    def close(self):
        self.closed = True
        return True


class GuangYaShareGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(owner="generation-test", session_id="session")
        self.generation = 9
        self.observation_ref = "OBS" + "A" * 32
        self.object_ref = "OBJ" + "B" * 24
        self.entry = {
            "file_id": "file-1",
            "parent_id": "0",
            "name": "测试目录",
            "is_dir": True,
            "size": 0,
            "etag": "test-etag",
            "updated_at": 0,
        }
        self.enterContext(mock.patch.object(
            actions,
            "load_directory_observation",
            side_effect=lambda *_args, **_kwargs: {
                "credential_generation": self.generation
            },
        ))
        self.enterContext(mock.patch.object(
            actions, "observation_entry_map", return_value={self.object_ref: self.entry}
        ))

    def _arguments(self, operation: str):
        if operation == "create":
            return actions.guangya_share_create_arguments({
                "observation_ref": self.observation_ref,
                "object_refs": [self.object_ref],
            })
        return {
            "guangya_shares_ref": "ref_generation_test",
            "indices": [1],
            "guangya_shares": {
                "credential_generation": self.generation,
                "page": 1,
                "page_size": 50,
                "items": [actions._share_snapshot(_ShareClient(self.generation).shares[0])],
            },
        }

    def _handlers(self, operation: str):
        if operation == "create":
            return actions.prepare_create_guangya_share, actions.execute_create_guangya_share
        return actions.prepare_revoke_guangya_shares, actions.execute_revoke_guangya_shares

    def _assert_generation_change_blocked(self, operation: str) -> None:
        clients = [_ShareClient(9), _ShareClient(9), _ShareClient(10)]
        arguments = self._arguments(operation)
        prepare, execute = self._handlers(operation)
        with mock.patch.object(actions, "GuangYaClient", side_effect=clients) as factory:
            _preview, fingerprint = prepare(arguments, self.context)
            with self.assertRaises(AgentToolError) as caught:
                execute(arguments, fingerprint, self.context)

        self.assertEqual(caught.exception.code, "confirmation_stale")
        self.assertEqual(factory.call_count, 3)
        self.assertTrue(all(not client.writes for client in clients))
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(clients[-1].shares[0]["shareId"], "existing-share")

    def test_create_rejects_generation_change_when_write_client_is_created(self) -> None:
        self._assert_generation_change_blocked("create")

    def test_revoke_rejects_generation_change_when_write_client_is_created(self) -> None:
        self._assert_generation_change_blocked("revoke")

    def _assert_same_generation_succeeds(self, operation: str) -> None:
        for generation in (0, 9):
            with self.subTest(generation=generation):
                self.generation = generation
                clients = [_ShareClient(generation) for _ in range(3)]
                arguments = self._arguments(operation)
                prepare, execute = self._handlers(operation)
                with mock.patch.object(actions, "GuangYaClient", side_effect=clients):
                    preview, fingerprint = prepare(arguments, self.context)
                    result = execute(arguments, fingerprint, self.context)

                self.assertEqual(result.status, "completed")
                self.assertTrue(result.data["verified"])
                self.assertFalse(result.data["verification_pending"])
                self.assertNotIn("credential_generation", str(preview.to_dict()))
                self.assertNotIn("credential_generation", str(result.to_dict()))
                expected_id = "file-1" if operation == "create" else "20001"
                self.assertEqual(clients[-1].writes, [(operation, (expected_id,))])
                self.assertTrue(all(not client.writes for client in clients[:-1]))
                self.assertTrue(all(client.closed for client in clients))

    def test_create_keeps_same_generation_success_including_zero(self) -> None:
        self._assert_same_generation_succeeds("create")

    def test_revoke_keeps_same_generation_success_including_zero(self) -> None:
        self._assert_same_generation_succeeds("revoke")

    def test_revoke_preserves_accepted_result_when_readback_fails(self) -> None:
        clients = [
            _ShareClient(9), _ShareClient(9), _ShareClient(9, fail_verification=True)
        ]
        arguments = self._arguments("revoke")
        with mock.patch.object(actions, "GuangYaClient", side_effect=clients):
            _preview, fingerprint = actions.prepare_revoke_guangya_shares(
                arguments, self.context
            )
            result = actions.execute_revoke_guangya_shares(
                arguments, fingerprint, self.context
            )

        self.assertEqual(result.status, "accepted")
        self.assertFalse(result.data["verified"])
        self.assertTrue(result.data["verification_pending"])
        self.assertNotIn("credential_generation", str(result.to_dict()))
        self.assertEqual(len(clients[-1].writes), 1)
        self.assertTrue(clients[-1].closed)

    def test_create_preserves_accepted_result_without_verifiable_handle(self) -> None:
        clients = [
            _ShareClient(9), _ShareClient(9), _ShareClient(9, omit_created_handle=True)
        ]
        arguments = self._arguments("create")
        with mock.patch.object(actions, "GuangYaClient", side_effect=clients):
            _preview, fingerprint = actions.prepare_create_guangya_share(
                arguments, self.context
            )
            result = actions.execute_create_guangya_share(
                arguments, fingerprint, self.context
            )

        self.assertEqual(result.status, "accepted")
        self.assertFalse(result.data["verified"])
        self.assertTrue(result.data["verification_pending"])
        self.assertNotIn("credential_generation", str(result.to_dict()))
        self.assertEqual(len(clients[-1].writes), 1)
        self.assertTrue(clients[-1].closed)


if __name__ == "__main__":
    unittest.main()
