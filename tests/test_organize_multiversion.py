"""光鸭整理多版本共存策略回归测试。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.clients.guangya import GuangYaFile
from app.config import web_credentials
from app.main import create_app
from app.modules.media_variant import MediaVariant, classify_variant, variants_can_coexist
from app.modules.naming import append_variant_tags, template_has_media_identity
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_postprocess import media_role
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class MediaVariantClassificationTests(unittest.TestCase):
    def test_dovi_and_standard_variants_can_coexist_when_enabled(self):
        rules = OrganizeRules(keep_multi_versions=True)
        dovi = classify_variant("Movie.2160p.DoVi.TrueHD.mkv", None)
        standard = classify_variant("Movie.2160p.SDR.TrueHD.mkv", None)

        self.assertEqual(dovi.dolby_vision, True)
        self.assertEqual(standard.dolby_vision, False)
        self.assertTrue(variants_can_coexist(dovi, standard, rules))

    def test_atmos_and_non_atmos_variants_can_coexist_when_enabled(self):
        rules = OrganizeRules(keep_multi_versions=True)
        atmos = classify_variant("Movie.1080p.WEB-DL.DDP5.1.Atmos.mkv", None)
        standard_audio = classify_variant("Movie.1080p.WEB-DL.DDP5.1.mkv", None)

        self.assertEqual(atmos.atmos, True)
        self.assertEqual(standard_audio.atmos, False)
        self.assertTrue(variants_can_coexist(atmos, standard_audio, rules))

    def test_non_atmos_hyphen_and_dot_forms_are_negative_before_atmos_match(self):
        for name in ("Movie.Non-Atmos.mkv", "Movie.Non.Atmos.mkv"):
            with self.subTest(name=name):
                self.assertIs(classify_variant(name, None).atmos, False)

    def test_remux_and_encode_require_the_dedicated_toggle(self):
        remux = classify_variant("Movie.2160p.UHD.BluRay.Remux.HEVC.mkv", None)
        encode = classify_variant("Movie.2160p.BluRay.x265.mkv", None)

        self.assertFalse(variants_can_coexist(
            remux, encode,
            OrganizeRules(keep_multi_versions=True, keep_remux_variant=False),
        ))
        self.assertTrue(variants_can_coexist(
            remux, encode,
            OrganizeRules(keep_multi_versions=True, keep_remux_variant=True),
        ))

    def test_identical_or_unknown_variants_do_not_bypass_same_bucket_priority(self):
        rules = OrganizeRules(keep_multi_versions=True, keep_remux_variant=True)
        first = classify_variant("Movie.2160p.DoVi.Atmos.Remux.mkv", None)
        second = classify_variant("Movie.2160p.DoVi.Atmos.Remux-GROUP.mkv", None)
        unknown = classify_variant("Movie.mkv", None)

        self.assertEqual(first, second)
        self.assertFalse(variants_can_coexist(first, second, rules))
        self.assertEqual(unknown, MediaVariant())
        self.assertFalse(variants_can_coexist(first, unknown, rules))

    def test_probe_profile_fills_explicit_standard_variant_metadata(self):
        profile = SimpleNamespace(
            dynamic_range="HDR10",
            audio_codec="EAC3",
            source="WEB-DL",
        )

        self.assertEqual(
            classify_variant("Movie.mkv", profile),
            MediaVariant(dolby_vision=False, atmos=False, remux=False),
        )


    def test_explicit_probe_variant_flags_override_filename_inference(self):
        profile = SimpleNamespace(
            dolby_vision=True, atmos=True, dynamic_range="HDR10",
            audio_codec="EAC3", source="WEB-DL",
        )
        self.assertEqual(
            classify_variant("Movie.Standard.NonAtmos.mkv", profile),
            MediaVariant(dolby_vision=True, atmos=True, remux=False),
        )

    def test_generated_negative_variant_tags_round_trip_through_classifier(self):
        self.assertEqual(
            classify_variant("Movie.Standard.NonAtmos.Encode.mkv", None),
            MediaVariant(dolby_vision=False, atmos=False, remux=False),
        )


class DirectoryIdentityTemplateTests(unittest.TestCase):
    def test_template_identity_detection_understands_aliases(self):
        self.assertTrue(template_has_media_identity("${showTitle} ${tmdbTag}"))
        self.assertTrue(template_has_media_identity("{showTitle}-{showTmdb}"))
        self.assertFalse(template_has_media_identity("${showTitle} (${showYear})"))
        self.assertFalse(template_has_media_identity(
            "${showTitle} ${tmdbTag}", tmdb_id="", identity_id="metatube:javbus:x",
        ))
        self.assertTrue(template_has_media_identity(
            "${showTitle} ${identityTag}", tmdb_id="", identity_id="metatube:javbus:x",
        ))

    def test_same_unmarked_media_root_cannot_bind_two_tmdb_ids_in_one_batch(self):
        client = Mock()
        client.list_dir.return_value = []
        organizer = Organizer(client=client, scraper=Mock())
        rules = OrganizeRules(target_dir_id="target")
        first = OrganizePlan(
            file_id="a", original_name="a.mkv", original_path="",
            match=MatchResult(tmdb_id="1", media_type="tv", title="Same", year="2024"),
            target_path="动漫/Same (2024)/Season 01",
            media_root_path="动漫/Same (2024)", identity_guard_required=True,
            new_name="Same.S01E01.mkv", action="move",
        )
        second = OrganizePlan(
            file_id="b", original_name="b.mkv", original_path="",
            match=MatchResult(tmdb_id="2", media_type="tv", title="Same", year="2024"),
            target_path="动漫/Same (2024)/Season 02",
            media_root_path="动漫/Same (2024)", identity_guard_required=True,
            new_name="Same.S02E01.mkv", action="move",
        )
        with patch.object(organizer, "_historical_root_identities", return_value=set()):
            organizer._preview_conflicts([first, second], rules)

        self.assertEqual(first.action, "move")
        self.assertEqual(second.action, "conflict")
        self.assertEqual(second.conflict_decision, "identity_blocked")

    def test_history_lookup_is_cached_once_for_same_media_root(self):
        client = Mock()
        client.list_dir.return_value = []
        organizer = Organizer(client=client, scraper=Mock())
        plans = [
            OrganizePlan(
                file_id=f"e{episode}", original_name=f"e{episode}.mkv", original_path="",
                match=MatchResult(tmdb_id="1", media_type="tv", title="Same", year="2024"),
                target_path="动漫/Same (2024)/Season 01",
                media_root_path="动漫/Same (2024)", identity_guard_required=True,
                new_name=f"Same.S01E{episode:02d}.mkv", action="move",
            )
            for episode in (1, 2, 3)
        ]
        with patch.object(
            organizer, "_historical_root_identities", return_value=set()
        ) as history:
            organizer._apply_identity_guards(plans)

        history.assert_called_once_with("动漫/Same (2024)")
        self.assertTrue(all(plan.action == "move" for plan in plans))

    def test_historical_identity_blocks_different_tmdb_from_unmarked_root(self):
        client = Mock()
        client.list_dir.return_value = []
        organizer = Organizer(client=client, scraper=Mock())
        plan = OrganizePlan(
            file_id="b", original_name="b.mkv", original_path="",
            match=MatchResult(tmdb_id="2", media_type="movie", title="Same", year="2024"),
            target_path="电影/Same (2024)", media_root_path="电影/Same (2024)",
            identity_guard_required=True, new_name="Same.2024.mkv", action="move",
        )
        with patch.object(
            organizer, "_historical_root_identities", return_value={("movie", "1")}
        ):
            organizer._preview_conflicts([plan], OrganizeRules(target_dir_id="target"))

        self.assertEqual(plan.action, "conflict")
        self.assertEqual(plan.conflict_decision, "identity_blocked")


class MultiVersionNamingTests(unittest.TestCase):
    def test_variant_tags_are_stable_deduplicated_and_extension_safe(self):
        self.assertEqual(
            append_variant_tags("Movie.2026.mkv", ("DoVi", "Atmos", "Remux")),
            "Movie.2026.DoVi.Atmos.Remux.mkv",
        )
        self.assertEqual(
            append_variant_tags("Movie.2026.DoVi.Atmos.mkv", ("DoVi", "Atmos")),
            "Movie.2026.DoVi.Atmos.mkv",
        )
        long_name = append_variant_tags("M" * 238 + ".mkv", ("Standard", "NonAtmos"))
        self.assertLessEqual(len(long_name), 240)
        self.assertTrue(long_name.endswith(".Standard.NonAtmos.mkv"))

    def test_build_new_name_carries_variant_tags_even_without_media_info_naming(self):
        organizer = Organizer(client=object(), scraper=object())
        rules = OrganizeRules(
            keep_multi_versions=True,
            keep_remux_variant=True,
            media_info_enabled=False,
        )
        match = MatchResult(
            tmdb_id="123",
            title="Variant Movie",
            year="2026",
            media_type="movie",
            confidence=1.0,
        )
        file = GuangYaFile(
            file_id="incoming",
            name="Variant.Movie.2026.2160p.DoVi.Atmos.Remux.mkv",
            is_dir=False,
            size=100,
        )

        name = organizer.build_new_name(match, file, {"season": None, "episode": None}, rules)

        self.assertIn(".Remux.", name)
        self.assertIn(".DoVi.", name)
        self.assertTrue(name.endswith(".Atmos.mkv"), name)

    def test_build_new_name_uses_probe_flags_for_variant_tags(self):
        organizer = Organizer(client=object(), scraper=object())
        rules = OrganizeRules(keep_multi_versions=True, media_info_enabled=True)
        match = MatchResult(
            tmdb_id="123", title="Variant Movie", year="2026",
            media_type="movie", confidence=1.0,
        )
        file = GuangYaFile(
            file_id="incoming", name="Variant.Movie.2026.mkv",
            is_dir=False, size=100,
        )
        profile = SimpleNamespace(
            dolby_vision=True, atmos=True, dynamic_range="HDR10",
            audio_codec="EAC3", source="WEB-DL",
        )

        name = organizer.build_new_name(
            match, file, {"season": None, "episode": None}, rules,
            media_info_override="2160p.HDR10.H.265.EAC3",
            media_variant_override=profile,
        )

        self.assertTrue(name.endswith(".DoVi.Atmos.mkv"), name)



class MultiVersionPriorityTests(unittest.TestCase):
    def test_dovi_priority_is_not_lost_when_media_info_uses_dovi_label(self):
        organizer = Organizer(client=object(), scraper=object())
        rules = OrganizeRules(conflict_strategy=2, dolby_first=True)
        existing = GuangYaFile(
            file_id="existing",
            name="Movie.2026.2160p.SDR.mkv",
            is_dir=False,
            size=10_000,
        )
        incoming = GuangYaFile(
            file_id="incoming",
            name="Movie.2026.2160p.DoVi.mkv",
            is_dir=False,
            size=1_000,
        )

        self.assertTrue(
            organizer.should_replace(existing, incoming, "Movie.2026.2160p.DoVi.mkv", rules)
        )

    def test_rules_read_both_multiversion_feature_flags(self):
        values = {
            "GY_ORGANIZE_KEEP_MULTI_VERSIONS": True,
            "GY_ORGANIZE_KEEP_REMUX_VARIANT": True,
        }

        with patch("app.modules.organize.get_bool", side_effect=lambda key, default=False: values.get(key, default)):
            rules = OrganizeRules.from_config()

        self.assertTrue(rules.keep_multi_versions)
        self.assertTrue(rules.keep_remux_variant)


class _VariantScraper:
    match_result = MatchResult(
        tmdb_id="123",
        title="Variant Movie",
        year="2026",
        media_type="movie",
        confidence=1.0,
    )

    def match(self, _filename):
        return self.match_result

    def parse_media(self, filename, parent_path="", match=None):
        position = re.search(r"(?i)S(\d{1,2})(?:E(\d{1,3}))?", filename or "")
        return release_parse_result(
            {
                "season": int(position.group(1)) if position else None,
                "episode": int(position.group(2)) if position and position.group(2) else None,
                "type": "tv" if position else "movie",
            },
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, _tmdb_id, _media_type):
        return {"genres": [], "origin_country": ["US"]}


class _TvVariantScraper(_VariantScraper):
    match_result = MatchResult(
        tmdb_id="113256",
        title="Better Show",
        year="2021",
        media_type="tv",
        confidence=1.0,
    )

    def get_detail(self, _tmdb_id, _media_type):
        return {
            "genres": [{"id": 16, "name": "动画"}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 3, "episode_count": 12}],
        }


class MediaIdentitySafetyTests(unittest.TestCase):
    def test_ambiguous_movie_and_tv_identity_fail_closed(self):
        organizer = Organizer(client=Mock(), scraper=_VariantScraper())
        rules = OrganizeRules()
        movie = OrganizePlan(
            file_id="new", original_name="Main.Movie.2026.mkv",
            original_path="/source", new_name="Main.Movie.2026.mkv",
            match=MatchResult(
                tmdb_id="123", title="Main Movie", year="2026",
                media_type="movie",
            ),
        )
        self.assertFalse(organizer._same_media_identity(
            movie,
            GuangYaFile("extra", "Behind.The.Scenes.2026.mkv", False),
            rules,
        ))
        for file_id, filename in (
            ("extras", "Extras.mkv"),
            ("interview", "Interview.mkv"),
            ("deleted", "Deleted.Scenes.mkv"),
            ("unrelated", "Unrelated.Movie.2026.1080p.mkv"),
        ):
            with self.subTest(filename=filename):
                self.assertFalse(organizer._same_media_identity(
                    movie,
                    GuangYaFile(file_id, filename, False),
                    rules,
                ))
        self.assertFalse(organizer._same_media_identity(
            movie,
            GuangYaFile(
                "wrong-tmdb",
                "Main.Movie.2026.{tmdb-999}.1080p.mkv",
                False,
            ),
            rules,
        ))
        self.assertTrue(organizer._same_media_identity(
            movie,
            GuangYaFile(
                "same-tmdb",
                "Main.Movie.2026.{tmdb-123}.1080p.mkv",
                False,
            ),
            rules,
        ))
        self.assertFalse(organizer._same_media_identity(
            movie,
            GuangYaFile(
                "same-id-wrong-type",
                "Main.Movie.S01E01.2026.{tmdb-123}.mkv",
                False,
            ),
            rules,
        ))

        tv = OrganizePlan(
            file_id="new-tv", original_name="Unknown.Episode.mkv",
            original_path="/source", new_name="Unknown.Episode.mkv",
            match=MatchResult(tmdb_id="456", title="Show", media_type="tv"),
            season=None, episode=None,
        )
        self.assertFalse(organizer._same_media_identity(
            tv, GuangYaFile("other", "Another.Unknown.mkv", False), rules,
        ))

    def test_tv_tmdb_marker_keeps_season_episode_identity(self):
        from app.modules.scraper import TMDBScraper

        organizer = Organizer(client=Mock(), scraper=TMDBScraper(client=Mock()))
        plan = OrganizePlan(
            file_id="incoming", original_name="Show.S01E02.mkv",
            original_path="/source", new_name="Show.S01E02.mkv",
            match=MatchResult(tmdb_id="456", title="Show", media_type="tv"),
            season=1, episode=2,
        )
        existing = GuangYaFile(
            "existing", "Show.S01E02.{tmdb-456}.2160p.mkv", False
        )

        self.assertTrue(organizer._same_media_identity(plan, existing, OrganizeRules()))


class DuplicateTargetDirectoryTests(unittest.TestCase):
    def test_duplicate_target_directory_chain_fails_closed(self):
        from app.modules.directory_scrape_errors import DirectoryScrapeConflictError

        client = Mock()
        client.list_dir.return_value = [
            GuangYaFile("dir-a", "Season 01", True, parent_id="show"),
            GuangYaFile("dir-b", "Season 01", True, parent_id="show"),
        ]
        organizer = Organizer(client=client, scraper=_VariantScraper())

        with self.assertRaisesRegex(DirectoryScrapeConflictError, "重复同名目录"):
            organizer._find_existing_dir_chain("show", "Season 01")
        with self.assertRaisesRegex(DirectoryScrapeConflictError, "重复同名目录"):
            organizer._find_subdir("show", "Season 01")
        client.create_dir.assert_not_called()

    def test_concurrent_target_directory_creation_reuses_unique_directory(self):
        client = Mock()
        client.list_dir.side_effect = [
            [],
            [GuangYaFile("raced", "Season 01", True, parent_id="show")],
        ]
        client.create_dir.side_effect = RuntimeError("name exists")
        organizer = Organizer(client=client, scraper=_VariantScraper())
        cache: dict[tuple[str, str], str] = {}

        result = organizer._ensure_dir_chain("show", "Season 01", cache)

        self.assertEqual(result, "raced")
        self.assertEqual(cache, {("show", "Season 01"): "raced"})
        self.assertEqual(client.list_dir.call_count, 2)
        client.create_dir.assert_called_once_with("Season 01", "show")

    def test_concurrent_target_directory_creation_preserves_original_error(self):
        client = Mock()
        client.list_dir.side_effect = [[], []]
        original_error = RuntimeError("permission denied")
        client.create_dir.side_effect = original_error
        organizer = Organizer(client=client, scraper=_VariantScraper())

        with self.assertRaises(RuntimeError) as caught:
            organizer._ensure_dir_chain("show", "Season 01", {})

        self.assertIs(caught.exception, original_error)
        self.assertEqual(client.list_dir.call_count, 2)

    def test_concurrent_target_directory_creation_still_rejects_duplicates(self):
        from app.modules.directory_scrape_errors import DirectoryScrapeConflictError

        client = Mock()
        client.list_dir.side_effect = [
            [],
            [
                GuangYaFile("dir-a", "Season 01", True, parent_id="show"),
                GuangYaFile("dir-b", "Season 01", True, parent_id="show"),
            ],
        ]
        client.create_dir.side_effect = RuntimeError("name exists")
        organizer = Organizer(client=client, scraper=_VariantScraper())

        with self.assertRaisesRegex(DirectoryScrapeConflictError, "重复同名目录"):
            organizer._ensure_dir_chain("show", "Season 01", {})


class _VariantTreeClient:
    def __init__(self, incoming: GuangYaFile, existing: GuangYaFile):
        incoming.parent_id = "source"
        existing.parent_id = "movie"
        self.tree = {
            "source": [incoming],
            "target": [GuangYaFile("category", "电影", True, parent_id="target")],
            "category": [
                GuangYaFile(
                    "movie", "Variant Movie (2026) {tmdb-123}", True,
                    parent_id="category",
                )
            ],
            "movie": [existing],
        }
        self.deleted: list[str] = []
        self.moves: list[tuple[tuple[str, ...], str]] = []
        self.renames: list[tuple[str, str]] = []
        self.list_calls: list[str] = []

    def list_dir(self, parent_id="0"):
        self.list_calls.append(parent_id)
        return list(self.tree.get(parent_id, []))

    def create_dir(self, name, parent_id="0"):
        file_id = f"dir-{len(self.tree)}"
        item = GuangYaFile(file_id, name, True, parent_id=parent_id)
        self.tree.setdefault(parent_id, []).append(item)
        self.tree[file_id] = []
        return file_id

    def _find(self, file_id):
        for parent_id, items in self.tree.items():
            for item in items:
                if item.file_id == file_id:
                    return parent_id, item
        raise AssertionError(f"unknown file: {file_id}")

    def move(self, file_ids, parent_id):
        self.moves.append((tuple(file_ids), parent_id))
        for file_id in file_ids:
            old_parent, item = self._find(file_id)
            self.tree[old_parent].remove(item)
            item.parent_id = parent_id
            self.tree.setdefault(parent_id, []).append(item)
        return True

    def rename(self, file_id, new_name):
        self.renames.append((file_id, new_name))
        _parent, item = self._find(file_id)
        item.name = new_name
        return True

    def file_info(self, file_id):
        try:
            _parent, item = self._find(file_id)
            return item
        except AssertionError:
            return None

    def delete(self, file_ids):
        self.deleted.extend(file_ids)
        for file_id in file_ids:
            parent_id, item = self._find(file_id)
            self.tree[parent_id].remove(item)
        return True


class _TvVariantTreeClient(_VariantTreeClient):
    def __init__(self, incoming: GuangYaFile, existing: GuangYaFile):
        incoming.parent_id = "source"
        existing.parent_id = "season"
        self.tree = {
            "source": [incoming],
            "target": [GuangYaFile("category", "动漫", True, parent_id="target")],
            "category": [GuangYaFile(
                "show", "Better Show (2021) {tmdb-113256}", True,
                parent_id="category",
            )],
            "show": [GuangYaFile("season", "Season 3", True, parent_id="show")],
            "season": [existing],
        }
        self.deleted = []
        self.moves = []
        self.renames = []
        self.list_calls = []


class OrganizeMultiVersionExecutionTests(IsolatedDatabaseTestCase):
    def test_historical_identity_is_not_lost_behind_large_audit_history(self):
        from app import database as db

        db.add_organize_log(
            "guangya",
            "incoming/old.mkv",
            "电影/目标影片 (2026) {tmdb-4242}/目标影片.mkv",
            "old-file",
            "success",
            "4242",
            provider="tmdb",
            external_id="4242",
            media_type="movie",
            original_parent_id="incoming",
            original_name="old.mkv",
        )
        stamp = db.now()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO organize_log("
                "source,original_path,new_path,file_id,status,tmdb_id,provider,external_id,"
                "media_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "guangya", f"incoming/noise-{index}.mkv",
                        f"其他/noise-{index}.mkv", f"noise-{index}", "success",
                        str(100000 + index), "tmdb", str(100000 + index), "movie",
                        stamp, stamp,
                    )
                    for index in range(5_100)
                ],
            )

        organizer = Organizer(client=Mock(), scraper=Mock())
        self.assertIn(
            ("movie", "tmdb:4242"),
            organizer._historical_root_identities("电影/目标影片 (2026) {tmdb-4242}"),
        )

    def test_organize_log_high_water_returns_all_rows_from_current_operation(self):
        from app import database as db

        before = db.latest_organize_log_id()
        first = db.add_organize_log(
            "guangya", "a.mkv", "电影/A/A.mkv", "a", "success",
            original_parent_id="source", original_name="a.mkv",
        )
        second = db.add_organize_log(
            "guangya", "b.mkv", "电影/B/B.mkv", "b", "manual",
            original_parent_id="source", original_name="b.mkv",
        )

        self.assertEqual(
            [int(row["id"]) for row in db.list_organize_logs_after(before)],
            [first, second],
        )

    def test_organize_logs_can_be_read_by_exact_operation_token(self):
        from app import database as db

        expected = db.add_organize_log(
            "guangya", "same.mkv", "电影/A.mkv", "same-file", "success",
            operation_token="manual-current",
            original_parent_id="source", original_name="same.mkv",
        )
        db.add_organize_log(
            "guangya", "same.mkv", "电影/B.mkv", "same-file", "success",
            operation_token="manual-other",
            original_parent_id="source", original_name="same.mkv",
        )

        self.assertEqual(
            [int(row["id"]) for row in db.list_organize_logs_by_operation_token("manual-current")],
            [expected],
        )

    def _rules(self, **overrides):
        values = {
            "target_dir_id": "target",
            "region_split": False,
            "year_split": False,
            "small_file_mb": 0,
            "clean_empty": False,
            "link_strm": False,
            "notify_enabled": False,
            "library_notify": False,
            "conflict_strategy": 2,
            "keep_multi_versions": True,
            "keep_remux_variant": False,
            "recycle_replaced_enabled": True,
        }
        values.update(overrides)
        return OrganizeRules(**values)

    def _organize(self, incoming_name, existing_name, *, incoming_size=1000,
                  existing_size=2000, incoming_id="incoming", existing_id="existing",
                  dry_run=False, rules=None):
        incoming = GuangYaFile(incoming_id, incoming_name, False, incoming_size, "incoming-etag")
        existing = GuangYaFile(existing_id, existing_name, False, existing_size, "existing-etag")
        client = _VariantTreeClient(incoming, existing)
        organizer = Organizer(client=client, scraper=_VariantScraper())
        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            plans, stats = organizer.organize(
                "source", rules or self._rules(), dry_run=dry_run, post_actions=False
            )
        return organizer, client, plans, stats

    def test_larger_tv_episode_reuses_existing_show_and_recycles_old_episode(self):
        incoming = GuangYaFile(
            "incoming",
            "Better.Show.2021.S03E09.1080p.WEB-DL.H264.AAC-NEW.mkv",
            False,
            2_000,
            "incoming-etag",
        )
        existing = GuangYaFile(
            "existing",
            "Better.Show.2021.S03E09.1080p.WEB-DL.H264.AAC-OLD.mkv",
            False,
            1_000,
            "existing-etag",
        )
        client = _TvVariantTreeClient(incoming, existing)
        organizer = Organizer(client=client, scraper=_TvVariantScraper())
        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            plans, stats = organizer.organize(
                "source",
                self._rules(keep_multi_versions=False),
                dry_run=False,
                post_actions=False,
            )

        self.assertEqual(plans[0].target_path, (
            "动漫/Better Show (2021) {tmdb-113256}/Season 3"
        ))
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["conflict"], 1)
        self.assertEqual(client.deleted, ["existing"])
        self.assertEqual([item.file_id for item in client.tree["target"]], ["category"])
        self.assertEqual([item.file_id for item in client.tree["category"]], ["show"])
        self.assertEqual([item.file_id for item in client.tree["show"]], ["season"])
        self.assertEqual(client._find("incoming")[0], "season")

    def test_historical_untagged_same_variant_uses_preview_and_execute_conflict_policy(self):
        names = (
            "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-NEW.mkv",
            "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-OLD.mkv",
        )
        _organizer, preview_client, plans, _stats = self._organize(*names, dry_run=True)

        self.assertEqual(plans[0].action, "skip")
        self.assertEqual(getattr(plans[0], "conflict_decision", ""), "skip")
        self.assertIn("同版本", getattr(plans[0], "conflict_note", ""))
        self.assertEqual(preview_client.list_calls.count("movie"), 1)

        _organizer, execute_client, _plans, stats = self._organize(*names)
        self.assertEqual(stats["moved"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(execute_client.moves, [])
        self.assertEqual(execute_client.deleted, [])

    def test_dovi_and_sdr_reach_production_coexistence_helper(self):
        coexist_spy = Mock(wraps=variants_can_coexist)
        with patch("app.modules.organize.variants_can_coexist", coexist_spy, create=True):
            _organizer, client, plans, stats = self._organize(
                "Variant.Movie.2026.2160p.DoVi.DDP5.1.x265.mkv",
                "Variant.Movie.2026.2160p.SDR.DDP5.1.x265.mkv",
            )

        self.assertGreater(coexist_spy.call_count, 0)
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(getattr(plans[0], "variant_label", ""), "DoVi / NonAtmos")

    def test_atmos_and_non_atmos_coexist_in_real_execution(self):
        _organizer, client, _plans, stats = self._organize(
            "Variant.Movie.2026.1080p.SDR.DDP5.1.Atmos.x265.mkv",
            "Variant.Movie.2026.1080p.SDR.DDP5.1.x265.mkv",
        )

        self.assertEqual(stats["moved"], 1)
        self.assertEqual(client.deleted, [])

    def test_cached_profile_classifies_historical_name_before_coexistence(self):
        incoming_version = ("incoming", "incoming-etag", 1000)
        existing_version = ("existing", "existing-etag", 2000)
        incoming_cached = (
            '{"dynamic_range":"DoVi","audio_codec":"EAC3","source":"WEB-DL"}'
        )
        existing_cached = (
            '{"dynamic_range":"SDR","audio_codec":"EAC3","source":"WEB-DL"}'
        )

        def cached_profiles(versions, *, allow_fingerprint_fallback=False):
            self.assertTrue(allow_fingerprint_fallback)
            return {
                version: payload
                for version, payload in (
                    (incoming_version, incoming_cached),
                    (existing_version, existing_cached),
                )
                if version in versions
            }

        with patch(
            "app.modules.organize.db.get_media_probe_cache_many",
            side_effect=cached_profiles,
        ) as batch_cache, patch(
            "app.modules.organize.get_media_probe_cache",
            side_effect=AssertionError("batch hit must not fall back to a per-file lookup"),
        ) as single_cache:
            _organizer, client, _plans, stats = self._organize(
                "Variant.Movie.2026.2160p.DoVi.DDP5.1.x265.mkv",
                "Variant Movie.2026.mkv",
                rules=self._rules(media_probe_enabled=True),
            )

        self.assertEqual(batch_cache.call_count, 2)
        batch_cache.assert_any_call(
            [incoming_version], allow_fingerprint_fallback=True,
        )
        batch_cache.assert_any_call(
            [existing_version], allow_fingerprint_fallback=True,
        )
        single_cache.assert_not_called()
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(client.deleted, [])

    def test_remux_toggle_switches_between_same_bucket_replacement_and_coexistence(self):
        incoming = "Variant.Movie.2026.2160p.SDR.DDP5.1.Remux.HEVC.mkv"
        existing = "Variant.Movie.2026.2160p.SDR.DDP5.1.BluRay.x265.mkv"

        _organizer, disabled_client, _plans, disabled_stats = self._organize(
            incoming, existing, rules=self._rules(keep_remux_variant=False)
        )
        self.assertEqual(disabled_stats["moved"], 1)
        self.assertEqual(disabled_client.deleted, ["existing"])

        _organizer, enabled_client, _plans, enabled_stats = self._organize(
            incoming, existing, rules=self._rules(keep_remux_variant=True)
        )
        self.assertEqual(enabled_stats["moved"], 1)
        self.assertEqual(enabled_client.deleted, [])

    def test_same_variant_uses_size_then_stable_file_id_priority(self):
        common = "Variant.Movie.2026.2160p.SDR.DDP5.1.x265"
        _organizer, size_client, _plans, size_stats = self._organize(
            f"{common}-SMALL.mkv", f"{common}-LARGE.mkv",
            incoming_size=1000, existing_size=2000,
        )
        self.assertEqual(size_stats["skipped"], 1)
        self.assertEqual(size_client.deleted, [])

        _organizer, id_client, _plans, id_stats = self._organize(
            f"{common}-A.mkv", f"{common}-Z.mkv",
            incoming_size=2000, existing_size=2000,
            incoming_id="a-incoming", existing_id="z-existing",
        )
        self.assertEqual(id_stats["moved"], 1)
        self.assertEqual(id_client.deleted, ["z-existing"])

    def test_incomplete_target_variant_scan_never_deletes_existing_file(self):
        incoming = GuangYaFile(
            "incoming", "Variant.Movie.2026.2160p.DoVi.DDP5.1.x265.mkv",
            False, 1000, "incoming-etag", "source",
        )
        existing = GuangYaFile(
            "existing", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265.mkv",
            False, 2000, "existing-etag", "movie",
        )
        client = _VariantTreeClient(incoming, existing)
        original_list_dir = client.list_dir

        def fail_target_scan(parent_id="0"):
            if parent_id == "movie":
                raise RuntimeError("incomplete target scan")
            return original_list_dir(parent_id)

        client.list_dir = fail_target_scan
        organizer = Organizer(client=client, scraper=_VariantScraper())
        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            _plans, stats = organizer.organize(
                "source", self._rules(), dry_run=False, post_actions=False
            )

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.moves, [])

    def test_preview_reports_variant_suffix_and_coexistence_without_recursive_target_scan(self):
        _organizer, client, plans, _stats = self._organize(
            "Variant.Movie.2026.2160p.DoVi.DDP5.1.x265.mkv",
            "Variant.Movie.2026.2160p.SDR.DDP5.1.x265.mkv",
            dry_run=True,
        )

        plan = plans[0]
        self.assertEqual(getattr(plan, "conflict_decision", ""), "coexist")
        self.assertIn("DoVi", getattr(plan, "variant_label", ""))
        self.assertIn("DoVi", getattr(plan, "variant_suffix", ""))
        self.assertIn("不同版本允许共存", getattr(plan, "conflict_note", ""))
        self.assertEqual(client.list_calls.count("movie"), 1)
        self.assertNotIn("existing", client.list_calls)

    def test_preview_simulates_earlier_plans_in_the_same_target_directory(self):
        first = GuangYaFile(
            "a-first", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-FIRST.mkv",
            False, 2000, "first-etag", "source",
        )
        second = GuangYaFile(
            "b-second", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-SECOND.mkv",
            False, 1000, "second-etag", "source",
        )
        placeholder = GuangYaFile("placeholder", "placeholder.txt", False, 1)
        client = _VariantTreeClient(first, placeholder)
        client.tree["source"] = [first, second]
        client.tree["movie"] = []
        organizer = Organizer(client=client, scraper=_VariantScraper())

        preview_plans, _preview_stats = organizer.organize(
            "source", self._rules(), dry_run=True, post_actions=False
        )

        self.assertEqual(preview_plans[0].conflict_decision, "new")
        self.assertEqual(preview_plans[1].conflict_decision, "skip")
        self.assertEqual(preview_plans[1].action, "skip")
        self.assertEqual(client.list_calls.count("movie"), 1)

        execute_first = GuangYaFile(
            "a-first", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-FIRST.mkv",
            False, 2000, "first-etag", "source",
        )
        execute_second = GuangYaFile(
            "b-second", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-SECOND.mkv",
            False, 1000, "second-etag", "source",
        )
        execute_client = _VariantTreeClient(execute_first, placeholder)
        execute_client.tree["source"] = [execute_first, execute_second]
        execute_client.tree["movie"] = []
        execute_organizer = Organizer(client=execute_client, scraper=_VariantScraper())
        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            _plans, execute_stats = execute_organizer.organize(
                "source", self._rules(), dry_run=False, post_actions=False
            )
        self.assertEqual(execute_stats["moved"], 1)
        self.assertEqual(execute_stats["skipped"], 1)


    def test_batch_arbitration_skips_earlier_smaller_candidate_before_cloud_write(self):
        smaller = GuangYaFile(
            "a-smaller", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-SMALL.mkv",
            False, 1000, "small-etag", "source",
        )
        larger = GuangYaFile(
            "b-larger", "Variant.Movie.2026.2160p.SDR.DDP5.1.x265-LARGE.mkv",
            False, 2000, "large-etag", "source",
        )
        placeholder = GuangYaFile("placeholder", "placeholder.txt", False, 1)
        client = _VariantTreeClient(smaller, placeholder)
        client.tree["source"] = [smaller, larger]
        client.tree["movie"] = []
        organizer = Organizer(client=client, scraper=_VariantScraper())

        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            plans, stats = organizer.organize(
                "source", self._rules(), dry_run=False, post_actions=False
            )

        by_id = {plan.file_id: plan for plan in plans}
        self.assertEqual(by_id["a-smaller"].action, "skip")
        self.assertEqual(
            by_id["a-smaller"].conflict_decision, "batch_superseded"
        )
        self.assertIn("未执行云盘写入", by_id["a-smaller"].note)
        self.assertEqual(by_id["b-larger"].action, "move")
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(client.moves, [(("b-larger",), "movie")])


    def test_multi_episode_execution_reuses_chain_but_rechecks_shared_season_per_plan(self):
        episodes = [
            GuangYaFile(
                f"episode-{number}",
                f"Better.Show.2021.S03E{number:02d}.1080p.WEB-DL.H264.AAC.mkv",
                False, 1000 + number, f"etag-{number}", "source",
            )
            for number in range(1, 4)
        ]
        placeholder = GuangYaFile("placeholder", "placeholder.txt", False, 1)
        client = _TvVariantTreeClient(episodes[0], placeholder)
        client.tree["source"] = episodes
        client.tree["season"] = []
        organizer = Organizer(client=client, scraper=_TvVariantScraper())

        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            plans, stats = organizer.organize(
                "source", self._rules(), dry_run=False, post_actions=False
            )

        self.assertTrue(all(
            plan.target_path == (
                "动漫/Better Show (2021) {tmdb-113256}/Season 3"
            )
            for plan in plans
        ))
        self.assertEqual(stats["moved"], 3)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["target_dir_refreshes"], 3)
        self.assertEqual(client.list_calls.count("target"), 2)
        self.assertEqual(client.list_calls.count("category"), 2)
        self.assertEqual(client.list_calls.count("show"), 2)
        self.assertEqual(client.list_calls.count("season"), 4)

    def test_shared_season_recheck_observes_external_conflict_before_second_episode(self):
        first = GuangYaFile(
            "episode-1",
            "Better.Show.2021.S03E01.1080p.WEB-DL.H264.AAC.mkv",
            False, 1000, "etag-1", "source",
        )
        second = GuangYaFile(
            "episode-2",
            "Better.Show.2021.S03E02.1080p.WEB-DL.H264.AAC.mkv",
            False, 1000, "etag-2", "source",
        )
        external = GuangYaFile(
            "external-2",
            "Better.Show.2021.S03E02.2160p.WEB-DL.H265.AAC.mkv",
            False, 10000, "external-etag", "season",
        )
        placeholder = GuangYaFile("placeholder", "placeholder.txt", False, 1)
        client = _TvVariantTreeClient(first, placeholder)
        client.tree["source"] = [first, second]
        client.tree["season"] = []
        original_list_dir = client.list_dir
        season_reads = 0

        def list_dir_with_external_change(parent_id="0"):
            nonlocal season_reads
            if parent_id == "season":
                season_reads += 1
                if season_reads == 3:
                    client.tree["season"].append(external)
            return original_list_dir(parent_id)

        client.list_dir = list_dir_with_external_change
        organizer = Organizer(client=client, scraper=_TvVariantScraper())
        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            plans, stats = organizer.organize(
                "source", self._rules(keep_multi_versions=False),
                dry_run=False, post_actions=False,
            )

        by_id = {plan.file_id: plan for plan in plans}
        self.assertEqual(season_reads, 3)
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(client.moves, [(("episode-1",), "season")])
        self.assertEqual(by_id["episode-2"].conflict_decision, "skip")
        self.assertIn("保留现有版本", by_id["episode-2"].conflict_note)
        self.assertEqual(client._find("external-2")[0], "season")



class MultiVersionConfigApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        login_page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(login_page.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/organize")
        self.headers = {"X-CSRF-Token": self._csrf(page.text)}

    def tearDown(self):
        self.client.close()

    @staticmethod
    def _csrf(html):
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def test_rules_page_renders_extension_tag_editors_and_recommended_defaults(self):
        response = self.client.get("/organize-rules")
        self.assertEqual(response.status_code, 200, response.text)
        html = response.text
        self.assertIn('data-extension-editor="video"', html)
        self.assertIn('data-extension-editor="metadata"', html)
        self.assertIn('data-key="GY_ORGANIZE_VIDEO_EXTS"', html)
        self.assertIn('data-key="GY_ORGANIZE_METADATA_EXTS"', html)
        self.assertIn('data-extension-value="m4v"', html)
        self.assertIn('data-extension-value="vtt"', html)
        self.assertIn('aria-label="移除 m4v"', html)
        self.assertIn('aria-label="移除 vtt"', html)
        self.assertIn('class="form-row organize-ext-row"', html)
        organize_css = Path('app/static/css/organize.css').read_text(encoding='utf-8')
        self.assertIn('flex:1 1 240px', organize_css)
        self.assertIn('width:min(760px,64%)', organize_css)
        self.assertIn("GY_ORGANIZE_CONFLICT_STRATEGY", html)
        self.assertIn('data-key="GY_ORGANIZE_AUTOMATIC_MATCH_PRESET"', html)
        self.assertIn('value="balanced" selected', html)
        self.assertNotIn('placeholder="mkv,mp4,ts,m2ts,avi"', html)


    def test_automatic_match_preset_is_validated_and_saved(self):
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={"GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "aggressive"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            save.call_args.args[0]["GY_ORGANIZE_AUTOMATIC_MATCH_PRESET"],
            "aggressive",
        )

        invalid = self.client.post(
            "/api/config",
            json={"GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "unsafe"},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("必须是以下预设之一", invalid.text)

    def test_full_organize_page_config_payload_accepts_multiversion_keys(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        payload = {key: "" for key in set(re.findall(r'data-key="([^"]+)"', html))}
        payload.update({
            "GY_ORGANIZE_KEEP_MULTI_VERSIONS": "1",
            "GY_ORGANIZE_KEEP_REMUX_VARIANT": "1",
            "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "balanced",
        })

        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post("/api/config", json=payload, headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        saved = save.call_args.args[0]
        self.assertEqual(saved["GY_ORGANIZE_KEEP_MULTI_VERSIONS"], "1")
        self.assertEqual(saved["GY_ORGANIZE_KEEP_REMUX_VARIANT"], "1")
        self.assertIn("mkv", saved["GY_ORGANIZE_VIDEO_EXTS"].split(","))
        self.assertIn("nfo", saved["GY_ORGANIZE_METADATA_EXTS"].split(","))

    def test_organize_extensions_are_canonicalized_and_validated(self):
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={
                    "GY_ORGANIZE_VIDEO_EXTS": ".MKV， mp4 mkv",
                    "GY_ORGANIZE_METADATA_EXTS": ".NFO, srt",
                },
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        saved = save.call_args.args[0]
        self.assertEqual(saved["GY_ORGANIZE_VIDEO_EXTS"], "mkv,mp4")
        self.assertEqual(saved["GY_ORGANIZE_METADATA_EXTS"], "nfo,srt")

        invalid = self.client.post(
            "/api/config", json={"GY_ORGANIZE_VIDEO_EXTS": "mkv,*.exe"},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("无效扩展名", invalid.text)

        overlap = self.client.post(
            "/api/config",
            json={
                "GY_ORGANIZE_VIDEO_EXTS": "mkv,mp4",
                "GY_ORGANIZE_METADATA_EXTS": "nfo,mkv",
            },
            headers=self.headers,
        )
        self.assertEqual(overlap.status_code, 400)
        self.assertIn("不能重复", overlap.text)

    def test_retired_media_naming_scope_is_rejected(self):
        response = self.client.post(
            "/api/config", json={"MEDIA_NAMING_SCOPE": "guangya"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不允许", response.text)

    def test_retired_naming_and_probe_settings_are_rejected_and_hidden(self):
        retired = {
            "MEDIA_NAMING_SCOPE": "guangya",
            "GY_ORGANIZE_RENAME": "0",
            "GY_ORGANIZE_MEDIAINFO": "0",
            "GY_ORGANIZE_MEDIA_PROBE_ENABLED": "0",
            "GY_ORGANIZE_MEDIA_PROBE_TIMEOUT_SECONDS": "120",
            "MEDIA_MOVIE_DIR_TEMPLATE": "legacy",
            "MEDIA_MOVIE_TEMPLATE": "legacy",
            "MEDIA_SHOW_DIR_TEMPLATE": "legacy",
            "MEDIA_TV_TEMPLATE": "legacy",
        }
        with patch("app.routes.api.config.all_items", return_value=retired):
            response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        for key in retired:
            self.assertNotIn(key, payload)
            rejected = self.client.post(
                "/api/config", json={key: retired[key]}, headers=self.headers,
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)
            self.assertIn(key, rejected.text)

    def test_blank_or_invalid_extension_text_falls_back_to_defaults(self):
        organizer = Organizer(client=object(), scraper=object())
        self.assertIn("mkv", organizer._parse_exts(" , ", {"mkv"}))
        self.assertEqual(organizer._parse_exts("...", {"mkv"}), {"mkv"})
        self.assertEqual(media_role("subtitle.zh.vtt"), "subtitle")
        self.assertEqual(media_role("subtitle.sup"), "subtitle")

    def test_preview_api_serializes_the_production_variant_decision(self):
        plan = OrganizePlan(
            file_id="incoming",
            original_name="Movie.DoVi.mkv",
            original_path="incoming",
            match=MatchResult(
                tmdb_id="123", title="Movie", year="2026", media_type="movie"
            ),
            new_name="Movie.DoVi.mkv",
            base_name="Movie.mkv",
            target_path="电影/Movie {tmdb-123}",
            variant_label="DoVi / NonAtmos",
            variant_suffix="DoVi.NonAtmos",
            conflict_decision="coexist",
            conflict_note="不同版本允许共存；同版本仍按冲突策略处理",
        )
        organizer = Mock()
        organizer.organize.return_value = ([plan], {"total": 1, "matched": 1})

        with patch("app.modules.organize.Organizer", return_value=organizer):
            response = self.client.post(
                "/api/guangya/organize/preview",
                json={
                    "source_dirs": [
                        {"id": "source", "name": "源"},
                        {"id": "nested-source", "name": "嵌套源"},
                    ],
                    "rules": {},
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        rendered = response.json()["plans"][0]
        self.assertEqual(rendered["variant_label"], "DoVi / NonAtmos")
        self.assertEqual(rendered["variant_suffix"], "DoVi.NonAtmos")
        self.assertEqual(rendered["conflict_decision"], "coexist")
        self.assertIn("同版本仍按冲突策略处理", rendered["conflict_note"])
        self.assertEqual(organizer.organize.call_count, 2)
        for call in organizer.organize.call_args_list:
            self.assertEqual(
                call.kwargs["protected_source_ids"],
                {"source", "nested-source"},
            )


class MultiVersionUiTests(unittest.TestCase):
    def test_organize_page_exposes_stable_multiversion_controls_and_preview_copy(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))

        self.assertIn('id="r_keep_multi" data-key="GY_ORGANIZE_KEEP_MULTI_VERSIONS"', html)
        self.assertIn('id="r_keep_remux" data-key="GY_ORGANIZE_KEEP_REMUX_VARIANT"', html)
        self.assertIn("DoVi / 标准版、Atmos / 非 Atmos", html)
        self.assertIn("Remux / Encode", html)
        self.assertNotIn("keep_multi_versions:bool('r_keep_multi')", html)
        self.assertNotIn("keep_remux_variant:bool('r_keep_remux')", html)
        self.assertIn("fillConfigFields(workspace,config)", html)
        self.assertIn("saveAppConfig(workspace)", html)
        self.assertIn("版本分类", html)
        self.assertIn("同版本仍按冲突策略处理", html)
        self.assertIn("plan.variant_label", html)
        self.assertIn("plan.variant_suffix", html)
        self.assertIn("plan.conflict_note", html)
        self.assertIn("document.getElementById('r_keep_remux').disabled=!keepMulti", html)
        self.assertNotIn("document.getElementById('keepRemuxRow').hidden", html)


if __name__ == "__main__":
    unittest.main()
