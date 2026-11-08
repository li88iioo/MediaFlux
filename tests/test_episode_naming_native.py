"""原生SDK完整剧集事实→共享算法→唯一cloud.change/SQLite；Provider仅内存替身。"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import config, database as db, sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.clients.guangya import GuangYaFile
from app.modules import guangya_episode_naming as naming, guangya_fs_change as fs
from app.modules.guangya_workspace import GuangYaWorkspaceError
from app.repositories import organize_operation_jobs as jobs
from tests.cloud_test_support import run_plan
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import _paused_manager, _run_real_worker
from tests.test_agent_guangya_episode_naming import EpisodeNamingClient
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
from tests.test_agent_sdk_cloud_effects import _context, _executor

OWNER = "webk:v1:" + "a" * 64


@pytest.fixture
def episode_backend(tmp_path, monkeypatch):
    with initialized_web_runtime():
        client = FakeGatewayClient()
        original = EpisodeNamingClient(target_directories=False, episodes_per_group=2)
        client.directories = original.directories
        client.credential_generation = original.credential_generation
        video = client.directories["release-a"][0]
        client.directories["release-a"].append(GuangYaFile(
            "Sub-Case-sensitive", video.name.rsplit(".", 1)[0] + ".zh-Hans.forced.srt", False,
            parent_id="release-a", size=10, etag="subtitle-v1", extension="srt"))
        for name in ("rename", "move", "copy", "delete", "create_dir"):
            monkeypatch.setattr(client, name, Mock(wraps=getattr(client, name)))
        monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
        monkeypatch.setattr(sdk_effects, "GuangYaClient", lambda: client)
        monkeypatch.setattr(fs, "_directory", lambda: tmp_path / "plans")
        execute = fs.execute_fs_change_plan
        monkeypatch.setattr(fs, "execute_fs_change_plan", partial(execute, client_factory=Mock(return_value=client)))
        monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
        with patch("socket.socket.connect", side_effect=AssertionError("禁止真实云盘/模型/TG网络")), _paused_manager():
            yield client


def groups():
    return [{"source_path": "/狐妖小红娘/发布组A", "source_season": 1,
             "source_episode_start": 1, "source_episode_end": 2, "expected_count": 2,
             "target_season": 2, "target_episode_start": 42}]


def preview():
    return sdk.cloud.preview_episodes("show", title="Series", groups=groups())


def rows():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM organize_operation_jobs")]


def test_inventory_returns_full_ids_paths_and_pure_operations_without_context(episode_backend, tmp_path):
    before = deepcopy(episode_backend.directories)
    facts = sdk.cloud.episodes("show")
    assert facts["scan_complete"] and facts["total"] == 7
    assert facts["mapping_status"] == "needs_verification" and facts["video_count"] == 4
    assert facts["matched_subtitle_count"] == 1
    assert {item["file_id"] for item in facts["items"]} == {"release-a", "release-b", "a-1", "a-2", "b-1", "b-2", "Sub-Case-sensitive"}
    assert facts["groups"][0]["source_path"] == "/狐妖小红娘/发布组A"
    assert facts["groups"][0]["file_ids"] == ["a-1", "a-2"]
    compiled = preview()
    changed = [item for item in compiled["operations"] if "file_id" in item]
    assert compiled["selected_files"] == 3 and compiled["created_directories"] == 1
    assert {item["file_id"] for item in changed} == {"a-1", "a-2", "Sub-Case-sensitive"}
    assert [item["new_name"] for item in changed] == ["Series - S02E42.mkv", "Series - S02E43.mkv", "Series - S02E42.zh-Hans.forced.srt"]
    assert all(item["expected"]["path"].startswith("/狐妖小红娘/发布组A/") for item in changed)
    assert not list(tmp_path.glob("plans/*.json")) and not rows()
    assert episode_backend.directories == before


def test_complete_two_hundred_with_missing_target_directories_stays_one_plan(episode_backend):
    original = EpisodeNamingClient(target_directories=False, episodes_per_group=100)
    episode_backend.directories = original.directories
    mappings = [{"source_path": f"/狐妖小红娘/发布组{letter}", "source_season": season,
                 "source_episode_start": 1, "source_episode_end": 100, "expected_count": 100,
                 "target_season": season} for letter, season in (("A", 1), ("B", 2))]
    compiled = sdk.cloud.preview_episodes("show", title="Series", groups=mappings)
    assert compiled["selected_files"] == 200 and len(compiled["operations"]) == 202
    plan = fs.build_fs_change_plan(episode_backend, owner="owner", operations=compiled["operations"], trigger_strm=False)
    assert plan["stats"]["total"] == 202 and plan["stats"]["relocate"] == 200
    assert len({item["source"]["file_id"] for item in plan["operations"] if "source" in item}) == 200
    assert not rows()


@pytest.mark.parametrize("change", ["count", "scope", "unknown", "two_versions", "invalid_boolean"])
def test_mapping_requires_explicit_complete_nonambiguous_selection(episode_backend, change):
    mapping = groups()
    if change == "count":
        mapping[0]["expected_count"] = 3
    elif change == "scope":
        mapping[0]["source_path"] = "/outside"
    elif change == "unknown":
        episode_backend.directories["release-a"][0].name = "no episode evidence.mkv"
    elif change == "two_versions":
        episode_backend.directories["release-a"].append(GuangYaFile("another-version", "[A] Show.S01E001.2160p.mkv", False, parent_id="release-a", size=2000, etag="version-2"))
        mapping[0]["expected_count"] = 3
    else:
        mapping[0]["include_extras"] = "true"
    with pytest.raises(naming.GuangYaEpisodeNamingError):
        sdk.cloud.preview_episodes("show", title="Series", groups=mapping)
    assert not rows() and episode_backend.move.call_count == 0


def test_fresh_preflight_rejects_path_version_and_target_collisions(episode_backend):
    compiled = preview()
    show = next(item for item in episode_backend.directories["0"] if item.file_id == "show")
    show.name = "changed-root"
    with pytest.raises(GuangYaWorkspaceError):
        fs.build_fs_change_plan(episode_backend, owner="owner", operations=compiled["operations"], trigger_strm=False)
    show.name = "狐妖小红娘"
    episode_backend.directories["release-a"][0].etag = "new-version"
    with pytest.raises(fs.GuangYaFSChangeStale):
        fs.build_fs_change_plan(episode_backend, owner="owner", operations=compiled["operations"], trigger_strm=False)
    compiled = preview()
    target = episode_backend.create_dir("Season 02", "show")
    episode_backend.directories[target].append(GuangYaFile("collision", "Series - S02E42.mkv", False, parent_id=target, etag="collision"))
    # 新读已存在的目标目录；唯一公共预检仍拒绝覆盖。
    compiled = preview()
    with pytest.raises(fs.GuangYaFSChangeError):
        fs.build_fs_change_plan(episode_backend, owner="owner", operations=compiled["operations"], trigger_strm=False)
    assert episode_backend.rename.call_count == episode_backend.move.call_count == 0


@pytest.mark.parametrize("boundary", ["cancel", "foreign_owner", "foreign_session", "config_drift"])
def test_native_plan_scope_cancel_and_cold_frozen_configuration(episode_backend, boundary):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner=OWNER)
        plan = (await _executor(store).prepare("cloud.change", {"operations": preview()["operations"], "trigger_strm": False}, context=context)).effect_plan
        assert plan is not None
        restored = _executor(SQLiteKernelStore())
        if boundary == "cancel":
            assert await restored.cancel_effect(plan.plan_id, lease=context.lease)
        elif boundary == "foreign_owner":
            context = await _context(restored.state_store, owner="webk:v1:" + "b" * 64)
        elif boundary == "foreign_session":
            context = await _context(restored.state_store, owner=OWNER, session="other")
        else:
            config.set_and_save({"GY_ORGANIZE_SOURCE_DIRS": "[]"})
        with pytest.raises(ToolPipelineError):
            await restored.execute_confirmed(plan.plan_id, context=context)
        assert not rows() and episode_backend.move.call_count == 0
    asyncio.run(check())


@pytest.mark.parametrize("error", [ValueError, fs.GuangYaFSChangeError])
def test_persisted_job_with_lost_enqueue_receipt_is_unknown_and_never_replayed(episode_backend, monkeypatch, error):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner=OWNER)
        plan = (await _executor(store).prepare("cloud.change", {
            "operations": preview()["operations"], "trigger_strm": False,
        }, context=context)).effect_plan
        assert plan is not None
        submit = fs.submit_fs_change_plan

        def lose_receipt(*args, **kwargs):
            submit(*args, **kwargs)
            raise error("持久任务已受理后丢失回执")

        monkeypatch.setattr(fs, "submit_fs_change_plan", lose_receipt)
        cold = _executor(SQLiteKernelStore())
        with pytest.raises(ToolPipelineError) as outcome:
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert outcome.value.code == "outcome_unknown" and outcome.value.execution_started is True
        job, = rows()
        assert job["status"] == "pending" and episode_backend.move.call_count == 0
        with pytest.raises(ToolPipelineError):
            await _executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert len(rows()) == 1
        claim = jobs.claim_organize_operation_job(job["job_id"])
        assert claim is not None
        _run_real_worker(claim)
        assert rows()[0]["status"] == "completed" and episode_backend.move.call_count == 3
    asyncio.run(check())


def test_existing_signed_job_with_historical_observation_fields_still_executes_once(episode_backend):
    plan = fs.build_fs_change_plan(episode_backend, owner="owner", operations=preview()["operations"], trigger_strm=False)
    plan["observation_ref"] = "OBS" + "A" * 32
    plan["observation_source"] = {"retired": True}
    for item in plan["operations"]:
        if "source" in item:
            item["object_ref"] = "OBJ" + item["source"]["file_id"].encode().hex().upper().ljust(24, "A")[:24]
    plan["fingerprint"] = fs._fingerprint(plan)
    fs._atomic_write(plan)
    result = run_plan(episode_backend, plan)
    assert not result["partial"] and result["stats"]["relocated"] == 3
    with pytest.raises(fs.GuangYaFSChangeError):
        run_plan(episode_backend, plan)
    assert episode_backend.move.call_count == 3


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实Linux root Python/CLI验收")
@pytest.mark.parametrize("language", ["python", "shell"])
def test_root_sdk_facts_to_sqlite_cold_confirmation_and_original_job_once(episode_backend, language):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner=OWNER)
        effects = _executor(store)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=store, reference_store=store, effect_store=effects.effect_store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        mapping = json.dumps(groups(), ensure_ascii=False)
        python = ('from mf import cloud\nfrom pathlib import Path\n'
                  'facts=cloud.episodes("show")\n'
                  'assert facts["total"]==7 and any(x["file_id"]=="Sub-Case-sensitive" for x in facts["items"])\n'
                  f'p=cloud.preview_episodes("show",title="Series",groups={mapping})\n'
                  'assert p["selected_files"]==3 and all("file_id" in op for op in p["operations"] if op["op"]!="create_directory")\n'
                  'Path("before").write_text("once")\ncloud.change(p["operations"],trigger_strm=False)\n'
                  'Path("after").write_text("not permitted")')
        shell = ('set -eu\nmf cloud.episodes --file-id show > facts.json\n'
                 'python -c \'import json; x=json.load(open("facts.json")); assert x["total"]==7; '
                 f'json.dump({mapping},open("groups.json","w"))\'\n'
                 'mf cloud.preview_episodes --file-id show --title Series --groups @groups.json > preview.json\n'
                 'python -c \'import json; p=json.load(open("preview.json")); assert p["selected_files"]==3; '
                 'json.dump(p["operations"],open("operations.json","w"))\'\n'
                 'echo once > before\nmf cloud.change --operations @operations.json --no-trigger-strm\n'
                 'echo invalid > after')
        try:
            proposed = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": python if language == "python" else shell}, context=context)
            assert proposed.effect_plan is not None, proposed.outcome.public_content
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            restored = _executor(SQLiteKernelStore())
            accepted = await restored.execute_confirmed(proposed.effect_plan.plan_id, context=context)
            assert accepted.outcome.public_content["status"] == "accepted" and len(rows()) == 1
            with pytest.raises(ToolPipelineError):
                await restored.execute_confirmed(proposed.effect_plan.plan_id, context=context)
            claim = jobs.claim_organize_operation_job(rows()[0]["job_id"])
            assert claim is not None
            _run_real_worker(claim)
            with sdk.call_owner(OWNER):
                receipt = sdk.cloud.operation(accepted.outcome.public_content["data"]["operation_ref"])
            assert receipt["status"] == "completed"
            assert episode_backend.file_info("a-1").name == "Series - S02E42.mkv"
            assert episode_backend.file_info("Sub-Case-sensitive").name == "Series - S02E42.zh-Hans.forced.srt"
            assert episode_backend.move.call_count == 3 and not (root / "work/after").exists()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
