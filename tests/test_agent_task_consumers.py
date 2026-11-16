"""Native/SQLite task snapshots consumed independently of turn completion."""
from typing import cast

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app import config
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.metrics import KernelMetrics
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.bot.agent_adapter import _render_turn
from app.bot.telegram_markdown import render_telegram_markdown
from app.routes import agent_api
from tests.support import authenticated_session, isolated_test_database
from tests.test_agent_task_first_slice import GOAL, OWNER, NativeScript
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


@pytest.fixture(scope="module")
def native_snapshots():
    async def run():
        metrics = KernelMetrics()
        results = []
        for component, create in (
            ("tmdb", True),
            ("qbittorrent", False),
            ("qa", False),
        ):
            script = NativeScript(component, create=create)
            if component == "qa":
                script.steps = ["finish"]
            store = SQLiteKernelStore(secret_provider=lambda: "consumer-test-only")
            runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
            events = [
                event
                async for event in metrics.track(
                    runtime.session.run(
                        AgentInput(
                            message=GOAL if create else "继续",
                            owner=OWNER,
                            session_id="task-consumer-session-0001",
                        )
                    ),
                    channel="web",
                )
            ]
            builder = TurnViewBuilder()
            for event in events:
                builder.apply(event)
            # A distinct store is used for the actual cold API boundary.
            cold = build_agent_kernel_runtime(
                model=script.adapter,
                store=SQLiteKernelStore(secret_provider=lambda: "consumer-test-only"),
            )
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "headers": [],
                    "session": authenticated_session(),
                }
            )
            with patch.object(agent_api, "get_agent_kernel_runtime", return_value=cold):
                response = await agent_api.get_session(
                    request, "task-consumer-session-0001"
                )
            assert response.status_code == 200
            api = json.loads(cast(bytearray | bytes | str, response.body))
            assert api["task"] == events[-1].payload["task"]
            results.append(
                {
                    "events": [e.to_dict() for e in events],
                    "api": api,
                    "view": builder.build(),
                    "metrics": metrics.snapshot(),
                }
            )
        return results

    with isolated_test_database("consumer.db"), patch.dict(config._SYSTEM_ENV_SNAPSHOT, {'AGENT_LLM_ENABLED': '1'}):
        result = asyncio.run(run())
    evidence = os.environ.get("CONSUMER_EVIDENCE")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        Path(evidence, "native.json").write_text(
            json.dumps(
                [{**r, "view": r["view"].to_dict()} for r in result],
                ensure_ascii=False,
                indent=2,
            )
        )
    return result


@pytest.mark.parametrize("task_mode", ["tracked", "untracked", "unknown"])
def test_telegram_approval_callers_preserve_buttons_and_html(native_snapshots, task_mode):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.agent.kernel.adapters import ApprovalView
    from app.bot import agent_adapter as adapter
    from app.bot.telegram_markdown import telegram_html_text_length
    from tests.test_agent_kernel_telegram_adapter import (
        TELEBOT,
        Call,
        FakeBot,
        FakeStore,
        FakeTelegramTransport,
        Message,
    )

    approval = ApprovalView(
        plan_id="plan_1234567890abcdef",
        tool_name="rss.create_subscription",
        effect="WRITE",
        preview={"summary": "创建 RSS 订阅 <测试>", "data": {"target": "qb"}},
        result={},
        expires_at="2099-01-01T00:00:00Z",
        confirmation={"action": "创建订阅"},
    )
    base = native_snapshots[0]["view"]
    task = {"tracked": base.task, "untracked": None, "unknown": {}}[task_mode]
    for confirm in (False, True):
        view = replace(
            base,
            status="approval_required",
            approval=approval,
            task=task,
            effect_result={"ok": True, "summary": "前项已执行"} if confirm else {},
            error_code="confirmation_stale" if confirm else "",
        )
        transport = FakeTelegramTransport(view, confirm_view=view)
        runtime = SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        with (
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
            patch.object(adapter.agent_rate_limiter, "allow", return_value=True),
            patch.object(adapter, "is_agent_enabled", return_value=True),
            patch.object(
                adapter.config,
                "get",
                side_effect=lambda key, default="": {
                    "TG_AGENT_ALLOWED_USER_IDS": "7",
                    "TG_CHAT_ID": "-100",
                    "TG_AGENT_ENABLED": "1",
                }.get(key, default),
            ),
            patch(
                "app.modules.telegram_model_preferences.get_telegram_model_preference",
                return_value="",
            ),
        ):
            if confirm:
                adapter.handle_agent_callback(
                    bot,
                    Call("agk:c:plan_1234567890abcdef", Message("preview", user_id=0)),
                    TELEBOT,
                )
            else:
                adapter._execute_query(
                    bot, TELEBOT, Message(), chat_id="-100", user_id="7", text="检查"
                )
        bodies = [e[0] for e in bot.edits if e[0]] + [e[1] for e in bot.sent]
        body = "\n".join(bodies)
        if task_mode == "tracked":
            assert "已声明检查项尚未全部核验" in body
            assert "观察配置 qbittorrent" in body
        elif task_mode == "unknown":
            assert "会话声明任务快照：任务验收状态未知" in body
        else:
            assert "会话声明任务快照" not in body
            assert "未建立任务验收清单" not in body
        assert "<b>" in body and "&lt;测试&gt;" in body
        assert "&lt;b&gt;" not in body
        if confirm:
            assert "前项已执行" in body and "请再次明确确认" in body
        markups = [e[3].get("reply_markup") for e in bot.edits] + [
            e[2].get("reply_markup") for e in bot.sent
        ]
        assert any(
            m
            and [b.callback_data for b in m.buttons]
            == ["agk:c:plan_1234567890abcdef", "agk:x:plan_1234567890abcdef", "agk:e:plan_1234567890abcdef"]
            for m in markups
        )
        assert all(telegram_html_text_length(b) <= 3900 for b in bodies)


def test_native_untracked_qa_keeps_turn_success(agent_llm_enabled):
    from contextlib import contextmanager

    from app import database as db

    async def run():
        script = NativeScript("tmdb")
        script.steps = ["finish"]
        store = SQLiteKernelStore(secret_provider=lambda: "consumer-test-only")
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
        metrics = KernelMetrics()
        trace = []
        original = db.get_conn

        @contextmanager
        def traced():
            with original() as connection:
                connection.set_trace_callback(trace.append)
                yield connection

        with patch.object(db, "get_conn", traced):
            events = [
                e
                async for e in metrics.track(
                    runtime.session.run(
                        AgentInput(
                            message="你好",
                            owner=OWNER,
                            session_id="consumer-qa-session-0001",
                        )
                    ),
                    channel="web",
                )
            ]
        state = await store.load(owner=OWNER, session_id="consumer-qa-session-0001")
        assert "task" not in state.metadata
        assert (
            events[-1].payload["status"] == "success"
            and events[-1].payload.get("task") is None
        )
        assert len(script.requests) == 1
        evidence = os.environ.get("CONSUMER_EVIDENCE")
        if evidence:
            Path(evidence, "qa-transactions.json").write_text(
                json.dumps(
                    [s for s in trace if s.startswith(("BEGIN", "COMMIT", "ROLLBACK"))],
                    indent=2,
                )
            )
            Path(evidence, "qa-events.json").write_text(
                json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2)
            )
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        assert metrics.snapshot()["recent"][-1].get("task_status") == "untracked"
        view = builder.build()
        rendered = _render_turn(view)
        assert rendered == render_telegram_markdown(view.answer)
        assert "会话声明任务快照" not in rendered
        assert "未建立任务验收清单" not in rendered

    with isolated_test_database("consumer-qa.db"):
        asyncio.run(run())


def test_native_metrics_consumer(native_snapshots):
    for item, status in zip(native_snapshots, ("partial", "complete")):
        terminal = item["events"][-1]["payload"]
        assert terminal["status"] == "success"
        assert terminal["task"]["status"] == status
        recent = item["metrics"]["recent"][-1]
        assert recent.get("task_status") == status
        assert recent["turn_status"] == recent["status"] == "success"
        assert item["metrics"]["task_statuses_by_turn"][status] == 1


def test_native_telegram_consumer(native_snapshots):
    for item, label in zip(
        native_snapshots, ("已声明检查项尚未全部核验", "已声明检查项已核验")
    ):
        rendered = _render_turn(item["view"])
        assert "全部任务完成" in rendered
        assert "会话声明任务快照" in rendered
        assert label in rendered
    assert "观察配置 qbittorrent" in _render_turn(native_snapshots[0]["view"])
