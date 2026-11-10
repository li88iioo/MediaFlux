"""目录展示不携带NSFW内部标记，计划仍按真实身份防止混入。"""
from __future__ import annotations

from typing import cast

import unittest
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.naming import MOVIE_DIR_DEFAULT, NamingContext, render_template
from app.modules.organize import OrganizePlan, Organizer, OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.scraper import MatchResult, TMDBScraper
from tests.support import isolated_test_database


class NsfwDirectoryNamingTests(unittest.TestCase):
    def setUp(self):
        self.organizer = Organizer(client=Mock(), scraper=Mock())
        cast(Mock, self.organizer.scraper.get_detail).return_value = {}
        self.rules = OrganizeRules(nsfw_enabled=True, nsfw_category_name="NSFW")
        self.enterContext(patch("app.modules.media_probe.probe_media_profile", return_value=None))

    def match(self, provider="clean_title", external_id="SODAH-003", *, year="", media_type="movie", number=""):
        return MatchResult(
            provider=provider, external_id=external_id, title="SODAH-003", year=year,
            media_type=media_type, confidence=1.0,
            metadata={"number": number} if number else {},
        )

    def plan(self, match, file_id="video"):
        file = GuangYaFile(file_id, "SODAH-003.mp4", False, 100, "etag")
        plan = OrganizePlan(
            file_id=file_id, original_name=file.name, original_path="source",
            match=match, action="move",
        )
        self.organizer._populate_move_plan(
            plan, file, self.rules, match, {}, {}, parsed_season=None,
            automatic=False, media_probe_cache_only=True,
            media_probe_cached_payload="", media_probe_cache_prefetched=True,
        )
        return plan

    def test_nsfw_directory_has_only_title_and_optional_year(self):
        for provider in ("metatube", "clean_title"):
            for media_type in ("movie", "tv"):
                for year in ("", "2024"):
                    with self.subTest(provider=provider, media_type=media_type, year=year):
                        match = self.match(provider, year=year, media_type=media_type)
                        expected = "SODAH-003 (2024)" if year else "SODAH-003"
                        self.assertEqual(self.organizer.build_media_dir(match, self.rules), expected)
                        self.assertEqual(self.organizer._match_identity_key(match), f"{provider}:SODAH-003")

    def test_ordinary_tmdb_directory_keeps_identity_and_omits_empty_year(self):
        for media_type in ("movie", "tv"):
            for year in ("", "2024"):
                with self.subTest(media_type=media_type, year=year):
                    match = MatchResult(tmdb_id="123", title="普通影视", year=year, media_type=media_type)
                    expected = f"普通影视{' (2024)' if year else ''} {{tmdb-123}}"
                    self.assertEqual(self.organizer.build_media_dir(match, self.rules), expected)
                    self.assertFalse(self.plan(match).identity_guard_required)

    def test_year_suffix_does_not_strip_parentheses_from_the_title(self):
        for year in ("", "2024"):
            with self.subTest(year=year):
                context = NamingContext(title="标题 (特别版)", year=year, identity_tag="{tmdb-123}")
                self.assertEqual(
                    render_template(MOVIE_DIR_DEFAULT, context),
                    f"标题 (特别版){' (2024)' if year else ''} {{tmdb-123}}",
                )
        long_name = render_template(
            MOVIE_DIR_DEFAULT, NamingContext(title="中文长标题" * 100, year="2024", identity_tag="{tmdb-123}"),
        )
        self.assertLessEqual(len(long_name.encode("utf-8")), 240)
        self.assertTrue(long_name.endswith(" (2024) {tmdb-123}"))

    def test_actual_unmarked_target_enables_identity_guard(self):
        for provider in ("metatube", "clean_title"):
            with self.subTest(provider=provider):
                plan = self.plan(self.match(provider))
                self.assertEqual(plan.media_root_path, "NSFW/SODAH-003")
                self.assertEqual(plan.target_path, "NSFW/SODAH-003")
                self.assertTrue(plan.identity_guard_required)
                self.assertEqual(self.organizer._plan_identity(plan), ("movie", "nsfw:SODAH-003"))

    def test_batch_without_number_keeps_exact_source_comparison(self):
        first_match = self.match("metatube", "javbus:first")
        second_match = self.match("metatube", "javbus:second")
        first_match.title = second_match.title = "无番号标题"
        first = self.plan(first_match, "first")
        second = self.plan(second_match, "second")
        self.organizer._apply_identity_guards([first, second], identity_history_loader=lambda _plan: set())
        self.assertEqual(first.action, "move")
        self.assertEqual(second.action, "conflict")
        self.assertEqual(second.conflict_decision, "identity_blocked")
        cast(Mock, self.organizer.client.create_dir).assert_not_called()

    def test_history_identity_is_retained_without_visible_marker(self):
        with isolated_test_database():
            db.add_organize_log(
                "guangya", "source", "NSFW/SODAH-003", "archived", "success", "",
                provider="clean_title", external_id="SODAH-003",
                original_parent_id="source", original_name="SODAH-003.mp4", media_type="movie",
            )
            same = self.plan(self.match())
            other = self.plan(self.match("metatube", "javbus:other"))
            self.organizer._apply_identity_guards([same])
            self.organizer._apply_identity_guards([other])
            self.assertEqual(same.action, "move")
            self.assertEqual(other.action, "conflict")
            self.assertEqual(other.conflict_decision, "identity_blocked")
            self.assertEqual(
                self.organizer._historical_root_identities("NSFW/SODAH-003"),
                {("movie", "clean_title:SODAH-003")},
            )

    def test_batch_compares_numbers_across_sources_and_rejects_other_media(self):
        for first_provider, first_id in (("metatube", "JAV321:idpc00001"), ("clean_title", "IDPC-001")):
            with self.subTest(first_provider=first_provider):
                plans = [
                    self.plan(self.match(provider, external_id, number=number, media_type=media_type), str(index))
                    for index, (provider, external_id, number, media_type) in enumerate((
                        (first_provider, first_id, "IDPC-001", "movie"),
                        ("metatube", "JavBus:IDPC-001", "idpc001", "movie"),
                        ("clean_title", "IDPC-001", "IDPC-001", "movie"),
                        ("metatube", "JavBus:IDPC-001", "IDPC-002", "movie"),
                        ("metatube", "JavBus:IDPC-001", "IDPC-001", "tv"),
                        ("tmdb", "123", "IDPC-001", "movie"),
                    ))
                ]
                # 相同展示标题落在同一目录，只有元数据番号和媒体类型能区分。
                for plan in plans:
                    plan.media_root_path = plans[0].media_root_path
                    plan.identity_guard_required = True
                self.organizer._apply_identity_guards(plans, identity_history_loader=lambda _plan: set())
                self.assertEqual([plan.action for plan in plans], ["move"] * 3 + ["conflict"] * 3)
                self.assertTrue(all(plan.conflict_decision == "identity_blocked" for plan in plans[3:]))
                self.assertEqual(self.organizer._match_identity_key(plans[0].match), f"{first_provider}:{first_id}")

    def test_batch_retains_number_evidence_after_an_incomplete_first_match(self):
        unknown = self.plan(self.match("metatube", "legacy"), "unknown")
        assert unknown.match is not None
        unknown.match.title = "无番号标题"
        known = self.plan(self.match("metatube", "legacy", number="SODAH-003"), "known")
        other = self.plan(self.match("metatube", "legacy", number="SODAH-004"), "other")
        same = self.plan(self.match("clean_title", "SODAH-003", number="SODAH-003"), "same")
        plans = [unknown, known, other, same]
        self.organizer._apply_identity_guards(plans, identity_history_loader=lambda _plan: set())
        self.assertEqual([plan.action for plan in plans], ["move", "move", "conflict", "move"])
        self.assertEqual(other.conflict_decision, "identity_blocked")

    def test_production_history_allows_same_number_from_another_provider(self):
        for number, provider, old_id in (
            ("IDPC-001", "metatube", "JAV321:idpc00001"),
            ("DLDSS-561", "metatube", "JAV321:1dldss00561"),
            ("IDPC-001", "clean_title", "IDPC-001"),
        ):
            with self.subTest(number=number, provider=provider), isolated_test_database():
                root = f"NSFW/{number} 已匹配标题"
                db.add_organize_log(
                    "guangya", "source", root, "archived", "success",
                    provider=provider, external_id=old_id, media_type="movie",
                    title=f"{number} 已匹配标题", current_name=f"{number}.mp4",
                    original_parent_id="source", original_name=f"4k688.com@{number}.mp4",
                )
                match = self.match("metatube", f"JavBus:{number}", number=number)
                plan = self.plan(match)
                plan.media_root_path = root
                self.organizer._apply_identity_guards([plan])
                self.assertEqual(plan.action, "move")
                self.assertEqual(self.organizer._historical_root_identities(root), {("movie", f"nsfw:{number}")})
                self.assertEqual(self.organizer._match_identity_key(match), f"metatube:JavBus:{number}")

    def test_history_uses_matched_title_then_current_name_and_falls_back_to_exact_source(self):
        old_id = "JAV321:sodah00003"
        for title, current_name, new_id, expected_key, blocked in (
            ("SODAH-004 人工纠正", "SODAH-003.mp4", old_id, "nsfw:SODAH-004", True),
            ("无番号标题", "SODAH-003.CD1.mp4", "JavBus:SODAH-003", "nsfw:SODAH-003", False),
            ("剧情提到 SODAH-004", "SODAH-003.mp4", "JavBus:SODAH-003", "nsfw:SODAH-003", False),
            ("SODAH-004 剧情提到 FC2-PPV-1234567", "SODAH-003.mp4", old_id, "nsfw:SODAH-004", True),
            ("SODAH-004剧情提到FC2-PPV-1234567", "SODAH-003.mp4", old_id, "nsfw:SODAH-004", True),
            ("剧情提到 SODAH-003", "", "JavBus:SODAH-003", f"metatube:{old_id}", True),
            ("", "剪辑_SODAH-003.mp4", "JavBus:SODAH-003", f"metatube:{old_id}", True),
            ("", "", old_id, f"metatube:{old_id}", False),
            ("", "", "JavBus:SODAH-003", f"metatube:{old_id}", True),
        ):
            with self.subTest(title=title, current_name=current_name, new_id=new_id), isolated_test_database():
                root = "NSFW/SODAH-003"
                db.add_organize_log(
                    "guangya", "source", root, "archived", "success",
                    provider="metatube", external_id=old_id, media_type="movie",
                    title=title, current_name=current_name,
                    original_parent_id="source", original_name="SODAH-003.mp4",
                )
                plan = self.plan(self.match("metatube", new_id, number="SODAH-003"))
                self.organizer._apply_identity_guards([plan])
                self.assertEqual(plan.action, "conflict" if blocked else "move")
                self.assertEqual(self.organizer._historical_root_identities(root), {("movie", expected_key)})

    def test_history_keeps_conflicting_titles_for_the_same_source_identity(self):
        with isolated_test_database():
            for number in ("SODAH-003", "SODAH-004"):
                db.add_organize_log(
                    "guangya", "source", "NSFW/SODAH-003", number, "success",
                    provider="metatube", external_id="JavBus:SODAH-003", media_type="movie",
                    title=number, original_parent_id="source", original_name="SODAH-003.mp4",
                )
            plan = self.plan(self.match("metatube", "JavBus:SODAH-003", number="SODAH-003"))
            self.organizer._apply_identity_guards([plan])
            self.assertEqual(plan.action, "conflict")
            self.assertEqual(plan.conflict_decision, "identity_blocked")

    def test_correction_preview_restores_number_for_title_without_a_leading_code(self):
        with isolated_test_database():
            scraper = TMDBScraper()
            service = OrganizeCorrectionService(client=Mock(), scraper=scraper)
            self.addCleanup(scraper.close)
            self.addCleanup(service.close)
            cast(Mock, service.client.file_info).return_value = GuangYaFile(
                "video", "SODAH-003.mp4", False, 100, "etag", "source",
            )
            log_id = db.add_organize_log(
                "guangya", "source", "SODAH-003.mp4", "video", "skipped",
                original_parent_id="source", original_name="SODAH-003.mp4",
                current_parent_id="source", current_name="SODAH-003.mp4", media_type="movie",
            )
            db.add_organize_log_items(log_id, [{
                "file_id": "video", "role": "video", "original_parent_id": "source",
                "original_name": "SODAH-003.mp4", "current_parent_id": "source",
                "current_name": "SODAH-003.mp4", "size": 100, "etag": "etag", "status": "skipped",
            }])
            match = self.match("metatube", "JavBus:SODAH-003", number="SODAH-003")
            match.title = "剧情提到 SODAH-003"
            with patch.object(service, "_rules_for_log", return_value=self.rules), \
                    patch.object(service, "_match", return_value=match):
                preview = service.preview_reorganize(log_id, "", "movie")
            restored = MatchResult(**preview["match"])
            history_id = db.add_organize_log(
                "guangya", "source", preview["target_path"], "archived", "success",
                provider="metatube", external_id=match.external_id, media_type="movie",
                title="SODAH-003", original_parent_id="source", original_name="SODAH-003.mp4",
            )
            service._verify_nsfw_target_identity(restored, preview["target_path"])
            db.update_organize_log(history_id, title="SODAH-004 人工纠正")
            with self.assertRaisesRegex(ValueError, "历史整理记录"):
                service._verify_nsfw_target_identity(restored, preview["target_path"])
            cast(Mock, service.client.move).assert_not_called()
            cast(Mock, service.client.rename).assert_not_called()

    def test_history_query_keeps_ordinary_tv_identity_aggregated(self):
        with isolated_test_database():
            for episode in (1, 2, 3):
                db.add_organize_log(
                    "guangya", "source", f"TV/同一剧集/Season 01/E{episode}.mkv", str(episode), "success", "123",
                    provider="tmdb", external_id="123", media_type="tv", title=f"第{episode}集",
                    current_name=f"S01E{episode:02d}.mkv", original_parent_id="source", original_name="tv.mkv",
                )
            rows = db.list_organize_root_identities("TV/同一剧集")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["title"], rows[0]["current_name"]), ("", ""))
            self.assertEqual(self.organizer._historical_root_identities("TV/同一剧集"), {("tv", "tmdb:123")})

    def test_old_provider_markers_are_still_recognized(self):
        for tag in ("{clean_title-SODAH-003}", "{metatube-javbus-sodah003}"):
            with self.subTest(tag=tag):
                self.assertEqual(self.organizer._identity_marker(f"NSFW/SODAH-003 () {tag}"), tag.lower())
