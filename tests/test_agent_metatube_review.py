"""MetaTube 冲突复核只能按冻结身份读取证据，并在无法消歧时弃权。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests  # noqa: F401  # 必须先隔离应用运行目录与配置。
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.modules.agent_recognition_review import RecognitionReviewDecision, _review_async
from app.modules.nsfw import MetaTubeClient
from tests.test_agent_recognition_review import _ScriptedModel, _tool_round


def _metadata(provider="javbus", item_id="abc123", year="2023", **changes):
    return {
        "provider": provider, "id": item_id, "number": "ABC-123",
        "title": "测试作品", "release_date": f"{year}-01-02T00:00:00Z",
        "actors": ["演员甲"], "genres": ["剧情"], "maker": "制作商甲",
        "label": "厂牌甲", "director": "导演甲", "series": "系列甲",
        **changes,
    }


def _payload(*metadata, name="ABC-123.mp4"):
    metadata = metadata or (_metadata(), _metadata("javdb", "other", "2024"))
    return {
        "kind": "guangya", "directory": "/private/待整理", "identity": "ABC-123",
        "reason": "同一番号存在相互冲突的元数据，请人工选择",
        "rules": {"nsfw_exclusive": True, "nsfw_strip_domains": ""},
        "files": [{"file_id": "file-1", "name": name, "size": 1024}],
        # 生产冻结候选没有原始 metadata；详情必须由只读接口重新读取。
        "candidates": [
            {"provider": "metatube", "external_id": f"{item['provider']}:{item['id']}",
             "tmdb_id": "", "media_type": "movie", "score": 1.0,
             "title": f"{item['number']} {item['title']}",
             "year": item["release_date"][:4]}
            for item in metadata
        ],
    }


def _model(*, selected=0, inspect_indices=(0, 1), inspect_case=True, decision="approve"):
    rounds = [_tool_round("case", "recognition.inspect_case", {})] if inspect_case else []
    rounds.extend(
        _tool_round(f"candidate-{index}", "recognition.inspect_candidate",
                    {"candidate_index": index})
        for index in inspect_indices
    )
    rounds.append(_tool_round("decision", "recognition.propose_review_decision", {
        "decision": decision, "candidate_index": selected, "confidence": 0.99,
        "reason_code": "evidence_consistent", "summary": "按只读证据提交复核建议",
    }))
    rounds.append([ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
    return _ScriptedModel(rounds)


def _tool_data(model, name):
    return [json.loads(message.content)["data"]
            for message in model.requests[-1].messages
            if message.tool_name == name and "data" in json.loads(message.content)]


class MetaTubeReviewTests(unittest.TestCase):
    def run_review(self, payload, metadata, model=None, *, failure_path="", responses=None):
        model = model or _model()
        records = {f"/v1/movies/{item['provider']}/{item['id']}": item
                   for item in metadata}
        records.update(responses or {})
        session = Mock()

        def get(url, **kwargs):
            path = url.removeprefix("http://metatube.invalid")
            if path == failure_path:
                import requests
                raise requests.Timeout("upstream access_token=upstream-secret")
            if path not in records:
                raise AssertionError(f"非冻结身份读取: {path}")
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"data": copy.deepcopy(records[path])})

        session.get.side_effect = get
        client = MetaTubeClient("http://metatube.invalid", session=session)
        settings = SimpleNamespace(model="test-model", timeout_seconds=1)
        values = {
            "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT": "http://metatube.invalid",
            "GY_ORGANIZE_NSFW_METATUBE_TOKEN": "server-private-secret",
        }
        with patch("app.modules.agent_recognition_review.ProviderSettings.from_config",
                   return_value=settings), patch(
            "app.modules.agent_recognition_review.OpenAICompatibleModelAdapter", return_value=model,
        ), patch("app.modules.agent_recognition_review.MetaTubeClient", return_value=client) as factory, patch(
            "app.modules.agent_recognition_review.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.modules.agent_recognition_review.TMDBScraper") as tmdb, patch.object(
            client, "close", wraps=client.close,
        ) as close:
            result = asyncio.run(_review_async(payload))
        tmdb.assert_not_called()
        if factory.called:
            close.assert_called_once()
        return result, model, session, factory

    def test_source_year_selects_nonfirst_candidate_and_exposes_structured_details(self):
        records = [_metadata(), _metadata("javdb", "other", "2024")]
        payload = _payload(*records, name="ABC-123 (2024).mp4")
        before = copy.deepcopy(payload)
        result, model, session, factory = self.run_review(payload, records, _model(selected=1))
        self.assertTrue(result.approved, result)
        self.assertEqual(result.candidate_index, 1)
        self.assertEqual(payload, before)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(factory.call_args.args[:2],
                         ("http://metatube.invalid", "server-private-secret"))
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["params"], {"lazy": "true"})
        case = _tool_data(model, "recognition.inspect_case")[0]
        self.assertEqual([item["external_id"] for item in case["candidates"]],
                         ["javbus:abc123", "javdb:other"])
        details = _tool_data(model, "recognition.inspect_candidate")
        self.assertEqual(details[1]["number"], "ABC-123")
        self.assertEqual(details[1]["release_date"], "2024-01-02")
        self.assertEqual(details[1]["actors"], ["演员甲"])
        self.assertEqual(details[1]["maker"], "制作商甲")
        visible = str(model.requests)
        for private in ("/private/", "server-private-secret", "file-1", "metatube.invalid"):
            self.assertNotIn(private, visible)

    def test_same_number_and_perfect_scores_do_not_resolve_year_conflict(self):
        records = [_metadata(), _metadata("javdb", "other", "2024")]
        result, *_ = self.run_review(_payload(*records), records)
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.reason_code, "metatube_conflict_unresolved")
        self.assertIsNone(result.candidate_index)

    def test_source_actor_can_disambiguate_different_years(self):
        records = [_metadata(), _metadata("javdb", "other", "2024", actors=["演员乙"])]
        result, *_ = self.run_review(_payload(*records, name="ABC-123 演员乙.mp4"),
                                    records, _model(selected=1))
        self.assertTrue(result.approved, result)
        self.assertEqual(result.candidate_index, 1)

    def test_source_date_distinguishes_same_year_releases(self):
        records = [_metadata(year="2024"), _metadata("javdb", "other", "2024",
                                                    release_date="2024-02-03")]
        result, *_ = self.run_review(_payload(*records, name="ABC-123.2024-02-03.mp4"),
                                    records, _model(selected=1))
        self.assertTrue(result.approved, result)

    def test_all_three_frozen_candidates_can_be_reviewed_in_existing_budget(self):
        records = [_metadata(), _metadata("javdb", "other", "2024"),
                   _metadata("duga", "third", "2025")]
        result, _, session, _ = self.run_review(
            _payload(*records, name="ABC-123 (2025).mp4"), records,
            _model(selected=2, inspect_indices=(0, 1, 2)),
        )
        self.assertTrue(result.approved, result)
        self.assertEqual(result.candidate_index, 2)
        self.assertEqual(session.get.call_count, 3)

    def test_repeated_inspection_reuses_one_frozen_detail(self):
        records = [_metadata()]
        result, _, session, _ = self.run_review(_payload(*records), records,
                                               _model(inspect_indices=(0, 0)))
        self.assertTrue(result.approved, result)
        session.get.assert_called_once()

    def test_parent_number_can_identify_numberless_parts(self):
        records = [_metadata()]
        payload = _payload(*records, name="part1.mp4")
        payload["directory"] = "/待整理/ABC-123"
        result, *_ = self.run_review(payload, records, _model(inspect_indices=(0,)))
        self.assertTrue(result.approved, result)

    def test_year_inside_number_is_not_release_evidence(self):
        records = [_metadata(number="T38-1999", year="2024")]
        result, model, *_ = self.run_review(_payload(*records, name="T38-1999.mp4"),
                                           records, _model(inspect_indices=(0,)))
        self.assertTrue(result.approved, result)
        case = _tool_data(model, "recognition.inspect_case")[0]
        self.assertEqual(case["metatube_source"]["number"], "T38-1999")
        self.assertEqual(case["metatube_source"]["years"], [])

    def test_malformed_frozen_candidates_are_not_filtered_into_new_indices(self):
        payload = _payload(_metadata())
        for candidates in ([None, *payload["candidates"]], payload["candidates"] * 4):
            with self.subTest(candidates=candidates):
                result, model, session, _ = self.run_review(
                    {**payload, "candidates": candidates}, [_metadata()],
                    _model(inspect_indices=(0,)),
                )
                self.assertEqual(result.reason_code, "invalid_frozen_candidates")
                self.assertFalse(model.requests)
                session.get.assert_not_called()

    def test_changed_frozen_endpoint_does_not_send_current_token_elsewhere(self):
        records = [_metadata()]
        payload = _payload(*records)
        payload["rules"]["nsfw_metatube_endpoint"] = "http://other.invalid"
        result, _, session, factory = self.run_review(payload, records,
                                                     _model(inspect_indices=(0,)))
        self.assertEqual(result.status, "abstained")
        factory.assert_not_called()
        session.get.assert_not_called()

    def test_incomplete_metatube_identity_remains_unsupported(self):
        records = [_metadata()]
        for external_id in ("ABC-123", ":abc123", "javbus:"):
            with self.subTest(external_id=external_id):
                payload = _payload(*records)
                payload["candidates"][0]["external_id"] = external_id
                result, _, session, factory = self.run_review(
                    payload, records, _model(inspect_indices=(0,)),
                )
                self.assertEqual(result.reason_code, "provider_not_revalidated")
                factory.assert_not_called()
                session.get.assert_not_called()

    def test_missing_or_conflicting_source_numbers_never_approve(self):
        records = [_metadata()]
        cases = [
            ("unknown.mp4", "/待整理"),
            ("ABC-123 XYZ-456.mp4", "/待整理"),
            ("XYZ-456.mp4", "/ABC-123"),
        ]
        for name, directory in cases:
            with self.subTest(name=name, directory=directory):
                payload = _payload(*records, name=name)
                payload["directory"] = directory
                result, *_ = self.run_review(payload, records, _model(inspect_indices=(0,)))
                self.assertFalse(result.approved, result)

    def test_every_file_must_belong_to_source_number(self):
        records = [_metadata()]
        payload = _payload(*records)
        payload["files"].append({"name": "XYZ-456.mp4", "size": 2048})
        result, *_ = self.run_review(payload, records, _model(inspect_indices=(0,)))
        self.assertFalse(result.approved, result)

    def test_detail_requires_exact_frozen_provider_id_and_explicit_number(self):
        original = _metadata()
        for changes in ({"provider": "javdb"}, {"id": "different"},
                        {"number": "XYZ-456"}, {"number": ""}):
            with self.subTest(changes=changes):
                payload = _payload(original)
                model = _model(inspect_indices=(0,))
                record = {**original, **changes}
                result, *_ = self.run_review(payload, [original], model,
                    responses={"/v1/movies/javbus/abc123": record})
                self.assertFalse(result.approved, result)

    def test_frozen_year_cannot_drift_to_live_year(self):
        records = [_metadata()]
        payload = _payload(*records)
        payload["candidates"][0]["year"] = "2024"
        result, *_ = self.run_review(payload, records, _model(inspect_indices=(0,)))
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.reason_code, "metatube_snapshot_mismatch")

    def test_unread_or_unavailable_rival_keeps_case_pending(self):
        records = [_metadata(), _metadata("javdb", "other", "2024")]
        payload = _payload(*records, name="ABC-123 (2023).mp4")
        for indices, failure in (((0,), ""), ((0, 1), "/v1/movies/javdb/other")):
            with self.subTest(indices=indices, failure=failure):
                result, model, *_ = self.run_review(payload, records,
                    _model(inspect_indices=indices), failure_path=failure)
                self.assertEqual(result.status, "abstained")
                self.assertNotIn("upstream-secret", str(model.requests))

    def test_number_and_title_without_corroboration_are_insufficient(self):
        records = [_metadata(release_date="", actors=[], maker="")]
        payload = _payload(*records)
        result, *_ = self.run_review(payload, records, _model(inspect_indices=(0,)))
        self.assertEqual(result.status, "abstained")

    def test_actor_does_not_override_conflicting_source_year(self):
        records = [_metadata(), _metadata("javdb", "other", "2024", actors=["演员乙"])]
        result, *_ = self.run_review(_payload(*records, name="ABC-123 (2023) 演员乙.mp4"),
                                    records, _model(selected=1))
        self.assertEqual(result.status, "abstained")

    def test_missing_or_truncated_rival_evidence_does_not_disprove_the_rival(self):
        selected = _metadata("javdb", "other", "2024", actors=["演员乙"])
        cases = [
            (_metadata(actors=[]), "ABC-123 演员乙.mp4"),
            (_metadata(release_date=""), "ABC-123 (2024).mp4"),
            (_metadata(actors=[f"群演{i}" for i in range(20)] + ["演员乙"]),
             "ABC-123 演员乙.mp4"),
        ]
        for rival, name in cases:
            with self.subTest(name=name, rival=rival):
                records = [rival, selected]
                result, *_ = self.run_review(_payload(*records, name=name), records,
                                            _model(selected=1))
                self.assertEqual(result.status, "abstained")
                self.assertEqual(result.reason_code, "metatube_conflict_unresolved")

    def test_selected_candidate_must_positively_verify_source_date(self):
        records = [_metadata(release_date="", actors=["演员甲"])]
        result, *_ = self.run_review(_payload(*records, name="ABC-123 (2024) 演员甲.mp4"),
                                    records, _model(inspect_indices=(0,)))
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.reason_code, "metatube_evidence_insufficient")

    def test_reading_detail_is_required_even_for_single_exact_candidate(self):
        records = [_metadata()]
        result, _, session, _ = self.run_review(_payload(*records), records,
                                               _model(inspect_indices=()))
        self.assertEqual(result.status, "abstained")
        session.get.assert_not_called()

    def test_model_can_abstain_and_cannot_select_unfrozen_index(self):
        records = [_metadata()]
        for decision, index in (("abstain", -1), ("approve", 1)):
            with self.subTest(decision=decision, index=index):
                result, *_ = self.run_review(_payload(*records), records,
                    _model(selected=index, inspect_indices=(0,), decision=decision))
                self.assertFalse(result.approved, result)

    def test_sensitive_live_and_frozen_metadata_never_reach_model(self):
        secret = "sk-abcdefghijklmnop123456"
        records = [_metadata(actors=[secret])]
        result, model, *_ = self.run_review(_payload(*records), records,
                                           _model(inspect_indices=(0,)))
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.evidence, [])
        self.assertNotIn(secret, str(model.requests))
        payload = _payload(_metadata())
        payload["candidates"][0]["metadata"] = records[0]
        result, model, session, _ = self.run_review(payload, records,
                                                  _model(inspect_indices=(0,)))
        self.assertEqual(result.reason_code, "unsafe_input")
        self.assertFalse(model.requests)
        session.get.assert_not_called()

    def test_decision_audits_only_safe_candidate_bound_metatube_evidence(self):
        records = [
            _metadata(homepage="https://example.invalid/?access_token=upstream-secret",
                      raw_secret="server-private-secret"),
            _metadata("javdb", "other", "2024"),
        ]
        result, *_ = self.run_review(_payload(*records, name="ABC-123 (2024).mp4"),
                                    records, _model(selected=1))
        self.assertTrue(result.approved, result)
        self.assertEqual([item["candidate_index"] for item in result.evidence], [0, 1])
        evidence = result.audit_payload()["evidence"]
        self.assertEqual(evidence, result.evidence)
        self.assertEqual(evidence[1]["external_id"], "javdb:other")
        self.assertEqual(evidence[1]["number"], "ABC-123")
        self.assertEqual(evidence[1]["year"], "2024")
        self.assertEqual(evidence[1]["release_date"], "2024-01-02")
        self.assertEqual(evidence[1]["actors"], ["演员甲"])
        self.assertEqual(evidence[1]["maker"], "制作商甲")
        self.assertTrue(evidence[1]["frozen_identity_matches"])
        self.assertTrue(evidence[1]["source_number_matches"])
        for private in ("http", "raw_secret", "upstream-secret", "server-private-secret",
                        "title", "summary", "private", "file-1"):
            self.assertNotIn(private, json.dumps(evidence))
        # audit_payload 返回独立快照，不让公共展示修改内存决策的证据。
        evidence[0]["actors"].append("外部修改")
        self.assertNotIn("外部修改", result.evidence[0]["actors"])

    def test_abstention_and_missing_decision_keep_already_read_evidence(self):
        records = [_metadata(), _metadata("javdb", "other", "2024")]
        result, *_ = self.run_review(_payload(*records), records)
        self.assertEqual(result.status, "abstained")
        self.assertEqual(len(result.audit_payload()["evidence"]), 2)
        model = _ScriptedModel([
            _tool_round("case", "recognition.inspect_case", {}),
            _tool_round("candidate", "recognition.inspect_candidate", {"candidate_index": 1}),
            [ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        result, *_ = self.run_review(_payload(*records), records, model)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "model_no_decision")
        self.assertEqual([item["candidate_index"] for item in result.evidence], [1])

    def test_identity_mismatch_is_visible_in_safe_evidence(self):
        records = [_metadata()]
        result, *_ = self.run_review(
            _payload(*records), records, _model(inspect_indices=(0,)),
            responses={"/v1/movies/javbus/abc123": _metadata(item_id="other")},
        )
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.evidence[0]["candidate_index"], 0)
        self.assertFalse(result.evidence[0]["frozen_identity_matches"])

    def test_empty_evidence_preserves_legacy_audit_shape_and_independent_defaults(self):
        first = RecognitionReviewDecision(status="abstained")
        second = RecognitionReviewDecision(status="approved", candidate_index=0,
                                           entry_mode="clean_title")
        self.assertEqual(first.evidence, [])
        self.assertNotIn("evidence", first.audit_payload())
        self.assertNotIn("evidence", second.audit_payload())
        first.evidence.append({"candidate_index": 0, "number": "ABC-123"})
        self.assertEqual(second.evidence, [])
