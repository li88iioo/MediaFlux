from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import patch

from app import config
from app import database as db
from app.repositories import agent_web_search as agent_web_search_repo
from app.modules import episode_research_service as service
from app.modules.episode_research import EpisodeEvidenceReader, EpisodeResearchError
from app.repositories.episode_research import invalidate_episode_research_cache
from tests.support import IsolatedDatabaseTestCase
from tests.test_episode_research_validation import (
    GROUP_ID,
    EvidenceClient,
    case_payload,
)


class EpisodeResearchServiceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM episode_research_cache")
            conn.execute("DELETE FROM agent_web_search_daily_usage WHERE provider='episode_research'")
        self.values = {
            "AGENT_ENABLED": "1", "AGENT_LLM_ENABLED": "1", "AGENT_RECOGNITION_REVIEW_ENABLED": "1",
            "AGENT_EPISODE_RESEARCH_ENABLED": "1", "AGENT_LLM_API_URL": "https://model.invalid",
            "AGENT_LLM_MODEL": "fixture", "AGENT_EPISODE_RESEARCH_DAILY_LIMIT": "10",
        }
        self.enterContext(patch.object(config, "get", side_effect=lambda k, default="": self.values.get(k, default)))
        self.enterContext(patch.object(config, "get_bool", side_effect=lambda k, default=False: str(self.values.get(k, default)).lower() in {"1", "true"}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("service tests forbid network")))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("service tests forbid HTTP")))
        self.payload = case_payload()
        self.calls = 0
        self.clients = []
        self.modify_client = None
        service._flights.clear()
        self.addCleanup(service._flights.clear)

    def reader(self, case):
        client = EvidenceClient()
        if self.modify_client:
            self.modify_client(client)
        self.clients.append(client)
        return EpisodeEvidenceReader(case, client=client)

    async def runner(self, payload, *, reader):
        self.calls += 1
        reader.inspect_candidate(0); reader.list_groups(0); reader.read_group(0, GROUP_ID)
        proposal = reader.validate(0, GROUP_ID)
        return {"status": "verified", "proposal": proposal, "reason_code": "episode_group_proven",
                "tool_calls": 5, "duration_ms": 10, "model": "fixture"}

    def research(self, payload=None, runner=None):
        return service.research_confirmation_episodes(payload or self.payload, runner=runner or self.runner, reader_factory=self.reader)

    def test_verified_cache_reuses_research_but_not_current_metadata_proof(self):
        first = self.research()
        self.assertEqual(first["status"], "verified")
        second = self.research()
        self.assertEqual(second["status"], "verified")
        self.assertTrue(second["cached"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all("/tv/100/season/0" in client.requests for client in self.clients))
        self.assertEqual(first["receipt"], second["receipt"])

    def test_explicit_approval_revalidates_when_only_proactive_review_is_disabled(self):
        result = self.research()
        self.assertEqual(result["status"], "verified")
        self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
        self.assertFalse(service.episode_research_enabled())
        before = len(self.clients)
        with self.assertRaises(EpisodeResearchError) as raised:
            service.revalidate_episode_research_receipt(self.payload, result["receipt"], reader_factory=self.reader)
        self.assertEqual(raised.exception.code, "episode_research_disabled")
        self.assertEqual(len(self.clients), before)
        current = service.revalidate_episode_research_receipt(self.payload, result["receipt"],
            expected_candidate_index=0, explicit_approval=True, reader_factory=self.reader)
        self.assertEqual(current["mappings"], result["proposal"]["mappings"])
        self.assertEqual(len(self.clients), before + 1)
        self.assertIn("/tv/100/season/0", self.clients[-1].requests)
        self.assertEqual(self.research()["reason_code"], "episode_research_disabled")
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.clients), before + 1)

    def test_explicit_approval_keeps_complex_research_and_runtime_guards(self):
        result = self.research()
        self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
        before = len(self.clients)
        for key, value in (("AGENT_EPISODE_RESEARCH_ENABLED", "0"), ("AGENT_ENABLED", "0"),
                           ("AGENT_LLM_ENABLED", "0"), ("AGENT_LLM_API_URL", ""), ("AGENT_LLM_MODEL", "")):
            with self.subTest(key=key), patch.dict(self.values, {key: value}):
                self.assertFalse(service.episode_research_enabled(explicit_approval=True))
                with self.assertRaises(EpisodeResearchError) as raised:
                    service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                        explicit_approval=True, reader_factory=self.reader)
                self.assertEqual(raised.exception.code, "episode_research_disabled")
                self.assertEqual(len(self.clients), before)

    def test_only_boolean_explicit_approval_can_replace_proactive_review(self):
        result = self.research()
        self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
        before = len(self.clients)
        for value in (False, None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(EpisodeResearchError) as raised:
                service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                    explicit_approval=cast(Any, value), reader_factory=self.reader)
            self.assertEqual(raised.exception.code, "episode_research_disabled")
        self.assertEqual(len(self.clients), before)

    def test_cache_tampering_scope_changes_or_expiry_never_authorize_mapping(self):
        for explicit in (False, True):
            self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "1"
            result = self.research()
            self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0" if explicit else "1"
            for change in ("receipt", "source", "candidate", "scope", "expiry"):
                with self.subTest(explicit_approval=explicit, change=change):
                    payload, receipt = copy.deepcopy(self.payload), copy.deepcopy(result["receipt"])
                    if change == "receipt": receipt["group_fingerprint"] = "0"*64
                    if change == "source": payload["files"][0]["name"] = "Other.Show.S01E11.mkv"
                    if change == "candidate": receipt["candidate_index"] = 1
                    if change == "scope": payload["kind"] = "local_media"
                    if change == "expiry": invalidate_episode_research_cache(receipt["cache_key"])
                    before = len(self.clients)
                    with self.assertRaises(EpisodeResearchError):
                        service.revalidate_episode_research_receipt(payload, receipt, expected_candidate_index=0,
                            explicit_approval=explicit, reader_factory=self.reader)
                    self.assertEqual(len(self.clients), before)

    def test_changed_stable_episode_id_blocks_old_receipt(self):
        for explicit in (False, True):
            with self.subTest(explicit_approval=explicit):
                self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "1"
                self.modify_client = None
                result = self.research()
                self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0" if explicit else "1"
                self.modify_client = lambda client: client.data["/tv/100/season/0"]["episodes"][0].update(id=9999)
                with self.assertRaises(EpisodeResearchError):
                    service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                        explicit_approval=explicit, reader_factory=self.reader)

    def test_reordered_group_requires_new_research_instead_of_silent_cache_rewrite(self):
        def change(client):
            group = client.data[f"/tv/episode_group/{GROUP_ID}"]["groups"][0]["episodes"]
            group[-4]["episode_number"], group[-1]["episode_number"] = 4, 1
            group[-4]["id"], group[-1]["id"] = 1014, 1011
        for explicit in (False, True):
            with self.subTest(explicit_approval=explicit):
                self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "1"
                self.modify_client = None
                result = self.research()
                self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0" if explicit else "1"
                self.modify_client = change
                with self.assertRaises(EpisodeResearchError) as raised:
                    service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                        explicit_approval=explicit, reader_factory=self.reader)
                self.assertEqual(raised.exception.code, "research_evidence_changed")

    def test_explicit_approval_rechecks_runtime_guards_after_metadata_validation(self):
        result = self.research()
        original = EpisodeEvidenceReader.validate
        for key in ("AGENT_RECOGNITION_REVIEW_ENABLED", "AGENT_EPISODE_RESEARCH_ENABLED", "AGENT_ENABLED"):
            with self.subTest(key=key), patch.dict(self.values):
                def validate_then_disable(reader, index, group_id, *, key=key):
                    current = original(reader, index, group_id)
                    self.values[key] = "0"
                    return current
                before = len(self.clients)
                with patch.object(EpisodeEvidenceReader, "validate", new=validate_then_disable):
                    if key == "AGENT_RECOGNITION_REVIEW_ENABLED":
                        current = service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                            explicit_approval=True, reader_factory=self.reader)
                        self.assertEqual(current["mappings"], result["proposal"]["mappings"])
                    else:
                        with self.assertRaises(EpisodeResearchError) as raised:
                            service.revalidate_episode_research_receipt(self.payload, result["receipt"],
                                explicit_approval=True, reader_factory=self.reader)
                        self.assertEqual(raised.exception.code, "episode_research_disabled")
                self.assertEqual(len(self.clients), before + 1)
                self.assertIn("/tv/100/season/0", self.clients[-1].requests)

    def test_disabled_and_local_media_paths_do_not_call_model_or_tmdb(self):
        for kind, enabled in (("guangya", "0"), ("local_media", "1")):
            with self.subTest(kind=kind):
                self.values["AGENT_EPISODE_RESEARCH_ENABLED"] = enabled
                payload = {**self.payload, "kind": kind}
                result = self.research(payload)
                self.assertNotEqual(result["status"], "verified")
        self.assertEqual((self.calls, len(self.clients)), (0, 0))

    def test_disabled_during_research_never_publishes_approval(self):
        async def runner(payload, *, reader):
            result = await self.runner(payload, reader=reader)
            self.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "0"
            return result
        result = self.research(runner=runner)
        self.assertEqual(result["reason_code"], "episode_research_disabled")
        self.assertIsNone(result["proposal"])

    def test_daily_case_budget_is_atomic_and_not_refunded_after_provider_failure(self):
        self.values["AGENT_EPISODE_RESEARCH_DAILY_LIMIT"] = "1"
        async def broken(payload, *, reader):
            self.calls += 1
            raise RuntimeError("provider failed")
        result = self.research(runner=broken)
        self.assertEqual(result["status"], "failed")
        another = copy.deepcopy(self.payload)
        another["files"][0]["size"] += 1
        self.assertEqual(self.research(another)["reason_code"], "research_daily_budget")
        self.assertEqual(self.calls, 1)
        self.assertEqual(agent_web_search_repo.get_agent_web_search_daily_usage(provider="episode_research", usage_date=agent_web_search_repo.current_agent_web_search_usage_date()), 1)

    def test_abstention_is_cached_without_model_free_text(self):
        async def abstain(payload, *, reader):
            self.calls += 1
            return {"status": "abstained", "reason_code": "ambiguous_episode_groups", "summary": "private context"}
        self.assertEqual(self.research(runner=abstain)["status"], "abstained")
        again = self.research(runner=abstain)
        self.assertTrue(again["cached"])
        self.assertEqual(self.calls, 1)
        with db.get_conn() as conn:
            stored = conn.execute("SELECT payload FROM episode_research_cache").fetchone()[0]
        self.assertNotIn("private context", stored)

    def test_invalid_verified_proposal_preserves_abstention_and_negative_cache(self):
        for index, proposal in enumerate((None, [], "bad")):
            with self.subTest(proposal=proposal):
                payload = copy.deepcopy(self.payload)
                payload["files"][0]["size"] += index
                calls_before = self.calls

                async def invalid_runner(payload, *, reader):
                    self.calls += 1
                    reader.close()
                    return {"status": "verified", "proposal": proposal}

                result = self.research(payload, runner=invalid_runner)
                self.assertEqual(result["status"], "abstained")
                self.assertEqual(result["reason_code"], "invalid_research_proposal")
                self.assertIsNone(result["proposal"])

                cached = self.research(payload, runner=invalid_runner)
                self.assertTrue(cached["cached"])
                self.assertEqual(cached["status"], "abstained")
                self.assertEqual(cached["reason_code"], "invalid_research_proposal")
                self.assertEqual(self.calls, calls_before + 1)

    def test_provider_failure_uses_short_negative_cache_instead_of_retry_storm(self):
        async def broken(payload, *, reader):
            self.calls += 1
            raise RuntimeError("transient provider error")
        self.assertEqual(self.research(runner=broken)["status"], "failed")
        second = self.research(runner=broken)
        self.assertTrue(second.get("cached", False))
        self.assertEqual(self.calls, 1)

    def test_concurrent_same_case_runs_model_once(self):
        entered, release = threading.Event(), threading.Event()
        async def delayed(payload, *, reader):
            entered.set()
            await asyncio.to_thread(release.wait, 4)
            return await self.runner(payload, reader=reader)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.research, None, delayed)
            self.assertTrue(entered.wait(3))
            second = executor.submit(self.research, None, delayed)
            release.set()
            results = [first.result(timeout=8), second.result(timeout=8)]
        self.assertTrue(all(result["status"] == "verified" for result in results), results)
        self.assertEqual(self.calls, 1)
        self.assertEqual(service._flights.active_count, 0)
