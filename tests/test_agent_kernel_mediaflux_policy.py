from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
from app.agent.kernel.pipeline import ToolCallContext, ToolPipelineError
from app.agent.kernel.ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
)
from app.agent.kernel.state import AgentInput, CancellationToken, PublicationLease


async def _progress(_payload):
    return None


def _context(owner: str) -> ToolCallContext:
    lease = PublicationLease(
        owner=owner,
        session_id="session-12345678",
        generation=1,
        turn_id="turn-12345678",
        request_id="request-12345678",
    )
    return ToolCallContext(
        owner=owner,
        session_id=lease.session_id,
        request_id=lease.request_id,
        turn_id=lease.turn_id,
        lease=lease,
        cancellation=CancellationToken(),
        report_progress=_progress,
    )


_TOOL = KernelToolSpec(
    name="library.search",
    domain="library",
    description="查询媒体库",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    effect=ToolEffect.READ,
    read=lambda _arguments, _context: {},
)


class MediaFluxPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_and_current_telegram_principals_are_authorized(self) -> None:
        policy = MediaFluxAuthorizationPolicy()
        web_owner = "webk:v1:" + "a" * 64
        with patch(
            "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
            return_value=True,
        ):
            await policy.authorize(_TOOL, {}, _context(web_owner))
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
                return_value=True,
            ),
            patch(
                "app.agent.kernel.ports.mediaflux_policy.telegram_owner_route_is_currently_authorized",
                return_value=True,
            ),
        ):
            await policy.authorize(_TOOL, {}, _context("tg:v1:-123\x1f456"))

    async def test_disabled_or_unknown_principal_is_rejected(self) -> None:
        policy = MediaFluxAuthorizationPolicy()
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
                return_value=False,
            ),
            self.assertRaisesRegex(ToolPipelineError, "未启用"),
        ):
            await policy.authorize(_TOOL, {}, _context("webk:v1:" + "a" * 64))
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
                return_value=True,
            ),
            self.assertRaises(ToolPipelineError) as raised,
        ):
            await policy.authorize(_TOOL, {}, _context("unknown"))
        self.assertEqual(raised.exception.code, "authorization_denied")

    async def test_turn_admission_rejects_late_publication_after_runtime_change(
        self,
    ) -> None:
        admission = MediaFluxTurnAdmission()
        owner = "webk:v1:" + "a" * 64
        agent_input = AgentInput(
            owner=owner,
            session_id="session-12345678",
            message="检查媒体库",
        )
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
                return_value=True,
            ),
            patch(
                "app.agent.kernel.ports.mediaflux_policy.current_agent_runtime_generation",
                return_value=4,
            ),
        ):
            token = await admission.begin(agent_input)
        self.assertEqual(token, 4)
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.is_agent_enabled",
                return_value=True,
            ),
            patch(
                "app.agent.kernel.ports.mediaflux_policy.agent_runtime_generation_is_current",
                return_value=False,
            ),
        ):
            self.assertFalse(await admission.is_current(token, agent_input))

    async def test_shared_rate_limiter_uses_canonical_tool_name(self) -> None:
        limiter = MediaFluxToolRateLimiter()
        with patch(
            "app.agent.kernel.ports.mediaflux_policy.allow_agent_tool",
            return_value=True,
        ) as allowed:
            await limiter.acquire(
                owner="owner", tool_name="confirm:rss.create_subscription", cost=2
            )
        allowed.assert_called_once_with("owner", "rss.create_subscription")
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.allow_agent_tool",
                return_value=False,
            ),
            self.assertRaises(ToolPipelineError) as raised,
        ):
            await limiter.acquire(owner="owner", tool_name="library.search", cost=1)
        self.assertEqual(raised.exception.code, "rate_limited")
