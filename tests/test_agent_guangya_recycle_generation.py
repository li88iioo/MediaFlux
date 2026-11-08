"""回收站确认复核与实际写入之间的凭据世代隔离回归。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

import asyncio
from typing import cast

from app.modules import guangya_recycle as recycle
from app.clients.guangya import GuangYaClient, GuangYaFile


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
    @staticmethod
    def prepare(client, operation):
        client = cast(GuangYaClient, client)
        return recycle.prepare_clear(client) if operation == "clear" else recycle.prepare_restore(client, ["trash-1"])

    @staticmethod
    def apply(client, operation, snapshot, **kwargs):
        handler = recycle.clear_prepared if operation == "clear" else recycle.restore_prepared
        return handler(cast(GuangYaClient, client), snapshot, **kwargs)

    def test_restore_and_clear_reject_changed_write_client_generation(self):
        for operation in ("restore", "clear"):
            with self.subTest(operation=operation):
                snapshot = self.prepare(_RecycleClient(7), operation)
                client = _RecycleClient(8)
                with self.assertRaises(recycle.GuangYaRecycleStale):
                    self.apply(client, operation, snapshot)
                self.assertEqual(client.writes, [])
                self.assertEqual(client.items[0].file_id, "trash-1")

    def test_same_generation_submits_including_zero(self):
        for operation in ("restore", "clear"):
            for generation in (0, 7):
                with self.subTest(operation=operation, generation=generation):
                    snapshot = self.prepare(_RecycleClient(generation), operation)
                    client = _RecycleClient(generation)
                    result = self.apply(client, operation, snapshot)
                    self.assertEqual(result["status"], "accepted")
                    self.assertFalse(result["verified"])
                    self.assertTrue(result["verification_pending"])
                    self.assertEqual(result["task_id"], operation + "-task")
                    self.assertEqual(client.writes, [(operation, () if operation == "clear" else ("trash-1",))])

    def test_generation_change_during_revalidation_and_cancellation_both_prevent_write(self):
        for operation in ("restore", "clear"):
            for change in ("generation", "cancel"):
                with self.subTest(operation=operation, change=change):
                    client = _RecycleClient(7)
                    snapshot = self.prepare(client, operation)
                    cancelled = False
                    def read(**kwargs):
                        nonlocal cancelled
                        if change == "generation":
                            client.credential_generation = 8
                        cancelled = True
                        return deepcopy(client.items)
                    def check():
                        if cancelled:
                            raise asyncio.CancelledError()
                    with mock.patch.object(client, "list_recycle", side_effect=read):
                        error = recycle.GuangYaRecycleStale if change == "generation" else asyncio.CancelledError
                        with self.assertRaises(error):
                            self.apply(client, operation, snapshot, cancel_check=check)
                    self.assertEqual(client.writes, [])

    def test_changed_or_missing_selected_objects_do_not_restore(self):
        for field, value in (("name", "replaced.mkv"), ("size", 456), ("etag", "other-content"), ("file_id", "new-id")):
            with self.subTest(field=field):
                client = _RecycleClient(7)
                snapshot = self.prepare(client, "restore")
                setattr(client.items[0], field, value)
                with self.assertRaises(recycle.GuangYaRecycleStale):
                    self.apply(client, "restore", snapshot)
                self.assertEqual(client.writes, [])

    def test_clear_freezes_entire_set_while_restore_limits_itself_to_selected_ids(self):
        for operation in ("restore", "clear"):
            with self.subTest(operation=operation):
                client = _RecycleClient(7)
                snapshot = self.prepare(client, operation)
                client.items.append(GuangYaFile("new", "new.mkv", False, parent_id="source"))
                if operation == "clear":
                    with self.assertRaises(recycle.GuangYaRecycleStale):
                        self.apply(client, operation, snapshot)
                    self.assertEqual(client.writes, [])
                else:
                    result = self.apply(client, operation, snapshot)
                    self.assertEqual(result["count"], 1)
                    self.assertEqual([item.file_id for item in client.items], ["new"])

    def test_partial_listing_and_unknown_ids_are_rejected_before_write(self):
        client = _RecycleClient(7)
        with self.assertRaises(recycle.GuangYaRecycleStale):
            recycle.prepare_restore(cast(GuangYaClient, client), ["missing"])
        for operation in ("restore", "clear"):
            snapshot = self.prepare(client, operation)
            with mock.patch.object(client, "list_recycle", side_effect=RuntimeError("page 2 failed")):
                with self.assertRaises(RuntimeError):
                    self.apply(client, operation, snapshot)
            self.assertEqual(client.writes, [])

    def test_unknown_write_never_becomes_known_failure_or_exposes_provider_error(self):
        for operation, method in (("restore", "restore_from_recycle"), ("clear", "clear_recycle_bin")):
            with self.subTest(operation=operation):
                client = _RecycleClient(7)
                snapshot = self.prepare(client, operation)
                with mock.patch.object(client, method, side_effect=RuntimeError("private-provider-token")) as remote:
                    result = self.apply(client, operation, snapshot)
                remote.assert_called_once()
                self.assertEqual(result["status"], "outcome_unknown")
                self.assertFalse(result["ok"])
                self.assertFalse(result["verified"])
                self.assertNotIn("private-provider-token", str(result))


if __name__ == "__main__":
    unittest.main()
