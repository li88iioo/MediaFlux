"""原生下载诊断的目录和真实隔离工作区文件 IPC；qB HTTP 后端仅用内存替身。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace

import pytest

from app import sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import download_diagnose_tool
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_sdk_effects import context_for
from tests.test_media_sdk import _qb_response, qb_session as qb_session


def test_download_diagnosis_has_one_native_read_entry_and_no_old_action():
    retired = "downloads.diagnose_queue"
    registered = build_tool_catalog()
    assert not registered.has(retired)
    assert registered.get("downloads.diagnose").effect is ToolEffect.READ
    for module in ("actions", "tools"):
        assert importlib.util.find_spec(f"app.agent.domains.downloads.{module}") is None
    assert sdk.METHODS["downloads.diagnose"] == sdk.downloads.diagnose
    assert "downloads.diagnose" not in sdk.WRITE_METHODS
    assert any(item["name"] == "downloads.diagnose" for item in sdk.describe_methods())
    with initialized_web_runtime():
        kernel = build_agent_kernel_runtime(model=ScriptedModel([])).session
        assert {spec.name for spec in kernel.visible_model_tools({})} == {"agent.run_code"}
        catalog = kernel.catalog
    assert not catalog.has(retired) and not catalog.has(retired.replace(".", "__"))
    assert catalog.get("downloads.diagnose").effect is ToolEffect.READ


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("queue_failed,transfer_failure,empty", [
    (False, None, False), (False, None, True),
    (True, None, False), (False, "http", False), (True, "http", False),
    (False, "invalid", False), (True, "invalid", False),
])
def test_download_diagnosis_runs_through_real_sdk_file_ipc(
    qb_session, monkeypatch, tmp_path, queue_failed, transfer_failure, empty,
):
    transfer_failed = transfer_failure is not None
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=tmp_path, runtime_dir=tmp_path / "runtime",
    ))
    hashes = ["A" * 40, "b" * 64]
    paths = []

    def get(url, **kwargs):
        paths.append(url.rsplit("/", 2)[-2:])
        if url.endswith("/torrents/info"):
            return _qb_response([] if empty else [
                {"hash": identity, "name": "release password=IPC_SECRET", "state": "stalledDL",
                 "progress": 0.4, "dlspeed": 0, "size": 100, "downloaded": 40,
                 "save_path": "/private/ipc", "category": "private-ipc"}
                for identity in hashes
            ], 503 if queue_failed else 200)
        assert url.endswith("/transfer/info"), url
        return _qb_response({} if transfer_failure == "invalid" else
                            {"connection_status": "connected", "dl_info_speed": 321},
                            503 if transfer_failure == "http" else 200)

    qb_session.get.side_effect = get

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog([download_diagnose_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": (
            "import json\nfrom mf import downloads\n"
            "print(json.dumps(downloads.diagnose(), ensure_ascii=False))\n"
        )}, context=context)
        content = result.outcome.public_content
        assert content["ok"], content
        data = content["data"]
        assert data["exit_code"] == 0 and not data["timed_out"], data
        assert data["stderr"] == "" and not data["stderr_truncated"], data
        snapshot = json.loads(data["stdout"])
        expected = ("unavailable" if queue_failed and transfer_failed else
                    "partial" if queue_failed or transfer_failed else
                    "healthy" if empty else "attention")
        assert snapshot["status"] == expected
        call, = data["sdk_calls"]
        assert call["method"] == "downloads.diagnose" and call["status"] == expected
        assert call["ok"] == snapshot["ok"]
        assert call["partial"] == snapshot["partial"] == (expected == "partial")
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert (root / data["stderr_path"].lstrip("/")).read_text() == ""
        archived = json.loads((root / call["response_path"].lstrip("/")).read_text())
        assert archived["result"] == snapshot
        queue = snapshot["data"]
        assert queue["queue_state"] == ("unavailable" if queue_failed else "reachable")
        assert queue["connection"]["api_state"] == ("unavailable" if transfer_failed else "reachable")
        if not transfer_failed:
            assert queue["connection"]["download_speed_bytes_per_sec"] == 321
        if queue_failed:
            assert queue["summary"] == {}
        else:
            assert queue["summary"]["total"] == (0 if empty else 2)
            assert [item["hash"] for item in queue["attention_tasks"]] == ([] if empty else hashes)
        assert not result.effect_plan
        for secret in ("IPC_SECRET", "private-sdk-key", "qb.invalid", "/private/ipc", "private-ipc"):
            assert secret not in json.dumps(snapshot)
        # 固定工作区接力通过 Kernel 原生只读入口得到同一服务的事实。
        native = await pipeline.execute("downloads.diagnose", {}, context=context)
        for key in ("ok", "status", "data", "partial"):
            assert native.outcome.public_content[key] == snapshot[key]
        assert not native.effect_plan

    with initialized_web_runtime():
        asyncio.run(check())
    assert paths == [["torrents", "info"], ["transfer", "info"]] * 2
    assert qb_session.close.call_count == 2
    qb_session.post.assert_not_called()
