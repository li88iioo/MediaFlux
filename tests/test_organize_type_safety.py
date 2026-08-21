"""动画电影分类、显式 TMDB 类型复核与重复整理安全回归。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_execution import execute_organize_plans
from app.modules.scraper import MatchResult, TMDBScraper, extract_recognition_context


class NumericMovieTitleTests(unittest.TestCase):
    def test_numeric_movie_titles_survive_manual_search_cleaning(self):
        scraper = TMDBScraper()

        for title in ("2012", "1917", "1984", "2001", "2046"):
            with self.subTest(title=title):
                self.assertEqual(scraper.clean_title(title), title)

    def test_manual_numeric_title_search_reaches_tmdb_and_scores_candidate(self):
        scraper = TMDBScraper()
        with patch.object(scraper, "search", return_value=[{
            "id": 14161,
            "title": "2012",
            "original_title": "2012",
            "release_date": "2009-10-10",
        }]) as search:
            candidates = scraper.search_candidates("2012", "2009", "movie")

        search.assert_called_once_with("2012", "2009", "movie")
        self.assertEqual(candidates[0].tmdb_id, "14161")
        self.assertEqual(candidates[0].score, 1.0)

    def test_release_numeric_title_is_not_replaced_by_technical_suffix(self):
        context = extract_recognition_context(
            "2012.2009.UHD.BluRay.2160p.x265.10bit.HDR.3Audio-MiniHD.mkv"
        )
        self.assertEqual(context.normalized_title, "2012")
        self.assertEqual(context.filename_year, "2009")

    def test_numeric_prefix_inside_formal_title_is_preserved(self):
        scraper = TMDBScraper()
        self.assertEqual(
            scraper.clean_title("2001.A.Space.Odyssey.1968.mkv"),
            "2001 A Space Odyssey",
        )
        self.assertEqual(
            scraper.clean_title("Blade.Runner.2049.2017.mkv"),
            "Blade Runner 2049",
        )


class ExplicitTmdbTypeSafetyTests(unittest.TestCase):
    @staticmethod
    def _detail(tmdb_id: str, media_type: str):
        rows = {
            ("22843", "tv"): {
                "id": 22843, "name": "International Showtime",
                "first_air_date": "1961-01-01",
            },
            ("22843", "movie"): {
                "id": 22843, "title": "福音战士新剧场版：破",
                "release_date": "2009-06-27",
            },
            ("4977", "tv"): {
                "id": 4977,
                "name": "Makin' It",
                "original_name": "Makin' It",
                "first_air_date": "1979-02-01",
            },
            ("4977", "movie"): {
                "id": 4977,
                "title": "红辣椒",
                "original_title": "パプリカ",
                "release_date": "2006-10-21",
            },
            ("14069", "tv"): {
                "id": 14069, "name": "扪心问诊",
                "first_air_date": "2008-01-28",
            },
            ("14069", "movie"): {
                "id": 14069, "title": "穿越时空的少女",
                "release_date": "2006-07-15",
            },
            ("128", "tv"): {
                "id": 128, "name": "Pasadena",
                "first_air_date": "2001-09-28",
            },
            ("128", "movie"): {
                "id": 128, "title": "幽灵公主",
                "release_date": "1997-07-12",
            },
            ("910850", "movie"): {
                "id": 910850,
                "title": "机动战士高达 闪光的哈萨维 喀耳刻的魔女",
                "release_date": "2026-01-01",
            },
            ("218642", "tv"): {
                "id": 218642,
                "name": "师兄啊师兄",
                "first_air_date": "2023-01-19",
            },
        }
        return rows.get((str(tmdb_id), media_type), {})

    def test_inherited_anime_movies_switch_from_wrong_tv_namespace(self):
        cases = (
            ("22843", "福音战士新剧场版：破", "2009"),
            ("4977", "红辣椒", "2006"),
            ("14069", "穿越时空的少女", "2006"),
            ("128", "幽灵公主", "1997"),
        )
        for tmdb_id, title, year in cases:
            with self.subTest(tmdb_id=tmdb_id):
                scraper = TMDBScraper()
                with patch.object(
                    scraper, "get_detail", side_effect=self._detail
                ) as detail:
                    result = scraper.match(
                        f"{title}.{year}.Remux.2160p.mkv",
                        f"动漫/{title} ({year}) {{tmdb-{tmdb_id}}}",
                    )

                self.assertEqual(
                    (result.tmdb_id, result.title, result.year, result.media_type),
                    (tmdb_id, title, year, "movie"),
                )
                self.assertEqual(
                    detail.call_args_list,
                    [
                        unittest.mock.call(tmdb_id, "tv"),
                        unittest.mock.call(tmdb_id, "movie"),
                    ],
                )

    def test_missing_tv_namespace_falls_back_to_movie_for_inherited_id(self):
        scraper = TMDBScraper()
        with patch.object(scraper, "get_detail", side_effect=self._detail):
            result = scraper.match(
                "机动战士高达.2026.WEB-DL.1080p.mkv",
                "动漫/机动战士高达 闪光的哈萨维 喀耳刻的魔女 "
                "(2026) {tmdb-910850}",
            )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.tmdb_id, "910850")

    def test_single_existing_namespace_keeps_explicit_id_for_custom_title(self):
        scraper = TMDBScraper()

        def detail(tmdb_id: str, media_type: str):
            if media_type == "movie":
                return {
                    "id": int(tmdb_id), "title": "Original Theatrical Title",
                    "release_date": "2020-01-01",
                }
            return {}

        with patch.object(scraper, "get_detail", side_effect=detail):
            result = scraper.match(
                "完全自定义译名.2020.mkv",
                "电影/完全自定义译名 (2020) {tmdb-321}",
            )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.tmdb_id, "321")

    def test_one_year_release_difference_does_not_break_exact_type_match(self):
        scraper = TMDBScraper()

        def detail(tmdb_id: str, media_type: str):
            if media_type == "movie":
                return {
                    "id": int(tmdb_id), "title": "跨年电影",
                    "release_date": "2024-12-31",
                }
            return {
                "id": int(tmdb_id), "name": "Other Show",
                "first_air_date": "2010-01-01",
            }

        with patch.object(scraper, "get_detail", side_effect=detail) as get_detail:
            result = scraper.match(
                "跨年电影.2025.mkv",
                "电影/跨年电影 (2025) {tmdb-654}",
            )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.tmdb_id, "654")
        get_detail.assert_called_once_with("654", "movie")

    def test_episode_position_keeps_tv_fast_path(self):
        scraper = TMDBScraper()
        with patch.object(scraper, "get_detail", side_effect=self._detail) as detail:
            result = scraper.match(
                "师兄啊师兄.2023.S01E155.WEB-DL.2160p.mp4",
                "动漫/师兄啊师兄 (2023) {tmdb-218642}/Season 1",
            )

        self.assertEqual(result.media_type, "tv")
        self.assertEqual(result.tmdb_id, "218642")
        detail.assert_called_once_with("218642", "tv")

    def test_both_namespaces_mismatching_folder_fail_closed(self):
        scraper = TMDBScraper()

        def detail(tmdb_id: str, media_type: str):
            if media_type == "movie":
                return {
                    "id": int(tmdb_id), "title": "Unrelated Movie",
                    "release_date": "2001-01-01",
                }
            return {
                "id": int(tmdb_id), "name": "Unrelated Show",
                "first_air_date": "2002-01-01",
            }

        with patch.object(scraper, "get_detail", side_effect=detail):
            result = scraper.match(
                "目标作品.2026.mkv",
                "动漫/目标作品 (2026) {tmdb-123}",
            )

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(result.matched_by, "tmdb_id_type_check")
        self.assertEqual({item.media_type for item in result.candidates}, {"movie", "tv"})
        self.assertIn("需人工确认类型", result.error)


class AnimationClassificationTests(unittest.TestCase):
    def test_animation_movie_uses_movie_category_but_animation_tv_stays_anime(self):
        organizer = Organizer(client=Mock(), scraper=Mock())
        detail = {
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "release_date": "2006-01-01",
            "first_air_date": "2023-01-01",
        }
        with patch.object(organizer, "_detail_for_match", return_value=detail):
            movie = organizer.classify(MatchResult(
                tmdb_id="4977", title="红辣椒", year="2006", media_type="movie"
            ))
            tv = organizer.classify(MatchResult(
                tmdb_id="218642", title="师兄啊师兄", year="2023", media_type="tv"
            ))

        self.assertEqual(movie[0], "电影")
        self.assertEqual(tv[0], "动漫")


class ReorganizeIdempotencyTests(unittest.TestCase):
    @staticmethod
    def _stats() -> dict:
        return {
            "moved": 0, "renamed": 0, "rename_failed": 0,
            "metadata_moved": 0, "stopped": 0, "skipped": 0,
            "conflict": 0, "failed": 0, "subtitle_moved": 0,
            "subtitle_skipped": 0, "subtitle_reasons": [], "skip_reasons": [],
            "scan_errors": [], "strm_changes": [], "strm_force_full": False,
        }

    def test_same_file_id_is_never_a_replacement_candidate(self):
        organizer = Organizer(client=Mock(), scraper=Mock())
        plan = OrganizePlan(
            file_id="same", original_name="Movie.2026.mkv", original_path="电影",
            match=MatchResult(tmdb_id="1", title="Movie", year="2026", media_type="movie"),
            new_name="Movie.2026.mkv",
        )
        candidate = GuangYaFile(
            "same", "Movie.2026.mkv", False, 100, "etag", "target"
        )

        existing, decision, _note = organizer._resolve_variant_conflict(
            plan, [candidate], OrganizeRules(small_file_mb=0)
        )

        self.assertIsNone(existing)
        self.assertEqual(decision, "new")

    def test_file_already_in_target_is_skipped_without_cloud_writes(self):
        client = Mock()
        client.file_info.return_value = GuangYaFile(
            "video", "Movie.2026.mkv", False, 100, "etag", "target-id"
        )
        organizer = Organizer(client=client, scraper=Mock())
        plan = OrganizePlan(
            file_id="video", original_name="Movie.2026.mkv", original_path="电影",
            original_parent_id="target-id", size=100, etag="etag",
            match=MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie"
            ),
            main_category="电影", new_name="Movie.2026.mkv",
            target_path="电影/Movie (2026) {tmdb-1}",
        )
        stats = self._stats()

        with patch.object(organizer, "_ensure_dir_chain", return_value="target-id"), patch.object(
            organizer, "_write_organize_audit", return_value=1
        ) as audit:
            execute_organize_plans(
                organizer, [plan], OrganizeRules(target_dir_id="archive"),
                stats, {}, source_dir_id="target-id",
            )

        client.move.assert_not_called()
        client.rename.assert_not_called()
        client.delete.assert_not_called()
        client.list_dir.assert_not_called()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(plan.conflict_decision, "already_organized")
        self.assertIn("未执行重复移动", plan.conflict_note)
        self.assertIn("未执行重复移动", audit.call_args.args[1]["error"])


if __name__ == "__main__":
    unittest.main()
