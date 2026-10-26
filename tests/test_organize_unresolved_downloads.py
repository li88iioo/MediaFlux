"""识别失败的跳过不能成为永远占用缺集的假完成。"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.modules.media_subscriptions import MediaSubscriptionService, _ExpectedMedia
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_execution import execute_organize_plans
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.scraper import MatchResult
from tests.support import isolated_test_database
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class UnresolvedDownloadCompletionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("unexpected network"),
            )
        )
        self.subscription = db.add_media_subscription(
            provider="tmdb",
            external_id="991",
            tmdb_id="991",
            media_type="tv",
            title="Test series",
            monitor_mode="missing",
            action="confirm",
            download_target="guangya",
            check_interval_minutes=10080,
        )

    def seed(self, *, episode=1, root="incoming", isolated=False):
        key = f"tmdb:991:tv:S01E{episode:03d}"
        candidate = db.replace_media_subscription_candidates(
            self.subscription,
            key,
            season=1,
            episode=episode,
            candidates=[
                {"result_id": f"fixture-resource-{episode}", "title": "Test series"}
            ],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission = db.claim_media_download_admission(
            media_key=key,
            tmdb_id="991",
            media_type="tv",
            subscription_id=self.subscription,
            candidate_id=candidate,
            season=1,
            episode=episode,
            subscription_revision=1,
        )
        assert admission is not None
        self.assertTrue(
            db.begin_media_download_dispatch(
                admission, subscription_id=self.subscription, subscription_revision=1
            )
        )
        request, _ = db.create_download_request(key, "magnet", admission_id=admission)
        db.update_download_request(
            request,
            status="completed",
            gy_status="completed",
            gy_target_dir=root,
            organize_started=1,
            organize_status="running",
            organize_task_id="test-task",
            strm_status="pending",
            notification_delivery_status="sent",
            gy_isolated=int(isolated),
            gy_staging_parent_dir="source" if isolated else "",
            gy_staging_name="MF-case" if isolated else "",
            targets="guangya",
        )
        db.sync_media_download_admission_for_request(request)
        return request, admission, key

    @staticmethod
    def plans(*, match=None, root="incoming", count=1, probe_pending=False):
        return [
            OrganizePlan(
                file_id=f"{root}-{i}",
                original_name=f"Test.S01E{i + 1:02d}.mkv",
                original_path=root,
                original_parent_id=root,
                action="skip",
                match=match or MatchResult(error="未匹配到元数据"),
                note="未匹配到元数据" if match is None else "测试跳过",
                media_probe_pending=probe_pending,
            )
            for i in range(count)
        ]

    def execute(self, sources, requests, *, cloud=None, isolated=False):
        cloud = cloud or Mock()
        organizer = Organizer(client=cloud, scraper=Mock())
        rules = OrganizeRules(
            target_dir_id="archive",
            link_strm=False,
            notify_enabled=False,
            clean_empty=isolated,
        )
        # 真实日志执行与任务汇总；只替换外部扫描/识别及通知。
        for root, (plans, stats) in sources.items():
            with patch.object(organizer, "_parse_media_fields", return_value={}):
                execute_organize_plans(
                    organizer, plans, rules, stats, {}, source_dir_id=root
                )
        manager = OrganizeTaskManager()
        run_id = db.add_task_run("guangya_organize", "download", result=json.dumps({"task_id": "test-task"}))
        for request in requests:
            db.update_download_request(request, organize_task_id="test-task", organize_run_id=run_id)
        manager._lock.acquire()
        manager._task = {"id": "test-task", "status": "running", "stats": {}}
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "app.modules.organize_tasks.Organizer",
                    return_value=organizer,
                    wraps=Organizer,
                )
            )
            stack.enter_context(
                patch.object(
                    organizer,
                    "organize",
                    side_effect=lambda source, *_a, **_k: sources[source],
                )
            )
            stack.enter_context(
                patch.object(organizer, "_validate_target_outside_source")
            )
            stack.enter_context(patch.object(manager, "_wake_download_tracker"))
            stack.enter_context(
                patch(
                    "app.modules.organize_tasks._publish_download_lifecycles",
                    return_value=False,
                )
            )
            stack.enter_context(
                patch(
                    "app.modules.organize_tasks.resolve_organize_workers",
                    return_value=1,
                )
            )
            stack.enter_context(
                patch.object(Organizer, "_schedule_agent_recognition_reviews")
            )
            manager._run(
                "test-task",
                run_id,
                [{"id": root, "name": root} for root in sources],
                rules,
                download_request_ids=requests,
                trigger_type="download",
            )
        return manager

    @staticmethod
    def stats(**values):
        result = Organizer._initial_stats()
        result.update(values)
        return result

    def admission_status(self, admission):
        with db.get_conn() as conn:
            return conn.execute(
                "SELECT status FROM media_download_admissions WHERE id=?", (admission,)
            ).fetchone()["status"]

    def assert_missing(self, key):
        service = MediaSubscriptionService()
        search = AsyncMock(return_value=(0, 0, None))
        with (
            patch(
                "app.modules.media_subscriptions._tmdb_detail",
                return_value={"id": 991, "name": "Test series"},
            ),
            patch.object(
                service,
                "_expected_tv",
                new=AsyncMock(
                    return_value=([_ExpectedMedia(key, 1, 1, "2020-01-01")], 0, 0)
                ),
            ),
            patch(
                "app.modules.media_subscriptions.inspect_series_episode_sources",
                return_value=[
                    {
                        "server_type": "jellyfin",
                        "server_name": "fixture",
                        "status": "ready",
                        "episodes": [],
                        "truncated": False,
                    }
                ],
            ),
            patch(
                "app.modules.media_subscription_notifications.drain_media_subscription_notifications"
            ),
            patch.object(service, "_search_missing_tv", new=search),
        ):
            result = asyncio.run(service.check_subscription(self.subscription))[
                "result"
            ]
        self.assertEqual(result["status"], "missing", result)
        self.assertEqual(
            (
                result["expected_count"],
                result["local_count"],
                result["missing_count"],
                result["inflight_count"],
            ),
            (1, 0, 1, 0),
        )
        search.assert_awaited_once()

    def unresolved_scenario(self, isolated):
        request, admission, key = self.seed(isolated=isolated)
        plans = self.plans(count=4)
        cloud = None
        if isolated:
            cloud = _CloudTree()
            cloud.nodes["incoming"] = cloud._dir("incoming", "source", "MF-case")
            cloud.children["incoming"] = []
            cloud.children["source"] = ["incoming"]
            for plan in plans:
                cloud.add_file(plan.file_id, "incoming")
        manager = self.execute(
            {"incoming": (plans, self.stats(total=4, skipped=4))},
            [request],
            cloud=cloud,
            isolated=isolated,
        )
        row = db.get_download_request(request)
        assert row is not None
        self.assertEqual(row["status"], "completed", "下载本身成功，不能改成下载失败")
        self.assertEqual(row["organize_status"], "failed")
        self.assertEqual(
            row["organize_started"], -1, "禁止tracker直接重复执行有副作用的整理"
        )
        self.assertIn("未匹配", row["organize_error"])
        self.assertEqual(manager._task["status"], "partial")
        self.assertIn(
            request, [r["id"] for r in db.list_download_requests_requiring_attention()]
        )
        self.assertEqual(self.admission_status(admission), "failed")
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM organize_log WHERE status='skipped'"
                ).fetchone()[0],
                4,
            )
        if isolated:
            assert cloud is not None
            self.assertEqual(cloud.deleted, [])
        db.init_db()
        db.reconcile_startup_media_download_admissions()
        self.assert_missing(key)

    def test_all_unmatched_skipped_remain_missing_after_restart(self):
        self.unresolved_scenario(False)

    def test_isolated_unmatched_skips_retain_cloud_files_and_release_false_inflight(
        self,
    ):
        self.unresolved_scenario(True)

    def test_known_duplicate_skip_is_not_a_recognition_failure(self):
        request, admission, key = self.seed()
        plans = self.plans(
            match=MatchResult(tmdb_id="991", title="Test series", media_type="tv")
        )
        self.execute({"incoming": (plans, self.stats(total=1, skipped=1))}, [request])
        get_download_request_result = db.get_download_request(request)
        assert get_download_request_result is not None
        self.assertEqual(
            get_download_request_result["organize_status"], "completed"
        )
        db.reconcile_media_download_admissions(
            self.subscription, {key}, expected_revision=1
        )
        self.assertEqual(self.admission_status(admission), "completed")

    def test_empty_source_without_recognition_failure_keeps_completion(self):
        request, _, _ = self.seed()
        self.execute({"incoming": ([], self.stats())}, [request])
        get_download_request_result_2 = db.get_download_request(request)
        assert get_download_request_result_2 is not None
        self.assertEqual(
            get_download_request_result_2["organize_status"], "completed"
        )

    def test_pending_confirmation_keeps_manual_state(self):
        request, admission, _ = self.seed()
        plans = self.plans(match=MatchResult(error="待用户确认", need_confirm=True))
        self.execute(
            {"incoming": (plans, self.stats(total=1, need_confirm=1))}, [request]
        )
        get_download_request_result_3 = db.get_download_request(request)
        assert get_download_request_result_3 is not None
        self.assertEqual(
            get_download_request_result_3["organize_status"], "requires_manual"
        )
        self.assertEqual(self.admission_status(admission), "processing")

    def test_pending_probe_is_not_misclassified_as_terminal_failure(self):
        request, _, _ = self.seed()
        plans = self.plans(probe_pending=True)
        self.execute({"incoming": (plans, self.stats(total=1, skipped=1))}, [request])
        get_download_request_result_4 = db.get_download_request(request)
        assert get_download_request_result_4 is not None
        self.assertEqual(
            get_download_request_result_4["organize_status"], "completed"
        )

    def test_mixed_sources_do_not_fail_the_healthy_request(self):
        bad, bad_admission, _ = self.seed(root="unmatched")
        good, good_admission, good_key = self.seed(root="known", episode=2)
        self.execute(
            {
                "unmatched": (
                    self.plans(root="unmatched"),
                    self.stats(total=1, skipped=1),
                ),
                "known": (
                    self.plans(
                        root="known",
                        match=MatchResult(tmdb_id="991", title="Test series"),
                    ),
                    self.stats(total=1, skipped=1),
                ),
            },
            [bad, good],
        )
        get_download_request_result_5 = db.get_download_request(bad)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["organize_status"], "failed")
        get_download_request_result_6 = db.get_download_request(good)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6["organize_status"], "completed")
        self.assertEqual(self.admission_status(bad_admission), "failed")
        db.reconcile_media_download_admissions(
            self.subscription, {good_key}, expected_revision=1
        )
        self.assertEqual(self.admission_status(good_admission), "completed")
