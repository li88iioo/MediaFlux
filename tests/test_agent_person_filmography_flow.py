from __future__ import annotations

import unittest

from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import AgentInput, InMemorySessionStateStore
from tests.test_agent_kernel_core import ScriptedModel, collect


class PersonFilmographyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_filmography_library_audit_uses_two_tool_calls(self) -> None:
        films = [
            {
                "tmdb_id": "77",
                "media_type": "movie",
                "title": "记忆碎片",
                "year": "2000",
            },
            {
                "tmdb_id": "157336",
                "media_type": "movie",
                "title": "星际穿越",
                "year": "2014",
            },
        ]
        calls: list[str] = []

        def filmography(arguments, _context):
            calls.append("discovery.person_filmography")
            self.assertEqual(arguments["person"], "诺兰")
            return {
                "ok": True,
                "status": "success",
                "summary": "已取得诺兰导演作品表",
                "data": {"items": films},
            }

        def batch_presence(arguments, _context):
            calls.append("library.batch_presence")
            self.assertEqual(arguments["items"], films)
            return {
                "ok": True,
                "status": "success",
                "summary": "已批量核对 2 部媒体",
                "data": {
                    "items": [
                        {**films[0], "library_status": "missing"},
                        {**films[1], "library_status": "present"},
                    ]
                },
            }

        catalog = ToolCatalog(
            [
                KernelToolSpec(
                    name="discovery.person_filmography",
                    domain="discovery",
                    description="读取导演全部电影并按上映日期排序",
                    examples=("列出诺兰导演的所有电影",),
                    input_schema={"type": "object"},
                    effect=ToolEffect.READ,
                    read=filmography,
                ),
                KernelToolSpec(
                    name="library.batch_presence",
                    domain="library",
                    description="一次批量核对片单中哪些已入库、哪些缺失",
                    examples=("标出片单里我库中缺哪几部",),
                    input_schema={"type": "object"},
                    effect=ToolEffect.READ,
                    read=batch_presence,
                ),
            ]
        )
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall(
                            "filmography-1",
                            "discovery.person_filmography",
                            {"person": "诺兰", "role": "directing"},
                        ),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall(
                            "presence-1",
                            "library.batch_presence",
                            {"items": films},
                        ),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TEXT_DELTA,
                        text="诺兰电影已按年份列出；《记忆碎片》缺失，《星际穿越》在库。",
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        state = InMemorySessionStateStore()
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="把诺兰导演的所有电影按上映年份排出来，标出我库里缺哪几部",
                    owner="owner",
                    session_id="session",
                )
            )
        )

        self.assertEqual(calls, ["discovery.person_filmography", "library.batch_presence"])
        self.assertNotIn(AgentEventType.TURN_FAILED, [event.type for event in events])
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["tool_calls"], 2)
        self.assertEqual(events[-1].payload["model_calls"], 3)
        self.assertIn("记忆碎片", events[-1].payload["answer"])
