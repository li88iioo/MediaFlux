from __future__ import annotations

import pytest

from app.agent.errors import AgentToolError
from app.agent.provider_actions import provider_capabilities_arguments


def test_provider_capability_limit_rejects_lossy_integer_values():
    for invalid in (True, 1.0, 1.9, "1.0", "1e3"):
        with pytest.raises(AgentToolError):
            provider_capabilities_arguments({"limit": invalid})
    assert provider_capabilities_arguments({"limit": "8"})["limit"] == 8
