from __future__ import annotations

import copy
import unicodedata
import unittest
from datetime import date, timedelta
from unittest.mock import Mock

from app.clients.guangya import GuangYaFile
from app.modules.directory_scrape import FixedMatchScraper
from app.modules.episode_mapping import DirectoryEpisodeEvidence
from app.modules.organize import (
    OrganizeRules,
    Organizer,
    _directory_episode_identity_hint,
    _recognition_identity_year,
)
from app.modules.organize_scan import ScannedVideo
from app.modules.scraper import (
    Candidate,
    CandidateScoreBreakdown,
    MatchResult,
    RecognitionContext,
    RecognitionResult,
    TMDBScraper,
)
from tests.support import IsolatedDatabaseTestCase


class _TreeClient:
    def __init__(self, tree: dict[str, list[GuangYaFile]], infos: dict[str, GuangYaFile]):
        self.tree = tree
        self.infos = infos

    def list_dir(self, file_id: str):
        return list(self.tree.get(file_id, []))

    def file_info(self, file_id: str):
        return self.infos.get(file_id)


class DirectoryIdentityCacheTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _rules() -> OrganizeRules:
        return OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

    @staticmethod
    def _scraper() -> TMDBScraper:
        scraper = TMDBScraper()
        scraper.match = Mock(return_value=MatchResult(
            tmdb_id="100",
            title="Example Show",
            year="2026",
            media_type="tv",
            confidence=1.0,
            status="matched",
            matched_by="search",
            provider="tmdb",
            external_id="100",
            directory_identity_cache_eligible=True,
        ))
        scraper.get_detail = Mock(return_value={
            "genres": [],
            "origin_country": ["US"],
            "seasons": [{"season_number": 1, "episode_count": 12}],
        })
        return scraper

    @staticmethod
    def _merged_cour_season_detail(*segment_lengths: int) -> dict:
        starts = (date(2026, 1, 11), date(2026, 7, 5), date(2027, 1, 10))
        episodes = []
        episode_number = 1
        for segment_index, length in enumerate(segment_lengths):
            started_on = starts[segment_index]
            for offset in range(length):
                episodes.append({
                    "episode_number": episode_number,
                    "air_date": (started_on + timedelta(days=7 * offset)).isoformat(),
                })
                episode_number += 1
        return {"season_number": 1, "episodes": episodes}

    def _fixed_cour_confirmation(
        self,
        *,
        source_season: int,
        source_episode_end: int,
        season_detail: dict,
        target_season: int = 1,
    ):
        show = GuangYaFile(
            "show",
            f"Example Show S{source_season:02d}E01-E{source_episode_end:02d}",
            True,
            parent_id="source",
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Example.Show.S{source_season:02d}E{episode:02d}.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(1, source_episode_end + 1)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        delegate = TMDBScraper()
        delegate.get_tv_season_detail = Mock(return_value=copy.deepcopy(season_detail))
        scraper = FixedMatchScraper(
            delegate,
            MatchResult(
                tmdb_id="82684",
                external_id="82684",
                provider="tmdb",
                title="Example Show",
                year="2026",
                media_type="tv",
                confidence=1.0,
                status="matched",
                need_confirm=False,
                matched_by="manual",
            ),
            {
                "id": 82684,
                "genres": [{"id": 16}],
                "origin_country": ["JP"],
                "first_air_date": "2026-01-11",
                "seasons": [{
                    "season_number": target_season,
                    "episode_count": len(season_detail.get("episodes") or []),
                }],
            },
            map_source_positions=True,
        )
        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )
        return plans, stats, delegate

    @staticmethod
    def _verified_identity_scraper(
        *,
        probe_season: int,
        seasons: list[dict],
        title: str = "Example Show",
        probe_episode: int = 1,
    ) -> TMDBScraper:
        episode_count = next(
            int(item["episode_count"])
            for item in seasons
            if int(item["season_number"]) == probe_season
        )
        validation = {
            "required": True,
            "passed": True,
            "season": probe_season,
            "episode": probe_episode,
            "episode_count": episode_count,
            "reason": "episode_verified",
        }
        seed_match = MatchResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title=title,
            year="2018",
            media_type="tv",
            confidence=0.85,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="匹配置信度 85% 低于严格模式阈值 90%",
            metadata={
                "verified_automatic_identity_proof": {
                    "version": 2,
                    "kind": "tmdb_tv_episode_identity",
                    "provider": "tmdb",
                    "external_id": "82684",
                    "media_type": "tv",
                    "confidence": 0.85,
                    "recognition_threshold": 0.9,
                    "automatic_match_preset": "balanced",
                    "global_threshold": 0.9,
                    "strong_title_score": 0.85,
                    "candidate_count": 1,
                    "candidate_gap": 1.0,
                    "decision_constraints": [],
                    "selected_constraints": [],
                    "expected_year": "",
                    "candidate_year": "2018",
                    "source_title_key": "tenshishitaraslimedattaken",
                    "matched_title_key": "tenshishitaraslimedattaken",
                    "source_position": {
                        "season": probe_season,
                        "episode": probe_episode,
                    },
                    "target_position": {
                        "season": probe_season,
                        "episode": probe_episode,
                    },
                    "position_validation": validation,
                },
            },
        )
        scraper = TMDBScraper()
        scraper.match = Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(seed_match))
        scraper.get_detail = Mock(return_value={
            "id": 82684,
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2018-10-02",
            "seasons": copy.deepcopy(seasons),
        })
        return scraper

    def test_auxiliary_hint_result_is_never_directory_cache_eligible(self):
        match = MatchResult(
            tmdb_id="100", external_id="100", provider="tmdb",
            title="Example Show", media_type="tv", confidence=1.0,
            status="matched", matched_by="bangumi_hint",
            directory_identity_cache_eligible=False,
        )

        self.assertFalse(Organizer._cacheable_directory_match(match))

    @staticmethod
    def _low_confidence_package_scraper(
        *,
        episode_count: int = 13,
        multiple_candidates: bool = False,
    ) -> TMDBScraper:
        """构造真实 Boruto 低分形态，验证目录级证明而非 mock 高分缓存。"""
        primary = Candidate(
            tmdb_id="70881",
            external_id="70881",
            provider="tmdb",
            title="博人传 火影忍者新时代",
            original_title="BORUTO-ボルト- NARUTO NEXT GENERATIONS",
            year="2017",
            media_type="tv",
            score=0.829,
            score_breakdown=CandidateScoreBreakdown(
                title_score=0.6,
                original_title_score=0.814,
                final_score=0.829,
                matched_title="BORUTO-ボルト- NARUTO NEXT GENERATIONS",
            ),
        )
        candidates = [primary]
        if multiple_candidates:
            candidates.append(Candidate(
                tmdb_id="99999",
                external_id="99999",
                provider="tmdb",
                title="Boruto Alternate",
                original_title="Boruto Alternate",
                year="2017",
                media_type="tv",
                score=0.821,
                score_breakdown=CandidateScoreBreakdown(
                    title_score=0.82,
                    final_score=0.821,
                    matched_title="Boruto Alternate",
                ),
            ))
        result = RecognitionResult(
            tmdb_id="70881",
            external_id="70881",
            provider="tmdb",
            title="博人传 火影忍者新时代",
            year="2017",
            media_type="tv",
            confidence=0.829,
            status="matched",
            matched_by="search",
            threshold=0.6,
            candidates=candidates,
            context=RecognitionContext(
                filename="Boruto.S01E01.mkv",
                parent_path="[台配] 博人传 -火影次世代-",
                normalized_title="Boruto",
                filename_title="Boruto",
                folder_title="博人传 火影次世代",
                media_type="tv",
                season=1,
                episode=1,
                title_variants=["Boruto", "博人传 火影次世代"],
            ),
            query_variants=["Boruto", "博人传 火影次世代"],
            threshold_decision={
                "threshold": 0.6,
                "score": 0.829,
                "passed": True,
                "reason": "score_met",
            },
        )
        scraper = TMDBScraper()
        scraper.match = Mock(side_effect=lambda *_args, **_kwargs: copy.deepcopy(result))
        scraper.get_detail = Mock(return_value={
            "id": 70881,
            "name": "博人传 火影忍者新时代",
            "first_air_date": "2017-04-05",
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": episode_count}],
        })
        return scraper

    @staticmethod
    def _boruto_tree(episodes: list[int], *, duplicate_first: bool = False):
        show = GuangYaFile(
            "show", "[台配] 博人传 -火影次世代-", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}-{index}",
                f"Boruto - {episode:03d}.mkv",
                False,
                1024,
                f"etag-{episode}-{index}",
                "show",
            )
            for index, episode in enumerate(episodes)
        ]
        if duplicate_first:
            files.append(GuangYaFile(
                "e1-duplicate", "Boruto - 001 v2.mkv", False,
                2048, "etag-1-duplicate", "show",
            ))
        return _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )

    def test_directory_identity_expands_only_compatible_parent_title(self):
        parent = (
            "[台配] 博人传-火影次世代- "
            "(Boruto: Naruto Next Generations) (Episode 1-156) "
            "(Web 1920x1080 AVC AAC)"
        )
        self.assertEqual(
            _directory_episode_identity_hint("Boruto - 001.mkv", parent),
            "Boruto: Naruto Next Generations",
        )
        self.assertEqual(
            _directory_episode_identity_hint("Boruto - 001.mkv", "Unrelated Show"),
            "Boruto",
        )
        self.assertEqual(
            _directory_episode_identity_hint("Boruto - 001.mkv", "Boruto"),
            "Boruto",
        )
        self.assertEqual(
            _directory_episode_identity_hint("Dune - 01.mkv", "Dune Prophecy"),
            "Dune",
        )

    def test_directory_identity_year_uses_only_parsed_year_evidence(self):
        self.assertEqual(
            _recognition_identity_year(
                "A Certain Magical Index 2008 S01E01 1080p.mkv",
                "A Certain Magical Index",
            ),
            "2008",
        )
        self.assertEqual(
            _recognition_identity_year(
                "Example Show S01E01 1080p x265.mkv",
                "Example Show [01-12] [2160p]",
            ),
            "",
        )

    def test_directory_identity_recognition_preserves_source_year(self):
        show = GuangYaFile(
            "show", "A Certain Magical Index", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"A Certain Magical Index 2008 S01E{episode:02d} 1080p.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(1, 4)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()

        Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(
            scraper.match.call_args.args[0],
            "A Certain Magical Index.2008.S01E03.mkv",
        )

    def test_episode_evidence_isolated_by_title_within_same_directory(self):
        first = ScannedVideo(
            GuangYaFile("a", "Example Show - 01.mkv", False, parent_id="mixed"),
            "Mixed",
        )
        second = ScannedVideo(
            GuangYaFile("b", "Example Show - 02.mkv", False, parent_id="mixed"),
            "Mixed",
        )
        unrelated = ScannedVideo(
            GuangYaFile("c", "Unrelated Movie.mkv", False, parent_id="mixed"),
            "Mixed",
        )

        first_key = Organizer._episode_evidence_group_key(first, "Root/Mixed")
        second_key = Organizer._episode_evidence_group_key(second, "Root/Mixed")
        unrelated_key = Organizer._episode_evidence_group_key(unrelated, "Root/Mixed")

        self.assertEqual(first_key, second_key)
        self.assertNotEqual(first_key, unrelated_key)

    def test_korean_episode_evidence_keeps_titles_isolated(self):
        first = ScannedVideo(
            GuangYaFile("ko-a", "오징어 게임 - 01.mkv", False, parent_id="mixed"),
            "Mixed",
        )
        second = ScannedVideo(
            GuangYaFile("ko-b", "오징어 게임 - 02.mkv", False, parent_id="mixed"),
            "Mixed",
        )
        unrelated = ScannedVideo(
            GuangYaFile("ko-c", "더 글로리 - 01.mkv", False, parent_id="mixed"),
            "Mixed",
        )

        first_key = Organizer._episode_evidence_group_key(first, "Root/Mixed")
        second_key = Organizer._episode_evidence_group_key(second, "Root/Mixed")
        unrelated_key = Organizer._episode_evidence_group_key(unrelated, "Root/Mixed")

        decomposed = ScannedVideo(
            GuangYaFile(
                "ko-d",
                unicodedata.normalize("NFD", "오징어 게임 - 03.mkv"),
                False,
                parent_id="mixed",
            ),
            "Mixed",
        )
        decomposed_key = Organizer._episode_evidence_group_key(decomposed, "Root/Mixed")

        self.assertEqual(first_key, "Mixed\x1f오징어게임")
        self.assertEqual(first_key, second_key)
        self.assertEqual(first_key, decomposed_key)
        self.assertNotEqual(first_key, unrelated_key)
        self.assertNotIn("__unknown__", first_key)

    def test_same_directory_reuses_identity_but_keeps_each_episode_position(self):
        show = GuangYaFile("show", "Example Show", True, parent_id="source")
        files = [
            GuangYaFile(f"e{episode}", f"Example Show - {episode:02d}.mkv", False,
                        1024, f"etag-{episode}", "show")
            for episode in range(1, 4)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(stats["directory_identity_cache_hits"], 2)
        self.assertEqual(stats["directory_identity_cache_groups"], 1)
        self.assertEqual([plan.episode for plan in plans], [1, 2, 3])
        self.assertEqual(
            [plan.new_name for plan in plans],
            [
                "Example Show.2026.S01E01.mkv",
                "Example Show.2026.S01E02.mkv",
                "Example Show.2026.S01E03.mkv",
            ],
        )

    def test_directory_identity_recognition_preserves_explicit_source_season(self):
        root_name = (
            "Mushoku Tensei III - Isekai Ittara Honki Dasu "
            "[TV-3] [2026] [WEBRip] [1080p] [RUS + JAP]"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Mushoku Tensei III - {episode:02d} "
                "(WEBRip 1920x1080 x264 AAC Rus + Jap).mkv",
                False,
                1024,
                f"etag-{episode}",
                "source",
            )
            for episode in range(1, 8)
        ]
        client = _TreeClient(
            {"source": files, "archive": []},
            {
                "source": GuangYaFile("source", root_name, True, parent_id="0"),
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.get_detail.return_value["seasons"] = [
            {"season_number": 3, "episode_count": 14},
        ]

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 1)
        recognition_name = scraper.match.call_args.args[0]
        self.assertTrue(
            recognition_name.endswith(".S03E01.mkv"),
            recognition_name,
        )
        self.assertNotIn(".S01E01.", recognition_name)
        self.assertEqual(stats["directory_identity_cache_hits"], 6)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(3, episode) for episode in range(1, 8)],
        )
        self.assertTrue(all(plan.action == "move" for plan in plans))

    def test_guangya_plan_uses_shared_unicode_roman_season_parser(self):
        filename = (
            "[ANi] Clevatess Ⅱ－魔獸之王與虛假的勇者傳承－ - 08 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )
        episode = GuangYaFile(
            "e8", filename, False, 1024, "etag-8", "source",
        )
        client = _TreeClient(
            {"source": [episode], "archive": []},
            {
                "source": GuangYaFile("source", "下载", True, parent_id="0"),
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = TMDBScraper()
        scraper.match = Mock(return_value=MatchResult(
            tmdb_id="258348",
            external_id="258348",
            provider="tmdb",
            title="克雷瓦提斯-魔兽之王与婴儿与尸之勇者-",
            year="2025",
            media_type="tv",
            confidence=1.0,
            status="matched",
            matched_by="search",
        ))
        scraper.get_detail = Mock(return_value={
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(len(plans), 1)
        self.assertEqual((plans[0].season, plans[0].episode), (2, 8))
        self.assertTrue(plans[0].target_path.endswith("/Season 2"))
        self.assertIn("S02E08", plans[0].new_name)

    def test_build_plans_preserves_verified_absolute_episode_mapping(self):
        show = GuangYaFile("show", "One Piece", True, parent_id="source")
        episode = GuangYaFile(
            "e1173", "One Piece - 1173.mkv", False,
            1024, "etag-1173", "show",
        )
        client = _TreeClient(
            {"source": [show], "show": [episode], "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        mapping = {
            "source_season": None,
            "source_episode": 1173,
            "target_season": 23,
            "target_episode": 18,
            "mode": "absolute",
            "reason": "absolute_numbering_rolled_over_tmdb_seasons",
            "confidence": 0.95,
            "range_start": None,
            "range_end": None,
            "changed": True,
            "label": "按绝对集数映射",
        }
        match = MatchResult(
            tmdb_id="37854",
            external_id="37854",
            provider="tmdb",
            title="One Piece",
            year="1999",
            media_type="tv",
            confidence=1.0,
            status="matched",
            matched_by="search",
            preprocess_evaluated=True,
            effective_season=23,
            effective_episode=18,
            metadata={"episode_mapping": mapping},
        )
        scraper = TMDBScraper()
        scraper.match = Mock(return_value=match)
        scraper.get_detail = Mock(return_value={
            "id": 37854,
            "genres": [{"name": "Animation"}],
            "origin_country": ["JP"],
            "first_air_date": "1999-10-20",
            "seasons": [{"season_number": 23, "episode_count": 18}],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.action, "move")
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual((plan.season, plan.episode), (23, 18))
        self.assertEqual((plan.source_season, plan.source_episode), (None, 1173))
        self.assertIsNotNone(plan.episode_mapping)
        self.assertEqual(plan.episode_mapping.mode, "absolute")
        self.assertTrue(plan.episode_mapping.changed)
        self.assertEqual(
            plan.match.metadata["episode_mapping"]["target_episode"], 18
        )

    def test_cached_identity_revalidates_each_episode_against_tmdb(self):
        show = GuangYaFile("show", "Example Show", True, parent_id="source")
        files = [
            GuangYaFile("e12", "Example Show - 12.mkv", False, 1024, "e12", "show"),
            GuangYaFile("e13", "Example Show - 13.mkv", False, 1024, "e13", "show"),
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(stats["directory_identity_cache_hits"], 1)
        self.assertEqual(plans[0].action, "move")
        self.assertEqual(plans[1].action, "skip")
        self.assertTrue(plans[1].match.need_confirm)
        self.assertIn("超出 TMDB", plans[1].note)
        self.assertEqual(
            plans[1].match.metadata["final_position_validation"]["reason"],
            "episode_out_of_range",
        )

    def test_source_s01e01_to_e24_rolls_over_into_tmdb_season_two(self):
        show = GuangYaFile("show", "Example Show S01E01-E24", True, parent_id="source")
        files = [
            GuangYaFile(
                f"e{episode}", f"Example Show S01E{episode:02d}.mkv", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(1, 25)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.get_detail.return_value["seasons"] = [
            {"season_number": 1, "episode_count": 10},
            {"season_number": 2, "episode_count": 14},
        ]

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        expected = [
            *[(1, episode) for episode in range(1, 11)],
            *[(2, episode) for episode in range(1, 15)],
        ]
        self.assertEqual([(plan.season, plan.episode) for plan in plans], expected)
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["conflict"], 0)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(stats["directory_identity_cache_hits"], 23)
        self.assertEqual(plans[10].source_episode, 11)
        self.assertEqual(plans[10].episode_mapping.mode, "absolute")
        self.assertTrue(plans[10].target_path.endswith("/Season 2"))

    def test_bare_episode_pack_rolls_over_across_tmdb_seasons(self):
        # Mapper 单测用真实长篇规模覆盖 001-156；这里用小季容量验证
        # Organizer 的 parse -> evidence -> mapping -> final validation 整条链，
        # 避免制造 O(n²) 冲突检查拖慢全量测试。
        show = GuangYaFile(
            "show",
            "[台配] 博人传-火影次世代- "
            "(Boruto: Naruto Next Generations) (Episode 1-006) "
            "(Web 1920x1080 AVC AAC)",
            True,
            parent_id="source",
        )
        files = [
            GuangYaFile(
                f"e{episode}", f"Boruto - {episode:03d}.mkv", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(1, 7)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.match.return_value.title = "Boruto: Naruto Next Generations"
        scraper.get_detail.return_value["seasons"] = [
            {"season_number": 1, "episode_count": 2},
            {"season_number": 2, "episode_count": 2},
            {"season_number": 3, "episode_count": 2},
        ]

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(len(plans), 6)
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["conflict"], 0)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(
            scraper.match.call_args.args[0],
            "Boruto: Naruto Next Generations.S01E06.mkv",
        )
        self.assertEqual(stats["directory_identity_cache_hits"], 5)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)],
        )
        self.assertEqual(plans[2].source_episode, 3)
        self.assertEqual(plans[2].episode_mapping.mode, "absolute")
        self.assertTrue(plans[2].target_path.endswith("/Season 2"))
        self.assertTrue(plans[4].target_path.endswith("/Season 3"))

    def test_publisher_s02_pack_rebases_to_second_segment_of_merged_tmdb_season(self):
        show = GuangYaFile(
            "show", "大主宰 第二季 S02E01-E33", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}", f"大主宰.S02E{episode:02d}.mp4", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(1, 34)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "大主宰", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.match.return_value.tmdb_id = "226045"
        scraper.match.return_value.external_id = "226045"
        scraper.match.return_value.title = "大主宰"
        scraper.get_detail.return_value.update({
            "id": 226045,
            "name": "大主宰",
            "first_air_date": "2023-06-30",
            "seasons": [{"season_number": 1, "episode_count": 104}],
        })
        episodes = []
        first_start = date(2023, 6, 30)
        second_start = date(2026, 1, 9)
        for episode in range(1, 105):
            if episode <= 52:
                aired_on = first_start + timedelta(days=7 * (episode - 1))
            else:
                aired_on = second_start + timedelta(days=7 * (episode - 53))
            # 兼容 TMDB 同批上线时连续集使用同一天的真实数据形态。
            if episode in {2, 3}:
                aired_on = first_start
            if episode in {54, 55}:
                aired_on = second_start
            episodes.append({
                "episode_number": episode,
                "air_date": aired_on.isoformat(),
            })
        scraper.get_tv_season_detail = Mock(return_value={
            "season_number": 1,
            "episodes": episodes,
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual(len(plans), 33)
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, episode) for episode in range(53, 86)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.reason
            == "publisher_cour_mapped_to_merged_tmdb_season"
            for plan in plans
        ))
        scraper.get_tv_season_detail.assert_called()

    def test_source_s02e13_to_e24_rebases_to_tmdb_second_season(self):
        show = GuangYaFile("show", "Example Show S02E13-E24", True, parent_id="source")
        files = [
            GuangYaFile(
                f"e{episode}", f"Example Show S02E{episode:02d}.mkv", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(13, 25)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.get_detail.return_value["seasons"] = [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(2, episode) for episode in range(1, 13)],
        )
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["conflict"], 0)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(stats["directory_identity_cache_hits"], 11)
        self.assertEqual(plans[0].source_episode, 13)
        self.assertEqual(plans[0].episode_mapping.mode, "season_continuous")
        self.assertEqual(plans[-1].new_name, "Example Show.2026.S02E12.mkv")

    def test_explicit_parent_tmdb_second_season_continued_numbers_rebase_safely(self):
        show = GuangYaFile(
            "show", "我独自升级 第二季", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}", f"我独自升级 第二季 - {episode:02d}.mp4", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(13, 26)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile(
                    "source", "我独自升级 tmdb127532", True, parent_id="0"
                ),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = TMDBScraper()
        scraper.get_detail = Mock(return_value={
            "id": 127532,
            "name": "我独自升级",
            "first_air_date": "2024-01-07",
            "genres": [{"name": "Animation"}],
            "origin_country": ["JP"],
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 13},
            ],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(stats["need_confirm"], 0)
        self.assertTrue(all(plan.match.tmdb_id == "127532" for plan in plans))
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(2, episode) for episode in range(1, 14)],
        )
        self.assertEqual(plans[0].new_name, "我独自升级.2024.S02E01.mp4")
        self.assertEqual(plans[-1].new_name, "我独自升级.2024.S02E13.mp4")

    def test_external_or_ai_hint_match_is_not_reused_for_the_directory(self):
        show = GuangYaFile("show", "我独自升级 第二季", True, parent_id="source")
        files = [
            GuangYaFile(
                f"e{episode}", f"我独自升级 第二季 - {episode:02d}.mp4", False,
                1024, f"etag-{episode}", "show",
            )
            for episode in range(13, 16)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._scraper()
        scraper.match.return_value.matched_by = "bangumi_hint"
        scraper.get_detail.return_value["seasons"] = [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]

        _plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 3)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)
        self.assertEqual(stats["directory_identity_cache_groups"], 0)

    def test_source_root_bare_tmdb_marker_is_inherited_by_nested_files(self):
        show = GuangYaFile("show", "第二季", True, parent_id="source")
        episode = GuangYaFile(
            "e13", "我独自升级 第二季 - 13.mp4", False, 1024, "etag-13", "show"
        )
        client = _TreeClient(
            {"source": [show], "show": [episode], "archive": []},
            {
                "source": GuangYaFile(
                    "source", "我独自升级 tmdb127532", True, parent_id="0"
                ),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = TMDBScraper()
        scraper.get_detail = Mock(return_value={
            "id": 127532,
            "name": "我独自升级",
            "first_air_date": "2024-01-07",
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["KR"],
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 13},
            ],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(stats["matched"], 1)
        self.assertEqual(plans[0].match.tmdb_id, "127532")
        self.assertNotEqual(plans[0].match.tmdb_id, "110934")

    def test_different_titles_and_sibling_directories_do_not_share_identity(self):
        first = GuangYaFile("first", "First", True, parent_id="source")
        second = GuangYaFile("second", "Second", True, parent_id="source")
        tree = {
            "source": [first, second],
            "first": [
                GuangYaFile("a1", "Alpha Show - 01.mkv", False, 1024, "a1", "first"),
                GuangYaFile("b1", "Beta Show - 02.mkv", False, 1024, "b1", "first"),
            ],
            "second": [
                GuangYaFile("a2", "Alpha Show - 03.mkv", False, 1024, "a2", "second"),
            ],
            "archive": [],
        }
        infos = {
            "source": GuangYaFile("source", "1", True, parent_id="0"),
            "first": first,
            "second": second,
            "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
        }
        scraper = self._scraper()

        _, stats = Organizer(client=_TreeClient(tree, infos), scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual(scraper.match.call_count, 3)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)
        self.assertEqual(stats["directory_identity_cache_groups"], 0)

    def test_verified_first_episode_attests_identity_but_revalidates_siblings(self):
        validation = {
            "required": True,
            "passed": True,
            "season": 1,
            "episode": 3,
            "episode_count": 12,
            "reason": "episode_verified",
        }
        seed_match = MatchResult(
            tmdb_id="9001",
            external_id="9001",
            provider="tmdb",
            title="Example Show",
            year="2024",
            media_type="tv",
            confidence=0.85,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="匹配置信度 85% 低于严格模式阈值 90%",
            metadata={
                "verified_automatic_identity_proof": {
                    "version": 2,
                    "kind": "tmdb_tv_episode_identity",
                    "provider": "tmdb",
                    "external_id": "9001",
                    "media_type": "tv",
                    "confidence": 0.85,
                    "recognition_threshold": 0.9,
                    "automatic_match_preset": "balanced",
                    "global_threshold": 0.9,
                    "strong_title_score": 0.85,
                    "candidate_count": 1,
                    "candidate_gap": 1.0,
                    "decision_constraints": [],
                    "selected_constraints": [],
                    "expected_year": "",
                    "candidate_year": "2024",
                    "source_title_key": "exampleshow",
                    "matched_title_key": "exampleshow",
                    "source_position": {"season": 1, "episode": 3},
                    "target_position": {"season": 1, "episode": 3},
                    "position_validation": validation,
                },
            },
        )
        tree = {
            "source": [GuangYaFile("show", "Example Show", True, parent_id="source")],
            "show": [
                GuangYaFile(
                    f"e{episode}",
                    f"Example.Show.S01E{episode:02d}.1080p.mkv",
                    False,
                    1024,
                    f"etag-{episode}",
                    "show",
                )
                for episode in range(1, 4)
            ],
            "archive": [],
        }
        infos = {
            "source": GuangYaFile("source", "1", True, parent_id="0"),
            "show": GuangYaFile("show", "Example Show", True, parent_id="source"),
            "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
        }
        scraper = TMDBScraper()
        scraper.match = Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(seed_match))
        scraper.get_detail = Mock(return_value={
            "id": 9001,
            "genres": [],
            "origin_country": [],
            "first_air_date": "2024-01-01",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        })

        plans, stats = Organizer(
            client=_TreeClient(tree, infos), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertEqual([plan.action for plan in plans], ["move"] * 3)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, 1), (1, 2), (1, 3)],
        )
        self.assertGreaterEqual(scraper.match.call_count, 1)
        self.assertEqual(stats["directory_identity_attestation_bindings"], 1)
        self.assertEqual(stats["directory_identity_attestation_hits"], 2)
        self.assertTrue(all(
            plan.match.metadata["final_position_validation"]["passed"]
            for plan in plans
        ))

    def test_slime_fourth_season_root_absolute_range_is_rebased(self):
        files = [
            GuangYaFile(
                f"e{episode}",
                "[ANi] 關於我轉生變成史萊姆這檔事 第四季 "
                f"- {episode} [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4",
                False,
                1024,
                f"etag-{episode}",
                "source",
            )
            for episode in range(73, 93)
        ]
        client = _TreeClient(
            {"source": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        seasons = [
            {"season_number": season, "episode_count": 24}
            for season in range(1, 5)
        ]
        scraper = self._verified_identity_scraper(
            probe_season=4,
            seasons=seasons,
            title="关于我转生变成史莱姆这档事",
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 20)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(4, episode) for episode in range(1, 21)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.mode == "season_continuous"
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["directory_identity_attestation_bindings"], 1)
        self.assertEqual(stats["directory_identity_attestation_hits"], 19)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertTrue(
            scraper.match.call_args.args[0].endswith(".S04E01.mp4")
        )

    def test_slime_fourth_season_maps_into_single_merged_tmdb_season(self):
        files = [
            GuangYaFile(
                f"e{episode}",
                "[ANi] 關於我轉生變成史萊姆這檔事 第四季 "
                f"- {episode} [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4",
                False,
                1024,
                f"etag-{episode}",
                "source",
            )
            for episode in range(73, 93)
        ]
        client = _TreeClient(
            {"source": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        failed_probe = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="关于我转生变成史莱姆这档事",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="TMDB 中不存在第 4 季，已阻止自动整理",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_season_not_found"],
        )
        scraper = TMDBScraper()
        scraper.match = Mock(
            side_effect=lambda *args, **kwargs: copy.deepcopy(failed_probe)
        )
        scraper.get_detail = Mock(return_value={
            "id": 82684,
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2018-10-02",
            "seasons": [{"season_number": 1, "episode_count": 92}],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 20)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, episode) for episode in range(73, 93)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.mode == "absolute"
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertTrue(scraper.match.call_args.args[0].endswith(".S04E01.mp4"))

    def test_slime_reset_second_season_only_isolates_out_of_range_tail(self):
        show = GuangYaFile(
            "show",
            "关于我转生变成史莱姆这档事 第二季 S02E01-E26",
            True,
            parent_id="source",
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                "关于我转生变成史莱姆这档事."
                f"S02E{episode:02d}.2021.2160p.WEB-DL.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(1, 27)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._verified_identity_scraper(
            probe_season=2,
            seasons=[
                {"season_number": 1, "episode_count": 24},
                {"season_number": 2, "episode_count": 24},
            ],
            title="关于我转生变成史莱姆这档事",
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans[:24]], ["move"] * 24)
        self.assertEqual([plan.action for plan in plans[24:]], ["skip", "skip"])
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans[:24]],
            [(2, episode) for episode in range(1, 25)],
        )
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans[24:]],
            [(2, 25), (2, 26)],
        )
        self.assertTrue(all("超出 TMDB" in plan.note for plan in plans[24:]))
        self.assertEqual(stats["need_confirm"], 2)
        self.assertEqual(scraper.match.call_count, 1)

    def test_later_season_continuous_pack_uses_e01_identity_probe(self):
        show = GuangYaFile(
            "show", "Example Show Second Season", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Example.Show.S02E{episode:02d}.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(13, 25)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        failed_probe = RecognitionResult(
            tmdb_id="100",
            external_id="100",
            provider="tmdb",
            title="Example Show",
            year="2026",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="第 2 季仅有 12 集，E13 超出 TMDB 范围",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_episode_out_of_range"],
        )
        scraper = TMDBScraper()
        scraper.match = Mock(
            side_effect=lambda *args, **kwargs: copy.deepcopy(failed_probe)
        )
        scraper.get_detail = Mock(return_value={
            "id": 100,
            "genres": [],
            "origin_country": ["US"],
            "first_air_date": "2026-01-01",
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 12)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(2, episode) for episode in range(1, 13)],
        )
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 11)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertTrue(scraper.match.call_args.args[0].endswith(".S02E01.mkv"))
        self.assertIn(
            "directory_identity_probe_position_recovery",
            plans[0].match.metadata,
        )

    def test_identity_probe_does_not_rebase_ordinary_partial_season(self):
        show = GuangYaFile(
            "show", "Example Show Season 3 E13-E24", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Example.Show.S03E{episode:02d}.2026.1080p.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(13, 25)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = self._verified_identity_scraper(
            probe_season=3,
            probe_episode=1,
            seasons=[
                {"season_number": season, "episode_count": 24}
                for season in range(1, 4)
            ],
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 12)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(3, episode) for episode in range(13, 25)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and not plan.episode_mapping.changed
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertTrue(scraper.match.call_args.args[0].endswith(".S03E01.mkv"))

    def test_fixed_tmdb_confirmation_uses_the_same_continued_number_mapping(self):
        show = GuangYaFile(
            "show", "Slime Season 3 S03E49-E52", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Tensei.Shitara.Slime.Datta.Ken.S03E{episode:02d}.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(49, 53)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        detail = {
            "id": 82684,
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2018-10-02",
            "seasons": [
                {"season_number": season, "episode_count": 24}
                for season in range(1, 5)
            ],
        }
        scraper = FixedMatchScraper(
            TMDBScraper(),
            MatchResult(
                tmdb_id="82684",
                external_id="82684",
                provider="tmdb",
                title="关于我转生变成史莱姆这档事",
                year="2018",
                media_type="tv",
                confidence=1.0,
                status="matched",
                need_confirm=False,
                matched_by="manual",
            ),
            detail,
            map_source_positions=True,
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 4)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(3, episode) for episode in range(1, 5)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.mode == "season_continuous"
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)

    def test_fixed_tmdb_confirmation_maps_second_cour_into_merged_season(self):
        plans, stats, delegate = self._fixed_cour_confirmation(
            source_season=2,
            source_episode_end=3,
            season_detail=self._merged_cour_season_detail(12, 13),
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 3)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, 13), (1, 14), (1, 15)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.reason
            == "publisher_cour_mapped_to_merged_tmdb_season"
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(delegate.get_tv_season_detail.call_count, 3)

    def test_fixed_tmdb_confirmation_keeps_existing_source_season(self):
        season_detail = self._merged_cour_season_detail(12, 13)
        season_detail["season_number"] = 2
        plans, stats, delegate = self._fixed_cour_confirmation(
            source_season=2,
            source_episode_end=3,
            season_detail=season_detail,
            target_season=2,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 3)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(2, 1), (2, 2), (2, 3)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and not plan.episode_mapping.changed
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        delegate.get_tv_season_detail.assert_not_called()

    def test_fixed_tmdb_confirmation_maps_third_cour_into_merged_season(self):
        plans, stats, _delegate = self._fixed_cour_confirmation(
            source_season=3,
            source_episode_end=3,
            season_detail=self._merged_cour_season_detail(12, 12, 12),
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 3)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, 25), (1, 26), (1, 27)],
        )
        self.assertEqual(stats["need_confirm"], 0)

    def test_fixed_tmdb_confirmation_split_cour_without_date_proof_fails_closed(self):
        variants = {}
        no_hiatus = self._merged_cour_season_detail(12, 13)
        first_air_date = date(2026, 1, 11)
        for offset, episode in enumerate(no_hiatus["episodes"]):
            episode["air_date"] = (
                first_air_date + timedelta(days=7 * offset)
            ).isoformat()
        variants["no_hiatus"] = no_hiatus
        incomplete = self._merged_cour_season_detail(12, 13)
        incomplete["episodes"][12]["air_date"] = ""
        variants["incomplete_dates"] = incomplete

        for name, season_detail in variants.items():
            with self.subTest(name=name):
                plans, stats, _delegate = self._fixed_cour_confirmation(
                    source_season=2,
                    source_episode_end=3,
                    season_detail=season_detail,
                )

                self.assertEqual([plan.action for plan in plans], ["skip"] * 3)
                self.assertTrue(all("TMDB" in plan.note for plan in plans))
                self.assertEqual(stats["need_confirm"], 3)

    def test_fixed_tmdb_preview_final_validates_precomputed_position(self):
        show = GuangYaFile("show", "Example Show", True, parent_id="source")
        filename = "Example.Show.S02E13.mkv"
        video = GuangYaFile(
            "e13", filename, False, 1024, "etag-13", "show"
        )
        client = _TreeClient(
            {"source": [show], "show": [video], "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        scraper = FixedMatchScraper(
            TMDBScraper(),
            MatchResult(
                tmdb_id="100",
                external_id="100",
                provider="tmdb",
                title="Example Show",
                year="2026",
                media_type="tv",
                confidence=1.0,
                status="matched",
                need_confirm=False,
                matched_by="manual",
            ),
            {
                "id": 100,
                "genres": [],
                "origin_country": ["US"],
                "first_air_date": "2026-01-01",
                "seasons": [
                    {"season_number": 1, "episode_count": 12},
                    {"season_number": 2, "episode_count": 12},
                ],
            },
            position_overrides={filename: (2, 13)},
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True,
        )

        self.assertTrue(scraper.supports_tmdb_position_validation)
        self.assertFalse(scraper.map_source_positions)
        self.assertEqual([plan.action for plan in plans], ["skip"])
        self.assertIn("超出 TMDB", plans[0].note)
        self.assertEqual(stats["need_confirm"], 1)

    def test_missing_source_season_position_failure_recovers_via_absolute_range(self):
        show = GuangYaFile(
            "show", "Slime Season 3 S03E49-E52", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Tensei.Shitara.Slime.Datta.Ken.S03E{episode:02d}.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(49, 53)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        failed_probe = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="关于我转生变成史莱姆这档事",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="TMDB 中不存在第 3 季，已阻止自动整理",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_season_not_found"],
        )
        scraper = TMDBScraper()
        scraper.match = Mock(
            side_effect=lambda *args, **kwargs: copy.deepcopy(failed_probe)
        )
        scraper.get_detail = Mock(return_value={
            "id": 82684,
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2018-10-02",
            "seasons": [{"season_number": 1, "episode_count": 92}],
        })

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 4)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, episode) for episode in range(49, 53)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and plan.episode_mapping.mode == "absolute"
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 3)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertIn(
            "directory_identity_probe_position_recovery",
            plans[0].match.metadata,
        )

    def test_identity_probe_recomputes_mapping_after_tmdb_detail_refresh(self):
        show = GuangYaFile(
            "show", "Example Show Season 3 S03E13-E15", True, parent_id="source"
        )
        files = [
            GuangYaFile(
                f"e{episode}",
                f"Example.Show.S03E{episode:02d}.mkv",
                False,
                1024,
                f"etag-{episode}",
                "show",
            )
            for episode in range(13, 16)
        ]
        client = _TreeClient(
            {"source": [show], "show": files, "archive": []},
            {
                "source": GuangYaFile("source", "1", True, parent_id="0"),
                "show": show,
                "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
            },
        )
        failed_probe = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="Example Show",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="TMDB 中不存在第 3 季，已阻止自动整理",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_season_not_found"],
        )
        stale_detail = {
            "id": 82684,
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2018-10-02",
            "seasons": [{"season_number": 1, "episode_count": 92}],
        }
        refreshed_detail = {
            **stale_detail,
            "seasons": [
                {"season_number": season, "episode_count": 24}
                for season in range(1, 4)
            ],
        }
        scraper = TMDBScraper()
        scraper.match = Mock(
            side_effect=lambda *args, **kwargs: copy.deepcopy(failed_probe)
        )
        scraper.get_detail = Mock(
            side_effect=lambda *_args, force_refresh=False, **_kwargs: copy.deepcopy(
                refreshed_detail if force_refresh else stale_detail
            )
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", self._rules(), dry_run=True, automatic=True,
        )

        self.assertEqual([plan.action for plan in plans], ["move"] * 3)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(3, 13), (3, 14), (3, 15)],
        )
        self.assertTrue(all(
            plan.episode_mapping is not None
            and not plan.episode_mapping.changed
            and plan.episode_mapping.confidence == 1.0
            for plan in plans
        ))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(scraper.get_detail.call_count, 2)
        self.assertTrue(
            plans[0].match.metadata["tmdb_detail_force_refreshed"]
        )
        self.assertTrue(
            plans[0].match.metadata["tmdb_detail_refresh_performed"]
        )
        self.assertIn(
            "directory_identity_probe_position_recovery",
            plans[0].match.metadata,
        )

    def test_cached_bare_absolute_mapping_is_recomputed_after_refresh(self):
        file = GuangYaFile(
            "e25", "Example Show - 25.mkv", False, 1024, "etag-25", "show"
        )
        stale_mapping = {
            "source_season": None,
            "source_episode": 25,
            "target_season": 1,
            "target_episode": 25,
            "mode": "absolute",
            "reason": "absolute_numbering_rolled_over_tmdb_seasons",
            "confidence": 0.95,
            "changed": True,
        }
        match = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="Example Show",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="matched",
            need_confirm=False,
            matched_by="search",
            preprocess_evaluated=True,
            effective_season=1,
            effective_episode=25,
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            metadata={"episode_mapping": stale_mapping},
        )
        stale_detail = {
            "id": 82684,
            "genres": [],
            "origin_country": [],
            "seasons": [{"season_number": 1, "episode_count": 24}],
        }
        refreshed_detail = {
            **stale_detail,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 13},
            ],
        }
        scraper = TMDBScraper()
        scraper.get_detail = Mock(return_value=copy.deepcopy(stale_detail))
        organizer = Organizer(client=object(), scraper=scraper)
        organizer._refresh_tmdb_detail_once = Mock(
            return_value=(copy.deepcopy(refreshed_detail), False)
        )

        plan = organizer._plan_one(
            file,
            "Example Show",
            self._rules(),
            match_override=match,
            automatic=True,
        )

        self.assertEqual(plan.action, "move")
        self.assertEqual((plan.season, plan.episode), (2, 13))
        self.assertIsNotNone(plan.episode_mapping)
        self.assertEqual(
            (plan.episode_mapping.source_season, plan.episode_mapping.source_episode),
            (None, 25),
        )
        self.assertEqual(plan.episode_mapping.mode, "absolute")
        self.assertTrue(plan.match.metadata["tmdb_detail_force_refreshed"])
        organizer._refresh_tmdb_detail_once.assert_called_once()

    def test_cached_mapping_is_recomputed_after_tmdb_detail_refresh(self):
        file = GuangYaFile(
            "e49", "Example.Show.S03E49.mkv", False, 1024, "etag-49", "show"
        )
        stale_mapping = {
            "source_season": 3,
            "source_episode": 49,
            "target_season": 1,
            "target_episode": 49,
            "mode": "absolute",
            "reason": "absolute_numbering_rolled_over_tmdb_seasons",
            "confidence": 1.0,
            "changed": True,
        }
        failed_probe = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="Example Show",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="TMDB 中第 1 季不存在第 49 集，已阻止自动整理",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_episode_out_of_range"],
            metadata={"episode_mapping": stale_mapping},
        )
        refreshed_detail = {
            "id": 82684,
            "genres": [],
            "origin_country": [],
            "seasons": [
                {"season_number": season, "episode_count": 24}
                for season in range(1, 5)
            ],
        }
        scraper = TMDBScraper()
        scraper.get_detail = Mock(return_value=copy.deepcopy(refreshed_detail))
        organizer = Organizer(client=object(), scraper=scraper)
        organizer._refresh_tmdb_detail_once = Mock(
            return_value=(copy.deepcopy(refreshed_detail), False)
        )
        evidence = DirectoryEpisodeEvidence(
            directory_key="show",
            directory_name="Example Show Season 3 S03E49-E51",
            source_season=3,
            range_start=49,
            range_end=51,
            episode_count=3,
        )

        plan = organizer._plan_one(
            file,
            "Example Show Season 3 S03E49-E51",
            self._rules(),
            recognition_name="Example.Show.S03E01.mkv",
            recognition_identity_only=True,
            parsed_override=(1, 49),
            source_position_override=(3, 49),
            directory_episode_evidence=evidence,
            directory_episode_member_count=3,
            match_override=failed_probe,
            automatic=True,
        )

        self.assertEqual(plan.action, "move")
        self.assertEqual((plan.season, plan.episode), (3, 1))
        self.assertIsNotNone(plan.episode_mapping)
        self.assertEqual(plan.episode_mapping.mode, "season_continuous")
        self.assertEqual(
            plan.episode_mapping.reason,
            "continued_numbering_rebased_to_tmdb_season",
        )
        self.assertTrue(plan.match.metadata["tmdb_detail_force_refreshed"])
        self.assertFalse(plan.match.metadata["tmdb_detail_refresh_performed"])
        organizer._refresh_tmdb_detail_once.assert_called_once()

    def test_identity_probe_consumes_detail_refreshed_by_parallel_planner(self):
        file = GuangYaFile(
            "e13", "Example.Show.S03E13.mkv", False, 1024, "etag-13", "show"
        )
        failed_probe = RecognitionResult(
            tmdb_id="82684",
            external_id="82684",
            provider="tmdb",
            title="Example Show",
            year="2018",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="TMDB 中不存在第 3 季，已阻止自动整理",
            matched_by="search",
            threshold_decision={
                "threshold": 0.9,
                "score": 1.0,
                "passed": True,
                "reason": "score_met",
            },
            rejected_constraints=["tmdb_position_season_not_found"],
        )
        stale_detail = {
            "id": 82684,
            "genres": [],
            "origin_country": [],
            "seasons": [{"season_number": 1, "episode_count": 92}],
        }
        refreshed_detail = {
            **stale_detail,
            "seasons": [
                {"season_number": season, "episode_count": 24}
                for season in range(1, 4)
            ],
        }
        scraper = TMDBScraper()
        scraper.get_detail = Mock(return_value=copy.deepcopy(stale_detail))
        organizer = Organizer(client=object(), scraper=scraper)
        organizer._refresh_tmdb_detail_once = Mock(
            return_value=(copy.deepcopy(refreshed_detail), False)
        )
        evidence = DirectoryEpisodeEvidence(
            directory_key="show",
            directory_name="Example Show Season 3 S03E13-E15",
            source_season=3,
            range_start=13,
            range_end=15,
            episode_count=3,
        )

        plan = organizer._plan_one(
            file,
            "Example Show Season 3 S03E13-E15",
            self._rules(),
            recognition_name="Example.Show.S03E01.mkv",
            recognition_identity_only=True,
            source_position_override=(3, 13),
            directory_episode_evidence=evidence,
            directory_episode_member_count=3,
            match_override=failed_probe,
            automatic=True,
        )

        self.assertEqual(plan.action, "move")
        self.assertEqual((plan.season, plan.episode), (3, 13))
        self.assertIsNotNone(plan.episode_mapping)
        self.assertFalse(plan.episode_mapping.changed)
        self.assertEqual(plan.episode_mapping.confidence, 1.0)
        self.assertTrue(plan.match.metadata["tmdb_detail_force_refreshed"])
        self.assertFalse(plan.match.metadata["tmdb_detail_refresh_performed"])
        organizer._refresh_tmdb_detail_once.assert_called_once()

    def test_missing_source_season_low_and_mid_ranges_still_fail_closed(self):
        for range_start, range_end in ((1, 3), (10, 12)):
            with self.subTest(range_start=range_start, range_end=range_end):
                show = GuangYaFile(
                    "show",
                    f"Example Show Season 3 S03E{range_start:02d}-E{range_end:02d}",
                    True,
                    parent_id="source",
                )
                files = [
                    GuangYaFile(
                        f"e{episode}",
                        f"Example.Show.S03E{episode:02d}.mkv",
                        False,
                        1024,
                        f"etag-{episode}",
                        "show",
                    )
                    for episode in range(range_start, range_end + 1)
                ]
                client = _TreeClient(
                    {"source": [show], "show": files, "archive": []},
                    {
                        "source": GuangYaFile("source", "1", True, parent_id="0"),
                        "show": show,
                        "archive": GuangYaFile(
                            "archive", "整理", True, parent_id="0"
                        ),
                    },
                )
                failed_probe = RecognitionResult(
                    tmdb_id="82684",
                    external_id="82684",
                    provider="tmdb",
                    title="Example Show",
                    year="2018",
                    media_type="tv",
                    confidence=1.0,
                    threshold=0.9,
                    status="low_confidence",
                    need_confirm=True,
                    error="TMDB 中不存在第 3 季，已阻止自动整理",
                    matched_by="search",
                    threshold_decision={
                        "threshold": 0.9,
                        "score": 1.0,
                        "passed": True,
                        "reason": "score_met",
                    },
                    rejected_constraints=["tmdb_position_season_not_found"],
                )
                scraper = TMDBScraper()
                scraper.match = Mock(
                    side_effect=lambda *args, **kwargs: copy.deepcopy(failed_probe)
                )
                scraper.get_detail = Mock(return_value={
                    "id": 82684,
                    "genres": [],
                    "origin_country": [],
                    "first_air_date": "2018-01-01",
                    "seasons": [{"season_number": 1, "episode_count": 92}],
                })

                plans, stats = Organizer(client=client, scraper=scraper).organize(
                    "source", self._rules(), dry_run=True, automatic=True,
                )

                self.assertEqual(
                    [plan.action for plan in plans],
                    ["skip"] * (range_end - range_start + 1),
                )
                self.assertTrue(all("TMDB" in plan.note for plan in plans))
                self.assertEqual(stats["need_confirm"], range_end - range_start + 1)
                self.assertTrue(
                    scraper.match.call_args_list[0].args[0].endswith(".S03E01.mkv")
                )

    def test_low_confidence_complete_package_uses_directory_identity_proof(self):
        scraper = self._low_confidence_package_scraper()
        plans, stats = Organizer(
            client=self._boruto_tree(list(range(1, 13))), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertEqual(len(plans), 12)
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["directory_package_identity_bindings"], 1)
        self.assertEqual(stats["directory_identity_cache_hits"], 11)
        self.assertEqual(scraper.match.call_count, 1)
        self.assertEqual(
            [(plan.season, plan.episode) for plan in plans],
            [(1, episode) for episode in range(1, 13)],
        )
        self.assertTrue(all(
            plan.match.metadata["final_position_validation"]["passed"]
            for plan in plans
        ))

    def test_low_confidence_short_package_still_requires_confirmation(self):
        scraper = self._low_confidence_package_scraper()
        plans, stats = Organizer(
            client=self._boruto_tree(list(range(1, 12))), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertTrue(all(plan.action == "skip" for plan in plans))
        self.assertEqual(stats["directory_package_identity_bindings"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)
        self.assertEqual(stats["recognition_work_cache_hits"], 10)
        self.assertEqual(stats["recognition_work_cache_groups"], 1)
        self.assertEqual(scraper.match.call_count, 1)

    def test_low_confidence_gapped_package_still_requires_confirmation(self):
        scraper = self._low_confidence_package_scraper(episode_count=13)
        plans, stats = Organizer(
            client=self._boruto_tree(list(range(1, 12)) + [13]), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertTrue(all(plan.action == "skip" for plan in plans))
        self.assertEqual(stats["directory_package_identity_bindings"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)

    def test_low_confidence_duplicate_episode_package_fails_closed(self):
        scraper = self._low_confidence_package_scraper()
        plans, stats = Organizer(
            client=self._boruto_tree(
                list(range(1, 13)), duplicate_first=True
            ),
            scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertTrue(all(plan.action == "skip" for plan in plans))
        self.assertEqual(stats["directory_package_identity_bindings"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)

    def test_low_confidence_multiple_candidates_fail_closed(self):
        scraper = self._low_confidence_package_scraper(multiple_candidates=True)
        plans, stats = Organizer(
            client=self._boruto_tree(list(range(1, 13))), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertTrue(all(plan.action == "skip" for plan in plans))
        self.assertEqual(stats["directory_package_identity_bindings"], 0)
        self.assertEqual(stats["directory_identity_cache_hits"], 0)

    def test_directory_identity_proof_does_not_bypass_tmdb_episode_range(self):
        scraper = self._low_confidence_package_scraper(episode_count=11)
        plans, stats = Organizer(
            client=self._boruto_tree(list(range(1, 13))), scraper=scraper,
        ).organize("source", self._rules(), dry_run=True, automatic=True)

        self.assertEqual([plan.action for plan in plans[:11]], ["move"] * 11)
        self.assertEqual(plans[11].action, "skip")
        self.assertIn("超出 TMDB", plans[11].note)
        self.assertEqual(stats["directory_package_identity_bindings"], 1)
        self.assertEqual(stats["need_confirm"], 1)


if __name__ == "__main__":
    unittest.main()
