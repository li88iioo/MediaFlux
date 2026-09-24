from __future__ import annotations

import json
import unittest

from app.agent.public_view import (
    format_public_result,
    public_conversation_messages,
    public_result_state,
    sanitize_confirmed_answer,
)


class AgentKernelPublicViewTests(unittest.TestCase):
    def test_background_job_receipt_exposes_public_ref_and_actual_change_counts(self):
        operation_ref = "GY-0000-0000-0000-0000-0000-0000-0000-0001"
        text = format_public_result({"ok": True, "status": "completed", "summary": "光鸭后台任务已完成",
            "data": {"operation_ref": operation_ref, "stats": {"renamed": 10, "moved": 10, "strm_scope_unknown": 1, "private": 99}}})
        self.assertIn(operation_ref, text)
        self.assertIn("改名 10 项", text)
        self.assertIn("移动 10 项", text)
        self.assertIn("未触发 STRM 联动", text)
        self.assertNotIn("private", text)

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

    def test_confirmed_answer_drops_embedded_internal_receipt(self) -> None:
        result = {
            "ok": True,
            "status": "accepted",
            "summary": "本地媒体任务 1 已修正为 S02E12 并重新排队",
            "data": {"operation": "remap_episode", "task_number": 1},
        }
        answer = (
            "已确认操作的可信系统结果（不是待执行计划）：\n"
            + json.dumps({**result, "evidence": [{"source": "sqlite:private"}]}, ensure_ascii=False)
            + "\n\n### 处理完成\n系统正在自动归档。"
        )

        public = sanitize_confirmed_answer(answer, result)

        self.assertIn("重新排队", public)
        self.assertIn("后台任务尚未完成", public)
        self.assertNotIn("可信系统结果", public)
        self.assertNotIn("evidence", public)
        self.assertNotIn("处理完成", public)
        self.assertNotIn("sqlite", public)

    def test_confirmed_answer_keeps_natural_completed_followup(self) -> None:
        result = {"ok": True, "status": "completed", "summary": "改名已完成"}
        answer = (
            "已确认操作的可信系统结果（不是待执行计划）：\n"
            + json.dumps(result, ensure_ascii=False)
            + "\n\n改名已经完成，可以继续下一步。"
        )

        self.assertEqual(
            sanitize_confirmed_answer(answer, result),
            "改名已经完成，可以继续下一步。",
        )

    def test_submitted_answer_drops_operation_specific_completion_claims(self) -> None:
        result = {"ok": True, "status": "accepted", "summary": "清空请求已提交"}
        answer = "光鸭回收站已经清空，删除成功。"

        public = sanitize_confirmed_answer(answer, result)

        self.assertIn("清空请求已提交", public)
        self.assertIn("后台任务尚未完成", public)
        self.assertNotIn("已经清空", public)
        self.assertNotIn("删除成功", public)


    def test_pending_and_uncertain_results_have_one_canonical_public_state(self) -> None:
        for status in ("accepted", "submitted"):
            with self.subTest(status=status):
                result = {"ok": True, "status": status, "summary": "任务状态已更新"}
                self.assertEqual(public_result_state(result), "submitted")
                self.assertTrue(format_public_result(result).startswith("📤 "))
                self.assertIn("请求已提交", format_public_result(result))
                self.assertIn("尚未完成", format_public_result(result))

        self.assertEqual(
            public_result_state(
                {"ok": False, "status": "accepted", "summary": "提交失败"}
            ),
            "failed",
        )

        for status in ("queued", "running", "in_progress", "retry_wait"):
            with self.subTest(status=status):
                result = {"ok": True, "status": status, "summary": "任务状态已更新"}
                self.assertEqual(public_result_state(result), "pending")
                self.assertTrue(format_public_result(result).startswith("⏳ "))
                self.assertIn("尚未完成", format_public_result(result))

        for status in ("partial", "attention", "outcome_unknown", "manual_review", "stopped", "cancelled"):
            with self.subTest(status=status):
                result = {"ok": status in {"stopped", "cancelled"}, "status": status, "summary": "需要核验"}
                self.assertEqual(public_result_state(result), "warning")
                self.assertTrue(format_public_result(result).startswith("⚠️ "))

    def test_background_unknown_overrides_last_running_snapshot(self) -> None:
        result = {
            "ok": False,
            "status": "outcome_unknown",
            "summary": "等待已达上限，结果尚未确认",
            "data": {"background_job": {"status": "running", "timed_out": True}},
        }

        self.assertEqual(public_result_state(result), "warning")
        self.assertTrue(format_public_result(result).startswith("⚠️ "))

    def test_non_success_confirmed_answer_drops_model_completion_claim(self) -> None:
        for status in ("queued", "running", "partial", "outcome_unknown", "stopped"):
            with self.subTest(status=status):
                result = {"ok": status != "outcome_unknown", "status": status, "summary": "真实业务状态"}
                answer = (
                    "已确认操作的可信系统结果（不是待执行计划）：\n"
                    + json.dumps(result, ensure_ascii=False)
                    + "\n\n### 处理完成\n全部已经完成。"
                )
                public = sanitize_confirmed_answer(answer, result)
                self.assertIn("真实业务状态", public)
                self.assertNotIn("处理完成", public)
                self.assertNotIn("全部已经完成", public)

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
