"""发布链 A：光鸭分页完整性、预算与 Tracker 缺失宽限期回归。"""
from __future__ import annotations

import tests  # noqa: F401 - 必须先隔离运行目录与数据库。

import json
import unittest
from unittest.mock import Mock, PropertyMock, patch

from app import database as db
from app.clients.guangya import GuangYaClient, IncompleteOfflineTaskListError
from app.modules.download_dispatcher import create_request, normalize_download_url
from app.modules.download_tracker import DownloadTracker
from tests.support import isolated_test_database


def page(start=0, count=50):
    return [{"taskId": f"gy-{i}", "name": f"task-{i}", "status": 0, "progress": 50}
            for i in range(start, start + count)]


class GuangYaPagingCompletenessTests(unittest.TestCase):
    def _client(self, pages):
        raw = Mock()
        raw.cloud_task_list.side_effect = pages
        client = object.__new__(GuangYaClient)
        client._call_read = Mock(side_effect=lambda _name, callback: callback())
        self.enterContext(patch.object(GuangYaClient, "raw", new_callable=PropertyMock, return_value=raw))
        return client, raw

    def test_short_tail_and_empty_tail_are_complete(self):
        for pages, count, calls in (([page(count=1)], 1, 1), ([page(), page(50, 1)], 51, 2),
                                    ([page(), []], 50, 2), ([[]], 0, 1)):
            with self.subTest(count=count, calls=calls):
                client, raw = self._client(pages)
                tasks = client.list_offline_tasks()
                self.assertEqual(len(tasks), count)
                self.assertEqual(raw.cloud_task_list.call_count, calls)
                for index, call in enumerate(raw.cloud_task_list.call_args_list):
                    self.assertEqual(call.kwargs, {"page": index, "page_size": 50, "status": [0, 1, 2, 3, 4, 5]})

    def test_repeated_full_page_and_no_new_ids_are_explicitly_incomplete(self):
        first = page()
        for repeated in (first, list(reversed(first))):
            with self.subTest(reversed=repeated != first):
                client, raw = self._client([first, repeated])
                with self.assertRaisesRegex(IncompleteOfflineTaskListError, "未推进"):
                    client.list_offline_tasks()
                self.assertEqual(raw.cloud_task_list.call_count, 2)

    def test_repeated_anonymous_full_page_is_incomplete(self):
        anonymous = [{"name": f"task-{i}", "status": 0} for i in range(50)]
        client, raw = self._client([anonymous, anonymous])
        with self.assertRaises(IncompleteOfflineTaskListError):
            client.list_offline_tasks()
        self.assertEqual(raw.cloud_task_list.call_count, 2)

    def test_full_page_budget_is_incomplete_without_201st_query(self):
        def pages(**kwargs):
            return page(kwargs["page"] * 50)
        client, raw = self._client(pages)
        with self.assertRaisesRegex(IncompleteOfflineTaskListError, "上限"):
            client.list_offline_tasks()
        self.assertEqual(raw.cloud_task_list.call_count, 200)
        self.assertEqual(raw.cloud_task_list.call_args.kwargs["page"], 199)

    def test_short_final_budget_page_proves_completion(self):
        def pages(**kwargs):
            number = kwargs["page"]
            return page(number * 50, 49 if number == 199 else 50)
        client, raw = self._client(pages)
        self.assertEqual(len(client.list_offline_tasks()), 9999)
        self.assertEqual(raw.cloud_task_list.call_count, 200)

    def test_overlapping_pages_with_new_ids_and_short_tail_complete_normally(self):
        client, raw = self._client([page(), page(25), page(75, 2)])
        tasks = client.list_offline_tasks()
        self.assertEqual(len(tasks), 77)
        self.assertEqual(len({task["id"] for task in tasks}), 77)
        self.assertEqual(raw.cloud_task_list.call_count, 3)


class GuangYaPagingTrackerBoundaryTests(unittest.TestCase):
    _client = GuangYaPagingCompletenessTests._client

    def setUp(self):
        self.enterContext(isolated_test_database())
        self.tracker = DownloadTracker()
        self.enterContext(patch.object(self.tracker, "_notify_completion"))
        self.organize = self.enterContext(patch.object(self.tracker, "_start_organize"))
        self.enterContext(patch.object(self.tracker, "_start_local_import"))
        self.enterContext(patch.object(self.tracker, "_staging_ready_for_organize", return_value=True))

    @staticmethod
    def _request(missing_since=None):
        request_id = create_request(normalize_download_url("magnet:?xt=urn:btih:" + "a" * 40), "100", "1")["id"]
        db.update_download_request(request_id, status="submitted", targets="guangya",
            gy_status="submitted", gy_task_id="gy-10000", gy_task_ids=json.dumps(["gy-10000"]),
            gy_batch_count=1, gy_task_missing_since=missing_since)
        return request_id

    def _read_and_update(self, request_id, pages):
        client, raw = self._client(pages)
        with patch("app.modules.download_tracker.GuangYaClient", return_value=client), \
             patch.object(GuangYaClient, "logged_in", new_callable=PropertyMock, return_value=True), \
             patch("app.modules.download_tracker.close_guangya_client"):
            available, tasks = DownloadTracker._gy_tasks()
        self.tracker._update_request(db.get_download_request(request_id), [], tasks,
                                     qb_available=False, gy_available=available)
        return available, raw, db.get_download_request(request_id)

    def test_incomplete_snapshot_neither_starts_nor_expires_missing_grace(self):
        for reason in ("budget", "repeat"):
            for missing in (None, "2000-01-01 00:00:00"):
                with self.subTest(reason=reason, missing=missing):
                    request_id = self._request(missing)
                    def pages(**kwargs):
                        # 真库存第 201 页含目标；预算前不得误断言目标不存在。
                        number = kwargs["page"] if reason == "budget" else 0
                        return page(number * 50)
                    available, raw, row = self._read_and_update(request_id, pages)
                    self.assertFalse(available)
                    self.assertEqual(raw.cloud_task_list.call_count, 200 if reason == "budget" else 2)
                    self.assertEqual(row["gy_status"], "submitted")
                    self.assertEqual(row["status"], "submitted")
                    self.assertEqual(row["gy_task_missing_since"], missing)
                    self.assertFalse(row["notification_event_status"])
                    self.organize.assert_not_called()

    def test_complete_empty_snapshot_still_uses_real_missing_grace(self):
        request_id = self._request()
        available, _, row = self._read_and_update(request_id, [[]])
        self.assertTrue(available)
        self.assertTrue(row["gy_task_missing_since"])
        self.assertEqual(row["gy_status"], "submitted")
        db.update_download_request(request_id, gy_task_missing_since="2000-01-01 00:00:00")
        available, _, row = self._read_and_update(request_id, [[]])
        self.assertTrue(available)
        self.assertEqual(row["gy_status"], "manual_review")
        self.assertEqual(row["status"], "manual_review")
        self.organize.assert_not_called()

    def test_complete_matching_snapshot_recovers_grace_and_hands_off_completion(self):
        request_id = self._request("2000-01-01 00:00:00")
        available, _, row = self._read_and_update(request_id, [[
            {"taskId": "gy-10000", "name": "finished", "status": 2, "progress": 100},
        ]])
        self.assertTrue(available)
        self.assertEqual(row["gy_status"], "completed")
        self.assertIsNone(row["gy_task_missing_since"])
        self.organize.assert_called_once()
