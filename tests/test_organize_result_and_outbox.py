"""结构化整理结果与 STRM 明细限流契约测试。

覆盖场景来自实施计划的验收条件：
- Task 6.1 版本化任务结果 Schema 与新旧兼容
- Task 6.3 STRM 明细刷屏上限
"""
from __future__ import annotations

import unittest
from app.modules.organize_results import (
    ORGANIZE_RESULT_SCHEMA_VERSION,
    build_organize_result,
    read_organize_result,
)
from app.modules.strm_notifications import build_strm_detail_messages


class OrganizeResultSchemaTests(unittest.TestCase):
    """Task 6.1：版本化结构化结果。"""

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
