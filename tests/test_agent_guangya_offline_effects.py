"""通过真实宿主、确认账本和下载请求验证光鸭离线操作。"""
from __future__ import annotations

import asyncio
import json
import os
import re
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.domains.library.missing_workflow_runtime import MissingMediaWorkflowRuntime
from app.agent.errors import ToolPipelineError
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.sandbox import _SDKRequests
from app.clients.guangya import GuangYaClient
from app.config import web_credentials
from app.indexers import downloads
from app.main import create_app
from app.modules import agent_files, download_dispatcher, download_tracker, offline
from app.repositories import download_requests
from tests.support import initialized_web_runtime
from tests.test_agent_sdk_cloud_effects import _context, _deny_network
from tests.test_guangya_offline_selection import FakeResponse, RESOLVE_SUBFILES_FIXTURE

URL = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=https://tracker.invalid/private-passkey"
SUBMIT = "downloads.submit"
DELETE = "cloud.delete_offline_tasks"


class OfflineProvider(GuangYaClient):
    logged_in = True
    credential_generation = 18

    def __init__(self):
        self.manifest = deepcopy(RESOLVE_SUBFILES_FIXTURE)
        self.rules = replace(offline.OfflineRules.from_config(), target_dir_id="receive", target_dir_name="接收目录")
        self.records = {}
        self.cloud_files = {"cloud-file": "原文件.mp4"}
        self.staging = []
        self.creates = []
        self.deletes = []
        self.reject_create = False
        self.delete_mode = "remove"
        self.transport = Mock(
            request=Mock(side_effect=self._request),
            cloud_resolve_url=Mock(side_effect=lambda _url: self.manifest),
            cloud_task_list=Mock(side_effect=lambda **_query: {"msg": "success", "data": {"list": list(self.records.values())}}),
        )

    @property
    def raw(self):
        return self.transport

    def _call_read(self, operation, callback, **kwargs):
        return callback()

    def close(self):
        return True

    def create_dir(self, name, parent_id="0", *, before_write=None):
        if before_write is not None:
            before_write()
        self.staging.append((name, parent_id))
        return f"staging-{len(self.staging)}"

    def file_info(self, file_id):
        # 模拟 Provider 已清理本次未受理时的空隔离目录。
        assert file_id.startswith("staging-")
        return None

    def add_record(self, task_id, status=2):
        self.records[task_id] = {
            "taskId": task_id, "fileName": f"{task_id}.mp4", "status": status,
            "parentId": "receive", "createTime": 123, "fileId": "cloud-file",
        }

    def _request(self, endpoint, *, method, json, **kwargs):
        assert method == "POST"
        if endpoint.endswith("/nd.bizcloudcollection.s/v1/create_task"):
            self.creates.append(json)
            if self.reject_create:
                return FakeResponse({"code": 400, "msg": "提交失败"})
            task_id = f"cloud-task-{len(self.creates)}"
            self.add_record(task_id, status=0)
            return FakeResponse({"msg": "success", "data": {"taskId": task_id}})
        assert endpoint == "https://api.guangyapan.com/cloudcollection/v2/delete_task"
        assert set(json) == {"taskIds"}
        self.deletes.append(json["taskIds"])
        if self.delete_mode == "timeout":
            raise TimeoutError("internal provider details")
        if self.delete_mode == "rejected":
            return FakeResponse({"msg": "失败", "data": {"success": False}})
        if self.delete_mode == "remove":
            for task_id in json["taskIds"]:
                del self.records[task_id]
        return FakeResponse({"msg": "success"})


@pytest.fixture
def cloud_backend(monkeypatch):
    import socket

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    with initialized_web_runtime():
        client = OfflineProvider()
        factory = Mock(return_value=client, wraps=GuangYaClient)
        for module in (download_dispatcher, downloads, offline, sdk_effects):
            monkeypatch.setattr(module, "GuangYaClient", factory)
        monkeypatch.setattr(offline.OfflineRules, "from_config", lambda: client.rules)
        yield client


def executor(store, runtime=None):
    return EffectExecutor(
        catalog=build_tool_catalog(), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle(missing_media_runtime=runtime),
    )


def submission(manifest, indexes):
    return {"url": URL, "target": "guangya", "file_indexes": indexes, "manifest_digest": manifest["manifest_digest"]}


def request_count():
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]


def test_sdk_selection_cold_confirm_and_duplicate_preserve_original_owner(cloud_backend, tmp_path):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        recent = MissingMediaWorkflowRuntime()
        effects = executor(store, recent)
        (tmp_path / "run").mkdir()
        bridge = _SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()

        async def call(method, arguments, name):
            request = tmp_path / "run/mf-requests" / name
            request.write_text(json.dumps({"method": method, "arguments": arguments}))
            await bridge.dispatch(request.name)
            return tmp_path / "run/mf-responses" / name

        try:
            response = await call("cloud.resolve_offline", {"url": URL}, "resolve.json")
            manifest = json.loads(response.read_text())["result"]
            assert [file["index"] for file in manifest["files"]] == [0, 1, 2, 3]
            assert [file["index"] for file in manifest["files"] if file["selected"]] == [0]
            assert [file["index"] for file in manifest["files"] if file["locked"]] == [2]
            assert "private-passkey" not in json.dumps(manifest)
            # 排除主视频后只提交用户明确保留的附件。
            args = submission(manifest, [3])
            await call(SUBMIT, args, "submit.json")
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
        finally:
            await bridge.close()
        assert plan is not None
        assert [item["title"] for item in plan.preview["data"]["resources"]] == ["readme.txt"]
        assert plan.preview["data"]["receiving_folders"] == ["接收目录"]
        assert request_count() == 0 and not cloud_backend.staging and not cloud_backend.creates

        cold = executor(SQLiteKernelStore(), recent)
        executed = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert executed["status"] == "accepted"
        request_id = executed["data"]["request_id"]
        assert cloud_backend.creates == [{"fileIndexes": [3], "url": URL, "parentId": "staging-1"}]
        assert cloud_backend.staging[0][1] == "receive"
        request = download_requests.get_download_request(request_id)
        assert request is not None
        assert request["origin"] == "agent" and request["gy_expected_file_count"] == 1
        assert json.loads(request["gy_task_ids"]) == ["cloud-task-1"]
        assert sdk.activity.timeline("download", request_id)["ok"]

        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        other = await _context(store, owner="other-owner")
        duplicate = (await effects.prepare(SUBMIT, args, context=other)).effect_plan
        assert duplicate is not None
        duplicate_result = (await effects.execute_confirmed(duplicate.plan_id, context=other)).outcome.public_content
        assert duplicate_result["status"] == "conflict"
        assert request_count() == 1 and len(cloud_backend.creates) == 1
        assert recent.recent_download_store.get(owner=context.owner)[0].request_id == request_id
        assert recent.recent_download_store.get(owner=other.owner)[0].request_id is None

    asyncio.run(run())


@pytest.mark.parametrize("extra", [
    {"file_indexes": []},
    {"file_indexes": None},
    {"file_indexes": [True]},
    {"file_indexes": [2]},
    {"file_indexes": [0, 99]},
    {"file_indexes": [0], "manifest_digest": None},
    {"file_indexes": None, "manifest_digest": None},
    {"file_indexes": [3], "target": "both"},
])
def test_invalid_explicit_selection_never_uses_default(cloud_backend, extra):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        manifest = sdk.cloud.resolve_offline(URL)
        with pytest.raises(ToolPipelineError):
            await executor(store).prepare(SUBMIT, {**submission(manifest, [0]), **extra}, context=context)
        assert request_count() == 0 and cloud_backend.creates == [] and cloud_backend.staging == []

    asyncio.run(run())


@pytest.mark.parametrize("change", ["manifest", "target", "account", "cancel", "link_rule"])
def test_selected_plan_rechecks_before_any_cloud_write(cloud_backend, change):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = executor(store)
        args = submission(sdk.cloud.resolve_offline(URL), [3])
        plan = (await effects.prepare(SUBMIT, args, context=context)).effect_plan
        assert plan is not None
        if change == "manifest":
            cloud_backend.manifest["data"]["subfiles"][-1]["displayName"] = "replaced.txt"
        elif change == "target":
            cloud_backend.rules = replace(cloud_backend.rules, target_dir_id="different")
        elif change == "account":
            cloud_backend.credential_generation += 1
        elif change == "cancel":
            await effects.cancel_effect(plan.plan_id, lease=context.lease)
        else:
            cloud_backend.rules = replace(cloud_backend.rules, exclude_keywords=("private-passkey",))
        try:
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        except ToolPipelineError:
            assert change != "manifest"
        else:
            assert not result["ok"]
            if change == "manifest":
                assert "离线文件清单已变化" in json.dumps(result, ensure_ascii=False)
        assert cloud_backend.creates == [] and cloud_backend.staging == []

    asyncio.run(run())


def test_default_and_failed_retry_follow_original_selection(cloud_backend):
    default = downloads.submit_download_url(URL, "guangya", origin="web")
    assert default["ok"] and cloud_backend.creates[0]["fileIndexes"] == [0]

    async def run():
        url = URL.replace("a" * 40, "b" * 40)
        cloud_backend.reject_create = True
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = executor(store)
        args = {**submission(sdk.cloud.resolve_offline(url), [3]), "url": url}
        plan = (await effects.prepare(SUBMIT, args, context=context)).effect_plan
        assert plan is not None
        failed = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        request_id = failed["data"]["request_id"]
        assert not failed["ok"]
        assert cloud_backend.creates[1]["fileIndexes"] == [3]
        assert not download_dispatcher.download_resubmit_capabilities(
            download_requests.get_download_request(request_id))["both"]["enabled"]
        cloud_backend.reject_create = False
        retry = (await effects.prepare("downloads.retry", {"request_id": request_id, "target": "guangya"},
                                       context=context)).effect_plan
        assert retry is not None
        success = (await effects.execute_confirmed(retry.plan_id, context=context)).outcome.public_content
        assert success["ok"] and cloud_backend.creates[2]["fileIndexes"] == [3]
        assert success["data"]["source_request_id"] == request_id

    asyncio.run(run())


@pytest.fixture
def torrent_payload(cloud_backend):
    from tests.test_torrent_manifest import _torrent

    payload = _torrent({
        b"name": b"Bundle", b"piece length": 16_384, b"pieces": b"0" * 20,
        b"files": [
            {b"length": 100, b"path": [b"Episode.mkv"]},
            {b"length": 12, b"path": [b"Notes.txt"]},
        ],
    })
    manifest = {"code": 0, "data": {"btResInfo": {"subfilesNum": 2, "subfiles": [
        {"fileIndex": 0, "fileName": "Episode.mkv", "fileSize": 100},
        {"fileIndex": 1, "fileName": "Notes.txt", "fileSize": 12},
    ]}}}
    cloud_backend.transport.cloud_resolve_torrent.return_value = manifest
    return payload


@pytest.mark.parametrize("source_kind,manual", [
    ("url", True), ("artifact_id", True), ("artifact_id", False),
])
def test_torrent_source_cold_confirm_keeps_bytes_and_deduplicates_magnet(
    cloud_backend, torrent_payload, monkeypatch, source_kind, manual,
):
    from app.modules import rss

    payload = torrent_payload
    cloud_backend.rules = replace(cloud_backend.rules, min_file_mb=0)
    fetch = Mock(return_value=(payload, {}))
    monkeypatch.setattr(rss, "_fetch_rss_payload", fetch)
    url = "https://fixture.invalid/bundle.torrent"
    magnet = download_dispatcher.torrent_download_input("bundle.torrent", payload).source_value

    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = executor(store)
        if source_kind == "artifact_id":
            artifact = agent_files.store_file(context.owner, context.session_id, "Bundle.TORRENT", BytesIO(payload))
            source = {"artifact_id": artifact["id"]}
        else:
            source = {"url": url}
        with sdk.call_owner(context.owner, session_id=context.session_id):
            inspected = sdk.cloud.resolve_offline(**source)
        assert [row["index"] for row in inspected["files"]] == [0, 1]
        assert [row["index"] for row in inspected["files"] if row["selected"]] == [0]
        assert "tracker.invalid" not in json.dumps(inspected)
        args: dict[str, object] = {**source, "target": "guangya"}
        if manual:
            args.update(file_indexes=[1], manifest_digest=inspected["manifest_digest"])
        plan = (await effects.prepare(SUBMIT, args, context=context)).effect_plan
        assert plan is not None
        assert plan.arguments == args and "artifact_id" not in plan.public_dict()["editing"]["schema"]["properties"]
        if manual:
            assert [row["title"] for row in plan.preview["data"]["resources"]] == ["Notes.txt"]
        assert request_count() == 0 and not cloud_backend.creates and not cloud_backend.staging
        cold = executor(SQLiteKernelStore())
        result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["ok"]
        saved = download_requests.get_download_request(result["data"]["request_id"])
        assert saved is not None
        assert saved["torrent_data"] == payload
        assert saved["source_value"] == (magnet if source_kind == "artifact_id" else url)
        assert cloud_backend.creates == [{"fileIndexes": [int(manual)], "url": magnet, "parentId": "staging-1"}]
        assert cloud_backend.transport.cloud_resolve_torrent.call_count == (3 if manual else 2)
        assert all(call.args == (payload,) for call in cloud_backend.transport.cloud_resolve_torrent.call_args_list)
        if source_kind == "artifact_id":
            fetch.assert_not_called()
        duplicate = downloads.submit_download_url(magnet, "guangya", origin="web")
        assert duplicate["status"] == "duplicate" and duplicate["request_id"] == saved["id"]
        assert request_count() == 1 and len(cloud_backend.creates) == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["before_preview", "same_size", "size", "removed", "manifest"])
def test_artifact_selection_rejects_changed_source(cloud_backend, torrent_payload, change):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        artifact_id = agent_files.store_file(
            context.owner, context.session_id, "Bundle.torrent", BytesIO(torrent_payload),
        )["id"]
        _, source = agent_files.open_file(context.owner, context.session_id, artifact_id)
        with source:
            path = Path(str(source.name))
        new_torrent = torrent_payload.replace(b"/announce", b"/changedx")
        assert len(new_torrent) == len(torrent_payload) and new_torrent != torrent_payload
        assert download_dispatcher.parse_torrent_metadata(new_torrent) == download_dispatcher.parse_torrent_metadata(torrent_payload)
        with sdk.call_owner(context.owner, session_id=context.session_id):
            manifest = sdk.cloud.resolve_offline(artifact_id=artifact_id)
        args = {"artifact_id": artifact_id, "target": "guangya",
                "file_indexes": [1], "manifest_digest": manifest["manifest_digest"]}
        effects = executor(store)
        if change == "before_preview":
            path.write_bytes(new_torrent)
            with pytest.raises(ToolPipelineError, match="离线文件清单已变化"):
                await effects.prepare(SUBMIT, args, context=context)
        else:
            plan = (await effects.prepare(SUBMIT, args, context=context)).effect_plan
            assert plan is not None
            cold = executor(SQLiteKernelStore())
            if change == "manifest":
                reply = cloud_backend.transport.cloud_resolve_torrent.return_value
                reply["data"]["btResInfo"]["subfiles"][1]["fileName"] = "Replaced.txt"
                result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
                assert not result["ok"] and "离线文件清单已变化" in result["error"]
            else:
                if change == "removed":
                    path.unlink()
                else:
                    path.write_bytes(new_torrent if change == "same_size" else torrent_payload[:-1])
                with pytest.raises(ToolPipelineError) as error:
                    await cold.execute_confirmed(plan.plan_id, context=context)
                assert error.value.code == "confirmation_stale"
        if change != "manifest":
            assert request_count() == 0
        assert not cloud_backend.creates and not cloud_backend.staging

    asyncio.run(run())


def test_torrent_artifact_requires_one_source_and_the_original_session(cloud_backend, torrent_payload):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        artifact_id = agent_files.store_file(
            context.owner, context.session_id, "Bundle.torrent", BytesIO(torrent_payload),
        )["id"]
        effects = executor(store)
        for invalid in ({}, {"url": URL, "artifact_id": artifact_id}, {"artifact_id": "/work/Bundle.torrent"}):
            with sdk.call_owner(context.owner, session_id=context.session_id), pytest.raises(ValueError):
                sdk.cloud.resolve_offline(**invalid)
            with pytest.raises(ToolPipelineError):
                await effects.prepare(SUBMIT, {"target": "guangya", **invalid}, context=context)
        args = {"artifact_id": artifact_id, "target": "guangya"}
        plan = (await effects.prepare(SUBMIT, args, context=context)).effect_plan
        assert plan is not None
        for foreign in (
            await _context(store, owner="other-owner", session=context.session_id),
            await _context(store, owner=context.owner, session="other-session"),
        ):
            with sdk.call_owner(foreign.owner, session_id=foreign.session_id), pytest.raises(ValueError, match="当前会话"):
                sdk.cloud.resolve_offline(artifact_id=artifact_id)
            with pytest.raises(ToolPipelineError, match="当前会话"):
                await effects.prepare(SUBMIT, args, context=foreign)
            with pytest.raises(ToolPipelineError):
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=foreign)
        assert request_count() == 0 and not cloud_backend.creates
        cloud_backend.transport.cloud_resolve_torrent.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("mode,status", [
    ("remove", "completed"), ("propagating", "accepted"), ("timeout", "outcome_unknown"),
])
def test_record_deletion_verifies_only_selected_and_leaves_download_history(cloud_backend, mode, status):
    async def run():
        cloud_backend.add_record("done")
        cloud_backend.add_record("keep")
        cloud_backend.delete_mode = mode
        row = download_dispatcher.create_request(downloads.direct_url_input(URL), "", "", origin="web")
        download_requests.update_download_request(row["id"], targets="guangya", status="completed",
                                                  gy_status="completed", gy_task_id="done")
        saved_request = download_requests.get_download_request(row["id"])
        assert saved_request is not None
        stages = sdk.activity.timeline("download", row["id"])["data"]["stages"]
        store = SQLiteKernelStore()
        context = await _context(store)
        plan = (await executor(store).prepare(DELETE, {"task_ids": ["done"]}, context=context)).effect_plan
        assert plan is not None
        assert not cloud_backend.deletes
        assert "保留云盘文件" in json.dumps(plan.public_dict(), ensure_ascii=False)

        cold = executor(SQLiteKernelStore())
        result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["status"] == status
        assert result["data"]["verified"] is (mode == "remove")
        assert cloud_backend.deletes == [["done"]]
        assert "keep" in cloud_backend.records and ("done" not in cloud_backend.records) is (mode == "remove")
        assert cloud_backend.cloud_files == {"cloud-file": "原文件.mp4"}
        assert download_requests.get_download_request(row["id"]) == saved_request
        assert sdk.activity.timeline("download", row["id"])["data"]["stages"] == stages
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert cloud_backend.deletes == [["done"]]

    asyncio.run(run())


def test_record_deletion_waits_for_idless_web_download_history(cloud_backend, monkeypatch):
    def provider_request(endpoint, **options):
        response = cloud_backend._request(endpoint, **options)
        if endpoint.endswith("/create_task"):
            cloud_backend.records["cloud-task-1"].update(
                parentId=options["json"]["parentId"], url=options["json"]["url"], status=2,
            )
            return FakeResponse({"code": 0})
        return response

    cloud_backend.transport.request.side_effect = provider_request
    cloud_backend.add_record("unrelated")
    cloud_backend.add_record("leave-alone")
    monkeypatch.setattr(download_tracker, "GuangYaClient", lambda: cloud_backend)

    def csrf(page):
        match = re.search(r'name="csrf_token" (?:value|content)="([^"]+)"', page.text)
        assert match is not None
        return match.group(1)

    def history(web):
        result = web.get("/api/downloads/logs", params={"source": "guangya"})
        assert result.status_code == 200, result.text
        log, = result.json()["items"]
        return log

    with TestClient(create_app(start_background=False)) as web:
        username, password = web_credentials()
        login = web.post("/login", data={
            "username": username, "password": password, "csrf_token": csrf(web.get("/login")),
        }, follow_redirects=False)
        assert login.status_code == 302
        result = web.post("/api/downloads/direct/submit", data={"url": URL, "target": "guangya"},
                          headers={"X-CSRF-Token": csrf(web.get("/downloads"))})
        assert result.status_code == 200 and result.json()["status"] == "submitted", result.text
        request_id = result.json()["request_id"]
        row = download_requests.get_download_request(request_id)
        assert row is not None
        assert (row["gy_isolated"], row["gy_target_dir"], row["gy_task_id"], row["gy_task_ids"]) == (
            1, "staging-1", "", "[]",
        )
        assert history(web)["status"] == "submitted"

        async def run():
            store = SQLiteKernelStore()
            context = await _context(store)
            effects = executor(store)
            args = {"task_ids": ["cloud-task-1", "unrelated"]}
            with pytest.raises(ToolPipelineError, match="仍在被 MediaFlux 跟踪"):
                await effects.prepare(DELETE, args, context=context)
            assert cloud_backend.deletes == []
            assert (await effects.prepare(DELETE, {"task_ids": ["unrelated"]}, context=context)).effect_plan

            assert download_tracker.DownloadTracker().run_once() == 1
            settled = download_requests.get_download_request(request_id)
            assert settled is not None
            assert (settled["gy_status"], settled["gy_task_id"]) == ("completed", "cloud-task-1")
            log = history(web)
            assert (log["request_id"], log["status"], log["backend_task_id"]) == (
                request_id, "success", "cloud-task-1",
            )

            plan = (await effects.prepare(DELETE, args, context=context)).effect_plan
            assert plan is not None
            cold = executor(SQLiteKernelStore())
            confirmed = await cold.execute_confirmed(plan.plan_id, context=context)
            assert confirmed.outcome.public_content["status"] == "completed"
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=context)
            assert cloud_backend.deletes == [["cloud-task-1", "unrelated"]]
            assert set(cloud_backend.records) == {"leave-alone"}
            assert cloud_backend.cloud_files == {"cloud-file": "原文件.mp4"}
            assert history(web) == log

        asyncio.run(run())


@pytest.mark.parametrize("change", [
    "running", "resource-id-only", "tracking", "tracking-legacy-source", "account", "cancel", "record", "missing",
    "rejected",
])
def test_record_deletion_refuses_running_unverified_or_changed_records(cloud_backend, change):
    async def run():
        cloud_backend.add_record("done")
        cloud_backend.records["done"]["url"] = URL
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = executor(store)
        args = {"task_ids": ["done"]}
        if change == "resource-id-only":
            record = cloud_backend.records["done"]
            record["resId"] = record.pop("taskId")
            task, = cloud_backend.list_offline_tasks()
            assert task["id"] == "done"
            with pytest.raises(ToolPipelineError, match="光鸭账号或离线任务已变化"):
                await effects.prepare(DELETE, args, context=context)
            assert cloud_backend.deletes == []
            return
        if change == "running":
            cloud_backend.records["done"]["status"] = 1
            with pytest.raises(ToolPipelineError, match="删除记录不能用于取消任务"):
                await effects.prepare(DELETE, args, context=context)
            assert cloud_backend.deletes == []
            return

        plan = (await effects.prepare(DELETE, args, context=context)).effect_plan
        assert plan is not None
        if change in {"tracking", "tracking-legacy-source"}:
            row = download_dispatcher.create_request(downloads.direct_url_input(URL), "", "")
            download_requests.update_download_request(
                row["id"], status="downloading", targets="guangya", gy_status="downloading",
                gy_task_ids='["done"]' if change == "tracking" else "[]",
            )
        elif change == "account":
            cloud_backend.credential_generation += 1
        elif change == "cancel":
            await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "record":
            cloud_backend.records["done"]["createTime"] = 124
        elif change == "missing":
            del cloud_backend.records["done"]
        else:
            cloud_backend.delete_mode = "rejected"
        try:
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        except ToolPipelineError:
            pass
        else:
            assert not result["ok"]
        assert cloud_backend.deletes == ([["done"]] if change == "rejected" else [])
        assert not cloud_backend.creates and cloud_backend.cloud_files == {"cloud-file": "原文件.mp4"}

    asyncio.run(run())
