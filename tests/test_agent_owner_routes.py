"""Agent owner 派生与低层路由边界测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.owner_routes import (
    telegram_owner_route_is_currently_authorized,
    web_kernel_owner,
)
from app.routes import agent_api


class AgentOwnerRouteTests(unittest.TestCase):
    def test_telegram_owner_route_is_denied_when_agent_switch_is_unconfigured(
        self,
    ) -> None:
        with patch(
            "app.agent.owner_routes.config.get_bool",
            side_effect=lambda _key, default=False: default,
        ) as get_bool:
            allowed = telegram_owner_route_is_currently_authorized("tg:v1:100\x1f200")

        self.assertFalse(allowed)
        get_bool.assert_called_once_with("AGENT_ENABLED", False)

    def test_web_owner_is_stable_across_browser_sessions_and_irreversible(self) -> None:
        identity = "admin"
        first = web_kernel_owner(identity)
        self.assertEqual(first, web_kernel_owner(identity))
        self.assertNotEqual(first, web_kernel_owner("another-user"))
        self.assertNotIn(identity, first)
        self.assertRegex(first, r"^webk:v1:[0-9a-f]{64}$")

    def test_web_api_owner_uses_login_identity_not_csrf_session(self) -> None:
        with patch.object(
            agent_api.config,
            "web_credentials",
            return_value=("admin", "ignored-password"),
        ):
            first = agent_api._owner(object())
            second = agent_api._owner(object())

        self.assertEqual(first, second)
        self.assertEqual(first, web_kernel_owner("admin"))
        self.assertNotIn("ignored-password", first)

    def test_web_owner_rejects_empty_or_unbounded_identity(self) -> None:
        for identity in ("", " " * 3, "x" * 513):
            with (
                self.subTest(identity_length=len(identity)),
                self.assertRaises(ValueError),
            ):
                web_kernel_owner(identity)


if __name__ == "__main__":
    unittest.main()
