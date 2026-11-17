"""Production Jinja/JS driven by real native and cold SQLite snapshots."""

import json
import os
from pathlib import Path

from tests.test_agent_task_consumers import native_snapshots
from tests.test_agent_ux_shell_browser import AgentShellBrowserCase
from tests import test_agent_kernel_browser as rail_support


class TaskConsumerBrowserTests(AgentShellBrowserCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshots = native_snapshots._get_wrapped_function()()
        super().setUpClass()

    def configured_shell(self, config, session, viewport=None):
        from tests.test_agent_kernel_browser import MOCK_FETCH

        page, unexpected, errors = self.make_shell(
            viewport or {"width": 900, "height": 700}, history=False
        )
        page.context.add_init_script(f"""({MOCK_FETCH})({json.dumps(config, ensure_ascii=False)});
            localStorage.setItem('mediaflux.agent.kernel.session.v1',{json.dumps(session)});
            localStorage.setItem('mediaflux.agent.kernel.session.v1.'+'a'.repeat(64),{json.dumps(session)});""")
        page.reload()
        return page, unexpected, errors

    def test_empty_snapshot_keeps_three_rows_and_same_node_lifecycle(self):
        from tests.test_agent_kernel_browser import _event

        session = "consumer-empty-session-0001"
        partial = self.snapshots[0]["api"]["task"]
        cases = (
            ("missing", {}, "untracked", "", 900, 700),
            ("null", {"task": None}, "untracked", "", 900, 700),
            ("unknown", {"task": {}}, "unknown", "任务进度待核对", 900, 700),
            ("partial", {"task": partial}, "partial", "1 项待核验", 360, 667),
        )
        for name, fields, status, label, width, height in cases:
            with self.subTest(snapshot=name):
                config = {
                    "sessions": {"draft_scope": "a" * 64, "sessions": []},
                    "sessionDetails": {session: {"messages": [], **fields}},
                    "nextActions": {"actions": [
                        {"id": f"action-{i}", "title": f"查看待办 {i}",
                         "description": "仅查看本地记录", "prompt": f"查看待办 {i}"}
                        for i in range(3)
                    ]},
                    "queryEvents": [_event(1, "turn.completed", {"status": "success", **fields})],
                }
                page, unexpected, errors = self.configured_shell(
                    config, session, {"width": width, "height": height}
                )
                page.wait_for_function(
                    "id=>window.__kernelCalls.some(c=>c.url==='/api/agent/sessions/'+id) && document.querySelector('#agentRestoreNotice').hidden",
                    arg=session,
                )
                if status != "untracked":
                    page.wait_for_function(
                        "status=>document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus===status",
                        arg=status,
                    )
                page.wait_for_function(
                    "document.querySelector('#agentStartActions').getAttribute('aria-busy')==='false'"
                )
                page.evaluate("async()=>{await document.fonts.ready}")
                panel = page.locator("#agentTaskSnapshot")
                if status == "untracked":
                    self.assertEqual(panel.count(), 0)
                else:
                    self.assertTrue(panel.is_visible())
                    self.assertIn(label, panel.inner_text())
                    self.assertFalse(panel.evaluate("node => node.open"))
                geometry = page.evaluate("""() => {
                    const rect = selector => document.querySelector(selector)?.getBoundingClientRect().toJSON();
                    window.__taskNode = document.querySelector('#agentTaskSnapshot');
                    return {rows:getComputedStyle(document.querySelector('.agent-console')).gridTemplateRows,
                        panel:rect('#agentTaskSnapshot'), composer:rect('#agentComposer'),
                        console:rect('.agent-console'), auxiliary:rect('.agent-start-panel'),
                        width:document.documentElement.clientWidth, scrollWidth:document.documentElement.scrollWidth,
                        parent:window.__taskNode?.parentElement.className};
                }""")
                evidence = os.environ.get("CONSUMER_EVIDENCE")
                if evidence:
                    Path(evidence, f"empty-{name}.json").write_text(json.dumps(geometry, indent=2))
                    page.screenshot(path=str(Path(evidence, f"empty-{name}.png")))
                self.assertEqual(len(geometry["rows"].split()), 3, geometry)
                if status != "untracked":
                    self.assertIn("agent-start-panel", geometry["parent"])
                self.assertLessEqual(abs(
                    geometry["composer"]["y"] + geometry["composer"]["height"] / 2
                    - geometry["console"]["y"] - geometry["console"]["height"] / 2
                ), 0.5)
                self.assertLessEqual(geometry["scrollWidth"], geometry["width"])
                if status != "untracked":
                    self.assertGreaterEqual(geometry["panel"]["top"], geometry["composer"]["bottom"])
                    self.assertLessEqual(geometry["panel"]["bottom"], geometry["auxiliary"]["bottom"])
                self.assertLessEqual(geometry["composer"]["bottom"], height)
                self.assertEqual(page.locator("[data-agent-action-key]").count(), 3)
                draft = page.get_by_role("button", name="填入草稿：查看待办 2", exact=True)
                draft.scroll_into_view_if_needed()
                draft.click()
                self.assertEqual(page.locator("#agentPrompt").input_value(), "查看待办 2")
                self.assertEqual(page.evaluate(
                    "window.__kernelCalls.filter(call=>call.url==='/api/agent/query').length"
                ), 0)
                page.locator("#agentSend").click()
                page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                if status == "untracked":
                    self.assertEqual(panel.count(), 0)
                else:
                    self.assertTrue(page.evaluate("""() => window.__taskNode === document.querySelector('#agentTaskSnapshot')
                        && window.__taskNode.nextElementSibling === document.querySelector('#agentComposer')"""))
                    self.assertTrue(panel.is_visible())
                rail_support.open_sessions(page)
                page.locator("#agentNewSession").click()
                self.assertEqual(panel.count(), 0)
                self.assertTrue(page.evaluate("!window.__taskNode?.isConnected"))
                self.assertEqual(errors, [])
                self.assertEqual(unexpected, [])

    def test_candidate_preview_and_active_poll_consume_snapshot(self):
        from tests.test_agent_kernel_browser import _event
        from tests.test_agent_ux_browser import active_turn, candidate_view

        session = "consumer-browser-session-0001"
        task = self.snapshots[0]["api"]["task"]
        complete = self.snapshots[1]["api"]["task"]
        candidate = candidate_view()
        approval = {
            "plan_id": "plan_consumer_preview_0001",
            "effect": "WRITE",
            "preview": {"summary": "只预览，等待确认"},
            "confirmation": {},
        }
        config = {
            "sessions": {"draft_scope": "a" * 64, "sessions": []},
            "sessionDetails": {
                session: {
                    "task": task,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "候选",
                            "candidate_view": candidate,
                        }
                    ],
                    "candidate_view": candidate,
                }
            },
            "queryEvents": [
                _event(1, "turn.started"),
                _event(2, "effect.approval_required", {"plan": approval}),
                _event(
                    3,
                    "turn.completed",
                    {"status": "approval_required", "task": complete},
                ),
            ],
        }
        page, unexpected, errors = self.configured_shell(config, session)
        page.locator(".agent-candidate-select:not([disabled])").first.click()
        page.wait_for_function(
            "document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='complete'"
        )
        self.assertTrue(page.locator("[data-effect-confirm]").is_enabled())
        self.assertIn("只预览", page.locator(".agent-confirmation-card").inner_text())
        self.assertEqual(
            page.evaluate(
                "window.__kernelCalls.filter(c=>c.url==='/api/agent/actions/confirm').length"
            ),
            0,
        )
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])
        current = active_turn("consumer-active-request", "consumer-active-turn")
        config["sessionDetails"][session] = {
            "task": task,
            "messages": [{"role": "user", "content": "后台问题"}],
            "active_turn": current,
        }
        page, unexpected, errors = self.configured_shell(config, session)
        page.wait_for_function(
            "document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='partial'"
        )
        page.evaluate(
            'window.__keptNode=document.querySelector(".agent-message-assistant")'
        )
        page.evaluate(
            "({id,task})=>window.__kernelConfig.sessionDetails[id].task=task",
            {"id": session, "task": complete},
        )
        page.wait_for_function(
            "document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='complete'",
            timeout=6000,
        )
        self.assertTrue(
            page.evaluate(
                'window.__keptNode===document.querySelector(".agent-message-assistant")'
            )
        )
        page.evaluate(
            """({id,current})=>Object.assign(window.__kernelConfig.sessionDetails[id],{
            active_turn:null,last_turn:{...current,status:'completed'},messages:[{role:'assistant',content:'后台回答结束'}]})""",
            {"id": session, "current": current},
        )
        page.wait_for_function(
            "document.querySelector('#agentResponseStatus').textContent.includes('本轮回复已结束')",
            timeout=6000,
        )
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_delayed_old_get_cannot_replace_switched_or_reset_panel(self):
        a, b = "consumer-browser-session-0001", "consumer-browser-session-0002"
        task = self.snapshots[0]["api"]["task"]
        complete = self.snapshots[1]["api"]["task"]
        for reset in (False, True):
            config = {
                "sessions": {
                    "draft_scope": "a" * 64,
                    "sessions": [
                        {"session_id": a, "title": "A"},
                        {"session_id": b, "title": "B"},
                    ],
                },
                "sessionDetails": {
                    a: {
                        "task": task,
                        "messages": [{"role": "assistant", "content": "A内容"}],
                    },
                    b: {
                        "task": complete,
                        "messages": [{"role": "assistant", "content": "B内容"}],
                    },
                },
            }
            page, unexpected, errors = self.configured_shell(config, b)
            page.wait_for_function(
                "document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='complete'"
            )
            page.evaluate(
                """id=>{const fetch=window.fetch;window.fetch=async(url,options)=>{
                const result=await fetch(url,options);
                if(String(url).endsWith('/'+id)){window.__held=true;await new Promise(resolve=>window.__release=resolve);}
                return result;};}""",
                a,
            )
            rail_support.open_sessions(page)
            page.locator(f'[data-session-open="{a}"]').click()
            page.wait_for_function("window.__held===true")
            # A 的响应未返回时，当前会话和任务面板都应属于 B。
            self.assertEqual(page.evaluate("localStorage.getItem('mediaflux.agent.kernel.session.v1')"), b)
            self.assertEqual(page.locator("#agentTaskSnapshot").get_attribute("data-task-status"), "complete")
            if reset:
                page.locator("#agentNewSession").evaluate("(n)=>n.click()")
            else:
                page.locator(f'[data-session-open="{b}"]').click()
                page.wait_for_function(
                    "document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='complete'"
                )
            page.evaluate("window.__release()")
            page.evaluate(
                "()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))"
            )
            if reset:
                self.assertEqual(page.locator("#agentTaskSnapshot").count(), 0)
            else:
                self.assertEqual(
                    page.locator("#agentTaskSnapshot").get_attribute(
                        "data-task-status"
                    ),
                    "complete",
                )
            self.assertEqual(errors, [])
            self.assertEqual(unexpected, [])

    def test_reused_partial_turn_complete_task_and_unknown_rendering(self):
        from tests.test_agent_kernel_browser import _event

        captured = json.loads(
            (
                Path(__file__).parent / "fixtures/task_complete_turn_partial.json"
            ).read_text()
        )
        page, unexpected, errors = self.make_shell(
            {"width": 360, "height": 667}, history=False
        )
        for task, label in (
            (None, None),
            ({"status": "complete", "root_goal": "DO NOT DISPLAY"}, "任务进度待核对"),
            (captured["payload"]["task"], "已核验全部检查项"),
        ):
            payload = {**captured["payload"], "task": task}
            page.evaluate(
                "(events)=>window.__kernelConfig.queryEvents=events",
                [_event(1, "turn.completed", payload)],
            )
            page.locator("#agentPrompt").fill("继续核对")
            page.locator("#agentSend").click()
            page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
            if label is None:
                self.assertEqual(page.locator("#agentTaskSnapshot").count(), 0)
            else:
                page.wait_for_function(
                    '(label)=>document.querySelector("#agentTaskSnapshot")?.textContent.includes(label)',
                    arg=label,
                )
                text_content = page.locator("#agentTaskSnapshot").text_content()
                assert text_content is not None
                self.assertNotIn("DO NOT DISPLAY", text_content)
            self.assertIn("部分完成", page.locator("#agentTranscript").inner_text())
        page.evaluate("window.__kernelConfig.sessions.draft_scope='b'.repeat(64)")
        rail_support.open_sessions(page)
        page.wait_for_function('document.querySelector("#agentTaskSnapshot")===null')
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_stream_cold_complete_and_session_scope(self):
        for width, height in ((360, 667), (667, 360)):
            page, unexpected, errors = self.make_shell(
                {"width": width, "height": height}, history=False
            )
            first, second, qa = self.snapshots
            page.evaluate(
                "(events) => window.__kernelConfig.queryEvents=events", first["events"]
            )
            page.locator("#agentPrompt").fill("观察 TMDB 和 qBittorrent")
            page.locator("#agentSend").click()
            page.wait_for_function(
                "document.querySelector('#agentTranscript').textContent.includes('全部任务完成') && document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'"
            )
            panel = page.locator("#agentTaskSnapshot")
            self.assertEqual(
                panel.count(), 1, "A committed session task needs its own panel"
            )
            self.assertTrue(panel.is_visible())
            self.assertIn("1 项待核验", panel.inner_text())
            self.assertNotIn("观察配置 qbittorrent", panel.inner_text())
            panel.locator("summary").click()
            self.assertIn("观察配置 qbittorrent", panel.inner_text())
            partial_geometry = page.evaluate("""() => ({text:document.querySelector('#agentTaskSnapshot').textContent,
                width:document.documentElement.clientWidth, scrollWidth:document.documentElement.scrollWidth,
                panel:document.querySelector('#agentTaskSnapshot').getBoundingClientRect().toJSON(),
                composer:document.querySelector('#agentComposer').getBoundingClientRect().toJSON()})""")
            self.assertLessEqual(partial_geometry["scrollWidth"], width)
            self.assertLessEqual(
                partial_geometry["panel"]["bottom"], partial_geometry["composer"]["top"]
            )
            self.assertLessEqual(partial_geometry["composer"]["bottom"], height + 1)
            evidence = os.environ.get("CONSUMER_EVIDENCE")
            if evidence:
                Path(evidence, f"partial-geometry-{width}-{height}.json").write_text(
                    json.dumps(partial_geometry, ensure_ascii=False, indent=2)
                )
                page.screenshot(
                    path=str(Path(evidence, f"partial-{width}-{height}.png"))
                )
            session = page.evaluate(
                "JSON.parse(window.__kernelCalls.find(c=>c.url==='/api/agent/query').body).session_id"
            )
            config = page.evaluate("window.__kernelConfig")
            config["sessionDetails"] = {
                session: {
                    **first["api"],
                    "session_id": session,
                    "draft_scope": "a" * 64,
                }
            }
            config["sessions"] = {
                "draft_scope": "a" * 64,
                "sessions": [
                    {"session_id": session, "title": "Native task", "message_count": 2}
                ],
            }
            from tests.test_agent_kernel_browser import MOCK_FETCH

            page.context.add_init_script(
                f"({MOCK_FETCH})({json.dumps(config, ensure_ascii=False)});"
            )
            page.reload()
            page.wait_for_function(
                "document.querySelector('#agentTaskSnapshot')?.textContent.includes('项待核验')"
            )
            self.assertEqual(panel.count(), 1)
            page.evaluate(
                "(events) => window.__kernelConfig.queryEvents=events", second["events"]
            )
            page.locator("#agentPrompt").fill("继续")
            page.locator("#agentSend").click()
            page.wait_for_function(
                "document.querySelector('#agentTaskSnapshot')?.textContent.includes('已核验全部检查项')"
            )
            self.assertNotIn("会话声明任务快照", panel.inner_text())
            # A new same-session question retains the committed session snapshot.
            page.evaluate(
                "(events) => {window.__kernelConfig.queryEvents=events; window.__kernelConfig.queryDelayMs=25}",
                qa["events"],
            )
            page.locator("#agentPrompt").fill("一个无关问题")
            page.locator("#agentSend").click()
            self.assertIn("已核验全部检查项", panel.inner_text())
            page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
            self.assertIn("已核验全部检查项", panel.inner_text())
            self.assertNotIn(
                "Media Agent 已完成", page.locator("#agentResponseStatus").inner_text()
            )
            geometry = page.evaluate("""() => ({width:document.documentElement.clientWidth,
                scrollWidth:document.documentElement.scrollWidth, height:innerHeight,
                panel:document.querySelector('#agentTaskSnapshot').getBoundingClientRect().toJSON(),
                composer:document.querySelector('#agentComposer').getBoundingClientRect().toJSON()})""")
            self.assertLessEqual(geometry["scrollWidth"], geometry["width"])
            self.assertLessEqual(
                geometry["panel"]["bottom"], geometry["composer"]["top"]
            )
            self.assertLessEqual(geometry["composer"]["bottom"], height + 1)
            evidence = os.environ.get("CONSUMER_EVIDENCE")
            if evidence:
                Path(evidence, f"geometry-{width}-{height}.json").write_text(
                    json.dumps(geometry, indent=2)
                )
                page.screenshot(
                    path=str(Path(evidence, f"populated-{width}-{height}.png"))
                )
            self.assertEqual(errors, [])
            self.assertEqual(unexpected, [])
