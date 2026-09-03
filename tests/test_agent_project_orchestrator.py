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
    is_agent_action_request,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.orchestrator import (
    AgentOrchestrator,
    _is_unsupported_engineering_request,
)
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import (
    build_tool_registry,
    prepare_strm_run_once,
    run_strm_once_confirmed,
    strm_run_arguments,
)


class AgentFreeOrchestrationTests(unittest.TestCase):
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
        self.assertEqual(self._names("推荐一个 Docker 部署方式"), [])
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
                self.assertNotIn("discovery.recommend", self._names(message))
        self.assertIn("discovery.recommend", self._names("最近想看点科幻"))

    def test_postfix_media_subscription_creation_exposes_confirmation_tool(self) -> None:
        self.assertIn(
            "media.create_subscription",
            self._names("帮我给光阴之外 创建一个每周刷新的订阅"),
        )

    def test_release_status_uses_only_explicitly_requested_sources(self) -> None:
        message = "辐射第二季跟三体第二季都上线了吗"
        self.assertEqual(self._names(message), ["web.search"])

    def test_release_scope_negation_does_not_pollute_media_identity(self) -> None:
        intent = infer_media_intent(
            "辐射第二季上线了吗，不要查本地和资源"
        )
        self.assertEqual(intent.preferred_sources, ("public_web",))
        self.assertEqual(
            set(intent.forbidden_sources),
            {"local_library", "resource_index"},
        )

    def test_release_followup_uses_safe_context_without_contract_gating(self) -> None:
        context = [{
            "role": "user",
            "text": "辐射第二季跟三体第二季都上线了吗",
        }]

        names = self._names("我问你上线没有 这两部剧", context=context)

        self.assertIn("web.search", names)

    def test_ordinary_recommendation_avoids_unneeded_web_search(self) -> None:
        names = self._names("最近想看点科幻")

        self.assertNotIn("web.search", names)
        self.assertIn("discovery.recommend", names)
        self.assertNotIn("discovery.search", names)

    def test_current_year_recommendation_fallback_keeps_relevant_catalog_tools(self) -> None:
        query = f"{date.today().year} 科幻 欧美剧集推荐"
        names = self._names(query)

        self.assertIn("discovery.recommend", names)
        self.assertIn("discovery.search", names)
        self.assertNotIn("discovery.add_watchlist", names)

    def test_exact_qb_realtime_and_media_total_use_native_provider_routes(self) -> None:
        agent = AgentOrchestrator(self.registry)
        sentinel = {"mode": "read_only", "result": {"ok": True}}
        with patch.object(
            agent, "_invoke_default_provider_read", return_value=sentinel
        ) as invoke:
            self.assertIs(
                agent._query_raw(
                    "qb 实时任务呢", owner="owner", allow_model_routing=False
                ),
                sentinel,
            )
            self.assertEqual(invoke.call_args.kwargs["provider"], "qbittorrent")
            self.assertEqual(
                invoke.call_args.kwargs["operation"], "qb.torrents.info"
            )

            self.assertIs(
                agent._query_raw(
                    "我的媒体库中的媒体总数是多少",
                    owner="owner",
                    allow_model_routing=False,
                ),
                sentinel,
            )
            self.assertEqual(invoke.call_args.kwargs["provider"], "media")
            self.assertEqual(
                invoke.call_args.kwargs["operation"], "media.items.counts"
            )

        self.assertIn(
            "library.count_series_episodes",
            self._names("查看媒体库中《九门》一共有多少集"),
        )

    def test_default_provider_read_preserves_capability_failures(self) -> None:
        agent = AgentOrchestrator(self.registry)
        failure = {
            "mode": "read_only",
            "tool_call": {
                "name": "provider.capabilities",
                "arguments": {"provider": "qbittorrent"},
                "elapsed_ms": 7,
            },
            "result": {
                "ok": False,
                "status": "unavailable",
                "summary": "Provider 能力暂时不可用",
                "data": {},
            },
        }
        with patch.object(
            agent, "_invoke_query_read", return_value=failure
        ) as invoke:
            result = agent._invoke_default_provider_read(
                provider="qbittorrent",
                operation="qb.torrents.info",
                arguments={"query": "", "limit": 100},
                intent="读取 qBittorrent 当前实时任务",
                owner="owner",
            )

        self.assertIs(result, failure)
        invoke.assert_called_once()

    def test_recent_guoman_and_year_followup_require_current_evidence(self) -> None:
        self.assertTrue({
            "discovery.recommend", "web.search",
        }.issubset(set(self._names("最近有什么推荐的国漫"))))
        self.assertTrue({
            "discovery.recommend", "web.search",
        }.issubset(set(self._names("2026 有新剧吗"))))

    def test_year_recommendation_inherits_previous_guoman_scope(self) -> None:
        agent = AgentOrchestrator(self.registry)
        sentinel = {"mode": "read_plan", "result": {"ok": True}}
        context = [{"role": "user", "text": "最近有什么推荐的国漫"}]
        with patch.object(
            agent, "_execute_read_plan", return_value=sentinel
        ) as execute:
            result = agent._query_raw(
                "2026 有新剧吗",
                owner="owner",
                conversation_context=context,
                allow_model_routing=False,
            )

        self.assertIs(result, sentinel)
        plan = execute.call_args.args[0]
        discovery = next(
            step for step in plan.steps if step.tool_name == "discovery.recommend"
        )
        web = next(step for step in plan.steps if step.tool_name == "web.search")
        self.assertEqual(discovery.arguments["year"], "2026")
        self.assertEqual(discovery.arguments["media_type"], "tv")
        self.assertEqual(discovery.arguments["region"], "中国大陆")
        self.assertIn("2026", web.arguments["query"])
        self.assertIn("中国大陆", web.arguments["query"])
        self.assertIn("动画剧集", web.arguments["query"])

    def test_named_series_update_uses_media_chain_without_cross_domain_tools(self) -> None:
        message = "检查师兄太稳健有没有更新"
        names = set(self._names(message))

        self.assertIn("library.check_updates", names)
        self.assertNotIn("guangya.directory_scrape.inspect", names)

    def test_series_update_without_resource_request_avoids_indexers(self) -> None:
        message = "师兄太稳健官方更新到多少集，本地有多少集"
        names = self._names(message)

        self.assertIn("web.search", names)
        self.assertIn("library.count_series_episodes", names)
        self.assertNotIn("indexer.search_resources", names)

    def test_series_workflow_uses_registry_validation_without_intent_contract(self) -> None:
        message = (
            "检查师兄太稳健官方更新到多少集、媒体库有多少集，"
            "有没有缺集并搜索可下载资源，只生成推送计划"
        )
        names = self._names(message)

        self.assertIn("web.search", names)
        self.assertIn("library.count_series_episodes", names)
        self.assertIn("indexer.search_resources", names)
        disposition, arguments = self.registry.validate_llm_orchestration_call(
            "library.count_series_episodes", {"query": "师兄啊师兄"}
        )
        self.assertEqual(disposition.value, "execute_read")
        self.assertEqual(arguments, {"query": "师兄啊师兄", "tmdb_id": ""})

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

    def test_aggregate_does_not_invent_unselected_required_sources(self) -> None:
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
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["required_failed"], 0)

    def test_semantic_fallback_does_not_invent_downstream_workflow_stages(self) -> None:
        strm_names = set(self._names("只同步整理和 NSFW 两个 STRM 来源"))
        source_list_names = set(self._names("有哪些 STRM 来源可以同步"))
        organize_names = set(
            self._names("整理光鸭 /待整理/某剧，只生成计划，不执行")
        )

        self.assertIn("strm.run_once", strm_names)
        self.assertEqual(source_list_names, {"strm.status"})
        self.assertIn("guangya.directory_scrape.inspect", organize_names)
        self.assertNotIn("guangya.directory_scrape.run", organize_names)

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
            preview, context = prepare_strm_run_once(arguments)
            result = run_strm_once_confirmed(arguments, context)

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
