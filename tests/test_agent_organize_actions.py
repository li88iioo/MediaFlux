"""Media Agent 光鸭整理预览与确认执行测试。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agent.errors import AgentToolError
from app.agent.models import ToolResult
from app.agent.organize_actions import (
    _configured_sources,
    prepare_guangya_organize_run_once,
    prepare_guangya_organize_stop,
    preview_guangya_organize,
    run_guangya_organize_once_confirmed,
    stop_guangya_organize_confirmed,
)
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_tasks import OrganizeTaskManager


def _no_arguments(arguments):
    if arguments:
        raise AgentToolError("不接受参数")
    return {}


def _cleanup_preview_arguments(arguments):
    if set(arguments) - {"max_candidates", "scope"}:
        raise AgentToolError("不支持参数")
    return {
        "max_candidates": int(arguments.get("max_candidates", 500)),
        "scope": str(arguments.get("scope") or "all"),
    }


def _preview_plan(title: str = "安全标题", action: str = "move"):
    return SimpleNamespace(
        action=action, match=SimpleNamespace(title=title, year="2026", media_type="tv")
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
        supports_guarded_empty_directory_delete=True,
        supports_atomic_empty_directory_delete=True,
        delete_empty_directory=Mock(return_value=True),
    )


class GuangYaOrganizeActionTests(unittest.TestCase):
    def test_configured_sources_normalize_and_deduplicate(self):
        values = {
            "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"source-a","name":"A"},"source-b",{"id":"source-a","name":"Duplicate"}]'
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
        ), self.assertRaises(AgentToolError) as invalid:
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

    def test_confirmation_context_changes_with_configuration_and_credential_generation(
        self,
    ):
        sources = [{"id": "secret-source", "name": "Secret"}]
        current = {
            "rules": OrganizeRules(target_dir_id="secret-target"),
            "generation": 7,
        }

        def configured_inputs():
            return (sources, current["rules"], "")

        def client_factory():
            return SimpleNamespace(credential_generation=current["generation"])

        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                side_effect=configured_inputs,
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient", side_effect=client_factory
            ),
            patch(
                "app.agent.organize_actions._organize_preview_snapshot",
                return_value=ToolResult(True, "preview", "preview"),
            ),
        ):
            _preview, first = prepare_guangya_organize_run_once({})
            _preview, repeated = prepare_guangya_organize_run_once({})
            self.assertEqual(first, repeated)
            current["rules"] = OrganizeRules(
                target_dir_id="secret-target-two", clean_empty=False
            )
            _preview, rules_changed = prepare_guangya_organize_run_once({})
            current["rules"] = OrganizeRules(target_dir_id="secret-target")
            current["generation"] = 8
            _preview, credential_changed = prepare_guangya_organize_run_once({})
        self.assertNotEqual(first, rules_changed)
        self.assertNotEqual(first, credential_changed)
        for context in (first, rules_changed, credential_changed):
            self.assertNotIn("secret", context)
            self.assertRegex(context, "^[0-9a-f]{64}$")

    def test_clean_empty_core_and_manager_preserve_failure_counts(self):
        client = Mock()
        client.list_dir.side_effect = OSError("secret scan failure")
        report = Organizer(client=client).clean_empty_dirs(
            "secret-source", with_report=True
        )
        self.assertEqual(
            report, {"cleaned": 0, "delete_failures": 0, "scan_failures": 1}
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
            "secret-source", with_report=True, protected_source_ids={"secret-source"}
        )
        task_manager._lock.release.assert_called_once_with()

    def test_clean_empty_protects_nested_configured_source_roots(self):
        client = Mock()
        client.supports_guarded_empty_directory_delete = True

        def list_dir(dir_id):
            if dir_id == "parent":
                return [
                    SimpleNamespace(
                        is_dir=True, file_id="child", etag="child-v1", updated_at=100
                    ),
                    SimpleNamespace(
                        is_dir=True,
                        file_id="ordinary-empty",
                        etag="empty-v1",
                        updated_at=200,
                    ),
                ]
            if dir_id == "ordinary-empty":
                return []
            if dir_id == "child":
                raise AssertionError("嵌套来源根目录不应由父来源递归扫描")
            return []

        client.list_dir.side_effect = list_dir
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v2", updated_at=201
        )
        organizer = Organizer(client=client)
        with patch("app.modules.organize.execute_recycle_bin_delete") as delete:
            report = organizer.clean_empty_dirs(
                "parent", with_report=True, protected_source_ids={"parent", "child"}
            )
        self.assertEqual(
            report, {"cleaned": 1, "delete_failures": 0, "scan_failures": 0}
        )
        delete.assert_called_once()
        self.assertEqual(delete.call_args.kwargs["candidate"].file_id, "ordinary-empty")
        delete.call_args.kwargs["delete_operation"]()
        client.delete_empty_directory.assert_called_once_with(
            "ordinary-empty", expected_etag="empty-v2", expected_updated_at=201
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
        client.supports_guarded_empty_directory_delete = True
        client.supports_atomic_empty_directory_delete = True
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v8", updated_at=701
        )
        client.list_dir.side_effect = lambda dir_id: (
            [child] if dir_id == "source" else []
        )
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=True,
            link_strm=False,
            notify_enabled=False,
        )
        with patch("app.modules.organize.execute_recycle_bin_delete") as delete:
            _plans, stats = Organizer(client=client).organize(
                "source", rules, dry_run=False, post_actions=False
            )
        self.assertEqual(stats["empty_dirs_cleaned"], 1)
        delete.assert_called_once()
        candidate = delete.call_args.kwargs["candidate"]
        self.assertEqual(candidate.file_id, "ordinary-empty")
        delete.call_args.kwargs["delete_operation"]()
        client.delete_empty_directory.assert_called_once_with(
            "ordinary-empty", expected_etag="empty-v8", expected_updated_at=701
        )

    def test_normal_organize_skips_nested_protected_source_root(self):
        nested = SimpleNamespace(
            is_dir=True,
            file_id="child-source",
            name="Nested",
            etag="nested-v1",
            updated_at=100,
        )
        ordinary = SimpleNamespace(
            is_dir=True,
            file_id="ordinary-empty",
            name="Empty",
            etag="empty-v1",
            updated_at=200,
        )
        client = Mock()
        client.supports_guarded_empty_directory_delete = True
        client.supports_atomic_empty_directory_delete = True
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="empty-v2", updated_at=201
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
        self.assertEqual(delete.call_args.kwargs["candidate"].file_id, "ordinary-empty")
        self.assertNotIn(
            "child-source", [call.args[0] for call in client.list_dir.call_args_list]
        )

    def test_clean_empty_missing_version_is_reported_and_not_deleted(self):
        client = Mock()
        client.supports_guarded_empty_directory_delete = True
        client.list_dir.side_effect = [
            [SimpleNamespace(is_dir=True, file_id="empty", etag="", updated_at=0)],
            [],
            [],
        ]
        client.file_info.return_value = SimpleNamespace(
            is_dir=True, etag="", updated_at=0
        )
        report = Organizer(client=client).clean_empty_dirs("source", with_report=True)
        self.assertEqual(
            report, {"cleaned": 0, "delete_failures": 1, "scan_failures": 0}
        )
        client.delete_empty_directory.assert_not_called()

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
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch("app.agent.organize_actions.GuangYaClient", return_value=client),
            patch("app.agent.organize_actions.Organizer", return_value=organizer),
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
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=_atomic_clean_client(),
            ),
            patch("app.agent.organize_actions.Organizer", return_value=organizer),
        ):
            result = preview_guangya_organize({})
        self.assertTrue(result.ok)
        self.assertEqual(organizer.organize.call_count, 2)
        for call in organizer.organize.call_args_list:
            self.assertEqual(
                call.kwargs["protected_source_ids"], {"parent-source", "child-source"}
            )

    def test_incomplete_scan_and_empty_confirmation_do_not_create_action_preview(self):
        sources = [{"id": "secret-source-id", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target-id")
        manager = Mock()
        manager.task_status.return_value = {"status": "idle"}
        organizer = Mock()
        organizer.organize.return_value = (
            [],
            _preview_stats(
                total=0, scan_errors=["secret-source-id: /private/path failed"]
            ),
        )
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=_atomic_clean_client(),
            ),
            patch("app.agent.organize_actions.Organizer", return_value=organizer),
        ):
            incomplete, _context = prepare_guangya_organize_run_once({})
        self.assertFalse(incomplete.ok)
        self.assertEqual(incomplete.status, "inconclusive")
        self.assertEqual(incomplete.data["scan_errors"], 1)
        self.assertNotIn("secret-source-id", str(incomplete.to_dict()))
        self.assertNotIn("/private/path", str(incomplete.to_dict()))
        organizer.organize.return_value = ([], _preview_stats(total=0))
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=_atomic_clean_client(),
            ),
            patch("app.agent.organize_actions.Organizer", return_value=organizer),
        ):
            empty, _context = prepare_guangya_organize_run_once({})
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
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch("app.agent.organize_actions.GuangYaClient", return_value=client),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch(
                "app.agent.organize_actions._organize_preview_snapshot",
                return_value=ToolResult(True, "preview", "preview"),
            ),
        ):
            _preview, context = prepare_guangya_organize_run_once({})
            result = run_guangya_organize_once_confirmed({}, context)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data, {"trigger_type": "manual", "source_count": 1})
        manager.start.assert_called_once_with(
            sources,
            rules,
            trigger_type="manual",
            client=client,
            expected_credential_generation=7,
            take_client_ownership=True,
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
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ) as configured,
            patch("app.agent.organize_actions.GuangYaClient", return_value=client),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),
            patch(
                "app.agent.organize_actions.Organizer", return_value=organizer
            ) as organizer_factory,
        ):
            preview, context = prepare_guangya_organize_run_once({})
        self.assertTrue(preview.ok)
        self.assertEqual(preview.status, "preview")
        self.assertRegex(context, "^[0-9a-f]{64}$")
        configured.assert_called_once_with()
        organizer.organize.assert_called_once()
        organizer_factory.assert_called_once_with(client=client)

    def test_confirmed_run_rejects_ticket_after_credential_generation_changes(self):
        sources = [{"id": "secret-source", "name": "Secret Source"}]
        rules = OrganizeRules(target_dir_id="secret-target")
        manager = Mock()
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=_atomic_clean_client(credential_generation=7),
            ),
            patch(
                "app.agent.organize_actions._organize_preview_snapshot",
                return_value=ToolResult(True, "preview", "preview"),
            ),
        ):
            _preview, expected = prepare_guangya_organize_run_once({})
        with (
            patch(
                "app.agent.organize_actions._configured_inputs",
                return_value=(sources, rules, ""),
            ),
            patch(
                "app.agent.organize_actions.GuangYaClient",
                return_value=_atomic_clean_client(credential_generation=8),
            ),
            patch(
                "app.agent.organize_actions.get_organize_manager", return_value=manager
            ),self.assertRaises(AgentToolError) as stale
        ):
            run_guangya_organize_once_confirmed({}, expected)
        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.start.assert_not_called()

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
            "app.agent.organize_actions.get_organize_manager", return_value=manager
        ):
            preview, context = prepare_guangya_organize_stop({})
            result = stop_guangya_organize_confirmed({}, context)
        self.assertTrue(preview.ok)
        self.assertEqual(preview.data, {"requested": True, "cooperative": True})
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data, {"accepted": True})
        manager.stop.assert_called_once_with(
            expected_task_id="secret-task-a", require_running=True
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
            "app.agent.organize_actions.get_organize_manager", return_value=manager
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
            "app.agent.organize_actions.get_organize_manager", return_value=manager
        ), self.assertRaises(AgentToolError) as stale:
            stop_guangya_organize_confirmed({}, context)
        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.stop.assert_called_once_with(
            expected_task_id="secret-task-a", require_running=True
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
            "app.agent.organize_actions.get_organize_manager", return_value=manager
        ):
            _preview, context = prepare_guangya_organize_stop({})
            current["id"] = "secret-task-b"
            with self.assertRaises(AgentToolError) as stale:
                stop_guangya_organize_confirmed({}, context)
        self.assertEqual(stale.exception.code, "confirmation_stale")
        manager.stop.assert_not_called()

    def test_stop_preview_requires_running_and_stoppable_task(self):
        manager = Mock()
        with patch(
            "app.agent.organize_actions.get_organize_manager", return_value=manager
        ):
            manager.task_status.return_value = {
                "id": "",
                "status": "idle",
                "stoppable": False,
                "started_at": "",
            }
            idle, _context = prepare_guangya_organize_stop({})
            manager.task_status.return_value = {
                "id": "secret",
                "status": "running",
                "stoppable": False,
                "started_at": "now",
            }
            atomic, _context = prepare_guangya_organize_stop({})
        self.assertFalse(idle.ok)
        self.assertEqual(idle.status, "conflict")
        self.assertFalse(atomic.ok)
        self.assertEqual(atomic.status, "conflict")
