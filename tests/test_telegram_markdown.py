from __future__ import annotations

import unittest

from app.bot.telegram_markdown import render_telegram_markdown


class TelegramMarkdownTests(unittest.TestCase):
    def test_common_markdown_becomes_supported_telegram_html(self):
        rendered = render_telegram_markdown(
            "# 标题\n\n"
            "- **重点**与*说明*\n"
            "1. ~~旧状态~~ `code`\n\n"
            "> 第一行\n> 第二行\n\n"
            "---\n\n"
            "| 名称 | 状态 |\n| --- | --- |\n| 新番 | 待播 |\n\n"
            "```python\nprint('ok')\n```"
        )

        self.assertIn("<b>标题</b>", rendered)
        self.assertIn("• <b>重点</b>与<i>说明</i>", rendered)
        self.assertIn("1. <s>旧状态</s> <code>code</code>", rendered)
        self.assertIn("<blockquote>第一行\n第二行</blockquote>", rendered)
        self.assertIn("────────", rendered)
        self.assertIn("<b>名称 · 状态</b>", rendered)
        self.assertIn("<pre><code>print('ok')</code></pre>", rendered)

    def test_links_and_raw_html_are_safely_projected(self):
        rendered = render_telegram_markdown(
            "[官网](https://example.com?a=1&b=2) "
            "[危险](javascript:alert(1)) "
            "<script>alert(1)</script> C:\\Media\\Anime"
        )

        self.assertIn(
            '<a href="https://example.com?a=1&amp;b=2">官网</a>', rendered
        )
        self.assertNotIn("javascript:", rendered)
        self.assertIn("危险", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn(r"C:\Media\Anime", rendered)

    def test_incomplete_streaming_markdown_always_returns_balanced_html(self):
        rendered = render_telegram_markdown("### 输出中\n**尚未闭合")

        self.assertEqual(rendered.count("<b>"), rendered.count("</b>"))
        self.assertIn("<b>输出中</b>", rendered)
        self.assertIn("**尚未闭合", rendered)


if __name__ == "__main__":
    unittest.main()
