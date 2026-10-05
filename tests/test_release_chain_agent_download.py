"""发布链 A：TG 下载结果隔离与 Agent pending 原子清理回归。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests  # noqa: F401 - 先隔离运行目录，禁止读取真实配置/数据库。
from app import database as db
from app.agent.confirmation import ConfirmationStore
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline
from app.agent.kernel.state import (
    CancellationToken,
    InMemorySessionStateStore,
    StalePublicationError,
    StateUpdate,
)
from app.bot import handlers
from app.modules import download_dispatcher as dispatcher
from app.routes import downloads_api
from tests.support import isolated_test_database


class TelegramDownloadResultBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_test_database())
        self.tracker = self.enterContext(
            patch("app.modules.download_tracker.get_download_tracker")
        ).return_value
        self.followup = self.enterContext(
            patch.object(handlers, "_download_follow_up_text", return_value="后续将跟踪任务")
        )

    @staticmethod
    def _request() -> int:
        item = dispatcher.normalize_download_url("magnet:?xt=urn:btih:" + "a" * 40)
        return dispatcher.create_request(item, "100", "1")["id"]

    def test_accepted_download_survives_first_delivery_failure(self) -> None:
        request_id = self._request()
        bot = SimpleNamespace(edit_message_text=Mock(side_effect=[RuntimeError("fake TG"), None]))
        with patch.object(dispatcher, "_submit_qb", return_value={
            "ok": True, "task_id": "a" * 40,
        }) as backend:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "qb")
        backend.assert_called_once()
        self.assertEqual(db.get_download_request(request_id)["qb_status"], "submitted")
        self.tracker.reload.assert_called_once()
        calls = bot.edit_message_text.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1], "投递重试必须保持同一业务回执")
        self.assertIn("下载任务已提交", calls[-1].args[0])
        self.assertNotIn("重试", calls[-1].args[0])

    def test_all_public_outcomes_ignore_legacy_ok_and_keep_safe_messages(self) -> None:
        cases = (
            ({"ok": False, "succeeded": ["qb"]}, "下载任务已提交"),
            ({"ok": True, "succeeded": ["qb"], "failed": ["guangya"]}, "下载任务部分提交"),
            ({"ok": True, "succeeded": ["qb"], "failed": ["guangya"],
              "outcome_unknown": True}, "下载提交结果待核对"),
            ({"ok": False, "outcome_unknown": True}, "下载提交结果待核对"),
            ({"ok": True, "duplicate": True, "existing_status": "submitted"}, "下载请求已存在"),
            ({"ok": True, "succeeded": [], "failed": ["qb"]}, "下载提交失败"),
        )
        for raw, title in cases:
            with self.subTest(title=title, raw=raw):
                bot = SimpleNamespace(edit_message_text=Mock(side_effect=[RuntimeError("fake TG"), None]))
                with patch.object(dispatcher, "dispatch_request", return_value={
                    **raw, "error": "private-provider-secret-not-for-TG",
                }) as dispatch:
                    handlers._dispatch_download_callback(bot, "100", "1", 7, "both")
                dispatch.assert_called_once_with(7, "both")
                calls = bot.edit_message_text.call_args_list
                self.assertEqual(calls[0], calls[1])
                text = calls[-1].args[0]
                self.assertIn(f"<b>{title}</b>", text)
                self.assertNotIn("private-provider-secret-not-for-TG", text)
                if raw.get("outcome_unknown"):
                    self.assertIn("勿直接重复提交", text)
                    self.assertNotIn("\n失败:", text)

    def test_guangya_explicit_rejection_keeps_raw_reason_for_web_and_public_receipt(self) -> None:
        request_id = self._request()
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "_submit_guangya", return_value={
            "ok": False, "error": "文件违规",
        }) as backend:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "guangya")

        backend.assert_called_once()
        row = db.get_download_request(request_id)
        self.assertEqual(row["gy_status"], "failed")
        self.assertEqual(row["error"], "guangya: 文件违规")
        text = bot.edit_message_text.call_args.args[0]
        self.assertIn(f"请求: #{request_id}", text)
        self.assertIn("原因: 光鸭返回：文件违规", text)

        stages = downloads_api._attention_stages(row)
        guangya_stage = next(stage for stage in stages if stage["key"] == "guangya")
        self.assertEqual(guangya_stage["error"], "文件违规")

    def test_partial_download_retries_only_telegram_receipt_and_keeps_qb_success(self) -> None:
        request_id = self._request()
        private_url = "https://private.example/download?token=fixture-token"
        private_token = "fixture-private-token"
        bot = SimpleNamespace(edit_message_text=Mock(side_effect=[RuntimeError("fake TG"), None]))
        with patch.object(dispatcher, "_submit_qb", return_value={
            "ok": True, "task_id": "a" * 40,
        }) as qb_backend, patch.object(dispatcher, "_submit_guangya", return_value={
            "ok": False,
            "error": f"文件违规；url={private_url}；token={private_token}",
        }) as guangya_backend:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "both")

        qb_backend.assert_called_once()
        guangya_backend.assert_called_once()
        row = db.get_download_request(request_id)
        self.assertEqual(row["qb_status"], "submitted")
        self.assertEqual(row["gy_status"], "failed")
        self.assertIn(private_url, row["error"])
        self.assertIn(private_token, row["error"])

        calls = bot.edit_message_text.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1], "TG 重发必须复用同一业务回执")
        text = calls[-1].args[0]
        self.assertIn("<b>下载任务部分提交</b>", text)
        self.assertIn(f"请求: #{request_id}", text)
        self.assertIn("成功: qBittorrent", text)
        self.assertIn("失败: 光鸭云盘", text)
        self.assertIn("原因: 光鸭返回：文件违规", text)
        self.assertNotIn(private_url, text)
        self.assertNotIn(private_token, text)

    def test_unknown_guangya_error_uses_web_details_but_unknown_result_stays_manual_review(self) -> None:
        request_id = self._request()
        private_url = "https://private.example/detail?token=unknown-fixture"
        private_token = "unknown-private-token"
        private_error = f"provider rejected request url={private_url} token={private_token}"
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "_submit_guangya", return_value={
            "ok": False, "error": private_error,
        }) as backend:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "guangya")

        backend.assert_called_once()
        text = bot.edit_message_text.call_args.args[0]
        self.assertIn("<b>下载提交失败</b>", text)
        self.assertIn("光鸭提交失败", text)
        self.assertIn(f"请到 Web 下载页查看请求 #{request_id} 的详细原因。", text)
        self.assertNotIn(private_url, text)
        self.assertNotIn(private_token, text)

        unknown_bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "dispatch_request", return_value={
            "ok": False,
            "succeeded": [],
            "failed": ["guangya"],
            "outcome_unknown": True,
            "error": private_error,
        }) as dispatch:
            handlers._dispatch_download_callback(
                unknown_bot, "100", "1", request_id, "guangya"
            )

        dispatch.assert_called_once_with(request_id, "guangya")
        unknown_text = unknown_bot.edit_message_text.call_args.args[0]
        self.assertIn("<b>下载提交结果待核对</b>", unknown_text)
        self.assertIn("勿直接重复提交", unknown_text)
        self.assertNotIn("光鸭提交失败", unknown_text)
        self.assertNotIn("请到 Web 下载页查看请求 #", unknown_text)
        self.assertNotIn(private_url, unknown_text)
        self.assertNotIn(private_token, unknown_text)

    def test_tracker_failure_does_not_change_or_block_receipt(self) -> None:
        self.tracker.reload.side_effect = RuntimeError("fake tracker")
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "dispatch_request", return_value={"succeeded": ["qb"]}) as dispatch:
            handlers._dispatch_download_callback(bot, "100", "1", 7, "qb")
        dispatch.assert_called_once()
        bot.edit_message_text.assert_called_once()
        self.assertIn("下载任务已提交", bot.edit_message_text.call_args.args[0])

    def test_permanent_delivery_failure_does_not_retry_backend_or_skip_tracker(self) -> None:
        request_id = self._request()
        bot = SimpleNamespace(edit_message_text=Mock(side_effect=RuntimeError("fake TG")))
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}) as backend:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "qb")
        backend.assert_called_once()
        self.tracker.reload.assert_called_once()
        self.assertEqual(bot.edit_message_text.call_count, 2)
        self.assertEqual(db.get_download_request(request_id)["status"], "submitted")

    def test_dispatch_exception_after_acceptance_is_unknown_not_rejected(self) -> None:
        request_id = self._request()
        real_dispatch = dispatcher.dispatch_request
        def accepted_then_error(*args, **kwargs):
            real_dispatch(*args, **kwargs)
            raise RuntimeError("fake late persistence error")
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}) as backend, \
             patch.object(dispatcher, "dispatch_request", side_effect=accepted_then_error) as dispatch:
            handlers._dispatch_download_callback(bot, "100", "1", request_id, "qb")
        backend.assert_called_once()
        dispatch.assert_called_once()
        self.tracker.reload.assert_called_once()
        self.assertEqual(db.get_download_request(request_id)["status"], "submitted")
        text = bot.edit_message_text.call_args.args[0]
        self.assertIn("下载提交结果待核对", text)
        self.assertIn("勿直接重复提交", text)
        self.assertNotIn("重试", text)

    def test_dispatch_exception_before_result_still_wakes_tracker_and_warns_unknown(self) -> None:
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "dispatch_request", side_effect=RuntimeError("fake failure")) as dispatch:
            handlers._dispatch_download_callback(bot, "100", "1", 7, "qb")
        dispatch.assert_called_once()
        self.tracker.reload.assert_called_once()
        self.assertIn("下载提交结果待核对", bot.edit_message_text.call_args.args[0])

    def test_followup_projection_failure_does_not_hide_accepted_result(self) -> None:
        self.followup.side_effect = RuntimeError("fake followup query")
        bot = SimpleNamespace(edit_message_text=Mock())
        with patch.object(dispatcher, "dispatch_request", return_value={"succeeded": ["qb"]}):
            handlers._dispatch_download_callback(bot, "100", "1", 7, "qb")
        self.tracker.reload.assert_called_once()
        self.assertIn("下载任务已提交", bot.edit_message_text.call_args.args[0])


class PendingPlanAtomicCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_test_database())
        self.ordinal = 0

    async def _fixture(self, kind):
        state = (InMemorySessionStateStore() if kind == "memory" else
                 SQLiteKernelStore(secret_provider=lambda: "release-chain-test-secret"))
        self.ordinal += 1
        lease, _ = await state.begin_turn(owner="owner-a", session_id=f"session-{self.ordinal}", request_id="first")
        async def progress(_payload):
            pass
        context = ToolCallContext(owner=lease.owner, session_id=lease.session_id,
                                  request_id=lease.request_id, turn_id=lease.turn_id,
                                  lease=lease, cancellation=CancellationToken(), report_progress=progress)
        clock = [10.0]
        effects = ConfirmationEffectPlanStore(ConfirmationStore(ttl_seconds=60, clock=lambda: clock[0]))
        execute = Mock(return_value={"ok": True, "summary": "fake accepted"})
        tool = KernelToolSpec(name="downloads.submit", domain="downloads", description="fake write",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "fake preview"}, snapshot_fingerprint="fixed"),
            execute_confirmed=execute)
        pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=state, effect_store=effects)
        prepared = await pipeline.execute(tool.name, {}, context=context)
        return state, context, pipeline, prepared.effect_plan.plan_id, clock, execute

    async def _pending(self, state, context):
        return (await state.load(owner=context.owner, session_id=context.session_id)).pending_effect_plan_id

    async def test_cancel_current_and_expired_current_clear_matching_pointer(self):
        for kind in ("memory", "sqlite"):
            for expired in (False, True):
                with self.subTest(store=kind, expired=expired):
                    state, context, pipeline, plan_id, clock, execute = await self._fixture(kind)
                    if expired:
                        clock[0] += 61
                    cancelled = await pipeline.cancel_effect(plan_id, lease=context.lease)
                    self.assertEqual(cancelled, not expired)
                    self.assertEqual(await self._pending(state, context), "")
                    execute.assert_not_called()

    async def test_old_or_expired_ticket_does_not_clear_new_same_generation_plan(self):
        for kind in ("memory", "sqlite"):
            for expired in (False, True):
                with self.subTest(store=kind, expired=expired):
                    state, context, pipeline, old_id, clock, execute = await self._fixture(kind)
                    if expired:
                        clock[0] += 61
                    new = await pipeline.execute("downloads.submit", {}, context=context)
                    new_id = new.effect_plan.plan_id
                    self.assertFalse(await pipeline.cancel_effect(old_id, lease=context.lease))
                    self.assertEqual(await self._pending(state, context), new_id)
                    claimed = pipeline.effect_store.claim(owner=context.owner, session_id=context.session_id,
                                                         generation=context.lease.generation, plan_id=new_id)
                    self.assertEqual(claimed.plan_id, new_id)
                    execute.assert_not_called()

    async def test_confirm_completion_clears_matching_pointer(self):
        for kind in ("memory", "sqlite"):
            with self.subTest(store=kind):
                state, context, pipeline, plan_id, _, execute = await self._fixture(kind)
                result = await pipeline.execute_confirmed(plan_id, context=context)
                self.assertTrue(result.outcome.public_content["ok"])
                self.assertEqual(await self._pending(state, context), "")
                execute.assert_called_once()

    async def test_same_generation_replacement_during_cleanup_is_preserved(self):
        for kind in ("memory", "sqlite"):
            for operation in ("cancel", "confirm"):
                with self.subTest(store=kind, operation=operation):
                    state, context, pipeline, old_id, _, execute = await self._fixture(kind)
                    commit, replacements = state.commit, []

                    async def publish_new_plan_before_commit(
                        lease, *, conversation=None, updates=(), _commit=commit,
                        _context=context, _pipeline=pipeline, _replacements=replacements,
                    ):
                        if any(update.key == "pending_effect_plan_id" for update in updates) and not _replacements:
                            new = _pipeline.effect_store.freeze(
                                owner=_context.owner, session_id=_context.session_id,
                                generation=lease.generation, tool_name="downloads.submit",
                                effect=ToolEffect.WRITE, arguments={},
                                prepared=PreparedEffect(
                                    preview={"summary": "new preview"}, snapshot_fingerprint="new-fixed",
                                ),
                            )
                            _replacements.append(new.plan_id)
                            await _commit(lease, updates=(StateUpdate(
                                "pending_effect_plan_id", new.plan_id),))
                        return await _commit(lease, conversation=conversation, updates=updates)

                    with patch.object(state, "commit", side_effect=publish_new_plan_before_commit):
                        if operation == "cancel":
                            self.assertTrue(await pipeline.cancel_effect(old_id, lease=context.lease))
                        else:
                            await pipeline.execute_confirmed(old_id, context=context)
                    self.assertEqual(await self._pending(state, context), replacements[0])
                    claimed = pipeline.effect_store.claim(
                        owner=context.owner, session_id=context.session_id,
                        generation=context.lease.generation, plan_id=replacements[0],
                    )
                    self.assertEqual(claimed.plan_id, replacements[0])
                    self.assertEqual(execute.call_count, int(operation == "confirm"))

    async def test_conditional_clear_still_rejects_stale_generation(self):
        for kind in ("memory", "sqlite"):
            with self.subTest(store=kind):
                state, context, _, old_id, _, _ = await self._fixture(kind)
                lease, _ = await state.begin_turn(owner=context.owner, session_id=context.session_id, request_id="next")
                await state.commit(lease, updates=(StateUpdate("pending_effect_plan_id", "new-plan"),))
                with self.assertRaises(StalePublicationError):
                    await state.commit(context.lease, updates=(StateUpdate(
                        "pending_effect_plan_id", old_id, mode="clear_if_equals"),))
                self.assertEqual(await self._pending(state, context), "new-plan")
