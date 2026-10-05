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

    def test_submitted_model_paraphrases_do_not_duplicate_the_canonical_receipt(self):
        result = {"ok": True, "status": "submitted", "summary": "下载请求 #276 已提交", "data": {"target": "guangya"}}
        text = "📤 下载请求 #276 已提交\n后台任务正在执行中\n磁力链接已识别并成功提交下载，任务编号 #276。"
        public = sanitize_confirmed_answer(text, result)
        self.assertEqual(public, format_public_result(result))
        self.assertEqual(public.count("#276"), 1)
        self.assertNotIn("✅", public)

    def test_invalid_internal_receipt_does_not_claim_success(self):
        text = sanitize_confirmed_answer("已确认操作的可信系统结果（不是待执行计划）：\nbroken-json")
        self.assertIn("尚未确认", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("broken-json", text)

    def test_restore_folds_only_receipt_with_a_matching_final_plan(self):
        receipt = {"role": "assistant", "tool_name": "ingest.submit", "effect_plan_id": "plan-1", "public_content": "📤 请求已提交", "content": "internal"}
        final = {"role": "assistant", "effect_plan_id": "plan-1", "content": "📤 请求已提交\n后台任务尚未完成"}
        conversation = [{"role": "user", "content": "下载"}, receipt, {"role": "tool", "tool_name": "download.inspect", "content": "internal"}, final]
        messages = public_conversation_messages(conversation)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["content"], final["content"])
        self.assertEqual(messages[-1]["tools"], ["ingest.submit", "download.inspect"])
        self.assertEqual(len(public_conversation_messages(conversation[:-1])), 2, "中断时必须保留已执行回执")
        other = {**receipt, "effect_plan_id": "plan-2"}
        self.assertEqual(len(public_conversation_messages([receipt, other, final])), 2, "不能隐藏其它计划")
        self.assertEqual(len(public_conversation_messages([{k: v for k, v in receipt.items() if k != "effect_plan_id"}, final])), 2, "旧历史无关联证据时不能误删")

    def test_folded_candidate_receipt_moves_to_matching_final_answer_only(self):
        receipt = {"role": "assistant", "tool_name": "ingest.submit", "effect_plan_id": "p", "candidate_result_ref": "r", "public_content": "已提交", "content": "internal"}
        final = {"role": "assistant", "effect_plan_id": "p", "content": "已提交"}
        view = {"ref": "r", "last_result": {"text": "已提交"}}
        messages = public_conversation_messages([receipt, final], candidate_view=view)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["candidate_result_ref"], "r")
        final["content"] = "已提交\n\n后续还有新事实。"
        messages = public_conversation_messages([receipt, final], candidate_view=view)
        self.assertEqual(messages[0]["candidate_result_ref"], "r")
        self.assertEqual(messages[0]["candidate_followup"], "后续还有新事实。", "候选卡不能吞掉后续说明")

    def test_legacy_submitted_history_merges_only_explicit_internal_receipt(self):
        result = {"ok": True, "status": "submitted", "summary": "下载请求 #276 已提交"}
        receipt = {"role": "assistant", "tool_name": "ingest.submit", "content": "已确认操作的可信系统结果（不是待执行计划）：\n" + json.dumps(result)}
        final = {"role": "assistant", "content": "下载请求 #276 已提交。任务 #276 正在执行中。"}
        messages = public_conversation_messages([receipt, final])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"].count("#276"), 1)
        self.assertEqual(len(public_conversation_messages([receipt])), 1)
        self.assertEqual(len(public_conversation_messages([receipt, {"role": "user", "content": "另一个问题"}, final])), 3)

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


class PublicToolArgumentErrorTests(unittest.TestCase):
    def test_actual_validator_error_survives_kernel_and_public_projection(self):
        from app.agent.guangya_fs_change_actions import guangya_fs_change_preview_arguments
        from app.agent.kernel.ports.existing_actions import _safe_error
        from app.agent.public_safety import sanitize_public_text
        from app.agent.errors import AgentToolError
        with self.assertRaises(AgentToolError) as caught:
            guangya_fs_change_preview_arguments({'operations': [{'op': 'rename', 'object_ref': 'OBJ' + 'A' * 24}]})
        error = _safe_error(caught.exception, fallback_code='invalid_tool_call')
        text = sanitize_public_text(str(error))
        self.assertIn('缺少 新名称', text)
        self.assertIn('对象引用', text)
        self.assertNotIn('内部状态', text)
        self.assertNotIn('OBJ' + 'A' * 24, text)
        self.assertEqual(text, sanitize_public_text(text))

    def test_schema_labels_do_not_disable_secret_and_internal_state_redaction(self):
        from app.agent.public_safety import sanitize_public_text
        text = sanitize_public_text('new_name object_ref target_path parent_path observation_ref session_snapshot')
        for label in ('新名称', '对象引用', '目标目录', '父目录', '目录观察引用'):
            self.assertIn(label, text)
        self.assertNotIn('session_snapshot', text)
        self.assertEqual(sanitize_public_text('new_name access_token=AbCd1234testCredential9876'), '')
        self.assertEqual(sanitize_public_text('new_name /data/private/test.env'), '')
