"""光鸭清洗复核只提出建议，绝不把无元数据归档冒充识别成功。"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.kernel.model import ModelEvent, ModelEventType
from app.modules.agent_recognition_review import _review_async
from app.modules.nsfw import build_clean_title_candidate
from app.modules.nsfw_clean_review import (
    inspect_nsfw_clean_candidate,
    nsfw_clean_review_enabled,
)
from tests.support import IsolatedDatabaseTestCase
from tests.test_agent_recognition_review import _ScriptedModel, _tool_round


def clean_payload(*names: str) -> dict:
    names = names or ("ABC-123.mp4",)
    return {
        "kind": "guangya",
        "source_dir_id": "source",
        "source_parent_id": "parent",
        "directory": "/待整理",
        "identity": "ABC-123",
        "reason": "没有元数据",
        "rules": {"nsfw_exclusive": True, "nsfw_strip_domains": ""},
        "files": [
            {"name": name, "file_id": f"file-{i}", "parent_id": "parent", "size": 1024}
            for i, name in enumerate(names)
        ],
        "companions": [],
        "candidates": [build_clean_title_candidate(names[0])],
    }


def approval_model(
    *, inspect_case=True, inspect_candidate=True, confidence=0.99, decision="approve"
):
    rounds = []
    if inspect_case:
        rounds.append(_tool_round("case", "recognition.inspect_case", {}))
    if inspect_candidate:
        rounds.append(
            _tool_round(
                "candidate", "recognition.inspect_candidate", {"candidate_index": 0}
            )
        )
    rounds.append(
        _tool_round(
            "decide",
            "recognition.propose_review_decision",
            {
                "decision": decision,
                "candidate_index": 0 if decision == "approve" else -1,
                "confidence": confidence,
                "reason_code": "clear_identity",
                "summary": "只核对清洗变换，不补造元数据",
            },
        )
    )
    rounds.append(
        [
            ModelEvent(ModelEventType.TEXT_DELTA, text="复核完成。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
        ]
    )
    return _ScriptedModel(rounds)


class CleanEvidenceTests(IsolatedDatabaseTestCase):
    def inspect(self, payload):
        return inspect_nsfw_clean_candidate(payload, payload["candidates"][0])

    def test_subscope_defaults_off(self):
        with patch(
            "app.modules.nsfw_clean_review.config.get_bool", return_value=False
        ) as read:
            self.assertFalse(nsfw_clean_review_enabled())
        read.assert_called_once_with("AGENT_NSFW_CLEAN_REVIEW_ENABLED", False)

    def test_explicit_single_and_numbered_parts_have_server_generated_preview(self):
        for names in [
            ("ABC-123.mp4",),
            ("ABC-123-CD1.mp4", "ABC-123-CD2.mp4"),
            ("FC2-PPV-1234567.mp4",),
        ]:
            with self.subTest(names=names):
                result = self.inspect(clean_payload(*names))
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["data"]["metadata_verified"])
                self.assertEqual(result["data"]["entry_mode"], "clean_title")
                self.assertEqual(len(result["data"]["files"]), len(names))

    def test_alphanumeric_prefix_parts_keep_clean_evidence_and_no_metadata_boundary(self):
        cases = [
            (("300MIUM-1474.mp4",), "300MIUM-1474"),
            (
                ("300MIUM-1474-CD1.mp4", "300MIUM-1474-CD2.mp4"),
                "300MIUM-1474",
            ),
            (("200GANA-3419-1.mp4",), "200GANA-3419"),
            (("4k688.com@T38-073.mp4",), "T38-073"),
            (("hhd800.com@SPSE-91.mp4",), "SPSE-91"),
            (("hhd800.com@C-2962.mp4",), "C-2962"),
            (("091926-001-carib.mp4",), "CARIB-091926-001"),
            (("4k688.com@T38-073-CD1.mp4", "T38-073-CD2.mp4"), "T38-073"),
        ]
        for names, expected in cases:
            with self.subTest(names=names):
                result = self.inspect(clean_payload(*names))
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["data"]["metadata_verified"])
                self.assertEqual(result["data"]["entry_mode"], "clean_title")
                self.assertEqual(result["data"]["number"], expected)

    def test_wrong_scope_forged_candidate_and_ambiguous_identity_are_rejected(self):
        cases = []

        def altered(change):
            p = clean_payload()
            change(p)
            cases.append(p)

        altered(lambda p: p.update(kind="local_media"))
        altered(lambda p: p["rules"].update(nsfw_exclusive=False))
        altered(lambda p: p["rules"].update(nsfw_exclusive="true"))
        altered(lambda p: p["candidates"][0].update(provider="metatube"))
        altered(lambda p: p["candidates"][0].update(external_id="XYZ-456"))
        altered(lambda p: p["candidates"][0].update(title="伪造标题"))
        altered(lambda p: p["candidates"][0].update(year="2026"))
        altered(lambda p: p["files"][0].update(name="无番号.mp4"))
        altered(lambda p: p["files"][0].update(name="ABC-123 XYZ-456.mp4"))
        altered(lambda p: p["files"][0].update(name="/internal/ABC-123.mp4"))
        altered(lambda p: p.update(multipart_strategy="sequence"))
        altered(lambda p: p.update(companions=[{"name": "XYZ-456.srt"}]))
        for p in cases:
            with self.subTest(payload=p):
                self.assertFalse(self.inspect(p)["ok"])
        for names in [
            ("ABC-123-A.mp4",),
            ("ABC-123.mp4", "ABC-123.mkv"),
            ("ABC-123-CD1.mp4", "ABC-123-CD1.mkv"),
            ("ABC-123-CD1.mp4", "XYZ-456-CD2.mp4"),
        ]:
            with self.subTest(names=names):
                self.assertFalse(self.inspect(clean_payload(*names))["ok"])

    def test_generic_companions_are_allowed_without_inventing_identity(self):
        p = clean_payload()
        p["companions"] = [{"name": "poster.jpg"}, {"name": "ABC-123.zh.srt"}]
        self.assertTrue(self.inspect(p)["ok"])


class CleanKernelReviewTests(IsolatedDatabaseTestCase):
    def run_review(self, payload=None, model=None, *, enabled=True):
        payload = payload or clean_payload()
        model = model or approval_model()
        original = copy.deepcopy(payload)
        with (
            patch(
                "app.modules.agent_recognition_review.nsfw_clean_review_enabled",
                return_value=enabled,
            ),
            patch(
                "app.modules.agent_recognition_review.ProviderSettings.from_config",
                return_value=SimpleNamespace(model="offline-test", timeout_seconds=1),
            ),
            patch(
                "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
                return_value=model,
            ),
            patch("app.modules.agent_recognition_review.TMDBScraper") as scraper,
        ):
            result = asyncio.run(_review_async(payload))
        self.assertEqual(payload, original)
        scraper.assert_not_called()
        return result, model

    def test_approved_suggestion_uses_same_readonly_kernel_without_tmdb_or_durable_chat(
        self,
    ):
        result, model = self.run_review()
        self.assertTrue(result.approved)
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(result.audit_payload()["entry_mode"], "clean_title")
        self.assertNotIn("summary", result.audit_payload())
        self.assertEqual(len(model.requests), 4)
        names = {tool["name"].replace("__", ".") for tool in model.requests[0].tools}
        self.assertEqual(
            names,
            {
                "recognition.inspect_case",
                "recognition.inspect_candidate",
                "recognition.inspect_season",
                "recognition.propose_review_decision",
            },
        )
        content = str(model.requests[2].messages)
        self.assertIn("metadata_verified", content)
        self.assertNotIn('"file_id"', content)

    def test_default_off_and_local_scope_never_call_model(self):
        result, model = self.run_review(enabled=False)
        self.assertFalse(result.approved)
        self.assertFalse(model.requests)
        p = clean_payload()
        p["kind"] = "local_media"
        result, model = self.run_review(p)
        self.assertFalse(result.approved)
        self.assertFalse(model.requests)

    def test_numeric_prefix_clean_review_stays_manual_when_subscope_is_off(self):
        payload = clean_payload("300MIUM-1474.mp4")
        result, model = self.run_review(payload, enabled=False)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason_code, "nsfw_clean_not_authorized")
        self.assertFalse(model.requests)

        result, model = self.run_review(payload, enabled=True)
        self.assertTrue(result.approved)
        self.assertEqual(result.entry_mode, "clean_title")
        self.assertTrue(model.requests)

    def test_skipped_evidence_low_confidence_and_abstention_preserve_manual(self):
        for kwargs in [
            {"inspect_case": False},
            {"inspect_candidate": False},
            {"confidence": 0.3},
            {"decision": "abstain"},
        ]:
            with self.subTest(kwargs=kwargs):
                result, _ = self.run_review(model=approval_model(**kwargs))
                self.assertFalse(result.approved)

    def test_metatube_is_still_not_supported(self):
        p = clean_payload()
        p["candidates"][0]["provider"] = "metatube"
        result, _ = self.run_review(p)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason_code, "provider_not_revalidated")

    def test_subscope_revoked_after_read_is_rechecked_before_approval(self):
        model = approval_model()
        # 初始、inspect_candidate允许；最终建议落地前已关闭。
        with (
            patch(
                "app.modules.agent_recognition_review.nsfw_clean_review_enabled",
                side_effect=[True, True, False],
            ),
            patch(
                "app.modules.agent_recognition_review.ProviderSettings.from_config",
                return_value=SimpleNamespace(model="test", timeout_seconds=1),
            ),
            patch(
                "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter",
                return_value=model,
            ),
        ):
            result = asyncio.run(_review_async(clean_payload()))
        self.assertFalse(result.approved)
        self.assertEqual(result.reason_code, "nsfw_clean_not_authorized")
