from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.agent.capability_retrieval import infer_media_intent
from app.agent.guangya_directory_scrape_actions import (
    directory_scrape_inspect_arguments,
    inspect_directory_scrape,
    reset_directory_scrape_context_for_tests,
)
from app.agent.llm_router import (
    _native_read_capabilities,
    _resolved_agent_objective,
    _validate_objective_tool_call,
    is_agent_action_request,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.orchestrator import (
    AgentOrchestrator,
    _is_unsupported_engineering_request,
)
from app.agent.objective_contract import infer_agent_objective
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import (
    build_tool_registry,
    preview_strm_run_once,
    run_strm_once,
    strm_run_arguments,
)


class AgentObjectiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = build_tool_registry()

    def _names(self, message: str, *, context=None) -> list[str]:
        return [
            self.registry.native_tool_name(item["name"])
            for item in _native_read_capabilities(
                self.registry,
                message,
                conversation_context=context,
                include_confirmations=True,
            )
        ]

    def test_technical_guidance_and_service_launch_avoid_media_tools(self) -> None:
        deployment = infer_agent_objective("推荐一个 Docker 部署方式")
        self.assertEqual(deployment.task_kind, "technical_guidance")
        self.assertEqual(deployment.max_capabilities, 0)
        self.assertEqual(self._names("推荐一个 Docker 部署方式"), [])
        self.assertEqual(
            infer_agent_objective("MediaFlux 服务上线了吗").task_kind,
            "general",
        )
        self.assertNotIn(
            "official_progress",
            infer_media_intent("MediaFlux 服务上线了吗").domains,
        )

    def test_guidance_questions_do_not_become_engineering_actions(self) -> None:
        self.assertFalse(
            _is_unsupported_engineering_request("推荐一个 Docker 部署方式")
        )
        self.assertFalse(
            _is_unsupported_engineering_request("Docker 应该怎么部署")
        )
        self.assertTrue(
            _is_unsupported_engineering_request("帮我重启 Docker 服务并重新部署")
        )

    def test_host_drive_and_ambiguous_library_sync_require_guidance(self) -> None:
        host_drive = infer_agent_objective("扫描 C 盘垃圾文件")
        library_sync = infer_agent_objective("同步媒体库")

        self.assertEqual(host_drive.task_kind, "host_drive_guidance")
        self.assertEqual(host_drive.max_capabilities, 0)
        self.assertEqual(library_sync.task_kind, "library_sync_clarification")
        self.assertEqual(library_sync.max_capabilities, 0)
        self.assertEqual(self._names("扫描 C 盘垃圾文件"), [])
        self.assertEqual(self._names("同步媒体库"), [])

    def test_guidance_fallbacks_are_specific_when_provider_is_unavailable(self) -> None:
        agent = AgentOrchestrator(self.registry)

        host_drive = agent._query_raw(
            "扫描 C 盘垃圾文件", owner="", allow_model_routing=False
        )
        library_sync = agent._query_raw(
            "同步媒体库", owner="", allow_model_routing=False
        )

        self.assertEqual(host_drive["mode"], "conversation")
        self.assertIn("不能直接扫描宿主机", host_drive["result"]["summary"])
        self.assertEqual(library_sync["mode"], "clarification")
        self.assertIn("可能指不同动作", library_sync["result"]["summary"])
        self.assertEqual(len(library_sync["result"]["suggestions"]), 3)

    def test_non_media_want_to_see_phrases_do_not_trigger_recommendations(self) -> None:
        for message in ("我想看 STRM 同步状态", "这个日志值得看吗"):
            with self.subTest(message=message):
                self.assertNotEqual(
                    infer_agent_objective(message).task_kind,
                    "media_recommendation",
                )
        self.assertEqual(
            infer_agent_objective("最近想看点科幻").task_kind,
            "media_recommendation",
        )

    def test_release_status_uses_only_explicitly_requested_sources(self) -> None:
        message = "辐射第二季跟三体第二季都上线了吗"
        objective = infer_agent_objective(message)

        self.assertEqual(objective.task_kind, "official_release_status")
        self.assertEqual(objective.entity_terms, ("辐射", "三体"))
        self.assertEqual(self._names(message), ["web.search"])

    def test_release_scope_negation_does_not_pollute_media_identity(self) -> None:
        objective = infer_agent_objective(
            "辐射第二季上线了吗，不要查本地和资源"
        )

        self.assertEqual(objective.task_kind, "official_release_status")
        self.assertEqual(objective.entity_terms, ("辐射",))
        self.assertEqual(objective.required_sources, ("public_web",))
        self.assertEqual(
            set(objective.forbidden_sources),
            {"local_library", "resource_index"},
        )

    def test_release_followup_inherits_only_media_entities(self) -> None:
        context = [{
            "role": "user",
            "text": "辐射第二季跟三体第二季都上线了吗",
        }]
        objective = _resolved_agent_objective(
            "我问你上线没有 这两部剧", context
        )

        self.assertEqual(objective.task_kind, "official_release_status")
        self.assertEqual(objective.entity_terms, ("辐射", "三体"))
        self.assertEqual(
            self._names("我问你上线没有 这两部剧", context=context),
            ["web.search"],
        )

    def test_ordinary_recommendation_avoids_unneeded_web_search(self) -> None:
        objective = infer_agent_objective("最近想看点科幻")
        names = self._names("最近想看点科幻")

        self.assertEqual(objective.task_kind, "media_recommendation")
        self.assertEqual(objective.required_sources, ("metadata_catalog",))
        self.assertNotIn("web.search", names)
        self.assertIn("discovery.recommend", names)
        self.assertNotIn("discovery.search", names)

    def test_current_year_recommendation_requires_catalog_and_web(self) -> None:
        query = f"{date.today().year} 科幻 欧美剧集推荐"
        objective = infer_agent_objective(query)
        names = self._names(query)

        self.assertEqual(objective.task_kind, "media_recommendation")
        self.assertEqual(
            objective.required_sources, ("metadata_catalog", "public_web")
        )
        self.assertIn("discovery.recommend", names)
        self.assertNotIn("discovery.search", names)
        self.assertIn("web.search", names)
        self.assertNotIn("discovery.add_watchlist", names)

    def test_named_series_update_uses_media_chain_without_cross_domain_tools(self) -> None:
        message = "检查师兄太稳健有没有更新"
        objective = infer_agent_objective(message)
        names = set(self._names(message))

        self.assertEqual(objective.task_kind, "series_update_audit")
        self.assertEqual(objective.entity_terms, ("师兄太稳健",))
        self.assertIn("web.search", names)
        self.assertIn("library.check_updates", names)
        self.assertNotIn("rss.diagnose", names)
        self.assertNotIn("media.subscription_updates", names)
        self.assertNotIn("downloads.diagnose_queue", names)
        self.assertNotIn("indexer.search_resources", names)

        scoped = infer_agent_objective("媒体库里的师兄太稳健有没有更新")
        self.assertEqual(scoped.entity_terms, ("师兄太稳健",))
        self.assertEqual(
            infer_agent_objective("检查 MediaFlux 有没有更新").task_kind,
            "general",
        )
        self.assertEqual(
            infer_agent_objective("我关注的动漫有更新吗").task_kind,
            "general",
        )

    def test_series_update_without_resource_request_avoids_indexers(self) -> None:
        message = "师兄太稳健官方更新到多少集，本地有多少集"
        objective = infer_agent_objective(message)
        names = self._names(message)

        self.assertEqual(objective.task_kind, "series_update_audit")
        self.assertEqual(objective.entity_terms, ("师兄太稳健",))
        self.assertEqual(
            objective.required_sources, ("public_web", "local_library")
        )
        self.assertIn("resource_index", objective.forbidden_sources)
        self.assertNotIn("indexer.search_resources", objective.allowed_tools)
        self.assertNotIn("indexer.search_resources", names)

    def test_series_workflow_locks_identity_and_tool_scope(self) -> None:
        message = (
            "检查师兄太稳健官方更新到多少集、媒体库有多少集，"
            "有没有缺集并搜索可下载资源，只生成推送计划"
        )
        objective = infer_agent_objective(message)
        names = self._names(message)

        self.assertEqual(objective.entity_terms, ("师兄太稳健",))
        self.assertIn("web.search", names)
        self.assertIn("library.count_series_episodes", names)
        self.assertIn("indexer.search_resources", names)
        self.assertNotIn("guangya.directory_scrape.inspect", names)
        _validate_objective_tool_call(
            objective,
            "library.count_series_episodes",
            {"query": "师兄太稳健"},
            registry=self.registry,
        )
        with self.assertRaises(AgentToolError) as caught:
            _validate_objective_tool_call(
                objective,
                "library.count_series_episodes",
                {"query": "师兄啊师兄"},
                registry=self.registry,
            )
        self.assertEqual(caught.exception.code, "identity_mismatch")

    def test_plan_only_language_is_not_misclassified_as_executed_action(self) -> None:
        self.assertFalse(is_agent_action_request("只生成推送计划，不要执行"))
        self.assertFalse(
            is_agent_action_request("整理光鸭 /待整理/某剧，只生成计划")
        )
        self.assertTrue(is_agent_action_request("立即同步整理 STRM 来源"))

    def test_auxiliary_failure_does_not_downgrade_completed_release_answer(self) -> None:
        orchestrator = AgentOrchestrator(ToolRegistry())
        executions = [
            {
                "tool_name": "web.search",
                "arguments": {"query": "某剧 第二季 上线日期"},
                "response": {
                    "tool_call": {"name": "web.search", "elapsed_ms": 3},
                    "result": ToolResult(True, "ok", "官方日期已核验").to_dict(),
                },
            },
            {
                "tool_name": "library.check_updates",
                "arguments": {"query": "某剧"},
                "response": {
                    "tool_call": {"name": "library.check_updates", "elapsed_ms": 2},
                    "result": ToolResult(
                        False, "unavailable", "本地媒体库不可用", error="连接失败"
                    ).to_dict(),
                },
            },
        ]

        response = orchestrator._aggregate_native_read_executions(
            executions,
            owner="",
            completed=True,
            narrative_suggestions=(),
            message="《某剧》第二季上线了吗",
        )

        self.assertIsNotNone(response)
        result = response["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["required_failed"], 0)
        self.assertEqual(result["data"]["supporting_failed"], 1)

    def test_aggregate_requires_success_from_every_objective_source(self) -> None:
        orchestrator = AgentOrchestrator(ToolRegistry())
        executions = [{
            "tool_name": "web.search",
            "arguments": {"query": "某剧 官方已播集数"},
            "response": {
                "tool_call": {"name": "web.search", "elapsed_ms": 1},
                "result": ToolResult(
                    True, "completed", "官方已播集数已核验"
                ).to_dict(),
            },
        }, {
            "tool_name": "indexer.search_resources",
            "arguments": {"query": "某剧 S01E08"},
            "response": {
                "tool_call": {
                    "name": "indexer.search_resources", "elapsed_ms": 1
                },
                "result": ToolResult(
                    True, "completed", "已找到资源候选"
                ).to_dict(),
            },
        }]
        # 本轮故意没有执行 local_library 来源，不能因为其他来源成功
        # 就宣称全链路完成。
        response = orchestrator._aggregate_native_read_executions(
            executions,
            owner="",
            completed=True,
            narrative_suggestions=(),
            message=(
                "检查某剧官方更新到多少集、媒体库有多少集，"
                "有没有缺集并搜索可下载资源"
            ),
        )

        self.assertIsNotNone(response)
        result = response["result"]
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["required_failed"], 1)

    def test_strm_and_object_organize_capabilities_remain_isolated(self) -> None:
        strm_names = set(self._names("只同步整理和 NSFW 两个 STRM 来源"))
        source_list_message = "有哪些 STRM 来源可以同步"
        source_list = self._names(source_list_message)
        status_message = "我想看 STRM 同步状态"
        status_names = set(self._names(status_message))
        organize_names = set(
            self._names("整理光鸭 /待整理/某剧，只生成计划，不执行")
        )
        colloquial_preview_names = set(
            self._names("整理光鸭 /待整理/某剧，先看看方案，别执行")
        )

        self.assertEqual(
            strm_names,
            {"strm.status", "strm.diagnose", "strm.run_history", "strm.run_once"},
        )
        self.assertEqual(source_list, ["strm.status"])
        self.assertFalse(is_agent_action_request(source_list_message))
        self.assertEqual(
            status_names, {"strm.status", "strm.diagnose", "strm.run_history"}
        )
        self.assertNotIn("strm.run_once", status_names)
        self.assertFalse(is_agent_action_request(status_message))
        self.assertEqual(
            organize_names,
            {
                "guangya.directory_scrape.inspect",
                "guangya.directory_scrape.search",
                "guangya.directory_scrape.preview",
            },
        )
        self.assertEqual(colloquial_preview_names, organize_names)

    def test_strm_source_catalog_has_zero_model_fallback(self) -> None:
        agent = AgentOrchestrator(self.registry)
        expected = {
            "mode": "read_only",
            "tool_call": {"name": "strm.status", "arguments": {}},
            "result": ToolResult(True, "ready", "STRM 当前空闲").to_dict(),
        }
        with patch.object(
            agent, "_invoke_query_read", return_value=expected
        ) as invoke:
            response = agent._query_raw(
                "有哪些 STRM 来源可以同步",
                owner="",
                allow_model_routing=False,
            )

        self.assertEqual(response, expected)
        invoke.assert_called_once_with(
            "strm.status", {}, owner="", rate_identity=""
        )


class AgentScopedWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_directory_scrape_context_for_tests()

    def tearDown(self) -> None:
        reset_directory_scrape_context_for_tests()

    def test_directory_scrape_accepts_private_absolute_path(self) -> None:
        self.assertEqual(
            directory_scrape_inspect_arguments({"path": "/待整理/某剧"}),
            {"path": "/待整理/某剧"},
        )
        for invalid in ("待整理/某剧", "/", "/待整理/../某剧"):
            with self.assertRaises(AgentToolError):
                directory_scrape_inspect_arguments({"path": invalid})

    def test_directory_scrape_resolves_path_server_side_and_closes_client(self) -> None:
        client = Mock()
        service = Mock()
        service.inspect.return_value = {
            "inspection_id": "inspection-private",
            "media_type": "tv",
            "suggested_query": "某剧",
            "season": 1,
            "episode": None,
            "requires_manual_match": False,
            "manual_match_reason": "",
            "counts": {"videos": 2},
        }
        target = SimpleNamespace(is_dir=True, file_id="private-directory-id")
        with patch(
            "app.agent.guangya_directory_scrape_actions.GuangYaClient",
            return_value=client,
        ), patch(
            "app.agent.guangya_directory_scrape_actions.resolve_workspace_path",
            return_value=target,
        ) as resolve, patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=service,
        ):
            result = inspect_directory_scrape(
                {"path": "/待整理/某剧"}, ToolContext(owner="owner-path")
            )

        resolve.assert_called_once_with(client, "/待整理/某剧")
        client.close.assert_called_once_with()
        service.inspect.assert_called_once_with("owner-path", "private-directory-id")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["scope_type"], "directory")
        self.assertNotIn("private-directory-id", str(result.to_dict()))
        self.assertNotIn("/待整理/某剧", str(result.to_dict()))

    def test_selected_strm_sources_are_resolved_and_submitted_by_server_ids(self) -> None:
        configured = [
            {"id": "source-a", "name": "整理"},
            {"id": "source-b", "name": "NSFW"},
        ]
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        arguments = {"source_names": ["整理", "NSFW"]}

        with patch(
            "app.modules.strm.configured_strm_source_plans",
            return_value=(configured, ""),
        ), patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            preview = preview_strm_run_once(arguments)
            result = run_strm_once(arguments)

        self.assertEqual(strm_run_arguments(arguments), arguments)
        self.assertEqual(
            strm_run_arguments({"source_names": ["NSFW", "nsfw"]}),
            {"source_names": ["NSFW"]},
        )
        self.assertTrue(preview.ok)
        self.assertEqual(preview.data["source_names"], ["整理", "NSFW"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["scoped"])
        scheduler.trigger.assert_called_once_with(
            "manual", selected_source_ids=["source-a", "source-b"]
        )
        self.assertNotIn("source-a", str(preview.to_dict()))


if __name__ == "__main__":
    unittest.main()
