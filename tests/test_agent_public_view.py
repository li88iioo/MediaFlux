from __future__ import annotations

from collections.abc import Mapping
import json
import unittest
import unicodedata

from app.agent.public_view import (
    format_partial_progress,
    format_public_result,
    public_active_turn_events,
    public_activities,
    public_conversation_messages,
    public_result_state,
    sanitize_confirmed_answer,
)


class AgentKernelPublicViewTests(unittest.TestCase):
    def test_activity_snapshot_preserves_business_facts_without_execution_payloads(self):
        active = {"request_id": "request", "turn_id": "turn"}
        activity = {"id": "read:1", "kind": "tool", "method": "watchlist.list",
                    "label": "读取收藏片单", "status": "partial", "summary": "本次返回 14 项；还有后续页",
                    "arguments": {"api_key": "private-secret"}, "stdout": "private script output"}
        rows = [{**active, "type": "tool.progress", "payload": {"activity": activity}},
                {**active, "turn_id": "foreign", "type": "tool.progress", "payload": {"activity": activity}}]
        restored = public_active_turn_events(rows, active)
        self.assertEqual(len(restored), 1)
        visible = restored[0]["payload"]["activity"]
        self.assertEqual(visible["summary"], "本次返回 14 项;还有后续页")
        self.assertNotIn("arguments", visible)
        self.assertNotIn("stdout", visible)
        messages = public_conversation_messages([{"role": "assistant", "content": "已查到收藏。", "activities": [activity]}])
        self.assertEqual(messages[0]["activities"], [visible])
        self.assertNotIn("activities", public_conversation_messages([{"role": "assistant", "content": "旧回复"}])[0])
        private = {**activity, "summary": "api_key=sk-private-secret-for-test"}
        self.assertEqual(public_activities([private])[0]["summary"], "")
        self.assertLessEqual(len(public_activities([{**activity, "id": str(i)} for i in range(100)])), 80)

    def test_activity_snapshot_survives_next_turn_but_never_becomes_model_input(self):
        from app.agent.kernel.conversation_context import persisted_conversation
        from app.agent.kernel.model import ModelMessage

        activity = {"id": "read:1", "kind": "tool", "method": "watchlist.list",
                    "label": "读取收藏片单", "status": "success", "summary": "本次返回 14 项"}
        original = [{"role": "user", "content": "收藏有哪些"},
                    {"role": "assistant", "content": "14部", "activities": [activity]}]
        restored = [ModelMessage.from_dict(row) for row in original]
        restored.append(ModelMessage(role="user", content="继续"))
        encoded = persisted_conversation(restored, current_user_index=2, original_message="继续", prior_conversation=original)
        self.assertEqual(encoded[1]["activities"], [activity])
        self.assertNotIn("activities", restored[1].to_dict())

    def test_active_journal_without_start_never_publishes_stream_text(self):
        active = {"request_id": "request", "turn_id": "turn"}
        restored = public_active_turn_events([
            {**active, "type": "model.delta", "payload": {"delta": "hidden-text"}},
            {**active, "type": "tool.completed", "payload": {"tool": "agent.run_code", "call_id": "call",
                "result": {"summary": "命令完成", "data": {"token": "secret"}}}},
        ], active)
        self.assertEqual(restored, [{**active, "type": "tool.completed", "payload": {
            "call_id": "call", "label": "工作区代码执行", "result": {"summary": "命令完成"}}}])

    def test_partial_reads_keep_entry_summary_when_analysis_only_reports_failure(self):
        analysis = "另一个目录读取失败。"
        text = format_partial_progress("后续查询未完成", [("光鸭目录列表", {
            "ok": True,
            "summary": "光鸭列表完成：共3个对象",
            "data": {"entries": [{"name": name} for name in ("目录甲", "目录乙", "目录丙")]},
        })], analysis_text=analysis)

        self.assertIn(analysis, text)
        self.assertEqual(text.count("共3个对象"), 1)
        self.assertIn("已完成的查询：", text)

    def test_partial_progress_keeps_business_receipts_without_workspace_status(self):
        command = {"ok": True, "summary": "命令已退出（退出码 1）", "_tool_name": "agent.run_code"}
        read = {"ok": True, "summary": "媒体库核对到3部影片"}
        receipt = {"ok": True, "status": "completed", "summary": "已移动1个目录"}
        for write_attempted, confirmed_result in ((False, None), (True, None), (False, receipt)):
            with self.subTest(write_attempted=write_attempted, confirmed_result=confirmed_result):
                text = format_partial_progress("回答中断", [("工作区", command), ("媒体库", read)],
                                               write_attempted=write_attempted, confirmed_result=confirmed_result)
                self.assertNotIn("退出码", text)
                self.assertIn(read["summary"], text)
                if confirmed_result is not None:
                    self.assertIn(receipt["summary"], text)

    def test_partial_reads_deduplicate_only_summaries_covered_by_analysis(self):
        summary = "光鸭列表完成：共3个对象"
        other_summary = "媒体库检查通过"
        results = [("光鸭目录列表", {"ok": True, "summary": summary}),
                   ("媒体库检查", {"ok": True, "summary": other_summary})] * 2
        for all_covered in (False, True):
            analysis = summary + "。\n" + (other_summary + "。\n" if all_covered else "") + "另一个目录读取失败。"
            with self.subTest(all_covered=all_covered):
                text = format_partial_progress("后续查询未完成", results, analysis_text=analysis)

                self.assertIn(analysis, text)
                self.assertEqual(text.count("共3个对象"), 1)
                self.assertEqual(text.count(other_summary), 1)
                if all_covered:
                    self.assertNotIn("已完成的查询：", text)
                else:
                    self.assertIn("已完成的查询：", text)
                    self.assertNotIn("共3个对象", text.partition("已完成的查询：")[2])
                self.assertNotIn("本轮尚未形成可交付的答复", text)

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

    def test_partial_reads_preserve_invalid_media_type_errors(self):
        for media_type in ({"bad": "type"}, ["movie"]):
            with self.subTest(media_type=media_type), self.assertRaises(TypeError):
                format_partial_progress("后续查询失败", [("查询", {
                    "ok": True, "data": {"items": [{"title": "测试片", "media_type": media_type}]},
                })])

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
                "scope_note": "仅处理已确认的剧集目录",
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
            current_errors=["目录详情查询失败"],
        )

        receipt = format_public_result(confirmed)
        self.assertIn(receipt, result)
        self.assertEqual(result.count(receipt), 1)
        self.assertIn("后续写操作尚未执行；已确认操作以以上回执为准。", result)
        self.assertIn("本轮工具核对结果（不代表后续操作已完成）：", result)
        self.assertIn("只读查询", result)
        self.assertIn("已找到相关目录", result)
        self.assertIn("执行范围：仅处理已确认的剧集目录", result)
        self.assertIn("已移动；改名未完成", result)
        self.assertIn("写后核验未完成", result)
        self.assertIn("部分完成：后续查询未完成", result)
        self.assertIn("未完成的查询：目录详情查询失败", result)
        self.assertIn("新的写操作仍需确认，不会自动重放。", result)

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

        for status in ("pending", "queued", "running", "in_progress", "retry_wait"):
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
        for status in ("pending", "queued", "running", "partial", "outcome_unknown", "stopped"):
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

    def test_folded_candidate_preserves_partial_failure_before_receipt(self):
        receipt = {"role": "assistant", "tool_name": "ingest.submit", "effect_plan_id": "p", "candidate_result_ref": "r", "public_content": "已提交", "content": "internal"}
        view = {"ref": "r", "last_result": {"text": "已提交"}}
        for suffix in ("", "\n\n未完成的查询：服务不可用"):
            with self.subTest(suffix=suffix):
                final = {"role": "assistant", "effect_plan_id": "p", "content": "部分完成：查询失败\n\n已提交" + suffix}
                messages = public_conversation_messages([receipt, final], candidate_view=view)
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["candidate_result_ref"], "r")
                self.assertEqual(messages[0]["candidate_followup"], "部分完成：查询失败" + suffix)
                self.assertEqual(messages[0]["content"], final["content"])

    def test_candidate_fold_requires_receipt_paragraph_and_matching_plan(self):
        receipt = {"role": "assistant", "tool_name": "ingest.submit", "effect_plan_id": "p", "candidate_result_ref": "r", "public_content": "已提交", "content": "internal"}
        view = {"ref": "r", "last_result": {"text": "已提交"}}
        for content in ("尚未提交", "尚未已提交", "已提交状态待定", "状态：已提交\n\n后续说明"):
            with self.subTest(content=content):
                final = {"role": "assistant", "effect_plan_id": "p", "content": content}
                messages = public_conversation_messages([receipt, final], candidate_view=view)
                self.assertNotIn("candidate_result_ref", messages[-1])
                self.assertEqual(messages[-1]["content"], content)
        final = {"role": "assistant", "effect_plan_id": "other", "content": "部分完成\n\n已提交"}
        messages = public_conversation_messages([receipt, final], candidate_view=view)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["candidate_result_ref"], "r")
        self.assertNotIn("candidate_result_ref", messages[1])

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


class PublicCompletionClaimRegressionTests(unittest.TestCase):
    _ENTRYPOINTS = ("format_partial_progress", "sanitize_confirmed_answer")
    _UNCONFIRMED_RECEIPTS = (
        {"ok": False, "status": "failed", "summary": "分享创建失败"},
        {"ok": True, "status": "pending", "summary": "分享创建尚未完成"},
    )
    _COMPLETION_SENTENCES = (
        "分享已经创建。",
        "**分享已经创建。**",
        "文件已经分享。",
        "文件已经保存。",
        "消息已经发送。",
        "分享创建成功。",
        "文件分享成功。",
        "文件保存成功。",
        "消息发送成功。",
        "The share has been created.",
        "The file has been shared.",
        "The file has been saved.",
        "The message has been sent.",
    )

    def _project(self, entrypoint: str, answer: str, result: Mapping[str, object]) -> str:
        if entrypoint == "format_partial_progress":
            return format_partial_progress(
                "后续查询未完成", [], confirmed_result=result, analysis_text=answer,
            )
        return sanitize_confirmed_answer(answer, result)

    def test_unconfirmed_receipts_do_not_leak_write_completion_claims(self) -> None:
        for result in self._UNCONFIRMED_RECEIPTS:
            for answer in self._COMPLETION_SENTENCES:
                for entrypoint in self._ENTRYPOINTS:
                    with self.subTest(entrypoint=entrypoint, status=result["status"], answer=answer):
                        public = self._project(entrypoint, answer, result)
                        self.assertIn(format_public_result(result), public)
                        self.assertNotIn(answer.rstrip("。.").casefold(), public.casefold())
                        self.assertFalse(any(line.strip() in {"**", "__", "`"} for line in public.splitlines()))

    def test_unconfirmed_receipts_keep_negation_and_next_steps(self) -> None:
        cases = (
            ("分享尚未创建，需要核验失败原因。", ("尚未创建", "核验失败原因")),
            ("**执行未完成：原确认卡已失效，未创建任何分享。**", ("**执行未完成", "未创建任何分享。**")),
            ("未能确认创建成功，请先核验实际状态。", ("未能确认创建成功", "核验实际状态")),
            ("未能确认这个分享已经创建，因此需要重新核验。", ("未能确认这个分享已经创建", "重新核验")),
            ("确认后再创建分享，不会自动重试。", ("确认后再创建", "不会自动重试")),
            ("The share has not been created yet. Please verify the result.",
             ("not been created", "verify the result")),
            ("Create the share after confirmation.", ("Create the share", "after confirmation")),
        )
        for result in self._UNCONFIRMED_RECEIPTS:
            for answer, retained in cases:
                for entrypoint in self._ENTRYPOINTS:
                    with self.subTest(entrypoint=entrypoint, status=result["status"], answer=answer):
                        public = self._project(entrypoint, answer, result)
                        self.assertIn(format_public_result(result), public)
                        for fragment in retained:
                            self.assertIn(fragment, public)

    def test_negated_chinese_clause_does_not_license_later_success_claims(self) -> None:
        cases = (
            ("分享尚未创建，但文件已经保存。", ("文件已经保存",)),
            ("未能确认创建成功，不过分享已经创建。", ("分享已经创建",)),
            ("确认后再创建，消息已经发送。", ("消息已经发送",)),
            ("不能确认前一步但分享已经创建。", ("分享已经创建",)),
            ("分享尚未创建；文件已经分享。", ("文件已经分享",)),
            ("分享尚未创建，但分享已经创建，文件保存成功。", ("分享已经创建", "文件保存成功")),
        )
        for result in self._UNCONFIRMED_RECEIPTS:
            for answer, forbidden in cases:
                for entrypoint in self._ENTRYPOINTS:
                    with self.subTest(entrypoint=entrypoint, status=result["status"], answer=answer):
                        # 既有契约以整句为单位；不从自相矛盾的一句话中拼接半句。
                        # 保留相邻的正常说明，但不能因前半句是否定就放行后半句。
                        safe_sentence = "建议核对实际文件状态。"
                        public = self._project(entrypoint, safe_sentence + answer, result)
                        self.assertIn(format_public_result(result), public)
                        self.assertIn(safe_sentence, public)
                        for claim in forbidden:
                            self.assertNotIn(claim, public)

    def test_unconfirmed_receipts_keep_filenames_and_quoted_names(self) -> None:
        cases = (
            ("目标文件是 Blue.Streak.S01E01.mkv，分享尚未创建。",
             ("Blue.Streak.S01E01.mkv", "尚未创建"), ()),
            ("《创建成功》是片名，确认后再创建分享。",
             ("《创建成功》", "确认后再创建"), ()),
            ("待发送文件是 notes.saved.txt，确认后再发送。",
             ("notes.saved.txt", "确认后再发送"), ()),
            ("目标文件是 Blue.Streak.S01E01.mkv。分享已经创建。",
             ("Blue.Streak.S01E01.mkv",), ("分享已经创建",)),
        )
        for result in self._UNCONFIRMED_RECEIPTS:
            for answer, retained, forbidden in cases:
                for entrypoint in self._ENTRYPOINTS:
                    with self.subTest(entrypoint=entrypoint, status=result["status"], answer=answer):
                        public = self._project(entrypoint, answer, result)
                        self.assertIn(format_public_result(result), public)
                        for fragment in retained:
                            self.assertIn(fragment, public)
                        for claim in forbidden:
                            self.assertNotIn(claim, public)

    def test_completion_guard_checks_the_text_that_will_actually_be_displayed(self) -> None:
        from urllib.parse import quote, unquote
        cases = ((quote("分享已经创建。", safe=""), "分享已经创建"),
                 ("Ｔｈｅ ｓｈａｒｅ ｈａｓ ｂｅｅｎ ｃｒｅａｔｅｄ.", "The share has been created"))
        for raw, canonical in cases:
            for entrypoint in self._ENTRYPOINTS:
                with self.subTest(entrypoint=entrypoint, raw=raw):
                    public = self._project(entrypoint, raw, self._UNCONFIRMED_RECEIPTS[0])
                    self.assertNotIn(canonical, public)
                    self.assertNotIn(raw.rstrip("。."), public)
        for entrypoint in self._ENTRYPOINTS:
            public = self._project(entrypoint, quote("分享尚未创建。", safe=""), self._UNCONFIRMED_RECEIPTS[0])
            self.assertIn("尚未创建", unquote(public))

    def test_success_receipts_keep_normal_completion_and_followup(self) -> None:
        for status in ("success", "completed"):
            result = {"ok": True, "status": status, "summary": "操作已核验完成"}
            for completion in self._COMPLETION_SENTENCES:
                answer = completion + "\n可继续核对文件信息与分享有效期。"
                for entrypoint in self._ENTRYPOINTS:
                    with self.subTest(entrypoint=entrypoint, status=status, answer=answer):
                        public = self._project(entrypoint, answer, result)
                        self.assertIn(completion.rstrip("。."), public)
                        self.assertIn("核对文件信息与分享有效期", public)
                        if entrypoint == "format_partial_progress":
                            self.assertIn(format_public_result(result), public)


if __name__ == "__main__":
    unittest.main()




def test_partial_status_line_copied_into_the_answer_appears_once():
    """真实 8 轮约束任务中，模型模仿上一轮回答把状态行抄进正文，导致同一句出现两次。"""
    status = "找到 9 项可查看资源，部分来源未完成"
    status_line = f"部分完成：{status}"
    quality_fact = "已将画质条件修改为2160p。"
    for facts in (
        [quality_fact],
        [quality_fact, f"{status_line}；另一个来源有 2 项可查看资源。",
         "部分完成：找到 4 项可查看资源，来源检查未完成"],
    ):
        text = format_partial_progress(
            status, [], current_errors=[status],
            analysis_text=status_line + "\n\n" + "\n".join(facts),
        )
        # 按可见整行统计，不能只统计保留中文逗号的那一份。
        canonical_lines = [unicodedata.normalize("NFKC", line).strip() for line in text.splitlines()]
        assert canonical_lines.count(unicodedata.normalize("NFKC", status_line)) == 1, text
        for fact in facts:
            assert unicodedata.normalize("NFKC", fact) in canonical_lines, text
