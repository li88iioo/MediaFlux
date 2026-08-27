"""光鸭媒体名称卫生能力测试（当前覆盖番号清理策略）。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.agent import guangya_rename_actions as actions
from app.agent.models import RiskLevel, ToolContext
from app.agent.tools import build_tool_registry
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
                    "dir", "(spam.example.com)-ABC-123", True,
                    parent_id="root", etag="dir-etag", updated_at=1,
                ),
            ],
            "dir": [
                GuangYaFile(
                    "video", "'spam.example.com'.ABC-123。mp4", False,
                    parent_id="dir", size=100, etag="video-etag", extension="mp4",
                ),
                GuangYaFile(
                    "subtitle", "(spam.example.com)-ABC-123.CHT.srt", False,
                    parent_id="dir", size=10, etag="sub-etag", extension="srt",
                ),
                GuangYaFile(
                    "poster", "poster.jpg", False,
                    parent_id="dir", size=5, etag="poster-etag", extension="jpg",
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
            mock.patch.object(guangya_rename, "_plan_directory", return_value=self.plan_dir),
            mock.patch.object(guangya_rename, "_owner_digest", return_value="owner-digest"),
            mock.patch.object(guangya_rename, "get_web_secret", return_value="test-secret"),
            mock.patch.object(actions, "organize_operation_owner_digest", return_value="owner-digest"),
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
        changes = {
            item["old_name"]: item["new_name"] for item in plan["entries"]
        }
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
        client.directories["dir"].append(GuangYaFile(
            "unknown-video", "unidentified-release.mp4", False,
            parent_id="dir", size=90, etag="unknown-etag", extension="mp4",
        ))
        plan = hygiene.build_media_hygiene_plan(
            client,
            owner="owner",
            path="/NSFW/(spam.example.com)-ABC-123",
            recursive=True,
            limit=100,
        )
        changes = {
            item["old_name"]: item["new_name"] for item in plan["entries"]
        }
        self.assertEqual(
            changes["'spam.example.com'.ABC-123。mp4"], "ABC-123.mp4"
        )
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

    def test_hygiene_execute_alias_rejects_ordinary_rename_flow(self):
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
        with self.assertRaisesRegex(Exception, "媒体名称清理预览"):
            actions.prepare_guangya_media_hygiene_confirmation({}, context)

    def test_durable_hygiene_execution_triggers_full_strm(self):
        scheduler = mock.Mock()
        scheduler.trigger.return_value = {"ok": True, "queued": True}
        with (
            mock.patch.object(actions, "load_rename_plan", return_value={"mode": "media_hygiene"}),
            mock.patch.object(
                actions,
                "execute_rename_plan",
                return_value={"partial": False, "stats": {"renamed": 2, "failed": 0}},
            ),
            mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
        ):
            result = actions.execute_durable_guangya_rename_job({
                "plan_id": "a" * 32, "plan_fingerprint": "b" * 64,
            })
        scheduler.trigger.assert_called_once_with(
            "organize", force_full=True, sync_mode="full"
        )
        self.assertEqual(result["stats"]["strm_triggered"], 1)

    def test_terminal_private_plan_is_removed_after_retention(self):
        client = FakeHygieneClient()
        plan = hygiene.build_media_hygiene_plan(
            client, owner="owner",
            path="/NSFW/(spam.example.com)-ABC-123", limit=100,
        )
        guangya_rename.update_rename_plan_execution(
            plan["plan_id"], status="completed", execution={"renamed": 3}
        )
        with mock.patch.object(
            guangya_rename.time, "time",
            return_value=plan["created_at_epoch"] + 8 * 24 * 60 * 60,
        ):
            result = guangya_rename.maintain_rename_plans()
        self.assertEqual(result["removed"], 1)
        self.assertFalse((self.plan_dir / f"{plan['plan_id']}.json").exists())

    def test_registry_exposes_hygiene_preview(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["guangya.media_hygiene.preview"]["risk"],
            RiskLevel.READ.value,
        )
        llm = {
            item["name"]
            for item in registry.llm_orchestration_capabilities(
                include_confirmations=True
            )
        }
        self.assertIn("guangya.media_hygiene.preview", llm)
        self.assertIn("guangya.media_hygiene.execute", llm)
        self.assertIn("guangya.rename.execute", llm)
        self.assertTrue(
            capabilities["guangya.media_hygiene.execute"]["requires_confirmation"]
        )


if __name__ == "__main__":
    unittest.main()
