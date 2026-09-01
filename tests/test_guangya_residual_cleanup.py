"""光鸭整理残留安全清理测试。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.agent import guangya_cleanup_actions as actions
from app.agent.models import RiskLevel, ToolContext
from app.agent.orchestrator import AgentOrchestrator
from app.agent.registry import AgentToolError
from app.agent.tools import build_tool_registry
from app.clients.guangya import GuangYaFile
from app.modules import guangya_residual_cleanup as cleanup


class FakeCleanupClient:
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 13
        self.closed = False
        self.next_id = 100
        self.directories = {
            "0": [GuangYaFile("source", "整理源", True, parent_id="0", etag="src")],
            "source": [
                GuangYaFile("empty", "空目录", True, parent_id="source", etag="e1", updated_at=1),
                GuangYaFile("residual", "a", True, parent_id="source", etag="e2", updated_at=2),
                GuangYaFile("keep", "保留元数据", True, parent_id="source", etag="e3", updated_at=3),
                GuangYaFile("video_dir", "媒体目录", True, parent_id="source", etag="e4", updated_at=4),
            ],
            "empty": [],
            "residual": [
                GuangYaFile("junk", "xxx.png", False, parent_id="residual", size=10, etag="j", extension="png")
            ],
            "keep": [
                GuangYaFile("poster", "poster.jpg", False, parent_id="keep", size=10, etag="p", extension="jpg")
            ],
            "video_dir": [
                GuangYaFile("video", "ABC-123.mp4", False, parent_id="video_dir", size=100, etag="v", extension="mp4")
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

    def create_dir(self, name, parent_id="0"):
        self.next_id += 1
        file_id = f"new-{self.next_id}"
        self.directories.setdefault(str(parent_id), []).append(
            GuangYaFile(file_id, str(name), True, parent_id=str(parent_id), etag=f"etag-{file_id}", updated_at=self.next_id)
        )
        self.directories[file_id] = []
        return file_id

    def move(self, file_ids, parent_id):
        for file_id in file_ids:
            found = None
            for items in self.directories.values():
                for index, item in enumerate(items):
                    if item.file_id == str(file_id):
                        found = items.pop(index)
                        break
                if found is not None:
                    break
            if found is None:
                raise RuntimeError("missing")
            found.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(found)
        return True

    def delete_empty_directory(self, file_id, *, expected_etag="", expected_updated_at=0):
        if self.directories.get(str(file_id)):
            raise RuntimeError("not empty")
        for items in self.directories.values():
            for index, item in enumerate(items):
                if item.file_id == str(file_id):
                    if expected_etag and item.etag != expected_etag:
                        raise RuntimeError("stale")
                    items.pop(index)
                    self.directories.pop(str(file_id), None)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True


class GuangYaResidualCleanupTests(unittest.TestCase):
    def setUp(self):
        actions.reset_guangya_cleanup_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.plan_dir = Path(self.temp.name) / "plans"
        self.patches = [
            mock.patch.object(cleanup, "_plan_directory", return_value=self.plan_dir),
            mock.patch.object(cleanup, "_owner_digest", return_value="owner-digest"),
            mock.patch.object(cleanup, "get_web_secret", return_value="test-secret"),
            mock.patch.object(actions, "organize_operation_owner_digest", return_value="owner-digest"),
            mock.patch.object(actions, "_configured_sources", return_value=[{"id": "source", "name": "整理源"}]),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        actions.reset_guangya_cleanup_context_for_tests()
        self.temp.cleanup()

    def test_plan_keeps_media_metadata_and_requires_filename_review(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client,
            owner="owner",
            sources=[{"id": "source", "name": "整理源"}],
            max_candidates=20,
        )
        self.assertEqual(plan["stats"]["empty_dir_count"], 1)
        self.assertEqual(plan["stats"]["candidate_count"], 1)
        self.assertEqual(plan["stats"]["undecided_count"], 1)
        self.assertEqual(plan["stats"]["residual_dir_count"], 0)
        self.assertEqual(plan["stats"]["quarantine_file_count"], 0)
        self.assertEqual(plan["candidates"][0]["root"]["name"], "a")
        self.assertEqual(plan["candidates"][0]["file_names"], ["xxx.png"])
        self.assertEqual(plan["empties"][0]["root"]["name"], "空目录")

    def test_review_candidates_never_hide_unshown_file_names(self):
        client = FakeCleanupClient()
        client.directories["residual"] = [
            GuangYaFile(
                f"junk-{index}",
                f"candidate-{index}.png",
                False,
                parent_id="residual",
                size=10,
                etag=f"junk-{index}",
                extension="png",
            )
            for index in range(cleanup._MAX_REVIEW_FILES + 1)
        ]
        plan = cleanup.build_cleanup_plan(
            client,
            owner="owner",
            sources=[{"id": "source", "name": "整理源"}],
            max_candidates=20,
        )
        self.assertEqual(plan["stats"]["candidate_count"], 0)
        self.assertEqual(plan["candidates"], [])
        self.assertGreaterEqual(plan["stats"]["preserved_dir_count"], 1)
        self.assertIsNotNone(client.file_info("residual"))

    def test_confirmed_execution_quarantines_residual_and_recycles_empty(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client,
            owner="owner",
            sources=[{"id": "source", "name": "整理源"}],
            max_candidates=20,
        )
        plan = cleanup.revise_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
            decisions=[{
                "candidate_number": 1,
                "action": "quarantine",
                "reason": "随机图片名且目录内无媒体文件",
            }],
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest",
                "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["quarantined"], 1)
        self.assertEqual(result["stats"]["empty_deleted"], 1)
        self.assertIsNone(client.file_info("empty"))
        self.assertIsNotNone(client.file_info("residual"))
        self.assertNotEqual(client.file_info("residual").parent_id, "source")


    def test_residual_is_rechecked_immediately_before_move(self):
        class MutatingClient(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.mutated = False

            def create_dir(self, name, parent_id="0"):
                created = super().create_dir(name, parent_id)
                if not self.mutated:
                    self.mutated = True
                    self.directories["residual"].append(
                        GuangYaFile(
                            "late-video", "late.mp4", False,
                            parent_id="residual", size=20, etag="late",
                            extension="mp4",
                        )
                    )
                return created

        client = MutatingClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        plan = cleanup.revise_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
            decisions=[{"candidate_number": 1, "action": "quarantine"}],
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1, "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest", "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["quarantined"], 0)
        self.assertEqual(result["stats"]["precondition_failed"], 1)
        self.assertEqual(client.file_info("residual").parent_id, "source")

    def test_empty_directory_is_rechecked_immediately_before_delete(self):
        class MutatingClient(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.mutated = False

            def create_dir(self, name, parent_id="0"):
                created = super().create_dir(name, parent_id)
                if not self.mutated:
                    self.mutated = True
                    self.directories["empty"].append(
                        GuangYaFile(
                            "late-junk", "late.txt", False, parent_id="empty",
                            size=1, etag="late-junk", extension="txt",
                        )
                    )
                return created

        client = MutatingClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        plan = cleanup.revise_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
            decisions=[{"candidate_number": 1, "action": "quarantine"}],
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1, "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest", "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["empty_deleted"], 0)
        self.assertEqual(result["stats"]["precondition_failed"], 1)
        self.assertIsNotNone(client.file_info("empty"))

    def test_agent_preview_and_durable_submission(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_cleanup({"max_candidates": 20}, context)
            reviewed = actions.classify_guangya_cleanup_candidates({
                "decisions": [{"candidate_number": 1, "action": "quarantine"}],
            }, context)
            confirmation, fingerprint = actions.prepare_guangya_cleanup_confirmation({}, context)
            manager = mock.Mock()
            manager.start_durable_operation.return_value = {
                "ok": True, "task_id": "a" * 32, "queued": True, "queue_position": 1,
            }
            with mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ):
                accepted = actions.execute_guangya_cleanup_confirmed(
                    {}, fingerprint, context
                )
        self.assertEqual(preview.status, "selection_required")
        self.assertEqual(reviewed.status, "ready")
        self.assertEqual(confirmation.status, "confirmation_required")
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(
            manager.start_durable_operation.call_args.kwargs["job_kind"],
            "agent_guangya_cleanup",
        )

    def test_agent_preview_can_target_exact_unconfigured_directory(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        arguments = actions.guangya_cleanup_preview_arguments({
            "path": "/整理源", "max_candidates": 20,
        })
        with (
            mock.patch.object(actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                actions, "_configured_cleanup_sources",
                side_effect=AssertionError("不应读取预配置来源"),
            ),
        ):
            preview = actions.preview_guangya_cleanup(arguments, context)
        self.assertEqual(preview.status, "selection_required")
        self.assertEqual(preview.data["source_count"], 1)
        self.assertEqual(preview.data["empty_dir_count"], 1)
        self.assertEqual(preview.data["candidate_count"], 1)

    def test_agent_reviews_large_frozen_plan_in_rolling_batches(self):
        client = FakeCleanupClient()
        client.directories["source"] = []
        for number in range(1, 19):
            directory_id = f"residual-{number}"
            client.directories["source"].append(
                GuangYaFile(
                    directory_id, f"残留-{number}", True,
                    parent_id="source", etag=f"dir-{number}", updated_at=number,
                )
            )
            client.directories[directory_id] = [
                GuangYaFile(
                    f"junk-{number}", f"ad-{number}.png", False,
                    parent_id=directory_id, size=10,
                    etag=f"junk-{number}", extension="png",
                )
            ]
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_cleanup({"max_candidates": 20}, context)
            first_batch = actions.classify_guangya_cleanup_candidates({
                "decisions": [
                    {"candidate_number": number, "action": "keep"}
                    for number in range(1, 17)
                ],
            }, context)
            final = actions.classify_guangya_cleanup_candidates({
                "decisions": [
                    {"candidate_number": 17, "action": "quarantine"},
                    {"candidate_number": 18, "action": "keep"},
                ],
            }, context)

        self.assertEqual(preview.data["candidate_count"], 18)
        self.assertEqual(len(preview.data["review_summaries"]), 16)
        self.assertIn("#1 ", preview.data["review_summaries"][0])
        self.assertEqual(first_batch.status, "selection_required")
        self.assertEqual(first_batch.data["undecided_count"], 2)
        self.assertEqual(len(first_batch.data["review_summaries"]), 2)
        self.assertIn("#17 ", first_batch.data["review_summaries"][0])
        self.assertIn("#18 ", first_batch.data["review_summaries"][1])
        self.assertEqual(final.status, "ready")
        self.assertEqual(final.data["selected_count"], 1)
        self.assertEqual(final.data["kept_count"], 17)

    def test_agent_cleanup_validators_cover_full_frozen_plan_range(self):
        self.assertEqual(
            actions.guangya_cleanup_preview_arguments({}),
            {"path": "", "max_candidates": 500, "scope": "all"},
        )
        self.assertEqual(
            actions.guangya_cleanup_preview_arguments({"scope": "empty_only"}),
            {"path": "", "max_candidates": 500, "scope": "empty_only"},
        )
        self.assertEqual(
            actions.guangya_cleanup_classify_arguments({
                "decisions": [{"candidate_number": 500, "action": "keep"}],
            })["decisions"][0]["candidate_number"],
            500,
        )
        with self.assertRaisesRegex(AgentToolError, "冻结计划范围"):
            actions.guangya_cleanup_classify_arguments({
                "decisions": [{"candidate_number": 501, "action": "keep"}],
            })

    def test_persisted_preview_rejects_non_list_public_fields(self):
        value = {key: 0 for key in actions._PREVIEW_KEYS}
        value.update({
            "sample_directories": [],
            "review_summaries": "not-a-list",
        })
        self.assertIsNone(actions._safe_preview(value))

    def test_unreviewed_candidate_cannot_enter_confirmation(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_cleanup({"max_candidates": 16}, context)
            with self.assertRaisesRegex(AgentToolError, "尚未逐项复核"):
                actions.prepare_guangya_cleanup_confirmation({}, context)
        self.assertEqual(preview.data["undecided_count"], 1)

    def test_keep_revision_invalidates_previous_confirmation_context(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_cleanup({"max_candidates": 16}, context)
            actions.classify_guangya_cleanup_candidates({
                "decisions": [{"candidate_number": 1, "action": "quarantine"}],
            }, context)
            _confirmation, previous_context = actions.prepare_guangya_cleanup_confirmation(
                {}, context
            )
            revised = actions.classify_guangya_cleanup_candidates({
                "decisions": [{
                    "candidate_number": 1,
                    "action": "keep",
                    "reason": "用户明确保留",
                }],
            }, context)
            _confirmation, current_context = actions.prepare_guangya_cleanup_confirmation(
                {}, context
            )
            with self.assertRaisesRegex(AgentToolError, "预览已变化"):
                actions.execute_guangya_cleanup_confirmed({}, previous_context, context)
        self.assertNotEqual(previous_context, current_context)
        self.assertEqual(revised.data["selected_count"], 0)
        self.assertEqual(revised.data["kept_count"], 1)
        self.assertIn("xxx.png", repr(revised.data["review_summaries"]))



    def test_failed_repreview_preserves_previous_cleanup_plan(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_cleanup({"max_candidates": 20}, context)
        previous = actions._flow("owner")
        self.assertIsNotNone(previous)
        previous_path = cleanup._plan_path(previous.plan_id)
        self.assertTrue(previous_path.is_file())

        with (
            mock.patch.object(actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                actions, "build_cleanup_plan",
                side_effect=cleanup.GuangYaCleanupPlanError("临时读取失败"),
            ),
            self.assertRaisesRegex(Exception, "临时读取失败"),
        ):
            actions.preview_guangya_cleanup({"max_candidates": 20}, context)
        self.assertEqual(actions._flow("owner").plan_id, previous.plan_id)
        self.assertTrue(previous_path.is_file())

    def test_terminal_cleanup_plan_is_removed_after_retention(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        cleanup._update_execution(
            plan["plan_id"], "completed", {"quarantined": 1, "empty_deleted": 1}
        )
        with mock.patch.object(
            cleanup.time, "time",
            return_value=plan["created_at_epoch"] + 8 * 24 * 60 * 60,
        ):
            result = cleanup.maintain_cleanup_plans()
        self.assertEqual(result["removed"], 1)
        self.assertFalse((self.plan_dir / f"{plan['plan_id']}.json").exists())

    def test_registry_exposes_preview_and_confirmed_cleanup(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertNotIn("guangya.organize.clean_empty", capabilities)
        self.assertEqual(
            capabilities["guangya.organize.cleanup.preview"]["risk"],
            RiskLevel.READ.value,
        )
        self.assertEqual(
            capabilities["guangya.organize.cleanup.classify"]["risk"],
            RiskLevel.READ.value,
        )
        self.assertEqual(
            capabilities["guangya.organize.cleanup.execute"]["risk"],
            RiskLevel.DANGER.value,
        )
        self.assertTrue(
            capabilities["guangya.organize.cleanup.execute"]["requires_confirmation"]
        )
        self.assertEqual(
            registry.confirmation_followup_for(
                "guangya.organize.cleanup.preview"
            ),
            "guangya.organize.cleanup.execute",
        )
        self.assertEqual(
            registry.confirmation_followup_for(
                "guangya.organize.cleanup.classify"
            ),
            "guangya.organize.cleanup.execute",
        )

    def test_empty_only_cleanup_uses_canonical_exact_frozen_plan(self):
        client = FakeCleanupClient()
        service = AgentOrchestrator(build_tool_registry())
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            prepared = service.prepare(
                "guangya.organize.cleanup.execute",
                {},
                owner="owner",
                _cleanup_scope="empty_only",
            )

        self.assertEqual(
            prepared["tool_call"]["name"],
            "guangya.organize.cleanup.execute",
        )
        self.assertEqual(prepared["result"]["data"]["scope"], "empty_only")
        self.assertEqual(prepared["result"]["data"]["empty_dir_count"], 1)
        self.assertEqual(prepared["result"]["data"]["candidate_count"], 0)
        flow = actions._flow("owner")
        self.assertIsNotNone(flow)
        self.assertRegex(flow.request_binding, r"^[0-9a-f]{64}$")
        plan = cleanup.load_cleanup_plan(
            flow.plan_id,
            owner="owner",
            expected_fingerprint=flow.fingerprint,
        )
        self.assertEqual(plan["stats"]["undecided_count"], 0)
        self.assertEqual(plan["stats"]["kept_count"], 1)
        self.assertEqual(plan["residuals"], [])
        self.assertEqual(plan["empties"][0]["root"]["file_id"], "empty")
        self.assertEqual(plan["empties"][0]["root"]["etag"], "e1")
        with self.assertRaisesRegex(AgentToolError, "不接受残留候选复核"):
            actions.classify_guangya_cleanup_candidates(
                {"decisions": [{"candidate_number": 1, "action": "quarantine"}]},
                ToolContext(owner="owner", session_id="session"),
            )

        # 预览后新出现的空目录不在冻结计划中，执行不能动态扩大范围。
        client.directories["source"].append(
            GuangYaFile(
                "late-empty", "稍后出现", True,
                parent_id="source", etag="late", updated_at=99,
            )
        )
        client.directories["late-empty"] = []
        cleanup.confirm_cleanup_plan(
            flow.plan_id, owner="owner", expected_fingerprint=flow.fingerprint
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1,
                "plan_id": flow.plan_id,
                "plan_fingerprint": flow.fingerprint,
                "owner_digest": "owner-digest",
                "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertEqual(result["stats"]["empty_deleted"], 1)
        self.assertIsNone(client.file_info("empty"))
        self.assertIsNotNone(client.file_info("late-empty"))
        self.assertIsNotNone(client.file_info("residual"))

    def test_empty_only_cleanup_rejects_flow_replaced_before_ticket_issue(self):
        client = FakeCleanupClient()
        service = AgentOrchestrator(build_tool_registry())
        original_prepare = service.registry.prepare_confirmation
        owner = "owner-race"

        def replace_flow_before_prepare(name, arguments, *, context=None):
            actions.preview_guangya_cleanup(
                {"max_candidates": 500, "scope": "empty_only"},
                ToolContext(owner=owner, request_id="replacement-request"),
            )
            return original_prepare(name, arguments, context=context)

        with (
            mock.patch.object(actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                service.registry,
                "prepare_confirmation",
                side_effect=replace_flow_before_prepare,
            ),
            self.assertRaises(AgentToolError) as stale,
        ):
            service.prepare(
                "guangya.organize.cleanup.execute",
                {},
                owner=owner,
                request_id="original-request",
                _cleanup_scope="empty_only",
            )

        self.assertEqual(stale.exception.code, "confirmation_stale")
        self.assertEqual(service.confirmation_store.list_active_tickets(owner=owner), [])

    def test_user_keep_decision_overrides_previous_quarantine_selection(self):
        client = FakeCleanupClient()
        initial = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        selected = cleanup.revise_cleanup_plan(
            initial["plan_id"], owner="owner",
            expected_fingerprint=initial["fingerprint"],
            decisions=[{"candidate_number": 1, "action": "quarantine"}],
        )
        vetoed = cleanup.revise_cleanup_plan(
            selected["plan_id"], owner="owner",
            expected_fingerprint=selected["fingerprint"],
            decisions=[{
                "candidate_number": 1,
                "action": "keep",
                "reason": "用户明确要求保留",
            }],
        )
        self.assertEqual(vetoed["stats"]["selected_count"], 0)
        self.assertEqual(vetoed["stats"]["kept_count"], 1)
        self.assertEqual(vetoed["stats"]["undecided_count"], 0)
        self.assertEqual(vetoed["residuals"], [])
        cleanup.confirm_cleanup_plan(
            vetoed["plan_id"], owner="owner",
            expected_fingerprint=vetoed["fingerprint"],
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1,
                "plan_id": vetoed["plan_id"],
                "plan_fingerprint": vetoed["fingerprint"],
                "owner_digest": "owner-digest",
                "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertEqual(result["stats"]["quarantined"], 0)
        self.assertEqual(client.file_info("residual").parent_id, "source")


if __name__ == "__main__":
    unittest.main()
