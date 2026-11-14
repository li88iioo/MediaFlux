"""整理拆分后的类型同一性、规则入口与共享运行态契约。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import inspect
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from app.modules import organize, organize_identity, organize_models, organize_results, organize_rules
from app.modules.organize_runtime import OrganizeTaskRuntime
from app.modules.scraper import MatchResult


class OrganizeModuleBoundaryTests(unittest.TestCase):
    def test_plan_confirmation_and_matched_transitions_keep_position_evidence(self):
        for rejected in (None, [], ["existing"], ["constraint"]):
            with self.subTest(rejected=rejected):
                match = MatchResult(tmdb_id="42", media_type="tv", metadata={"identity": "keep"})
                setattr(match, "rejected_constraints", rejected.copy() if isinstance(rejected, list) else None)
                plan = organize_models.OrganizePlan("video", "A.mkv", "Season", match=match, season=2, episode=3)
                plan.require_confirmation("needs confirmation", constraint="constraint")
                plan.require_confirmation("needs confirmation", constraint="constraint")
                self.assertEqual((plan.action, plan.note, match.need_confirm, match.status, match.error),
                    ("skip", "needs confirmation", True, "low_confidence", "needs confirmation"))
                expected = None if rejected is None else list(dict.fromkeys([*rejected, "constraint"]))
                self.assertEqual(getattr(match, "rejected_constraints"), expected)
                plan.mark_matched()
                plan.mark_matched()
                self.assertEqual((match.need_confirm, match.status, match.error), (False, "matched", ""))
                self.assertEqual((plan.season, plan.episode, plan.action, plan.note), (2, 3, "skip", "needs confirmation"))
                self.assertEqual(match.metadata, {"identity": "keep"})
                self.assertEqual(getattr(match, "rejected_constraints"), expected)

    def test_inventory_owner_reuses_revision_and_refreshes_at_write_budget(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        runtime = OrganizeTaskRuntime()
        item = SimpleNamespace(file_id="video", name="A.mkv", is_dir=False)
        listing = Mock(return_value=[item])
        revision = Mock(return_value=("etag", 1))
        stats = {}
        first = runtime.load_target_inventory("target", list_files=listing, read_revision=revision, stats=stats)
        second = runtime.load_target_inventory("target", list_files=listing, read_revision=revision, stats=stats)
        self.assertIs(first, second)
        self.assertEqual(first.evidence_names, {"video": "A.mkv"})
        self.assertEqual((listing.call_count, revision.call_count), (1, 3))
        self.assertEqual(stats["target_inventory_cache_hits"], 1)
        first.writes_since_refresh = 32
        refreshed = runtime.load_target_inventory("target", list_files=listing, read_revision=revision, stats=stats)
        self.assertIsNot(first, refreshed)
        self.assertEqual(refreshed.writes_since_refresh, 0)
        self.assertEqual((listing.call_count, revision.call_count), (2, 5))
        self.assertEqual(stats["target_inventory_refreshes"], 2)

    def test_missing_or_unstable_inventory_revision_never_reuses_unverified_files(self):
        from unittest.mock import Mock

        runtime = OrganizeTaskRuntime()
        listing = Mock(return_value=[])
        revision = Mock(return_value=None)
        stats = {}
        for _ in range(2):
            runtime.load_target_inventory("missing-revision", list_files=listing, read_revision=revision, stats=stats)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(stats["target_revision_fallback_refreshes"], 1)
        revision.side_effect = [("a", 1), ("b", 2), ("c", 3)]
        with self.assertRaisesRegex(RuntimeError, "持续变化"):
            runtime.load_target_inventory("changing", list_files=listing, read_revision=revision, stats=stats)
        self.assertIsNone(runtime.get_inventory("changing"))
        self.assertEqual(stats["target_inventory_unstable_retries"], 1)
        self.assertEqual(stats["target_inventory_unstable_failures"], 1)
        revision.side_effect = None
        revision.return_value = ("stable", 4)
        recovered = runtime.load_target_inventory("changing", list_files=listing, read_revision=revision, stats=stats)
        self.assertEqual(recovered.revision, ("stable", 4))

    def test_models_rules_and_identity_are_direct_exports_not_parallel_implementations(self):
        for module in (organize_models, organize_rules, organize_identity):
            for name, value in inspect.getmembers(module):
                if not (inspect.isfunction(value) or inspect.isclass(value)):
                    continue
                if value.__module__ != module.__name__:
                    continue
                with self.subTest(module=module.__name__, name=name):
                    self.assertIs(getattr(organize, name), value)
        for name in ("_format_scan_summary", "_format_phase_timing"):
            self.assertIs(getattr(organize, name), getattr(organize_results, name))
        for name in ("DEFAULT_ORGANIZE_VIDEO_EXTS", "DEFAULT_ORGANIZE_METADATA_EXTS"):
            self.assertIs(getattr(organize, name), getattr(organize_rules, name))

    def test_leaf_modules_can_load_before_the_coordinator(self):
        orders = (
            ("organize_models", "organize_rules", "organize_identity"),
            ("organize_identity", "organize_rules", "organize_models"),
        )
        for order in orders:
            with self.subTest(order=order):
                code = f'''
from importlib import import_module
import sys
from unittest.mock import patch
with patch("sqlite3.connect", side_effect=AssertionError("import opened database")):
    for name in {order!r}:
        import_module("app.modules." + name)
    assert "app.modules.organize" not in sys.modules
    from app.modules import organize, organize_models, organize_rules
assert organize.OrganizeContext is organize_models.OrganizeContext
assert organize.OrganizeRules is organize_rules.OrganizeRules
'''
                completed = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "MEDIAFLUX_DISABLE_FILE_LOGGING": "1"},
                    capture_output=True, text=True, timeout=30, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_context_replacement_preserves_shared_runtime_lock_executor_and_cancellation(self):
        runtime = OrganizeTaskRuntime()
        event = threading.Event()
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            context = organize_models.OrganizeContext(
                source_dir_id="source-a", cancel_event=event, execution_lock=lock,
                planning_executor=executor, task_runtime=runtime,
            )
            other = replace(context, source_dir_id="source-b", post_actions=False)
            self.assertIs(other.task_runtime, runtime)
            self.assertIs(other.execution_lock, lock)
            self.assertIs(other.planning_executor, executor)
            self.assertTrue(other.probe_cache_only)
            self.assertFalse(replace(other, dry_run=False).probe_cache_only)
            self.assertFalse(other.cancelled())
            event.set()
            self.assertTrue(context.cancelled())
            self.assertTrue(other.cancelled())
            self.assertEqual(executor.submit(lambda: "still-owned-by-caller").result(), "still-owned-by-caller")

    def test_plan_defaults_and_positional_fields_remain_shared_across_entrypoints(self):
        plan = organize.OrganizePlan("video-1", "Movie.mkv", "/source/Movie.mkv", "source", 42)
        other = organize_models.OrganizePlan("video-2", "Other.mkv", "/source/Other.mkv")
        assert isinstance(plan, organize_models.OrganizePlan)
        self.assertEqual((plan.original_parent_id, plan.size), ("source", 42))
        self.assertEqual(plan.variant, other.variant)
        self.assertIsNot(plan.variant, other.variant)
        self.assertEqual(other.action, "move")
        self.assertEqual(other.source_group_id, "")

    def test_config_rules_and_snapshots_use_one_policy_without_persisting_server_secret(self):
        with patch.object(organize_rules, "get", side_effect=lambda key, default="": default), patch.object(
            organize_rules, "get_bool", side_effect=lambda key, default=False: default,
        ), patch.object(organize_rules, "get_int", side_effect=lambda key, default=0: default):
            rules = organize.OrganizeRules.from_config("target-dir")
        self.assertEqual(rules.target_dir_id, "target-dir")
        trusted = replace(rules, nsfw_metatube_token="fixture-current-secret")
        snapshot = organize.organize_rules_snapshot(trusted)
        self.assertNotIn("nsfw_metatube_token", snapshot)
        restored = organize_rules.restore_organize_rules_snapshot(
            {**snapshot, "nsfw_metatube_token": "fixture-stale-secret", "rename_enabled": False},
            trusted_rules=trusted,
        )
        self.assertEqual(restored.nsfw_metatube_token, "fixture-current-secret")
        self.assertTrue(restored.rename_enabled)
        self.assertFalse(organize.organize_rules_snapshot_changes(snapshot, restored))
        self.assertEqual(organize.organize_rules_snapshot(restored), snapshot)

    def test_duplicate_recycling_switch_is_frozen_and_invalidates_changed_previews(self):
        rules = organize_rules.OrganizeRules()
        self.assertTrue(rules.recycle_duplicates_enabled)
        snapshot = organize_rules.organize_rules_snapshot(rules)
        disabled = replace(rules, recycle_duplicates_enabled=False)
        self.assertEqual(
            organize_rules.organize_rules_snapshot_changes(snapshot, disabled),
            ["recycle_duplicates_enabled"],
        )
        disabled_snapshot = organize_rules.organize_rules_snapshot(disabled)
        restored = organize_rules.restore_organize_rules_snapshot(disabled_snapshot, trusted_rules=rules)
        self.assertFalse(restored.recycle_duplicates_enabled)
        self.assertEqual(organize_rules.organize_rules_snapshot_changes(disabled_snapshot, restored), [])
        self.assertEqual(
            organize_rules.organize_rules_snapshot_changes(disabled_snapshot, rules),
            ["recycle_duplicates_enabled"],
        )
        legacy_snapshot = {key: value for key, value in snapshot.items() if key != "recycle_duplicates_enabled"}
        self.assertEqual(organize_rules.organize_rules_snapshot_changes(legacy_snapshot, rules), [])
        self.assertEqual(
            organize_rules.organize_rules_snapshot_changes(legacy_snapshot, disabled),
            ["recycle_duplicates_enabled"],
        )
        self.assertFalse(organize_rules.restore_organize_rules_snapshot(
            legacy_snapshot, trusted_rules=disabled,
        ).recycle_duplicates_enabled)

    def test_automatic_confirmation_and_result_formatting_keep_their_contract(self):
        matched = MatchResult(status="matched", confidence=0.95, need_confirm=False)
        self.assertFalse(organize_rules.automatic_match_requires_confirmation(matched))
        self.assertTrue(organize.automatic_match_requires_confirmation(replace(matched, need_confirm=True)))
        self.assertTrue(organize.automatic_match_requires_confirmation(None))
        stats = {
            "total": 2, "matched": 1, "failed": 1,
            "scan_complete": False, "scan_limit_kind": "depth",
            "source_groups": [{"name": "Example"}],
            "scan_elapsed_seconds": 1.25, "total_elapsed_seconds": 2.5,
            "scan_list_dir_calls": 3,
        }
        self.assertEqual(
            organize._format_scan_summary(stats),
            "[Example] 共 2 个视频 · 已识别 1 · 失败 1 · 扫描不完整(depth) | 扫描=1.25s",
        )
        self.assertEqual(
            organize._format_phase_timing(stats),
            "scan=1.25s total=2.50s list_dir=3",
        )
