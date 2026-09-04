"""整理识别 Agent 主动复核的安全、持久化与回退契约。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelToolCall,
)
from app.modules import organize_confirmations
from app.modules.agent_recognition_review import (
    RecognitionReviewDecision,
    _review_async,
)
from app.modules.organize import OrganizeRules
from tests.support import IsolatedDatabaseTestCase


class _ScriptedModel:
    def __init__(self, rounds: list[list[ModelEvent]]) -> None:
        self.rounds = list(rounds)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, *, cancellation) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self.rounds:
            raise AssertionError("unexpected model round")
        for event in self.rounds.pop(0):
            cancellation.raise_if_cancelled()
            await asyncio.sleep(0)
            yield event


class _FakeScraper:
    def __init__(self, *, season_episodes: tuple[int, ...] = (1, 2)) -> None:
        self.season_episodes = season_episodes
        self.closed = False

    @staticmethod
    def parse_source_position(_filename: str, _parent_path: str = ""):
        return 1, 1

    @staticmethod
    def get_detail(tmdb_id: str, media_type: str):
        assert media_type == "tv"
        return {
            "id": int(tmdb_id),
            "name": "测试动画",
            "original_name": "Test Animation",
            "first_air_date": "2026-01-03",
            "number_of_seasons": 1,
            "number_of_episodes": 2,
        }

    def get_tv_season_detail(self, tmdb_id: str, season: int):
        assert tmdb_id == "100"
        assert season == 1
        return {
            "id": 1001,
            "name": "Season 1",
            "episodes": [
                {"episode_number": episode, "name": f"E{episode}"}
                for episode in self.season_episodes
            ],
        }

    def close(self) -> None:
        self.closed = True


def _tool_round(call_id: str, name: str, arguments: dict) -> list[ModelEvent]:
    return [
        ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(call_id, name, arguments),
        ),
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
    ]


def _review_payload() -> dict:
    return {
        "version": 2,
        "kind": "guangya",
        "identity": "测试动画 2026",
        "reason": "候选接近，需要复核",
        "directory": "/动漫/测试动画",
        "files": [
            {
                "file_id": "file-1",
                "parent_id": "parent-1",
                "name": "测试动画.S01E01.mkv",
                "size": 1024,
                "season": 1,
                "episode": 1,
            }
        ],
        "candidates": [
            {
                "provider": "tmdb",
                "tmdb_id": "100",
                "title": "测试动画",
                "year": "2026",
                "media_type": "tv",
                "score": 0.82,
            }
        ],
    }


def _approval_model() -> _ScriptedModel:
    return _ScriptedModel(
        [
            _tool_round("case", "recognition.inspect_case", {}),
            _tool_round(
                "candidate",
                "recognition.inspect_candidate",
                {"candidate_index": 0},
            ),
            _tool_round(
                "season",
                "recognition.inspect_season",
                {"candidate_index": 0, "season": 1},
            ),
            _tool_round(
                "decision",
                "recognition.propose_review_decision",
                {
                    "decision": "approve",
                    "candidate_index": 0,
                    "confidence": 0.97,
                    "reason_code": "evidence_consistent",
                    "summary": "标题、年份与季集边界一致",
                },
            ),
            [
                ModelEvent(ModelEventType.TEXT_DELTA, text="复核完成。"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ],
        ]
    )


class RecognitionReviewKernelTests(IsolatedDatabaseTestCase):
    def test_internal_kernel_approves_only_after_read_evidence(self) -> None:
        model = _approval_model()
        scraper = _FakeScraper()
        settings = SimpleNamespace(model="test-model", timeout_seconds=1)
        with patch(
            "app.modules.agent_recognition_review.ProviderSettings.from_config",
            return_value=settings,
        ), patch(
            "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
            return_value=model,
        ), patch(
            "app.modules.agent_recognition_review.TMDBScraper",
            return_value=scraper,
        ):
            decision = asyncio.run(_review_async(_review_payload()))

        self.assertTrue(decision.approved)
        self.assertEqual(decision.candidate_index, 0)
        self.assertEqual(decision.tool_calls, 4)
        self.assertTrue(scraper.closed)
        self.assertEqual(len(model.requests), 5)
        self.assertEqual(
            {tool["name"].replace("__", ".") for tool in model.requests[0].tools},
            {
                "recognition.inspect_case",
                "recognition.inspect_candidate",
                "recognition.inspect_season",
                "recognition.propose_review_decision",
            },
        )

    def test_skipping_case_inspection_forces_abstain(self) -> None:
        model = _ScriptedModel(
            [
                _tool_round(
                    "candidate",
                    "recognition.inspect_candidate",
                    {"candidate_index": 0},
                ),
                _tool_round(
                    "season",
                    "recognition.inspect_season",
                    {"candidate_index": 0, "season": 1},
                ),
                _tool_round(
                    "decision",
                    "recognition.propose_review_decision",
                    {
                        "decision": "approve",
                        "candidate_index": 0,
                        "confidence": 0.97,
                        "reason_code": "evidence_consistent",
                        "summary": "候选与季集边界一致",
                    },
                ),
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="复核完成。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        settings = SimpleNamespace(model="test-model", timeout_seconds=1)
        with patch(
            "app.modules.agent_recognition_review.ProviderSettings.from_config",
            return_value=settings,
        ), patch(
            "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
            return_value=model,
        ), patch(
            "app.modules.agent_recognition_review.TMDBScraper",
            return_value=_FakeScraper(),
        ):
            decision = asyncio.run(_review_async(_review_payload()))

        self.assertEqual(decision.status, "abstained")
        self.assertEqual(decision.reason_code, "case_not_inspected")

    def test_missing_tmdb_episode_forces_abstain_even_when_model_approves(self) -> None:
        model = _approval_model()
        settings = SimpleNamespace(model="test-model", timeout_seconds=1)
        with patch(
            "app.modules.agent_recognition_review.ProviderSettings.from_config",
            return_value=settings,
        ), patch(
            "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
            return_value=model,
        ), patch(
            "app.modules.agent_recognition_review.TMDBScraper",
            return_value=_FakeScraper(season_episodes=(2,)),
        ):
            decision = asyncio.run(_review_async(_review_payload()))

        self.assertFalse(decision.approved)
        self.assertEqual(decision.status, "abstained")
        self.assertEqual(decision.reason_code, "episode_boundary_unverified")

    def test_title_mismatch_forces_abstain(self) -> None:
        payload = _review_payload()
        payload["candidates"][0]["title"] = "另一部作品"
        model = _approval_model()
        settings = SimpleNamespace(model="test-model", timeout_seconds=1)
        with patch(
            "app.modules.agent_recognition_review.ProviderSettings.from_config",
            return_value=settings,
        ), patch(
            "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
            return_value=model,
        ), patch(
            "app.modules.agent_recognition_review.TMDBScraper",
            return_value=_FakeScraper(),
        ):
            decision = asyncio.run(_review_async(payload))

        self.assertEqual(decision.status, "abstained")
        self.assertEqual(decision.reason_code, "candidate_title_mismatch")

    def test_sensitive_filename_never_reaches_external_model(self) -> None:
        payload = _review_payload()
        payload["files"][0]["name"] = (
            "测试动画.S01E01.sk-abcdefghijklmnop123456.mkv"
        )
        with patch(
            "app.modules.agent_recognition_review.ProviderSettings.from_config"
        ) as settings:
            decision = asyncio.run(_review_async(payload))

        self.assertEqual(decision.status, "abstained")
        self.assertEqual(decision.reason_code, "unsafe_input")
        settings.assert_not_called()


class RecognitionReviewQueueTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmation_delivery_outbox")
            conn.execute("DELETE FROM organize_confirmations")

    def _create_review(self, token: str = "review-token") -> None:
        db.create_organize_confirmation(
            token=token,
            fingerprint=f"fingerprint-{token}",
            chat_id="100",
            source_name="测试来源",
            directory_path="/动漫/测试动画",
            payload=_review_payload(),
            expires_at=(
                datetime.now(timezone.utc).astimezone() + timedelta(hours=1)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            review_requested=True,
            review_ready=True,
        )

    def test_cloud_confirmation_keeps_human_buttons_while_review_is_pending(self) -> None:
        group = {
            "source_dir_id": "source",
            "source_name": "下载",
            "directory": "/动漫/测试动画",
            "source_parent_id": "parent",
            "identity": "测试动画",
            "reason": "候选接近，需要复核",
            "files": [{
                "file_id": "file-1",
                "parent_id": "parent",
                "name": "测试动画.S01E01.mkv",
                "size": 1024,
                "season": 1,
                "episode": 1,
            }],
            "companions": [],
            "candidates": _review_payload()["candidates"],
        }
        with patch.object(
            organize_confirmations,
            "_recognition_review_is_enabled",
            return_value=True,
        ):
            actions = organize_confirmations.create_confirmation_actions(
                group,
                OrganizeRules(),
                source_name="下载",
                chat_id="100",
            )

        self.assertEqual(len(actions), 2)
        self.assertIn("测试动画", actions[0].label)
        self.assertEqual(actions[-1].label, "跳过此组")
        token = actions[0].callback_data.split(":")[1]
        row = db.get_organize_confirmation(token)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["review_status"], "waiting")
        self.assertEqual(row["confirmation_actor"], "")
        self.assertIsNone(db.claim_next_organize_confirmation_review())
        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            return_value=True,
        ):
            published = organize_confirmations.publish_confirmation_event(
                organize_confirmations.NotificationEvent(
                    "待确认",
                    actions=actions,
                ),
                chat_id="100",
            )
        self.assertTrue(published)
        self.assertEqual(
            db.get_organize_confirmation(token)["review_status"], "pending"
        )

    def test_failed_initial_notification_still_releases_review_to_web_fallback(self) -> None:
        group = {
            "source_dir_id": "source",
            "source_name": "下载",
            "directory": "/动漫/测试动画",
            "source_parent_id": "parent",
            "identity": "测试动画",
            "reason": "候选接近，需要复核",
            "files": [{
                "file_id": "file-1",
                "parent_id": "parent",
                "name": "测试动画.S01E01.mkv",
                "size": 1024,
                "season": 1,
                "episode": 1,
            }],
            "companions": [],
            "candidates": _review_payload()["candidates"],
        }
        with patch.object(
            organize_confirmations,
            "_recognition_review_is_enabled",
            return_value=True,
        ):
            actions = organize_confirmations.create_confirmation_actions(
                group,
                OrganizeRules(),
                source_name="下载",
                chat_id="100",
            )
        token = actions[0].callback_data.split(":")[1]

        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            return_value=False,
        ):
            published = organize_confirmations.publish_confirmation_event(
                organize_confirmations.NotificationEvent("待确认", actions=actions),
                chat_id="100",
            )

        self.assertFalse(published)
        self.assertEqual(
            db.get_organize_confirmation(token)["review_status"], "pending"
        )

    def test_review_activation_failure_does_not_break_human_notification(self) -> None:
        event = organize_confirmations.NotificationEvent(
            "待确认",
            actions=(
                organize_confirmations.NotificationAction(
                    "候选", "orgc:activation-failure:0"
                ),
            ),
        )
        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            return_value=True,
        ), patch.object(
            db,
            "activate_organize_confirmation_review",
            side_effect=RuntimeError("database busy"),
        ):
            self.assertTrue(
                organize_confirmations.publish_confirmation_event(
                    event, chat_id="100"
                )
            )

    def test_silent_local_confirmation_uses_same_review_queue(self) -> None:
        source_id = db.create_local_media_source(
            name="本地下载",
            qb_profile="",
            qb_path_prefix="",
            local_root="/downloads",
            owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id,
            "",
            "/downloads/测试动画.S01E01.mkv",
            owner="admin",
            trigger="scan",
        )
        db.update_local_media_task(
            task_id,
            owner="admin",
            status="requires_manual",
            snapshot_digest="digest-1",
            rules_snapshot="{}",
            error="候选接近，需要复核",
        )
        task = db.get_local_media_task(task_id, owner="admin")
        source = db.get_local_media_source(source_id, owner="admin")
        preview = {
            "reason": "候选接近，需要复核",
            "snapshot_digest": "digest-1",
            "rules_snapshot": "{}",
            "files": [{"name": "测试动画.S01E01.mkv"}],
            "candidates": _review_payload()["candidates"],
        }
        with patch.object(
            organize_confirmations,
            "_recognition_review_is_enabled",
            return_value=True,
        ):
            scheduled = organize_confirmations.schedule_local_media_recognition_review(
                task,
                source,
                preview,
                owner="admin",
            )

        self.assertTrue(scheduled)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM organize_confirmations ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["review_status"], "pending")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["kind"], "local_media")
        self.assertEqual(payload["local_task_id"], task_id)
        self.assertTrue(payload["_notification_suppressed"])
        self.assertNotIn("/downloads/", row["payload_json"])

    def test_silent_review_expiry_does_not_create_telegram_delivery(self) -> None:
        payload = {**_review_payload(), "_notification_suppressed": True}
        db.create_organize_confirmation(
            token="silent-expired",
            fingerprint="silent-expired-fingerprint",
            chat_id="100",
            source_name="测试来源",
            directory_path="/动漫/测试动画",
            payload=payload,
            expires_at="2000-01-01 00:00:00",
            review_requested=True,
            review_ready=True,
        )
        expired_row = db.get_organize_confirmation("silent-expired")

        with patch.object(
            organize_confirmations, "_dispatch_due_confirmation_delivery"
        ) as dispatch:
            self.assertTrue(
                organize_confirmations._publish_expired_confirmation(expired_row)
            )

        current = db.get_organize_confirmation("silent-expired")
        self.assertEqual(current["status"], "expired")
        dispatch.assert_not_called()
        with db.get_conn() as conn:
            deliveries = conn.execute(
                "SELECT COUNT(*) FROM organize_confirmation_delivery_outbox "
                "WHERE confirmation_token='silent-expired'"
            ).fetchone()[0]
        self.assertEqual(deliveries, 0)

    def test_agent_approval_claims_same_confirmation_once(self) -> None:
        self._create_review()
        row = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(row)
        decision = RecognitionReviewDecision(
            status="approved",
            candidate_index=0,
            confidence=0.98,
            reason_code="verified",
            summary="证据一致",
            model="test-model",
            tool_calls=4,
            duration_ms=12,
        )
        with patch.object(
            organize_confirmations,
            "_recognition_review_is_enabled",
            return_value=True,
        ), patch(
            "app.modules.agent_recognition_review.review_confirmation_payload",
            return_value=decision,
        ), patch.object(
            organize_confirmations,
            "_dispatch_next_queued_confirmation",
            return_value={"ok": False, "idle": True},
        ), patch.object(
            organize_confirmations,
            "wake_confirmation_dispatcher",
            return_value=False,
        ):
            status = organize_confirmations._process_recognition_review_row(row)

        current = db.get_organize_confirmation("review-token")
        self.assertEqual(status, "approved")
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["review_status"], "approved")
        self.assertEqual(current["confirmation_actor"], "agent")
        audit = json.loads(current["review_result_json"])
        self.assertEqual(audit["reason_code"], "verified")
        self.assertNotIn("summary", audit)
        self.assertNotIn("payload", audit)

    def test_human_claim_cancels_running_agent_review(self) -> None:
        self._create_review()
        row = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(row)

        claimed = db.claim_organize_confirmation(
            "review-token", chat_id="100", selected_index=0, actor="human"
        )

        self.assertEqual(claimed["confirmation_actor"], "human")
        self.assertEqual(claimed["review_status"], "cancelled")
        self.assertFalse(
            db.stage_organize_confirmation_review_result(
                "review-token", {"reason_code": "too_late"}
            )
        )

    def test_disabling_review_during_model_run_preserves_human_confirmation(self) -> None:
        self._create_review()
        row = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(row)
        decision = RecognitionReviewDecision(
            status="approved",
            candidate_index=0,
            confidence=0.98,
            reason_code="verified",
            summary="证据一致",
            model="test-model",
            tool_calls=4,
            duration_ms=12,
        )
        with patch.object(
            organize_confirmations,
            "_recognition_review_is_enabled",
            side_effect=(True, False),
        ), patch(
            "app.modules.agent_recognition_review.review_confirmation_payload",
            return_value=decision,
        ), patch.object(
            organize_confirmations,
            "start_confirmation",
        ) as start:
            status = organize_confirmations._process_recognition_review_row(row)

        current = db.get_organize_confirmation("review-token")
        self.assertEqual(status, "cancelled")
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["review_status"], "cancelled")
        self.assertEqual(current["confirmation_actor"], "")
        start.assert_not_called()

    def test_superseding_card_cancels_running_review(self) -> None:
        self._create_review()
        running = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(running)

        db.create_organize_confirmation(
            token="replacement-token",
            fingerprint="fingerprint-review-token",
            chat_id="100",
            source_name="测试来源",
            directory_path="/动漫/测试动画",
            payload=_review_payload(),
            expires_at=(
                datetime.now(timezone.utc).astimezone() + timedelta(hours=1)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            review_requested=True,
            review_ready=True,
        )

        old = db.get_organize_confirmation("review-token")
        replacement = db.get_organize_confirmation("replacement-token")
        self.assertEqual(old["status"], "expired")
        self.assertEqual(old["review_status"], "cancelled")
        self.assertTrue(old["review_completed_at"])
        self.assertEqual(replacement["status"], "pending")
        self.assertEqual(replacement["review_status"], "pending")

    def test_cancelling_card_cancels_running_review(self) -> None:
        self._create_review()
        running = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(running)

        cancelled = db.cancel_organize_confirmation(
            "review-token",
            chat_id="100",
            enqueue_delivery=False,
        )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["review_status"], "cancelled")
        self.assertTrue(cancelled["review_completed_at"])

    def test_interrupted_review_recovers_twice_then_falls_back_to_human(self) -> None:
        self._create_review()
        first = db.claim_next_organize_confirmation_review()
        self.assertEqual(first["review_attempts"], 1)
        self.assertEqual(db.recover_interrupted_organize_confirmation_reviews(), 1)
        second = db.claim_next_organize_confirmation_review()
        self.assertEqual(second["review_attempts"], 2)
        self.assertEqual(db.recover_interrupted_organize_confirmation_reviews(), 1)
        third = db.claim_next_organize_confirmation_review()
        self.assertEqual(third["review_attempts"], 3)
        self.assertEqual(db.recover_interrupted_organize_confirmation_reviews(), 1)

        current = db.get_organize_confirmation("review-token")
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["review_status"], "failed")
        self.assertIsNone(db.claim_next_organize_confirmation_review())
