"""光鸭媒体名称卫生能力测试（当前覆盖番号清理策略）。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from app.agent import guangya_rename_actions as actions
from app.agent.models import ToolContext
from app.clients.guangya import GuangYaFile
from app.modules import guangya_media_hygiene as hygiene
from app.modules import guangya_rename


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
        actions.reset_guangya_rename_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.plan_dir = Path(self.temp.name) / "plans"
        self.patches = [
            mock.patch.object(
                guangya_rename, "_plan_directory", return_value=self.plan_dir
            ),
            mock.patch.object(
                guangya_rename, "_owner_digest", return_value="owner-digest"
            ),
            mock.patch.object(
                guangya_rename, "get_web_secret", return_value="test-secret"
            ),
            mock.patch.object(
                actions, "organize_operation_owner_digest", return_value="owner-digest"
            ),
            mock.patch.object(
                hygiene.config,
                "get",
                side_effect=lambda key, default="": {
                    "GY_ORGANIZE_VIDEO_EXTS": "",
                    "GY_ORGANIZE_METADATA_EXTS": "",
                    "GY_ORGANIZE_NSFW_STRIP_DOMAINS": "",
                    "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT": "",
                }.get(key, default),
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def test_numeric_provider_sequence_in_year_range_is_cleaned(self):
        client = FakeHygieneClient()
        video = client.directories["dir"][0]
        video.name = "4k688.com@259LUXU-1900.mp4"
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            result = actions.preview_guangya_media_hygiene(
                {"path": "/NSFW", "recursive": True, "limit": 20, "enrich_metadata": False},
                ToolContext(owner="year-sequence-owner"),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["video_rename_count"], 1)
        self.assertEqual(result.data["identified_video_count"], 1)
        self.assertIn("4k688.com@259LUXU-1900.mp4 → 259LUXU-1900.mp4", result.data["sample_changes"])

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        actions.reset_guangya_rename_context_for_tests()
        self.temp.cleanup()

    def test_preview_cleans_domain_pollution_and_preserves_companion_suffix(self):
        client = FakeHygieneClient()
        plan = hygiene.build_media_hygiene_plan(
            client,
            owner="owner",
            path="/NSFW/(spam.example.com)-ABC-123",
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
        plan = hygiene.build_media_hygiene_plan(
            client,
            owner="owner",
            path="/NSFW/(spam.example.com)-ABC-123",
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

    def test_agent_preview_uses_shared_rename_confirmation_flow(self):
        client = FakeHygieneClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_media_hygiene(
                {
                    "path": "/NSFW/(spam.example.com)-ABC-123",
                    "recursive": True,
                    "limit": 100,
                    "enrich_metadata": False,
                },
                context,
            )
            confirmation, _fingerprint = actions.prepare_guangya_rename_confirmation(
                {}, context
            )
        self.assertEqual(preview.status, "ready")
        self.assertEqual(preview.data["rename_count"], 3)
        self.assertEqual(preview.data["mode"], "media_hygiene")
        self.assertIn("STRM", " ".join(confirmation.data["effects"]))

    def test_canonical_execute_accepts_ordinary_rename_flow(self):
        client = FakeHygieneClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_rename(
                {
                    "paths": ["/NSFW/(spam.example.com)-ABC-123"],
                    "mode": "replace_text",
                    "recursive": True,
                    "limit": 100,
                    "find_text": "spam.example.com",
                    "replace_text": "clean",
                },
                context,
            )
            confirmation, _fingerprint = actions.prepare_guangya_rename_confirmation(
                {}, context
            )
        self.assertEqual(confirmation.status, "confirmation_required")
        self.assertEqual(confirmation.data["mode"], "replace_text")

    def test_hygiene_uses_the_same_scoped_strm_handoff(self):
        for source_id, expected in (("root", True), ("outside", False)):
            with self.subTest(source_id=source_id):
                client = FakeHygieneClient()
                client.directories["0"].append(GuangYaFile("outside", "outside", True, parent_id="0"))
                plan = hygiene.build_media_hygiene_plan(client, owner="owner", path="/NSFW/(spam.example.com)-ABC-123", recursive=True, limit=100)
                guangya_rename.confirm_rename_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
                scheduler = mock.Mock()
                scheduler.trigger.return_value = {"ok": True, "queued": True}
                with (mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
                      mock.patch("app.modules.strm.configured_strm_source_plans", return_value=([{"id": source_id, "name": source_id}], "")),
                      mock.patch.object(guangya_rename.time, "sleep")):
                    result = guangya_rename.execute_rename_plan(
                        {"version": 1, "plan_id": plan["plan_id"], "plan_fingerprint": plan["fingerprint"],
                         "owner_digest": "owner-digest", "credential_generation": 11}, client_factory=lambda: client)
                self.assertEqual(result["stats"]["renamed"], 3)
                self.assertFalse(result["partial"])
                if expected:
                    scheduler.trigger.assert_called_once_with("organize", sync_mode="full", selected_source_ids=[source_id])
                else:
                    scheduler.trigger.assert_not_called()
                    self.assertEqual(result["stats"]["strm_trigger_skipped"], 1)

    def test_removed_legacy_mode_cannot_trigger_strm(self):
        client = FakeHygieneClient()
        plan = hygiene.build_media_hygiene_plan(client, owner="owner", path="/NSFW/(spam.example.com)-ABC-123", recursive=True, limit=100)
        guangya_rename.confirm_rename_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        stored = guangya_rename.load_rename_plan(plan["plan_id"], require_confirmed=True)
        stored["mode"] = "declarative"
        guangya_rename._atomic_write_plan(guangya_rename._plan_path(plan["plan_id"]), stored)
        factory = mock.Mock()
        with self.assertRaises(guangya_rename.GuangYaRenamePlanError):
            guangya_rename.execute_rename_plan({"version": 1, "plan_id": plan["plan_id"], "plan_fingerprint": plan["fingerprint"]}, client_factory=factory)
        factory.assert_not_called()

    def test_terminal_private_plan_is_removed_after_retention(self):
        client = FakeHygieneClient()
        plan = hygiene.build_media_hygiene_plan(
            client, owner="owner", path="/NSFW/(spam.example.com)-ABC-123", limit=100
        )
        guangya_rename.update_rename_plan_execution(
            plan["plan_id"], status="completed", execution={"renamed": 3}
        )
        with mock.patch.object(
            guangya_rename.time,
            "time",
            return_value=plan["created_at_epoch"] + 8 * 24 * 60 * 60,
        ):
            result = guangya_rename.maintain_rename_plans()
        self.assertEqual(result["removed"], 1)
        self.assertFalse((self.plan_dir / f"{plan['plan_id']}.json").exists())
