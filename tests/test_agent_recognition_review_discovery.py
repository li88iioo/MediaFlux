"""零候选人工只读复核的 TMDB 发现与来源证据。"""
from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import Mock, call, patch

from app.clients.tmdb import TMDBClient
from app.discovery.models import ProviderUnavailable
from app.modules import agent_recognition_review as review
from app.modules.scraper import TMDBScraper
from app.repositories.organize_confirmations import MAX_REVIEW_FILES, RETRY_SELECTED_INDEX_KEY


_TITLE = "有人刚好提起我吗？"
_ENGLISH_TITLE = "Did Someone Happen to Mention Me"
_ID = "300527"


def _payload(*episode_numbers: int) -> dict:
    return {
        "kind": "guangya",
        "identity": "Act 8 A Renowned Actor is Reborn",
        "directory": "/剧集/【高清剧集网发布 www.DDHDTV.com】有人刚好提起我吗？[杜比视界版本][全8集]",
        "rules": {"nsfw_exclusive": False},
        "files": [
            {
                "name": (
                    f"Did.Someone.Happen.to.Mention.Me.S01E{episode:02d}.Act.{episode}."
                    "A.Renowned.Actor.is.Reborn.2160p.NF.WEB-DL.mkv"
                ),
                "file_id": str(episode), "season": 1, "episode": episode,
            }
            for episode in episode_numbers
        ],
        "candidates": [],
        "review_only": True,
        "discover_candidates": True,
    }


def _search_hit(tmdb_id: str = _ID, *, year: str = "2026") -> dict:
    return {
        "id": int(tmdb_id), "name": _TITLE, "original_name": _TITLE,
        "first_air_date": f"{year}-01-20", "genre_ids": [18],
    }


def _detail(tmdb_id: str = _ID, *, year: str = "2026") -> dict:
    return {
        "id": int(tmdb_id), "name": _TITLE, "original_name": _TITLE,
        "first_air_date": f"{year}-01-20", "number_of_episodes": 8,
        "translations": {"translations": [
            {"iso_639_1": "en", "data": {"name": _ENGLISH_TITLE}},
        ]},
    }


def _season(*episode_numbers: int, season: int = 1) -> dict:
    return {
        "season_number": season,
        "episodes": [
            {"episode_number": number, "season_number": season}
            for number in episode_numbers
        ],
    }


class RecognitionReviewDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=TMDBClient)
        self.client.api_key = "offline-testing"
        self.client.config_error = ""
        self.client.search.return_value = [_search_hit()]
        self.client.detail_with_alternative_titles.return_value = _detail()
        self.client.tv_season_detail.return_value = _season(*range(1, 9))
        self.scraper = TMDBScraper(client=self.client)
        self.create_scraper = self.enterContext(patch.object(review, "TMDBScraper", return_value=self.scraper))
        self.close_scraper = self.enterContext(patch.object(self.scraper, "close", wraps=self.scraper.close))
        self.enterContext(patch.object(review.feature_gate, "recognition_review_enabled", return_value=True))
        self.enterContext(patch("app.modules.recognition.formats.active_rules", return_value=[]))
        settings = self.enterContext(patch.object(review.ProviderSettings, "from_config"))
        self.addCleanup(settings.assert_not_called)

    def test_act_identity_does_not_override_source_titles_and_index_follows_verified_array(self):
        payload = _payload(*range(1, 9))
        before = deepcopy(payload)
        wrong_hit = {**_search_hit("301"), "original_name": _ENGLISH_TITLE}
        wrong_detail = {**_detail("301"), "translations": {}, "name": "另一个剧集", "original_name": "另一个剧集"}
        self.client.search.return_value = [wrong_hit, _search_hit()]
        self.client.detail_with_alternative_titles.side_effect = [wrong_detail, _detail()]

        decision = review.review_confirmation_payload(payload)

        self.assertTrue(decision.approved)
        self.assertEqual(decision.candidate_index, 0)
        self.assertEqual(len(decision.discovered_candidates), 1)
        candidate = decision.discovered_candidates[0]
        self.assertEqual(candidate["title"], _TITLE)
        self.assertEqual(
            (candidate["provider"], candidate["external_id"], candidate["tmdb_id"], candidate["year"],
             candidate["media_type"], candidate["support"], candidate["genre_ids"]),
            ("tmdb", _ID, _ID, "2026", "tv", 8, [18]),
        )
        self.assertNotIn("discovered_candidates", decision.audit_payload())
        self.assertEqual(payload, before)
        self.client.search.assert_called_once_with(_ENGLISH_TITLE, "", "tv")
        self.assertEqual(self.client.detail_with_alternative_titles.call_args_list, [call("301", "tv"), call(_ID, "tv")])
        self.client.tv_season_detail.assert_called_once_with(_ID, 1)
        self.close_scraper.assert_called_once_with()

    def test_folder_fallback_reuses_the_same_exact_filename_proof(self):
        self.client.search.side_effect = [[], [_search_hit()]]
        payload = _payload(2)
        payload["files"][0].pop("season")
        payload["files"][0].pop("episode")

        decision = review.review_confirmation_payload(payload)

        self.assertTrue(decision.approved)
        self.assertEqual(self.client.search.call_args_list, [
            call(_ENGLISH_TITLE, "", "tv"), call(self.scraper.clean_title(_TITLE), "", "tv"),
        ])
        self.client.tv_season_detail.assert_called_once_with(_ID, 1)

    def test_folder_hit_cannot_replace_the_actual_file_title(self):
        self.client.search.return_value = [_search_hit()]
        self.client.detail_with_alternative_titles.return_value = {**_detail(), "translations": {}}

        decision = review.review_confirmation_payload(_payload(2))

        self.assertEqual(decision.reason_code, "candidate_title_mismatch")
        self.assertEqual(decision.discovered_candidates, [])
        self.assertEqual(self.client.search.call_count, 2)
        self.client.detail_with_alternative_titles.assert_called_once_with(_ID, "tv")
        self.client.tv_season_detail.assert_not_called()

    def test_missing_eighth_episode_abstains_for_the_whole_group_without_fallback(self):
        self.client.tv_season_detail.return_value = _season(1, 2, 3, 4, 5, 6, 7, 9)

        decision = review.review_confirmation_payload(_payload(*range(1, 9)))

        self.assertEqual(decision.reason_code, "episode_boundary_unverified")
        self.assertIn("S01E08", decision.summary)
        self.assertEqual(decision.discovered_candidates, [])
        self.client.search.assert_called_once()
        self.client.tv_season_detail.assert_called_once_with(_ID, 1)
        self.close_scraper.assert_called_once_with()

    def test_borrowed_scraper_shares_cache_and_each_old_card_checks_its_own_episode(self):
        self.client.tv_season_detail.return_value = _season(*range(1, 8))

        decisions = [
            review.review_confirmation_payload(_payload(episode), discovery_scraper=self.scraper)
            for episode in range(1, 9)
        ]

        self.assertTrue(all(decision.approved for decision in decisions[:7]))
        self.assertEqual(decisions[-1].reason_code, "episode_boundary_unverified")
        self.assertIn("S01E08", decisions[-1].summary)
        self.assertEqual(decisions[-1].discovered_candidates, [])
        self.client.search.assert_called_once()
        self.client.detail_with_alternative_titles.assert_called_once()
        self.client.tv_season_detail.assert_called_once()
        self.create_scraper.assert_not_called()
        self.close_scraper.assert_not_called()

    def test_two_matching_tmdb_works_leave_no_suggestion(self):
        self.client.search.return_value = [_search_hit(), _search_hit("301", year="2025")]
        self.client.detail_with_alternative_titles.side_effect = [_detail(), _detail("301", year="2025")]

        decision = review.review_confirmation_payload(_payload(1))

        self.assertEqual(decision.reason_code, "discovery_ambiguous")
        self.assertIn("多个同名", decision.summary)
        self.assertEqual(decision.discovered_candidates, [])
        self.client.search.assert_called_once()

    def test_search_outage_is_distinct_from_missing_results_and_stops_fallback(self):
        self.client.search.side_effect = ProviderUnavailable("TMDB unavailable")

        decision = review.review_confirmation_payload(_payload(1))

        self.assertEqual(decision.reason_code, "discovery_search_unavailable")
        self.assertIn("稍后重试", decision.summary)
        self.assertEqual(decision.discovered_candidates, [])
        self.client.search.assert_called_once()
        self.client.detail_with_alternative_titles.assert_not_called()

    def test_both_searches_genuinely_empty(self):
        self.client.search.return_value = []

        decision = review.review_confirmation_payload(_payload(1))

        self.assertEqual(decision.reason_code, "no_candidate")
        self.assertIn("未查到", decision.summary)
        self.assertEqual(decision.discovered_candidates, [])
        self.assertEqual(self.client.search.call_count, 2)

    def test_failed_detail_does_not_get_bypassed_by_folder_search(self):
        self.client.detail_with_alternative_titles.side_effect = ProviderUnavailable("TMDB unavailable")

        decision = review.review_confirmation_payload(_payload(1))

        self.assertEqual(decision.reason_code, "candidate_not_revalidated")
        self.assertIn("无法核验", decision.summary)
        self.assertEqual(decision.discovered_candidates, [])
        self.client.search.assert_called_once()

    def test_marker_conflicts_with_search_hit(self):
        payload = _payload(1)
        payload["files"][0]["name"] = payload["files"][0]["name"].replace(".mkv", " {tmdb-301}.mkv")

        decision = review.review_confirmation_payload(payload)

        self.assertEqual(decision.reason_code, "discovery_tmdb_marker_conflict")
        self.assertEqual(decision.discovered_candidates, [])
        self.client.search.assert_called_once()
        self.client.detail_with_alternative_titles.assert_not_called()

    def test_unsafe_or_inconsistent_sources_abstain_before_scraper(self):
        cases = [
            ({"directory": "/sk-simulatedcredential123456/" + _TITLE}, "unsafe_input"),
            ({"files": [{"name": "Some.Other.Show.S01E01.mkv"}, _payload(2)["files"][0]]},
             "discovery_source_title_conflict"),
            ({"files": [{"name": "Same.Show.2026.S01E01.mkv"}, {"name": "Same.Show.2025.S01E02.mkv"}]},
             "discovery_year_conflict"),
            ({"files": [{"name": "Same.Show.S01E01-E02.mkv"}]}, "discovery_multi_episode_file"),
            ({"files": [{"name": "Same.Show.S01E01.mkv", "season": 1, "episode": 2}]},
             "discovery_episode_position_conflict"),
        ]
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                decision = review.review_confirmation_payload({**_payload(1), **overrides})
                self.assertEqual(decision.reason_code, reason)
                self.assertEqual(decision.discovered_candidates, [])
        self.create_scraper.assert_not_called()

    def test_unmarked_or_ineligible_cases_never_enter_discovery_or_episode_research(self):
        cases = [
            {"review_only": False}, {"discover_candidates": False}, {"discover_candidates": 1},
            {"rules": {"nsfw_exclusive": True}}, {"kind": "local_media"},
            {RETRY_SELECTED_INDEX_KEY: 0}, {"files": [{"name": ""}]},
            {"files": [{"name": "Show.S01E01.mkv"}] * (MAX_REVIEW_FILES + 1)},
        ]
        with patch("app.modules.episode_research_service.episode_research_enabled") as research:
            for overrides in cases:
                with self.subTest(overrides=list(overrides)):
                    decision = review.review_confirmation_payload({**_payload(1), **overrides})
                    self.assertEqual(decision.status, "abstained")
                    self.assertEqual(decision.discovered_candidates, [])
            research.assert_not_called()
        self.create_scraper.assert_not_called()
