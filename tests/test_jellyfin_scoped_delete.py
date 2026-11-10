"""Jellyfin 范围删除集成回归：临时 STRM/index/审计，状态化云盘，禁止网络。"""

from __future__ import annotations

from typing import Any

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app import config
from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import jellyfin_delete as deletion
from app.modules import organize_rules, strm
from app.modules.directory_scrape_errors import DirectoryScrapePublicError
from app.repositories.strm import list_strm_path_owners
from tests import test_jellyfin_delete as legacy
from tests.support import IsolatedDatabaseTestCase


class _GuardedDirectoryConflict(RuntimeError):
    provider_write_not_started = True


class _Cloud:
    """每次增删改都更新父目录版本；宽泛 delete 绝不接受目录。"""

    def __init__(self, case, fixture):
        self.case, self.fixture = case, fixture
        self.nodes, self.listings = fixture.nodes, fixture.listings
        self.events = []
        self.after_delete = None
        self.after_list = None
        self.before_empty_delete = None
        self.provider = Mock(
            spec=(
                "logged_in",
                "file_info",
                "list_dir",
                "delete",
                "delete_empty_directory",
                "supports_guarded_empty_directory_delete",
                "supports_atomic_empty_directory_delete",
            )
        )
        self.provider.logged_in = True
        self.provider.supports_guarded_empty_directory_delete = True
        self.provider.supports_atomic_empty_directory_delete = True
        self.provider.file_info.side_effect = self.info
        self.provider.list_dir.side_effect = self.list_dir
        self.provider.delete.side_effect = self.delete
        self.provider.delete_empty_directory.side_effect = self.delete_empty_directory
        fixture.provider = self.provider
        fixture.factory.return_value = self.provider
        for item in tuple(self.nodes.values()):
            if item.is_dir:
                fixture.store_node(
                    replace(item, etag=f"directory-{item.file_id}-1", updated_at=1)
                )

    def info(self, file_id):
        item = self.nodes.get(file_id)
        return replace(item) if item is not None else None

    def list_dir(self, parent_id):
        snapshot = [replace(item) for item in self.listings.get(parent_id, ())]
        if self.after_list is not None:
            self.after_list(parent_id)
        return snapshot

    def bump(self, file_id):
        item = self.nodes.get(file_id)
        if item is not None and item.is_dir:
            version = item.updated_at + 1
            self.fixture.store_node(
                replace(
                    item,
                    etag=f"directory-{file_id}-{version}",
                    updated_at=version,
                )
            )

    def put(self, item):
        old = self.nodes.get(item.file_id)
        self.fixture.store_node(item)
        for parent_id in {item.parent_id, old.parent_id if old else item.parent_id}:
            self.bump(parent_id)
        return item

    def remove(self, file_id):
        item = self.nodes.pop(file_id)
        self.listings[item.parent_id][:] = [
            child for child in self.listings[item.parent_id] if child.file_id != file_id
        ]
        self.listings.pop(file_id, None)
        self.bump(item.parent_id)

    def delete(self, file_ids):
        self.case.assertEqual(len(file_ids), 1, "范围删除必须逐文件回收、逐项审计")
        file_id = file_ids[0]
        self.case.assertIn(file_id, self.nodes)
        self.case.assertFalse(
            self.nodes[file_id].is_dir, "禁止用宽泛 provider.delete 递归删除目录"
        )
        pending = [
            row
            for row in self.fixture.audits()
            if row["file_id"] == file_id and row["status"] == "pending"
        ]
        self.case.assertEqual(len(pending), 1, "云端写入前必须已有真实 pending 审计")
        self.events.append(("file", file_id))
        self.remove(file_id)
        if self.after_delete is not None:
            self.after_delete(file_id)

    def delete_empty_directory(
        self, file_id, *, expected_etag="", expected_updated_at=0
    ):
        self.case.assertTrue(
            expected_etag or expected_updated_at, "空目录删除必须携带版本凭据"
        )
        pending = [
            row
            for row in self.fixture.audits()
            if row["file_id"] == file_id and row["status"] == "pending"
        ]
        self.case.assertEqual(len(pending), 1, "空目录回收前必须已有真实 pending 审计")
        if self.before_empty_delete is not None:
            self.before_empty_delete(file_id)
        # 模拟真实 provider 的版本检查与双重空目录复核，不让测试桩掩盖递归删除。
        for _ in range(2):
            current = self.provider.file_info(file_id)
            if (
                current is None
                or not current.is_dir
                or (expected_etag and current.etag != expected_etag)
                or (expected_updated_at and current.updated_at != expected_updated_at)
            ):
                raise _GuardedDirectoryConflict("目录版本变化，已保留")
            if self.provider.list_dir(file_id):
                raise _GuardedDirectoryConflict("目录非空，已保留")
        self.events.append(("directory", file_id))
        self.remove(file_id)
        return True

    @property
    def deleted_files(self):
        return {file_id for kind, file_id in self.events if kind == "file"}

    @property
    def deleted_directories(self):
        return [file_id for kind, file_id in self.events if kind == "directory"]


class JellyfinScopedDeleteTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        # 组合旧类的 fixture，不继承其 test_*，避免重复执行全部旧测试。
        self.fx = legacy.JellyfinDeleteTests(methodName="runTest")
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.enterContext(patch.object(organize_rules, "get", side_effect=config.get))
        self.reset_cloud()

    def reset_case(self):
        self.fx.values.clear()
        self.fx.reset_case()
        self.reset_cloud()

    def reset_cloud(self):
        self.fx.values.update(
            {
                "GY_ORGANIZE_SOURCE_DIRS": "[]",
                "GY_ORGANIZE_TARGET_DIR": "0",
                "GY_ORGANIZE_VIDEO_EXTS": "",
            }
        )
        self.cloud = _Cloud(self, self.fx)

    def directory(self, file_id, name, parent_id="source-a"):
        self.cloud.put(
            GuangYaFile(
                file_id,
                name,
                True,
                etag=f"directory-{file_id}-1",
                parent_id=parent_id,
                updated_at=1,
            )
        )
        return file_id

    def file(self, file_id, name, parent_id, *, size=100):
        self.cloud.put(
            GuangYaFile(file_id, name, False, size, f"etag-{file_id}", parent_id)
        )
        return file_id

    def video(self, file_id, name, parent_id, *, source_id="source-a"):
        self.file(file_id, name, parent_id, size=1024)
        plans, error = strm.configured_strm_source_plans()
        self.assertEqual(error, "")
        source = next(plan for plan in plans if plan["id"] == source_id)
        parts, current = [], parent_id
        while current != source_id:
            self.assertNotEqual(current, "0", "fixture 视频必须属于指定 STRM 来源")
            item = self.cloud.nodes[current]
            parts.append(item.name)
            current = item.parent_id
        relative_directory = "/".join(
            part for part in (source["rel_prefix"], *reversed(parts)) if part
        )
        target = strm._strm_target(
            self.cloud.nodes[file_id], relative_directory, self.fx.values["STRM_ROOT"]
        )
        return self.fx.seed(
            file_id, source_id, target.relative_to(self.fx.media_root).as_posix()
        )

    def remote(self, path):
        return "/jellyfin/" + path.relative_to(self.fx.media_root).as_posix()

    def payload(self, kind, root, *paths, mode="mf") -> dict[str, Any]:
        payload = self.fx.payload(*paths, mode=mode)
        payload["scopes"] = [
            {
                "kind": kind,
                "root": self.remote(root) if isinstance(root, Path) else root,
                "paths": list(payload["paths"]),
            }
        ]
        return payload

    def movie(self):
        self.directory("movie", "Movie")
        path = self.video("movie-video", "Movie.mkv", "movie")
        return path, path.parent

    def show(self):
        self.directory("show", "Show")
        self.directory("season-1", "Season 01", "show")
        self.directory("season-2", "Season 02", "show")
        first = self.video("episode-1", "Show.S01E01.mkv", "season-1")
        second = self.video("episode-2", "Show.S01E02.mkv", "season-1")
        third = self.video("episode-3", "Show.S02E01.mkv", "season-2")
        return first, second, third

    def execute(self, payload):
        return deletion.execute_delete(payload, deletion.prepare_delete(payload))

    def execute_or_reject(self, payload, expected=None):
        try:
            return deletion.execute_delete(
                payload,
                deletion.prepare_delete(payload) if expected is None else expected,
            )
        except DirectoryScrapePublicError:
            return None

    def assert_no_write(self):
        self.cloud.provider.delete.assert_not_called()
        self.cloud.provider.delete_empty_directory.assert_not_called()
        self.fx.assert_no_write()

    def assert_rejected(self, payload, expected=None):
        result = self.execute_or_reject(payload, expected)
        if result is not None:
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["completed"], 0)
            self.assertEqual(result["items"], [])
            self.assertTrue(result["error"])
        self.assert_no_write()

    def assert_success(self, result, deleted_files, *, sources=("source-a",)):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["error"], "")
        self.assertEqual(self.cloud.deleted_files, set(deleted_files))
        self.assertEqual(result["completed"], len(result["items"]))
        successes = [row for row in self.fx.audits() if row["status"] == "success"]
        self.assertTrue(
            (set(deleted_files) | set(self.cloud.deleted_directories))
            <= {row["file_id"] for row in successes}
        )
        self.assertFalse(
            [row for row in self.fx.audits() if row["status"] in {"pending", "failed"}]
        )
        for row in successes:
            if row["file_id"] in deleted_files:
                self.assertEqual(row["trigger"], "jellyfin_delete")
        self.fx.assert_retired(*sources)
        self.fx.close.assert_called_once_with(self.cloud.provider)

    def test_explicit_folder_scope_recycles_all_indexed_members_and_companions(self):
        first, root = self.movie()
        second = self.video("second-part", "Movie.CD2.mkv", "movie")
        self.file("poster", "poster.jpg", "movie")
        result = self.execute(self.payload("folder", root, first, second))
        self.assert_success(result, {"movie-video", "second-part", "poster"})
        self.assertIn("movie", self.cloud.deleted_directories)

    def test_movie_multiple_paths_are_one_complete_directory_scope(self):
        first, root = self.movie()
        second = self.video("second-part", "Movie.CD2.mkv", "movie")
        result = self.execute(self.payload("movie", root, first, second))
        self.assert_success(result, {"movie-video", "second-part"})
        self.assertIn("movie", self.cloud.deleted_directories)

    def test_folder_scope_requires_every_video_and_rejects_duplicate_scope_members(self):
        first, root = self.movie()
        second = self.video("second-part", "Movie.CD2.mkv", "movie")
        self.assert_rejected(self.payload("folder", root, first))
        payload = self.payload("folder", root, first, second)
        payload["scopes"].append(dict(payload["scopes"][0]))
        self.assert_rejected(payload)

    def test_movie_dedicated_directory_recycles_every_file_and_removes_tree(self):
        for mode in ("mf", "manual"):
            with self.subTest(mode=mode):
                self.reset_case()
                path, root = self.movie()
                files = {"movie-video"}
                for file_id, name in (
                    ("nfo", "Movie.nfo"),
                    ("subtitle", "Movie.zh-CN.srt"),
                    ("poster", "poster.jpg"),
                    ("unknown", "unknown.X"),
                ):
                    files.add(self.file(file_id, name, "movie"))
                self.directory("extras", "Extras", "movie")
                self.directory("empty-child", "Empty", "movie")
                files.add(self.file("nested-unknown", "notes.X", "extras"))
                result = self.execute(self.payload("movie", root, path, mode=mode))
                self.assert_success(result, files)
                self.assertEqual(
                    set(self.cloud.deleted_directories),
                    {"extras", "empty-child", "movie"},
                )
                self.assertEqual(self.cloud.deleted_directories[-1], "movie")
                self.assertNotIn("movie", self.cloud.nodes)
                self.assertIn("source-a", self.cloud.nodes)
                # 只退役已删除对象的索引，本地文件仍由 Jellyfin 原生删除。
                self.assertTrue(path.exists())
                self.assertEqual(list_strm_path_owners(str(path)), [])

    def test_retirement_cancels_inflight_metadata_without_touching_other_jobs(self):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        local_nfo = root / "Movie.nfo"
        local_nfo.write_bytes(b"metadata")
        db.upsert_strm_index(
            "guangya-meta:source-a", "nfo", "etag-nfo", 100, "Movie.nfo",
            str(local_nfo), legacy.fingerprint(local_nfo.read_bytes()),
        )
        db.enqueue_strm_metadata_jobs([
            {"source_id": "source-a", "file_id": file_id, "filename": filename,
             "etag": "etag-" + file_id, "size": 100, "parent_id": "movie"}
            for file_id, filename in (("nfo", "Movie.nfo"), ("unrelated", "Other.nfo"))
        ])
        job = db.claim_due_strm_metadata_jobs(owner="test-worker", limit=1)[0]
        lease = {"expected_lease_generation": job["lease_generation"],
                 "expected_revision": job["revision"], "expected_owner": "test-worker"}
        self.assertTrue(db.strm_metadata_job_is_current(job["id"], **lease))
        result = self.execute(self.payload("movie", root, path))
        self.assert_success(result, {"movie-video", "nfo"})
        self.assertEqual(result["stats"]["strm_indexes_retired"], 2)
        self.assertEqual(result["stats"]["metadata_jobs_cancelled"], 1)
        self.assertEqual(list_strm_path_owners(str(local_nfo)), [])
        self.assertEqual(local_nfo.read_bytes(), b"metadata")
        self.assertFalse(db.strm_metadata_job_is_current(job["id"], **lease))
        self.assertEqual(db.settle_strm_metadata_job(job["id"], **lease), "stale")
        with db.get_conn() as conn:
            states = {row["file_id"]: row["status"] for row in conn.execute("SELECT * FROM strm_metadata_queue")}
        self.assertEqual(states, {"nfo": "cancelled", "unrelated": "queued"})

    def test_movie_mixed_directory_only_recycles_selected_stem_companions(self):
        path, root = self.movie()
        self.file("other-video", "Other.mp4", "movie", size=2048)
        selected = {"movie-video"}
        for file_id, name in (
            ("nfo", "Movie.nfo"),
            ("subtitle", "Movie.zh-CN.srt"),
            ("thumb", "Movie-thumb.jpg"),
        ):
            selected.add(self.file(file_id, name, "movie"))
        kept = {"other-video"}
        for file_id, name in (
            ("other-nfo", "Other.nfo"),
            ("poster", "poster.jpg"),
            ("prefix-collision", "Movie Extra.nfo"),
            ("unknown", "release.X"),
        ):
            kept.add(self.file(file_id, name, "movie"))
        result = self.execute(self.payload("movie", root, path))
        self.assert_success(result, selected)
        self.assertTrue(kept <= self.cloud.nodes.keys())
        self.assertIn("movie", self.cloud.nodes)
        self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_episode_recycles_only_own_nfo_language_subtitles_and_thumbnail(self):
        first, _, _ = self.show()
        selected = {"episode-1"}
        for file_id, name in (
            ("nfo", "Show.S01E01.nfo"),
            ("zh-subtitle", "Show.S01E01.zh-CN.srt"),
            ("forced-subtitle", "Show.S01E01.en.forced.ass"),
            ("thumb", "Show.S01E01-thumb.jpg"),
        ):
            selected.add(self.file(file_id, name, "season-1"))
        kept = {"episode-2", "episode-3", "season-1", "season-2", "show"}
        for file_id, name in (
            ("tvshow", "tvshow.nfo"),
            ("poster", "poster.jpg"),
            ("other-nfo", "Show.S01E02.nfo"),
            ("prefix-collision", "Show.S01E010.nfo"),
            ("unknown", "release.X"),
        ):
            kept.add(self.file(file_id, name, "season-1"))
        result = self.execute(self.payload("episode", None, first))
        self.assert_success(result, selected)
        self.assertTrue(kept <= self.cloud.nodes.keys())
        self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_season_complete_selection_recycles_tree_but_preserves_other_season_and_show(
        self,
    ):
        first, second, _ = self.show()
        self.directory("extras", "Extras", "season-1")
        bonus = self.video("bonus-video", "Bonus.mkv", "extras")
        files = {"episode-1", "episode-2", "bonus-video"}
        files.add(self.file("nested-extra", "release.X", "extras"))
        files.add(self.file("season-poster", "poster.jpg", "season-1"))
        self.file("tvshow", "tvshow.nfo", "show")
        self.file("show-poster", "poster.jpg", "show")
        result = self.execute(
            self.payload("season", first.parent, first, second, bonus)
        )
        self.assert_success(result, files)
        self.assertEqual(self.cloud.deleted_directories, ["extras", "season-1"])
        self.assertTrue(
            {"show", "season-2", "episode-3", "tvshow", "show-poster"}
            <= self.cloud.nodes.keys()
        )

    def test_series_complete_selection_recycles_nested_videos_and_all_metadata(self):
        first, second, third = self.show()
        self.directory("extras", "Extras", "season-2")
        self.directory("empty-child", "Empty", "show")
        bonus = self.video("bonus-video", "Bonus.mkv", "extras")
        files = {"episode-1", "episode-2", "episode-3", "bonus-video"}
        for file_id, name, parent in (
            ("tvshow", "tvshow.nfo", "show"),
            ("poster", "poster.jpg", "show"),
            ("unknown", "release.X", "show"),
            ("nested-extra", "notes.X", "extras"),
        ):
            files.add(self.file(file_id, name, parent))
        result = self.execute(
            self.payload("series", first.parent.parent, first, second, third, bonus)
        )
        self.assert_success(result, files)
        self.assertEqual(
            set(self.cloud.deleted_directories),
            {"extras", "empty-child", "season-1", "season-2", "show"},
        )
        self.assertLess(
            self.cloud.deleted_directories.index("extras"),
            self.cloud.deleted_directories.index("season-2"),
        )
        self.assertEqual(self.cloud.deleted_directories[-1], "show")
        self.assertIn("source-a", self.cloud.nodes)

    def scope_tree(self, kind):
        if kind == "movie":
            path, root = self.movie()
            return (path,), root, "movie"
        first, second, third = self.show()
        if kind == "season":
            return (first, second), first.parent, "season-1"
        return (first, second, third), first.parent.parent, "show"

    def reference(self, file_id, *, source_id="source-b"):
        plans, error = strm.configured_strm_source_plans()
        self.assertEqual(error, "")
        source = next(plan for plan in plans if plan["id"] == source_id)
        target = strm._strm_target(
            self.cloud.nodes[file_id],
            source["rel_prefix"] + "/External reference",
            self.fx.values["STRM_ROOT"],
        )
        return self.fx.seed(
            file_id, source_id, target.relative_to(self.fx.media_root).as_posix()
        )

    def deep_video(self):
        parent = "source-a"
        for index in range(16):
            parent = self.directory(
                f"deep-{index}", f"Directory {index}-" + "x" * 200, parent
            )
        path = self.video("deep-video", "Movie.mkv", parent)
        self.assertTrue(
            any(part.startswith("~path-") for part in path.parts),
            "必须触发真实路径预算折叠",
        )
        return path, parent

    def test_multiple_scopes_preflight_is_read_only_and_syncs_each_source_once(self):
        first, root = self.movie()
        self.file("nfo-a", "Movie.nfo", "movie")
        self.directory("movie-b", "Movie B", "source-b")
        second = self.video("video-b", "Movie B.mkv", "movie-b", source_id="source-b")
        self.file("nfo-b", "Movie B.nfo", "movie-b")
        payload = self.fx.payload(first, second)
        payload["scopes"] = [
            {"kind": "movie", "root": self.remote(root), "paths": [self.remote(first)]},
            {
                "kind": "movie",
                "root": self.remote(second.parent),
                "paths": [self.remote(second)],
            },
        ]
        expected = deletion.prepare_delete(payload)
        self.fx.factory.assert_not_called()
        self.assert_no_write()
        result = deletion.execute_delete(payload, expected)
        self.assert_success(
            result,
            {"movie-video", "video-b", "nfo-a", "nfo-b"},
            sources=("source-a", "source-b"),
        )
        self.assertEqual(set(self.cloud.deleted_directories), {"movie", "movie-b"})

    def test_unauthorized_scope_kinds_are_rejected_before_any_cloud_write(self):
        path, root = self.movie()
        for kind in ("file", "boxset", "music", "Movie", "", None, 1, [], {}):
            with self.subTest(kind=kind):
                payload = self.payload("movie", root, path)
                payload["scopes"][0]["kind"] = kind
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()
        self.fx.factory.assert_not_called()

    def test_malformed_scopes_and_members_do_not_fall_back_to_file_delete(self):
        path, root = self.movie()
        raw = self.remote(path)
        invalid_scopes = [
            None,
            [],
            {},
            "movie",
            [None],
            ["movie"],
            [{"kind": "movie", "root": self.remote(root)}],
        ]
        invalid_scopes += [
            [{"kind": "movie", "root": self.remote(root), "paths": members}]
            for members in (
                None,
                [],
                raw,
                [None],
                [raw, raw],
                ["/jellyfin/missing.strm"],
            )
        ]
        for scopes in invalid_scopes:
            with self.subTest(scopes=scopes):
                payload = self.fx.payload(path)
                payload["scopes"] = scopes
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()

    def test_scopes_must_partition_all_selected_paths_without_duplicates_or_extra_members(
        self,
    ):
        first, second, _ = self.show()
        root = self.remote(first.parent)
        for members in (
            [self.remote(first)],
            [self.remote(first), self.remote(second), "/jellyfin/extra.strm"],
        ):
            with self.subTest(members=members):
                payload = self.fx.payload(first, second)
                payload["scopes"] = [{"kind": "season", "root": root, "paths": members}]
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()
        payload = self.payload("season", first.parent, first, second)
        payload["scopes"].append(dict(payload["scopes"][0]))
        with self.assertRaises(DirectoryScrapePublicError):
            deletion.prepare_delete(payload)
        self.assert_no_write()

    def test_episode_scope_requires_literal_null_directory_authorization(self):
        first, _, _ = self.show()
        for root in (self.remote(first.parent), "", False, 0, [], {}):
            with self.subTest(root=root):
                payload = self.payload("episode", root, first)
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()

    def test_scope_root_outside_members_mapping_root_traversal_or_symlink_is_rejected(
        self,
    ):
        path, root = self.movie()
        alias = root.parent / "Alias"
        alias.symlink_to(root, target_is_directory=True)
        for candidate in (
            "/jellyfin",
            "/unmapped/Movie",
            "/jellyfin/Source a/Other",
            self.remote(root) + "/..",
            self.remote(path),
            self.remote(alias),
            1,
        ):
            with self.subTest(root=candidate):
                payload = self.payload("movie", candidate, path)
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()

    def test_inferred_source_root_organize_source_and_archive_root_are_never_deleted(
        self,
    ):
        for protected in ("source", "organize_source", "archive"):
            with self.subTest(protected=protected):
                self.reset_case()
                path, root = self.movie()
                self.file("unknown", "release.X", "movie")
                kind = "movie"
                if protected == "source":
                    root, kind = root.parent, "series"
                elif protected == "organize_source":
                    self.fx.values["GY_ORGANIZE_SOURCE_DIRS"] = json.dumps(
                        [{"id": "movie", "name": "Movie"}]
                    )
                else:
                    self.fx.values["GY_ORGANIZE_TARGET_DIR"] = "movie"
                self.assert_rejected(self.payload(kind, root, path))
                self.assertTrue(
                    {"source-a", "movie", "movie-video", "unknown"}
                    <= self.cloud.nodes.keys()
                )

    def test_directory_tree_containing_nested_configured_source_is_rejected(self):
        path, root = self.movie()
        self.directory("nested-source", "Nested Source", "movie")
        self.file("unknown", "release.X", "nested-source")
        sources = json.loads(self.fx.values["GY_STRM_SOURCE_DIRS"])
        self.fx.values["GY_STRM_SOURCE_DIRS"] = json.dumps(
            sources + [{"id": "nested-source", "name": "Nested Source"}]
        )
        self.assert_rejected(self.payload("movie", root, path))
        self.assertIn("nested-source", self.cloud.nodes)

    def test_season_and_series_reject_unselected_video_even_without_a_local_index(self):
        for kind in ("season", "series"):
            for indexed in (True, False):
                with self.subTest(kind=kind, indexed=indexed):
                    self.reset_case()
                    paths, root, root_id = self.scope_tree(kind)
                    skipped = "episode-2" if kind == "season" else "episode-3"
                    if not indexed:
                        with db.get_conn() as conn:
                            conn.execute(
                                "DELETE FROM strm_index WHERE file_id=?", (skipped,)
                            )
                    self.file("shared-poster", "poster.jpg", root_id)
                    self.assert_rejected(self.payload(kind, root, *paths[:-1]))
                    self.assertTrue(
                        {skipped, root_id, "shared-poster"} <= self.cloud.nodes.keys()
                    )

    def test_tree_selection_requires_each_strm_fingerprint_and_index(self):
        for kind in ("season", "series"):
            for change in (
                "missing-index",
                "missing-client-fingerprint",
                "missing-index-fingerprint",
                "changed-bytes",
            ):
                with self.subTest(kind=kind, change=change):
                    self.reset_case()
                    paths, root, _ = self.scope_tree(kind)
                    payload = self.payload(kind, root, *paths)
                    last = paths[-1]
                    if change == "missing-client-fingerprint":
                        payload["fingerprints"].pop(self.remote(last))
                    elif change == "changed-bytes":
                        last.write_bytes(b"changed by another process")
                    else:
                        with db.get_conn() as conn:
                            if change == "missing-index":
                                conn.execute(
                                    "DELETE FROM strm_index WHERE strm_path=?",
                                    (str(last),),
                                )
                            else:
                                conn.execute(
                                    "UPDATE strm_index SET content_fingerprint='' WHERE strm_path=?",
                                    (str(last),),
                                )
                    self.assert_rejected(payload)

    def test_unselected_cross_source_reference_protects_video_and_companion(self):
        for referenced in ("movie-video", "nfo"):
            with self.subTest(referenced=referenced):
                self.reset_case()
                path, root = self.movie()
                self.file("nfo", "Movie.nfo", "movie")
                alias = self.reference(referenced)
                self.assert_rejected(self.payload("movie", root, path))
                self.assertTrue(alias.exists())
                self.assertTrue(
                    {"movie-video", "nfo", "movie"} <= self.cloud.nodes.keys()
                )

    def test_same_stem_other_version_owns_its_companions_by_longest_complete_stem(self):
        for other_name, own_nfo, other_nfo in (
            ("Movie.extended.mkv", "Movie.nfo", "Movie.extended.nfo"),
            ("Movie.mp4", None, "Movie.nfo"),
        ):
            with self.subTest(other_name=other_name):
                self.reset_case()
                path, root = self.movie()
                self.file("other-video", other_name, "movie")
                self.file("other-nfo", other_nfo, "movie")
                selected = {"movie-video"}
                if own_nfo:
                    selected.add(self.file("own-nfo", own_nfo, "movie"))
                result = self.execute(self.payload("movie", root, path))
                self.assert_success(result, selected)
                self.assertTrue(
                    {"movie", "other-video", "other-nfo"} <= self.cloud.nodes.keys()
                )

    def test_prepared_scope_rechecks_local_bytes_index_mapping_and_authorization(self):
        for change in ("bytes", "index", "mapping", "kind", "root"):
            with self.subTest(change=change):
                self.reset_case()
                path, root = self.movie()
                payload = self.payload("movie", root, path)
                expected = deletion.prepare_delete(payload)
                if change == "bytes":
                    path.write_bytes(b"external replacement")
                elif change == "index":
                    with db.get_conn() as conn:
                        conn.execute("UPDATE strm_index SET etag='changed'")
                elif change == "mapping":
                    self.fx.values["JELLYFIN_PATH_MAPPINGS"] = json.dumps(
                        [
                            {"local": str(self.fx.media_root), "server": "/new-prefix"},
                        ]
                    )
                elif change == "kind":
                    payload["scopes"][0].update(kind="episode", root=None)
                else:
                    payload["scopes"][0]["root"] = None
                self.assert_rejected(payload, expected)

    def test_cloud_video_identity_changes_after_prepare_prevent_any_write(self):
        for field, value in (
            ("etag", "changed"),
            ("name", "Changed.mkv"),
            ("size", 2048),
            ("is_dir", True),
        ):
            with self.subTest(field=field):
                self.reset_case()
                path, root = self.movie()
                payload = self.payload("movie", root, path)
                expected = deletion.prepare_delete(payload)
                self.cloud.put(
                    replace(self.cloud.nodes["movie-video"], **{field: value})
                )
                self.assert_rejected(payload, expected)

    def test_unchanged_etag_moved_video_or_ancestor_cannot_rebind_scope_to_new_cloud_root(
        self,
    ):
        for kind in ("movie", "season", "series"):
            with self.subTest(kind=kind):
                self.reset_case()
                paths, root, root_id = self.scope_tree(kind)
                payload = self.payload(kind, root, *paths)
                expected = deletion.prepare_delete(payload)
                original_etags = {
                    target.file_id: self.cloud.nodes[target.file_id].etag
                    for target in expected
                }
                if kind == "series":
                    self.directory("other-parent", "Other Parent")
                    self.cloud.put(
                        replace(self.cloud.nodes["show"], parent_id="other-parent")
                    )
                else:
                    parent = "source-a" if kind == "movie" else "show"
                    self.directory(
                        "other-root",
                        "Other Movie" if kind == "movie" else "Season 99",
                        parent,
                    )
                    for target in expected:
                        self.cloud.put(
                            replace(
                                self.cloud.nodes[target.file_id], parent_id="other-root"
                            )
                        )
                    self.file(
                        "foreign-unknown", "belongs-to-other-scope.X", "other-root"
                    )
                self.assertEqual(
                    original_etags,
                    {
                        file_id: self.cloud.nodes[file_id].etag
                        for file_id in original_etags
                    },
                )
                self.assert_rejected(payload, expected)
                self.assertIn(root_id, self.cloud.nodes)
                if kind != "series":
                    self.assertIn("foreign-unknown", self.cloud.nodes)

    def test_folded_season_and_series_paths_cannot_authorize_inferred_cloud_tree(self):
        for kind in ("season", "series"):
            with self.subTest(kind=kind):
                self.reset_case()
                path, root_id = self.deep_video()
                self.file("unknown", "release.X", root_id)
                self.assert_rejected(self.payload(kind, path.parent, path))
                self.assertTrue(
                    {"deep-video", root_id, "unknown"} <= self.cloud.nodes.keys()
                )

    def test_movie_direct_parent_scope_can_clean_folded_ancestor_path(self):
        path, root_id = self.deep_video()
        self.file("unknown", "release.X", root_id)
        result = self.execute(self.payload("movie", path.parent, path))
        self.assert_success(result, {"deep-video", "unknown"})
        self.assertEqual(self.cloud.deleted_directories, [root_id])
        self.assertIn("deep-14", self.cloud.nodes)

    def test_new_cross_source_companion_reference_after_first_recycle_stops_remaining_writes(
        self,
    ):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        self.cloud.after_delete = lambda file_id: (
            self.reference("nfo") if file_id == "movie-video" else None
        )
        result = self.execute(self.payload("movie", root, path))
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.cloud.deleted_files, {"movie-video"})
        self.assertTrue({"movie", "nfo"} <= self.cloud.nodes.keys())
        self.cloud.provider.delete_empty_directory.assert_not_called()
        self.fx.assert_retired("source-a")

    def test_companion_changed_after_first_recycle_is_preserved_with_its_directory(
        self,
    ):
        for fields in (
            {"etag": "changed"},
            {"size": 200},
            {"name": "Other.nfo"},
            {"parent_id": "season-b"},
        ):
            with self.subTest(fields=fields):
                self.reset_case()
                path, root = self.movie()
                self.file("nfo", "Movie.nfo", "movie")

                def mutate(file_id, *, fields=fields):
                    if file_id == "movie-video":
                        self.cloud.put(replace(self.cloud.nodes["nfo"], **fields))

                self.cloud.after_delete = mutate
                result = self.execute(self.payload("movie", root, path))
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.cloud.deleted_files, {"movie-video"})
                self.assertTrue({"movie", "nfo"} <= self.cloud.nodes.keys())
                self.cloud.provider.delete_empty_directory.assert_not_called()
                self.fx.assert_retired("source-a")

    def test_new_cloud_video_and_unknown_file_after_first_recycle_are_preserved(self):
        for kind in ("movie", "season", "series"):
            with self.subTest(kind=kind):
                self.reset_case()
                paths, root, root_id = self.scope_tree(kind)
                self.file("shared-poster", "poster.jpg", root_id)
                self.file("shared-nfo", "tvshow.nfo", root_id)

                def add_new_contents(file_id, *, root_id=root_id):
                    self.cloud.after_delete = None
                    self.file("new-video", "New.mkv", root_id)
                    self.file("new-unknown", "new.X", root_id)

                self.cloud.after_delete = add_new_contents
                result = self.execute(self.payload(kind, root, *paths))
                self.assertFalse(result["ok"], result)
                self.assertTrue(
                    {root_id, "new-video", "new-unknown", "shared-poster", "shared-nfo"}
                    <= self.cloud.nodes.keys()
                )
                self.assertNotIn(root_id, self.cloud.deleted_directories)
                self.assertFalse(
                    {"new-video", "new-unknown"} & self.cloud.deleted_files
                )
                self.fx.assert_retired("source-a")

    def test_directory_move_or_rename_after_first_write_preserves_remaining_companion(
        self,
    ):
        for fields in ({"name": "Other Movie"}, {"parent_id": "source-b"}):
            with self.subTest(fields=fields):
                self.reset_case()
                path, root = self.movie()
                self.file("nfo", "Movie.nfo", "movie")

                def mutate(file_id, *, fields=fields):
                    if file_id == "movie-video":
                        self.cloud.put(replace(self.cloud.nodes["movie"], **fields))

                self.cloud.after_delete = mutate
                result = self.execute(self.payload("movie", root, path))
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.cloud.deleted_files, {"movie-video"})
                self.assertTrue({"movie", "nfo"} <= self.cloud.nodes.keys())
                self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_guarded_empty_directory_delete_rechecks_new_children_and_versions(self):
        for change in ("new-child", "version"):
            with self.subTest(change=change):
                self.reset_case()
                path, root = self.movie()

                def race(file_id, *, change=change):
                    self.cloud.before_empty_delete = None
                    if change == "new-child":
                        self.file("late-file", "late.X", file_id)
                    else:
                        self.cloud.bump(file_id)

                self.cloud.before_empty_delete = race
                result = self.execute(self.payload("movie", root, path))
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.cloud.deleted_files, {"movie-video"})
                self.assertEqual(self.cloud.deleted_directories, [])
                self.assertIn("movie", self.cloud.nodes)
                self.cloud.provider.delete_empty_directory.assert_called_once()
                if change == "new-child":
                    self.assertIn("late-file", self.cloud.nodes)
                self.assertTrue(
                    any(
                        row["file_id"] == "movie" and row["status"] == "blocked"
                        for row in self.fx.audits()
                    )
                )
                self.fx.assert_retired("source-a")

    def test_provider_without_guarded_cleanup_retains_empty_directory_not_recursive_delete(
        self,
    ):
        path, root = self.movie()
        self.cloud.provider.supports_guarded_empty_directory_delete = False
        result = self.execute(self.payload("movie", root, path))
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.cloud.deleted_files, {"movie-video"})
        self.assertIn("movie", self.cloud.nodes)
        self.cloud.provider.delete_empty_directory.assert_not_called()
        self.fx.assert_retired("source-a")

    def test_active_download_and_pending_organize_protect_scope_before_first_write(
        self,
    ):
        for status, organize in (
            ("downloading", ""),
            ("outcome_unknown", ""),
            ("completed", "pending"),
            ("completed", "running"),
        ):
            with self.subTest(status=status, organize=organize):
                self.reset_case()
                path, root = self.movie()
                self.file("unknown", "release.X", "movie")
                payload = self.payload("movie", root, path)
                expected = deletion.prepare_delete(payload)
                self.fx.request_download(
                    source_id="movie", status=status, organize_status=organize
                )
                self.assert_rejected(payload, expected)

    def test_download_protected_nested_directory_prevents_entire_batch_write(self):
        path, root = self.movie()
        self.directory("extras", "Extras", "movie")
        self.file("protected-file", "release.X", "extras")
        self.fx.request_download(source_id="extras")
        self.assert_rejected(self.payload("movie", root, path))
        self.assertIn("protected-file", self.cloud.nodes)

    def test_finished_staging_cleanup_or_unrelated_download_does_not_block_scope_cleanup(
        self,
    ):
        for status, source in (("completed", "movie"), ("downloading", "source-c")):
            with self.subTest(status=status, source=source):
                self.reset_case()
                path, root = self.movie()
                request_id = self.fx.request_download(
                    source_id=source, status=status, organize_status="completed"
                )
                if status == "completed":
                    db.update_download_request(
                        request_id, gy_staging_cleanup_status="completed"
                    )
                result = self.execute(self.payload("movie", root, path))
                self.assert_success(result, {"movie-video"})
                self.assertEqual(self.cloud.deleted_directories, ["movie"])

    def test_new_download_occupancy_after_first_recycle_blocks_companion_and_directory_cleanup(
        self,
    ):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        self.cloud.after_delete = lambda file_id: (
            self.fx.request_download(source_id="movie")
            if file_id == "movie-video"
            else None
        )
        result = self.execute(self.payload("movie", root, path))
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.cloud.deleted_files, {"movie-video"})
        self.assertTrue({"movie", "nfo"} <= self.cloud.nodes.keys())
        self.cloud.provider.delete_empty_directory.assert_not_called()
        self.fx.assert_retired("source-a")

    def test_companion_recycle_failure_records_partial_audit_and_does_not_remove_directory(
        self,
    ):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")

        def delete(file_ids):
            if file_ids == ["nfo"]:
                raise TimeoutError("secret-must-not-appear-in-result")
            return self.cloud.delete(file_ids)

        self.cloud.provider.delete.side_effect = delete
        result = self.execute(self.payload("movie", root, path))
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["completed"], 1)
        self.assertEqual([item["file_id"] for item in result["items"]], ["movie-video"])
        self.assertNotIn("secret-must-not-appear", json.dumps(result))
        self.assertEqual(
            [(row["file_id"], row["status"]) for row in self.fx.audits()],
            [("movie-video", "success"), ("nfo", "failed")],
        )
        self.assertTrue({"movie", "nfo"} <= self.cloud.nodes.keys())
        self.cloud.provider.delete_empty_directory.assert_not_called()
        self.fx.assert_retired("source-a")

    def test_scoped_api_still_accepts_writer_callback_without_cloud_work_at_202(self):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        payload = self.payload("movie", root, path)
        expected = deletion.prepare_delete(payload)
        client, headers = self.fx.web_client()
        response = client.post("/api/jellyfin/delete", json=payload, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["task_id"], "jellyfin-delete-task")
        self.fx.manager.start_operation.assert_called_once()
        args, kwargs = self.fx.manager.start_operation.call_args
        self.assertEqual(
            kwargs,
            {"queue_if_busy": False, "dedupe_key": deletion.delete_key(expected)},
        )
        self.fx.factory.assert_not_called()
        self.assert_no_write()
        self.assert_success(args[2](), {"movie-video", "nfo"})
        self.assertEqual(self.cloud.deleted_directories, ["movie"])

    def test_delete_capabilities_reports_scope_v1_only_to_authenticated_session(self):
        anonymous, _ = self.fx.web_client(login=False)
        response = anonymous.get(
            "/api/jellyfin/delete-capabilities", follow_redirects=False
        )
        self.assertEqual(response.status_code, 401, response.text)
        client, _ = self.fx.web_client()
        response = client.get("/api/jellyfin/delete-capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"scope_version": 1, "kinds": ["movie", "episode", "season", "series", "folder"]},
        )
        self.fx.manager.start_operation.assert_not_called()
        self.fx.factory.assert_not_called()
        self.assert_no_write()

    def test_finished_download_with_retained_staging_root_stays_protected(self):
        path, root = self.movie()
        self.fx.request_download(
            source_id="movie", status="completed", organize_status="completed"
        )
        self.assert_rejected(self.payload("movie", root, path))
        self.assertTrue({"movie", "movie-video"} <= self.cloud.nodes.keys())

    def test_configured_source_rel_prefix_not_cloud_source_display_name_binds_scopes(
        self,
    ):
        configurations = (
            [{"id": "source-a", "name": "Source a"}],
            [
                {"id": "source-a", "name": "Configured Label"},
                {"id": "source-b", "name": "Other Label"},
            ],
            [
                {"id": "source-a", "name": "Same Label"},
                {"id": "source-b", "name": "Same Label"},
            ],
        )
        for sources in configurations:
            with self.subTest(sources=sources):
                self.reset_case()
                self.fx.values["GY_STRM_SOURCE_DIRS"] = json.dumps(sources)
                path, root = self.movie()
                result = self.execute(self.payload("movie", root, path))
                self.assert_success(result, {"movie-video"})
                self.assertEqual(self.cloud.deleted_directories, ["movie"])

    def test_movie_unselected_nested_video_prevents_full_tree_cleanup(self):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        self.file("poster", "poster.jpg", "movie")
        self.directory("extras", "Extras", "movie")
        self.file("other-video", "Trailer.mkv", "extras")
        self.file("other-nfo", "Trailer.nfo", "extras")
        result = self.execute(self.payload("movie", root, path))
        self.assert_success(result, {"movie-video", "nfo"})
        self.assertTrue(
            {"movie", "extras", "other-video", "other-nfo", "poster"}
            <= self.cloud.nodes.keys()
        )
        self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_companion_without_cloud_etag_prevents_entire_batch_write(self):
        path, root = self.movie()
        self.file("nfo", "Movie.nfo", "movie")
        self.cloud.put(replace(self.cloud.nodes["nfo"], etag=""))
        self.assert_rejected(self.payload("movie", root, path))
        self.assertTrue({"movie", "movie-video", "nfo"} <= self.cloud.nodes.keys())

    def test_scope_roots_and_original_fingerprint_keys_support_windows_and_unc_mapping(
        self,
    ):
        for server, original_prefix in (
            ("D:/Media", "d:\\MEDIA"),
            ("//NAS/Media", "\\\\nas\\MEDIA"),
        ):
            with self.subTest(server=server):
                self.reset_case()
                path, root = self.movie()
                self.file("nfo", "Movie.nfo", "movie")
                relative = (
                    path.relative_to(self.fx.media_root).as_posix().replace("/", "\\")
                )
                root_relative = (
                    root.relative_to(self.fx.media_root).as_posix().replace("/", "\\")
                )
                raw = original_prefix + "\\" + relative
                raw_root = original_prefix + "\\" + root_relative
                mappings = [{"local": str(self.fx.media_root), "server": server}]
                self.fx.values["JELLYFIN_PATH_MAPPINGS"] = json.dumps(mappings)
                payload = {
                    "paths": [raw],
                    "fingerprints": {raw: legacy.fingerprint(path.read_bytes())},
                    "mapping_mode": "manual",
                    "mappings": mappings,
                    "scopes": [{"kind": "movie", "root": raw_root, "paths": [raw]}],
                }
                result = self.execute(payload)
                self.assert_success(result, {"movie-video", "nfo"})
                self.assertEqual(self.cloud.deleted_directories, ["movie"])

    def test_episode_null_scope_does_not_require_stale_local_parent_to_match_cloud_path(
        self,
    ):
        first, _, _ = self.show()
        self.file("nfo", "Show.S01E01.nfo", "season-1")
        payload = self.payload("episode", None, first)
        expected = deletion.prepare_delete(payload)
        self.directory("other-season", "Season 99", "show")
        # 文件 ID/etag/内容指纹均未改变；null scope 只授权明确视频和同名伴随。
        for file_id in ("episode-1", "nfo"):
            self.cloud.put(replace(self.cloud.nodes[file_id], parent_id="other-season"))
        self.file("poster", "poster.jpg", "other-season")
        self.file("tvshow", "tvshow.nfo", "other-season")
        result = deletion.execute_delete(payload, expected)
        self.assert_success(result, {"episode-1", "nfo"})
        self.assertTrue(
            {"show", "season-1", "episode-2", "other-season", "poster", "tvshow"}
            <= self.cloud.nodes.keys()
        )
        self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_tree_scope_checks_configured_video_extensions_not_just_indexed_mkv(self):
        for extensions in ("mxf", "mkv， .mxf"):
            with self.subTest(extensions=extensions):
                self.reset_case()
                first, second, _ = self.show()
                self.fx.values["GY_ORGANIZE_VIDEO_EXTS"] = extensions
                self.file("unindexed-video", "Unindexed.mxf", "season-1")
                self.assert_rejected(
                    self.payload("season", first.parent, first, second)
                )
                self.assertIn("unindexed-video", self.cloud.nodes)

    def test_series_new_unselected_episode_after_plan_prevents_every_write(self):
        self.directory("show", "Show")
        self.directory("season-1", "Season 01", "show")
        first = self.video("episode-1", "Show.S01E01.mkv", "season-1")
        self.file("poster", "poster.jpg", "show")
        self.file("tvshow", "tvshow.nfo", "show")
        mutations = []
        original = deletion._cloud_plan

        def plan_then_add_episode(*args, **kwargs):
            plan = original(*args, **kwargs)
            self.assert_no_write()
            self.file("new-episode", "Show.S01E02.mkv", "season-1")
            mutations.append("new-episode")
            return plan

        with patch.object(deletion, "_cloud_plan", side_effect=plan_then_add_episode):
            self.assert_rejected(self.payload("series", first.parent.parent, first))
        self.assertEqual(mutations, ["new-episode"], "必须在真实 plan 完成后触发竞态")
        self.assertTrue(
            {"episode-1", "new-episode", "poster", "tvshow", "show", "season-1"}
            <= self.cloud.nodes.keys()
        )

    def test_latest_longest_stem_claim_protects_episode_subtitle_after_plan_or_first_write(
        self,
    ):
        for timing in ("after-plan", "after-first-write"):
            with self.subTest(timing=timing):
                self.reset_case()
                first, _, _ = self.show()
                self.file("subtitle", "Show.S01E01.en.srt", "season-1")
                claims, mutations = [], []
                original = deletion._cloud_plan

                def add_longer_video(file_id=None, *, mutations=mutations):
                    self.cloud.after_delete = None
                    self.file("new-version", "Show.S01E01.en.mkv", "season-1")
                    mutations.append("new-version")

                def plan_then_change_ownership(
                    *args,
                    original=original,
                    timing=timing,
                    claims=claims,
                    add_video=add_longer_video,
                    **kwargs,
                ):
                    plan = original(*args, **kwargs)
                    self.assertIn(
                        "subtitle", plan[0], "竞态必须发生在 plan 已认领字幕之后"
                    )
                    claims.append("subtitle")
                    if timing == "after-plan":
                        add_video()
                    else:
                        self.cloud.after_delete = add_video
                    return plan

                with patch.object(
                    deletion, "_cloud_plan", side_effect=plan_then_change_ownership
                ):
                    self.execute_or_reject(self.payload("episode", None, first))
                self.assertEqual(claims, ["subtitle"])
                self.assertEqual(mutations, ["new-version"])
                self.assertTrue(
                    {"subtitle", "new-version", "season-1"} <= self.cloud.nodes.keys()
                )
                self.assertTrue(self.cloud.deleted_files <= {"episode-1"})
                self.assertFalse(
                    [
                        row
                        for row in self.fx.audits()
                        if row["file_id"] == "subtitle" and row["status"] == "success"
                    ]
                )
                self.cloud.provider.delete_empty_directory.assert_not_called()

    def test_selected_second_episode_changes_during_plan_before_first_write_rejects_batch(
        self,
    ):
        first, second, _ = self.show()
        mutations = []
        original = deletion._cloud_plan

        def change_after_snapshot(parent_id):
            if parent_id == "season-1":
                self.cloud.after_list = None
                self.cloud.put(
                    replace(self.cloud.nodes["episode-2"], etag="changed-during-plan")
                )
                mutations.append("episode-2")

        def plan_with_stale_listing(*args, **kwargs):
            self.cloud.after_list = change_after_snapshot
            try:
                return original(*args, **kwargs)
            finally:
                self.cloud.after_list = None

        with patch.object(deletion, "_cloud_plan", side_effect=plan_with_stale_listing):
            self.assert_rejected(self.payload("season", first.parent, first, second))
        self.assertEqual(
            mutations,
            ["episode-2"],
            "必须在 plan 扫描中改变第二集，而不是进入 Writer 前",
        )
        self.assertTrue(
            {"episode-1", "episode-2", "season-1"} <= self.cloud.nodes.keys()
        )

    def test_explicit_movie_scopes_sharing_one_directory_merge_without_double_recycle(
        self,
    ):
        first, root = self.movie()
        second = self.video("second-video", "Second.mkv", "movie")
        self.file("poster", "poster.jpg", "movie")
        payload = self.fx.payload(first, second)
        payload["scopes"] = [
            {"kind": "movie", "root": self.remote(root), "paths": [self.remote(path)]}
            for path in (first, second)
        ]
        result = self.execute(payload)
        self.assert_success(result, {"movie-video", "second-video", "poster"})
        self.assertEqual(self.cloud.deleted_directories, ["movie"])
        self.assertEqual(result["stats"]["media_deleted"], 2)
        self.assertEqual(result["stats"]["companions_deleted"], 1)

    def test_explicit_nested_movie_scopes_close_shared_parent_without_recycling_twice(
        self,
    ):
        first, _root = self.movie()
        self.directory("nested", "Nested", "movie")
        second = self.video("second-video", "Second.mkv", "nested")
        self.file("poster", "poster.jpg", "movie")
        self.file("nested-poster", "poster.jpg", "nested")
        payload = self.fx.payload(first, second)
        payload["scopes"] = [
            {
                "kind": "movie",
                "root": self.remote(path.parent),
                "paths": [self.remote(path)],
            }
            for path in (first, second)
        ]
        result = self.execute(payload)
        self.assert_success(
            result, {"movie-video", "second-video", "poster", "nested-poster"}
        )
        self.assertEqual(self.cloud.deleted_directories, ["nested", "movie"])
        self.assertTrue(
            first.exists() and second.exists()
        )  # 本地仍由后续 STRM/原生删除收尾。


if __name__ == "__main__":
    unittest.main()
