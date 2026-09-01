"""识别热路径的宽松性能契约：约束缓存复用，不绑定具体机器速度。"""
from __future__ import annotations

import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import guessit as guessit_module

from app.modules import scraper as scraper_module
from app.modules.recognition.extractors import guessit_adapter
from app.modules.scraper import extract_recognition_context
from tests.recognition_eval_helpers import load_release_recognition_cases


FIXTURE = Path(__file__).parent / "fixtures" / "release_recognition_cases.jsonl"


class RecognitionPerformanceContractTests(unittest.TestCase):
    def test_full_corpus_reuses_guessit_cache_on_warm_pass(self):
        cases = load_release_recognition_cases(FIXTURE)
        self.assertGreaterEqual(len(cases), 200)
        guessit_adapter._guessit_cached.cache_clear()

        with patch.object(
            guessit_module, "guessit", wraps=guessit_module.guessit,
        ) as guessit_mock:
            started = perf_counter()
            for case in cases:
                extract_recognition_context(case.filename, case.parent_path)
            cold_elapsed = perf_counter() - started
            cold_call_count = guessit_mock.call_count
            cold_cache = guessit_adapter._guessit_cached.cache_info()

            started = perf_counter()
            for case in cases:
                extract_recognition_context(case.filename, case.parent_path)
            warm_elapsed = perf_counter() - started
            warm_call_count = guessit_mock.call_count
            warm_cache = guessit_adapter._guessit_cached.cache_info()

        # 这是防止意外退化成重复解析的结构契约；30 秒仅用于捕获明显死循环/灾难性回归。
        self.assertLess(
            cold_elapsed,
            30.0,
            f"{len(cases)} 条识别冷启动耗时异常: {cold_elapsed:.3f}s",
        )
        self.assertGreater(cold_call_count, 0)
        self.assertLessEqual(cold_call_count, len(cases) + 32)
        self.assertEqual(warm_call_count, cold_call_count)
        self.assertEqual(warm_cache.misses, cold_cache.misses)
        self.assertGreaterEqual(warm_cache.hits - cold_cache.hits, len(cases))
        self.assertLess(
            warm_elapsed,
            30.0,
            f"{len(cases)} 条识别热缓存耗时异常: {warm_elapsed:.3f}s",
        )


if __name__ == "__main__":
    unittest.main()
