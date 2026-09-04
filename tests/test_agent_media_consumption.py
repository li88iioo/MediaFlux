"""媒体消费、显式偏好、今日摘要与通知规则的 Agent 回归。"""

from __future__ import annotations

from unittest.mock import Mock, patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.media_consumption_actions import (
    continue_watching_arguments,
    notification_rule_update_arguments,
    preferences_update_arguments,
    recently_played_arguments,
)
from app.agent.models import ToolResult
from app.agent.provider_models import ProviderGatewayError
from app.agent.rate_limit import agent_rate_limiter
from app.modules.media_server_profiles import MediaServerProfile
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


class MediaConsumptionAgentTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_subscription_notification_outbox")
            conn.execute("DELETE FROM media_subscription_notification_rules")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
            conn.execute("DELETE FROM agent_media_preferences")
            conn.execute("DELETE FROM agent_action_history")
            conn.execute("DELETE FROM download_log")
        reset_agent_service_for_tests()
        self.sid = db.add_media_subscription(
            provider="tmdb",
            external_id="12345",
            tmdb_id="12345",
            media_type="tv",
            title="示例追更",
            original_title="PRIVATE ORIGINAL",
            year="2026",
            poster_key="/private/poster.jpg",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
            enabled=True,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def test_registry_exposes_read_and_confirmation_tools(self) -> None:
        service = get_agent_service()
        capabilities = {item["name"]: item for item in service.capabilities()["tools"]}
        for name in (
            "media.continue_watching",
            "media.recently_added",
            "media.recently_played",
            "media.preferences",
            "media.today_summary",
            "media.subscription_notification_rule",
            "media_proxy.playback_failure_summary",
        ):
            self.assertEqual(capabilities[name]["risk"], "read")
            self.assertFalse(capabilities[name]["requires_confirmation"])
        for name in (
            "media.set_preferences",
            "media.clear_preferences",
            "media.set_subscription_notification_rule",
            "media.reset_subscription_notification_rule",
        ):
            self.assertEqual(capabilities[name]["risk"], "low_write")
            self.assertTrue(capabilities[name]["requires_confirmation"])

    def test_validators_are_strict_and_bounded(self) -> None:
        self.assertEqual(
            continue_watching_arguments({}), {"server": "auto", "limit": 8}
        )
        self.assertEqual(
            recently_played_arguments({"server": "emby", "limit": 20}),
            {"server": "emby", "limit": 20},
        )
        self.assertEqual(
            preferences_update_arguments({"preferred_download_target": "both"}),
            {"preferred_download_target": "both"},
        )
        self.assertEqual(
            notification_rule_update_arguments(
                {"subscription_number": 1, "enabled": True, "notify_on_missing": True}
            ),
            {"subscription_number": 1, "enabled": True, "notify_on_missing": True},
        )
        with self.assertRaises(AgentToolError):
            continue_watching_arguments({"server": "admin"})
        with self.assertRaises(AgentToolError):
            preferences_update_arguments({"quality_profile": "quality"})
        with self.assertRaises(AgentToolError):
            notification_rule_update_arguments({"subscription_number": 1})

    def test_continue_watching_uses_config_or_default_user_and_redacts_ids(
        self,
    ) -> None:
        service = get_agent_service()
        profile = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://private.local",
            credential="PRIVATE-TOKEN",
            enabled=True,
            user_id="PRIVATE-USER-ID",
        )
        gateway = Mock()
        gateway.query.return_value = ToolResult(
            True,
            "success",
            "Jellyfin 返回 1 项继续观看内容",
            data={
                "server_label": "Jellyfin",
                "user_selection": "配置用户",
                "history_kind": "继续观看",
                "count": 1,
                "items": [{
                    "name": "第 3 集",
                    "series_name": "示例动画",
                    "type": "Episode",
                    "season_number": 1,
                    "episode_number": 3,
                    "progress_percent": 42.5,
                }],
            },
        )
        with (
            patch(
                "app.agent.media_consumption_actions.list_configured_profiles",
                return_value=[profile],
            ),
            patch(
                "app.agent.media_consumption_actions.get_provider_gateway",
                return_value=gateway,
            ),
        ):
            response = service.invoke(
                "media.continue_watching",
                {"server": "auto", "limit": 8},
                owner="owner-a",
            )
        item = response["result"]["data"]["items"][0]
        self.assertEqual(item["series_name"], "示例动画")
        self.assertEqual(item["episode_number"], 3)
        self.assertEqual(item["progress_percent"], 42.5)
        serialized = repr(response)
        for secret in (
            "PRIVATE-ITEM-ID",
            "PRIVATE-USER-ID",
            "PRIVATE-TOKEN",
            "private.local",
        ):
            self.assertNotIn(secret, serialized)
        gateway.query.assert_called_once()
        self.assertEqual(
            gateway.query.call_args.kwargs["operation"],
            "media.items.continue_watching",
        )
        self.assertEqual(gateway.query.call_args.kwargs["arguments"], {"limit": 8})
        failed_gateway = Mock()
        failed_gateway.query.side_effect = ProviderGatewayError(
            "媒体服务器当前不可用", code="provider_unavailable"
        )
        with (
            patch(
                "app.agent.media_consumption_actions.list_configured_profiles",
                return_value=[profile],
            ),
            patch(
                "app.agent.media_consumption_actions.get_provider_gateway",
                return_value=failed_gateway,
            ),
        ):
            unavailable = service.invoke(
                "media.continue_watching",
                {"server": "jellyfin", "limit": 8},
                owner="owner-a",
            )
        self.assertEqual(unavailable["result"]["status"], "provider_unavailable")
        profile_without_user = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://private.local",
            credential="PRIVATE-TOKEN",
            enabled=True,
            user_id="",
        )
        default_gateway = Mock()
        default_gateway.query.return_value = ToolResult(
            True,
            "success",
            "Jellyfin 返回 0 项继续观看内容",
            data={
                "server_label": "Jellyfin",
                "user_selection": "服务器默认用户",
                "history_kind": "继续观看",
                "count": 0,
                "items": [],
            },
        )
        with (
            patch(
                "app.agent.media_consumption_actions.list_configured_profiles",
                return_value=[profile_without_user],
            ),
            patch(
                "app.agent.media_consumption_actions.get_provider_gateway",
                return_value=default_gateway,
            ),
        ):
            fallback = service.invoke("media.continue_watching", {}, owner="owner-a")
        self.assertEqual(fallback["result"]["status"], "success")
        self.assertEqual(
            fallback["result"]["data"]["user_selection"], "服务器默认用户"
        )

    def test_recent_played_uses_provider_gateway_and_selects_recommendation(self) -> None:
        service = get_agent_service()
        profile = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://private.local",
            credential="PRIVATE-TOKEN",
            enabled=True,
            user_id="PRIVATE-USER-ID",
        )
        gateway = Mock()
        gateway.query.return_value = ToolResult(
            True,
            "success",
            "Jellyfin 返回 1 项最近播放记录",
            data={
                "server_label": "Jellyfin",
                "history_kind": "最近播放",
                "count": 1,
                "items": [{"name": "示例电影", "genres": ["科幻"]}],
                "preference_signals": {
                    "recent_titles": ["示例电影"],
                    "top_genres": ["科幻"],
                    "media_types": ["movie"],
                },
            },
        )
        with (
            patch(
                "app.agent.media_consumption_actions.list_configured_profiles",
                return_value=[profile],
            ),
            patch(
                "app.agent.media_consumption_actions.get_provider_gateway",
                return_value=gateway,
            ),
        ):
            response = service.invoke(
                "media.recently_played",
                {"server": "auto", "limit": 8},
                owner="owner-a",
            )
        self.assertEqual(response["result"]["data"]["history_kind"], "最近播放")
        self.assertEqual(
            gateway.query.call_args.kwargs["operation"], "media.items.recent_played"
        )

        from app.agent.kernel.capabilities import CapabilityRetriever

        selection = CapabilityRetriever().retrieve(
            "根据我最近播放的内容推荐一个片单",
            service.catalog,
        )
        self.assertIn("media.recently_played", selection.names)
        self.assertIn("discovery.recommend", selection.names)

    def test_preferences_are_owner_isolated_confirmation_gated_and_clearable(
        self,
    ) -> None:
        service = get_agent_service()
        before = service.invoke("media.preferences", {}, owner="owner-a")
        self.assertFalse(before["result"]["data"]["explicit"])
        prepared = service.prepare(
            "media.set_preferences",
            {"preferred_server": "jellyfin", "preferred_download_target": "both"},
            owner="owner-a",
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        self.assertEqual(confirmed["result"]["status"], "completed")
        owner_a = service.invoke("media.preferences", {}, owner="owner-a")
        owner_b = service.invoke("media.preferences", {}, owner="owner-b")
        self.assertEqual(owner_a["result"]["data"]["preferred_server"], "jellyfin")
        self.assertTrue(owner_a["result"]["data"]["explicit"])
        self.assertEqual(owner_b["result"]["data"]["preferred_server"], "any")
        self.assertFalse(owner_b["result"]["data"]["explicit"])
        clear = service.prepare("media.clear_preferences", {}, owner="owner-a")
        service.confirm(clear["action_plan"]["plan_id"], owner="owner-a")
        cleared = service.invoke("media.preferences", {}, owner="owner-a")
        self.assertFalse(cleared["result"]["data"]["explicit"])
        self.assertEqual(
            cleared["result"]["data"]["preferred_download_target"], "guangya"
        )

    def test_preference_confirmation_detects_stale_revision(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_preferences", {"preferred_server": "jellyfin"}, owner="owner-a"
        )
        from app.agent.action_history import action_history_owner_digest
        from app.repositories.media_experience import set_media_preferences

        changed = set_media_preferences(
            action_history_owner_digest("owner-a"),
            expected_revision=0,
            updates={"preferred_server": "emby"},
        )
        self.assertIsNotNone(changed)
        with self.assertRaises(AgentToolError) as captured:
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        self.assertEqual(captured.exception.code, "confirmation_stale")

    def test_notification_rule_is_revisioned_and_resettable(self) -> None:
        service = get_agent_service()
        defaults = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        self.assertFalse(defaults["result"]["data"]["enabled"])
        self.assertTrue(defaults["result"]["data"]["notify_on_missing"])
        self.assertFalse(defaults["result"]["data"]["explicit"])
        prepared = service.prepare(
            "media.set_subscription_notification_rule",
            {
                "subscription_number": self.sid,
                "enabled": True,
                "notify_on_satisfied": True,
            },
            owner="owner-a",
        )
        service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        explicit = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-b",
        )
        self.assertTrue(explicit["result"]["data"]["enabled"])
        self.assertTrue(explicit["result"]["data"]["notify_on_satisfied"])
        self.assertTrue(explicit["result"]["data"]["explicit"])
        reset = service.prepare(
            "media.reset_subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        service.confirm(reset["action_plan"]["plan_id"], owner="owner-a")
        restored = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        self.assertFalse(restored["result"]["data"]["enabled"])
        self.assertFalse(restored["result"]["data"]["explicit"])

    def test_today_summary_prioritizes_bounded_safe_content_events(self) -> None:
        stamp = db.now()
        revision = int(db.get_media_subscription(self.sid)["revision"])
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO media_subscription_runs(subscription_id,trigger_type,subscription_revision,status,summary,payload_json,error,started_at,finished_at) VALUES(?, 'manual', ?, 'missing', '', '{}', '', ?, ?)",
                (self.sid, revision, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) VALUES('qb', ?, 'success', ?, ?, ?)",
                ("安全标题", stamp, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) VALUES('qb', ?, 'success', ?, ?, ?)",
                ("/private/library/SECRET.mkv", stamp, stamp, stamp),
            )
        response = get_agent_service().invoke(
            "media.today_summary", {}, owner="owner-a"
        )
        data = response["result"]["data"]
        self.assertEqual(data["event_count"], 3)
        self.assertEqual(data["subscription_runs"]["missing"], 1)
        self.assertEqual(data["downloads"]["success"], 2)
        self.assertIn("安全标题", data["content_titles"])
        self.assertNotIn("SECRET", repr(response))
        self.assertLessEqual(len(data["content_titles"]), 8)

    def test_resource_submit_retrieval_includes_saved_preference_reader(self) -> None:
        from app.agent.kernel.capabilities import CapabilityRetriever

        selection = CapabilityRetriever().retrieve(
            "把刚才第 1 个资源按我的默认下载目标提交",
            get_agent_service().catalog,
        )
        self.assertIn("ingest.submit", selection.names)
        self.assertIn("media.preferences", selection.names)
