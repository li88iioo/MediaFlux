from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.discovery.models import MediaCard


class RecognitionHintTests(unittest.TestCase):
    def tearDown(self):
        from app.modules.recognition_hints import clear_recognition_hint_cache
        clear_recognition_hint_cache()

    def test_provider_switches_are_real_and_tv_only_enables_bangumi(self):
        from app.modules.recognition_hints import enabled_hint_providers

        values = {
            "ORGANIZE_DOUBAN_HINTS_ENABLED": True,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "ORGANIZE_BANGUMI_HINTS_ENABLED": True,
        }
        with patch(
            "app.modules.recognition_hints.get_bool",
            side_effect=lambda key, default=False: values.get(key, default),
        ):
            self.assertEqual(enabled_hint_providers("movie"), ("douban",))
            self.assertEqual(enabled_hint_providers("tv"), ("douban", "bangumi"))

    def test_search_uses_short_budget_and_cache(self):
        from app.modules.recognition_hints import search_recognition_hints

        card = MediaCard(
            provider="douban", external_id="1", media_type="movie",
            title="钢铁侠", original_title="Iron Man", year="2008",
        )
        service = Mock()
        service.search.return_value = SimpleNamespace(
            items=(card,), providers_attempted=("douban",), errors=(),
        )
        values = {
            "ORGANIZE_DOUBAN_HINTS_ENABLED": True,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "ORGANIZE_BANGUMI_HINTS_ENABLED": False,
        }
        with patch(
            "app.modules.recognition_hints.get_bool",
            side_effect=lambda key, default=False: values.get(key, default),
        ), patch(
            "app.modules.recognition_hints.get_discovery_search_service",
            return_value=service,
        ):
            first = search_recognition_hints("钢铁侠", "movie")
            second = search_recognition_hints("钢铁侠", "movie")

        self.assertEqual(first.items, (card,))
        self.assertTrue(second.cached)
        service.search.assert_called_once_with(
            "钢铁侠", 1, ["douban"], timeout_seconds=4.0
        )

    def test_scraper_accepts_only_strict_tmdb_revalidated_hint(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        context = RecognitionContext(
            filename="Iron.Man.2008.mkv", normalized_title="Iron Man",
            filename_title="Iron Man", filename_year="2008",
            media_type="movie", title_variants=["Iron Man"],
        )
        failed = RecognitionResult(
            media_type="movie", status="no_result", need_confirm=True,
            context=context,
        )
        matched = RecognitionResult(
            tmdb_id="1726", title="钢铁侠", year="2008", media_type="movie",
            confidence=0.96, status="matched", need_confirm=False,
        )
        hints = SimpleNamespace(items=(MediaCard(
            provider="douban", external_id="1", media_type="movie",
            title="钢铁侠", original_title="Iron Man", year="2008",
        ),))
        with patch(
            "app.modules.recognition_hints.search_recognition_hints",
            return_value=hints,
        ), patch.object(
            scraper, "_recognize_context", return_value=matched
        ) as recognize:
            result = scraper._external_hint_fallback(
                "Iron.Man.2008.mkv", "", failed
            )

        self.assertIs(result, matched)
        self.assertEqual(result.tmdb_id, "1726")
        self.assertEqual(recognize.call_args.kwargs["match_mode"], "strict")
        evidence = result.metadata["recognition_evidence"]
        self.assertEqual(evidence["kind"], "external_title_hint")
        self.assertEqual(evidence["provider"], "douban")
        self.assertEqual(evidence["external_id"], "1")
        self.assertTrue(evidence["source_anchor_verified"])
        self.assertTrue(evidence["tmdb_revalidated"])
        self.assertEqual(evidence["tmdb_id"], "1726")

    def test_unrelated_external_hint_cannot_redirect_source_title(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        context = RecognitionContext(
            filename="我独自升级 第二季 - 13.mp4",
            normalized_title="我独自升级 第二季",
            filename_title="我独自升级 第二季",
            media_type="tv", season=2, episode=13,
            title_variants=["我独自升级 第二季"],
        )
        failed = RecognitionResult(
            media_type="tv", status="no_result", need_confirm=True, context=context,
        )
        unrelated = MediaCard(
            provider="bangumi", external_id="1", media_type="tv",
            title="我为歌狂", original_title="我为歌狂", year="2001",
        )
        with patch(
            "app.modules.recognition_hints.search_recognition_hints",
            return_value=SimpleNamespace(items=(unrelated,)),
        ), patch.object(scraper, "_recognize_context") as recognize:
            result = scraper._external_hint_fallback(
                "我独自升级 第二季 - 13.mp4", "我独自升级 第二季", failed
            )

        self.assertIs(result, failed)
        recognize.assert_not_called()

    def test_source_related_hint_cannot_validate_unrelated_tmdb_result(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        context = RecognitionContext(
            filename="我独自升级 第二季 - 13.mp4",
            normalized_title="我独自升级",
            filename_title="我独自升级",
            media_type="tv", season=2, episode=13,
            title_variants=["我独自升级"],
        )
        failed = RecognitionResult(
            media_type="tv", status="no_result", need_confirm=True, context=context,
        )
        related_hint = MediaCard(
            provider="bangumi", external_id="1", media_type="tv",
            title="我独自升级", original_title="俺だけレベルアップな件", year="2024",
        )
        unrelated_tmdb = RecognitionResult(
            tmdb_id="110934", title="我为歌狂", year="2001", media_type="tv",
            confidence=0.99, status="matched", need_confirm=False,
        )
        with patch(
            "app.modules.recognition_hints.search_recognition_hints",
            return_value=SimpleNamespace(items=(related_hint,)),
        ), patch.object(scraper, "_recognize_context", return_value=unrelated_tmdb):
            result = scraper._external_hint_fallback(
                "我独自升级 第二季 - 13.mp4", "我独自升级 第二季", failed
            )

        self.assertIs(result, failed)

    def test_preprocessed_hint_cannot_replace_the_raw_source_identity(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        context = RecognitionContext(
            filename="RightTitle.S01E01.mkv", normalized_title="RightTitle",
            filename_title="RightTitle", media_type="tv", season=1, episode=1,
            title_variants=["RightTitle"],
        )
        failed = RecognitionResult(
            media_type="tv", status="no_result", need_confirm=True, context=context,
        )
        hint = MediaCard(
            provider="bangumi", external_id="1", media_type="tv",
            title="RightTitle", original_title="Right Title", year="2026",
        )
        with patch(
            "app.modules.recognition_hints.search_recognition_hints",
            return_value=SimpleNamespace(items=(hint,)),
        ), patch.object(scraper, "_recognize_context") as recognize:
            result = scraper._external_hint_fallback(
                "RightTitle.S01E01.mkv", "", failed,
                source_anchors=["Completely Different Source"],
            )

        self.assertIs(result, failed)
        recognize.assert_not_called()

    def test_position_conflict_does_not_call_external_hints(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        failed = RecognitionResult(
            media_type="tv", status="low_confidence", need_confirm=True,
            rejected_constraints=["tmdb_position_episode_out_of_range"],
            context=RecognitionContext(
                filename="Show.S02E13.mkv", normalized_title="Show",
                filename_title="Show", media_type="tv", season=2, episode=13,
            ),
        )
        with patch(
            "app.modules.recognition_hints.search_recognition_hints"
        ) as hints:
            result = scraper._external_hint_fallback("Show.S02E13.mkv", "Show", failed)

        self.assertIs(result, failed)
        hints.assert_not_called()


if __name__ == "__main__":
    unittest.main()
