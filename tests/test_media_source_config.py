"""整理/STRM 多源配置契约与前端多选入口测试。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.modules.organize_sources import (
    normalize_organize_source_ids,
    normalize_organize_sources,
)
from app.modules.organize_scheduler import OrganizeScheduler
from app.modules.strm import parse_strm_sources


class OrganizeSourceParserTests(unittest.TestCase):
    def test_accepts_dict_and_string_items_and_deduplicates(self):
        sources, error = normalize_organize_sources(
            '[{"id":"11","name":"源一"},"22",{"id":"11","name":"重复"}]'
        )
        self.assertEqual(error, "")
        self.assertEqual(sources, [
            {"id": "11", "name": "源一"},
            {"id": "22", "name": "源目录2"},
        ])
    def test_root_directory_is_rejected(self):
        sources, error = normalize_organize_sources('[{"id":"0"}]')
        self.assertEqual(sources, [])
        self.assertIn("不能为根目录", error)

    def test_source_scope_is_canonical_deduplicated_and_limited_to_configured_sources(self):
        configured = [
            {"id": "11", "name": "普通源"},
            {"id": "22", "name": "成人源"},
        ]
        source_ids, error = normalize_organize_source_ids(
            '["22", {"id":"22"}, "11"]',
            configured_sources=configured,
        )
        self.assertEqual(error, "")
        self.assertEqual(source_ids, ["22", "11"])

        source_ids, error = normalize_organize_source_ids(
            '["33"]', configured_sources=configured,
        )
        self.assertEqual(source_ids, [])
        self.assertIn("未配置的整理源目录", error)

    def test_source_count_and_field_lengths_are_bounded(self):
        sources, error = normalize_organize_sources(
            [{"id": str(index), "name": "Source"} for index in range(1, 66)]
        )
        self.assertEqual(sources, [])
        self.assertIn("最多允许 64 个来源", error)

        sources, error = normalize_organize_sources(
            [{"id": "x" * 1025, "name": "Source"}]
        )
        self.assertEqual(sources, [])
        self.assertIn("ID 过长", error)

    def test_strm_empty_canonical_list_is_valid_for_settings_save(self):
        sources, error = parse_strm_sources("[]", require_nonempty=False)
        self.assertEqual((sources, error), ([], ""))

    def test_scheduler_uses_same_string_item_and_empty_list_semantics(self):
        values = {
            "GY_ORGANIZE_SOURCE_DIRS": '["11",{"id":"22","name":"源二"}]',
        }
        scheduler = OrganizeScheduler(
            manager=MagicMock(),
            get_value=lambda key, default="": values.get(key, default),
        )
        self.assertEqual(scheduler._configured_sources(), [
            {"id": "11", "name": "源目录1"},
            {"id": "22", "name": "源二"},
        ])
        values["GY_ORGANIZE_SOURCE_DIRS"] = "[]"
        self.assertEqual(scheduler._configured_sources(), [])

    def test_telegram_uses_same_empty_list_semantics(self):
        from app.bot import handlers

        values = {
            "GY_ORGANIZE_SOURCE_DIRS": "[]",
        }
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)):
            self.assertEqual(handlers._configured_organize_sources(), [])


class MediaConfigSaveTests(unittest.TestCase):
    def setUp(self):
        from app.routes import api

        self.api = api
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))
        )

    def _save(self, payload):
        strm_scheduler = MagicMock()
        organize_scheduler = MagicMock()
        with patch("app.routes.api.require_api_login"), patch(
            "app.routes.api.config.set_and_save"
        ) as save, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.scheduler.get_scheduler", return_value=strm_scheduler
        ), patch(
            "app.modules.organize_scheduler.get_organize_scheduler",
            return_value=organize_scheduler,
        ):
            result = self.api.save_config(self.request, payload)
        return result, save, strm_scheduler, organize_scheduler

    def test_source_arrays_are_normalized(self):
        result, save, strm_scheduler, organize_scheduler = self._save({
            "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"11","name":"源一"},"22"]',
            "GY_STRM_SOURCE_DIRS": '[{"id":"33","name":"STRM"}]',
        })
        self.assertEqual(result, {"success": True})
        saved = save.call_args.args[0]
        self.assertEqual(json.loads(saved["GY_ORGANIZE_SOURCE_DIRS"]), [
            {"id": "11", "name": "源一"},
            {"id": "22", "name": "源目录2"},
        ])
        strm_scheduler.reload.assert_called_once_with()
        organize_scheduler.reload.assert_called_once_with()

    def test_removing_organize_source_prunes_and_persists_nsfw_scope(self):
        current = {
            "GY_ORGANIZE_SOURCE_DIRS": json.dumps([
                {"id": "11", "name": "普通源"},
                {"id": "22", "name": "成人源"},
            ]),
            "GY_ORGANIZE_NSFW_SOURCE_IDS": '["11","22"]',
        }
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": current.get(key, default),
        ):
            result, save, _, _ = self._save({
                "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"11","name":"普通源"}]',
            })

        self.assertEqual(result, {"success": True})
        saved = save.call_args.args[0]
        self.assertEqual(json.loads(saved["GY_ORGANIZE_NSFW_SOURCE_IDS"]), ["11"])

    def test_advanced_media_server_mapping_is_validated_and_canonicalized(self):
        result, save, _, _ = self._save({
            "JELLYFIN_PATH_MAPPINGS": json.dumps({
                "/data/strm/电影": r"\\NAS\Media\电影",
            }, ensure_ascii=False),
        })

        self.assertEqual(result, {"success": True})
        saved = save.call_args.args[0]
        self.assertEqual(json.loads(saved["JELLYFIN_PATH_MAPPINGS"]), [{
            "local": "/data/strm/电影",
            "server": "//NAS/Media/电影",
        }])

    def test_invalid_advanced_media_server_mapping_is_rejected(self):
        result, save, _, _ = self._save({
            "JELLYFIN_PATH_MAPPINGS": '[{"local":"relative","server":"/media"}]',
        })

        self.assertEqual(result.status_code, 400)
        self.assertIn("路径映射无效", result.body.decode("utf-8"))
        save.assert_not_called()

    def test_invalid_sources_cron_and_numeric_limits_are_rejected(self):
        cases = [
            ({"GY_ORGANIZE_SOURCE_DIRS": "bad"}, "不是有效 JSON"),
            ({"GY_STRM_SOURCE_DIRS": '[{"id":"0"}]'}, "不能为根目录"),
            ({"STRM_SCHEDULE_CRON": "0 0 0 0 0 0"}, "5 段 cron"),
            ({"GY_ORGANIZE_SCHEDULE_CRON": "invalid"}, "5 段 cron"),
            ({"STRM_SKIP_THRESHOLD_MB": "-1"}, "不小于 0"),
            ({"GY_ORGANIZE_MEDIA_PROBE_TIMEOUT_SECONDS": "301"}, "包含不允许的配置项"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                result, save, _, _ = self._save(payload)
                self.assertEqual(result.status_code, 400)
                self.assertIn(expected, result.body.decode("utf-8"))
                save.assert_not_called()


class MediaSourceUiContractTests(unittest.TestCase):
    def test_organize_and_strm_use_batch_directory_picker(self):
        root = Path(__file__).resolve().parents[1]
        organize = ((root / "app/templates/organize.html").read_text(encoding="utf-8") + (root / "app/static/js/organize.js").read_text(encoding="utf-8") + (root / "app/static/css/organize.css").read_text(encoding="utf-8"))
        strm = ((root / "app/templates/guangya_strm.html").read_text(encoding="utf-8") + (root / "app/static/js/guangya-strm.js").read_text(encoding="utf-8"))
        app_js = (root / "app/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("批量选择源目录", organize)
        self.assertIn("multiple:mode==='source'", organize)
        self.assertIn("selected:mode==='source'?sources:[]", organize)
        self.assertIn("批量选择源目录", strm)
        self.assertIn("multiple:true,selected:sources", strm)
        self.assertIn("const selected = new Map", app_js)
        self.assertIn("已选择 ${selected.size} 个", app_js)
        self.assertIn("确认选择", app_js)


if __name__ == "__main__":
    unittest.main()
