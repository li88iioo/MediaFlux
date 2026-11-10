"""自动清洗只扩展光鸭人工清洗入口；冻结、竞争及开写边界回归。"""

from __future__ import annotations

from contextlib import ExitStack
from typing import cast
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaFile
from app.modules import organize_confirmations as confirmations
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.organize import OrganizePlan, Organizer, OrganizeRules
from app.modules.organize_execution import execute_organize_plans
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_agent_nsfw_clean_review import clean_payload
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class CleanConfirmationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
        self.rules = OrganizeRules(
            target_dir_id="archive",
            nsfw_enabled=True,
            nsfw_source_ids='["source"]',
            clean_empty=True,
        ).for_source("source")
        self.payload = clean_payload()
        self.payload["rules"] = confirmations.organize_rules_snapshot(self.rules)
        self.payload["_notification_suppressed"] = True
        self.candidate = self.payload["candidates"][0]
        self.enabled = True
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(
                confirmations,
                "_recognition_review_is_enabled",
                return_value=True,
            )
        )
        self.stack.enter_context(
            patch(
                "app.modules.nsfw_clean_review.nsfw_clean_review_enabled",
                side_effect=lambda: self.enabled,
            )
        )
        self.stack.enter_context(
            patch.object(
                confirmations.OrganizeRules,
                "from_config",
                return_value=self.rules,
            )
        )
        self.stack.enter_context(
            patch.object(confirmations, "wake_confirmation_dispatcher")
        )

    def ticket(self, *, running=False, actor="agent"):
        token = "frozen-clean-test"
        db.create_organize_confirmation(
            token=token,
            fingerprint="clean-test",
            chat_id="100",
            source_name="source",
            directory_path="/待整理",
            payload=self.payload,
            expires_at=(
                datetime.now(timezone.utc).astimezone() + timedelta(hours=1)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        db.update_organize_confirmation(token, review_status="running")
        if running:
            db.claim_organize_confirmation(
                token, chat_id="100", selected_index=0, actor=actor
            )
            assert (db.claim_queued_organize_confirmation(token)) is not None
        return token

    def pending_manual(self):
        with db.get_conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM organize_confirmations WHERE status='pending'"
                )
            ]

    def test_disabled_scope_cannot_claim_but_human_still_can(self):
        token = self.ticket()
        self.enabled = False
        with self.assertRaises(DirectoryScrapeConflictError):
            confirmations.start_confirmation(token, 0, chat_id="100", actor="agent")
        get_organize_confirmation_result = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result is not None
        self.assertEqual(get_organize_confirmation_result["status"], "pending")
        manager = SimpleNamespace(
            start_operation=lambda *_a, **_kw: {"ok": True, "task_id": "t"}
        )
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            confirmations.start_confirmation(token, 0, chat_id="100", actor="human")
        get_organize_confirmation_result_2 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_2 is not None
        self.assertEqual(
            get_organize_confirmation_result_2["confirmation_actor"], "human"
        )
        self.enabled = True
        with self.assertRaisesRegex(ValueError, "人工"):
            confirmations.start_confirmation(token, 0, chat_id="100", actor="agent")

    def test_numeric_prefix_candidate_keeps_agent_authorization_boundary(self):
        self.payload = clean_payload("300MIUM-1474.mp4")
        self.payload["rules"] = confirmations.organize_rules_snapshot(self.rules)
        self.payload["_notification_suppressed"] = True
        self.candidate = self.payload["candidates"][0]
        token = self.ticket()
        self.enabled = False

        with self.assertRaises(DirectoryScrapeConflictError):
            confirmations.start_confirmation(token, 0, chat_id="100", actor="agent")
        get_organize_confirmation_result_3 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_3 is not None
        self.assertEqual(get_organize_confirmation_result_3["status"], "pending")

    def test_disable_queued_review_hands_off_without_network_or_auto_retry(self):
        token = self.ticket(running=True)
        self.enabled = False
        with (
            patch.object(confirmations, "GuangYaClient") as client,
            self.assertRaises(DirectoryScrapeConflictError),
        ):
            confirmations._execute_guangya_confirmation(
                token,
                self.payload,
                self.candidate,
                selected_index=0,
                chat_id="100",
                actor="agent",
            )
        client.assert_not_called()
        get_organize_confirmation_result_4 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_4 is not None
        self.assertEqual(get_organize_confirmation_result_4["status"], "failed")
        manual = self.pending_manual()
        self.assertEqual(len(manual), 1)
        self.assertNotEqual(manual[0]["review_status"], "pending")
        self.assertNotEqual(manual[0]["token"], token)

    def test_stale_rules_or_file_cannot_create_an_unusable_retry_ticket(self):
        from dataclasses import replace

        with patch.object(
            confirmations.OrganizeRules,
            "from_config",
            return_value=replace(self.rules, target_dir_id="changed"),
        ):
            self.assertFalse(
                confirmations._clean_confirmation_retry_is_current(self.payload, None)
            )
        remote = GuangYaFile(
            "file-0", "ABC-123.changed.mp4", False, size=1024, parent_id="parent"
        )
        self.assertFalse(
            confirmations._clean_confirmation_retry_is_current(
                self.payload,
                SimpleNamespace(file_info=lambda _: remote),
            )
        )
        token = self.ticket(running=True)
        with (
            patch.object(
                confirmations,
                "GuangYaClient",
                return_value=SimpleNamespace(file_info=lambda _: remote),
            ),
            self.assertRaises(DirectoryScrapeConflictError),
        ):
            confirmations._execute_guangya_confirmation(
                token,
                self.payload,
                self.candidate,
                selected_index=0,
                chat_id="100",
                actor="agent",
            )
        get_organize_confirmation_result_5 = db.get_organize_confirmation(token)
        assert get_organize_confirmation_result_5 is not None
        self.assertEqual(get_organize_confirmation_result_5["status"], "failed")
        self.assertEqual(self.pending_manual(), [])

    def run_worker(self, *, decision="new", revoke=False, fail_after_commit=False, actor="agent", provider="clean_title"):
        token = self.ticket(running=True, actor=actor)
        calls = []
        outer = self
        plan = SimpleNamespace(
            file_id="file-0", action="move", conflict_decision=decision
        )

        class FakeOrganizer(Organizer):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.guard = kwargs.get("before_plan_write")

            def _validate_target_outside_source(self, source_id, target_id):
                pass

            def organize(self, source_dir_id, rules, dry_run=True, max_files=0, cancel_event=None, **kwargs):
                calls.append((dry_run, rules.clean_empty))
                assert kwargs["source_member_ids"] == frozenset({"file-0"})
                assert kwargs["group_pipeline"] is False
                if not dry_run:
                    if revoke:
                        outer.enabled = False
                    if self.guard:
                        self.guard(plan, "commit")
                    if fail_after_commit:
                        raise RuntimeError("injected move failure")
                    cloud.children["parent"].remove("file-0")
                    remote.parent_id = "archive"
                    cloud.children["archive"].append("file-0")
                return [plan], {} if dry_run else {"moved": 1, "empty_dirs_cleaned": 0}

            @staticmethod
            def trigger_post_actions(*_args, **_kwargs):
                return None

        remote = GuangYaFile(
            "file-0", "ABC-123.mp4", False, size=1024, parent_id="parent"
        )
        cloud = _CloudTree()
        cloud.add_dir("parent", "source")
        cloud.add_dir("archive", "0")
        cloud.nodes["file-0"] = cast(SimpleNamespace, remote)
        cloud.children["parent"].append("file-0")
        self.cloud = cloud
        with (
            patch.object(confirmations, "GuangYaClient", return_value=cloud),
            patch("app.modules.organize_tasks.config.get", side_effect=lambda key, default="": (
                '[{"id":"source","name":"Source"}]' if key == "GY_ORGANIZE_SOURCE_DIRS" else default
            )),
            patch.object(download_requests_repo, "list_protected_guangya_staging_ids", return_value=[]),
            patch.object(confirmations, "_record_confirmation_learning", return_value=[]),
            patch.object(
                confirmations,
                "_resolve_guangya_confirmation_candidate",
                return_value=(object(), object(), {}, provider),
            ),
            patch.object(confirmations, "FixedMatchScraper", return_value=object()),
            patch.object(confirmations, "Organizer", FakeOrganizer),
        ):
            result = confirmations._execute_guangya_confirmation(
                token,
                self.payload,
                self.candidate,
                selected_index=0,
                chat_id="100",
                actor=actor,
            )
        return token, result, calls

    def test_success_cleans_only_empty_source_parent_and_labels_fallback(self):
        token, result, calls = self.run_worker()
        self.assertEqual(calls, [(True, True), (False, True)])
        self.assertEqual(self.cloud.deleted, ["parent"])
        self.assertEqual(result["stats"]["empty_dirs_cleaned"], 1)
        self.assertEqual(result["stats"]["moved"], 1)
        row = db.get_organize_confirmation(token)
        assert row is not None
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["confirmation_actor"], "agent")
        event = confirmations._confirmation_result_event(
            self.payload,
            self.candidate,
            {"moved": 1},
            actor="agent",
        )
        self.assertIn("Agent", str(event.title))
        self.assertIn("清洗入库 · 无完整元数据", str(event.fields))

    def test_human_clean_confirmation_cleans_source_parent(self):
        _, result, _ = self.run_worker(actor="human")
        self.assertEqual(self.cloud.deleted, ["parent"])
        self.assertEqual(result["stats"]["empty_dirs_cleaned"], 1)

    def test_human_video_confirmation_cleans_source_parent(self):
        _, result, _ = self.run_worker(actor="human", provider="tmdb")
        self.assertEqual(self.cloud.deleted, ["parent"])
        self.assertEqual(result["stats"]["empty_dirs_cleaned"], 1)

    def test_cleanup_toggle_off_also_keeps_agent_clean_source(self):
        self.rules.clean_empty = False
        self.payload["rules"] = confirmations.organize_rules_snapshot(self.rules)
        _, result, calls = self.run_worker()
        self.assertEqual(calls, [(True, False), (False, False)])
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(result["stats"]["moved"], 1)

    def test_agent_clean_download_uses_staging_finalizer_not_generic_cleanup(self):
        from app.modules.process_lock import CrossProcessLock

        request_id, _ = download_requests_repo.create_download_request("agent-clean-download", "magnet")
        download_requests_repo.update_download_request(
            request_id, targets="guangya", status="completed", gy_status="completed",
            gy_isolated=1, gy_target_dir="parent", gy_staging_parent_dir="source", gy_staging_name="parent",
            gy_staging_cleanup_status="retained", organize_started=1,
            organize_status="requires_manual", organize_task_id="clean-task",
            gy_task_id="cloud-task", gy_task_ids='["cloud-task"]',
        )
        self.payload.update(source_dir_id="parent", download_request_ids=[request_id], organize_task_id="clean-task")
        self.payload["_notification_suppressed"] = False
        lock = CrossProcessLock("guangya-organize", directory=self.test_db_path.parent)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with (
            patch.object(confirmations, "_cleanup_confirmed_source") as generic_cleanup,
            patch.object(confirmations, "_dispatch_due_confirmation_delivery"),
            patch("app.modules.organize_tasks.get_organize_manager", return_value=SimpleNamespace(_lock=lock)),
            patch("app.modules.telegram_download_lifecycle.publish_download_lifecycle"),
        ):
            token, result, _ = self.run_worker()
        generic_cleanup.assert_not_called()
        get_organize_confirmation_delivery_result = db.get_organize_confirmation_delivery(token)
        assert get_organize_confirmation_delivery_result is not None
        event = confirmations.deserialize_notification_event(get_organize_confirmation_delivery_result["event_json"])
        self.assertNotIn("已清理 0 个", str(event.fields))
        self.assertIn(("目录清理", "由下载流程核验收尾，结果见下载记录"), event.fields)
        self.assertEqual(result["stats"]["download_staging_policy"], "cleanup_empty")
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["organize_status"], "completed")
        self.assertEqual(row["gy_staging_cleanup_status"], "completed")
        self.assertEqual(self.cloud.deleted, ["parent"])
        self.assertNotIn("source", self.cloud.deleted)
        self.assertNotIn("archive", self.cloud.deleted)
        self.assertGreater(result["stats"]["empty_dirs_cleaned"], 0)

    def test_preflight_conflict_and_late_revocation_remain_manual(self):
        with self.assertRaises(DirectoryScrapeConflictError):
            self.run_worker(decision="replace")
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(len(self.pending_manual()), 1)

    def test_after_preflight_authorization_can_be_revoked(self):
        with self.assertRaises(DirectoryScrapeConflictError):
            self.run_worker(revoke=True)
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(len(self.pending_manual()), 1)

    def test_after_write_attempt_does_not_issue_blind_retry(self):
        with self.assertRaises(RuntimeError):
            self.run_worker(fail_after_commit=True)
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.pending_manual(), [])

    def real_execution(self, *, snapshot_hook=None, conflict="new", target_files=()):
        guard = confirmations._AgentCleanWriteBoundary(self.payload, self.candidate)
        client = Mock()
        client.list_dir.return_value = list(target_files)
        organizer = Organizer(client=client, recognition_enabled=False, before_plan_write=guard)
        plan = OrganizePlan(
            file_id="file-0",
            original_name="ABC-123.mp4",
            original_path="parent",
            original_parent_id="parent",
            size=1024,
            target_path="adult/ABC-123",
            new_name="ABC-123.cleaned.mp4",
            action="move",
            conflict_decision="new",
            match=MatchResult(
                title="ABC-123",
                media_type="movie",
                provider="clean_title",
                external_id="ABC-123",
            ),
        )
        stats = dict.fromkeys(
            (
                "moved",
                "renamed",
                "metadata_moved",
                "subtitle_moved",
                "stopped",
                "skipped",
                "conflict",
                "failed",
            ),
            0,
        )
        existing = (
            GuangYaFile("old", "ABC-123.old.mp4", False, size=100)
            if conflict == "replace"
            else None
        )
        with (
            patch.object(organizer, "_ensure_dir_chain", return_value="target"),
            patch.object(
                organizer, "_verify_remote_snapshot", side_effect=snapshot_hook
            ),
            patch.object(
                organizer,
                "_resolve_variant_conflict",
                return_value=(existing, conflict, ""),
            ),
        ):
            execute_organize_plans(organizer, [plan], self.rules, stats, {}, None)
        return guard, client, stats

    def test_last_snapshot_read_can_revoke_authorization_without_write(self):
        count = 0

        def snapshot(*_args, **_kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.enabled = False

        guard, client, stats = self.real_execution(snapshot_hook=snapshot)
        self.assertEqual(count, 2)
        self.assertFalse(guard.media_write_attempted)
        client.move.assert_not_called()
        client.rename.assert_not_called()
        client.delete.assert_not_called()
        self.assertEqual(stats["failed"], 1)

    def test_last_snapshot_read_failure_never_counts_as_a_write_attempt(self):
        guard, client, _ = self.real_execution(
            snapshot_hook=[None, RuntimeError("source changed")]
        )
        self.assertFalse(guard.media_write_attempted)
        client.move.assert_not_called()
        client.rename.assert_not_called()
        client.delete.assert_not_called()

    def test_live_conflict_cannot_backup_replace_or_recycle_old_media(self):
        guard, client, _ = self.real_execution(conflict="replace")
        self.assertFalse(guard.media_write_attempted)
        client.move.assert_not_called()
        client.rename.assert_not_called()
        client.delete.assert_not_called()

    def test_orphan_target_metadata_is_not_implicitly_overwritten(self):
        guard, client, _ = self.real_execution(
            target_files=[
                GuangYaFile("old-nfo", "ABC-123.nfo", False, size=100),
            ]
        )
        self.assertFalse(guard.media_write_attempted)
        client.move.assert_not_called()
        client.rename.assert_not_called()
        client.delete.assert_not_called()

    def test_already_moved_part_of_same_frozen_group_is_not_a_foreign_conflict(self):
        guard = confirmations._AgentCleanWriteBoundary(self.payload, self.candidate)
        guard(
            SimpleNamespace(action="move", conflict_decision="new"),
            "conflict",
            target_files=[GuangYaFile("file-0", "ABC-123.mp4", False, size=1024)],
        )
        self.assertFalse(guard.media_write_attempted)

    def test_real_engine_success_marks_attempt_only_before_move(self):
        guard, client, stats = self.real_execution()
        self.assertTrue(guard.media_write_attempted)
        client.move.assert_called_once_with(["file-0"], "target")
        self.assertEqual(stats["moved"], 1)
        client.delete.assert_not_called()

    def test_real_organizer_and_confirmation_share_one_safe_clean_write_path(self):
        from tests.test_organize_multiversion import _VariantTreeClient

        # 仅替换云端传输/探测/通知；保留冻结解析、扫描、规划、移动与审计代码。
        self.rules.small_file_mb = 0
        self.rules.link_strm = False
        self.rules.emby_refresh = False
        self.payload["rules"] = confirmations.organize_rules_snapshot(self.rules)
        token = self.ticket(running=True)
        incoming = GuangYaFile(
            "file-0", "ABC-123.mp4", False, size=1024, parent_id="parent"
        )
        client = _VariantTreeClient(
            incoming, GuangYaFile("unused", "Unused.mp4", False)
        )
        incoming.parent_id = "parent"
        client.tree = {
            "0": [
                GuangYaFile("source", "专用源", True, parent_id="0"),
                GuangYaFile("archive", "归档", True, parent_id="0"),
            ],
            "source": [GuangYaFile("parent", "待整理", True, parent_id="source")],
            "parent": [incoming],
            "archive": [],
        }
        with (
            patch.object(confirmations, "GuangYaClient", return_value=client),
            patch.object(Organizer, "_probe_move_plan_profiles"),
            patch.object(Organizer, "trigger_post_actions"),
            patch.object(confirmations, "NsfwRecognizer") as metatube,
        ):
            result = confirmations._execute_guangya_confirmation(
                token,
                self.payload,
                self.candidate,
                selected_index=0,
                chat_id="100",
                actor="agent",
            )
        metatube.assert_not_called()
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(len(client.moves), 1)
        self.assertEqual(client.deleted, [])
        self.assertIn("parent", client.tree)  # 原待整理空目录不自动清理。
        self.assertNotEqual(incoming.parent_id, "parent")
        rows = [
            dict(r)
            for r in db.list_organize_logs_by_operation_token(
                f"recognition-confirm:{token}"
            )
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row["confirmation_actor"] == "agent" for row in rows))
        self.assertTrue(all(row["provider"] == "clean_title" for row in rows))
