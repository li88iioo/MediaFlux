"""Agent 能力收敛契约：防止旧工具、隐藏写入口和领域覆盖回归。"""

from __future__ import annotations

from pathlib import Path

from app.agent.domain_catalog import build_tool_specs
from app.agent.provider_operations import build_provider_catalog
from app.agent.public_safety import public_tool_label

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_TOOL_NAMES = {
    "downloads.pause_task",
    "downloads.resume_task",
    "downloads.delete_task",
    "library.refresh_library",
    "indexer.submit_resource",
    "indexer.submit_resource_batch",
    "rss.set_subscription_enabled",
    "rss.set_refresh_interval",
    "guangya.change_plan.execute",
    "guangya.media_hygiene.execute",
    "guangya.organize.clean_empty",
}
_REQUIRED_PROJECT_TOOLS = {
    "agent.capabilities",
    "agent.runtime_status",
    "automation.diagnose_pipeline",
    "workspace.briefing",
    "workspace.health",
    "workspace.next_actions",
    "workspace.search",
    "workspace.todo",
    "provider.capabilities",
    "provider.query",
    "provider.change.preview",
    "provider.change.execute",
    "provider.job.status",
    "config.diagnose",
    "config.diagnose_media_servers",
    "config.explain_component",
    "config.feature_summary",
    "config.safe_policy_summary",
    "config.set_feature_state",
    "config.set_safe_policy",
    "library.search",
    "library.batch_presence",
    "library.audit_episodes",
    "library.audit_library_episodes",
    "library.check_updates",
    "library.count_series_episodes",
    "library.search_missing_episode_resources",
    "library.search_missing_season_resources",
    "library.patrol_status",
    "library.set_patrol_policy",
    "library.trigger_patrol_now",
    "downloads.diagnose_queue",
    "downloads.request_summaries",
    "downloads.retry_submission",
    "config.indexer_sites_summary",
    "config.set_indexer_sites",
    "indexer.diagnose_readiness",
    "indexer.search_resources",
    "ingest.inspect",
    "ingest.submit",
    "ingest.status",
    "web.read",
    "web.search",
    "bangumi.calendar",
    "discovery.search",
    "discovery.person_filmography",
    "discovery.detail",
    "discovery.recommend",
    "discovery.lookup_rating",
    "discovery.mapping_candidates",
    "discovery.confirm_mapping",
    "discovery.watchlist_summaries",
    "discovery.get_watchlist_summary",
    "discovery.add_watchlist",
    "discovery.remove_watchlist",
    "media.subscription_summaries",
    "media.subscription_updates",
    "media.get_subscription_summary",
    "media.get_subscription_policy",
    "media.create_subscription",
    "media.set_subscription_policy",
    "media.set_subscription_enabled",
    "media.delete_subscription",
    "media.subscription_notification_rule",
    "media.set_subscription_notification_rule",
    "media.reset_subscription_notification_rule",
    "media.continue_watching",
    "media.recently_added",
    "media.recently_played",
    "media.recommend_from_library",
    "guangya.capabilities",
    "guangya.account.status",
    "guangya.connection_status",
    "guangya.fs.query",
    "guangya.fs.change.preview",
    "guangya.fs.change.execute",
    "guangya.recycle.list",
    "guangya.recycle.restore",
    "guangya.recycle.clear",
    "guangya.operation.status",
    "guangya.share.list",
    "guangya.share.create",
    "guangya.share.revoke",
    "guangya.organize.preview",
    "guangya.organize.run_once",
    "guangya.organize.status",
    "guangya.organize.cleanup.classify",
    "guangya.organize.cleanup.preview",
    "guangya.organize.cleanup.execute",
    "guangya.rename.preview",
    "guangya.rename.execute",
    "guangya.directory_scrape.inspect",
    "guangya.directory_scrape.search",
    "guangya.directory_scrape.preview",
    "guangya.directory_scrape.run",
    "strm.diagnose",
    "strm.status",
    "strm.run_history",
    "strm.run_once",
    "strm.retry_failures",
    "strm.schedule_policy",
    "strm.set_schedule_policy",
    "local_media.diagnose",
    "local_media.source_summaries",
    "local_media.get_source_summary",
    "local_media.task_summaries",
    "local_media.inspect_task",
    "local_media.preview_task",
    "local_media.scan_sources",
    "local_media.retry_task",
    "local_media.refresh_task_library",
    "local_media.verify_task_library_visibility",
    "local_media.set_source_trigger_enabled",
    "rss.diagnose",
    "rss.subscription_summaries",
    "rss.get_subscription_summary",
    "rss.create_subscription",
    "rss.update_subscription",
    "rss.delete_subscription",
    "rss.recent_activity",
    "rss.entry_summaries",
    "rss.mark_entries",
    "rss.refresh_subscription",
    "rss.refresh_subscriptions",
    "rss.submit_entries_to_qb",
    "rss.submit_pending_to_qb",
    "rss.retry_failed",
}
_REQUIRED_PROVIDER_OPERATIONS = {
    "media.system.info",
    "media.libraries.list",
    "media.library.counts",
    "media.items.search",
    "media.items.recent_added",
    "media.items.recent_played",
    "media.items.continue_watching",
    "media.series.search",
    "media.series.episodes",
    "media.library.refresh",
    "media.item.refresh",
    "qb.app.version",
    "qb.transfer.info",
    "qb.torrents.info",
    "qb.torrents.files",
    "qb.torrents.pause",
    "qb.torrents.resume",
    "qb.torrents.delete_task",
}


def test_provider_catalog_is_the_only_external_media_and_qb_write_surface() -> None:
    operations = {
        spec.operation_id: spec for spec in build_provider_catalog().operations()
    }
    assert _REQUIRED_PROVIDER_OPERATIONS <= operations.keys()
    for operation in (
        "media.library.refresh",
        "media.item.refresh",
        "qb.torrents.pause",
        "qb.torrents.resume",
        "qb.torrents.delete_task",
    ):
        assert operations[operation].risk.value != "read"


def test_all_registered_tools_have_one_complete_kernel_lifecycle() -> None:
    """防止新增能力只有声明、只有预览或引用到不存在的下一环。"""
    specs = build_tool_specs()
    by_name = {spec.name: spec for spec in specs}
    assert len(by_name) == len(specs)

    for spec in specs:
        assert public_tool_label(spec.name) != "项目操作", spec.name
        for related in spec.related_tools:
            assert related in by_name, f"{spec.name} -> {related}"
        if spec.risk.value == "read":
            assert spec.handler is not None or spec.context_handler is not None, spec.name
            assert not spec.requires_confirmation, spec.name
            continue
        assert spec.requires_confirmation, spec.name
        assert spec.context_confirmation_preparer is not None, spec.name
        assert spec.context_confirmed_handler is not None, spec.name


def test_provider_operations_are_bounded_and_writes_are_reference_scoped() -> None:
    for spec in build_provider_catalog().operations():
        assert spec.max_items > 0, spec.operation_id
        assert spec.timeout_seconds > 0, spec.operation_id
        if spec.risk.value != "read":
            assert spec.reference_arguments, spec.operation_id


def test_batch_updates_use_one_read_and_recheck_the_persisted_list(monkeypatch) -> None:
    """十部片单与省略标题的续问穿过真实目录/Kernel/审计；不连接真实媒体库。"""
    import asyncio
    import json
    from collections import Counter

    from app.agent import episode_audit
    from app.agent.kernel.capabilities import CapabilityRetriever
    from app.agent.kernel.events import AgentEventType
    from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
    from app.agent.kernel.persistence import SQLiteKernelStore
    from app.agent.kernel.pipeline import ToolPipeline
    from app.agent.kernel.ports import catalog_from_tool_specs
    from app.agent.kernel.session import AgentSession, SessionLimits
    from app.agent.kernel.state import AgentInput
    from tests.support import isolated_test_database
    from tests.test_agent_kernel_core import ScriptedModel

    titles = ["师兄啊师兄", "凡人修仙传", "光阴之外", "择日飞升", "大主宰",
              "牧神记", "沧元图", "一斩苍穹", "东大高武学院", "将夜"]
    identities = {title: str(10000 + index) for index, title in enumerate(titles)}
    inventory = [(1, 1)]
    reads = []

    def inspect(query, **_kwargs):
        reads.append(query)
        return [{
            "server_type": "jellyfin", "server_name": "测试媒体库", "status": "ready",
            "selected": {"name": query, "year": "2026", "tmdb_id": identities[query]},
            "episodes": list(inventory), "truncated": False,
        }]

    class TMDB:
        def detail(self, tmdb_id, _media_type):
            return {"name": next(title for title in titles if identities[title] == tmdb_id),
                    "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}]}

        def tv_season_detail(self, _tmdb_id, _season):
            return {"episodes": [{"episode_number": number, "air_date": "2026-01-01"} for number in (1, 2)]}

    monkeypatch.setattr(episode_audit, "inspect_series_episode_sources", inspect)
    monkeypatch.setattr(episode_audit, "TMDBClient", TMDB)
    monkeypatch.setattr(episode_audit, "close_tmdb_client", lambda _client: None)
    episode_audit.reset_episode_audit_cache_for_tests()

    async def run():
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = SQLiteKernelStore()
        for turn in (1, 2):
            if turn == 2:
                inventory.append((1, 2))
            model = ScriptedModel([
                [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                    f"batch-{turn}", "library.check_updates",
                    {"queries": titles, "media_type": "tv", "as_of": "2026-08-01"},
                )), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
                [ModelEvent(ModelEventType.TEXT_DELTA, text="本轮已逐部核对，未检索全网资源。"),
                 ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
            ])
            # 重建会话对象，验证续问依赖持久对话，而不是旧实例中的临时片单。
            session = AgentSession(
                model=model, catalog=catalog, retriever=CapabilityRetriever(),
                pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
                limits=SessionLimits(max_tool_calls=2, max_model_rounds=4),
            )
            events = [event async for event in session.run(AgentInput(
                owner="batch-owner", session_id="batch-session", request_id=f"request-{turn}",
                message="媒体库中的 " + "/".join(titles) + " 都有更新吗？" if turn == 1 else "再检查一下更新",
            ))]
            selected = next(event for event in events if event.type == AgentEventType.CAPABILITIES_SELECTED)
            assert "library.check_updates" in selected.payload["tools"]
            assert events[-1].type == AgentEventType.TURN_COMPLETED
            assert events[-1].payload["tool_calls"] == 1
            assert not any(event.type == AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
            tool_message = next(message for message in reversed(model.requests[-1].messages) if message.role == "tool")
            data = json.loads(tool_message.content)["data"]
            assert [item["query"] for item in data["items"]] == titles
            assert {item["status"] for item in data["items"]} == {"updates_available" if turn == 1 else "up_to_date"}
            assert data["counts"]["requested"] == 10
            if turn == 2:
                history = "\n".join(message.content for message in model.requests[0].messages)
                assert all(title in history for title in titles)
        assert Counter(reads) == Counter({title: 2 for title in titles})

    try:
        with isolated_test_database():
            asyncio.run(run())
    finally:
        episode_audit.reset_episode_audit_cache_for_tests()
