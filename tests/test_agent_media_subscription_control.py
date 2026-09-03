"""媒体追更订阅的安全摘要、自然语言路由与确认写入回归。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.media_subscription_actions import (
    get_media_subscription_summary,
    inspect_media_subscription_updates,
    list_media_subscription_summaries,
)
from app.agent.rate_limit import agent_rate_limiter
from app.discovery.models import MediaCard
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
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
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        return (prepared, confirmed)

    @staticmethod
    def _subscription_by_identity(tmdb_id: str, media_type: str):
        with db.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM media_subscriptions WHERE tmdb_id=? AND media_type=? AND deleted_at IS NULL",
                (tmdb_id, media_type),
            ).fetchone()

    def _seed_inflight_state(self) -> None:
        stamp = db.now()
        revision = int(db.get_media_subscription(self.sid)["revision"])
        with db.get_conn() as conn:
            candidate = conn.execute(
                "INSERT INTO media_subscription_candidates(subscription_id,media_key,season,episode,result_id,site_id,site_name,title,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                "INSERT INTO media_download_admissions(media_key,tmdb_id,media_type,season,episode,subscription_id,subscription_revision,candidate_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                "INSERT INTO media_subscription_runs(subscription_id,trigger_type,subscription_revision,status,started_at) VALUES(?,?,?,?,?)",
                (self.sid, "manual", revision, "running", stamp),
            )

    def test_subscription_policy_read_and_confirmed_update(self) -> None:
        service = get_agent_service()
        read = service.invoke(
            "media.get_subscription_policy",
            {"subscription_id": self.sid},
            owner="owner",
        )
        self.assertEqual(read["result"]["data"]["download_target"], "guangya")
        self.assertEqual(read["result"]["data"]["action"], "confirm")
        self.assertNotIn("private-site", repr(read))
        with patch(
            "app.agent.media_subscription_actions._reload_scheduler", return_value=True
        ):
            prepared = service.prepare(
                "media.set_subscription_policy",
                {
                    "subscription_id": self.sid,
                    "download_target": "both",
                    "action": "notify",
                    "check_interval_minutes": 120,
                },
                owner="owner",
            )
            self.assertEqual(
                db.get_media_subscription(self.sid)["download_target"], "guangya"
            )
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        after = db.get_media_subscription(self.sid)
        self.assertEqual(after["download_target"], "both")
        self.assertEqual(after["action"], "notify")
        self.assertEqual(after["check_interval_minutes"], 120)
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner")

    def test_subscription_policy_enforces_effective_tv_season_invariant(self) -> None:
        service = get_agent_service()
        with self.assertRaises(AgentToolError):
            service.prepare(
                "media.set_subscription_policy",
                {"subscription_id": self.sid, "monitor_mode": "selected"},
                owner="owner",
            )
        db.update_media_subscription_config(
            self.sid, monitor_mode="selected", seasons_json="[1]"
        )
        with self.assertRaises(AgentToolError):
            service.prepare(
                "media.set_subscription_policy",
                {"subscription_id": self.sid, "seasons": []},
                owner="owner",
            )
        unchanged = db.get_media_subscription(self.sid)
        self.assertEqual(unchanged["monitor_mode"], "selected")
        self.assertEqual(json.loads(unchanged["seasons_json"]), [1])
        db.update_media_subscription_config(
            self.sid, monitor_mode="missing", seasons_json="[2]"
        )
        prepared = service.prepare(
            "media.set_subscription_policy",
            {"subscription_id": self.sid, "monitor_mode": "selected"},
            owner="owner",
        )
        self.assertEqual(prepared["result"]["data"]["effective"]["seasons"], [2])
        with patch(
            "app.agent.media_subscription_actions._reload_scheduler", return_value=True
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        after = db.get_media_subscription(self.sid)
        self.assertEqual(after["monitor_mode"], "selected")
        self.assertEqual(json.loads(after["seasons_json"]), [2])

    def test_subscription_policy_rejects_movie_season_fields_without_writes(
        self,
    ) -> None:
        movie_id = db.add_media_subscription(
            provider="tmdb",
            external_id="99901",
            tmdb_id="99901",
            media_type="movie",
            title="示例电影",
            action="confirm",
            download_target="guangya",
            enabled=True,
        )
        service = get_agent_service()
        before = int(db.get_media_subscription(movie_id)["revision"])
        for delta in (
            {"monitor_mode": "selected"},
            {"seasons": [1]},
            {"include_specials": True},
        ):
            with self.subTest(delta=delta), self.assertRaises(AgentToolError):
                service.prepare(
                    "media.set_subscription_policy",
                    {"subscription_id": movie_id, **delta},
                    owner="owner",
                )
        after = db.get_media_subscription(movie_id)
        self.assertEqual(int(after["revision"]), before)
        self.assertEqual(json.loads(after["seasons_json"]), [])
        self.assertFalse(bool(after["include_specials"]))

    def test_subscription_policy_reports_irreversible_inflight_auto_dispatch(
        self,
    ) -> None:
        db.update_media_subscription_config(self.sid, action="auto")
        self._seed_inflight_state()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_download_admissions SET status='dispatching' WHERE subscription_id=?",
                (self.sid,),
            )
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_policy",
            {"subscription_id": self.sid, "action": "notify"},
            owner="owner",
        )
        preview = prepared["result"]["data"]
        self.assertEqual(preview["in_flight_dispatches_at_preflight"], 1)
        self.assertTrue(any("无法" in item for item in preview["effects"]))
        plan = prepared["action_plan"]
        self.assertIn("无需再次确认", plan["confirmation"]["impact"])
        self.assertIn("提交阶段", plan["confirmation"]["reversibility"])
        with patch(
            "app.agent.media_subscription_actions._reload_scheduler", return_value=True
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["data"]["in_flight_dispatches"], 1)
        self.assertEqual(db.get_media_subscription(self.sid)["action"], "notify")

    def test_subscription_policy_stales_when_admission_enters_dispatching(self) -> None:
        self._seed_inflight_state()
        row = db.get_media_subscription(self.sid)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT id FROM media_download_admissions WHERE subscription_id=? AND status='claimed'",
                (self.sid,),
            ).fetchone()
        self.assertIsNotNone(admission)
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_policy",
            {"subscription_id": self.sid, "action": "notify"},
            owner="owner",
        )
        self.assertEqual(
            prepared["result"]["data"]["in_flight_dispatches_at_preflight"], 0
        )
        self.assertEqual(
            prepared["result"]["data"]["pending_admissions_at_preflight"], 1
        )
        self.assertTrue(
            db.begin_media_download_dispatch(
                int(admission["id"]),
                subscription_id=self.sid,
                subscription_revision=int(row["revision"]),
            )
        )
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")
        unchanged = db.get_media_subscription(self.sid)
        self.assertEqual(unchanged["action"], "confirm")
        self.assertEqual(int(unchanged["revision"]), int(row["revision"]))

    def test_subscription_policy_stale_snapshot_fails_closed(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_subscription_policy",
            {"subscription_id": self.sid, "download_target": "qb"},
            owner="owner",
        )
        db.update_media_subscription_config(self.sid, action="notify")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")
        self.assertEqual(
            db.get_media_subscription(self.sid)["download_target"], "guangya"
        )

    def test_safe_list_and_detail_do_not_expose_private_configuration(self) -> None:
        listed = list_media_subscription_summaries({}).to_dict()
        detail = get_media_subscription_summary({"subscription_id": self.sid}).to_dict()
        self.assertEqual(listed["data"]["items"][0]["subscription_number"], self.sid)
        self.assertEqual(detail["data"]["title"], "平凡职业造就世界最强")
        serialized = json.dumps(
            {"listed": listed, "detail": detail}, ensure_ascii=False
        )
        for secret in (
            "PRIVATE_ORIGINAL_TITLE",
            "/private-poster.jpg",
            "private-site",
            "guangya",
            "86034",
        ):
            self.assertNotIn(secret, serialized)

    def test_realtime_update_query_checks_all_subscriptions_and_returns_safe_candidates(
        self,
    ) -> None:
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
                    "resource_search": {
                        "status": "not_run",
                        "truncated": False,
                        "items": [],
                    },
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
                    "items": [
                        {
                            "label": "S01E155",
                            "candidates": [
                                {
                                    "result_id": "abcdefghijklmnop",
                                    "title": "师兄啊师兄 S01E155 4K",
                                    "site_id": "mikan",
                                    "site_name": "Mikan",
                                    "download_state": "ready",
                                    "download_kinds": ["magnet"],
                                    "relevance_score": 95,
                                    "confidence": "high",
                                    "match": "exact_episode",
                                }
                            ],
                        }
                    ],
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
            item
            for item in result["data"]["subscriptions"]
            if item["status"] == "missing"
        )
        self.assertEqual(missing["delivery"]["target_label"], "光鸭")
        self.assertTrue(result["data"]["read_only"])
        self.assertNotIn(
            "PRIVATE_ORIGINAL_TITLE", json.dumps(result, ensure_ascii=False)
        )
        self.assertEqual(preview.await_count, 2)

    def test_subscription_updates_preserve_successes_when_one_item_times_out(
        self,
    ) -> None:
        rows = list(db.list_media_subscriptions(limit=8))
        self.assertGreaterEqual(len(rows), 1)
        good = {
            "subscription_number": int(rows[0]["id"]),
            "title": "已核对订阅",
            "media_type": "tv",
            "enabled": True,
            "status": "missing",
            "summary": "发现 1 集缺失",
            "missing_count": 1,
            "resource_search": {"status": "partial", "truncated": False, "items": []},
            "delivery": {"state": "partial_unavailable", "partial": True},
        }
        unavailable = {
            **good,
            "subscription_number": int(rows[0]["id"]) + 1000,
            "title": "超时订阅",
            "status": "unavailable",
            "resource_search": {"status": "not_run", "truncated": False, "items": []},
        }
        with (
            patch(
                "app.agent.media_subscription_actions.db.count_media_subscriptions",
                return_value=2,
            ),
            patch(
                "app.agent.media_subscription_actions.db.list_media_subscriptions",
                return_value=[rows[0], rows[0]],
            ),
            patch(
                "app.agent.media_subscription_actions._preview_media_subscription_rows",
                new=AsyncMock(return_value=[good, unavailable]),
            ),
        ):
            result = inspect_media_subscription_updates({}).to_dict()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["updates_available_count"], 1)
        self.assertEqual(result["data"]["inconclusive_count"], 1)
        self.assertEqual(len(result["data"]["subscriptions"]), 2)

    def test_prepare_does_not_write_and_confirm_atomically_invalidates_inflight_rows(
        self,
    ) -> None:
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
                prepared["action_plan"]["plan_id"], owner="owner"
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
                prepared["action_plan"]["plan_id"], owner="owner"
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
        stale = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(stale["result"]["ok"])
        self.assertEqual(stale["result"]["status"], "conflict")
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner")

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

    def test_create_subscription_requires_confirmation_and_writes_only_after_confirm(
        self,
    ) -> None:
        service = get_agent_service()
        discovery = Mock()
        discovery.get_detail.return_value = MediaCard(
            provider="tmdb",
            external_id="999",
            media_type="tv",
            title="庆余年",
            year="2019",
        )
        scheduler = Mock()
        detail = {
            "id": 999,
            "name": "庆余年",
            "original_name": "Qing Yu Nian",
            "first_air_date": "2019-11-26",
            "poster_path": "/poster.jpg",
        }
        arguments = {
            "provider": "tmdb",
            "external_id": "999",
            "media_type": "tv",
            "season": 2,
            "check_interval_minutes": 10080,
        }
        with patch(
            "app.agent.media_subscription_actions.get_discovery_service",
            return_value=discovery,
        ):
            prepared = service.prepare(
                "media.create_subscription", arguments, owner="owner"
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "media.create_subscription")
        self.assertIsNone(self._subscription_by_identity("999", "tv"))
        with (
            patch(
                "app.modules.media_subscriptions.TMDBClient.detail", return_value=detail
            ),
            patch(
                "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
                return_value=scheduler,
            ),
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["season"], 2)
        self.assertEqual(confirmed["result"]["data"]["check_interval_minutes"], 10080)
        row = self._subscription_by_identity("999", "tv")
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["seasons_json"]), [2])
        self.assertEqual(row["monitor_mode"], "selected")
        self.assertEqual(row["check_interval_minutes"], 10080)
        scheduler.reload.assert_called_once_with()

    def test_delete_subscription_soft_deletes_and_cancels_pending_work(self) -> None:
        self._seed_inflight_state()
        service = get_agent_service()
        scheduler = Mock()
        prepared = service.prepare(
            "media.delete_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        self.assertEqual(prepared["action_plan"]["confirmation"]["risk"], "danger")
        self.assertIsNotNone(db.get_media_subscription(self.sid))
        with patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
            return_value=scheduler,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["expired_candidates"], 1)
        self.assertEqual(confirmed["result"]["data"]["cancelled_admissions"], 1)
        self.assertEqual(confirmed["result"]["data"]["cancelled_runs"], 1)
        self.assertIsNone(db.get_media_subscription(self.sid))
        self.assertTrue(
            db.get_media_subscription(self.sid, include_deleted=True)["deleted_at"]
        )
        scheduler.reload.assert_called_once_with()
