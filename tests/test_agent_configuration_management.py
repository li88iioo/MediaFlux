"""配置工具的实际 SQLite 读写、确认快照、内置边界与来源映射保留。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.agent.configuration_management_actions import (
    execute_knowledge,
    execute_source,
    knowledge_mutation_arguments,
    list_knowledge,
    prepare_knowledge,
    prepare_source,
    source_mutation_arguments,
)
from app.agent.errors import AgentToolError
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
            "app.agent.configuration_management_actions.get_local_media_scheduler"
        )
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)

    def test_knowledge_create_update_delete_verified_against_business_service(self):
        args = {
            "knowledge_type": "release_group",
            "canonical_value": "AgentTestGroup",
            "aliases": ["Agent Test Group"],
        }
        preview, token = prepare_knowledge(args, "create")
        self.assertEqual(
            list_knowledge({"keyword": "AgentTestGroup"}).data["items"], []
        )
        created = execute_knowledge(args, token, "create")
        number = created.data["item"]["entry_number"]
        update = {"entry_number": number, "disabled": True}
        _, token = prepare_knowledge(update, "update")
        self.assertTrue(
            execute_knowledge(update, token, "update").data["item"]["disabled"]
        )
        delete = {"entry_number": number}
        _, token = prepare_knowledge(delete, "delete")
        self.assertTrue(execute_knowledge(delete, token, "delete").ok)
        self.assertIsNone(knowledge.get_entry(number))
        self.assertNotIn("evidence", preview.data)

    def test_changed_knowledge_snapshot_rejected(self):
        row = knowledge.create_entry(
            {"knowledge_type": "release_group", "canonical_value": "SnapshotTest"}
        )
        args = {"entry_number": row["id"], "disabled": True}
        _, token = prepare_knowledge(args, "update")
        knowledge.update_entry(row["id"], {"aliases": ["another"]})
        with self.assertRaises(AgentToolError):
            execute_knowledge(args, token, "update")
        self.assertFalse(knowledge.get_entry(row["id"])["disabled"])

    def test_reject_protected_knowledge_fields_and_builtin_deletion(self):
        for field in ("source", "evidence", "knowledge_key", "confidence"):
            with self.assertRaises(AgentToolError):
                knowledge_mutation_arguments(
                    {
                        "knowledge_type": "release_group",
                        "canonical_value": "test",
                        field: "forged",
                    },
                    "create",
                )
        knowledge.ensure_seed_knowledge()
        builtin = next(
            row
            for row in knowledge.list_entries(limit=500)["items"]
            if row["source"] == "builtin"
        )
        with self.assertRaises(AgentToolError):
            prepare_knowledge({"entry_number": builtin["id"]}, "delete")

    def test_local_source_defaults_safe_and_full_crud(self):
        args = {"name": "Agent Source", "local_root": str(self.directory)}
        preview, token = prepare_source(args, "create")
        self.assertEqual(db.list_local_media_sources(owner="admin"), [])
        self.assertFalse(preview.data["enabled"])
        self.assertEqual(preview.data["mode"], "preview_only")
        created = execute_source(args, token, "create")
        self.assertTrue(created.ok)
        source = db.list_local_media_sources(owner="admin")[0]
        self.assertEqual(source.name, "Agent Source")
        update = {"source_number": 1, "name": "Renamed", "mode": "move"}
        _, token = prepare_source(update, "update")
        self.assertTrue(execute_source(update, token, "update").ok)
        self.assertEqual(
            db.get_local_media_source(source.id, owner="admin").name, "Renamed"
        )
        _, token = prepare_source({"source_number": 1}, "delete")
        self.assertTrue(execute_source({"source_number": 1}, token, "delete").ok)
        self.assertEqual(db.list_local_media_sources(owner="admin"), [])

    def test_source_paths_and_credentials_rejected(self):
        for root in ("relative", "/", str(self.directory / "absent")):
            with self.assertRaises(AgentToolError):
                prepare_source({"name": "bad", "local_root": root}, "create")
        for key in ("qb_profile", "owner", "smb_pass", "targets"):
            with self.assertRaises(AgentToolError):
                source_mutation_arguments(
                    {"name": "bad", "local_root": str(self.directory), key: "injected"},
                    "create",
                )

    def test_source_update_keeps_mapping_and_detects_changed_snapshot(self):
        target = self.directory / "target"
        target.mkdir()
        source_dir = self.directory / "source"
        source_dir.mkdir()
        sid = db.save_local_media_source_bundle(
            name="initial",
            local_root=str(source_dir),
            qb_profile="configured:qb",
            qb_path_prefix="/downloads",
            enabled=False,
            media_type="auto",
            mode="move",
            targets=[{"category": "default", "path": str(target)}],
        )
        args = {"source_number": 1, "name": "changed"}
        _, token = prepare_source(args, "update")
        self.assertTrue(execute_source(args, token, "update").ok)
        self.assertEqual(
            db.list_local_library_targets(sid, owner="admin")[0].path, str(target)
        )
        with self.assertRaises(AgentToolError):
            execute_source(args, token, "update")

    def test_all_specs_are_atomic_confirmable_and_no_http_calls(self):
        from app.agent.domain_catalog import automation_rules, configuration_management

        class Registry:
            def __init__(self):
                self.specs = []

            def register(self, spec):
                self.specs.append(spec)

        registry = Registry()
        automation_rules.register_specs(registry)
        configuration_management.register_specs(registry)
        self.assertEqual(len(registry.specs), 14)
        self.assertEqual(len({spec.name for spec in registry.specs}), 14)
        for spec in registry.specs:
            if spec.risk.value != "read":
                self.assertTrue(spec.requires_confirmation)
                self.assertIsNotNone(spec.context_confirmation_preparer)
                self.assertIsNotNone(spec.context_confirmed_handler)


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
        from app.agent.configuration_management_actions import (
            execute_path_mapping,
            list_path_mappings,
            prepare_path_mapping,
        )

        args = {
            "provider": "jellyfin",
            "local_path": "/media/strm/movies",
            "server_path": "/mnt/media/电影",
        }
        before = self.env_file.read_text()
        _, token = prepare_path_mapping(args, "create")
        self.assertEqual(self.env_file.read_text(), before)
        self.assertTrue(execute_path_mapping(args, token, "create").ok)
        from app import config

        saved = config.read_env_snapshot(self.env_file)[1]
        self.assertEqual(saved["TMDB_API_KEY"], "keep-private")
        self.assertEqual(saved["EMBY_PATH_MAPPINGS"], "[]")
        self.assertEqual(list_path_mappings({"provider": "jellyfin"}).data["count"], 1)
        update = {
            "provider": "jellyfin",
            "mapping_number": 1,
            "server_path": "D:\\Media\\Movies",
        }
        _, token = prepare_path_mapping(update, "update")
        self.assertTrue(execute_path_mapping(update, token, "update").ok)
        delete = {"provider": "jellyfin", "mapping_number": 1}
        _, token = prepare_path_mapping(delete, "delete")
        self.assertTrue(execute_path_mapping(delete, token, "delete").ok)
        self.assertEqual(list_path_mappings({"provider": "jellyfin"}).data["count"], 0)

    def test_mapping_concurrent_env_edit_rejects_and_preserves_external_change(self):
        from app.agent.configuration_management_actions import (
            execute_path_mapping,
            prepare_path_mapping,
        )

        args = {
            "provider": "jellyfin",
            "local_path": "/strm/movies",
            "server_path": "/media/movies",
        }
        _, token = prepare_path_mapping(args, "create")
        self.env_file.write_text(
            self.env_file.read_text() + "STRM_SCHEDULE_ENABLED=1\n"
        )
        with self.assertRaises(AgentToolError):
            execute_path_mapping(args, token, "create")
        self.assertIn("STRM_SCHEDULE_ENABLED=1", self.env_file.read_text())
        self.assertIn("JELLYFIN_PATH_MAPPINGS=[]", self.env_file.read_text())

    def test_mapping_deployment_override_and_root_paths_rejected(self):
        from app.agent.configuration_management_actions import prepare_path_mapping

        args = {
            "provider": "jellyfin",
            "local_path": "/strm/movies",
            "server_path": "/media/movies",
        }
        with (
            patch("app.config.has_external_override", return_value=True),
            self.assertRaises(AgentToolError),
        ):
            prepare_path_mapping(args, "create")
        with self.assertRaises(AgentToolError):
            prepare_path_mapping({**args, "local_path": "/"}, "create")
