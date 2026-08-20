"""TMDB 正则识别规则的边界、优先级、API 与 UI 契约测试。"""
from __future__ import annotations

import importlib.util
import inspect
import sqlite3
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from app import database as db
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class TmdbRegexRuleSchemaTests(IsolatedDatabaseTestCase):
    def test_init_db_creates_tmdb_regex_rules_table(self):
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tmdb_regex_rules'"
            ).fetchone()
        self.assertIsNotNone(row)


class TmdbRegexRuleModuleContractTests(unittest.TestCase):
    def test_tmdb_regex_rules_module_is_importable(self):
        self.assertIsNotNone(importlib.util.find_spec("app.modules.tmdb_regex_rules"))

    def test_tmdb_regex_rules_module_exposes_bounded_crud_and_matcher(self):
        from app.modules import tmdb_regex_rules as rules

        for name in (
            "RuleMatch", "create_rule", "list_rules", "update_rule",
            "delete_rule", "find_tmdb_regex_match", "preview_rule",
        ):
            self.assertTrue(callable(getattr(rules, name, None)), name)


class TmdbRegexScraperContractTests(unittest.TestCase):
    def test_match_result_and_match_signature_expose_rule_diagnostics(self):
        from app.modules.scraper import MatchResult, TMDBScraper

        self.assertIn("regex_rule_id", MatchResult.__dataclass_fields__)
        self.assertIn("season_override", MatchResult.__dataclass_fields__)
        self.assertIn("parent_path", inspect.signature(TMDBScraper.match).parameters)


class _RegexDetailClient:
    api_key = "test-key"
    base_url = "https://tmdb.test/3"
    session = None
    config_error = ""

    def __init__(self):
        self.detail_calls = []
        self.search_calls = []

    def detail(self, tmdb_id, media_type):
        self.detail_calls.append((str(tmdb_id), media_type))
        if media_type == "tv":
            return {"id": int(tmdb_id), "name": "规则剧集", "first_air_date": "2020-01-02"}
        return {"id": int(tmdb_id), "title": "规则电影", "release_date": "2021-03-04"}

    def search(self, title, year, media_type):
        self.search_calls.append((title, year, media_type))
        return [{"id": 999, "name": "搜索结果", "first_air_date": "2022-01-01"}]


class TmdbRegexBackwardCompatibilityTests(unittest.TestCase):
    def test_matcher_without_initialized_rule_table_falls_back_to_no_match(self):
        from app.modules import tmdb_regex_rules as rules

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row

        @contextmanager
        def bare_connection():
            try:
                yield connection
            finally:
                pass

        try:
            with patch.object(rules, "get_conn", bare_connection):
                self.assertIsNone(rules.find_tmdb_regex_match("Example.mkv", "/TV", "movie"))
        finally:
            connection.close()

    def test_organizer_keeps_legacy_single_argument_scraper_compatible(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import OrganizeRules, Organizer
        from app.modules.scraper import MatchResult

        class LegacyScraper:
            def __init__(self):
                self.calls = []

            def match(self, filename):
                self.calls.append(filename)
                return MatchResult(media_type="movie", need_confirm=True)

        scraper = LegacyScraper()
        organizer = Organizer(client=object(), scraper=scraper)
        organizer._plan_one(
            GuangYaFile("file-1", "Legacy.mkv", False), "旧目录", OrganizeRules()
        )
        self.assertEqual(scraper.calls, ["Legacy.mkv"])


class TmdbRegexScraperPrecedenceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM tmdb_regex_rules")
            conn.execute("DELETE FROM tmdb_lock")

    @staticmethod
    def create_rule(**overrides):
        from app.modules import tmdb_regex_rules as rules

        data = {
            "name": "规则剧集", "pattern": "Example", "match_target": "filename",
            "tmdb_id": "321", "media_type": "tv", "season_override": 5,
            "priority": 100, "disabled": False,
        }
        data.update(overrides)
        return rules.create_rule(data)

    def test_explicit_tmdb_id_precedes_regex_rule(self):
        from app.modules.scraper import TMDBScraper

        self.create_rule(pattern="Movie", media_type="any", season_override=None)
        client = _RegexDetailClient()
        result = TMDBScraper(client=client).match("Movie.2026.{tmdb-777}.mkv", "/TV")

        self.assertEqual(result.tmdb_id, "777")
        self.assertEqual(result.matched_by, "tmdb_id")
        self.assertIsNone(result.regex_rule_id)
        self.assertEqual(client.detail_calls, [("777", "movie")])
        self.assertEqual(client.search_calls, [])

    def test_manual_lock_precedes_regex_rule(self):
        from app.modules.scraper import TMDBScraper

        self.create_rule()
        client = _RegexDetailClient()
        scraper = TMDBScraper(client=client)
        scraper.confirm(
            "Example.Show.S01E01.mkv", "888", "人工锁定", "2019", "tv",
            parent_path="/TV/Example",
        )
        with db.get_conn() as conn:
            stored = conn.execute(
                "SELECT lock_source,parent_path,season,key_version "
                "FROM tmdb_lock WHERE raw_name=?",
                ("Example.Show.S01E01.mkv",),
            ).fetchone()
        result = scraper.match("Example.Show.S01E01.mkv", "/TV/Example")

        self.assertEqual(stored["lock_source"], "manual")
        self.assertEqual(stored["parent_path"], "TV/Example")
        self.assertEqual(stored["season"], 1)
        self.assertEqual(stored["key_version"], 1)
        self.assertEqual(result.tmdb_id, "888")
        self.assertEqual(result.matched_by, "lock")
        self.assertEqual((result.provider, result.external_id), ("tmdb", "888"))
        self.assertIsNone(result.regex_rule_id)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(client.search_calls, [])


    def test_manual_locks_are_isolated_by_parent_context(self):
        from app.modules.scraper import TMDBScraper

        scraper = TMDBScraper(client=_RegexDetailClient())
        filename = "01.mkv"
        scraper.confirm(
            filename, "101", "节目甲", "2024", "tv",
            parent_path="/TV/节目甲/Season 01",
        )
        scraper.confirm(
            filename, "202", "节目乙", "2025", "tv",
            parent_path="/TV/节目乙/Season 01",
        )

        first = scraper.match(filename, "/TV/节目甲/Season 01", media_type_hint="tv")
        second = scraper.match(filename, "/TV/节目乙/Season 01", media_type_hint="tv")

        self.assertEqual((first.tmdb_id, second.tmdb_id), ("101", "202"))
        self.assertEqual((first.matched_by, second.matched_by), ("lock", "lock"))
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT parent_path FROM tmdb_lock WHERE raw_name=? ORDER BY tmdb_id",
                (filename,),
            ).fetchall()
        self.assertEqual(
            [row["parent_path"] for row in rows],
            ["TV/节目甲/Season 01", "TV/节目乙/Season 01"],
        )

    def test_regex_rule_precedes_search_and_uses_tmdb_detail(self):
        from app.modules.scraper import TMDBScraper

        rule = self.create_rule()
        client = _RegexDetailClient()
        result = TMDBScraper(client=client).match(
            "Example.Show.S02E03.1080p.mkv", "/TV/Example Show"
        )

        self.assertEqual(result.tmdb_id, "321")
        self.assertEqual((result.title, result.year, result.media_type), ("规则剧集", "2020", "tv"))
        self.assertEqual(result.matched_by, "regex_rule")
        self.assertEqual(result.regex_rule_id, rule["id"])
        self.assertEqual(result.season_override, 5)
        self.assertEqual(result.confidence, 1.0)
        self.assertFalse(result.need_confirm)
        self.assertEqual(client.detail_calls, [("321", "tv")])
        self.assertEqual(client.search_calls, [])

    def test_no_rule_hit_falls_back_to_existing_search(self):
        from app.modules.scraper import TMDBScraper

        self.create_rule(pattern="NeverMatches")
        client = _RegexDetailClient()
        with patch("app.modules.scraper.get_bool", return_value=False):
            result = TMDBScraper(client=client).match("Search.Show.S01E01.mkv", "/TV")

        self.assertEqual(result.matched_by, "search")
        self.assertTrue(client.search_calls)


class TmdbRegexOrganizeIntegrationTests(unittest.TestCase):
    def test_organizer_passes_parent_path_to_scraper(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import OrganizeRules, Organizer
        from app.modules.scraper import MatchResult

        scraper = Mock()
        scraper.match.return_value = MatchResult(media_type="movie", need_confirm=True)
        organizer = Organizer(client=object(), scraper=scraper)
        organizer._plan_one(
            GuangYaFile("file-1", "Example.mkv", False, parent_id="parent-1"),
            "源目录/电影",
            OrganizeRules(),
        )
        scraper.match.assert_called_once_with("Example.mkv", "源目录/电影")

    def test_organizer_prefers_declared_parse_media_and_uses_effective_position(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import OrganizeRules, Organizer
        from app.modules.scraper import (
            MatchResult, ReleaseParseResult, extract_recognition_context,
        )

        class Scraper:
            def __init__(self):
                self.parse_media_calls = []

            def match(self, filename, parent_path=""):
                return MatchResult(
                    tmdb_id="321", external_id="321", provider="tmdb",
                    title="统一解析剧集", year="2020", media_type="tv",
                    confidence=1.0, status="matched", matched_by="search",
                )

            def parse_media(self, filename, parent_path="", match=None):
                self.parse_media_calls.append((filename, parent_path, match))
                context = extract_recognition_context(filename, parent_path)
                return ReleaseParseResult(
                    filename=filename, parent_path=parent_path,
                    title="统一解析剧集", year="2020", media_type="tv",
                    tmdb_id="321", source_season=2, source_episode=13,
                    effective_season=4, effective_episode=6, context=context,
                )

            def get_detail(self, tmdb_id, media_type):
                return {
                    "genres": [], "origin_country": ["CN"],
                    "seasons": [{"season_number": 4, "episode_count": 12}],
                }

        scraper = Scraper()
        organizer = Organizer(client=object(), scraper=scraper)
        plan = organizer._plan_one(
            GuangYaFile(
                "file-1", "Example.Show.S02E13.mkv", False, parent_id="parent-1",
            ),
            "源目录/剧集",
            OrganizeRules(
                region_split=False, year_split=False, media_info_enabled=False,
            ),
        )

        self.assertEqual(len(scraper.parse_media_calls), 1)
        self.assertEqual(
            scraper.parse_media_calls[0][:2],
            ("Example.Show.S02E13.mkv", "源目录/剧集"),
        )
        self.assertEqual((plan.source_season, plan.source_episode), (2, 13))
        self.assertEqual((plan.season, plan.episode), (4, 6))
        self.assertIn("S04E06", plan.new_name)

    def test_organizer_rejects_scraper_without_declared_parse_media(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import OrganizeRules, Organizer
        from app.modules.scraper import MatchResult

        scraper = Mock()
        scraper.manual_position_confirmed = False
        scraper.match.return_value = MatchResult(
            tmdb_id="321", external_id="321", provider="tmdb",
            title="统一剧集", year="2020", media_type="tv",
            confidence=1.0, status="matched", matched_by="search",
        )
        scraper.get_detail.return_value = {
            "genres": [], "origin_country": ["CN"],
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }

        organizer = Organizer(client=object(), scraper=scraper)
        with self.assertRaisesRegex(TypeError, "parse_media"):
            organizer._plan_one(
                GuangYaFile(
                    "file-1", "Example.Show.S01E02.mkv", False, parent_id="parent-1",
                ),
                "源目录/剧集",
                OrganizeRules(
                    region_split=False, year_split=False, media_info_enabled=False,
                ),
            )

    def test_organizer_applies_rule_season_override_to_naming_plan(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import OrganizeRules, Organizer
        from app.modules.scraper import MatchResult

        class Scraper:
            def match(self, filename, parent_path=""):
                return MatchResult(
                    tmdb_id="321", title="规则剧集", year="2020", media_type="tv",
                    confidence=1.0, status="matched", matched_by="regex_rule",
                    regex_rule_id=7, season_override=5,
                )

            def parse_media(self, filename, parent_path="", match=None):
                return release_parse_result(
                    {"title": "规则剧集", "year": "2020", "type": "tv", "season": 5, "episode": 3},
                    filename=filename, parent_path=parent_path,
                    source_season=2, source_episode=3,
                )

            def get_detail(self, tmdb_id, media_type):
                return {"genres": [], "origin_country": ["CN"], "seasons": [{"season_number": 5, "episode_count": 12}]}

        organizer = Organizer(client=object(), scraper=Scraper())
        plan = organizer._plan_one(
            GuangYaFile("file-1", "Example.Show.S02E03.mkv", False, parent_id="parent-1"),
            "源目录/剧集",
            OrganizeRules(region_split=False, year_split=False, media_info_enabled=False),
        )

        self.assertEqual(plan.season, 5)
        self.assertEqual(plan.episode, 3)
        self.assertEqual(plan.season_total, 12)
        self.assertIn("S05E03", plan.new_name)


class TmdbRegexRuleApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM tmdb_regex_rules")

    @staticmethod
    def payload(**overrides):
        data = {
            "name": "API 规则", "pattern": "Api.Show", "match_target": "filename",
            "tmdb_id": "456", "media_type": "tv", "season_override": 3,
            "priority": 40, "disabled": False,
        }
        data.update(overrides)
        return data

    def test_tools_api_exposes_regex_rule_crud_and_preview_handlers(self):
        from app.routes import tools_api

        for name in (
            "list_tmdb_regex_rules_api", "create_tmdb_regex_rule_api",
            "update_tmdb_regex_rule_api", "delete_tmdb_regex_rule_api",
            "preview_tmdb_regex_rule_api",
        ):
            self.assertTrue(callable(getattr(tools_api, name, None)), name)

    def test_scrape_confirm_api_preserves_parent_context(self):
        from app.routes import tools_api

        scraper = Mock()
        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "TMDBScraper", return_value=scraper
        ):
            response = tools_api.scrape_confirm(Mock(), {
                "filename": "01.mkv",
                "parent_path": "/TV/Example/Season 02",
                "tmdb_id": "456",
                "title": "API 规则剧集",
                "year": "2020",
                "media_type": "tv",
            })

        self.assertEqual(response.status_code, 200)
        scraper.confirm.assert_called_once_with(
            "01.mkv", "456", "API 规则剧集", "2020", "tv",
            parent_path="/TV/Example/Season 02",
        )

    def test_crud_and_sample_preview_api(self):
        from app.routes import tools_api

        verified = {"tmdb_id": "456", "title": "API 规则剧集", "year": "2020", "media_type": "tv"}
        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_validate_tmdb_regex_target", return_value=verified
        ):
            created_response = tools_api.create_tmdb_regex_rule_api(Mock(), self.payload())
            self.assertEqual(created_response.status_code, 201)
            created = __import__("json").loads(created_response.body)

            listed = __import__("json").loads(tools_api.list_tmdb_regex_rules_api(Mock()).body)
            self.assertEqual([row["id"] for row in listed], [created["id"]])

            preview_response = tools_api.preview_tmdb_regex_rule_api(Mock(), {
                "rule": self.payload(pattern=r"Api[ ._-]+Show", season_override=4),
                "filename": "Api.Show.S02E01.mkv",
                "parent_path": "/TV/Api Show",
                "media_type": "tv",
            })
            preview = __import__("json").loads(preview_response.body)
            self.assertTrue(preview["matched"])
            self.assertEqual(preview["season_override"], 4)

            updated_response = tools_api.update_tmdb_regex_rule_api(
                Mock(), created["id"], self.payload(name="更新规则", priority=90, disabled=True)
            )
            updated = __import__("json").loads(updated_response.body)
            self.assertEqual((updated["name"], updated["priority"], updated["disabled"]), ("更新规则", 90, True))

            deleted = __import__("json").loads(
                tools_api.delete_tmdb_regex_rule_api(Mock(), created["id"]).body
            )
            self.assertTrue(deleted["success"])

    def test_api_returns_bounded_validation_errors(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_validate_tmdb_regex_target", return_value={
                "tmdb_id": "456", "title": "API 规则剧集", "year": "2020", "media_type": "tv",
            }
        ):
            invalid = tools_api.create_tmdb_regex_rule_api(Mock(), self.payload(pattern="("))
            missing = tools_api.update_tmdb_regex_rule_api(Mock(), 99999, self.payload())
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("正则", __import__("json").loads(invalid.body)["error"])
        self.assertEqual(missing.status_code, 404)

    def test_api_rejects_unverified_tmdb_target_as_upstream_unavailable(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_validate_tmdb_regex_target",
            side_effect=tools_api.TmdbRegexTargetUnavailable("暂时无法确认 TMDB 456"),
        ):
            response = tools_api.create_tmdb_regex_rule_api(Mock(), self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertIn("TMDB 456", __import__("json").loads(response.body)["error"])

    def test_any_type_requires_sample_and_resolves_to_runtime_type(self):
        from app.routes import tools_api
        from app.modules.scraper import MatchResult

        class FakeScraper:
            def match_from_tmdb(self, tmdb_id, media_type):
                return MatchResult(
                    tmdb_id=str(tmdb_id), title="样例剧", year="2020",
                    media_type=media_type, status="matched",
                )

        any_payload = self.payload(media_type="any", season_override=None)
        with patch.object(tools_api, "TMDBScraper", return_value=FakeScraper()):
            with self.assertRaisesRegex(ValueError, "样例文件名"):
                tools_api._validate_tmdb_regex_target(any_payload)
            verified = tools_api._validate_tmdb_regex_target(
                any_payload, filename="Sample.Show.S02E03.mkv", parent_path="/TV/Sample Show",
            )
        self.assertEqual(verified["media_type"], "tv")

    def test_update_missing_rule_short_circuits_tmdb_validation(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_validate_tmdb_regex_target"
        ) as validate:
            response = tools_api.update_tmdb_regex_rule_api(Mock(), 99999, self.payload())
        self.assertEqual(response.status_code, 404)
        validate.assert_not_called()

    def test_create_any_rule_persists_the_sample_resolved_type(self):
        from app.routes import tools_api

        payload = self.payload(
            media_type="any", season_override=None,
            sample_filename="Sample.Show.S02E01.mkv",
            sample_parent_path="/TV/Sample Show",
        )
        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_validate_tmdb_regex_target", return_value={
                "tmdb_id": "456", "title": "样例剧", "year": "2020", "media_type": "tv",
            },
        ):
            response = tools_api.create_tmdb_regex_rule_api(Mock(), payload)
        self.assertEqual(response.status_code, 201)
        created = __import__("json").loads(response.body)
        self.assertEqual(created["media_type"], "tv")
        self.assertEqual(created["verified_target"]["title"], "样例剧")


class _RulePreviewScraper:
    match_mode = "strict"

    def parse_media(self, filename, parent_path="", match=None):
        return release_parse_result(
            {"title": "Api Show", "year": "2020", "type": "tv", "season": 4, "episode": 1},
            filename=filename, parent_path=parent_path,
            source_season=2, source_episode=1,
        )

    def parse_resource_tags(self, filename):
        return {}

    def match(self, filename, parent_path=""):
        from app.modules.scraper import MatchResult

        return MatchResult(
            tmdb_id="456", title="规则剧集", year="2020", media_type="tv",
            confidence=1.0, status="matched", matched_by="regex_rule",
            threshold=1.0, regex_rule_id=17, season_override=4,
        )

    def get_detail(self, tmdb_id, media_type):
        return {"id": 456, "name": "规则剧集", "first_air_date": "2020-01-01"}


class TmdbRegexScrapePreviewTests(unittest.TestCase):
    def test_scrape_preview_reports_rule_id_parent_path_and_season_override(self):
        import json
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "TMDBScraper", return_value=_RulePreviewScraper()
        ), patch.object(tools_api, "_build_naming_preview", return_value={}) as naming:
            response = tools_api.scrape_preview(Mock(), {
                "filename": "Api.Show.S02E01.mkv", "parent_path": "/TV/Api Show"
            })

        payload = json.loads(response.body)
        self.assertEqual(payload["diagnostic"]["regex_rule_id"], 17)
        naming.assert_called_once_with(
            "456", "tv", 4, 1, "Api.Show.S02E01.mkv", "规则剧集", "2020"
        )


class TmdbRegexRuleUiTests(unittest.TestCase):
    def test_organize_page_has_rule_table_modal_and_sample_preview(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        for marker in (
            "openTmdbRegexRulesBtn", "tmdbRegexRulesModal", "tmdbRegexRuleTableBody",
            "tmdbRegexRuleForm", "tmdbRegexSampleFilename", "tmdbRegexSampleParent",
            "tmdbRegexSamplePreview", "tmdb-regex-rule-card", "tmdb-regex-editor-section",
            "匹配条件", "锁定结果", "验证样例", "/api/tools/tmdb-regex-rules",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('<table class="download-table tmdb-regex-table">', html)

    def test_rule_table_and_preview_reserve_stable_layout_space(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        self.assertIn("tmdb-regex-table-frame", html)
        self.assertIn("tmdb-regex-preview-frame", html)
        self.assertIn("min-height:280px", html)
        self.assertIn("min-height:96px", html)

    def test_rule_ui_uses_force_match_copy_and_preserves_any_type(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        self.assertIn("TMDB 强制匹配规则", html)
        self.assertNotIn("TMDB 正则规则台账", html)
        self.assertNotIn("==='any'?'tv'", html)
        self.assertIn("verified_target", html)


class TmdbRegexRulePersistenceTests(IsolatedDatabaseTestCase):
    @staticmethod
    def payload(**overrides):
        data = {
            "name": "剧集固定映射",
            "pattern": r"(?i)example[ ._-]+show",
            "match_target": "filename",
            "tmdb_id": "12345",
            "media_type": "tv",
            "season_override": 2,
            "priority": 10,
            "disabled": False,
        }
        data.update(overrides)
        return data

    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM tmdb_regex_rules")

    def test_create_rejects_invalid_or_overlong_regex(self):
        from app.modules import tmdb_regex_rules as rules

        with self.assertRaisesRegex(ValueError, "正则"):
            rules.create_rule(self.payload(pattern="("))
        with self.assertRaisesRegex(ValueError, "500"):
                rules.create_rule(self.payload(pattern="x" * 501))

    def test_create_rejects_high_backtracking_patterns(self):
        from app.modules import tmdb_regex_rules as rules

        for pattern in (
            r"(a+)+$",
            r"(a|aa)+$",
            r"(.*){2,}",
            r"(\w+)*$",
            r"^(a)\1+$",
            r"(?P<letter>a)(?P=letter)",
            r"a*a*a*a*b",
            r"a?a?a?a?b",
            r"^[^a]*[^b]*[^c]*Z$",
            r"^[^一-龥]*[^丁-龥]*[^丂-龥]*Z$",
            r"^[^一-龥]*(?:ab)*Z$",
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, "回溯|安全"
            ):
                rules.create_rule(self.payload(pattern=pattern))

    def test_matcher_skips_legacy_unsafe_named_backreference_rule(self):
        from app.modules import tmdb_regex_rules as rules

        timestamp = db.now()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO tmdb_regex_rules("
                "name,pattern,match_target,tmdb_id,media_type,season_override,"
                "priority,disabled,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-unsafe", r"(?P<name>Example)(?P=name)", "filename",
                    "999", "movie", None, 100, 0, timestamp, timestamp,
                ),
            )

        self.assertIsNone(
            rules.find_tmdb_regex_match("ExampleExample.mkv", "/Movies", "movie")
        )

    def test_safe_grouped_release_pattern_remains_supported(self):
        from app.modules import tmdb_regex_rules as rules

        created = rules.create_rule(self.payload(
            pattern=r"^(?:Example|示例)[ ._-]+S\d{2}E\d{2}(?:\.mkv)?$"
        ))
        self.assertEqual(
            rules.find_tmdb_regex_match(
                "Example.S01E02.mkv", "/TV", "tv"
            ).rule_id,
            created["id"],
        )

    def test_create_update_list_and_delete_rule(self):
        from app.modules import tmdb_regex_rules as rules

        created = rules.create_rule(self.payload())
        self.assertGreater(created["id"], 0)
        self.assertEqual(created["pattern"], r"(?i)example[ ._-]+show")
        self.assertEqual(created["season_override"], 2)
        self.assertFalse(created["disabled"])

        updated = rules.update_rule(created["id"], {
            **self.payload(name="电影规则", media_type="movie", season_override=None),
            "priority": 80,
            "disabled": True,
        })
        self.assertEqual(updated["name"], "电影规则")
        self.assertEqual(updated["priority"], 80)
        self.assertTrue(updated["disabled"])
        self.assertIsNone(updated["season_override"])
        self.assertEqual([row["id"] for row in rules.list_rules()], [created["id"]])
        self.assertTrue(rules.delete_rule(created["id"]))
        self.assertFalse(rules.delete_rule(created["id"]))

    def test_update_compiles_before_persisting(self):
        from app.modules import tmdb_regex_rules as rules

        created = rules.create_rule(self.payload())
        with self.assertRaisesRegex(ValueError, "正则"):
            rules.update_rule(created["id"], {**self.payload(), "pattern": "["})
        self.assertEqual(rules.list_rules()[0]["pattern"], r"(?i)example[ ._-]+show")

    def test_create_enforces_two_hundred_rule_limit(self):
        from app.modules import tmdb_regex_rules as rules

        timestamp = db.now()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO tmdb_regex_rules(name,pattern,match_target,tmdb_id,media_type,"
                "season_override,priority,disabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (f"rule-{index}", f"pattern-{index}", "filename", str(index + 1),
                     "any", None, 0, 0, timestamp, timestamp)
                    for index in range(200)
                ],
            )
        with self.assertRaisesRegex(ValueError, "200"):
            rules.create_rule(self.payload(name="overflow"))

    def test_priority_disabled_target_media_type_and_season_override(self):
        from app.modules import tmdb_regex_rules as rules

        disabled = rules.create_rule(self.payload(
            name="disabled", pattern="Disabled", tmdb_id="1", priority=999,
            media_type="any", disabled=True,
        ))
        movie = rules.create_rule(self.payload(
            name="movie-only", pattern="Cinema", tmdb_id="2", priority=500,
            media_type="movie", season_override=None,
        ))
        lower = rules.create_rule(self.payload(
            name="lower", pattern="Example", tmdb_id="3", priority=10,
            media_type="tv", season_override=1,
        ))
        parent = rules.create_rule(self.payload(
            name="parent", pattern=r"Series[\\/]Season 05", match_target="parent",
            tmdb_id="4", priority=100, media_type="tv", season_override=5,
        ))
        both = rules.create_rule(self.payload(
            name="both", pattern=r"Specials[\\/].*E01", match_target="both",
            tmdb_id="5", priority=200, media_type="tv", season_override=0,
        ))

        match = rules.find_tmdb_regex_match("Example.Show.S01E01.mkv", "/TV", "tv")
        self.assertEqual(match.rule_id, lower["id"])
        self.assertEqual((match.tmdb_id, match.season_override), ("3", 1))

        parent_match = rules.find_tmdb_regex_match(
            "Episode.S05E02.mkv", r"TV\Series\Season 05", "tv"
        )
        self.assertEqual(parent_match.rule_id, parent["id"])
        self.assertEqual(parent_match.season_override, 5)

        both_match = rules.find_tmdb_regex_match(
            "Episode.E01.mkv", "/TV/Specials", "tv"
        )
        self.assertEqual(both_match.rule_id, both["id"])
        self.assertIsNone(rules.find_tmdb_regex_match("Disabled.Show.mkv", "/TV", "tv"))
        self.assertIsNone(rules.find_tmdb_regex_match("Cinema.2026.mkv", "/Movies", "tv"))
        movie_match = rules.find_tmdb_regex_match("Cinema.2026.mkv", "/Movies", "movie")
        self.assertEqual(movie_match.rule_id, movie["id"])

    def test_preview_validates_without_persisting(self):
        from app.modules import tmdb_regex_rules as rules

        preview = rules.preview_rule(
            self.payload(pattern=r"Show[ ._-]+S03", season_override=3),
            "Show.S03E01.mkv",
            "/TV/Show",
            "tv",
        )
        self.assertTrue(preview["matched"])
        self.assertEqual(preview["tmdb_id"], "12345")
        self.assertEqual(preview["season_override"], 3)
        self.assertEqual(rules.list_rules(), [])


if __name__ == "__main__":
    unittest.main()
