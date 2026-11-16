"""本地回收在真实来源映射下保护尚未完成的 qB 下载。"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from app.modules.local_media_candidates import candidate_payload
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.qb_control import qb_control_write_lease
from app.modules.scraper import TMDBScraper
from app.repositories import local_media as repository
from tests.support import IsolatedDatabaseTestCase


class LocalMediaTrashDownloadTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory) / "downloads"
        self.root.mkdir()
        self.source_id = repository.create_local_media_source(
            self.id(), "configured:qb", "/qb/downloads", str(self.root), enabled=0,
        )
        self.source = repository.get_local_media_source(self.source_id)
        self.service = LocalMediaService(scraper=cast(TMDBScraper, object()))
        self.addCleanup(self.service.close)
        self.qb = Mock()
        self.qb.list_torrents.return_value = []
        self.factory = self.enterContext(patch(
            "app.modules.qb_control.configured_qb_client", return_value=self.qb,
        ))
        self.settings = self.enterContext(patch(
            "app.modules.local_media_service.config.get_many",
            return_value={"QB_URL": "http://offline.invalid", "QB_USERNAME": "", "QB_PASSWORD": "", "QB_API_KEY": "fixture"},
        ))

    def directory_item(self, name: str = "Movie"):
        selected = self.root / name
        selected.mkdir()
        video = selected / "Movie.mkv"
        video.write_bytes(b"media")
        item = candidate_payload(self.source, selected, organize_ready=False)
        return selected, video, item["identity"]

    @staticmethod
    def torrent(path="/qb/downloads/Movie", *, state="downloading", progress=0.5, save_path="/qb/downloads"):
        return SimpleNamespace(content_path=path, save_path=save_path, state=state, progress=progress)

    def test_downloading_directory_is_preserved_while_existing_child_is_written(self):
        selected, video, identity = self.directory_item()
        self.qb.list_torrents.return_value = [self.torrent()]
        self.assertEqual(repository.list_local_media_tasks(), [])
        with video.open("ab") as writer:
            writer.write(b" still downloading")
            writer.flush()
            os.fsync(writer.fileno())
            self.assertEqual(str(selected.stat().st_mtime_ns), identity["mtime_ns"])
            with self.assertRaisesRegex(LocalMediaServiceError, "qB.*尚未完成"):
                self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
            writer.write(b" safe continuation")
        self.assertTrue(video.read_bytes().endswith(b"safe continuation"))
        self.assertFalse((self.root / ".mediaflux-trash").exists())
        self.qb.close.assert_called_once_with()

    def test_download_equal_ancestor_and_descendant_ranges_are_protected(self):
        for relation in ("equal", "ancestor", "descendant"):
            with self.subTest(relation=relation):
                selected, video, identity = self.directory_item(relation)
                path = {"equal": f"/qb/downloads/{relation}", "ancestor": "/qb/downloads",
                        "descendant": f"/qb/downloads/{relation}/Movie.mkv"}[relation]
                self.qb.list_torrents.return_value = [self.torrent(path)]
                with self.assertRaisesRegex(LocalMediaServiceError, "qB.*尚未完成"):
                    self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
                self.assertEqual(video.read_bytes(), b"media")

    def test_only_safe_completed_tasks_allow_recycling(self):
        for state, progress, completed in (("pausedDL", 0.4, False), ("checkingDL", 1.0, False),
                                           ("stalledUP", 1.0, True)):
            with self.subTest(state=state):
                selected, video, identity = self.directory_item(state)
                self.qb.list_torrents.return_value = [self.torrent(f"/qb/downloads/{state}", state=state, progress=progress)]
                if completed:
                    destination = self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
                    self.assertFalse(selected.exists())
                    self.assertEqual((destination / video.name).read_bytes(), b"media")
                else:
                    with self.assertRaisesRegex(LocalMediaServiceError, "qB.*尚未完成"):
                        self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
                    self.assertTrue(video.exists())

    def test_other_source_and_sibling_downloads_do_not_block_selected_item(self):
        selected, video, identity = self.directory_item()
        other_root = Path(self.directory) / "other-downloads"
        other_root.mkdir()
        other = other_root / "Movie"
        other.mkdir()
        (other / "Movie.mkv").write_bytes(b"other media")
        repository.create_local_media_source("other", "configured:qb", "/qb/other", str(other_root))
        self.qb.list_torrents.return_value = [self.torrent("/qb/other/Movie"), self.torrent("/qb/downloads/Sibling")]
        destination = self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
        self.assertFalse(selected.exists())
        self.assertEqual((destination / video.name).read_bytes(), b"media")
        self.assertEqual((other / "Movie.mkv").read_bytes(), b"other media")

    def test_other_downloader_profile_recycles_without_reading_current_qb(self):
        repository.update_local_media_source(self.source_id, qb_profile="other:qb")
        selected, video, identity = self.directory_item()
        self.qb.list_torrents.return_value = [self.torrent()]

        destination = self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)

        self.assertFalse(selected.exists())
        self.assertEqual((destination / video.name).read_bytes(), b"media")
        self.factory.assert_not_called()
        self.qb.list_torrents.assert_not_called()

    def test_empty_prefix_uses_existing_save_path_mapping(self):
        repository.update_local_media_source(self.source_id, qb_path_prefix="")
        selected, video, identity = self.directory_item()
        self.qb.list_torrents.return_value = [self.torrent()]
        with self.assertRaisesRegex(LocalMediaServiceError, "qB.*尚未完成"):
            self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
        self.assertTrue(video.exists())

    def test_unknown_content_path_protects_its_mapped_save_directory(self):
        selected, video, identity = self.directory_item()
        self.qb.list_torrents.return_value = [self.torrent("")]
        with self.assertRaisesRegex(LocalMediaServiceError, "qB.*尚未完成"):
            self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
        self.assertTrue(video.exists())

    def test_configured_downloader_read_failure_keeps_media(self):
        selected, video, identity = self.directory_item()
        self.qb.list_torrents.side_effect = RuntimeError("offline fixture")
        with self.assertRaisesRegex(LocalMediaServiceError, "无法核验.*qB"):
            self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
        self.assertTrue(video.exists())
        self.assertFalse((self.root / ".mediaflux-trash").exists())
        self.qb.close.assert_called_once_with()

    def test_local_only_installation_does_not_require_downloader(self):
        self.settings.return_value = {"QB_URL": "", "QB_USERNAME": "", "QB_PASSWORD": "", "QB_API_KEY": ""}
        selected, video, identity = self.directory_item()
        destination = self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
        self.assertEqual((destination / video.name).read_bytes(), b"media")
        self.factory.assert_not_called()

    def test_recycle_waits_for_qb_control_before_reading_and_moving(self):
        selected, video, identity = self.directory_item()
        read_started = threading.Event()
        finished = threading.Event()
        errors = []
        self.qb.list_torrents.side_effect = lambda: read_started.set() or []

        def recycle():
            try:
                self.service.move_media_item_to_trash("admin", self.source_id, selected, identity)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with qb_control_write_lease():
            worker = threading.Thread(target=recycle)
            worker.start()
            self.assertFalse(read_started.wait(timeout=0.05))
            self.assertTrue(video.exists())
        worker.join(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertFalse(selected.exists())
