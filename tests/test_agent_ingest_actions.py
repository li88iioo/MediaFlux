"""原生分享接入与下载回执的真实确认边界；旧 Ingest 执行入口已退役。"""
from __future__ import annotations
import asyncio
import json
import os
from unittest.mock import patch
import pytest
import unittest
from app.repositories.download_requests import download_request_public_summary
from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import TurnView
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import EffectExecutor, ConfirmationEffectPlanStore
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.ports import sdk_effects
from app.agent.models import ToolResult
from app.agent.public_view import format_public_result, public_result_state
from app.bot.agent_adapter import _render_turn
from app.bot.telegram_markdown import render_telegram_markdown
from app.modules import download_dispatcher as dispatcher, share_transfer
from tests.support import isolated_test_database, initialized_web_runtime
from tests.test_agent_sdk_cloud_effects import _context, _deny_network
from tests.test_telegram_guangya_share import FakeShareClient, SHARE_URL, SECRET_TOKEN, SIGNED_URL, inspected_payload
_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


@pytest.fixture
def share_backend(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    with initialized_web_runtime():
        client = FakeShareClient(inspected_payload(201))
        monkeypatch.setattr(share_transfer, "GuangYaClient", lambda: client)
        monkeypatch.setattr(share_transfer, "get", lambda key, default="": {
            "GY_SHARE_TARGET_DIR": "target", "GY_SHARE_TARGET_DIR_NAME": "分享目标",
            "GY_ORGANIZE_TARGET_DIR": "",
        }.get(key, default))
        yield client


def share_executor(store):
    return EffectExecutor(catalog=ToolCatalog([sdk_effects.share_transfer_tool()]), state_store=store,
                          reference_store=store, effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))


def test_native_share_bridge_cold_confirm_and_repeated_approval_write_once(share_backend, tmp_path, monkeypatch):
    from app.agent.kernel.sandbox import _SDKRequests
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        from app import sdk
        with sdk.call_owner(context.owner):
            inspected = sdk.cloud.inspect_share(SHARE_URL)
        assert inspected["count"] == 201 and inspected["scope"] == "top_level" and inspected["has_more"] is False
        assert inspected["files"][-1]["id"] == "file-201"
        assert SECRET_TOKEN not in json.dumps(inspected) and SIGNED_URL not in json.dumps(inspected)
        (tmp_path / "run").mkdir()
        bridge = _SDKRequests(context, tmp_path, share_executor(store))
        bridge.uid = os.getuid()
        request = tmp_path / "run/mf-requests/share.json"
        request.write_text(json.dumps({"method": "cloud.transfer_share", "arguments": {
            "url": SHARE_URL, "file_ids": ["file-201", "file-1"],
        }}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
        finally:
            await bridge.close()
        assert plan is not None and plan.preview["data"]["count"] == 2
        assert not share_backend.restore_calls
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
        rendered = json.dumps(plan.public_dict())
        assert SECRET_TOKEN not in rendered and SIGNED_URL not in rendered and SHARE_URL not in rendered
        # 确认只需原持久计划，新 executor 和空公共预览缓存仍须复核原内容。
        monkeypatch.setattr(share_transfer, "_store", share_transfer.ShareTransferPreviewStore())
        cold = share_executor(SQLiteKernelStore())
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["data"]["success"]
        assert share_backend.restore_calls[0][1] == ["file-1", "file-201"]
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert len(share_backend.restore_calls) == 1
        assert len(share_backend.inspect_calls) == 3  # 原脚本和整次 prepare 未被重放。
        return result.outcome.public_content["data"]["request_id"]
    request_id = asyncio.run(run())
    from app.sdk import activity
    observed = activity.timeline("download", request_id)
    assert observed["ok"]
    assert "未建立媒体库可见性复核" in json.dumps(observed, ensure_ascii=False)


@pytest.mark.parametrize("change", ["cancel", "foreign_owner", "file", "target", "cancel_during_read"])
def test_native_share_rejects_changed_or_cancelled_confirmation_without_write(share_backend, monkeypatch, change):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        executor = share_executor(store)
        plan = (await executor.prepare("cloud.transfer_share", {"url": SHARE_URL, "file_ids": ["file-1"]}, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await executor.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "foreign_owner":
            context = await _context(store, owner="foreign-owner")
        elif change == "file":
            share_backend.inspected["files"][0]["size"] += 1
        elif change == "target":
            monkeypatch.setattr(share_transfer, "get", lambda *_a: "other-target")
        else:
            original = share_backend.inspect_share
            def cancel_read(url):
                context.cancellation.cancel()
                return original(url)
            share_backend.inspect_share = cancel_read
        try:
            await executor.execute_confirmed(plan.plan_id, context=context)
        except (ToolPipelineError, asyncio.CancelledError):
            pass
        assert share_backend.restore_calls == []
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize(
    "qb_ok,gy_ok,repeat,tool_status,dispatch_status,public_state,summary",
    [
        pytest.param(True, False, False, "partial", "partial", "warning", "仅部分目标已提交", id="qb-only"),
        pytest.param(False, True, False, "partial", "partial", "warning", "仅部分目标已提交", id="guangya-only"),
        pytest.param(True, True, False, "accepted", "submitted", "submitted", "已提交", id="both-success"),
        pytest.param(False, False, False, "unavailable", "failed", "failed", "未完成提交", id="both-failed"),
        pytest.param(True, True, True, "conflict", "duplicate", "failed", "未完成提交", id="duplicate-both"),
        pytest.param(True, False, True, "conflict", "duplicate", "failed", "未完成提交", id="duplicate-qb-only"),
        pytest.param(False, True, True, "conflict", "duplicate", "failed", "未完成提交", id="duplicate-guangya-only"),
    ],
)
def test_direct_submission_receipt_matches_real_dispatch(
    qb_ok, gy_ok, repeat, tool_status, dispatch_status, public_state, summary,
):
    """真实落盘与分发；只替换后端边界，确认结果由 Web/TG 共用生产投影。"""
    with (
        isolated_test_database(),
        patch.dict(os.environ, {"QB_URL": "http://qb.invalid"}),
        patch("socket.socket.connect", side_effect=AssertionError("禁止外联")),
        patch("app.indexers.downloads.GuangYaClient") as cloud_client,
        patch.object(dispatcher, "_submit_qb", return_value={"ok": qb_ok}) as qb,
        patch.object(dispatcher, "_submit_guangya", return_value={"ok": gy_ok}) as gy,
    ):
        cloud_client.return_value.logged_in = True
        from app.agent.kernel.persistence import SQLiteKernelStore
        from tests.test_agent_sdk_effects import context_for, executor

        async def execute():
            store = SQLiteKernelStore()
            context = await context_for(store, owner="owner-a", session="direct-receipt")
            effects = executor(store)
            arguments = {"url": _MAGNET + "&dn=Fixture.S01E02.mp4", "target": "both"}
            prepared = await effects.prepare("downloads.submit", arguments, context=context)
            plan = prepared.effect_plan
            assert plan is not None and plan.preview["data"]["backends"] == {"qb": True, "guangya": True}
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
            qb.assert_not_called()
            gy.assert_not_called()
            first = await effects.execute_confirmed(plan.plan_id, context=context)
            if repeat:
                # 另一张卡表达新的提交意图；活跃请求仍由业务核心去重。
                prepared = await effects.prepare("downloads.submit", arguments, context=context)
                assert prepared.effect_plan is not None
                result = await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
            else:
                result = first
            fields = ("ok", "status", "summary", "data", "error")
            return (ToolResult(**{key: item.outcome.public_content[key] for key in fields})
                    for item in (first, result)), context

        pair, context = asyncio.run(execute())
        first, result = pair
        request_number = result.data["request_id"]
        assert request_number == first.data["request_id"]
        assert result.data["title"] == "Fixture.S01E02.mp4"
        assert "Fixture.S01E02.mp4" in format_public_result({
            "ok": result.ok, "status": result.status, "summary": result.summary, "data": result.data,
        })
        assert result.data["created"] is (not repeat)
        assert result.data["duplicate"] is repeat
        qb.assert_called_once()
        gy.assert_called_once()
        row = db.get_download_request(request_number)
        assert row is not None
        assert row["qb_status"] == ("submitted" if qb_ok else "failed")
        assert row["gy_status"] == ("submitted" if gy_ok else "failed")
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM download_log").fetchone()[0] == 2
        assert result.data["status"] == dispatch_status
        assert result.ok is (not repeat and (qb_ok or gy_ok))
        assert result.data["succeeded"] == ([] if repeat else [
            name for name, ok in (("qb", qb_ok), ("guangya", gy_ok)) if ok
        ])
        assert result.data["failed"] == ([] if repeat else [
            name for name, ok in (("qb", qb_ok), ("guangya", gy_ok)) if not ok
        ])
        assert (result.status, result.summary) == (tool_status, f"下载请求 #{request_number} {summary}")

        public = dict(DefaultProjector().project(result).public_content)
        receipt = format_public_result(public)
        assert public_result_state(public) == public_state
        assert result.summary in receipt
        view = TurnView(
            session_id=context.session_id, turn_id="direct-receipt", request_id="direct-receipt",
            status="effect_completed" if result.ok else "failed", effect_result=public,
        )
        web = view.to_dict()["effect_result"]
        assert web == public
        assert public_result_state(web) == public_state
        assert _render_turn(view) == render_telegram_markdown(receipt)
        assert ("⚠️" in receipt) is (public_state == "warning")


class DownloadRequestPublicStatusTests(unittest.TestCase):
    def _status_row(self, **overrides):
        row = {
            "id": 7,
            "title": "Safe title",
            "kind": "magnet",
            "targets": "both",
            "status": "submitted",
            "qb_status": "submitted",
            "gy_status": "failed",
            "organize_status": "pending",
            "strm_status": "pending",
            "updated_at": "2026-09-01 12:00:00",
            "error": "guangya: 文件违规; url=https://private.example/?token=secret-token",
            "source_value": _MAGNET,
        }
        row.update(overrides)
        return row


    def test_status_exposes_safe_partial_error_without_overwriting_stage_status(
        self
    ):
        row = self._status_row()

        result = download_request_public_summary(row)

        request = result
        self.assertEqual(request["status"], "submitted")
        self.assertEqual(request["qb_status"], "submitted")
        self.assertEqual(request["guangya_status"], "failed")
        self.assertEqual(request["error"], "光鸭返回：文件违规")
        self.assertNotIn("private.example", repr(result))
        self.assertNotIn("secret-token", repr(result))


    def test_status_exposes_safe_manual_review_reason(self):
        row = self._status_row(
            status="manual_review",
            qb_status="outcome_unknown",
            gy_status="failed",
            error="qb: task_id=private-qb-task; guangya: 文件违规; url=https://private.example/x",
        )

        result = download_request_public_summary(row)

        request = result
        self.assertEqual(request["status"], "manual_review")
        self.assertEqual(request["qb_status"], "outcome_unknown")
        self.assertEqual(request["guangya_status"], "failed")
        self.assertEqual(
            request["error"],
            "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        )
        self.assertNotIn("private-qb-task", repr(result))
        self.assertNotIn("private.example", repr(result))


    def test_status_exposes_safe_outcome_unknown_reason(self):
        row = self._status_row(
            status="submitted",
            qb_status="outcome_unknown",
            gy_status="not_started",
            error="qb: provider accepted but task_id=private-qb-task is unknown",
        )

        result = download_request_public_summary(row)

        request = result
        self.assertEqual(request["status"], "submitted")
        self.assertEqual(request["qb_status"], "outcome_unknown")
        self.assertEqual(request["guangya_status"], "not_started")
        self.assertEqual(
            request["error"],
            "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        )
        self.assertNotIn("private-qb-task", repr(result))


    def test_status_does_not_invent_failure_for_waiting_or_skipped_rows(self):
        cases = (
            {"status": "pending", "qb_status": "pending", "gy_status": "pending"},
            {"status": "pending", "qb_status": "not_started", "gy_status": "not_started"},
            {"status": "cancelled", "qb_status": "cancelled", "gy_status": "cancelled"},
            {"status": "skipped", "qb_status": "skipped", "gy_status": "skipped"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                row = self._status_row(error="", **overrides)
                result = download_request_public_summary(row)
                self.assertEqual(result["error"], "")


    def test_status_returns_only_public_request_projection(self):
        row = {
            "id": 7,
            "title": "Safe title",
            "kind": "magnet",
            "targets": "qb",
            "status": "downloading",
            "qb_status": "downloading",
            "gy_status": "not_started",
            "organize_status": "pending",
            "strm_status": "pending",
            "updated_at": "2026-09-01 12:00:00",
            "source_value": _MAGNET,
        }
        result = download_request_public_summary(row)
        self.assertEqual(result["status"], "downloading")
        self.assertEqual(result["request_number"], 7)
        self.assertNotIn("source_value", result)
        self.assertNotIn("magnet:?", repr(result))



def test_unexpired_ingest_ticket_is_invalidated_without_native_reinterpretation(share_backend):
    from app.agent.kernel.capabilities import ToolEffect
    from app.agent.kernel.tool_calls import PreparedEffect
    from app.agent.kernel.state_types import _PendingEffectTransition
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = share_executor(store)
        reference = await store.put(owner=context.owner, session_id=context.session_id, kind="ingest_snapshot",
                                    value={"source_type": "guangya_share", "preview_id": "old-preview"}, ttl_seconds=900)
        ticket = effects.effect_store.freeze(owner=context.owner, session_id=context.session_id,
            generation=context.lease.generation, tool_name="ingest.submit", effect=ToolEffect.DANGER,
            arguments={"source_type": "guangya_share", "ingest_snapshot_ref": reference.ref},
            prepared=PreparedEffect(preview={"ok": True, "summary": "旧分享确认卡"},
                                    snapshot_fingerprint="old-share-context"))
        await store.commit(context.lease, updates=(_PendingEffectTransition(
            "pending_effect_plan_id", ticket.plan_id, plan_generation=context.lease.generation,
            goal={"role": "user", "content": "旧分享转存目标"}),))
        cold = share_executor(SQLiteKernelStore())
        assert await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
        with pytest.raises(ToolPipelineError) as failure:
            await cold.execute_confirmed(ticket.plan_id, context=context)
        assert failure.value.code == "tool_not_found" and failure.value.execution_started is False
        assert await cold.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        assert not share_backend.inspect_calls and not share_backend.restore_calls
    asyncio.run(run())
