"""Nyaa 文件清单自动整理基准工具契约。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.indexers.http import IndexerHttpResponse
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult, RecognitionResult, TMDBScraper
from tools.benchmark_organize import (
    Candidate,
    ManifestGuangYaClient,
    _safe_match,
    bucket_tags,
    candidate_queues,
    fetch_nyaa_candidates,
    isolated_benchmark_database,
    observed_outcome,
    run_benchmark_case,
    scaled_quotas,
    score_results,
    truth_template,
    write_csv_report,
    write_html_report,
    write_json,
    write_jsonl,
)


def _case(
    *,
    files: list[dict] | None = None,
    manifest_name: str = "Example Show",
) -> dict:
    return {
        "schema_version": 1,
        "case_id": "fixture-1",
        "bucket": "standard_episode",
        "title": "[Test] Example Show - 01 [1080p]",
        "category": "Anime - English-translated",
        "size_text": "100 MiB",
        "size_bytes": 104_857_600,
        "seeders": 1,
        "leechers": 0,
        "downloads": 1,
        "published_at": "2026-08-11T00:00:00+00:00",
        "manifest_name": manifest_name,
        "manifest_version": "v1",
        "manifest_sha256": "a" * 64,
        "files": files or [
            {"path": ["Season 01", "Example Show - 01.mkv"], "length": 100_000_000},
        ],
    }


def _benchmark_rules() -> OrganizeRules:
    return OrganizeRules(
        target_dir_id=ManifestGuangYaClient.target_id,
        small_file_mb=0,
        clean_empty=False,
        link_strm=False,
        notify_enabled=False,
        library_notify=False,
        strm_detail_notify=False,
        emby_refresh=False,
        media_info_enabled=False,
        media_probe_enabled=False,
    )


def _tv_match(
    tmdb_id: str = "123",
    *,
    title: str = "Example Show",
    media_type: str = "tv",
) -> MatchResult:
    return MatchResult(
        tmdb_id=str(tmdb_id),
        title=title,
        year="2024",
        media_type=media_type,
        confidence=1.0,
        status="matched",
        matched_by="tmdb_id",
        provider="tmdb",
        external_id=str(tmdb_id),
    )


def _tv_detail(
    title: str = "Example Show",
    *,
    seasons: list[dict] | None = None,
) -> dict:
    return {
        "id": 123,
        "name": title,
        "first_air_date": "2024-01-01",
        "genres": [{"id": 16, "name": "Animation"}],
        "origin_country": ["JP"],
        "seasons": seasons
        or [
            {"season_number": 0, "episode_count": 20},
            {"season_number": 1, "episode_count": 100},
        ],
        "poster_path": "",
        "backdrop_path": "",
    }


class OrganizeBenchmarkTests(unittest.TestCase):
    def test_bucket_tags_cover_expected_boundary_classes(self):
        self.assertIn("specials", bucket_tags("[Group] Show OVA [1080p]"))
        self.assertIn("range_batch", bucket_tags("[Group] Show [01-12] [1080p]"))
        self.assertIn("absolute_mapping", bucket_tags("[Group] Show - 25 [1080p]"))
        self.assertIn("standard_episode", bucket_tags("[Group] Show S02E03 [1080p]"))
        self.assertIn(
            "release_noise",
            bucket_tags("[Group][WebRip][1080p][HEVC][10bit][AAC] Show - 01"),
        )
        self.assertEqual(bucket_tags("A title without a position"), {"ambiguous"})

    def test_quota_scaling_and_candidate_order_are_deterministic(self):
        self.assertEqual(sum(scaled_quotas(50).values()), 50)
        self.assertEqual(sum(scaled_quotas(7).values()), 7)
        candidates = [
            Candidate(
                title=f"[Group] Show - {episode:02d} [1080p]",
                category="Anime",
                size_text="1 GiB",
                size_bytes=1,
                seeders=1,
                leechers=0,
                downloads=1,
                published_at="",
                torrent_url=f"https://nyaa.si/download/{episode}.torrent",
            )
            for episode in range(1, 5)
        ]
        first = candidate_queues(candidates, seed=42)
        second = candidate_queues(list(reversed(candidates)), seed=42)
        self.assertEqual(
            [item.key for item in first["standard_episode"]],
            [item.key for item in second["standard_episode"]],
        )

    def test_nyaa_collection_follows_icon_only_pagination(self):
        def html(item_id: int, *, next_page: int | None = None) -> bytes:
            pagination = (
                f'<ul class="pagination"><li><a href="/?p={next_page}">'
                '<span class="glyphicon glyphicon-chevron-right"></span></a></li></ul>'
                if next_page is not None else ""
            )
            return f"""
            <html><body>
              <table class="torrent-list"><tbody><tr>
                <td><a title="Anime - English-translated">Anime</a></td>
                <td><a href="/view/{item_id}" title="Show {item_id} - 01">Show</a></td>
                <td><a href="/download/{item_id}.torrent">Torrent</a></td>
                <td>100 MiB</td><td data-timestamp="1786406400"></td>
                <td>10</td><td>1</td><td>20</td>
              </tr></tbody></table>{pagination}
            </body></html>
            """.encode()

        class FakeHttp:
            def __init__(self):
                self.pages: list[int] = []

            async def get(self, url, *, params=None, **kwargs):
                page = int((params or {}).get("p", 1))
                self.pages.append(page)
                body = html(100 + page, next_page=page + 1) if page <= 2 else html(102)
                return IndexerHttpResponse(
                    url=f"https://nyaa.si/?p={page}",
                    status_code=200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=body,
                )

        http = FakeHttp()
        candidates = asyncio.run(fetch_nyaa_candidates(http, pages=3, query=""))
        self.assertEqual([item.title for item in candidates], [
            "Show 101 - 01", "Show 102 - 01",
        ])
        self.assertEqual(http.pages, [1, 2, 3])

    def test_manifest_client_builds_nested_read_only_tree(self):
        client = ManifestGuangYaClient(_case(files=[
            {"path": ["Season 01", "Episode 01.mkv"], "length": 123},
            {"path": ["Season 01", "Episode 01.ass"], "length": 12},
        ]))
        root_names = [item.name for item in client.list_dir("0")]
        self.assertIn("Example Show", root_names)
        self.assertIn("基准归档目标", root_names)
        source_children = client.list_dir(client.source_id)
        self.assertEqual(len(source_children), 1)
        self.assertTrue(source_children[0].is_dir)
        season_children = client.list_dir(source_children[0].file_id)
        self.assertEqual([item.name for item in season_children], ["Episode 01.ass", "Episode 01.mkv"])
        with self.assertRaises(AssertionError):
            client.move("a", "b")

    def test_outcome_projection_separates_isolation_from_unrecognized_skip(self):
        isolated = observed_outcome(
            [{"source_path": "Odd.mkv", "note": "已隔离在源目录等待人工识别"}],
            {"skip_reasons": []},
            "skip",
        )
        unrecognized = observed_outcome(
            [{"source_path": "Unknown.mkv", "note": "未识别"}],
            {"skip_reasons": ["未识别"]},
            "skip",
        )
        self.assertEqual(isolated, "isolated")
        self.assertEqual(unrecognized, "unrecognized")

    def test_truth_scoring_exposes_actionable_outcome_counts(self):
        results = [
            {
                "case_id": "correct", "observed_action": "auto",
                "plans": [{
                    "action": "move", "season": 1, "episode": 1,
                    "match": {"provider": "tmdb", "external_id": "1", "media_type": "tv"},
                }],
            },
            {
                "case_id": "wrong", "observed_action": "auto",
                "plans": [{
                    "action": "move", "season": 1, "episode": 1,
                    "match": {"provider": "tmdb", "external_id": "9", "media_type": "tv"},
                }],
            },
            {"case_id": "confirm", "observed_action": "confirm", "plans": []},
            {"case_id": "isolated", "observed_action": "skip", "observed_outcome": "isolated", "plans": []},
            {"case_id": "unknown", "observed_action": "skip", "observed_outcome": "unrecognized", "plans": []},
            {"case_id": "error", "observed_action": "error", "plans": []},
        ]
        truths = [
            {"case_id": case_id, "expected": {
                "action": action, "provider": "tmdb", "external_id": external_id,
                "media_type": "tv", "season": 1, "episodes": [1],
            }}
            for case_id, action, external_id in (
                ("correct", "auto", "1"), ("wrong", "auto", "1"),
                ("confirm", "confirm", ""), ("isolated", "skip", ""),
                ("unknown", "skip", ""), ("error", "error", ""),
            )
        ]
        scoring = score_results(results, truths)
        self.assertEqual(scoring["automatic_correct"], 1)
        self.assertEqual(scoring["wrong_match"], 1)
        self.assertEqual(scoring["needs_confirmation"], 1)
        self.assertEqual(scoring["isolated"], 1)
        self.assertEqual(scoring["unrecognized"], 1)
        self.assertEqual(scoring["runtime_error"], 1)
        self.assertEqual(scoring["truth_coverage"], 1.0)
        self.assertEqual(scoring["auto_precision"], 0.5)
        self.assertEqual(scoring["confirmation_recall"], 1.0)
        self.assertEqual(scoring["tmdb_id_errors"], 1)
        self.assertEqual(scoring.get("season_episode_errors", 0), 0)

    def test_truth_template_and_scoring_detect_false_auto(self):
        cases = [_case()]
        template = truth_template(cases)
        self.assertEqual(template[0]["case_id"], "fixture-1")
        self.assertEqual(template[0]["expected"]["plans"], [])
        self.assertFalse(template[0]["expected"]["plans_exact"])
        result = {
            "case_id": "fixture-1",
            "observed_action": "auto",
            "plans": [{
                "action": "move",
                "season": 1,
                "episode": 1,
                "match": {"provider": "tmdb", "external_id": "999", "media_type": "tv"},
            }],
        }
        truth = [{
            "case_id": "fixture-1",
            "expected": {
                "action": "auto",
                "provider": "tmdb",
                "external_id": "123",
                "media_type": "tv",
                "season": "1",
                "episodes": [1],
            },
        }]
        scoring = score_results([result], truth)
        self.assertEqual(scoring["labeled"], 1)
        self.assertEqual(scoring["correct"], 0)
        self.assertEqual(scoring["dangerous_false_auto"], 1)
        self.assertEqual(scoring["tmdb_id_errors"], 1)
        self.assertEqual(scoring["auto_precision"], 0.0)

    def test_truth_scoring_rejects_wrong_move_hidden_by_confirmation(self):
        result = {
            "case_id": "fixture-1",
            "observed_action": "confirm",
            "plans": [
                {
                    "action": "move",
                    "source_path": "Show - 01.mkv",
                    "season": 1,
                    "episode": 1,
                    "match": {
                        "provider": "tmdb", "external_id": "999", "media_type": "tv",
                    },
                },
                {"action": "skip", "source_path": "unknown.mkv"},
            ],
        }
        truth = [{
            "case_id": "fixture-1",
            "expected": {
                "action": "confirm",
                "provider": "tmdb",
                "external_id": "123",
                "media_type": "tv",
            },
        }]

        scoring = score_results([result], truth)

        self.assertEqual(scoring["correct"], 0)
        self.assertEqual(scoring["unsafe_move_in_non_auto_batch"], 1)
        self.assertEqual(scoring["tmdb_id_errors"], 1)
        self.assertTrue(scoring["rows"][0]["unsafe_move"])

    def test_truth_scoring_rejects_non_auto_batch_even_when_move_matches_identity(self):
        result = {
            "case_id": "fixture-1",
            "observed_action": "confirm",
            "plans": [
                {
                    "action": "move",
                    "source_path": "Show - 01.mkv",
                    "season": 1,
                    "episode": 1,
                    "match": {
                        "provider": "tmdb", "external_id": "123", "media_type": "tv",
                    },
                },
                {"action": "skip", "source_path": "unknown.mkv"},
            ],
        }
        truth = [{
            "case_id": "fixture-1",
            "expected": {
                "action": "confirm",
                "provider": "tmdb",
                "external_id": "123",
                "media_type": "tv",
            },
        }]

        scoring = score_results([result], truth)

        self.assertEqual(scoring["correct"], 0)
        self.assertEqual(scoring["unsafe_move_in_non_auto_batch"], 1)
        self.assertTrue(scoring["rows"][0]["unsafe_move"])

    def test_truth_scoring_rejects_auto_without_move_plan(self):
        result = {
            "case_id": "fixture-1",
            "observed_action": "auto",
            "plans": [],
        }
        truth = [{
            "case_id": "fixture-1",
            "expected": {"action": "auto", "provider": "tmdb", "external_id": "123"},
        }]

        scoring = score_results([result], truth)

        self.assertEqual(scoring["correct"], 0)
        self.assertEqual(scoring["dangerous_false_auto"], 1)
        self.assertFalse(scoring["rows"][0]["identity_correct"])
        self.assertFalse(scoring["rows"][0]["mapping_correct"])

    def test_truth_scoring_supports_exact_per_source_episode_mapping(self):
        result = {
            "case_id": "fixture-1",
            "observed_action": "auto",
            "plans": [
                {
                    "action": "move",
                    "source_path": "Extra/NCOP.mkv",
                    "season": 0,
                    "episode": 1,
                    "match": {"provider": "tmdb", "external_id": "123", "media_type": "tv"},
                },
                {
                    "action": "move",
                    "source_path": "Show 2 - 01.mkv",
                    "season": 1,
                    "episode": 1,
                    "match": {"provider": "tmdb", "external_id": "123", "media_type": "tv"},
                },
            ],
        }
        truth = [{
            "case_id": "fixture-1",
            "expected": {
                "action": "auto",
                "provider": "tmdb",
                "external_id": "123",
                "media_type": "tv",
                "season": None,
                "episodes": [],
                "plans_exact": True,
                "plans": [
                    {"source_path": "Extra/NCOP.mkv", "season": 0, "episode": 1},
                    {"source_path": "Show 2 - 01.mkv", "season": 2, "episode": 1},
                ],
            },
        }]
        scoring = score_results([result], truth)
        self.assertEqual(scoring["labeled"], 1)
        self.assertEqual(scoring["correct"], 0)
        self.assertEqual(scoring["dangerous_false_auto"], 1)
        self.assertTrue(scoring["rows"][0]["identity_correct"])
        self.assertFalse(scoring["rows"][0]["mapping_correct"])
        self.assertEqual(scoring["season_episode_errors"], 1)
        self.assertEqual(scoring.get("tmdb_id_errors", 0), 0)

        result["plans"][1]["season"] = 2
        scoring = score_results([result], truth)
        self.assertEqual(scoring["correct"], 1)
        self.assertEqual(scoring.get("dangerous_false_auto", 0), 0)
        self.assertTrue(scoring["rows"][0]["identity_correct"])
        self.assertTrue(scoring["rows"][0]["mapping_correct"])
        self.assertEqual(scoring.get("season_episode_errors", 0), 0)

    def test_real_organizer_dry_run_uses_manifest_tree_without_writes(self):
        scraper = TMDBScraper()
        match = MatchResult(
            tmdb_id="123",
            title="Example Show",
            year="2024",
            media_type="tv",
            confidence=1.0,
            status="matched",
            matched_by="test",
            provider="tmdb",
            external_id="123",
        )
        detail = {
            "id": 123,
            "name": "Example Show",
            "first_air_date": "2024-01-01",
            "genres": [{"id": 16, "name": "Animation"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": 12}],
            "poster_path": "",
            "backdrop_path": "",
        }
        rules = _benchmark_rules()
        with isolated_benchmark_database(), patch.object(
            scraper, "match", return_value=match
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(_case(), scraper=scraper, base_rules=rules)
        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertEqual(result["plans"][0]["season"], 1)
        self.assertEqual(result["plans"][0]["episode"], 1)
        self.assertTrue(result["plans"][0]["target_path"])

    def test_filename_tmdb_markers_keep_specials_on_tv_identity(self):
        marker_formats = (
            "tmdb123",
            "tmdb 123",
            "tmdb+123",
            "tmdb-123",
            "tdmb+123",
        )
        for marker in marker_formats:
            with self.subTest(marker=marker):
                scraper = TMDBScraper()

                def match_from_tmdb(tmdb_id, media_type):
                    return _tv_match(str(tmdb_id), media_type=str(media_type))

                case = _case(files=[
                    {
                        "path": [f"Example Show S01E01 {marker}.mkv"],
                        "length": 100_000_000,
                    },
                    {
                        "path": ["Extra", f"Example Show - NCOP {marker}.mkv"],
                        "length": 100_000_000,
                    },
                ])
                with isolated_benchmark_database(), patch.object(
                    scraper, "match_from_tmdb", side_effect=match_from_tmdb
                ) as lookup, patch.object(
                    scraper, "get_detail", return_value=_tv_detail()
                ):
                    result = run_benchmark_case(
                        case, scraper=scraper, base_rules=_benchmark_rules()
                    )

                self.assertEqual(result["observed_action"], "auto")
                self.assertEqual(result["stats"]["need_confirm"], 0)
                self.assertEqual(lookup.call_count, 2)
                self.assertTrue(
                    all(call.args == ("123", "tv") for call in lookup.call_args_list)
                )
                self.assertEqual(
                    {
                        plan["source_path"]: plan["match"]["media_type"]
                        for plan in result["plans"]
                    },
                    {
                        f"Example Show S01E01 {marker}.mkv": "tv",
                        f"Extra/Example Show - NCOP {marker}.mkv": "tv",
                    },
                )
                self.assertEqual(
                    {
                        plan["source_path"]: (plan["season"], plan["episode"])
                        for plan in result["plans"]
                    },
                    {
                        f"Example Show S01E01 {marker}.mkv": (1, 1),
                        f"Extra/Example Show - NCOP {marker}.mkv": (0, 1),
                    },
                )

    def test_explicit_tmdb_markers_map_unique_absolute_episode_positions(self):
        marker_formats = ("tmdb37854", "tmdb 37854", "tdmb+37854")
        filenames = (
            ("One Piece - 1173 {marker}.mkv", None),
            ("One.Piece.S01E1173.1080p {marker}.mkv", 1),
        )
        detail = {
            "id": 37854,
            "name": "航海王",
            "first_air_date": "1999-10-20",
            "genres": [{"id": 16, "name": "Animation"}],
            "origin_country": ["JP"],
            "seasons": [
                {"season_number": 1, "episode_count": 100},
                {"season_number": 2, "episode_count": 1100},
            ],
            "poster_path": "",
            "backdrop_path": "",
        }
        for marker in marker_formats:
            for filename_template, expected_source_season in filenames:
                filename = filename_template.format(marker=marker)
                with self.subTest(marker=marker, filename=filename):
                    scraper = TMDBScraper()
                    with isolated_benchmark_database(), patch.object(
                        scraper,
                        "match_from_tmdb",
                        return_value=_tv_match("37854", title="航海王"),
                    ), patch.object(scraper, "get_detail", return_value=detail):
                        result = run_benchmark_case(
                            _case(files=[{
                                "path": [filename],
                                "length": 100_000_000,
                            }]),
                            scraper=scraper,
                            base_rules=_benchmark_rules(),
                        )

                    self.assertEqual(result["observed_action"], "auto")
                    self.assertEqual(result["stats"]["need_confirm"], 0)
                    plan = result["plans"][0]
                    self.assertEqual(
                        (plan["source_season"], plan["source_episode"]),
                        (expected_source_season, 1173),
                    )
                    self.assertEqual((plan["season"], plan["episode"]), (2, 1073))
                    self.assertEqual(plan["episode_mapping"]["mode"], "absolute")

    def test_explicit_tmdb_marker_keeps_missing_season_fail_closed(self):
        scraper = TMDBScraper()
        detail = _tv_detail(seasons=[
            {"season_number": 1, "episode_count": 12},
        ])
        case = _case(files=[{
            "path": ["Example Show S02E06 tmdb123.mkv"],
            "length": 100_000_000,
        }])
        with isolated_benchmark_database(), patch.object(
            scraper, "match_from_tmdb", return_value=_tv_match("123")
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        self.assertIn("季号在 TMDB 中不存在", result["plans"][0]["note"])

    def test_explicit_tmdb_marker_rejects_mismatched_returned_identity(self):
        scraper = TMDBScraper()
        case = _case(files=[{
            "path": ["Example Show S01E01 tmdb123.mkv"],
            "length": 100_000_000,
        }])
        with isolated_benchmark_database(), patch.object(
            scraper, "match_from_tmdb", return_value=_tv_match("999")
        ), patch.object(scraper, "get_detail") as detail:
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        self.assertIn("显式 TMDB 标记 123", result["plans"][0]["note"])
        self.assertIn("999", result["plans"][0]["note"])
        detail.assert_not_called()

    def test_explicit_tv_marker_rejects_movie_result_for_episode(self):
        scraper = TMDBScraper()
        case = _case(files=[{
            "path": ["Example Show S01E01 tmdb123.mkv"],
            "length": 100_000_000,
        }])
        with isolated_benchmark_database(), patch.object(
            scraper, "match_from_tmdb", return_value=_tv_match(
                "123", title="Example Movie", media_type="movie"
            )
        ), patch.object(scraper, "get_detail") as detail:
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertIn("识别结果不是剧集", result["plans"][0]["note"])
        detail.assert_not_called()

    def test_explicit_tv_marker_rejects_movie_result_for_special(self):
        scraper = TMDBScraper()
        case = _case(files=[{
            "path": ["Extra", "Example Show NCOP tmdb123.mkv"],
            "length": 100_000_000,
        }])
        with isolated_benchmark_database(), patch.object(
            scraper, "match_from_tmdb", return_value=_tv_match(
                "123", title="Example Movie", media_type="movie"
            )
        ):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertIn("识别结果不是剧集", result["plans"][0]["note"])

    def test_explicit_tv_marker_keeps_s00_when_tmdb_omits_special_season(self):
        scraper = TMDBScraper()
        case = _case(files=[{
            "path": ["Extra", "Example Show NCOP tmdb123.mkv"],
            "length": 100_000_000,
        }])
        detail = _tv_detail(seasons=[
            {"season_number": 1, "episode_count": 12},
        ])
        with isolated_benchmark_database(), patch.object(
            scraper, "match_from_tmdb", return_value=_tv_match("123")
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["plans"][0]["season"], 0)
        self.assertEqual(result["plans"][0]["episode"], 1)

    def test_contiguous_episode_package_prefers_clean_filename_identity(self):
        root = "[SubsMix] ToonsHub 完整包 [1080p]"
        scraper = TMDBScraper()

        def match_only_clean_filename(name, parent_path, *, media_type_hint=""):
            if (
                name == "Clean Series.S01E03.mkv"
                and parent_path == root
                and media_type_hint == ""
            ):
                return _tv_match(title="Clean Series")
            return MatchResult(
                media_type="tv",
                confidence=0.0,
                need_confirm=True,
                status="need_confirm",
                error="必须使用连续文件名身份",
            )

        case = _case(
            manifest_name=root,
            files=[
                {"path": ["Clean Series S01E01.mkv"], "length": 100_000_000},
                {"path": ["Clean Series S01E02.mkv"], "length": 100_000_000},
                {"path": ["Clean Series S01E03.mkv"], "length": 100_000_000},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper, "match", side_effect=match_only_clean_filename
        ) as match, patch.object(
            scraper, "get_detail", return_value=_tv_detail("Clean Series")
        ):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["need_confirm"], 0)
        self.assertEqual(match.call_count, 3)
        self.assertTrue(
            all(
                call.args == ("Clean Series.S01E03.mkv", root)
                and call.kwargs == {"media_type_hint": ""}
                for call in match.call_args_list
            )
        )
        self.assertEqual(
            {
                plan["source_path"]: (plan["season"], plan["episode"])
                for plan in result["plans"]
            },
            {
                "Clean Series S01E01.mkv": (1, 1),
                "Clean Series S01E02.mkv": (1, 2),
                "Clean Series S01E03.mkv": (1, 3),
            },
        )

    def test_directory_identity_placeholder_keeps_first_sequel_episode_position(self):
        scraper = TMDBScraper()
        case = _case(
            manifest_name="Example Show Second Season",
            files=[
                {"path": [f"Example Show S02E{episode:02d}.mkv"], "length": 100_000_000}
                for episode in range(1, 4)
            ],
        )
        detail = _tv_detail(seasons=[
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ])
        with isolated_benchmark_database(), patch.object(
            scraper, "match", return_value=_tv_match(title="Example Show")
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["need_confirm"], 0)
        self.assertEqual(
            {
                plan["source_path"]: (plan["season"], plan["episode"])
                for plan in result["plans"]
            },
            {
                "Example Show S02E01.mkv": (2, 1),
                "Example Show S02E02.mkv": (2, 2),
                "Example Show S02E03.mkv": (2, 3),
            },
        )

    def test_bracketed_roman_sequel_archives_to_second_season(self):
        scraper = TMDBScraper()
        filename = (
            "[orion origin] Gaikotsu Kishi-sama, Tadaima Isekai e "
            "Odekakechuu II [05] [1080p] [H265 AAC] [CHS_JPN].mp4"
        )
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu",
            seasons=[
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match(
                title="Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"
            ),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["need_confirm"], 0)
        plan = result["plans"][0]
        self.assertEqual((plan["source_season"], plan["source_episode"]), (2, 5))
        self.assertEqual((plan["season"], plan["episode"]), (2, 5))

    def test_implicit_roman_media_title_requires_confirmation(self):
        scraper = TMDBScraper()
        filename = "The Evil Dead II [01] [1080p] [HEVC].mkv"
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "The Evil Dead II",
            seasons=[
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match(title="The Evil Dead II"),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["action"], "skip")
        self.assertIn("正式名称", plan["note"])

    def test_parent_directory_roman_media_title_requires_confirmation(self):
        scraper = TMDBScraper()
        filename = "[LoliHouse] 05 [1080p].mkv"
        directory = "The Evil Dead II [01] [1080p]"
        case = _case(
            manifest_name=directory,
            files=[{
                "path": [directory, filename],
                "length": 100_000_000,
            }],
        )
        detail = _tv_detail(
            "The Evil Dead II",
            seasons=[
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match(title="The Evil Dead II"),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["action"], "skip")
        self.assertIn("正式名称", plan["note"])

    def test_localized_series_title_does_not_treat_season_alias_as_official_roman_title(self):
        scraper = TMDBScraper()
        filename = (
            "[Sakurato] Katainaka no Ossan, Kensei ni Naru II - 03 "
            "[1080p][WEB-DL][HEVC].mkv"
        )
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "乡下大叔成为剑圣",
            seasons=[
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        )
        # TMDB 的翻译/别名可能收录第二季发行名。它只能参与候选召回，
        # 不能反过来把发布名末尾的 II 判为作品正式标题。
        detail["alternative_titles"] = {
            "results": [{"title": "Katainaka no Ossan, Kensei ni Naru II"}]
        }
        detail["original_name"] = "片田舎のおっさん、剣聖になる"
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match("260823", title="乡下大叔成为剑圣"),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["need_confirm"], 0)
        plan = result["plans"][0]
        self.assertEqual((plan["source_season"], plan["source_episode"]), (2, 3))
        self.assertEqual((plan["season"], plan["episode"]), (2, 3))

    def test_unique_tmdb_season_name_maps_bare_episode(self):
        scraper = TMDBScraper()
        filename = (
            "[ANi] BLEACH 死神 千年血戰篇-禍進譚- - 43 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "死神",
            seasons=[
                {"season_number": 1, "name": "代理死神篇", "episode_count": 366},
                {"season_number": 2, "name": "千年血战篇-祸进谭-", "episode_count": 52},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match("30984", title="死神"),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["need_confirm"], 0)
        plan = result["plans"][0]
        self.assertEqual((plan["source_season"], plan["source_episode"]), (None, 43))
        self.assertEqual((plan["season"], plan["episode"]), (2, 43))
        self.assertEqual(plan["episode_mapping"]["mode"], "season_title")

    def test_non_tmdb_provider_does_not_use_tmdb_season_title_mapping(self):
        scraper = TMDBScraper()
        filename = "Example Show Second Story Arc - 03 [1080p].mkv"
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "Example Show",
            seasons=[
                {
                    "season_number": 1,
                    "name": "Season One",
                    "episode_count": 12,
                },
                {
                    "season_number": 2,
                    "name": "Second Story Arc",
                    "episode_count": 12,
                },
            ],
        )
        match = _tv_match(title="Example Show")
        match.provider = "tvdb"
        match.external_id = "tvdb-123"
        match.metadata = detail
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=match,
        ), patch.object(scraper, "get_detail", return_value=detail) as get_detail:
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["action"], "skip")
        self.assertIn("当前候选不是 TMDB 身份", plan["note"])
        self.assertNotEqual(
            (plan.get("episode_mapping") or {}).get("mode"),
            "season_title",
        )
        get_detail.assert_not_called()

    def test_isolated_high_bare_episode_requires_confirmation(self):
        scraper = TMDBScraper()
        filename = "[LoliHouse] Hyakkano - 29 [WebRip 1080p HEVC-10bit AAC].mkv"
        case = _case(files=[{"path": [filename], "length": 100_000_000}])
        detail = _tv_detail(
            "Hyakkano",
            seasons=[
                {"season_number": 1, "episode_count": 31},
                {"season_number": 2, "episode_count": 12},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper,
            "match",
            return_value=_tv_match(title="Hyakkano"),
        ), patch.object(scraper, "get_detail", return_value=detail):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "confirm")
        self.assertEqual(result["stats"]["need_confirm"], 1)
        plan = result["plans"][0]
        self.assertEqual(plan["action"], "skip")
        self.assertIn("孤立高集号", plan["note"])

    def test_root_s00_files_do_not_collide_with_first_regular_episode(self):
        scraper = TMDBScraper()
        case = _case(
            manifest_name="Example Show S1+Sp",
            files=[
                {"path": ["Example Show S00E01.mkv"], "length": 100_000_000},
                {"path": ["Example Show S00E02.mkv"], "length": 100_000_000},
                {"path": ["Example Show S01E01.mkv"], "length": 100_000_000},
                {"path": ["Example Show S01E02.mkv"], "length": 100_000_000},
                {"path": ["Example Show S01E03.mkv"], "length": 100_000_000},
            ],
        )
        with isolated_benchmark_database(), patch.object(
            scraper, "match", return_value=_tv_match(title="Example Show")
        ), patch.object(
            scraper, "get_detail", return_value=_tv_detail("Example Show")
        ):
            result = run_benchmark_case(
                case, scraper=scraper, base_rules=_benchmark_rules()
            )

        self.assertEqual(result["observed_action"], "auto")
        self.assertEqual(result["stats"]["skipped"], 0)
        by_source = {plan["source_path"]: plan for plan in result["plans"]}
        self.assertEqual(
            (by_source["Example Show S00E01.mkv"]["season"],
             by_source["Example Show S00E01.mkv"]["episode"]),
            (0, 1),
        )
        self.assertEqual(
            (by_source["Example Show S00E02.mkv"]["season"],
             by_source["Example Show S00E02.mkv"]["episode"]),
            (0, 2),
        )
        self.assertTrue(
            by_source["Example Show S00E01.mkv"]["target_path"].endswith("/Specials")
        )
        self.assertEqual(
            (by_source["Example Show S01E01.mkv"]["season"],
             by_source["Example Show S01E01.mkv"]["episode"]),
            (1, 1),
        )

    def test_safe_match_whitelists_fusion_evidence_and_normalizes_numbers(self):
        result = RecognitionResult(
            tmdb_id="42",
            external_id="42",
            provider="tmdb",
            title="The Eminence in Shadow",
            year="2024",
            media_type="movie",
            confidence=float("nan"),
            status="matched",
            matched_by="ai_tavily_tmdb_revalidated",
            error="Authorization: Bearer bearer-secret",
            metadata={
                "recognition_evidence": {
                    "mode": "llm_tavily_tmdb",
                    "tmdb_id": "42",
                    "llm_confidence": float("inf"),
                    "source_web_score": 0.885,
                    "web_tmdb_score": 0.845,
                    "strict_tmdb_confidence": 0.96,
                    "raw_prompt": "api_key=json-secret",
                },
            },
            ai_diagnostic={
                "attempted": True,
                "reason": "deterministic_failed",
                "error": "password: provider-secret",
                "input": {"normalized_title": "private source title"},
                "output": {
                    "title": "The Eminence in Shadow",
                    "year": 2024,
                    "media_type": "movie",
                    "confidence": float("nan"),
                    "raw_response": "secret response",
                },
                "tmdb_revalidation": {
                    "passed": True,
                    "resolution_mode": "llm_tavily_tmdb",
                    "tavily_corroboration": {
                        "attempted": True,
                        "status": "matched",
                        "passed": True,
                        "source_web_score": 0.885,
                        "web_tmdb_score": 0.845,
                        "matched_hint": "private web title",
                    },
                },
            },
        )
        result.score = "invalid-number"

        payload = _safe_match(result)
        rendered = str(payload)

        self.assertEqual(payload["confidence"], 0.0)
        self.assertEqual(payload["score"], 0.0)
        self.assertEqual(
            payload["recognition_evidence"]["llm_confidence"], 0.0
        )
        self.assertEqual(
            payload["ai_evidence"]["output"]["confidence"], 0.0
        )
        self.assertNotIn("raw_prompt", rendered)
        self.assertNotIn("raw_response", rendered)
        self.assertNotIn("private source title", rendered)
        self.assertNotIn("private web title", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("provider-secret", rendered)
        self.assertIn("[redacted]", rendered)

    def test_persisted_reports_redact_urls_magnets_and_tokens(self):
        result = {
            "case_id": "fixture-1",
            "bucket": "ambiguous",
            "observed_action": "error",
            "manifest_video_files": 1,
            "elapsed_seconds": 0.1,
            "title": '{"api_key":"json-secret"} https://example.invalid/private',
            "error": (
                "magnet:?xt=urn:btih:abc token=secret "
                "Authorization: Bearer bearer-secret"
            ),
            "stats": {},
            "plans": [],
        }
        summary = {
            "cases": 1,
            "automation_rate": 0,
            "manual_confirmation_rate": 0,
            "error_rate": 1,
            "truth_scoring": {"accuracy": None, "note": ""},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "report.json", {"results": [result]})
            write_jsonl(root / "cases.jsonl", [result])
            write_csv_report(root / "report.csv", [result])
            write_html_report(root / "report.html", summary, [result])
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.iterdir()
            )
        self.assertNotIn("https://example.invalid", combined)
        self.assertNotIn("magnet:?", combined)
        self.assertNotIn("token=secret", combined)
        self.assertNotIn("json-secret", combined)
        self.assertNotIn("bearer-secret", combined)
        self.assertIn("[redacted]", combined)


if __name__ == "__main__":
    unittest.main()
