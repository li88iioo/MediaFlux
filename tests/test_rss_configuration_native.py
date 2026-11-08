"""RSS原生配置：真实分页、CAS、冷确认、取消和未知写回执。"""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import database as db
from app import sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.modules import rss_subscriptions as service
from app.modules.rss import _release_rss_refresh, _try_acquire_rss_refresh
from tests.support import isolated_test_database
from tests.test_agent_sdk_effects import context_for, executor


def test_full_configuration_and_real_pages_keep_fields_and_redact_url_credentials():
    with isolated_test_database():
        identities = [
            db.add_rss_subscription(
                "长" * 150 + str(i),
                f"https://user:pwd@example.invalid/{i}?passkey=PRIVATE",
                exclude_keywords="界" * 1000,
                qb_save_path="/library/" + "a" * 900,
                media_tmdb_id="123",
                media_default_season=0,
                skip_existing_episodes=1,
            )
            for i in range(205)
        ]
        found = []
        options = {"limit": 73}
        while True:
            page = sdk.rss.subscriptions(**options)["data"]
            found.extend(item["id"] for item in page["items"])
            for item in page["items"]:
                assert (
                    item["exclude_keywords"] == "界" * 1000
                    and item["media_default_season"] == 0
                )
                assert len(item["qb_save_path"]) == 909
                assert "pwd" not in item["urls"] and "PRIVATE" not in item["urls"]
                assert len(item["name"]) > 150
            if not page["has_more"]:
                assert page["next"] is None
                break
            options = page["next"]
        assert found == identities
        assert sdk.rss.subscriptions(after_id=identities[-1])["data"]["total"] == 205
        assert (
            sdk.rss.subscription(identities[0])["data"]["exclude_keywords"]
            == "界" * 1000
        )


@pytest.mark.parametrize(
    "change",
    [
        "statistics",
        "configuration",
        "duplicate",
        "cancel",
        "owner",
        "session",
        "unknown",
    ],
)
def test_cold_confirmation_uses_real_identity_and_never_replays_unknown(change):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        sid = db.add_rss_subscription(
            "Existing", "https://example.invalid/old", refresh_interval_minutes=30
        )
        name = "rss.create" if change in {"duplicate", "unknown"} else "rss.update"
        arguments = (
            {"name": "New", "urls": ["https://example.invalid/new"]}
            if name == "rss.create"
            else {"subscription_id": sid, "enabled": False}
        )
        effects = executor(store)
        prepared = await effects.prepare(name, arguments, context=context)
        plan = prepared.effect_plan
        assert plan is not None
        row = db.get_rss_subscription(sid)
        assert row is not None and row["enabled"] == 1
        if change == "statistics":
            db.add_rss_entry_with_media(sid, "new entry", "new-guid")
            db.update_rss_subscription(
                sid, {"last_refreshed_at": "2026-11-05 12:00:00"}
            )
        elif change == "configuration":
            db.update_rss_subscription(sid, {"urls": "https://example.invalid/changed"})
        elif change == "duplicate":
            db.add_rss_subscription("New", "https://example.invalid/new")
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change in {"owner", "session"}:
            context = await context_for(
                store,
                owner="other" if change == "owner" else context.owner,
                session="other" if change == "session" else context.session_id,
            )
        rebuilt = executor(SQLiteKernelStore())
        if change in {"cancel", "owner", "session"}:
            with pytest.raises(ToolPipelineError):
                await rebuilt.execute_confirmed(plan.plan_id, context=context)
            row = db.get_rss_subscription(sid)
            assert row is not None and row["enabled"] == 1
            return
        if change == "unknown":
            with patch.object(service.db, "get_rss_subscription", return_value=None):
                result = await rebuilt.execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "outcome_unknown"
            assert len(db.list_rss_subscriptions()) == 2
        else:
            result = await rebuilt.execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == (
                "completed" if change == "statistics" else "conflict"
            )
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)

    with (
        isolated_test_database(),
        patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()),
    ):
        asyncio.run(run())


def test_delete_refuses_running_refresh_or_submission_without_losing_rows():
    with isolated_test_database():
        sid = db.add_rss_subscription("Feed", "https://example.invalid/rss")
        args = {"subscription_id": sid}
        snapshot = service.prepare_change("delete", args)["snapshot"]
        assert _try_acquire_rss_refresh(sid)
        try:
            with pytest.raises(service.RSSSubscriptionError, match="正在刷新"):
                service.apply_change("delete", args, expected_snapshot=snapshot)
        finally:
            _release_rss_refresh(sid)
        eid = db.add_rss_entry_with_media(sid, "entry", "guid")["id"]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET status='submitting' WHERE id=?", (eid,)
            )
        snapshot = service.prepare_change("delete", args)["snapshot"]
        with pytest.raises(service.RSSSubscriptionError, match="提交"):
            service.apply_change("delete", args, expected_snapshot=snapshot)
        assert (
            db.get_rss_subscription(sid) is not None
            and db.get_rss_entry(eid) is not None
        )


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="Linux root真实工作区RPC")
def test_rss_rpc_cold_confirmation_and_full_result_file(tmp_path):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        root = tmp_path / "rpc"
        root.mkdir()
        (root / "run").mkdir()
        bridge = sandbox._SDKRequests(context, root, executor(store))
        bridge.uid = os.getuid()
        request = root / "run/mf-requests/create.json"
        request.write_text(
            json.dumps(
                {
                    "method": "rss.create",
                    "arguments": {
                        "name": "RPC",
                        "urls": ["https://example.invalid/rss?passkey=PRIVATE"],
                        "exclude_keywords": "完整" * 300,
                        "media_tmdb_id": "123",
                        "skip_existing_episodes": True,
                    },
                }
            )
        )
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal and bridge.proposal.effect_plan
            plan = bridge.proposal.effect_plan
            assert db.list_rss_subscriptions() == []
        finally:
            await bridge.close()
        result = await executor(SQLiteKernelStore()).execute_confirmed(
            plan.plan_id, context=context
        )
        sid = result.outcome.public_content["data"]["subscription_id"]
        with sdk.call_owner(context.owner):
            assert sdk.rss.subscription(sid)["data"]["exclude_keywords"] == "完整" * 300
        row = db.get_rss_subscription(sid)
        assert row is not None and row["urls"].endswith("PRIVATE")
        catalog = ToolCatalog()
        pipeline = ToolPipeline(
            catalog=catalog, state_store=store, reference_store=store
        )
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": f'from mf import rss\nv = rss.subscription({sid})["data"]\nprint(v["id"], len(v["exclude_keywords"]))'
            },
            context=context,
        )
        data = json.loads(result.outcome.model_content)["data"]
        assert data["exit_code"] == 0 and data["stdout"].strip() == str(sid) + " 600"
        workspace, _ = sandbox._workspace_location(context.owner, context.session_id)
        response = json.loads(
            (workspace / data["sdk_calls"][0]["response_path"].lstrip("/")).read_text()
        )
        assert response["result"]["data"]["exclude_keywords"] == "完整" * 300
        assert "PRIVATE" not in response["result"]["data"]["urls"]

    with (
        isolated_test_database(),
        patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()),
        patch.object(
            sandbox,
            "get_runtime_paths",
            return_value=SimpleNamespace(
                data_dir=tmp_path, runtime_dir=tmp_path / "runtime"
            ),
        ),
    ):
        asyncio.run(run())


@pytest.mark.parametrize(
    "other_name,other_url,conflict",
    [
        ("Target", "https://example.invalid/other", False),
        ("Other", "https://example.invalid/rss", False),
        ("Target", "https://example.invalid/rss", True),
    ],
)
def test_create_snapshot_uses_both_name_and_urls(other_name, other_url, conflict):
    with (
        isolated_test_database(),
        patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()),
    ):
        arguments = {"name": "Target", "urls": ["https://example.invalid/rss"]}
        snapshot = service.prepare_change("create", arguments)["snapshot"]
        db.add_rss_subscription(other_name, other_url)
        result = service.apply_change("create", arguments, expected_snapshot=snapshot)
        assert result["status"] == ("conflict" if conflict else "completed")
        assert len(db.list_rss_subscriptions()) == (1 if conflict else 2)


def test_public_config_and_binding_write_roll_back_together():
    with isolated_test_database():
        sid = db.add_rss_subscription("Original", "https://example.invalid/rss")
        arguments = {"subscription_id": sid, "name": "Changed", "media_tmdb_id": "123"}
        snapshot = service.prepare_change("update", arguments)["snapshot"]
        row = db.get_rss_subscription(sid)
        assert row is not None
        before = dict(row)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER deny_binding BEFORE INSERT ON rss_media_bindings BEGIN SELECT RAISE(ABORT, 'denied'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            service.apply_change("update", arguments, expected_snapshot=snapshot)
        row = db.get_rss_subscription(sid)
        assert row is not None and dict(row) == before
