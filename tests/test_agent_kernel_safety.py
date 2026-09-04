"""Agent Kernel 隐私、EffectPlan 并发与确认审计边界。"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.confirmation import ConfirmationStore
from app.agent.feature_gate import current_agent_runtime_generation
from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.effects import (
    ConfirmationEffectPlanStore,
    EffectPlanError,
    PreparedEffect,
)
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import (
    InMemoryRateLimiter,
    ToolCallContext,
    ToolPipeline,
)
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import (
    AgentInput,
    CancellationToken,
    InMemorySessionStateStore,
)
from app.agent.models import ToolResult
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel, collect


async def _ignore_progress(_payload: dict[str, Any]) -> None:
    return None


async def _context(
    state: InMemorySessionStateStore,
    *,
    owner: str = "owner-1",
    session_id: str = "session-1",
) -> ToolCallContext:
    lease, _snapshot = await state.begin_turn(
        owner=owner,
        session_id=session_id,
        request_id="request-1",
    )
    return ToolCallContext(
        owner=owner,
        session_id=session_id,
        request_id=lease.request_id,
        turn_id=lease.turn_id,
        lease=lease,
        cancellation=CancellationToken(),
        report_progress=_ignore_progress,
    )


class KernelPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_arguments_are_not_published_or_persisted(self) -> None:
        secret = "rss-passkey-very-secret"
        private_path = "/home/aio/private/downloads"
        tool = KernelToolSpec(
            name="rss.inspect",
            domain="rss",
            description="检查 RSS 配置",
            examples=("检查 RSS",),
            input_schema={
                "type": "object",
                "required": ["url", "path"],
                "properties": {
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            read=lambda _arguments, _context: {
                "ok": True,
                "status": "success",
                "summary": "检查完成",
            },
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall(
                            "call-secret",
                            "rss.inspect",
                            {
                                "url": f"https://example.invalid/rss?passkey={secret}",
                                "path": private_path,
                            },
                        ),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="RSS 配置可用。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="检查这个 RSS",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        serialized_events = json.dumps(
            [event.to_dict() for event in events], ensure_ascii=False
        )
        self.assertNotIn(secret, serialized_events)
        self.assertNotIn(private_path, serialized_events)
        tool_call = next(
            event for event in events if event.type is AgentEventType.MODEL_TOOL_CALL
        )
        self.assertEqual(tool_call.payload["argument_keys"], ["path", "url"])
        self.assertNotIn("arguments", tool_call.payload)

        persisted = await state.load(owner="owner-1", session_id="session-1")
        serialized_state = json.dumps(persisted.conversation, ensure_ascii=False)
        self.assertNotIn(secret, serialized_state)
        self.assertNotIn(private_path, serialized_state)
        assistant_call = next(
            item
            for item in persisted.conversation
            if item.get("role") == "assistant" and item.get("tool_calls")
        )
        self.assertEqual(assistant_call["tool_calls"][0]["arguments"], {})


class EffectPlanStoreSafetyTests(unittest.TestCase):
    @staticmethod
    def _prepared(label: str) -> PreparedEffect:
        return PreparedEffect(
            preview={"summary": label},
            snapshot_fingerprint=f"snapshot:{label}",
            metadata={"risk": "write"},
        )

    def test_new_plan_replaces_old_without_old_claim_revoking_new(self) -> None:
        tokens = iter(("plan-old-123456789", "plan-new-123456789"))
        store = ConfirmationEffectPlanStore(
            ConfirmationStore(token_factory=lambda: next(tokens))
        )
        old = store.freeze(
            owner="owner",
            session_id="session",
            generation=1,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": 1},
            prepared=self._prepared("old"),
        )
        new = store.freeze(
            owner="owner",
            session_id="session",
            generation=2,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": 2},
            prepared=self._prepared("new"),
        )

        with self.assertRaises(EffectPlanError):
            store.claim(
                owner="owner",
                session_id="session",
                generation=1,
                plan_id=old.plan_id,
            )
        claimed = store.claim(
            owner="owner",
            session_id="session",
            generation=2,
            plan_id=new.plan_id,
        )
        self.assertEqual(claimed.arguments, {"value": 2})

    def test_stale_generation_is_rejected_before_ticket_consumption(self) -> None:
        store = ConfirmationEffectPlanStore(ConfirmationStore())
        plan = store.freeze(
            owner="owner",
            session_id="session",
            generation=7,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": 7},
            prepared=self._prepared("current"),
        )

        with self.assertRaisesRegex(EffectPlanError, "stale"):
            store.claim(
                owner="owner",
                session_id="session",
                generation=6,
                plan_id=plan.plan_id,
            )
        claimed = store.claim(
            owner="owner",
            session_id="session",
            generation=7,
            plan_id=plan.plan_id,
        )
        self.assertEqual(claimed.plan_id, plan.plan_id)
        # 内存存储使用 monotonic 时钟，不能伪装成 Unix 时间戳。
        self.assertEqual(claimed.public_dict()["expires_at"], "")

    def test_revoke_session_invalidates_only_that_session(self) -> None:
        store = ConfirmationEffectPlanStore(ConfirmationStore())
        first = store.freeze(
            owner="owner",
            session_id="session-a",
            generation=1,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": "first"},
            prepared=self._prepared("first"),
        )
        second = store.freeze(
            owner="owner",
            session_id="session-b",
            generation=1,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": "second"},
            prepared=self._prepared("second"),
        )

        self.assertEqual(
            store.revoke_session(owner="owner", session_id="session-a"),
            1,
        )
        with self.assertRaisesRegex(EffectPlanError, "unavailable"):
            store.claim(
                owner="owner",
                session_id="session-a",
                generation=1,
                plan_id=first.plan_id,
            )
        claimed = store.claim(
            owner="owner",
            session_id="session-b",
            generation=1,
            plan_id=second.plan_id,
        )
        self.assertEqual(claimed.arguments, {"value": "second"})


class EffectLifecycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._database = isolated_test_database("agent-kernel-effect-lifecycle.db")
        self._database.__enter__()

    def tearDown(self) -> None:
        self._database.__exit__(None, None, None)

    async def test_confirmed_effect_records_actual_owner_and_invalidates_runtime(
        self,
    ) -> None:
        owner = "kernel-history-owner"

        def prepare(_arguments: dict[str, Any], _context: ToolCallContext):
            return PreparedEffect(
                preview={
                    "ok": True,
                    "status": "preview",
                    "summary": "将执行 STRM 同步",
                },
                snapshot_fingerprint="snapshot:v1",
                metadata={"risk": "danger"},
            )

        def execute(
            _arguments: dict[str, Any],
            _snapshot: str,
            _context: ToolCallContext,
        ) -> ToolResult:
            return ToolResult(
                ok=True,
                status="completed",
                summary="STRM 同步完成",
                data={"accepted": True},
            )

        tool = KernelToolSpec(
            name="strm.run_once",
            domain="automation",
            description="执行 STRM 同步",
            examples=("同步 STRM",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.DANGER,
            prepare=prepare,
            execute_confirmed=execute,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(
            catalog=catalog,
            state_store=state,
            effect_store=ConfirmationEffectPlanStore(
                ConfirmationStore(),
                record_actions=True,
            ),
            rate_limiter=InMemoryRateLimiter(limit=100),
            effect_lifecycle=MediaFluxEffectLifecycle(),
        )
        context = await _context(state, owner=owner)
        preview = await pipeline.execute("strm.run_once", {}, context=context)
        self.assertIsNotNone(preview.effect_plan)
        before = current_agent_runtime_generation()

        with patch("app.agent.feature_gate._runtime_generation", before):
            await pipeline.execute_confirmed(
                preview.effect_plan.plan_id,
                context=context,
            )
            self.assertEqual(current_agent_runtime_generation(), before + 1)

        rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(owner),
            limit=10,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "strm.run_once")
        self.assertEqual(rows[0]["status"], "completed")
        self.assertTrue(bool(rows[0]["ok"]))
        scoped_rows = db.list_agent_action_history(
            owner_digest=action_history_owner_digest(f"kernel:{owner}\x1fsession-1"),
            limit=10,
        )
        self.assertEqual(scoped_rows, [])
