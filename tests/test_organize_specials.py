from __future__ import annotations

import re
import unittest
from unittest.mock import Mock

from app.clients.guangya import GuangYaFile
from app.modules.organize import (
    OrganizeRules,
    Organizer,
    _special_filename_identity_hint,
)
from app.modules.scraper import MatchResult, TMDBScraper
from app.modules.directory_scrape import FixedMatchScraper
from app.modules.organize_scan import ScannedVideo
from tests.support import IsolatedDatabaseTestCase, release_parse_result

from app.modules.special_media import (
    fixed_special_media_position,
    fractional_episode_position,
    has_fractional_episode_position,
    is_special_directory_name,
    is_special_media_name,
    special_media_position,
    special_parent_context,
)


class _TreeClient:
    def __init__(self):
        show_name = "[H-Enc] Arifureta Shokugyou de Sekai Saikyou Season 3"
        self.tree = {
            "source": [GuangYaFile("show", show_name, True, parent_id="source")],
            "show": [
                GuangYaFile(
                    "episode", "Arifureta Shokugyou de Sekai Saikyou Season 3 - 01.mkv",
                    False, 1024, "e1", "show",
                ),
                GuangYaFile("extra", "Extra", True, parent_id="show"),
            ],
            "extra": [
                GuangYaFile("ncop", "NCOP.mkv", False, 300, "op", "extra"),
                GuangYaFile("nced", "NCED.mkv", False, 200, "ed", "extra"),
            ],
            "archive": [],
        }
        self.info = {
            "source": GuangYaFile("source", "1", True, parent_id="0"),
            "show": self.tree["source"][0],
            "extra": self.tree["show"][1],
            "archive": GuangYaFile("archive", "整理", True, parent_id="0"),
        }

    def list_dir(self, file_id):
        return list(self.tree.get(file_id, []))

    def file_info(self, file_id):
        return self.info.get(file_id)


class _ShowScraper:
    supports_parent_path = True

    def __init__(self):
        self.calls = []

    def match(self, filename, parent_path=""):
        self.calls.append((filename, parent_path))
        return MatchResult(
            tmdb_id="86034",
            title="平凡职业造就世界最强",
            year="2019",
            media_type="tv",
            confidence=1.0,
        )

    def parse_media(self, filename, parent_path="", match=None):
        season = re.search(r"(?i)(?:season\s*|s)(\d{1,2})", filename)
        episode = re.search(r"(?i)(?:\be|\s-\s)(\d{1,3})(?:\D|$)", filename)
        return release_parse_result(
            {
                "type": "tv" if season or episode else "movie",
                "season": int(season.group(1)) if season else None,
                "episode": int(episode.group(1)) if episode else None,
            },
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, _tmdb_id, _media_type):
        return {
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "seasons": [
                {"season_number": 0, "episode_count": 10},
                {"season_number": 3, "episode_count": 12},
            ],
        }


class _ExactTVClient:
    api_key = "test-key"
    base_url = "https://tmdb.test/3"
    config_error = ""
    session = None

    def search(self, title, year, media_type):
        if media_type != "tv":
            return []
        return [{
            "id": 4242,
            "name": "Example Show",
            "original_name": "Example Show",
            "first_air_date": "2020-01-01",
            "media_type": "tv",
        }]

    def detail(self, tmdb_id, media_type):
        return {
            "id": int(tmdb_id),
            "name": "Example Show",
            "original_name": "Example Show",
            "first_air_date": "2020-01-01",
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }

    def detail_with_alternative_titles(self, tmdb_id, media_type):
        return self.detail(tmdb_id, media_type)


class AutomaticSpecialsTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _strict_tmdb_match(tmdb_id="4242"):
        return MatchResult(
            tmdb_id=str(tmdb_id),
            external_id=str(tmdb_id),
            provider="tmdb",
            title="Example Show",
            year="2020",
            media_type="tv",
            confidence=1.0,
            threshold=0.9,
            status="matched",
            need_confirm=False,
            directory_identity_cache_eligible=True,
        )

    @staticmethod
    def _partial_special_match(tmdb_id="4242"):
        return MatchResult(
            tmdb_id=str(tmdb_id),
            external_id=str(tmdb_id),
            provider="tmdb",
            title="Example Show",
            year="2020",
            media_type="tv",
            confidence=0.88,
            threshold=0.9,
            status="low_confidence",
            need_confirm=True,
            error="候选仅命中部分标题，完整标题仍有显著片段未匹配，需要人工确认",
        )

    def _same_directory_special_result(self, special_tmdb_id="4242"):
        client = _TreeClient()
        client.tree["source"][0].name = "[Group] Example Show Season 1"
        client.info["show"] = client.tree["source"][0]
        client.tree["show"] = [
            GuangYaFile(
                "episode", "Example Show - 01 [1080p].mkv",
                False, 1024, "episode-etag", "show",
            ),
            GuangYaFile(
                "special", "Example Show - Extra 1 [1080p].mkv",
                False, 512, "special-etag", "show",
            ),
        ]
        scraper = TMDBScraper(client=_ExactTVClient())

        def match(filename, _parent_path="", **_kwargs):
            if "Extra" in filename:
                return self._partial_special_match(special_tmdb_id)
            return self._strict_tmdb_match()

        scraper.match = Mock(side_effect=match)
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        return organizer.organize("source", rules, dry_run=True, automatic=True)

    def test_special_reuses_strict_same_directory_tmdb_identity(self):
        plans, stats = self._same_directory_special_result()
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(by_id["episode"].action, "move")
        self.assertEqual(by_id["special"].action, "move")
        self.assertEqual(by_id["special"].match.tmdb_id, "4242")
        self.assertTrue(by_id["special"].target_path.endswith("/Specials"))
        self.assertIn("S00E01", by_id["special"].new_name)
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(stats["directory_special_identity_bindings"], 1)
        self.assertEqual(
            by_id["special"].match.metadata["directory_special_identity_binding"],
            {
                "tmdb_id": "4242",
                "source": "verified_regular_same_scan",
            },
        )

    def test_special_does_not_reuse_different_tmdb_identity(self):
        plans, stats = self._same_directory_special_result("9999")
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(by_id["episode"].action, "move")
        self.assertEqual(by_id["special"].action, "skip")
        self.assertTrue(by_id["special"].match.need_confirm)
        self.assertEqual(stats["need_confirm"], 1)
        self.assertEqual(stats["directory_special_identity_bindings"], 0)

    def test_special_does_not_reuse_identity_from_another_first_level_tree(self):
        client = _TreeClient()
        donor = GuangYaFile(
            "donor", "[Group] Example Show Season 1", True, parent_id="source"
        )
        other = GuangYaFile(
            "other", "[Group] Example Show Alternate", True, parent_id="source"
        )
        client.tree["source"] = [donor, other]
        client.tree["donor"] = [
            GuangYaFile(
                "episode", "Example Show - 01 [1080p].mkv",
                False, 1024, "episode-etag", "donor",
            )
        ]
        client.tree["other"] = [
            GuangYaFile(
                "special", "Example Show - Extra 1 [1080p].mkv",
                False, 512, "special-etag", "other",
            )
        ]
        client.info.update({"donor": donor, "other": other})
        scraper = TMDBScraper(client=_ExactTVClient())

        def match(filename, _parent_path="", **_kwargs):
            if "Extra" in filename:
                return self._partial_special_match()
            return self._strict_tmdb_match()

        scraper.match = Mock(side_effect=match)
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0,
            region_split=False, year_split=False, clean_empty=False,
            link_strm=False, notify_enabled=False,
        )

        plans, stats = organizer.organize(
            "source", rules, dry_run=True, automatic=True
        )
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(by_id["episode"].action, "move")
        self.assertEqual(by_id["special"].action, "skip")
        self.assertTrue(by_id["special"].match.need_confirm)
        self.assertEqual(stats["need_confirm"], 1)
        self.assertEqual(stats["directory_special_identity_bindings"], 0)

    def test_special_under_explicit_source_marker_does_not_use_donor_binding(self):
        client = _TreeClient()
        client.info["source"] = GuangYaFile(
            "source", "Example Show tmdb4242", True, parent_id="0"
        )
        client.tree["source"][0].name = "[Group] Example Show Season 1"
        client.info["show"] = client.tree["source"][0]
        client.tree["show"] = [
            GuangYaFile(
                "episode", "Example Show - 01 [1080p].mkv",
                False, 1024, "episode-etag", "show",
            ),
            GuangYaFile(
                "special", "Example Show - Extra 1 [1080p].mkv",
                False, 512, "special-etag", "show",
            ),
        ]
        scraper = TMDBScraper(client=_ExactTVClient())

        def match(filename, _parent_path="", **_kwargs):
            if "Extra" in filename:
                return self._partial_special_match()
            return self._strict_tmdb_match()

        scraper.match = Mock(side_effect=match)
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0,
            region_split=False, year_split=False, clean_empty=False,
            link_strm=False, notify_enabled=False,
        )

        plans, stats = organizer.organize(
            "source", rules, dry_run=True, automatic=True
        )
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(by_id["episode"].action, "move")
        self.assertEqual(by_id["special"].action, "skip")
        self.assertTrue(by_id["special"].match.need_confirm)
        self.assertEqual(stats["directory_special_identity_bindings"], 0)

    def test_extra_subtree_uses_parent_show_and_specials(self):
        client = _TreeClient()
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(stats["total"], 3)
        self.assertTrue(by_id["episode"].target_path.endswith("/Season 3"))
        self.assertTrue(by_id["nced"].target_path.endswith("/Specials"))
        self.assertTrue(by_id["ncop"].target_path.endswith("/Specials"))
        self.assertIn("S00E01", by_id["nced"].new_name)
        self.assertIn("S00E02", by_id["ncop"].new_name)
        self.assertEqual(by_id["nced"].match.tmdb_id, "86034")
        self.assertNotIn("Extra (", by_id["nced"].target_path)

        special_calls = [call for call in scraper.calls if call[0].endswith(".mkv") and "Arifureta" in call[0]]
        self.assertEqual(len(special_calls), 3)
        self.assertTrue(all(".S00E" not in call[0] for call in special_calls))
        self.assertTrue(all(call[1].endswith("Season 3") for call in special_calls))
        self.assertTrue(all("Extra" not in call[1] for call in special_calls))

    def test_special_file_title_beats_noisy_release_parent(self):
        client = _TreeClient()
        noisy_show = client.tree["source"][0]
        noisy_show.name = "[BDrip] Gabriel Dropout S01 [ktnbytes]"
        client.tree["show"] = [
            GuangYaFile(
                "special",
                "Gabriel Dropout 2017 S00E01-[1080p][BDRIP][AV1.OPUS].mkv",
                False,
                300,
                "special-gcid",
                "show",
            )
        ]
        client.info["show"] = noisy_show
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)

        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(len(plans), 1)
        self.assertTrue(plans[0].target_path.endswith("/Specials"))
        self.assertIn("S00E01", plans[0].new_name)
        self.assertEqual(len(scraper.calls), 1)
        self.assertTrue(scraper.calls[0][0].startswith("Gabriel Dropout.S01."))
        self.assertNotIn("BDrip", scraper.calls[0][0])
        self.assertTrue(scraper.calls[0][1].endswith("[BDrip] Gabriel Dropout S01 [ktnbytes]"))

    def test_show_root_extra_uses_tv_identity_without_requiring_tmdb_season_zero(self):
        client = _TreeClient()
        client.tree["source"][0].name = "Example Show"
        client.tree["show"] = [GuangYaFile("extra", "Extra", True, parent_id="show")]
        client.tree["extra"] = [
            GuangYaFile("ncop", "NCOP.mkv", False, 300, "op", "extra")
        ]
        client.info["show"] = client.tree["source"][0]
        client.info["extra"] = client.tree["show"][0]
        scraper = TMDBScraper(client=_ExactTVClient())
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)

        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["need_confirm"], 0)
        self.assertEqual(plans[0].match.media_type, "tv")
        self.assertEqual(plans[0].season, 0)
        self.assertEqual(plans[0].episode, 1)
        self.assertTrue(plans[0].target_path.endswith("/Specials"))
        self.assertIn("S00E01", plans[0].new_name)

    def test_specials_in_sibling_directories_share_one_episode_sequence(self):
        client = _TreeClient()
        show = client.info["show"]
        endings = GuangYaFile("endings", "SPs", True, parent_id="show")
        client.tree["show"].append(endings)
        client.tree["extra"] = [
            GuangYaFile("ncop", "NCOP.mkv", False, 300, "op", "extra")
        ]
        client.tree["endings"] = [
            GuangYaFile("nced1", "NCED1.mkv", False, 200, "ed1", "endings")
        ]
        client.info["endings"] = endings
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, _stats = organizer.organize("source", rules, dry_run=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertIn("S00E01", by_id["ncop"].new_name)
        self.assertIn("S00E02", by_id["nced1"].new_name)
        self.assertNotEqual(by_id["ncop"].new_name, by_id["nced1"].new_name)

    def test_release_batch_directories_share_special_episode_sequence(self):
        client = _TreeClient()
        show = client.info["show"]
        batch_a = GuangYaFile(
            "batch-a",
            "[KTXP][Isekai_Maou_to_Shoukan_Shoujo_no_Dorei_Majutsu][01-10][GB]",
            True,
            parent_id="show",
        )
        batch_b = GuangYaFile(
            "batch-b",
            "[KTXP][Isekai_Maou_to_Shoukan_Shoujo_no_Dorei_Majutsu][01-12][BIG5]",
            True,
            parent_id="show",
        )
        client.tree["show"] = [client.tree["show"][0], batch_a, batch_b]
        client.tree["batch-a"] = [
            GuangYaFile("batch-ncop", "NCOP.mkv", False, 300, "op", "batch-a")
        ]
        client.tree["batch-b"] = [
            GuangYaFile("batch-nced1", "NCED1.mkv", False, 200, "ed1", "batch-b")
        ]
        client.info.update({"batch-a": batch_a, "batch-b": batch_b})
        scraper = _ShowScraper()
        # 测试替身补齐与生产 TMDBScraper 一致的目录标题清洗能力。
        scraper.clean_title = lambda value: re.sub(
            r"(?i)[\[【(（](?:01-(?:10|12)|GB|BIG5)[\]】)）]", " ", value
        ).replace("[KTXP]", "").strip(" []")
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, _stats = organizer.organize("source", rules, dry_run=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertIn("S00E01", by_id["batch-ncop"].new_name)
        self.assertIn("S00E02", by_id["batch-nced1"].new_name)

    def test_identical_special_titles_in_different_source_trees_restart_sequence(self):
        scraper = _ShowScraper()
        scraper.clean_title = lambda _value: "Example Show"
        organizer = Organizer(client=_TreeClient(), scraper=scraper)
        candidates = [
            ScannedVideo(
                file=GuangYaFile("tree-a-op", "NCOP.mkv", False, 300, "a", "a-batch"),
                relative_dir="tree-a/[KTXP][Example_Show][01-12]/Extra",
                special=True,
                recognition_parent_path="Example Show",
            ),
            ScannedVideo(
                file=GuangYaFile("tree-b-ed", "NCED.mkv", False, 300, "b", "b-batch"),
                relative_dir="tree-b/[KTXP][Example_Show][01-12]/Extra",
                special=True,
                recognition_parent_path="Example Show",
            ),
        ]

        positions = organizer._special_position_overrides(candidates)

        self.assertEqual(positions["tree-a-op"], (0, 1))
        self.assertEqual(positions["tree-b-ed"], (0, 1))

    def test_fractional_specials_for_different_root_titles_restart_sequence(self):
        organizer = Organizer(client=_TreeClient(), scraper=_ShowScraper())
        candidates = [
            ScannedVideo(
                file=GuangYaFile(
                    "alpha-fractional", "Alpha Show - 1.5.mkv", False,
                    300, "alpha", "source",
                ),
                relative_dir="",
                special=True,
                recognition_parent_path="download",
            ),
            ScannedVideo(
                file=GuangYaFile(
                    "beta-fractional", "Beta Show - 1.5.mkv", False,
                    300, "beta", "source",
                ),
                relative_dir="",
                special=True,
                recognition_parent_path="download",
            ),
        ]

        positions = organizer._special_position_overrides(candidates)

        self.assertEqual(positions["alpha-fractional"], (0, 1))
        self.assertEqual(positions["beta-fractional"], (0, 1))

    def test_nested_extra_category_uses_owner_show_context(self):
        client = _TreeClient()
        nested = GuangYaFile("twitter", "Twitter連載ピクチャードラマ", True, parent_id="extra")
        picture = GuangYaFile(
            "picture",
            "Arifureta Shokugyou de Sekai Saikyou Season 3 Picture Drama - 01.mkv",
            False,
            300,
            "picture",
            "twitter",
        )
        client.tree["extra"] = [nested]
        client.tree["twitter"] = [picture]
        client.info["twitter"] = nested
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(stats["total"], 2)
        self.assertTrue(by_id["picture"].target_path.endswith("/Specials"))
        self.assertEqual(by_id["picture"].match.tmdb_id, "86034")
        self.assertIn("S00E01", by_id["picture"].new_name)
        special_call = next(
            call for call in scraper.calls
            if "Arifureta" in call[0] and ".S00E" not in call[0] and "picture" not in call[0].lower()
        )
        self.assertTrue(special_call[1].endswith("Season 3"))
        self.assertNotIn("Extra", special_call[1])
        self.assertNotIn("Twitter", special_call[1])

    def test_special_parent_context_stops_at_special_container(self):
        self.assertEqual(
            special_parent_context("Extra/Twitter連載ピクチャードラマ", "Show"),
            "Show",
        )
        self.assertEqual(
            special_parent_context("Show/Season 03/Extra/Xミニドラマ", "Root"),
            "Show/Season 03",
        )

    def test_sps_ncop_and_omnibus_use_show_context_and_specials_target(self):
        client = _TreeClient()
        show = client.info["show"]
        sps = GuangYaFile("sps", "SPs", True, parent_id="show")
        client.tree["show"].extend([
            GuangYaFile("root-ncop", "NCOP.mkv", False, 250, "root-op", "show"),
            GuangYaFile(
                "omnibus",
                "Arifureta Shokugyou de Sekai Saikyou.OMNIBUS.mkv",
                False,
                350,
                "omnibus",
                "show",
            ),
            sps,
        ])
        client.tree["sps"] = [
            GuangYaFile("feature", "Featurette.mkv", False, 150, "feature", "sps")
        ]
        client.info["sps"] = sps
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual(stats["total"], 6)
        for file_id in ("root-ncop", "omnibus", "feature"):
            self.assertTrue(by_id[file_id].target_path.endswith("/Specials"))
            self.assertNotIn("SPs (", by_id[file_id].target_path)
            self.assertEqual(by_id[file_id].match.tmdb_id, "86034")
        special_names = {
            file_id: by_id[file_id].new_name
            for file_id in ("root-ncop", "omnibus", "feature")
        }
        self.assertIn("S00E01", special_names["omnibus"])
        self.assertIn("S00E02", special_names["root-ncop"])
        self.assertIn("S00E05", special_names["feature"])
        special_calls = [
            call for call in scraper.calls
            if call[1].endswith("Season 3") and ".S00E" not in call[0]
        ]
        self.assertEqual(len(special_calls), 6)
        self.assertTrue(all("SPs" not in call[1] and "Extra" not in call[1] for call in special_calls))


class SpecialFilenameIdentityHintTests(unittest.TestCase):
    def test_special_filename_uses_clean_show_title(self):
        expected = {
            "Example Show S00E01.mkv": "Example Show",
            "Example Show OVA 01.mkv": "Example Show",
            "Example Show OAD 02.mkv": "Example Show",
            "Example Show - NCOP.mkv": "Example Show",
            "Example Show - NCED03.mkv": "Example Show",
        }
        for filename, title in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(_special_filename_identity_hint(filename), title)

    def test_bare_special_token_fails_closed_to_parent_context(self):
        for filename in ("NCOP.mkv", "NCED1.mkv", "OVA.mkv", "Specials.mkv"):
            with self.subTest(filename=filename):
                self.assertEqual(_special_filename_identity_hint(filename), "")


class SpecialMediaTokenTests(unittest.TestCase):
    def test_numbered_ncop_nced_and_bare_op_ed_are_specials(self):
        expected = {
            "NCOP.mkv": None,
            "NCOP2-1.mkv": 2,
            "NCED03.mkv": 3,
            "OP_03.mkv": 3,
            "Show - ED01.mkv": 1,
        }
        for filename, episode in expected.items():
            with self.subTest(filename=filename):
                self.assertTrue(is_special_media_name(filename))
                self.assertEqual(special_media_position(filename), episode)

    def test_sp_tokens_are_specials_without_capturing_normal_words(self):
        expected = {
            "SP.mkv": None,
            "SP03.mkv": 3,
            "Show - SP 2.mkv": 2,
        }
        for filename, episode in expected.items():
            with self.subTest(filename=filename):
                self.assertTrue(is_special_media_name(filename))
                self.assertEqual(special_media_position(filename), episode)
        for filename in ("XSP03.mkv", "Space.mkv"):
            with self.subTest(filename=filename):
                self.assertFalse(is_special_media_name(filename))
                self.assertIsNone(special_media_position(filename))

    def test_fractional_episode_detection_avoids_quality_and_size_tokens(self):
        for filename in (
            "Show.S01E07.5.mkv",
            "Show.EP07.5.mkv",
            "Show - 07.5.mkv",
            "Show.[07.5].mkv",
        ):
            with self.subTest(filename=filename):
                self.assertTrue(has_fractional_episode_position(filename))
                self.assertIsNotNone(fractional_episode_position(filename))
        for filename in (
            "Show.S01E01.1080p.mkv",
            "Movie.1.5GB.mkv",
            "Show.v1.5.mkv",
        ):
            with self.subTest(filename=filename):
                self.assertFalse(has_fractional_episode_position(filename))


    def test_explicit_zero_episode_reserves_first_special_position(self):
        self.assertEqual(fixed_special_media_position("Show.S01E00.mkv"), 1)
        self.assertEqual(fixed_special_media_position("Show.S00E03.mkv"), 3)
        self.assertIsNone(fixed_special_media_position("Show.NCED1.mkv"))

    def test_release_root_bonus_suffixes_are_treated_as_specials(self):
        for filename in (
            "Please Twins! - Clean Opening [BD x265].mkv",
            "Please Twins! - Clean Closings [BD x265].mkv",
            "Please Twins! - Extra 2 [BD x265].mkv",
            "Please Twins! - Image Vocals 13 [BD x265].mkv",
            "Please Twins! - Menu 2 [BD x265].mkv",
            "Please Twins! - Extra 2 [1080p][x265].mkv",
            "Please Twins! - Menu 2 [French] [BD x265].mkv",
            "Please Twins! - Image Vocals 13 [WEB-DL].mkv",
            "Please Twins! - OAV [BD x265].mkv",
        ):
            with self.subTest(filename=filename):
                self.assertTrue(is_special_media_name(filename))

    def test_explicit_tmdb_markers_do_not_break_special_detection(self):
        marker_variants = (
            "tmdb20177",
            "tmdb 20177",
            "tdmb+20177",
            "{tmdb-20177}",
        )
        for marker in marker_variants:
            with self.subTest(marker=marker, kind="directory"):
                self.assertTrue(is_special_directory_name(f"Extra {marker}"))
            for filename in (
                f"Please Twins! - Clean Opening [BD x265] {marker}.mkv",
                f"Please Twins! - Extra 2 [1080p][x265] {marker}.mkv",
                f"Please Twins! - Image Vocals 13 [WEB-DL] {marker}.mkv",
                f"Please Twins! - Menu 2 [BD x265] {marker}.mkv",
                f"Please Twins! - NCOP {marker}.mkv",
            ):
                with self.subTest(marker=marker, filename=filename):
                    self.assertTrue(is_special_media_name(filename))

    def test_release_root_bonus_suffixes_do_not_capture_normal_titles(self):
        for filename in (
            "The Menu (2022).mkv",
            "Extraordinary Attorney Woo S01E01.mkv",
            "Clean (2022).mkv",
            "Vocal 01.mkv",
            "Show - Menu.mkv",
            "A Normal Movie - Menu 2 (2026).mkv",
            "A Normal Movie - Extra 2 (2026).mkv",
            "A Normal Movie - Image Vocals 13 (2026).mkv",
            "A Normal Movie - Clean Opening (2026).mkv",
            "A Normal Movie - Menu 2.mkv",
            "A Normal Movie - Menu 2 [2026].mkv",
        ):
            with self.subTest(filename=filename):
                self.assertFalse(is_special_media_name(filename))

    def test_zero_episode_and_prologue_are_treated_as_specials(self):
        for filename in (
            "Example.Show.E00.1080p.mkv",
            "Example.Show.S02E00.WEB-DL.mkv",
            "Example Show - Prologue.mkv",
        ):
            with self.subTest(filename=filename):
                self.assertTrue(is_special_media_name(filename))
                self.assertEqual(special_media_position(filename), 1)

    def test_explicit_season_zero_episodes_are_specials(self):
        expected = {
            "Example Show S00E01.mkv": 1,
            "Example.Show.S00E02.1080p.mkv": 2,
            "Example_Show_S000E123_WEB-DL.mkv": 123,
        }
        for filename, episode in expected.items():
            with self.subTest(filename=filename):
                self.assertTrue(is_special_media_name(filename))
                self.assertEqual(special_media_position(filename), episode)

    def test_special_token_boundaries_reject_normal_words_and_quality_numbers(self):
        for filename in (
            "Opera.mkv", "Education.mkv", "The.ED.ucator.mkv",
            "XNCOP.mkv", "NCOP1080p.mkv",
        ):
            with self.subTest(filename=filename):
                self.assertFalse(is_special_media_name(filename))
                self.assertIsNone(special_media_position(filename))


class OrganizePositionSafetyTests(unittest.TestCase):
    def test_tv_season_without_episode_is_blocked_before_naming(self):
        client = _TreeClient()
        ambiguous = GuangYaFile(
            "ambiguous",
            "Arifureta Shokugyou de Sekai Saikyou S2.mkv",
            False,
            1024,
            "ambiguous",
            "source",
        )
        client.tree["source"] = [ambiguous]
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].action, "skip")
        self.assertTrue(plans[0].match.need_confirm)
        self.assertEqual(plans[0].season, 2)
        self.assertIsNone(plans[0].episode)
        self.assertIn("无法确定集号", plans[0].note)
        self.assertEqual(stats["need_confirm"], 1)
        self.assertEqual(plans[0].new_name, "")

    def test_real_tmdb_scraper_does_not_treat_season_only_as_position_override(self):
        client = _TreeClient()
        client.tree["source"] = [GuangYaFile(
            "ambiguous-real",
            "Arifureta Shokugyou de Sekai Saikyou S2.mkv",
            False,
            1024,
            "ambiguous-real",
            "source",
        )]
        scraper = TMDBScraper()
        scraper.match = Mock(return_value=MatchResult(
            tmdb_id="86034",
            title="平凡职业造就世界最强",
            year="2019",
            media_type="tv",
            confidence=1.0,
            status="matched",
        ))
        scraper.get_detail = Mock(return_value={
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "seasons": [{"season_number": 2, "episode_count": 12}],
        })
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", rules, dry_run=True
        )

        self.assertEqual(plans[0].action, "skip")
        self.assertTrue(plans[0].match.need_confirm)
        self.assertEqual(plans[0].season, 2)
        self.assertIsNone(plans[0].episode)
        self.assertIn("无法确定集号", plans[0].note)
        self.assertEqual(stats["need_confirm"], 1)

    def test_fractional_episode_is_auto_mapped_for_manual_and_automatic_runs(self):
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                client = _TreeClient()
                client.tree["source"] = [GuangYaFile(
                    "fractional", "Example Show S01E07.5.mkv", False,
                    1024, "fractional", "source",
                )]
                plans, stats = Organizer(
                    client=client, scraper=_ShowScraper()
                ).organize("source", rules, dry_run=True, automatic=automatic)

                self.assertEqual(plans[0].action, "move")
                self.assertFalse(plans[0].match.need_confirm)
                self.assertEqual((plans[0].season, plans[0].episode), (0, 1))
                self.assertIn("S00E01", plans[0].new_name)
                self.assertEqual(stats["need_confirm"], 0)
                self.assertEqual(stats["fractional_specials_mapped"], 1)

    def test_fractional_episodes_share_numeric_special_sequence(self):
        client = _TreeClient()
        client.tree["source"] = [
            GuangYaFile("fractional-7", "Example Show - 7.5.mkv", False, 1024, "f7", "source"),
            GuangYaFile("fractional-1", "Example Show - 1.5.mkv", False, 1024, "f1", "source"),
            GuangYaFile("fractional-4", "Example Show - 4.5.mkv", False, 1024, "f4", "source"),
        ]
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, stats = Organizer(
            client=client, scraper=_ShowScraper()
        ).organize("source", rules, dry_run=True, automatic=True)
        by_id = {plan.file_id: plan for plan in plans}

        self.assertEqual((by_id["fractional-1"].season, by_id["fractional-1"].episode), (0, 1))
        self.assertEqual((by_id["fractional-4"].season, by_id["fractional-4"].episode), (0, 2))
        self.assertEqual((by_id["fractional-7"].season, by_id["fractional-7"].episode), (0, 3))
        self.assertEqual(stats["fractional_specials_mapped"], 3)

    def test_zero_episode_is_auto_mapped_to_first_special(self):
        client = _TreeClient()
        client.tree["source"] = [GuangYaFile(
            "episode-zero", "Example Show S01E00.mkv", False,
            1024, "episode-zero", "source",
        )]
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, stats = Organizer(
            client=client, scraper=_ShowScraper()
        ).organize("source", rules, dry_run=True, automatic=True)

        self.assertEqual(plans[0].action, "move")
        self.assertEqual((plans[0].season, plans[0].episode), (0, 1))
        self.assertIn("S00E01", plans[0].new_name)
        self.assertEqual(stats["need_confirm"], 0)

    def test_fractional_episode_can_proceed_after_explicit_manual_episode_override(self):
        client = _TreeClient()
        client.tree["source"] = [GuangYaFile(
            "fractional", "Example Show S01E07.5.mkv", False,
            1024, "fractional", "source",
        )]
        delegate = _ShowScraper()
        scraper = FixedMatchScraper(
            delegate,
            MatchResult(
                tmdb_id="86034", title="平凡职业造就世界最强", year="2019",
                media_type="tv", confidence=1.0, status="matched",
                matched_by="manual", need_confirm=False,
            ),
            delegate.get_detail("86034", "tv"),
            season_override=3,
            episode_override=7,
        )
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, stats = Organizer(client=client, scraper=scraper).organize(
            "source", rules, dry_run=True
        )

        self.assertEqual(plans[0].action, "move")
        self.assertEqual((plans[0].season, plans[0].episode), (3, 7))
        self.assertEqual(stats["need_confirm"], 0)

    def test_multi_episode_range_is_blocked_until_manually_confirmed(self):
        client = _TreeClient()
        ranged = GuangYaFile(
            "range", "Example.Show.S01E01-E03.mkv", False,
            1024, "range", "source",
        )
        client.tree["source"] = [ranged]
        scraper = _ShowScraper()
        organizer = Organizer(client=client, scraper=scraper)
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )

        plans, stats = organizer.organize("source", rules, dry_run=True)

        self.assertEqual(plans[0].action, "skip")
        self.assertTrue(plans[0].match.need_confirm)
        self.assertIn("多集文件", plans[0].note)
        self.assertEqual(stats["need_confirm"], 1)


if __name__ == "__main__":
    unittest.main()
