"""原生工作区缺集检索、完整文件与整季包冷确认；仅隔离SQLite及下载替身。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import agent_download_verification as agent_download_verification_repo
from app import sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.domains.library.missing_workflow_runtime import (
    MissingMediaWorkflowRuntime,
)
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.sdk_effects import resource_download_tool
from app.indexers import downloads
from app.indexers.models import IndexerItem
from app.indexers.providers.nyaa import NyaaAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from app.modules import missing_resources
from app.modules.agent_download_verification_scheduler import (
    DownloadLibraryVerificationScheduler,
)
from app.repositories.agent_download_verification import (
    SQLiteMissingMediaWorkflowRepository,
)
from tests.support import isolated_test_database
from tests.test_agent_sdk_effects import context_for
from tests.test_missing_resources import _search_result, audit_for


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("precise_timeout", [False, True])
def test_native_search_file_cli_cold_batch_confirmation_and_original_verification(
    monkeypatch, tmp_path, precise_timeout
):
    service = IndexerService(
        registry=IndexerRegistry(
            {
                "nyaa": NyaaAdapter(
                    site_id="nyaa",
                    site_name="Nyaa",
                    base_url="https://fixture.invalid",
                    http=SimpleNamespace(),
                    default_enabled=True,
                )
            }
        ),
        result_store=IndexerResultStore(),
    )
    searches = []

    async def search(request, sites=None, **kwargs):
        searches.append(request)
        if precise_timeout and request.title == "First":
            if request.episode is None:
                return _search_result(query=request.title, items=[])
            if request.episode == 8:
                raise TimeoutError("fixture exact E08 timeout")
        item = IndexerItem(
            "nyaa",
            "Nyaa",
            f"{request.title}.S01E08-09.1080p.WEB-DL",
            download_state="ready",
            download_kinds=("magnet",),
            magnet="magnet:?xt=urn:btih:"
            + ("a" if request.title == "First" else "b") * 40,
        )
        result_id = service.result_store.put(item)
        return _search_result(
            query=request.title,
            items=[{**item.to_public_dict(), "result_id": result_id}],
        )

    monkeypatch.setattr(service, "search_media", search)

    def audit(arguments, **kwargs):
        return audit_for(
            arguments["query"],
            episodes=[8, 9],
            identity="1" if arguments["query"] == "First" else "2",
        )

    monkeypatch.setattr(missing_resources, "audit_series_episodes", audit)
    monkeypatch.setattr(downloads.episode_audit, "audit_series_episodes", audit)
    monkeypatch.setattr(missing_resources, "get_indexer_service", lambda: service)
    monkeypatch.setattr("app.indexers.runtime.get_indexer_service", lambda: service)
    monkeypatch.setattr(
        "app.agent.kernel.ports.sdk_effects.get_indexer_service", lambda: service
    )
    monkeypatch.setattr(
        downloads,
        "download_target_readiness",
        lambda target: {"qb": True, "guangya": True},
    )
    monkeypatch.setattr(downloads, "guangya_download_credential_generation", lambda: 1)
    monkeypatch.setattr(missing_resources.config, "get_bool", lambda *a, **k: True)
    monkeypatch.setattr(
        sandbox,
        "get_runtime_paths",
        lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"),
    )
    writes = []

    def dispatch(request_id, target, **kwargs):
        writes.append((request_id, target))
        download_requests_repo.update_download_request(
            request_id, status="downloading", targets=target, qb_status="downloading"
        )
        return {"ok": True, "status": "submitted", "succeeded": [target], "failed": []}

    monkeypatch.setattr(downloads, "dispatch_request", dispatch)

    def pipeline():
        store = SQLiteKernelStore()
        value = ToolPipeline(
            catalog=ToolCatalog([resource_download_tool()]),
            state_store=store,
            reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
            effect_lifecycle=MediaFluxEffectLifecycle(
                missing_media_runtime=MissingMediaWorkflowRuntime()
            ),
        )
        value.catalog.register(sandbox.run_code_tool(value))
        return value

    async def run():
        first = pipeline()
        context = await context_for(
            first.state_store,
            owner="native-missing-owner",
            session="native-missing-session",
        )
        read = await first.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": """import json,subprocess
from mf import resources
try:
    resources.search_missing(items=[])
except RuntimeError:
    print("参数已纠正")
r=resources.search_missing(items=[{"query":"First","season":1},{"query":"Second","season":1}])
json.dump(r,open("missing.json","w"))
assert len(r["result_ids"])==2 and len(r["verification_targets"])==4
assert r["partial"] == bool(r["groups"][0]["failed_episodes"])
p=json.loads(subprocess.check_output(["mf","resources.missing_workflows","--limit","1"]))
assert p["total"]==2 and p["returned"]==1 and p["has_more"] and p["next_offset"]==1
print(r["status"],p["workflows"][0]["source_tool"])
"""
            },
            context=context,
        )
        data = read.outcome.public_content["data"]
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == [
            "参数已纠正",
            ("partial" if precise_timeout else "success") + " resources.search_missing",
        ]
        expected_searches = 4 if precise_timeout else 2
        assert len(searches) == expected_searches and not writes
        prepared = await first.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": """import json
from mf import resources
r=json.load(open("missing.json"))
assert len(r["items"])==2 and {g["verification"]["tmdb_id"] for g in r["groups"]}=={"1","2"}
resources.download(result_ids=r["result_ids"],target="qb",verification_targets=r["verification_targets"])
raise AssertionError("确认后不能重跑脚本")
"""
            },
            context=context,
        )
        plan = prepared.effect_plan
        assert plan is not None and plan.preview["data"]["verification_count"] == 4, (
            prepared.outcome.public_content
        )
        assert len(plan.arguments["result_ids"]) == 2 and not writes
        assert {
            r["item_state"]
            for r in SQLiteMissingMediaWorkflowRepository().list_for_owner(
                owner=context.owner
            )
        } == {"confirmation_required"}
        service.result_store = IndexerResultStore()
        cold = pipeline()
        pending = await cold.get_pending_plan(
            owner=context.owner, session_id=context.session_id
        )
        assert pending is not None and pending.plan_id == plan.plan_id
        completed = await cold.execute_confirmed(plan.plan_id, context=context)
        assert completed.outcome.public_content["ok"], completed.outcome.public_content
        assert len(writes) == 2 and len(searches) == expected_searches
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert len(writes) == 2
        with sdk.call_owner(context.owner):
            value = sdk.resources.missing_workflows(limit=30)
        assert len(value["workflows"]) == 2 and all(
            len(g["items"]) == 2 for g in value["workflows"]
        )
        assert {r["state"] for g in value["workflows"] for r in g["items"]} == {
            "verification_pending"
        }
        for request_id, _ in writes:
            job = agent_download_verification_repo.get_agent_download_verification(request_id)
            assert (
                job is not None
                and job["owner"] == context.owner
                and len(job["targets"]) == 2
            )
            download_requests_repo.update_download_request(
                request_id, status="completed", qb_status="completed"
            )
        for _ in writes:
            job = agent_download_verification_repo.claim_due_agent_download_verification(
                current_time="2099-01-01 00:00:00"
            )
            assert job is not None
            job["targets"] = [{**p, "result": "visible"} for p in job["targets"]]
            DownloadLibraryVerificationScheduler()._finish(
                job, status="visible", result="visible", attempts=1, current=db.now()
            )
        with sdk.call_owner(context.owner):
            visible = sdk.resources.missing_workflows()
        assert {g["state"] for g in visible["workflows"]} == {"visible"}
        assert {r["state"] for g in visible["workflows"] for r in g["items"]} == {
            "visible"
        }

    try:
        with isolated_test_database():
            asyncio.run(run())
    finally:
        sandbox.clear_workspace(
            owner="native-missing-owner", session_id="native-missing-session"
        )
