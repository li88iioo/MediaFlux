"""本地媒体识别、统一命名和预览测试。"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.modules.local_media_service import LocalMediaService
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class FakeScraper:
    supports_parent_path = True

    def __init__(self, match: MatchResult):
        self.result = match
        self.parents: list[str] = []
        self.media_type_hints: list[str] = []

    def match(self, filename: str, parent_path: str = "", *, media_type_hint: str = ""):
        self.parents.append(parent_path)
        self.media_type_hints.append(media_type_hint)
        return self.result

    def match_from_tmdb(self, tmdb_id: str, media_type: str):
        return MatchResult(tmdb_id=str(tmdb_id), title=self.result.title, year=self.result.year,
                           media_type=media_type, confidence=1.0, status="matched")

    def parse_media(self, filename: str, parent_path: str = "", match=None):
        import re
        season = re.search(r"(?i)S(\d{1,2})", filename)
        episode = re.search(r"(?i)E(\d{1,3})", filename)
        return release_parse_result(
            {
                "season": int(season.group(1)) if season else None,
                "episode": int(episode.group(1)) if episode else None,
                "title": "", "year": "", "type": self.result.media_type,
            },
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str):
        genre = 16 if self.result.title == "攻壳机动队" else 28
        return {"genres": [{"id": genre}], "origin_country": ["JP"],
                "release_date": "2025-01-01", "first_air_date": "2026-01-01"}

    def search_candidates(self, query, year, media_type):
        return []


class LocalMediaServiceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        probe = patch(
            "app.modules.local_media_service.probe_local_media_profile", return_value=None,
        )
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def _source(self, source_root: Path, target_root: Path, category: str) -> int:
        source_id = db.create_local_media_source(
            name=f"source-{source_root.name}-{category}", qb_profile="", qb_path_prefix="",
            local_root=str(source_root), owner="admin",
        )
        db.upsert_local_library_target(source_id, category, str(target_root), owner="admin")
        return source_id

    def test_movie_preview_uses_independent_media_directory_and_organizer_naming(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Creation.of.the.Gods.2.2025.1080p.H265.mkv"
            movie.write_bytes(b"movie")
            source_id = self._source(source_root, target_root, "movie")
            scraper = FakeScraper(MatchResult(tmdb_id="1155281", title="封神第二部：战火西岐",
                                              year="2025", media_type="movie", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            with patch("app.modules.local_media_service.OrganizeRules.from_config") as rules_factory:
                from app.modules.organize import OrganizeRules
                rules_factory.return_value = OrganizeRules(region_split=False, year_split=False, naming_scope="both")
                preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            target = Path(preview["plans"][0]["target_path"])
            self.assertIn("封神第二部：战火西岐 (2025) {tmdb-1155281}", target.parent.name)
            self.assertIn("封神第二部：战火西岐.2025", target.name)

    def test_local_preview_reuses_one_bounded_probe_budget_for_all_videos(self):
        from app.modules.media_probe import ProbeBudget
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "anime-downloads"
            target_root = root / "anime-library"
            show = source_root / "The Ghost in the Shell"
            show.mkdir(parents=True)
            target_root.mkdir()
            (show / "The Ghost in the Shell.S01E01.mkv").write_bytes(b"episode-1")
            (show / "The Ghost in the Shell.S01E02.mkv").write_bytes(b"episode-2")
            source_id = self._source(source_root, target_root, "anime")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="255358", title="攻壳机动队", year="2026",
                media_type="tv", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, show)
            with patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=OrganizeRules(region_split=False, year_split=False),
            ):
                preview = service.preview("admin", inspection["inspection_id"])

        self.assertEqual(preview["status"], "planned")
        self.assertEqual(len(preview["plans"]), 2)
        budgets = [call.kwargs.get("budget") for call in self.probe.call_args_list]
        self.assertEqual(len(budgets), 2)
        self.assertIsInstance(budgets[0], ProbeBudget)
        self.assertIs(budgets[0], budgets[1])
        self.assertIsNotNone(budgets[0].remaining_seconds())

    def test_rules_snapshot_keeps_manual_execution_consistent_with_preview(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "snapshot-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Movie.2026.mkv").write_bytes(b"movie")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            original = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                movie_dir_template="ORIGINAL-${showTitle}",
                movie_template="ORIGINAL-${showTitle}.${ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=original):
                preview = service.preview("admin", inspection["inspection_id"])
            changed = OrganizeRules(
                region_split=True, year_split=True, naming_scope="both",
                movie_dir_template="CHANGED-${showTitle}",
                movie_template="CHANGED-${showTitle}.${ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=changed):
                replay = service.preview(
                    "admin", inspection["inspection_id"],
                    rules_snapshot=preview["rules_snapshot"],
                )
            self.assertEqual(replay["plans"][0]["target_path"], preview["plans"][0]["target_path"])
            self.assertIn("Movie (2026) {tmdb-1}", replay["plans"][0]["target_path"])
            self.assertNotIn("ORIGINAL-", replay["plans"][0]["target_path"])
            self.assertNotIn("CHANGED-", replay["plans"][0]["target_path"])

    def test_rules_snapshot_excludes_token_and_ignores_client_runtime_endpoint(self):
        from app.modules.organize import OrganizeRules

        configured = OrganizeRules(
            nsfw_enabled=True,
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
            nsfw_metatube_token="server-secret",
            nsfw_timeout_seconds=8,
        )
        snapshot = LocalMediaService._serialize_rules_snapshot(configured)
        self.assertNotIn("server-secret", snapshot)
        self.assertNotIn("nsfw_metatube_token", snapshot)
        self.assertNotIn("nsfw_metatube_endpoint", snapshot)

        tampered = json.loads(snapshot)
        tampered.update({
            "nsfw_enabled": True,
            "nsfw_metatube_endpoint": "http://169.254.169.254",
            "nsfw_metatube_token": "attacker",
            "nsfw_timeout_seconds": 30,
        })
        with patch(
            "app.modules.local_media_service.OrganizeRules.from_config",
            return_value=configured,
        ):
            restored = LocalMediaService._restore_rules_snapshot(json.dumps(tampered))
        self.assertEqual(restored.nsfw_metatube_endpoint, "http://127.0.0.1:8080")
        self.assertEqual(restored.nsfw_metatube_token, "server-secret")
        self.assertEqual(restored.nsfw_timeout_seconds, 8)

    def test_local_preview_applies_shared_large_file_conflict_strategy(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "conflict-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"new-version-is-larger")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both", conflict_strategy=2
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                first = service.preview("admin", inspection["inspection_id"])
                target = Path(first["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"old")
                second = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(second["plans"][0]["action"], "replace")
            self.assertIn("替换", second["plans"][0]["note"])

    def test_manual_conflict_always_uses_safe_replace_but_automatic_keeps_strategy(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "manual-conflict-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"incoming-version")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, incoming)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both", conflict_strategy=1
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                initial = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                target = Path(initial["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"existing-library-version")
                manual = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                automatic = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie",
                    automatic=True,
                )
            self.assertEqual(manual["plans"][0]["action"], "replace")
            self.assertIn("安全覆盖", manual["plans"][0]["note"])
            self.assertEqual(Path(manual["plans"][0]["target_path"]), target)
            self.assertEqual(automatic["plans"][0]["action"], "skip")

    def test_manual_execution_replaces_existing_target_without_numbered_duplicate(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "manual-execute-downloads"
            target_root = root / "manual-execute-library"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"incoming-version")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, incoming)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                conflict_strategy=1, emby_refresh=False,
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                preview = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                target = Path(preview["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"existing-library-version")
                task_id = service.create_manual_task(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie",
                    rules_snapshot=preview["rules_snapshot"],
                )
                self.assertTrue(db.claim_local_media_task(
                    task_id, expected="waiting_stable", owner="admin"
                ))
                result = service.execute_task("admin", task_id)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(target.read_bytes(), b"incoming-version")
            self.assertFalse(incoming.exists())
            self.assertEqual(list(target_root.rglob("*.mkv")), [target])
            self.assertEqual(list(target.parent.glob(".*.mediaflux-replaced-*")), [])

    def test_tv_episode_and_language_subtitle_are_planned_together(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "anime"
            show = source_root / "The Ghost in the Shell"
            show.mkdir(parents=True); target_root.mkdir()
            video = show / "[LoliHouse] The Ghost in the Shell - S01E03.mkv"
            subtitle = show / "[LoliHouse] The Ghost in the Shell - S01E03.zh.ass"
            video.write_bytes(b"video"); subtitle.write_text("subtitle")
            source_id = self._source(source_root, target_root, "anime")
            scraper = FakeScraper(MatchResult(tmdb_id="255358", title="攻壳机动队", year="2026",
                                              media_type="tv", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, show)
            with patch("app.modules.local_media_service.OrganizeRules.from_config") as rules_factory:
                from app.modules.organize import OrganizeRules
                rules_factory.return_value = OrganizeRules(region_split=False, year_split=False, naming_scope="both")
                preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            self.assertEqual([item["role"] for item in preview["plans"]], ["video", "subtitle"])
            video_target = Path(preview["plans"][0]["target_path"])
            subtitle_target = Path(preview["plans"][1]["target_path"])
            self.assertIn("S01E03", preview["plans"][0]["target_name"])
            self.assertTrue(preview["plans"][1]["target_name"].endswith(".zh.ass"))
            self.assertEqual(video_target.parent.name, "Season 1")
            self.assertEqual(video_target.parent.parent.name, "攻壳机动队 (2026) {tmdb-255358}")
            self.assertEqual(subtitle_target.parent, video_target.parent)
            self.assertEqual(scraper.parents, ["The Ghost in the Shell"])


    def test_inspection_filters_non_media_files_from_snapshot(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads-filter"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Movie.2026.mkv").write_bytes(b"movie")
            (source_root / "Movie.2026.zh.ass").write_text("subtitle")
            (source_root / "readme.txt").write_text("ignored")
            (source_root / "archive.zip").write_bytes(b"ignored")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Movie", year="2026", media_type="movie", confidence=1.0,
            )))

            inspection = service.inspect_source("admin", source_id, source_root)

            self.assertEqual(inspection["file_count"], 2)
            self.assertEqual(inspection["video_count"], 1)
            self.assertEqual(
                {item["name"] for item in inspection["files"]},
                {"Movie.2026.mkv", "Movie.2026.zh.ass"},
            )

    def test_single_tv_file_can_override_season_and_keep_parsed_episode(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-season-override"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.S01E07.mkv"
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            self.assertEqual(inspection["selected_kind"], "file")
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                season_override=2,
            )
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["position_overrides"], {"season": 2, "episode": None})
            self.assertEqual(preview["matches"][0]["season"], 2)
            self.assertEqual(preview["matches"][0]["episode"], 7)
            self.assertIn("S02E07", preview["plans"][0]["target_name"])
            self.assertEqual(Path(preview["plans"][0]["target_path"]).parent.name, "Season 2")

    def test_single_tv_file_episode_override_defaults_to_season_one(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-episode-override"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.mkv"
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                episode_override=9,
            )
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["position_overrides"], {"season": 1, "episode": 9})
            self.assertIn("S01E09", preview["plans"][0]["target_name"])

    def test_manual_task_execution_reuses_persisted_position_override(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-persisted-position"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.S01E07.mkv"
            episode.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="persisted-position-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), media_type="tv", mode="preview_only",
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(source_id, "tv", str(target_root), owner="admin")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                season_override=2,
            )
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                rules_snapshot=preview["rules_snapshot"], season_override=2,
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            result = service.execute_task("admin", task_id)
            self.assertEqual(result["status"], "completed")
            self.assertIn("S02E07", result["preview"]["plans"][0]["target_name"])
            self.assertTrue(episode.exists())

    def test_directory_scope_rejects_one_episode_override(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-directory-override"; target_root = root / "tv"
            show = source_root / "Show"
            show.mkdir(parents=True); target_root.mkdir()
            (show / "Show.S01E01.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, show)
            self.assertEqual(inspection["selected_kind"], "directory")
            with self.assertRaisesRegex(Exception, "目录整理只能指定归档季"):
                service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                    episode_override=2,
                )

    def test_manual_tmdb_selection_does_not_apply_preprocess_position_offsets(self):
        class PositionAwareScraper(FakeScraper):
            def parse_media(self, filename, parent_path="", match=None):
                parsed = super().parse_media(filename, parent_path, match)
                return dataclasses.replace(
                    parsed,
                    source_season=1,
                    source_episode=2,
                    effective_season=9,
                    effective_episode=99,
                )

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "manual-downloads"; target_root = root / "manual-tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.S01E02.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            scraper = PositionAwareScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            ))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv"
            )
            self.assertEqual(preview["status"], "planned")
            self.assertIn("S01E02", preview["plans"][0]["target_name"])
            self.assertNotIn("S09E99", preview["plans"][0]["target_name"])

    def test_source_media_type_is_forwarded_to_automatic_recognition(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.S01E01.mkv").write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="tv-source", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                media_type="tv", owner="admin",
            )
            db.upsert_local_library_target(source_id, "tv", str(target_root), owner="admin")
            scraper = FakeScraper(MatchResult(
                tmdb_id="1", title="Show", year="2026", media_type="tv", confidence=1.0
            ))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(scraper.media_type_hints, ["tv"])

    def test_preview_only_task_finishes_without_moving_files(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"; movie.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="preview-source", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                stable_seconds=0, mode="preview_only", owner="admin",
            )
            db.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
            task_id = db.create_local_media_task(source_id, "", str(movie), owner="admin", trigger="manual")
            self.assertTrue(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            with patch("app.modules.local_media_service.LocalMoveTransaction.execute") as execute:
                result = service.execute_task("admin", task_id)
            execute.assert_not_called()
            self.assertEqual(result["status"], "completed")
            self.assertTrue(movie.exists())
            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.status, "completed")
            self.assertIn("仅预览模式", task.warning)

    def test_tv_without_episode_requires_manual_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            scraper = FakeScraper(MatchResult(tmdb_id="1", title="Show", year="2026",
                                              media_type="tv", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "requires_manual")
            self.assertEqual(preview["candidates"][0]["tmdb_id"], "1")
            self.assertEqual(preview["files"], [{"name": "Show.mkv"}])
            self.assertTrue(preview["snapshot_digest"])
            self.assertEqual(list(target_root.rglob("*")), [])


    def test_local_media_ignores_legacy_scope_and_template_overrides(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"; movie.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="scope-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            db.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            legacy = OrganizeRules(
                region_split=False, year_split=False, naming_scope="local",
                movie_dir_template="LOCAL-{title}", movie_template="LOCAL-{title}.{ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=legacy):
                preview = service.preview("admin", inspection["inspection_id"])
            target_path = preview["plans"][0]["target_path"]
            self.assertIn("Movie (2026) {tmdb-1}", target_path)
            self.assertNotIn("LOCAL-", target_path)


if __name__ == "__main__":
    unittest.main()
