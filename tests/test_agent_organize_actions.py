"""Media Agent 光鸭整理预览与确认执行测试。"""
from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_guangya_organize_clean_empty_message,
    is_guangya_organize_run_message,
    is_guangya_organize_stop_message,
)
from app.agent.organize_actions import (
    _configured_sources,
    clean_empty_guangya_organize_sources,
    organize_clean_empty_confirmation_context,
    organize_confirmation_context,
    organize_stop_confirmation_context,
    prepare_guangya_organize_run_once,
    prepare_guangya_organize_stop,
    preview_guangya_organize,
    preview_guangya_organize_clean_empty,
    preview_guangya_organize_run_once,
    preview_guangya_organize_stop,
    run_guangya_organize_once,
    run_guangya_organize_once_confirmed,
    stop_guangya_organize_confirmed,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from app.modules.organize import OrganizeRules, Organizer
from app.modules.organize_tasks import OrganizeTaskManager
from tests.support import IsolatedDatabaseTestCase


def _no_arguments(arguments):
    if arguments:
        raise AgentToolError("不接受参数")
    return {}


def _preview_plan(title: str = "安全标题", action: str = "move"):
    return SimpleNamespace(
        action=action,
        match=SimpleNamespace(title=title, year="2026", media_type="tv"),
    )


def _preview_stats(*, total: int = 1, scan_errors=None):
    return {
        "total": total,
        "matched": total,
        "need_confirm": 0,
        "skipped": 0,
        "conflict": 0,
        "failed": 0,
        "subtitle_skipped": 0,
        "scan_errors": list(scan_errors or []),
    }


def _atomic_clean_client(*, credential_generation: int = 0):
    return SimpleNamespace(
        logged_in=True,
        credential_generation=credential_generation,
        supports_atomic_empty_directory_delete=True,
        delete_empty_directory=Mock(return_value=True),
    )


class GuangYaOrganizeActionTests(unittest.TestCase):
    def test_configured_sources_normalize_and_deduplicate(self):
        values = {
            "GY_ORGANIZE_SOURCE_DIRS": (
                '[{"id":"source-a","name":"A"},"source-b",'
                '{"id":"source-a","name":"Duplicate"}]'
            ),
        }
        with patch(
            "app.agent.organize_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            self.assertEqual(
                _configured_sources(),
                [
                    {"id": "source-a", "name": "A"},
                    {"id": "source-b", "name": "源目录2"},
                ],
            )

        values["GY_ORGANIZE_SOURCE_DIRS"] = "invalid-json"
        with patch(
            "app.agent.organize_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            with self.assertRaises(AgentToolError) as invalid:
                _configured_sources()
        self.assertEqual(invalid.exception.code, "invalid_configuration")

        values["GY_ORGANIZE_SOURCE_DIRS"] = "[]"
        with patch(
            "app.agent.organize_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            self.assertEqual(_configured_sources(), [])

        values["GY_ORGANIZE_SOURCE_DIRS"] = ""
        with patch(
            "app.agent.organize_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            self.assertEqual(_configured_sources(), [])

    def test_confirmation_context_changes_with_configuration_and_credential_generation(self):
        sources = [{"id": "secret-source", "name": "Secret"}]
        current = {
            "rules": OrganizeRules(target_dir_id="secret-target"),
            "generation": 7,
        }

        def configured_inputs():
            return sources, current["rules"], ""

        def client_factory():
            return SimpleNamespace(credential_generation=current["generation"])

        with patch(
            "app.agent.organize_actions._configured_inputs",
            side_effect=configured_inputs,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            side_effect=client_factory,
        ):
            first = organize_confirmation_context({})
            self.assertEqual(first, organize_confirmation_context({}))
            current["rules"] = OrganizeRules(
                target_dir_id="secret-target-two",
                clean_empty=False,
            )
            rules_changed = organize_confirmation_context({})
            current["rules"] = OrganizeRules(target_dir_id="secret-target")
            current["generation"] = 8
            credential_changed = organize_confirmation_context({})

        self.assertNotEqual(first, rules_changed)
        self.assertNotEqual(first, credential_changed)
        for context in (first, rules_changed, credential_changed):
            self.assertNotIn("secret", context)
            self.assertRegex(context, r"^[0-9a-f]{64}$")

    def test_clean_empty_context_tracks_exact_source_snapshot(self):
        current = [{"id": "secret-source-one", "name": "Secret One"}]
        with patch(
            "app.agent.organize_actions._configured_sources",
            side_effect=lambda: list(current),
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=SimpleNamespace(
                logged_in=True,
                credential_generation=7,
            ),
        ):
            first = organize_clean_empty_confirmation_context({})
            self.assertEqual(first, organize_clean_empty_confirmation_context({}))
            current.append({"id": "secret-source-two", "name": "Secret Two"})
            second = organize_clean_empty_confirmation_context({})

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertRegex(second, r"^[0-9a-f]{64}$")
        self.assertNotIn("secret-source-one", first)
        self.assertNotIn("Secret One", first)

    def test_clean_empty_preview_and_execution_return_only_safe_counts(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        manager.clean_empty.return_value = {
            "ok": True,
            "cleaned": 3,
            "sources": [{**sources[0], "cleaned": 3}],
        }
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ):
            preview = preview_guangya_organize_clean_empty({})
            completed = clean_empty_guangya_organize_sources({})

        self.assertTrue(preview.ok)
        self.assertEqual(preview.data, {"source_count": 1})
        self.assertTrue(completed.ok)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            completed.data,
            {"cleaned": 3, "failed": 0, "source_count": 1},
        )
        manager.clean_empty.assert_called_once()
        self.assertEqual(manager.clean_empty.call_args.args, (sources,))
        self.assertTrue(manager.clean_empty.call_args.kwargs["client"].logged_in)
        serialized = str(preview.to_dict()) + str(completed.to_dict())
        self.assertNotIn("secret-source-id", serialized)
        self.assertNotIn("Secret Source", serialized)

    def test_clean_empty_partial_result_is_not_reported_as_complete(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        manager = Mock()
        manager.clean_empty.return_value = {
            "ok": True,
            "partial": True,
            "cleaned": 1,
            "scan_failures": 2,
            "delete_failures": 1,
        }
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ):
            result = clean_empty_guangya_organize_sources({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            result.data,
            {"cleaned": 1, "failed": 3, "source_count": 1},
        )
        self.assertNotIn("secret-source-id", str(result.to_dict()))

    def test_clean_empty_core_and_manager_preserve_failure_counts(self):
        client = Mock()
        client.list_dir.side_effect = OSError("secret scan failure")
        report = Organizer(client=client).clean_empty_dirs(
            "secret-source",
            with_report=True,
        )
        self.assertEqual(
            report,
            {"cleaned": 0, "delete_failures": 0, "scan_failures": 1},
        )

        task_manager = OrganizeTaskManager()
        task_manager._lock = Mock()
        task_manager._lock.acquire.return_value = True
        organizer = Mock()
        organizer.clean_empty_dirs.return_value = {
            "cleaned": 2,
            "scan_failures": 1,
            "delete_failures": 1,
        }
        sources = [{"id": "secret-source", "name": "Secret Source"}]
        with patch("app.modules.organize_tasks.Organizer", return_value=organizer):
            aggregated = task_manager.clean_empty(sources)

        self.assertTrue(aggregated["ok"])
        self.assertTrue(aggregated["partial"])
        self.assertEqual(aggregated["cleaned"], 2)
        self.assertEqual(aggregated["scan_failures"], 1)
        self.assertEqual(aggregated["delete_failures"], 1)
        organizer.clean_empty_dirs.assert_called_once_with(
            "secret-source",
            with_report=True,
            protected_source_ids={"secret-source"},
        )
        task_manager._lock.release.assert_called_once_with()

    def test_clean_empty_protects_nested_configured_source_roots(self):
        client = Mock()

        def list_dir(dir_id):
            if dir_id == "parent":
                return [
                    SimpleNamespace(
                        is_dir=True, file_id="child", etag="child-v1", updated_at=100,
                    ),
                    SimpleNamespace(
                        is_dir=True, file_id="ordinary-empty", etag="empty-v1", updated_at=200,
                    ),
                ]
            if dir_id == "ordinary-empty":
                return []
            if dir_id == "child":
                raise AssertionError("嵌套来源根目录不应由父来源递归扫描")
            return []

        client.list_dir.side_effect = list_dir
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v2", updated_at=201,
        )
        organizer = Organizer(client=client)
        with patch("app.modules.organize.execute_recycle_bin_delete") as delete:
            report = organizer.clean_empty_dirs(
                "parent",
                with_report=True,
                protected_source_ids={"parent", "child"},
            )

        self.assertEqual(
            report,
            {"cleaned": 1, "delete_failures": 0, "scan_failures": 0},
        )
        delete.assert_called_once()
        self.assertEqual(delete.call_args.kwargs["candidate"].file_id, "ordinary-empty")
        delete.call_args.kwargs["delete_operation"]()
        client.delete_empty_directory.assert_called_once_with(
            "ordinary-empty",
            expected_etag="empty-v2",
            expected_updated_at=201,
        )

    def test_normal_organize_cleanup_refreshes_current_directory_version(self):
        child = SimpleNamespace(
            is_dir=True,
            file_id="ordinary-empty",
            name="Empty",
            etag="empty-v7",
            updated_at=700,
        )
        client = Mock()
        client.supports_atomic_empty_directory_delete = True
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v8", updated_at=701,
        )
        client.list_dir.side_effect = lambda dir_id: [child] if dir_id == "source" else []
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=True,
            link_strm=False,
            notify_enabled=False,
        )

        with patch("app.modules.organize.execute_recycle_bin_delete") as delete:
            _plans, stats = Organizer(client=client).organize(
                "source",
                rules,
                dry_run=False,
                post_actions=False,
            )

        self.assertEqual(stats["empty_dirs_cleaned"], 1)
        delete.assert_called_once()
        candidate = delete.call_args.kwargs["candidate"]
        self.assertEqual(candidate.file_id, "ordinary-empty")
        delete.call_args.kwargs["delete_operation"]()
        client.delete_empty_directory.assert_called_once_with(
            "ordinary-empty",
            expected_etag="empty-v8",
            expected_updated_at=701,
        )

    def test_normal_organize_skips_nested_protected_source_root(self):
        nested = SimpleNamespace(
            is_dir=True, file_id="child-source", name="Nested", etag="nested-v1",
            updated_at=100,
        )
        ordinary = SimpleNamespace(
            is_dir=True, file_id="ordinary-empty", name="Empty", etag="empty-v1",
            updated_at=200,
        )
        client = Mock()
        client.supports_atomic_empty_directory_delete = True
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v2", updated_at=201,
        )

        def list_dir(dir_id):
            if dir_id == "parent-source":
                return [nested, ordinary]
            if dir_id == "child-source":
                raise AssertionError("嵌套来源根目录不应被父来源扫描或清理")
            return []

        client.list_dir.side_effect = list_dir
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=True,
            link_strm=False,
            notify_enabled=False,
        )
        with patch("app.modules.organize.execute_recycle_bin_delete") as delete:
            _plans, stats = Organizer(client=client).organize(
                "parent-source",
                rules,
                dry_run=False,
                post_actions=False,
                protected_source_ids={"parent-source", "child-source"},
            )

        self.assertEqual(stats["empty_dirs_cleaned"], 1)
        delete.assert_called_once()
        self.assertEqual(
            delete.call_args.kwargs["candidate"].file_id,
            "ordinary-empty",
        )
        self.assertNotIn("child-source", [call.args[0] for call in client.list_dir.call_args_list])

    def test_clean_empty_missing_version_is_reported_and_not_deleted(self):
        client = Mock()
        client.list_dir.side_effect = [
            [SimpleNamespace(is_dir=True, file_id="empty", etag="", updated_at=0)],
            [],
            [],
        ]
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="", updated_at=0,
        )

        report = Organizer(client=client).clean_empty_dirs("source", with_report=True)

        self.assertEqual(
            report,
            {"cleaned": 0, "delete_failures": 1, "scan_failures": 0},
        )
        client.delete_empty_directory.assert_not_called()

    def test_clean_empty_confirm_rejects_toctou_source_change(self):
        original_sources = [{"id": "source-old", "name": "Old Source"}]
        current_sources = list(original_sources)
        replacement_sources = [{"id": "source-new", "name": "New Source"}]
        calls = 0

        def configured_sources():
            nonlocal calls, current_sources
            calls += 1
            snapshot = list(current_sources)
            if calls == 2:
                current_sources = list(replacement_sources)
            return snapshot

        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        manager.clean_empty.return_value = {
            "ok": True,
            "cleaned": 1,
            "scan_failures": 0,
            "delete_failures": 0,
        }
        service = AgentOrchestrator(build_tool_registry())
        with patch(
            "app.agent.organize_actions._configured_sources",
            side_effect=configured_sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ):
            prepared = service.prepare(
                "guangya.organize.clean_empty",
                {},
                owner="owner-a",
            )
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(
                    prepared["confirmation"]["confirmation_id"],
                    owner="owner-a",
                )

        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.clean_empty.assert_not_called()
        self.assertEqual(calls, 3)

    def test_clean_empty_confirmation_rejects_credential_generation_change(self):
        sources = [{"id": "source", "name": "Source"}]
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        service = AgentOrchestrator(build_tool_registry())
        clients = [
            _atomic_clean_client(credential_generation=1),
            _atomic_clean_client(credential_generation=2),
        ]
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            side_effect=clients,
        ):
            prepared = service.prepare(
                "guangya.organize.clean_empty",
                {},
                owner="owner-a",
            )
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(
                    prepared["confirmation"]["confirmation_id"],
                    owner="owner-a",
                )

        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.clean_empty.assert_not_called()

    def test_clean_empty_explicit_empty_config_is_rejected(self):
        values = {
            "GY_ORGANIZE_SOURCE_DIRS": "[]",
        }
        manager = Mock()
        service = AgentOrchestrator(build_tool_registry())
        with patch(
            "app.agent.organize_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            with self.assertRaises(AgentToolError) as blocked:
                service.prepare(
                    "guangya.organize.clean_empty",
                    {},
                    owner="owner-a",
                )

        self.assertEqual(blocked.exception.code, "precondition_failed")
        manager.clean_empty.assert_not_called()

    def test_clean_empty_preview_rejects_missing_login_and_running_task(self):
        manager = Mock()
        sources = [{"id": "source", "name": "Source"}]
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=[],
        ):
            missing = preview_guangya_organize_clean_empty({})
        manager.task_status.return_value = {"status": "running"}
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            running = preview_guangya_organize_clean_empty({})
        manager.task_status.return_value = {"status": "idle"}
        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=SimpleNamespace(logged_in=False),
        ):
            logged_out = preview_guangya_organize_clean_empty({})

        self.assertEqual(missing.status, "not_configured")
        self.assertEqual(running.status, "conflict")
        self.assertEqual(logged_out.status, "not_configured")

    def test_preview_is_dry_run_bounded_and_sanitized(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        rules = OrganizeRules(
            target_dir_id="secret-target-id",
            clean_empty=True,
            recycle_replaced_enabled=True,
            link_strm=True,
        )
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        organizer = Mock()
        organizer.organize.return_value = (
            [_preview_plan("安全标题")],
            _preview_stats(),
        )
        client = _atomic_clean_client()

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=client,
        ), patch(
            "app.agent.organize_actions.Organizer",
            return_value=organizer,
        ):
            result = preview_guangya_organize({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.data["source_count"], 1)
        self.assertEqual(result.data["sampled_files"], 1)
        self.assertEqual(result.data["samples"][0]["title"], "安全标题")
        organizer.organize.assert_called_once_with(
            "secret-source-id",
            rules,
            dry_run=True,
            max_files=100,
            post_actions=False,
            protected_source_ids={"secret-source-id"},
        )
        manager.start.assert_not_called()
        serialized = str(result.to_dict())
        for secret in ("secret-source-id", "Secret Source", "secret-target-id"):
            self.assertNotIn(secret, serialized)

    def test_preview_protects_every_configured_source_root(self):
        sources = [
            {"id": "parent-source", "name": "Parent"},
            {"id": "child-source", "name": "Child"},
        ]
        rules = OrganizeRules(target_dir_id="target")
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        organizer = Mock()
        organizer.organize.side_effect = [
            ([], _preview_stats(total=1)),
            ([], _preview_stats(total=1)),
        ]

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ), patch(
            "app.agent.organize_actions.Organizer",
            return_value=organizer,
        ):
            result = preview_guangya_organize({})

        self.assertTrue(result.ok)
        self.assertEqual(organizer.organize.call_count, 2)
        for call in organizer.organize.call_args_list:
            self.assertEqual(
                call.kwargs["protected_source_ids"],
                {"parent-source", "child-source"},
            )

    def test_incomplete_scan_and_empty_confirmation_do_not_create_action_preview(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target-id")
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        organizer = Mock()
        organizer.organize.return_value = (
            [],
            _preview_stats(total=0, scan_errors=["secret-source-id: /private/path failed"]),
        )

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ), patch(
            "app.agent.organize_actions.Organizer",
            return_value=organizer,
        ):
            incomplete = preview_guangya_organize_run_once({})

        self.assertFalse(incomplete.ok)
        self.assertEqual(incomplete.status, "inconclusive")
        self.assertEqual(incomplete.data["scan_errors"], 1)
        self.assertNotIn("secret-source-id", str(incomplete.to_dict()))
        self.assertNotIn("/private/path", str(incomplete.to_dict()))

        organizer.organize.return_value = ([], _preview_stats(total=0))
        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ), patch(
            "app.agent.organize_actions.Organizer",
            return_value=organizer,
        ):
            empty = preview_guangya_organize_run_once({})

        self.assertFalse(empty.ok)
        self.assertEqual(empty.status, "no_changes")

    def test_confirmed_run_uses_manual_trigger_and_hides_task_identifiers(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target-id")
        manager = Mock()
        manager.start.return_value = {
            "ok": True,
            "task_id": "secret-task-id",
            "message": "/private/path",
        }

        client = _atomic_clean_client(credential_generation=7)
        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=client,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            result = run_guangya_organize_once({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data, {"trigger_type": "manual", "source_count": 1})
        manager.start.assert_called_once_with(
            sources, rules, trigger_type="manual", client=client,
            expected_credential_generation=7,
        )
        serialized = str(result.to_dict())
        for secret in (
            "secret-source-id",
            "Secret Source",
            "secret-target-id",
            "secret-task-id",
            "/private/path",
        ):
            self.assertNotIn(secret, serialized)

    def test_run_once_confirmation_preparer_uses_one_snapshot(self):
        sources = [{"id": "secret-source", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target")
        client = _atomic_clean_client(credential_generation=11)
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        organizer = Mock()
        organizer.organize.return_value = ([_preview_plan()], _preview_stats())

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ) as configured, patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=client,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.Organizer",
            return_value=organizer,
        ) as organizer_factory:
            preview, context = prepare_guangya_organize_run_once({})

        self.assertTrue(preview.ok)
        self.assertEqual(preview.status, "preview")
        self.assertRegex(context, r"^[0-9a-f]{64}$")
        configured.assert_called_once_with()
        organizer.organize.assert_called_once()
        organizer_factory.assert_called_once_with(client=client)

    def test_confirmed_run_rejects_ticket_after_credential_generation_changes(self):
        sources = [{"id": "secret-source", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target")
        manager = Mock()

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(credential_generation=7),
        ):
            expected = organize_confirmation_context({})

        with patch(
            "app.agent.organize_actions._configured_inputs",
            return_value=(sources, rules, ""),
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(credential_generation=8),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            with self.assertRaises(AgentToolError) as stale:
                run_guangya_organize_once_confirmed({}, expected)

        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.start.assert_not_called()

    def test_registry_exposes_preview_and_blocks_direct_write_execution(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["guangya.organize.preview"]["risk"], "read")
        self.assertEqual(capabilities["guangya.organize.run_once"]["risk"], "danger")
        self.assertTrue(capabilities["guangya.organize.run_once"]["requires_confirmation"])
        self.assertEqual(capabilities["guangya.organize.stop"]["risk"], "danger")
        self.assertTrue(capabilities["guangya.organize.stop"]["requires_confirmation"])
        self.assertEqual(capabilities["guangya.organize.clean_empty"]["risk"], "danger")
        self.assertTrue(capabilities["guangya.organize.clean_empty"]["requires_confirmation"])
        with self.assertRaises(AgentToolError) as blocked:
            registry.execute("guangya.organize.run_once", {})
        self.assertEqual(blocked.exception.code, "confirmation_required")
        with self.assertRaises(AgentToolError) as clean_blocked:
            registry.execute("guangya.organize.clean_empty", {})
        self.assertEqual(clean_blocked.exception.code, "confirmation_required")

    def test_stop_preview_and_confirmation_are_bound_to_current_task(self):
        current = {
            "id": "secret-task-a",
            "status": "running",
            "stoppable": True,
            "started_at": "2026-08-03 20:00:00",
        }
        manager = Mock()
        manager.task_status.side_effect = lambda: dict(current)
        manager.stop.return_value = {"ok": True, "message": "/secret/path"}

        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            preview = preview_guangya_organize_stop({})
            context = organize_stop_confirmation_context({})
            result = stop_guangya_organize_confirmed({}, context)

        self.assertTrue(preview.ok)
        self.assertEqual(preview.data, {"requested": True, "cooperative": True})
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data, {"accepted": True})
        manager.stop.assert_called_once_with(
            expected_task_id="secret-task-a",
            require_running=True,
        )
        combined = str(preview.to_dict()) + str(result.to_dict())
        self.assertNotIn("secret-task-a", combined)
        self.assertEqual(json.loads(context)["task_id"], "secret-task-a")
        self.assertNotIn("/secret/path", combined)

    def test_stop_confirmation_preparer_uses_one_atomic_snapshot(self):
        manager = Mock()
        manager.task_status.return_value = {
            "id": "secret-task-a",
            "status": "running",
            "stoppable": True,
            "started_at": "2026-08-03 20:00:00",
        }

        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            preview, context = prepare_guangya_organize_stop({})

        self.assertTrue(preview.ok)
        self.assertEqual(json.loads(context)["task_id"], "secret-task-a")
        manager.task_status.assert_called_once_with()

    def test_stop_confirmation_rejects_atomic_task_replacement(self):
        task = {
            "id": "secret-task-a",
            "status": "running",
            "stoppable": True,
            "started_at": "2026-08-03 20:00:00",
        }
        manager = Mock()
        manager.task_status.return_value = dict(task)
        manager.stop.return_value = {"ok": False, "error": "task changed"}
        context = json.dumps(
            {
                "task_id": task["id"],
                "status": task["status"],
                "stoppable": task["stoppable"],
                "started_at": task["started_at"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            with self.assertRaises(AgentToolError) as stale:
                stop_guangya_organize_confirmed({}, context)

        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.stop.assert_called_once_with(
            expected_task_id="secret-task-a",
            require_running=True,
        )

    def test_stop_confirmation_rejects_task_change_without_side_effect(self):
        current = {
            "id": "secret-task-a",
            "status": "running",
            "stoppable": True,
            "started_at": "2026-08-03 20:00:00",
        }
        manager = Mock()
        manager.task_status.side_effect = lambda: dict(current)
        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            context = organize_stop_confirmation_context({})
            current["id"] = "secret-task-b"
            with self.assertRaises(AgentToolError) as stale:
                stop_guangya_organize_confirmed({}, context)

        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.stop.assert_not_called()

    def test_stop_preview_requires_running_and_stoppable_task(self):
        manager = Mock()
        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            manager.task_status.return_value = {
                "id": "", "status": "idle", "stoppable": False, "started_at": "",
            }
            idle = preview_guangya_organize_stop({})
            manager.task_status.return_value = {
                "id": "secret", "status": "running", "stoppable": False, "started_at": "now",
            }
            atomic = preview_guangya_organize_stop({})

        self.assertFalse(idle.ok)
        self.assertEqual(idle.status, "conflict")
        self.assertFalse(atomic.ok)
        self.assertEqual(atomic.status, "conflict")


class GuangYaOrganizeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in ("guangya.organize.preview", "guangya.organize.status"):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    self.calls.append((tool, dict(arguments)))
                    or ToolResult(True, "success", tool)
                ),
                validator=_no_arguments,
            ))
        registry.register(ToolSpec(
            name="guangya.organize.run_once",
            description="run",
            risk=RiskLevel.DANGER,
            parameters={},
            handler=lambda arguments: (
                self.calls.append(("run", dict(arguments)))
                or ToolResult(True, "accepted", "done")
            ),
            validator=_no_arguments,
            requires_confirmation=True,
            preview_handler=lambda _arguments: ToolResult(
                True,
                "preview",
                "确认后执行",
            ),
            confirmation_context=lambda _arguments: "stable-context",
        ))
        registry.register(ToolSpec(
            name="guangya.organize.stop",
            description="stop",
            risk=RiskLevel.DANGER,
            parameters={},
            handler=lambda arguments: (
                self.calls.append(("stop", dict(arguments)))
                or ToolResult(True, "accepted", "stopping", data={"requested": True})
            ),
            validator=_no_arguments,
            requires_confirmation=True,
            preview_handler=lambda _arguments: ToolResult(
                True,
                "preview",
                "确认后停止",
            ),
            confirmation_context=lambda _arguments: "stable-stop-context",
        ))
        registry.register(ToolSpec(
            name="guangya.organize.clean_empty",
            description="clean empty",
            risk=RiskLevel.DANGER,
            parameters={},
            handler=lambda arguments: (
                self.calls.append(("clean_empty", dict(arguments)))
                or ToolResult(True, "completed", "cleaned", data={"cleaned": 1})
            ),
            validator=_no_arguments,
            requires_confirmation=True,
            preview_handler=lambda _arguments: ToolResult(True, "preview", "确认后清理"),
            confirmation_context=lambda _arguments: "stable-clean-context",
        ))
        self.service = AgentOrchestrator(registry)

    def test_preview_status_and_confirmed_run_intents_are_distinct(self):
        preview = self.service.query("先看看光鸭云盘会怎么整理")
        self.assertEqual(preview["tool_call"]["name"], "guangya.organize.preview")

        status = self.service.query("光鸭整理任务现在进度怎么样")
        self.assertEqual(status["tool_call"]["name"], "guangya.organize.status")

        anonymous = self.service.query("立即整理光鸭云盘")
        self.assertIsNone(anonymous["tool_call"])
        self.assertEqual(anonymous["result"]["status"], "unsupported")

        prepared = self.service.query("立即整理光鸭云盘", owner="owner-a")
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["confirmation"]["tool"], "guangya.organize.run_once")
        self.assertNotIn(("run", {}), self.calls)

        confirmed = self.service.confirm(
            prepared["confirmation"]["confirmation_id"],
            owner="owner-a",
        )
        self.assertEqual(confirmed["result"]["status"], "accepted")
        self.assertIn(("run", {}), self.calls)

        stop_prepared = self.service.query("停止光鸭云盘整理任务", owner="owner-a")
        self.assertEqual(stop_prepared["mode"], "confirmation_required")
        self.assertEqual(stop_prepared["confirmation"]["tool"], "guangya.organize.stop")
        self.assertNotIn(("stop", {}), self.calls)
        stop_confirmed = self.service.confirm(
            stop_prepared["confirmation"]["confirmation_id"],
            owner="owner-a",
        )
        self.assertEqual(stop_confirmed["result"]["status"], "accepted")
        self.assertIn(("stop", {}), self.calls)

        clean_anonymous = self.service.query("清理光鸭整理源空目录")
        self.assertEqual(clean_anonymous["result"]["status"], "unsupported")
        clean_prepared = self.service.query("清理光鸭整理源空目录", owner="owner-a")
        self.assertEqual(clean_prepared["mode"], "confirmation_required")
        self.assertEqual(
            clean_prepared["confirmation"]["tool"],
            "guangya.organize.clean_empty",
        )
        self.assertNotIn(("clean_empty", {}), self.calls)
        clean_confirmed = self.service.confirm(
            clean_prepared["confirmation"]["confirmation_id"],
            owner="owner-a",
        )
        self.assertEqual(clean_confirmed["result"]["status"], "completed")
        self.assertIn(("clean_empty", {}), self.calls)

    def test_run_intent_is_conservative(self):
        self.assertTrue(is_guangya_organize_run_message("立即整理光鸭云盘"))
        for message in (
            "不要立即整理光鸭云盘",
            "不用启动光鸭整理",
            "能否立即整理光鸭云盘",
            "立即整理光鸭云盘吗？",
            "如何启动光鸭云盘整理？",
            "如果整理光鸭云盘会怎样",
            "查看光鸭整理状态",
        ):
            self.assertFalse(is_guangya_organize_run_message(message), message)

    def test_stop_intent_is_conservative(self):
        self.assertTrue(is_guangya_organize_stop_message("取消云盘整理任务"))
        self.assertTrue(is_guangya_organize_stop_message("中止光鸭整理"))
        for message in (
            "能不能停止光鸭整理",
            "不要停止光鸭整理",
            "停止光鸭整理后再启动",
            "如何停止云盘整理",
            "光鸭云盘整理停止了吗",
            "取消光鸭云盘定时整理",
            "停止自动云盘整理",
            "停止普通任务",
        ):
            self.assertFalse(is_guangya_organize_stop_message(message), message)

    def test_clean_empty_intent_is_conservative(self):
        self.assertTrue(is_guangya_organize_clean_empty_message("清理光鸭整理源空目录"))
        self.assertTrue(is_guangya_organize_clean_empty_message("删除云盘源目录中的空文件夹"))
        for message in (
            "能不能清理光鸭空目录",
            "不要删除云盘空目录",
            "如何清理网盘空文件夹",
            "光鸭空目录清理了吗",
            "删除云盘空目录的命令是什么",
            "光鸭空目录有没有删除",
            "删除云盘里的空文件夹",
            "清理网盘空目录",
            "自动清理云盘空目录",
            "清理本地空目录",
            "清理光鸭整理空目录后还能恢复吗",
            "删除云盘整理来源空目录之后风险大吗",
            "清理光鸭整理空目录有什么影响呢",
        ):
            self.assertFalse(is_guangya_organize_clean_empty_message(message), message)
        self.assertTrue(is_guangya_organize_clean_empty_message("清理光鸭整理源空目录吧"))


class GuangYaOrganizeActionAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    @staticmethod
    def _organize_patches(current, manager, organizer):
        current.setdefault("client", _atomic_clean_client(credential_generation=7))
        return (
            patch(
                "app.agent.organize_actions._configured_inputs",
                side_effect=lambda: (current["sources"], current["rules"], ""),
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=current["client"],
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager",
                return_value=manager,
            ),
            patch(
                "app.agent.organize_actions.Organizer",
                return_value=organizer,
            ),
        )

    def test_query_prepare_confirm_replay_and_direct_execution_gate(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        current = {
            "sources": [{"id": "secret-source-id", "name": "Secret Source"}],
            "rules": OrganizeRules(target_dir_id="secret-target-id"),
        }
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        manager.start.return_value = {
            "ok": True,
            "task_id": "secret-task-id",
            "message": "/private/path",
        }
        organizer = Mock()
        organizer.organize.return_value = (
            [_preview_plan("安全标题")],
            _preview_stats(),
        )
        patches = self._organize_patches(current, manager, organizer)

        with patches[0], patches[1], patches[2], patches[3]:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "立即整理光鸭云盘"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["mode"], "confirmation_required")
            confirmation_id = prepared.json()["confirmation"]["confirmation_id"]
            manager.start.assert_not_called()

            direct = self.client.post(
                "/api/agent/tools/guangya.organize.run_once",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)
            manager.start.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "accepted")
            manager.start.assert_called_once_with(
                current["sources"],
                current["rules"],
                trigger_type="manual",
                client=current["client"],
                expected_credential_generation=7,
            )

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertEqual(manager.start.call_count, 1)

        combined = prepared.text + direct.text + confirmed.text + replay.text
        for secret in (
            "secret-source-id",
            "Secret Source",
            "secret-target-id",
            "secret-task-id",
            "/private/path",
        ):
            self.assertNotIn(secret, combined)

    def test_clean_empty_query_confirm_replay_and_source_change_gate(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        current = {
            "sources": [{"id": "secret-source-id", "name": "Secret Source"}],
        }
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        manager.clean_empty.return_value = {
            "ok": True,
            "cleaned": 2,
            "sources": [{**current["sources"][0], "cleaned": 2}],
        }

        with patch(
            "app.agent.organize_actions._configured_sources",
            side_effect=lambda: list(current["sources"]),
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ):
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "清理光鸭整理源空目录"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["mode"], "confirmation_required")
            self.assertEqual(
                prepared.json()["confirmation"]["tool"],
                "guangya.organize.clean_empty",
            )
            confirmation_id = prepared.json()["confirmation"]["confirmation_id"]
            manager.clean_empty.assert_not_called()

            direct = self.client.post(
                "/api/agent/tools/guangya.organize.clean_empty",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)
            manager.clean_empty.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "completed")
            self.assertEqual(
                confirmed.json()["result"]["data"],
                {"cleaned": 2, "failed": 0, "source_count": 1},
            )
            manager.clean_empty.assert_called_once()
            self.assertEqual(manager.clean_empty.call_args.args, (current["sources"],))
            self.assertTrue(manager.clean_empty.call_args.kwargs["client"].logged_in)

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertEqual(manager.clean_empty.call_count, 1)

            stale_prepare = self.client.post(
                "/api/agent/actions/guangya.organize.clean_empty/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(stale_prepare.status_code, 200, stale_prepare.text)
            current["sources"] = [
                {"id": "secret-source-two", "name": "Secret Source Two"},
            ]
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001",
                    "confirmation_id": stale_prepare.json()["confirmation"]["confirmation_id"]
                },
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(manager.clean_empty.call_count, 1)

        combined = prepared.text + direct.text + confirmed.text + replay.text + stale.text
        for secret in ("secret-source-id", "Secret Source", "secret-source-two"):
            self.assertNotIn(secret, combined)

    def test_clean_empty_query_and_prepare_share_one_rate_limit_budget(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        sources = [{"id": "source", "name": "Source"}]
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}

        with patch(
            "app.agent.organize_actions._configured_sources",
            return_value=sources,
        ), patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ), patch(
            "app.agent.organize_actions.GuangYaClient",
            return_value=_atomic_clean_client(),
        ):
            for _ in range(4):
                response = self.client.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": "清理光鸭整理源空目录"},
                )
                self.assertEqual(response.status_code, 200, response.text)

            limited = self.client.post(
                "/api/agent/actions/guangya.organize.clean_empty/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )

        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.json()["error"], "Agent 请求过于频繁，请稍后重试")

    def test_stop_query_prepare_confirm_replay_and_stale_task_gate(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        current = {
            "id": "secret-task-a",
            "status": "running",
            "stoppable": True,
            "started_at": "2026-08-03 20:00:00",
        }
        manager = Mock()
        manager.task_status.side_effect = lambda: dict(current)
        manager.stop.return_value = {"ok": True, "message": "/private/path"}

        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=manager,
        ):
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "停止光鸭云盘整理任务"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["mode"], "confirmation_required")
            self.assertEqual(
                prepared.json()["confirmation"]["tool"],
                "guangya.organize.stop",
            )
            confirmation_id = prepared.json()["confirmation"]["confirmation_id"]
            manager.stop.assert_not_called()

            direct = self.client.post(
                "/api/agent/tools/guangya.organize.stop",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "accepted")
            manager.stop.assert_called_once_with(
                expected_task_id="secret-task-a",
                require_running=True,
            )

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertEqual(manager.stop.call_count, 1)

            current.update({"id": "secret-task-b", "status": "running"})
            stale_prepare = self.client.post(
                "/api/agent/actions/guangya.organize.stop/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(stale_prepare.status_code, 200, stale_prepare.text)
            current["status"] = "stopping"
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001",
                    "confirmation_id": stale_prepare.json()["confirmation"]["confirmation_id"]
                },
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(manager.stop.call_count, 1)

        combined = prepared.text + direct.text + confirmed.text + replay.text + stale.text
        for secret in ("secret-task-a", "secret-task-b", "/private/path"):
            self.assertNotIn(secret, combined)

    def test_explicit_prepare_rejects_arguments_and_configuration_change(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        current = {
            "sources": [{"id": "source-one", "name": "One"}],
            "rules": OrganizeRules(target_dir_id="target-one"),
        }
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        manager.start.return_value = {"ok": True, "task_id": "must-not-run"}
        organizer = Mock()
        organizer.organize.return_value = (
            [_preview_plan()],
            _preview_stats(),
        )
        patches = self._organize_patches(current, manager, organizer)

        with patches[0], patches[1], patches[2], patches[3]:
            rejected = self.client.post(
                "/api/agent/actions/guangya.organize.run_once/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"source": "attacker-controlled"}},
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)

            prepared = self.client.post(
                "/api/agent/actions/guangya.organize.run_once/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            confirmation_id = prepared.json()["confirmation"]["confirmation_id"]
            current["rules"] = OrganizeRules(
                target_dir_id="target-two",
                clean_empty=False,
            )

            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertIn("配置已变化", stale.json()["error"])
            manager.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
