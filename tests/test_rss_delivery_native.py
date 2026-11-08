"""RSS 原生条目投递：完整范围、原始回执、冷确认及真实工作区 RPC。"""

import asyncio
import json
import os
import shlex
from unittest.mock import patch

import pytest

from app import database as db
from app import sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.modules import rss_delivery
from app.modules.rss import RSSEngine
from tests import test_agent_session_result as session_result_tests
from tests.support import isolated_test_database, seed_rss_entry_state
from tests.test_agent_sdk_effects import context_for, executor

native_workspace = session_result_tests.native_workspace

RUNTIME = {"url": "http://qb.invalid", "default_method": "qb", "category": "rss"}


def feed(method="qb"):
    return db.add_rss_subscription(
        "公开订阅" * 50,
        "https://feed.invalid/rss?passkey=PRIVATE",
        download_method=method,
    )


def entry(sid, number, title=""):
    identifier = db.add_rss_entry_with_media(
        sid,
        title or f"Episode {number}",
        f"guid-{sid}-{number}",
        payload=json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{number:040x}"}),
    )["id"]
    assert identifier is not None
    return identifier


def _workspace_call(operation, arguments, language):
    if language == "python":
        return f"from mf import rss\nrss.{operation}(**{arguments!r})"
    words = ["mf", f"rss.{operation}"]
    for key, value in arguments.items():
        words.extend(["--" + key.replace("_", "-"), json.dumps(value) if isinstance(value, list) else str(value)])
    return shlex.join(words)


@pytest.mark.parametrize("operation,scope,language", [
    ("refresh", "selected", "python"), ("refresh", "all", "shell"),
    ("download", "selected", "python"), ("download", "pending", "shell"),
    ("retry", "selected", "python"), ("retry", "pending", "shell"),
])
def test_workspace_optional_defaults_prepare_full_scope_and_cancel(native_workspace, operation, scope, language):
    async def run():
        sid = feed()
        eid = entry(sid, 1)
        if operation == "retry":
            db.record_rss_entry_failure(eid, "qb_unavailable", True)
        row = db.get_rss_entry(eid)
        assert row is not None
        before = dict(row)
        arguments = ({"subscription_ids": [sid]} if scope == "selected" else {"scope": "all_configured"}) if operation == "refresh" else ({"entry_ids": [eid]} if scope == "selected" else {})
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        pipe = ToolPipeline(catalog=effects.catalog, state_store=store, effect_store=effects.effect_store)
        pipe.catalog.register(sandbox.run_code_tool(pipe))
        with patch.object(rss_delivery, "execute", side_effect=AssertionError("取消后不能提交")) as submit:
            result = await pipe.execute(sandbox.RUN_CODE_TOOL,
                {"code": _workspace_call(operation, arguments, language), "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None, result.outcome.model_content
            assert plan.tool_name == "rss." + operation
            assert plan.arguments == rss_delivery.normalize(operation, arguments)
            preview = plan.preview["data"]
            if operation == "refresh":
                assert preview["subscription_count"] == 1
                assert [item["subscription_id"] for item in preview["subscriptions"]] == [sid]
            else:
                assert preview["selected_count"] == 1 and preview["entry_ids"] == [eid]
                assert preview["has_more"] is False
            cold = executor(SQLiteKernelStore())
            restored = await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
            assert restored is not None and restored.arguments == plan.arguments
            assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=context)
            submit.assert_not_called()
        row = db.get_rss_entry(eid)
        assert row is not None and dict(row) == before

    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")):
        asyncio.run(run())


@pytest.mark.parametrize("operation,arguments,language", [
    ("refresh", {}, "python"),
    ("refresh", {"subscription_ids": []}, "shell"),
    ("refresh", {"subscription_ids": [1], "scope": "all_configured"}, "python"),
    ("download", {"entry_ids": []}, "python"),
    ("download", {"entry_ids": [1], "limit": 1}, "shell"),
    ("download", {"entry_ids": [1], "subscription_id": 1}, "python"),
    ("retry", {"entry_ids": []}, "shell"),
    ("retry", {"entry_ids": [1], "limit": 1}, "python"),
    ("retry", {"entry_ids": [1], "subscription_id": 1}, "shell"),
])
def test_workspace_invalid_rss_scope_never_freezes_or_executes(native_workspace, operation, arguments, language):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        pipe = ToolPipeline(catalog=effects.catalog, state_store=store, effect_store=effects.effect_store)
        pipe.catalog.register(sandbox.run_code_tool(pipe))
        with patch.object(rss_delivery, "prepare", side_effect=AssertionError("无效范围不能读取业务快照")) as prepare, patch.object(rss_delivery, "execute", side_effect=AssertionError("无效范围不能提交")) as submit:
            result = await pipe.execute(sandbox.RUN_CODE_TOOL,
                {"code": _workspace_call(operation, arguments, language), "language": language}, context=context)
            assert result.effect_plan is None
            assert "invalid_arguments" in result.outcome.model_content
            assert await pipe.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
            prepare.assert_not_called()
            submit.assert_not_called()

    with isolated_test_database():
        asyncio.run(run())


def test_entry_pages_preserve_full_titles_real_ids_and_delivery_without_source_secrets():
    with isolated_test_database():
        sid = feed()
        ids = [
            entry(sid, i, "长标题" * 160 + f" {i} passkey=PRIVATE") for i in range(135)
        ]
        options: dict = {"limit": 51, "status": "all", "subscription_id": sid}
        found = []
        while True:
            page = sdk.rss.entries(**options)["data"]
            assert page["total"] == 135
            for item in page["items"]:
                assert len(item["title"]) > 480 and item["subscription_id"] == sid
                assert "PRIVATE" not in json.dumps(item)
                assert item["delivery"]["status"] == ""
                found.append(item["id"])
            if not page["has_more"]:
                assert page["next"] is None
                break
            options = page["next"]
        assert found == ids[::-1]
        assert sdk.rss.entries(subscription_id=sid, offset=135)["data"]["total"] == 135
        assert (
            sdk.rss.entries(subscription_id=sid, query=" 123 ")["data"]["items"][0][
                "id"
            ]
            == ids[123]
        )


def test_download_configuration_failure_does_not_freeze_an_unexecutable_plan():
    async def run():
        sid = feed()
        eid = entry(sid, 1)
        effects = executor(SQLiteKernelStore())
        context = await context_for(effects.state_store)
        with pytest.raises(ToolPipelineError) as failed:
            await effects.prepare("rss.download", {"entry_ids": [eid]}, context=context)
        assert failed.value.code == "not_configured"
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        row = db.get_rss_entry(eid)
        assert row is not None and row["status"] == "pending"

    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "missing")):
        asyncio.run(run())


def test_full_selected_batch_claims_both_targets_and_mark_has_no_old_count_cutoff():
    with (
        isolated_test_database(),
        patch(
            "app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")
        ),
    ):
        qb, gy = feed(), feed("guangya")
        ids = [entry(qb if i % 2 else gy, i) for i in range(1, 72)]
        preview, fingerprint = rss_delivery.prepare("download", {"entry_ids": ids})
        assert (
            preview["data"]["selected_count"] == 71
            and preview["data"]["target"] == "both"
        )
        observed = []

        def submit(_engine, row, **kwargs):
            observed.append((row["id"], row["download_method"]))
            assert kwargs["entry_already_claimed"]
            return {"pending": True, "ok": False}

        with patch.object(RSSEngine, "_download_entry", submit):
            result = rss_delivery.execute("download", {"entry_ids": ids}, fingerprint)
        assert result["status"] == "in_progress" and result["data"]["pending"] == 71
        assert {i for i, _ in observed} == set(ids)
        assert {method for _, method in observed} == {"qb", "guangya"}
        with pytest.raises(rss_delivery.RSSSubscriptionError):
            rss_delivery.execute("download", {"entry_ids": ids}, fingerprint)
        more = [entry(qb, i) for i in range(100, 171)]
        assert sdk.rss.mark(more, True)["data"]["affected"] == 71
        assert all(
            (row := db.get_rss_entry(i)) is not None and row["status"] == "skipped"
            for i in more
        )


@pytest.mark.parametrize(
    "change", ["cold", "row", "configuration", "owner", "session", "cancel", "unknown"]
)
def test_effect_confirmation_cold_restart_checks_scope_and_never_replays(change):
    async def run():
        sid = feed()
        eid = entry(sid, 1)
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        proposal = await effects.prepare(
            "rss.download", {"entry_ids": [eid]}, context=context
        )
        plan = proposal.effect_plan
        assert (
            plan is not None
            and (row := db.get_rss_entry(eid)) is not None
            and row["status"] == "pending"
        )
        if change == "row":
            seed_rss_entry_state(eid, "submitting")
        elif change == "configuration":
            db.update_rss_subscription(sid, {"gy_target_dir": "changed"})
        elif change in {"owner", "session"}:
            context = await context_for(
                store,
                owner="other" if change == "owner" else context.owner,
                session="other" if change == "session" else context.session_id,
            )
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        calls = []

        def submit(_engine, row, **kwargs):
            calls.append(row["id"])
            if change == "unknown":
                db.record_rss_entry_failure(eid, "qb_outcome_unknown", False)
                return {"ok": False, "review_required": True}
            return {"pending": True}

        rebuilt = executor(SQLiteKernelStore())
        with patch.object(RSSEngine, "_download_entry", submit):
            if change in {"owner", "session", "cancel", "row", "configuration"}:
                with pytest.raises(ToolPipelineError):
                    await rebuilt.execute_confirmed(plan.plan_id, context=context)
                assert not calls
            else:
                result = await rebuilt.execute_confirmed(plan.plan_id, context=context)
                assert result.outcome.public_content["status"] == (
                    "review_required" if change == "unknown" else "in_progress"
                )
                assert calls == [eid]
            with pytest.raises(ToolPipelineError):
                await rebuilt.execute_confirmed(plan.plan_id, context=context)
        if change == "unknown":
            assert (
                rss_delivery.prepare("retry", {"limit": 10})[0]["status"]
                == "no_changes"
            )

    with (
        isolated_test_database(),
        patch(
            "app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")
        ),
    ):
        asyncio.run(run())


def test_refresh_complete_scope_persists_exact_run_receipt_and_never_falls_back():
    with isolated_test_database():
        ids = [feed() for _ in range(35)]
        preview, fingerprint = rss_delivery.prepare(
            "refresh", {"scope": "all_configured"}
        )
        assert preview["data"]["subscription_count"] == 35
        with patch.object(
            RSSEngine, "refresh", return_value={"total": 2, "new": 1, "skipped": 0}
        ) as refresh:
            result = rss_delivery.execute(
                "refresh", {"scope": "all_configured"}, fingerprint
            )
        assert refresh.call_count == 35 and result["data"]["refreshed"] == 35
        assert {
            row["subscription_id"] for row in result["data"]["subscriptions"]
        } == set(ids)
        run_id = result["data"]["run_id"]
        assert sdk.rss.activity(run_id=run_id) == result
        assert sdk.rss.activity(run_id=run_id + 1)["status"] == "not_found"
        row = db.get_task_run(run_id)
        assert row is not None and json.loads(row["result"]) == result


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="Linux root真实工作区RPC")
def test_root_workspace_proposal_cold_confirm_and_full_persisted_readback(tmp_path):
    async def run():
        sid = feed()
        eid = entry(sid, 1, "全文" * 600)
        store = SQLiteKernelStore()
        context = await context_for(store)
        root = tmp_path / "rpc"
        (root / "run").mkdir(parents=True)
        bridge = sandbox._SDKRequests(context, root, executor(store))
        bridge.uid = os.getuid()
        request = root / "run/mf-requests/mark.json"
        request.write_text(
            json.dumps(
                {
                    "method": "rss.mark",
                    "arguments": {"entry_ids": [eid], "processed": True},
                }
            )
        )
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal and bridge.proposal.effect_plan
            plan = bridge.proposal.effect_plan
            assert (row := db.get_rss_entry(eid)) is not None and row[
                "status"
            ] == "pending"
        finally:
            await bridge.close()
        result = await executor(SQLiteKernelStore()).execute_confirmed(
            plan.plan_id, context=context
        )
        assert result.outcome.public_content["data"]["affected"] == 1
        catalog = ToolCatalog()
        pipeline = ToolPipeline(
            catalog=catalog, state_store=store, reference_store=store
        )
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": f'from mf import rss\nv = rss.entries(subscription_id={sid}, status="all")["data"]["items"][0]\nprint(v["id"], v["status"], len(v["title"]))'
            },
            context=context,
        )
        data = json.loads(result.outcome.model_content)["data"]
        assert (
            data["exit_code"] == 0 and data["stdout"].strip() == f"{eid} skipped 1200"
        )
        workspace, _ = sandbox._workspace_location(context.owner, context.session_id)
        response = json.loads(
            (workspace / data["sdk_calls"][0]["response_path"].lstrip("/")).read_text()
        )
        assert "全文" * 600 in json.dumps(response, ensure_ascii=False)
        assert (row := db.get_rss_entry(eid)) is not None and row[
            "title"
        ] == "全文" * 600

    with isolated_test_database():
        asyncio.run(run())
