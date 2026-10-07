from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
from app.agent.kernel.pipeline import ToolCallContext, ToolPipelineError
from app.agent.kernel.ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
)
from app.agent.kernel.state import AgentInput, CancellationToken, PublicationLease
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
            "app.agent.kernel.ports.mediaflux_policy.allow_agent_tool",
            return_value=True,
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
                "app.agent.kernel.ports.mediaflux_policy.allow_agent_tool",
                return_value=False,
            ),
            self.assertRaises(ToolPipelineError) as raised,
        ):
            await limiter.acquire(
                owner="owner", tool_name="library.search", cost=1, arguments={}
            )
        self.assertEqual(raised.exception.code, "rate_limited")

    async def test_provider_query_budget_is_shared_by_provider_and_owner(self) -> None:
        from app import database as db
        from app.agent.provider_actions import get_provider_gateway

        db.init_db()
        catalog = get_provider_gateway().catalog
        media_operation = catalog.get("media.system.info")
        other_media_operation = catalog.get("media.items.counts")
        qbittorrent_operation = catalog.get("qb.app.version")
        self.assertEqual(media_operation.provider, "media")
        self.assertEqual(other_media_operation.provider, "media")
        self.assertEqual(qbittorrent_operation.provider, "qbittorrent")

        # tests package pins the shared limiter to its isolated SQLite database.
        first_limiter = MediaFluxToolRateLimiter()
        second_limiter = MediaFluxToolRateLimiter()
        owner = f"live-p2-01:{uuid.uuid4().hex}"
        for index in range(8):
            await (first_limiter if index % 2 == 0 else second_limiter).acquire(
                owner=owner,
                tool_name="provider.query",
                cost=1,
                arguments={
                    "profile_ref": "media-profile-a",
                    "operation": media_operation.operation_id,
                    "arguments": {},
                },
            )

        # Operation and profile changes do not mint another media-provider bucket.
        with self.assertRaises(ToolPipelineError) as changed_operation:
            await second_limiter.acquire(
                owner=owner,
                tool_name="provider.query",
                cost=1,
                arguments={
                    "profile_ref": "media-profile-a",
                    "operation": other_media_operation.operation_id,
                    "arguments": {},
                },
            )
        self.assertEqual(changed_operation.exception.code, "rate_limited")
        self.assertIn("MediaFlux", str(changed_operation.exception))
        self.assertIn("本地", str(changed_operation.exception))
        self.assertIn("未访问后端", str(changed_operation.exception))

        with self.assertRaises(ToolPipelineError) as changed_profile:
            await first_limiter.acquire(
                owner=owner,
                tool_name="provider.query",
                cost=1,
                arguments={
                    "profile_ref": "media-profile-b",
                    "operation": media_operation.operation_id,
                    "arguments": {},
                },
            )
        self.assertEqual(changed_profile.exception.code, "rate_limited")

        # A different owner has an independent budget; a different static provider
        # has a separate suffix even for the original owner.
        await second_limiter.acquire(
            owner=f"live-p2-01:{uuid.uuid4().hex}",
            tool_name="provider.query",
            cost=1,
            arguments={
                "profile_ref": "media-profile-a",
                "operation": media_operation.operation_id,
                "arguments": {},
            },
        )
        await second_limiter.acquire(
            owner=owner,
            tool_name="provider.query",
            cost=1,
            arguments={
                "profile_ref": "qb-profile-a",
                "operation": qbittorrent_operation.operation_id,
                "arguments": {},
            },
        )

    async def test_unknown_provider_operation_does_not_consume_budget(self) -> None:
        limiter = MediaFluxToolRateLimiter()
        with (
            patch(
                "app.agent.kernel.ports.mediaflux_policy.allow_agent_tool",
                return_value=True,
            ) as allowed,
            self.assertRaises(ToolPipelineError) as raised,
        ):
            await limiter.acquire(
                owner="owner",
                tool_name="provider.query",
                cost=1,
                arguments={
                    "profile_ref": "not-used",
                    "operation": "media.operation.not_registered",
                    "arguments": {},
                },
            )
        self.assertEqual(raised.exception.code, "operation_not_allowed")
        allowed.assert_not_called()

    async def test_preview_cannot_spend_confirmation_budget_across_workers(self) -> None:
        with isolated_test_database("confirmation-stage-budget.db"):
            limiters = (MediaFluxToolRateLimiter(), MediaFluxToolRateLimiter())
            owner = "webk:v1:" + uuid.uuid4().hex * 2
            # Three complete preview/confirm pairs must work, including the
            # first confirmation after the entire preview budget was spent.
            for stage in ("", "confirm:"):
                for index in range(3):
                    await limiters[index % 2].acquire(
                        owner=owner, tool_name=stage + "guangya.fs.change.execute",
                        cost=1, arguments={},
                    )
                with self.assertRaises(ToolPipelineError) as raised:
                    await limiters[1].acquire(
                        owner=owner, tool_name=stage + "guangya.fs.change.execute",
                        cost=1, arguments={},
                    )
                self.assertEqual(raised.exception.code, "rate_limited")
                self.assertIn("未访问后端", str(raised.exception))

    async def test_guangya_directory_scrape_reads_have_independent_budgets(self) -> None:
        with isolated_test_database("guangya-scrape-rate-limit.db"):
            first_limiter = MediaFluxToolRateLimiter()
            second_limiter = MediaFluxToolRateLimiter()
            owner = "webk:v1:" + uuid.uuid4().hex * 2
            other_owner = "webk:v1:" + uuid.uuid4().hex * 2

            async def acquire(
                limiter: MediaFluxToolRateLimiter, principal: str, tool_name: str
            ) -> None:
                await limiter.acquire(
                    owner=principal,
                    tool_name=tool_name,
                    cost=1,
                    arguments={},
                )

            # Different sessions/workers with the same owner share SQLite budgets.
            for _ in range(2):
                await acquire(
                    first_limiter, owner, "guangya.directory_scrape.inspect"
                )
                await acquire(
                    second_limiter, owner, "guangya.directory_scrape.search"
                )

            # Two inspect calls must not spend the search budget; search call 3
            # and preview call 1 both remain available.
            await acquire(
                first_limiter, owner, "guangya.directory_scrape.search"
            )
            await acquire(
                second_limiter, owner, "guangya.directory_scrape.preview"
            )

            # Each read tool independently allows four calls, then rejects call 5.
            await acquire(
                first_limiter, owner, "guangya.directory_scrape.search"
            )
            with self.assertRaises(ToolPipelineError) as search_limited:
                await acquire(
                    second_limiter, owner, "guangya.directory_scrape.search"
                )
            self.assertEqual(search_limited.exception.code, "rate_limited")

            for _ in range(2):
                await acquire(
                    second_limiter, owner, "guangya.directory_scrape.inspect"
                )
            with self.assertRaises(ToolPipelineError) as inspect_limited:
                await acquire(
                    first_limiter, owner, "guangya.directory_scrape.inspect"
                )
            self.assertEqual(inspect_limited.exception.code, "rate_limited")

            for _ in range(3):
                await acquire(
                    first_limiter, owner, "guangya.directory_scrape.preview"
                )
            with self.assertRaises(ToolPipelineError) as preview_limited:
                await acquire(
                    second_limiter, owner, "guangya.directory_scrape.preview"
                )
            self.assertEqual(preview_limited.exception.code, "rate_limited")

            # A different owner has an independent per-tool budget.
            for tool_name in (
                "guangya.directory_scrape.inspect",
                "guangya.directory_scrape.search",
                "guangya.directory_scrape.preview",
            ):
                await acquire(second_limiter, other_owner, tool_name)

    async def test_guangya_directory_scrape_run_confirm_has_own_write_budget(self) -> None:
        with isolated_test_database("guangya-scrape-run-rate-limit.db"):
            first_limiter = MediaFluxToolRateLimiter()
            second_limiter = MediaFluxToolRateLimiter()
            owner = "webk:v1:" + uuid.uuid4().hex * 2

            for _ in range(3):
                await first_limiter.acquire(
                    owner=owner,
                    tool_name="guangya.directory_scrape.run",
                    cost=1,
                    arguments={},
                )

            # Preview exhaustion must not reject an already offered confirmation.
            await second_limiter.acquire(
                owner=owner,
                tool_name="confirm:guangya.directory_scrape.run",
                cost=1,
                arguments={},
            )
            with self.assertRaises(ToolPipelineError) as raised:
                await first_limiter.acquire(
                    owner=owner,
                    tool_name="guangya.directory_scrape.run",
                    cost=1,
                    arguments={},
                )
            self.assertEqual(raised.exception.code, "rate_limited")

    async def test_configuration_reads_do_not_spend_write_preview_budget(self) -> None:
        families = (
            ("media.preferences", "media.set_preferences", "media.clear_preferences"),
            (
                "media.subscription_notification_rule",
                "media.set_subscription_notification_rule",
                "media.reset_subscription_notification_rule",
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
