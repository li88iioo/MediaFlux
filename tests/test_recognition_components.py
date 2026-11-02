"""渐进拆出的识别纯组件及 scraper 兼容门面测试。"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


class RecognitionCleanerTests(unittest.TestCase):
    def test_cleaner_normalizes_and_deduplicates_title_variants(self):
        from app.modules.recognition.cleaner import (
            _comparison_key,
            _split_title_variants,
            _unique_text,
            normalize_release_text,
            strip_media_file_suffix,
        )

        self.assertEqual(strip_media_file_suffix("Demo.Show.mkv"), "Demo.Show")
        self.assertEqual(strip_media_file_suffix("Demo.Show.2026"), "Demo.Show.2026")
        self.assertEqual(normalize_release_text("  Demo___Show  "), "Demo___Show")
        self.assertEqual(_comparison_key("  Amélie／天使爱美丽  "), "amelie 天使爱美丽")
        self.assertEqual(_unique_text([" Demo.Show ", "Demo Show", "示例剧"]), ["Demo.Show", "示例剧"])
        self.assertEqual(
            _split_title_variants("示例剧 | Example Show"),
            ["示例剧 | Example Show", "示例剧", "Example Show"],
        )
        self.assertEqual(
            _split_title_variants("Let's Camp!露营少女/摇曳露营/Yuru Camp"),
            [
                "Let's Camp!露营少女/摇曳露营/Yuru Camp",
                "Let's Camp!露营少女",
                "摇曳露营",
                "Yuru Camp",
                "露营少女",
                "Let's Camp",
            ],
        )
        self.assertEqual(_split_title_variants("Fate/stay night"), ["Fate/stay night"])
        self.assertNotIn(
            "100 III",
            _split_title_variants(
                "灵能百分百 / 路人超能100 Ⅲ / モブサイコ100 III / Mob Psycho 100 Ⅲ"
            ),
        )

    def test_scraper_reexports_same_cleaner_functions(self):
        from app.modules import scraper
        from app.modules.recognition import cleaner

        self.assertIs(scraper._comparison_key, cleaner._comparison_key)
        self.assertIs(scraper._split_title_variants, cleaner._split_title_variants)
        self.assertIs(scraper._unique_text, cleaner._unique_text)


class DeterministicExtractorTests(unittest.TestCase):
    def test_explicit_position_rules_are_available_without_scraper(self):
        from app.modules.recognition.extractors import deterministic

        self.assertEqual(deterministic._extract_explicit_season("Demo S02E03"), 2)
        self.assertEqual(deterministic._extract_episode("Demo S02E03"), 3)
        self.assertEqual(deterministic._extract_explicit_season("示例剧 第二季 第三集"), 2)
        self.assertEqual(deterministic._extract_episode("示例剧 第二季 第三集"), 3)
        self.assertEqual(deterministic._parse_release_x_position("Demo 2x03"), (2, 3, (5, 9)))
        self.assertIsNone(deterministic._parse_release_x_position("Demo 16x9"))
        self.assertTrue(deterministic._has_unaccepted_release_x_position("Demo 16x9"))
        self.assertIsNone(deterministic._extract_episode("Demo 2024 1080p"))

    def test_plain_trailing_episode_requires_explicit_mapping_context(self):
        from app.modules.recognition.extractors import deterministic

        self.assertIsNone(
            deterministic._extract_episode("Fox Spirit Matchmaker 014")
        )
        self.assertEqual(
            deterministic._extract_episode(
                "Fox Spirit Matchmaker 014",
                allow_plain_trailing_episode=True,
            ),
            14,
        )
        self.assertEqual(
            deterministic._extract_episode(
                "Fox Spirit Matchmaker 001 [1080p]",
                allow_plain_trailing_episode=True,
            ),
            1,
        )
        for filename in (
            "Fox Spirit Matchmaker 2024",
            "Fox Spirit Matchmaker 1080p",
            "Fox Spirit Matchmaker 1080 [HEVC]",
            "Fox Spirit Matchmaker x264",
        ):
            with self.subTest(filename=filename):
                self.assertIsNone(
                    deterministic._extract_episode(
                        filename,
                        allow_plain_trailing_episode=True,
                    )
                )

    def test_mikan_attached_season_and_dual_episode_positions(self):
        from app.modules.recognition.extractors import deterministic

        self.assertEqual(
            deterministic._extract_explicit_season("间谍过家家Season 3 - 13 END"),
            3,
        )
        self.assertEqual(
            deterministic._extract_episode("间谍过家家Season 3 - 13 END [1080p]"),
            13,
        )
        self.assertEqual(
            deterministic._extract_explicit_season("我的英雄学院S4 [01(64)]"),
            4,
        )
        self.assertEqual(
            deterministic._extract_episode("我的英雄学院S4 [01(64)] [720P]"),
            1,
        )
        self.assertEqual(
            deterministic._extract_episode("史莱姆 4th Season - 20(92) [1080p]"),
            20,
        )
        self.assertIsNone(
            deterministic._extract_explicit_season("Preseason 3 - 01")
        )
        self.assertIsNone(
            deterministic._extract_explicit_season(
                "Shingeki no Kyojin - The Final Season - 30"
            )
        )
        self.assertEqual(
            deterministic._extract_episode(
                "Shingeki no Kyojin - The Final Season - 30 [1080p]"
            ),
            30,
        )
        for token in ("Season 3", "Season-3", "Season_3", "Season.3"):
            with self.subTest(token=token):
                self.assertEqual(
                    deterministic._extract_explicit_season(f"Demo {token} - 07"),
                    3,
                )

    def test_traditional_numerals_and_fullwidth_episode_boundaries(self):
        from app.modules import scraper
        from app.modules.recognition.extractors import deterministic

        cases = (
            ("示例劇 第兩季 第三話 [1080p]", 2, 3),
            ("示例劇 第二季 第兩集 [1080p]", 2, 2),
            ("Example Show S02E03［1080p］", 2, 3),
            ("Example Show EP03－副标题", None, 3),
        )
        for title, season, episode in cases:
            with self.subTest(title=title):
                self.assertEqual(deterministic._extract_explicit_season(title), season)
                self.assertEqual(deterministic._extract_episode(title), episode)
                self.assertEqual(
                    scraper.parse_release_position(f"{title}.mkv"),
                    {"season": season, "episode": episode, "episode_end": None},
                )

        self.assertEqual(deterministic._extract_explicit_season("第兩季"), 2)
        self.assertIsNone(deterministic._extract_explicit_season("第兩-三季"))
        for title in ("第兩話題", "第兩集錦", "兩個人的世界"):
            with self.subTest(title=title):
                self.assertIsNone(deterministic._extract_episode(title))

    def test_scraper_reexports_same_deterministic_functions(self):
        from app.modules import scraper
        from app.modules.recognition.extractors import deterministic

        self.assertIs(scraper._extract_episode, deterministic._extract_episode)
        self.assertIs(scraper._extract_explicit_season, deterministic._extract_explicit_season)
        self.assertIs(scraper._parse_release_x_position, deterministic._parse_release_x_position)


class GuessItAdapterTests(unittest.TestCase):
    def test_plain_title_numbers_do_not_authorize_source_positions(self):
        from app.modules import scraper

        parser = scraper.TMDBScraper()
        self.addCleanup(parser.close)
        cases = (
            ("Example Movie 13", "/电影/Example Movie 13", "movie", None),
            ("Example Movie 104", "/电影/Example Movie 104", "movie", None),
            ("Example Movie 1408", "/电影/Example Movie 1408", "movie", None),
            ("Example Show 104", "/剧集/Example Show 104/Season 2", "tv", 2),
        )
        for title, parent, media_type, season in cases:
            filename = f"{title}.mkv"
            with self.subTest(filename=filename, parent=parent):
                context = scraper.extract_recognition_context(filename, parent)
                self.assertEqual(
                    (context.normalized_title, context.media_type, context.season, context.episode),
                    (title, media_type, season, None),
                )
                parsed = parser.parse_media(filename, parent)
                self.assertEqual((parsed.source_season, parsed.source_episode), (season, None))
                self.assertEqual(parsed.title, title)
                self.assertEqual(context.folder_title, title)
                self.assertFalse(any(item.kind == "episode" for item in parsed.evidence))
                self.assertEqual(parser.clean_title(filename), title)

        explicit = parser.parse_media("Example Show 104 S02E03.mkv")
        self.assertEqual((explicit.title, explicit.source_season, explicit.source_episode), (
            "Example Show 104", 2, 3,
        ))
        self.assertEqual(
            scraper.parse_release_position("Example Show 014.mkv", tv_episode_mapping_context=True),
            {"season": None, "episode": 14, "episode_end": None},
        )
        grouped = parser.parse_media("[LoliHouse] 05 [1080p].mkv", "/TV/Example Show/Season 2")
        self.assertEqual((grouped.source_season, grouped.source_episode), (2, 5))
        bracketed_title = parser.parse_media("[Movie Title] 13.mkv", "/电影")
        self.assertIsNone(bracketed_title.source_episode)

    def test_structural_number_cleanup_keeps_file_directory_and_release_titles_aligned(self):
        from app.modules.scraper import TMDBScraper, extract_recognition_context

        parser = TMDBScraper()
        self.addCleanup(parser.close)
        for filename, parent, title, season, episode in (
            ("03.mkv", "/剧集/示例剧/第兩季", "示例剧", 2, 3),
            ("示例劇 第二季 第兩話 [1080p].mkv", "", "示例劇", 2, 2),
            ("Example Show S02E03［1080p］.mkv", "", "Example Show", 2, 3),
            ("Example Show Episode-03.mkv", "", "Example Show", None, 3),
            ("2001 A Space Odyssey.1968.mkv", "", "2001 A Space Odyssey", None, None),
        ):
            with self.subTest(filename=filename):
                context = extract_recognition_context(filename, parent)
                parsed = parser.parse_media(filename, parent)
                self.assertEqual(context.normalized_title, title)
                self.assertEqual(parsed.title, title)
                self.assertEqual((parsed.source_season, parsed.source_episode), (season, episode))

    def test_cache_and_copy_isolation_remain_inside_adapter(self):
        import guessit as guessit_module
        from app.modules.recognition.extractors import guessit_adapter

        guessit_adapter._guessit_cached.cache_clear()
        with patch.object(guessit_module, "guessit", return_value={"title": "Demo"}) as mocked:
            first = guessit_adapter._guessit_info("Demo.S01E01.mkv")
            first["title"] = "mutated"
            second = guessit_adapter._guessit_info("Demo.S01E01.mkv")

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(second["title"], "Demo")

    def test_scraper_uses_only_public_guessit_adapter_functions(self):
        from app.modules import scraper
        from app.modules.recognition.extractors import guessit_adapter

        self.assertFalse(hasattr(scraper, "_guessit_cached"))
        self.assertIs(scraper._guessit_info, guessit_adapter._guessit_info)
        self.assertIs(
            scraper._guessit_episode_is_untrusted,
            guessit_adapter._guessit_episode_is_untrusted,
        )


class RecognitionResolverTests(unittest.TestCase):
    def test_marker_conflicts_and_position_validation_fail_closed(self):
        from app.modules.recognition import resolver

        self.assertEqual(
            resolver._resolve_explicit_tmdb_marker("Demo {tmdb-12} (tmdb-34)"),
            ("", True),
        )
        self.assertEqual(
            resolver._resolve_explicit_tmdb_marker("/TV/Demo {tmdb-12}/Season 01", nearest_first=True),
            ("12", False),
        )
        validation = resolver._validate_tmdb_position(
            {"seasons": [{"season_number": 1, "episode_count": 2}]},
            "tv",
            1,
            3,
        )
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["reason"], "episode_out_of_range")
        self.assertIsNone(resolver._strict_non_negative_int(True))
        self.assertIsNone(resolver._strict_non_negative_int(1.0))

    def test_scraper_reexports_same_resolver_functions(self):
        from app.modules import scraper
        from app.modules.recognition import resolver

        self.assertIs(scraper._resolve_explicit_tmdb_marker, resolver._resolve_explicit_tmdb_marker)
        self.assertIs(scraper._validate_tmdb_position, resolver._validate_tmdb_position)
        self.assertIs(scraper._source_year_matches_tmdb, resolver._source_year_matches_tmdb)

    def test_components_import_without_loading_scraper(self):
        script = textwrap.dedent(
            """
            import sys
            import app.modules.recognition.cleaner
            import app.modules.recognition.extractors.deterministic
            import app.modules.recognition.extractors.guessit_adapter
            import app.modules.recognition.resolver
            assert "app.modules.scraper" not in sys.modules
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
