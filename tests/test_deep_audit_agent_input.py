"""TG 输入到 Kernel 的同一身份配置契约，不进行实际消息发送。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app import config
from app.agent.kernel.ports.mediaflux_policy import MediaFluxTurnAdmission
from app.agent.kernel.state_types import AgentInput
from app.agent.owner_routes import telegram_owner_route_is_currently_authorized
from app.bot.agent_adapter import (
    telegram_agent_access,
    telegram_agent_owner,
    telegram_agent_session_id,
)


@pytest.mark.parametrize("separator", [",", ";", "，", "；", " ", "\n", "\t"])
def test_normal_telegram_entry_and_kernel_share_allowed_user_syntax(separator):
    values = {
        "AGENT_LLM_ENABLED": "1",
        "TG_AGENT_ENABLED": "1",
        "TG_CHAT_ID": "-100123",
        "TG_AGENT_ALLOWED_USER_IDS": f"123{separator}456",
    }
    with (
        patch.object(
            config, "get", side_effect=lambda key, default="": values.get(key, default)
        ),
        patch.object(
            config,
            "get_bool",
            side_effect=lambda key, default=False: (
                values.get(key, "1" if default else "0") == "1"
            ),
        ),
    ):
        for user in ("123", "456"):
            assert telegram_agent_access("-100123", user) == "allowed"
            agent_input = AgentInput(
                message="查看任务状态",
                owner=telegram_agent_owner("-100123", user),
                session_id=telegram_agent_session_id("-100123", user),
                channel="telegram",
            )
            # 入口既然允许，Kernel 不能对同一白名单格式再作相反判断。
            assert isinstance(
                asyncio.run(MediaFluxTurnAdmission().begin(agent_input)), int
            )
            assert telegram_owner_route_is_currently_authorized(agent_input.owner)
        assert telegram_agent_access("-100123", "789") == "unauthorized"
        assert not telegram_owner_route_is_currently_authorized("tg:v1:-100123\x1f789")


def test_history_telegram_route_revalidates_revoked_user_without_cached_allow():
    values = {
        "AGENT_LLM_ENABLED": "1",
        "TG_AGENT_ENABLED": "1",
        "TG_CHAT_ID": "-100123",
        "TG_AGENT_ALLOWED_USER_IDS": "123，456",
    }
    owner = telegram_agent_owner("-100123", "123")
    with (
        patch.object(
            config, "get", side_effect=lambda key, default="": values.get(key, default)
        ),
        patch.object(config, "get_bool", return_value=True),
    ):
        assert telegram_owner_route_is_currently_authorized(owner)
        values["TG_AGENT_ALLOWED_USER_IDS"] = "456"
        assert not telegram_owner_route_is_currently_authorized(owner)
        assert telegram_agent_access("-100123", "123") == "unauthorized"
