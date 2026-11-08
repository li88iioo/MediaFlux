"""公共媒体名称编译的完整事实与STRM范围回归。"""

from __future__ import annotations
from typing import cast
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import guangya_media_hygiene as hygiene, guangya_fs_change as fs
from tests.support import initialized_web_runtime
from tests.cloud_test_support import run_plan


class FakeHygieneClient:
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 11
        self.closed = False
        self.directories = {
            "0": [GuangYaFile("root", "NSFW", True, parent_id="0")],
            "root": [
                GuangYaFile(
                    "dir",
                    "(spam.example.com)-ABC-123",
                    True,
                    parent_id="root",
                    etag="dir-etag",
                    updated_at=1,
                )
            ],
            "dir": [
                GuangYaFile(
                    "video",
                    "'spam.example.com'.ABC-123。mp4",
                    False,
                    parent_id="dir",
                    size=100,
                    etag="video-etag",
                    extension="mp4",
                ),
                GuangYaFile(
                    "subtitle",
                    "(spam.example.com)-ABC-123.CHT.srt",
                    False,
                    parent_id="dir",
                    size=10,
                    etag="sub-etag",
                    extension="srt",
                ),
                GuangYaFile(
                    "poster",
                    "poster.jpg",
                    False,
                    parent_id="dir",
                    size=5,
                    etag="poster-etag",
                    extension="jpg",
                ),
            ],
        }

    def list_dir(self, parent_id="0"):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def rename(self, file_id, new_name):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    item.name = str(new_name)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True


class GuangYaMediaHygieneTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(initialized_web_runtime())
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(
            mock.patch.object(fs, "_directory", return_value=Path(folder) / "plans")
        )
        self.enterContext(
            mock.patch.object(
                hygiene.config, "get", side_effect=lambda key, default="": default
            )
        )

    def test_preview_cleans_domain_pollution_and_preserves_companion_suffix(self):
        client = FakeHygieneClient()
        plan = hygiene.preview_media_hygiene(
            cast(GuangYaClient, client),
            file_id="dir",
            recursive=True,
            limit=100,
        )
        changes = {item["old_name"]: item["new_name"] for item in plan["entries"]}
        self.assertEqual(changes["'spam.example.com'.ABC-123。mp4"], "ABC-123.mp4")
        self.assertEqual(
            changes["(spam.example.com)-ABC-123.CHT.srt"], "ABC-123.CHT.srt"
        )
        self.assertEqual(changes["(spam.example.com)-ABC-123"], "ABC-123")
        self.assertNotIn("poster.jpg", changes)
        self.assertEqual(plan["stats"]["video_rename_count"], 1)
        self.assertEqual(plan["stats"]["companion_rename_count"], 1)
        self.assertEqual(plan["stats"]["directory_rename_count"], 1)

    def test_unknown_second_video_blocks_companion_and_directory_rename(self):
        client = FakeHygieneClient()
        client.directories["dir"].append(
            GuangYaFile(
                "unknown-video",
                "unidentified-release.mp4",
                False,
                parent_id="dir",
                size=90,
                etag="unknown-etag",
                extension="mp4",
            )
        )
        plan = hygiene.preview_media_hygiene(
            cast(GuangYaClient, client),
            file_id="dir",
            recursive=True,
            limit=100,
        )
        changes = {item["old_name"]: item["new_name"] for item in plan["entries"]}
        self.assertEqual(changes["'spam.example.com'.ABC-123。mp4"], "ABC-123.mp4")
        self.assertNotIn("(spam.example.com)-ABC-123.CHT.srt", changes)
        self.assertNotIn("(spam.example.com)-ABC-123", changes)
        self.assertEqual(plan["stats"]["unidentified_video_count"], 1)
        self.assertEqual(plan["stats"]["companion_rename_count"], 0)
        self.assertEqual(plan["stats"]["directory_rename_count"], 0)

    def test_hygiene_uses_the_same_scoped_strm_handoff(self):
        for source_id, expected in (("root", True), ("outside", False)):
            with self.subTest(source_id=source_id):
                client = FakeHygieneClient()
                client.directories["0"].append(
                    GuangYaFile("outside", "outside", True, parent_id="0")
                )
                plan = hygiene.preview_media_hygiene(
                    cast(GuangYaClient, client),
                    file_id="dir",
                    recursive=True,
                    limit=100,
                )
                plan = fs.build_fs_change_plan(
                    cast(GuangYaClient, client),
                    owner="owner",
                    operations=plan["operations"],
                    trigger_strm=True,
                )
                scheduler = mock.Mock()
                scheduler.trigger.return_value = {"ok": True, "queued": True}
                with (
                    mock.patch(
                        "app.modules.scheduler.get_scheduler", return_value=scheduler
                    ),
                    mock.patch(
                        "app.modules.strm.configured_strm_source_plans",
                        return_value=([{"id": source_id, "name": source_id}], ""),
                    ),
                    mock.patch("app.clients.guangya.sleep"),
                ):
                    result = run_plan(client, plan)
                self.assertEqual(result["stats"]["renamed"], 3)
                self.assertFalse(result["partial"])
                if expected:
                    scheduler.trigger.assert_called_once_with(
                        "organize", sync_mode="full", selected_source_ids=[source_id]
                    )
                else:
                    scheduler.trigger.assert_not_called()
                    self.assertEqual(result["stats"]["strm_trigger_skipped"], 1)
