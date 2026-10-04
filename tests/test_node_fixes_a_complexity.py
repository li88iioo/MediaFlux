"""A 域规模回归：计数实际访问次数，不以计时替代复杂度证明。"""
from __future__ import annotations

import json
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.indexers.errors import IndexerResultExpired, IndexerResultNotFound
from app.indexers.models import IndexerItem
from app.indexers.result_store import IndexerResultStore
from app.modules.download_tracker import DownloadTracker


class CountedEntries(OrderedDict):
    visited = 0
    def items(self):
        for item in super().items():
            self.visited += 1
            yield item


class CountedTasks(list):
    visited = 0
    def __iter__(self):
        for task in super().__iter__():
            self.visited += 1
            yield task


class ResultStoreScaleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.store = IndexerResultStore(clock=lambda: self.now)
        self.item = IndexerItem(site_id="memory", site_name="Memory", title="Count")

    def test_full_store_does_not_scan_all_entries_on_each_put(self):
        for _ in range(10_000):
            self.store.put(self.item)
        self.store._entries = CountedEntries(self.store._entries)
        for _ in range(100):
            self.store.put(self.item)
        self.assertLessEqual(self.store._entries.visited, 10_000)
        self.assertEqual(len(self.store._entries), 10_000)

    def test_restore_renews_expiry_without_stale_heap_deleting_new_entry(self):
        token = self.store.put(self.item)
        self.now += timedelta(seconds=500)
        self.store.restore(token, self.item)
        self.now += timedelta(seconds=101)
        self.store.put(self.item)
        self.assertEqual(self.store.get(token).title, "Count")
        self.now += timedelta(seconds=500)
        with self.assertRaises(IndexerResultExpired):
            self.store.get(token)

    def test_clock_rollback_does_not_hide_expired_entries_behind_newer_expiry(self):
        first = self.store.put(self.item)
        self.now -= timedelta(seconds=500)
        second = self.store.put(self.item)
        self.now += timedelta(seconds=601)
        self.store.put(self.item)
        self.assertNotIn(second, self.store._entries)
        self.assertEqual(self.store.get(first).title, "Count")
        with self.assertRaises(IndexerResultExpired):
            self.store.get(second)

    def test_capacity_and_restore_keep_expiry_metadata_bounded(self):
        self.store = IndexerResultStore(max_entries=5, clock=lambda: self.now)
        token = self.store.put(self.item)
        for _ in range(1000):
            self.store.restore(token, self.item)
        self.assertLessEqual(len(self.store._expiry_heap), 10)
        for _ in range(100):
            self.now += timedelta(seconds=601)
            self.store.put(self.item)
        self.assertLessEqual(len(self.store._expired_ids), 5)
        self.assertLessEqual(len(self.store._expiry_heap), 10)

    def test_concurrent_restore_get_and_put_preserve_copies_and_capacity(self):
        self.store = IndexerResultStore(max_entries=1000, clock=lambda: self.now)
        def worker(index):
            token = self.store.put(self.item)
            for _ in range(10):
                self.store.restore(token, self.item)
                result = self.store.get(token)
                result.title = "mutation"
            return token
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(worker, range(100)))
        self.assertEqual(len(set(tokens)), 100)
        self.assertEqual({self.store.get(token).title for token in tokens}, {"Count"})
        self.assertLessEqual(len(self.store._entries), 1000)

    def test_eviction_is_not_reported_as_expiry_and_restore_moves_fifo_position(self):
        self.store = IndexerResultStore(max_entries=2, clock=lambda: self.now)
        first = self.store.put(self.item)
        second = self.store.put(self.item)
        self.store.restore(first, self.item)
        self.store.put(self.item)
        with self.assertRaises(IndexerResultNotFound):
            self.store.get(second)
        self.assertEqual(self.store.get(first).title, "Count")


class TrackerScaleTests(unittest.TestCase):
    def setUp(self):
        self.tracker = DownloadTracker()
        self.enterContext(patch.object(self.tracker, "_run_torrent_data_cleanup_if_due"))
        self.enterContext(patch("app.modules.download_tracker.db.recover_stale_submitting_download_requests", return_value=0))
        self.enterContext(patch("app.modules.download_tracker.db.list_local_media_sources", return_value=[]))
        self.enterContext(patch("app.modules.download_tracker.db.kv_get", return_value="0"))
        self.enterContext(patch("app.modules.download_tracker.db.kv_set"))
        self.apply = self.enterContext(patch("app.modules.download_tracker.apply_download_tracker_update", return_value=None))

    def test_one_index_per_round_not_per_request_and_no_cross_round_reuse(self):
        rows = [{"id": i + 1, "status": "submitted", "qb_status": "submitted", "qb_task_id": str(i),
                 "gy_status": "submitted", "gy_task_ids": json.dumps([str(i)]), "gy_batch_count": 1} for i in range(100)]
        qb_tasks = CountedTasks(SimpleNamespace(hash=str(i), name=str(i), progress=0.1, state="downloading") for i in range(1000))
        gy_tasks = CountedTasks({"id": str(i), "status": "running", "progress": 0.1} for i in range(1000))
        self.enterContext(patch("app.modules.download_tracker.db.list_active_download_requests", return_value=rows))
        qb_fetch = self.enterContext(patch.object(self.tracker, "_qb_tasks", return_value=(True, qb_tasks)))
        gy_fetch = self.enterContext(patch.object(self.tracker, "_gy_tasks", return_value=(True, gy_tasks)))
        self.assertEqual(self.tracker.run_once(), 100)
        self.assertLessEqual(qb_tasks.visited, 1000)
        self.assertLessEqual(gy_tasks.visited, 1000)
        self.assertEqual(self.apply.call_count, 100)
        qb_fetch.assert_called_once()
        gy_fetch.assert_called_once()
        self.apply.reset_mock()
        qb_fetch.return_value = (True, [])
        gy_fetch.return_value = (True, [])
        self.tracker.run_once()
        for call in self.apply.call_args_list:
            self.assertIn("qb_task_missing_since", call.kwargs)
            self.assertIn("gy_task_missing_since", call.kwargs)

    def test_matching_priority_and_ambiguous_directory_contracts(self):
        source = {"id": "source", "name": "different", "target_dir": "a", "raw": {"url": "magnet:x"}}
        title = {"id": "title", "name": "TITLE", "target_dir": "b"}
        tasks = [source, title]
        base = {"source_value": "magnet:x", "title": "title", "gy_target_dir": "b"}
        self.assertIs(DownloadTracker._match_gy(base, tasks), source)
        self.assertIs(DownloadTracker._match_gy({**base, "gy_isolated": 1}, tasks), title)
        self.assertIsNone(DownloadTracker._match_gy({**base, "gy_isolated": 1}, tasks + [dict(title)]))
        self.assertIsNone(DownloadTracker._match_gy({**base, "gy_task_id": "missing"}, tasks))
        self.assertIsNone(DownloadTracker._match_gy({"title": "title"}, tasks))
        qb = SimpleNamespace(hash="ABC", name=" Title ")
        self.assertIs(DownloadTracker._match_qb({"qb_task_id": "abc"}, [qb]), qb)
        self.assertIsNone(DownloadTracker._match_qb({"kind": "http", "title": "title"}, [qb]))
        self.assertIsNone(DownloadTracker._match_qb({"kind": "magnet", "title": "title"}, [qb]))
