"""发布格式教学在光鸭目录检查阶段的父目录上下文回归。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.clients.tmdb import TMDBClient

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.directory_media import DirectoryMediaInspector
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.organize import OrganizeRules
from app.modules.recognition import formats
from app.modules.scraper import TMDBScraper
from tests.support import IsolatedDatabaseTestCase


TEMPLATE = "[Example-Team][{title}][track{episode}r{version}][{resolution}].mkv"
ROOT_NAME = "Teaching"


def filename(episode: int, title: str = "星海航行", version: int = 2) -> str:
    return f"[Example-Team][{title}][track{episode:03d}r{version}][1080p].mkv"


def _dir(file_id: str, name: str, parent_id: str = "0") -> GuangYaFile:
    return GuangYaFile(
        file_id, name, True, 0, f"etag-{file_id}", parent_id, updated_at=1,
    )


def _file(file_id: str, name: str, parent_id: str) -> GuangYaFile:
    return GuangYaFile(
        file_id, name, False, 1024 * 1024 * 1024,
        f"etag-{file_id}", parent_id,
    )


class _TreeClient:
    def __init__(self, tree: dict[str, list[GuangYaFile]], infos: dict[str, GuangYaFile]):
        self.tree = tree
        self.infos = infos

    def list_dir(self, directory_id: str) -> list[GuangYaFile]:
        return list(self.tree.get(directory_id, []))

    def file_info(self, file_id: str) -> GuangYaFile | None:
        return self.infos.get(file_id)


class _RecordingScraper:
    def __init__(self) -> None:
        self.inner = TMDBScraper(cast(TMDBClient, "offline-fixture"))
        self.parse_calls: list[tuple[str, str]] = []

    def close(self) -> None:
        self.inner.close()

    def parse_media(self, filename: str, parent_path: str = "", match=None, *, filename_only=False, is_directory=False):
        self.parse_calls.append((filename, parent_path))
        return self.inner.parse_media(filename, parent_path, match, filename_only=filename_only, is_directory=is_directory)


def teaching_rule(parent_path: str) -> dict:
    return {
        "draft": {
            "name": "光鸭目录发布格式",
            "template": TEMPLATE,
            "scope": "directory",
            "parent_path": parent_path,
        },
        "examples": [
            {
                "filename": filename(13),
                "title": "星海航行",
                "episode": 13,
            },
            {
                "filename": filename(14),
                "title": "星海航行",
                "episode": 14,
            },
        ],
        "filenames": [filename(15)],
    }


def save_teaching_rule(parent_path: str) -> dict:
    value = teaching_rule(parent_path)
    preview = formats.preview(value)
    if not preview["can_save"]:
        raise AssertionError(preview)
    item, created = formats.save({
        **value,
        "confirmed": True,
        "preview_token": preview["preview_token"],
    })
    if not created:
        raise AssertionError("fixture must create a fresh rule")
    return item


class ReleaseFormatDirectoryInspectionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()

    def test_conflicting_taught_numbers_cannot_be_hidden_by_directory_identity_probe(self):
        save_teaching_rule(ROOT_NAME)
        episode = _file("episode-15", filename(15, "Show [03]"), "source")
        client = _TreeClient({"source": [episode], "archive": []}, {
            "source": _dir("source", ROOT_NAME), "episode-15": episode,
            "archive": _dir("archive", "媒体库"),
        })
        scraper = TMDBScraper(cast(TMDBClient, "offline-fixture"))
        self.addCleanup(scraper.close)
        inspector = DirectoryMediaInspector(client=cast(GuangYaClient, client), scraper=scraper)
        rules = OrganizeRules(target_dir_id="archive", small_file_mb=0)
        with self.assertRaisesRegex(DirectoryScrapeConflictError, "发布格式存在冲突"):
            inspector.inspect("source", rules)
        with self.assertRaisesRegex(DirectoryScrapeConflictError, "发布格式存在冲突"):
            inspector.inspect_file("episode-15", rules)

    def test_filename_evidence_does_not_inherit_folder_fields_without_teaching(self):
        scraper = TMDBScraper(cast(TMDBClient, "offline-fixture"))
        self.addCleanup(scraper.close)
        for name, parent in (("04.mkv", "Show Season 2"), ("Movie.2020.mkv", "Other {tmdb-123}"),
                             ("Show - 03.mkv", "Show 2nd Attack")):
            with self.subTest(filename=name):
                before = scraper.parse_media(name)
                after = scraper.parse_media(name, parent, filename_only=True)
                for key in ("title", "year", "media_type", "tmdb_id", "source_season", "source_episode",
                            "effective_season", "effective_episode", "preprocess_rules", "context"):
                    self.assertEqual(getattr(before, key), getattr(after, key), key)

    def test_directory_scope_is_applied_to_root_files_with_shared_parent_context(self):
        save_teaching_rule(ROOT_NAME)
        episode = _file("episode-15", filename(15), "source")
        tree = {"source": [episode], "archive": []}
        infos = {
            "source": _dir("source", ROOT_NAME),
            "episode-15": episode,
            "archive": _dir("archive", "媒体库"),
        }
        scraper = _RecordingScraper()
        self.addCleanup(scraper.close)

        inspection = DirectoryMediaInspector(
            client=cast(GuangYaClient, _TreeClient(tree, infos)), scraper=cast(TMDBScraper, scraper),
        ).inspect(
            "source",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual((inspection.videos[0].season, inspection.videos[0].episode), (None, 15))
        self.assertIn((filename(15), ROOT_NAME), scraper.parse_calls)

    def test_nested_directory_uses_relative_context_and_does_not_leak_to_other_root(self):
        save_teaching_rule(f"{ROOT_NAME}/Season 02")
        nested = _file("nested-15", filename(15), "season-02")
        other = _file("other-15", filename(15), "other")
        tree = {
            "source": [_dir("season-02", "Season 02", "source")],
            "season-02": [nested],
            "other": [other],
            "archive": [],
        }
        infos = {
            "source": _dir("source", ROOT_NAME),
            "season-02": _dir("season-02", "Season 02", "source"),
            "nested-15": nested,
            "other": _dir("other", "Other"),
            "other-15": other,
            "archive": _dir("archive", "媒体库"),
        }
        scraper = _RecordingScraper()
        self.addCleanup(scraper.close)
        inspector = DirectoryMediaInspector(
            client=cast(GuangYaClient, _TreeClient(tree, infos)), scraper=cast(TMDBScraper, scraper),
        )
        rules = OrganizeRules(target_dir_id="archive", small_file_mb=0)

        nested_inspection = inspector.inspect("source", rules)
        other_inspection = inspector.inspect("other", rules)

        self.assertEqual(nested_inspection.media_type, "tv")
        self.assertEqual(nested_inspection.videos[0].episode, 15)
        self.assertIn((filename(15), f"{ROOT_NAME}/Season 02"), scraper.parse_calls)
        self.assertEqual(other_inspection.media_type, "movie")
        self.assertIsNone(other_inspection.videos[0].episode)

    def test_file_inspection_passes_direct_parent_context_to_shared_core(self):
        save_teaching_rule(ROOT_NAME)
        episode = _file("episode-15", filename(15), "source")
        tree = {"source": [episode], "archive": []}
        infos = {
            "source": _dir("source", ROOT_NAME),
            "episode-15": episode,
            "archive": _dir("archive", "媒体库"),
        }
        scraper = _RecordingScraper()
        self.addCleanup(scraper.close)

        inspection = DirectoryMediaInspector(
            client=cast(GuangYaClient, _TreeClient(tree, infos)), scraper=cast(TMDBScraper, scraper),
        ).inspect_file(
            "episode-15",
            OrganizeRules(target_dir_id="archive", small_file_mb=0),
        )

        self.assertEqual(inspection.media_type, "tv")
        self.assertEqual((inspection.season, inspection.episode), (1, 15))
        self.assertEqual(inspection.suggested_query, "星海航行")
        self.assertIn((filename(15), ROOT_NAME), scraper.parse_calls)
