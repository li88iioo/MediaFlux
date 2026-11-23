"""多入口逐项批量确认的冻结范围、只读复核和一次性认领行为。"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from threading import Barrier, Event
from unittest.mock import Mock, patch

from app import database as db
from app.clients.tmdb import TMDBClient
from app.repositories import organize_confirmations as organize_confirmations_repo
from app.modules import organize_confirmation_batches as batches
from app.modules import organize_confirmations as confirmations
from app.modules import telegram_organize_lifecycle  # noqa: F401 -- 在测试替换通知函数前加载其正式导入绑定。
from app.modules.agent_recognition_review import RecognitionReviewDecision
from app.modules.organize import OrganizeRules, organize_rules_snapshot
from tests.support import IsolatedDatabaseTestCase
from app.agent import feature_gate


class ConfirmationBatchTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
        self.rules = OrganizeRules(target_dir_id="target")
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.enterContext(patch.object(OrganizeRules, "for_source", return_value=self.rules))
        self.enterContext(patch.object(confirmations, "wake_confirmation_dispatcher"))
        self.enterContext(patch.object(confirmations, "wake_recognition_review_dispatcher"))
        self.enterContext(patch.object(confirmations, "_dispatch_due_confirmation_delivery"))

    def row(self, token: str):
        row = organize_confirmations_repo.get_organize_confirmation(token)
        assert row is not None
        return row

    def log(self, log_id: int) -> sqlite3.Row:
        row = db.get_organize_log(log_id)
        assert row is not None
        return row

    def create(self, token: str, *, batch="", chat="chat", review=False):
        payload: dict = {
            "identity": f"作品{token}", "directory": f"/incoming/{token}",
            "reason": "同一番号存在相互冲突的元数据", "source_name": "来源A",
            "source_dir_id": "source", "source_parent_id": "parent",
            "files": [{"file_id": token, "name": f"{token}.mkv", "parent_id": "parent", "size": 100000000}],
            "candidates": [{"title": f"候选{i}-{token}", "year": str(2020 + i),
                            "tmdb_id": str(100 + i), "provider": "tmdb", "media_type": "movie"}
                           for i in range(2)],
            "rules": organize_rules_snapshot(self.rules),
        }
        if batch:
            payload.update(_confirmation_batch=batch, review_only=True)
        organize_confirmations_repo.create_organize_confirmation(
            token=token, fingerprint=confirmations._fingerprint(payload), chat_id=chat,
            source_name="来源A", directory_path=payload["directory"], payload=payload,
            expires_at=(datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            organize_task_id="task1", review_requested=review, review_ready=review,
        )
        return payload

    def test_cross_page_selection_is_frozen_and_two_works_use_distinct_choices(self):
        for index in range(25):
            self.create(str(index))
        page = batches.list_confirmations(page_size=20)
        self.assertEqual((page["total"], len(page["items"]), len(page["confirmation_ids"])), (25, 20, 25))
        frozen = page["confirmation_ids"]
        self.create("new")
        self.assertEqual(batches.list_confirmations(confirmation_ids=frozen)["total"], 25)
        entries = [{"confirmation_id": "0", "candidate_index": 0}, {"confirmation_id": "24", "candidate_index": 1}]
        preview = batches.preview_choices(entries)
        self.assertNotEqual(preview["items"][0]["candidate"]["title"], preview["items"][1]["candidate"]["title"])
        self.assertEqual(self.row("0")["status"], "pending")
        result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (2, 0))
        self.assertEqual(self.row("24")["selected_index"], 1)
        queued_at = self.row("0")["queued_at"]
        self.assertEqual(batches.submit_choices(entries, preview["digest"])["accepted"], 2)
        self.assertEqual(self.row("0")["queued_at"], queued_at)

    def test_manual_draft_survives_late_model_result_without_executing(self):
        self.create("one", review=True)
        organize_confirmations_repo.claim_next_organize_confirmation_review()
        batches.save_selection("one", 1)
        self.assertFalse(organize_confirmations_repo.complete_organize_confirmation_review(
            "one", status="approved", result={"candidate_index": 0},
        ))
        view = batches.list_confirmations()["items"][0]
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["suggestion"]["candidate_index"], 1)
        self.assertEqual(view["suggestion"]["source"], "human")

    def test_selection_limit_does_not_truncate_total_or_pages(self):
        self.create("seed")
        # 复制持久记录以验证真实分页；避免为同一场景重复1000次建票事务。
        row = dict(self.row("seed"))
        row.pop("id")
        columns = list(row)
        with db.get_conn() as conn:
            conn.executemany(
                f"INSERT INTO organize_confirmations ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [tuple({**row, "token": f"item-{index}", "fingerprint": f"fp-{index}"}[key]
                       for key in columns) for index in range(1000)],
            )
        result = batches.list_confirmations(page=11, page_size=100)
        self.assertEqual((result["total"], result["pages"], result["counts"]), (1001, 11, {"pending": 1001}))
        self.assertEqual([item["confirmation_id"] for item in result["items"]], ["seed"])
        self.assertEqual(len(result["confirmation_ids"]), 1000)
        self.assertTrue(result["selection_truncated"])
        self.assertEqual(batches.list_confirmations(page=12, page_size=100)["items"], [])

    def test_expired_projection_matches_filters_and_counts(self):
        self.create("one")
        self.create("two")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01' WHERE token='one'")
        self.assertEqual(batches.list_confirmations(status="all")["counts"], {"pending": 1, "expired": 1})
        self.assertEqual([item["confirmation_id"] for item in batches.list_confirmations(status="expired")["items"]], ["one"])

    def test_rescan_keeps_superseded_group_visible_in_original_batch_progress(self):
        payload = self.create("old", batch="old-batch")
        self.create("other", batch="old-batch")
        payload["_confirmation_batch"] = "new-batch"
        organize_confirmations_repo.create_organize_confirmation(
            token="new", fingerprint=confirmations._fingerprint(payload), chat_id="chat",
            source_name="来源A", directory_path=payload["directory"], payload=payload,
            expires_at=self.row("old")["expires_at"],
        )
        old_batch = batches.list_confirmations(batch_id="old-batch", status="all")
        self.assertEqual(old_batch["counts"], {"expired": 1, "pending": 1})
        self.assertEqual(old_batch["total"], 2)
        self.assertEqual(batches.list_confirmations(status="all")["total"], 2)

    def test_retry_choice_is_rejected_before_saving_an_unexecutable_suggestion(self):
        payload = self.create("retry")
        payload["_retry_selected_index"] = 0
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='retry'", (json.dumps(payload),))
        with self.assertRaisesRegex(ValueError, "绑定其他候选"):
            batches.save_selection("retry", 1)
        self.assertIsNone(batches.list_confirmations()["items"][0]["suggestion"])
        batches.save_selection("retry", 0)
        self.assertEqual(batches.list_confirmations()["items"][0]["suggestion"]["candidate_index"], 0)

    def test_bulk_review_records_suggestion_but_does_not_claim_files(self):
        self.create("one", review=True, batch="b")
        row = organize_confirmations_repo.claim_next_organize_confirmation_review()
        decision = RecognitionReviewDecision(status="approved", candidate_index=1, confidence=.99,
                                             reason_code="evidence_consistent", summary="核对年份一致")
        with patch.object(feature_gate, "recognition_review_enabled", return_value=True), \
                patch("app.modules.agent_recognition_review.review_confirmation_payload", return_value=decision), \
                patch("app.modules.telegram_confirmation_batch.publish_batch_summary"), \
                patch.object(confirmations, "start_confirmation") as start:
            self.assertEqual(confirmations._process_recognition_review_row(row), "approved")
        start.assert_not_called()
        self.assertEqual(self.row("one")["status"], "pending")
        self.assertEqual(batches.list_confirmations()["items"][0]["suggestion"]["candidate_index"], 1)

    @staticmethod
    def _unmatched_series(title: str, count: int = 1) -> dict:
        return {
            "identity": title, "directory": f"/incoming/{title}", "reason": "暂无可用元数据",
            "source_dir_id": "source", "source_parent_id": "parent", "candidates": [],
            "files": [{
                "file_id": f"{title}-{episode}", "name": f"{title}.S01E{episode:02}.mkv",
                "parent_id": "parent", "size": 100000000, "etag": f"etag-{episode}",
                "season": 1, "episode": episode,
            } for episode in range(1, count + 1)],
        }

    def _schedule_unmatched_group(self, group: dict, task_id: str) -> str:
        self.assertEqual(confirmations.schedule_guangya_recognition_reviews(
            {"task_id": task_id, "confirmation_groups": [group]}, self.rules, chat_id="chat",
        ), 1)
        [token] = batches.list_confirmations(task_id=task_id)["confirmation_ids"]
        return token

    @staticmethod
    def _discovered_series(title: str) -> RecognitionReviewDecision:
        return RecognitionReviewDecision(
            status="approved", candidate_index=0, confidence=.99,
            reason_code="evidence_consistent", summary="已核对作品和季集",
            discovered_candidates=[{
                "title": title, "year": "2024", "media_type": "tv",
                "provider": "tmdb", "tmdb_id": "7654", "external_id": "7654",
            }],
        )

    def test_proactive_single_and_batch_notifications_do_not_queue_empty_groups(self):
        known = self._unmatched_series("known")
        known["candidates"] = [{"title": "已识别剧集", "provider": "tmdb", "tmdb_id": "7653", "media_type": "tv"}]
        scenarios = {
            "single": [self._unmatched_series("single")],
            "empty-batch": [self._unmatched_series(f"empty-{index}") for index in range(2)],
            "mixed-batch": [self._unmatched_series("mixed-empty"), known],
        }
        with (
            patch.object(feature_gate, "recognition_review_enabled", return_value=True),
            patch.object(batches, "request_reviews", wraps=batches.request_reviews) as manual_request,
            patch("app.modules.telegram_notification_center.publish_notification_thread", return_value=True) as single,
            patch("app.modules.telegram_confirmation_batch.publish_batch_summary", return_value=True) as multiple,
        ):
            for task_id, groups in scenarios.items():
                self.assertTrue(confirmations.Organizer._deliver_task_confirmation_groups(
                    groups, self.rules, chat_id="chat", organize_task_id=task_id,
                ))
                items = batches.list_confirmations(task_id=task_id)["items"]
                self.assertEqual(len(items), len(groups))
                for item in items:
                    with self.subTest(task=task_id, group=item["identity"]):
                        has_candidates = item["identity"] == known["identity"]
                        self.assertEqual(item["status"], "pending")
                        self.assertEqual(item["review_status"], "pending" if has_candidates else "")
                        self.assertIs(item["can_discover_candidates"], not has_candidates)
                        payload = confirmations.decode_row(self.row(item["confirmation_id"]))
                        self.assertFalse(payload.get("discover_candidates"))
        single.assert_called_once()
        self.assertEqual(multiple.call_count, 2)
        manual_request.assert_not_called()
        claimed = organize_confirmations_repo.claim_next_organize_confirmation_review()
        assert claimed is not None
        self.assertEqual(confirmations.decode_row(claimed)["identity"], known["identity"])
        self.assertIsNone(organize_confirmations_repo.claim_next_organize_confirmation_review())

    def test_manual_discovery_saves_suggestion_and_requires_confirmation_for_an_eight_episode_group(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import logs_api

        app = FastAPI()
        app.include_router(logs_api.router)
        self.enterContext(patch.object(logs_api, "require_api_login"))
        self.enterContext(patch.object(feature_gate, "recognition_review_enabled", return_value=True))
        client = self.enterContext(TestClient(app))
        endpoint = "/api/logs/organize/confirmations"
        group = self._unmatched_series("有人刚好提起我吗", count=8)
        token = self._schedule_unmatched_group(group, "one-series")
        ordinary = self.create("ordinary")
        exclusive = self._unmatched_series("exclusive")
        exclusive["rules"] = {"nsfw_exclusive": True}
        excluded = self._schedule_unmatched_group(exclusive, "exclusive")
        selection = {"confirmation_ids": [token, "ordinary", excluded]}
        before = client.post(f"{endpoint}/query", json=selection)
        self.assertEqual(before.status_code, 200, before.text)
        views = {item["confirmation_id"]: item for item in before.json()["items"]}
        self.assertEqual({key: item["can_discover_candidates"] for key, item in views.items()},
                         {token: True, "ordinary": False, excluded: False})
        self.assertIsNone(organize_confirmations_repo.claim_next_organize_confirmation_review())

        entries = [{"confirmation_id": token, "candidate_index": 0}]
        self.assertEqual(client.post(f"{endpoint}/preview", json={"entries": entries}).status_code, 400)
        requested = client.post(f"{endpoint}/review", json=selection)
        self.assertEqual(requested.status_code, 200, requested.text)
        self.assertEqual(requested.json(), {"requested": 2, "unchanged": 0, "unavailable": 1})
        discovered_request = confirmations.decode_row(self.row(token))
        self.assertIs(discovered_request["review_only"], True)
        self.assertIs(discovered_request["discover_candidates"], True)
        self.assertFalse(confirmations.decode_row(self.row("ordinary")).get("discover_candidates"))
        self.assertEqual(self.row(excluded)["review_status"], "")

        decision = self._discovered_series(group["identity"])
        existing_decision = RecognitionReviewDecision(status="approved", candidate_index=1, confidence=.99)
        with patch("app.modules.agent_recognition_review.review_confirmation_payload",
                   side_effect=[decision, existing_decision]), \
                patch.object(confirmations, "start_confirmation") as start:
            for expected in (token, "ordinary"):
                claimed = organize_confirmations_repo.claim_next_organize_confirmation_review()
                assert claimed is not None
                self.assertEqual(claimed["token"], expected)
                self.assertEqual(confirmations._process_recognition_review_row(claimed), "approved")
        start.assert_not_called()
        self.assertIsNone(organize_confirmations_repo.claim_next_organize_confirmation_review())
        self.assertEqual(confirmations.decode_row(self.row("ordinary"))["candidates"], ordinary["candidates"])

        stored = self.row(token)
        payload = confirmations.decode_row(stored)
        self.assertEqual(payload["candidates"], decision.discovered_candidates)
        self.assertEqual(payload["files"], group["files"])
        self.assertEqual((stored["status"], stored["review_status"], stored["selected_index"]),
                         ("pending", "approved", None))
        audit = json.loads(stored["review_result_json"])
        self.assertEqual(audit["candidate_index"], 0)
        self.assertNotIn("discovered_candidates", audit)
        response = client.post(f"{endpoint}/query", json={"confirmation_ids": [token]})
        self.assertEqual(response.status_code, 200, response.text)
        [view] = response.json()["items"]
        self.assertFalse(view["can_discover_candidates"])
        self.assertEqual(view["suggestion"]["source"], "agent")
        self.assertEqual(view["candidates"][view["suggestion"]["candidate_index"]]["tmdb_id"], "7654")
        with self.assertRaises(ValueError):
            confirmations.start_confirmation(token, 0, chat_id="chat", actor="agent", dispatch=False)
        self.assertEqual(client.post(f"{endpoint}/submit", json={"entries": entries}).status_code, 400)

        preview = client.post(f"{endpoint}/preview", json={"entries": entries})
        self.assertEqual(preview.status_code, 200, preview.text)
        [item] = preview.json()["items"]
        self.assertEqual([(file["season"], file["episode"]) for file in item["files"]],
                         [(1, episode) for episode in range(1, 9)])
        self.assertIn("Season 1", item["target"])
        self.assertEqual(self.row(token)["status"], "pending")
        accepted = client.post(f"{endpoint}/submit", json={"entries": entries, "digest": preview.json()["digest"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual((accepted.json()["accepted"], accepted.json()["failed"]), (1, 0))
        queued = self.row(token)
        self.assertEqual((queued["status"], queued["confirmation_actor"], queued["selected_index"]), ("queued", "human", 0))
        self.assertEqual(confirmations.decode_row(queued)["files"], group["files"])
        self.assertEqual(self.row("ordinary")["status"], "pending")

    def test_legacy_eight_card_batch_shares_search_and_checks_episodes_individually(self):
        self.enterContext(patch.object(feature_gate, "recognition_review_enabled", return_value=True))
        title, original_title = "有人刚好提起我吗？", "Did Someone Happen to Mention Me"
        series = self._unmatched_series(title, count=8)
        directory = (
            f"/incoming/【高清剧集网发布 www.DDHDTV.com】{title}"
            "[杜比视界版本][全8集][简繁英字幕].2160p.NF.WEB-DL.DDP.5.1.DV.H.265-BlackTV"
        )
        missing_episode = 8
        files = series["files"]
        groups = []
        # 先处理缺集卡，后续七张卡仍须在相同缓存上独立核验成功。
        for file in [files[-1], *files[:-1]]:
            episode = file["episode"]
            subtitle = {1: "Becomes.a.Nobody", missing_episode: "is.Reborn"}.get(episode, f"Episode.{episode:02}")
            name = (
                f"{original_title.replace(' ', '.')}.S01E{episode:02}.Act.{episode}.A.Renowned.Actor.{subtitle}"
                ".2160p.NF.WEB-DL.DDP.5.1.DV.H.265-BlackTV.mkv"
            )
            groups.append({**series, "identity": f"Act {episode}", "directory": directory,
                           "files": [{**file, "name": name}]})
        task_id = "legacy-eight-cards"
        self.assertEqual(confirmations.schedule_guangya_recognition_reviews(
            {"task_id": task_id, "confirmation_groups": groups}, self.rules, chat_id="chat",
        ), 8)
        batch = batches.list_confirmations(task_id=task_id)
        [batch_id] = {item["batch_id"] for item in batch["items"]}
        self.assertTrue(batch_id)
        self.assertEqual(batches.request_reviews(batch["confirmation_ids"]),
                         {"requested": 8, "unchanged": 0, "unavailable": 0})

        available = set(range(1, 9)) - {missing_episode}
        detail = {
            "id": 7654, "name": title, "original_name": original_title, "first_air_date": "2024-05-03",
        }
        tmdb = Mock(spec=TMDBClient, api_key="fixture-key", config_error="")
        tmdb.search.return_value = [detail]
        tmdb.detail_with_alternative_titles.return_value = detail
        tmdb.tv_season_detail.return_value = {
            "season_number": 1, "episodes": [{"episode_number": number} for number in sorted(available)],
        }
        stop, wake = Event(), Event()
        remaining = len(groups)

        def skip_wait(_timeout: float) -> bool:
            nonlocal remaining
            remaining -= 1
            if remaining == 0:
                stop.set()
            return True

        with patch("app.modules.scraper.TMDBClient", return_value=tmdb), \
                patch.object(wake, "wait", side_effect=skip_wait):
            confirmations._recognition_review_loop(stop_event=stop, wakeup_event=wake)

        items = batches.list_confirmations(batch_id=batch_id)["items"]
        self.assertEqual(len(items), 8)
        approved = set()
        for item in items:
            [file] = item["files"]
            episode = file["episode"]
            with self.subTest(episode=episode):
                self.assertEqual((item["status"], item["selected_index"]), ("pending", None))
                if episode == missing_episode:
                    self.assertEqual(item["review_status"], "abstained")
                    self.assertIsNone(item["suggestion"])
                    self.assertEqual(item["candidates"], [])
                    self.assertIn("S01E08", item["review_summary"])
                    row = self.row(item["confirmation_id"])
                    audit = confirmations.decode_json_object(row["review_result_json"])
                    self.assertEqual(audit["reason_code"], "episode_boundary_unverified")
                else:
                    self.assertEqual(item["review_status"], "approved")
                    index = item["suggestion"]["candidate_index"]
                    self.assertEqual(item["candidates"][index]["tmdb_id"], "7654")
                    approved.add(episode)
        self.assertEqual(approved, available)
        tmdb.search.assert_called_once_with(original_title, "", "tv")
        tmdb.tv_season_detail.assert_called_once_with("7654", 1)

    def test_discovery_does_not_add_candidates_after_skip_expiry_supersession_or_no_match(self):
        self.enterContext(patch.object(feature_gate, "recognition_review_enabled", return_value=True))
        for state in ("skipped", "expired", "superseded", "abstained"):
            with self.subTest(state=state):
                group = self._unmatched_series(f"late-{state}")
                token = self._schedule_unmatched_group(group, state)
                self.assertEqual(batches.request_reviews([token])["requested"], 1)
                claimed = organize_confirmations_repo.claim_next_organize_confirmation_review()
                assert claimed is not None
                self.assertEqual(claimed["token"], token)
                now = db.now()
                replacement = None

                def decide(_payload):
                    nonlocal now, replacement
                    if state == "skipped":
                        self.assertEqual(batches.skip_confirmations([token])["skipped"], 1)
                    elif state == "expired":
                        now = (datetime.fromisoformat(claimed["expires_at"]) + timedelta(seconds=1)).isoformat(sep=" ")
                    elif state == "superseded":
                        replacement = self._schedule_unmatched_group(group, "replacement")
                    else:
                        return RecognitionReviewDecision(status="abstained", reason_code="no_match")
                    return self._discovered_series(group["identity"])

                # 复核已经领取，推进时间或走实际人工操作后才让模型返回。
                with patch.object(db, "now", side_effect=lambda: now), \
                        patch("app.modules.agent_recognition_review.review_confirmation_payload", side_effect=decide):
                    confirmations._process_recognition_review_row(claimed)
                    [view] = batches.list_confirmations(confirmation_ids=[token], status="all")["items"]
                self.assertEqual(view["candidates"], [])
                self.assertIsNone(view["suggestion"])
                self.assertIsNone(view["selected_index"])
                self.assertEqual(view["status"], {"skipped": "cancelled", "abstained": "pending"}.get(state, "expired"))
                self.assertIs(view["can_discover_candidates"], state == "abstained")
                row = self.row(token)
                self.assertEqual(confirmations.decode_row(row)["candidates"], [])
                self.assertIsNone(confirmations.decode_json_object(row["review_result_json"]).get("candidate_index"))
                if state in {"skipped", "superseded"}:
                    self.assertEqual(json.loads(row["result_json"])["resolution"], state)
                if replacement is not None:
                    self.assertEqual(confirmations.decode_row(self.row(replacement))["candidates"], [])
                if state == "abstained":
                    self.assertEqual(row["review_status"], "abstained")

    def test_candidate_discovery_request_checks_the_frozen_group_eligibility(self):
        self.enterContext(patch.object(feature_gate, "recognition_review_enabled", return_value=True))
        fixtures: dict[str, dict] = {
            "max": self._unmatched_series("max", count=80),
            "over-limit": self._unmatched_series("over-limit", count=81),
            "empty": self._unmatched_series("empty", count=0),
            "unnamed": {**self._unmatched_series("unnamed"), "files": [{"name": "  "}]},
            "retry": {**self._unmatched_series("retry"), organize_confirmations_repo.RETRY_SELECTED_INDEX_KEY: 0},
            "local": {**self._unmatched_series("local"), "kind": "local_media"},
        }
        expires_at = (datetime.now() + timedelta(hours=1)).isoformat(sep=" ", timespec="seconds")
        for token, payload in fixtures.items():
            organize_confirmations_repo.create_organize_confirmation(
                token=token, fingerprint=token, chat_id="chat", source_name="来源A",
                directory_path=payload["directory"], payload=payload, expires_at=expires_at,
            )
        tokens = list(fixtures)
        views = batches.list_confirmations(confirmation_ids=tokens)["items"]
        self.assertEqual({view["confirmation_id"]: view["can_discover_candidates"] for view in views},
                         {token: token == "max" for token in fixtures if token != "local"})
        self.assertEqual(batches.request_reviews(tokens), {"requested": 1, "unchanged": 0, "unavailable": 5})
        claimed = organize_confirmations_repo.claim_next_organize_confirmation_review()
        assert claimed is not None
        self.assertEqual(claimed["token"], "max")
        self.assertIsNone(organize_confirmations_repo.claim_next_organize_confirmation_review())
        for token in fixtures.keys() - {"max"}:
            self.assertFalse(confirmations.decode_row(self.row(token)).get("discover_candidates"))

    def test_discovery_candidate_and_suggestion_writes_roll_back_together(self):
        self.enterContext(patch.object(feature_gate, "recognition_review_enabled", return_value=True))
        failures = {
            "candidate": "json_array_length(NEW.payload_json,'$.candidates') > 0",
            "suggestion": "NEW.review_status='approved'",
        }
        # 分别拒绝候选与建议写入，防止任一先单独提交后留下半成品。
        for boundary, condition in failures.items():
            with self.subTest(boundary=boundary):
                group = self._unmatched_series(f"failure-{boundary}")
                token = self._schedule_unmatched_group(group, boundary)
                self.assertEqual(batches.request_reviews([token])["requested"], 1)
                decision = self._discovered_series(group["identity"])
                self._run_review_with_failing_write(condition, decision)
                stored = self.row(token)
                self.assertEqual((stored["status"], stored["review_status"], stored["selected_index"]),
                                 ("pending", "failed", None))
                self.assertEqual(confirmations.decode_row(stored)["candidates"], [])
                self.assertIsNone(confirmations.decode_json_object(stored["review_result_json"]).get("candidate_index"))
                [view] = batches.list_confirmations(confirmation_ids=[token])["items"]
                self.assertIsNone(view["suggestion"])

    @staticmethod
    def _run_review_with_failing_write(condition: str, decision: RecognitionReviewDecision) -> None:
        with db.get_conn() as conn:
            conn.execute(f"""
                CREATE TRIGGER reject_review_write BEFORE UPDATE ON organize_confirmations
                WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'test review write failed'); END
            """)
        stop, wake = Event(), Event()
        try:
            with (
                patch("app.modules.agent_recognition_review.review_confirmation_payload", return_value=decision),
                patch.object(wake, "wait", side_effect=lambda _: stop.set()),
            ):
                confirmations._recognition_review_loop(stop_event=stop, wakeup_event=wake)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER reject_review_write")

    def test_read_only_request_revokes_automatic_claim_even_if_worker_already_read_payload(self):
        self.create("one", review=True)
        row = organize_confirmations_repo.claim_next_organize_confirmation_review()
        self.assertIsNotNone(row)
        with patch.object(feature_gate, "recognition_review_enabled", return_value=True):
            result = batches.request_reviews(["one"])
        self.assertEqual(result["unchanged"], 1)
        with self.assertRaises(ValueError):
            organize_confirmations_repo.claim_organize_confirmation("one", chat_id="chat", selected_index=0, actor="agent")
        self.assertEqual(self.row("one")["status"], "pending")
        preview = batches.preview_choices([{"confirmation_id": "one", "candidate_index": 1}])
        result = batches.submit_choices(preview["entries"], preview["digest"], actor="agent")
        self.assertEqual(result["accepted"], 1)
        row = self.row("one")
        self.assertEqual(row["confirmation_actor"], "agent")
        self.assertTrue(json.loads(row["payload_json"])["_explicit_approval"])

    def test_wrong_chat_changed_candidate_and_duplicate_group_are_rejected(self):
        self.create("one")
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        with self.assertRaises(ValueError):
            batches.preview_choices(entries, chat_id="other")
        self.assertEqual(batches.list_confirmations(chat_id="other")["items"], [])
        with self.assertRaises(ValueError):
            batches.preview_choices(entries * 2)
        preview = batches.preview_choices(entries)
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices([{"confirmation_id": "one", "candidate_index": 1}], preview["digest"])
        row = self.row("one")
        payload = json.loads(row["payload_json"])
        payload["files"][0]["size"] += 1
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='one'", (json.dumps(payload),))
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices(entries, preview["digest"])

    def test_expiry_and_rules_changes_reject_preview_without_cloud_writes(self):
        self.create("one")
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        self.rules.target_dir_id = "another-target"
        with self.assertRaisesRegex(ValueError, "整理规则已变化"):
            batches.preview_choices(entries)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01' WHERE token='one'")
        self.assertEqual(batches.list_confirmations()["items"], [])
        with self.assertRaisesRegex(ValueError, "过期"):
            batches.preview_choices(entries)

    def test_queue_rejection_does_not_replay_accepted_items(self):
        self.create("one")
        self.create("two")
        entries = [{"confirmation_id": token, "candidate_index": 0} for token in ("one", "two")]
        preview = batches.preview_choices(entries)
        original = confirmations.start_confirmation
        def racing_start(token, *args, **kwargs):
            if token == "two":
                raise ValueError("人工已选择其他候选")
            return original(token, *args, **kwargs)
        with patch.object(confirmations, "start_confirmation", side_effect=racing_start):
            result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (1, 1))
        self.assertEqual(self.row("one")["status"], "queued")
        self.assertEqual(self.row("two")["status"], "pending")

    def test_notification_and_review_mode_do_not_change_file_identity(self):
        payload = self.create("one")
        fingerprint = confirmations._fingerprint(payload)
        payload.update(_confirmation_batch="b", review_only=True, _explicit_approval=True)
        self.assertEqual(confirmations._fingerprint(payload), fingerprint)

    def test_skip_ends_each_group_like_telegram_and_reports_rejected_groups(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import logs_api

        self.create("candidates")
        no_metadata = self.create("no-metadata", chat="")
        no_metadata.update(candidates=[], allow_skip_terminal=True)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='no-metadata'",
                         (json.dumps(no_metadata, ensure_ascii=False),))
        self.create("old")
        self.create("done")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01 00:00:00' WHERE token='old'")
            conn.execute("UPDATE organize_confirmations SET status='completed' WHERE token='done'")
        app = FastAPI()
        app.include_router(logs_api.router)
        logs: list[tuple[str, str]] = []
        with patch.object(logs_api, "require_api_login"), TestClient(app) as client, \
             patch.object(confirmations, "_finalize_guangya_manual_logs",
                          side_effect=lambda payload, status, error, **_: logs.append((status, error))):
            result = client.post("/api/logs/organize/confirmations/skip",
                                 json={"confirmation_ids": ["candidates", "no-metadata", "old", "done", "missing"]}).json()
            self.assertEqual(client.post("/api/logs/organize/confirmations/skip", json={"confirmation_ids": "x"}).status_code, 400)
        self.assertEqual((result["skipped"], result["failed"]), (2, 3))
        self.assertEqual({item["confirmation_id"]: item["skipped"] for item in result["items"]},
                         {"candidates": True, "no-metadata": True, "old": False, "done": False, "missing": False})
        # 有候选的组按“暂不处理”、无元数据的组按“跳过此组”，文件不动，日志统一标记为跳过；
        # 已过期的组沿用现有过期收尾，不算作跳过。
        self.assertEqual(logs, [("skipped", "用户选择暂不处理"), ("skipped", "用户选择跳过：暂无可用元数据"),
                                ("skipped", "人工确认已过期")])
        self.assertEqual({token: self.row(token)["status"] for token in ("candidates", "no-metadata", "done")},
                         {"candidates": "cancelled", "no-metadata": "cancelled", "done": "completed"})
        self.assertNotEqual(self.row("old")["status"], "pending")

    def test_logs_skip_their_pending_group_and_detail_offers_skip_only_for_those(self):
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.modules.organize_correction import OrganizeCorrectionService
        from app.routes import logs_api

        manual, sibling = (db.add_organize_log("guangya", "/incoming/group", "", file_id, "manual", "",
                                               original_name=f"{file_id}.mkv", legacy_incomplete=False)
                           for file_id in ("group", "group-2"))
        self.create("group")
        payload = json.loads(self.row("group")["payload_json"])
        payload["files"].append({**payload["files"][0], "file_id": "group-2", "name": "group-2.mkv"})
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='group'",
                         (json.dumps(payload, ensure_ascii=False),))
        done = db.add_organize_log("guangya", "/incoming/done", "/archive/done", "done", "success", "", legacy_incomplete=False)
        orphan = db.add_organize_log("guangya", "/incoming/orphan", "", "orphan", "manual", "", legacy_incomplete=False)
        service = OrganizeCorrectionService(client=MagicMock(), scraper=MagicMock())
        try:
            self.assertEqual([service.detail(log_id)["allowed_actions"]["skip"] for log_id in (manual, done, orphan)],
                             [True, False, False])
        finally:
            service.close()
        app = FastAPI()
        app.include_router(logs_api.router)
        with patch.object(logs_api, "require_api_login"), TestClient(app) as client:
            result = client.post("/api/logs/organize/batch/skip", json={"log_ids": [manual, done, orphan]}).json()
            for invalid in ([], "x", [0], list(range(1, 52))):
                self.assertEqual(client.post("/api/logs/organize/batch/skip", json={"log_ids": invalid}).status_code, 400)
        self.assertEqual([(item["log_id"], item["skipped"]) for item in result["items"]],
                         [(manual, True), (done, False), (orphan, False)])
        self.assertEqual((result["skipped"], result["failed"]), (1, 2))
        # 整组结束：同组的另一条待确认日志一并标记为跳过，文件不动。
        self.assertEqual(self.row("group")["status"], "cancelled")
        self.assertEqual([self.log(log_id)["status"] for log_id in (manual, sibling, done, orphan)],
                         ["skipped", "skipped", "success", "manual"])

    def test_log_skip_deduplicates_group_and_replay_does_not_change_logs_or_files(self):
        log_ids = [db.add_organize_log("guangya", "/incoming/group", "", "group", "manual", "",
                                       original_name="group.mkv", original_parent_id="parent",
                                       current_name="group.mkv", current_parent_id="parent") for _ in range(2)]
        self.create("group")
        with patch.object(confirmations, "GuangYaClient") as cloud:
            result = batches.skip_logs([*log_ids, log_ids[0]])
            self.assertEqual((result["skipped"], result["failed"]), (2, 0))
            logs = [dict(self.log(log_id)) for log_id in log_ids]
            self.assertEqual((batches.skip_logs(log_ids)["failed"], batches.skip_confirmations(["group"])["failed"]), (2, 1))
            self.assertEqual([dict(self.log(log_id)) for log_id in log_ids], logs)
            self.assertTrue(all(log["current_parent_id"] == "parent" and log["current_name"] == "group.mkv" for log in logs))
            cloud.assert_not_called()
        self.assertIsNone(organize_confirmations_repo.get_next_queued_organize_confirmation())

    def test_new_logs_are_not_owned_by_an_older_confirmation(self):
        self.create("group")
        log_id = db.add_organize_log("guangya", "/incoming/group", "", "group", "manual", "")
        result = batches.skip_logs([log_id])
        self.assertEqual((result["skipped"], result["failed"]), (0, 1))
        self.assertEqual(self.row("group")["status"], "pending")
        self.assertEqual(self.log(log_id)["status"], "manual")

    def test_confirmation_terminal_actions_never_settle_later_scan_logs(self):
        for action in ('defer', 'skip', 'expire', 'legacy'):
            with self.subTest(action=action):
                old = db.add_organize_log('guangya', '/incoming', '', action, 'manual', '')
                self.create(action)
                payload = json.loads(self.row(action)['payload_json'])
                if action == 'skip':
                    payload['allow_skip_terminal'] = True
                if action == 'legacy':
                    payload.pop('_organize_log_upper_bound')
                with db.get_conn() as conn:
                    conn.execute('UPDATE organize_confirmations SET payload_json=? WHERE token=?', (json.dumps(payload), action))
                    conn.execute("UPDATE organize_log SET updated_at='2000-01-01 00:00:00' WHERE id=?", (old,))
                newer = db.add_organize_log('guangya', '/incoming', '', action, 'manual', '')
                if action == 'legacy':
                    with db.get_conn() as conn:
                        conn.execute("UPDATE organize_log SET updated_at='2999-01-01 00:00:00' WHERE id=?", (newer,))
                before = dict(self.log(newer))
                if action == 'expire':
                    with db.get_conn() as conn:
                        conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01 00:00:00' WHERE token=?", (action,))
                batches.skip_confirmations([action])
                self.assertEqual(self.log(old)['status'], 'skipped')
                self.assertEqual(dict(self.log(newer)), before)

    def test_latest_queued_confirmation_does_not_fall_back_to_an_older_pending_group(self):
        log_id = db.add_organize_log("guangya", "/incoming/group", "", "old", "manual", "")
        self.create("old")
        payload = self.create("new")
        payload["files"][0]["file_id"] = "old"
        # 新快照的指纹不同，旧待确认仍存在；不能按 pending 筛选后命中旧组。
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='new'",
                         (json.dumps(payload),))
        organize_confirmations_repo.claim_organize_confirmation("new", chat_id="chat", selected_index=0)
        result = batches.skip_logs([log_id])
        self.assertEqual(result["failed"], 1)
        self.assertEqual([self.row(token)["status"] for token in ("old", "new")], ["pending", "queued"])
        self.assertEqual(self.log(log_id)["status"], "manual")

    def test_legacy_log_lookup_uses_strict_timestamp_and_ignores_local_or_corrupt_payloads(self):
        log_id = db.add_organize_log("guangya", "/incoming/group", "", "group", "manual", "")
        self.create("group")
        payload = json.loads(self.row("group")["payload_json"])
        payload.pop("_organize_log_upper_bound")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET updated_at='2000-01-01 00:00:00' WHERE id=?", (log_id,))
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='group'", (json.dumps(payload),))
        for token, encoded in (("local", json.dumps({"kind": "local_media", "files": payload["files"]})),
                               ("corrupt", "{broken")):
            self.create(token)
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token=?", (encoded, token))
        self.assertEqual(organize_confirmations_repo.pending_confirmation_tokens_by_log([log_id]), {log_id: "group"})
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET updated_at=? WHERE id=?", (self.row("group")["created_at"], log_id))
        self.assertEqual(batches.skip_logs([log_id])["skipped"], 0)
        self.assertEqual(batches.skip_confirmations(["local"])["skipped"], 0)
        self.assertEqual(self.row("local")["status"], "pending")

    def test_concurrent_skip_claims_the_group_once(self):
        log_id = db.add_organize_log("guangya", "/incoming/group", "", "group", "manual", "")
        self.create("group", review=True)
        barrier, original_row = Barrier(2), batches._row

        def simultaneous_read(*args, **kwargs):
            row = original_row(*args, **kwargs)
            barrier.wait(timeout=5)
            return row

        with patch.object(batches, "_row", side_effect=simultaneous_read), ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: batches.skip_confirmations(["group"]), range(2)))
        self.assertEqual(sorted(result["skipped"] for result in results), [0, 1])
        self.assertEqual((self.row("group")["status"], self.row("group")["review_status"]), ("cancelled", "cancelled"))
        self.assertEqual(self.log(log_id)["status"], "skipped")

    def test_enqueue_wins_between_skip_lookup_and_atomic_cancellation(self):
        log_id = db.add_organize_log("guangya", "/incoming/group", "", "group", "manual", "")
        self.create("group")
        cancel = confirmations.cancel_confirmation

        def enqueue_then_cancel(*args, **kwargs):
            organize_confirmations_repo.claim_organize_confirmation("group", chat_id="chat", selected_index=1)
            return cancel(*args, **kwargs)

        with patch.object(confirmations, "cancel_confirmation", side_effect=enqueue_then_cancel):
            result = batches.skip_logs([log_id])
        self.assertEqual((result["skipped"], result["failed"]), (0, 1))
        self.assertEqual((self.row("group")["status"], self.row("group")["selected_index"]), ("queued", 1))
        self.assertEqual(self.log(log_id)["status"], "manual")

    def test_batch_skip_continues_after_one_storage_failure(self):
        for token in ("bad", "good"):
            self.create(token)
        cancel = organize_confirmations_repo.cancel_organize_confirmation

        def fail_one(token, **kwargs):
            if token == "bad":
                raise sqlite3.OperationalError("database is locked")
            return cancel(token, **kwargs)

        with patch.object(organize_confirmations_repo, "cancel_organize_confirmation", side_effect=fail_one):
            result = batches.skip_confirmations(["bad", "good"])
        self.assertEqual((result["skipped"], result["failed"]), (1, 1))
        self.assertEqual([self.row(token)["status"] for token in ("bad", "good")], ["pending", "cancelled"])

    def test_api_roundtrip_uses_saved_selection_and_same_durable_queue(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import logs_api
        app = FastAPI()
        app.include_router(logs_api.router)
        self.create("one")
        self.create("unselected")
        with patch.object(logs_api, "require_api_login"), TestClient(app) as client:
            response = client.post("/api/logs/organize/confirmations/selection", json={"confirmation_id": "one", "candidate_index": 1})
            self.assertEqual(response.status_code, 200)
            view = client.post("/api/logs/organize/confirmations/query", json={"confirmation_ids": ["one"]}).json()
            self.assertEqual(view["confirmation_ids"], ["one"])
            self.assertEqual([item["confirmation_id"] for item in view["items"]], ["one"])
            self.assertEqual(client.get("/api/logs/organize/confirmations",
                                        params={"confirmation_ids": "one", "status": "all"}).json(), view)
            self.assertEqual(view["items"][0]["suggestion"]["candidate_index"], 1)
            entries = [{"confirmation_id": "one", "candidate_index": 1}]
            preview = client.post("/api/logs/organize/confirmations/preview", json={"entries": entries})
            self.assertEqual(preview.status_code, 200)
            accepted = client.post("/api/logs/organize/confirmations/submit", json={"entries": entries, "digest": preview.json()["digest"]})
            self.assertEqual(accepted.json()["accepted"], 1)
            self.assertEqual(client.post("/api/logs/organize/confirmations/query", json={"confirmation_ids": ["one"]}).json()["items"][0]["status"], "queued")
            invalid = client.post("/api/logs/organize/confirmations/review", json={"confirmation_ids": "one"})
            self.assertEqual(invalid.status_code, 400)

    def test_agent_and_notifications_off_still_persist_web_candidates(self):
        payload = self.create("fixture")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
        group = {**payload, "files": payload["files"]}
        with patch.object(feature_gate, "recognition_review_enabled", return_value=False), \
                patch.object(confirmations.Organizer, "_validated_task_confirmation_groups", return_value=([group], 1)):
            self.assertEqual(confirmations.schedule_guangya_recognition_reviews({"task_id": "silent"}, self.rules), 1)
        view = batches.list_confirmations(task_id="silent")
        self.assertEqual(view["total"], 1)
        self.assertFalse(view["items"][0]["review_status"])

    def test_batch_downstream_remains_in_same_records(self):
        self.create("one", batch="batch")
        with patch("app.modules.telegram_confirmation_batch.publish_batch_summary", return_value=True) as publish:
            self.assertTrue(confirmations.update_confirmation_lifecycle_downstream(
                "one", chat_id="chat", strm_status="完成", media_refresh="已排队",
            ))
        publish.assert_called_once_with("batch", chat_id="chat")
        self.assertEqual(batches.list_confirmations()["items"][0]["downstream"]["strm"], "完成")

    def test_downstream_notification_scope_keeps_receipts_without_changing_payload(self):
        for batch_id in ("", "batch"):
            with self.subTest(batch=bool(batch_id)):
                token = "scoped-batch" if batch_id else "scoped-single"
                self.create(token, batch=batch_id)
                organize_confirmations_repo.update_organize_confirmation(
                    token, status="completed", result_json=json.dumps({"moved": 1}),
                )
                original_payload = self.row(token)["payload_json"]
                previous = confirmations.NotificationEvent("确认完成", fields=(("STRM 状态", "等待"),))

                def publish(*_args, **_kwargs):
                    # 启用通知的默认调用仍必须先持久化回执，再投递。
                    saved = json.loads(self.row(token)["result_json"])
                    self.assertEqual(saved["_downstream"]["media_refresh"], "已完成")
                    return True

                with (patch("app.modules.telegram_notification_center.get_notification_thread_snapshot", return_value=SimpleNamespace(event=previous, revision=1)) as lookup,
                      patch.object(confirmations, "publish_confirmation_event", side_effect=publish) as single,
                      patch("app.modules.telegram_confirmation_batch.publish_batch_summary", side_effect=publish) as batch):
                    self.assertFalse(confirmations.update_confirmation_lifecycle_downstream(
                        token, chat_id="chat", strm_status="完成", media_refresh="刷新失败", partial=True,
                        error="媒体库不可用", notify_enabled=False,
                    ))
                    lookup.assert_not_called()
                    single.assert_not_called()
                    batch.assert_not_called()
                    row = self.row(token)
                    self.assertEqual((row["status"], row["payload_json"]), ("completed", original_payload))
                    saved = json.loads(row["result_json"])
                    self.assertEqual(saved["moved"], 1)
                    self.assertEqual(saved["_downstream"], {
                        "strm": "完成", "media_refresh": "刷新失败", "partial": True, "error": "媒体库不可用",
                    })
                    self.assertTrue(confirmations.update_confirmation_lifecycle_downstream(
                        token, chat_id="chat", strm_status="完成", media_refresh="已完成",
                    ))
                    self.assertEqual(self.row(token)["payload_json"], original_payload)
                    if batch_id:
                        batch.assert_called_once_with(batch_id, chat_id="chat")
                        single.assert_not_called()
                    else:
                        single.assert_called_once()
                        batch.assert_not_called()

    def test_older_strm_run_cannot_replace_receipt_or_publish_confirmation(self):
        for batch_id in ("", "batch"):
            with self.subTest(batch=bool(batch_id)):
                token = "fenced-batch" if batch_id else "fenced-single"
                self.create(token, batch=batch_id)
                historical = {"moved": 1, "execution": {"moved": 1}, "strm": {"ok": True}, "warnings": ["保留"]}
                organize_confirmations_repo.update_organize_confirmation(
                    token, status="completed", result_json=json.dumps(historical),
                )
                payload = self.row(token)["payload_json"]
                with (patch("app.modules.telegram_notification_center.get_notification_thread_snapshot") as lookup,
                      patch.object(confirmations, "publish_confirmation_event") as single,
                      patch("app.modules.telegram_confirmation_batch.publish_batch_summary") as batch):
                    for refresh in ("Emby 已排队", "Emby 已刷新"):
                        # 真实单owner规格probe可能新开run而不换request代次；围栏归属确认原记录。
                        self.assertFalse(confirmations.update_confirmation_lifecycle_downstream(
                            token, strm_status="完成", media_refresh=refresh, strm_run_id=2, notify_enabled=False,
                        ))
                        row = self.row(token)
                        saved = json.loads(row["result_json"])
                        self.assertEqual(saved["_downstream_run_id"], 2)
                        self.assertEqual(saved["_downstream"], {
                            "strm": "完成", "media_refresh": refresh, "partial": False, "error": "",
                        })
                        self.assertEqual({key: saved[key] for key in historical}, historical)
                        # 上次连接已经关闭，旧回执只能依赖新连接读到的持久run值做判断。
                        self.assertFalse(confirmations.update_confirmation_lifecycle_downstream(
                            token, strm_status="完成", media_refresh="Jellyfin 部分失败", partial=True,
                            error="旧run迟到", strm_run_id=1,
                        ))
                        self.assertEqual(self.row(token)["result_json"], row["result_json"])
                        self.assertEqual((self.row(token)["payload_json"], self.row(token)["status"]), (payload, "completed"))
                    lookup.assert_not_called()
                    single.assert_not_called()
                    batch.assert_not_called()

    def test_downstream_run_fence_preserves_same_run_and_unversioned_contract(self):
        self.create("run-contract")
        queued = {"strm": "完成", "media_refresh": "已排队", "partial": False, "error": ""}
        finished = {**queued, "media_refresh": "已刷新"}
        self.assertTrue(organize_confirmations_repo.set_confirmation_downstream("run-contract", queued))
        self.assertNotIn("_downstream_run_id", json.loads(self.row("run-contract")["result_json"]))
        self.assertTrue(organize_confirmations_repo.set_confirmation_downstream("run-contract", queued, strm_run_id=2))
        self.assertTrue(organize_confirmations_repo.set_confirmation_downstream("run-contract", finished, strm_run_id=2))
        before = self.row("run-contract")["result_json"]
        self.assertTrue(organize_confirmations_repo.set_confirmation_downstream("run-contract", finished, strm_run_id=2))
        self.assertEqual(self.row("run-contract")["result_json"], before)
        self.assertFalse(organize_confirmations_repo.set_confirmation_downstream("run-contract", queued, strm_run_id=1))
        self.assertEqual(self.row("run-contract")["result_json"], before)
        # 无run的旧接口仍可更新原四字段，不删除已持久的围栏。
        self.assertTrue(organize_confirmations_repo.set_confirmation_downstream("run-contract", queued))
        saved = json.loads(self.row("run-contract")["result_json"])
        self.assertEqual((saved["_downstream"], saved["_downstream_run_id"]), (queued, 2))
        self.assertFalse(organize_confirmations_repo.set_confirmation_downstream("missing", queued, strm_run_id=3))

    def test_silent_confirmation_keeps_handoff_and_faster_downstream_receipts(self):
        from app.clients.guangya import GuangYaFile
        from app.modules.scraper import MatchResult

        rules = replace(self.rules, clean_empty=False, notify_enabled=False, library_notify=False)
        for outcome in ("rejected", "exception", "accepted", "skipped"):
            with self.subTest(outcome=outcome):
                token = f"silent-{outcome}"
                payload = self.create(token)
                payload.update(_notification_suppressed=True, rules=organize_rules_snapshot(rules))
                with db.get_conn() as conn:
                    conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token=?", (json.dumps(payload), token))
                organize_confirmations_repo.claim_organize_confirmation(token, chat_id="chat", selected_index=0)
                claimed = organize_confirmations_repo.claim_queued_organize_confirmation(token)
                payload = confirmations.decode_row(claimed)
                file = payload["files"][0]
                client = SimpleNamespace(file_info=lambda _id: GuangYaFile(
                    file["file_id"], file["name"], False, file["size"], "", file["parent_id"],
                ))
                candidate = payload["candidates"][0]
                match = MatchResult(tmdb_id=candidate["tmdb_id"], title=candidate["title"],
                                    year=candidate["year"], media_type="movie", confidence=1.0)
                scraper = SimpleNamespace(get_detail_with_credits=lambda *_: {"id": 100},
                    match_from_tmdb=lambda *_: match, confirm=lambda *_, **__: None)

                def post_actions(stats, *_args, **_kwargs):
                    if outcome == "exception":
                        raise RuntimeError("STRM 提交异常")
                    if outcome == "accepted":
                        # 完成回调快于 trigger 返回，保存交接不得再把终态覆盖成排队。
                        confirmations.update_confirmation_lifecycle_downstream(
                            token, strm_status="完成", media_refresh="未启用或无目标",
                        )
                    stats["strm"] = ({"ok": False, "error": "STRM 调度器正在停止"}
                                     if outcome == "rejected" else {"ok": True, "skipped": True}
                                     if outcome == "skipped" else {"ok": True, "queued": True})

                with (patch.object(OrganizeRules, "from_config", return_value=rules),
                      patch.object(OrganizeRules, "for_source", return_value=rules),
                      patch.object(confirmations, "GuangYaClient", return_value=client),
                      patch.object(confirmations, "TMDBScraper", return_value=scraper),
                      patch.object(confirmations, "Organizer") as factory,
                      patch.object(confirmations, "_dispatch_due_confirmation_delivery") as delivery):
                    factory.return_value.organize.side_effect = [
                        ([SimpleNamespace(file_id=token)], {}),
                        ([], {"total": 1, "moved": 1, "skipped": 0, "failed": 0, "need_confirm": 0}),
                    ]
                    factory.trigger_post_actions.side_effect = post_actions
                    result = confirmations._execute_confirmation(
                        token, payload, candidate, selected_index=0, chat_id="chat",
                    )
                delivery.assert_not_called()
                row = self.row(token)
                saved = json.loads(row["result_json"])
                self.assertEqual((row["status"], saved["moved"]), ("completed", 1))
                self.assertEqual(saved["strm"], result["stats"]["strm"])
                view = batches.list_confirmations(confirmation_ids=[token], status="all")["items"][0]
                downstream = view["downstream"]
                self.assertEqual(downstream["strm"], "完成" if outcome == "accepted" else
                                 "已跳过" if outcome == "skipped" else "启动失败")
                self.assertEqual(downstream["partial"], outcome in {"rejected", "exception"})
                if outcome in {"rejected", "exception"}:
                    self.assertTrue(downstream["error"])
                self.assertEqual(factory.return_value.organize.call_count, 2)

    def test_non_batch_downstream_is_persisted_without_a_telegram_thread(self):
        self.create("no-thread")
        with patch("app.modules.telegram_notification_center.get_notification_thread_snapshot", return_value=None):
            self.assertFalse(confirmations.update_confirmation_lifecycle_downstream(
                "no-thread", strm_status="完成", media_refresh="刷新失败", partial=True, error="媒体库不可用",
            ))
        view = batches.list_confirmations()["items"][0]
        self.assertEqual(view["downstream"], {
            "strm": "完成", "media_refresh": "刷新失败", "partial": True, "error": "媒体库不可用",
        })

    def test_sdk_and_web_read_execution_receipt_without_requerying_cloud(self):
        from app import sdk
        from app.routes import logs_api
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.create("done", batch="batch")
        receipt = {
            "total": 3, "moved": 1, "renamed": 1, "skipped": 1, "failed": 1,
            "scan_complete": True, "warnings": ["刮削失败 api_key=private-live-key"],
            "scan_errors": ["扫描失败 access_token=private-scan-key", "目录暂不可读"],
            "media_items": [{"title": "作品", "category": "电影/作品 (2026)",
                             "filename": "作品.2026.mp4", "size": 100000000,
                             "poster_path": "unneeded-poster"}],
            "_downstream": {"strm": "已排队", "media_refresh": "等待 STRM 完成"},
            "internal_context": "must-not-export",
        }
        organize_confirmations_repo.update_organize_confirmation("done", status="completed", result_json=json.dumps(receipt),
                                       completed_at="2026-11-14 10:00:00")
        log_id = db.add_organize_log("guangya", "/source", "/archive", "done", "success",
                                     operation_token="recognition-confirm:done")
        app = FastAPI()
        app.include_router(logs_api.router)
        with patch("socket.socket.connect", side_effect=AssertionError("查询回执不应访问外部服务")), \
                patch.object(logs_api, "require_api_login"), TestClient(app) as client:
            sdk_result = sdk.activity.organize_confirmations(confirmation_ids=["done"], status="all")
            response = client.post("/api/logs/organize/confirmations/query",
                                   json={"confirmation_ids": ["done"], "status": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sdk_result)
        item = sdk_result["items"][0]
        self.assertEqual(item["status"], "completed")  # 队列结束，但实际执行仍有失败。
        self.assertEqual(item["execution"]["failed"], 1)
        self.assertEqual(item["execution"]["moved"], 1)
        self.assertEqual(item["execution"]["warnings"], ["刮削失败 api_key=********"])
        self.assertEqual(item["execution"]["scan_errors"], ["扫描失败 access_token=********", "目录暂不可读"])
        self.assertEqual(item["execution"]["media_items"], [{
            "title": "作品", "category": "电影/作品 (2026)", "filename": "作品.2026.mp4", "size": 100000000,
        }])
        self.assertEqual(item["completed_at"], "2026-11-14 10:00:00")
        self.assertEqual(item["log_ids"], [log_id])
        self.assertEqual(item["downstream"], receipt["_downstream"])
        self.assertNotIn("internal_context", item["execution"])
        self.assertNotIn("private-live-key", json.dumps(item))
        self.assertNotIn("private-scan-key", json.dumps(item))

    def test_missing_execution_receipt_stays_unknown_instead_of_zero_success(self):
        for token, status, result in (("queued", "queued", {}), ("legacy", "completed", {}),
                                      ("cancelled", "cancelled", {"resolution": "cancelled"})):
            self.create(token)
            organize_confirmations_repo.update_organize_confirmation(token, status=status, result_json=json.dumps(result))
        items = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        self.assertIsNone(items["queued"]["execution"])
        self.assertIsNone(items["legacy"]["execution"])
        self.assertEqual(items["cancelled"]["execution"], {"resolution": "cancelled"})
        self.assertEqual(items["legacy"]["downstream"], {})

    def test_exact_log_links_survive_same_names_and_receipt_survives_log_clear(self):
        for token in ("first", "second"):
            self.create(token)
            organize_confirmations_repo.update_organize_confirmation(token, status="completed", result_json=json.dumps({
                "total": 1, "moved": 1, "failed": 0,
            }))
        ids = [db.add_organize_log("guangya", "/same", "/same-target", "same-file", "success",
                                   operation_token=f"recognition-confirm:{token}")
               for token in ("first", "second", "unrelated")]
        items = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        self.assertEqual(items["first"]["log_ids"], ids[:1])
        self.assertEqual(items["second"]["log_ids"], ids[1:2])
        db.clear_organize_logs(origin="guangya", status="success")
        after = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        for token in items:
            self.assertEqual(after[token]["log_ids"], [])
            self.assertEqual(after[token]["execution"], items[token]["execution"])

    def test_background_probe_resolves_batch_thread_without_losing_per_item_identity(self):
        from app.modules.organize_probe_notifications import build_notification_context, publish_probe_scope
        from app.notifier import NotificationEvent
        self.create("one", batch="batch")
        context = build_notification_context(confirmation_token="one", chat_id="chat")
        self.assertEqual(context["notification_threads"][0]["token"], "one")
        event = NotificationEvent("⚠️ 整理待确认批次", state="confirmation_batch")
        with patch("app.modules.telegram_notification_center.get_notification_thread_snapshot", return_value=SimpleNamespace(event=event)) as lookup, \
                patch("app.modules.telegram_confirmation_batch.publish_batch_summary", return_value=True):
            self.assertTrue(publish_probe_scope({"context": context}, strm_status="完成", media_refresh="已排队"))
        self.assertEqual(lookup.call_args.args[0], "confirmation-batch:batch")
        self.assertEqual(batches.list_confirmations()["items"][0]["downstream"]["media_refresh"], "已排队")

    def test_episode_mapping_is_visible_and_bound_to_approval(self):
        from app.modules.episode_research_service import POLICY_VERSION, proposal_receipt
        from app.repositories.episode_research import put_episode_research_cache
        self.create("one")
        row = self.row("one")
        payload = json.loads(row["payload_json"])
        payload["candidates"][0]["media_type"] = "tv"
        payload["files"][0].update(season=1, episode=19)
        mapping = {"file_index": 0, "source_season": 1, "source_episode": 19,
                   "target_season": 2, "target_episode": 6}
        proposal = {"version": POLICY_VERSION, "status": "verified", "case_key": "a" * 64,
                    "candidate_index": 0, "tmdb_id": "100", "group_id": "b" * 24,
                    "group_fingerprint": "c" * 64, "mappings": [mapping]}
        receipt = proposal_receipt(proposal)
        audit = {"entry_mode": "episode_research", "candidate_index": 0, "episode_research_receipt": receipt}
        put_episode_research_cache("a" * 64, {"proposal": proposal}, status="verified", ttl_seconds=100, policy_version=POLICY_VERSION)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=?,review_result_json=? WHERE token='one'",
                         (json.dumps(payload), json.dumps(audit)))
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        preview = batches.preview_choices(entries)
        self.assertEqual(preview["items"][0]["episode_mappings"], [mapping])
        self.assertIn("Season 2", preview["items"][0]["target"])
        # 即使仍是同一个候选作品，研究季集映射改变也必须使旧确认失效。
        audit["episode_research_receipt"]["mapping_digest"] = "d" * 64
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET review_result_json=? WHERE token='one'", (json.dumps(audit),))
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices(entries, preview["digest"])

    def test_selection_saved_after_preview_invalidates_that_preview(self):
        self.create("one", review=True)
        organize_confirmations_repo.update_organize_confirmation(
            "one", review_status="approved",
            review_result_json=json.dumps({"candidate_index": 0, "summary": "Agent 建议"}),
        )
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        preview = batches.preview_choices(entries)
        batches.save_selection("one", 1)
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices(entries, preview["digest"])
        row = self.row("one")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["selected_index"])
        self.assertEqual(json.loads(row["review_result_json"])["candidate_index"], 1)

    def test_review_change_between_preview_check_and_claim_cannot_change_write_scope(self):
        self.create("one", review=True)
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        preview = batches.preview_choices(entries)
        original = confirmations.start_confirmation
        def racing_start(token, *args, **kwargs):
            batches.save_selection(token, 1)
            return original(token, *args, **kwargs)
        with patch.object(confirmations, "start_confirmation", side_effect=racing_start):
            result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (0, 1))
        self.assertEqual(self.row("one")["status"], "pending")
