"""研究提案经冻结确认执行器的整合：只用假云盘，实际运行规划和写保护。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from app import config
from app import database as db
from app.clients.tmdb import TMDBClient
from app.modules import episode_research_service as service
from app.modules import organize_confirmations as confirmations
from app.modules.agent_recognition_review import (
    RecognitionReviewDecision,
    review_confirmation_payload,
)
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.episode_research import EpisodeEvidenceReader, EpisodeResearchError
from app.modules.organize import OrganizeRules, organize_rules_snapshot
from app.modules.scraper import TMDBScraper
from tests.support import IsolatedDatabaseTestCase
from tests.test_episode_research_validation import (
    GROUP_ID,
    EvidenceClient,
    case_payload,
)
from tests.test_guangya_directory_scrape import _dir, _file, _MutableTreeClient


class EpisodeResearchConfirmationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
            conn.execute("DELETE FROM episode_research_cache")
            conn.execute("DELETE FROM agent_web_search_daily_usage WHERE provider='episode_research'")
        self.values = {"AGENT_ENABLED": "1", "AGENT_LLM_ENABLED": "1", "AGENT_RECOGNITION_REVIEW_ENABLED": "1",
                       "AGENT_EPISODE_RESEARCH_ENABLED": "1", "AGENT_LLM_API_URL": "https://model.invalid",
                       "AGENT_LLM_MODEL": "fixture", "AGENT_EPISODE_RESEARCH_DAILY_LIMIT": "10"}
        self.enterContext(patch.object(config, "get", side_effect=lambda k, default="": self.values.get(k, default)))
        self.enterContext(patch.object(config, "get_bool", side_effect=lambda k, default=False: str(self.values.get(k, default)).lower() in {"1", "true"}))
        for target in ("socket.socket.connect", "socket.create_connection", "requests.sessions.Session.request"):
            self.enterContext(patch(target, side_effect=AssertionError("confirmation regression forbids real network")))
        self.rules = OrganizeRules(target_dir_id="archive", small_file_mb=0, region_split=False,
                                   year_split=False, link_strm=False, notify_enabled=False, clean_empty=False)
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.payload = case_payload()
        self.payload.update(kind="guangya", source_dir_id="source", source_parent_id="source", source_name="Fixture Show",
                            directory="Fixture Show", rules=organize_rules_snapshot(self.rules.for_source("source")),
                            companions=[], _notification_suppressed=True)
        for item in self.payload["files"]:
            item.update(parent_id="source", etag="etag-"+item["file_id"])
        source = [_file(item["file_id"], item["name"], "source", size=item["size"], etag=item["etag"]) for item in self.payload["files"]]
        self.cloud = _MutableTreeClient({"source": source, "archive": []}, {
            "source": _dir("source", "Fixture Show"), "archive": _dir("archive", "Media"),
            **{item.file_id: item for item in source},
        })
        self.enterContext(patch.object(confirmations, "GuangYaClient", return_value=self.cloud))
        self.enterContext(patch.object(confirmations, "_dispatch_next_queued_confirmation", return_value={"idle": True}))
        self.enterContext(patch.object(confirmations, "wake_confirmation_dispatcher", return_value=False))
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.enterContext(patch("app.modules.organize.Organizer.trigger_post_actions"))
        self.enterContext(patch.object(service, "EpisodeEvidenceReader", side_effect=self.reader))
        service._flights.clear()
        self.addCleanup(service._flights.clear)

    @staticmethod
    def reader(case):
        return EpisodeEvidenceReader(case, client=EvidenceClient())

    async def runner(self, payload, *, reader):
        reader.inspect_candidate(0); reader.list_groups(0); reader.read_group(0, GROUP_ID)
        return {"status": "verified", "proposal": reader.validate(0, GROUP_ID), "reason_code": "episode_group_proven",
                "model": "scripted", "tool_calls": 5, "duration_ms": 1}

    def decision(self):
        result = service.research_confirmation_episodes(self.payload, runner=self.runner, reader_factory=self.reader)
        self.assertEqual(result["status"], "verified", result)
        return RecognitionReviewDecision(status="approved", candidate_index=0, confidence=1.0,
                                         reason_code="episode_group_proven", entry_mode="episode_research",
                                         episode_research_receipt=result["receipt"])

    def create(self, decision):
        db.create_organize_confirmation(token="episode-case", fingerprint=confirmations._fingerprint(self.payload),
                                       chat_id="owner-chat", source_name="Fixture Show", directory_path="Fixture Show",
                                       payload=self.payload, expires_at=(datetime.now(timezone.utc).astimezone()+timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                                       review_requested=True, review_ready=True)
        row = db.claim_next_organize_confirmation_review()
        assert (row) is not None
        with patch("app.modules.agent_recognition_review.review_confirmation_payload", return_value=decision):
            return confirmations._process_recognition_review_row(row)

    def scraper(self):
        class Client(EvidenceClient):
            api_key = "fixture"
            base_url = "https://tmdb.invalid/3"
            def get(self, path, params=None, **kwargs): return super().get(path, **kwargs)
            def detail(self, identity, media_type): return self.get(f"/{media_type}/{identity}")
            def tv_season_detail(self, identity, season): return self.get(f"/tv/{identity}/season/{season}")
        client = Client()
        client.data["/tv/100"].update(original_name="Fixture Show", genres=[{"id": 16, "name": "Animation"}], origin_country=["CN"], credits={"cast": [], "crew": []})
        scraper = TMDBScraper(client=cast(TMDBClient, client))
        self.addCleanup(scraper.close)
        return scraper

    def execute(self):
        row = db.claim_queued_organize_confirmation("episode-case")
        assert (row) is not None
        payload = confirmations._decode_row(row)
        with patch.object(confirmations, "TMDBScraper", return_value=self.scraper()):
            return confirmations._execute_guangya_confirmation("episode-case", payload, payload["candidates"][0],
                                                               selected_index=0, chat_id="owner-chat", actor="agent")

    def test_verified_research_runs_existing_frozen_executor_not_old_numbering_rule(self):
        self.assertEqual(self.create(self.decision()), "approved")
        get_organize_confirmation_result = db.get_organize_confirmation("episode-case")
        assert get_organize_confirmation_result is not None
        self.assertEqual(get_organize_confirmation_result["status"], "queued")
        result = self.execute()
        self.assertEqual(result["stats"]["moved"], 4)
        for number, item in enumerate(self.payload["files"], 1):
            current = self.cloud.infos[item["file_id"]]
            self.assertIn(f"S00E{number:02d}", current.name)
            self.assertEqual(self.cloud.infos[current.parent_id].name, "Specials")
        self.assertEqual(self.cloud.deleted, [])
        row = db.get_organize_confirmation("episode-case")
        assert row is not None
        self.assertEqual((row["status"], row["confirmation_actor"]), ("completed", "agent"))
        self.assertIn("episode_research_receipt", json.loads(row["review_result_json"]))

    def test_explicit_approval_executes_frozen_mapping_with_proactive_review_off_without_human_learning(self):
        self.payload["review_only"] = True
        self.assertEqual(self.create(self.decision()), "approved")
        self.values["AGENT_RECOGNITION_REVIEW_ENABLED"] = "0"
        with self.assertRaises(EpisodeResearchError):
            confirmations.start_confirmation("episode-case", 0, chat_id="owner-chat", actor="agent", dispatch=False)
        queued = confirmations.start_confirmation("episode-case", 0, chat_id="owner-chat", actor="agent",
            explicit_approval=True, dispatch=False)
        self.assertEqual(queued["status"], "queued")
        with patch.object(confirmations, "_record_confirmation_learning") as learning:
            result = self.execute()
            learning.assert_not_called()
        self.assertEqual(result["stats"]["moved"], 4)
        for number, item in enumerate(self.payload["files"], 1):
            current = self.cloud.infos[item["file_id"]]
            self.assertIn(f"S00E{number:02d}", current.name)
            self.assertEqual(self.cloud.infos[current.parent_id].name, "Specials")
        row = db.get_organize_confirmation("episode-case")
        assert row is not None
        self.assertEqual((row["status"], row["confirmation_actor"]), ("completed", "agent"))
        self.assertIs(json.loads(row["payload_json"])["_explicit_approval"], True)
        self.assertEqual(self.cloud.deleted, [])

    def test_switch_off_before_queue_or_before_write_cannot_move_files(self):
        decision = self.decision()
        self.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "0"
        self.assertNotEqual(self.create(decision), "approved")
        get_organize_confirmation_result_2 = db.get_organize_confirmation("episode-case")
        assert get_organize_confirmation_result_2 is not None
        self.assertEqual(get_organize_confirmation_result_2["status"], "pending")
        self.assertEqual(self.cloud.deleted, [])
        self.assertTrue(all(self.cloud.infos[item["file_id"]].parent_id == "source" for item in self.payload["files"]))

    def test_switch_off_after_queue_and_source_snapshot_changes_fail_before_any_media_write(self):
        for fault in ("disabled", "changed"):
            with self.subTest(fault=fault):
                # 独立票据，无重试旧运行状态。
                with db.get_conn() as conn: conn.execute("DELETE FROM organize_confirmations")
                self.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "1"
                self.assertEqual(self.create(self.decision()), "approved")
                before = copy.deepcopy(self.cloud.tree)
                if fault == "disabled": self.values["AGENT_EPISODE_RESEARCH_ENABLED"] = "0"
                else:
                    original = self.cloud.infos[self.payload["files"][0]["file_id"]]
                    self.cloud.infos[original.file_id] = _file(original.file_id, "Changed.S01E11.mkv", "source", size=original.size)
                with self.assertRaises((EpisodeResearchError, DirectoryScrapeConflictError)):
                    self.execute()
                self.assertEqual(self.cloud.tree, before)
                self.assertEqual(self.cloud.deleted, [])

    def test_human_candidate_selection_does_not_implicitly_apply_agent_mapping(self):
        self.assertIsNone(confirmations._episode_research_for_execution("missing", self.payload, 0, "human"))

    def test_boundary_rejects_overwrite_wrong_position_and_orphan_companions(self):
        proposal = service.research_confirmation_episodes(self.payload, runner=self.runner, reader_factory=self.reader)["proposal"]
        boundary = confirmations._AgentEpisodeWriteBoundary(self.payload, proposal, client=self.cloud)
        plan = SimpleNamespace(file_id=self.payload["files"][0]["file_id"], source_season=1, source_episode=11,
                               season=0, episode=1, action="move", conflict_decision="new", new_name="Fixture.Show.S00E01.mkv")
        boundary(plan, "prepare")
        for kind in ("replace", "wrong_target", "orphan_subtitle"):
            with self.subTest(kind=kind):
                bad = copy.copy(plan); targets = ()
                if kind == "replace": bad.action = "replace"
                if kind == "wrong_target": bad.episode = 2
                if kind == "orphan_subtitle": targets = (_file("unrelated", "Fixture.Show.S00E01.zh.srt", "target"),)
                with self.assertRaises(DirectoryScrapeConflictError): boundary(bad, "commit", target_files=targets)
        self.assertFalse(boundary.media_write_attempted)

    def test_known_position_failure_uses_research_without_pointless_old_model_review(self):
        result = service.research_confirmation_episodes(self.payload, runner=self.runner, reader_factory=self.reader)
        with patch("app.modules.agent_recognition_review._review_async", side_effect=AssertionError("old review cannot alter numbering")), \
             patch.object(service, "research_confirmation_episodes", return_value=result):
            decision = review_confirmation_payload(self.payload)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.entry_mode, "episode_research")
        self.assertEqual(decision.episode_research_receipt, result["receipt"])

    def test_multiple_candidates_keep_existing_standard_review_before_research(self):
        from unittest.mock import AsyncMock
        payload = copy.deepcopy(self.payload)
        payload["candidates"].append({"tmdb_id": "200", "title": "Fixture Show", "year": "2022", "media_type": "tv"})
        decision = RecognitionReviewDecision(status="approved", candidate_index=1, confidence=0.97)
        with patch("app.modules.agent_recognition_review._review_async", new=AsyncMock(return_value=decision)) as old, \
             patch.object(service, "research_confirmation_episodes", side_effect=AssertionError("standard review already resolved identity")):
            result = review_confirmation_payload(payload)
        self.assertIs(result, decision)
        old.assert_awaited_once()
