
from typing import Any
import asyncio

import pytest

from app.routes import agent_api
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime, api_client
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


@pytest.mark.parametrize(
    "case",
    [
        "anonymous",
        "csrf",
        "wrong_owner",
        "disabled",
        "rate",
        "stream_type",
        "message_type",
        "fields",
    ],
)
def test_web_explain_rejects_before_generation_or_model(agent_llm_enabled, case, monkeypatch):
    with isolated_test_database("explain-security.db"):
        runtime = NativeRuntime()
        with api_client(runtime, monkeypatch) as (client, owner):
            client.get("/_test_login")
            sid = "session_security_explain_001"
            approval = runtime.preview(owner, sid)
            before = asyncio.run(runtime.store.load(owner=owner, session_id=sid))
            frozen = asyncio.run(
                runtime.session.pipeline.get_pending_plan(owner=owner, session_id=sid)
            )
            headers = {"X-CSRF-Token": "explain-csrf"}
            payload: dict[str, Any] = {
                "session_id": sid,
                "plan_id": approval.plan_id,
                "message": "解释",
                "stream": False,
            }
            if case == "anonymous":
                client.cookies.clear()
            elif case == "csrf":
                headers.clear()
            elif case == "wrong_owner":
                monkeypatch.setattr(
                    agent_api.config,
                    "web_credentials",
                    lambda: ("another-account", "unused"),
                )
                # 重新签发第二个账户的会话，检验计划归属而非旧会话撤销。
                client.get("/_test_login")
            elif case == "disabled":
                monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: False)
            elif case == "rate":
                payload["plan_id"] = approval.plan_id[:-1] + (
                    "0" if approval.plan_id[-1] != "0" else "1"
                )
                for _ in range(12):
                    assert (
                        client.post(
                            "/api/agent/actions/explain", json=payload, headers=headers
                        ).status_code
                        == 409
                    )
            elif case == "stream_type":
                payload["stream"] = "true"
            elif case == "message_type":
                payload["message"] = ["explain"]
            elif case == "fields":
                payload["generation"] = 1
            response = client.post(
                "/api/agent/actions/explain", json=payload, headers=headers
            )
            if case == "wrong_owner":
                assert (
                    response.status_code == 409
                    and response.json()["error"]["code"] == "confirmation_invalid"
                )
            else:
                assert response.status_code in (
                    {401, 403}
                    if case in {"anonymous", "csrf"}
                    else {429}
                    if case == "rate"
                    else {400}
                )
            assert (
                asyncio.run(runtime.store.load(owner=owner, session_id=sid)) == before
            )
            assert (
                asyncio.run(
                    runtime.session.pipeline.get_pending_plan(
                        owner=owner, session_id=sid
                    )
                )
                == frozen
            )
            assert (
                len(runtime.wires) == 1
                and runtime.writes == []
                and len(runtime.prepares) == 1
            )
