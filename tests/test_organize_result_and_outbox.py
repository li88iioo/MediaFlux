"""结构化整理结果与 STRM 明细限流契约测试。

覆盖场景来自实施计划的验收条件：
- Task 6.1 版本化任务结果 Schema 与新旧兼容
- Task 6.3 STRM 明细刷屏上限
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch
from app.modules.organize_results import (
    ORGANIZE_RESULT_SCHEMA_VERSION,
    build_organize_result,
    observe_organize_task,
    organize_status,
    read_organize_result,
)
from app.modules.strm_notifications import build_strm_detail_messages


class OrganizeResultSchemaTests(unittest.TestCase):
    """Task 6.1：版本化结构化结果。"""

    def test_rejected_handoff_is_attention_without_reclassifying_moved_files(self):
        from app.modules.telegram_organize_lifecycle import build_organize_lifecycle_event
        from app.repositories.organize_operation_jobs import sanitize_organize_operation_result

        stats = {"total": 1, "moved": 1, "failed": 0,
                 "strm": {"ok": False, "error": "STRM 调度器正在停止", "secret": "private-value"}}
        persisted = sanitize_organize_operation_result({"stats": stats})
        self.assertEqual(persisted["stats"]["strm"], {"ok": False, "error": "STRM 调度器正在停止"})
        self.assertEqual(sanitize_organize_operation_result(persisted), persisted)
        self.assertEqual(persisted["stats"]["failed"], 0)
        event = build_organize_lifecycle_event(stats, source_name="test", strm_status="启动失败", media_refresh="未触发")
        self.assertEqual(event.state, "partial")
        self.assertTrue(str(event.title).startswith("⚠️"))

    def test_handoff_error_shared_with_notifications_redacts_credentials(self):
        from app.modules.organize_results import organize_strm_error

        error = organize_strm_error({"strm": {"ok": False, "error": "STRM 联动失败 token=private-audit-token"}})
        self.assertIn("STRM 联动失败", error)
        self.assertNotIn("private-audit-token", error)

    def test_result_exposes_counters_groups_and_strm_without_log_parsing(self):
        result = build_organize_result(
            {
                "total": 10, "moved": 8, "failed": 1,
                "group_results": [{"group_path": "作品 A", "status": "completed"}],
                "strm": {"ok": True},
                "strm_changes": [{"rel_dir": "剧集/A", "name": "E01.mkv"}],
                "media_refresh": {"Jellyfin": True},
                "group_progress": {"total": 3, "completed": 3},
            },
            status="completed",
            source_results=[{"id": "root", "name": "待整理"}],
            notification_sent=True,
        )

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["counters"]["moved"], 8)
        self.assertEqual(result["groups"][0]["group_path"], "作品 A")
        self.assertEqual(result["strm"], {"ok": True})
        self.assertEqual(result["media_refresh"], {"Jellyfin": True})
        self.assertTrue(result["notification"]["sent"])
        self.assertEqual(result["sources"][0]["id"], "root")

    def test_missing_fields_default_without_raising(self):
        result = build_organize_result({}, status="stopped")

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["counters"]["moved"], 0)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["strm"], {})

    def test_protected_download_directories_survive_result_serialization(self):
        blocked = [{
            "file_id": "stage", "source_dir_id": "source",
            "path": "NewNsfw/MF-101-FixtureRelease", "reason": "受保护下载目录未扫描",
        }]
        result = build_organize_result(
            {"scan_complete": False, "protected_staging_dirs": blocked}, status="partial",
        )

        self.assertEqual(result["protected_staging_dirs"], blocked)
        self.assertFalse(result["scan_complete"])
        self.assertEqual(read_organize_result(result)["protected_staging_dirs"], blocked)
        self.assertEqual(build_organize_result({}, status="completed")["protected_staging_dirs"], [])
        self.assertTrue(build_organize_result({}, status="completed")["scan_complete"])

    def test_status_explains_protected_staging_in_live_and_persisted_results(self):
        stats = {"total": 0, "matched": 0, "moved": 0, "scan_complete": False,
                 "protected_staging_dirs": [{
                     "file_id": "private-stage-id", "source_dir_id": "private-source-id",
                     "path": "/private/Anime/MF-557-release",
                     "reason": "受保护下载目录未扫描 token=private-provider-token",
                 }]}
        for persisted in (False, True):
            with self.subTest(persisted=persisted):
                manager = Mock()
                manager.status.return_value = {"status": "partial", "stats": stats}
                row = {"task_name": "guangya_organize", "status": "partial",
                       "result": json.dumps(build_organize_result(stats, status="partial")),
                       "trigger_type": "manual", "started_at": "", "finished_at": ""}
                with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), patch(
                    "app.database.get_task_run", return_value=row,
                ):
                    result = organize_status(557 if persisted else None)

                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "attention")
                self.assertIn("受保护下载目录", result["summary"])
                self.assertIn("仍可能被写入或等待核验", result["error"])
                self.assertNotIn("已移动目录", result["error"])
                self.assertIn("原整理日志查看详情并纠正后重新整理", str(result["suggestions"]))
                self.assertEqual(result["data"]["task"]["protected_staging_count"], 1)
                self.assertFalse(result["data"]["task"]["scan_complete"])
                for private in ("private-stage-id", "private-source-id", "/private/", "private-provider-token"):
                    self.assertNotIn(private, str(result))

    def test_protected_staging_cannot_claim_completion_and_keeps_continuation_guidance(self):
        from app.modules.organize_results import project_organize_status

        for resumed in ([], [557]):
            with self.subTest(resumed=bool(resumed)):
                result = project_organize_status({"status": "completed", "stats": {
                    "moved": 1, "strm_scope_unknown": 1, "scan_complete": False,
                    "protected_staging_dirs": [{"file_id": "stage"}], "resumed_download_ids": resumed,
                }}, overview={})

                self.assertFalse(result["ok"])
                self.assertEqual(result["data"]["task"]["status"], "partial")
                self.assertEqual(result["data"]["task"]["stats"]["moved"], 1)
                self.assertIn("同步范围未能确认", str(result["suggestions"]))
                if resumed:
                    self.assertIn("尚不代表入库完成", result["summary"])
                    self.assertIn("无需再次提交", str(result["suggestions"]))
                    self.assertNotIn("未自动接管", result["error"])
                else:
                    self.assertIn("原整理日志", str(result["suggestions"]))

    def test_reorganize_stop_and_undo_observe_the_named_task_without_following_latest(self):
        from app.modules.organize_tasks import OrganizeTaskManager

        for operation in ("reorganize", "stop", "undo"):
            with self.subTest(operation=operation):
                manager = OrganizeTaskManager()
                manager._task = {"id": "later-task", "status": "completed", "stats": {"moved": 99}}
                manager._task_history.append({
                    "id": "original-task", "status": "partial", "operation": operation,
                    "stats": {"moved": 1, "failed": 1},
                })
                with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), patch.object(
                    manager, "status", return_value={"status": "completed", "stats": {"moved": 99}},
                ), patch("app.modules.organize_tasks.get_organize_operation_job", return_value=None):
                    result = observe_organize_task({
                        "kind": "guangya_organize_task", "operation": operation, "task_id": "original-task",
                    })
                    missing = observe_organize_task({"operation": operation, "task_id": "missing-task"})

                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "attention")
                self.assertEqual(result["data"]["task"]["status"], "partial")
                self.assertEqual(result["data"]["task"]["stats"], {"moved": 1, "failed": 1})
                self.assertEqual(missing["status"], "unknown")

    def test_protected_download_prevents_a_success_notification(self):
        from app.modules.organize import Organizer
        from app.modules.telegram_organize_lifecycle import build_organize_lifecycle_event

        _groups, stats = Organizer._notification_projection({
            "total": 0, "scan_complete": False,
            "protected_staging_dirs": [{"file_id": "stage", "path": "NewNsfw/MF-fixture",
                                        "reason": "受保护下载目录未扫描"}],
        })
        event = build_organize_lifecycle_event(
            stats, source_name="NewNsfw", strm_status="未启用", media_refresh="未触发",
        )

        self.assertEqual(event.state, "partial")
        self.assertIn("部分完成", str(event.title))

    def test_protected_downloads_allow_only_confirmed_incremental_postprocessing(self):
        from unittest.mock import patch

        from app.modules.organize import Organizer, OrganizeRules

        for changes in ([], [{"rel_dir": "Normal", "name": "Normal.mkv"}]):
            with self.subTest(has_changes=bool(changes)):
                stats = {"moved": 1, "scan_complete": False, "strm_changes": changes,
                         "protected_staging_dirs": [{"file_id": "stage"}]}
                rules = OrganizeRules(link_strm=True, notify_enabled=False, library_notify=False)
                with patch.object(Organizer, "_post_organize_link") as link, patch.object(
                    Organizer, "_schedule_agent_recognition_reviews",
                ):
                    Organizer.trigger_post_actions(stats, rules, notify_result=False)

                if changes:
                    self.assertTrue(link.call_args.kwargs["force_incremental"])
                else:
                    link.assert_not_called()
                    assert isinstance(stats['strm'], dict)
                    self.assertTrue(stats["strm"]["skipped"])

    def test_legacy_stats_payload_is_still_readable(self):
        legacy = {"total": 4, "moved": 3, "status": "completed"}

        result = read_organize_result(legacy)

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["counters"]["moved"], 3)
        self.assertEqual(result["status"], "completed")

    def test_legacy_task_wrapper_preserves_identifiers_sources_and_counters(self):
        legacy = {
            "task_id": "task-legacy",
            "status": "partial",
            "current_source": "待整理/动漫",
            "error": "部分目录待确认",
            "notification_sent": True,
            "stats": {"total": 4, "moved": 3, "need_confirm": 1},
            "source_results": [{"id": "root", "status": "partial"}],
        }

        result = read_organize_result(legacy)

        self.assertEqual(result["schema_version"], ORGANIZE_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["task_id"], "task-legacy")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["current_source"], "待整理/动漫")
        self.assertEqual(result["error"], "部分目录待确认")
        self.assertEqual(result["counters"]["moved"], 3)
        self.assertEqual(result["counters"]["need_confirm"], 1)
        self.assertEqual(result["sources"], [{"id": "root", "status": "partial"}])
        self.assertTrue(result["notification"]["sent"])

    def test_unknown_future_version_is_read_best_effort(self):
        payload = {
            "schema_version": 99,
            "status": "completed",
            "counters": {"moved": 5},
            "brand_new_field": {"a": 1},
        }

        result = read_organize_result(payload)

        self.assertEqual(result["schema_version"], 99)
        self.assertEqual(result["counters"]["moved"], 5)
        self.assertEqual(result["counters"]["failed"], 0)
        self.assertEqual(result["brand_new_field"], {"a": 1})

    def test_non_dict_payload_degrades_to_an_empty_result(self):
        for payload in (None, "text", 5, []):
            with self.subTest(payload=payload):
                result = read_organize_result(payload)

                self.assertEqual(result["counters"]["moved"], 0)

    def test_changed_target_dirs_are_derived_from_strm_changes(self):
        # 任务级 stats 不单独维护 changed_target_dirs，必须能从变化清单推导。
        result = build_organize_result(
            {
                "strm_changes": [
                    {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
                    {"rel_dir": "剧集/A/Season 01", "name": "E02.mkv"},
                    {"rel_dir": "电影/B", "name": "B.mkv"},
                ],
            },
            status="completed",
        )

        self.assertEqual(
            result["changed_target_dirs"], ["剧集/A/Season 01", "电影/B"]
        )

    def test_explicit_changed_target_dirs_take_precedence(self):
        result = build_organize_result(
            {
                "changed_target_dirs": ["显式目录"],
                "strm_changes": [{"rel_dir": "别的", "name": "x.mkv"}],
            },
            status="completed",
        )

        self.assertEqual(result["changed_target_dirs"], ["显式目录"])

    def test_malformed_counters_never_raise(self):
        result = build_organize_result(
            {"moved": "not-a-number", "failed": None}, status="partial",
        )

        self.assertEqual(result["counters"]["moved"], 0)
        self.assertEqual(result["counters"]["failed"], 0)


class StrmDetailFloodProtectionTests(unittest.TestCase):
    """Task 6.3：STRM 明细刷屏上限。"""

    @staticmethod
    def _changes(count: int) -> list[dict]:
        return [
            {"action": "generated", "directory": f"剧集/作品 {index // 20}", "filename": f"E{index:04d}.strm"}
            for index in range(count)
        ]

    def test_details_are_paged_at_twenty_files(self):
        messages = build_strm_detail_messages(self._changes(21))

        self.assertEqual(len(messages), 2)

    def test_exactly_twenty_files_stay_in_one_message(self):
        messages = build_strm_detail_messages(self._changes(20))

        self.assertEqual(len(messages), 1)

    def test_large_sync_falls_back_to_a_single_summary(self):
        messages = build_strm_detail_messages(self._changes(200), max_messages=3)

        self.assertEqual(len(messages), 1)
        self.assertIn("只发送摘要", messages[0])
        self.assertIn("200", messages[0])

    def test_summary_fallback_reports_previously_omitted_changes(self):
        messages = build_strm_detail_messages(
            self._changes(200), max_messages=3, omitted_count=45,
        )

        self.assertIn("45", messages[0])

    def test_limit_disabled_keeps_full_detail_pages(self):
        messages = build_strm_detail_messages(self._changes(200), max_messages=0)

        self.assertGreater(len(messages), 3)

    def test_no_changes_produces_no_message(self):
        self.assertEqual(build_strm_detail_messages([], max_messages=3), [])


if __name__ == "__main__":
    unittest.main()
