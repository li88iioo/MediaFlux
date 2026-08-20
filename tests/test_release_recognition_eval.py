"""固定真实发布名样本，防止识别清洗、年份和季集解析悄然回归。"""
from __future__ import annotations

import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from app.modules.scraper import TMDBScraper, extract_recognition_context
from tests.support import release_parse_fields
from tests.recognition_eval_helpers import (
    FIELDS,
    classify_field,
    evaluate_projection,
    format_recognition_report,
    load_release_recognition_cases,
    recognition_metrics,
)


FIXTURE = Path(__file__).parent / "fixtures" / "release_recognition_cases.jsonl"
CATEGORY_MINIMUMS = {
    "category-standard": 30,
    "category-absolute": 15,
    "category-season-context": 20,
    "category-release-noise": 20,
    "category-multilingual": 25,
    "category-special-range": 18,
    "category-movie": 20,
    "category-metadata": 3,
    "category-negative": 30,
    "category-long-running": 10,
}
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


class ReleaseRecognitionFixtureSchemaTests(unittest.TestCase):
    def test_fixture_schema_is_strict_and_case_ids_are_unique(self):
        cases = load_release_recognition_cases(FIXTURE)

        self.assertGreaterEqual(len(cases), 200)
        self.assertEqual(len({case.case_id for case in cases}), len(cases))
        self.assertEqual(
            len({(case.filename, case.parent_path) for case in cases}),
            len(cases),
        )

    def test_fixture_schema_rejects_unknown_fields_with_case_id(self):
        invalid = (
            '{"case_id":"bad-row","filename":"Demo.mkv","parent_path":"",'
            '"expected":{"title":"Demo","year":"","media_type":"movie",'
            '"season":null,"episode":null},"surprise":true}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.jsonl"
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"bad-row: 未知字段: surprise"):
                load_release_recognition_cases(path)

    def test_fixture_schema_rejects_duplicate_case_ids(self):
        row = (
            '{"case_id":"duplicate","filename":"Demo.mkv","parent_path":"",'
            '"expected":{"title":"Demo","year":"","media_type":"movie",'
            '"season":null,"episode":null}}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.jsonl"
            path.write_text(row + row, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"duplicate: case_id 重复"):
                load_release_recognition_cases(path)

    def test_fixture_schema_rejects_duplicate_source_projection(self):
        first = (
            '{"case_id":"source-one","filename":"Demo.mkv","parent_path":"Season 01",'
            '"expected":{"title":"Demo","year":"","media_type":"tv",'
            '"season":1,"episode":1}}\n'
        )
        second = first.replace('"source-one"', '"source-two"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-source.jsonl"
            path.write_text(first + second, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, r"source-two: filename 与 parent_path 组合重复"
            ):
                load_release_recognition_cases(path)

    def test_fixture_schema_rejects_invalid_assert_fields(self):
        invalid = (
            '{"case_id":"bad-assert-fields","filename":"Demo.mkv","parent_path":"",'
            '"expected":{"title":"Demo","year":"","media_type":"movie",'
            '"season":null,"episode":null},"assert_fields":["episode","bogus"]}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-assert-fields.jsonl"
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"assert_fields 必须是非空的受支持字段数组"):
                load_release_recognition_cases(path)

    def test_corpus_has_balanced_categories_and_boundary_coverage(self):
        cases = load_release_recognition_cases(FIXTURE)
        categories = Counter()
        for case in cases:
            category_tags = [tag for tag in case.tags if tag.startswith("category-")]
            self.assertEqual(category_tags, category_tags[:1], case.case_id)
            self.assertEqual(len(category_tags), 1, case.case_id)
            categories.update(category_tags)

        for category, minimum in CATEGORY_MINIMUMS.items():
            self.assertGreaterEqual(categories[category], minimum, category)

        negative_cases = [case for case in cases if "category-negative" in case.tags]
        self.assertGreaterEqual(len(negative_cases), 30)
        self.assertTrue(all("negative" in case.tags for case in negative_cases))
        self.assertGreaterEqual(sum(bool(case.parent_path) for case in cases), 20)
        self.assertGreaterEqual(
            sum(bool(_CJK_RE.search(f"{case.filename} {case.parent_path}")) for case in cases),
            50,
        )
        self.assertGreaterEqual(
            sum(case.expected["media_type"] == "movie" for case in cases), 40
        )
        self.assertGreaterEqual(sum(bool(case.expected["year"]) for case in cases), 40)
        self.assertGreaterEqual(sum(case.expected["season"] == 0 for case in cases), 10)
        self.assertGreaterEqual(sum(case.expected["episode"] is None for case in cases), 50)

    def test_field_report_identifies_case_and_field_without_parent_path(self):
        case = next(
            case for case in load_release_recognition_cases(FIXTURE)
            if case.parent_path
        )
        actual = dict(case.expected)
        actual["episode"] = None
        report = format_recognition_report(evaluate_projection(case, actual))

        self.assertIn(f"{case.case_id}.episode: unresolved", report)
        self.assertNotIn(case.parent_path, report)

    def test_field_classifier_distinguishes_failure_categories(self):
        self.assertEqual(classify_field("episode", None, 3), "false_positive")
        self.assertEqual(classify_field("episode", 3, None), "unresolved")
        self.assertEqual(classify_field("episode", 3, 4), "conflict")
        self.assertEqual(classify_field("episode", 3, 3), "matched")


class ReleaseRecognitionEvaluationTests(unittest.TestCase):
    def test_real_release_name_corpus_matches_expected_field_metrics(self):
        cases = load_release_recognition_cases(FIXTURE)
        outcomes = []
        parser = TMDBScraper()
        compatibility_failures: list[str] = []

        for case in cases:
            context = extract_recognition_context(case.filename, case.parent_path)
            actual = {
                "title": context.normalized_title,
                "year": context.filename_year or context.folder_year,
                "media_type": context.media_type,
                "season": context.season,
                "episode": context.episode,
            }
            outcomes.extend(evaluate_projection(case, actual))

            # 统一识别结果同时保留文件名投影；目录级样本允许由 parent_path 补全标题与季号。
            if not case.parent_path:
                parsed = release_parse_fields(parser.parse_media(case.filename))
                if not str(parsed["title"] or "").strip():
                    # 显式 TMDB 标记的旧投影允许留空标题，由后续详情补全。
                    self.assertTrue(context.normalized_title)
                if "legacy-divergence" not in case.tags:
                    for field in ("season", "episode"):
                        if parsed[field] != getattr(context, field):
                            compatibility_failures.append(
                                f"{case.case_id}.{field}: parse_media={parsed[field]!r}, "
                                f"context={getattr(context, field)!r}"
                            )

        mismatches = [row for row in outcomes if row.category != "matched"]
        metrics = recognition_metrics(outcomes)
        self.assertEqual(
            metrics["overall"]["total"],
            sum(len(case.assert_fields) for case in cases),
        )
        self.assertEqual(
            mismatches,
            [],
            format_recognition_report(outcomes),
        )
        self.assertEqual(
            compatibility_failures,
            [],
            "\n".join(compatibility_failures),
        )


class ReleaseParserCacheTests(unittest.TestCase):
    def test_guessit_result_is_reused_without_sharing_top_level_mutation(self):
        import guessit as guessit_module
        from app.modules import scraper as scraper_module

        scraper_module._guessit_cached.cache_clear()
        release_name = "[CacheProbe] Stable.Show.S02E03.1080p.WEB-DL.mkv"
        with patch.object(
            guessit_module, "guessit", wraps=guessit_module.guessit,
        ) as guessit_mock:
            first = scraper_module._guessit_info(release_name)
            second = scraper_module._guessit_info(release_name)

        self.assertEqual(guessit_mock.call_count, 1)
        self.assertEqual(first, second)
        first["title"] = "mutated"
        self.assertNotEqual(
            scraper_module._guessit_info(release_name).get("title"), "mutated",
        )
        self.assertEqual(scraper_module._guessit_cached.cache_info().hits, 2)


if __name__ == "__main__":
    unittest.main()
