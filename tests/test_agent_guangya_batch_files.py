"""原batch_files能力以真实目录ID进入唯一cloud.change，不保留会话句柄。"""
from __future__ import annotations
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app.agent.errors import ToolPipelineError
from app.agent.kernel.persistence import SQLiteKernelStore
from app.modules import guangya_fs_change as fs
from tests.support import initialized_web_runtime
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
from tests.test_agent_sdk_cloud_effects import _context, _executor
from tests.test_agent_effect_admission import _paused_manager

_THRESHOLD = 200 * 1024 * 1024


def _arguments(**values):
    return {"operations": [fs._batch_files_arguments({"op": "batch_files", "file_id": "source", **values})], "trigger_strm": False}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    with initialized_web_runtime(), patch("socket.socket.connect", side_effect=AssertionError("禁止外联")), _paused_manager():
        client = FakeGatewayClient()
        monkeypatch.setattr(fs, "_directory", lambda: tmp_path / "plans")
        monkeypatch.setattr("app.agent.kernel.ports.sdk_effects.GuangYaClient", lambda: client)
        yield client, SimpleNamespace(owner="owner")


@pytest.mark.parametrize("values", [
    {}, {"file_id": "0", "cleanup_empty_dirs": True}, {"file_id": 1, "cleanup_empty_dirs": True},
    {"trash_filter": {}}, {"trash_filter": None}, {"trash_filter": {"size": 1}},
    {"trash_filter": {"size_lt_bytes": True}}, {"trash_filter": {"size_lt_bytes": -1}},
    {"trash_filter": {"size_lt_bytes": "200M"}}, {"trash_filter": {"size_lt_bytes": 2**53}},
    {"trash_filter": {"size_lt_bytes": 10, "size_gte_bytes": 10}},
    {"trash_filter": {"kinds": ["directory"]}}, {"trash_filter": {"kinds": []}},
    {"trash_filter": {"extensions": [".mp4"]}}, {"trash_filter": {"name_contains": [""]}},
    {"move_filter": {}}, {"target_path": "../relative"}, {"target_path": 123},
    {"cleanup_empty_dirs": "true"}, {"cleanup_empty_dirs": True, "conflict": "overwrite"},
    {"cleanup_empty_dirs": True, "purge": True},
])
def test_ambiguous_or_unbounded_rules_rejected(values):
    with pytest.raises(fs.GuangYaFSChangeError):
        _arguments(**values)


def test_rules_keep_and_filters_without_implicit_trash():
    batch = _arguments(target_path="/source", cleanup_empty_dirs=True)["operations"][0]
    assert "trash_filter" not in batch and batch["move_filter"] == {} and batch["conflict"] == "keep_both"
    batch = _arguments(trash_filter={"kinds": [" VIDEO ", "video"], "extensions": ["MP4"], "name_contains": ["广告", "AD"]})["operations"][0]
    assert batch["trash_filter"] == {"kinds": ["video"], "extensions": ["mp4"], "name_contains": ["广告", "ad"]}


def test_native_cold_confirmation_freezes_all_225_true_ids_once(environment):
    from app.clients.guangya import GuangYaFile
    from app.repositories import organize_operation_jobs as jobs
    client, _ = environment
    client.directories["source"] = [GuangYaFile(f"file-{i}", f"part-{i}.mp4", False, parent_id="source", size=_THRESHOLD - 1, etag=str(i)) for i in range(225)]
    client.directories["source"].append(GuangYaFile("boundary", "keep.mp4", False, parent_id="source", size=_THRESHOLD))
    async def run():
        context = await _context(SQLiteKernelStore(), owner="owner")
        first = _executor(SQLiteKernelStore())
        prepared = await first.prepare("cloud.change", _arguments(trash_filter={"size_lt_bytes": _THRESHOLD}), context=context)
        assert prepared.effect_plan is not None and prepared.effect_plan.preview["data"]["stats"]["trash"] == 225
        frozen = fs.load_fs_change_plan(prepared.effect_plan.metadata["fs_change_plan_id"], owner="owner")
        assert {op["source"]["file_id"] for op in frozen["operations"]} == {f"file-{i}" for i in range(225)}
        client.directories["source"].append(GuangYaFile("later", "later.mp4", False, parent_id="source", size=1))
        accepted = await _executor(SQLiteKernelStore()).execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert accepted.outcome.public_content["status"] == "accepted"
        queued = jobs.get_organize_operation_job(jobs.organize_operation_job_id_from_public_ref(accepted.outcome.public_content["data"]["operation_ref"]))
        assert queued is not None and queued["status"] == "pending"
        stored = fs.load_fs_change_plan(frozen["plan_id"], owner="owner")
        assert len(stored["operations"]) == 225 and stored["operations"] == frozen["operations"]
        with pytest.raises(ToolPipelineError):
            await _executor(SQLiteKernelStore()).execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert client.file_info("later") is not None
    asyncio.run(run())
