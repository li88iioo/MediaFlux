"""原生SDK帮助可达性、可信任务来源、证据与普通问答边界；脚本不代表真实LLM采纳率。"""
import asyncio
import json
import re
from contextlib import contextmanager
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from starlette.requests import Request

from app import database as db
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.task_state import task_projection
from app.routes import agent_api
from tests.agent_task_native_helpers import returned_task, task_call
from tests.test_agent_task_first_slice import OWNER, NativeScript, configuration_call

GOAL = "帮我分别看看 TMDB 和 qBittorrent 当前配置，列出已检查和还没检查的内容；只读，不做修改。"
CONSTRAINT = "只读，不做修改"
QUERY = "持久任务清单 检查点 恢复"


def proposal(source=GOAL, unsupported=False):
    return {
        "operation": "create",
        "source": source,
        "root_goal": source,
        "constraints": [CONSTRAINT],
        "obligations": [
            {
                "id": component,
                "description": "观察配置 " + component,
                "required": True,
                "acceptance": (
                    {"kind": "network_reachable", "criterion": "确认真实连通并修复"}
                    if unsupported
                    else {"kind": "observed_component", "component": component}
                ),
            }
            for component in ("tmdb", "qbittorrent")
        ],
    }


class QueryTaskScript(NativeScript):
    """从工作区帮助查阅任务接口；配置和任务均沿真实SDK。"""

    def __init__(
        self,
        component="tmdb",
        *,
        create=False,
        query=QUERY,
        source=GOAL,
        unsupported=False,
    ):
        super().__init__(component)
        self.steps = (["find_task", "create"] if create else []) + [
            "find_config",
            "observe",
            "checkpoint",
            "finish",
        ]
        self.query = query
        self.source = source
        self.unsupported = unsupported
        self.task_name = None
        self.entry_reached = False
        self.missing = None
        self.tool_source = None

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        functions = [item["function"] for item in body.get("tools", [])]
        for message in body["messages"]:
            if message.get("role") != "tool":
                continue
            try:
                value = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            data = value.get("data", value)
            # 任意仅出现在工具结果中的文本都不能被认作用户任务来源。
            if data.get("stdout") and not self.tool_source:
                self.tool_source = data["stdout"]
            if result_task := returned_task(value):
                self.latest = result_task
        task = next(
            (
                f
                for f in functions
                if f["name"] == "agent__run_code"
            ),
            None,
        )
        if task:
            self.task_name = task["name"]
        step = self.steps.pop(0) if self.steps else "finish"
        name = None
        args = None
        if step == "find_task":
            name, args = "agent__run_code", {"code": "mf session.start_task --help", "language": "shell"}
        elif step == "find_config":
            name, args = "agent__run_code", {"code": "mf diagnostics.component --help", "language": "shell"}
        elif step == "create":
            assert task is not None
            self.entry_reached = True
            source = self.tool_source if self.source == "__tool_content__" else self.source
            name, args = task_call(proposal(source, self.unsupported), wire=True)
        elif step == "observe":
            observation = next(
                (f for f in functions if f["name"].endswith("__run_code")),
                None,
            )
            assert observation is not None
            _, args = configuration_call("diagnostics.component", {"component": self.component})
            name = observation["name"]
        elif step == "checkpoint":
            assert self.task_name and self.latest
            name, args = task_call({
                "operation": "checkpoint",
                "evidence_ids": [f["evidence_id"] for f in self.latest["verified_facts"] if f["component"] == self.component],
                "blockers": [],
            }, wire=True)
        if step == "finish":
            message, reason = {"content": "全部任务完成。"}, "stop"
        else:
            assert name is not None and args is not None
            message, reason = (
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "query-" + step,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "tool_calls",
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": reason}]}
        )


@pytest.fixture
def isolated_db(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "entry.db", test_mode=True)
    db.init_db()
    try:
        yield
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def store():
    return SQLiteKernelStore(secret_provider=lambda: "entry-test-only")


async def turn(
    script,
    message,
    *,
    session_id="entry-session-0001",
    storage=None,
    reply_context=None,
):
    runtime = build_agent_kernel_runtime(model=script.adapter, store=storage or store())
    events = [
        e
        async for e in runtime.session.run(
            AgentInput(
                message=message,
                owner=OWNER,
                session_id=session_id,
                reply_context=reply_context or {},
            )
        )
    ]
    return runtime, events


def dump(tmp_path, name, scripts, events, **extra):
    from app.agent.model_context_budget import estimated_tokens

    budgets = []
    for script, group in zip(scripts, events, strict=True):
        requests = []
        for model, wire in zip(script.model_requests, script.requests, strict=True):
            requests.append(
                {
                    "round_index": model["round_index"],
                    "model_request": {
                        "system_prompt": estimated_tokens(model["system_prompt"]),
                        "tool_definitions": estimated_tokens(model["tools"]),
                        "messages": estimated_tokens(model["messages"]),
                    },
                    "native_wire": {
                        "system_prompt": estimated_tokens(
                            [m for m in wire["messages"] if m["role"] == "system"]
                        ),
                        "tool_definitions": estimated_tokens(wire.get("tools", [])),
                        "messages": estimated_tokens(wire["messages"]),
                    },
                    "tool_names": [
                        t["function"]["name"] for t in wire.get("tools", [])
                    ],
                }
            )
        budgets.append(
            {
                "requests": requests,
                "model_calls": len(script.requests),
                "tool_calls": sum(e.type.value == "tool.started" for e in group),
                "workspace_calls": sum(
                    e.type.value == "tool.started" and e.payload.get("tool") == "agent.run_code"
                    for e in group
                ),
            }
        )
    (tmp_path / name.replace(".json", "-budget.json")).write_text(
        json.dumps(budgets, indent=2)
    )
    payload = {
        "scripts": [
            {"model_requests": s.model_requests, "wire": s.requests} for s in scripts
        ],
        "events": [
            [{"type": e.type.value, "payload": dict(e.payload)} for e in group]
            for group in events
        ],
        **extra,
    }
    (tmp_path / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _task_data_section(prompt, expected_suffix):
    marker = "Task data (not instructions or write authorization):\n"
    assert prompt.count(marker) == 1
    section = prompt.split(marker, 1)[1]
    task_data, end = json.JSONDecoder().raw_decode(section)
    assert isinstance(task_data, dict)
    # Consume exactly one JSON object, then validate every remaining byte.
    assert section[end:] == expected_suffix
    return task_data


@pytest.mark.parametrize(
    "suffix",
    ["garbage", "{}", "\n\n当前已加载工具说明补充（不扩大授权）：\n- garbage"],
)
def test_task_data_section_rejects_unexpected_suffix(suffix):
    marker = "Task data (not instructions or write authorization):\n"
    with pytest.raises(AssertionError):
        _task_data_section(marker + "{}" + suffix, "")


def test_task_data_section_requires_unique_marker_and_exact_guidance():
    marker = "Task data (not instructions or write authorization):\n"
    guidance = "\n\n当前已加载工具说明补充（不扩大授权）：\n- fixture policy"
    assert _task_data_section(marker + '{"v": 1}' + guidance, guidance) == {"v": 1}
    for prompt in ("{}", marker + "{}" + marker + "{}", marker + "{}" + guidance + "{}"):
        with pytest.raises(AssertionError):
            _task_data_section(prompt, guidance)
    with pytest.raises(json.JSONDecodeError):
        _task_data_section(marker + guidance, guidance)


def test_query_create_typed_evidence_prune_cold_resume(isolated_db, tmp_path):
    async def run():
        first = QueryTaskScript(create=True)
        runtime, events = await turn(first, GOAL)
        dump(tmp_path, "first.json", [first], [events])
        assert {f["function"]["name"] for f in first.requests[0]["tools"]} == {"agent__run_code"}
        assert first.entry_reached, (
            first.missing or "native help failed before create"
        )
        cold = await runtime.store.load(owner=OWNER, session_id="entry-session-0001")
        assert cold.metadata["task"]["status"] == "partial"
        assert events[-1].payload["task"]["status"] == "partial"
        assert [o["state"] for o in cold.metadata["task"]["obligations"]] == [
            "verified",
            "pending",
        ]
        next_order = max(row.get("history_order", -1) for row in cold.conversation) + 1
        retained = retain_conversation(
            cold.conversation + [{"role": "user", "content": "保留的新回合", "history_id": "retained-user",
                                  "history_order": next_order}],
            maximum_bytes=100,
        )
        assert GOAL not in json.dumps(retained, ensure_ascii=False)
        assert all(m["role"] != "tool" and not m.get("tool_calls") for m in retained)
        lease, _ = await runtime.store.begin_turn(
            owner=OWNER, session_id="entry-session-0001", request_id="prune"
        )
        await runtime.store.commit(lease, conversation=retained)
        reopened = store()
        assert (
            await reopened.load(owner=OWNER, session_id="entry-session-0001")
        ).conversation == retained
        second = QueryTaskScript("qbittorrent")
        runtime2, events2 = await turn(second, "继续", storage=reopened)
        for projection in (second.model_requests[0], second.requests[0]):
            text = json.dumps(projection, ensure_ascii=False)
            assert GOAL in text and CONSTRAINT in text
            assert "verified_facts" in text and "tmdb" in text and "qbittorrent" in text
            assert "remaining" in text
        for prompt in (
            second.model_requests[0]["system_prompt"],
            second.requests[0]["messages"][0]["content"],
        ):
            task_data = _task_data_section(prompt, "")
            assert task_data == task_projection(cold.metadata["task"])
            assert task_data["root_goal"] == GOAL
            assert task_data["constraints"] == [CONSTRAINT]
            assert task_data["remaining_obligation_ids"] == ["qbittorrent"]
            assert [f["component"] for f in task_data["verified_facts"]] == ["tmdb"]
            assert task_data["obligations"][0]["state"] == "verified"
        observations = [
            e.payload
            for e in events2
            if e.type.value == "tool.started"
            and e.payload.get("tool") == "agent.run_code"
        ]
        assert len(observations) == 3
        calls = [
            call
            for wire in second.requests
            for message in wire["messages"]
            for call in message.get("tool_calls", [])
            if call["function"]["name"].endswith("__run_code")
        ]
        assert any(json.loads(c["function"]["arguments"]) == configuration_call("diagnostics.component", {"component": "qbittorrent"})[1] for c in calls)
        assert all(json.loads(c["function"]["arguments"]) == {"code": "mf diagnostics.component --help", "language": "shell"} or "diagnostics.component" in json.loads(c["function"]["arguments"])["code"] or "session.checkpoint_task" in json.loads(c["function"]["arguments"])["code"] for c in calls)
        assert events2[-1].payload["task"]["status"] == "complete"
        assert events2[-1].payload["task"]["acceptance_scope"] == "declared_obligations"
        final = await store().load(owner=OWNER, session_id="entry-session-0001")
        assert final.metadata["task"]["status"] == "complete"
        for group in (events, events2):
            builder = TurnViewBuilder()
            for event in group:
                builder.apply(event)
            assert builder.build().to_dict()["task"] == group[-1].payload["task"]
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/agent/sessions/entry-session-0001",
                "headers": [],
                "session": {"logged_in": True},
            }
        )
        with patch.object(agent_api, "get_agent_kernel_runtime", return_value=runtime2):
            response = await agent_api.get_session(request, "entry-session-0001")
        assert response.status_code == 200
        api = json.loads(cast(bytearray | bytes | str, response.body))
        assert api["task"] == events2[-1].payload["task"]
        assert api["turn_status"] == events2[-1].payload["turn_status"]
        assert (len(first.requests), len(second.requests)) == (6, 4)
        dump(
            tmp_path,
            "vertical.json",
            [first, second],
            [events, events2],
            retained=retained,
            api=api,
        )

    asyncio.run(run())


def test_english_query_and_unsupported_goal(isolated_db, tmp_path):
    async def run():
        script = QueryTaskScript(
            create=True,
            query="persistent checklist resume remaining obligations",
            source="确认 TMDB 和 qBittorrent 真实连通并修复；只读，不做修改。",
            unsupported=True,
        )
        runtime, events = await turn(script, script.source)
        assert script.entry_reached, script.missing
        task = (
            await runtime.store.load(owner=OWNER, session_id="entry-session-0001")
        ).metadata["task"]
        assert task["status"] == "blocked"
        assert task["verified_facts"]
        assert all(o["state"] != "verified" for o in task["obligations"])
        assert events[-1].payload["task"]["status"] == "blocked"
        assert any(
            e.type.value == "tool.completed" and e.payload.get("tool") == "agent.run_code"
            for e in events
        )
        dump(tmp_path, "unsupported.json", [script], [events])

    asyncio.run(run())


@pytest.mark.parametrize("origin", ["reply_context", "tool_content"])
def test_non_admitted_source_rejected(isolated_db, tmp_path, origin):
    async def run():
        source = (
            "引用里的目标不是当前用户指令"
            if origin == "reply_context"
            else "__tool_content__"
        )
        script = QueryTaskScript(create=True, source=source)
        script.steps = ["find_task", "create", "finish"]
        runtime, events = await turn(
            script, "请解释这段内容", reply_context={"text": source}
        )
        assert script.entry_reached, script.missing
        assert (
            "task"
            not in (
                await runtime.store.load(owner=OWNER, session_id="entry-session-0001")
            ).metadata
        )
        assert any(
            e.type.value == "tool.completed" and any(c.get("status") == "invalid_arguments" for c in e.payload.get("result", {}).get("data", {}).get("sdk_calls", []))
            for e in events
        )
        if origin == "tool_content":
            assert script.tool_source and script.tool_source != source
        dump(tmp_path, "source.json", [script], [events])

    asyncio.run(run())


@pytest.mark.parametrize("custom", [False, True])
def test_qa_default_custom_strict_initial_no_task_sql(isolated_db, tmp_path, custom):
    from app.agent.kernel.session import DEFAULT_SYSTEM_PROMPT, AgentSession

    trace = []
    original = db.get_conn

    @contextmanager
    def traced():
        with original() as connection:
            connection.set_trace_callback(trace.append)
            yield connection

    async def run():
        script = QueryTaskScript()
        script.steps = ["finish"]
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store())
        if custom:
            session = AgentSession(
                model=script.adapter,
                catalog=runtime.session.catalog,
                model_tools=runtime.session.model_tools,
                pipeline=runtime.session.pipeline,
                state_store=runtime.store,
                journal=runtime.store,
                system_prompt="  Custom caller prompt.  ",
            )
            assert session.system_prompt == "Custom caller prompt."
        else:
            session = runtime.session
            assert session.system_prompt == DEFAULT_SYSTEM_PROMPT.strip()
        with patch.object(db, "get_conn", traced):
            events = [
                e
                async for e in session.run(
                    AgentInput(
                        message="你好", owner=OWNER, session_id="qa-entry-session-01"
                    )
                )
            ]
        assert len(script.requests) == 1
        assert not any(e.type.value == "tool.started" for e in events)
        assert events[-1].payload["task"] is None
        assert {f["function"]["name"] for f in script.requests[0]["tools"]} == {"agent__run_code"}
        # 读栅栏可增加空事务；约束真实持久化边界和无业务/任务写入。
        assert sum(s.startswith("BEGIN") for s in trace) == sum(s.startswith("COMMIT") for s in trace) > 0
        assert not any(s.startswith("ROLLBACK") for s in trace)
        written = [match.group(1) for statement in trace if (match := re.match(
            r"(?:INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM) ([a-z_]+)", statement))]
        assert written and all(table.startswith("agent_kernel_") for table in written)
        saved = await runtime.store.load(owner=OWNER, session_id="qa-entry-session-01")
        assert "task" not in saved.metadata
        if custom:
            assert QUERY not in script.model_requests[0]["system_prompt"]
        dump(
            tmp_path,
            "qa.json",
            [script],
            [events],
            transactions=[
                s for s in trace if s.startswith(("BEGIN", "COMMIT", "ROLLBACK"))
            ],
        )

    asyncio.run(run())


def test_admitted_multiline_file_goal_is_task_data_not_a_public_summary_filter():
    from types import SimpleNamespace

    from app.agent.kernel.task_state import proposal as make_patch
    from app.agent.kernel.task_state import reduce_task

    source = '为 /临时验收/test.txt 准备一天分享。\n先建立任务清单，未经确认不得分享。'
    args = {
        'operation': 'create', 'source': source,
        'root_goal': '为 /临时验收/test.txt 准备分享确认卡',
        'constraints': ['只操作 /临时验收/test.txt', '未经用户确认不分享'],
        'obligations': [{'id': 'prepare', 'description': '核对 /临时验收/test.txt 并生成确认卡',
                         'required': True, 'acceptance': {
                             'kind': 'plan_prepared', 'criterion': '/临时验收/test.txt 的预览等待人工确认'}}],
    }
    context = SimpleNamespace(request_id='task-file-goal', user_source=source, current_task=None)
    task = reduce_task(None, make_patch(args, context), owner=OWNER, session_id='file-goal')
    assert task['source_quote'] == source
    assert task['root_goal'] == args['root_goal']
    assert task['status'] == 'blocked', '允许登记不等于伪造通用验收完成'
    assert task['verified_facts'] == []
    public = task_projection(task)
    assert public is not None
    assert public['root_goal'] == args['root_goal']
    assert public['constraints'] == args['constraints']
    with pytest.raises(ValueError, match='source must be'):
        make_patch(args, SimpleNamespace(request_id='other-source', user_source='另外一个用户目标'))


@pytest.mark.parametrize('unsafe', ['api_key=sk_live_ABCDEF1234567890Secret', 'token=actualPrivateCredential987654321', '目标\x00秘密', '目标\x1b[31m秘密', '目标\x7f秘密'])
def test_task_text_still_rejects_credentials_and_terminal_controls(unsafe):
    from app.agent.kernel.task_state import validate_proposal
    args = proposal()
    for field in ('source', 'root_goal'):
        changed = {**args, field: unsafe}
        with pytest.raises(ValueError):
            validate_proposal(changed)


@pytest.mark.parametrize('operation', ['create', 'revise', 'checkpoint', 'cancel'])
def test_task_sdk_signatures_and_domain_fields_match(operation):
    import inspect

    from app import sdk
    from app.agent.kernel.session_service import SessionService
    from app.agent.kernel.task_state import validate_proposal
    method = {"create": "start_task", "revise": "revise_task", "checkpoint": "checkpoint_task", "cancel": "cancel_task"}[operation]
    if operation in {"create", "revise"}:
        args = {**proposal(), "operation": operation}
    elif operation == "checkpoint":
        args = {"operation": operation, "evidence_ids": ["known-proof"], "blockers": []}
    else:
        args = {"operation": operation, "source": GOAL}
    assert validate_proposal(args) == args
    values = {k: v for k,v in args.items() if k != "operation"}
    native = inspect.signature(sdk.METHODS["session." + method])
    service = inspect.signature(getattr(SessionService, method))
    assert list(service.parameters)[1:] == list(native.parameters)
    native.bind(**values)
    for missing in values:
        with pytest.raises(TypeError):
            native.bind(**{k:v for k,v in values.items() if k != missing})
    for retired in (None, {"task_id": "existing-task", "epoch": 1, "revision": 35}):
        with pytest.raises(TypeError):
            native.bind(**values, expected=retired)
        with pytest.raises(ValueError):
            validate_proposal({**args, "expected": retired})


@pytest.mark.parametrize('acceptance', [
    {'kind': 'observed_component'},
    {'kind': 'observed_component', 'component': 'unknown'},
    {'kind': 'observed_component', 'component': 'tmdb', 'criterion': 'extra'},
    {'kind': 'network_reachable'},
    {'kind': 'network_reachable', 'criterion': ''},
])
def test_task_domain_rejects_invalid_acceptance_shape(acceptance):
    from app.agent.kernel.task_state import validate_proposal
    args = proposal()
    args['obligations'][0]['acceptance'] = acceptance
    with pytest.raises(ValueError):
        validate_proposal(args)


def test_explicit_checklist_keeps_native_entry_visible_without_auto_creation(isolated_db):
    async def run():
        script = QueryTaskScript()
        script.steps = ['finish']
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store())
        events = [e async for e in runtime.session.run(AgentInput(
            message='请建立一个清单并核对TMDB、qBittorrent两个组件当前配置是否完整；只观察配置，不联网、不修改，检查完成后更新清单。',
            owner=OWNER, session_id='first-checklist'))]
        names = {item['function']['name'] for item in script.requests[0]['tools']}
        assert names == {'agent__run_code'}
        assert not any(e.type.value == 'tool.started' for e in events)
        assert events[-1].payload['task'] is None  # 可见不等于自动建立、授权或执行任务。
    asyncio.run(run())
