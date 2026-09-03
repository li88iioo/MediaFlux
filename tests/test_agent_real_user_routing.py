"""开发机真实用户话术的确定性 Agent 路由回归。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    _MEDIA_LIBRARY_TOTAL_PATTERN,
    AgentOrchestrator,
    discovery_watchlist_title_request,
    is_rss_subscription_refresh_write_message,
    is_telegram_test_notification_message,
    media_proxy_restart_request,
    media_subscription_control_name_request,
    rss_subscription_control_name_request,
)
from app.agent.registry import AgentToolError, ToolRegistry


def _identity(arguments):
    return dict(arguments)


def _read_tool(name: str, calls: list[tuple[str, dict]]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        risk=RiskLevel.READ,
        parameters={},
        handler=lambda arguments, tool_name=name: (
            calls.append((tool_name, dict(arguments)))
            or ToolResult(True, "success", f"{tool_name} completed")
        ),
        validator=_identity,
    )


class AgentRealUserRoutingTests(unittest.TestCase):
    def _agent(self, *tool_names: str):
        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in tool_names:
            registry.register(_read_tool(name, calls))
        return AgentOrchestrator(registry), calls

    def test_mixed_subscription_summary_falls_back_to_both_reads(self):
        agent, calls = self._agent(
            "rss.subscription_summaries",
            "media.subscription_summaries",
        )
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            response = agent.query(
                "我配置了哪些 RSS 订阅源？再列出媒体追更订阅",
                owner="web-owner",
                present=False,
            )
        model.assert_called_once()
        self.assertEqual(response["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(calls, [
            ("rss.subscription_summaries", {}),
            ("media.subscription_summaries", {}),
        ])

    def test_local_and_guangya_failure_questions_keep_deterministic_fallback(self):
        agent, calls = self._agent("local_media.diagnose", "organize.audit_logs")
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            local = agent.query(
                "本地整理最近有没有失败或待确认？",
                owner="web-owner",
                present=False,
            )
            guangya = agent.query(
                "光鸭整理最近有没有失败？",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(model.call_count, 2)
        self.assertEqual(local["tool_call"]["name"], "local_media.diagnose")
        self.assertEqual(guangya["tool_call"]["name"], "organize.audit_logs")
        self.assertEqual(guangya["tool_call"]["arguments"], {
            "origin": "guangya", "status": "failed", "limit": 20,
        })
        self.assertEqual(calls[0], ("local_media.diagnose", {}))

    def test_guangya_root_and_followup_directory_browse_use_read_only_list(self):
        agent, calls = self._agent("guangya.fs.query")
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            root = agent.query(
                "帮我看下光鸭云盘根目录有哪些文件夹",
                owner="web-owner",
                present=False,
            )
            child = agent.query(
                "打开 3 目录看看里面有什么",
                owner="web-owner",
                conversation_context=[{
                    "role": "assistant",
                    "tool_name": "guangya.fs.query",
                    "text": "已列出光鸭根目录。",
                }],
                trusted_conversation_context=True,
                present=False,
            )
        self.assertEqual(model.call_count, 2)
        self.assertEqual(root["tool_call"]["arguments"]["path"], "/")
        self.assertEqual(child["tool_call"]["arguments"]["path"], "/3")
        self.assertEqual([name for name, _args in calls], [
            "guangya.fs.query", "guangya.fs.query",
        ])

    def test_guangya_directory_natural_language_pagination_reuses_safe_snapshot(self):
        agent, calls = self._agent("guangya.fs.query")
        context = [{
            "role": "assistant",
            "tool_name": "guangya.fs.query",
            "text": "光鸭列表完成：第 1 页展示 10 个对象。",
        }]
        cursor = {
            "observation_ref": "OBS0123456789ABCDEF0123456789ABCDEF",
            "page": 1,
            "page_size": 10,
            "has_more": True,
        }
        with patch(
            "app.agent.orchestrator.latest_guangya_observation_cursor",
            return_value=cursor,
        ), patch.object(agent, "_query_with_model_tools") as model:
            response = agent.query(
                "下一页",
                owner="web-owner",
                conversation_context=context,
                trusted_conversation_context=True,
                present=False,
            )
        model.assert_not_called()
        self.assertEqual(response["tool_call"]["name"], "guangya.fs.query")
        self.assertEqual(calls, [(
            "guangya.fs.query",
            {
                "observation_ref": cursor["observation_ref"],
                "page": 2,
                "page_size": 10,
            },
        )])

    def test_guangya_directory_last_page_does_not_query_or_guess(self):
        agent, calls = self._agent("guangya.fs.query")
        context = [{
            "role": "assistant",
            "tool_name": "guangya.fs.query",
            "text": "光鸭列表完成。",
        }]
        with patch(
            "app.agent.orchestrator.latest_guangya_observation_cursor",
            return_value={
                "observation_ref": "OBS0123456789ABCDEF0123456789ABCDEF",
                "page": 2,
                "page_size": 10,
                "has_more": False,
            },
        ), patch.object(agent, "_query_with_model_tools") as model:
            response = agent.query(
                "还有呢",
                owner="web-owner",
                conversation_context=context,
                trusted_conversation_context=True,
                present=False,
            )
        model.assert_not_called()
        self.assertEqual(calls, [])
        self.assertIn("最后一页", response["result"]["summary"])

    def test_arbitrary_guangya_cleanup_starts_with_read_only_frozen_preview(self):
        agent, calls = self._agent("guangya.organize.cleanup.preview")
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            response = agent.query(
                "清理光鸭根目录 3 中的空目录和垃圾残余",
                owner="web-owner",
                present=False,
            )
        model.assert_called_once()
        self.assertEqual(
            response["tool_call"]["name"],
            "guangya.organize.cleanup.preview",
        )
        self.assertEqual(calls, [(
            "guangya.organize.cleanup.preview",
            {"path": "/3", "max_candidates": 500, "scope": "all"},
        )])

    def test_media_metadata_wording_and_ordinal_rating_keep_discovery_context(self):
        agent, _calls = self._agent("discovery.search", "discovery.lookup_rating")
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            response = agent.query(
                "搜索《暗芝居》的影视资料",
                owner="web-owner",
                present=False,
            )
        model.assert_called_once()
        self.assertEqual(response["tool_call"]["name"], "discovery.search")
        self.assertEqual(response["tool_call"]["arguments"]["query"], "暗芝居")

        agent.recent_discovery_store.capture(
            owner="web-owner",
            result=ToolResult(
                True,
                "success",
                "找到 1 项",
                data={"query": "暗芝居", "items": [{
                    "provider": "tmdb",
                    "external_id": "56559",
                    "media_type": "tv",
                    "title": "暗芝居",
                    "year": "2013",
                }]},
            ),
        )
        with patch.object(agent, "_query_with_model_tools") as model:
            rating = agent.query(
                "第一个评分多少？",
                owner="web-owner",
                present=False,
            )
        model.assert_not_called()
        self.assertEqual(rating["tool_call"]["name"], "discovery.lookup_rating")
        self.assertEqual(rating["tool_call"]["arguments"], {
            "query": "暗芝居",
            "media_type": "tv",
            "year": "2013",
            "allow_web_fallback": True,
        })

    def test_year_specific_new_donghua_recommendation_runs_catalog_and_public_web(self):
        agent, calls = self._agent("discovery.recommend", "web.search")
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            response = agent.query(
                "2026 年有哪些新国漫？",
                owner="web-owner",
                present=False,
            )
        model.assert_called_once()
        self.assertEqual(response["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(calls[0], (
            "discovery.recommend",
            {
                "provider": "tmdb",
                "media_type": "tv",
                "page": 1,
                "limit": 10,
                "year": "2026",
                "region": "中国大陆",
            },
        ))
        self.assertEqual(calls[1][0], "web.search")
        self.assertIn("2026", calls[1][1]["query"])
        self.assertIn("动画剧集", calls[1][1]["query"])

    def test_existing_media_subscription_is_reported_as_successful_noop(self):
        agent, _calls = self._agent("discovery.search")
        agent.recent_discovery_store.capture(
            owner="web-owner",
            result=ToolResult(
                True,
                "success",
                "找到 1 项",
                data={"query": "光阴之外", "items": [{
                    "provider": "tmdb",
                    "external_id": "281233",
                    "media_type": "tv",
                    "title": "光阴之外",
                    "year": "2025",
                }]},
            ),
        )
        with patch.object(
            agent,
            "prepare",
            side_effect=AgentToolError(
                "该媒体已经在追更订阅中", code="precondition_failed"
            ),
        ):
            response = agent.query(
                "订阅第 1 个，每周检查",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(response["mode"], "conversation")
        self.assertIn("已经在媒体追更订阅中", response["result"]["summary"])
        self.assertIn("无需重复创建", response["result"]["summary"])

    def test_ambiguous_library_refresh_and_bulk_qb_delete_return_specific_safe_guidance(self):
        agent = AgentOrchestrator(ToolRegistry())
        refresh = agent.query("刷新 Jellyfin 媒体库", owner="web-owner", present=False)
        self.assertEqual(refresh["mode"], "clarification")
        self.assertIn("具体", refresh["result"]["summary"])
        self.assertIn("动漫库", " ".join(refresh["result"]["suggestions"]))

        deletion = agent.query(
            "删除 qBittorrent 的全部任务并删除文件",
            owner="web-owner",
            present=False,
        )
        self.assertEqual(deletion["mode"], "clarification")
        self.assertIn("不支持批量删除", deletion["result"]["summary"])
        self.assertIn("不会代为删除下载文件", deletion["result"]["summary"])

    def test_workspace_search_trims_followup_clause_and_media_breakdown_is_exact(self):
        agent, calls = self._agent("workspace.search")
        with patch.object(agent, "_query_with_model_tools", return_value=None):
            response = agent.query(
                "全局搜索一下光阴之外，告诉我媒体库、订阅和任务里分别有什么",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(response["tool_call"]["name"], "workspace.search")
        self.assertEqual(calls[0][1]["query"], "光阴之外")
        self.assertIsNotNone(
            _MEDIA_LIBRARY_TOTAL_PATTERN.search(
                "我的媒体库总共有多少部电影、多少部剧、多少集？"
            )
        )

    def test_local_source_and_task_requests_beat_broad_diagnosis(self):
        agent, calls = self._agent(
            "local_media.source_summaries",
            "local_media.task_summaries",
            "local_media.diagnose",
        )
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            sources = agent.query(
                "本地整理配置了哪些来源？哪些开启了 qB 完成自动接管？",
                owner="web-owner",
                present=False,
            )
            tasks = agent.query(
                "列出最近的本地整理任务，重点看失败和待确认",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(model.call_count, 2)
        self.assertEqual(sources["tool_call"]["name"], "local_media.source_summaries")
        self.assertEqual(tasks["tool_call"]["name"], "local_media.task_summaries")
        self.assertEqual(tasks["tool_call"]["arguments"]["scope"], "attention")
        self.assertEqual([name for name, _args in calls], [
            "local_media.source_summaries", "local_media.task_summaries",
        ])

    def test_runtime_indexer_and_safety_questions_use_deterministic_reads(self):
        agent, calls = self._agent(
            "agent.runtime_status",
            "config.indexer_sites_summary",
            "agent.capabilities",
            "config.safe_policy_summary",
        )
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            runtime = agent.query(
                "Agent、Telegram 接入和模型路由现在是否启用？",
                owner="web-owner",
                present=False,
            )
            indexers = agent.query(
                "索引站配置了哪些？哪些开启、关闭或不可搜索？",
                owner="web-owner",
                present=False,
            )
            safety = agent.query(
                "Agent 当前有哪些安全限制？哪些操作一定要二次确认？",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(model.call_count, 3)
        self.assertEqual(runtime["tool_call"]["name"], "agent.runtime_status")
        self.assertEqual(indexers["tool_call"]["name"], "config.indexer_sites_summary")
        self.assertEqual(safety["tool_call"]["name"], "agent.read_plan")
        self.assertEqual([name for name, _args in calls], [
            "agent.runtime_status",
            "config.indexer_sites_summary",
            "agent.capabilities",
            "config.safe_policy_summary",
        ])

    def test_rss_constraints_and_named_media_controls_keep_the_requested_action(self):
        self.assertEqual(
            rss_subscription_control_name_request(
                "把蜜柑 RSS 的自动刷新周期改成 6 小时，保持下载策略不变"
            ),
            ("rss.update_subscription", "蜜柑", {"refresh_interval_minutes": 360}),
        )
        self.assertTrue(is_rss_subscription_refresh_write_message(
            "立即刷新全部 RSS 订阅，但不要直接下载未确认的资源"
        ))
        self.assertEqual(
            media_subscription_control_name_request(
                "暂停《沧元图》的媒体追更订阅，不要删除"
            ),
            ("沧元图", False),
        )

    def test_named_watchlist_addition_searches_before_requesting_candidate(self):
        self.assertEqual(
            discovery_watchlist_title_request("把《暗芝居》加入探索想看列表"),
            "暗芝居",
        )
        agent, calls = self._agent("discovery.search")
        with patch.object(agent, "_query_with_model_tools") as model:
            response = agent.query(
                "把《暗芝居》加入探索想看列表",
                owner="web-owner",
                present=False,
            )
        model.assert_not_called()
        self.assertEqual(response["tool_call"]["name"], "discovery.search")
        self.assertEqual(calls, [("discovery.search", {"query": "暗芝居", "limit": 20})])
        self.assertIn("加入探索收藏", " ".join(response["result"]["suggestions"]))

    def test_telegram_and_proxy_restart_phrasing_reach_confirmation_gate(self):
        self.assertTrue(is_telegram_test_notification_message(
            "给我的 Telegram 发送一条测试通知"
        ))
        self.assertIsNone(media_proxy_restart_request("重启当前启用的媒体反代实例"))

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="media_proxy.status_summary",
            description="status",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda _arguments: ToolResult(
                True,
                "ready",
                "ready",
                data={"instances": [{"instance_number": 1, "enabled": True}]},
            ),
            validator=_identity,
        ))
        agent = AgentOrchestrator(registry)

        def prepared(tool_name, arguments, *, owner):
            calls.append((tool_name, dict(arguments)))
            return {
                "mode": "confirmation_required",
                "tool_call": {"name": tool_name, "arguments": dict(arguments)},
                "result": {"ok": True, "status": "confirmation_required", "summary": "prepared"},
            }

        with patch.object(agent, "prepare", side_effect=prepared), patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as model:
            telegram = agent.query(
                "给我的 Telegram 发送一条测试通知",
                owner="web-owner",
                present=False,
            )
            restart = agent.query(
                "重启当前启用的媒体反代实例",
                owner="web-owner",
                present=False,
            )
        self.assertEqual(model.call_count, 2)
        self.assertEqual(telegram["tool_call"]["name"], "telegram.send_test_notification")
        self.assertEqual(restart["tool_call"]["name"], "media_proxy.restart_instance")
        self.assertEqual(calls, [
            ("telegram.send_test_notification", {}),
            ("media_proxy.restart_instance", {"instance_number": 1}),
        ])


if __name__ == "__main__":
    unittest.main()
