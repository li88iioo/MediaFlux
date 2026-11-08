from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
from app.agent.kernel.ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
)
from app.agent.kernel.state_types import AgentInput, CancellationToken, PublicationLease
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.rate_limit import RateLimitDecision
from tests.support import isolated_test_database


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
            "app.agent.kernel.ports.mediaflux_policy.consume_agent_tool",
            return_value=RateLimitDecision(
                allowed=True, limit=4, cost=1, remaining=3,
                retry_after_seconds=0.0, retry_at_epoch=None, reason="allowed",
            ),
        ) as allowed:
            await limiter.acquire(
                owner="owner",
                tool_name="confirm:rss.create_subscription",
                cost=2,
                arguments={},
            )
        allowed.assert_called_once_with(
            "owner", "rss.create_subscription", scope_suffix="confirmed"
        )
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.consume_agent_tool",
                return_value=RateLimitDecision(
                    allowed=False, limit=12, cost=1, remaining=0,
                    retry_after_seconds=65.0, retry_at_epoch=185.0, reason="quota",
                ),
            ),
            self.assertRaises(ToolPipelineError) as raised,
        ):
            await limiter.acquire(
                owner="owner", tool_name="library.search", cost=1, arguments={}
            )
        self.assertEqual(raised.exception.code, "rate_limited")


    async def test_preview_cannot_spend_confirmation_budget_across_workers(self) -> None:
        with isolated_test_database("confirmation-stage-budget.db"):
            limiters = (MediaFluxToolRateLimiter(), MediaFluxToolRateLimiter())
            owner = "webk:v1:" + uuid.uuid4().hex * 2
            # Every preview/confirm pair within the policy limit must work, including
            # the first confirmation after the entire preview budget was spent.
            from app.agent.rate_limit import tool_rate_limit_policy

            limit = tool_rate_limit_policy("cloud.change")[1]
            for stage in ("", "confirm:"):
                for index in range(limit):
                    await limiters[index % 2].acquire(
                        owner=owner, tool_name=stage + "cloud.change",
                        cost=1, arguments={},
                    )
                with self.assertRaises(ToolPipelineError) as raised:
                    await limiters[1].acquire(
                        owner=owner, tool_name=stage + "cloud.change",
                        cost=1, arguments={},
                    )
                self.assertEqual(raised.exception.code, "rate_limited")
                self.assertIn("未访问后端", str(raised.exception))


    async def test_native_cloud_scrape_confirm_has_own_write_budget(self) -> None:
        with isolated_test_database("guangya-scrape-run-rate-limit.db"):
            first_limiter = MediaFluxToolRateLimiter()
            second_limiter = MediaFluxToolRateLimiter()
            owner = "webk:v1:" + uuid.uuid4().hex * 2

            for _ in range(3):
                await first_limiter.acquire(
                    owner=owner,
                    tool_name="cloud.scrape",
                    cost=1,
                    arguments={},
                )

            # Preview exhaustion must not reject an already offered confirmation.
            await second_limiter.acquire(
                owner=owner,
                tool_name="confirm:cloud.scrape",
                cost=1,
                arguments={},
            )
            with self.assertRaises(ToolPipelineError) as raised:
                await first_limiter.acquire(
                    owner=owner,
                    tool_name="cloud.scrape",
                    cost=1,
                    arguments={},
                )
            self.assertEqual(raised.exception.code, "rate_limited")

    async def test_configuration_reads_do_not_spend_write_preview_budget(self) -> None:
        families = (
            (None, "preferences.update", "preferences.clear"),
            (
                "subscriptions.notification",
                "subscriptions.set_notification",
                "subscriptions.reset_notification",
            ),
        )
        with (
            isolated_test_database("config-read-write-budgets.db"),
            patch("app.agent.rate_limit.time.time", return_value=1800000000.0),
        ):
            limiters = (MediaFluxToolRateLimiter(), MediaFluxToolRateLimiter())
            for read, update, clear in families:
                with self.subTest(read=read):
                    owner = "webk:v1:" + uuid.uuid4().hex * 2
                    # Actual shared SQLite budget, not an allow() mock. Queries
                    # before/after changes must not consume a write's allowance.
                    if read is None:
                        from app.sdk import call_owner, preferences

                        # 普通 SDK 读取不再伪装成旧只读工具；仍不能消耗或补充写预算。
                        with call_owner(owner):
                            for _ in range(12):
                                self.assertFalse(preferences.get()["explicit"])
                    else:
                        for index in range(12):
                            await limiters[index % 2].acquire(
                                owner=owner, tool_name=read, cost=1, arguments={}
                            )
                        with self.assertRaises(ToolPipelineError) as read_limited:
                            await limiters[1].acquire(
                                owner=owner, tool_name=read, cost=1, arguments={}
                            )
                        self.assertEqual(read_limited.exception.code, "rate_limited")
                    for stage in ("", "confirm:"):
                        for index, tool in enumerate((update, clear, update, clear)):
                            await limiters[index % 2].acquire(
                                owner=owner, tool_name=stage + tool,
                                cost=1, arguments={},
                            )
                        # Changing tool/session/worker cannot replenish the
                        # shared write budget; confirmations have their own.
                        for tool in (update, clear):
                            with self.assertRaises(ToolPipelineError) as limited:
                                await limiters[0].acquire(
                                    owner=owner, tool_name=stage + tool,
                                    cost=1, arguments={},
                                )
                            self.assertEqual(limited.exception.code, "rate_limited")
                    await limiters[1].acquire(
                        owner="webk:v1:" + uuid.uuid4().hex * 2,
                        tool_name=update, cost=1, arguments={},
                    )
