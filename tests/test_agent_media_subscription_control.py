"""媒体追更订阅的安全摘要、自然语言路由与确认写入回归。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.media_subscription_actions import (
    get_media_subscription_summary,
    inspect_media_subscription_updates,
    list_media_subscription_summaries,
    media_subscription_enabled_arguments,
    media_subscription_summaries_arguments,
    media_subscription_summary_arguments,
    media_subscription_updates_arguments,
)
from app.agent.orchestrator import (
    is_media_subscription_control_write_message,
    is_media_subscription_summaries_message,
    is_media_subscription_updates_message,
    media_subscription_control_request,
    media_subscription_summary_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from tests.support import IsolatedDatabaseTestCase


class MediaSubscriptionAgentControlTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        self.sid = db.add_media_subscription(
            provider="tmdb",
            external_id="86034",
            tmdb_id="86034",
            media_type="tv",
            title="平凡职业造就世界最强",
            original_title="PRIVATE_ORIGINAL_TITLE",
            year="2019",
            poster_key="/private-poster.jpg",
            action="confirm",
            download_target="guangya",
            sites=("mikan", "private-site"),
            enabled=True,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def _confirm(self, enabled: bool):
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_enabled",
            {"subscription_id": self.sid, "enabled": enabled},
            owner="owner",
        )
        confirmed = service.confirm(
            prepared["confirmation"]["confirmation_id"], owner="owner"
        )
        return prepared, confirmed

    def _seed_inflight_state(self) -> None:
        stamp = db.now()
        revision = int(db.get_media_subscription(self.sid)["revision"])
        with db.get_conn() as conn:
            candidate = conn.execute(
                "INSERT INTO media_subscription_candidates("
                "subscription_id,media_key,season,episode,result_id,site_id,site_name,title,"
                "status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.sid,
                    "tv:86034:s1:e1",
                    1,
                    1,
                    "private-result-id",
                    "private-site",
                    "PRIVATE SITE",
                    "PRIVATE RESOURCE TITLE",
                    "available",
                    "2099-01-01 00:00:00",
                    stamp,
                    stamp,
                ),
            )
            candidate_id = int(candidate.lastrowid)
            conn.execute(
                "INSERT INTO media_download_admissions("
                "media_key,tmdb_id,media_type,season,episode,subscription_id,"
                "subscription_revision,candidate_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "tv:86034:s1:e1",
                    "86034",
                    "tv",
                    1,
                    1,
                    self.sid,
                    revision,
                    candidate_id,
                    "claimed",
                    stamp,
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO media_subscription_runs("
                "subscription_id,trigger_type,subscription_revision,status,started_at) "
                "VALUES(?,?,?,?,?)",
                (self.sid, "manual", revision, "running", stamp),
            )

    def test_validators_registry_and_natural_language_are_strict(self) -> None:
        self.assertEqual(media_subscription_summaries_arguments({}), {})
        self.assertEqual(media_subscription_updates_arguments({}), {})
        with self.assertRaises(AgentToolError):
            media_subscription_updates_arguments({"refresh": True})
        self.assertEqual(
            media_subscription_summary_arguments({"subscription_id": self.sid}),
            {"subscription_id": self.sid},
        )
        self.assertEqual(
            media_subscription_enabled_arguments(
                {"subscription_id": self.sid, "enabled": False}
            ),
            {"subscription_id": self.sid, "enabled": False},
        )
        for invalid in (
            {"subscription_id": True},
            {"subscription_id": 0},
            {"subscription_id": str(self.sid)},
            {"subscription_id": self.sid, "extra": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                media_subscription_summary_arguments(invalid)
        for invalid in (
            {},
            {"subscription_id": self.sid, "enabled": 1},
            {"subscription_id": True, "enabled": False},
            {"subscription_id": self.sid, "enabled": False, "all": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                media_subscription_enabled_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        self.assertEqual(tools["media.subscription_summaries"]["risk"], "read")
        self.assertEqual(tools["media.subscription_updates"]["risk"], "read")
        self.assertEqual(tools["media.get_subscription_summary"]["risk"], "read")
        self.assertEqual(tools["media.set_subscription_enabled"]["risk"], "low_write")
        self.assertTrue(tools["media.set_subscription_enabled"]["requires_confirmation"])

        self.assertTrue(is_media_subscription_summaries_message("列出全部媒体追更订阅"))
        for message in (
            "我订阅的媒体又更新吗",
            "订阅有更新吗",
            "我订阅的媒体有更新吗",
            "我的订阅更新了吗",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_media_subscription_updates_message(message))
        self.assertFalse(is_media_subscription_updates_message("RSS 订阅有更新吗"))
        self.assertFalse(is_media_subscription_updates_message("修改媒体订阅更新间隔"))
        self.assertEqual(
            media_subscription_summary_request(f"查看媒体订阅 {self.sid} 状态"),
            {"subscription_id": self.sid},
        )
        self.assertEqual(
            media_subscription_control_request(f"暂停媒体订阅 {self.sid}"),
            ("media.set_subscription_enabled", {"subscription_id": self.sid, "enabled": False}),
        )
        self.assertEqual(
            media_subscription_control_request(f"恢复追更订阅 {self.sid}"),
            ("media.set_subscription_enabled", {"subscription_id": self.sid, "enabled": True}),
        )
        self.assertIsNone(media_subscription_control_request("暂停所有媒体订阅"))
        self.assertTrue(is_media_subscription_control_write_message("暂停所有媒体订阅"))

    def test_safe_list_and_detail_do_not_expose_private_configuration(self) -> None:
        listed = list_media_subscription_summaries({}).to_dict()
        detail = get_media_subscription_summary({"subscription_id": self.sid}).to_dict()
        self.assertEqual(listed["data"]["items"][0]["subscription_number"], self.sid)
        self.assertEqual(detail["data"]["title"], "平凡职业造就世界最强")
        serialized = json.dumps({"listed": listed, "detail": detail}, ensure_ascii=False)
        for secret in (
            "PRIVATE_ORIGINAL_TITLE",
            "/private-poster.jpg",
            "private-site",
            "guangya",
            "86034",
        ):
            self.assertNotIn(secret, serialized)

    def test_realtime_update_query_checks_all_subscriptions_and_returns_safe_candidates(self) -> None:
        second = db.add_media_subscription(
            provider="tmdb",
            external_id="218642",
            tmdb_id="218642",
            media_type="tv",
            title="师兄啊师兄",
            action="auto",
            download_target="guangya",
            enabled=True,
        )
        async def _preview(subscription_id: int, **_kwargs):
            if subscription_id == self.sid:
                return {
                "subscription_number": self.sid,
                "title": "平凡职业造就世界最强",
                "media_type": "tv",
                "enabled": True,
                "status": "satisfied",
                "summary": "已播剧集均已收录或正在下载",
                "missing_count": 0,
                "resource_search": {"status": "not_run", "truncated": False, "items": []},
                "delivery": {"state": "no_action", "target_label": "光鸭"},
                }
            return {
                "subscription_number": second,
                "title": "师兄啊师兄",
                "media_type": "tv",
                "enabled": True,
                "status": "missing",
                "summary": "发现 1 集已播但尚未收录",
                "missing_count": 1,
                "resource_search": {
                    "status": "success",
                    "truncated": False,
                    "items": [{
                        "label": "S01E155",
                        "candidates": [{
                            "result_id": "abcdefghijklmnop",
                            "title": "师兄啊师兄 S01E155 4K",
                            "site_id": "mikan",
                            "site_name": "Mikan",
                            "download_state": "ready",
                            "download_kinds": ["magnet"],
                            "relevance_score": 95,
                            "confidence": "high",
                            "match": "exact_episode",
                        }],
                    }],
                },
                "delivery": {
                    "state": "auto_eligible",
                    "target": "guangya",
                    "target_label": "光鸭",
                },
            }

        preview = AsyncMock(side_effect=_preview)
        service = Mock(preview_subscription_updates=preview)
        with patch(
            "app.agent.media_subscription_actions.get_media_subscription_service",
            return_value=service,
        ):
            result = inspect_media_subscription_updates({}).to_dict()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "updates_available")
        self.assertEqual(result["data"]["returned"], 2)
        self.assertEqual(result["data"]["updates_available_count"], 1)
        self.assertEqual(result["data"]["candidate_count"], 1)
        self.assertEqual(result["data"]["items"][0]["episode_label"], "S01E155")
        missing = next(
            item for item in result["data"]["subscriptions"] if item["status"] == "missing"
        )
        self.assertEqual(missing["delivery"]["target_label"], "光鸭")
        self.assertTrue(result["data"]["read_only"])
        self.assertNotIn("PRIVATE_ORIGINAL_TITLE", json.dumps(result, ensure_ascii=False))
        self.assertEqual(preview.await_count, 2)

    def test_orchestrator_routes_subscription_update_questions_before_patrol_tools(self) -> None:
        preview = AsyncMock(return_value={
            "subscription_number": self.sid,
            "title": "平凡职业造就世界最强",
            "media_type": "tv",
            "enabled": True,
            "status": "missing",
            "summary": "发现 1 集已播但尚未收录",
            "missing_count": 1,
            "resource_search": {
                "status": "success",
                "truncated": False,
                "items": [{
                    "label": "S01E03",
                    "candidates": [{
                        "result_id": "abcdefghijklmnop",
                        "title": "平凡职业造就世界最强 S01E03",
                        "site_id": "mikan",
                        "site_name": "Mikan",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    }],
                }],
            },
            "delivery": {"state": "confirmation_required", "target_label": "光鸭"},
        })
        mocked = Mock(preview_subscription_updates=preview)
        with patch(
            "app.agent.media_subscription_actions.get_media_subscription_service",
            return_value=mocked,
        ):
            result = get_agent_service().query(
                "我订阅的媒体又更新吗",
                owner="owner",
                present=False,
            )
        self.assertEqual(result["tool_call"]["name"], "media.subscription_updates")
        self.assertNotIn("patrol", result["tool_call"]["name"])
        snapshot = get_agent_service().recent_resource_store.get(owner="owner")
        self.assertEqual(snapshot["candidates"][0]["result_id"], "abcdefghijklmnop")
        preview.assert_awaited_once()

    def test_prepare_does_not_write_and_confirm_atomically_invalidates_inflight_rows(self) -> None:
        self._seed_inflight_state()
        before = db.get_media_subscription(self.sid)
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_enabled",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        after_prepare = db.get_media_subscription(self.sid)
        self.assertEqual(int(after_prepare["enabled"]), 1)
        self.assertEqual(int(after_prepare["revision"]), int(before["revision"]))
        self.assertEqual(prepared["mode"], "confirmation_required")

        scheduler = Mock()
        with patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
            return_value=scheduler,
        ):
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["expired_candidates"], 1)
        self.assertEqual(confirmed["result"]["data"]["cancelled_admissions"], 1)
        self.assertEqual(confirmed["result"]["data"]["cancelled_runs"], 1)
        scheduler.reload.assert_called_once_with()
        row = db.get_media_subscription(self.sid)
        self.assertEqual(int(row["enabled"]), 0)
        self.assertEqual(row["status"], "paused")
        self.assertEqual(int(row["revision"]), int(before["revision"]) + 1)
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_subscription_candidates WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "expired",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_download_admissions WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "cancelled",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_subscription_runs WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "cancelled",
            )

    def test_resume_preserves_inflight_rows_for_scheduler_recovery(self) -> None:
        self._seed_inflight_state()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET enabled=0,status='paused' WHERE id=?",
                (self.sid,),
            )

        scheduler = Mock()
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_enabled",
            {"subscription_id": self.sid, "enabled": True},
            owner="owner",
        )
        self.assertEqual(
            prepared["result"]["data"]["effects"],
            ["恢复后会重新进入检查队列，并在下一次调度时核对媒体库。"],
        )
        with patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
            return_value=scheduler,
        ):
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["expired_candidates"], 0)
        self.assertEqual(confirmed["result"]["data"]["cancelled_admissions"], 0)
        self.assertEqual(confirmed["result"]["data"]["cancelled_runs"], 0)
        scheduler.reload.assert_called_once_with()
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_subscription_candidates WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "available",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_download_admissions WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "claimed",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM media_subscription_runs WHERE subscription_id=?",
                    (self.sid,),
                ).fetchone()["status"],
                "running",
            )

    def test_stale_state_and_confirmation_ticket_are_one_time(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_enabled",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        db.update_media_subscription_config(self.sid, check_interval_minutes=120)
        stale = service.confirm(
            prepared["confirmation"]["confirmation_id"], owner="owner"
        )
        self.assertFalse(stale["result"]["ok"])
        self.assertEqual(stale["result"]["status"], "conflict")
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")

    def test_scheduler_reload_failure_does_not_leak_and_history_is_safe(self) -> None:
        with patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
            side_effect=RuntimeError("PRIVATE_SCHEDULER_SECRET"),
        ):
            _prepared, confirmed = self._confirm(False)
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(confirmed["result"]["data"]["runtime_refreshed"])
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("PRIVATE_SCHEDULER_SECRET", serialized)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        details = json.loads(history["safe_details"])
        self.assertEqual(details["operation"], "disable")
        self.assertEqual(details["subscription_number"], self.sid)
        self.assertNotIn("subscription_id", details)
        self.assertNotIn("title", details)

    def test_orchestrator_prepares_exact_write_and_refuses_bulk_write(self) -> None:
        service = get_agent_service()
        result = service.query(f"暂停媒体订阅 {self.sid}", owner="owner")
        self.assertEqual(result["mode"], "confirmation_required")
        self.assertEqual(result["confirmation"]["tool"], "media.set_subscription_enabled")
        bulk = service.query("暂停所有媒体订阅", owner="owner")
        self.assertEqual(bulk["mode"], "read_only")
        self.assertEqual(bulk["result"]["status"], "unsupported")
        self.assertIn("一个", bulk["result"]["summary"])


if __name__ == "__main__":
    import unittest

    unittest.main()
