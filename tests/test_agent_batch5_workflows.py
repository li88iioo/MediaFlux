"""Media Agent 第 5 批长尾业务安全闭环回归。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.agent.action_history import list_action_history
from app.agent.confirmation import ConfirmationStore, confirmation_context_fingerprint
from app.agent.discovery_mapping_actions import (
    _recent,
    configure_discovery_mapping_context,
    confirm_discovery_mapping_confirmed,
    discovery_confirm_mapping_arguments,
    get_discovery_detail,
    get_discovery_mapping_candidates,
    prepare_confirm_discovery_mapping,
)
from app.agent.guangya_directory_scrape_actions import (
    _flows,
    configure_directory_scrape_context,
    directory_scrape_run_arguments,
    execute_durable_directory_scrape_job,
    inspect_directory_scrape,
    prepare_run_directory_scrape,
    preview_directory_scrape,
    run_directory_scrape_confirmed,
    search_directory_scrape,
)
from app.agent.library_patrol_trigger_actions import (
    patrol_trigger_arguments,
    prepare_trigger_patrol_now,
    trigger_patrol_now_confirmed,
)
from app.agent.llm_router import LLMConversationReply, LLMToolSelection
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    discovery_mapping_confirmation_request,
    guangya_directory_scrape_request,
    is_patrol_trigger_now_message,
    is_strm_run_history_message,
    recent_discovery_candidate_request,
    rss_entry_mark_request,
    rss_entry_submit_request,
    rss_entry_summaries_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.result_projection import (
    project_agent_response_for_llm,
    project_agent_result_for_user,
)
from app.agent.rss_download_actions import _capture as capture_pending_rss
from app.agent.rss_entry_actions import (
    _submit_snapshot,
    list_rss_entry_summaries,
    mark_rss_entries_confirmed,
    prepare_mark_rss_entries,
    prepare_submit_rss_entries,
    rss_entry_summaries_arguments,
    rss_mark_entries_arguments,
    rss_submit_entries_arguments,
    submit_rss_entries_confirmed,
)
from app.agent.rss_retry_actions import _capture as capture_retryable_rss
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.agent.strm_history_actions import (
    get_strm_run_history,
    strm_run_history_arguments,
)
from app.discovery.models import MediaCard
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.modules.directory_scrape_errors import DirectoryScrapeGoneError
from tests.support import IsolatedDatabaseTestCase


class Batch5AgentWorkflowTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()
        _flows.clear()
        with db.get_conn() as conn:
            for table in (
                "rss_entry_media", "rss_entries", "rss_items", "media_external_ids",
                "task_runs", "strm_change_queue", "strm_metadata_queue", "strm_failures",
                "agent_library_patrol", "organize_operation_jobs",
            ):
                conn.execute(f"DELETE FROM {table}")

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        _flows.clear()

    def _rss(self) -> tuple[int, int, int]:
        sid = db.add_rss_subscription(
            name="安全订阅",
            urls="https://secret.invalid/feed?token=PRIVATE",
            download_method="qb",
            qb_save_path="/private/rss",
        )
        first = db.add_rss_entry(
            sid, "公开标题 1", "SECRET-GUID-1",
            payload=json.dumps({"torrent_url": "magnet:?xt=urn:btih:PRIVATE1"}),
        )
        second = db.add_rss_entry(
            sid, "公开标题 2", "SECRET-GUID-2",
            payload=json.dumps({"torrent_url": "magnet:?xt=urn:btih:PRIVATE2"}),
        )
        assert first and second
        return sid, int(first), int(second)

    def test_registry_and_argument_contracts_are_strict(self) -> None:
        caps = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        expected = {
            "rss.entry_summaries": ("read", False),
            "rss.mark_entries": ("low_write", True),
            "rss.submit_entries_to_qb": ("danger", True),
            "strm.run_history": ("read", False),
            "guangya.directory_scrape.inspect": ("read", False),
            "guangya.directory_scrape.search": ("read", False),
            "guangya.directory_scrape.preview": ("read", False),
            "guangya.directory_scrape.run": ("danger", True),
            "discovery.detail": ("read", False),
            "discovery.mapping_candidates": ("read", False),
            "discovery.confirm_mapping": ("low_write", True),
            "library.trigger_patrol_now": ("low_write", True),
        }
        for name, contract in expected.items():
            self.assertEqual((caps[name]["risk"], caps[name]["requires_confirmation"]), contract)
            self.assertFalse(caps[name]["parameters"].get("additionalProperties", True))
        self.assertEqual(rss_entry_summaries_arguments({}), {"status": "pending", "limit": 20})
        self.assertEqual(rss_mark_entries_arguments({"entry_numbers": [1, 2], "processed": True})["entry_numbers"], [1, 2])
        self.assertEqual(rss_submit_entries_arguments({"entry_numbers": [1]}), {"entry_numbers": [1]})
        self.assertEqual(strm_run_history_arguments({}), {"limit": 8, "status": "all"})
        self.assertEqual(patrol_trigger_arguments({}), {})
        self.assertEqual(directory_scrape_run_arguments({}), {})
        self.assertEqual(discovery_confirm_mapping_arguments({"candidate_number": 1}), {"candidate_number": 1})
        for invalid in (
            {"entry_numbers": [True], "processed": True},
            {"entry_numbers": [1, 1], "processed": True},
            {"entry_numbers": [], "processed": True},
        ):
            with self.assertRaises(AgentToolError):
                rss_mark_entries_arguments(invalid)

    def test_rss_safe_list_mark_and_exact_qb_submit(self) -> None:
        sid, first, second = self._rss()
        listed = list_rss_entry_summaries({"subscription_number": sid, "status": "pending", "limit": 20})
        self.assertEqual(listed.data["entry_count"], 2)
        rendered = json.dumps(listed.to_dict(), ensure_ascii=False)
        self.assertIn("公开标题", rendered)
        for secret in ("SECRET-GUID", "magnet", "PRIVATE", "/private/rss", "secret.invalid"):
            self.assertNotIn(secret, rendered)

        mark_args = {"entry_numbers": [first, second], "processed": True}
        preview, fingerprint = prepare_mark_rss_entries(mark_args)
        self.assertTrue(preview.ok)
        marked = mark_rss_entries_confirmed(mark_args, fingerprint)
        self.assertEqual(marked.data["affected"], 2)
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "skipped")

        # 恢复为 pending 后，精确提交只接受确认时冻结的集合与 qB 配置。
        db.update_rss_entries_processed([first, second], False)
        runtime = {
            "url": "http://qb.internal", "username": "u", "password": "secret",
            "api_key": "key", "category": "rss", "default_save_path": "/private",
            "default_method": "qb", "timeout": 10,
        }
        submit_args = {"entry_numbers": [first, second]}
        with patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(runtime, "")):
            submit_preview, submit_context = prepare_submit_rss_entries(submit_args)
            self.assertTrue(submit_preview.ok)
            raw = {"requested": 2, "claimed": 2, "submitted": 2, "failed": 0}
            with patch("app.modules.rss.RSSEngine.submit_pending_qb_snapshot", return_value=raw) as submit:
                result = submit_rss_entries_confirmed(submit_args, submit_context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["submitted"], 2)
        submitted_rows = submit.call_args.args[0]
        self.assertEqual({row["id"] for row in submitted_rows}, {first, second})

    def test_rss_mark_snapshot_fails_closed_after_state_change(self) -> None:
        _sid, first, _second = self._rss()
        args = {"entry_numbers": [first], "processed": True}
        _preview, fingerprint = prepare_mark_rss_entries(args)
        db.update_rss_entry_status(first, "downloaded")
        with self.assertRaises(AgentToolError) as caught:
            mark_rss_entries_confirmed(args, fingerprint)
        self.assertEqual(caught.exception.code, "confirmation_stale")

    def test_rss_mark_snapshot_rejects_atomic_race_without_overwriting_failure(self) -> None:
        _sid, first, _second = self._rss()
        args = {"entry_numbers": [first], "processed": True}
        _preview, fingerprint = prepare_mark_rss_entries(args)
        original_update = db.update_rss_entries_processed_snapshot

        def race_then_update(snapshot, processed):
            db.record_rss_entry_failure(first, "qb_unavailable", True)
            return original_update(snapshot, processed)

        with patch(
            "app.agent.rss_entry_actions.db.update_rss_entries_processed_snapshot",
            side_effect=race_then_update,
        ), self.assertRaises(AgentToolError) as caught:
            mark_rss_entries_confirmed(args, fingerprint)

        self.assertEqual(caught.exception.code, "confirmation_stale")
        row = db.get_rss_entry(first)
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(str(row["failure_code"]), "qb_unavailable")
        self.assertTrue(bool(row["failure_retryable"]))

    def test_strm_history_exposes_only_safe_aggregates(self) -> None:
        run_id = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(run_id, "failed", result=json.dumps({
            "mode": "full", "base_url": "https://secret.invalid/token",
            "stats": {"generated": 3, "failed": 1, "changed_dirs": ["/private/path"]},
            "sources": [{"id": "PRIVATE-ID", "name": "secret source"}],
            "elapsed_seconds": 12.5,
        }), error="PRIVATE traceback /private/path")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO strm_change_queue(provider,source_id,state,dirty,version,attempts,lease_owner,lease_generation,lease_until,next_attempt_at,pending_changes_json,inflight_changes_json,last_error,created_at,updated_at) "
                "VALUES('guangya','PRIVATE','queued',1,1,0,'',0,0,0,'[]','[]','PRIVATE',0,0)"
            )
            conn.execute(
                "INSERT INTO task_runs(task_name,trigger_type,status,started_at,finished_at,result,error) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "strm_sync",
                    "https://evil.invalid/trigger",
                    "/private/status",
                    "/private/started",
                    "SECRET-FINISHED",
                    json.dumps({
                        "mode": "/private/mode",
                        "stats": {"generated": 7, "/private/stat": 99},
                        "elapsed_seconds": 999999999,
                    }),
                    "SECRET-ERROR",
                ),
            )
        result = get_strm_run_history({"limit": 8, "status": "all"})
        self.assertEqual(result.data["run_count"], 2)
        self.assertEqual(result.data["runs"][0]["status"], "unknown")
        self.assertEqual(result.data["runs"][0]["trigger_type"], "unknown")
        self.assertEqual(result.data["runs"][0]["started_at"], "")
        self.assertEqual(result.data["runs"][0]["finished_at"], "")
        self.assertEqual(result.data["runs"][0]["mode"], "unknown")
        self.assertEqual(result.data["runs"][0]["elapsed_seconds"], 604800.0)
        self.assertEqual(result.data["runs"][1]["stats"]["generated"], 3)
        rendered = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "secret.invalid", "evil.invalid", "PRIVATE-ID", "/private",
            "traceback", "secret source", "SECRET-ERROR", "SECRET-FINISHED",
        ):
            self.assertNotIn(secret, rendered)

    def test_discovery_detail_candidates_and_confirmed_mapping(self) -> None:
        card = MediaCard(
            provider="douban", external_id="1292052", media_type="movie",
            title="肖申克的救赎", original_title="The Shawshank Redemption",
            year="1994", overview="安全简介", rating=9.7, rating_source="douban",
        )
        candidate = {"tmdb_id": "278", "title": "The Shawshank Redemption", "year": "1994", "score": 0.98, "media_type": "movie"}
        service = Mock()
        service.get_detail.return_value = card
        service.lookup_tmdb_mapping.return_value = {"tmdb_id": "", "confirmed": False, "candidates": [candidate]}
        service.verify_tmdb_mapping_candidate.return_value = {
            "tmdb_id": "278", "title": "The Shawshank Redemption",
            "year": "1994", "media_type": "movie",
        }
        service.confirm_tmdb_mapping_if_unchanged.return_value = {
            "tmdb_id": "278", "confirmed": True
        }
        context = ToolContext(owner="owner-a")
        with patch("app.agent.discovery_mapping_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_mapping_actions.get_discovery_service", return_value=service
        ):
            detail = get_discovery_detail({"provider": "douban", "external_id": "1292052", "media_type": "movie"}, context)
            candidates = get_discovery_mapping_candidates({"provider": "douban", "external_id": "1292052", "media_type": "movie"}, context)
            preview, fingerprint = prepare_confirm_discovery_mapping({"candidate_number": 1}, context)
            confirmed = confirm_discovery_mapping_confirmed({"candidate_number": 1}, fingerprint, context)
        self.assertTrue(detail.ok)
        self.assertEqual(candidates.data["candidate_count"], 1)
        self.assertNotIn("tmdb_id", candidates.data["candidates"][0])
        self.assertTrue(preview.ok)
        self.assertTrue(confirmed.data["mapping_confirmed"])
        service.confirm_tmdb_mapping_if_unchanged.assert_called_once_with(
            "douban", "1292052", "movie", "278", None
        )

    def test_discovery_mapping_cas_allows_only_one_concurrent_confirmation(self) -> None:
        barrier = threading.Barrier(3)

        def save(tmdb_id: str) -> bool:
            barrier.wait(timeout=2)
            return db.confirm_media_external_id_if_unchanged(
                "douban",
                "race-source",
                "movie",
                tmdb_id,
                f"候选 {tmdb_id}",
                "2026",
                None,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, tmdb_id) for tmdb_id in ("101", "202")]
            barrier.wait(timeout=2)
            outcomes = [future.result(timeout=3) for future in futures]

        self.assertEqual(sum(bool(value) for value in outcomes), 1)
        stored = db.get_media_external_id("douban", "race-source", "movie")
        self.assertIn(str(stored["tmdb_id"]), {"101", "202"})
        self.assertTrue(bool(stored["confirmed"]))

    def test_discovery_mapping_confirmation_restores_persisted_snapshot(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "mapping-test-secret"
        )
        configure_discovery_mapping_context(repository)
        card = MediaCard(
            provider="douban", external_id="persisted-source", media_type="movie",
            title="持久化映射", year="2026",
        )
        candidate = {
            "tmdb_id": "909", "title": "持久化候选", "year": "2026",
            "score": 0.97, "media_type": "movie",
        }
        service = Mock()
        service.get_detail.return_value = card
        service.lookup_tmdb_mapping.return_value = {
            "tmdb_id": "", "confirmed": False, "candidates": [candidate]
        }
        service.verify_tmdb_mapping_candidate.return_value = dict(candidate)
        service.confirm_tmdb_mapping_if_unchanged.return_value = {
            "tmdb_id": "909", "confirmed": True,
        }
        context = ToolContext(owner="owner-persisted-mapping")
        with patch(
            "app.agent.discovery_mapping_actions.config.get_bool", return_value=True,
        ), patch(
            "app.agent.discovery_mapping_actions.get_discovery_service",
            return_value=service,
        ):
            get_discovery_mapping_candidates(
                {
                    "provider": "douban", "external_id": "persisted-source",
                    "media_type": "movie",
                },
                context,
            )
            _preview, fingerprint = prepare_confirm_discovery_mapping(
                {"candidate_number": 1}, context
            )
            _recent.clear()  # 模拟确认请求命中另一个 Worker / 进程重启。
            confirmed = confirm_discovery_mapping_confirmed(
                {"candidate_number": 1}, fingerprint, context
            )
        self.assertTrue(confirmed.ok)
        self.assertIsNone(repository.get_latest(
            owner=context.owner, context_type="discovery_mapping", now=time.time(),
        ))

    def test_discovery_mapping_cas_preserves_concurrently_confirmed_mapping(self) -> None:
        db.upsert_media_external_id(
            "douban", "race-existing", "movie", "100", "旧候选", "2025",
            0.8, False,
        )
        before = db.get_media_external_id("douban", "race-existing", "movie")
        expected = {
            "tmdb_id": str(before["tmdb_id"] or ""),
            "confirmed": bool(before["confirmed"]),
            "version": int(before["version"] or 0),
            "updated_at": str(before["updated_at"] or ""),
        }
        with patch("app.database.now", return_value=str(before["updated_at"])):
            db.upsert_media_external_id(
                "douban", "race-existing", "movie", "200", "并发确认候选",
                "2026", 1.0, True,
            )
            saved = db.confirm_media_external_id_if_unchanged(
                "douban", "race-existing", "movie", "300", "迟到候选",
                "2027", expected,
            )

        self.assertFalse(saved)
        stored = db.get_media_external_id("douban", "race-existing", "movie")
        self.assertEqual(str(stored["tmdb_id"]), "200")
        self.assertEqual(str(stored["title"]), "并发确认候选")
        self.assertTrue(bool(stored["confirmed"]))

    def test_discovery_mapping_reports_stale_when_atomic_save_loses_race(self) -> None:
        card = MediaCard(
            provider="douban", external_id="race-agent", media_type="movie",
            title="并发候选", year="2026",
        )
        candidate = {
            "tmdb_id": "303", "title": "候选 303", "year": "2026",
            "score": 0.95, "media_type": "movie",
        }
        service = Mock()
        service.get_detail.return_value = card
        service.lookup_tmdb_mapping.return_value = {
            "tmdb_id": "", "confirmed": False, "candidates": [candidate]
        }
        service.verify_tmdb_mapping_candidate.return_value = dict(candidate)
        service.confirm_tmdb_mapping_if_unchanged.return_value = None
        context = ToolContext(owner="owner-mapping-race")
        with patch(
            "app.agent.discovery_mapping_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.discovery_mapping_actions.get_discovery_service",
            return_value=service,
        ):
            get_discovery_mapping_candidates(
                {
                    "provider": "douban", "external_id": "race-agent",
                    "media_type": "movie",
                },
                context,
            )
            _preview, fingerprint = prepare_confirm_discovery_mapping(
                {"candidate_number": 1}, context
            )
            with self.assertRaises(AgentToolError) as caught:
                confirm_discovery_mapping_confirmed(
                    {"candidate_number": 1}, fingerprint, context
                )
        self.assertEqual(caught.exception.code, "confirmation_stale")

    def test_guangya_scrape_facade_keeps_ids_and_paths_private(self) -> None:
        record = SimpleNamespace(created_at=1.0, signature=(("PRIVATE-ID", "/private/path"),), claimed=False)
        service = Mock()
        service.inspect.return_value = {
            "inspection_id": "inspection-secret", "media_type": "tv", "suggested_query": "沧元图",
            "season": 3, "episode": None, "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 2},
            "directory": {"id": "PRIVATE-DIR", "name": "PRIVATE DIR"},
        }
        service.search.return_value = [{
            "provider": "tmdb", "external_id": "123", "tmdb_id": "123",
            "title": "沧元图", "year": "2026", "media_type": "tv", "score": 0.95,
        }]
        service.preview.return_value = {
            "preview_id": "preview-secret",
            "plans": [
                {"file_id": "PRIVATE-FILE", "original_path": "/private/a", "target_path": "/private/b", "action": "move", "conflict_decision": "none"},
                {"file_id": "PRIVATE-FILE-2", "original_path": "/private/c", "target_path": "/private/d", "action": "PRIVATE-ACTION", "conflict_decision": "none"},
            ],
            "companion_plans": [],
        }
        service.store.get_preview.return_value = record
        service.preview_reference.return_value = "PRIVATE DIR"
        context = ToolContext(owner="owner-a")
        with patch("app.agent.guangya_directory_scrape_actions.get_directory_scrape_service", return_value=service):
            inspected = inspect_directory_scrape({"directory_id": "dir-1"}, context)
            searched = search_directory_scrape({"media_type": "auto"}, context)
            previewed = preview_directory_scrape({"candidate_number": 1, "numbering_mode": "auto"}, context)
            prepared, fingerprint = prepare_run_directory_scrape({}, context)
            original_signature = record.signature
            record.signature = (("CHANGED-ID", "/changed/private/path"),)
            with self.assertRaises(AgentToolError) as stale:
                run_directory_scrape_confirmed({}, fingerprint, context)
            self.assertEqual(stale.exception.code, "confirmation_stale")
            record.signature = original_signature
            with patch("app.modules.organize_tasks.get_organize_manager") as manager:
                manager.return_value.start_durable_operation.return_value = {"ok": True, "queued": True, "queue_position": 2}
                accepted = run_directory_scrape_confirmed({}, fingerprint, context)
        self.assertTrue(inspected.ok and searched.ok and previewed.ok and prepared.ok)
        self.assertEqual(accepted.status, "accepted")
        with self.assertRaises(AgentToolError) as consumed:
            run_directory_scrape_confirmed({}, fingerprint, context)
        self.assertEqual(consumed.exception.code, "confirmation_stale")
        rendered = json.dumps({
            "inspect": inspected.to_dict(), "search": searched.to_dict(),
            "preview": previewed.to_dict(), "prepare": prepared.to_dict(), "accepted": accepted.to_dict(),
        }, ensure_ascii=False)
        for secret in (
            "inspection-secret", "preview-secret", "PRIVATE-DIR", "PRIVATE-FILE",
            "/private/a", "/private/b", "/private/c", "/private/d", "PRIVATE-ACTION",
        ):
            self.assertNotIn(secret, rendered)

    def test_guangya_scrape_filters_candidates_that_cannot_be_restored(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "scrape-candidate-validation-secret"
        )
        configure_directory_scrape_context(repository)
        context = ToolContext(owner="owner-scrape-candidate-validation")
        service = Mock()
        service.inspect.return_value = {
            "inspection_id": "inspection-candidates", "media_type": "tv",
            "suggested_query": "合法候选", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        service.search.return_value = [
            {
                "provider": "tmdb", "external_id": "12/34",
                "tmdb_id": "12/34", "title": "非法候选",
                "year": "2026", "media_type": "tv", "score": 0.99,
            },
            {
                "provider": "tmdb", "external_id": "567",
                "tmdb_id": "567", "title": "合法候选",
                "year": "2026", "media_type": "tv", "score": 0.95,
            },
        ]
        service.store.get_inspection.return_value = SimpleNamespace()
        service.preview.return_value = {
            "preview_id": "preview-candidates",
            "plans": [{
                "action": "move", "conflict_decision": "none",
                "file_id": "PRIVATE-FILE", "target_path": "/private/candidate",
            }],
            "companion_plans": [],
        }
        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=service,
        ):
            inspect_directory_scrape({"directory_id": "dir-candidates"}, context)
            searched = search_directory_scrape({"media_type": "auto"}, context)
            self.assertEqual(searched.data["candidate_count"], 1)
            self.assertEqual(
                searched.data["candidates"][0]["candidate_number"], 1
            )
            self.assertEqual(
                searched.data["candidates"][0]["title"], "合法候选"
            )
            _flows.clear()
            previewed = preview_directory_scrape(
                {"candidate_number": 1, "numbering_mode": "auto"}, context
            )
        self.assertTrue(previewed.ok)
        self.assertEqual(service.preview.call_args.args[2], "567")
        self.assertEqual(service.preview.call_args.args[3], "tv")

    def test_guangya_scrape_queue_rejection_restores_preview_for_retry(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "scrape-retry-secret"
        )
        configure_directory_scrape_context(repository)
        context = ToolContext(owner="owner-scrape-queue-retry")
        record = SimpleNamespace(
            inspection=SimpleNamespace(fingerprint="retry-inspection"),
            rules=None, signature=(("move", "retry-plan"),),
            target_snapshot=(("target", "retry"),), claimed=False,
        )
        service = Mock()
        service.inspect.return_value = {
            "inspection_id": "inspection-retry", "media_type": "tv",
            "suggested_query": "重试预览", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        service.search.return_value = [{
            "provider": "tmdb", "external_id": "321", "tmdb_id": "321",
            "title": "重试预览", "year": "2026",
            "media_type": "tv", "score": 0.95,
        }]
        service.preview.return_value = {
            "preview_id": "preview-retry",
            "plans": [{
                "action": "move", "conflict_decision": "none",
                "file_id": "PRIVATE-FILE", "target_path": "/private/retry",
            }],
            "companion_plans": [],
        }
        service.store.get_inspection.return_value = SimpleNamespace()
        service.store.get_preview.return_value = record
        service.preview_reference.return_value = "PRIVATE DIRECTORY"

        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=service,
        ), patch("app.modules.organize_tasks.get_organize_manager") as manager:
            inspect_directory_scrape({"directory_id": "dir-retry"}, context)
            search_directory_scrape({"media_type": "auto"}, context)
            preview_directory_scrape(
                {"candidate_number": 1, "numbering_mode": "auto"}, context
            )
            _prepared, fingerprint = prepare_run_directory_scrape({}, context)
            manager.return_value.start_durable_operation.return_value = {
                "ok": False, "error": "queue unavailable"
            }
            with self.assertRaises(AgentToolError) as rejected:
                run_directory_scrape_confirmed({}, fingerprint, context)
            self.assertEqual(rejected.exception.code, "precondition_failed")
            self.assertIn("预览已保留", str(rejected.exception))

            _retry_preview, retry_fingerprint = prepare_run_directory_scrape({}, context)
            manager.return_value.start_durable_operation.return_value = {
                "ok": True, "queued": True, "queue_position": 1
            }
            accepted = run_directory_scrape_confirmed(
                {}, retry_fingerprint, context
            )

        self.assertEqual(accepted.status, "accepted")
        self.assertIsNone(repository.get_latest(
            owner=context.owner, context_type="directory_scrape", now=time.time(),
        ))

    def test_guangya_scrape_confirmation_rebuilds_preview_in_another_worker(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "scrape-test-secret"
        )
        configure_directory_scrape_context(repository)
        context = ToolContext(owner="owner-persisted-scrape")
        inspection = SimpleNamespace(fingerprint="stable-inspection")
        original_record = SimpleNamespace(
            inspection=inspection,
            rules=None,
            signature=(("move", "same-plan"),),
            target_snapshot=(("target", "same"),),
            claimed=False,
        )
        rebuilt_record = SimpleNamespace(
            inspection=inspection,
            rules=None,
            signature=original_record.signature,
            target_snapshot=original_record.target_snapshot,
            claimed=False,
        )
        plans = [{
            "action": "move", "conflict_decision": "none",
            "file_id": "PRIVATE-FILE", "target_path": "/private/target",
        }]
        first = Mock()
        first.inspect.return_value = {
            "inspection_id": "inspection-old", "media_type": "tv",
            "suggested_query": "沧元图", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        first.search.return_value = [{
            "provider": "tmdb", "external_id": "123", "tmdb_id": "123",
            "title": "沧元图", "year": "2026", "media_type": "tv", "score": 0.95,
        }]
        first.preview.return_value = {
            "preview_id": "preview-old", "plans": plans, "companion_plans": [],
        }
        first.store.get_inspection.return_value = SimpleNamespace()
        first.store.get_preview.return_value = original_record

        second = Mock()
        second.store.get_inspection.side_effect = DirectoryScrapeGoneError("gone")

        def get_preview(_owner: str, preview_id: str):
            if preview_id == "preview-old":
                raise DirectoryScrapeGoneError("gone")
            self.assertEqual(preview_id, "preview-new")
            return rebuilt_record

        second.store.get_preview.side_effect = get_preview
        second.inspect.return_value = {
            "inspection_id": "inspection-new", "media_type": "tv",
            "suggested_query": "沧元图", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        second.preview.return_value = {
            "preview_id": "preview-new", "plans": plans, "companion_plans": [],
        }
        second.preview_reference.return_value = "PRIVATE DIRECTORY"

        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=first,
        ):
            inspect_directory_scrape({"directory_id": "dir-restore"}, context)
            search_directory_scrape({"media_type": "auto"}, context)
            preview_directory_scrape(
                {"candidate_number": 1, "numbering_mode": "auto"}, context,
            )
            _prepared, fingerprint = prepare_run_directory_scrape({}, context)

        _flows.clear()  # 模拟确认请求命中另一个 Worker / 进程重启。
        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=second,
        ), patch("app.modules.organize_tasks.get_organize_manager") as manager:
            manager.return_value.start_durable_operation.return_value = {
                "ok": True, "queued": True, "queue_position": 1,
            }
            accepted = run_directory_scrape_confirmed({}, fingerprint, context)
            durable_payload = manager.return_value.start_durable_operation.call_args.kwargs["payload"]

        self.assertTrue(accepted.ok)
        second.inspect.assert_called_once_with(context.owner, "dir-restore")
        second.preview.assert_called_once()
        second.execute_preview.return_value = {"stats": {"moved": 1}}
        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=second,
        ):
            durable_result = execute_durable_directory_scrape_job(durable_payload)
        self.assertEqual(durable_result["stats"]["moved"], 1)
        execution_owner = durable_payload["execution_owner"]
        second.inspect.assert_any_call(execution_owner, "dir-restore")
        second.execute_preview.assert_called_once_with(execution_owner, "preview-new")
        self.assertIsNone(repository.get_latest(
            owner=context.owner, context_type="directory_scrape", now=time.time(),
        ))

    def test_reset_session_clears_mapping_and_scrape_continuations(self) -> None:
        agent = get_agent_service()
        repository = agent.session_context_repository
        self.assertIsNotNone(repository)
        owner = "owner-reset-batch5"
        context = ToolContext(owner=owner)

        discovery = Mock()
        discovery.get_detail.return_value = MediaCard(
            provider="douban", external_id="reset-source", media_type="movie",
            title="重置测试", year="2026",
        )
        discovery.lookup_tmdb_mapping.return_value = {
            "tmdb_id": "", "confirmed": False,
            "candidates": [{
                "tmdb_id": "808", "title": "重置候选", "year": "2026",
                "score": 0.9, "media_type": "movie",
            }],
        }
        scrape = Mock()
        scrape.inspect.return_value = {
            "inspection_id": "inspection-reset", "media_type": "tv",
            "suggested_query": "重置测试", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        with patch(
            "app.agent.discovery_mapping_actions.config.get_bool", return_value=True,
        ), patch(
            "app.agent.discovery_mapping_actions.get_discovery_service",
            return_value=discovery,
        ), patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=scrape,
        ):
            get_discovery_mapping_candidates(
                {
                    "provider": "douban", "external_id": "reset-source",
                    "media_type": "movie",
                },
                context,
            )
            inspect_directory_scrape({"directory_id": "dir-reset"}, context)

        self.assertIn(owner, _recent)
        self.assertIn(owner, _flows)
        reset = agent.reset_session(owner=owner)
        self.assertTrue(reset["reset"])
        self.assertNotIn(owner, _recent)
        self.assertNotIn(owner, _flows)
        self.assertIsNone(repository.get_latest(
            owner=owner, context_type="discovery_mapping", now=time.time(),
        ))
        self.assertIsNone(repository.get_latest(
            owner=owner, context_type="directory_scrape", now=time.time(),
        ))

    def test_reset_race_cannot_reinsert_discovery_mapping_context(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "mapping-reset-race-secret"
        )
        configure_discovery_mapping_context(repository)
        agent = AgentOrchestrator(
            ToolRegistry(), session_context_repository=repository,
        )
        owner = "owner-mapping-reset-race"
        context = ToolContext(owner=owner)
        entered = threading.Event()
        release = threading.Event()
        failures: list[Exception] = []
        service = Mock()
        service.get_detail.return_value = MediaCard(
            provider="douban", external_id="race-reset", media_type="movie",
            title="重置竞态", year="2026",
        )

        def lookup(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            return {
                "tmdb_id": "", "confirmed": False,
                "candidates": [{
                    "tmdb_id": "707", "title": "竞态候选", "year": "2026",
                    "score": 0.9, "media_type": "movie",
                }],
            }

        service.lookup_tmdb_mapping.side_effect = lookup

        def run_lookup() -> None:
            try:
                get_discovery_mapping_candidates(
                    {
                        "provider": "douban", "external_id": "race-reset",
                        "media_type": "movie",
                    },
                    context,
                )
            except Exception as exc:  # 断言发生在主线程。
                failures.append(exc)

        with patch(
            "app.agent.discovery_mapping_actions.config.get_bool", return_value=True,
        ), patch(
            "app.agent.discovery_mapping_actions.get_discovery_service",
            return_value=service,
        ):
            worker = threading.Thread(target=run_lookup)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            agent.reset_session(owner=owner)
            release.set()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AgentToolError)
        self.assertEqual(getattr(failures[0], "code", ""), "precondition_failed")
        self.assertIsNone(repository.get_latest(
            owner=owner, context_type="discovery_mapping", now=time.time(),
        ))
        with self.assertRaises(AgentToolError):
            prepare_confirm_discovery_mapping({"candidate_number": 1}, context)

    def test_reset_race_cannot_reinsert_directory_scrape_context(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "scrape-reset-race-secret"
        )
        configure_directory_scrape_context(repository)
        agent = AgentOrchestrator(
            ToolRegistry(), session_context_repository=repository,
        )
        owner = "owner-scrape-reset-race"
        context = ToolContext(owner=owner)
        entered = threading.Event()
        release = threading.Event()
        failures: list[Exception] = []
        service = Mock()

        def inspect(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            return {
                "inspection_id": "inspection-race", "media_type": "tv",
                "suggested_query": "重置竞态", "requires_manual_match": False,
                "manual_match_reason": "", "counts": {"videos": 1},
            }

        service.inspect.side_effect = inspect

        def run_inspect() -> None:
            try:
                inspect_directory_scrape({"directory_id": "dir-race"}, context)
            except Exception as exc:  # 断言发生在主线程。
                failures.append(exc)

        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=service,
        ):
            worker = threading.Thread(target=run_inspect)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            agent.reset_session(owner=owner)
            release.set()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AgentToolError)
        self.assertEqual(getattr(failures[0], "code", ""), "precondition_failed")
        self.assertIsNone(repository.get_latest(
            owner=owner, context_type="directory_scrape", now=time.time(),
        ))
        with self.assertRaises(AgentToolError):
            search_directory_scrape({"media_type": "auto"}, context)

    def test_persisted_reset_invalidates_stale_worker_memory_cache(self) -> None:
        repository = SQLiteAgentSessionContextRepository(
            secret_provider=lambda: "stale-worker-cache-secret"
        )
        configure_directory_scrape_context(repository)
        owner = "owner-stale-worker-cache"
        context = ToolContext(owner=owner)
        service = Mock()
        service.inspect.return_value = {
            "inspection_id": "inspection-stale", "media_type": "tv",
            "suggested_query": "旧目录", "requires_manual_match": False,
            "manual_match_reason": "", "counts": {"videos": 1},
        }
        with patch(
            "app.agent.guangya_directory_scrape_actions.get_directory_scrape_service",
            return_value=service,
        ):
            inspect_directory_scrape({"directory_id": "dir-stale"}, context)
            self.assertIn(owner, _flows)
            repository.invalidate_owner(
                owner=owner, context_types=("directory_scrape",),
            )
            with self.assertRaises(AgentToolError) as caught:
                search_directory_scrape({"media_type": "auto"}, context)
        self.assertEqual(caught.exception.code, "precondition_failed")
        service.search.assert_not_called()
        self.assertNotIn(owner, _flows)

    def test_batch5_llm_projection_keeps_safe_context_and_drops_private_fields(self) -> None:
        projected = project_agent_response_for_llm({
            "tool_call": {"name": "strm.run_history", "arguments": {"path": "/private"}},
            "result": {
                "ok": True,
                "status": "attention",
                "summary": "STRM 运行摘要",
                "data": {
                    "runs": [{
                        "run_number": 1,
                        "status": "failed",
                        "trigger_type": "manual",
                        "started_at": "2026-08-23T01:00:00+08:00",
                        "finished_at": "2026-08-23T01:01:00+08:00",
                        "elapsed_seconds": 60,
                        "mode": "incremental",
                        "stats": {"generated": 3, "failed": 1, "private_source": "SECRET"},
                    }],
                    "failure_context": {
                        "open": 1,
                        "by_action": {"generate": {"open": 1}},
                    },
                    "queue_context": {"metadata_queue": {"retry_wait": 2}},
                    "path": "/private/result",
                },
            },
        })
        assert projected is not None
        rendered = json.dumps(projected, ensure_ascii=False)
        self.assertIn("已生成", rendered)
        self.assertIn("等待重试", rendered)
        for secret in ("/private", "private_source", "SECRET", "arguments"):
            self.assertNotIn(secret, rendered)
        review = project_agent_result_for_user({
            "ok": False,
            "status": "review_required",
            "summary": "提交结果未知，请人工核对",
            "data": {"outcome_unknown": 1},
        })
        self.assertEqual(review["status"], {
            "key": "attention", "label": "需人工核对", "tone": "warning",
        })

    def test_discovery_detail_and_candidates_survive_safe_projections(self) -> None:
        candidates_response = {
            "mode": "read_only",
            "tool_call": {
                "name": "discovery.mapping_candidates",
                "arguments": {
                    "provider": "douban", "external_id": "1292052",
                    "media_type": "movie",
                },
            },
            "result": ToolResult(
                True,
                "selection_required",
                "找到了 1 个可核对候选",
                data={
                    "source_title": "肖申克的救赎",
                    "candidate_count": 1,
                    "candidates": [{
                        "candidate_number": 1,
                        "candidate_title": "The Shawshank Redemption",
                        "candidate_year": "1994",
                        "score": 0.98,
                        "tmdb_id": "278",
                        "private_path": "/private/candidate",
                    }],
                },
            ).to_dict(),
        }
        projected_candidates = project_agent_response_for_llm(candidates_response)
        self.assertIsNotNone(projected_candidates)
        rendered_candidates = json.dumps(projected_candidates, ensure_ascii=False)
        self.assertIn("The Shawshank Redemption", rendered_candidates)
        self.assertIn("1994", rendered_candidates)
        self.assertIn("0.98", rendered_candidates)
        self.assertNotIn("tmdb_id", rendered_candidates)
        self.assertNotIn("/private", rendered_candidates)

        detail_result = ToolResult(
            True,
            "completed",
            "已读取影视详情",
            data={
                "provider": "douban",
                "media_type": "movie",
                "title": "肖申克的救赎",
                "release_date": "1994-09-23",
                "overview": "公开简介",
                "external_id": "PRIVATE-ID",
            },
        ).to_dict()
        projected_detail = project_agent_response_for_llm({
            "mode": "read_only",
            "tool_call": {"name": "discovery.detail", "arguments": {}},
            "result": detail_result,
        })
        public_detail = project_agent_result_for_user(detail_result)
        self.assertIsNotNone(projected_detail)
        for projected in (projected_detail, public_detail):
            rendered = json.dumps(projected, ensure_ascii=False)
            self.assertIn("1994-09-23", rendered)
            self.assertIn("公开简介", rendered)
            self.assertNotIn("PRIVATE-ID", rendered)

    def test_llm_confirmation_paths_reserve_each_tool_budget_once(self) -> None:
        _sid, first, _second = self._rss()
        agent = get_agent_service()
        arguments = {"entry_numbers": [first], "processed": True}

        def native(_message, _registry, execute_tool, **_kwargs):
            prepared = execute_tool("rss.mark_entries", arguments)
            return LLMConversationReply(
                "标记操作已完成预检，尚未执行。",
                tool_executions=({
                    "tool_name": "rss.mark_entries",
                    "arguments": arguments,
                    "response": prepared,
                },),
            )

        with patch(
            "app.agent.orchestrator.run_native_read_agent", side_effect=native
        ), patch(
            "app.agent.orchestrator.allow_agent_tool", return_value=True
        ) as allow_native:
            native_response = agent._query_with_model_tools(
                "把这个 RSS 条目标记为已处理",
                owner="owner-native-budget",
                llm_rate_owner="",
                llm_tool_rate_identity="shared-native-budget",
                conversation_context=None,
                read_only=False,
            )
        self.assertEqual(native_response["mode"], "confirmation_required")
        allow_native.assert_called_once_with(
            "shared-native-budget", "rss.mark_entries"
        )

        with patch(
            "app.agent.orchestrator.run_native_read_agent", return_value=None
        ), patch(
            "app.agent.orchestrator.select_orchestration_tool",
            return_value=LLMToolSelection("rss.mark_entries", arguments),
        ), patch(
            "app.agent.orchestrator.allow_agent_tool", return_value=True
        ) as allow_selection:
            selection_response = agent._query_with_model_tools(
                "把这个 RSS 条目标记为已处理",
                owner="owner-selection-budget",
                llm_rate_owner="",
                llm_tool_rate_identity="shared-selection-budget",
                conversation_context=None,
                read_only=False,
            )
        self.assertEqual(selection_response["mode"], "confirmation_required")
        allow_selection.assert_called_once_with(
            "shared-selection-budget", "rss.mark_entries"
        )

    def test_confirmation_context_uses_keyed_digest_and_stales_on_qb_secret_change(self) -> None:
        _sid, first, _second = self._rss()
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
            state = _submit_snapshot({"entry_numbers": [first]})
            prepared = get_agent_service().prepare(
                "rss.submit_entries_to_qb", {"entry_numbers": [first]}, owner="owner-hmac"
            )
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT context_fingerprint FROM agent_confirmations ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row["context_fingerprint"]), state["fingerprint"])
            raw_sha = hashlib.sha256(json.dumps({
                "requested": [first],
                "entries": state["entries"],
                "runtime": state["runtime"],
                "config_error": state["config_error"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            self.assertNotEqual(state["fingerprint"], raw_sha)

            runtime["password"] = "changed-password"
            with self.assertRaises(AgentToolError) as stale:
                get_agent_service().confirm(
                    prepared["action_plan"]["plan_id"], owner="owner-hmac"
                )
        self.assertEqual(stale.exception.code, "confirmation_stale")

    def test_keyed_confirmation_fingerprints_are_canonical_domain_separated_and_cover_legacy_rss(self) -> None:
        value_a = {"a": 1, "b": [2, 3]}
        value_b = {"b": [2, 3], "a": 1}
        with patch("app.agent.confirmation.get_web_secret", return_value="a" * 32):
            first = confirmation_context_fingerprint(value_a, domain="domain-a")
            reordered = confirmation_context_fingerprint(value_b, domain="domain-a")
            other_domain = confirmation_context_fingerprint(value_a, domain="domain-b")
        with patch("app.agent.confirmation.get_web_secret", return_value="b" * 32):
            other_secret = confirmation_context_fingerprint(value_a, domain="domain-a")
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, other_domain)
        self.assertNotEqual(first, other_secret)

        for invalid_value in (
            {"value": float("nan")},
            {"value": object()},
            {"value": "\ud800"},
        ):
            with self.subTest(invalid_value=type(invalid_value["value"]).__name__):
                with self.assertRaises(ValueError):
                    confirmation_context_fingerprint(
                        invalid_value, domain="domain-a"
                    )
        for invalid_domain in ("", "确认域", None):
            with self.subTest(invalid_domain=invalid_domain):
                with self.assertRaises(ValueError):
                    confirmation_context_fingerprint(
                        value_a, domain=invalid_domain  # type: ignore[arg-type]
                    )

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
            pending = capture_pending_rss({"limit": 1})
            db.record_rss_entry_failure(second_entry, "qb_unavailable", True)
            retryable = capture_retryable_rss({"limit": 1})

        def raw_sha(state: dict) -> str:
            return hashlib.sha256(json.dumps({
                "limit": state["limit"],
                "entries": state["entries"],
                "has_more": state["has_more"],
                "runtime_config": state["runtime_config"],
                "config_error": state["config_error"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        self.assertIn(pending["entries"][0]["id"], {first_entry, second_entry})
        self.assertNotEqual(pending["fingerprint"], raw_sha(pending))
        self.assertNotEqual(retryable["fingerprint"], raw_sha(retryable))

    def test_natural_write_routes_share_declared_tool_rate_budget(self) -> None:
        registry = ToolRegistry()

        def identity(arguments: dict) -> dict:
            return dict(arguments)

        for tool_name in (
            "rss.mark_entries",
            "rss.submit_entries_to_qb",
            "rss.submit_pending_to_qb",
            "rss.retry_failed_to_qb",
            "discovery.confirm_mapping",
            "guangya.directory_scrape.run",
            "library.trigger_patrol_now",
        ):
            registry.register(ToolSpec(
                name=tool_name,
                description="rate test",
                risk=RiskLevel.LOW_WRITE,
                parameters={"type": "object"},
                validator=identity,
                requires_confirmation=True,
                context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(lambda arguments, tool_name=tool_name: (
                    ToolResult(
                        True,
                        "confirmation_required",
                        "preview",
                        data=dict(arguments),
                    ),
                    f"rate-test:{tool_name}:{arguments!r}",
                )),
                context_confirmed_handler=ToolSpec.context_free_confirmed_handler(lambda _arguments, _expected_context: ToolResult(
                    True, "completed", "done"
                )),
            ))
        agent = AgentOrchestrator(registry, confirmation_store=ConfirmationStore())
        cases = (
            ("把 RSS 条目 12 和 13 标记为已处理", "rss.mark_entries"),
            ("下载 RSS 条目 12 和 13 到 qB", "rss.submit_entries_to_qb"),
            ("提交 5 个待处理 RSS 条目到 qB", "rss.submit_pending_to_qb"),
            ("重试 5 个 RSS 失败条目", "rss.retry_failed_to_qb"),
            ("确认第 2 个映射", "discovery.confirm_mapping"),
            ("执行刚才的光鸭刮削预览", "guangya.directory_scrape.run"),
        )
        for index, (message, tool_name) in enumerate(cases):
            with self.subTest(tool_name=tool_name), patch(
                "app.agent.orchestrator.allow_agent_tool", return_value=True
            ) as allow:
                response = agent.query(
                    message,
                    owner=f"owner-rate-{index}",
                    query_tool_rate_identity="shared-rate-owner",
                    present=False,
                )
                self.assertEqual(response["mode"], "confirmation_required")
                allow.assert_called_once_with("shared-rate-owner", tool_name)

        direct_arguments = {
            "rss.mark_entries": {"entry_numbers": [1], "processed": True},
            "rss.submit_entries_to_qb": {"entry_numbers": [1]},
            "rss.submit_pending_to_qb": {"limit": 1},
            "rss.retry_failed_to_qb": {"limit": 1},
            "discovery.confirm_mapping": {"candidate_number": 1},
            "guangya.directory_scrape.run": {},
            "library.trigger_patrol_now": {},
        }
        for index, (tool_name, arguments) in enumerate(direct_arguments.items()):
            with self.subTest(direct_tool=tool_name), patch(
                "app.agent.orchestrator.allow_agent_tool", return_value=True
            ) as allow:
                prepared = agent.prepare(
                    tool_name,
                    arguments,
                    owner=f"owner-direct-rate-{index}",
                    rate_identity="shared-rate-owner",
                )
                self.assertEqual(prepared["mode"], "confirmation_required")
                allow.assert_called_once_with("shared-rate-owner", tool_name)

        with patch("app.agent.orchestrator.allow_agent_tool", return_value=True) as allow:
            patrol = agent.query(
                "按当前策略立即巡检媒体库",
                owner="owner-patrol-rate",
                query_tool_rate_identity="shared-rate-owner",
                present=False,
            )
        self.assertEqual(patrol["mode"], "confirmation_required")
        allow.assert_called_once_with("shared-rate-owner", "library.trigger_patrol_now")

        with patch("app.agent.orchestrator.allow_agent_tool", return_value=False):
            with self.assertRaises(AgentToolError) as limited:
                agent.query(
                    "下载 RSS 条目 99 到 qB",
                    owner="owner-rate-limited",
                    query_tool_rate_identity="shared-rate-owner",
                    present=False,
                )
        self.assertEqual(limited.exception.code, "rate_limited")

    def test_patrol_natural_and_direct_prepare_share_real_persistent_budget(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.trigger_patrol_now",
            description="rate integration",
            risk=RiskLevel.LOW_WRITE,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            validator=lambda _arguments: {},
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(lambda _arguments: (
                ToolResult(True, "confirmation_required", "preview"),
                "patrol-now",
            )),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(lambda _arguments, _expected_context: ToolResult(
                True, "accepted", "queued"
            )),
        ))
        agent = AgentOrchestrator(registry, confirmation_store=ConfirmationStore())
        identity = "patrol-shared-real-budget"
        agent_rate_limiter.reset()
        try:
            natural = agent.query(
                "按当前策略立即巡检媒体库",
                owner="owner-patrol-natural",
                query_tool_rate_identity=identity,
                present=False,
            )
            direct = agent.prepare(
                "library.trigger_patrol_now",
                {},
                owner="owner-patrol-direct",
                rate_identity=identity,
            )
            self.assertEqual(natural["mode"], "confirmation_required")
            self.assertEqual(direct["mode"], "confirmation_required")
            with self.assertRaises(AgentToolError) as limited:
                agent.prepare(
                    "library.trigger_patrol_now",
                    {},
                    owner="owner-patrol-third",
                    rate_identity=identity,
                )
            self.assertEqual(limited.exception.code, "rate_limited")
        finally:
            agent_rate_limiter.reset()

    def test_rss_unknown_submission_is_preserved_in_action_history(self) -> None:
        _sid, first, _second = self._rss()
        runtime = {
            "url": "http://qb.private.invalid",
            "username": "admin",
            "password": "PRIVATE-PASSWORD",
            "api_key": "PRIVATE-API-KEY",
            "default_method": "qb",
            "default_save_path": "/private/default",
            "category": "media",
            "timeout": 10,
        }
        with patch(
            "app.modules.rss.capture_rss_qb_runtime_config",
            return_value=(runtime, ""),
        ), patch(
            "app.modules.rss.RSSEngine.submit_pending_qb_snapshot",
            return_value={
                "requested": 1, "claimed": 1, "submitted": 0,
                "failed": 1, "outcome_unknown": 1,
            },
        ):
            prepared = get_agent_service().prepare(
                "rss.submit_entries_to_qb",
                {"entry_numbers": [first]},
                owner="owner-history",
            )
            confirmed = get_agent_service().confirm(
                prepared["action_plan"]["plan_id"], owner="owner-history"
            )
        self.assertEqual(confirmed["result"]["status"], "review_required")
        item = list_action_history(
            {"limit": 10, "outcome": "all"}, ToolContext(owner="owner-history")
        ).data["items"][0]
        self.assertEqual(item["status"], "review_required")
        self.assertEqual(item["details"]["outcome_unknown"], 1)
        rendered = json.dumps(item, ensure_ascii=False)
        for secret in ("/private", "PRIVATE-PASSWORD", "PRIVATE-API-KEY", "qb.private.invalid"):
            self.assertNotIn(secret, rendered)

    def test_patrol_trigger_queues_or_reuses_without_policy_change(self) -> None:
        scheduler = AgentLibraryPatrolScheduler()
        with patch.object(scheduler, "_enabled", return_value=True), patch.object(
            scheduler, "_now", return_value="2026-08-23 12:00:00"
        ), patch("app.modules.agent_library_patrol_scheduler.db.reschedule_agent_library_patrol", return_value=True) as reschedule, patch(
            "app.modules.agent_library_patrol_scheduler.db.get_agent_library_patrol",
            return_value={"status": "pending"},
        ):
            outcome = scheduler.trigger_now()
        self.assertEqual(outcome["status"], "queued")
        reschedule.assert_called_once_with(next_run_at="2026-08-23 12:00:00")
        self.assertTrue(scheduler._wake_event.is_set())

        with patch("app.agent.library_patrol_trigger_actions.config.get_bool", return_value=True), patch(
            "app.agent.library_patrol_trigger_actions.config.get_int", side_effect=lambda key, default: default
        ):
            preview, fingerprint = prepare_trigger_patrol_now({})
            fake = Mock()
            fake.trigger_now.return_value = {"ok": True, "status": "queued"}
            with patch("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", return_value=fake):
                confirmed = trigger_patrol_now_confirmed({}, fingerprint)
        self.assertTrue(preview.ok)
        self.assertEqual(confirmed.status, "accepted")
        self.assertTrue(confirmed.data["queued"])

    def test_natural_parsers_route_narrowly(self) -> None:
        self.assertEqual(rss_entry_summaries_request("列出 RSS 待处理条目")["status"], "pending")
        self.assertEqual(rss_entry_mark_request("把 RSS 条目 12 和 13 标记为已处理"), {"entry_numbers": [12, 13], "processed": True})
        self.assertEqual(rss_entry_submit_request("下载 RSS 条目 12 和 13 到 qB"), {"entry_numbers": [12, 13]})
        self.assertTrue(is_strm_run_history_message("看看 STRM 最近运行历史和失败上下文"))
        self.assertEqual(guangya_directory_scrape_request("检查光鸭目录 dir-1 做刮削"), ("guangya.directory_scrape.inspect", {"directory_id": "dir-1"}))
        self.assertEqual(discovery_mapping_confirmation_request("确认第 2 个映射"), {"candidate_number": 2})
        self.assertEqual(recent_discovery_candidate_request("查看刚才搜索第 1 个的 TMDB 映射候选")["action"], "mapping")
        self.assertTrue(is_patrol_trigger_now_message("按当前策略立即巡检媒体库"))
        self.assertFalse(is_patrol_trigger_now_message("修改巡检策略后立即运行"))


if __name__ == "__main__":
    import unittest
    unittest.main()
