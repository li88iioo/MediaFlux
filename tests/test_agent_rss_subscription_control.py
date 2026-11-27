"""RSS 订阅管理动作的严格解析、确认、竞态与脱敏回归。"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.kernel.adapters import ApprovalView, TurnView
from app.agent.rate_limit import agent_rate_limiter
from app.bot.agent_adapter import _render_approval_turn
from app.database import media_owner_digest
from app.modules.rss_subscription_config import (
    RSSSubscriptionConfigError,
    normalize_rss_subscription_create,
    normalize_rss_subscription_update,
)
from app.modules.rss_subscriptions import RSSSubscriptionError, prepare_change
from app.repositories import rss as rss_repo
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


class RSSSubscriptionControlTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        self.sid = rss_repo.add_rss_subscription(
            "Private Feed",
            "https://secret.invalid/rss?passkey=RSS_SECRET",
            exclude_keywords="PRIVATE_FILTER",
            enabled=1,
            refresh_interval_minutes=30,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def _confirm(self, tool: str, arguments: dict):
        service = get_agent_service()
        prepared = service.prepare(tool, arguments, owner="owner")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        return (prepared, confirmed)

    def test_enable_and_interval_confirm_reload_runtime_without_private_url(self):
        scheduler = Mock()
        with (
            patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=scheduler),
            patch.object(rss_repo, "list_rss_entries", side_effect=AssertionError("非过滤设置不读取条目")),
        ):
            prepared, confirmed = self._confirm(
                "rss.update",
                {"subscription_id": self.sid, "enabled": False},
            )
            prepared2, confirmed2 = self._confirm(
                "rss.update",
                {"subscription_id": self.sid, "refresh_interval_minutes": 0},
            )
        self.assertEqual(scheduler.reload.call_count, 2)
        row = rss_repo.get_rss_subscription(self.sid)
        assert row is not None
        self.assertEqual(int(row["enabled"]), 0)
        self.assertEqual(int(row["refresh_interval_minutes"]), 0)
        self.assertTrue(confirmed["result"]["data"]["runtime_refreshed"])
        self.assertTrue(confirmed2["result"]["data"]["runtime_refreshed"])
        serialized = json.dumps(
            {
                "prepared": prepared,
                "confirmed": confirmed,
                "prepared2": prepared2,
                "confirmed2": confirmed2,
            },
            ensure_ascii=False,
        )
        for secret in ("https://", "/rss", "passkey", "RSS_SECRET", "PRIVATE_FILTER"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(prepared["action_plan"]["preview"]["data"]["resources"], [
            {"title": f"#{self.sid} · Private Feed", "site_name": "secret.invalid"},
        ])
        self.assertIn("启用状态 · 启用 → 停用", prepared["action_plan"]["preview"]["data"]["effects"])
        self.assertIn("刷新间隔 · 30 分钟 → 不定时刷新", prepared2["action_plan"]["preview"]["data"]["effects"])

    def test_update_approval_distinguishes_same_name_sources_and_renders_exact_change_in_telegram(self):
        first = rss_repo.add_rss_subscription(
            "晚更 & 动画", "https://fixture-user:fixture-password@bay.invalid/path-secret?opaque=QUERY_SECRET",
            enabled=0, exclude_keywords="试看",
        )
        second = rss_repo.add_rss_subscription(
            "晚更 & 动画", "https://harbor.invalid/private-feed?unknown=SECOND_SECRET",
            enabled=0, exclude_keywords="繁体,试播",
        )
        excerpts = (
            "旧条目 A", "旧条目 B", "青屿夜班 繁体 1080p", "青屿夜班 简体 720P",
            "青屿夜班 试播 1080p", "青屿夜班 简体 1080p",
        )
        for index, title in enumerate(excerpts):
            rss_repo.add_rss_entry_with_media(second, title, f"harbor-{index}")
        rss_repo.add_rss_entry_with_media(first, "同名另一站的条目 720P", "bay-newer")
        service = get_agent_service()
        for sid, host, other_host, update, change in (
            (first, "bay.invalid", "harbor.invalid", {"enabled": True}, "启用状态 · 停用 → 启用"),
            (second, "harbor.invalid", "bay.invalid", {"exclude_keywords": "繁体,试播,720p"},
             "排除词 · 繁体,试播 → 繁体,试播,720p"),
        ):
            with self.subTest(sid=sid):
                prepared = service.prepare("rss.update", {"subscription_id": sid, **update}, owner="owner")
                plan = prepared["action_plan"]
                approval = ApprovalView(plan["plan_id"], plan["tool_name"], plan["effect"], plan["preview"],
                                        {}, plan["expires_at"])
                body = _render_approval_turn(TurnView("rss", "turn", "request", "approval_required", approval=approval))
                self.assertIn(f"#{sid} · 晚更 &amp; 动画 · {host}", body)
                self.assertIn(change, body)
                if sid == second:
                    self.assertIn("本地标题试算 · 最近 4 条，共 6 条（抽样）", body)
                    self.assertIn("试算保留 · 青屿夜班 简体 1080p", body)
                    for title in excerpts[2:5]:
                        self.assertIn(f"试算排除 · {title}", body)
                    for hidden_title in (*excerpts[:2], "同名另一站的条目"):
                        self.assertNotIn(hidden_title, body)
                else:
                    self.assertNotIn("本地标题试算", body)
                serialized = json.dumps(prepared, ensure_ascii=False) + body
                for hidden in (other_host, "fixture-user", "fixture-password", "path-secret", "opaque=",
                               "QUERY_SECRET", "private-feed", "unknown=", "SECOND_SECRET"):
                    self.assertNotIn(hidden, serialized)
                before = rss_repo.get_rss_subscription(sid)
                assert before is not None
                self.assertEqual(before["enabled"], 0)
                self.assertNotIn("720p", before["exclude_keywords"])
        # 样本只用于核对计划，新到条目不会使已经冻结的配置修改失效。
        rss_repo.add_rss_entry_with_media(second, "卡片生成之后到的 720P", "harbor-later")
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            confirmed = service.confirm(plan["plan_id"], owner="owner")
        self.assertTrue(confirmed["result"]["data"]["verified"])
        first_row = rss_repo.get_rss_subscription(first)
        second_row = rss_repo.get_rss_subscription(second)
        assert first_row is not None and second_row is not None
        self.assertEqual((first_row["enabled"], first_row["exclude_keywords"]), (0, "试看"))
        self.assertEqual((second_row["enabled"], second_row["exclude_keywords"]), (0, "繁体,试播,720p"))
        self.assertEqual(rss_repo.list_rss_entries(second, status="pending").total, 7)

    def test_filter_preview_labels_complete_and_empty_local_samples(self):
        fields = {"subscription_id": self.sid, "exclude_keywords": "试播，繁体 720p"}
        empty = prepare_change("update", fields)["preview"]
        self.assertIn("本地标题试算 · 暂无本地条目", empty["summary"])
        rss_repo.add_rss_entry_with_media(self.sid, "海灯 720P", "sample-720p", skip_reason="历史标记")
        rss_repo.add_rss_entry_with_media(self.sid, "海灯 繁体 1080p", "sample-traditional")
        rss_repo.add_rss_entry_with_media(self.sid, "海灯 简体 1080p", "sample-keep")
        rss_repo.add_rss_entry_with_media(self.sid, "海灯 https://fixture.invalid/path?token=SAMPLE_SECRET", "sample-private")
        filled = prepare_change("update", fields)["preview"]
        self.assertIn("本地标题试算 · 本地全部 4 条", filled["summary"])
        sample = [item["title"] for item in filled["data"]["resources"][1:]]
        self.assertIn("试算保留 · 海灯 简体 1080p", sample)
        self.assertIn("试算排除 · 海灯 繁体 1080p", sample)
        self.assertIn("试算排除 · 海灯 720P", sample)
        self.assertNotIn("SAMPLE_SECRET", json.dumps(filled, ensure_ascii=False))

    def test_stale_subscription_update_conflicts(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        rss_repo.update_rss_subscription(self.sid, {"exclude_keywords": "CHANGED_SECRET"})
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        get_rss_subscription_result = rss_repo.get_rss_subscription(self.sid)
        assert get_rss_subscription_result is not None
        self.assertEqual(int(get_rss_subscription_result["enabled"]), 1)

    def test_delete_is_cascaded_locally_and_stale_entry_count_conflicts(self):
        rss_repo.add_rss_entry_with_media(self.sid, "PRIVATE_TITLE", "secret-guid")
        other = rss_repo.add_rss_subscription("Other", "https://other.invalid/rss")
        rss_repo.add_rss_entry_with_media(other, "OTHER_TITLE", "other-guid")
        service = get_agent_service()
        prepared = service.prepare(
            "rss.delete", {"subscription_id": self.sid}, owner="owner"
        )
        rss_repo.add_rss_entry_with_media(self.sid, "LATE_PRIVATE_TITLE", "late-guid")
        conflict = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(conflict["result"]["status"], "conflict")
        assert (rss_repo.get_rss_subscription(self.sid)) is not None
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            _, confirmed = self._confirm(
                "rss.delete", {"subscription_id": self.sid}
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["deleted_entries"], 2)
        self.assertIsNone(rss_repo.get_rss_subscription(self.sid))
        assert (rss_repo.get_rss_subscription(other)) is not None
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM rss_entries WHERE rss_item_id=?", (other,)
                ).fetchone()[0],
                1,
            )

    def test_reload_failure_keeps_committed_change_and_history_is_safe(self):
        with patch(
            "app.modules.rss_scheduler.get_rss_scheduler",
            side_effect=RuntimeError("secret scheduler failure"),
        ):
            _, confirmed = self._confirm(
                "rss.update",
                {"subscription_id": self.sid, "enabled": False},
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(confirmed["result"]["data"]["runtime_refreshed"])
        get_rss_subscription_result_2 = rss_repo.get_rss_subscription(self.sid)
        assert get_rss_subscription_result_2 is not None
        self.assertEqual(int(get_rss_subscription_result_2["enabled"]), 0)
        history = db.list_agent_action_history(
            owner_digest=media_owner_digest("owner"), limit=1
        )[0]
        details = json.loads(history["safe_details"])
        self.assertEqual(details["operation"], "update")
        self.assertEqual(details["affected"], 1)
        self.assertNotIn("subscription_id", details)
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("secret scheduler failure", serialized)

    def test_create_and_update_use_shared_safe_config_chain(self):
        create_arguments = {
            "name": "Anime Feed",
            "urls": ["https://feed.example/rss"],
            "exclude_keywords": "CAM",
            "action": "download",
            "enabled": True,
            "refresh_interval_minutes": 45,
            "download_method": "qb",
            "media_tmdb_id": "12345",
            "media_default_season": 2,
            "skip_existing_episodes": True,
        }
        normalized = prepare_change("create", create_arguments)["snapshot"]["fields"]
        self.assertEqual(normalized["urls"], "https://feed.example/rss")
        self.assertEqual(normalized["download_method"], "qb")
        with self.assertRaises((RSSSubscriptionError, RSSSubscriptionConfigError)):
            prepare_change("create", {**create_arguments, "qb_save_path": 123})
        service = get_agent_service()
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.create", create_arguments, owner="owner"
            )
            serialized = json.dumps(prepared, ensure_ascii=False)
            self.assertNotIn("feed.example", serialized)
            created = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertTrue(created["result"]["ok"])
        self.assertTrue(created["result"]["data"]["verified"])
        self.assertIn("核验", created["result"]["summary"])
        created_id = int(created["result"]["data"]["subscription_id"])
        self.assertEqual(created["result"]["data"]["subscription_id"], created_id)
        row = rss_repo.get_rss_subscription(created_id)
        assert row is not None
        self.assertEqual(row["name"], "Anime Feed")
        self.assertEqual(row["media_tmdb_id"], "12345")
        update_arguments = {
                "subscription_id": created_id,
                "name": "Anime Feed Updated",
                "refresh_interval_minutes": 60,
            }
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.update", update_arguments, owner="owner"
            )
            updated = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertTrue(updated["result"]["ok"])
        row = rss_repo.get_rss_subscription(created_id)
        assert row is not None
        self.assertEqual(row["name"], "Anime Feed Updated")
        self.assertEqual(int(row["refresh_interval_minutes"]), 60)

    def test_create_reports_outcome_unknown_when_readback_cannot_verify(self):
        service = get_agent_service()
        arguments = {
            "name": "Readback Feed",
            "urls": ["https://feed.example/readback.xml"],
            "action": "subscribe",
            "enabled": False,
            "refresh_interval_minutes": 360,
            "download_method": "qb",
        }
        prepared = service.prepare("rss.create", arguments, owner="owner")
        with patch(
            "app.repositories.rss.get_rss_subscription",
            return_value=None,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "outcome_unknown")
        self.assertFalse(confirmed["result"]["data"]["verified"])
        self.assertIn("避免重复", confirmed["result"]["error"])

    def test_update_urls_validator_is_idempotent_and_confirmation_succeeds(self):
        urls = ["https://updated.example/rss?scope=URL_SCOPE_SECRET", "https://side.example/hidden-feed"]
        arguments = {
            "subscription_id": self.sid,
            "urls": urls,
        }
        normalized = prepare_change("update", arguments)["snapshot"]["fields"]
        self.assertEqual(
            normalized,
            {"urls": "\n".join(urls)},
        )
        self.assertEqual(prepare_change("update", {"subscription_id": self.sid, **normalized})["snapshot"]["fields"], normalized)
        service = get_agent_service()
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.update", arguments, owner="owner"
            )
            serialized = json.dumps(prepared, ensure_ascii=False)
            for hidden in ("https://", "/rss", "scope=", "URL_SCOPE_SECRET", "hidden-feed", "RSS_SECRET"):
                self.assertNotIn(hidden, serialized)
            self.assertIn("订阅地址 · secret.invalid → updated.example、side.example",
                          prepared["action_plan"]["preview"]["data"]["effects"])
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        get_rss_subscription_result_3 = rss_repo.get_rss_subscription(self.sid)
        assert get_rss_subscription_result_3 is not None
        self.assertEqual(get_rss_subscription_result_3["urls"], "\n".join(urls))

    def test_shared_config_integer_fields_reject_lossy_values(self):
        base = {"name": "Strict Feed", "urls": "https://example.invalid/rss"}
        for field in ("refresh_interval_minutes", "media_default_season"):
            for invalid in (True, 1.0, 1.9, "1.0", "1e3", "not-an-integer"):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(RSSSubscriptionConfigError),
                ):
                    normalize_rss_subscription_create({**base, field: invalid})
        normalized = normalize_rss_subscription_create(
            {**base, "refresh_interval_minutes": "45", "media_default_season": "2", "media_tmdb_id": "123"}
        )
        self.assertEqual(normalized["refresh_interval_minutes"], 45)
        self.assertEqual(normalized["media_default_season"], 2)

    def test_shared_config_rejects_nonempty_cron_but_accepts_legacy_empty_value(self):
        base = {"name": "Cron Feed", "urls": "https://example.invalid/rss"}
        for normalizer in (
            normalize_rss_subscription_create,
            lambda payload: normalize_rss_subscription_update(
                payload, current={"media_tmdb_id": "", "media_default_season": 1,
                                  "skip_existing_episodes": 0}
            ),
        ):
            with self.assertRaisesRegex(
                RSSSubscriptionConfigError, "refresh_interval_minutes"
            ):
                normalizer({**base, "refresh_cron": "0 4 * * *"})

        normalized = normalize_rss_subscription_create(
            {**base, "refresh_cron": "", "refresh_interval_minutes": 45}
        )
        self.assertEqual(normalized["refresh_cron"], "")
        self.assertEqual(normalized["refresh_interval_minutes"], 45)

        with self.assertRaisesRegex(RSSSubscriptionError, "refresh_interval_minutes"):
            prepare_change("create",
                {**base, "refresh_cron": "0 4 * * *"}
            )
        with self.assertRaisesRegex(AgentToolError, "refresh_interval_minutes"):
            get_agent_service().prepare(
                "rss.update",
                {"subscription_id": self.sid, "refresh_cron": "0 4 * * *"},
                owner="owner",
            )

    def test_update_confirmation_uses_one_write_transaction_for_revision_and_bindings(
        self,
    ):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update",
            {
                "subscription_id": self.sid,
                "name": "Atomic Feed",
                "media_tmdb_id": "12345",
                "media_default_season": 2,
            },
            owner="owner",
        )
        original_update = rss_repo.update_rss_subscription
        observed: list[tuple[bool, bool]] = []

        def update_in_transaction(sub_id, fields, **kwargs):
            connection = kwargs.get("connection")
            assert connection is not None
            observed.append((connection is not None, bool(connection.in_transaction)))
            return original_update(sub_id, fields, **kwargs)

        with patch(
            "app.repositories.rss.update_rss_subscription",
            side_effect=update_in_transaction,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(observed, [(True, True)])
        current = rss_repo.get_rss_subscription(self.sid)
        assert current is not None
        self.assertEqual(current["name"], "Atomic Feed")
        self.assertEqual(current["media_tmdb_id"], "12345")
        self.assertEqual(current["media_default_season"], 2)

    def test_update_confirmation_conflicts_after_subscription_changes(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update",
            {"subscription_id": self.sid, "exclude_keywords": "SAFE"},
            owner="owner",
        )
        rss_repo.update_rss_subscription(self.sid, {"refresh_interval_minutes": 31})
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        get_rss_subscription_result_4 = rss_repo.get_rss_subscription(self.sid)
        assert get_rss_subscription_result_4 is not None
        self.assertEqual(
            get_rss_subscription_result_4["exclude_keywords"], "PRIVATE_FILTER"
        )
