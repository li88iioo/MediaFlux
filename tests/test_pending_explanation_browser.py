"""Actual Chromium click bridged to authenticated ASGI API, no network."""

import asyncio
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.support import isolated_test_database
from tests.test_agent_ux_shell_browser import AgentShellBrowserCase
from tests.test_pending_explanation_transports import NativeRuntime, api_client


class PendingExplanationBrowserTests(AgentShellBrowserCase):
    @pytest.mark.usefixtures("agent_llm_enabled")
    def test_real_click_sqlite_cold_same_card_confirm(self):
        with (
            isolated_test_database("explain-browser.db") as database_path,
            pytest.MonkeyPatch.context() as mp,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            runtime = NativeRuntime()
            search_result = re.search(
                r'<meta name="csrf-token" content="([^"]+)"', self.html
            )
            assert search_result is not None
            csrf = search_result.group(1)
            with api_client(runtime, mp, csrf) as (client, owner):
                client.get("/_test_login")
                session_id = "session_browser_explain_001"
                approval = executor.submit(runtime.preview, owner, session_id).result()
                frozen = executor.submit(
                    asyncio.run,
                    runtime.session.pipeline.get_pending_plan(
                        owner=owner, session_id=session_id
                    ),
                ).result()
                context = self.browser.new_context(
                    viewport={"width": 390, "height": 844}
                )
                self.addCleanup(context.close)
                unexpected = []
                errors = []
                requests = []

                def route(item):
                    url = urlsplit(item.request.url)
                    if url.netloc != "testserver":
                        unexpected.append(item.request.url)
                        item.abort()
                    elif url.path == "/agent":
                        item.fulfill(
                            status=200, content_type="text/html", body=self.html
                        )
                    elif url.path.startswith("/static/"):
                        asset = (
                            self.static_root / url.path.removeprefix("/static/")
                        ).resolve()
                        assert asset.is_relative_to(self.static_root)
                        if asset.is_file():
                            item.fulfill(
                                status=200,
                                content_type=mimetypes.guess_type(str(asset))[0]
                                or "application/octet-stream",
                                body=asset.read_bytes(),
                            )
                        else:
                            item.fulfill(status=404, body="")
                    elif url.path.startswith("/api/agent/"):
                        requests.append(
                            (item.request.method, url.path, item.request.post_data)
                        )
                        response = client.request(
                            item.request.method,
                            url.path + ("?" + url.query if url.query else ""),
                            content=item.request.post_data_buffer,
                            headers={
                                k: v
                                for k, v in item.request.headers.items()
                                if k.lower() in ("content-type", "x-csrf-token")
                            },
                        )
                        item.fulfill(
                            status=response.status_code,
                            content_type=response.headers.get(
                                "content-type", "application/json"
                            ),
                            body=response.content,
                        )
                    else:
                        item.fulfill(status=404, body="")

                context.route("**/*", route)
                context.route_web_socket("**/*", lambda socket: socket.close())
                context.add_init_script(
                    f"localStorage.setItem('mediaflux.agent.kernel.session.v1', '{session_id}');"
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://testserver/agent")
                page.locator("[data-effect-explain]").wait_for()
                page.locator("[data-effect-explain]").click()
                evidence = database_path.parent / "screens"
                evidence.mkdir(exist_ok=True)
                for width, height in [(360, 667), (393, 852), (844, 390)]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                    )
                    page.get_by_label("解释问题").scroll_into_view_if_needed()
                    page.screenshot(
                        path=str(evidence / f"explain-{width}x{height}.png")
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_label("解释问题").fill("这张卡有什么影响？不修改计划。")
                page.get_by_role("button", name="发送解释问题", exact=True).click()
                page.locator("[data-effect-confirm]:not([disabled])").wait_for()
                assert any(
                    path == "/api/agent/actions/explain" for _, path, _ in requests
                )
                assert runtime.writes == [] and len(runtime.prepares) == 1
                runtime.reopen()
                page.reload()
                page.locator("[data-effect-confirm]:not([disabled])").wait_for()
                assert (
                    executor.submit(
                        asyncio.run,
                        runtime.session.pipeline.get_pending_plan(
                            owner=owner, session_id=session_id
                        ),
                    ).result()
                    == frozen
                )
                assert (
                    page.locator("[data-effect-confirm]").get_attribute(
                        "data-effect-confirm"
                    )
                    == approval.plan_id
                )
                page.locator("[data-effect-confirm]").click()
                page.wait_for_function(
                    "!document.querySelector('[data-effect-confirm]')"
                )
                assert runtime.writes == [({"step": 1}, "frozen-1")]
                assert not unexpected and not errors
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                )
