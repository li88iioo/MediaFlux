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

    def test_scraper_reexports_same_deterministic_functions(self):
        from app.modules import scraper
        from app.modules.recognition.extractors import deterministic

        self.assertIs(scraper._extract_episode, deterministic._extract_episode)
        self.assertIs(scraper._extract_explicit_season, deterministic._extract_explicit_season)
        self.assertIs(scraper._parse_release_x_position, deterministic._parse_release_x_position)


class GuessItAdapterTests(unittest.TestCase):
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
