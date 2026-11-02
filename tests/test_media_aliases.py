from __future__ import annotations

from typing import cast
from unittest.mock import Mock, patch

from app import database as db
from app.clients.tmdb import TMDBClient
from app.clients.guangya import GuangYaFile
from app.modules.media_aliases import lookup_manual_aliases, record_manual_confirmation
from app.modules.organize import OrganizeRules, Organizer
from app.modules.scraper import TMDBScraper
from tests.support import IsolatedDatabaseTestCase


class MediaAliasPersistenceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_title_aliases")
            conn.execute("DELETE FROM tmdb_lock")
            conn.execute("DELETE FROM organize_log")

    def test_confirmation_recalls_candidates_even_without_release_year(self):
        record_manual_confirmation(["Virgin Punk Clockwork Girl", "ヴァージン・パンク"],
                                   tmdb_id="1360829", title="Virgin Punk: Clockwork Girl",
                                   year="2025", media_type="movie")
        matches = lookup_manual_aliases(["Virgin Punk Clockwork Girl", "ヴァージン・パンク"],
                                        media_type="movie")
        self.assertEqual([item["tmdb_id"] for item in matches], ["1360829"])
        self.assertEqual(matches[0]["year"], "2025")
        self.assertEqual(lookup_manual_aliases(["Virgin Punk Clockwork Girl"], media_type="tv"), [])

    def test_single_file_choice_does_not_disable_other_identity_or_remake(self):
        for ident, year in (("100", "2025"), ("200", "2025"), ("300", "1999")):
            record_manual_confirmation(["Shared Title"], tmdb_id=ident, title="Shared Title",
                                       year=year, media_type="movie")
        self.assertEqual({item["tmdb_id"] for item in lookup_manual_aliases(
            ["Shared Title"], media_type="movie")}, {"100", "200", "300"})
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_title_aliases WHERE blocked=1").fetchone()[0], 0)

    def scraper(self, details, search_results=()):
        client = Mock(spec=TMDBClient)
        client.api_key = "synthetic-key"
        client.base_url = "https://synthetic.invalid"
        client.config_error = ""
        client.search.return_value = list(search_results)
        client.detail.side_effect = lambda ident, kind: details.get(str(ident), {})
        client.detail_with_alternative_titles.side_effect = client.detail.side_effect
        scraper = TMDBScraper(client=cast(TMDBClient, client))
        self.addCleanup(scraper.close)
        return scraper

    def test_learned_movie_alias_uses_official_detail_without_global_lock(self):
        detail = {"id": 1360829, "title": "Virgin Punk: Clockwork Girl", "release_date": "2025-06-27"}
        scraper = self.scraper({"1360829": detail})
        scraper.confirm("Virgin Punk Clockwork Girl.2025.mkv", "1360829",
                        detail["title"], "2025", "movie")
        result = scraper.match("Virgin Punk Clockwork Girl.1080p.mkv", media_type_hint="movie")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "1360829")
        self.assertFalse(result.locked)
        self.assertEqual(result.metadata["learned_candidate_ids"], ["1360829"])

    def test_wrong_single_file_correction_cannot_poison_other_episode(self):
        correct = {"id": 303287, "name": "一斩苍穹", "original_name": "一斩苍穹", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1, "episode_count": 26}]}
        wrong = {"id": 326695, "name": "择日飞升", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1, "episode_count": 30}]}
        scraper = self.scraper({"303287": correct, "326695": wrong}, [correct])
        scraper.confirm("一斩苍穹 S01E17.mkv", "326695", "择日飞升", "2026", "tv")
        result = scraper.match("一斩苍穹.S01E15.2026.mkv", media_type_hint="tv")
        self.assertEqual(result.tmdb_id, "303287")
        self.assertEqual(result.status, "matched")
        self.assertFalse(result.locked)
        # 原文件仍尊重精确人工锁；不把该文件选择强加给其它集。
        exact = scraper.match("一斩苍穹 S01E17.mkv", media_type_hint="tv")
        self.assertTrue(exact.locked)
        self.assertEqual(exact.tmdb_id, "326695")

    def test_historical_poisoned_alias_does_not_supply_title_evidence(self):
        detail = {"id": 326695, "name": "择日飞升", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1, "episode_count": 30}]}
        record_manual_confirmation(["一斩苍穹"], tmdb_id="326695", title="择日飞升", year="2026", media_type="tv")
        scraper = self.scraper({"326695": detail})
        result = scraper.deterministic_recognize("一斩苍穹.S01E15.2026.mkv", media_type_hint="tv")
        self.assertNotEqual(result.status, "matched")
        self.assertFalse(result.locked)

    def test_yearless_remakes_are_not_resolved_by_latest_confirmation(self):
        details = {str(ident): {"id": ident, "title": "Shared Title", "release_date": year+"-01-01"}
                   for ident, year in ((100, "1999"), (200, "2025"))}
        scraper = self.scraper(details)
        for ident, year in (("100", "1999"), ("200", "2025")):
            record_manual_confirmation(["Shared Title"], tmdb_id=ident, title="Shared Title", year=year, media_type="movie")
        result = scraper.deterministic_recognize("Shared.Title.1080p.mkv", media_type_hint="movie")
        self.assertNotEqual(result.status, "matched")
        exact = scraper.deterministic_recognize("Shared.Title.2025.1080p.mkv", media_type_hint="movie")
        self.assertEqual(exact.tmdb_id, "200")
        self.assertEqual(exact.status, "matched")

    def test_learned_animation_preserves_official_genres(self):
        detail = {"id": 303287, "name": "一斩苍穹", "first_air_date": "2026-01-01",
                  "genres": [{"id": 16, "name": "Animation"}],
                  "seasons": [{"season_number": 1, "episode_count": 26}]}
        record_manual_confirmation(["一斩苍穹"], tmdb_id="303287", media_type="tv")
        scraper = self.scraper({"303287": detail})
        result = scraper.deterministic_recognize("[GM-Team][国漫][一斩苍穹][Gu An][2026][15][1080p].mp4", media_type_hint="tv")
        self.assertEqual(result.status, "matched", result.error)
        self.assertTrue(result.metadata["content_kind_evidence"]["verified"])

    def test_learned_candidate_overflow_cannot_hide_identity_ambiguity(self):
        from app.modules.scraper import MAX_ALIAS_ENRICHMENT_CANDIDATES
        for ident in range(100, 101 + MAX_ALIAS_ENRICHMENT_CANDIDATES):
            record_manual_confirmation(["Shared Movie"], tmdb_id=ident, media_type="movie")
        detail = {"id": 100, "title": "Shared Movie", "release_date": "2025-01-01"}
        scraper = self.scraper({"100": detail}, [detail])
        result = scraper.deterministic_recognize("Shared.Movie.mkv", media_type_hint="movie")
        self.assertNotEqual(result.status, "matched")
        self.assertIn("manual_alias_coverage_incomplete", result.rejected_constraints)

    def test_fuzzy_search_does_not_taint_independently_verified_learned_candidate(self):
        correct = {"id": 200, "title": "Example Movie Dub", "release_date": "2026-01-01"}
        wrong = {"id": 100, "title": "Example Movie", "release_date": "1999-01-01"}
        record_manual_confirmation(["Example Movie Dub"], tmdb_id="200", media_type="movie")
        for fallback in (wrong, correct):
            with self.subTest(fallback=fallback["id"]):
                scraper = self.scraper({"100": wrong, "200": correct})
                cast(Mock, scraper.client.search).side_effect = lambda query, *args: [fallback] if query == "Example Movie" else []
                result = scraper.deterministic_recognize("Example Movie Dub.2026.mkv", media_type_hint="movie")
                self.assertEqual(result.status, "matched", result.error)
                self.assertEqual(result.tmdb_id, "200")
                self.assertNotIn("search_only_variant_used", result.metadata)

    def test_wrong_detail_identity_cannot_supply_aliases_or_pollute_detail_cache(self):
        candidate = {"id": 100, "title": "Unrelated Work", "release_date": "2026-01-01"}
        for identity in ({"id": 200}, {"id": 100, "media_type": "tv"}, {}):
            with self.subTest(identity=identity):
                scraper = self.scraper({"100": candidate}, [candidate])
                cast(Mock, scraper.client.detail_with_alternative_titles).side_effect = None
                cast(Mock, scraper.client.detail_with_alternative_titles).return_value = {
                    **identity, "title": "Actual Movie", "release_date": "2026-01-01",
                    "alternative_titles": {"titles": [{"title": "Query Movie"}]},
                }
                result = scraper.deterministic_recognize("Query.Movie.2026.mkv", media_type_hint="movie")
                self.assertNotEqual(result.status, "matched")
                self.assertNotIn("Query Movie", result.candidates[0].aliases)
                self.assertEqual(scraper.get_detail("100", "movie"), candidate)

    def test_unavailable_known_identity_blocks_guessing_and_recovers_after_failure_ttl(self):
        from app.modules.scraper import _TMDB_FAILURE_CACHE_TTL_SECONDS, RecognitionResult

        for ident in (100, 200):
            record_manual_confirmation(["同名电影"], tmdb_id=ident, media_type="movie")
        first = {"id": 100, "title": "同名电影", "release_date": "2000-01-01"}
        second = {"id": 200, "title": "不同作品", "release_date": "2010-01-01"}
        scraper = self.scraper({"100": first, "200": second}, [first])
        attempts = []

        def fetch(ident, _kind):
            if str(ident) == "200":
                attempts.append(ident)
                if len(attempts) == 1:
                    raise TimeoutError("synthetic detail timeout")
            return first if str(ident) == "100" else second

        cast(Mock, scraper.client.detail_with_alternative_titles).side_effect = fetch
        with patch("app.modules.scraper.time.monotonic", return_value=1000) as clock, patch.object(
            scraper, "_external_hint_fallback", return_value=RecognitionResult(status="matched", tmdb_id="999")
        ) as fallback:
            for tick in (1000, 1001):
                clock.return_value = tick
                result = scraper.match("同名电影.1080p.mkv", media_type_hint="movie")
                self.assertNotEqual(result.status, "matched")
                self.assertIsInstance(result, RecognitionResult)
                assert isinstance(result, RecognitionResult)
                self.assertIn("manual_alias_coverage_incomplete", result.rejected_constraints)
                self.assertNotIn("低于", result.error)
            self.assertEqual(len(attempts), 1)
            fallback.assert_not_called()
            clock.return_value = 1000 + _TMDB_FAILURE_CACHE_TTL_SECONDS + 1
            recovered = scraper.match("同名电影.1080p.mkv", media_type_hint="movie")
            self.assertEqual(recovered.status, "matched", recovered.error)
            self.assertEqual(recovered.tmdb_id, "100")
            self.assertEqual(len(attempts), 2)

    def test_unavailable_learned_details_are_not_reported_as_no_results(self):
        scraper = self.scraper({})
        unknown = scraper.deterministic_recognize("Known.Movie.mkv", media_type_hint="movie")
        self.assertEqual(unknown.status, "no_result")
        record_manual_confirmation(["Known Movie"], tmdb_id="100", media_type="movie")
        unresolved = scraper.deterministic_recognize("Known.Movie.mkv", media_type_hint="movie")
        self.assertEqual(unresolved.status, "request_error")
        self.assertIn("详情", unresolved.error)

    @staticmethod
    def black_clover_detail(first_season_count=170):
        return {
            "id": 73223, "name": "黑色五叶草", "original_name": "ブラッククローバー",
            "first_air_date": "2017-10-03",
            "aliases": ["黑色五葉草", "Black Clover"],
            "seasons": [{"season_number": 1, "episode_count": first_season_count},
                        {"season_number": 2, "episode_count": 26}],
        }

    @staticmethod
    def release(episode, group="ANi"):
        return f"[{group}] 黑色五葉草 Season 2 - {episode} [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"

    def remember_episode(self, target_episode=5, **overrides):
        fields = dict(
            source="guangya", original_path="/", new_path="archive/Season 2",
            file_id="synthetic-video", status="success", tmdb_id="73223",
            original_name=self.release(175), original_parent_id="synthetic-source",
            media_type="tv", season=2, episode=target_episode,
            release_parse={"manual_position": {"version": 1, "source": "manual_correction",
                                              "season": 2, "episode": target_episode}},
        )
        fields.update(overrides)
        return db.add_organize_log(**fields)

    def test_confirmed_continuous_numbering_reaches_next_release_and_exact_lock(self):
        detail = self.black_clover_detail()
        self.remember_episode()
        scraper = self.scraper({"73223": detail}, [detail])
        scraper.confirm(self.release(175), "73223", "黑色五叶草", "2017", "tv", parent_path="/")
        for source, target in ((175, 5), (176, 6), (6, 6)):
            with self.subTest(source=source):
                name = self.release(source)
                parent = "/" if source == 175 else "/MF-503-" + name.removesuffix(".mp4")
                match = scraper.match(name, parent, media_type_hint="tv")
                self.assertEqual(match.status, "matched", match.error)
                self.assertFalse(match.need_confirm)
                parsed = scraper.parse_media(name, parent, match)
                self.assertEqual((parsed.source_season, parsed.source_episode), (2, source))
                self.assertEqual((parsed.effective_season, parsed.effective_episode), (2, target))
                if source > 26:
                    self.assertIn("manual_confirmed", match.metadata["episode_mapping"]["reason"])
                else:
                    self.assertNotIn("episode_mapping", match.metadata)

        # 组内和跨目录缓存只能记住作品，不得把176的S02E06带给177；
        # 缓存也不能跳过本集对人工编号历史的重新核验。
        matched = scraper.match(self.release(176), media_type_hint="tv")
        organizer = Organizer(local_only=True, scraper=scraper)
        self.addCleanup(organizer.close)
        rules = OrganizeRules(target_dir_id="archive", small_file_mb=0, region_split=False,
                              year_split=False, clean_empty=False, link_strm=False, notify_enabled=False)
        for cached in (organizer._identity_neutral_work_match(matched),
                       organizer._task_identity_neutral_match(matched)):
            plan = organizer._plan_one(
                GuangYaFile("synthetic-next", self.release(177), False, 1024, "etag", "source"),
                "/", rules, match_override=cached, automatic=True,
            )
            self.assertEqual(plan.action, "move", plan.note)
            self.assertEqual((plan.season, plan.episode), (2, 7))

    def test_mapping_requires_successful_same_identity_publisher_confirmation(self):
        detail = self.black_clover_detail()
        cases = (
            {"status": "failed"}, {"status": "skipped"},
            {"tmdb_id": "99999"}, {"media_type": "movie"},
            {"original_name": self.release(175, "GM-Team")},
            {"original_name": self.release(175).replace("黑色五葉草", "另一部动画")},
            {"original_name": self.release(175).replace("Season 2", "Season 3")},
            {"release_parse": {}},
        )
        scraper = self.scraper({"73223": detail}, [detail])
        for fields in cases:
            with self.subTest(fields=fields):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM organize_log")
                self.remember_episode(**fields)
                result = scraper.deterministic_recognize(self.release(176), media_type_hint="tv")
                self.assertNotEqual(result.status, "matched")
                self.assertNotIn("episode_mapping", result.metadata)

    def test_changed_tmdb_counts_or_conflicting_manual_positions_cannot_reuse_offset(self):
        self.remember_episode()
        for changed_counts, conflicting in ((169, False), (170, True)):
            with self.subTest(changed_counts=changed_counts, conflicting=conflicting):
                if conflicting:
                    self.remember_episode(target_episode=8)
                detail = self.black_clover_detail(changed_counts)
                scraper = self.scraper({"73223": detail}, [detail])
                result = scraper.deterministic_recognize(self.release(176), media_type_hint="tv")
                self.assertNotEqual(result.status, "matched")
                self.assertNotIn("episode_mapping", result.metadata)

    def test_unconfirmed_overflow_and_exceeding_total_remain_unmapped(self):
        detail = self.black_clover_detail()
        scraper = self.scraper({"73223": detail}, [detail])
        result = scraper.deterministic_recognize(self.release(175), media_type_hint="tv")
        self.assertNotEqual(result.status, "matched")
        self.remember_episode()
        result = scraper.deterministic_recognize(self.release(197), media_type_hint="tv")
        self.assertNotEqual(result.status, "matched")
        self.assertNotIn("episode_mapping", result.metadata)
