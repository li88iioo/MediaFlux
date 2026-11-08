"""Agent 能力收敛契约：防止旧工具、隐藏写入口和领域覆盖回归。"""

from __future__ import annotations

from pathlib import Path

from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.public_safety import public_tool_label

_REPO_ROOT = Path(__file__).resolve().parents[1]






def test_watchlist_reads_use_native_services_and_writes_keep_confirmation() -> None:
    import importlib.util
    from app import sdk
    from app.agent.kernel.capabilities import ToolEffect
    from app.agent.kernel.ports.sdk_effects import watchlist_tools

    catalog = build_tool_catalog()
    for retired in ("library.search", "discovery.watchlist_summaries", "discovery.get_watchlist_summary",
                    "discovery.add_watchlist", "discovery.remove_watchlist"):
        assert not catalog.has(retired) and not catalog.has(retired.replace(".", "__"))
    assert importlib.util.find_spec("app.agent.domains.discovery.watchlist_actions") is None
    reads = {"watchlist.list", "watchlist.get", "watchlist.statistics"}
    assert reads <= sdk.METHODS.keys() and reads.isdisjoint(sdk.WRITE_METHODS)
    for native in watchlist_tools():
        assert catalog.has(native.name)
        assert native.effect is ToolEffect.WRITE and native.prepare is not None and native.execute_confirmed is not None
    assert catalog.has("watchlist.add") and catalog.has("watchlist.remove")


def test_rss_delivery_has_only_native_sdk_entries() -> None:
    from app import sdk
    from app.agent.kernel.ports.sdk_effects import rss_delivery_tools
    old = {"rss.entry_summaries", "rss.mark_entries", "rss.refresh_subscriptions", "rss.retry_failed", "rss.submit_entries_to_qb", "rss.submit_pending_to_qb"}
    assert old.isdisjoint({spec.name for spec in build_tool_catalog().visible()})
    assert {spec.name for spec in rss_delivery_tools()} == {"rss.mark", "rss.refresh", "rss.retry", "rss.download"}
    assert callable(sdk.rss.entries)
    for name in ("rss_entry_actions", "rss_refresh_actions", "rss_download_actions", "rss_retry_actions"):
        assert not (_REPO_ROOT / "app/agent/domains/subscriptions" / (name + ".py")).exists()


def test_cloud_change_uses_native_write_gate_and_no_legacy_model_spec() -> None:
    from app.agent.kernel.capabilities import ToolEffect
    from app.agent.kernel.ports.sdk_effects import cloud_change_tool
    from app import sdk

    tool = cloud_change_tool()
    assert tool.effect is ToolEffect.WRITE
    assert tool.read is None and tool.prepare is not None and tool.execute_confirmed is not None
    assert set(tool.input_schema["properties"]) == {"operations", "trigger_strm"}
    assert sdk.METHODS["cloud.episodes"] == sdk.cloud.episodes
    assert sdk.METHODS["cloud.preview_episodes"] == sdk.cloud.preview_episodes
    assert "cloud.change" in sdk.WRITE_METHODS


def test_guangya_fs_change_retired_preview_has_no_runtime_metadata_or_alias() -> None:
    from app.agent import (
        action_history,
        confirmation_contract,
        public_safety,
        rate_limit,
    )

    execute = "cloud.change"
    retired = "guangya.fs.change.preview"
    mappings = (
        action_history._TOOL_LABELS,
        action_history._SAFE_FIELDS,
        confirmation_contract._CONFIRMATION_COPY,
        public_safety._PUBLIC_TOOL_LABELS,
        public_safety._PUBLIC_TOOL_ALIASES,
        rate_limit._TOOL_RATE_LIMIT_SCOPES,
        rate_limit._TOOL_RATE_LIMITS,
    )
    for mapping in mappings:
        assert retired not in mapping
        assert retired.replace(".", "__") not in mapping
    assert action_history._TOOL_LABELS[execute] == public_tool_label(execute) == "光鸭文件变更"
    assert public_tool_label(execute.replace(".", "__")) == "光鸭文件变更"
    from app import sdk
    assert callable(sdk.cloud.change)


def test_guangya_fs_change_uses_one_policy_with_separate_confirmation_budget(monkeypatch) -> None:
    from app.agent import rate_limit

    execute = "cloud.change"
    # 真实光鸭写入验收（2026-11-04）：预检、过期重预检、下一项预检在一分钟内即触顶 3 次。
    assert rate_limit.tool_rate_limit_policy(execute) == (f"tool:{execute}", 6, 1)
    limiter = rate_limit.AgentRateLimiter(clock=lambda: 100.0)
    monkeypatch.setattr(rate_limit, "agent_rate_limiter", limiter)
    for remaining in (5, 4, 3, 2, 1, 0):
        decision = rate_limit.consume_agent_tool("fs-owner", execute)
        assert decision.allowed and decision.remaining == remaining
    assert not rate_limit.consume_agent_tool("fs-owner", execute).allowed
    # 准备额度耗尽不能阻止用户确认已有卡片；仍使用唯一 execute 的限流合同。
    confirmed = rate_limit.consume_agent_tool("fs-owner", execute, scope_suffix="confirmed")
    assert confirmed.allowed and confirmed.limit == 6 and confirmed.remaining == 5




def test_native_updates_refreshes_the_same_persisted_list(monkeypatch, tmp_path) -> None:
    import json
    from app.modules import episode_audit
    from app.sdk import library
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

    saved = tmp_path / "queries.json"
    saved.write_text(json.dumps(titles))
    try:
        for turn in (1, 2):
            if turn == 2:
                inventory.append((1, 2))
            result = library.updates(queries=json.loads(saved.read_text()), media_type="tv", as_of="2026-08-01")
            data = result["data"]
            assert [item["query"] for item in data["items"]] == titles
            assert {item["status"] for item in data["items"]} == {"updates_available" if turn == 1 else "up_to_date"}
            assert data["counts"]["requested"] == 10
            assert data["resource_search_status"] == "not_checked"
        assert sorted(reads) == sorted(titles * 2)
    finally:
        episode_audit.reset_episode_audit_cache_for_tests()


def test_library_inventory_has_native_reads_without_legacy_modules_or_live_metadata():
    import importlib.util
    from app import sdk
    from app.agent import rate_limit

    catalog = build_tool_catalog()
    for retired, module in (
        ("library.batch_presence", "batch_presence"),
        ("library.count_series_episodes", "episode_count"),
    ):
        assert not catalog.has(retired) and not catalog.has(retired.replace(".", "__"))
        assert importlib.util.find_spec(f"app.agent.domains.library.{module}") is None
        for mapping in (
                        rate_limit._TOOL_RATE_LIMIT_SCOPES, rate_limit._TOOL_RATE_LIMITS):
            assert retired not in mapping
        # 历史记录仍能显示原业务名称；不能再成为执行入口。
        assert public_tool_label(retired) != retired
    assert {"library.presence", "library.episodes"} <= sdk.METHODS.keys()
    assert {"library.presence", "library.episodes"}.isdisjoint(sdk.WRITE_METHODS)


def test_library_audit_updates_have_no_legacy_execution_entries():
    import importlib.util
    from app import sdk
    from app.agent import rate_limit
    catalog = build_tool_catalog()
    for name in ("library.audit_episodes", "library.check_updates"):
        assert not catalog.has(name) and not catalog.has(name.replace(".", "__"))
    assert importlib.util.find_spec("app.agent.domains.library.update_actions") is None
    assert {"library.audit", "library.updates"} <= sdk.METHODS.keys()
    assert {"library.audit", "library.updates"}.isdisjoint(sdk.WRITE_METHODS)
    for name in ("library.audit_episodes", "library.check_updates", "library.audit", "library.updates"):
        assert name not in rate_limit._TOOL_RATE_LIMIT_SCOPES
        assert name not in rate_limit._TOOL_RATE_LIMITS


def test_schedule_policies_have_one_business_service_and_native_sdk_confirmation():
    import importlib.util
    from app import sdk
    from app.agent.kernel.capabilities import ToolEffect
    from app.agent.kernel.ports.sdk_effects import schedule_policy_tools

    catalog = build_tool_catalog()
    reads = {"strm.schedule", "cloud.organize_schedule", "library.patrol_policy"}
    writes = {"strm.set_schedule", "cloud.set_organize_schedule", "library.set_patrol_policy"}
    assert reads <= sdk.METHODS.keys() and reads.isdisjoint(sdk.WRITE_METHODS)
    assert writes <= sdk.WRITE_METHODS
    for module in ("app.agent.policy_confirmation", "app.agent.domains.strm.schedule_actions",
                   "app.agent.domains.guangya.schedule_actions", "app.agent.domains.library.patrol_config_actions"):
        assert importlib.util.find_spec(module) is None
    for tool in schedule_policy_tools():
        assert catalog.has(tool.name)
        assert tool.effect is ToolEffect.WRITE and tool.prepare is not None and tool.execute_confirmed is not None
        assert public_tool_label(tool.name) != tool.name
    assert writes == {tool.name for tool in schedule_policy_tools()}

def test_activity_follow_uses_native_ids_and_public_repository_only():
    import importlib.util

    from app import sdk
    from app.agent.kernel.ports.sdk_effects import activity_follow_tools

    assert importlib.util.find_spec("app.agent.domains.activity.follow_actions") is None
    names = {"activity.follow", "activity.follows", "activity.unfollow"}
    assert names <= sdk.METHODS.keys()
    assert names & sdk.WRITE_METHODS == {"activity.follow", "activity.unfollow"}
    specs = {tool.name: tool for tool in activity_follow_tools()}
    assert specs.keys() == {"activity.follow", "activity.unfollow"}
    assert specs["activity.follow"].input_schema["required"] == ["kind", "identifier"]
    assert "activity_selection_ref" not in specs["activity.follow"].input_schema["properties"]
    assert all("owner" not in item["signature"] and "chat_id" not in item["signature"]
               for item in sdk.describe_methods() if item["name"] in names)


def test_media_subscriptions_have_one_native_create_and_no_legacy_actions():
    import importlib.util
    from app import sdk
    from app.agent.kernel.ports.sdk_effects import subscription_tools, automation_tools

    assert {"subscriptions.list", "subscriptions.get", "subscriptions.create", "subscriptions.update", "subscriptions.delete",
            "subscriptions.check_updates", "subscriptions.notification", "subscriptions.set_notification", "subscriptions.reset_notification", "subscriptions.today"} <= sdk.METHODS.keys()
    assert "automation.create_media_rule" not in sdk.METHODS
    assert {tool.name for tool in automation_tools()} == {"automation.set_digest"}
    assert {tool.name for tool in subscription_tools()} == {"subscriptions.create", "subscriptions.update", "subscriptions.delete", "subscriptions.set_notification", "subscriptions.reset_notification"}
    for name in ("actions", "queries", "policy", "common", "consumption_actions"):
        assert importlib.util.find_spec("app.agent.domains.subscriptions." + name) is None
