"""媒体追更订阅的安全摘要、自然语言路由与确认写入回归。"""

from __future__ import annotations
from typing import Any

import json
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.database import media_owner_digest
from app import sdk
from app.modules.media_subscriptions import MediaSubscriptionError, get_media_subscription_service
from app.agent.errors import AgentToolError
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
            "subscriptions.update",
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
        get_media_subscription_result = db.get_media_subscription(self.sid)
        assert get_media_subscription_result is not None
        revision = int(get_media_subscription_result["revision"])
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
            assert candidate.lastrowid is not None
            candidate_id = candidate.lastrowid
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
        read = sdk.subscriptions.get(self.sid)
        self.assertEqual(read["download_target"], "guangya")
        self.assertEqual(read["action"], "confirm")
        self.assertIn("private-site", read["sites"])
        with patch(
            "app.modules.media_subscriptions._reload_subscription_scheduler", return_value=True
        ):
            prepared = service.prepare(
                "subscriptions.update",
                {
                    "subscription_id": self.sid,
                    "download_target": "both",
                    "action": "notify",
                    "check_interval_minutes": 120,
                },
                owner="owner",
            )
            get_media_subscription_result_2 = db.get_media_subscription(self.sid)
            assert get_media_subscription_result_2 is not None
            self.assertEqual(
                get_media_subscription_result_2["download_target"], "guangya"
            )
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        after = db.get_media_subscription(self.sid)
        assert after is not None
        self.assertEqual(after["download_target"], "both")
        self.assertEqual(after["action"], "notify")
        self.assertEqual(after["check_interval_minutes"], 120)
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner")

    def _assert_delete_rechecks_transaction(self, change: str) -> None:
        service = get_media_subscription_service()
        original = service.delete_subscription
        args = {"subscription_id": self.sid}
        prepared = service.prepare_change("delete", args)
        def concurrent_change(subscription_id, **kwargs):
            if change == "revision":
                db.update_media_subscription_config(subscription_id, action="notify")
            else:
                self._seed_inflight_state()
            return original(subscription_id, **kwargs)
        with patch.object(service, "delete_subscription", side_effect=concurrent_change):
            with self.assertRaises(MediaSubscriptionError) as error:
                service.apply_change("delete", args, expected_snapshot=prepared["snapshot"])
        self.assertEqual(error.exception.code, "conflict")
        row = db.get_media_subscription(self.sid)
        assert row is not None
        if change == "revision":
            self.assertEqual(row["action"], "notify")
        else:
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT status FROM media_subscription_candidates WHERE subscription_id=?", (self.sid,)).fetchone()[0], "available")
                self.assertEqual(conn.execute("SELECT status FROM media_download_admissions WHERE subscription_id=?", (self.sid,)).fetchone()[0], "claimed")

    def test_delete_rechecks_revision_inside_final_write_transaction(self) -> None:
        self._assert_delete_rechecks_transaction("revision")

    def test_delete_rechecks_related_work_inside_final_write_transaction(self) -> None:
        self._assert_delete_rechecks_transaction("related_work")

    def test_create_rechecks_identity_at_final_upsert_for_each_policy(self) -> None:
        service = get_agent_service()
        cases = [(style, restored) for style in ("ordinary", "automatic") for restored in (False, True)]
        for index, (style, restored) in enumerate(cases):
            with self.subTest(style=style, restored=restored):
                tool = "subscriptions.create"
                tmdb_id = str(99000 + index)
                data: dict[str, Any] = {"provider": "tmdb", "external_id": tmdb_id, "tmdb_id": tmdb_id,
                        "media_type": "movie", "title": "Concurrent fixture", "enabled": False,
                        "action": "notify", "check_interval_minutes": 10080}
                if restored:
                    sid = db.add_media_subscription(**data)
                    self.assertTrue(db.delete_media_subscription(sid))
                args: dict[str, Any] = {"provider": "tmdb", "external_id": tmdb_id, "media_type": "movie"}
                if style == "automatic":
                    args.update(enabled=False, action="auto", download_target="qb", check_interval_minutes=10080)
                card = MediaCard(provider="tmdb", external_id=tmdb_id,
                                 media_type="movie", title="Concurrent fixture")
                with patch("app.modules.media_subscriptions.get_discovery_service") as discovery:
                    discovery.return_value.get_detail.return_value = card
                    with patch("app.modules.media_subscriptions.get_discovery_service", return_value=discovery.return_value):
                        prepared = service.prepare(tool, args, owner="owner")
                def concurrent_create(*_args, data=data, **_kwargs):
                    db.upsert_media_subscription(**data)
                    return {"title": "Concurrent fixture", "release_date": "1999-01-01"}
                with patch("app.modules.media_subscriptions._tmdb_detail", side_effect=concurrent_create):
                    result = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
                self.assertFalse(result["result"]["ok"])
                self.assertEqual(result["result"]["status"], "conflict")
                row = self._subscription_by_identity(tmdb_id, "movie")
                self.assertEqual(row["action"], "notify")
                self.assertFalse(row["enabled"])
                self.assertEqual(row["check_interval_minutes"], 10080)

    def _assert_create_rechecks_source_mapping(self, *, revoke_confirmed: bool) -> None:
        service = get_agent_service()
        external_id = "99110" if revoke_confirmed else "99111"
        tmdb_id = "99120"
        changed_tmdb_id = tmdb_id if revoke_confirmed else "99121"
        args = {"provider": "douban", "external_id": external_id, "media_type": "movie"}
        db.upsert_media_external_id(
            "douban", external_id, "movie", tmdb_id, confirmed=True
        )
        before_count = db.count_media_subscriptions()
        card = MediaCard(
            provider="douban", external_id=external_id,
            media_type="movie", title="Concurrent mapping fixture",
        )
        with patch("app.modules.media_subscriptions.get_discovery_service") as discovery:
            discovery.return_value.get_detail.return_value = card
            prepared = service.prepare("subscriptions.create", args, owner="owner")

        def concurrent_mapping_change(requested_id, media_type):
            self.assertEqual((requested_id, media_type), (tmdb_id, "movie"))
            # 外层映射/订阅快照复核已通过；只提交另一写方的映射变更。
            # 撤销 confirmed 使用真实 SQL，避免 upsert 对已确认映射的保护吞掉变更。
            with db.get_conn() as conn:
                changed = conn.execute(
                    "UPDATE media_external_ids SET tmdb_id=?,confirmed=?,"
                    "version=version+1,updated_at=? "
                    "WHERE provider=? AND external_id=? AND media_type=?",
                    (changed_tmdb_id, int(not revoke_confirmed), db.now(),
                     "douban", external_id, "movie"),
                )
                self.assertEqual(changed.rowcount, 1)
            return {"id": int(tmdb_id), "title": card.title, "release_date": "1999-01-01"}

        scheduler = Mock()
        with (
            patch("app.modules.media_subscriptions._tmdb_detail",
                  side_effect=concurrent_mapping_change) as detail,
            patch("app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
                  return_value=scheduler),
        ):
            result = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")

        detail.assert_called_once_with(tmdb_id, "movie")
        mapping = db.get_media_external_id("douban", external_id, "movie")
        assert mapping is not None
        self.assertEqual(mapping["tmdb_id"], changed_tmdb_id)
        self.assertEqual(bool(mapping["confirmed"]), not revoke_confirmed)
        self.assertEqual(result["result"]["status"], "conflict")
        self.assertFalse(result["result"]["ok"])
        self.assertIsNone(self._subscription_by_identity(tmdb_id, "movie"))
        self.assertIsNone(self._subscription_by_identity(changed_tmdb_id, "movie"))
        self.assertEqual(db.count_media_subscriptions(), before_count)
        scheduler.reload.assert_not_called()

    def test_create_rechecks_mapping_target_inside_final_write_transaction(self) -> None:
        self._assert_create_rechecks_source_mapping(revoke_confirmed=False)

    def test_create_rechecks_mapping_confirmation_inside_final_write_transaction(self) -> None:
        self._assert_create_rechecks_source_mapping(revoke_confirmed=True)

    def test_subscription_policy_enforces_effective_tv_season_invariant(self) -> None:
        service = get_agent_service()
        with self.assertRaises(AgentToolError):
            service.prepare(
                "subscriptions.update",
                {"subscription_id": self.sid, "monitor_mode": "selected"},
                owner="owner",
            )
        db.update_media_subscription_config(
            self.sid, monitor_mode="selected", seasons_json="[1]"
        )
        with self.assertRaises(AgentToolError):
            service.prepare(
                "subscriptions.update",
                {"subscription_id": self.sid, "seasons": []},
                owner="owner",
            )
        unchanged = db.get_media_subscription(self.sid)
        assert unchanged is not None
        self.assertEqual(unchanged["monitor_mode"], "selected")
        self.assertEqual(json.loads(unchanged["seasons_json"]), [1])
        db.update_media_subscription_config(
            self.sid, monitor_mode="missing", seasons_json="[2]"
        )
        prepared = service.prepare(
            "subscriptions.update",
            {"subscription_id": self.sid, "monitor_mode": "selected"},
            owner="owner",
        )
        self.assertEqual(prepared["result"]["data"]["effective"]["seasons"], [2])
        with patch(
            "app.modules.media_subscriptions._reload_subscription_scheduler", return_value=True
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["status"], "completed")
        after = db.get_media_subscription(self.sid)
        assert after is not None
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
        get_media_subscription_result_3 = db.get_media_subscription(movie_id)
        assert get_media_subscription_result_3 is not None
        before = int(get_media_subscription_result_3["revision"])
        for delta in (
            {"monitor_mode": "selected"},
            {"seasons": [1]},
            {"include_specials": True},
        ):
            with self.subTest(delta=delta), self.assertRaises(AgentToolError):
                service.prepare(
                    "subscriptions.update",
                    {"subscription_id": movie_id, **delta},
                    owner="owner",
                )
        after = db.get_media_subscription(movie_id)
        assert after is not None
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
            "subscriptions.update",
            {"subscription_id": self.sid, "action": "notify"},
            owner="owner",
        )
        preview = prepared["result"]["data"]
        self.assertEqual(preview["in_flight_dispatches_at_preflight"], 1)
        self.assertTrue(any("不能" in item or "无法" in item for item in preview["effects"]))
        plan = prepared["action_plan"]
        self.assertIn("无需再次确认", plan["confirmation"]["impact"])
        self.assertIn("提交阶段", plan["confirmation"]["reversibility"])
        with patch(
            "app.modules.media_subscriptions._reload_subscription_scheduler", return_value=True
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(confirmed["result"]["data"]["in_flight_dispatches"], 1)
        get_media_subscription_result_4 = db.get_media_subscription(self.sid)
        assert get_media_subscription_result_4 is not None
        self.assertEqual(get_media_subscription_result_4["action"], "notify")

    def test_subscription_policy_stales_when_admission_enters_dispatching(self) -> None:
        self._seed_inflight_state()
        row = db.get_media_subscription(self.sid)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT id FROM media_download_admissions WHERE subscription_id=? AND status='claimed'",
                (self.sid,),
            ).fetchone()
        assert (admission) is not None
        service = get_agent_service()
        prepared = service.prepare(
            "subscriptions.update",
            {"subscription_id": self.sid, "action": "notify"},
            owner="owner",
        )
        self.assertEqual(
            prepared["result"]["data"]["in_flight_dispatches_at_preflight"], 0
        )
        self.assertEqual(
            prepared["result"]["data"]["pending_admissions_at_preflight"], 1
        )
        assert row is not None
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
        assert unchanged is not None
        self.assertEqual(unchanged["action"], "confirm")
        self.assertEqual(int(unchanged["revision"]), int(row["revision"]))

    def test_subscription_policy_stale_snapshot_fails_closed(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "subscriptions.update",
            {"subscription_id": self.sid, "download_target": "qb"},
            owner="owner",
        )
        db.update_media_subscription_config(self.sid, action="notify")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")
        get_media_subscription_result_5 = db.get_media_subscription(self.sid)
        assert get_media_subscription_result_5 is not None
        self.assertEqual(
            get_media_subscription_result_5["download_target"], "guangya"
        )

    def test_native_list_and_detail_preserve_full_persisted_business_fields(self) -> None:
        listed = sdk.subscriptions.list()
        detail = sdk.subscriptions.get(self.sid)
        self.assertEqual(listed["items"][0]["id"], self.sid)
        self.assertEqual(listed["items"][0], detail)
        self.assertEqual(detail["title"], "平凡职业造就世界最强")
        self.assertEqual(detail["tmdb_id"], "86034")
        self.assertIn("private-site", detail["sites"])
        self.assertIn("last_error", detail)
        self.assertIn("revision", detail)

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
        service = get_media_subscription_service()
        with patch.object(service, "preview_subscription_updates", preview):
            result = service.check_updates()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "updates_available")
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["updates_available_count"], 1)
        missing = next(
            item
            for item in result["items"]
            if item["status"] == "missing"
        )
        self.assertEqual(missing["delivery"]["target_label"], "光鸭")
        self.assertTrue(result["read_only"])
        self.assertNotIn(
            "PRIVATE_ORIGINAL_TITLE", json.dumps(result, ensure_ascii=False)
        )
        self.assertEqual(preview.await_count, 2)

    def test_subscription_updates_preserve_successes_when_one_item_times_out(self) -> None:
        other = db.add_media_subscription(provider="tmdb", external_id="999", tmdb_id="999", media_type="tv", title="超时订阅")
        good = {"subscription_number": self.sid, "title": "已核对订阅", "status": "missing", "missing_count": 1,
                "resource_search": {"status": "partial", "truncated": False, "items": []}}
        async def preview(identifier, **kwargs):
            if identifier == other:
                raise TimeoutError()
            return good
        service = get_media_subscription_service()
        with patch.object(service, "preview_subscription_updates", side_effect=preview):
            result = service.check_updates()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["updates_available_count"], 1)
        self.assertEqual(result["inconclusive_count"], 1)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(next(row for row in result["items"] if row["subscription_number"] == other)["status"], "timeout")

    def test_prepare_does_not_write_and_confirm_atomically_invalidates_inflight_rows(
        self,
    ) -> None:
        self._seed_inflight_state()
        before = db.get_media_subscription(self.sid)
        service = get_agent_service()
        prepared = service.prepare(
            "subscriptions.update",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        after_prepare = db.get_media_subscription(self.sid)
        assert after_prepare is not None
        self.assertEqual(int(after_prepare["enabled"]), 1)
        assert before is not None
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
        assert row is not None
        self.assertEqual(int(row["enabled"]), 0)
        self.assertEqual(row["status"], "paused")
        assert before is not None
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

    def test_shared_mutation_rolls_back_config_and_invalidations_together(self) -> None:
        import sqlite3

        from app.modules.media_subscriptions import MediaSubscriptionService

        self._seed_inflight_state()
        before_row = db.get_media_subscription(self.sid)
        assert before_row is not None
        before = dict(before_row)
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_run_cancel BEFORE UPDATE ON media_subscription_runs "
                         "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                MediaSubscriptionService().update_subscription(self.sid, {"enabled": False})
            after_row = db.get_media_subscription(self.sid)
            assert after_row is not None
            self.assertEqual(dict(after_row), before)
            with db.get_conn() as conn:
                for table, status in (("media_subscription_candidates", "available"),
                                      ("media_download_admissions", "claimed"),
                                      ("media_subscription_runs", "running")):
                    self.assertEqual(conn.execute(f"SELECT status FROM {table} WHERE subscription_id=?", (self.sid,)).fetchone()["status"], status)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER reject_run_cancel")

    def test_web_short_interval_invalidates_agent_plan_without_losing_saved_scope(self) -> None:
        from app.modules.media_subscriptions import MediaSubscriptionService

        agent = get_agent_service()
        plan = agent.prepare("subscriptions.update", {"subscription_id": self.sid, "action": "auto"}, owner="owner")
        saved = MediaSubscriptionService().update_subscription(self.sid, {"check_interval_minutes": 5, "seasons": [0, 1]})
        self.assertEqual(saved["check_interval_minutes"], 5)
        stale = agent.confirm(plan["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(stale["result"]["status"], "conflict")
        policy = sdk.subscriptions.get(self.sid)
        self.assertEqual(policy["seasons"], [0, 1])
        self.assertEqual(policy["check_interval_minutes"], 5)

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
            "subscriptions.update",
            {"subscription_id": self.sid, "enabled": True},
            owner="owner",
        )
        self.assertTrue(any("恢复启用保留在途工作" in effect for effect in prepared["result"]["data"]["effects"]))
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
            "subscriptions.update",
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
            owner_digest=media_owner_digest("owner"), limit=1
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
            "app.modules.media_subscriptions.get_discovery_service",
            return_value=discovery,
        ):
            prepared = service.prepare(
                "subscriptions.create", arguments, owner="owner"
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "subscriptions.create")
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
        assert (row) is not None
        self.assertEqual(json.loads(row["seasons_json"]), [2])
        self.assertEqual(row["monitor_mode"], "selected")
        self.assertEqual(row["check_interval_minutes"], 10080)
        scheduler.reload.assert_called_once_with()

    def test_delete_subscription_soft_deletes_and_cancels_pending_work(self) -> None:
        self._seed_inflight_state()
        service = get_agent_service()
        scheduler = Mock()
        prepared = service.prepare(
            "subscriptions.delete", {"subscription_id": self.sid}, owner="owner"
        )
        self.assertEqual(prepared["action_plan"]["confirmation"]["risk"], "danger")
        assert (db.get_media_subscription(self.sid)) is not None
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
        get_media_subscription_result_6 = db.get_media_subscription(self.sid, include_deleted=True)
        assert get_media_subscription_result_6 is not None
        self.assertTrue(
            get_media_subscription_result_6["deleted_at"]
        )
        scheduler.reload.assert_called_once_with()
