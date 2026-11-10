"""下载确认绑定真实持久登录世代；云盘提交边界离线，不访问真实账号。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.clients import guangya
from app.indexers import downloads
from app.indexers.models import IndexerItem, ResolvedDownload
from app.indexers.result_store import IndexerResultStore
from app.modules import download_dispatcher as dispatcher
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_existing_ports import context_for
from tests.test_guangya_token_operations import _RotatingRawClient

OWNER = "webk:v1:" + "a" * 64
URL = "ed2k://|file|audit.mkv|1024|0123456789abcdef0123456789abcdef|/"


def resource_service(on_resolve=lambda: None):
    item = IndexerItem(site_id="nyaa", site_name="Nyaa", title="audit", download_state="ready",
                       download_kinds=("magnet",), magnet="magnet:?xt=urn:btih:" + "a" * 40)
    result_store = IndexerResultStore()
    result_store.restore("credential-resource", item)
    async def resolve(*_args, **_kwargs):
        on_resolve()
        assert item.magnet is not None
        return ResolvedDownload(kind="magnet", value=item.magnet)
    return SimpleNamespace(result_store=result_store, get_result=lambda identity, **_: result_store.get(identity), resolve=resolve)


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    with initialized_web_runtime():
        token_file = tmp_path / "offline-token.json"
        payload = {"access_token": "offline-access", "refresh_token": "offline-refresh", "expires_at": 1900000000}
        token_file.write_text(json.dumps(payload))
        monkeypatch.setattr(guangya, "_load_raw", lambda: _RotatingRawClient)
        created = []
        def client():
            value = guangya.GuangYaClient(token_file=token_file)
            created.append(value)
            return value
        monkeypatch.setattr(downloads, "GuangYaClient", client)
        monkeypatch.setattr(dispatcher, "GuangYaClient", client)
        def change(action):
            current = client()
            before = current.credential_generation
            if action == "refresh":
                current.refresh_now()
                assert current.credential_generation == before
            else:
                current.clear_tokens()
                token_file.write_text(json.dumps({**payload, "access_token": "offline-other-account"}))
                assert client().credential_generation != before
        submitted = []
        def submit(_url, *, client=None, **_kwargs):
            assert client is not None and client.logged_in
            submitted.append(client.credential_generation)
            return {"ok": True, "task_ids": ["offline-task"]}
        backend = Mock(side_effect=submit)
        monkeypatch.setattr(dispatcher, "submit_offline", backend)
        try:
            yield SimpleNamespace(change=change, backend=backend, submitted=submitted, client=client)
        finally:
            for value in created:
                value.close()


@pytest.mark.parametrize("method", ["downloads.submit", "downloads.retry", "resources.download"])
@pytest.mark.parametrize("action", ["refresh", "relogin"])
def test_confirmation_allows_token_refresh_but_rejects_new_login(credentials, monkeypatch, method, action):
    async def check():
        store = SQLiteKernelStore()
        effects = EffectExecutor(catalog=ToolCatalog([
            sdk_effects.download_submit_tool(), sdk_effects.download_retry_tool(), sdk_effects.resource_download_tool(),
        ]), state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
            authorization=MediaFluxAuthorizationPolicy())
        context = await context_for(store, owner=OWNER)
        if method == "downloads.retry":
            request_id, _ = db.create_download_request("credential-retry", "ed2k", title="audit", source_value=URL)
            db.update_download_request(request_id, targets="guangya", status="failed", gy_status="failed")
            arguments = {"request_id": request_id, "target": "guangya"}
        elif method == "resources.download":
            from app import config
            config.set_and_save({"INDEXER_SEARCH_ENABLED": "1"})
            service = resource_service()
            monkeypatch.setattr(sdk_effects, "get_indexer_service", lambda: service)
            arguments = {"result_ids": ["credential-resource"], "target": "guangya"}
        else:
            arguments = {"url": URL, "target": "guangya"}
        plan = (await effects.prepare(method, arguments, context=context)).effect_plan
        assert plan is not None
        credentials.backend.assert_not_called()
        before = credentials.client().credential_generation
        credentials.change(action)
        if action == "relogin":
            with pytest.raises(ToolPipelineError) as error:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert error.value.code == "confirmation_stale"
            credentials.backend.assert_not_called()
        else:
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["ok"], result
            assert credentials.submitted == [before]
    asyncio.run(check())


def test_generation_change_after_admission_never_submits_to_new_account(credentials, monkeypatch):
    _, snapshot = downloads.preview_download_url(URL, "guangya")
    claim = db.claim_download_request
    def change_after_claim(*args, **kwargs):
        result = claim(*args, **kwargs)
        if result:
            credentials.change("relogin")
        return result
    monkeypatch.setattr(db, "claim_download_request", change_after_claim)
    result = downloads.submit_download_url(URL, "guangya", origin="agent", expected_snapshot=snapshot)
    credentials.backend.assert_not_called()
    assert result["failed"] == ["guangya"] and not result["ok"]


def test_resource_resolution_rechecks_login_before_persisting(credentials):
    service = resource_service(lambda: credentials.change("relogin"))
    snapshot = downloads.preflight_indexer_download(service, "credential-resource", "guangya", scope="agent")
    result = asyncio.run(downloads.download_indexer_result_public(service, "credential-resource", "guangya",
        expected_snapshot=snapshot, scope="agent", origin_namespace="agent"))
    assert result["pre_submission_failure"] and not result["ok"]
    credentials.backend.assert_not_called()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0


def test_qb_preview_does_not_read_cloud_credentials(monkeypatch):
    monkeypatch.setenv("QB_URL", "http://qb.invalid")
    client = Mock(side_effect=AssertionError("qB-only 不读取云盘凭据"))
    monkeypatch.setattr(dispatcher, "GuangYaClient", client)
    monkeypatch.setattr(downloads, "GuangYaClient", client)
    _, snapshot = downloads.preview_download_url(URL, "qb")
    assert "guangya_credential_generation" not in snapshot
    client.assert_not_called()
