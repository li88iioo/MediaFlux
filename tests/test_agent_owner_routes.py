"""Agent owner 派生与低层路由边界测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.owner_routes import (
    telegram_owner_route_is_currently_authorized,
    web_agent_owner,
)


class AgentOwnerRouteTests(unittest.TestCase):
    def test_telegram_owner_route_is_denied_when_agent_switch_is_unconfigured(self) -> None:
        with patch(
            "app.agent.owner_routes.config.get_bool",
            side_effect=lambda _key, default=False: default,
        ) as get_bool:
            allowed = telegram_owner_route_is_currently_authorized(
                "tg:v1:100\x1f200"
            )

        self.assertFalse(allowed)
        get_bool.assert_called_once_with("AGENT_ENABLED", False)

    def test_web_owner_is_stable_irreversible_and_session_scoped(self) -> None:
        csrf = "raw-csrf-owner"
        first = web_agent_owner(csrf, session_id="sessionOwner0001")
        second = web_agent_owner(csrf, session_id="sessionOwner0002")
        self.assertEqual(
            first, web_agent_owner(csrf, session_id="sessionOwner0001")
        )
        self.assertNotEqual(first, second)
        self.assertNotIn(csrf, first)

    def test_web_owner_rejects_invalid_session_ids_with_value_error(self) -> None:
        for session_id in ("short", "含中文的session编号0001", "session with space"):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                web_agent_owner("csrf", session_id=session_id)


if __name__ == "__main__":
    unittest.main()
