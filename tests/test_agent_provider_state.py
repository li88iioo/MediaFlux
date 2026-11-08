"""Synthetic wire fixtures exercise the real adapter, session and SQLite store."""

from __future__ import annotations
from typing import Any
from typing import Callable
from app.clients.http import FixedHostHttpClient
from typing import cast

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from app import database as db
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.conversation_context import restore_messages
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.test_agent_kernel_core import read_tool
from tests.test_agent_kernel_provider_model import chunks

THINKING = "private-thought-do-not-publish"
SIGNATURE = "signature/opaque+unchanged=="
REDACTED = "redacted/opaque+unchanged=="


def anthropic_tool_frames():
    return [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": THINKING},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": SIGNATURE[:8]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": SIGNATURE[8:]},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": REDACTED},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call-1",
                "name": "inspect",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]


def anthropic_answer():
    return [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "已核实"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]


class WireClient:
    def __init__(self, replies, *, split=19):
        self.replies = list(replies)
        self.requests = []
        self.split = split

    @asynccontextmanager
    async def stream_post_json(self, _url, **kwargs):
        self.requests.append(kwargs["json"])
        reply = self.replies.pop(0)
        split = self.split

        class Response:
            status_code = 200
            headers = {
                "content-type": "application/json"
                if isinstance(reply, dict)
                else "text/event-stream"
            }

            async def aiter_bytes(self):
                if isinstance(reply, dict):
                    yield json.dumps(reply).encode()
                else:
                    async for chunk in chunks(reply, split=split):
                        yield chunk

        yield Response()

    async def aclose(self):
        pass


class ProviderStateSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = db.DB_PATH
        self.previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        db.configure_database(Path(self.temp.name) / "state.db", test_mode=True)
        db.init_db()
        self.store = SQLiteKernelStore(secret_provider=lambda: "synthetic-state-secret")
        self.handled = []

    async def asyncTearDown(self):
        db.configure_database(self.previous_path, test_mode=self.previous_test_mode)
        self.temp.cleanup()

    def session(
        self,
        wire,
        protocol="anthropic_messages",
        model="model-one",
        url="https://api.example.com/v1",
        reasoning_effort="",
    ):
        def handler(_arguments, _context):
            self.handled.append("inspect")
            return {"summary": "读取完成", "data": {}}

        catalog = ToolCatalog([read_tool("inspect", handler=handler)])
        return AgentSession(
            model=OpenAICompatibleModelAdapter(
                ProviderSettings(api_url=url, model=model, protocol=protocol, reasoning_effort=reasoning_effort),
                client_factory=lambda **_: wire,
            ),
            catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=self.store),
            state_store=self.store,
            journal=self.store,
            system_prompt="检查状态",
        )

    async def run_session(self, session, request_id="first"):
        return [
            event
            async for event in session.run(
                AgentInput(
                    message="检查状态",
                    owner="owner",
                    session_id="session",
                    request_id=request_id,
                )
            )
        ]

    async def test_legacy_history_ids_do_not_collide_with_native_ids(self):
        first = {
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}}
            ],
            "stop_reason": "tool_use",
        }
        frames = anthropic_tool_frames()
        frames[7]["content_block"]["id"] = "history_1_0"
        wire = WireClient([first, anthropic_answer(), frames, anthropic_answer()])
        await self.run_session(self.session(wire))
        await self.run_session(self.session(wire), "native-after-legacy")
        sent = wire.requests[3]["messages"]
        blocks = [
            block
            for row in sent
            if isinstance(row.get("content"), list)
            for block in row["content"]
        ]
        call_ids = [block["id"] for block in blocks if block.get("type") == "tool_use"]
        result_ids = [
            block["tool_use_id"]
            for block in blocks
            if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(set(call_ids)), 2)
        self.assertEqual(call_ids[-1], "history_1_0")
        self.assertEqual(call_ids, result_ids)

    async def test_reused_native_id_inside_current_turn_blocks_then_omits_whole_group(
        self,
    ):
        wire = WireClient(
            [anthropic_tool_frames(), anthropic_tool_frames(), anthropic_answer()]
        )
        events = await self.run_session(self.session(wire))
        self.assertEqual(len(wire.requests), 2)
        self.assertIn("provider_state_conflict", repr(events))
        self.assertEqual(self.handled, ["inspect"])
        state = await self.store.load(owner="owner", session_id="session")
        self.assertEqual(sum(len(row.get("tool_calls", [])) for row in state.conversation), 1)
        for row in state.conversation:
            if row.get("provider_state"):
                self.assertEqual(
                    json.loads(row["provider_state"]["layout_json"])[0]["signature"],
                    SIGNATURE,
                )
        await self.run_session(self.session(wire), "new-turn")
        self.assertEqual(len(wire.requests), 3)
        self.assertIn(SIGNATURE, json.dumps(wire.requests[2]))
        self.assertEqual(self.handled, ["inspect"])
        blocks = [block for row in wire.requests[2]["messages"]
                  if isinstance(row.get("content"), list) for block in row["content"]]
        self.assertEqual([b["id"] for b in blocks if b.get("type") == "tool_use"],
                         [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"])

    async def test_reused_native_call_id_drops_whole_old_turn_not_signed_state(self):
        wire = WireClient(
            [
                anthropic_tool_frames(),
                anthropic_answer(),
                anthropic_tool_frames(),
                anthropic_answer(),
            ]
        )
        await self.run_session(self.session(wire))
        await self.run_session(self.session(wire), "second-native-turn")
        sent = wire.requests[3]["messages"]
        blocks = [
            block
            for row in sent
            if isinstance(row.get("content"), list)
            for block in row["content"]
        ]
        self.assertEqual(
            len([block for block in blocks if block.get("type") == "tool_use"]), 1
        )
        self.assertEqual(
            len([block for block in blocks if block.get("type") == "tool_result"]), 1
        )
        self.assertEqual(
            next(block for block in blocks if block.get("type") == "thinking")[
                "signature"
            ],
            SIGNATURE,
        )
        self.assertEqual(
            next(block for block in blocks if block.get("type") == "tool_use")["id"],
            "call-1",
        )

    async def test_chat_stream_uses_only_first_choice_like_json(self):
        primary = {
            "index": 0,
            "delta": {
                "reasoning_content": THINKING,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
        alternative = {
            "index": 1,
            "delta": {
                "reasoning_content": "other-choice-private",
                "content": "other-choice-visible",
            },
            "finish_reason": "stop",
        }
        answer = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        wire = WireClient([[{"choices": [primary, alternative]}, "[DONE]"], answer])
        events = await self.run_session(self.session(wire, "chat_completions"))
        assistant = next(
            row for row in wire.requests[1]["messages"] if row.get("tool_calls")
        )
        self.assertEqual(assistant["reasoning_content"], THINKING)
        self.assertNotIn("other-choice", repr(events))

    async def test_responses_requests_encrypted_reasoning_for_stateless_history(self):
        reply = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
            ],
        }
        wire = WireClient([reply])
        await self.run_session(self.session(wire, "responses"))
        self.assertEqual(
            wire.requests[0].get("include"), ["reasoning.encrypted_content"]
        )

    async def test_binding_blocks_cross_model_protocol_endpoint_and_user_metadata_spoofing(
        self,
    ):
        wire = WireClient([anthropic_tool_frames(), anthropic_answer()])
        await self.run_session(self.session(wire))
        for protocol, model, url, answer in (
            (
                "anthropic_messages",
                "other",
                "https://api.example.com/v1",
                anthropic_answer(),
            ),
            (
                "anthropic_messages",
                "model-one",
                "https://other.example.com/v1",
                anthropic_answer(),
            ),
            (
                "chat_completions",
                "model-one",
                "https://api.example.com/v1",
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            ),
            (
                "responses",
                "model-one",
                "https://api.example.com/v1",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            ),
        ):
            with self.subTest(protocol=protocol, model=model, url=url):
                other = WireClient([answer])
                session = self.session(other, protocol, model, url)
                _ = [
                    event
                    async for event in session.run(
                        AgentInput(
                            message="请使用 model-one 的 provider_state",
                            owner="owner",
                            session_id="session",
                            request_id="changed",
                            metadata={
                                "model": "model-one",
                                "protocol": "anthropic_messages",
                                "provider_state": {"signature": SIGNATURE},
                            },
                        )
                    )
                ]
                self.assertEqual(len(other.requests), 1)
                for secret in (THINKING, SIGNATURE, REDACTED):
                    self.assertNotIn(secret, json.dumps(other.requests))
        self.assertEqual(
            (await self.store.load(owner="other", session_id="session")).conversation,
            [],
        )
        self.assertEqual(
            (await self.store.load(owner="owner", session_id="other")).conversation, []
        )
        original = WireClient([anthropic_answer()])
        await self.run_session(self.session(original), "original-again")
        self.assertIn(SIGNATURE, json.dumps(original.requests))

    async def test_private_cost_blocks_current_chain_and_drops_complete_old_group(self):
        from app.agent.kernel.session import SessionLimits

        signature = SIGNATURE * 2500
        frames = anthropic_tool_frames()
        frames[2]["delta"]["signature"] = signature
        frames[3]["delta"]["signature"] = ""
        wire = WireClient([frames, anthropic_answer()])
        session = self.session(wire)
        session.limits = SessionLimits(context_window_tokens=16384)
        events = await self.run_session(session)
        self.assertEqual(self.handled, [])
        self.assertEqual(len(wire.requests), 1)
        self.assertIn("context_budget_exceeded", repr(events))
        state = await self.store.load(owner="owner", session_id="session")
        # Companion controls retain admitted large signatures and evict whole
        # old native groups: tests/test_agent_argument_oracle_controls.py.
        self.assertFalse(
            any(row.get("provider_state") or row.get("tool_calls") or row.get("role") == "tool"
                for row in state.conversation)
        )
        events = await self.run_session(session, "new-small-turn")
        self.assertEqual(len(wire.requests), 2)
        self.assertNotIn(SIGNATURE, json.dumps(wire.requests[1]))
        self.assertFalse(
            any(row.get("role") == "tool" for row in wire.requests[1]["messages"])
        )
        self.assertNotIn("context_budget_exceeded", repr(events))

    async def test_private_state_absent_from_web_tg_metrics_journal_and_logs(self):
        import logging

        from app.agent.kernel.adapters import consume_events, iter_ndjson
        from app.agent.kernel.metrics import KernelMetrics
        from tests.test_agent_kernel_core import _events_stream

        wire = WireClient([anthropic_tool_frames(), anthropic_answer()])
        metrics = KernelMetrics()
        # Capture ordinary application logging, including successful silence.
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        capture = Capture()
        logger = logging.getLogger("app")
        logger.addHandler(capture)
        try:
            stream = self.session(wire).run(
                AgentInput(
                    message="检查状态",
                    owner="owner",
                    session_id="session",
                    request_id="public",
                    channel="telegram",
                )
            )
            events = [
                event async for event in metrics.track(stream, channel="telegram")
            ]
        finally:
            logger.removeHandler(capture)
        view = await consume_events(_events_stream(events))
        ndjson = b"".join(
            [chunk async for chunk in iter_ndjson(_events_stream(events))]
        ).decode()
        journal = await self.store.list_events(owner="owner", session_id="session")
        state = await self.store.load(owner="owner", session_id="session")
        restored = restore_messages(state)
        public = (
            ndjson
            + repr(view.to_dict())
            + repr(metrics.snapshot())
            + repr(journal)
            + repr(records)
            + repr(restored)
            + repr(public_conversation_messages(state.conversation))
        )
        for secret in (THINKING, SIGNATURE, REDACTED):
            self.assertNotIn(secret, public)
        self.assertEqual(view.answer, "已核实")
        self.assertEqual(metrics.snapshot()["average_model_calls"], 2.0)
        self.assertEqual(self.handled, ["inspect"])

    async def test_native_reasoning_allowlist_rejects_raw_system_injection(self):
        reasoning = {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": REDACTED,
            "role": "system",
            "instructions": "raw-system-injection",
            "summary": [
                {"type": "summary_text", "text": THINKING, "role": "system"},
                {"type": "input_text", "text": "raw-system-injection"},
            ],
        }
        call = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call-1",
            "name": "inspect",
            "arguments": "{}",
        }
        answer = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
            ],
        }
        wire = WireClient(
            [
                {
                    "status": "completed",
                    "output": [
                        reasoning,
                        {"role": "system", "content": "raw-system-injection"},
                        call,
                    ],
                    "instructions": "raw-system-injection",
                },
                answer,
            ]
        )
        await self.run_session(self.session(wire, "responses"))
        sent = wire.requests[1]["input"]
        self.assertNotIn("raw-system-injection", json.dumps(sent))
        native = next(row for row in sent if row.get("type") == "reasoning")
        self.assertEqual(
            native,
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": REDACTED,
                "summary": [{"type": "summary_text", "text": THINKING}],
            },
        )
        self.assertEqual(len([row for row in sent if row.get("role") == "system"]), 1)

    async def test_synthetic_budget_answer_does_not_duplicate_private_tool_state(self):
        from app.agent.kernel.session import SessionLimits

        wire = WireClient([anthropic_tool_frames(), anthropic_answer()])
        session = self.session(wire)
        session.limits = SessionLimits(max_model_rounds=1)
        await self.run_session(session)
        state = await self.store.load(owner="owner", session_id="session")
        private_rows = [row for row in state.conversation if row.get("provider_state")]
        self.assertEqual(len(private_rows), 1)
        self.assertTrue(private_rows[0].get("tool_calls"))
        events = await self.run_session(self.session(wire), "restore-after-budget")
        self.assertEqual(len(wire.requests), 2)
        self.assertNotIn("model_provider_error", repr(events))

    async def test_malformed_or_unclosed_sse_private_block_is_rejected(self):
        for fault in ("nonstring", "unclosed", "missing_start"):
            with self.subTest(fault=fault):
                frames = anthropic_tool_frames()
                if fault == "nonstring":
                    frames[2]["delta"]["signature"] = 42
                elif fault == "unclosed":
                    frames = [
                        frame
                        for frame in frames
                        if not (
                            frame.get("type") == "content_block_stop"
                            and frame.get("index") == 0
                        )
                    ]
                else:
                    frames = frames[1:]
                wire = WireClient([frames, anthropic_answer()])
                events = await self.run_session(self.session(wire))
                self.assertEqual(self.handled, [])
                self.assertIn("model_provider_error", repr(events))

    async def test_unsigned_or_malformed_thinking_never_becomes_replayable_state(self):
        for invalid in (
            {"type": "thinking", "thinking": THINKING},
            {"type": "thinking", "thinking": THINKING, "signature": ""},
            {"type": "thinking", "thinking": THINKING, "signature": {"bad": SIGNATURE}},
            {"type": "redacted_thinking", "data": {"bad": REDACTED}},
        ):
            with self.subTest(kind=invalid["type"]):
                wire = WireClient(
                    [
                        {
                            "content": [
                                invalid,
                                {
                                    "type": "tool_use",
                                    "id": "call-1",
                                    "name": "inspect",
                                    "input": {},
                                },
                            ],
                            "stop_reason": "tool_use",
                        },
                        anthropic_answer(),
                    ]
                )
                events = await self.run_session(self.session(wire))
                self.assertEqual(self.handled, [])
                self.assertIn("model_provider_error", repr(events))
                self.assertNotIn(THINKING, repr(events))

    async def test_response_byte_limit_applies_to_sse_and_json_before_finish(self):
        from app.agent.kernel.model import ModelRequest
        from app.agent.kernel.provider_model import ModelProviderError
        from app.agent.kernel.state_types import CancellationToken

        for reply in (
            [{"choices": [{"delta": {"reasoning_content": "x" * 800}}]}] * 4
            + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"],
            {
                "choices": [
                    {
                        "message": {"reasoning_content": "x" * 3200, "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ):
            with self.subTest(streamed=isinstance(reply, list)):
                wire = WireClient([reply])
                model = OpenAICompatibleModelAdapter(
                    ProviderSettings(
                        api_url="https://api.example.com/v1",
                        model="one",
                        protocol="chat_completions",
                        max_response_bytes=1024,
                    ),
                    client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: wire),
                )
                with self.assertRaisesRegex(ModelProviderError, "响应超过安全上限"):
                    _ = [
                        event
                        async for event in model.stream(
                            ModelRequest(system_prompt="", messages=(), tools=()),
                            cancellation=CancellationToken(),
                        )
                    ]

    async def test_private_state_over_limit_blocks_before_tool_handler_without_truncation(
        self,
    ):
        frames = anthropic_tool_frames()
        frames[1:2] = [
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": THINKING * 2000},
            }
            for _ in range(4)
        ]
        wire = WireClient([frames, anthropic_answer()])
        with self.assertLogs("app.agent.kernel.turn", level="WARNING") as logs:
            events = await self.run_session(self.session(wire))
        self.assertEqual(self.handled, [])
        self.assertEqual(len(wire.requests), 1)
        self.assertIn("model_provider_error", repr(events))
        self.assertNotIn(THINKING, repr(events) + repr(logs.output))
        state = await self.store.load(owner="owner", session_id="session")
        self.assertFalse(any(row.get("provider_state") for row in state.conversation))

    async def test_anthropic_json_preserves_interleaved_blocks_and_final_answer_state(
        self,
    ):
        blocks = [
            {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
            {"type": "text", "text": "核实中"},
            {"type": "redacted_thinking", "data": REDACTED},
            {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
        ]
        answer_blocks = [
            {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
            {"type": "text", "text": "已核实"},
        ]
        wire = WireClient(
            [
                {"content": blocks, "stop_reason": "tool_use"},
                {"content": answer_blocks, "stop_reason": "end_turn"},
                anthropic_answer(),
            ]
        )
        events = await self.run_session(self.session(wire))
        self.assertEqual(wire.requests[1]["messages"][1]["content"], blocks)
        self.store = SQLiteKernelStore(secret_provider=lambda: "synthetic-state-secret")
        events += await self.run_session(self.session(wire), "restored")
        assistants = [
            row["content"]
            for row in wire.requests[2]["messages"]
            if row["role"] == "assistant"
        ]
        self.assertEqual(assistants, [blocks, answer_blocks])
        self.assertNotIn(THINKING, repr(events))

    async def test_chat_reasoning_content_and_tool_arguments_equivalent_sse_json(self):
        for streamed, reasoning in ((True, THINKING), (False, THINKING), (True, ""), (False, "")):
            with self.subTest(streamed=streamed, reasoning=reasoning):
                call = {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
                message = {
                    "role": "assistant",
                    "reasoning_content": reasoning,
                    "content": "<think>hidden-markup</think>核实中",
                    "tool_calls": [call],
                }
                first = (
                    [
                        {"choices": [{"delta": {"reasoning_content": None}}]},
                        {"choices": [{"delta": {"reasoning_content": reasoning[:9]}}]},
                        {
                            "choices": [
                                {
                                    "delta": {
                                        **message,
                                        "reasoning_content": reasoning[9:],
                                        "tool_calls": [{**call, "index": 0}],
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        },
                        {"choices": [{"delta": {"reasoning_content": None}}]},
                        "[DONE]",
                    ]
                    if streamed
                    else {
                        "choices": [{"message": message, "finish_reason": "tool_calls"}]
                    }
                )
                answer = {
                    "choices": [
                        {"message": {"content": "已核实", "reasoning_content": reasoning}, "finish_reason": "stop"}
                    ]
                }
                wire = WireClient([first, answer, answer])
                events = await self.run_session(
                    self.session(wire, "chat_completions"), f"{streamed}-{len(reasoning)}"
                )
                assistant = [
                    row for row in wire.requests[1]["messages"] if row.get("tool_calls")
                ][-1]
                self.assertEqual(assistant, {**message, "content": "核实中"})
                self.store = SQLiteKernelStore(
                    secret_provider=lambda: "synthetic-state-secret"
                )
                events += await self.run_session(
                    self.session(wire, "chat_completions"), f"restored-{streamed}-{len(reasoning)}"
                )
                assistant = [
                    row for row in wire.requests[2]["messages"] if row.get("tool_calls")
                ][-1]
                self.assertEqual(assistant["reasoning_content"], reasoning)
                previous_answer = [row for row in wire.requests[2]["messages"]
                                   if row["role"] == "assistant" and row.get("content") == "已核实"][-1]
                self.assertEqual(previous_answer["reasoning_content"], reasoning)
                self.assertNotIn(THINKING, repr(events))
                self.assertNotIn("hidden-markup", repr(events))

    async def test_responses_reasoning_order_and_encrypted_content_roundtrip_sse_and_json(
        self,
    ):
        public_summary = "Check the library before answering."
        reasoning = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": public_summary}],
            "content": [{"type": "reasoning_text", "text": THINKING}],
            "encrypted_content": REDACTED,
            "status": "completed",
        }
        call = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call-1",
            "name": "inspect",
            "arguments": "{}",
        }
        answer = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "已核实"}],
                }
            ],
        }
        for streamed in (True, False):
            with self.subTest(streamed=streamed):
                self.store = SQLiteKernelStore(
                    secret_provider=lambda: "synthetic-state-secret"
                )
                first = [
                    {
                        "type": "response.output_item.done",
                        "output_index": index,
                        "item": item,
                    }
                    for index, item in enumerate([reasoning, call])
                ]
                first += [
                    {"type": "response.completed", "response": {"status": "completed"}}
                ]
                wire = WireClient(
                    [
                        first
                        if streamed
                        else {"status": "completed", "output": [reasoning, call]},
                        answer,
                        answer,
                    ]
                )
                events = await self.run_session(
                    self.session(wire, "responses"), str(streamed)
                )
                sent = wire.requests[1]["input"]
                found = [
                    item
                    for item in sent
                    if item.get("type")
                    in {"reasoning", "function_call", "function_call_output"}
                ]
                self.assertEqual(found[-3:-1], [reasoning, call])
                self.assertEqual(found[-1]["call_id"], "call-1")
                self.store = SQLiteKernelStore(
                    secret_provider=lambda: "synthetic-state-secret"
                )
                events += await self.run_session(
                    self.session(wire, "responses"), "restored"
                )
                self.assertIn(reasoning, wire.requests[2]["input"])
                state = await self.store.load(owner="owner", session_id="session")
                public = repr(events) + json.dumps(
                    public_conversation_messages(state.conversation)
                )
                self.assertNotIn(THINKING, public)
                self.assertNotIn(REDACTED, public)
                self.assertIn(public_summary, public)

    async def test_public_summary_redacts_cross_chunk_credentials_without_changing_native_replay(self):
        from app.agent.kernel.adapters import consume_events, iter_ndjson
        from app.agent.kernel.events import AgentEventType
        from tests.test_agent_kernel_core import _events_stream

        secret = "summary-credential-canary"
        summary = f"  Check source https://example.com/info .\n\napi_key={secret}  "
        public_summary = "  Check source https://example.com/info .\n\napi_key=********  "
        reasoning = {
            "type": "reasoning", "id": "rs_summary", "encrypted_content": REDACTED,
            "summary": [{"type": "summary_text", "text": summary}],
            "content": [{"type": "reasoning_text", "text": THINKING}],
        }
        call = {"type": "function_call", "id": "fc_summary", "call_id": "call-summary",
                "name": "inspect", "arguments": "{}"}
        done = {"type": "response.reasoning_summary_text.done", "item_id": "rs_summary",
                "output_index": 0, "summary_index": 0, "text": summary}
        frames = [
            {"type": "response.reasoning_summary_text.delta", "item_id": "rs_summary",
             "output_index": 0, "summary_index": 0, "delta": character}
            for character in summary
        ] + [
            done, done,
            {"type": "response.reasoning_text.delta", "delta": THINKING},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.done", "output_index": 1, "item": call},
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
        answer = {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已核实"}]},
        ]}
        wire = WireClient([frames, answer, answer], split=1)
        events = await self.run_session(self.session(wire, "responses", reasoning_effort="high"))
        summaries = [event for event in events if event.type is AgentEventType.MODEL_REASONING_SUMMARY_DELTA]
        self.assertEqual([dict(event.payload) for event in summaries], [
            {"delta": public_summary, "round": 1, "summary_id": "rs_summary:0"},
        ])
        self.assertEqual(wire.requests[0]["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(len(wire.requests), 2)
        self.assertIn(reasoning, wire.requests[1]["input"])
        view = await consume_events(_events_stream(events))
        self.assertEqual(view.answer, "已核实")
        state = await self.store.load(owner="owner", session_id="session")
        self.assertTrue(all(summary not in message.content for message in restore_messages(state)))
        journal = await self.store.list_events(owner="owner", session_id="session")
        ndjson = b"".join([part async for part in iter_ndjson(_events_stream(events))]).decode()
        public = repr(events) + repr(journal) + ndjson + repr(public_conversation_messages(state.conversation))
        for private in (secret, THINKING, REDACTED):
            self.assertNotIn(private, public)

        self.store = SQLiteKernelStore(secret_provider=lambda: "synthetic-state-secret")
        restored_events = await self.run_session(
            self.session(wire, "responses", reasoning_effort="high"), "summary-restored",
        )
        self.assertIn(reasoning, wire.requests[2]["input"])
        self.assertFalse(any(event.type is AgentEventType.MODEL_REASONING_SUMMARY_DELTA for event in restored_events))
        self.assertEqual(self.handled, ["inspect"])

    async def test_anthropic_text_block_tails_survive_tool_round_and_sqlite_restore(self):
        # A block boundary ends markup lookahead; delta boundaries do not.
        cases = (
            (("", ["检索文件名中的字符：<"]),),
            (("首块<", []), ("", ["末块<"])),
            (("", ["首块<thi"]), ("nk>字面文本", [])),
            (("<thi", ["nk>hidden-markup</th", "ink>可见<", "tag>"]),),
            (("", ["首块<", "think>hidden-markup</think>可见<"]),
             ("后块", ["<"])),
        )
        expected_texts = (
            ["检索文件名中的字符：<"],
            ["首块<", "末块<"],
            ["首块<thi", "nk>字面文本"],
            ["可见<tag>"],
            ["首块可见<", "后块<"],
        )
        for case, expected in zip(cases, expected_texts):
            for streamed in (True, False):
                with self.subTest(case=case, streamed=streamed):
                    # Isolate each history so no previous signed group can mask loss.
                    await self.store.delete_session(owner="owner", session_id="session")
                    self.handled.clear()
                    private = [
                        {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
                        {"type": "redacted_thinking", "data": REDACTED},
                    ]
                    tool = {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}}
                    raw_blocks: list[dict[str, Any]] = [private[0]]
                    expected_blocks: list[dict[str, Any]] = [private[0]]
                    frames = anthropic_tool_frames()[:5]
                    for position, ((start, deltas), visible) in enumerate(zip(case, expected)):
                        index = len(raw_blocks)
                        raw_blocks.append({"type": "text", "text": start + "".join(deltas)})
                        expected_blocks.append({"type": "text", "text": visible})
                        frames.append({"type": "content_block_start", "index": index,
                                       "content_block": {"type": "text", "text": start}})
                        frames.extend({"type": "content_block_delta", "index": index,
                                       "delta": {"type": "text_delta", "text": text}} for text in deltas)
                        frames.append({"type": "content_block_stop", "index": index})
                        if position == 0:
                            # Later text must not receive the first block's pending '<'.
                            for block in (private[1], tool):
                                index = len(raw_blocks)
                                frames.extend([
                                    {"type": "content_block_start", "index": index, "content_block": block},
                                    {"type": "content_block_stop", "index": index},
                                ])
                                raw_blocks.append(block)
                                expected_blocks.append(block)
                    frames.extend([
                        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                        {"type": "message_stop"},
                    ])
                    first = frames if streamed else {"content": raw_blocks, "stop_reason": "tool_use"}
                    wire = WireClient([first, anthropic_answer(), anthropic_answer()])
                    events = await self.run_session(self.session(wire))
                    self.assertEqual(self.handled, ["inspect"])
                    self.assertEqual(len(wire.requests), 2)
                    self.store = SQLiteKernelStore(secret_provider=lambda: "synthetic-state-secret")
                    state = await self.store.load(owner="owner", session_id="session")
                    row = next(row for row in state.conversation if row.get("provider_state"))
                    self.assertEqual(row["content"], "".join(expected))
                    events += await self.run_session(self.session(wire), "restored")
                    self.assertEqual(len(wire.requests), 3)
                    self.assertEqual(self.handled, ["inspect"])
                    for request in wire.requests[1:]:
                        assistant = next(row for row in request["messages"] if row["role"] == "assistant")
                        self.assertEqual(assistant["content"], expected_blocks)
                        results = [block for row in request["messages"]
                                   if isinstance(row.get("content"), list) for block in row["content"]
                                   if block.get("type") == "tool_result"]
                        self.assertEqual([block["tool_use_id"] for block in results], ["call-1"])
                    self.assertNotIn("model_provider_error", repr(events))
                    for secret in (THINKING, SIGNATURE, REDACTED, "hidden-markup"):
                        self.assertNotIn(secret, repr(events))

    async def test_anthropic_sse_private_blocks_survive_tool_round_and_sqlite_restore(
        self,
    ):
        wire = WireClient(
            [anthropic_tool_frames(), anthropic_answer(), anthropic_answer()]
        )
        events = await self.run_session(self.session(wire))
        self.assertEqual(self.handled, ["inspect"])
        expected = [
            {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
            {"type": "redacted_thinking", "data": REDACTED},
            {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
        ]
        assistant = next(
            row for row in wire.requests[1]["messages"] if row["role"] == "assistant"
        )
        self.assertEqual(assistant["content"], expected)
        self.store = SQLiteKernelStore(secret_provider=lambda: "synthetic-state-secret")
        events += await self.run_session(self.session(wire), "restored")
        assistant = next(
            row for row in wire.requests[2]["messages"] if row["role"] == "assistant"
        )
        self.assertEqual(assistant["content"], expected)
        self.assertEqual(
            wire.requests[2]["messages"][2]["content"][0]["tool_use_id"], "call-1"
        )
        state = await self.store.load(owner="owner", session_id="session")
        public = json.dumps(public_conversation_messages(state.conversation)) + repr(
            events
        )
        for secret in (THINKING, SIGNATURE, REDACTED):
            self.assertNotIn(secret, public)
