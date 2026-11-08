"""原生 recognition：真实 EffectExecutor、冻结确认、冷恢复和隔离 MF 工作区。"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import release_format_save_tool
from app.agent.kernel.state_types import CancellationToken, StalePublicationError
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import recognition_service
from app.modules.recognition import formats
from app.modules.scraper import _parse_release_core
from tests.support import initialized_web_runtime
from tests.test_release_formats import PARENT, TEMPLATE, filename, teaching

SAVE = "recognition.save_release_format"
OWNER = "webk:v1:" + "a" * 64


@pytest.fixture
def runtime():
    with initialized_web_runtime():
        formats.invalidate_cache()
        yield
        formats.invalidate_cache()


async def context_for(store, *, owner=OWNER, session="teaching-session"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="teaching-request")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


def executor(store, *, cls=EffectExecutor, confirmation_store=None):
    return cls(catalog=ToolCatalog([release_format_save_tool()]), state_store=store, reference_store=store,
        authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle(),
        effect_store=ConfirmationEffectPlanStore(confirmation_store or SQLiteConfirmationStore()))


def test_cold_confirmation_executes_original_preview_once_and_keeps_secrets_private(runtime):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        arguments = teaching()
        plan = (await effects.prepare(SAVE, arguments, context=context)).effect_plan
        assert plan is not None and plan.effect is ToolEffect.WRITE
        public = json.dumps(plan.public_dict(), ensure_ascii=False)
        assert "星海航行" in public and "13" in public
        assert all(private not in public for private in ("preview_token", TEMPLATE, PARENT, "snapshot_fingerprint"))
        assert plan.preview["data"]["review_required"] == 1
        assert formats.list_rules() == []
        # 改变原调用对象不能扩大已冻结计划。
        arguments["filenames"].append(filename(16))
        rebuilt = executor(SQLiteKernelStore())
        restored = await rebuilt.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        assert len(restored.arguments["filenames"]) == 2
        with patch.object(formats, "preview", side_effect=AssertionError("确认不能重新预览")), patch.object(
            recognition_service, "save_release_format", wraps=recognition_service.save_release_format,
        ) as save:
            result = (await rebuilt.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["ok"] and result["data"]["created"] and result["data"]["enabled"]
            assert result["data"]["rule_type"] == "release_format"
            assert result["data"]["rule_id"] == formats.list_rules()[0]["id"]
            with pytest.raises(ToolPipelineError):
                await rebuilt.execute_confirmed(plan.plan_id, context=context)
            assert save.call_count == 1
        assert len(formats.list_rules()) == 1
        assert _parse_release_core(filename(15), PARENT).context.episode == 15
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "owner", "session", "publication", "config_aba", "epoch", "registry", "expired"])
def test_native_confirmation_rejects_stale_or_foreign_scope_before_writing(runtime, change):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(SAVE, teaching(), context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change in {"owner", "session"}:
            context = await context_for(store, owner="webk:v1:" + "b" * 64 if change == "owner" else OWNER,
                                        session="other-session" if change == "session" else context.session_id)
        elif change == "publication":
            await store.begin_turn(owner=OWNER, session_id=context.session_id, request_id="superseded")
        elif change == "config_aba":
            original = config.get("TG_QB_SAVE_PATH", "")
            config.set_and_save({"TG_QB_SAVE_PATH": "/changed"})
            config.set_and_save({"TG_QB_SAVE_PATH": original})
        elif change == "registry":
            other = teaching()
            other["draft"]["parent_path"] = "/Anime/Other"
            snapshot = recognition_service.prepare_release_format(other)["snapshot"]
            recognition_service.save_release_format(other, expected_snapshot=snapshot)
        before = formats.list_rules()
        scope = (patch.object(formats, "_PREVIEW_EPOCH", "another-process") if change == "epoch"
                 else patch("itsdangerous.timed.time.time", return_value=time.time() + 901) if change == "expired"
                 else patch.object(formats, "preview", side_effect=AssertionError("确认不能重新预览")))
        with scope, pytest.raises((ToolPipelineError, StalePublicationError)) as raised:
            await effects.execute_confirmed(plan.plan_id, context=context)
        if change in {"epoch", "registry", "expired", "config_aba"}:
            assert isinstance(raised.value, ToolPipelineError)
            assert raised.value.code == "confirmation_stale"
        assert formats.list_rules() == before
    asyncio.run(check())


def test_cancellation_after_preflight_and_before_submit_stays_read_only(runtime):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(SAVE, teaching(), context=context)).effect_plan
        assert plan is not None
        context.cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert formats.list_rules() == []
    asyncio.run(check())


@pytest.mark.parametrize("invalid", ["confirmed", "preview_token", "missing_sample", "same_episode", "cross_title", "wrong_label"])
def test_native_preflight_cannot_invent_samples_or_bypass_teaching_contract(runtime, invalid):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        value = teaching(scope="release") if invalid == "cross_title" else teaching()
        if invalid in {"confirmed", "preview_token"}:
            value[invalid] = True if invalid == "confirmed" else "private"
        elif invalid == "missing_sample":
            value["examples"] = value["examples"][:1]
        elif invalid == "same_episode":
            value["examples"][1]["episode"] = value["examples"][0]["episode"]
        elif invalid == "cross_title":
            value["examples"][1]["title"] = value["examples"][0]["title"]
        else:
            value["examples"][0]["episode"] = 999
        with pytest.raises(ToolPipelineError):
            await effects.prepare(SAVE, value, context=context)
        assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
        assert formats.list_rules() == []
    asyncio.run(check())


def test_sample_only_confirmation_keeps_dotted_names_and_seasons_visible(runtime):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        value = teaching()
        title = "星海航行" * 40
        value["draft"]["template"] = "Example.Team.{title}.track{episode}r{version}.{resolution}.mkv"
        value["examples"] = [{"filename": f"Example.Team.{title}.track{episode:03d}r2.1080p.mkv",
                              "title": title, "episode": episode} for episode in (13, 14)]
        value["filenames"] = None
        plan = (await executor(store).prepare(SAVE, value, context=context)).effect_plan
        assert plan is not None
        data = plan.preview["data"]
        assert data["total"] == data["sample_count"] == 2 and data["batch_total"] == 0
        assert "Example.Team." in data["resources"][0]["title"]
        assert "第13集" in data["resources"][0]["title"] and title in data["resources"][0]["title"]
    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_real_mf_workspace_reads_full_batches_and_save_stops_script_until_cold_confirmation(runtime):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        pipeline = executor(store, cls=ToolPipeline)
        assert isinstance(pipeline, ToolPipeline)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        value = teaching()
        value["filenames"] = [filename(i) for i in range(15, 115)]
        code = ("import json, mf\n"
                f"value = json.loads({json.dumps(value, ensure_ascii=False)!r})\n"
                "parsed = mf.recognition.inspect_filenames(value['filenames'])\n"
                "preview = mf.recognition.preview_release_format(**value)\n"
                "json.dump({'parsed': parsed, 'preview': preview}, open('recognition.json', 'w'))\n"
                "print(len(parsed['rows']), len(preview['rows']), preview['rows'][-1]['after']['episode'])\n")
        read = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
        assert read.effect_plan is None
        data = read.outcome.public_content["data"]
        assert data["stdout"].strip() == "100 100 114" and data["exit_code"] == 0
        assert [call["method"] for call in data["sdk_calls"]] == ["recognition.inspect_filenames", "recognition.preview_release_format"]
        assert all(call.get("response_path") for call in data["sdk_calls"])
        assert formats.list_rules() == []
        later = ("import json, mf\n"
                 "saved = json.load(open('recognition.json'))\n"
                 "print(len(saved['parsed']['rows']), saved['preview']['rows'][-1]['after']['episode'], flush=True)\n"
                 f"mf.recognition.save_release_format(**json.loads({json.dumps(value, ensure_ascii=False)!r}))\n"
                 "open('should-not-run', 'w').write('bad')\n")
        write = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": later}, context=context)
        assert write.effect_plan is not None and write.effect_plan.tool_name == SAVE
        assert write.outcome.public_content["data"]["stdout"].strip() == "100 114"
        assert formats.list_rules() == []
        rebuilt = executor(SQLiteKernelStore(), cls=ToolPipeline)
        assert isinstance(rebuilt, ToolPipeline)
        assert (await rebuilt.execute_confirmed(write.effect_plan.plan_id, context=context)).outcome.public_content["ok"]
        assert len(formats.list_rules()) == 1
        rebuilt.catalog.register(sandbox.run_code_tool(rebuilt))
        observed = await rebuilt.execute(sandbox.RUN_CODE_TOOL,
            {"code": "import pathlib\nprint(pathlib.Path('should-not-run').exists())\n"}, context=context)
        assert observed.outcome.public_content["data"]["stdout"].strip() == "False"
    asyncio.run(check())


class TeachingModel:
    """仅替换外部模型；经真实 mf SDK 执行预览和保存。"""
    def __init__(self, *, save=True):
        self.save = save
        self.requests = []

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            arguments = teaching()
            if self.save:
                arguments.pop("filenames")
            value = json.dumps(arguments, ensure_ascii=False)
            code = (f"import json, mf\nvalue = json.loads({value!r})\n"
                    "r = mf.recognition.preview_release_format(**value)\n"
                    "print(r['summary'], r['examples'])\n")
            if self.save:
                code += "mf.recognition.save_release_format(**value)\n"
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                "teaching-workspace", sandbox.RUN_CODE_TOOL, {"code": code}))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            trusted = any("可信系统结果" in message.content for message in request.messages)
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="发布格式已保存，样本已复核。" if trusted else "已核对原始样本，仅预览。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("save", [False, True])
def test_web_chat_uses_native_workspace_and_existing_persistent_confirmation(runtime, save):
    from app.main import create_app
    from tests.test_release_formats import ReleaseFormatApiTests

    model = TeachingModel(save=save)
    kernel = build_agent_kernel_runtime(model=model)
    with patch("app.routes.agent_api.get_agent_kernel_runtime", return_value=kernel):
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            csrf = ReleaseFormatApiTests.csrf(client.get("/login"))
            response = client.post("/login", data={"csrf_token": csrf, "username": "admin", "password": "123456"}, follow_redirects=False)
            assert response.status_code == 302
            headers = {"X-CSRF-Token": ReleaseFormatApiTests.csrf(client.get("/agent"))}
            response = client.post("/api/agent/query", json={
                "message": "记住这个发布格式，先核对明确样本让我确认保存。" if save else "仅预览用户样本，不要保存。",
                "session_id": "native-teaching-http", "stream": False,
            }, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert formats.list_rules() == []
            if not save:
                assert payload["status"] == "success" and not payload.get("approval"), payload
                return
            assert payload["status"] == "approval_required", payload
            assert payload["approval"]["tool_name"] == SAVE
            assert len(model.requests) == 1
            response = client.post("/api/agent/actions/confirm", json={
                "session_id": "native-teaching-http", "plan_id": payload["approval"]["plan_id"], "stream": False,
            }, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "success", response.text
            assert response.json()["effect_result"]["ok"]
            assert len(model.requests) == 2
            assert len(client.get("/api/tools/release-formats").json()["items"]) == 1
            assert _parse_release_core(filename(15), PARENT).context.episode == 15
