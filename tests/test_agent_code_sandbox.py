"""普通代码工作区：真实进程、SDK、持久文件和停止；旧 AST 白名单/ToolPipeline call 协议已退役。"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelMessage, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.model_context_budget import bounded_model_messages
from tests.support import isolated_test_database

from tests.test_agent_kernel_core import ScriptedModel, read_tool
from tests.test_cancelled_read_inline import sqlite_store


async def make_context(store, owner="o", session="s"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="workspace")
    async def progress(_):
        pass
    return ToolCallContext(owner=owner, session_id=session, request_id="workspace", turn_id=lease.turn_id,
                           lease=lease, cancellation=CancellationToken(), report_progress=progress)


class TerminalBudgetTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {"ok": True, "status": "success", "data": {
            "exit_code": 124, "timed_out": True, "working_directory": "/work",
            "stdout": "字" * 16000, "stderr": "TimeoutError: 等待来源超时\n",
            "stdout_path": "/output/" + "a" * 32 + ".stdout",
            "stderr_path": "/output/" + "a" * 32 + ".stderr",
            "stdout_truncated": False, "stderr_truncated": False,
            "sdk_calls": [{"method": "library.search", "ok": False, "status": "TimeoutError"}],
        }}
        self.messages = [ModelMessage(role="user", content="只读，保留原始条件"),
            ModelMessage(role="assistant", tool_calls=(ModelToolCall("run", "agent__run_code", {"code": "read()"}),)),
            ModelMessage(role="tool", tool_name="agent__run_code", tool_call_id="run", content=json.dumps(self.receipt))]

    def fit(self, available):
        return bounded_model_messages(self.messages, history_end=0, consumed_end=0,
            tool_definitions=(), system_prompt="", context_window_tokens=16384,
            output_tokens=1024, conservative=True, output_available=available)

    def test_unread_preview_preserves_timeout_and_complete_short_stderr(self):
        view = self.fit(lambda _: True)
        result = json.loads(view[-1].content)
        self.assertEqual(view[:-1], tuple(self.messages[:-1]))
        self.assertEqual(view[-1].tool_call_id, "run")
        self.assertTrue(result["details_omitted"])
        self.assertNotIn("output_archived", result)
        self.assertIn("省略", result["data"]["stdout"])
        self.assertTrue(result["data"]["stdout_truncated"])
        self.assertLess(len(result["data"]["stdout"]), 16000)
        self.assertEqual({k: v for k, v in result["data"].items() if k not in {"stdout", "stdout_truncated"}},
                         {k: v for k, v in self.receipt["data"].items() if k not in {"stdout", "stdout_truncated"}})
        self.assertEqual(json.loads(self.messages[-1].content), self.receipt)

    def test_unread_overflow_keeps_output_when_either_complete_file_is_unavailable(self):
        from app.agent.errors import ToolPipelineError

        for missing in ("stdout", "stderr", None):
            with self.subTest(missing=missing):
                available = (lambda path: not path.endswith("." + missing)) if missing else None
                with self.assertRaises(ToolPipelineError) as raised:
                    self.fit(available)
                self.assertEqual(raised.exception.code, "context_budget_exceeded")
                self.assertEqual(json.loads(self.messages[-1].content), self.receipt)

    def test_completed_large_output_uses_file_only_when_available(self):
        messages = [*self.messages, ModelMessage(role="assistant", content="来源超时，未执行写入"),
                    ModelMessage(role="user", content="继续，其他条件不变"),
                    ModelMessage(role="assistant", tool_calls=(ModelToolCall("next", "agent__run_code", {"code": "read()"}),)),
                    replace(self.messages[-1], tool_call_id="next")]
        for available in (True, False):
            with self.subTest(files_available=available):
                view = bounded_model_messages(messages, history_end=4,
                    tool_definitions=(), system_prompt="", context_window_tokens=272000,
                    output_tokens=16000, output_available=lambda _: available)
                old = json.loads(view[2].content)
                if available:
                    self.assertTrue(old['output_archived'])
                    self.assertEqual(old['data'], {key: value for key, value in self.receipt['data'].items()
                                                  if key not in {'stdout', 'stderr'}})
                else:
                    self.assertEqual(old, self.receipt)
                self.assertEqual(old["data"]["stderr_path"], self.receipt["data"]["stderr_path"])
                self.assertEqual(old["data"]["exit_code"], 124)
                self.assertTrue(old["data"]["timed_out"])
                self.assertEqual(view[0].content, messages[0].content)
                self.assertEqual(view[3:], tuple(messages[3:]))
                self.assertEqual(json.loads(messages[2].content), self.receipt)


class WorkspaceContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_exposes_sdk_methods_without_loading_business_schemas(self):
        store = InMemorySessionStateStore()
        catalog = ToolCatalog([ *[
            read_tool(f"library.item_{i}", domain="library", description="查媒体库")
            for i in range(100)
        ]])
        pipeline = ToolPipeline(catalog=catalog, state_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="你好。"),
                                ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=pipeline, state_store=store, model_tools=["agent.run_code"])
        events = [event async for event in session.run(AgentInput(
            owner="o", session_id="native-entry", message="你好，我想查媒体库。"))]
        self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
        request = model.requests[0]
        self.assertEqual({tool["name"] for tool in request.tools},
                         {catalog.get(name).model_name for name in ("agent.run_code",)})
        self.assertNotIn("library.item_99", request.system_prompt)
        description = next(tool["description"] for tool in request.tools
                           if tool["name"] == catalog.get(sandbox.RUN_CODE_TOOL).model_name)
        self.assertEqual(description, catalog.get(sandbox.RUN_CODE_TOOL).description)
        for name, purpose in sandbox.describe_namespaces().items():
            self.assertIn(f"mf.{name}: {purpose}", description)
        for skill in sandbox._SKILLS.glob("*/SKILL.md"):
            self.assertIn(f"/skills/{skill.parent.name}/SKILL.md", description)

    async def test_explicit_business_read_remains_callable_without_a_discovery_round(self):
        store = InMemorySessionStateStore()
        catalog = ToolCatalog([ read_tool(
            "library.detail", domain="library", description="作品详情",
            handler=lambda *_: {"summary": "查到详情", "data": {"title": "样本作品"}},
        )])
        pipeline = ToolPipeline(catalog=catalog, state_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read", "library.detail", {})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="查到样本作品。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=pipeline, state_store=store)
        events = [event async for event in session.run(AgentInput(
            owner="o", session_id="native-entry", message="继续看刚才的作品。"))]
        self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
        self.assertIn(catalog.get("library.detail").model_name,
                         {tool["name"] for tool in model.requests[0].tools})
        results = [json.loads(m.content) for m in model.requests[-1].messages if m.role == "tool"]
        self.assertEqual(results[-1]["data"]["title"], "样本作品")

    async def test_workspace_stays_visible_when_internal_catalog_changes(self):
        store = InMemorySessionStateStore()
        catalog = ToolCatalog()
        pipeline = ToolPipeline(catalog=catalog, state_store=store)
        code = sandbox.run_code_tool(pipeline)
        catalog.register(code)
        session = AgentSession(model=ScriptedModel([]), catalog=catalog, pipeline=pipeline,
                               state_store=store, model_tools=[code.name])
        catalog.register(read_tool("library.test"))
        self.assertEqual(session.visible_model_tools({}), (code,))


@unittest.skipUnless(sandbox.sandbox_supported(), "需要可运行 Bubblewrap 的 Linux；宿主和单容器均执行同一套测试")
class NativeWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        self.temp = tempfile.TemporaryDirectory(prefix="mf-native-workspace-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        paths = SimpleNamespace(data_dir=self.directory, runtime_dir=self.directory / ("long-runtime-" * 10))
        self.paths_patch = patch.object(sandbox, "get_runtime_paths", return_value=paths)
        self.paths_patch.start()
        self.addCleanup(self.paths_patch.stop)
        self.store = InMemorySessionStateStore()
        self.catalog = ToolCatalog()
        self.pipeline = ToolPipeline(catalog=self.catalog, state_store=self.store)
        self.catalog.register(sandbox.run_code_tool(self.pipeline))
        self.context = await make_context(self.store)

    def background_processes(self):
        # worker PID 是命名空间内编号；通过唯一测试参数核验宿主真实进程已收净。
        found = []
        for command_line in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if self.directory.name.encode() in command_line.read_bytes().split(b"\0"):
                    found.append(command_line.parent.name)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
        return found

    async def execute(self, code, *, language="python", timeout=15, context=None):
        result = await self.pipeline.execute(sandbox.RUN_CODE_TOOL,
            {"code": code, "language": language, "timeout_seconds": timeout}, context=context or self.context)
        payload = json.loads(result.outcome.full_model_content or result.outcome.model_content)
        return payload["data"]

    async def test_python_has_sdk_binding_but_keeps_process_state_isolated(self):
        with patch.dict(sandbox.METHODS, {"library.episodes": lambda series_id: {"id": series_id}}):
            first = await self.execute('print(mf.library.episodes("first")["id"])\n'
                                       'print(__name__, __file__)\n'
                                       'temporary_value = 42\nmf = None')
            second = await self.execute('print(mf.library.episodes("second")["id"])\n'
                                        'print("temporary_value" in globals())')
        self.assertEqual(first["exit_code"], 0, first["stderr"])
        self.assertEqual(first["stdout"], "first\n__main__ /run/code.py\n")
        self.assertEqual(second["exit_code"], 0, second["stderr"])
        self.assertEqual(second["stdout"], "second\nFalse\n")

    async def test_sdk_activity_stream_survives_history_without_polluting_model_context(self):
        from app.agent.public_view import public_conversation_messages

        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                "read", sandbox.RUN_CODE_TOOL, {"code": 'mf.library.episodes("sample")'})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="本次取得两项，仍有后续页。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="好的。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=self.catalog, pipeline=self.pipeline,
                               state_store=self.store, model_tools=[sandbox.RUN_CODE_TOOL])
        with patch.dict(sandbox.METHODS, {"library.episodes": lambda series_id: {
            "items": [{"id": "one"}, {"id": "two"}], "has_more": True, "partial": True,
        }}):
            events = [event async for event in session.run(AgentInput(
                owner="activity-owner", session_id="activity-native", message="查询季集"))]
        self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
        activities = [event.payload["activity"] for event in events if event.payload.get("activity")]
        self.assertEqual([item["status"] for item in activities], ["running", "partial"])
        self.assertEqual(activities[0]["id"], activities[1]["id"])
        self.assertIn("本次返回 2 项", activities[1]["summary"])
        self.assertIn("还有后续页", activities[1]["summary"])
        state = await self.store.load(owner="activity-owner", session_id="activity-native")
        visible = public_conversation_messages(state.conversation)
        self.assertEqual(visible[-1]["activities"], [activities[-1]])
        self.assertNotIn("activities", model.requests[1].messages[-1].to_dict())
        _ = [event async for event in session.run(AgentInput(
            owner="activity-owner", session_id="activity-native", message="谢谢"))]
        state = await self.store.load(owner="activity-owner", session_id="activity-native")
        self.assertEqual(public_conversation_messages(state.conversation)[1]["activities"], [activities[-1]])

    async def test_python_passes_full_search_items_directly_to_inventory(self):
        from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
        from app.discovery.models import MediaCard
        from app.discovery.search import DiscoverySearchResult
        from unittest.mock import Mock

        search = Mock()
        search.search.return_value = DiscoverySearchResult(
            query="测试剧", page=1, has_more=False, providers_attempted=("tmdb",),
            providers_succeeded=("tmdb",), errors=(), items=tuple(
                MediaCard(provider="tmdb", external_id=identity, tmdb_id=identity, media_type="tv",
                          title=f"测试剧{identity}", year="2020", release_date="2020-01-01")
                for identity in ("77", "88")
            ),
        )
        client = Mock()
        client.list_media_identity_inventory.return_value = MediaIdentityInventory(
            candidates=[MediaIdentityCandidate(tmdb_id="77", name="测试剧77", media_type="tv")], total=1,
        )
        with patch("app.discovery.search.get_discovery_search_service", return_value=search), patch(
            "app.discovery.service.require_discovery_enabled",
        ), patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
            result = await self.execute('import json\nfrom mf import media, library\n'
                'found = media.search("测试剧", providers=["tmdb"], media_type="tv")\n'
                'checked = library.presence(found["items"])\n'
                'print(json.dumps([(x["tmdb_id"], x["library_status"]) for x in checked["data"]["items"]]))')

        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertEqual(json.loads(result["stdout"]), [["77", "present"], ["88", "missing"]])
        self.assertEqual([row["method"] for row in result["sdk_calls"]], ["media.search", "library.presence"])
        self.assertEqual(search.search.call_count, 1)
        client.list_media_identity_inventory.assert_called_once()
        call = client.list_media_identity_inventory.call_args
        self.assertEqual(call.args, ("tv",))
        options = dict(call.kwargs)
        self.assertTrue(callable(options.pop("cancelled")))
        self.assertEqual(options, {"max_items": 5000, "page_size": 200})
        client.close.assert_called_once_with()

    async def test_python_help_reaches_model_and_history_without_leaking_credentials(self):
        from tests.test_sensitive_data import SESSION_BATCH_HELP

        code = "help(mf.session.batch)\nprint('password:\\n\\nhistory-private-value')"
        model = ScriptedModel([[
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall("help", sandbox.RUN_CODE_TOOL, {"code": code})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ], [ModelEvent(ModelEventType.TEXT_DELTA, text="已核对帮助"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session = AgentSession(model=model, catalog=self.catalog, pipeline=self.pipeline,
            state_store=self.store, model_tools=[sandbox.RUN_CODE_TOOL])
        events = [event async for event in session.run(AgentInput(
            owner="help-owner", session_id="help-native", message="查看原生帮助"))]
        self.assertEqual(events[-1].payload["status"], "success")
        messages = [message for message in model.requests[1].messages if message.role == "tool"]
        self.assertEqual(len(messages), 1)
        result = json.loads(messages[0].content)
        self.assertEqual(result["data"]["exit_code"], 0)
        stdout = result["data"]["stdout"]
        self.assertTrue(stdout.startswith(SESSION_BATCH_HELP), stdout)
        self.assertNotIn("history-private-value", stdout)
        self.assertIn("password:\n\n********", stdout)
        root, _ = sandbox._workspace_location("help-owner", "help-native")
        raw_stdout = (root / result["data"]["stdout_path"].lstrip("/")).read_text()
        self.assertTrue(raw_stdout.startswith(SESSION_BATCH_HELP))
        self.assertIn("history-private-value", raw_stdout)
        state = await self.store.load(owner="help-owner", session_id="help-native")
        saved = [row for row in state.conversation if row["role"] == "tool"]
        self.assertEqual(json.loads(saved[0]["content"])["data"]["stdout"], stdout)

    async def test_full_service_results_stay_in_workspace_across_model_rounds(self):
        payload = "full-service-payload-" * 600_000
        def search(query: str):
            return {"items": list(range(10_000)), "body": payload}
        commands = [
            'import json\nfrom mf import library\n'
            'v = library.episodes("sample")\n'
            'json.dump(v, open("facts.json", "w"))\nprint(len(v["items"]))',
            'import json\nv = json.load(open("facts.json"))\n'
            'print(v["items"][-1], len(v["body"]))',
        ]
        rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                             tool_call=ModelToolCall(str(i), sandbox.RUN_CODE_TOOL, {"code": code})),
                   ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
                  for i, code in enumerate(commands)]
        rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="已核对完整结果。"),
                       ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        model = ScriptedModel(rounds)
        session = AgentSession(model=model, catalog=self.catalog,
                               pipeline=self.pipeline, state_store=self.store)
        with patch.dict(sandbox.METHODS, {"library.episodes": search}):
            events = [event async for event in session.run(AgentInput(
                owner="o", session_id="model-chain", message="读取全部结果，再核对最后一项。"))]
        self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
        self.assertEqual(len(model.requests), 3)
        tools = [json.loads(message.content) for message in model.requests[-1].messages if message.role == "tool"]
        self.assertEqual([item["data"]["stdout"].strip() for item in tools], ["10000", f"9999 {len(payload)}"])
        history = json.dumps([message.to_dict() for message in model.requests[-1].messages])
        self.assertNotIn("full-service-payload-", history)
        self.assertLess(len(history), 10_000)  # 12MB结果在工作区，模型只接收命令与实际输出。

    async def test_failed_answer_does_not_report_process_exits_as_completed_queries(self):
        from app.agent.kernel.provider_model import ModelProviderError

        class FailsAfterCommands(ScriptedModel):
            async def stream(self, request, *, cancellation):
                if not self.rounds:
                    raise ModelProviderError("simulated upstream timeout")
                async for event in super().stream(request, cancellation=cancellation):
                    yield event

        model = FailsAfterCommands([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                str(i), sandbox.RUN_CODE_TOOL, {"code": code})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
            for i, code in enumerate(('print("saved observation")', 'raise NameError("bad variable")'))
        ])
        session = AgentSession(model=model, catalog=self.catalog,
                               pipeline=self.pipeline, state_store=self.store)
        events = [event async for event in session.run(AgentInput(
            owner="o", session_id="failed-answer", message="核对结果后回答"))]
        self.assertEqual(events[-1].payload["status"], "partial")
        answer = events[-1].payload["answer"]
        self.assertNotIn("已完成的查询", answer)
        self.assertNotIn("退出码", answer)
        self.assertIn("尚未形成", answer)
        state = await self.store.load(owner="o", session_id="failed-answer")
        results = [json.loads(row["content"]) for row in state.conversation if row["role"] == "tool"]
        self.assertEqual([row["data"]["exit_code"] for row in results], [0, 1])
        self.assertIn("NameError", results[-1]["data"]["stderr"])

    async def test_old_terminal_outputs_restore_from_cold_sqlite_and_scoped_files(self):
        with isolated_test_database() as database:
            store = sqlite_store()
            catalog = ToolCatalog()
            pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
            catalog.register(sandbox.run_code_tool(pipeline))
            commands = [
                ('from mf import library\nimport sys\nlibrary.episodes("sample")\n'
                 'print("X" * 12000 + "OLD_END")\nprint("error" * 100, file=sys.stderr)\nsys.exit(7)'),
                'print("second-result")',
            ]
            rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                str(i), sandbox.RUN_CODE_TOOL, {"code": code})), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
                for i, code in enumerate(commands)]
            model = ScriptedModel([*rounds, [ModelEvent(ModelEventType.TEXT_DELTA, text="已记录退出状态"),
                                           ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
            session = AgentSession(model=model, catalog=catalog,
                                   pipeline=pipeline, state_store=store)
            with patch.dict(sandbox.METHODS, {"library.episodes": lambda query: {"ok": False, "status": "partial", "cached": True,
                                                                              "raw_fact": "SDK_ORIGINAL"}}), \
                 patch.object(pipeline, "save_result", side_effect=AssertionError("native输出不得进入通用快照")):
                events = [e async for e in session.run(AgentInput(
                    owner="alice", session_id="cold-native", message="保留全部条件，核验末项"))]
            self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
            tools = [json.loads(m.content) for m in model.requests[-1].messages if m.role == "tool"]
            self.assertEqual([t["data"]["exit_code"] for t in tools], [7, 0])
            self.assertNotIn("output_archived", tools[0])
            self.assertEqual(tools[0]["data"]["stdout"], "X" * 12000 + "OLD_END\n")
            self.assertEqual(tools[0]["data"]["stderr"], "error" * 100 + "\n")
            self.assertEqual(tools[1]["data"]["stdout"], "second-result\n")
            saved = await sqlite_store().load(owner="alice", session_id="cold-native")
            originals = [json.loads(row["content"]) for row in saved.conversation if row["role"] == "tool"]
            self.assertEqual(originals[0]["data"]["stdout"], "X" * 12000 + "OLD_END\n")
            self.assertEqual(originals[0]["data"]["stderr"], "error" * 100 + "\n")
            self.assertNotIn("output_archived", originals[0])
            self.assertTrue(all("result_handle" not in t and "read_tool" not in t for t in tools))
            self.assertNotIn("agent__read_result", {t["name"] for t in model.requests[-1].tools})
            other = await make_context(store, owner="bob", session="cold-native")
            read_path = tools[0]["data"]["sdk_calls"][0]["response_path"]
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code":
                f'from pathlib import Path\nprint(Path({tools[0]["data"]["stdout_path"]!r}).exists(), '
                f'Path({tools[0]["data"]["stderr_path"]!r}).exists(), Path({read_path!r}).exists())'}, context=other)
            self.assertEqual(json.loads(result.outcome.model_content)["data"]["stdout"].strip(), "False False False")
            state_file = self.directory / "cold-output.json"
            state_file.write_text(json.dumps({
                "database": str(database), "data_dir": str(self.directory),
                "runtime_dir": str(self.directory / ("long-runtime-" * 10)), "tools": originals, "commands": commands,
            }))
            script = textwrap.dedent(r'''
                import tests
                import asyncio, json, sys
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import patch
                from app import database as db
                from app.agent.kernel import sandbox
                from app.agent.kernel.capabilities import ToolCatalog
                from app.agent.kernel.events import AgentEventType
                from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
                from app.agent.kernel.pipeline import ToolPipeline
                from app.agent.kernel.session import AgentSession
                from app.agent.kernel.state_types import AgentInput
                from tests.test_agent_kernel_core import ScriptedModel
                from tests.test_cancelled_read_inline import sqlite_store
                state = json.loads(Path(sys.argv[1]).read_text())
                db.configure_database(state["database"], test_mode=True)
                paths = SimpleNamespace(data_dir=Path(state["data_dir"]), runtime_dir=Path(state["runtime_dir"]))
                async def run():
                    store = sqlite_store()
                    saved = await store.load(owner="alice", session_id="cold-native")
                    assert [json.loads(row["content"]) for row in saved.conversation if row["role"] == "tool"] == state["tools"]
                    catalog = ToolCatalog()
                    pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
                    catalog.register(sandbox.run_code_tool(pipeline))
                    path = state["tools"][0]["data"]["stdout_path"]
                    stderr_path = state["tools"][0]["data"]["stderr_path"]
                    code = (f'from pathlib import Path\ns = Path({path!r}).read_text()\ne = Path({stderr_path!r}).read_text()\n'
                            'assert s == "X" * 12000 + "OLD_END\\n"\nassert e == "error" * 100 + "\\n"\n'
                            'print(len(s), s[-8:].strip())')
                    response_path = state["tools"][0]["data"]["sdk_calls"][0]["response_path"]
                    code += f'\nimport json\nv = json.load(open({response_path!r}))\nassert v["result"]["raw_fact"] == "SDK_ORIGINAL" and v["result"]["cached"] is True'
                    model = ScriptedModel([[
                        ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("read-tail", sandbox.RUN_CODE_TOOL, {"code": code})),
                        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")], [
                        ModelEvent(ModelEventType.TEXT_DELTA, text="已核验末项"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
                    session = AgentSession(model=model, catalog=catalog, model_tools=[sandbox.RUN_CODE_TOOL], pipeline=pipeline, state_store=store)
                    with patch.object(pipeline, "save_result", side_effect=AssertionError("不得重建终端快照")):
                        events = [e async for e in session.run(AgentInput(owner="alice", session_id="cold-native", message="继续核验输出末项"))]
                    assert events[-1].type == AgentEventType.TURN_COMPLETED and events[-1].payload["status"] == "success", events[-1].payload
                    for request in model.requests:
                        old = [json.loads(m.content) for m in request.messages if m.role == "tool"][:2]
                        assert len(old) == 2
                        assert old[0] == state["tools"][0]
                        assert old[1] == state["tools"][1]
                        assert all("result_handle" not in got and "read_tool" not in got for got in old)
                        assert old[0]["data"]["sdk_calls"][0]["cached"] is True
                        assert any(m.role == "user" and m.content == "保留全部条件，核验末项" for m in request.messages)
                        assert [c.arguments["code"] for m in request.messages for c in m.tool_calls][:2] == state["commands"]
                    current = [json.loads(m.content) for m in model.requests[-1].messages if m.role == "tool"][-1]
                    assert current["data"]["exit_code"] == 0 and current["data"]["stderr"] == ""
                    assert current["data"]["stdout"].strip() == "12008 OLD_END"
                    assert "output_archived" not in current
                    saved = await sqlite_store().load(owner="alice", session_id="cold-native")
                    assert [json.loads(row["content"]) for row in saved.conversation if row["role"] == "tool"][:2] == state["tools"]
                with patch.object(sandbox, "get_runtime_paths", return_value=paths):
                    asyncio.run(run())
                print("cold-native-output-ok")
            ''')
            completed = await asyncio.to_thread(subprocess.run, [sys.executable, "-B", "-c", script, str(state_file)],
                                                capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("cold-native-output-ok", completed.stdout)

    async def test_current_native_overflow_previews_errors_and_recovers_from_complete_files(self):
        error_tail = "RuntimeError: 源数据格式无效，未执行写入\n"
        command = ('import sys\nprint("字" * 16000)\n'
                   'sys.stderr.write("诊断上下文\\n" * 4000)\n'
                   'raise RuntimeError("源数据格式无效，未执行写入")')

        class RecoveringModel(ScriptedModel):
            async def stream(self, request, *, cancellation):
                if len(self.requests) == 1:
                    output = json.loads(next(m.content for m in request.messages if m.role == "tool"))
                    data = output["data"]
                    code = (f'from pathlib import Path\nout = Path({data["stdout_path"]!r}).read_text()\n'
                            f'err = Path({data["stderr_path"]!r}).read_text()\n'
                            f'print(len(out), out.endswith("\\n"), err.endswith({error_tail!r}))')
                    self.rounds.insert(0, [
                        ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                            tool_call=ModelToolCall("read-files", sandbox.RUN_CODE_TOOL, {"code": code})),
                        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                    ])
                async for event in super().stream(request, cancellation=cancellation):
                    yield event

        model = RecoveringModel([[
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                       tool_call=ModelToolCall("large-current", sandbox.RUN_CODE_TOOL, {"code": command})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ], [ModelEvent(ModelEventType.TEXT_DELTA, text="已读取完整输出并核对错误。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session = AgentSession(model=model, catalog=self.catalog,
                               pipeline=self.pipeline, state_store=self.store,
                               limits=SessionLimits(context_window_tokens=16384, max_output_tokens=1024))
        with isolated_test_database():
            events = [e async for e in session.run(AgentInput(
                owner="o", session_id="large-current", message="核验本次大输出"))]
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["status"], "success", events[-1].payload)
        self.assertEqual(len(model.requests), 3)
        preview = json.loads(next(m.content for m in model.requests[1].messages if m.role == "tool"))
        self.assertTrue(preview["details_omitted"])
        self.assertEqual(preview["data"]["exit_code"], 1)
        self.assertFalse(preview["data"]["timed_out"])
        self.assertTrue(preview["data"]["stderr"].endswith(error_tail))
        self.assertIn("省略", preview["data"]["stdout"])
        self.assertIn("省略", preview["data"]["stderr"])
        readback = json.loads([m.content for m in model.requests[-1].messages if m.role == "tool"][-1])
        self.assertEqual(readback["data"]["exit_code"], 0)
        self.assertEqual(readback["data"]["stdout"].strip(), "16001 True True")
        state = await self.store.load(owner="o", session_id="large-current")
        output = json.loads(next(row["content"] for row in state.conversation if row["role"] == "tool"))
        self.assertNotIn("output_archived", output)
        self.assertEqual(output["data"]["exit_code"], 1)
        self.assertTrue(output["data"]["stdout"].startswith("字" * 100))
        self.assertTrue(output["data"]["stdout"].endswith("字" * 100 + "\n"))
        self.assertTrue(output["data"]["stdout_truncated"])
        self.assertEqual(len(output["data"]["stdout"]), 16000)
        workspace, _ = sandbox._workspace_location("o", "large-current")
        self.assertEqual((workspace / output["data"]["stdout_path"].lstrip("/")).read_text(), "字" * 16000 + "\n")
        self.assertTrue((workspace / output["data"]["stderr_path"].lstrip("/")).read_text().endswith(error_tail))
        for stream in ("stdout", "stderr"):
            self.assertEqual(preview["data"][stream + "_path"], output["data"][stream + "_path"])

    async def test_concurrent_workspaces_bind_host_identity_without_model_owner_argument(self):
        from app import sdk

        barrier = threading.Barrier(2, timeout=5)

        def read(query: str):
            barrier.wait()
            return {"owner": sdk._owner()}

        contexts = [await make_context(self.store, owner=owner, session="owner-read")
                    for owner in ("alice", "bob")]
        with patch.dict(sandbox.METHODS, {"library.search": read}):
            results = await asyncio.gather(*[
                self.execute("from mf import library\nprint(library.search('sample')['owner'])", context=context)
                for context in contexts
            ])
        self.assertEqual([result["stdout"].strip() for result in results], ["alice", "bob"])
        with self.assertRaisesRegex(ValueError, "身份"):
            sdk._owner()

    async def test_organize_history_python_and_cli_preserve_full_page_and_details(self):
        from app import database as db
        from tests.support import isolated_test_database

        with isolated_test_database():
            with db.get_conn() as conn:
                conn.executemany("INSERT INTO organize_log(source,original_path,new_path,title,status,error,created_at,updated_at) "
                    "VALUES('guangya','/fixture/source','/fixture/target',?,'deleted','token=fixture-private','2026-01-01','2026-01-01')",
                    [(f"回收条目{i}",) for i in range(63)])
            result = await self.execute("import json\nfrom pathlib import Path\nfrom mf import activity\n"
                "p=activity.organize_logs(status='deleted',limit=60)\n"
                "Path('history.json').write_text(json.dumps(p,ensure_ascii=False))\n"
                "print(p['returned'],p['total'],p['next_offset'])")
            self.assertEqual(result["exit_code"], 0, result["stderr"])
            self.assertEqual(result["stdout"].strip(), "60 63 60")
            cli = await self.execute("mf activity.organize_logs --status deleted --offset 60 > tail.json", language="shell")
            self.assertEqual(cli["exit_code"], 0, cli["stderr"])
            final = await self.execute("import json\n"
                "p=json.load(open('history.json'));t=json.load(open('tail.json'))\n"
                "assert len({i['id'] for i in p['items']+t['items']})==63\n"
                "assert all(i['kind']=='organize' and i['status']=='deleted' for i in t['items'])\n"
                "assert 'fixture-private' not in json.dumps(p)\n"
                "print(t['returned'],t['has_more'])")
            self.assertEqual(final["exit_code"], 0, final["stderr"])
            self.assertEqual(final["stdout"].strip(), "3 False")

    async def test_sdk_errors_are_observations_and_normal_retry_keeps_full_result(self):
        def read(query: str):
            if query == "bad":
                raise ValueError("query 不正确；token=secret-token-for-test")
            return {"ok": True, "status": "success", "items": list(range(100)), "blob": "x" * (5 * 1024 * 1024),
                    "cached": True, "partial": False, "has_more": None,
                    "technical_evidence": {"dynamic_range": None}}
        started = datetime.now(timezone.utc)
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            result = await self.execute('import json, mf\nfrom pathlib import Path\n'
                'try: mf.library.episodes("bad")\nexcept mf.Error as e: print(e.code, str(e))\n'
                'value = mf.library.episodes("valid")\n'
                'Path("large.json").write_text(json.dumps(value))\n'
                'print(len(value["items"]), len(value["blob"]))\n'
                'try: mf._request("library.delete", {})\nexcept mf.Error as e: print(str(e))\n'
                'try: mf.library.episodes("x" * (4 * 1024 * 1024))\n'
                'except mf.Error as e: print(e.code)')
            failed = await self.execute('mf.library.episodes("bad")')
            self.assertNotEqual(failed["exit_code"], 0)
            self.assertIn("mf.Error: ValueError:", failed["stderr"])
            self.assertNotIn("secret-token", failed["stderr"])
            cli = await self.execute('mf library.episodes --query bad', language="shell")
            self.assertEqual(cli["exit_code"], 1)
            self.assertEqual(cli["stderr"].count("ValueError:"), 1)
            self.assertNotIn("secret-token", cli["stderr"])
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("ValueError", result["stdout"])
        self.assertNotIn("secret-token", result["stdout"] + result["stderr"])
        self.assertIn("100 5242880", result["stdout"])
        self.assertIn("未知 SDK 方法", result["stdout"])
        self.assertIn("request_too_large", result["stdout"])
        self.assertEqual([row["ok"] for row in result["sdk_calls"]], [False, True, False])
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        for call in result["sdk_calls"][:2]:
            path = workspace / call["response_path"].lstrip("/")
            archived = json.loads(path.read_text())
            self.assertEqual(archived["method"], "library.episodes")
            self.assertEqual(archived["called_at"], call["called_at"])
            self.assertLessEqual(started, datetime.fromisoformat(call["called_at"]))
            self.assertLessEqual(datetime.fromisoformat(call["called_at"]), datetime.fromisoformat(archived["completed_at"]))
            self.assertLessEqual(datetime.fromisoformat(archived["completed_at"]), datetime.now(timezone.utc))
            self.assertEqual(path.stat().st_uid, os.geteuid())
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertNotIn("secret-token", path.read_text())
        self.assertEqual(json.loads((workspace / result["sdk_calls"][0]["response_path"].lstrip("/")).read_text())["error"]["code"], "ValueError")
        valid = result["sdk_calls"][1]
        self.assertEqual({key: valid[key] for key in ("cached", "partial", "has_more")},
                         {"cached": True, "partial": False, "has_more": None})
        self.assertNotIn("truncated", valid)
        self.assertNotIn("technical_evidence", valid)
        self.assertNotIn("response_path", result["sdk_calls"][2])  # 未实际调用未知方法。
        self.assertEqual(json.loads((workspace / valid["response_path"].lstrip("/")).read_text())["result"], read("valid"))
        followup = await self.execute('import json\nv = json.load(open("large.json"))\n'
                                      'print(v["items"][-1], len(v["blob"]), v["blob"][-1])')
        self.assertEqual(followup["exit_code"], 0, followup["stderr"])
        self.assertEqual(followup["stdout"].strip(), "99 5242880 x")
        reread = await self.execute(f'import json\nv = json.load(open({valid["response_path"]!r}))["result"]\n'
                                   'print(v["items"][-1], len(v["blob"]), v["technical_evidence"]["dynamic_range"])')
        self.assertEqual(reread["stdout"].strip(), "99 5242880 None")

    async def test_normal_python_shell_and_persistent_files(self):
        first = await self.execute('import json, pathlib, sqlite3, subprocess, asyncio, inspect, mf\n'
            'print("SDK源码", "def calendar(" in inspect.getsource(mf.media.calendar))\n'
            'pathlib.Path("facts.json").write_text(json.dumps({"集数": 53}))\n'
            'print(subprocess.check_output(["/bin/sh", "-c", "printf shell-ok"], text=True))\n'
            'async def value(): return await asyncio.to_thread(lambda: 42)\nprint(asyncio.run(value()))\n')
        self.assertEqual(first["exit_code"], 0, first["stderr"])
        self.assertIn("shell-ok\n42", first["stdout"])
        self.assertIn("SDK源码 True", first["stdout"])
        self.assertEqual(first["stderr"], "")
        second = await self.execute('python -c \'import json; print(json.load(open("facts.json"))["集数"])\'\nmf --help\nmf library.audit --help', language="shell")
        self.assertEqual(second["exit_code"], 0, second["stderr"])
        self.assertIn("53", second["stdout"])
        self.assertIn("--tmdb-id", second["stdout"])
        for namespace in sandbox.describe_namespaces():
            self.assertIn(namespace, second["stdout"])
        text = await self.execute(
            'set -e\nprintf "beta\\nalpha\\nbeta\\n" > rows.txt\n'
            'sort rows.txt | uniq | head -n 1\n'
            'find /work -name rows.txt | xargs wc -l\n'
            'sed -n 1p rows.txt | grep beta', language="shell")
        self.assertEqual(text["exit_code"], 0, text["stderr"])
        self.assertEqual(text["stdout"].splitlines(), ["alpha", "3 /work/rows.txt", "beta"])
        skill = await self.execute('from pathlib import Path\n'
            'print("\\n".join(str(p) for p in sorted(Path("/skills").glob("*/SKILL.md"))))\n'
            'path = Path("/skills/media-research/SKILL.md")\n'
            'print("watchlist.list" in path.read_text())\n'
            'try: path.write_text("override")\n'
            'except OSError as exc:\n'
            '    assert exc.errno in (13, 30)\n    print("builtin-readonly")')
        self.assertEqual(skill["exit_code"], 0, skill["stderr"])
        self.assertIn("True\nbuiltin-readonly", skill["stdout"])
        for installed in sandbox._SKILLS.glob("*/SKILL.md"):
            self.assertIn(f"/skills/{installed.parent.name}/SKILL.md", skill["stdout"])

    async def test_old_runtime_upgrade_preserves_work_and_output_paths(self):
        root, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        for directory in ("work", "output", "usr/local/bin"):
            (root / directory).mkdir(parents=True)
        (root / ".version").write_text("retired-runtime-template")
        (root / "usr/local/bin/python").write_text("old binary")
        (root / "work/notes.json").write_text('{"kept":53}')
        (root / "output/old.sdk.json").write_text('{"kept":79}')
        if os.geteuid() == 0:
            os.chown(root / "work/notes.json", 100777, 100777)
            os.chown(root / "work", 100777, 100777)
            (root / "work").chmod(0o700)
        result = await self.execute('import json\nprint(json.load(open("notes.json"))["kept"])\n'
                                   'print(json.load(open("/output/old.sdk.json"))["kept"])')
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertEqual(result["stdout"].splitlines(), ["53", "79"])
        self.assertFalse((root / ".version").exists())
        self.assertFalse((root / "usr").exists())

    async def test_python_exception_can_be_corrected_on_next_call(self):
        first = await self.execute('print("before-error")\n1 / 0')
        self.assertNotEqual(first["exit_code"], 0)
        self.assertIn("ZeroDivisionError", first["stderr"])
        self.assertIn("before-error", first["stdout"])
        second = await self.execute('import sys\nprint(12 / 3)\nprint("warning", file=sys.stderr)')
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(second["stdout"].strip(), "4.0")
        self.assertEqual(second["stderr"], "warning\n")

    async def test_resource_sources_use_public_registry_from_python_and_cli(self):
        from tests.test_agent_indexer_readiness import _Adapter, _Service

        adapters = {f"site-{i}": _Adapter(f"site-{i}", f"来源{i}", pagination=True, kinds=("magnet",)) for i in range(101)}
        service = _Service(adapters, set(adapters) - {"site-100"})
        with patch("app.indexers.runtime.config.get_bool", return_value=True), patch(
            "app.indexers.runtime.get_indexer_service", return_value=service,
        ):
            result = await self.execute('import json, subprocess\nfrom mf import resources\n'
                'a = resources.sources()\nb = json.loads(subprocess.check_output(["mf", "resources.sources"]))\n'
                'assert a == b\nassert a["scope"] == "agent" and a["network_accessed"] is False\n'
                'assert a["sites"][-1]["reason"] == "site_disabled"\n'
                'print(a["status"], len(a["sites"]), a["counts"]["enabled"])')
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].strip(), "ready 101 100")
        self.assertEqual(result["stderr"], "")
        self.assertEqual([row["method"] for row in result["sdk_calls"]], ["resources.sources"] * 2)

    async def test_concurrent_sdk_calls_retry_and_subprocess_cli(self):
        active = maximum = 0
        lock = threading.Lock()
        def read(query: str, tmdb_id: str = "", library_name: str = ""):
            nonlocal active, maximum
            if query == "bad":
                raise ValueError("请提供片名")
            with lock:
                active += 1
                maximum = max(active, maximum)
            try:
                time.sleep(0.025)
                return {"ok": True, "status": "success", "query": query, "items": list(range(80))}
            finally:
                with lock:
                    active -= 1
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            result = await self.execute('import json, subprocess, mf\nfrom concurrent.futures import ThreadPoolExecutor\n'
                'from mf import library, Error\n'
                'try: mf._request("library.episodes", {"query": "bad"})\nexcept Error as e: print(e.code)\n'
                'with ThreadPoolExecutor(8) as pool:\n'
                '    rows = list(pool.map(library.episodes, [str(i) for i in range(8)]))\n'
                'print(json.dumps([[r["query"], len(r["items"])] for r in rows]))\n'
                'value = subprocess.check_output(["mf", "library.episodes", "--query", "CLI"], text=True)\n'
                'print(json.loads(value)["query"])')
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("ValueError", result["stdout"])
        self.assertIn('["7", 80]', result["stdout"])
        self.assertIn("CLI", result["stdout"])
        self.assertEqual(len(result["sdk_calls"]), 10)
        self.assertFalse(result["sdk_calls"][0]["ok"])
        self.assertTrue(all(row["ok"] for row in result["sdk_calls"][1:]))
        self.assertEqual(maximum, 4)
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        self.assertEqual(len({row["response_path"] for row in result["sdk_calls"]}), 10)
        for row in result["sdk_calls"]:
            archived = json.loads((workspace / row["response_path"].lstrip("/")).read_text())
            self.assertEqual(archived["called_at"], row["called_at"])
            self.assertEqual({key: archived["arguments"][key] for key in ("tmdb_id", "library_name")},
                             {"tmdb_id": "", "library_name": ""})
            self.assertFalse({"cached", "partial", "truncated", "has_more"} & row.keys())
            if row["ok"]:
                self.assertEqual(archived["result"]["query"], archived["arguments"]["query"])
                self.assertEqual(archived["result"]["items"], list(range(80)))
        self.assertFalse((workspace / "run/mf-responses").exists())

    async def test_output_is_saved_completely_and_recoverable(self):
        body = "输出开始\n" + "正文" * 12000 + "\n尾部汇总：成功 3，失败 1\n"
        result = await self.execute(f'print({body!r}, end="")')
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stdout"].startswith("输出开始\n"))
        self.assertTrue(result["stdout"].endswith("尾部汇总：成功 3，失败 1\n"))
        self.assertLessEqual(len(result["stdout"]), 16000)
        self.assertNotIn("\ufffd", result["stdout"])
        workspace, _ = sandbox._workspace_location("o", "s")
        self.assertEqual((workspace / result["stdout_path"].lstrip("/")).read_text(), body)
        followup = await self.execute(f'from pathlib import Path\nprint(len(Path({result["stdout_path"]!r}).read_text()))')
        self.assertEqual(followup["exit_code"], 0, followup["stderr"])
        self.assertEqual(followup["stdout"].strip(), str(len(body)))

    async def test_long_stderr_exposes_final_error_without_replaying_command(self):
        result = await self.execute('import sys\nprint("读取已完成")\n'
                                   'sys.stderr.write("诊断上下文\\n" * 4000)\n'
                                   'raise RuntimeError("源数据格式无效，未执行写入")')
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "读取已完成\n")
        self.assertTrue(result["stderr_truncated"])
        self.assertTrue(result["stderr"].startswith("诊断上下文\n"))
        self.assertTrue(result["stderr"].endswith("RuntimeError: 源数据格式无效，未执行写入\n"))
        self.assertLessEqual(len(result["stderr"]), 16000)

    async def test_output_preview_preserves_short_output_and_utf8_boundaries(self):
        for body in ("", "字" * 16000, "开头\n" + "字" * 12000 + "😀" * 8000 + "\n结尾\n"):
            with self.subTest(characters=len(body)):
                result = await self.execute(f'print({body!r}, end="")')
                self.assertEqual(result["exit_code"], 0, result["stderr"])
                if len(body) <= 16000:
                    self.assertEqual(result["stdout"], body)
                    self.assertFalse(result["stdout_truncated"])
                else:
                    self.assertTrue(result["stdout_truncated"])
                    self.assertTrue(result["stdout"].startswith("开头\n"))
                    self.assertTrue(result["stdout"].endswith("😀" * 100 + "\n结尾\n"))
                    self.assertNotIn("\ufffd", result["stdout"])
                    self.assertLessEqual(len(result["stdout"]), 16000)

    async def test_observation_io_failure_retains_completed_calls_and_stops_worker(self):
        for failure in ("open", "startup", "write", "archive", "read"):
            with self.subTest(failure=failure):
                context = await make_context(self.store, session="observation-" + failure)
                workspace, _ = sandbox._workspace_location(context.owner, context.session_id)
                write, replace = sandbox._SDKRequests.write, os.replace
                open_file, start_process = Path.open, asyncio.create_subprocess_exec
                writes = moves = 0
                def fail_write(rpc, name, data):
                    nonlocal writes
                    writes += 1
                    if writes == 2 and failure == "write":
                        raise OSError(errno.ENOSPC, "No space left on device; token=secret-token")
                    return write(rpc, name, data)
                def fail_archive(source, target, **kwargs):
                    nonlocal moves
                    if str(target).endswith(".sdk.json"):
                        moves += 1
                        if moves == 2 and failure == "archive":
                            raise OSError(errno.ENOSPC, "No space left on device; token=secret-token")
                    return replace(source, target, **kwargs)
                def fail_output(path, mode="r", *args, **kwargs):
                    if failure == "open" and path.suffix == ".stdout" and mode == "wb":
                        raise OSError(errno.ENOSPC, "cannot create stdout; token=secret-token")
                    if failure in {"write", "read"} and path.suffix == ".stderr" and mode == "r":
                        raise OSError(errno.EACCES, "cannot read stderr; token=secret-token")
                    return open_file(path, mode, *args, **kwargs)
                async def fail_start(*args, **kwargs):
                    if failure == "startup":
                        raise OSError(errno.EAGAIN, "cannot start worker; token=secret-token")
                    return await start_process(*args, **kwargs)
                code = ('from mf import library\nimport subprocess\n'
                        'library.episodes("first")\n'
                        f'child = subprocess.Popen(["python", "-c", "import time; time.sleep(30)", {self.directory.name!r}])\n'
                        'print(child.pid, flush=True)\nlibrary.episodes("second")')
                with patch.dict(sandbox.METHODS, {"library.episodes": lambda query: {"query": query, "cached": True}}), \
                     patch.object(sandbox._SDKRequests, "write", new=fail_write), \
                     patch.object(sandbox.os, "replace", side_effect=fail_archive), \
                     patch.object(Path, "open", new=fail_output), \
                     patch.object(sandbox.asyncio, "create_subprocess_exec", new=fail_start):
                    result = await asyncio.wait_for(sandbox.run_code(self.pipeline,
                        {"code": code, "timeout_seconds": 10}, context), 5)
                payload = result.outcome.public_content
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], "failed")
                data = payload["data"]
                self.assertEqual(data["error"]["code"], "workspace_io_error")
                expected = {"open": "cannot create stdout", "startup": "cannot start worker", "read": "cannot read stderr"}
                self.assertIn(expected.get(failure, "No space left on device"), data["error"]["message"])
                self.assertEqual(payload["summary"], data["error"]["message"])
                self.assertNotIn("secret-token", json.dumps(payload))
                if failure in {"open", "startup"}:
                    self.assertEqual(data["sdk_calls"], [])
                    self.assertIsNone(data["exit_code"])
                    self.assertFalse(data["timed_out"])
                    self.assertEqual(data["stdout"], "")
                    if failure == "open":
                        self.assertIsNone(data["stdout_path"])
                        self.assertIsNone(data["stderr_path"])
                    self.assertFalse((workspace / "run/mf-responses").exists())
                    continue
                self.assertEqual(len(data["sdk_calls"]), 2)
                first, second = data["sdk_calls"]
                self.assertTrue(first["ok"] and second["ok"])  # 两次宿主调用均已发生；归档错误不能洗掉事实。
                if failure == "read":
                    self.assertTrue((workspace / second["response_path"].lstrip("/")).is_file())
                else:
                    self.assertNotIn("response_path", second)
                self.assertTrue(first["cached"] and second["cached"])
                self.assertIn("called_at", second)
                self.assertEqual(json.loads((workspace / first["response_path"].lstrip("/")).read_text())["result"]["query"], "first")
                self.assertEqual((workspace / data["stdout_path"].lstrip("/")).read_text().strip(), data["stdout"].strip())
                self.assertFalse(self.background_processes())

    async def test_cancel_keeps_completed_read_without_publishing_late_result(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def read(query: str):
            if query == "slow":
                entered.set()
                release.wait(5)
                finished.set()
            return {"query": query}
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            task = asyncio.create_task(self.execute('from mf import library\nlibrary.episodes("done")\nlibrary.episodes("slow")'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.context.cancellation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 3)
        archived = [json.loads(path.read_text()) for path in (workspace / "output").glob("*.sdk.json")]
        self.assertEqual([item["result"]["query"] for item in archived], ["done"])
        self.assertFalse((workspace / "run/mf-responses").exists())
        sandbox.clear_workspace(owner=self.context.owner, session_id=self.context.session_id)
        self.assertFalse(workspace.exists())

    async def test_cancel_cloud_listing_stops_provider_pages_and_closes_client(self):
        from tests.test_guangya_read_contracts import ReadClient

        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        client = ReadClient(None)
        client._token_lock = threading.RLock()
        pages = []
        def page(**arguments):
            pages.append((arguments["parent_id"], arguments["page"]))
            entered.set()
            release.wait(5)
            if arguments["parent_id"] is None:
                return {"data": {"total": 2, "list": [{"fileId": f"dir-{arguments['page']}",
                    "fileName": "目录", "resType": 2}]}}
            return {"data": {"total": 0, "list": []}}
        client.raw.fs_files.side_effect = page
        original_close = client.close
        def close():
            try:
                return original_close()
            finally:
                closed.set()
        with patch("app.clients.guangya.GuangYaClient", return_value=client), patch.object(client, "close", close):
            task = asyncio.create_task(self.execute('from mf import cloud\ncloud.list(recursive=True)'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.context.cancellation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(closed.wait, 3))
        self.assertEqual(pages, [(None, 0)])
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        self.assertEqual(list((workspace / "output").glob("*.sdk.json")), [])

    async def test_cancel_resource_search_releases_bridge_and_provider(self):
        from app.indexers import runtime
        from app.indexers.registry import IndexerRegistry
        from app.indexers.result_store import IndexerResultStore
        from app.indexers.service import IndexerService
        from tests.test_indexer_service import FakeAdapter

        entered, finished = asyncio.Event(), asyncio.Event()

        class GatedAdapter(FakeAdapter):
            async def search(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                    return await super().search(request)
                finally:
                    finished.set()

        service = IndexerService(registry=IndexerRegistry({"nyaa": GatedAdapter("nyaa")}),
                                 result_store=IndexerResultStore(),
                                 site_timeout_seconds=3, total_timeout_seconds=4)
        loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(loop)
        task = None
        try:
            with patch.object(runtime, "get_indexer_service", return_value=service):
                task = asyncio.create_task(self.execute('mf.resources.search("Demo", sites=["nyaa"])'))
                await asyncio.wait_for(entered.wait(), 2)
                self.context.cancellation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                await asyncio.wait_for(finished.wait(), 1)
            workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
            self.assertEqual(list((workspace / "output").glob("*.sdk.json")), [])
            self.assertFalse(self.background_processes())
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await service.aclose()
            runtime.unbind_indexer_event_loop(loop)

    async def test_unclassified_registered_method_is_rejected_without_calling_it(self):
        invoked = []

        def unclassified():
            invoked.append(True)
            return {"ok": True}

        with patch.dict(sandbox.METHODS, {"cloud.unclassified": unclassified}):
            data = await self.execute('mf.cloud.unclassified()')
        self.assertNotEqual(data["exit_code"], 0)
        self.assertIn("未声明行为类别", data["stderr"])
        self.assertEqual(invoked, [])
        self.assertFalse(data["sdk_calls"][0]["ok"])

    async def test_internal_skill_writes_do_not_archive_write_arguments_or_results(self):
        with patch.dict(sandbox.METHODS, {
            "skills.save": lambda name: {"write_receipt": "private-save"},
            "skills.restore": lambda name: {"write_receipt": "private-restore"},
            "skills.list": lambda: {"items": ["readable"]},
        }):
            data = await self.execute('from mf import skills\nskills.save("private-save")\n'
                                      'skills.restore("private-restore")\nskills.list()')
        self.assertEqual(data["exit_code"], 0, data["stderr"])
        self.assertTrue(all(set(row) == {"method", "ok", "status"} for row in data["sdk_calls"][:2]))
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        files = list((workspace / "output").glob("*.sdk.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["result"], {"items": ["readable"]})
        self.assertNotIn("private-", files[0].read_text())

    async def test_other_session_cannot_read_work_files(self):
        await self.execute('open("private-note", "w").write("first-session")')
        other = await make_context(self.store, session="other")
        result = await self.execute('from pathlib import Path\nprint(Path("private-note").exists())', context=other)
        self.assertEqual(result["stdout"].strip(), "False")

    async def test_files_and_network_do_not_escape_to_host(self):
        secret = self.directory / "host-secret"
        secret.write_text("PRIVATE-CREDENTIAL")
        address = "\0mf-native-" + self.directory.name
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as service:
            service.bind(address)
            service.setblocking(False)
            result = await self.execute('import os, socket\nfrom pathlib import Path\n'
                f'print(Path({str(secret)!r}).exists(), Path("/proc/1/root").exists())\n'
                'for family in (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6, socket.AF_NETLINK, socket.AF_PACKET):\n'
                '    try: socket.socket(family)\n'
                '    except PermissionError: print("socket-blocked", family)\n'
                'a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)\n'
                f'for send in (lambda: a.connect({address!r}), lambda: a.sendto(b"bad", {address!r}), '
                f'lambda: a.sendmsg([b"bad"], [], 0, {address!r})):\n'
                '    try: send()\n'
                '    except PermissionError: print("pair-external-blocked")\n'
                'a.send(b"local"); print(b.recv(16).decode())\n'
                'print(sorted(os.environ))')
            with self.assertRaises(BlockingIOError):
                service.recv(100)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("False False", result["stdout"])
        self.assertEqual(result["stdout"].count("socket-blocked"), 5)
        self.assertEqual(result["stdout"].count("pair-external-blocked"), 3)
        self.assertIn("local", result["stdout"])
        self.assertNotIn("PRIVATE-CREDENTIAL", result["stdout"] + result["stderr"])
        self.assertNotIn("MEDIAFLUX_WEB_PASSWORD", result["stdout"])

    async def test_timeout_and_cancel_reap_background_child(self):
        self.enterContext(patch.dict(sandbox.METHODS, {"library.episodes": lambda query: {"query": query, "cached": False}}))
        code = ('from mf import library\nlibrary.episodes("before-stop")\nimport subprocess, time\n'
                f'p = subprocess.Popen(["python", "-c", "import time; time.sleep(30)", {self.directory.name!r}])\n'
                'print(p.pid, flush=True)\ntime.sleep(30)')
        timed = await self.execute(code, timeout=1)
        self.assertEqual(timed["exit_code"], 124, timed)
        self.assertTrue(timed["timed_out"])
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        self.assertEqual(json.loads((workspace / timed["sdk_calls"][0]["response_path"].lstrip("/")).read_text())["result"]["query"], "before-stop")
        self.assertFalse(self.background_processes())
        task = asyncio.create_task(self.execute(code))
        await asyncio.sleep(0.5)
        self.context.cancellation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        roots = sandbox.get_runtime_paths().runtime_dir / "agent-workspaces"
        self.assertFalse(self.background_processes())
        # poll 自身和异步响应写入的故障都要结束等待并清组，不能等用户命令超时。
        for failure in ("scan", "write"):
            with self.subTest(failure=failure):
                context = await make_context(self.store, session=failure)
                original_scan = os.scandir
                raised = False
                def scan(path):
                    nonlocal raised
                    if isinstance(path, int) and not raised:
                        if any(p.read_text().startswith("poll-") for p in roots.glob("*/output/*.stdout")):
                            raised = True
                            raise OSError("request polling failed")
                    return original_scan(path)
                fault = (patch.object(sandbox.os, "scandir", side_effect=scan) if failure == "scan"
                         else patch.object(sandbox._SDKRequests, "write", side_effect=OSError("response write failed")))
                run = code.replace('print(p.pid, flush=True)', 'print("poll-" + str(p.pid), flush=True)')
                if failure == "write":
                    run = run.rsplit('time.sleep(30)', 1)[0] + 'from mf import library\nlibrary.episodes("fault")\ntime.sleep(30)'
                with fault, patch.dict(sandbox.METHODS, {"library.episodes": lambda query: {"ok": True}}):
                    result = await asyncio.wait_for(sandbox.run_code(self.pipeline,
                        {"code": run, "timeout_seconds": 30}, context), 5)
                    self.assertFalse(result.outcome.public_content["ok"])
                    self.assertIn("failed", result.outcome.public_content["data"]["error"]["message"])
                self.assertFalse(self.background_processes())

    async def test_cancel_during_approval_cleanup_revokes_only_undelivered_plan(self):
        calls = []
        self.catalog.register(KernelToolSpec(
            name="resources.download", domain="resources", effect=ToolEffect.WRITE,
            description="批量下载", input_schema={"type": "object", "properties": {}},
            prepare=lambda *_: PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="test"),
            execute_confirmed=lambda *_: calls.append("download") or {"ok": True},
        ))
        close = sandbox._SDKRequests.close
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                context = await make_context(self.store, session=f"cancel-card-{replacement}")
                plans = []

                async def stop_after_proposal(rpc):
                    await close(rpc)
                    root, _ = sandbox._workspace_location(context.owner, context.session_id)
                    self.assertFalse(list((root / "output").glob("*.sdk.json")))
                    self.assertEqual(rpc.calls, [{"method": "resources.download", "ok": True, "status": "approval_required"}])
                    self.assertIsNotNone(rpc.proposal)
                    plans.append(rpc.proposal.effect_plan)
                    await self.store.cancel_publication(owner=context.owner, session_id=context.session_id,
                                                        request_id=context.request_id)
                    context.cancellation.cancel()
                    if replacement:
                        newer = await make_context(self.store, session=context.session_id)
                        result = await self.pipeline.prepare("resources.download", {}, context=newer)
                        plans.append(result.effect_plan)

                with patch.dict(sandbox.METHODS, {"resources.download": lambda: None}), \
                     patch.object(sandbox._SDKRequests, "close", new=stop_after_proposal):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.execute("from mf import resources\nresources.download()", context=context)
                pending = await self.pipeline.get_pending_plan(owner=context.owner, session_id=context.session_id)
                self.assertEqual(pending.plan_id if pending else None, plans[-1].plan_id if replacement else None)
                original = plans[0]
                self.assertIsNone(self.pipeline.effect_store.get_active_plan(
                    owner=context.owner, session_id=context.session_id,
                    generation=original.generation, plan_id=original.plan_id))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
