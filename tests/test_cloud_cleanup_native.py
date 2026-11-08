"""真实SDK/Python/CLI、SQLite确认与公共执行器；云盘始终为内存替身。"""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import config, database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_fs_change as fs, guangya_residual_cleanup as cleanup
from app.modules import guangya_workspace as workspace
from app.repositories import organize_operation_jobs as jobs
from tests.cloud_test_support import run_plan
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import _paused_manager, _run_real_worker
from tests.test_agent_sdk_effects import context_for as sdk_context_for
from tests.test_guangya_residual_cleanup import FakeCleanupClient


class CloudClient(FakeCleanupClient):
    supports_guarded_empty_directory_delete = True

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self.reject_ids: set[str] = set()
        self.directories["video_dir"].append(
            GuangYaFile(
                "bitrate",
                "剧集.S01E01.2.4Mbps.h265.mkv",
                False,
                parent_id="video_dir",
                size=101,
                etag="b",
                extension="mkv",
                updated_at=17,
            )
        )

    def rename(self, file_id, new_name):
        self.calls.append(("rename", file_id, new_name))
        if file_id in self.reject_ids:
            raise GuangYaWriteRejected("rename", code="166", message="rejected")
        for rows in self.directories.values():
            for item in rows:
                if item.file_id == file_id:
                    item.name = new_name
                    return True
        raise ValueError("missing")

    def move(self, file_ids, parent_id):
        self.calls.append(("move", tuple(file_ids), parent_id))
        return super().move(file_ids, parent_id)

    def create_dir(self, name, parent_id="0"):
        self.calls.append(("create", name, parent_id))
        return super().create_dir(name, parent_id)

    def delete_empty_directory(
        self, file_id, *, expected_etag="", expected_updated_at=0
    ):
        self.calls.append(("recycle_empty", file_id))
        return super().delete_empty_directory(
            file_id,
            expected_etag=expected_etag,
            expected_updated_at=expected_updated_at,
        )


@pytest.fixture
def cloud_env(tmp_path, monkeypatch):
    with initialized_web_runtime():
        client = CloudClient()
        monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
        monkeypatch.setattr(sdk_effects, "GuangYaClient", lambda: client)
        monkeypatch.setattr(fs, "_directory", lambda: tmp_path / "plans")
        monkeypatch.setattr(
            fs,
            "execute_fs_change_plan",
            lambda payload, **kw: execute_plan(payload, client, **kw),
        )
        monkeypatch.setattr(
            cleanup,
            "configured_cleanup_sources",
            lambda: [{"id": "source", "name": "整理源"}],
        )
        monkeypatch.setattr(
            sandbox,
            "get_runtime_paths",
            lambda: SimpleNamespace(
                data_dir=tmp_path, runtime_dir=tmp_path / "runtime"
            ),
        )
        with (
            patch(
                "socket.socket.connect", side_effect=AssertionError("离线清理禁止网络")
            ),
            _paused_manager(),
        ):
            yield client


execute_original = fs.execute_fs_change_plan


def execute_plan(payload, client, **kwargs):
    kwargs["client_factory"] = lambda: client
    return execute_original(payload, **kwargs)


async def context_for(store, owner="webk:v1:" + "a" * 64, session="cloud-cleanup"):
    return await sdk_context_for(store, owner=owner, session=session)


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(
        catalog=ToolCatalog(
            [sdk_effects.cloud_change_tool()]
        ),
        state_store=store,
        reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
    )


def stored_jobs():
    with db.get_conn() as conn:
        return [
            dict(row) for row in conn.execute("SELECT * FROM organize_operation_jobs")
        ]


def preview_operations():
    return sdk.cloud.preview_cleanup(quarantine_ids=["residual"])["operations"]


@pytest.mark.parametrize("method,arguments", [
    ("list", {"path": "/a"}), ("list", {"limit": 0}), ("info", {}),
])
def test_sdk_reads_reject_invalid_arguments_without_cloud_mutation(cloud_env, method, arguments):
    with pytest.raises((TypeError, workspace.GuangYaWorkspaceError)):
        getattr(sdk.cloud, method)(**arguments)
    assert not cloud_env.calls


def test_reads_keep_complete_ids_properties_filters_and_real_pagination(cloud_env):
    client = cloud_env
    for index in range(137):
        client.directories["video_dir"].append(
            GuangYaFile(
                f"extra-{index}",
                f"part-{index}.mkv",
                False,
                parent_id="video_dir",
                size=index,
                etag=f"etag-{index}",
                mime_type="video/x-matroska",
                created_at=index,
                updated_at=index + 1,
            )
        )
    first = sdk.cloud.list("video_dir", query="part", kinds=["video"], limit=101)
    second = sdk.cloud.list(
        "video_dir", query="part", kinds=["video"], limit=101, offset=101
    )
    assert first["total"] == second["total"] == 137
    assert first["has_more"] and not second["has_more"]
    assert len(first["items"]) == 101 and len(second["items"]) == 36
    rows = first["items"] + second["items"]
    assert len({row["file_id"] for row in rows}) == 137
    assert rows[-1]["mime_type"] == "video/x-matroska" and rows[-1]["updated_at"] == 137
    assert rows[-1]["path"].endswith("/part-136.mkv")
    assert sdk.cloud.info("extra-136")["item"]["size"] == 136
    assert sdk.cloud.info("missing")["item"] is None
    assert not client.calls


@pytest.mark.parametrize("preview", [False, True])
def test_episode_scan_and_preview_stop_before_visiting_child_directories(cloud_env, monkeypatch, preview):
    stopped = threading.Event()
    read = cloud_env.list_dir
    visited = []
    def listing(parent_id):
        visited.append(parent_id)
        stopped.set()
        return read(parent_id)
    monkeypatch.setattr(cloud_env, "list_dir", listing)
    with sdk.call_owner("reader", cancelled=stopped.is_set), pytest.raises(InterruptedError, match="停止"):
        if preview:
            sdk.cloud.preview_episodes("source", title="Example", groups=[{
                "source_path": "/整理源/媒体目录", "source_season": 1,
                "source_episode_start": 1, "source_episode_end": 1,
                "target_season": 1, "target_episode_start": 1, "expected_count": 1,
            }])
        else:
            sdk.cloud.episodes("source")
    assert visited == ["source"]
    assert cloud_env.closed and not cloud_env.calls


@pytest.mark.parametrize(
    "boundary",
    ["read_failure", "budget", "depth", "duplicate", "parent", "same_name_replacement"],
)
def test_reads_never_claim_incomplete_or_replaced_scope_is_full(
    cloud_env, boundary, monkeypatch
):
    client = cloud_env
    arguments: dict = {"parent_id": "source", "recursive": True}
    if boundary == "read_failure":
        monkeypatch.setattr(
            client,
            "list_dir",
            lambda _: (_ for _ in ()).throw(RuntimeError("分页失败")),
        )
    elif boundary == "budget":
        arguments["max_items"] = 2
    elif boundary == "depth":
        arguments["max_depth"] = 0
    elif boundary == "duplicate":
        client.directories["source"].append(deepcopy(client.directories["source"][0]))
    elif boundary == "parent":
        client.directories["source"][0].parent_id = "other"
    else:
        client.directories["0"][0].file_id = "replacement"
    with pytest.raises((workspace.GuangYaWorkspaceError, RuntimeError)):
        sdk.cloud.list(**arguments)
    assert not client.calls


def test_names_use_real_ids_complete_facts_and_conflict_exclusion(cloud_env):
    client = cloud_env
    preview = sdk.cloud.preview_names(["video_dir"], mode="remove_bitrate")
    assert preview["stats"]["rename_count"] == 1
    entry = preview["entries"][0]
    assert entry["file_id"] == "bitrate" and entry["old_name"].endswith(
        "2.4Mbps.h265.mkv"
    )
    assert entry["new_name"] == "剧集.S01E01.h265.mkv"
    assert preview["operations"][0]["expected"]["updated_at"] == 17
    client.directories["video_dir"].append(
        GuangYaFile("collision", entry["new_name"], False, parent_id="video_dir")
    )
    conflict = sdk.cloud.preview_names(["video_dir"])
    assert (
        conflict["operations"] == []
        and conflict["conflicts"][0]["file_id"] == "bitrate"
    )
    assert not client.calls


def test_cleanup_keeps_unselected_media_and_all_review_facts(cloud_env):
    client = cloud_env
    before = deepcopy(client.directories)
    preview = sdk.cloud.preview_cleanup(limit=1)
    assert preview["total"] == 1 and preview["returned"] == 1
    assert preview["candidates"][0]["file_names"] == ["xxx.png"]
    assert {item["file_id"] for item in preview["candidates"][0]["tree"]} == {
        "residual",
        "junk",
    }
    assert preview["operations"] == [
        {
            "op": "trash",
            "file_id": "empty",
            "expected": preview["empty_directories"][0]["root"],
            "require_empty": True,
        }
    ]
    assert (
        sdk.cloud.preview_cleanup(include_empty=False, quarantine_ids=[])["operations"]
        == []
    )
    with pytest.raises(cleanup.GuangYaCleanupError):
        sdk.cloud.preview_cleanup(quarantine_ids=["video_dir"])
    assert client.directories == before and not client.calls


def test_cleanup_pagination_no_old_500_candidate_cap_and_no_hidden_names(cloud_env):
    client = cloud_env
    for index in range(505):
        key = f"candidate-{index}"
        client.directories["source"].append(
            GuangYaFile(key, key, True, parent_id="source", etag=key)
        )
        client.directories[key] = [
            GuangYaFile(key + "-junk", f"广告{index}.url", False, parent_id=key, size=1)
        ]
    preview = sdk.cloud.preview_cleanup(limit=500, include_empty=False)
    last = sdk.cloud.preview_cleanup(limit=500, offset=500, include_empty=False)
    assert preview["total"] == last["total"] == 506
    assert preview["has_more"] and not last["has_more"] and len(last["candidates"]) == 6
    selected = sdk.cloud.preview_cleanup(
        quarantine_ids=["candidate-504"], include_empty=False
    )
    assert selected["selected_candidates"][0]["file_names"] == ["广告504.url"]
    assert not client.calls


def test_cleanup_protects_nested_configured_roots_and_their_ancestors(
    cloud_env, monkeypatch
):
    client = cloud_env
    client.directories["source"].append(
        GuangYaFile("ancestor", "ancestor", True, parent_id="source", etag="a")
    )
    client.directories["ancestor"] = [
        GuangYaFile("protected", "下载目录", True, parent_id="ancestor", etag="p")
    ]
    client.directories["protected"] = [
        GuangYaFile("ad", "广告.url", False, parent_id="protected")
    ]
    monkeypatch.setattr(
        cleanup,
        "configured_cleanup_sources",
        lambda: [{"id": "protected", "name": "下载目录"}],
    )
    preview = sdk.cloud.preview_cleanup(source_ids=["source"])
    assert not {"source", "ancestor", "protected"} & {
        op.get("file_id") for op in preview["operations"]
    }
    assert not {"ancestor", "protected"} & {
        row["file_id"] for row in preview["candidates"]
    }
    with pytest.raises((cleanup.GuangYaCleanupError, workspace.GuangYaWorkspaceError)):
        sdk.cloud.preview_cleanup(source_ids=["0"])


def test_preview_constraints_rechecked_before_freeze_and_before_each_write(cloud_env):
    client = cloud_env
    preview = sdk.cloud.preview_cleanup(quarantine_ids=["residual"])
    client.directories["residual"].append(
        GuangYaFile("late", "new.mp4", False, parent_id="residual", size=2)
    )
    with pytest.raises(fs.GuangYaFSChangeStale):
        fs.build_fs_change_plan(
            client, owner="owner", operations=preview["operations"], trigger_strm=False
        )
    client.directories["residual"].pop()
    plan = fs.build_fs_change_plan(
        client, owner="owner", operations=preview["operations"], trigger_strm=False
    )
    client.directories["residual"].append(
        GuangYaFile("late", "new.mp4", False, parent_id="residual", size=2)
    )
    with pytest.raises(fs.GuangYaFSChangeStale):
        run_plan(client, plan)
    assert client.file_info("residual").parent_id == "source"
    assert not any(call[0] == "move" for call in client.calls)


def test_atomic_empty_guard_preserves_late_child_and_unknown_delete_never_replays(
    cloud_env, monkeypatch
):
    client = cloud_env
    ops = sdk.cloud.preview_cleanup()["operations"]
    plan = fs.build_fs_change_plan(
        client, owner="owner", operations=ops, trigger_strm=False
    )
    original = client.delete_empty_directory

    def raced(file_id, **guards):
        client.directories[file_id].append(
            GuangYaFile("arrived", "movie.mkv", False, parent_id=file_id)
        )
        return original(file_id, **guards)

    monkeypatch.setattr(client, "delete_empty_directory", raced)
    result = run_plan(client, plan)
    assert (
        result["partial"]
        and client.file_info("empty") is not None
        and client.file_info("arrived") is not None
    )
    with pytest.raises(fs.GuangYaFSChangeError):
        run_plan(client, plan)
    assert len([call for call in client.calls if call[0] == "recycle_empty"]) == 1


def test_tree_guard_rechecks_after_quarantine_directory_creation(cloud_env, monkeypatch):
    client = cloud_env
    preview = sdk.cloud.preview_cleanup(quarantine_ids=["residual"], include_empty=False)
    plan = fs.build_fs_change_plan(client, owner="owner", operations=preview["operations"], trigger_strm=False)
    original = client.create_dir

    def arrived(name, parent_id="0"):
        created = original(name, parent_id)
        if not client.file_info("late-video"):
            client.directories["residual"].append(GuangYaFile("late-video", "movie.mp4", False, parent_id="residual", size=20))
        return created

    monkeypatch.setattr(client, "create_dir", arrived)
    result = run_plan(client, plan)
    assert result["partial"] and result["stats"]["created"] > 0
    assert client.file_info("residual").parent_id == "source" and client.file_info("late-video") is not None
    assert not any(call[0] == "move" for call in client.calls)


@pytest.mark.parametrize(
    "boundary",
    ["cancel", "foreign_owner", "foreign_session", "config_drift", "credential_drift"],
)
def test_real_sqlite_frozen_confirmation_scope_and_cancel(cloud_env, boundary):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with sdk.call_owner(context.owner):
            operations = preview_operations()
        prepared = await effects.prepare(
            "cloud.change",
            {"operations": operations, "trigger_strm": False},
            context=context,
        )
        assert prepared.effect_plan is not None
        plan = prepared.effect_plan
        cold = executor()
        pending = await cold.get_pending_plan(
            owner=context.owner, session_id=context.session_id
        )
        assert pending is not None and pending.plan_id == plan.plan_id
        if boundary == "cancel":
            assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
        elif boundary == "foreign_owner":
            context = await context_for(cold.state_store, owner="foreign")
        elif boundary == "foreign_session":
            context = await context_for(cold.state_store, session="foreign")
        elif boundary == "config_drift":
            config.set_and_save({"GY_ORGANIZE_SOURCE_DIRS": "[]"})
        else:
            cloud_env.credential_generation += 1
        if boundary == "credential_drift":
            # 入队不写云盘；worker以冻结凭据核验，不用新账号执行。
            accepted = await cold.execute_confirmed(plan.plan_id, context=context)
            assert accepted.outcome.public_content["status"] == "accepted"
            claimed = jobs.claim_organize_operation_job(stored_jobs()[0]["job_id"])
            assert claimed is not None
            _run_real_worker(claimed)
            assert not cloud_env.calls
        else:
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=context)
            assert not stored_jobs()
        assert not cloud_env.calls

    asyncio.run(check())


def test_real_sqlite_cold_confirm_single_job_and_recycle_only(cloud_env):
    async def check():
        context = await context_for(SQLiteKernelStore())
        with sdk.call_owner(context.owner):
            operations = preview_operations()
        prepared = await executor().prepare(
            "cloud.change",
            {"operations": operations, "trigger_strm": False},
            context=context,
        )
        assert prepared.effect_plan is not None
        receipt = await executor().execute_confirmed(
            prepared.effect_plan.plan_id, context=context
        )
        assert receipt.outcome.public_content["status"] == "accepted"
        rows = stored_jobs()
        assert len(rows) == 1 and rows[0]["job_kind"] == "agent_guangya_fs_change"
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(
                prepared.effect_plan.plan_id, context=context
            )
        claimed = jobs.claim_organize_operation_job(rows[0]["job_id"])
        assert claimed is not None
        _run_real_worker(claimed)
        assert cloud_env.file_info("empty") is None
        assert cloud_env.file_info("residual").parent_id != "source"
        assert (
            cloud_env.file_info("poster") is not None
            and cloud_env.file_info("video") is not None
        )
        assert (
            len([call for call in cloud_env.calls if call[0] == "recycle_empty"]) == 1
        )
        assert len([call for call in cloud_env.calls if call[0] == "move"]) == 1
        with sdk.call_owner(context.owner):
            status = sdk.cloud.operation(
                receipt.outcome.public_content["data"]["operation_ref"]
            )
        assert status["status"] == "completed"
        with sdk.call_owner("foreign"):
            assert not sdk.cloud.operation(
                receipt.outcome.public_content["data"]["operation_ref"]
            )["ok"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure", [ValueError("post commit"), fs.GuangYaFSChangeError("post commit")]
)
def test_after_admission_business_error_is_unknown_started_not_retryable(
    cloud_env, monkeypatch, failure
):
    async def check():
        context = await context_for(SQLiteKernelStore())
        prepared = await executor().prepare(
            "cloud.change",
            {
                "operations": [
                    {"op": "rename", "file_id": "bitrate", "new_name": "clean.mkv"}
                ],
                "trigger_strm": False,
            },
            context=context,
        )
        assert prepared.effect_plan is not None
        submit = fs.submit_fs_change_plan

        def committed(*args, **kwargs):
            submit(*args, **kwargs)
            raise failure

        monkeypatch.setattr(fs, "submit_fs_change_plan", committed)
        with pytest.raises(ToolPipelineError) as raised:
            await executor().execute_confirmed(
                prepared.effect_plan.plan_id, context=context
            )
        assert raised.value.code == "outcome_unknown" and raised.value.execution_started
        assert len(stored_jobs()) == 1
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(
                prepared.effect_plan.plan_id, context=context
            )
        assert len(stored_jobs()) == 1 and not cloud_env.calls

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["agent_guangya_cleanup", "agent_guangya_rename"])
@pytest.mark.parametrize("restart_state", ["pending", "running"])
def test_legacy_queued_operations_require_review_without_replaying_old_engine(cloud_env, kind, restart_state):
    row, replayed = jobs.enqueue_organize_operation_job(
        job_kind=kind, owner="owner", operation="历史任务", reference="", payload={"version": 1},
        dedupe_key=kind,
    )
    assert not replayed
    if restart_state == "running":
        assert jobs.claim_organize_operation_job(row["job_id"]) is not None
    # 重启恢复读真实SQLite；无旧计划加载或旧执行器可供重放。
    recovered = jobs.recover_orphaned_organize_operation_jobs()
    assert recovered == (1 if restart_state == "running" else 0)
    claimed = jobs.claim_organize_operation_job(row["job_id"])
    if restart_state == "pending":
        assert claimed is not None
        _run_real_worker(claimed)
    else:
        assert claimed is None
    terminal = jobs.get_organize_operation_job(row["job_id"])
    assert terminal is not None and terminal["status"] == "manual_review"
    assert not cloud_env.calls


@pytest.mark.skipif(
    not sandbox.sandbox_supported(), reason="真实Linux root Python/CLI容器验收"
)
@pytest.mark.parametrize("language", ["python", "shell"])
def test_native_python_cli_complete_read_to_sqlite_cold_confirmation_without_script_replay(
    cloud_env, language
):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        pipeline = ToolPipeline(
            catalog=effects.catalog,
            state_store=effects.state_store,
            reference_store=effects.reference_store,
            effect_store=effects.effect_store,
        )
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        python = (
            "from pathlib import Path\nfrom mf import cloud\n"
            'read=cloud.list("source", recursive=True)\n'
            'assert read["scan_complete"] and read["total"] >= 8\n'
            'assert any(row["file_id"]=="junk" and row["name"]=="xxx.png" for row in read["items"])\n'
            'preview=cloud.preview_names(["video_dir"])\n'
            'assert preview["entries"][0]["old_name"].endswith("2.4Mbps.h265.mkv")\n'
            'Path("before").write_text("once")\n'
            'cloud.change(preview["operations"], trigger_strm=False)\n'
            'Path("after").write_text("must not execute")\n'
        )
        shell = (
            "set -eu\nmf cloud.list --parent-id source --recursive > facts.json\n"
            "mf cloud.preview_names --file-ids video_dir > preview.json\n"
            'python -c \'import json; p=json.load(open("preview.json")); '
            'assert p["entries"][0]["file_id"]=="bitrate"; '
            'json.dump(p["operations"],open("operations.json","w"))\'\n'
            "echo once > before\n"
            "mf cloud.change --operations @operations.json --no-trigger-strm\n"
            "echo invalid > after\n"
        )
        try:
            result = await pipeline.execute(
                sandbox.RUN_CODE_TOOL,
                {
                    "code": python if language == "python" else shell,
                    "language": language,
                },
                context=context,
            )
            assert result.effect_plan is not None, result.outcome.public_content
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (
                root / "work/after"
            ).exists()
            assert not cloud_env.calls
            await executor().execute_confirmed(
                result.effect_plan.plan_id, context=context
            )
            claimed = jobs.claim_organize_operation_job(stored_jobs()[0]["job_id"])
            assert claimed is not None
            _run_real_worker(claimed)
            assert cloud_env.file_info("bitrate").name == "剧集.S01E01.h265.mkv"
            assert len([call for call in cloud_env.calls if call[0] == "rename"]) == 1
            assert not (root / "work/after").exists()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)

    asyncio.run(check())
