"""光鸭目录级手动刮削与自动匹配回归测试。"""
from __future__ import annotations

import json
import re
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.clients.guangya import GuangYaFile
from app.config import web_credentials
from app.main import create_app
from app.modules.organize import OrganizeRules
from app.modules.scraper import Candidate, MatchResult, TMDBScraper
from tests.support import (
    InitializedWebTestCase,
    IsolatedDatabaseTestCase,
    release_parse_result,
)


def _dir(file_id: str, name: str, parent_id: str = "0") -> GuangYaFile:
    return GuangYaFile(
        file_id, name, True, 0, f"etag-{file_id}", parent_id, updated_at=1,
    )


def _file(
    file_id: str,
    name: str,
    parent_id: str,
    *,
    size: int = 1024 * 1024 * 1024,
    etag: str = "",
) -> GuangYaFile:
    return GuangYaFile(file_id, name, False, size, etag or f"etag-{file_id}", parent_id)


class _TreeClient:
    def __init__(self, tree: dict[str, list[GuangYaFile]], infos: dict[str, GuangYaFile]):
        self.tree = tree
        self.infos = infos

    def list_dir(self, directory_id: str) -> list[GuangYaFile]:
        return list(self.tree.get(directory_id, []))

    def file_info(self, file_id: str) -> GuangYaFile | None:
        return self.infos.get(file_id)


class _MutableTreeClient(_TreeClient):
    supports_guarded_empty_directory_delete = True

    def __init__(self, tree: dict[str, list[GuangYaFile]], infos: dict[str, GuangYaFile]):
        super().__init__(tree, infos)
        self._sequence = 0
        self.deleted: list[str] = []

    def create_dir(self, name: str, parent_id: str = "0") -> str:
        self._sequence += 1
        file_id = f"created-{self._sequence}"
        directory = _dir(file_id, name, parent_id)
        self.infos[file_id] = directory
        self.tree.setdefault(parent_id, []).append(directory)
        self.tree[file_id] = []
        return file_id

    def move(self, file_ids: list[str], parent_id: str) -> bool:
        for file_id in file_ids:
            item = self.infos[file_id]
            self.tree[item.parent_id] = [
                current for current in self.tree.get(item.parent_id, [])
                if current.file_id != file_id
            ]
            moved = GuangYaFile(
                item.file_id,
                item.name,
                item.is_dir,
                item.size,
                item.etag,
                parent_id,
            )
            self.infos[file_id] = moved
            self.tree.setdefault(parent_id, []).append(moved)
        return True

    def rename(self, file_id: str, new_name: str) -> bool:
        item = self.infos[file_id]
        renamed = GuangYaFile(
            item.file_id,
            new_name,
            item.is_dir,
            item.size,
            item.etag,
            item.parent_id,
        )
        self.infos[file_id] = renamed
        self.tree[item.parent_id] = [
            renamed if current.file_id == file_id else current
            for current in self.tree.get(item.parent_id, [])
        ]
        return True

    def delete(self, file_ids: list[str]) -> bool:
        for file_id in file_ids:
            item = self.infos.get(file_id)
            if item is None:
                continue
            if item.is_dir and self.tree.get(file_id):
                raise RuntimeError("目录非空")
            self.tree[item.parent_id] = [
                current
                for current in self.tree.get(item.parent_id, [])
                if current.file_id != file_id
            ]
            self.tree.pop(file_id, None)
            self.infos.pop(file_id, None)
            self.deleted.append(file_id)
        return True

    def delete_empty_directory(
        self,
        file_id: str,
        *,
        expected_etag: str = "",
        expected_updated_at: int = 0,
    ) -> bool:
        item = self.infos.get(file_id)
        if item is None or not item.is_dir:
            raise RuntimeError("目录状态已变化")
        if expected_etag and item.etag != expected_etag:
            raise RuntimeError("目录版本已变化")
        if expected_updated_at and item.updated_at != expected_updated_at:
            raise RuntimeError("目录更新时间已变化")
        if self.list_dir(file_id):
            raise RuntimeError("目录非空")
        return self.delete([file_id])


class _EventuallyConsistentTreeClient(_MutableTreeClient):
    """模拟光鸭创建目录成功后，父目录列表短时间仍不可见新目录。"""

    def __init__(self, tree: dict[str, list[GuangYaFile]], infos: dict[str, GuangYaFile]):
        super().__init__(tree, infos)
        self.create_calls: list[tuple[str, str]] = []

    def create_dir(self, name: str, parent_id: str = "0") -> str:
        file_id = super().create_dir(name, parent_id)
        self.create_calls.append((parent_id, name))
        self.tree[parent_id] = [
            item for item in self.tree.get(parent_id, [])
            if item.file_id != file_id
        ]
        return file_id


class _ParsingScraper:
    @staticmethod
    def _fields(filename: str) -> dict[str, object]:
        tmdb = re.search(r"(?i)tmdb-(\d+)", filename)
        season_episode = re.search(r"(?i)S(\d{1,2})E(\d{1,3})", filename)
        if season_episode:
            return {
                "type": "tv",
                "title": re.split(r"(?i)[._ -]S\d{1,2}E\d{1,3}", filename)[0],
                "season": int(season_episode.group(1)),
                "episode": int(season_episode.group(2)),
                "tmdb_id": tmdb.group(1) if tmdb else "",
            }
        stem = filename.rsplit(".", 1)[0]
        title = re.sub(r"[._ -]+(?:19|20)\d{2}.*$", "", stem)
        return {
            "type": "movie", "title": title, "season": None, "episode": None,
            "tmdb_id": tmdb.group(1) if tmdb else "",
        }

    @classmethod
    def parse_media(cls, filename: str, parent_path: str = "", match=None):
        return release_parse_result(
            cls._fields(filename), filename=filename, parent_path=parent_path
        )


class DirectoryMediaInspectorTests(unittest.TestCase):
    def test_movie_versions_are_one_media_group(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "movie-dir": [
                _file("v1", "Iron.Man.2008.1080p.mkv", "movie-dir"),
                _file("v2", "Iron.Man.2008.2160p.mkv", "movie-dir"),
                _file("s1", "Iron.Man.2008.chs.srt", "movie-dir", size=1024),
            ],
            "archive": [],
        }
        infos = {
            "movie-dir": _dir("movie-dir", "钢铁侠"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect(
            "movie-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.directory_name, "钢铁侠")
        self.assertEqual(inspection.media_type, "movie")
        self.assertEqual(inspection.suggested_query, "钢铁侠")
        self.assertEqual(inspection.counts, {"video": 2, "subtitle": 1, "metadata": 0})
        self.assertEqual([item.file_id for item in inspection.videos], ["v1", "v2"])
        self.assertFalse(inspection.mixed)
        self.assertEqual(len(inspection.fingerprint), 64)

    def test_ordinal_season_directory_is_cleaned_and_prefills_second_season(self):
        from app.modules.directory_media import DirectoryMediaInspector

        directory_name = (
            "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
            "2nd Season (BDRip 1080p HEVC FLAC)"
        )
        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "01.mkv",
                    "show-dir",
                ),
                _file(
                    "e2",
                    "Arifureta Shokugyou de Sekai Saikyou - 02.mkv",
                    "show-dir",
                ),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", directory_name),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.suggested_query, "Arifureta Shokugyou de Sekai Saikyou")
        self.assertEqual(inspection.season, 2)
        self.assertEqual([item.episode for item in inspection.videos], [1, 2])

    def test_bracketed_year_directory_infers_first_season_instead_of_year(self):
        from app.modules.directory_media import DirectoryMediaInspector

        directory_name = (
            "[GM-Team][国漫][东大高武学院][Oriental Martial Academy]"
            "[2026][01-04][GB][4K HEVC 10Bit]"
        )
        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "[GM-Team][国漫][东大高武学院][Oriental Martial Academy]"
                    "[2026][01][GB][4K HEVC 10Bit].mp4",
                    "show-dir",
                ),
                _file(
                    "e4",
                    "[GM-Team][国漫][东大高武学院][Oriental Martial Academy]"
                    "[2026][04][GB][4K HEVC 10Bit].mp4",
                    "show-dir",
                ),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", directory_name),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper()
        ).inspect(
            "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.season, 1)
        self.assertTrue(inspection.season_inferred)
        self.assertEqual(
            [(item.season, item.episode) for item in inspection.videos],
            [(None, 1), (None, 4)],
        )

    def test_parent_ordinal_attack_alias_supplies_directory_season_for_bare_files(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "01.mkv", "show-dir"),
                _file("e2", "02.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "不要欺负我，长瀞同学 2nd Attack"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper()
        ).inspect(
            "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.season, 2)
        self.assertTrue(inspection.season_inferred)
        self.assertEqual([item.episode for item in inspection.videos], [1, 2])

    def test_single_explicit_season_is_inherited_by_bare_episode_files(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "Example.Show.S02E01.mkv", "show-dir"),
                _file("e2", "Example Show - 02.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper()
        ).inspect(
            "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
        )

        self.assertEqual(inspection.season, 2)
        self.assertTrue(inspection.season_inferred)
        self.assertEqual([item.episode for item in inspection.videos], [2, 1])

    def test_directory_and_explicit_file_season_conflict_is_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "Example.Show.S01E01.mkv", "show-dir"),
                _file("e2", "Example.Show.S01E02.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show Season 02"),
            "archive": _dir("archive", "媒体库"),
        }

        with self.assertRaisesRegex(ValueError, "目录季号与文件季号冲突"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos), scraper=TMDBScraper()
            ).inspect(
                "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
            )

    def test_season_marker_and_bare_episode_are_preserved_in_directory_snapshot(self):
        from app.modules.directory_media import DirectoryMediaInspector

        name = (
            "Arifureta Shokugyou de Sekai Saikyou Season 3 - 16.mkv"
        )
        tree = {
            "show-dir": [_file("e16", name, "show-dir")],
            "archive": [],
        }
        infos = {
            "show-dir": _dir(
                "show-dir",
                "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
                "Season 3 (BDRip 1080p HEVC FLAC)",
            ),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper()
        ).inspect(
            "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
        )

        self.assertEqual(inspection.season, 3)
        self.assertEqual(inspection.videos[0].episode, 16)

    def test_episode_files_are_one_tv_group(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [_dir("season-1", "Season 1", "show-dir")],
            "season-1": [
                _file("e1", "Example.Show.S01E01.mkv", "season-1"),
                _file("e2", "Example.Show.S01E02.mkv", "season-1"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "示例剧"),
            "season-1": _dir("season-1", "Season 1", "show-dir"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(
            [(item.relative_dir, item.season, item.episode) for item in inspection.videos],
            [("Season 1", 1, 1), ("Season 1", 1, 2)],
        )

    def test_bare_episode_files_in_multiple_season_directories_keep_each_season(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _dir("season-1", "Season 01", "show-dir"),
                _dir("season-2", "Season 02", "show-dir"),
            ],
            "season-1": [_file("s1e1", "01.mkv", "season-1")],
            "season-2": [_file("s2e1", "01.mkv", "season-2")],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "season-1": _dir("season-1", "Season 01", "show-dir"),
            "season-2": _dir("season-2", "Season 02", "show-dir"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper()
        ).inspect(
            "show-dir", OrganizeRules(target_dir_id="archive", small_file_mb=0)
        )

        self.assertIsNone(inspection.season)
        self.assertEqual(
            [(item.relative_dir, item.season, item.episode) for item in inspection.videos],
            [("Season 01", 1, 1), ("Season 02", 2, 1)],
        )

    def test_all_weak_files_across_distinct_show_directories_are_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "root": [
                _dir("show-a", "Show A", "root"),
                _dir("show-b", "Show B", "root"),
            ],
            "show-a": [_file("a1", "01.mkv", "show-a")],
            "show-b": [_file("b1", "01.mkv", "show-b")],
            "archive": [],
        }
        infos = {
            "root": _dir("root", "Downloads"),
            "show-a": _dir("show-a", "Show A", "root"),
            "show-b": _dir("show-b", "Show B", "root"),
            "archive": _dir("archive", "媒体库"),
        }

        with self.assertRaisesRegex(ValueError, "多个低信息媒体子目录"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos), scraper=TMDBScraper()
            ).inspect(
                "root", OrganizeRules(target_dir_id="archive", small_file_mb=0)
            )

    def test_real_ani_episode_directory_is_one_tv_group(self):
        from app.modules.directory_media import DirectoryMediaInspector

        directory_name = (
            "[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生"
            "（仅限港澳台)"
        )
        tree = {
            "show-dir": [
                _file(
                    f"e{episode}",
                    f"被解僱的暗黑士兵 開始了慢生活的第二人生 - {episode:02d} .mp4",
                    "show-dir",
                )
                for episode in range(1, 13)
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", directory_name),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.season, 1)
        self.assertTrue(inspection.season_inferred)
        self.assertEqual([item.episode for item in inspection.videos], list(range(1, 13)))
        self.assertEqual(
            inspection.suggested_query,
            "被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生",
        )

    def test_episode_directory_keeps_single_weak_video_for_manual_confirmation(self):
        from app.modules.directory_media import DirectoryMediaInspector

        episodes = [
            _file(
                f"e{episode}",
                f"Example.Show.S01E{episode:02d}.mkv",
                "show-dir",
            )
            for episode in range(1, 13)
        ]
        tree = {
            "show-dir": [*episodes, _file("unknown", "x.mp4", "show-dir")],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(len(inspection.videos), 12)
        self.assertEqual(inspection.counts["pending_video"], 1)
        self.assertEqual(inspection.counts["video_total"], 13)
        self.assertTrue(inspection.mixed)
        self.assertEqual(inspection.pending_videos[0].file.file_id, "unknown")
        self.assertIn("保留在源目录", inspection.pending_videos[0].reason)

    def test_same_series_file_without_episode_does_not_reject_directory(self):
        from app.modules.directory_media import DirectoryMediaInspector

        title = "Boukensha ni Naritai to Miyako ni Deteitta Musume ga S-Rank"
        episodes = [
            _file(
                f"e{episode}",
                f"[orion origin] {title} - {episode:02d} [1080p].mp4",
                "show-dir",
            )
            for episode in range(1, 13)
        ]
        tree = {
            "show-dir": [
                *episodes,
                _file("unpositioned", f"[orion origin] {title} .mp4", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", f"[orion origin] {title} [01-12]"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.counts["video"], 12)
        self.assertEqual(inspection.counts["pending_video"], 1)
        self.assertEqual(inspection.counts["video_total"], 13)
        self.assertEqual(inspection.suggested_query, title.replace("-", " "))
        self.assertNotIn("unpositioned", {item.file_id for item in inspection.videos})
        self.assertEqual(inspection.pending_videos[0].file.file_id, "unpositioned")
        self.assertIn("未识别到集号", inspection.pending_videos[0].reason)

    def test_single_clearly_named_other_series_is_isolated_instead_of_blocking_primary(self):
        from app.modules.directory_media import DirectoryMediaInspector

        episodes = [
            _file(
                f"e{episode}",
                f"Example.Show.S01E{episode:02d}.mkv",
                "show-dir",
            )
            for episode in range(1, 12)
        ]
        tree = {
            "show-dir": [
                *episodes,
                _file("other", "Different.Show.S01E01.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.counts["video"], 11)
        self.assertEqual(inspection.counts["pending_video"], 1)
        self.assertEqual(inspection.pending_videos[0].file.file_id, "other")
        self.assertIn("隔离", inspection.pending_videos[0].reason)
        self.assertNotIn("other", {item.file_id for item in inspection.videos})

    def test_two_complete_series_groups_are_rejected_even_with_equal_episode_numbers(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                *[
                    _file(f"a{episode}", f"Alpha.Show.S01E{episode:02d}.mkv", "show-dir")
                    for episode in range(1, 7)
                ],
                *[
                    _file(f"b{episode}", f"Beta.Show.S01E{episode:02d}.mkv", "show-dir")
                    for episode in range(1, 7)
                ],
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Mixed Shows"),
            "archive": _dir("archive", "媒体库"),
        }

        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect(
                "show-dir",
                OrganizeRules(target_dir_id="archive", small_file_mb=0),
            )

    def test_weak_sample_with_episode_token_still_requires_manual_confirmation(self):
        from app.modules.directory_media import DirectoryMediaInspector

        episodes = [
            _file(f"e{episode}", f"Example.Show.S01E{episode:02d}.mkv", "show-dir")
            for episode in range(1, 5)
        ]
        tree = {
            "show-dir": [*episodes, _file("sample", "sample.S01E01.mkv", "show-dir")],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual({item.file_id for item in inspection.videos}, {"e1", "e2", "e3", "e4"})
        self.assertEqual(inspection.pending_videos[0].file.file_id, "sample")

    def test_other_series_ova_cannot_bypass_mixed_media_guard(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "Example.Show.S01E01.mkv", "show-dir"),
                _file("e2", "Example.Show.S01E02.mkv", "show-dir"),
                _file("e3", "Example.Show.S01E03.mkv", "show-dir"),
                _file("ova", "Other.Show.OVA.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
        }

        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect(
                "show-dir",
                OrganizeRules(target_dir_id="archive", small_file_mb=0),
            )

    def test_traditional_and_simplified_episode_titles_share_one_group(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "被解僱的暗黑士兵 開始了慢生活的第二人生 - 01 .mp4",
                    "show-dir",
                ),
                _file(
                    "e2",
                    "被解雇的暗黑士兵 开始了慢生活的第二人生 - 02 .mp4",
                    "show-dir",
                ),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "被解雇的暗黑士兵开始了慢生活的第二人生"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual([item.episode for item in inspection.videos], [1, 2])

    def test_single_parenthesized_series_qualifier_does_not_split_group(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "被解僱的暗黑士兵 開始了慢生活的第二人生 - 01 .mp4",
                    "show-dir",
                ),
                _file(
                    "e2",
                    "被解僱的暗黑士兵（30多岁）開始了慢生活的第二人生 - 02 .mp4",
                    "show-dir",
                ),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.counts["video"], 2)

    def test_special_only_generic_directory_requires_manual_tmdb_confirmation(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "root": [_dir("extras", "Specials", "root")],
            "extras": [_file("ova", "OVA.mkv", "extras")],
            "archive": [],
        }
        infos = {
            "root": _dir("root", "1"),
            "extras": _dir("extras", "Specials", "root"),
            "archive": _dir("archive", "媒体库"),
        }
        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper(),
        ).inspect("root", OrganizeRules(target_dir_id="archive", small_file_mb=0))

        self.assertTrue(inspection.requires_manual_match)
        self.assertIn("人工选择", inspection.manual_match_reason)
        self.assertEqual([(item.season, item.episode) for item in inspection.videos], [(0, 1)])

    def test_special_only_named_work_directory_can_still_auto_match(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show": [_file("ova", "OVA.mkv", "show")],
            "archive": [],
        }
        infos = {
            "show": _dir("show", "Kono Subarashii Sekai ni Shukufuku wo!"),
            "archive": _dir("archive", "媒体库"),
        }
        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos), scraper=TMDBScraper(),
        ).inspect("show", OrganizeRules(target_dir_id="archive", small_file_mb=0))

        self.assertFalse(inspection.requires_manual_match)

    def test_ova_is_grouped_with_numbered_episodes_as_a_tv_special(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "Kono Subarashii Sekai ni Shukufuku wo! - 01.mkv", "show-dir"),
                _file("ova", "Kono Subarashii Sekai ni Shukufuku wo! - OVA.mkv", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Kono Subarashii Sekai ni Shukufuku wo!"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(
            [(item.file_id, item.season, item.episode) for item in inspection.videos],
            [("e1", None, 1), ("ova", 0, 1)],
        )

    def test_numbered_ncop_nced_are_grouped_with_bracketed_mp4_episodes(self):
        from app.modules.directory_media import DirectoryMediaInspector

        title = "Isekai_Maou_to_Shoukan_Shoujo_no_Dorei_Majutsu"
        tree = {
            "show-dir": [
                _file("e1", f"[KTXP][{title}][01][BIG5][1080p][BDrip].mp4", "show-dir"),
                _file("e2", f"[KTXP][{title}][02][BIG5][1080p][BDrip].mp4", "show-dir"),
                _file("nced1", f"[KTXP][{title}][NCED1][BIG5][1080p][BDrip].mp4", "show-dir"),
                _file("ncop", f"[KTXP][{title}][NCOP][BIG5][1080p][BDrip].mp4", "show-dir"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", title),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(
            inspection.suggested_query,
            "Isekai Maou to Shoukan Shoujo no Dorei Majutsu",
        )
        self.assertEqual(
            [(item.file_id, item.season, item.episode) for item in inspection.videos],
            [("e1", None, 1), ("e2", None, 2), ("nced1", 0, 1), ("ncop", 0, 2)],
        )

    def test_extra_subdirectory_is_grouped_as_tv_specials(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "Kono Subarashii Sekai ni Shukufuku wo! 2 - 01.mkv",
                    "show-dir",
                ),
                _dir("extras", "Extra", "show-dir"),
            ],
            "extras": [
                _file("nced", "NCED.mkv", "extras"),
                _file("ncop", "NCOP.mkv", "extras"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir(
                "show-dir",
                "Kono Subarashii Sekai ni Shukufuku wo! 2nd Season",
            ),
            "extras": _dir("extras", "Extra", "show-dir"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.season, 2)
        self.assertEqual(
            [
                (item.file_id, item.relative_dir, item.season, item.episode)
                for item in inspection.videos
            ],
            [
                ("e1", "", None, 1),
                ("nced", "Extra", 0, 1),
                ("ncop", "Extra", 0, 2),
            ],
        )

    def test_extra_menu_videos_are_grouped_as_tv_specials(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file(
                    "e1",
                    "Isekai Maou to Shoukan Shoujo no Dorei Majutsu - 01.mkv",
                    "show-dir",
                ),
                _file(
                    "e2",
                    "Isekai Maou to Shoukan Shoujo no Dorei Majutsu - 02.mkv",
                    "show-dir",
                ),
                _dir("extras", "Extra", "show-dir"),
            ],
            "extras": [
                _file("menu-1", "Menu 1-1.mkv", "extras"),
                _file("menu-2", "Menu 1-2.mkv", "extras"),
            ],
            "archive": [],
        }
        infos = {
            "show-dir": _dir(
                "show-dir",
                "Isekai Maou to Shoukan Shoujo no Dorei Majutsu",
            ),
            "extras": _dir("extras", "Extra", "show-dir"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        snapshots = {item.file_id: item for item in inspection.videos}
        self.assertEqual(set(snapshots), {"e1", "e2", "menu-1", "menu-2"})
        self.assertEqual((snapshots["menu-1"].season, snapshots["menu-1"].episode), (0, 1))
        self.assertEqual((snapshots["menu-2"].season, snapshots["menu-2"].episode), (0, 2))

    def test_sps_path_ncop_and_omnibus_are_grouped_as_tv_specials(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "show-dir": [
                _file("e1", "Arifureta Shokugyou de Sekai Saikyou - 01.mkv", "show-dir"),
                _file("ncop", "NCOP.mkv", "show-dir"),
                _file("omnibus", "Arifureta Shokugyou de Sekai Saikyou.OMNIBUS.mkv", "show-dir"),
                _dir("sps", "SPs", "show-dir"),
            ],
            "sps": [_file("feature", "Featurette.mkv", "sps")],
            "archive": [],
        }
        infos = {
            "show-dir": _dir(
                "show-dir",
                "[H-Enc] Arifureta Shokugyou de Sekai Saikyou 2nd Season",
            ),
            "sps": _dir("sps", "SPs", "show-dir"),
            "archive": _dir("archive", "媒体库"),
        }

        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=TMDBScraper(),
        ).inspect(
            "show-dir",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual(inspection.season, 2)
        by_id = {item.file_id: item for item in inspection.videos}
        self.assertEqual((by_id["e1"].season, by_id["e1"].episode), (None, 1))
        self.assertEqual((by_id["ncop"].season, by_id["ncop"].episode), (0, 2))
        self.assertEqual((by_id["omnibus"].season, by_id["omnibus"].episode), (0, 1))
        self.assertEqual((by_id["feature"].season, by_id["feature"].episode), (0, 3))

    def test_distinct_video_child_directories_are_rejected_as_mixed(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "mixed": [
                _dir("iron-man", "钢铁侠", "mixed"),
                _dir("captain", "美国队长", "mixed"),
            ],
            "iron-man": [_file("v1", "Iron.Man.2008.mkv", "iron-man")],
            "captain": [_file("v2", "Captain.America.2011.mkv", "captain")],
            "archive": [],
        }
        infos = {
            "mixed": _dir("mixed", "漫威电影"),
            "iron-man": _dir("iron-man", "钢铁侠", "mixed"),
            "captain": _dir("captain", "美国队长", "mixed"),
            "archive": _dir("archive", "媒体库"),
        }

        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect(
                "mixed",
                OrganizeRules(target_dir_id="archive", small_file_mb=0),
            )

    def test_archive_target_inside_source_is_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "source": [
                _file("v1", "Iron.Man.2008.mkv", "source"),
                _dir("archive-child", "归档", "source"),
            ],
            "archive-child": [],
        }
        infos = {
            "source": _dir("source", "钢铁侠"),
            "archive-child": _dir("archive-child", "归档", "source"),
        }

        with self.assertRaisesRegex(ValueError, "归档目标位于所选目录内"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect(
                "source",
                OrganizeRules(target_dir_id="archive-child", small_file_mb=0),
            )

    def test_distinct_movies_in_selected_directory_root_are_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "mixed": [
                _file("v1", "Iron.Man.2008.mkv", "mixed"),
                _file("v2", "Captain.America.2011.mkv", "mixed"),
            ],
            "archive": [],
        }
        infos = {
            "mixed": _dir("mixed", "漫威电影"),
            "archive": _dir("archive", "媒体库"),
        }
        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect("mixed", OrganizeRules(target_dir_id="archive", small_file_mb=0))

    def test_movie_and_episode_mix_is_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "mixed": [
                _file("movie", "Iron.Man.2008.mkv", "mixed"),
                _file("episode", "Example.Show.S01E01.mkv", "mixed"),
            ],
            "archive": [],
        }
        infos = {
            "mixed": _dir("mixed", "混合目录"),
            "archive": _dir("archive", "媒体库"),
        }
        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos),
                scraper=_ParsingScraper(),
            ).inspect("mixed", OrganizeRules(target_dir_id="archive", small_file_mb=0))

    def test_same_movie_in_resolution_subdirectories_is_not_mixed(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "movie": [_dir("1080", "1080p", "movie"), _dir("2160", "2160p", "movie")],
            "1080": [_file("v1", "Iron.Man.2008.mkv", "1080")],
            "2160": [_file("v2", "Iron.Man.2008.mkv", "2160")],
            "archive": [],
        }
        infos = {
            "movie": _dir("movie", "钢铁侠"),
            "1080": _dir("1080", "1080p", "movie"),
            "2160": _dir("2160", "2160p", "movie"),
            "archive": _dir("archive", "媒体库"),
        }
        inspection = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        ).inspect("movie", OrganizeRules(target_dir_id="archive", small_file_mb=0))
        self.assertEqual(inspection.media_type, "movie")
        self.assertEqual(inspection.counts["video"], 2)

    def test_distinct_explicit_tmdb_ids_are_rejected(self):
        from app.modules.directory_media import DirectoryMediaInspector
        tree = {
            "mixed": [
                _file("v1", "Movie.A.{tmdb-100}.mkv", "mixed"),
                _file("v2", "Movie.B.{tmdb-200}.mkv", "mixed"),
            ],
            "archive": [],
        }
        infos = {"mixed": _dir("mixed", "混合"), "archive": _dir("archive", "媒体库")}
        with self.assertRaisesRegex(ValueError, "多个不同媒体"):
            DirectoryMediaInspector(
                client=_TreeClient(tree, infos), scraper=_ParsingScraper()
            ).inspect("mixed", OrganizeRules(target_dir_id="archive", small_file_mb=0))


class DirectoryMediaValidationErrorTests(unittest.TestCase):
    def setUp(self):
        from app.modules.directory_media import DirectoryMediaInspector

        self.inspector_type = DirectoryMediaInspector
        self.rules = OrganizeRules(target_dir_id="archive", small_file_mb=0)

    def test_no_supported_video_is_explicit_request_error(self):
        from app.modules.directory_scrape_errors import DirectoryScrapeRequestError

        client = _TreeClient(
            {"source": [_file("note", "README.txt", "source")], "archive": []},
            {"source": _dir("source", "空目录"), "archive": _dir("archive", "媒体库")},
        )
        with self.assertRaisesRegex(DirectoryScrapeRequestError, "没有支持的视频"):
            self.inspector_type(client=client, scraper=_ParsingScraper()).inspect(
                "source", self.rules
            )

    def test_directory_cycle_is_explicit_request_error(self):
        from app.modules.directory_scrape_errors import DirectoryScrapeRequestError

        client = _TreeClient(
            {"source": [_dir("source", "循环", "source")], "archive": []},
            {"source": _dir("source", "循环目录"), "archive": _dir("archive", "媒体库")},
        )
        with self.assertRaisesRegex(DirectoryScrapeRequestError, "循环引用"):
            self.inspector_type(client=client, scraper=_ParsingScraper()).inspect(
                "source", self.rules
            )

    def test_directory_depth_limit_is_explicit_request_error(self):
        from app.modules.directory_scrape_errors import DirectoryScrapeRequestError

        tree = {"archive": []}
        infos = {"archive": _dir("archive", "媒体库")}
        for index in range(66):
            current = "source" if index == 0 else f"d{index}"
            child = f"d{index + 1}"
            tree[current] = [_dir(child, child, current)]
            infos.setdefault(current, _dir(current, current))
            infos[child] = _dir(child, child, current)
        with self.assertRaisesRegex(DirectoryScrapeRequestError, "64 层"):
            self.inspector_type(
                client=_TreeClient(tree, infos), scraper=_ParsingScraper()
            ).inspect("source", self.rules)


class SingleFileInspectionTests(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules.directory_media import DirectoryMediaInspector

        source_items = [
            _file("supergirl", "Supergirl.2026.mkv", "source"),
            _file("other", "Other.Movie.2025.mkv", "source"),
            _file(
                "supergirl-sub",
                "Supergirl.2026.zh.srt",
                "source",
                size=1024,
            ),
            _file("other-sub", "Other.Movie.2025.srt", "source", size=1024),
            _file("supergirl-nfo", "Supergirl.2026.nfo", "source", size=1024),
            _file("supergirl-poster", "Supergirl.2026.jpg", "source", size=1024),
            _file("advert", "请勿相信广告.txt", "source", size=1024),
            _dir("extras", "花絮", "source"),
        ]
        tree = {
            "source": source_items,
            "extras": [_file("extra-video", "Behind.The.Scenes.mkv", "extras")],
            "archive": [],
        }
        infos = {
            "source": _dir("source", "下载目录"),
            "archive": _dir("archive", "媒体库"),
            **{item.file_id: item for item in source_items},
        }
        self.rules = OrganizeRules(target_dir_id="archive", small_file_mb=0)
        self.inspector = DirectoryMediaInspector(
            client=_TreeClient(tree, infos),
            scraper=_ParsingScraper(),
        )

    def test_selected_video_does_not_include_other_videos_or_junk(self):
        inspection = self.inspector.inspect_file("supergirl", self.rules)

        self.assertEqual([item.file_id for item in inspection.videos], ["supergirl"])
        self.assertEqual(
            [item.file_id for item in inspection.companions],
            ["supergirl-sub"],
        )
        self.assertEqual(inspection.directory_id, "source")
        self.assertEqual(inspection.directory_name, "Supergirl.2026.mkv")
        self.assertEqual(inspection.counts, {
            "video": 1,
            "subtitle": 1,
            "metadata": 0,
        })

    def test_release_episode_uses_clean_title_and_infers_season_one(self):
        from app.modules.directory_media import DirectoryMediaInspector

        episode = _file(
            "episode-3",
            "[LoliHouse] The Ghost in the Shell - 03 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv",
            "source",
        )
        client = _TreeClient(
            {"source": [episode], "archive": []},
            {
                "source": _dir("source", "下载目录"),
                "archive": _dir("archive", "媒体库"),
                "episode-3": episode,
            },
        )
        inspection = DirectoryMediaInspector(
            client=client,
            scraper=TMDBScraper(),
        ).inspect_file("episode-3", self.rules)

        self.assertEqual(inspection.suggested_query, "The Ghost in the Shell")
        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual((inspection.season, inspection.episode), (1, 3))
        self.assertTrue(inspection.season_inferred)

    def test_single_file_rejects_parent_directory_as_archive_target(self):
        rules = OrganizeRules(target_dir_id="source", small_file_mb=0)

        with self.assertRaisesRegex(ValueError, "归档目标位于所选目录内"):
            self.inspector.inspect_file("supergirl", rules)

    def test_single_file_rejects_descendant_archive_target(self):
        nested = _dir("nested-target", "归档", "source")
        self.inspector.client.tree["source"].append(nested)
        self.inspector.client.tree["nested-target"] = []
        self.inspector.client.infos["nested-target"] = nested
        rules = OrganizeRules(target_dir_id="nested-target", small_file_mb=0)

        with self.assertRaisesRegex(ValueError, "归档目标位于所选目录内"):
            self.inspector.inspect_file("supergirl", rules)

    def test_non_video_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "视频"):
            self.inspector.inspect_file("advert", self.rules)

    def test_video_below_small_file_threshold_is_rejected_during_inspection(self):
        small_video = _file(
            "small-video",
            "Short.Movie.2026.mkv",
            "source",
            size=5 * 1024 * 1024,
        )
        self.inspector.client.tree["source"].append(small_video)
        self.inspector.client.infos["small-video"] = small_video
        rules = OrganizeRules(target_dir_id="archive", small_file_mb=10)

        with self.assertRaisesRegex(ValueError, "小文件"):
            self.inspector.inspect_file("small-video", rules)

    def test_source_inside_archive_sibling_branch_can_inspect_preview_and_execute(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        inbox = _dir("inbox", "inbox", "archive")
        movies = _dir("movies", "电影", "archive")
        source_items = [
            _file("selected", "Iron.Man.2008.mkv", "inbox"),
            _file("selected-sub", "Iron.Man.2008.zh.srt", "inbox", size=1024),
            _file("other", "Other.Movie.2025.mkv", "inbox"),
            _file("other-sub", "Other.Movie.2025.srt", "inbox", size=1024),
            _file("selected-nfo", "Iron.Man.2008.nfo", "inbox", size=1024),
            _file("selected-poster", "Iron.Man.2008.jpg", "inbox", size=1024),
            _file("note", "说明.txt", "inbox", size=1024),
        ]
        tree = {
            "archive": [inbox, movies],
            "inbox": source_items,
            "movies": [],
        }
        infos = {
            "archive": _dir("archive", "媒体库"),
            "inbox": inbox,
            "movies": movies,
            **{item.file_id: item for item in source_items},
        }
        client = _MutableTreeClient(tree, infos)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        service = DirectoryScrapeService(
            client=client,
            scraper=_DirectoryScrapeTMDB(),
            store=DirectoryScrapeStore(),
            rules_loader=lambda: rules,
        )

        inspection_id = service.inspect_file("owner", "selected")["inspection_id"]
        preview = service.preview("owner", inspection_id, "1726", "movie")
        self.assertEqual([row["file_id"] for row in preview["plans"]], ["selected"])
        self.assertEqual(
            [
                row["file_id"]
                for row in preview["companion_plans"]
                if row["action"] == "move"
            ],
            ["selected-sub"],
        )

        result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["moved"], 1)
        self.assertNotEqual(client.infos["selected"].parent_id, "inbox")
        self.assertNotEqual(client.infos["selected-sub"].parent_id, "inbox")
        for file_id in (
            "other",
            "other-sub",
            "selected-nfo",
            "selected-poster",
            "note",
        ):
            self.assertEqual(client.infos[file_id].parent_id, "inbox")


class _DirectoryScrapeTMDB:
    api_key = "tmdb-test-key"

    def __init__(self):
        self.search_calls: list[tuple[str, str, str]] = []
        self.detail_calls: list[tuple[str, str]] = []
        self.auto_result = MatchResult(
            tmdb_id="1726",
            title="钢铁侠",
            year="2008",
            media_type="movie",
            confidence=0.96,
            status="matched",
            matched_by="search",
            threshold=0.9,
        )

    def search_candidates(
        self,
        query: str,
        year: str = "",
        media_type: str = "movie",
    ) -> list[Candidate]:
        self.search_calls.append((query, year, media_type))
        if media_type == "tv":
            return [
                Candidate(
                    "123",
                    "钢铁侠：装甲冒险",
                    "2009",
                    0.81,
                    "tv",
                    original_title="Iron Man: Armored Adventures",
                )
            ]
        return [
            Candidate(
                "1726",
                "钢铁侠",
                "2008",
                0.96,
                "movie",
                original_title="Iron Man",
                overview="托尼·斯塔克制造钢铁战甲。",
                poster_path="/movie.jpg",
                backdrop_path="/movie-bg.jpg",
                release_date="2008-04-30",
            )
        ]

    def get_detail_with_credits(self, tmdb_id: str, media_type: str) -> dict:
        self.detail_calls.append((tmdb_id, media_type))
        if media_type == "tv":
            return {
                "id": int(tmdb_id),
                "name": "钢铁侠：装甲冒险",
                "original_name": "Iron Man: Armored Adventures",
                "first_air_date": "2009-04-24",
                "overview": "少年托尼·斯塔克的装甲冒险。",
                "vote_average": 7.2,
                "poster_path": "/tv.jpg",
                "backdrop_path": "/tv-bg.jpg",
                "genres": [{"name": "动画"}],
                "created_by": [{"name": "Craig Kyle"}],
                "credits": {"cast": [{"name": "Adrian Petriw"}], "crew": []},
            }
        return {
            "id": int(tmdb_id),
            "title": "钢铁侠",
            "original_title": "Iron Man",
            "release_date": "2008-04-30",
            "overview": "托尼·斯塔克制造钢铁战甲。",
            "vote_average": 7.6,
            "poster_path": "/movie.jpg",
            "backdrop_path": "/movie-bg.jpg",
            "genres": [{"name": "动作"}, {"name": "科幻"}],
            "credits": {
                "cast": [
                    {"name": "小罗伯特·唐尼"},
                    {"name": "格温妮斯·帕特洛"},
                ],
                "crew": [{"name": "乔恩·费儒", "job": "Director"}],
            },
        }

    def match_from_tmdb(self, tmdb_id: str, media_type: str) -> MatchResult:
        detail = self.get_detail_with_credits(tmdb_id, media_type)
        title = detail.get("name") or detail.get("title") or ""
        date = detail.get("first_air_date") or detail.get("release_date") or ""
        return MatchResult(
            tmdb_id=str(tmdb_id),
            title=title,
            year=str(date)[:4],
            media_type=media_type,
            confidence=1.0,
            locked=True,
            status="matched",
            matched_by="tmdb_id",
            threshold=1.0,
        )

    def parse_media(self, filename: str, parent_path: str = "", match=None):
        return release_parse_result(
            _ParsingScraper._fields(filename),
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str) -> dict:
        return self.get_detail_with_credits(tmdb_id, media_type)

    def match(self, _filename: str, _parent_path: str = "") -> MatchResult:
        return self.auto_result


class _RecordingTMDBClient:
    api_key = "key"
    base_url = "https://api.themoviedb.org/3"
    session = None

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, dict(params or {})))
        return {"id": 1726, "credits": {"cast": [], "crew": []}}


class TMDBCreditsDetailTests(unittest.TestCase):
    def test_detail_with_credits_uses_append_to_response(self):
        client = _RecordingTMDBClient()

        detail = TMDBScraper(client=client).get_detail_with_credits("1726", "movie")

        self.assertEqual(detail["id"], 1726)
        self.assertEqual(
            client.calls,
            [("/movie/1726", {"append_to_response": "credits"})],
        )


    def test_successful_credits_detail_is_reused_by_plain_detail_and_repeat_calls(self):
        client = _RecordingTMDBClient()
        scraper = TMDBScraper(client=client)

        first = scraper.get_detail_with_credits("1726", "movie")
        second = scraper.get_detail_with_credits("1726", "movie")
        plain = scraper.get_detail("1726", "movie")

        self.assertEqual(first["id"], 1726)
        self.assertEqual(second, first)
        self.assertEqual(plain, first)
        self.assertEqual(
            client.calls,
            [("/movie/1726", {"append_to_response": "credits"})],
        )


class DirectoryScrapeServiceTests(unittest.TestCase):
    def setUp(self):
        from app.modules.directory_media import DirectoryMediaInspector
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        self.tree = {
            "movie-dir": [
                _file("v1", "Iron.Man.2008.1080p.mkv", "movie-dir"),
                _file("v2", "Iron.Man.2008.2160p.mkv", "movie-dir"),
                _file("s1", "Iron.Man.2008.chs.srt", "movie-dir", size=1024),
            ],
            "archive": [],
        }
        self.infos = {
            "movie-dir": _dir("movie-dir", "钢铁侠"),
            "archive": _dir("archive", "媒体库"),
        }
        self.client = _TreeClient(self.tree, self.infos)
        self.scraper = _DirectoryScrapeTMDB()
        self.rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        self.inspection = DirectoryMediaInspector(
            client=self.client,
            scraper=self.scraper,
        ).inspect("movie-dir", self.rules)
        self.store = DirectoryScrapeStore(clock=lambda: 100.0)
        self.inspection_id = self.store.put_inspection(
            "owner",
            self.inspection,
            self.rules,
        )
        self.service = DirectoryScrapeService(
            client=self.client,
            scraper=self.scraper,
            store=self.store,
            rules_loader=lambda: self.rules,
        )

    def test_store_rejects_another_session_owner(self):
        with self.assertRaisesRegex(KeyError, "检查记录不存在或已过期"):
            self.store.get_inspection("owner-b", self.inspection_id)

    def test_store_expires_inspection_after_ten_minutes(self):
        clock = SimpleNamespace(value=100.0)
        from app.modules.directory_scrape import DirectoryScrapeStore

        store = DirectoryScrapeStore(clock=lambda: clock.value)
        inspection_id = store.put_inspection("owner", self.inspection, self.rules)
        clock.value = 701.0

        with self.assertRaisesRegex(KeyError, "检查记录不存在或已过期"):
            store.get_inspection("owner", inspection_id)

    def test_search_auto_uses_detected_media_type_only(self):
        candidates = self.service.search(
            "owner",
            self.inspection_id,
            "钢铁侠",
            "auto",
        )

        self.assertEqual(
            [(item["tmdb_id"], item["media_type"]) for item in candidates],
            [("1726", "movie")],
        )
        self.assertEqual(candidates[0]["director"], [])
        self.assertEqual(candidates[0]["cast"], [])
        self.assertEqual(candidates[0]["poster_url"], "https://image.tmdb.org/t/p/w342/movie.jpg")
        self.assertEqual(
            self.scraper.search_calls,
            [("钢铁侠", "", "movie")],
        )
        self.assertEqual(self.scraper.detail_calls, [])

    def test_external_hints_are_read_only_and_do_not_change_tmdb_scoring(self):
        from app.discovery.models import MediaCard

        discovery = Mock()
        discovery.search.return_value = SimpleNamespace(
            query="钢铁侠",
            items=(MediaCard(
                provider="douban",
                external_id="1432146",
                media_type="movie",
                title="钢铁侠",
                original_title="Iron Man",
                year="2008",
                rating=8.4,
                rating_source="douban",
            ),),
            errors=(),
            providers_attempted=("douban",),
            providers_succeeded=("douban",),
        )
        with patch(
            "app.discovery.search.get_discovery_search_service",
            return_value=discovery,
        ), patch("app.modules.recognition_hints.get_bool", return_value=True):
            payload = self.service.external_hints(
                "owner", self.inspection_id, "钢铁侠", "auto"
            )

        self.assertEqual(payload["items"][0]["provider"], "douban")
        self.assertEqual(payload["items"][0]["title"], "钢铁侠")
        self.assertIn("自动整理失败后的第二轮 TMDB 查询", payload["advisory"])
        discovery.search.assert_called_once_with(
            "钢铁侠", 1, ["douban"], timeout_seconds=5.0
        )
        self.assertEqual(self.scraper.search_calls, [])

    def test_manual_preview_applies_one_tmdb_match_to_all_movie_versions(self):
        original_list_dir = self.client.list_dir
        self.client.list_dir = Mock(wraps=original_list_dir)
        preview = self.service.preview(
            "owner",
            self.inspection_id,
            "1726",
            "movie",
        )

        self.assertFalse(preview["cloud_write"])
        self.assertEqual(preview["match"]["tmdb_id"], "1726")
        self.assertEqual(len(preview["plans"]), 2)
        self.assertTrue(all(
            plan["target_path"].endswith("钢铁侠 (2008) {tmdb-1726}")
            for plan in preview["plans"]
        ))
        self.assertEqual(preview["counts"]["subtitle"], 1)
        self.assertEqual(len(preview["preview_id"]), 32)
        source_reads = [
            call for call in self.client.list_dir.call_args_list
            if call.args and call.args[0] == "movie-dir"
        ]
        self.assertEqual(
            len(source_reads), 1,
            "预览应复用刚完成的目录检查快照，不能再次扫描源目录",
        )

    def test_dry_run_media_probe_only_reads_cache(self):
        from app.modules.media_probe import probe_media_profile

        file = _file("probe", "Movie.2026.1080p.mkv", "movie-dir")
        client = Mock()
        with patch("app.modules.media_probe.db.get_media_probe_cache", return_value=None):
            profile = probe_media_profile(
                file, client, enabled=True, timeout=90, cache_only=True,
            )

        self.assertIsNone(profile)
        client.get_download_url.assert_not_called()

    def test_preview_rejects_source_changes_after_inspection(self):
        self.tree["movie-dir"][0] = _file(
            "v1", "Changed.Name.2008.mkv", "movie-dir"
        )
        with self.assertRaisesRegex(RuntimeError, "目录内容已变化"):
            self.service.preview(
                "owner",
                self.inspection_id,
                "1726",
                "movie",
            )

    def test_preview_rejects_invalid_media_type(self):
        with self.assertRaisesRegex(ValueError, "媒体类型"):
            self.service.preview("owner", self.inspection_id, "1726", "music")

    def test_single_video_preview_includes_subtitle_target_name(self):
        from app.modules.directory_media import DirectoryMediaInspector

        tree = {
            "single": [
                _file("video", "Iron.Man.2008.mkv", "single"),
                _file("subtitle", "Iron.Man.2008.chs.srt", "single", size=1024),
            ],
            "archive": [],
        }
        infos = {
            "single": _dir("single", "钢铁侠"),
            "archive": _dir("archive", "媒体库"),
        }
        client = _TreeClient(tree, infos)
        inspection = DirectoryMediaInspector(
            client=client, scraper=self.scraper
        ).inspect("single", self.rules)
        inspection_id = self.store.put_inspection("owner", inspection, self.rules)
        from app.modules.directory_scrape import DirectoryScrapeService

        preview = DirectoryScrapeService(
            client=client,
            scraper=self.scraper,
            store=self.store,
            rules_loader=lambda: self.rules,
        ).preview("owner", inspection_id, "1726", "movie")
        self.assertEqual(preview["companion_plans"], [{
            "file_id": "subtitle",
            "role": "subtitle",
            "original_name": "Iron.Man.2008.chs.srt",
            "relative_dir": "",
            "video_file_id": "video",
            "action": "move",
            "target_name": "钢铁侠.2008.zh-Hans.srt",
            "note": "",
        }])

    def test_auto_low_confidence_returns_manual_payload_without_preview(self):
        self.scraper.auto_result = MatchResult(
            tmdb_id="1726",
            title="钢铁侠",
            year="2008",
            media_type="movie",
            confidence=0.5,
            candidates=self.scraper.search_candidates("钢铁侠", "", "movie"),
            need_confirm=True,
            error="匹配置信度不足",
            status="low_confidence",
            matched_by="search",
            threshold=0.9,
        )

        result = self.service.auto_match("owner", self.inspection_id)

        self.assertEqual(result["status"], "requires_manual")
        self.assertEqual(result["suggested_query"], "钢铁侠")
        self.assertEqual(result["candidates"][0]["tmdb_id"], "1726")
        self.assertNotIn("preview_id", result)

    def test_auto_type_mismatch_requires_manual_confirmation(self):
        self.scraper.auto_result = MatchResult(
            tmdb_id="123",
            title="钢铁侠：装甲冒险",
            year="2009",
            media_type="tv",
            confidence=0.99,
            status="matched",
            matched_by="search",
            threshold=0.9,
        )
        result = self.service.auto_match("owner", self.inspection_id)
        self.assertEqual(result["status"], "requires_manual")
        self.assertIn("类型", result["message"])


class DirectoryScrapeExecutionTests(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules.directory_media import DirectoryMediaInspector
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        tree = {
            "movie-dir": [
                _file("v1", "Iron.Man.2008.1080p.mkv", "movie-dir"),
                _file("v2", "Iron.Man.2008.2160p.mkv", "movie-dir"),
            ],
            "archive": [],
        }
        infos = {
            "movie-dir": _dir("movie-dir", "钢铁侠"),
            "archive": _dir("archive", "媒体库"),
        }
        infos.update({
            item.file_id: item
            for items in tree.values()
            for item in items
        })
        self.client = _MutableTreeClient(tree, infos)
        self.scraper = _DirectoryScrapeTMDB()
        self.rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
            keep_multi_versions=True,
        )
        self.store = DirectoryScrapeStore()
        inspection = DirectoryMediaInspector(
            client=self.client,
            scraper=self.scraper,
        ).inspect("movie-dir", self.rules)
        inspection_id = self.store.put_inspection("owner", inspection, self.rules)
        self.service = DirectoryScrapeService(
            client=self.client,
            scraper=self.scraper,
            store=self.store,
            rules_loader=lambda: self.rules,
        )
        self.preview_id = self.service.preview(
            "owner",
            inspection_id,
            "1726",
            "movie",
        )["preview_id"]

    def test_execute_preview_applies_fixed_match_and_existing_conflict_rules(self):
        self.scraper.confirm = Mock()

        result = self.service.execute_preview("owner", self.preview_id)

        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["skipped"], 1)
        self.assertEqual(
            {db.get_organize_log(log_id)["tmdb_id"] for log_id in result["log_ids"]},
            {"1726"},
        )
        operation_tokens = {
            str(db.get_organize_log(log_id)["operation_token"] or "")
            for log_id in result["log_ids"]
        }
        self.assertEqual(len(operation_tokens), 1)
        self.assertTrue(next(iter(operation_tokens)).startswith("manual-"))
        self.assertNotEqual(self.client.infos["v1"].parent_id, "movie-dir")
        self.assertEqual(self.client.infos["v2"].parent_id, "movie-dir")
        self.assertIn("钢铁侠.2008", self.client.infos["v1"].name)
        self.scraper.confirm.assert_called_once()
        args = self.scraper.confirm.call_args.args
        kwargs = self.scraper.confirm.call_args.kwargs
        self.assertEqual(args[:5], (
            "Iron.Man.2008.1080p.mkv", "1726", "钢铁侠", "2008", "movie",
        ))
        self.assertEqual(kwargs["parent_path"], "钢铁侠")

    def test_manual_learning_failure_does_not_rollback_successful_move(self):
        self.scraper.confirm = Mock(side_effect=RuntimeError("learning unavailable"))

        result = self.service.execute_preview("owner", self.preview_id)

        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["failed"], 0)
        self.assertNotEqual(self.client.infos["v1"].parent_id, "movie-dir")
        self.scraper.confirm.assert_called_once()

    def test_execute_preview_keeps_probe_cache_only_to_match_preview(self):
        from app.modules.organize import Organizer

        original = Organizer.organize
        calls = []

        def tracked(instance, *args, **kwargs):
            calls.append(dict(kwargs))
            return original(instance, *args, **kwargs)

        with patch.object(Organizer, "organize", autospec=True, side_effect=tracked):
            self.service.execute_preview("owner", self.preview_id)

        write_calls = [call for call in calls if call.get("dry_run") is False]
        self.assertEqual(len(write_calls), 1)
        self.assertIs(write_calls[0].get("media_probe_cache_only"), True)

    def test_preview_warms_the_probe_cache_online(self):
        """执行阶段只读缓存，因此预览必须负责在线探测把缓存预热。

        否则动态范围/位深/帧率/音频只能从原文件名猜测，最终归档名会退化。
        """
        from app.modules.organize import Organizer

        original = Organizer.organize
        calls = []

        def tracked(instance, *args, **kwargs):
            calls.append(dict(kwargs))
            return original(instance, *args, **kwargs)

        from app.modules.directory_media import DirectoryMediaInspector

        inspection = DirectoryMediaInspector(
            client=self.client, scraper=self.scraper,
        ).inspect("movie-dir", self.rules)
        inspection_id = self.store.put_inspection("owner", inspection, self.rules)
        with patch.object(Organizer, "organize", autospec=True, side_effect=tracked):
            self.service.preview("owner", inspection_id, "1726", "movie")

        self.assertTrue(calls)
        self.assertIs(calls[0].get("media_probe_cache_only"), False)

    def test_execute_preview_rejects_changed_directory_snapshot(self):
        self.client.rename("v1", "Changed.Name.2008.mkv")

        with self.assertRaisesRegex(RuntimeError, "目录内容已变化"):
            self.service.execute_preview("owner", self.preview_id)

    def test_execute_preview_rejects_target_conflict_drift(self):
        movie_root = self.client.create_dir("电影", "archive")
        media_dir = self.client.create_dir("钢铁侠 (2008) {tmdb-1726}", movie_root)
        existing = _file("existing", "钢铁侠.2008.mkv", media_dir, size=2 * 1024 ** 3)
        self.client.tree[media_dir].append(existing)
        self.client.infos[existing.file_id] = existing

        with self.assertRaisesRegex(RuntimeError, "归档目标内容已变化"):
            self.service.execute_preview("owner", self.preview_id)


class PartialDirectoryScrapeExecutionTests(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        source_items = [
            _file(f"e{episode}", f"Example.Show.S01E{episode:02d}.mkv", "show-dir")
            for episode in range(1, 5)
        ]
        source_items.append(_file("unknown", "x.mp4", "show-dir"))
        tree = {"show-dir": source_items, "archive": []}
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
            **{item.file_id: item for item in source_items},
        }
        self.client = _MutableTreeClient(tree, infos)
        self.scraper = _DirectoryScrapeTMDB()
        self.rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=True,
            link_strm=False,
            notify_enabled=False,
        )
        self.store = DirectoryScrapeStore()
        self.service = DirectoryScrapeService(
            client=self.client,
            scraper=self.scraper,
            store=self.store,
            rules_loader=lambda: self.rules,
        )

    def test_preview_and_execute_only_process_primary_series_group(self):
        inspection = self.service.inspect("owner", "show-dir")
        self.assertEqual(inspection["counts"]["video"], 4)
        self.assertEqual(inspection["counts"]["pending_video"], 1)
        self.assertEqual(inspection["pending_videos"][0]["file_id"], "unknown")

        preview = self.service.preview(
            "owner",
            inspection["inspection_id"],
            "123",
            "tv",
        )
        self.assertEqual(
            {item["file_id"] for item in preview["plans"]},
            {"e1", "e2", "e3", "e4"},
        )
        self.assertEqual(preview["stats"]["pending_confirmation"], 1)

        result = self.service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["moved"], 4)
        self.assertEqual(result["stats"]["pending_confirmation"], 1)
        self.assertEqual(self.client.infos["unknown"].parent_id, "show-dir")
        self.assertIn("show-dir", self.client.infos)
        for episode in range(1, 5):
            self.assertNotEqual(self.client.infos[f"e{episode}"].parent_id, "show-dir")

    def test_pending_file_change_invalidates_existing_preview(self):
        inspection = self.service.inspect("owner", "show-dir")
        preview = self.service.preview(
            "owner",
            inspection["inspection_id"],
            "123",
            "tv",
        )
        self.client.rename("unknown", "changed-x.mp4")

        with self.assertRaisesRegex(RuntimeError, "目录内容已变化"):
            self.service.execute_preview("owner", preview["preview_id"])

    def test_new_empty_source_directory_invalidates_existing_preview(self):
        inspection = self.service.inspect("owner", "show-dir")
        preview = self.service.preview(
            "owner",
            inspection["inspection_id"],
            "123",
            "tv",
        )
        new_directory_id = self.client.create_dir("new-empty", "show-dir")

        with self.assertRaisesRegex(RuntimeError, "目录内容已变化"):
            self.service.execute_preview("owner", preview["preview_id"])

        self.assertIn(new_directory_id, self.client.infos)

    def test_video_specific_metadata_moves_while_pending_video_stays(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        episodes = [
            _file(f"m{episode}", f"Example.Show.S01E{episode:02d}.mkv", "show-dir")
            for episode in range(1, 5)
        ]
        metadata = _file("episode-nfo", "Example.Show.S01E01.nfo", "show-dir", size=1024)
        unknown = _file("metadata-unknown", "x.mp4", "show-dir")
        tree = {"show-dir": [*episodes, metadata, unknown], "archive": []}
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "archive": _dir("archive", "媒体库"),
            "episode-nfo": metadata,
            "metadata-unknown": unknown,
            **{item.file_id: item for item in episodes},
        }
        client = _MutableTreeClient(tree, infos)
        service = DirectoryScrapeService(
            client=client,
            scraper=self.scraper,
            store=DirectoryScrapeStore(),
            rules_loader=lambda: self.rules,
        )
        inspection = service.inspect("owner", "show-dir")
        self.assertEqual(inspection["counts"]["metadata"], 1)
        preview = service.preview("owner", inspection["inspection_id"], "123", "tv")
        self.assertEqual(
            [item["file_id"] for item in preview["companion_plans"] if item["action"] == "move"],
            ["episode-nfo"],
        )

        result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["metadata_moved"], 1)
        self.assertNotEqual(client.infos["episode-nfo"].parent_id, "show-dir")
        self.assertEqual(client.infos["metadata-unknown"].parent_id, "show-dir")

    def test_nested_pending_directory_is_preserved_during_empty_cleanup(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        season_dir = _dir("season", "Season 01", "show-dir")
        pending_dir = _dir("pending-dir", "待处理", "show-dir")
        episodes = [
            _file(f"n{episode}", f"Example.Show.S01E{episode:02d}.mkv", "season")
            for episode in range(1, 5)
        ]
        unknown = _file("nested-unknown", "x.mp4", "pending-dir")
        tree = {
            "show-dir": [season_dir, pending_dir],
            "season": episodes,
            "pending-dir": [unknown],
            "archive": [],
        }
        infos = {
            "show-dir": _dir("show-dir", "Example Show"),
            "season": season_dir,
            "pending-dir": pending_dir,
            "nested-unknown": unknown,
            "archive": _dir("archive", "媒体库"),
            **{item.file_id: item for item in episodes},
        }
        client = _MutableTreeClient(tree, infos)
        service = DirectoryScrapeService(
            client=client,
            scraper=self.scraper,
            store=DirectoryScrapeStore(),
            rules_loader=lambda: self.rules,
        )
        inspection = service.inspect("owner", "show-dir")
        preview = service.preview("owner", inspection["inspection_id"], "123", "tv")

        result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["moved"], 4)
        self.assertNotIn("season", client.infos)
        self.assertIn("pending-dir", client.infos)
        self.assertEqual(client.infos["nested-unknown"].parent_id, "pending-dir")


class SingleFileScrapeScopeTests(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        source_items = [
            _file("supergirl", "Supergirl.2026.mkv", "source"),
            _file("other", "Other.Movie.2025.mkv", "source"),
            _file(
                "supergirl-sub",
                "Supergirl.2026.zh.srt",
                "source",
                size=1024,
            ),
            _file("other-sub", "Other.Movie.2025.srt", "source", size=1024),
            _file("supergirl-nfo", "Supergirl.2026.nfo", "source", size=1024),
            _file("supergirl-poster", "Supergirl.2026.jpg", "source", size=1024),
            _file("advert", "请勿相信广告.txt", "source", size=1024),
            _dir("extras", "花絮", "source"),
        ]
        tree = {
            "source": source_items,
            "extras": [_file("extra-video", "Behind.The.Scenes.mkv", "extras")],
            "archive": [],
        }
        infos = {
            "source": _dir("source", "下载目录"),
            "archive": _dir("archive", "媒体库"),
            **{
                item.file_id: item
                for items in tree.values()
                for item in items
            },
        }
        self.client = _MutableTreeClient(tree, infos)
        self.scraper = _DirectoryScrapeTMDB()
        self.rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        self.store = DirectoryScrapeStore()
        self.service = DirectoryScrapeService(
            client=self.client,
            scraper=self.scraper,
            store=self.store,
            rules_loader=lambda: self.rules,
        )

    def _inspect_file(self) -> str:
        result = self.service.inspect_file("owner", "supergirl")
        return result["inspection_id"]

    def test_file_inspection_records_file_scope(self):
        inspection_id = self._inspect_file()

        record = self.store.get_inspection("owner", inspection_id)
        self.assertEqual(record.scope_type, "file")
        self.assertEqual(record.scope_id, "supergirl")

    def test_file_preview_only_plans_selected_video_and_matching_subtitle(self):
        inspection_id = self._inspect_file()

        preview = self.service.preview("owner", inspection_id, "1726", "movie")

        self.assertEqual(
            [row["file_id"] for row in preview["plans"]],
            ["supergirl"],
        )
        moved_companions = [
            row["file_id"]
            for row in preview["companion_plans"]
            if row["action"] == "move"
        ]
        self.assertEqual(moved_companions, ["supergirl-sub"])
        preview_record = self.store.get_preview("owner", preview["preview_id"])
        self.assertEqual(preview_record.scope_type, "file")
        self.assertEqual(preview_record.scope_id, "supergirl")

    def test_unknown_scope_type_is_rejected_instead_of_scanning_broadly(self):
        inspection_id = self._inspect_file()
        record = self.store.get_inspection("owner", inspection_id)
        record.scope_type = "unknown"

        with self.assertRaisesRegex(RuntimeError, "作用域"):
            self.service.preview("owner", inspection_id, "1726", "movie")

    def test_file_execute_never_moves_other_video_or_junk(self):
        inspection_id = self._inspect_file()
        preview_id = self.service.preview(
            "owner",
            inspection_id,
            "1726",
            "movie",
        )["preview_id"]

        result = self.service.execute_preview("owner", preview_id)

        self.assertEqual(result["stats"]["moved"], 1)
        self.assertNotEqual(self.client.infos["supergirl"].parent_id, "source")
        self.assertNotEqual(self.client.infos["supergirl-sub"].parent_id, "source")
        for file_id in (
            "other",
            "other-sub",
            "supergirl-nfo",
            "supergirl-poster",
            "advert",
            "extras",
            "extra-video",
        ):
            expected_parent = "extras" if file_id == "extra-video" else "source"
            self.assertEqual(self.client.infos[file_id].parent_id, expected_parent)


class DirectorySeasonOverrideTests(IsolatedDatabaseTestCase):
    class SeasonScraper(_DirectoryScrapeTMDB):
        _parser = TMDBScraper()

        def parse_media(self, filename: str, parent_path: str = "", match=None):
            return self._parser.parse_media(filename, parent_path, match)

        def get_detail_with_credits(self, tmdb_id: str, media_type: str) -> dict:
            if media_type == "tv":
                return {
                    "id": int(tmdb_id),
                    "name": "平凡职业造就世界最强",
                    "original_name": "Arifureta Shokugyou de Sekai Saikyou",
                    "first_air_date": "2019-07-08",
                    "overview": "",
                    "genres": [{"id": 16, "name": "动画"}],
                    "origin_country": ["JP"],
                    "seasons": [
                        {"season_number": 1, "episode_count": 13},
                        {"season_number": 2, "episode_count": 12},
                        {"season_number": 3, "episode_count": 16},
                    ],
                    "credits": {"cast": [], "crew": []},
                }
            return super().get_detail_with_credits(tmdb_id, media_type)

    def _build(
        self,
        *,
        clean_empty: bool = False,
        directory_name: str | None = None,
        source_items: list[GuangYaFile] | None = None,
        client_type: type[_MutableTreeClient] = _MutableTreeClient,
    ):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        directory_name = directory_name or (
            "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
            "2nd Season (BDRip 1080p HEVC FLAC)"
        )
        episodes = source_items or [
            _file("episode-1", "Arifureta Shokugyou de Sekai Saikyou - 01.mkv", "source"),
            _file("episode-2", "Arifureta Shokugyou de Sekai Saikyou - 02.mkv", "source"),
        ]
        tree = {"source": episodes, "archive": []}
        infos = {
            "source": _dir("source", directory_name),
            "archive": _dir("archive", "媒体库"),
            **{item.file_id: item for item in episodes},
        }
        client = client_type(tree, infos)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=clean_empty,
            link_strm=False,
            notify_enabled=False,
        )
        store = DirectoryScrapeStore()
        service = DirectoryScrapeService(
            client=client,
            scraper=self.SeasonScraper(),
            store=store,
            rules_loader=lambda: rules,
        )
        return service, store, client

    def test_directory_inferred_season_is_used_by_preview_and_execute(self):
        service, store, client = self._build()
        inspected = service.inspect("owner", "source")

        self.assertEqual(inspected["suggested_query"], "Arifureta Shokugyou de Sekai Saikyou")
        self.assertEqual(inspected["season"], 2)
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )

        self.assertTrue(all("/Season 2" in plan["target_path"] for plan in preview["plans"]))
        self.assertEqual(
            [plan["new_name"] for plan in preview["plans"]],
            [
                "平凡职业造就世界最强.2019.S02E01.mkv",
                "平凡职业造就世界最强.2019.S02E02.mkv",
            ],
        )
        record = store.get_preview("owner", preview["preview_id"])
        self.assertEqual((record.season_override, record.episode_override), (2, None))

        service.execute_preview("owner", preview["preview_id"])
        self.assertIn("S02E01", client.infos["episode-1"].name)
        self.assertIn("S02E02", client.infos["episode-2"].name)

    def test_multi_season_subdirectories_preview_duplicate_episode_names_safely(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        tree = {
            "source": [
                _dir("season-1", "Season 01", "source"),
                _dir("season-2", "Season 02", "source"),
            ],
            "season-1": [_file("s1e1", "01.mkv", "season-1")],
            "season-2": [_file("s2e1", "01.mkv", "season-2")],
            "archive": [],
        }
        infos = {
            "source": _dir("source", "Arifureta Shokugyou de Sekai Saikyou"),
            "season-1": _dir("season-1", "Season 01", "source"),
            "season-2": _dir("season-2", "Season 02", "source"),
            "archive": _dir("archive", "媒体库"),
            "s1e1": tree["season-1"][0],
            "s2e1": tree["season-2"][0],
        }
        client = _MutableTreeClient(tree, infos)
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0,
            region_split=False, year_split=False,
            link_strm=False, notify_enabled=False,
        )
        service = DirectoryScrapeService(
            client=client,
            scraper=self.SeasonScraper(),
            store=DirectoryScrapeStore(),
            rules_loader=lambda: rules,
        )

        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )

        self.assertEqual(
            [(plan["file_id"], plan["new_name"]) for plan in preview["plans"]],
            [
                ("s1e1", "平凡职业造就世界最强.2019.S01E01.mkv"),
                ("s2e1", "平凡职业造就世界最强.2019.S02E01.mkv"),
            ],
        )
        self.assertIn("/Season 1", preview["plans"][0]["target_path"])
        self.assertIn("/Season 2", preview["plans"][1]["target_path"])

    def test_filename_season_and_bare_episode_preview_includes_episode_number(self):
        service, _store, _client = self._build(
            directory_name=(
                "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
                "Season 3 (BDRip 1080p HEVC FLAC)"
            ),
            source_items=[_file(
                "episode-16",
                "Arifureta Shokugyou de Sekai Saikyou Season 3 - 16.mkv",
                "source",
            )],
        )
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )

        self.assertEqual(len(preview["plans"]), 1)
        self.assertIn("/Season 3", preview["plans"][0]["target_path"])
        self.assertIn("S03E16", preview["plans"][0]["new_name"])

    def test_execute_reuses_created_target_chain_when_directory_listing_is_stale(self):
        source_items = [
            _file(
                f"episode-{episode}",
                f"Arifureta Shokugyou de Sekai Saikyou - {episode:02d}.mkv",
                "source",
            )
            for episode in range(1, 4)
        ]
        service, _store, client = self._build(
            directory_name=(
                "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
                "3rd Season (BDRip 1080p HEVC FLAC)"
            ),
            source_items=source_items,
            client_type=_EventuallyConsistentTreeClient,
        )
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )
        target_paths = {plan["target_path"] for plan in preview["plans"]}

        service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(len(target_paths), 1)
        self.assertEqual(
            [name for _parent_id, name in client.create_calls],
            list(next(iter(target_paths)).split("/")),
        )
        self.assertEqual(
            len({client.infos[item.file_id].parent_id for item in source_items}),
            1,
        )

    def test_ova_is_archived_under_specials_without_overriding_regular_season(self):
        service, _store, client = self._build(
            directory_name="Kono Subarashii Sekai ni Shukufuku wo!",
            source_items=[
                _file(
                    "episode-1",
                    "Kono Subarashii Sekai ni Shukufuku wo! - 01.mkv",
                    "source",
                ),
                _file(
                    "ova-1",
                    "Kono Subarashii Sekai ni Shukufuku wo! - OVA.mkv",
                    "source",
                ),
            ],
            client_type=_EventuallyConsistentTreeClient,
        )
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv", season=1
        )

        targets = {plan["file_id"]: plan for plan in preview["plans"]}
        self.assertTrue(targets["episode-1"]["target_path"].endswith("/Season 1"))
        self.assertIn("S01E01", targets["episode-1"]["new_name"])
        self.assertTrue(targets["ova-1"]["target_path"].endswith("/Specials"))
        self.assertIn("S00E01", targets["ova-1"]["new_name"])

        service.execute_preview("owner", preview["preview_id"])

        episode_dir = client.infos["episode-1"].parent_id
        specials_dir = client.infos["ova-1"].parent_id
        self.assertNotEqual(episode_dir, specials_dir)
        self.assertEqual(
            client.infos[episode_dir].parent_id,
            client.infos[specials_dir].parent_id,
        )
        self.assertEqual(
            sum(name.endswith("{tmdb-86034}") for _parent_id, name in client.create_calls),
            1,
        )

    def test_execute_cleans_selected_empty_source_directory_when_enabled(self):
        service, _store, client = self._build(clean_empty=True)
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )

        result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["source_dir_cleaned"], 1)
        self.assertIn("source", client.deleted)
        self.assertNotIn("source", client.infos)

    def test_cleanup_preserves_configured_permanent_source_directory(self):
        service, _store, client = self._build(clean_empty=True)
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )
        configured = '[{"id":"source","name":"永久来源"}]'

        with patch("app.modules.directory_scrape.config.get", return_value=configured):
            result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["source_dir_cleaned"], 0)
        self.assertEqual(result["stats"]["source_dir_cleanup_protected"], 1)
        self.assertIn(
            "根目录按安全策略保留",
            result["stats"]["empty_dir_cleanup_reasons"][0],
        )
        self.assertNotIn("source", client.deleted)
        self.assertIn("source", client.infos)

    def test_cleanup_refreshes_directory_version_after_confirming_it_is_empty(self):
        service, _store, client = self._build(clean_empty=True)
        client.tree["source"] = []
        fresh = GuangYaFile(
            "source", "下载目录", True, 0, "etag-fresh", "0", updated_at=99
        )
        client.infos["source"] = fresh
        original_delete = client.delete_empty_directory
        client.file_info = Mock(wraps=client.file_info)
        client.delete_empty_directory = Mock(wraps=original_delete)
        record = SimpleNamespace(
            scope_type="directory", scope_id="source", rules=service.rules_loader()
        )
        current = SimpleNamespace(directory_id="source", directory_name="下载目录")
        stats = {}

        service._cleanup_selected_source(record, current, stats)

        client.file_info.assert_called_once_with("source")
        client.delete_empty_directory.assert_called_once_with(
            "source", expected_etag="etag-fresh", expected_updated_at=99
        )
        self.assertEqual(stats["source_dir_cleaned"], 1)

    def test_cleanup_rechecks_cancel_immediately_before_recycle_delete(self):
        service, _store, client = self._build(clean_empty=True)
        client.tree["source"] = []
        client.delete_empty_directory = Mock(wraps=client.delete_empty_directory)
        record = SimpleNamespace(
            scope_type="directory", scope_id="source", rules=service.rules_loader()
        )
        current = SimpleNamespace(directory_id="source", directory_name="下载目录")
        stats = {}
        checks = iter((False, True))

        service._cleanup_selected_source(
            record,
            current,
            stats,
            cancel_check=lambda: next(checks),
        )

        client.delete_empty_directory.assert_not_called()
        self.assertEqual(stats["stopped"], 1)
        self.assertEqual(stats["source_dir_cleanup_skipped"], 1)
        self.assertIn("source", client.infos)
        audit = db.list_organize_delete_audits(limit=1)[0]
        self.assertEqual(audit["status"], "blocked")
        self.assertEqual(
            audit["provider_result"],
            "未调用光鸭 provider；对象保留",
        )

    def test_cleanup_preserves_empty_source_when_execution_stats_are_unsafe(self):
        from types import SimpleNamespace

        service, _store, client = self._build(clean_empty=True)
        client.tree["source"] = []
        record = SimpleNamespace(
            scope_type="directory", scope_id="source", rules=service.rules_loader()
        )
        current = SimpleNamespace(directory_id="source", directory_name="下载目录")
        stats = {"failed": 1}

        service._cleanup_selected_source(record, current, stats)

        self.assertEqual(stats["source_dir_cleaned"], 0)
        self.assertEqual(stats["source_dir_cleanup_skipped"], 1)
        self.assertNotIn("source", client.deleted)
        self.assertIn("source", client.infos)

    def test_execute_preserves_source_directory_when_non_media_file_remains(self):
        service, _store, client = self._build(
            clean_empty=True,
            source_items=[
                _file(
                    "episode-1",
                    "Arifureta Shokugyou de Sekai Saikyou - 01.mkv",
                    "source",
                ),
                _file("note", "说明.txt", "source", size=128),
            ],
        )
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv"
        )

        result = service.execute_preview("owner", preview["preview_id"])

        self.assertEqual(result["stats"]["source_dir_cleaned"], 0)
        self.assertEqual(result["stats"]["source_dir_cleanup_not_empty"], 1)
        self.assertIn(
            "仍有 1 个文件或子目录",
            result["stats"]["empty_dir_cleanup_reasons"][0],
        )
        self.assertNotIn("source", client.deleted)
        self.assertIn("source", client.infos)

    def test_extra_files_preview_and_execute_into_specials(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        root_items = [
            _file(
                "episode-1",
                "Arifureta Shokugyou de Sekai Saikyou - 01.mkv",
                "source",
            ),
            _dir("extras", "Extra", "source"),
        ]
        extra_items = [
            _file("nced", "NCED.mkv", "extras"),
            _file("ncop", "NCOP.mkv", "extras"),
        ]
        tree = {"source": root_items, "extras": extra_items, "archive": []}
        infos = {
            "source": _dir(
                "source",
                "[H-Enc] Arifureta Shokugyou de Sekai Saikyou 2nd Season",
            ),
            "extras": _dir("extras", "Extra", "source"),
            "archive": _dir("archive", "媒体库"),
            **{item.file_id: item for item in [*root_items, *extra_items]},
        }
        client = _MutableTreeClient(tree, infos)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        service = DirectoryScrapeService(
            client=client,
            scraper=self.SeasonScraper(),
            store=DirectoryScrapeStore(),
            rules_loader=lambda: rules,
        )
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner", inspected["inspection_id"], "86034", "tv", season=2
        )
        plans = {plan["file_id"]: plan for plan in preview["plans"]}

        self.assertTrue(plans["episode-1"]["target_path"].endswith("/Season 2"))
        self.assertIn("S02E01", plans["episode-1"]["new_name"])
        self.assertTrue(plans["nced"]["target_path"].endswith("/Specials"))
        self.assertIn("S00E01", plans["nced"]["new_name"])
        self.assertTrue(plans["ncop"]["target_path"].endswith("/Specials"))
        self.assertIn("S00E02", plans["ncop"]["new_name"])

        service.execute_preview("owner", preview["preview_id"])

        self.assertIn("S00E01", client.infos["nced"].name)
        self.assertIn("S00E02", client.infos["ncop"].name)
        self.assertEqual(
            client.infos["nced"].parent_id,
            client.infos["ncop"].parent_id,
        )
        self.assertEqual(client.infos[client.infos["nced"].parent_id].name, "Specials")

    def test_manual_directory_season_override_changes_all_episodes_only(self):
        service, store, _client = self._build()
        inspected = service.inspect("owner", "source")
        preview = service.preview(
            "owner",
            inspected["inspection_id"],
            "86034",
            "tv",
            season=3,
        )

        self.assertEqual(
            [plan["new_name"] for plan in preview["plans"]],
            [
                "平凡职业造就世界最强.2019.S03E01.mkv",
                "平凡职业造就世界最强.2019.S03E02.mkv",
            ],
        )
        record = store.get_preview("owner", preview["preview_id"])
        self.assertEqual((record.season_override, record.episode_override), (3, None))

        with self.assertRaisesRegex(
            Exception, "不能覆盖全部集号"
        ):
            service.preview(
                "owner",
                inspected["inspection_id"],
                "86034",
                "tv",
                season=3,
                episode=1,
            )


class SingleEpisodeOverrideTests(IsolatedDatabaseTestCase):
    class EpisodeScraper(_DirectoryScrapeTMDB):
        _parser = TMDBScraper()

        def parse_media(self, filename: str, parent_path: str = "", match=None):
            return self._parser.parse_media(filename, parent_path, match)

        def get_detail_with_credits(self, tmdb_id: str, media_type: str) -> dict:
            if media_type == "tv":
                return {
                    "id": int(tmdb_id),
                    "name": "攻壳机动队",
                    "original_name": "The Ghost in the Shell",
                    "first_air_date": "2026-01-01",
                    "overview": "",
                    "genres": [{"id": 16, "name": "动画"}],
                    "origin_country": ["JP"],
                    "seasons": [{"season_number": 0, "episode_count": 3}, {"season_number": 1, "episode_count": 12}],
                    "credits": {"cast": [], "crew": []},
                }
            return super().get_detail_with_credits(tmdb_id, media_type)

    def _build(self, filename=None):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore

        filename = filename or "[LoliHouse] The Ghost in the Shell - 03 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        episode = _file("episode-3", filename, "source")
        tree = {"source": [episode], "archive": []}
        infos = {
            "source": _dir("source", "下载目录"),
            "archive": _dir("archive", "媒体库"),
            "episode-3": episode,
        }
        client = _MutableTreeClient(tree, infos)
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            region_split=False,
            year_split=False,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        store = DirectoryScrapeStore()
        service = DirectoryScrapeService(
            client=client,
            scraper=self.EpisodeScraper(),
            store=store,
            rules_loader=lambda: rules,
        )
        return service, store, client

    def test_episode_without_season_defaults_to_s01_and_clean_query(self):
        service, _store, _client = self._build()
        inspected = service.inspect_file("owner", "episode-3")

        self.assertEqual(inspected["suggested_query"], "The Ghost in the Shell")
        self.assertEqual(inspected["media_type"], "tv")
        self.assertEqual((inspected["season"], inspected["episode"]), (1, 3))
        self.assertTrue(inspected["season_inferred"])

        preview = service.preview(
            "owner", inspected["inspection_id"], "255358", "tv"
        )
        self.assertIn("S01E03", preview["plans"][0]["new_name"])
        self.assertTrue(preview["plans"][0]["target_path"].endswith("/Season 1"))

    def test_season_only_hint_does_not_require_an_episode_override(self):
        service, _store, _client = self._build("The.Ghost.in.the.Shell.S01.mkv")
        inspected = service.inspect_file("owner", "episode-3")

        self.assertEqual((inspected["season"], inspected["episode"]), (1, None))
        preview = service.preview(
            "owner", inspected["inspection_id"], "255358", "tv"
        )
        self.assertIn("S01", preview["plans"][0]["new_name"])

    def test_parsed_episode_zero_is_normalized_to_first_special_in_preview(self):
        service, _store, _client = self._build("The.Ghost.in.the.Shell.S01E00.mkv")
        inspected = service.inspect_file("owner", "episode-3")

        self.assertEqual((inspected["season"], inspected["episode"]), (1, 0))
        preview = service.preview(
            "owner", inspected["inspection_id"], "255358", "tv"
        )
        self.assertIn("S00E01", preview["plans"][0]["new_name"])
        self.assertTrue(preview["plans"][0]["target_path"].endswith("/Specials"))

    def test_fractional_episode_is_normalized_to_special_in_preview(self):
        service, _store, _client = self._build(
            "The.Ghost.in.the.Shell.S01E07.5.mkv"
        )
        inspected = service.inspect_file("owner", "episode-3")

        preview = service.preview(
            "owner", inspected["inspection_id"], "255358", "tv"
        )

        self.assertIn("S00E01", preview["plans"][0]["new_name"])
        self.assertTrue(preview["plans"][0]["target_path"].endswith("/Specials"))

    def test_manual_episode_override_does_not_relabel_existing_other_episodes(self):
        service, _store, client = self._build(
            "The.Ghost.in.the.Shell.S02E04.1080p.WEB-DL.H265.AAC.mkv"
        )
        anime = client.create_dir("动漫", "archive")
        show = client.create_dir("攻壳机动队 (2026) {tmdb-255358}", anime)
        season = client.create_dir("Season 2", show)
        existing = [
            _file(
                "existing-2",
                "攻壳机动队.2026.S02E02.1080p.WEB-DL.H265.AAC.mkv",
                season,
                size=2 * 1024 * 1024 * 1024,
            ),
            _file(
                "existing-3",
                "攻壳机动队.2026.S02E03.1080p.WEB-DL.H265.AAC.mkv",
                season,
                size=2 * 1024 * 1024 * 1024,
            ),
        ]
        client.tree[season].extend(existing)
        client.infos.update({item.file_id: item for item in existing})

        inspected = service.inspect_file("owner", "episode-3")
        preview = service.preview(
            "owner",
            inspected["inspection_id"],
            "255358",
            "tv",
            season=2,
            episode=4,
        )

        self.assertEqual(preview["plans"][0]["action"], "move")
        self.assertEqual(preview["plans"][0]["conflict_decision"], "new")
        service.execute_preview("owner", preview["preview_id"])
        self.assertEqual(client.infos["episode-3"].parent_id, season)
        self.assertIn("S02E04", client.infos["episode-3"].name)
        self.assertIn("existing-2", client.infos)
        self.assertIn("existing-3", client.infos)

    def test_manual_season_zero_override_is_persisted_for_execute(self):
        service, store, client = self._build()
        inspected = service.inspect_file("owner", "episode-3")
        preview = service.preview(
            "owner",
            inspected["inspection_id"],
            "255358",
            "tv",
            season=0,
            episode=3,
        )

        self.assertIn("S00E03", preview["plans"][0]["new_name"])
        self.assertTrue(preview["plans"][0]["target_path"].endswith("/Specials"))
        record = store.get_preview("owner", preview["preview_id"])
        self.assertEqual((record.season_override, record.episode_override), (0, 3))

        service.execute_preview("owner", preview["preview_id"])
        self.assertIn("S00E03", client.infos["episode-3"].name)

class ScopedGuangYaClientTests(unittest.TestCase):
    def test_source_parent_is_filtered_only_when_source_scan_is_armed(self):
        from app.modules.directory_scrape import ScopedGuangYaClient

        selected = _file("selected", "Selected.mkv", "source")
        other = _file("other", "Other.mkv", "source")
        archived = _file("archived", "Archived.mkv", "archive")
        client = _TreeClient(
            {"source": [selected, other], "archive": [archived]},
            {
                "source": _dir("source", "下载目录"),
                "archive": _dir("archive", "媒体库"),
                "selected": selected,
                "other": other,
                "archived": archived,
            },
        )

        scoped = ScopedGuangYaClient(client, "source", {"selected"})

        scoped.begin_source_scan()
        self.assertEqual(
            [item.file_id for item in scoped.list_dir("source")],
            ["selected"],
        )
        self.assertEqual(
            [item.file_id for item in scoped.list_dir("source")],
            ["selected", "other"],
        )
        scoped.begin_source_scan()
        self.assertEqual(
            [item.file_id for item in scoped.list_dir("source")],
            ["selected"],
        )
        self.assertEqual(
            [item.file_id for item in scoped.list_dir("archive")],
            ["archived"],
        )
        self.assertIs(scoped.file_info("other"), other)


class _RouteService:
    def __init__(self):
        self.auto_result = {
            "status": "requires_manual",
            "inspection_id": "inspection-1",
            "suggested_query": "钢铁侠",
            "candidates": [{"tmdb_id": "1726", "media_type": "movie"}],
        }

    def inspect(self, owner: str, directory_id: str) -> dict:
        return {
            "inspection_id": "inspection-1",
            "directory": {"id": directory_id, "name": "钢铁侠"},
            "media_type": "movie",
            "suggested_query": "钢铁侠",
            "counts": {"video": 1, "subtitle": 0, "metadata": 0},
            "archive_target": {"id": "archive", "name": "媒体库"},
            "rules_summary": {"rename_enabled": True},
        }

    def inspect_file(self, owner: str, file_id: str) -> dict:
        return {
            "inspection_id": "file-inspection-1",
            "directory": {"id": "source", "name": "女超人"},
            "media_type": "tv",
            "suggested_query": "女超人",
            "counts": {"video": 1, "subtitle": 1, "metadata": 0},
            "archive_target": {"id": "archive", "name": "媒体库"},
            "rules_summary": {"rename_enabled": True},
            "selected_file_id": file_id,
        }

    def search(self, *_args, **_kwargs) -> list[dict]:
        return [{"tmdb_id": "1726", "title": "钢铁侠", "media_type": "movie"}]

    def preview(self, *_args, **_kwargs) -> dict:
        return {"preview_id": "preview-1", "cloud_write": False, "plans": []}

    def auto_match(self, *_args, **_kwargs) -> dict:
        return dict(self.auto_result)

    def execute_preview(self, _owner: str, _preview_id: str) -> dict:
        return {"stats": {"moved": 1}}

    def preview_reference(self, _owner: str, _preview_id: str) -> str:
        return "钢铁侠"


class DirectoryScrapeApiTests(InitializedWebTestCase):
    def setUp(self):
        import app.routes.guangya_scrape_api as scrape_api

        self.service = _RouteService()
        self.manager = Mock()
        self.manager.start_operation.return_value = {
            "ok": True,
            "task_id": "task-1",
            "message": "目录刮削已启动",
        }
        self.service_patch = patch.object(
            scrape_api,
            "get_directory_scrape_service",
            return_value=self.service,
        )
        self.manager_patch = patch.object(
            scrape_api,
            "get_organize_manager",
            return_value=self.manager,
        )
        self.service_patch.start()
        self.manager_patch.start()
        self.client = TestClient(create_app(start_background=False))

    def tearDown(self):
        self.client.close()
        self.manager_patch.stop()
        self.service_patch.stop()

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _login(self) -> dict[str, str]:
        page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(page.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/guangya")
        return {"X-CSRF-Token": self._csrf(page.text)}

    def test_inspect_requires_login(self):
        response = self.client.post(
            "/api/guangya/directory-scrape/inspect",
            json={"directory_id": "source"},
        )
        self.assertEqual(response.status_code, 401)

    def test_inspect_requires_csrf_after_login(self):
        self._login()
        response = self.client.post(
            "/api/guangya/directory-scrape/inspect",
            json={"directory_id": "source"},
        )
        self.assertEqual(response.status_code, 403)

    def test_authenticated_inspect_returns_server_result(self):
        headers = self._login()
        response = self.client.post(
            "/api/guangya/directory-scrape/inspect",
            json={"directory_id": "source", "directory_name": "不可信名称"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["directory"]["name"], "钢铁侠")

    def test_inspect_file_routes_to_file_scope(self):
        headers = self._login()
        with patch.object(
            self.service,
            "inspect_file",
            wraps=self.service.inspect_file,
        ) as inspect_file:
            response = self.client.post(
                "/api/guangya/directory-scrape/inspect",
                json={"file_id": "supergirl"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selected_file_id"], "supergirl")
        owner, file_id = inspect_file.call_args.args
        self.assertTrue(owner)
        self.assertEqual(file_id, "supergirl")

    def test_preview_validates_and_forwards_episode_overrides(self):
        headers = self._login()
        with patch.object(self.service, "preview", wraps=self.service.preview) as preview:
            valid = self.client.post(
                "/api/guangya/directory-scrape/preview",
                json={
                    "inspection_id": "file-inspection-1",
                    "tmdb_id": "255358",
                    "media_type": "tv",
                    "season": 0,
                    "episode": 3,
                },
                headers=headers,
            )
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(preview.call_args.kwargs, {"season": 0, "episode": 3})

        invalid_payloads = (
            {"season": -1, "episode": 3},
            {"season": 100, "episode": 3},
            {"season": 1, "episode": 0},
            {"season": 1, "episode": 1000},
            {"season": True, "episode": 3},
            {"season": 1, "episode": "3"},
        )
        for values in invalid_payloads:
            with self.subTest(values=values):
                response = self.client.post(
                    "/api/guangya/directory-scrape/preview",
                    json={
                        "inspection_id": "file-inspection-1",
                        "tmdb_id": "255358",
                        "media_type": "tv",
                        **values,
                    },
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400, response.text)

    def test_inspect_requires_exactly_one_scope_key_by_presence(self):
        headers = self._login()
        payloads = (
            {},
            {"directory_id": "source", "file_id": "supergirl"},
            {"directory_id": "source", "file_id": "   "},
            {"directory_id": "   ", "file_id": "supergirl"},
            {"directory_id": "source", "file_id": None},
            {"directory_id": None, "file_id": "supergirl"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/guangya/directory-scrape/inspect",
                    json=payload,
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400, response.text)

    def test_inspect_rejects_blank_single_scope_id(self):
        headers = self._login()
        for payload in ({"directory_id": "   "}, {"file_id": "\t"}):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/guangya/directory-scrape/inspect",
                    json=payload,
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400, response.text)

    def test_inspect_rejects_non_string_single_scope_id(self):
        headers = self._login()
        for field in ("directory_id", "file_id"):
            for value in (True, ["source"], {"id": "source"}):
                with self.subTest(field=field, value=value):
                    response = self.client.post(
                        "/api/guangya/directory-scrape/inspect",
                        json={field: value},
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 400, response.text)

    def test_manual_run_starts_shared_organize_operation(self):
        headers = self._login()
        response = self.client.post(
            "/api/guangya/directory-scrape/run",
            json={"mode": "manual", "preview_id": "preview-1"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["task_id"], "task-1")
        self.manager.start_operation.assert_called_once()
        operation, reference, callback = self.manager.start_operation.call_args.args
        self.assertEqual((operation, reference), ("目录刮削", "钢铁侠"))
        self.assertEqual(callback(), {"stats": {"moved": 1}})
        kwargs = self.manager.start_operation.call_args.kwargs
        self.assertTrue(kwargs["queue_if_busy"])
        self.assertTrue(kwargs["dedupe_key"].startswith("directory-scrape:"))
        self.assertTrue(kwargs["dedupe_key"].endswith(":preview-1"))

    def test_auto_low_confidence_returns_manual_payload_without_task(self):
        headers = self._login()
        response = self.client.post(
            "/api/guangya/directory-scrape/run",
            json={"mode": "auto", "inspection_id": "inspection-1"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "requires_manual")
        self.assertEqual(response.json()["suggested_query"], "钢铁侠")
        self.manager.start_operation.assert_not_called()

    def test_existing_organize_task_returns_conflict(self):
        self.manager.start_operation.return_value = {
            "ok": False,
            "error": "网盘整理任务正在运行",
        }
        headers = self._login()
        response = self.client.post(
            "/api/guangya/directory-scrape/run",
            json={"mode": "manual", "preview_id": "preview-1"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("正在运行", response.json()["error"])


class DirectoryScrapeSecretBoundaryTests(IsolatedDatabaseTestCase):
    SECRET = "opaque-provider-secret"

    def test_runtime_provider_errors_are_opaque_for_inspect_preview_and_run_http_and_logs(self):
        import app.routes.guangya_scrape_api as scrape_api

        service = _RouteService()
        manager = Mock()
        manager.start_operation.return_value = {
            "ok": True, "task_id": "task-1", "message": "目录刮削已启动",
        }
        with patch.object(
            scrape_api, "get_directory_scrape_service", return_value=service
        ), patch.object(
            scrape_api, "get_organize_manager", return_value=manager
        ), TestClient(create_app(start_background=False)) as client:
            page = client.get("/login")
            csrf = DirectoryScrapeApiTests._csrf(page.text)
            username, password = web_credentials()
            login = client.post(
                "/login",
                data={
                    "csrf_token": csrf,
                    "username": username,
                    "password": password,
                },
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            page = client.get("/guangya")
            headers = {"X-CSRF-Token": DirectoryScrapeApiTests._csrf(page.text)}

            cases = (
                ("inspect", "/api/guangya/directory-scrape/inspect", {"directory_id": "source"}),
                ("preview", "/api/guangya/directory-scrape/preview", {
                    "inspection_id": "inspection-1", "tmdb_id": "1", "media_type": "movie",
                }),
                ("preview_reference", "/api/guangya/directory-scrape/run", {
                    "mode": "manual", "preview_id": "preview-1",
                }),
            )
            for method_name, endpoint, payload in cases:
                with self.subTest(endpoint=endpoint), patch.object(
                    service, method_name, side_effect=RuntimeError(self.SECRET)
                ), self.assertLogs("app.routes.guangya_scrape_api", level="ERROR") as captured:
                    response = client.post(endpoint, json=payload, headers=headers)

                serialized = response.text + "\n" + "\n".join(captured.output)
                self.assertNotIn(self.SECRET, serialized)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(response.json()["error"], "目录刮削请求失败")
                self.assertIn("RuntimeError", serialized)

    def test_preview_http_target_scan_failure_is_safe_and_fail_closed(self):
        from app.modules.directory_scrape import (
            DirectoryScrapeService,
            DirectoryScrapeStore,
        )
        import app.routes.guangya_scrape_api as scrape_api

        video = _file("video", "Iron.Man.2008.mkv", "source")

        class TargetScanFailureClient(_TreeClient):
            def __init__(self):
                super().__init__(
                    {"source": [video], "archive": []},
                    {
                        "source": _dir("source", "钢铁侠"),
                        "archive": _dir("archive", "媒体库"),
                        "video": video,
                    },
                )
                self.archive_reads = 0

            def list_dir(self, directory_id):
                if directory_id == "archive":
                    self.archive_reads += 1
                    if self.archive_reads == 1:
                        raise RuntimeError(DirectoryScrapeSecretBoundaryTests.SECRET)
                return super().list_dir(directory_id)

        client = TargetScanFailureClient()
        rules = OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, clean_empty=False, link_strm=False,
            notify_enabled=False,
        )
        store = DirectoryScrapeStore()
        service = DirectoryScrapeService(
            client=client, scraper=_DirectoryScrapeTMDB(),
            store=store, rules_loader=lambda: rules,
        )
        inspection = service.inspect("placeholder", "source")
        inspection_id = inspection["inspection_id"]
        record = store._inspections.pop(inspection_id)

        class OwnerBindingService:
            def preview(self, owner, incoming_inspection_id, tmdb_id, media_type):
                record.owner = owner
                store._inspections[incoming_inspection_id] = record
                return service.preview(owner, incoming_inspection_id, tmdb_id, media_type)

        with patch.object(
            scrape_api, "get_directory_scrape_service", return_value=OwnerBindingService()
        ), TestClient(create_app(start_background=False)) as api_client:
            page = api_client.get("/login")
            username, password = web_credentials()
            login = api_client.post(
                "/login",
                data={
                    "csrf_token": DirectoryScrapeApiTests._csrf(page.text),
                    "username": username, "password": password,
                },
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            page = api_client.get("/guangya")
            headers = {"X-CSRF-Token": DirectoryScrapeApiTests._csrf(page.text)}
            with self.assertLogs("app.modules.organize", level="ERROR") as captured:
                response = api_client.post(
                    "/api/guangya/directory-scrape/preview",
                    json={
                        "inspection_id": inspection_id,
                        "tmdb_id": "1726", "media_type": "movie",
                    },
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        serialized = response.text + "\n" + "\n".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertTrue(payload["plans"])
        self.assertTrue(all(plan["action"] == "conflict" for plan in payload["plans"]))
        self.assertTrue(all(
            plan["note"] == "目标版本扫描失败，禁止替换"
            for plan in payload["plans"]
        ))
        self.assertIn("RuntimeError", serialized)

    def test_inspect_no_supported_video_returns_safe_http_400(self):
        from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore
        import app.routes.guangya_scrape_api as scrape_api

        service = DirectoryScrapeService(
            client=_TreeClient(
                {"source": [_file("note", "README.txt", "source")], "archive": []},
                {"source": _dir("source", "空目录"), "archive": _dir("archive", "媒体库")},
            ),
            scraper=_DirectoryScrapeTMDB(),
            store=DirectoryScrapeStore(),
            rules_loader=lambda: OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )
        with patch.object(
            scrape_api, "get_directory_scrape_service", return_value=service
        ), TestClient(create_app(start_background=False)) as api_client:
            page = api_client.get("/login")
            username, password = web_credentials()
            login = api_client.post(
                "/login",
                data={
                    "csrf_token": DirectoryScrapeApiTests._csrf(page.text),
                    "username": username, "password": password,
                },
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            page = api_client.get("/guangya")
            response = api_client.post(
                "/api/guangya/directory-scrape/inspect",
                json={"directory_id": "source"},
                headers={"X-CSRF-Token": DirectoryScrapeApiTests._csrf(page.text)},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"], "所选目录中没有支持的视频文件")

    def test_explicit_directory_scrape_conflict_remains_user_visible(self):
        import app.modules.directory_scrape as directory_scrape
        import app.routes.guangya_scrape_api as scrape_api

        conflict_type = getattr(directory_scrape, "DirectoryScrapeConflictError", None)
        self.assertIsNotNone(conflict_type)
        response = scrape_api._error(conflict_type("目录内容已变化，请重新检查"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            json.loads(response.body)["error"],
            "目录内容已变化，请重新检查",
        )

    def test_organizer_scan_errors_and_complete_scan_exception_hide_provider_text(self):
        from app.modules.organize import Organizer

        client = Mock()
        client.list_dir.side_effect = RuntimeError(self.SECRET)
        organizer = Organizer(client=client, scraper=Mock())
        rules = OrganizeRules(target_dir_id="archive", notify_enabled=False)

        with self.assertLogs("app.modules.organize", level="ERROR") as captured:
            _plans, stats = organizer.organize("source", rules, dry_run=True)
        serialized = json.dumps(stats, ensure_ascii=False) + "\n" + "\n".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(stats["scan_errors"], ["source: 目录读取失败"])
        self.assertIn("RuntimeError", serialized)

        with self.assertLogs("app.modules.organize", level="ERROR") as captured:
            with self.assertRaises(RuntimeError) as raised:
                organizer.organize(
                    "source", rules, dry_run=False, require_complete_scan=True
                )
        serialized = str(raised.exception) + "\n" + "\n".join(captured.output)
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(
            str(raised.exception),
            "目录扫描不完整，已在首次云盘写入前终止，请稍后重试",
        )

    def test_preview_reuses_inspection_without_second_provider_scan(self):
        from app.modules.directory_media import DirectoryInspection
        from app.modules.directory_scrape import (
            DirectoryScrapeService,
            DirectoryScrapeStore,
        )

        client = Mock()
        client.list_dir.side_effect = RuntimeError(self.SECRET)
        client.file_info.return_value = _dir("archive", "媒体库")
        scraper = Mock()
        scraper.match_from_tmdb.return_value = MatchResult(
            tmdb_id="1", title="测试电影", year="2026", media_type="movie"
        )
        scraper.get_detail_with_credits.return_value = {
            "id": 1, "title": "测试电影", "release_date": "2026-01-01"
        }
        rules = OrganizeRules(
            target_dir_id="archive",
            small_file_mb=0,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
        )
        inspection = DirectoryInspection(
            directory_id="source",
            directory_name="测试目录",
            media_type="movie",
            suggested_query="测试电影",
            videos=(),
            companions=(),
            counts={"video": 0, "subtitle": 0, "metadata": 0},
            mixed=False,
            fingerprint="stable",
        )
        store = DirectoryScrapeStore()
        inspection_id = store.put_inspection(
            "owner", inspection, rules, scope_type="directory", scope_id="source"
        )
        service = DirectoryScrapeService(
            client=client,
            scraper=scraper,
            store=store,
            rules_loader=lambda: rules,
        )

        with patch.object(service, "_inspect_scope", return_value=inspection):
            preview = service.preview("owner", inspection_id, "1", "movie")

        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(preview["stats"]["scan_errors"], [])
        client.list_dir.assert_not_called()

    def test_execute_task_status_and_logs_hide_unexpected_callback_error(self):
        from app.modules.organize_tasks import OrganizeTaskManager

        manager = OrganizeTaskManager()
        with self.assertLogs("app.modules.organize_tasks", level="ERROR") as captured:
            started = manager.start_operation(
                "目录刮削",
                "测试目录",
                lambda: (_ for _ in ()).throw(RuntimeError(self.SECRET)),
            )
            self.assertTrue(started["ok"])
            deadline = time.monotonic() + 2
            status = manager.task_status()
            while status["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
                status = manager.task_status()

        serialized = json.dumps(status, ensure_ascii=False) + "\n" + "\n".join(captured.output)
        self.assertEqual(status["status"], "failed")
        self.assertNotIn(self.SECRET, serialized)
        self.assertEqual(status["error"], "目录刮削失败，请稍后重试")
        self.assertNotIn("result", status)
        self.assertIn("RuntimeError", serialized)


if __name__ == "__main__":
    unittest.main()
