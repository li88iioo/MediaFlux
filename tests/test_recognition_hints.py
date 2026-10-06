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

    def test_transient_hint_errors_expire_before_successful_results(self):
        from app.modules.recognition_hints import clear_recognition_hint_cache, search_recognition_hints

        card = MediaCard(provider="douban", external_id="1", media_type="movie", title="Iron Man")
        recovered = SimpleNamespace(items=(card,), providers_attempted=("douban",), errors=())
        for first_result in (
            TimeoutError("temporary"),
            SimpleNamespace(items=(), providers_attempted=("douban",), errors=({"code": "timeout"},)),
            SimpleNamespace(items=(card,), providers_attempted=("douban",), errors=({"code": "timeout"},)),
        ):
            with self.subTest(first_result=type(first_result).__name__):
                clear_recognition_hint_cache()
                service = Mock()
                service.search.side_effect = [first_result, recovered]
                with patch("app.modules.recognition_hints.enabled_hint_providers", return_value=("douban",)), patch(
                    "app.modules.recognition_hints.get_discovery_search_service", return_value=service,
                ), patch("app.modules.recognition_hints.time.monotonic", return_value=100.0) as clock:
                    first = search_recognition_hints("Iron Man", "movie")
                    self.assertTrue(first.errors)
                    clock.return_value = 110.0
                    self.assertTrue(search_recognition_hints("Iron Man", "movie").cached)
                    self.assertEqual(service.search.call_count, 1)
                    clock.return_value = 131.0
                    second = search_recognition_hints("Iron Man", "movie")
                    self.assertFalse(second.errors)
                    self.assertFalse(second.cached)
                    self.assertEqual(second.items, (card,))
                    clock.return_value = 500.0
                    self.assertTrue(search_recognition_hints("Iron Man", "movie").cached)
                    self.assertEqual(service.search.call_count, 2)

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
            metadata={
                "recognition_evidence": {
                    "matched_query": "Iron Man",
                    "matched_title": "钢铁侠",
                },
            },
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
        self.assertEqual(evidence["matched_query"], "Iron Man")
        self.assertEqual(evidence["matched_title"], "钢铁侠")
        self.assertEqual(evidence["source"]["filename_title"], "Iron Man")
        self.assertEqual(evidence["source"]["approved_anchor"], "Iron Man")
        self.assertEqual(evidence["source"]["matched_hint_title"], "Iron Man")
        self.assertEqual(evidence["source"]["anchor_score"], 1.0)
        self.assertEqual(evidence["tmdb"]["id"], "1726")
        self.assertEqual(evidence["tmdb"]["matched_query"], "Iron Man")

    def test_external_hint_cannot_expand_source_into_a_different_work(self):
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        for source, hint in (
            ("Alien", "Alien Nation"),
            ("Sample", "Corps Samples"),
            ("The Thing", "The Thing Returns"),
        ):
            with self.subTest(source=source, hint=hint):
                # Provider 是可控输入，TMDB 查询、评分与二次识别走真实实现。
                candidate = {
                    "id": 42, "title": hint, "original_title": hint,
                    "release_date": "2021-01-01", "media_type": "movie",
                }
                client = Mock(api_key="test-key", base_url="https://tmdb.test/3", config_error="", session=None)
                client.search.return_value = [candidate]
                client.detail.return_value = candidate
                client.detail_with_alternative_titles.return_value = candidate
                scraper = TMDBScraper(client=client)
                context = RecognitionContext(
                    filename=f"{source}.mkv", normalized_title=source,
                    filename_title=source, media_type="movie", title_variants=[source],
                )
                failed = RecognitionResult(
                    media_type="movie", status="low_confidence", need_confirm=True,
                    rejected_constraints=["ambiguous_near_tie"], context=context,
                )
                card = MediaCard(
                    provider="douban", external_id="fixture", media_type="movie",
                    title=hint, original_title=hint, year="2021",
                )
                with patch(
                    "app.modules.recognition_hints.search_recognition_hints",
                    return_value=SimpleNamespace(items=(card,)),
                ):
                    result = scraper._external_hint_fallback(context.filename, "", failed)
                self.assertIs(result, failed)
                self.assertTrue(result.need_confirm)
                client.search.assert_not_called()

    def test_bidirectional_identity_keeps_alias_and_season_evidence(self):
        from app.modules.scraper import _verify_source_title_anchor

        for source, candidates, season in (
            (["Iron Man"], ["钢铁侠", "Iron Man"], None),
            (["我独自升级"], ["我独自升级 2nd Season"], 2),
            (["我独自升级 2nd Season"], ["我独自升级"], 2),
            (["Animatica「北斗之拳 拳王軍雜兵們的輓歌」"], ["北斗之拳 拳王軍雜兵們的輓歌"], 1),
        ):
            with self.subTest(source=source, candidates=candidates):
                self.assertTrue(_verify_source_title_anchor(source, candidates, season=season)[0])
        for source, candidates in ((["Example"], ["Example II"]), (["Example II"], ["Example"])):
            with self.subTest(source=source, candidates=candidates):
                self.assertFalse(_verify_source_title_anchor(source, candidates, season=None)[0])

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
