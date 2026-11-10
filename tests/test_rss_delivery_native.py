"""RSS 原生条目投递：完整范围、原始回执、冷确认及真实工作区 RPC。"""

import asyncio
import json
import os
import shlex
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import rss as rss_repo
from app import sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state_types import CancellationToken
from app.clients.qbittorrent import TorrentAddResult
from app.modules import rss_delivery
from app.modules import download_dispatcher
from app.modules.rss import RSSEngine
from tests import test_agent_session_result as session_result_tests
from tests.support import assert_present, isolated_test_database, seed_rss_entry_state
from tests.test_agent_sdk_effects import context_for, executor

native_workspace = session_result_tests.native_workspace

RUNTIME = {"url": "http://qb.invalid", "default_method": "qb", "category": "rss"}


def feed(method="qb"):
    return rss_repo.add_rss_subscription(
        "公开订阅" * 50,
        "https://feed.invalid/rss?passkey=PRIVATE",
        download_method=method,
    )


def entry(sid, number, title=""):
    identifier = rss_repo.add_rss_entry_with_media(
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


@pytest.mark.parametrize("operation", ["download", "retry"])
def test_sourceless_entries_fail_before_confirmation_without_claiming_the_batch(operation):
    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")):
        sid = feed()
        valid = entry(sid, 1)
        invalid = rss_repo.add_rss_entry_with_media(sid, "没有下载来源", "no-source", payload="{}")["id"]
        ids = [valid, invalid]
        if operation == "retry":
            for eid in ids:
                rss_repo.record_rss_entry_failure(eid, "qb_unavailable", True)
        before = [dict(assert_present(rss_repo.get_rss_entry(eid))) for eid in ids]
        with pytest.raises(rss_delivery.RSSSubscriptionError, match=f"{invalid}.*下载链接"):
            rss_delivery.prepare(operation, {"entry_ids": ids})
        assert [dict(assert_present(rss_repo.get_rss_entry(eid))) for eid in ids] == before


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
            rss_repo.record_rss_entry_failure(eid, "qb_unavailable", True)
        row = rss_repo.get_rss_entry(eid)
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
        row = rss_repo.get_rss_entry(eid)
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
        row = rss_repo.get_rss_entry(eid)
        assert row is not None and row["status"] == "pending"

    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "missing")):
        asyncio.run(run())


def test_full_selected_batch_claims_both_targets_and_mark_has_no_old_count_cutoff():
    with (
        isolated_test_database(),
        patch(
            "app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")
        ),
        patch.object(download_dispatcher, "guangya_download_credential_generation", return_value=31),
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
            (row := rss_repo.get_rss_entry(i)) is not None and row["status"] == "skipped"
            for i in more
        )


@pytest.mark.parametrize("operation", ["download", "retry"])
@pytest.mark.parametrize("accepted", [True, False], ids=["accepted", "unknown"])
def test_batch_cancellation_preserves_started_receipt_and_restores_unstarted_entries(operation, accepted):
    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")):
        ids = [entry(feed(), number) for number in range(1, 4)]
        if operation == "retry":
            for identifier in ids:
                rss_repo.record_rss_entry_failure(identifier, "qb_unavailable", True)
        before = {identifier: dict(assert_present(rss_repo.get_rss_entry(identifier))) for identifier in ids}
        args = {"entry_ids": ids}
        _, fingerprint = rss_delivery.prepare(operation, args)
        cancellation = CancellationToken()

        def submit(**kwargs):
            cancellation.cancel("用户停止批量投递")
            return TorrentAddResult(True) if accepted else TorrentAddResult(False, "qb_outcome_unknown", False)

        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed", side_effect=submit) as backend:
            result = rss_delivery.execute(operation, args, fingerprint, check_cancelled=cancellation.raise_if_cancelled)
        assert backend.call_count == 1
        assert result["status"] == ("partial" if accepted else "review_required")
        assert result["data"]["cancelled"] == 2 and result["data"]["submitted"] == int(accepted)
        first = assert_present(rss_repo.get_rss_entry(ids[-1]))
        assert first["status"] == ("downloaded" if accepted else "failed")
        if not accepted:
            assert result["data"]["outcome_unknown"] == 1 and first["failure_code"] == "qb_outcome_unknown"
        restored_fields = ("status", "submitted_at", "failure_code", "failure_retryable", "failed_at", "retry_count", "download_request_id", "download_request_key")
        for identifier in ids[:-1]:
            current = assert_present(rss_repo.get_rss_entry(identifier))
            assert all(current[key] == before[identifier][key] for key in restored_fields)
        remaining = {"entry_ids": ids[:-1]}
        _, fresh = rss_delivery.prepare(operation, remaining)
        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed", return_value=TorrentAddResult(True)) as resumed:
            result = rss_delivery.execute(operation, remaining, fresh)
        assert result["status"] == "completed" and resumed.call_count == 2


@pytest.mark.parametrize("operation", ["download", "retry"])
def test_cancellation_after_claim_before_first_submission_restores_complete_scope(operation):
    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")):
        sid = feed()
        ids = [entry(sid, number) for number in range(1, 3)]
        if operation == "retry":
            for identifier in ids:
                rss_repo.record_rss_entry_failure(identifier, "qb_unavailable", True)
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET processed=NULL WHERE id=?", (ids[0],))
        before = {identifier: dict(assert_present(rss_repo.get_rss_entry(identifier))) for identifier in ids}
        args = {"entry_ids": ids}
        _, fingerprint = rss_delivery.prepare(operation, args)
        cancellation = CancellationToken()
        claim_name = "claim_pending_rss_entries" if operation == "download" else "claim_retryable_failed_rss_entries"
        original_claim = getattr(rss_repo, claim_name)
        def claim(*args, **kwargs):
            rows = original_claim(*args, **kwargs)
            cancellation.cancel("认领后取消")
            return rows
        with patch.object(rss_repo, claim_name, side_effect=claim), patch.object(RSSEngine, "_download_entry") as backend:
            result = rss_delivery.execute(operation, args, fingerprint, check_cancelled=cancellation.raise_if_cancelled)
        backend.assert_not_called()
        assert not result["ok"] and result["status"] == "cancelled" and result["data"]["cancelled"] == 2
        assert all(dict(assert_present(rss_repo.get_rss_entry(identifier))) == before[identifier] for identifier in ids)


def test_batch_cancellation_preserves_late_receipts_and_reassigned_claims():
    with isolated_test_database(), patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=(RUNTIME, "")):
        sid = feed()
        ids = [entry(sid, number) for number in range(1, 5)]
        args = {"entry_ids": ids}
        _, fingerprint = rss_delivery.prepare("download", args)
        cancellation = CancellationToken()
        def submit(**kwargs):
            rss_repo.record_rss_entry_failure(ids[0], "qb_outcome_unknown", False)
            with db.get_conn() as conn:
                conn.execute("UPDATE rss_entries SET submitted_at='2001-01-01 01:02:03' WHERE id=?", (ids[1],))
            cancellation.cancel("停止剩余投递")
            return TorrentAddResult(True)
        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed", side_effect=submit) as backend:
            result = rss_delivery.execute("download", args, fingerprint, check_cancelled=cancellation.raise_if_cancelled)
        backend.assert_called_once()
        assert result["status"] == "partial" and result["data"]["cancelled"] == 3
        assert assert_present(rss_repo.get_rss_entry(ids[-1]))["status"] == "downloaded"
        late = assert_present(rss_repo.get_rss_entry(ids[0]))
        assert late["status"] == "failed" and late["failure_code"] == "qb_outcome_unknown"
        reassigned = assert_present(rss_repo.get_rss_entry(ids[1]))
        assert reassigned["status"] == "submitting" and reassigned["submitted_at"] == "2001-01-01 01:02:03"
        assert assert_present(rss_repo.get_rss_entry(ids[2]))["status"] == "pending"


@pytest.mark.parametrize("source", ["pending", "unlinked_retry", "linked_retry"])
@pytest.mark.parametrize("change", ["before_confirmation", "after_claim", "unchanged"])
def test_cloud_confirmation_freezes_account_through_original_and_retry_dispatch(source, change):
    cloud = SimpleNamespace(logged_in=True, credential_generation=31, close=lambda: None)
    operation = "download" if source == "pending" else "retry"
    with (
        isolated_test_database(),
        patch.object(download_dispatcher, "GuangYaClient", return_value=cloud),
        patch("app.indexers.downloads.GuangYaClient", return_value=cloud),
        patch.object(download_dispatcher, "_recover_guangya_magnet_torrent", return_value=None),
        patch.object(download_dispatcher, "submit_offline", return_value={"ok": True, "task_ids": ["audit-cloud-task"]}) as remote,
    ):
        sid = feed("guangya")
        eid = entry(sid, 1)
        if source == "linked_retry":
            from app.repositories.rss import bind_rss_entry_download
            item = download_dispatcher.DownloadInput("magnet", "Episode", f"magnet:?xt=urn:btih:{1:040x}")
            assert rss_repo.claim_rss_entry(eid)
            bind_rss_entry_download(eid, download_dispatcher.request_key(item), "guangya")
            request = download_dispatcher.create_request(item, "", "", origin=f"rss:{sid}")
            download_requests_repo.update_download_request(request["id"], targets="guangya", status="failed", gy_status="failed")
            # 已绑定请求的重试从原请求读取来源，不要求 RSS 再保留链接。
            with db.get_conn() as conn:
                conn.execute("UPDATE rss_entries SET payload='{}' WHERE id=?", (eid,))
        if operation == "retry":
            rss_repo.record_rss_entry_failure(eid, "guangya_unavailable", True)
        args = {"entry_ids": [eid]}
        _, fingerprint = rss_delivery.prepare(operation, args)
        before = dict(assert_present(rss_repo.get_rss_entry(eid)))
        if change == "before_confirmation":
            cloud.credential_generation += 1
            with pytest.raises(rss_delivery.RSSSubscriptionError) as error:
                rss_delivery.execute(operation, args, fingerprint)
            assert error.value.code == "confirmation_stale"
            assert dict(assert_present(rss_repo.get_rss_entry(eid))) == before
        else:
            claim_name = "claim_pending_rss_entries" if operation == "download" else "claim_retryable_failed_rss_entries"
            original_claim = getattr(rss_repo, claim_name)
            def claim(*args, **kwargs):
                rows = original_claim(*args, **kwargs)
                if change == "after_claim":
                    cloud.credential_generation += 1
                return rows
            with patch.object(rss_repo, claim_name, side_effect=claim):
                result = rss_delivery.execute(operation, args, fingerprint)
            if change == "unchanged":
                assert result["status"] == "completed"
                assert remote.call_args.kwargs["client"] is cloud
            else:
                assert result["status"] == "failed" and result["data"]["failed"] == 1
                assert assert_present(rss_repo.get_rss_entry(eid))["failure_code"] == "guangya_credentials_changed"
        assert remote.call_count == int(change == "unchanged")


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
            and (row := rss_repo.get_rss_entry(eid)) is not None
            and row["status"] == "pending"
        )
        if change == "row":
            seed_rss_entry_state(eid, "submitting")
        elif change == "configuration":
            rss_repo.update_rss_subscription(sid, {"gy_target_dir": "changed"})
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
                rss_repo.record_rss_entry_failure(eid, "qb_outcome_unknown", False)
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
            assert (row := rss_repo.get_rss_entry(eid)) is not None and row[
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
        assert (row := rss_repo.get_rss_entry(eid)) is not None and row[
            "title"
        ] == "全文" * 600

    with isolated_test_database():
        asyncio.run(run())
