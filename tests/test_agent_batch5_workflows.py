"""Media Agent 第 5 批长尾业务安全闭环回归。"""

from __future__ import annotations
from app.modules import rss_delivery

from typing import cast

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app import config, database as db, sdk
from app.repositories import rss as rss_repo
from app.repositories import discovery as discovery_repo
from app.repositories import agent_library_patrol as agent_library_patrol_repo
from app.discovery.service import DiscoveryService
from app.modules.web_secret import signed_context_fingerprint
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.sdk_effects import media_mapping_tool, library_patrol_tool
from tests.test_agent_kernel_existing_ports import context_for
from app.sdk import strm
from app.modules.rss_subscriptions import RSSSubscriptionError
from app.discovery.models import MediaCard
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from tests.support import IsolatedDatabaseTestCase, seed_rss_entry_state


class Batch5AgentWorkflowTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            for table in (
                "rss_entry_media",
                "rss_entries",
                "rss_items",
                "media_external_ids",
                "task_runs",
                "strm_change_queue",
                "strm_metadata_queue",
                "strm_failures",
                "agent_library_patrol",
                "organize_operation_jobs",
            ):
                conn.execute(f"DELETE FROM {table}")


    def _rss(self) -> tuple[int, int, int]:
        sid = rss_repo.add_rss_subscription(
            name="安全订阅",
            urls="https://secret.invalid/feed?token=PRIVATE",
            download_method="qb",
            qb_save_path="/private/rss",
        )
        first = rss_repo.add_rss_entry_with_media(
            sid,
            "公开标题 1",
            "SECRET-GUID-1",
            payload=json.dumps({"torrent_url": "magnet:?xt=urn:btih:PRIVATE1"}),
        )["id"]
        second = rss_repo.add_rss_entry_with_media(
            sid,
            "公开标题 2",
            "SECRET-GUID-2",
            payload=json.dumps({"torrent_url": "magnet:?xt=urn:btih:PRIVATE2"}),
        )["id"]
        assert first and second
        return (sid, int(first), int(second))


    def test_rss_safe_list_mark_and_exact_qb_submit(self) -> None:
        sid, first, second = self._rss()
        listed = rss_delivery.entries(subscription_id=sid, status="pending", limit=20
        )
        self.assertEqual(listed["data"]["total"], 2)
        rendered = json.dumps(listed, ensure_ascii=False)
        self.assertIn("公开标题", rendered)
        for secret in (
            "SECRET-GUID",
            "magnet",
            "PRIVATE",
            "/private/rss",
            "secret.invalid",
        ):
            self.assertNotIn(secret, rendered)
        mark_args = {"entry_ids": [first, second], "processed": True}
        preview, fingerprint = rss_delivery.prepare("mark", mark_args)
        self.assertTrue(preview["ok"])
        marked = rss_delivery.execute("mark", mark_args, fingerprint)
        self.assertEqual(marked["data"]["affected"], 2)
        get_rss_entry_result = rss_repo.get_rss_entry(first)
        assert get_rss_entry_result is not None
        self.assertEqual(str(get_rss_entry_result["status"]), "skipped")
        rss_repo.update_rss_entries_processed([first, second], False)
        runtime = {
            "url": "http://qb.internal",
            "username": "u",
            "password": "secret",
            "api_key": "key",
            "category": "rss",
            "default_save_path": "/private",
            "default_method": "qb",
            "timeout": 10,
        }
        submit_args = {"entry_ids": [first, second]}
        with patch(
            "app.modules.rss.capture_rss_qb_runtime_config", return_value=(runtime, "")
        ):
            submit_preview, submit_context = rss_delivery.prepare("download", submit_args)
            self.assertTrue(submit_preview["ok"])
            raw = {"requested": 2, "claimed": 2, "submitted": 2, "failed": 0}
            with patch(
                "app.modules.rss.RSSEngine.submit_snapshot", return_value=raw
            ) as submit:
                result = rss_delivery.execute("download", submit_args, submit_context)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["submitted"], 2)
        self.assertIs(
            submit.call_args.kwargs["claim"], rss_repo.claim_pending_rss_entries
        )
        submitted_rows = submit.call_args.args[0]
        self.assertEqual({row["id"] for row in submitted_rows}, {first, second})


    def test_exact_rss_submit_reports_pending_and_mixed_outcomes(self) -> None:
        _sid, first, second = self._rss()
        runtime = {
            "url": "http://qb.internal",
            "username": "u",
            "password": "secret",
            "api_key": "key",
            "category": "rss",
            "default_save_path": "/private",
            "default_method": "qb",
            "timeout": 10,
        }
        scenarios = (
            ([first], {"requested": 1, "claimed": 1, "submitted": 0, "failed": 0, "pending": 1}, "in_progress", True),
            ([first, second], {"requested": 2, "claimed": 2, "submitted": 1, "failed": 0, "pending": 1}, "partial", True),
            ([first], {"requested": 1, "claimed": 1, "submitted": 0, "failed": 1}, "failed", False),
        )
        with patch(
            "app.modules.rss.capture_rss_qb_runtime_config", return_value=(runtime, "")
        ):
            for numbers, raw, expected_status, expected_ok in scenarios:
                with self.subTest(raw=raw):
                    arguments = {"entry_ids": numbers}
                    preview, context = rss_delivery.prepare("download", arguments)
                    self.assertTrue(preview["ok"])
                    with patch(
                        "app.modules.rss.RSSEngine.submit_snapshot", return_value=raw
                    ):
                        result = rss_delivery.execute("download", arguments, context)
                    self.assertEqual(result["status"], expected_status)
                    self.assertEqual(result["ok"], expected_ok)
                    if raw.get("pending"):
                        self.assertEqual(result["data"]["pending"], raw["pending"])
                        self.assertIn("提交中 1", result["summary"])
                    else:
                        self.assertNotIn("pending", result["data"])
                    if raw["failed"]:
                        self.assertIn("未成功提交", result["summary"])


    def test_rss_mark_snapshot_fails_closed_after_state_change(self) -> None:
        _sid, first, _second = self._rss()
        args = {"entry_ids": [first], "processed": True}
        _preview, fingerprint = rss_delivery.prepare("mark", args)
        seed_rss_entry_state(first, "downloaded")
        with self.assertRaises(RSSSubscriptionError) as caught:
            rss_delivery.execute("mark", args, fingerprint)
        self.assertEqual(caught.exception.code, "confirmation_stale")


    def test_rss_mark_snapshot_rejects_atomic_race_without_overwriting_failure(
        self,
    ) -> None:
        _sid, first, _second = self._rss()
        args = {"entry_ids": [first], "processed": True}
        _preview, fingerprint = rss_delivery.prepare("mark", args)
        original_update = rss_repo.update_rss_entries_processed_snapshot

        def race_then_update(snapshot, processed):
            rss_repo.record_rss_entry_failure(first, "qb_unavailable", True)
            return original_update(snapshot, processed)

        with (
            patch(
                "app.repositories.rss.update_rss_entries_processed_snapshot",
                side_effect=race_then_update,
            ),
            self.assertRaises(RSSSubscriptionError) as caught,
        ):
            rss_delivery.execute("mark", args, fingerprint)
        self.assertEqual(caught.exception.code, "confirmation_stale")
        row = rss_repo.get_rss_entry(first)
        assert row is not None
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(str(row["failure_code"]), "qb_unavailable")
        self.assertTrue(bool(row["failure_retryable"]))


    def test_strm_history_exposes_only_safe_aggregates(self) -> None:
        run_id = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(
            run_id,
            "failed",
            result=json.dumps(
                {
                    "mode": "full",
                    "base_url": "https://secret.invalid/token",
                    "stats": {
                        "generated": 3,
                        "failed": 1,
                        "changed_dirs": ["/private/path"],
                    },
                    "sources": [{"id": "PRIVATE-ID", "name": "secret source"}],
                    "elapsed_seconds": 12.5,
                }
            ),
            error="PRIVATE traceback /private/path",
        )
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO strm_change_queue(provider,source_id,state,dirty,version,attempts,lease_owner,lease_generation,lease_until,next_attempt_at,pending_changes_json,inflight_changes_json,last_error,created_at,updated_at) VALUES('guangya','PRIVATE','queued',1,1,0,'',0,0,0,'[]','[]','PRIVATE',0,0)"
            )
            conn.execute(
                "INSERT INTO task_runs(task_name,trigger_type,status,started_at,finished_at,result,error) VALUES(?,?,?,?,?,?,?)",
                (
                    "strm_sync",
                    "https://evil.invalid/trigger",
                    "/private/status",
                    "/private/started",
                    "SECRET-FINISHED",
                    json.dumps(
                        {
                            "mode": "/private/mode",
                            "stats": {"generated": 7, "/private/stat": 99},
                            "elapsed_seconds": 999999999,
                        }
                    ),
                    "SECRET-ERROR",
                ),
            )
        result = strm.history(limit=8, status="all")
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["items"][0]["status"], "unknown")
        self.assertEqual(result["items"][0]["trigger_type"], "unknown")
        self.assertEqual(result["items"][0]["started_at"], "")
        self.assertEqual(result["items"][0]["finished_at"], "")
        self.assertEqual(result["items"][0]["mode"], "unknown")
        # SDK保留真实耗时；旧604800上限属于展示约束。
        self.assertEqual(result["items"][0]["elapsed_seconds"], 999999999.0)
        self.assertEqual(result["items"][1]["stats"]["generated"], 3)
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in (
            "secret.invalid",
            "evil.invalid",
            "PRIVATE-ID",
            "/private",
            "traceback",
            "secret source",
            "SECRET-ERROR",
            "SECRET-FINISHED",
        ):
            self.assertNotIn(secret, rendered)

    def _mapping_service(self) -> DiscoveryService:
        original_get_bool = config.get_bool
        enabled = patch.object(config, "get_bool", side_effect=lambda key, *args, **kwargs:
            True if key == "DISCOVERY_ENABLED" else original_get_bool(key, *args, **kwargs))
        enabled.start()
        self.addCleanup(enabled.stop)
        scraper = SimpleNamespace(
            search_candidates=lambda *_args: [SimpleNamespace(
                tmdb_id="278", title="The Shawshank Redemption", year="1994", score=0.98, media_type="movie",
            )],
            match_from_tmdb=lambda tmdb_id, media_type: SimpleNamespace(
                tmdb_id=tmdb_id, title=f"候选 {tmdb_id}", year="2026", media_type=media_type, need_confirm=False,
            ),
        )
        service = DiscoveryService(scraper_factory=lambda: scraper)
        self.addCleanup(service.shutdown)
        return service

    def test_discovery_detail_candidates_and_confirmed_mapping(self) -> None:
        service = self._mapping_service()
        card = MediaCard(provider="douban", external_id="1292052", media_type="movie", title="肖申克的救赎", year="1994")
        with patch.object(service, "get_detail", return_value=card), patch("app.discovery.service.get_discovery_service", return_value=service):
            detail = sdk.media.detail("douban", "movie", "1292052")
            candidates = sdk.media.resolve("douban", "1292052", "movie", card.title, card.year)
            self.assertIsNone(discovery_repo.get_media_external_id("douban", "1292052", "movie"))
            prepared = service.prepare_tmdb_mapping("douban", "1292052", "movie", "278")
            assert prepared is not None
            confirmed = service.confirm_tmdb_mapping_if_unchanged(
                "douban", "1292052", "movie", "278", prepared["mapping"],
                expected_source=prepared["source"], expected_target=prepared["target"],
            )
        self.assertEqual(detail["item"]["external_id"], "1292052")
        self.assertEqual(candidates["candidates"][0]["tmdb_id"], "278")
        assert confirmed is not None
        self.assertTrue(confirmed["confirmed"])
        stored = discovery_repo.get_media_external_id("douban", "1292052", "movie")
        assert stored is not None
        self.assertEqual((stored["tmdb_id"], stored["confirmed"]), ("278", 1))

    def test_discovery_mapping_cas_allows_only_one_concurrent_confirmation(self) -> None:
        service = self._mapping_service()
        barrier = threading.Barrier(3)

        def save(tmdb_id: str) -> bool:
            barrier.wait(timeout=2)
            return service.confirm_tmdb_mapping_if_unchanged("douban", "race-source", "movie", tmdb_id, None) is not None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, tmdb_id) for tmdb_id in ("101", "202")]
            barrier.wait(timeout=2)
            outcomes = [future.result(timeout=3) for future in futures]
        self.assertEqual(sum(outcomes), 1)
        stored = discovery_repo.get_media_external_id("douban", "race-source", "movie")
        assert stored is not None
        self.assertIn(str(stored["tmdb_id"]), {"101", "202"})
        self.assertTrue(bool(stored["confirmed"]))

    def test_discovery_mapping_confirmation_restores_persisted_snapshot(self) -> None:
        service = self._mapping_service()
        card = MediaCard(provider="douban", external_id="persisted-source", media_type="movie", title="持久化映射", year="2026")

        def effects(store):
            return EffectExecutor(catalog=ToolCatalog([media_mapping_tool()]), state_store=store, reference_store=store,
                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))

        async def exercise():
            store = SQLiteKernelStore()
            context = await context_for(store, owner="mapping-batch5", session="mapping-session")
            original = effects(store)
            prepared = await original.prepare("media.confirm_mapping", {
                "provider": "douban", "external_id": "persisted-source", "media_type": "movie", "tmdb_id": "909",
            }, context=context)
            assert prepared.effect_plan is not None
            for field in ("vote_count", "popularity"):
                self.assertNotIn(field, prepared.effect_plan.metadata["mapping_snapshot"]["source"])
            self.assertIsNone(discovery_repo.get_media_external_id("douban", "persisted-source", "movie"))
            cold = effects(SQLiteKernelStore())
            pending = await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
            assert pending is not None and pending.plan_id == prepared.effect_plan.plan_id
            completed = await cold.execute_confirmed(pending.plan_id, context=context)
            self.assertTrue(completed.outcome.public_content["ok"])
            row = discovery_repo.get_media_external_id("douban", "persisted-source", "movie")
            assert row is not None and row["tmdb_id"] == "909" and row["confirmed"]
            with self.assertRaises(ToolPipelineError):
                await cold.execute_confirmed(pending.plan_id, context=context)

        # 新增的推荐指标不得改变旧票据的 source 格式或使映射确认失效。
        current = MediaCard.from_dict({**card.to_dict(), "vote_count": 100, "popularity": 12.5})
        with patch.object(service, "get_detail", side_effect=[card, current]), patch("app.agent.kernel.ports.sdk_effects.get_discovery_service", return_value=service):
            asyncio.run(exercise())

    def test_discovery_mapping_cas_preserves_concurrently_confirmed_mapping(self) -> None:
        service = self._mapping_service()
        card = MediaCard(provider="douban", external_id="race-existing", media_type="movie", title="原始来源", year="2025")
        discovery_repo.upsert_media_external_id("douban", "race-existing", "movie", "100", "旧候选", "2025", 0.8, False)
        with patch.object(service, "get_detail", return_value=card):
            prepared = service.prepare_tmdb_mapping("douban", "race-existing", "movie", "300")
            assert prepared is not None
            with patch("app.database.now", return_value=prepared["mapping"]["updated_at"]):
                discovery_repo.upsert_media_external_id("douban", "race-existing", "movie", "200", "并发确认候选", "2026", 1.0, True)
                saved = service.confirm_tmdb_mapping_if_unchanged(
                    "douban", "race-existing", "movie", "300", prepared["mapping"],
                    expected_source=prepared["source"], expected_target=prepared["target"],
                )
        self.assertIsNone(saved)
        stored = discovery_repo.get_media_external_id("douban", "race-existing", "movie")
        assert stored is not None
        self.assertEqual((stored["tmdb_id"], stored["title"], stored["confirmed"]), ("200", "并发确认候选", 1))

    def test_discovery_mapping_reports_stale_when_atomic_save_loses_race(self) -> None:
        service = self._mapping_service()
        card = MediaCard(provider="douban", external_id="race-agent", media_type="movie", title="原始来源", year="2026")
        verify = service.verify_tmdb_mapping_candidate

        def concurrent_confirmation(tmdb_id, media_type):
            verified = verify(tmdb_id, media_type)
            discovery_repo.upsert_media_external_id("douban", "race-agent", "movie", "404", "并发确认", "2026", 1.0, True)
            return verified

        async def exercise():
            store = SQLiteKernelStore()
            context = await context_for(store, owner="mapping-race", session="mapping-session")
            effects = EffectExecutor(catalog=ToolCatalog([media_mapping_tool()]), state_store=store, reference_store=store,
                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
            prepared = await effects.prepare("media.confirm_mapping", {
                "provider": "douban", "external_id": "race-agent", "media_type": "movie", "tmdb_id": "303",
            }, context=context)
            assert prepared.effect_plan is not None
            with patch.object(service, "verify_tmdb_mapping_candidate", side_effect=concurrent_confirmation):
                with self.assertRaises(ToolPipelineError) as caught:
                    await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
            self.assertEqual(caught.exception.code, "confirmation_stale")
            self.assertFalse(caught.exception.execution_started)
            row = discovery_repo.get_media_external_id("douban", "race-agent", "movie")
            assert row is not None and row["tmdb_id"] == "404" and row["confirmed"]

        with patch.object(service, "get_detail", return_value=card), patch("app.agent.kernel.ports.sdk_effects.get_discovery_service", return_value=service):
            asyncio.run(exercise())

    def test_keyed_confirmation_fingerprints_are_canonical_domain_separated_and_cover_rss(
        self,
    ) -> None:
        value_a = {"a": 1, "b": [2, 3]}
        value_b = {"b": [2, 3], "a": 1}
        with patch("app.modules.web_secret.get_web_secret", return_value="a" * 32):
            first = signed_context_fingerprint(value_a, domain="domain-a")
            reordered = signed_context_fingerprint(value_b, domain="domain-a")
            other_domain = signed_context_fingerprint(value_a, domain="domain-b")
        with patch("app.modules.web_secret.get_web_secret", return_value="b" * 32):
            other_secret = signed_context_fingerprint(value_a, domain="domain-a")
        self.assertEqual(first, reordered)
        # 已受理旧任务必须仍能验签；这是迁移前保存的固定签名向量。
        self.assertEqual(first, "7ed6cef4291756bb183923b42a59604005adc70aa7cb5c40449c35cbc77966a2")
        self.assertNotEqual(first, other_domain)
        self.assertNotEqual(first, other_secret)
        for invalid_value in (
            {"value": float("nan")},
            {"value": object()},
            {"value": "\ud800"},
        ):
            with self.subTest(invalid_value=type(invalid_value["value"]).__name__):
                with self.assertRaises(ValueError):
                    signed_context_fingerprint(invalid_value, domain="domain-a")
        for invalid_domain in ("", "确认域", None):
            with self.subTest(invalid_domain=invalid_domain):
                with self.assertRaises(ValueError):
                    signed_context_fingerprint(value_a, domain=cast(str, invalid_domain))
        _sid, first_entry, second_entry = self._rss()
        runtime = {
            "url": "http://qb.private.invalid",
            "username": "admin",
            "password": "weak-password",
            "api_key": "PRIVATE-API-KEY",
            "default_method": "qb",
            "default_save_path": "/private/default",
            "category": "media",
            "timeout": 10,
        }
        with patch(
            "app.modules.rss.capture_rss_qb_runtime_config",
            side_effect=lambda: (dict(runtime), ""),
        ):
            pending = rss_delivery._capture("download", {"limit": 1})
            pending_fingerprint = rss_delivery.prepare("download", {"limit": 1})[1]
            rss_repo.record_rss_entry_failure(second_entry, "qb_unavailable", True)
            retryable = rss_delivery._capture("retry", {"limit": 1})
            retry_fingerprint = rss_delivery.prepare("retry", {"limit": 1})[1]

        def raw_sha(state: dict) -> str:
            return hashlib.sha256(
                json.dumps(
                    {
                        "limit": 1,
                        "entries": state["entries"],
                        "has_more": state["has_more"],
                        "runtime_config": state["runtime_config"],
                        "config_error": state["config_error"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        self.assertIn(pending["entries"][0]["id"], {first_entry, second_entry})
        self.assertNotEqual(pending_fingerprint, raw_sha(pending))
        self.assertNotEqual(retry_fingerprint, raw_sha(retryable))


    def test_patrol_trigger_queues_or_reuses_without_policy_change(self) -> None:
        scheduler = AgentLibraryPatrolScheduler()
        original_get_bool = config.get_bool

        async def exercise():
            store = SQLiteKernelStore()
            context = await context_for(store, owner="patrol-batch5", session="patrol-session")
            effects = EffectExecutor(catalog=ToolCatalog([library_patrol_tool()]), state_store=store, reference_store=store,
                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
            first = await effects.prepare("library.run_patrol", {}, context=context)
            assert first.effect_plan is not None
            self.assertEqual(first.outcome.public_content["status"], "confirmation_required")
            accepted = await effects.execute_confirmed(first.effect_plan.plan_id, context=context)
            result = accepted.outcome.public_content
            self.assertEqual(result["status"], "accepted")
            self.assertTrue(result["data"]["queued"])
            run_id = result["data"]["run_id"]
            claimed = agent_library_patrol_repo.claim_due_agent_library_patrol(current_time=scheduler._now())
            assert claimed is not None and claimed["cycle_run_id"] == run_id
            second = await effects.prepare("library.run_patrol", {}, context=context)
            assert second.effect_plan is not None
            reused = (await effects.execute_confirmed(second.effect_plan.plan_id, context=context)).outcome.public_content
            self.assertTrue(reused["data"]["reused"])
            self.assertEqual(reused["data"]["run_id"], run_id)
            run = db.get_task_run(run_id)
            assert run is not None
            self.assertEqual(run["status"], "running")
            self.assertTrue(scheduler._wake_event.is_set())

        with (
            patch.object(config, "get_bool", side_effect=lambda key, default=False:
                True if key == "AGENT_LIBRARY_PATROL_ENABLED" else original_get_bool(key, default)),
            patch("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", return_value=scheduler),
        ):
            asyncio.run(exercise())
