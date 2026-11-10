"""同一光鸭快照的单项和分批任务必须采用一致的 ID 首项归并规则。"""

from __future__ import annotations

from tests.support import assert_present

import unittest
from unittest.mock import patch

from app.repositories import download_requests as download_requests_repo
from app.modules.download_tracker import DownloadTracker, _GYTaskIndex
from tests.support import isolated_test_database


class DownloadTrackerSnapshotConsistencyTests(unittest.TestCase):
    def test_single_and_batch_read_the_same_task_when_snapshot_repeats_id(self):
        # 正常 client 已按 ID 保留首项；tracker 的直接快照入口也不应另用末项策略。
        snapshot = [
            {
                "id": "same-task",
                "name": "sample",
                "status": "downloading",
                "progress": 0.3,
            },
            {"id": "same-task", "name": "sample", "status": "failed", "progress": 0.0},
        ]
        with isolated_test_database():
            ids = []
            for title, task_ids in (("single", "[]"), ("batch", '["same-task"]')):
                request_id, _ = download_requests_repo.create_download_request(title, "magnet", title=title)
                download_requests_repo.update_download_request(
                    request_id,
                    targets="guangya",
                    status="submitted",
                    gy_status="submitted",
                    gy_task_id="same-task",
                    gy_task_ids=task_ids,
                    gy_batch_count=1,
                )
                ids.append(request_id)
            index = _GYTaskIndex(snapshot)
            tracker = DownloadTracker()
            with (
                patch.object(tracker, "_notify_completion"),
                patch.object(tracker, "_start_local_import"),
                patch.object(tracker, "_publish_lifecycle"),
            ):
                for request_id in ids:
                    tracker._update_request(
                        download_requests_repo.get_download_request(request_id), [], index
                    )
            self.assertEqual(
                [assert_present(download_requests_repo.get_download_request(i))["gy_status"] for i in ids],
                ["downloading", "downloading"],
            )
