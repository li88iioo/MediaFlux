from __future__ import annotations

import unittest

from tests.support import release_parse_result
from datetime import date, timedelta
from unittest.mock import Mock

from app.modules.directory_media import DirectoryInspection, MediaSnapshot
from app.modules.directory_scrape import DirectoryScrapeService, FixedMatchScraper
from app.modules.scraper import MatchResult
from app.modules.episode_mapping import (
    build_directory_episode_evidence,
    extract_release_episode_range,
    infer_episode_mapping,
    infer_merged_season_cour_mapping,
)


class EpisodeMappingTests(unittest.TestCase):
    def test_release_range_parser_accepts_common_pack_notation(self):
        self.assertEqual(
            extract_release_episode_range("Example Show S01E01-E24"),
            (1, 1, 24),
        )
        self.assertEqual(
            extract_release_episode_range("[01-20 FIN][1080P]"),
            (None, 1, 20),
        )

    def test_release_range_parser_accepts_fullwidth_tilde_and_rejects_cross_season(self):
        self.assertEqual(
            extract_release_episode_range("[01～20 FIN][1080P]"),
            (None, 1, 20),
        )
        self.assertEqual(
            extract_release_episode_range("Example Show S01E11-S02E03"),
            (None, None, None),
        )

    def test_s01_absolute_pack_rolls_over_tmdb_seasons(self):
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 10},
            {"season_number": 2, "episode_count": 14},
        ]}
        mapping = infer_episode_mapping(
            source_season=1,
            source_episode=11,
            parent_path="Example Show S01E01-E24",
            detail=detail,
        )
        self.assertTrue(mapping.changed)
        self.assertEqual(mapping.mode, "absolute")
        self.assertEqual((mapping.target_season, mapping.target_episode), (2, 1))

    def test_second_season_continued_numbering_rebases_to_episode_one(self):
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]}
        mapping = infer_episode_mapping(
            source_season=2,
            source_episode=13,
            parent_path="Example Show S02E13-E24",
            detail=detail,
        )
        self.assertTrue(mapping.changed)
        self.assertEqual(mapping.mode, "season_continuous")
        self.assertEqual((mapping.target_season, mapping.target_episode), (2, 1))

    def test_auto_mode_never_changes_a_position_that_exists_in_tmdb(self):
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 24},
        ]}
        mapping = infer_episode_mapping(
            source_season=2,
            source_episode=13,
            parent_path="Example Show S02E13-E24",
            detail=detail,
        )
        self.assertFalse(mapping.changed)
        self.assertEqual((mapping.target_season, mapping.target_episode), (2, 13))

    def test_specials_and_missing_tmdb_counts_stay_unchanged(self):
        special = infer_episode_mapping(
            source_season=0, source_episode=1, parent_path="Specials", detail={}
        )
        unknown = infer_episode_mapping(
            source_season=2, source_episode=13, parent_path="Show", detail={}
        )
        self.assertFalse(special.changed)
        self.assertFalse(unknown.changed)

    def test_isolated_continued_episode_is_not_rebased_without_directory_evidence(self):
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]}
        mapping = infer_episode_mapping(
            source_season=2, source_episode=13, parent_path="Example Show", detail=detail,
        )

        self.assertFalse(mapping.changed)
        self.assertEqual((mapping.target_season, mapping.target_episode), (2, 13))
        self.assertEqual(mapping.reason, "identity")

    def test_directory_evidence_rebases_second_season_continued_numbering(self):
        evidence = build_directory_episode_evidence([
            ("show", "Example Show Second Season", 2, episode)
            for episode in range(13, 26)
        ])["show"]
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]}

        first = infer_episode_mapping(
            source_season=2, source_episode=13, parent_path="Example Show Second Season",
            detail=detail, directory_evidence=evidence,
        )
        last = infer_episode_mapping(
            source_season=2, source_episode=25, parent_path="Example Show Second Season",
            detail=detail, directory_evidence=evidence,
        )

        self.assertEqual((first.target_season, first.target_episode), (2, 1))
        self.assertEqual((last.target_season, last.target_episode), (2, 13))
        self.assertEqual(first.mode, "season_continuous")

    def test_directory_evidence_keeps_merged_tmdb_season_absolute_numbering(self):
        evidence = build_directory_episode_evidence([
            ("show", "Example Show Second Season", 2, episode)
            for episode in range(13, 26)
        ])["show"]
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 25},
        ]}

        mapping = infer_episode_mapping(
            source_season=2, source_episode=13, parent_path="Example Show Second Season",
            detail=detail, directory_evidence=evidence,
        )

        self.assertEqual((mapping.target_season, mapping.target_episode), (1, 13))
        self.assertEqual(mapping.mode, "absolute")

    @staticmethod
    def _split_cour_season_detail(*, missing_air_date: int | None = None):
        episodes = []
        first_start = date(2026, 1, 11)
        second_start = date(2026, 7, 5)
        for number in range(1, 26):
            if number <= 12:
                aired_on = first_start + timedelta(days=7 * (number - 1))
            else:
                aired_on = second_start + timedelta(days=7 * (number - 13))
            episodes.append({
                "episode_number": number,
                "air_date": "" if number == missing_air_date else aired_on.isoformat(),
            })
        return {"season_number": 1, "episodes": episodes}

    def test_explicit_publisher_second_cour_maps_into_merged_tmdb_season(self):
        mapping = infer_merged_season_cour_mapping(
            source_season=2,
            source_episode=6,
            detail={"seasons": [{"season_number": 1, "episode_count": 25}]},
            season_detail=self._split_cour_season_detail(),
        )

        self.assertTrue(mapping.changed)
        self.assertEqual((mapping.target_season, mapping.target_episode), (1, 18))
        self.assertEqual((mapping.range_start, mapping.range_end), (13, 25))
        self.assertEqual(mapping.mode, "absolute")
        self.assertEqual(
            mapping.reason, "publisher_cour_mapped_to_merged_tmdb_season"
        )
        self.assertEqual(mapping.confidence, 1.0)

    def test_merged_tmdb_cour_mapping_allows_same_day_batch_dates(self):
        season_detail = self._split_cour_season_detail()
        episodes = season_detail["episodes"]
        episodes[1]["air_date"] = episodes[0]["air_date"]
        episodes[2]["air_date"] = episodes[0]["air_date"]
        episodes[13]["air_date"] = episodes[12]["air_date"]

        mapping = infer_merged_season_cour_mapping(
            source_season=2,
            source_episode=6,
            detail={"seasons": [{"season_number": 1, "episode_count": 25}]},
            season_detail=season_detail,
        )

        self.assertTrue(mapping.changed)
        self.assertEqual((mapping.target_season, mapping.target_episode), (1, 18))
        self.assertEqual(
            mapping.reason, "publisher_cour_mapped_to_merged_tmdb_season"
        )

    def test_merged_tmdb_cour_mapping_fails_closed_without_strong_hiatus(self):
        episodes = [
            {
                "episode_number": number,
                "air_date": (date(2026, 1, 11) + timedelta(days=7 * (number - 1))).isoformat(),
            }
            for number in range(1, 26)
        ]

        mapping = infer_merged_season_cour_mapping(
            source_season=2, source_episode=6,
            detail={"seasons": [{"season_number": 1, "episode_count": 25}]},
            season_detail={"season_number": 1, "episodes": episodes},
        )

        self.assertFalse(mapping.changed)
        self.assertEqual(mapping.confidence, 0.0)

    def test_merged_tmdb_cour_mapping_fails_closed_on_incomplete_episode_dates(self):
        mapping = infer_merged_season_cour_mapping(
            source_season=2, source_episode=6,
            detail={"seasons": [{"season_number": 1, "episode_count": 25}]},
            season_detail=self._split_cour_season_detail(missing_air_date=13),
        )

        self.assertFalse(mapping.changed)
        self.assertEqual(mapping.confidence, 0.0)

    def test_merged_tmdb_cour_mapping_rejects_episode_outside_target_segment(self):
        mapping = infer_merged_season_cour_mapping(
            source_season=2, source_episode=14,
            detail={"seasons": [{"season_number": 1, "episode_count": 25}]},
            season_detail=self._split_cour_season_detail(),
        )

        self.assertFalse(mapping.changed)
        self.assertEqual(mapping.confidence, 0.0)

    def test_non_contiguous_pack_does_not_create_mapping_evidence(self):
        evidence = build_directory_episode_evidence([
            ("show", "Example Show Second Season", 2, 13),
            ("show", "Example Show Second Season", 2, 15),
            ("show", "Example Show Second Season", 2, 16),
        ])

        self.assertNotIn("show", evidence)

    def test_bare_contiguous_pack_from_episode_one_can_form_absolute_evidence(self):
        evidence = build_directory_episode_evidence([
            ("boruto", "Boruto Episode 1-156", None, episode)
            for episode in range(1, 157)
        ])["boruto"]
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 52},
            {"season_number": 2, "episode_count": 52},
            {"season_number": 3, "episode_count": 52},
        ]}

        self.assertEqual(evidence.source_season, 1)
        self.assertEqual((evidence.range_start, evidence.range_end), (1, 156))
        self.assertEqual(evidence.episode_count, 156)
        first = infer_episode_mapping(
            source_season=1, source_episode=1, detail=detail,
            directory_evidence=evidence,
        )
        second_season = infer_episode_mapping(
            source_season=1, source_episode=53, detail=detail,
            directory_evidence=evidence,
        )
        last = infer_episode_mapping(
            source_season=1, source_episode=156, detail=detail,
            directory_evidence=evidence,
        )

        self.assertEqual((first.target_season, first.target_episode), (1, 1))
        self.assertEqual((second_season.target_season, second_season.target_episode), (2, 1))
        self.assertEqual((last.target_season, last.target_episode), (3, 52))
        self.assertEqual(second_season.mode, "absolute")
        self.assertEqual(last.mode, "absolute")

    def test_bare_episode_evidence_fails_closed_without_complete_single_mode_sequence(self):
        cases = {
            "missing_first": [
                ("show", "Show", None, episode) for episode in range(2, 6)
            ],
            "non_contiguous": [
                ("show", "Show", None, episode) for episode in (1, 2, 4)
            ],
            "mixed_season": [
                ("show", "Show", None, 1),
                ("show", "Show", 1, 2),
                ("show", "Show", 1, 3),
            ],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                self.assertNotIn("show", build_directory_episode_evidence(entries))


class DirectoryScrapeEpisodeMappingTests(unittest.TestCase):
    def test_manual_preview_position_overrides_use_the_same_mapping_plan(self):
        videos = tuple(
            MediaSnapshot(
                file_id=f"e{episode}",
                parent_id="show",
                name=f"Example Show S02E{episode:02d}.mkv",
                size=1024,
                etag=f"etag-{episode}",
                role="video",
                relative_dir="",
                season=2,
                episode=episode,
            )
            for episode in range(13, 25)
        )
        inspection = DirectoryInspection(
            directory_id="show",
            directory_name="Example Show S02E13-E24",
            media_type="tv",
            suggested_query="Example Show",
            videos=videos,
            companions=(),
            counts={"videos": 12},
            mixed=False,
            fingerprint="fixture",
            season=2,
        )
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]}

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto",
        )

        self.assertEqual(overrides[("", videos[0].name)], (2, 1))
        self.assertEqual(overrides[("", videos[-1].name)], (2, 12))
        self.assertEqual(mappings["e13"].source_episode, 13)
        self.assertEqual(mappings["e13"].target_episode, 1)
        self.assertTrue(all(item.changed for item in mappings.values()))

    def test_existing_absolute_mapping_is_not_overwritten_by_split_cour_probe(self):
        videos = tuple(
            MediaSnapshot(
                file_id=f"e{episode}", parent_id="show",
                name=f"Example Show S02E{episode:02d}.mkv", size=1024,
                etag=f"etag-{episode}", role="video", relative_dir="",
                season=2, episode=episode,
            )
            for episode in range(13, 25)
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show S02E13-E24",
            media_type="tv", suggested_query="Example Show", videos=videos,
            companions=(), counts={"videos": 12}, mixed=False,
            fingerprint="fixture", season=2,
        )
        detail = {"seasons": [{"season_number": 1, "episode_count": 25}]}
        loader = Mock(return_value=EpisodeMappingTests._split_cour_season_detail())

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto", season_detail_loader=loader,
        )

        self.assertEqual(overrides[("", videos[0].name)], (1, 13))
        self.assertEqual(overrides[("", videos[-1].name)], (1, 24))
        self.assertEqual(mappings["e13"].mode, "absolute")
        loader.assert_not_called()

    def test_isolated_second_season_episode_does_not_trigger_split_cour_probe(self):
        video = MediaSnapshot(
            file_id="e1", parent_id="show",
            name="Example Show S02E01.mkv", size=1024,
            etag="etag-1", role="video", relative_dir="",
            season=2, episode=1,
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show Second Cour",
            media_type="tv", suggested_query="Example Show", videos=(video,),
            companions=(), counts={"videos": 1}, mixed=False,
            fingerprint="fixture", season=2,
        )
        detail = {"id": 100, "seasons": [{"season_number": 1, "episode_count": 25}]}
        loader = Mock(return_value=EpisodeMappingTests._split_cour_season_detail())

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto", season_detail_loader=loader,
        )

        self.assertEqual(overrides[("", video.name)], (2, 1))
        self.assertEqual(mappings["e1"].mode, "auto")
        loader.assert_not_called()

    def test_two_episode_sample_does_not_trigger_split_cour_probe(self):
        videos = tuple(
            MediaSnapshot(
                file_id=f"e{episode}", parent_id="show",
                name=f"Example Show S02E{episode:02d}.mkv", size=1024,
                etag=f"etag-{episode}", role="video", relative_dir="",
                season=2, episode=episode,
            )
            for episode in range(1, 3)
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show Second Cour",
            media_type="tv", suggested_query="Example Show", videos=videos,
            companions=(), counts={"videos": 2}, mixed=False,
            fingerprint="fixture", season=2,
        )
        detail = {"id": 100, "seasons": [{"season_number": 1, "episode_count": 25}]}
        loader = Mock(return_value=EpisodeMappingTests._split_cour_season_detail())

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto", season_detail_loader=loader,
        )

        self.assertEqual(overrides[("", videos[0].name)], (2, 1))
        self.assertEqual(overrides[("", videos[1].name)], (2, 2))
        self.assertTrue(all(item.mode == "auto" for item in mappings.values()))
        loader.assert_not_called()

    def test_split_cour_mapping_rebases_second_release_season_into_merged_tmdb_season(self):
        videos = tuple(
            MediaSnapshot(
                file_id=f"e{episode}", parent_id="show",
                name=f"Example Show S02E{episode:02d}.mkv", size=1024,
                etag=f"etag-{episode}", role="video", relative_dir="",
                season=2, episode=episode,
            )
            for episode in range(1, 14)
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show Second Cour",
            media_type="tv", suggested_query="Example Show", videos=videos,
            companions=(), counts={"videos": 13}, mixed=False,
            fingerprint="fixture", season=2,
        )
        detail = {"id": 100, "seasons": [{"season_number": 1, "episode_count": 25}]}
        loader = Mock(return_value=EpisodeMappingTests._split_cour_season_detail())

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto", season_detail_loader=loader,
        )

        self.assertEqual(overrides[("", videos[0].name)], (1, 13))
        self.assertEqual(overrides[("", videos[-1].name)], (1, 25))
        self.assertTrue(all(item.reason == "publisher_cour_mapped_to_merged_tmdb_season" for item in mappings.values()))
        loader.assert_called_once_with("100", 1)

    def test_manual_overrides_distinguish_root_and_nested_duplicate_filenames(self):
        videos = (
            MediaSnapshot(
                file_id="root", parent_id="show", name="Show.S02E13.mkv",
                size=1024, etag="root", role="video", relative_dir="",
                season=2, episode=13,
            ),
            MediaSnapshot(
                file_id="nested", parent_id="nested", name="Show.S02E13.mkv",
                size=1024, etag="nested", role="video", relative_dir="Disc 2",
                season=2, episode=14,
            ),
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show Second Season",
            media_type="tv", suggested_query="Example Show", videos=videos,
            companions=(), counts={"videos": 2}, mixed=False,
            fingerprint="fixture", season=2,
        )
        overrides, _mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, {"seasons": [{"season_number": 1, "episode_count": 24}]},
            "auto", season_override=1,
        )

        class Delegate:
            @staticmethod
            def parse_media(filename, parent_path="", match=None):
                return release_parse_result(
                    {"type": "tv", "season": 2, "episode": 13},
                    filename=filename, parent_path=parent_path,
                )

        scraper = FixedMatchScraper(
            Delegate(),
            MatchResult(tmdb_id="100", title="Example Show", year="2026", media_type="tv"),
            season_override=1,
            position_overrides=overrides,
        )

        self.assertEqual(
            scraper.parse_source_position("Show.S02E13.mkv", "Example Show Second Season"),
            (2, 13),
        )
        root_parse = scraper.parse_media(
            "Show.S02E13.mkv", "Example Show Second Season"
        )
        self.assertEqual(
            (root_parse.effective_season, root_parse.effective_episode),
            (1, 13),
        )
        self.assertEqual(
            scraper.parse_source_position(
                "Show.S02E13.mkv", "Example Show Second Season/Disc 2"
            ),
            (2, 13),
        )
        nested_parse = scraper.parse_media(
            "Show.S02E13.mkv", "Example Show Second Season/Disc 2"
        )
        self.assertEqual(
            (nested_parse.effective_season, nested_parse.effective_episode),
            (1, 14),
        )

    def test_manual_season_override_remains_final_archive_season(self):
        videos = tuple(
            MediaSnapshot(
                file_id=f"e{episode}", parent_id="show",
                name=f"Example Show S02E{episode:02d}.mkv", size=1024,
                etag=f"etag-{episode}", role="video", relative_dir="",
                season=2, episode=episode,
            )
            for episode in range(13, 26)
        )
        inspection = DirectoryInspection(
            directory_id="show", directory_name="Example Show Second Season",
            media_type="tv", suggested_query="Example Show", videos=videos,
            companions=(), counts={"videos": 13}, mixed=False,
            fingerprint="fixture", season=2,
        )
        detail = {"seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]}

        overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "auto", season_override=1,
        )

        self.assertEqual(overrides[("", videos[0].name)], (1, 1))
        self.assertEqual(overrides[("", videos[-1].name)], (1, 13))
        self.assertTrue(all(item.target_season == 1 for item in mappings.values()))


if __name__ == "__main__":
    unittest.main()
