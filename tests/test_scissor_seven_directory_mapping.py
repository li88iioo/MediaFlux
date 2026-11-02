"""已核验 NF 发布顺序在目录预览与自动整理中的一致性回归。"""
from __future__ import annotations

from typing import Any

from typing import cast
from app.clients.guangya import GuangYaClient
from app.clients.tmdb import TMDBClient

import copy
from datetime import date, timedelta
from unittest.mock import Mock

from app.modules.directory_media import DirectoryInspection, MediaSnapshot
from app.modules.directory_scrape import DirectoryScrapeService
from tests.support import IsolatedDatabaseTestCase


def scissor_details() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    # 2026-09-10 只读核验 TMDB 79141 及 episode group
    # 5e6c0d87396e9700138b38d2；测试使用固定公开元数据，不调用网络。
    detail = {"id": 79141, "seasons": [
        {"season_number": 0, "episode_count": 4},
        {"season_number": 1, "episode_count": 10},
        {"season_number": 2, "episode_count": 10},
    ]}
    regular = {"season_number": 1, "episodes": [
        {"episode_number": number, "air_date": "2018-04-25" if number == 1 else
         (date(2018, 4, 25) + timedelta(days=7 * (number - 2))).isoformat()}
        for number in range(1, 11)
    ]}
    special = {"season_number": 0, "episodes": [
        {"id": identity, "season_number": 0, "episode_number": number,
         "name": name, "air_date": (date(2018, 8, 1) + timedelta(days=7 * (number - 1))).isoformat()}
        for number, identity, name in (
            (1, 1535061, "梅花十三"), (2, 1543201, "鸡中霸王（上）"),
            (3, 1549531, "鸡中霸王（中）"), (4, 1556661, "鸡中霸王（下）"),
        )
    ]}
    return detail, {0: special, 1: regular}


def scissor_inspection(episodes=range(11, 15)):
    videos = tuple(MediaSnapshot(
        file_id=f"e{episode}", parent_id="source",
        name=f"Scissor.Seven.S01E{episode:02d}.Episode.{episode}.1080p.NF.WEB-DL.DDP2.0.H.264-Mys.mkv",
        size=1024**3, etag=f"etag-{episode}", role="video", relative_dir="",
        season=1, episode=episode,
    ) for episode in episodes)
    return DirectoryInspection(
        directory_id="source", directory_name="Scissor Seven Season 1",
        media_type="tv", suggested_query="Scissor Seven", videos=videos,
        companions=(), counts={"videos": len(videos)}, mixed=False,
        fingerprint="fixture", season=1,
    )


class ScissorSevenDirectoryMappingTests(IsolatedDatabaseTestCase):
    def test_auto_preview_preserves_verified_specials_when_regular_season_selected(self):
        detail, seasons = scissor_details()
        for selected in (None, 1):
            with self.subTest(selected=selected):
                loader = Mock(side_effect=lambda identity, season: copy.deepcopy(seasons[season]))
                inspection = scissor_inspection()
                overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
                    inspection, detail, "auto", season_override=selected,
                    season_detail_loader=loader,
                )
                for item in inspection.videos:
                    assert item.episode is not None
                    target = (0, item.episode - 10)
                    self.assertEqual(overrides[("", item.name)], target)
                    plan = mappings[item.file_id]
                    self.assertEqual((plan.source_season, plan.source_episode), (1, item.episode))
                    self.assertEqual((plan.target_season, plan.target_episode), target)
                    self.assertEqual(plan.mode, "tmdb_special")
                self.assertEqual(loader.call_count, 2)

    def test_missing_special_proof_never_rebases_to_regular_first_four(self):
        detail, _ = scissor_details()
        for loader in (None, Mock(return_value={}), Mock(side_effect=RuntimeError("offline"))):
            with self.subTest(loader=loader):
                inspection = scissor_inspection()
                overrides, mappings = DirectoryScrapeService._mapped_position_overrides(
                    inspection, detail, "auto", season_override=1,
                    season_detail_loader=loader,
                )
                for item in inspection.videos:
                    self.assertEqual(overrides[("", item.name)], (1, item.episode))
                    self.assertFalse(mappings[item.file_id].changed)

    def test_standard_numbering_does_not_fetch_or_change_special_positions(self):
        detail, _ = scissor_details()
        inspection = scissor_inspection()
        loader = Mock(side_effect=AssertionError("保持原编号不应读取特别篇"))
        overrides, _ = DirectoryScrapeService._mapped_position_overrides(
            inspection, detail, "standard", season_override=1, season_detail_loader=loader,
        )
        for item in inspection.videos:
            self.assertEqual(overrides[("", item.name)], (1, item.episode))
        loader.assert_not_called()


    def test_verified_special_mapping_rejects_fractional_tmdb_identifiers_and_positions(self):
        from app.modules.episode_mapping import build_directory_episode_evidence, infer_overflow_tmdb_special_mapping
        for corrupt in ("work_id", "episode_id", "episode_number", "season_number"):
            with self.subTest(corrupt=corrupt):
                detail, seasons = scissor_details()
                if corrupt == "work_id":
                    detail["id"] = 79141.75
                else:
                    field = {"episode_id": "id", "episode_number": "episode_number", "season_number": "season_number"}[corrupt]
                    seasons[0]["episodes"][0][field] += 0.75
                evidence = build_directory_episode_evidence([
                    ("source", "Scissor Seven", 1, number) for number in range(11, 15)
                ])["source"]
                self.assertIsNone(infer_overflow_tmdb_special_mapping(
                    source_season=1, source_episode=11, detail=detail,
                    source_season_detail=seasons[1], special_season_detail=seasons[0],
                    directory_evidence=evidence, directory_member_count=4,
                ))


class ScissorSevenPlanningTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        for target in ("socket.socket.connect", "socket.create_connection", "requests.sessions.Session.request"):
            self.enterContext(patch(target, side_effect=AssertionError("回归禁止实网")))

    def _build(self, *, special_missing=False):
        from app.modules.directory_scrape import DirectoryScrapeStore
        from app.modules.organize import OrganizeRules
        from app.modules.scraper import TMDBScraper
        from tests.test_guangya_directory_scrape import _dir, _file, _MutableTreeClient

        detail, seasons = scissor_details()
        detail.update(name="刺客伍六七", original_name="Scissor Seven", first_air_date="2018-04-25",
                      genres=[{"id": 16, "name": "动画"}], origin_country=["CN"], credits={"cast": [], "crew": []})
        class Client:
            api_key = "fixture"
            base_url = "https://tmdb.invalid/3"
            def detail(self, identity, media_type):
                return self.get(f"/{media_type}/{identity}")
            def tv_season_detail(self, identity, season, **kwargs):
                return self.get(f"/tv/{identity}/season/{season}")
            def get(self, path, params=None):
                if path == "/tv/79141":
                    return copy.deepcopy(detail)
                if path in ("/tv/79141/season/0", "/tv/79141/season/1"):
                    number = int(path.rsplit("/", 1)[-1])
                    return {} if number == 0 and special_missing else copy.deepcopy(seasons[number])
                raise AssertionError(f"未登记元数据路径: {path}")
        source = [_file(item.file_id, item.name, "source") for item in scissor_inspection().videos]
        existing = [_file(f"old-{number}", f"刺客伍六七.2018.S01E{number:02d}-WEB-DL.1080p.H.264.24fps.EAC3.2.0.mkv", "season1") for number in range(1, 11)]
        tree = {
            "source": source,
            "archive": [_dir("anime", "动漫", "archive")],
            "anime": [_dir("show", "刺客伍六七 (2018) {tmdb-79141}", "anime")],
            "show": [_dir("season1", "Season 1", "show")],
            "season1": existing,
        }
        infos = {"source": _dir("source", "Scissor Seven Season 1"), "archive": _dir("archive", "媒体库")}
        infos.update({item.file_id: item for listing in tree.values() for item in listing})
        cloud = _MutableTreeClient(tree, infos)
        rules = OrganizeRules(target_dir_id="archive", small_file_mb=0, region_split=False,
                              year_split=False, link_strm=False, notify_enabled=False, clean_empty=False)
        scraper = TMDBScraper(client=cast(TMDBClient, Client()))
        self.addCleanup(scraper.close)
        service = DirectoryScrapeService(client=cast(GuangYaClient, cloud), scraper=scraper, store=DirectoryScrapeStore(), rules_loader=lambda: rules)
        return service, cloud, rules, scraper, detail

    def test_full_preview_targets_specials_without_colliding_with_existing_regular_episodes(self):
        service, cloud, _, _, _ = self._build()
        before = copy.deepcopy(cloud.tree)
        inspected = service.inspect("owner", "source")
        preview = service.preview("owner", inspected["inspection_id"], "79141", "tv", season=1)
        self.assertEqual(len(preview["plans"]), 4)
        for plan in preview["plans"]:
            number = int(plan["file_id"][1:])
            self.assertEqual((plan["source_season"], plan["source_episode"]), (1, number))
            self.assertEqual((plan["season"], plan["episode"]), (0, number - 10))
            self.assertIn(f"S00E{number - 10:02d}", plan["new_name"])
            self.assertTrue(plan["target_path"].endswith("/Specials"), plan["target_path"])
            self.assertNotEqual(plan["action"], "skip", plan)
        self.assertEqual(cloud.tree, before)
        self.assertEqual(cloud.deleted, [])

    def test_organizer_uses_identical_verified_mapping_and_never_falls_back_without_proof(self):
        from app.modules.episode_mapping import build_directory_episode_evidence
        from app.modules.organize import Organizer
        for missing in (False, True):
            with self.subTest(missing=missing):
                _, cloud, _, scraper, detail = self._build(special_missing=missing)
                organizer = Organizer(client=cast(GuangYaClient, cloud), scraper=scraper)
                self.addCleanup(organizer.close)
                match = scraper.match_from_tmdb("79141", "tv")
                evidence = build_directory_episode_evidence([
                    ("source", "Scissor Seven Season 1", 1, n) for n in range(11, 15)
                ])["source"]
                for number in range(11, 15):
                    mapping = organizer._infer_tmdb_episode_mapping(
                        source_context=scraper.parse_media(f"Scissor Seven.S01E{number:02d}.mkv").context,
                        match=match, detail=detail, source_season=1, source_episode=number,
                        raw_source_season=1, parent_path="Scissor Seven {tmdb-79141}",
                        directory_episode_evidence=evidence, directory_sequence_evidence=evidence,
                        directory_member_count=4, automatic=True, explicit_tmdb_id="79141",
                    )
                    self.assertEqual((mapping.target_season, mapping.target_episode),
                                     (1, number) if missing else (0, number - 10))
                    if missing:
                        self.assertFalse(mapping.changed)
                        self.assertEqual(mapping.confidence, 0)

    def test_automatic_plan_revalidates_cached_absolute_and_special_targets(self):
        from unittest.mock import patch
        from app.modules.episode_mapping import EpisodeMappingPlan, build_directory_episode_evidence
        from app.modules.organize import Organizer
        evidence = build_directory_episode_evidence([
            ("source", "Scissor Seven Season 1", 1, n) for n in range(11, 15)
        ])["source"]
        for mode in ("absolute", "tmdb_special"):
            for missing in (False, True):
                with self.subTest(mode=mode, missing=missing):
                    _, cloud, rules, scraper, _ = self._build(special_missing=missing)
                    organizer = Organizer(client=cast(GuangYaClient, cloud), scraper=scraper)
                    self.addCleanup(organizer.close)
                    before = copy.deepcopy(cloud.tree)
                    original_get = scraper.client.get
                    def get(path, params=None):
                        data = original_get(path, params)
                        if path == "/tv/79141/season/0" and data:
                            # 相同稳定 ID 被 TMDB 重排，旧 S00E01 缓存不再可信。
                            numbers = {1535061: 4, 1543201: 2, 1549531: 1, 1556661: 3}
                            for row in data["episodes"]:
                                row["episode_number"] = numbers[row["id"]]
                        return data
                    with patch.object(scraper.client, "get", side_effect=get):
                        match = scraper.match_from_tmdb("79141", "tv")
                        old_target = (2, 1) if mode == "absolute" else (0, 1)
                        cached = EpisodeMappingPlan(1, 11, *old_target, mode=mode, confidence=1.0)
                        match.preprocess_evaluated = True
                        match.effective_season, match.effective_episode = old_target
                        match.metadata = {"episode_mapping": cached.to_dict()}
                        plan = organizer._plan_one(
                            cloud.tree["source"][0], "Scissor Seven {tmdb-79141}", rules,
                            match_override=match, directory_episode_evidence=evidence,
                            directory_sequence_evidence=evidence, directory_episode_member_count=4,
                            recognition_media_type_hint="tv", media_probe_cache_only=True, automatic=True,
                        )
                    self.assertEqual((plan.source_season, plan.source_episode), (1, 11))
                    if missing:
                        self.assertEqual(plan.action, "skip")
                        assert plan.match is not None
                        self.assertTrue(plan.match.need_confirm)
                        assert plan.episode_mapping is not None
                        self.assertFalse(plan.episode_mapping.changed)
                    else:
                        self.assertEqual((plan.season, plan.episode), (0, 4))
                        self.assertEqual(plan.action, "move")
                        self.assertIn("S00E04", plan.new_name)
                        assert plan.episode_mapping is not None
                        self.assertEqual(plan.episode_mapping.mode, "tmdb_special")
                    self.assertEqual(cloud.tree, before)
                    self.assertEqual(cloud.deleted, [])
