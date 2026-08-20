"""可选动漫解析器的降级、证据与影子决策门测试。"""
from __future__ import annotations

import builtins
import unittest
from unittest.mock import patch

from app.modules.recognition.evaluation import (
    compare_shadow_case,
    merge_shadow_projection,
    project_anime_evidence,
    summarize_shadow_evaluation,
)
from app.modules.recognition.extractors import anime_adapter
from app.modules.recognition.models import ReleaseParseEvidence


class _Kind:
    def __init__(self, name: str):
        self.name = name


class _Item:
    def __init__(self, kind: str, value: str):
        self.kind = _Kind(kind)
        self.value = value


class AnimeAdapterTests(unittest.TestCase):
    def setUp(self):
        anime_adapter._parse_anitomy_ng.cache_clear()

    def test_missing_optional_dependency_returns_empty_evidence(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "anitomy_ng":
                raise ImportError("optional parser unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            self.assertEqual(anime_adapter.parse_anime_evidence("Demo - 01.mkv"), ())

    def test_adapter_only_emits_normalized_evidence(self):
        parsed = (
            _Item("TITLE", "Toradora!"),
            _Item("YEAR", "2008"),
            _Item("EPISODE", "01"),
            _Item("TYPE", "OVA"),
            _Item("VIDEO_RESOLUTION", "1920x1080"),
        )
        with patch.object(anime_adapter, "_parse_anitomy_ng", return_value=tuple(
            (item.kind.name, item.value) for item in parsed
        )):
            evidence = anime_adapter.parse_anime_evidence("[Group] Toradora! - 01 OVA.mkv")

        self.assertEqual(
            [(item.kind, item.value) for item in evidence],
            [("title", "Toradora!"), ("year", "2008"), ("episode", 1), ("special_type", "OVA")],
        )
        self.assertTrue(all(item.source == "anitomy_ng" for item in evidence))

    def test_parser_failure_is_fail_closed(self):
        with patch.object(anime_adapter, "_parse_anitomy_ng", side_effect=RuntimeError("bad input")):
            self.assertEqual(anime_adapter.parse_anime_evidence("bad.mkv"), ())


class AnimeCorpusSliceTests(unittest.TestCase):
    def test_corpus_has_explicit_positive_and_hard_negative_slices(self):
        from pathlib import Path

        from tests.recognition_eval_helpers import load_release_recognition_cases

        fixture = Path(__file__).parent / "fixtures" / "release_recognition_cases.jsonl"
        cases = load_release_recognition_cases(fixture)
        anime = [case for case in cases if "anime" in case.tags]
        negatives = [case for case in cases if "anime-negative" in case.tags]
        self.assertGreaterEqual(len(anime), 30)
        self.assertGreaterEqual(len(negatives), 10)
        self.assertTrue(all("anime-negative" not in case.tags for case in anime))


class AnimeShadowEvaluationTests(unittest.TestCase):
    def test_projection_rejects_conflicting_fields_and_maps_specials(self):
        evidence = (
            ReleaseParseEvidence("title", "anitomy_ng", "Demo", 0.9),
            ReleaseParseEvidence("episode", "anitomy_ng", 1, 0.98),
            ReleaseParseEvidence("episode", "anitomy_ng", 2, 0.98),
            ReleaseParseEvidence("special_type", "anitomy_ng", "OVA", 0.95),
        )
        projection = project_anime_evidence(evidence)
        self.assertEqual(projection["title"], "Demo")
        self.assertNotIn("episode", projection)
        self.assertEqual(projection["season"], 0)

    def test_shadow_merge_does_not_mutate_baseline(self):
        baseline = {"title": "Old", "year": "", "media_type": "tv", "season": None, "episode": 1}
        evidence = (ReleaseParseEvidence("title", "anitomy_ng", "New", 0.9),)
        experiment = merge_shadow_projection(baseline, evidence)
        self.assertEqual(baseline["title"], "Old")
        self.assertEqual(experiment["title"], "New")


    def test_installed_adapter_is_measured_against_real_corpus_before_promotion(self):
        import importlib.util
        from pathlib import Path

        if importlib.util.find_spec("anitomy_ng") is None:
            self.skipTest("安装 requirements-anime-eval.txt 后执行真实影子评估")

        from app.modules.scraper import extract_recognition_context
        from tests.recognition_eval_helpers import load_release_recognition_cases

        fixture = Path(__file__).parent / "fixtures" / "release_recognition_cases.jsonl"
        cases = [
            case
            for case in load_release_recognition_cases(fixture)
            if {"anime", "anime-negative"}.intersection(case.tags)
        ]
        outcomes = []
        for case in cases:
            context = extract_recognition_context(case.filename, case.parent_path)
            baseline = {
                "title": context.normalized_title,
                "year": context.filename_year or context.folder_year,
                "media_type": context.media_type,
                "season": context.season,
                "episode": context.episode,
            }
            experiment = merge_shadow_projection(
                baseline,
                anime_adapter.parse_anime_evidence(case.filename),
            )
            outcomes.extend(compare_shadow_case(
                case_id=case.case_id,
                tags=case.tags,
                expected=case.expected,
                baseline=baseline,
                experiment=experiment,
                fields=case.assert_fields,
            ))

        summary = summarize_shadow_evaluation(outcomes)
        self.assertGreater(summary.total, 0)
        self.assertFalse(
            summary.promotion_allowed,
            "当前 evidence-only 适配器若达到晋级条件，必须先人工审阅再接入生产",
        )

    def test_promotion_requires_improvement_without_regression_or_new_false_positive(self):
        improved = compare_shadow_case(
            case_id="anime-one",
            tags=("anime",),
            expected={"episode": 1},
            baseline={"episode": None},
            experiment={"episode": 1},
            fields=("episode",),
        )
        self.assertTrue(summarize_shadow_evaluation(improved).promotion_allowed)

        regressed = compare_shadow_case(
            case_id="anime-negative",
            tags=("anime-negative",),
            expected={"episode": None},
            baseline={"episode": None},
            experiment={"episode": 1080},
            fields=("episode",),
        )
        summary = summarize_shadow_evaluation(regressed)
        self.assertFalse(summary.promotion_allowed)
        self.assertEqual(summary.new_false_positive_keys, ("anime-negative.episode",))


if __name__ == "__main__":
    unittest.main()
