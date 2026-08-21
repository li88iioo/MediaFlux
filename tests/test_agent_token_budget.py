"""Agent LLM 上下文 token 预算测试。"""
from __future__ import annotations

import unittest

from app.agent.token_budget import (
    estimate_tokens,
    fit_structured_user_content,
    request_fits_token_budget,
    resolve_context_window,
)


class AgentTokenBudgetTests(unittest.TestCase):
    def test_estimator_is_conservative_for_mixed_cjk_text(self):
        self.assertGreater(estimate_tokens("媒体库 update 2026"), 0)
        self.assertGreater(estimate_tokens("媒体库更新"), estimate_tokens("update"))

    def test_context_window_uses_configured_value_and_safe_fallback(self):
        self.assertEqual(resolve_context_window("16384", model="unknown"), 16384)
        self.assertEqual(resolve_context_window("12", model="unknown"), 4096)
        self.assertEqual(resolve_context_window("invalid", model="unknown"), 8192)
        self.assertEqual(resolve_context_window("", model="gpt-5"), 32768)

    def test_long_user_context_is_tail_trimmed_with_output_reserve(self):
        body = {"model": "test", "system": "安全系统提示", "schema": {"type": "object"}}
        current = "CURRENT-QUESTION: 继续检查这部剧"
        fitted = fit_structured_user_content(
            body_without_user=body,
            user_content=("很早的历史。" * 3000) + current,
            context_window=4096,
            output_reserve=700,
        )
        self.assertIsNotNone(fitted)
        assert fitted is not None
        self.assertTrue(fitted.startswith("[较早上下文已按预算省略]"))
        self.assertTrue(fitted.endswith(current))
        request = {**body, "user": fitted}
        self.assertTrue(request_fits_token_budget(
            request, context_window=4096, output_reserve=700
        ))

    def test_oversized_fixed_schema_fails_closed(self):
        body = {"tools": [{"description": "超长" * 5000}]}
        self.assertIsNone(fit_structured_user_content(
            body_without_user=body,
            user_content="当前问题",
            context_window=4096,
            output_reserve=700,
        ))
        self.assertFalse(request_fits_token_budget(
            body, context_window=4096, output_reserve=700
        ))


if __name__ == "__main__":
    unittest.main()
