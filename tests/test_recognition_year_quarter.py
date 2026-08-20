"""识别年份、季度与 Romaji 边界（Sprint 5）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 5.1 具体目标集 air_date 年份证据
- Task 5.2 Q1–Q4 / 春夏秋冬番线索只作为候选重排弱证据
- Task 5.3 Romaji 与 split-cour 季集位置组合验证
"""
from __future__ import annotations

import unittest

from app.modules.recognition.resolver import (
    _source_year_matches_tmdb,
    _target_episode_air_year,
    _validate_tmdb_position,
)
from app.modules.scraper import (
    RecognitionContext,
    _air_date_quarter,
    parse_release_quarter,
    score_candidate,
)


def _tv_detail(*, first_air_date: str, seasons: list[dict]) -> dict:
    return {"id": "100", "first_air_date": first_air_date, "seasons": seasons}


class TargetEpisodeAirYearTests(unittest.TestCase):
    """Task 5.1：季首播年不足以判断时，验证具体目标集播出年。"""

    DETAIL = _tv_detail(
        first_air_date="2019-04-01",
        seasons=[
            {"season_number": 1, "air_date": "2019-04-01", "episode_count": 12},
            {"season_number": 3, "air_date": "2024-10-05", "episode_count": 24},
        ],
    )
    SPLIT_COUR = [
        {"episode_number": 12, "air_date": "2024-12-21"},
        {"episode_number": 13, "air_date": "2025-01-11"},
    ]

    def test_series_year_still_matches_without_season_context(self):
        matched, reason = _source_year_matches_tmdb(self.DETAIL, "tv", "2019")

        self.assertTrue(matched)
        self.assertEqual(reason, "series_or_movie_year")

    def test_target_season_year_matches_a_later_season(self):
        matched, reason = _source_year_matches_tmdb(
            self.DETAIL, "tv", "2024", target_season=3,
        )

        self.assertTrue(matched)
        self.assertEqual(reason, "target_season_year")

    def test_cross_year_episode_is_accepted_via_its_own_air_date(self):
        matched, reason = _source_year_matches_tmdb(
            self.DETAIL, "tv", "2025",
            target_season=3, target_episode=13, season_episodes=self.SPLIT_COUR,
        )

        self.assertTrue(matched)
        self.assertEqual(reason, "target_episode_air_year")

    def test_episode_inside_the_season_year_still_reports_season_evidence(self):
        matched, reason = _source_year_matches_tmdb(
            self.DETAIL, "tv", "2024",
            target_season=3, target_episode=12, season_episodes=self.SPLIT_COUR,
        )

        self.assertTrue(matched)
        self.assertEqual(reason, "target_season_year")

    def test_wrong_year_is_still_rejected_with_episode_evidence_available(self):
        matched, reason = _source_year_matches_tmdb(
            self.DETAIL, "tv", "2021",
            target_season=3, target_episode=13, season_episodes=self.SPLIT_COUR,
        )

        self.assertFalse(matched)
        self.assertEqual(reason, "target_season_year_mismatch")

    def test_missing_episode_air_date_does_not_fabricate_a_match(self):
        matched, reason = _source_year_matches_tmdb(
            self.DETAIL, "tv", "2025",
            target_season=3, target_episode=13,
            season_episodes=[{"episode_number": 13, "air_date": ""}],
        )

        self.assertFalse(matched)
        self.assertEqual(reason, "target_season_year_mismatch")

    def test_unavailable_season_episodes_keep_the_previous_verdict(self):
        for payload in (None, [], "not-a-list"):
            with self.subTest(payload=payload):
                matched, reason = _source_year_matches_tmdb(
                    self.DETAIL, "tv", "2025",
                    target_season=3, target_episode=13, season_episodes=payload,
                )

                self.assertFalse(matched)
                self.assertEqual(reason, "target_season_year_mismatch")

    def test_movies_never_use_episode_evidence(self):
        matched, reason = _source_year_matches_tmdb(
            {"release_date": "2019-04-01"}, "movie", "2025",
            target_season=1, target_episode=1,
            season_episodes=[{"episode_number": 1, "air_date": "2025-01-01"}],
        )

        self.assertFalse(matched)
        self.assertEqual(reason, "year_mismatch")

    def test_episode_air_year_helper_requires_an_exact_episode(self):
        episodes = [{"episode_number": 13, "air_date": "2025-01-11"}]

        self.assertEqual(_target_episode_air_year(episodes, 13), "2025")
        self.assertEqual(_target_episode_air_year(episodes, 14), "")
        self.assertEqual(_target_episode_air_year(episodes, None), "")
        self.assertEqual(_target_episode_air_year(None, 13), "")


class ReleaseQuarterClueTests(unittest.TestCase):
    """Task 5.2：季度线索解析。"""

    def test_explicit_quarter_tokens_are_parsed(self):
        for text, expected in (
            ("Show.2024.Q1.1080p.mkv", "Q1"),
            ("Show 2024 q4 WEB-DL", "Q4"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_release_quarter(text), expected)

    def test_seasonal_anime_words_map_to_quarters(self):
        cases = {
            "2024冬季番/E01.mkv": "Q1",
            "2024春番 作品": "Q2",
            "七月番 作品": "Q3",
            "2024秋季番": "Q4",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_release_quarter(text), expected)

    def test_conflicting_clues_are_discarded(self):
        self.assertEqual(parse_release_quarter("2024春番 Q4 作品"), "")

    def test_unrelated_tokens_are_not_quarter_clues(self):
        for text in ("Show.Q5.mkv", "Show.2160p.mkv", "AQ1B", "Q12"):
            with self.subTest(text=text):
                self.assertEqual(parse_release_quarter(text), "")

    def test_air_date_quarter_requires_a_complete_date(self):
        self.assertEqual(_air_date_quarter("2024-01-11"), "Q1")
        self.assertEqual(_air_date_quarter("2024-10-05"), "Q4")
        self.assertEqual(_air_date_quarter("2024"), "")
        self.assertEqual(_air_date_quarter("2024-13-01"), "")
        self.assertEqual(_air_date_quarter(""), "")


class AiringEpisodePositionTests(unittest.TestCase):
    """连载中番剧：episode_count 滞后时用 last/next_episode_to_air 指针放行。

    生产数据里 74% 的人工确认来自「文件集号超出 TMDB 记录范围」，
    其中绝大多数是每周更新、TMDB 集数未跟上的连载番。
    """

    @staticmethod
    def _detail(*, episode_count, last=None, next_=None) -> dict:
        detail: dict = {
            "seasons": [{"season_number": 1, "episode_count": episode_count}],
        }
        if last is not None:
            detail["last_episode_to_air"] = last
        if next_ is not None:
            detail["next_episode_to_air"] = next_
        return detail

    def test_in_range_episode_keeps_strict_verified_reason(self):
        result = _validate_tmdb_position(self._detail(episode_count=12), "tv", 1, 12)

        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "episode_verified")

    def test_episode_covered_by_last_aired_pointer_passes(self):
        detail = self._detail(
            episode_count=12,
            last={"season_number": 1, "episode_number": 24},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 24)

        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "episode_verified_airing")

    def test_next_to_air_pointer_covers_the_upcoming_episode(self):
        detail = self._detail(
            episode_count=20,
            last={"season_number": 1, "episode_number": 21},
            next_={"season_number": 1, "episode_number": 22},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 22)

        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "episode_verified_airing")

    def test_episode_beyond_airing_pointers_stays_blocked(self):
        detail = self._detail(
            episode_count=12,
            last={"season_number": 1, "episode_number": 24},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 25)

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "episode_out_of_range")

    def test_pointer_from_another_season_is_ignored(self):
        detail = self._detail(
            episode_count=12,
            last={"season_number": 2, "episode_number": 24},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 24)

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "episode_out_of_range")

    def test_missing_count_with_pointer_passes_without_it_fails(self):
        covered = self._detail(
            episode_count=None,
            last={"season_number": 1, "episode_number": 8},
        )
        uncovered = self._detail(episode_count=None)

        self.assertTrue(_validate_tmdb_position(covered, "tv", 1, 8)["passed"])
        self.assertEqual(
            _validate_tmdb_position(uncovered, "tv", 1, 8)["reason"],
            "episode_count_missing",
        )

    def test_airing_reason_never_equals_the_strict_proof_reason(self):
        """身份凭证与目标季年份证据只认 episode_verified，禁止被连载放行冒充。"""
        detail = self._detail(
            episode_count=12,
            last={"season_number": 1, "episode_number": 24},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 24)

        self.assertNotEqual(result["reason"], "episode_verified")

    def test_malformed_pointers_are_ignored(self):
        detail = self._detail(
            episode_count=12,
            last="not-a-dict",
            next_={"season_number": "x", "episode_number": "y"},
        )

        result = _validate_tmdb_position(detail, "tv", 1, 13)

        self.assertFalse(result["passed"])


class QuarterReorderingTests(unittest.TestCase):
    """Task 5.2：季度线索只重排候选，不得绕过安全门。"""

    @staticmethod
    def _context(filename: str) -> RecognitionContext:
        return RecognitionContext(
            filename=filename,
            parent_path="",
            normalized_title="作品",
            filename_title="作品",
            media_type="tv",
            season=1,
            episode=1,
        )

    @staticmethod
    def _candidate(air_date: str) -> dict:
        return {
            "id": 1, "media_type": "tv", "name": "作品",
            "original_name": "作品", "first_air_date": air_date,
        }

    def test_matching_quarter_raises_the_candidate_score(self):
        # 标题不是满分，季度加成才能体现在最终分数上（满分候选会被上限截断）。
        candidate = {
            "id": 1, "media_type": "tv", "name": "作品 第二部",
            "original_name": "作品 第二部", "first_air_date": "2024-10-05",
        }
        matching = score_candidate(self._context("作品.2024秋季番.S01E01.mkv"), candidate)
        neutral = score_candidate(self._context("作品.S01E01.mkv"), candidate)

        self.assertGreater(matching.quarter_bonus, 0)
        self.assertEqual(neutral.quarter_bonus, 0)
        self.assertGreater(matching.final_score, neutral.final_score)

    def test_bonus_never_exceeds_the_score_ceiling(self):
        matching = score_candidate(
            self._context("作品.2024秋季番.S01E01.mkv"), self._candidate("2024-10-05"),
        )

        self.assertGreater(matching.quarter_bonus, 0)
        self.assertLessEqual(matching.final_score, 1.0)

    def test_mismatched_quarter_is_not_penalized(self):
        context = self._context("作品.2024秋季番.S01E01.mkv")

        mismatched = score_candidate(context, self._candidate("2024-01-11"))
        neutral = score_candidate(self._context("作品.S01E01.mkv"), self._candidate("2024-01-11"))

        self.assertEqual(mismatched.quarter_bonus, 0)
        self.assertEqual(mismatched.final_score, neutral.final_score)

    def test_quarter_clue_cannot_rescue_a_rejected_candidate(self):
        context = self._context("作品.2024秋季番.S01E01.mkv")
        movie_candidate = {
            "id": 1, "media_type": "movie", "title": "作品",
            "original_title": "作品", "release_date": "2024-10-05",
        }

        breakdown = score_candidate(context, movie_candidate)

        self.assertIn("media_type_mismatch", breakdown.rejected_constraints)
        self.assertEqual(breakdown.quarter_bonus, 0)
        self.assertEqual(breakdown.final_score, 0.0)

    def test_bonus_is_bounded_and_cannot_reach_a_perfect_score_alone(self):
        context = RecognitionContext(
            filename="完全不同的名字.2024秋季番.S01E01.mkv",
            normalized_title="完全不同的名字",
            filename_title="完全不同的名字",
            media_type="tv",
            season=1,
            episode=1,
        )

        breakdown = score_candidate(context, self._candidate("2024-10-05"))

        self.assertLessEqual(breakdown.quarter_bonus, 0.03)
        self.assertLess(breakdown.final_score, 0.9)


if __name__ == "__main__":
    unittest.main()
