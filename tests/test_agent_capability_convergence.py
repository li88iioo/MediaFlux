"""Agent 能力收敛契约：防止旧工具、隐藏写入口和领域覆盖回归。"""
from __future__ import annotations

from pathlib import Path

from app.agent.provider_operations import build_provider_catalog
from app.agent.tools import build_tool_registry

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
}

_REQUIRED_PROJECT_TOOLS = {
    # 工作台与统一 Provider 网关。
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
    # 配置诊断与受控开关。
    "config.diagnose",
    "config.diagnose_media_servers",
    "config.explain_component",
    "config.feature_summary",
    "config.safe_policy_summary",
    "config.set_feature_state",
    "config.set_safe_policy",
    # 媒体库读取、缺集审计与巡检。
    "library.search",
    "library.audit_episodes",
    "library.audit_library_episodes",
    "library.check_updates",
    "library.count_series_episodes",
    "library.search_missing_episode_resources",
    "library.search_missing_season_resources",
    "library.patrol_status",
    "library.set_patrol_policy",
    "library.trigger_patrol_now",
    # 下载请求管理；qB 原生暂停/恢复/删除由 Provider operation 承担。
    "downloads.diagnose_queue",
    "downloads.request_summaries",
    "downloads.retry_submission",
    # 资源站：状态、站点开关、搜索和按会话候选提交。
    "config.indexer_sites_summary",
    "config.set_indexer_sites",
    "indexer.diagnose_readiness",
    "indexer.search_resources",
    "indexer.submit_candidate",
    "indexer.submit_candidates",
    # 探索：搜索、详情、推荐、评分、Web/Tavily 辅助、映射和收藏闭环。
    "web.search",
    "bangumi.calendar",
    "discovery.search",
    "discovery.detail",
    "discovery.recommend",
    "discovery.lookup_rating",
    "discovery.mapping_candidates",
    "discovery.confirm_mapping",
    "discovery.watchlist_summaries",
    "discovery.get_watchlist_summary",
    "discovery.add_watchlist",
    "discovery.remove_watchlist",
    # 媒体追更：查询、创建、策略、通知、启停和删除闭环。
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
    # 光鸭：读能力、预检/确认写入、整理、清理、刮削和调度闭环。
    "guangya.capabilities",
    "guangya.connection_status",
    "guangya.fs.query",
    "guangya.fs.change.preview",
    "guangya.fs.change.execute",
    "guangya.organize.preview",
    "guangya.organize.run_once",
    "guangya.organize.status",
    "guangya.organize.clean_empty",
    "guangya.organize.cleanup.classify",
    "guangya.organize.cleanup.preview",
    "guangya.organize.cleanup.execute",
    "guangya.rename.preview",
    "guangya.rename.execute",
    "guangya.directory_scrape.inspect",
    "guangya.directory_scrape.search",
    "guangya.directory_scrape.preview",
    "guangya.directory_scrape.run",
    # STRM：诊断、状态、历史、运行、失败重试和调度策略。
    "strm.diagnose",
    "strm.status",
    "strm.run_history",
    "strm.run_once",
    "strm.retry_failures",
    "strm.schedule_policy",
    "strm.set_schedule_policy",
    # 本地整理：来源、任务、预览、扫描、重试和媒体库可见性闭环。
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
    # RSS：查询、完整配置、刷新、条目处理和下载提交闭环。
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
    "rss.retry_failed_to_qb",
}

_REQUIRED_PROVIDER_OPERATIONS = {
    "media.system.info",
    "media.libraries.list",
    "media.items.search",
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


def test_every_registered_project_tool_is_exposed_through_one_llm_contract() -> None:
    registry = build_tool_registry()
    capabilities = {item["name"]: item for item in registry.capabilities()}
    llm_reads = {item["name"] for item in registry.llm_read_capabilities()}
    llm_confirmations = {
        item["name"] for item in registry.llm_confirmation_capabilities()
    }

    assert not (_FORBIDDEN_TOOL_NAMES & capabilities.keys())
    assert _REQUIRED_PROJECT_TOOLS <= capabilities.keys()
    assert llm_reads.isdisjoint(llm_confirmations)
    assert llm_reads | llm_confirmations == capabilities.keys()

    for name, capability in capabilities.items():
        if capability["risk"] == "read":
            assert name in llm_reads
            assert capability["requires_confirmation"] is False
        else:
            assert name in llm_confirmations
            assert capability["requires_confirmation"] is True


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


def test_removed_legacy_action_modules_and_web_calls_do_not_return() -> None:
    assert not (_REPO_ROOT / "app/agent/download_control_actions.py").exists()
    assert not (_REPO_ROOT / "app/agent/media_library_actions.py").exists()

    database_source = (_REPO_ROOT / "app/database.py").read_text(encoding="utf-8")
    telegram_schema = database_source.split(
        "CREATE TABLE IF NOT EXISTS telegram_agent_actions", 1
    )[1].split(");", 1)[0]
    assert "result_id" not in telegram_schema

    web_source = (_REPO_ROOT / "app/static/js/agent.js").read_text(encoding="utf-8")
    assert "indexer.submit_resource" not in web_source
    assert "data-agent-resource-id" not in web_source
    assert "/api/agent/actions/indexer.submit_candidate/prepare" in web_source
    assert "data-agent-resource-position" in web_source
