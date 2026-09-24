"""回收站确认复核与实际写入之间的凭据世代隔离回归。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from app.agent import guangya_recycle_actions as actions
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
from app.clients.guangya import GuangYaFile


class _RecycleClient:
    logged_in = True

    def __init__(self, generation: int) -> None:
        self.credential_generation = generation
        self.items = [
            GuangYaFile(
                "trash-1",
                "待恢复.mkv",
                False,
                parent_id="source",
                size=123,
                etag="test-gcid",
            )
        ]
        self.writes: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False

    def list_recycle(self, **_kwargs):
        return deepcopy(self.items)

    def restore_from_recycle(self, file_ids):
        self.writes.append(("restore", tuple(file_ids)))
        self.items = [item for item in self.items if item.file_id not in file_ids]
        return "restore-task"

    def clear_recycle_bin(self):
        self.writes.append(("clear", ()))
        self.items = []
        return "clear-task"

    def close(self):
        self.closed = True
        return True


class GuangYaRecycleGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(owner="generation-test", session_id="session")

    def _arguments(self, operation: str, generation: int):
        if operation == "clear":
            return {}
        return {
            "guangya_recycle_items_ref": "ref_generation_test",
            "indices": [1],
            "guangya_recycle_items": {
                "credential_generation": generation,
                "page": 1,
                "page_size": 50,
                "items": [actions._snapshot(_RecycleClient(generation).items[0])],
            },
        }

    def _handlers(self, operation: str):
        if operation == "clear":
            return (
                actions.prepare_clear_guangya_recycle,
                actions.execute_clear_guangya_recycle,
            )
        return (
            actions.prepare_restore_guangya_recycle,
            actions.execute_restore_guangya_recycle,
        )

    def _assert_generation_change_blocked(self, operation: str) -> None:
        clients = [_RecycleClient(7), _RecycleClient(7), _RecycleClient(8)]
        arguments = self._arguments(operation, 7)
        prepare, execute = self._handlers(operation)
        with mock.patch.object(actions, "GuangYaClient", side_effect=clients) as factory:
            _preview, fingerprint = prepare(arguments, self.context)
            with self.assertRaises(AgentToolError) as caught:
                execute(arguments, fingerprint, self.context)

        self.assertEqual(caught.exception.code, "confirmation_stale")
        self.assertEqual(factory.call_count, 3)
        self.assertTrue(all(not client.writes for client in clients))
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(clients[-1].items[0].file_id, "trash-1")

    def test_restore_rejects_generation_change_when_write_client_is_created(self) -> None:
        self._assert_generation_change_blocked("restore")

    def test_clear_rejects_generation_change_when_write_client_is_created(self) -> None:
        self._assert_generation_change_blocked("clear")

    def _assert_same_generation_submits(self, operation: str) -> None:
        for generation in (0, 7):
            with self.subTest(generation=generation):
                clients = [_RecycleClient(generation) for _ in range(3)]
                arguments = self._arguments(operation, generation)
                prepare, execute = self._handlers(operation)
                with mock.patch.object(actions, "GuangYaClient", side_effect=clients):
                    preview, fingerprint = prepare(arguments, self.context)
                    result = execute(arguments, fingerprint, self.context)

                self.assertEqual(result.status, "accepted")
                self.assertFalse(result.data["verified"])
                self.assertTrue(result.data["verification_pending"])
                self.assertEqual(result.references[0].kind, "guangya_task")
                self.assertNotIn("credential_generation", preview.data)
                self.assertNotIn("credential_generation", result.data)
                expected_ids = ("trash-1",) if operation == "restore" else ()
                self.assertEqual(clients[-1].writes, [(operation, expected_ids)])
                self.assertTrue(all(not client.writes for client in clients[:-1]))
                self.assertTrue(all(client.closed for client in clients))

    def test_restore_keeps_same_generation_submission_including_zero(self) -> None:
        self._assert_same_generation_submits("restore")

    def test_clear_keeps_same_generation_submission_including_zero(self) -> None:
        self._assert_same_generation_submits("clear")



if __name__ == "__main__":
    unittest.main()
