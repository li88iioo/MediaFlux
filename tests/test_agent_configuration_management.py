"""配置工具的实际 SQLite 读写、确认快照、内置边界与来源映射保留。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from app import database as db, sdk
from app.modules import recognition_knowledge as knowledge
from tests.support import IsolatedDatabaseTestCase


class ConfigurationManagementTests(IsolatedDatabaseTestCase):
    def setUp(self):
        knowledge.reset_runtime_state_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
        self.scheduler = patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler"
        )
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)

    def test_local_source_defaults_safe_and_full_crud(self):
        created = sdk.local.create_source(name="Agent Source", local_root=str(self.directory))
        self.assertTrue(created["ok"])
        source = created["data"]["source"]
        self.assertFalse(source["enabled"])
        self.assertEqual(source["mode"], "preview_only")
        self.assertTrue(sdk.local.update_source(source["id"], name="Renamed", mode="move")["ok"])
        self.assertEqual(sdk.local.source(source["id"])["name"], "Renamed")
        self.assertTrue(sdk.local.delete_source(source["id"])["ok"])
        self.assertEqual(sdk.local.sources()["items"], [])

    def test_source_paths_and_credentials_rejected(self):
        for root in ("relative", "/", str(self.directory / "absent")):
            with self.assertRaises(ValueError):
                sdk.local.create_source(name="bad", local_root=root)
        for key in ("owner", "smb_pass"):
            with self.assertRaises(TypeError):
                sdk.prepare_sdk_method("local.create_source", {"name": "bad", "local_root": str(self.directory), key: "injected"}, owner="test")

    def test_automation_writes_are_native_effects(self):
        from app.agent.kernel.bootstrap import build_tool_catalog
        from app.agent.kernel.capabilities import ToolEffect
        from app.agent.kernel.ports.sdk_effects import automation_tools

        specs = automation_tools()
        self.assertEqual({spec.name for spec in specs}, {
            "automation.set_digest",
        })
        catalog_names = {spec.name for spec in build_tool_catalog().visible()}
        self.assertFalse(catalog_names & {"automation.create_media_rule", "automation.digest_rules"})
        self.assertIn("automation.set_digest", catalog_names)
        for spec in specs:
            self.assertEqual(spec.effect, ToolEffect.WRITE)
            self.assertIsNotNone(spec.prepare)
            self.assertIsNotNone(spec.execute_confirmed)


class MediaPathMappingTests(IsolatedDatabaseTestCase):
    def setUp(self):
        import os

        from app import config

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_file = Path(self.temp.name) / "user.env"
        self.env_file.write_text(
            "JELLYFIN_PATH_MAPPINGS=[]\nEMBY_PATH_MAPPINGS=[]\nTMDB_API_KEY=keep-private\n",
            encoding="utf-8",
        )
        for context in (
            patch.dict(os.environ, {}, clear=False),
            patch.object(config, "ENV_FILE", self.env_file),
            patch.object(config, "_cache", None),
            patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
        ):
            context.start()
            self.addCleanup(context.stop)
        for key in ("JELLYFIN_PATH_MAPPINGS", "EMBY_PATH_MAPPINGS"):
            os.environ.pop(key, None)

    def test_mapping_full_crud_preserves_credentials_and_other_server(self):
        from app import config
        from app.agent.feature_gate import initialize_runtime_fence
        from app.modules import media_server_path_mapping as service

        initialize_runtime_fence()
        args = {"server": "jellyfin", "local_path": "/media/strm/movies", "server_path": "/mnt/media/movies"}
        before = self.env_file.read_bytes()
        prepared = service.prepare_path_mapping_change("create", **args)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertTrue(service.apply_path_mapping_change("create", **args, expected_fingerprint=prepared["fingerprint"])["ok"])
        saved = config.read_env_snapshot(self.env_file)[1]
        self.assertEqual(saved["TMDB_API_KEY"], "keep-private")
        self.assertEqual(saved["EMBY_PATH_MAPPINGS"], "[]")
        self.assertEqual(service.list_path_mappings("jellyfin")["count"], 1)
        update = {"server": "jellyfin", "local_path": "/media/strm/movies", "server_path": "D:/Media/Movies"}
        prepared = service.prepare_path_mapping_change("update", **update)
        self.assertTrue(service.apply_path_mapping_change("update", **update, expected_fingerprint=prepared["fingerprint"])["ok"])
        delete = {"server": "jellyfin", "local_path": "/media/strm/movies"}
        prepared = service.prepare_path_mapping_change("delete", **delete)
        self.assertTrue(service.apply_path_mapping_change("delete", **delete, expected_fingerprint=prepared["fingerprint"])["ok"])
        self.assertEqual(service.list_path_mappings("jellyfin")["count"], 0)

    def test_mapping_concurrent_env_edit_rejects_and_preserves_external_change(self):
        from app.modules import media_server_path_mapping as service

        args = {"server": "jellyfin", "local_path": "/strm/movies", "server_path": "/media/movies"}
        prepared = service.prepare_path_mapping_change("create", **args)
        self.env_file.write_text(self.env_file.read_text() + "STRM_SCHEDULE_ENABLED=1\n")
        with self.assertRaises(service.MappingSnapshotChanged):
            service.apply_path_mapping_change("create", **args, expected_fingerprint=prepared["fingerprint"])
        self.assertIn("STRM_SCHEDULE_ENABLED=1", self.env_file.read_text())
        self.assertIn("JELLYFIN_PATH_MAPPINGS=[]", self.env_file.read_text())

    def test_mapping_deployment_override_and_root_paths_rejected(self):
        from app.modules import media_server_path_mapping as service

        args = {"server": "jellyfin", "local_path": "/strm/movies", "server_path": "/media/movies"}
        with patch("app.config.has_external_override", return_value=True), self.assertRaises(ValueError):
            service.prepare_path_mapping_change("create", **args)
        with self.assertRaises(ValueError):
            service.prepare_path_mapping_change("create", **{**args, "local_path": "/"})
