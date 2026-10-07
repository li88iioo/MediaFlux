from __future__ import annotations

import json
import unittest

from app.agent.public_view import (
    format_public_result,
    format_partial_progress,
    public_conversation_messages,
    public_result_state,
    sanitize_confirmed_answer,
)


class AgentKernelPublicViewTests(unittest.TestCase):
    def test_partial_reads_keep_distinct_episode_positions_without_guessing(self):
        rows = [
            {"series_name": "测试剧", "name": "首集", "type": "Episode", "season_number": 1, "episode_number": 1},
            {"series_name": "测试剧", "name": "次集", "type": "Episode", "season_number": 1, "episode_number": 2},
            {"series_name": "测试剧", "name": "特别篇", "type": "Episode", "season_number": 0, "episode_number": 1},
            {"series_name": "测试剧", "name": "未知分季", "type": "Episode", "episode_number": 3},
        ]
        text = format_partial_progress("后续查询限流", [("最近入库", {"ok": True, "data": {"items": rows + rows[:1]}})])
        for position in ("S01E01", "S01E02", "S00E01", "E03"):
            self.assertEqual(text.count(position), 1)
        self.assertNotIn("S01E03", text)
        self.assertNotIn("写操作", text)

    def test_background_job_receipt_exposes_public_ref_and_actual_change_counts(self):
        operation_ref = "GY-0000-0000-0000-0000-0000-0000-0000-0001"
        text = format_public_result({"ok": True, "status": "completed", "summary": "光鸭后台任务已完成",
            "data": {"operation_ref": operation_ref, "stats": {"renamed": 10, "moved": 10, "strm_scope_unknown": 1, "private": 99}}})
        self.assertIn(operation_ref, text)
        self.assertIn("改名 10 项", text)
        self.assertIn("移动 10 项", text)
        self.assertIn("未触发 STRM 联动", text)
        self.assertNotIn("private", text)

    def test_fs_change_receipt_shows_partial_actions_and_blocked_items(self):
        text = format_public_result({
            "ok": False,
            "status": "partial",
            "summary": "文件变更部分完成",
            "data": {
                "stats": {"total": 3, "moved": 1},
                "operation_items": [
                    {
                        "position": 4,
                        "operation": "relocate",
                        "label": "第 4 项：整理并移入目标目录",
                        "status": "partial",
                        "completed_actions": ["move"],
                        "reason": "verification_pending",
                    },
                    {
                        "position": 5,
                        "operation": "rename",
                        "label": "第 5 项：重命名文件",
                        "status": "blocked",
                        "completed_actions": [],
                        "reason": "dependency_failed",
                    },
                    {
                        "position": 6,
                        "operation": "move",
                        "label": "第 6 项：已完成移动",
                        "status": "completed",
                        "completed_actions": ["move"],
                    },
                ],
            },
        })

        self.assertIn("已完成 1/总 3 项", text)
        self.assertIn("#4 · 第 4 项:整理并移入目标目录：已移动；改名未完成；原因：写后核验未完成", text)
        self.assertIn("#5 · 第 5 项:重命名文件：受阻；已确认动作：无；原因：依赖项未完成", text)
        self.assertIn("#6 · 第 6 项:已完成移动：已完成", text)
        self.assertLess(text.index("#4"), text.index("#5"))
        self.assertLess(text.index("#5"), text.index("#6"))

    def test_fs_change_unknown_item_warns_against_blind_retry(self):
        text = format_public_result({
            "ok": False,
            "status": "outcome_unknown",
            "summary": "结果尚未确认",
            "data": {
                "stats": {"total": 1},
                "operation_items": [{
                    "position": 12,
                    "operation": "copy",
                    "label": "复制文件到备份目录",
                    "status": "unknown",
                    "completed_actions": [],
                    "reason": "write_outcome_unknown",
                }],
            },
        })

        self.assertIn("#12 · 复制文件到备份目录：已发起但待核验，不建议盲目重试；已确认动作：无；原因：写入结果未知", text)

    def test_fs_change_complete_receipt_summarizes_without_listing_all_items(self):
        items = [
            {
                "position": position,
                "operation": "rename",
                "label": f"已完成文件 {position}",
                "status": "completed",
                "completed_actions": ["rename"],
            }
            for position in range(1, 4)
        ]
        text = format_public_result({
            "ok": True,
            "status": "completed",
            "summary": "文件变更已完成",
            "data": {"stats": {"total": 3, "renamed": 3}, "operation_items": items},
        })

        self.assertIn("计划结果：已完成 3/总 3 项", text)
        self.assertNotIn("已完成文件", text)

    def test_fs_change_mismatched_item_count_marks_missing_results_unknown(self):
        text = format_public_result({
            "ok": True,
            "status": "partial",
            "summary": "文件变更结果",
            "data": {
                "stats": {"total": 3},
                "operation_items": [
                    {
                        "position": 1,
                        "operation": "move",
                        "label": "已移动文件",
                        "status": "completed",
                        "completed_actions": ["move"],
                    },
                    {
                        "position": 2,
                        "operation": "rename",
                        "label": "未完成改名文件",
                        "status": "failed",
                        "completed_actions": [],
                        "reason": "write_rejected",
                    },
                ],
            },
        })

        self.assertIn("已确认完成 1/总 3 项；另有 1 项结果未核对，状态未知", text)
        self.assertIn("#2 · 未完成改名文件：失败；已确认动作：无；原因：写入被拒绝", text)
        self.assertIn("#1 · 已移动文件：已完成", text)
        self.assertLess(text.index("#2"), text.index("#1"))

    def test_partial_relocate_reports_rename_before_move_when_confirmed(self):
        text = format_public_result({
            "ok": False,
            "status": "partial",
            "summary": "文件变更部分完成",
            "data": {
                "stats": {"total": 1},
                "operation_items": [{
                    "position": 1,
                    "operation": "relocate",
                    "label": "整理并移动目录",
                    "status": "partial",
                    "completed_actions": ["rename"],
                    "reason": "verification_pending",
                }],
            },
        })

        self.assertIn("已改名；移动未完成；原因：写后核验未完成", text)

    def test_fs_change_receipt_limits_remaining_details_to_eight(self):
        items = [
            {
                "position": position,
                "operation": "rename",
                "label": f"待改名文件 {position}",
                "status": "failed",
                "completed_actions": [],
                "reason": "write_rejected",
            }
            for position in range(1, 11)
        ]
        items.append({
            "position": 11,
            "operation": "move",
            "label": "已完成移动",
            "status": "completed",
            "completed_actions": ["move"],
        })
        text = format_public_result({
            "ok": False,
            "status": "partial",
            "summary": "文件变更部分完成",
            "data": {"stats": {"total": 11}, "operation_items": items},
        })

        for position in range(1, 9):
            self.assertIn(f"#{position} ·", text)
        self.assertNotIn("#9 ·", text)
        self.assertNotIn("#11 ·", text)
        self.assertIn("另有 2 项剩余或未知结果未展示", text)

    def test_fs_change_item_label_preserves_dotted_filename(self):
        text = format_public_result({
            "ok": False,
            "status": "partial",
            "summary": "文件变更部分完成",
            "data": {
                "stats": {"total": 1},
                "operation_items": [{
                    "position": 1,
                    "operation": "rename",
                    "label": "改名：movie.mkv → Blue.Streak.S01E01.mkv",
                    "status": "failed",
                    "completed_actions": [],
                }],
            },
        })

        self.assertIn("movie.mkv → Blue.Streak.S01E01.mkv", text)
        self.assertNotIn("内部检查", text)

    def test_fs_change_item_label_is_sanitized_again_before_display(self):
        text = format_public_result({
            "ok": False,
            "status": "partial",
            "summary": "文件变更未完成",
            "data": {
                "stats": {"total": 1},
                "operation_items": [{
                    "position": 1,
                    "operation": "rename",
                    "label": "改名 access_token=AbCd1234testCredential9876 /data/private/test.env",
                    "status": "failed",
                    "completed_actions": [],
                    "reason": "execution_error",
                }],
            },
        })

        self.assertNotIn("access_token", text)
        self.assertNotIn("AbCd1234testCredential9876", text)
        self.assertNotIn("/data/private/test.env", text)

    def test_partial_progress_uses_confirmed_fs_receipt_as_authoritative(self):
        confirmed = {
            "ok": False,
            "status": "partial",
            "summary": "文件变更部分完成",
            "data": {
                "stats": {"total": 1},
                "operation_items": [{
                    "position": 2,
                    "operation": "relocate",
                    "label": "剧集目录整理",
                    "status": "partial",
                    "completed_actions": ["move"],
                    "reason": "verification_pending",
                }],
            },
        }
        result = format_partial_progress(
            "后续查询未完成",
            [("只读查询", {"ok": True, "summary": "已找到相关目录"})],
            confirmed_result=confirmed,
            write_attempted=True,
        )

        self.assertEqual(result, format_public_result(confirmed))
        self.assertNotIn("后续写操作尚未执行", result)
        self.assertNotIn("只读查询", result)

    def test_fs_change_items_do_not_mix_with_read_only_or_candidate_items(self):
        read_only = format_partial_progress(
            "查询部分完成",
            [(
                "只读查询",
                {
                    "ok": True,
                    "summary": "已找到目录",
                    "data": {"operation_items": [{"label": "不应作为写回执展示"}]},
                },
            )],
        )
        candidates = format_public_result({
            "ok": True,
            "status": "completed",
            "summary": "资源已提交",
            "data": {
                "source_type": "resource_candidates",
                "items": [{"position": 3, "title": "资源候选", "status": "submitted"}],
            },
        })

        self.assertIn("已找到目录", read_only)
        self.assertNotIn("不应作为写回执展示", read_only)
        self.assertNotIn("后续写操作尚未执行", read_only)
        self.assertIn("#3 · 资源候选：已提交", candidates)
        self.assertNotIn("计划结果", candidates)

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
