from __future__ import annotations

import json
import unittest

from app.agent.kernel.public_view import (
    format_public_result,
    public_conversation_messages,
)


class AgentKernelPublicViewTests(unittest.TestCase):
    def test_conversation_hides_empty_tool_turns_and_internal_confirmed_json(self) -> None:
        internal_result = {
            "ok": True,
            "status": "partial",
            "summary": "批量提交完成：2 个已受理，1 个未受理",
            "data": {
                "target": "guangya",
                "total": 3,
                "succeeded": 2,
                "failed": 1,
                "items": [
                    {
                        "result_id": "private-result-id",
                        "request_id": 54,
                        "ok": False,
                        "error": "索引站点响应超时",
                    }
                ],
            },
        }
        conversation = [
            {"role": "user", "content": "搜索并推送 4K 版"},
            {
                "role": "assistant",
                "content": "查询已完成。",
                "tool_calls": [
                    {"call_id": "call-1", "name": "indexer.search_resources"}
                ],
            },
            {
                "role": "tool",
                "content": "internal",
                "tool_name": "indexer.search_resources",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"call_id": "call-2", "name": "ingest.submit"}],
            },
            {
                "role": "assistant",
                "content": (
                    "已确认操作的可信系统结果（不是待执行计划）：\n"
                    + json.dumps(internal_result, ensure_ascii=False)
                ),
                "tool_name": "ingest.submit",
                "public_content": format_public_result(internal_result),
            },
        ]

        messages = public_conversation_messages(conversation)

        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertNotIn("查询已完成", str(messages))
        self.assertNotIn("private-result-id", str(messages))
        self.assertNotIn("request_id", str(messages))
        self.assertIn("批量提交完成", messages[-1]["content"])
        self.assertIn("索引站点响应超时", messages[-1]["content"])
        self.assertEqual(
            messages[-1]["tools"],
            ["indexer.search_resources", "ingest.submit"],
        )
        self.assertEqual(
            messages[-1]["tool_labels"],
            ["多站资源搜索", "资源接入提交"],
        )

    def test_legacy_confirmed_result_is_compacted_without_raw_identifiers(self) -> None:
        content = (
            "已确认操作的可信系统结果（不是待执行计划）：\n"
            '{"ok":true,"status":"success","summary":"任务已创建",'
            '"data":{"target":"guangya","request_id":99,"total":1}}'
        )

        messages = public_conversation_messages(
            [{"role": "assistant", "content": content, "tool_name": "ingest.submit"}]
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("任务已创建", messages[0]["content"])
        self.assertIn("光鸭云盘", messages[0]["content"])
        self.assertNotIn("request_id", messages[0]["content"])
        self.assertNotIn("99", messages[0]["content"])

    def test_partial_result_uses_compact_human_labels(self) -> None:
        text = format_public_result(
            {
                "ok": True,
                "status": "partial",
                "summary": "部分完成",
                "data": {
                    "target": "guangya",
                    "total": 3,
                    "succeeded": 2,
                    "failed": 1,
                },
            }
        )

        self.assertEqual(
            text,
            "⚠️ 部分完成\n- 目标：光鸭云盘\n- 请求：3 项\n- 已受理：2 项\n- 未完成：1 项",
        )


if __name__ == "__main__":
    unittest.main()
