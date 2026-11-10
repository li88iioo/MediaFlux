"""媒体消费、显式偏好、今日摘要与通知规则的 Agent 回归。"""

from __future__ import annotations

import pytest

from app import database as db
from app.repositories import media_subscriptions as media_subscriptions_repo
from app import sdk
from app.modules.media_subscription_notifications import notification_arguments
from app.modules.media_subscriptions import MediaSubscriptionError
from app.agent.errors import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.modules.media_preferences import validate_preference_updates
from app.sdk import call_owner, describe_methods, preferences
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


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
        self.sid = media_subscriptions_repo.add_media_subscription(
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
        self.assertTrue({"subscriptions.today", "subscriptions.notification"} <= sdk.METHODS.keys())
        self.assertNotIn("subscriptions.today", capabilities)
        self.assertNotIn("subscriptions.notification", capabilities)
        for name in (
            "preferences.update",
            "preferences.clear",
            "subscriptions.set_notification",
            "subscriptions.reset_notification",
        ):
            self.assertEqual(capabilities[name]["risk"], "low_write")
            self.assertTrue(capabilities[name]["requires_confirmation"])

    def test_validators_are_strict_and_bounded(self) -> None:
        self.assertEqual(
            validate_preference_updates({"preferred_download_target": "both"}),
            {"preferred_download_target": "both"},
        )
        self.assertEqual(
            notification_arguments(
                {"subscription_id": 1, "enabled": True, "notify_on_missing": True}
            ),
            {"subscription_id": 1, "enabled": True, "notify_on_missing": True},
        )
        with self.assertRaises(ValueError):
            validate_preference_updates({"quality_profile": "quality"})
        with self.assertRaises(MediaSubscriptionError):
            notification_arguments({"subscription_id": 1})






    def test_preferences_are_owner_isolated_confirmation_gated_and_clearable(
        self,
    ) -> None:
        service = get_agent_service()
        with call_owner("owner-a"):
            self.assertFalse(preferences.get()["explicit"])
        prepared = service.prepare(
            "preferences.update",
            {"preferred_server": "jellyfin", "preferred_download_target": "both"},
            owner="owner-a",
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        self.assertEqual(confirmed["result"]["status"], "completed")
        with call_owner("owner-a"):
            owner_a = preferences.get()
        with call_owner("owner-b"):
            owner_b = preferences.get()
        self.assertEqual(owner_a["preferred_server"], "jellyfin")
        self.assertTrue(owner_a["explicit"])
        self.assertEqual(owner_b["preferred_server"], "any")
        self.assertFalse(owner_b["explicit"])
        clear = service.prepare("preferences.clear", {}, owner="owner-a")
        service.confirm(clear["action_plan"]["plan_id"], owner="owner-a")
        with call_owner("owner-a"):
            cleared = preferences.get()
        self.assertFalse(cleared["explicit"])
        self.assertEqual(
            cleared["preferred_download_target"], "guangya"
        )

    def test_preference_confirmation_detects_stale_revision(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "preferences.update", {"preferred_server": "jellyfin"}, owner="owner-a"
        )
        from app.database import media_owner_digest
        from app.repositories.media_experience import set_media_preferences

        changed = set_media_preferences(
            media_owner_digest("owner-a"),
            expected_revision=0,
            updates={"preferred_server": "emby"},
        )
        assert (changed) is not None
        with self.assertRaises(AgentToolError) as captured:
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        self.assertEqual(captured.exception.code, "confirmation_stale")

    def test_notification_rule_is_revisioned_and_resettable(self) -> None:
        service = get_agent_service()
        defaults = sdk.subscriptions.notification(self.sid)
        self.assertFalse(defaults["enabled"])
        self.assertTrue(defaults["notify_on_missing"])
        self.assertFalse(defaults["explicit"])
        prepared = service.prepare(
            "subscriptions.set_notification",
            {
                "subscription_id": self.sid,
                "enabled": True,
                "notify_on_satisfied": True,
            },
            owner="owner-a",
        )
        service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        explicit = sdk.subscriptions.notification(self.sid)
        self.assertTrue(explicit["enabled"])
        self.assertTrue(explicit["notify_on_satisfied"])
        self.assertTrue(explicit["explicit"])
        reset = service.prepare(
            "subscriptions.reset_notification",
            {"subscription_id": self.sid},
            owner="owner-a",
        )
        service.confirm(reset["action_plan"]["plan_id"], owner="owner-a")
        restored = sdk.subscriptions.notification(self.sid)
        self.assertFalse(restored["enabled"])
        self.assertFalse(restored["explicit"])

    def test_today_summary_prioritizes_bounded_safe_content_events(self) -> None:
        stamp = db.now()
        get_media_subscription_result = media_subscriptions_repo.get_media_subscription(self.sid)
        assert get_media_subscription_result is not None
        revision = int(get_media_subscription_result["revision"])
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
        response = sdk.subscriptions.today()
        data = response
        self.assertEqual(data["event_count"], 3)
        self.assertEqual(data["subscription_runs"]["missing"], 1)
        self.assertEqual(data["downloads"]["success"], 2)
        self.assertIn("安全标题", data["content_titles"])
        self.assertNotIn("SECRET", repr(response))
        self.assertLessEqual(len(data["content_titles"]), 8)

    def test_resource_submit_remains_reachable_and_sdk_lists_preference_reader(self) -> None:


        catalog = get_agent_service().catalog
        self.assertTrue(catalog.has("resources.download"))
        self.assertIn("preferences.get", {item["name"] for item in describe_methods()})
        self.assertFalse(catalog.has("media.preferences"))


def test_native_library_reads_follow_owner_preference_and_keep_real_scope(monkeypatch):
    from app import sdk
    from app.modules import media_library

    observed = []
    monkeypatch.setattr(media_library, 'recent', lambda server, **kw: observed.append((server, kw)) or {})
    with isolated_test_database(), sdk.call_owner('owner'):
        monkeypatch.setattr(sdk.preferences, 'get', lambda: {'preferred_server': 'emby'})
        sdk.library.recent(library_id='drama', media_type='movie', limit=80)
        sdk.library.recent(server='jellyfin', library_name='动漫', media_type='episode')
    assert observed == [('emby', {'limit': 80, 'library_id': 'drama', 'library_name': '', 'media_type': 'movie'}),
                        ('jellyfin', {'limit': 20, 'library_id': '', 'library_name': '动漫', 'media_type': 'episode'})]


def test_native_library_recent_invalid_filters_do_not_query(monkeypatch):
    from app.modules import media_library

    monkeypatch.setattr(media_library, 'list_configured_profiles', lambda: (_ for _ in ()).throw(AssertionError('invalid request reached provider')))
    for arguments in ({'limit': True}, {'limit': 201}, {'limit': 0}, {'limit': '20'}, {'media_type': 'anime'}, {'library_id': 12}):
        with pytest.raises(media_library.MediaLibraryError) as caught:
            media_library.recent(**arguments)
        assert caught.value.code == 'invalid_arguments'
