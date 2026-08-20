"""识别预处理规则的持久化、安全边界、全链路投影与 UI 契约。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from tests.support import IsolatedDatabaseTestCase


class RecognitionPreprocessSchemaTests(IsolatedDatabaseTestCase):
    def test_schema_and_recommended_rules_are_available(self):
        from app.modules import recognition_preprocess_rules as rules

        with db.get_conn() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='recognition_preprocess_rules'"
            ).fetchone()
        self.assertIsNotNone(table)
        items = rules.list_rules()
        self.assertEqual(len([item for item in items if item["builtin"]]), 21)
        self.assertEqual(len([item for item in items if item["builtin"] and not item["disabled"]]), 14)
        self.assertTrue(any(item["action"] == "season_override" and item["disabled"] for item in items))


class RecognitionPreprocessPersistenceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_preprocess_rules")

    @staticmethod
    def payload(**overrides):
        data = {
            "name": "发布组别名清理",
            "matcher_type": "text",
            "pattern": "OldTitle",
            "scope": "filename",
            "action": "replace",
            "replacement": "NewTitle",
            "numeric_value": None,
            "priority": 50,
            "disabled": False,
        }
        data.update(overrides)
        return data

    def test_crud_preserves_builtins_and_custom_rules(self):
        from app.modules import recognition_preprocess_rules as rules

        created = rules.create_rule(self.payload())
        self.assertFalse(created["builtin"])
        updated = rules.update_rule(created["id"], self.payload(name="已更新", disabled=True))
        self.assertEqual(updated["name"], "已更新")
        self.assertTrue(updated["disabled"])
        self.assertTrue(rules.delete_rule(created["id"]))
        builtin = next(item for item in rules.list_rules() if item["builtin"])
        with self.assertRaisesRegex(ValueError, "推荐规则不能删除"):
            rules.delete_rule(builtin["id"])

    def test_validation_rejects_unsafe_regex_and_invalid_numeric_action(self):
        from app.modules import recognition_preprocess_rules as rules

        with self.assertRaises(ValueError):
            rules.create_rule(self.payload(matcher_type="regex", pattern="(a+)+$"))
        with self.assertRaisesRegex(ValueError, "季号"):
            rules.create_rule(self.payload(action="season_override", numeric_value=-1))

    def test_text_then_numeric_rules_apply_in_priority_order(self):
        from app.modules import recognition_preprocess_rules as rules

        configured = [
            rules.normalize_rule(self.payload(priority=100)),
            rules.normalize_rule(self.payload(
                name="第二季覆盖", pattern="NewTitle", action="season_override",
                replacement="", numeric_value=2, priority=90,
            )),
            rules.normalize_rule(self.payload(
                name="集数偏移", pattern="NewTitle", action="episode_offset",
                replacement="", numeric_value=1, priority=80,
            )),
        ]
        result = rules.apply_rules(
            "OldTitle.S01E03.mkv", "/TV/OldTitle", season=1, episode=3,
            rules=configured,
        )
        self.assertEqual(result.filename, "NewTitle.S01E03.mkv")
        self.assertEqual((result.season, result.episode), (2, 4))
        self.assertEqual([item["action"] for item in result.applied_rules], [
            "replace", "season_override", "episode_offset",
        ])

    def test_regex_matchers_are_compiled_once_until_rules_change(self):
        from app.modules import recognition_preprocess_rules as rules

        configured = [rules.normalize_rule(self.payload(
            name="清理发布组", matcher_type="regex", pattern=r"(?i)\[group\]",
            scope="both", action="delete", replacement="", priority=100,
        ))]
        rules.invalidate_active_cache()
        original_compile = rules.re.compile
        with patch.object(rules.re, "compile", wraps=original_compile) as compile_mock:
            first = rules.apply_rules(
                "[Group] Show.S01E01.mkv", "/TV/[Group] Show", rules=configured,
            )
            second = rules.apply_rules(
                "[Group] Show.S01E02.mkv", "/TV/[Group] Show", rules=configured,
            )

        self.assertEqual(first.filename, " Show.S01E01.mkv")
        self.assertEqual(second.parent_path, "/TV/ Show")
        self.assertEqual(compile_mock.call_count, 1)
        self.assertEqual(rules._compile_regex.cache_info().currsize, 1)

        rules.invalidate_active_cache()
        self.assertEqual(rules._compile_regex.cache_info().currsize, 0)

    def test_recommended_unicode_and_technical_rules_are_low_risk_and_chainable(self):
        from app.modules import recognition_preprocess_rules as rules

        configured = [dict(item) for item in rules.BUILTIN_RULES if not item["disabled"]]
        result = rules.apply_rules(
            "\u200eShow．S01–E02［中文字幕］（2026）.DoVi.DTS-X.mkv",
            "/TV/\u202fShow－Season 01",
            season=1,
            episode=2,
            rules=configured,
        )
        self.assertEqual(result.filename, "Show.S01-E02(2026)...mkv")
        self.assertEqual(result.parent_path, "/TV/ Show-Season 01")
        applied = {item["name"] for item in result.applied_rules}
        self.assertIn("清理扩展 Unicode 格式控制符", applied)
        self.assertIn("统一兼容句点", applied)
        self.assertIn("清理 Dolby Vision 发布规格", applied)
        self.assertIn("清理 DTS:X 发布规格", applied)

    def test_offset_does_not_invent_missing_position(self):
        from app.modules import recognition_preprocess_rules as rules

        configured = [rules.normalize_rule(self.payload(
            action="episode_offset", replacement="", numeric_value=1,
        ))]
        result = rules.apply_rules("OldTitle.mkv", season=None, episode=None, rules=configured)
        self.assertIsNone(result.episode)

    def test_restore_defaults_resets_builtin_without_deleting_custom(self):
        from app.modules import recognition_preprocess_rules as rules

        custom = rules.create_rule(self.payload())
        builtin = next(item for item in rules.list_rules() if item["builtin"])
        changed = {**builtin, "name": "被修改", "disabled": True}
        rules.update_rule(builtin["id"], changed)
        restored = rules.restore_builtin_rules()
        self.assertTrue(any(item["id"] == custom["id"] for item in restored))
        self.assertFalse(any(item["builtin"] and item["name"] == "被修改" for item in restored))


class RecognitionPreprocessScraperTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_preprocess_rules")
            conn.execute("DELETE FROM tmdb_regex_rules")
            conn.execute("DELETE FROM tmdb_lock")

    def test_search_consumes_cleaned_name_and_result_carries_effective_position(self):
        from app.modules import recognition_preprocess_rules as rules
        from app.modules.scraper import RecognitionResult, TMDBScraper

        rules.create_rule({
            "name": "别名替换", "matcher_type": "text", "pattern": "WrongTitle",
            "scope": "both", "action": "replace", "replacement": "RightTitle",
            "priority": 100, "disabled": False,
        })
        rules.create_rule({
            "name": "第二季", "matcher_type": "text", "pattern": "RightTitle",
            "scope": "both", "action": "season_override", "numeric_value": 2,
            "priority": 90, "disabled": False,
        })
        scraper = TMDBScraper(client=Mock())
        matched = RecognitionResult(
            tmdb_id="42", title="正确标题", year="2024", media_type="tv",
            confidence=1.0, status="matched", matched_by="search",
        )
        with patch.object(scraper, "deterministic_recognize", return_value=matched) as recognize:
            result = scraper.match("WrongTitle.S01E03.mkv", "/TV/WrongTitle")
        self.assertEqual(recognize.call_args.args[:2], (
            "RightTitle.S01E03.mkv", "/TV/RightTitle",
        ))
        self.assertEqual((result.effective_season, result.effective_episode), (2, 3))
        parsed = scraper.parse_media(
            "WrongTitle.S01E03.mkv", "/TV/WrongTitle", result,
        )
        self.assertEqual(
            (parsed.effective_season, parsed.effective_episode),
            (2, 3),
        )

    def test_auxiliary_fallbacks_receive_raw_source_anchors_before_preprocess(self):
        from app.modules import recognition_preprocess_rules as rules
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        rules.create_rule({
            "name": "危险别名替换", "matcher_type": "text", "pattern": "WrongTitle",
            "scope": "both", "action": "replace", "replacement": "RightTitle",
            "priority": 100, "disabled": False,
        })
        scraper = TMDBScraper(client=Mock())
        failed = RecognitionResult(
            status="no_result", need_confirm=True, media_type="tv",
            context=RecognitionContext(
                filename="RightTitle.S01E01.mkv", normalized_title="RightTitle",
                filename_title="RightTitle", media_type="tv", season=1, episode=1,
            ),
        )
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed,
        ), patch.object(
            scraper, "_external_hint_fallback", return_value=failed,
        ) as external, patch.object(
            scraper, "_release_group_fallback", return_value=failed,
        ), patch.object(
            scraper, "_ai_fallback", return_value=failed,
        ) as ai:
            result = scraper.match("WrongTitle.S01E01.mkv", "/TV/WrongTitle")

        self.assertIs(result, failed)
        self.assertIn("WrongTitle", external.call_args.kwargs["source_anchors"])
        self.assertNotIn("RightTitle", external.call_args.kwargs["source_anchors"])
        self.assertEqual(
            ai.call_args.kwargs["source_anchors"],
            external.call_args.kwargs["source_anchors"],
        )

    def test_explicit_tmdb_id_still_wins_before_preprocess_search(self):
        from app.modules.scraper import MatchResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        fixed = MatchResult(
            tmdb_id="123", title="固定", year="2020", media_type="tv",
            confidence=1.0, status="matched", matched_by="tmdb_id",
        )
        with patch.object(scraper, "match_from_tmdb", return_value=fixed), patch.object(
            scraper, "deterministic_recognize"
        ) as recognize:
            result = scraper.match("固定 {tmdb-123}.S01E01.mkv", "/TV")
        recognize.assert_not_called()
        self.assertEqual(result.matched_by, "tmdb_id")
        self.assertTrue(result.preprocess_evaluated)


class RecognitionPreprocessApiTests(IsolatedDatabaseTestCase):
    def test_crud_preview_and_restore_handlers(self):
        from app.routes import tools_api

        payload = RecognitionPreprocessPersistenceTests.payload()
        with patch.object(tools_api, "require_api_login", return_value=None):
            listed = json.loads(tools_api.list_recognition_preprocess_rules_api(Mock()).body)
            self.assertEqual(listed["summary"]["builtin"], 21)
            created_response = tools_api.create_recognition_preprocess_rule_api(Mock(), payload)
            created = json.loads(created_response.body)
            preview_response = tools_api.preview_recognition_preprocess_rules_api(Mock(), {
                "rule": payload, "filename": "OldTitle.S01E01.mkv", "season": 1, "episode": 1,
            })
            preview = json.loads(preview_response.body)
            self.assertEqual(preview["filename_after"], "NewTitle.S01E01.mkv")
            updated = json.loads(tools_api.update_recognition_preprocess_rule_api(
                Mock(), created["id"], {**payload, "name": "已更新"},
            ).body)
            self.assertEqual(updated["name"], "已更新")
            deleted = json.loads(tools_api.delete_recognition_preprocess_rule_api(
                Mock(), created["id"],
            ).body)
            self.assertTrue(deleted["success"])
            restored = json.loads(tools_api.restore_recognition_preprocess_rules_api(Mock()).body)
            self.assertEqual(restored["restored"], 21)


class RecognitionPreprocessUiTests(unittest.TestCase):
    def test_rules_page_exposes_stable_preprocess_ledger_editor_and_defaults(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        for marker in (
            "openPreprocessRulesBtn", "preprocessRulesModal", "preprocessRuleTableBody",
            "preprocessRuleForm", "restorePreprocessRulesBtn", "preprocessSamplePreview",
            "推荐规则", "季号偏移", "集数偏移",
            "常见与复合发布组由本地识别词库清洗",
            "/api/tools/recognition-preprocess-rules",
        ):
            self.assertIn(marker, html)
        self.assertIn("tmdb-regex-table-frame", html)
        self.assertIn("min-height:96px", html)
        self.assertIn("正在刷新，保留当前列表", html)
        script = Path("app/static/js/organize.js").read_text(encoding="utf-8")
        self.assertNotIn("{{", script)
        self.assertNotIn("{% if", script)
        self.assertIn("if(isRules){", script)
