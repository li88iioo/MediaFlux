"""STRM 仅调用带预算与取消语义的正式分页协议，不回退到旧快照接口。"""
from __future__ import annotations

from typing import cast

import tempfile
import threading
import unittest
from unittest.mock import patch

from app.clients.guangya import GuangYaClient
from app.modules.strm import sync_strm
from tests.support import isolated_test_database


class StrmSingleTrackScanTests(unittest.TestCase):
    def test_snapshot_only_client_cannot_bypass_paging_budget(self) -> None:
        class SnapshotOnlyClient:
            calls = 0

            def list_dir(self, directory):
                self.calls += 1
                return []

        client = SnapshotOnlyClient()
        with (
            tempfile.TemporaryDirectory() as root,
            patch("app.modules.strm.db.list_strm_index", return_value=[]),
            patch("app.modules.strm.clean_invalid_strm") as cleanup,
        ):
            stats = sync_strm("root", "http://play.invalid", root, client=cast(GuangYaClient, client), clean_invalid=True)
        self.assertTrue(stats["scan_incomplete"])
        self.assertTrue(stats["clean_skipped"])
        self.assertEqual(client.calls, 0)
        cleanup.assert_not_called()

    def test_formal_client_receives_budget_without_signature_introspection(self) -> None:
        class PagedClient:
            def __init__(self):
                self.calls = []

            def iter_dir(self, directory, *, should_stop, max_items):
                self.calls.append((directory, should_stop, max_items))
                return iter(())

        client = PagedClient()
        with (
            isolated_test_database(),
            tempfile.TemporaryDirectory() as root,
            patch("app.modules.strm.db.list_strm_index", return_value=[]),
            patch("app.modules.strm._scan_limits", return_value=(100, 17, 100, 60)),
            patch("inspect.signature", side_effect=AssertionError("不得探测旧协议")),
        ):
            stats = sync_strm("root", "http://play.invalid", root, client=cast(GuangYaClient, client), clean_invalid=False)
        self.assertFalse(stats["scan_incomplete"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][2], 17)
        self.assertTrue(callable(client.calls[0][1]))

    def test_real_paging_budget_reports_entries_not_remote_failure(self) -> None:
        client = object.__new__(GuangYaClient)
        client._read_metrics_lock = threading.Lock()
        client._read_metrics = None
        files = [{"fileId": str(index), "fileName": f"{index}.mkv"} for index in range(2)]
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(client, "_call_read", return_value=files),
            patch("app.modules.strm.db.list_strm_index", return_value=[]),
            patch("app.modules.strm._scan_limits", return_value=(100, 1, 100, 60)),
            patch("app.modules.strm.clean_invalid_strm") as cleanup,
        ):
            stats = sync_strm("root", "http://play.invalid", root, client=client, clean_invalid=True)
        self.assertTrue(stats["scan_incomplete"])
        self.assertEqual(stats["scan_limit_reason"], "entries")
        self.assertEqual(stats["generated"], 0)
        self.assertTrue(stats["clean_skipped"])
        cleanup.assert_not_called()
