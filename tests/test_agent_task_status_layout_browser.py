"""任务进度只占一行；真实外壳/本地资源下检查刷新、折叠与窄屏布局。"""
import copy
import json
import os
from pathlib import Path

from tests.test_agent_kernel_browser import MOCK_FETCH, _event
from tests.test_agent_task_consumer_controls import shape
from tests.test_agent_ux_shell_browser import AgentShellBrowserCase


class TaskStatusLayoutBrowserTests(AgentShellBrowserCase):
    def test_narrow_toolbar_keeps_touch_targets_and_restore_error_reachable(self):
        from tests.test_agent_restore_browser import AgentRestoreBrowserTests

        for width in (320, 360, 390):
            with self.subTest(width=width):
                page, _, errors, unexpected = AgentRestoreBrowserTests.restore_page(
                    self, viewport={"width": width, "height": 300}, fail=True
                )
                page.locator("#agentRestoreNew").wait_for(state="visible")
                notice = page.locator("#agentRestoreNotice").bounding_box()
                assert notice is not None
                composer = page.locator("#agentComposer").bounding_box()
                assert composer is not None
                self.assertLessEqual(notice["y"] + notice["height"] + 4, composer["y"])
                page.locator("#agentRestoreNew").focus()
                button = page.locator("#agentRestoreNew").bounding_box()
                assert button is not None
                self.assertLessEqual(button["y"] + button["height"], notice["y"] + notice["height"] + 0.5)
                controls = page.locator(".agent-composer-header :is(a, button):visible")
                self.assertEqual(controls.count(), 4)
                self.assertTrue(page.locator("#agentSend").is_hidden())
                self.assertFalse(page.locator("#agentStopSession").is_visible())
                boxes = []
                for control in controls.all():
                    box = control.bounding_box()
                    assert box is not None
                    boxes.append(box)
                for box in boxes:
                    self.assertGreaterEqual(box["width"], 44)
                    self.assertGreaterEqual(box["height"], 44)
                    self.assertAlmostEqual(box["y"], boxes[0]["y"], delta=0.5)
                self.assertEqual(page.locator("#agentResumeLatestSession").get_attribute("aria-label"), "继续上次会话")
                page.locator("#agentPrompt").click(position={"x": 20, "y": 8})
                self.assertEqual(page.evaluate("document.activeElement.id"), "agentPrompt")
                self.assertEqual(page.locator(".agent-console").evaluate("n=>n.scrollLeft"), 0)
                self.assertEqual(errors, [])
                self.assertEqual(unexpected, [])

    def test_task_summary_alignment_keyboard_and_repeated_updates(self):
        for width, height in ((320, 667), (360, 667), (768, 900), (1440, 900), (667, 360)):
            with self.subTest(width=width, height=height):
                page, unexpected, errors = self.make_shell(
                    {"width": width, "height": height}, history=False
                )
                task = shape()
                task["root_goal"] = "核对媒体任务" * 80
                task["blockers"] = [{"text": "等待人工核对" * 70} for _ in range(3)]
                session = "layout-task-session-0001"
                config = {
                    "sessions": {"draft_scope": "a" * 64, "sessions": [
                        {"session_id": session, "title": "媒体任务"}
                    ]},
                    "sessionDetails": {session: {
                        "task": task, "messages": [{"role": "assistant", "content": "已读取检查项。"}]
                    }},
                }
                # 保留真实 Lucide 渲染，不用图标替身验证布局。
                mock = MOCK_FETCH.replace("window.renderLucideIcons = () => {};", "")
                page.context.add_init_script(f"""({mock})({json.dumps(config, ensure_ascii=False)});
                    localStorage.setItem('mediaflux.agent.kernel.session.v1.'+'a'.repeat(64), '{session}');""")
                page.reload()
                panel = page.locator("#agentTaskSnapshot")
                summary = panel.locator("summary")
                page.wait_for_function("document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='partial'")
                page.evaluate("async()=>{await document.fonts.ready}")
                before = page.locator("#agentComposer").bounding_box()
                assert before is not None
                bounds = panel.bounding_box()
                assert bounds is not None
                self.assertAlmostEqual(bounds["x"], before["x"], delta=0.5)
                self.assertAlmostEqual(bounds["width"], before["width"], delta=0.5)
                self.assertTrue(page.locator(".agent-composer-header").evaluate(
                    "n=>n.scrollWidth<=n.clientWidth"
                ))
                self.assertFalse(panel.evaluate("n=>n.open"))
                self.assertEqual(panel.inner_text().strip(), "1 项待核验")
                summary.focus()
                page.keyboard.press("Enter")
                self.assertTrue(panel.evaluate("n=>n.open"))
                self.assertIn("目标：", panel.inner_text())
                self.assertIn("不代表其他后续操作已完成", panel.inner_text())
                page.evaluate("""() => {
                    window.__panel = document.querySelector('#agentTaskSnapshot');
                    window.__summary = window.__panel.firstElementChild;
                    const body=window.__panel.lastElementChild;
                    body.scrollTop=50;
                }""")
                for index in range(6):
                    updated = copy.deepcopy(task)
                    updated["revision"] += index + 1
                    updated["status"] = "blocked" if index % 2 else "partial"
                    page.evaluate("events=>window.__kernelConfig.queryEvents=events", [
                        _event(1, "turn.completed", {"status": "partial", "task": updated})
                    ])
                    page.locator("#agentPrompt").fill("核对进度")
                    page.locator("#agentSend").click()
                    page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                    self.assertTrue(page.evaluate("""() => {
                        const p=document.querySelector('#agentTaskSnapshot');
                        return p===window.__panel && p.firstElementChild===window.__summary && p.open;
                    }"""))
                    self.assertEqual(panel.locator(".agent-task-details").evaluate("n=>n.scrollTop"), 50)
                    after = page.locator("#agentComposer").bounding_box()
                    assert after is not None
                    self.assertEqual(page.locator(".agent-console").evaluate("n=>n.scrollLeft"), 0)
                    for key in ("x", "y", "width", "height"):
                        self.assertAlmostEqual(before[key], after[key], delta=0.5,
                                               msg={"dimension": key, "before": before, "after": after})
                summary.focus()
                page.keyboard.press("Space")
                self.assertFalse(panel.evaluate("n=>n.open"))
                self.assertTrue(summary.evaluate("n=>n===document.activeElement"))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                bounds = page.locator("#agentComposer").bounding_box()
                assert bounds is not None
                self.assertLessEqual(bounds["y"] + before["height"], height)
                bounds = page.locator("#agentTranscript").bounding_box()
                assert bounds is not None
                self.assertGreater(bounds["height"], 30)
                evidence = os.environ.get("CONSUMER_EVIDENCE")
                if evidence:
                    page.screenshot(path=str(Path(evidence, f"task-summary-{width}-{height}.png")))
                # 刷新不自动展开；无任务的新终态会移除旧摘要。
                page.reload()
                page.wait_for_function("document.querySelector('#agentTaskSnapshot')?.dataset.taskStatus==='partial'")
                self.assertFalse(panel.evaluate("n=>n.open"))
                page.evaluate("events=>window.__kernelConfig.queryEvents=events", [
                    _event(1, "turn.completed", {"status": "success", "task": None})
                ])
                page.locator("#agentPrompt").fill("你是谁")
                page.locator("#agentSend").click()
                page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                self.assertEqual(panel.count(), 0)
                self.assertNotIn("任务验收", page.locator(".agent-console").inner_text())
                self.assertEqual(unexpected, [])
                self.assertEqual(errors, [])

    def test_only_transcript_scrolls_in_long_conversations(self):
        for width, height in ((360, 667), (768, 900), (1440, 900), (2000, 1100)):
            with self.subTest(width=width):
                page, unexpected, errors = self.make_shell(
                    {"width": width, "height": height}, history=False
                )
                page.evaluate("events=>window.__kernelConfig.queryEvents=events", [
                    _event(1, "turn.completed", {
                        "status": "success", "answer": "\n\n".join(f"第 {i} 条媒体记录：这是长对话滚动验证。" for i in range(80))
                    })
                ])
                page.locator("#agentPrompt").fill("查看媒体记录")
                page.locator("#agentSend").click()
                page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                for collapsed in ((False, True, False) if width > 900 else (False,)):
                    sidebar = "collapsed" if collapsed else "expanded"
                    if page.evaluate("document.documentElement.dataset.sidebar") != sidebar:
                        page.locator("#collapseSidebar").click()
                        page.wait_for_function("state=>document.documentElement.dataset.sidebar===state", arg=sidebar)
                        page.evaluate("() => Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})))")
                    metrics = page.evaluate("""() => {
                        const root=document.scrollingElement, t=document.querySelector('#agentTranscript');
                        const ancestors=[];
                        for(let n=t.parentElement;n;n=n.parentElement) ancestors.push({
                            tag:n.tagName, cls:n.className, overflow:getComputedStyle(n).overflowY,
                            height:n.clientHeight, scrollHeight:n.scrollHeight
                        });
                        return {overflow:getComputedStyle(root).overflowY, rootHeight:root.clientHeight,
                            rootScrollHeight:root.scrollHeight, transcriptHeight:t.clientHeight,
                            transcriptScrollHeight:t.scrollHeight, ancestors,
                            transcriptRight:t.getBoundingClientRect().right,
                            viewportWidth:document.documentElement.clientWidth,
                            contentPaddingRight:getComputedStyle(document.querySelector('.main > .content')).paddingRight};
                    }""")
                    self.assertAlmostEqual(metrics["transcriptRight"], metrics["viewportWidth"], delta=0.5, msg=metrics)
                    self.assertEqual(metrics["contentPaddingRight"], "0px", metrics)
                    self.assertEqual(metrics["overflow"], "hidden", metrics)
                    self.assertLessEqual(metrics["rootScrollHeight"], metrics["rootHeight"], metrics)
                    self.assertGreater(metrics["transcriptScrollHeight"], metrics["transcriptHeight"])
                    for ancestor in metrics["ancestors"]:
                        self.assertNotEqual(ancestor["overflow"], "scroll", ancestor)
                        self.assertLessEqual(ancestor["scrollHeight"], ancestor["height"] + 1, ancestor)
                    before = page.locator("#agentComposer").bounding_box()
                    assert before is not None
                    page.locator("#agentTranscript").evaluate("n=>n.scrollTop=0")
                    page.locator("#agentTranscript").hover()
                    page.mouse.wheel(0, 450)
                    page.wait_for_function("document.querySelector('#agentTranscript').scrollTop>0")
                    self.assertEqual(page.evaluate("window.scrollY"), 0)
                    after = page.locator("#agentComposer").bounding_box()
                    assert after is not None
                    for key in ("x", "y", "width", "height"):
                        self.assertAlmostEqual(before[key], after[key], delta=0.5)
                    evidence = os.environ.get("CONSUMER_EVIDENCE")
                    if evidence:
                        page.screenshot(path=str(Path(evidence, f"single-scroll-{width}-{collapsed}.png")))
                self.assertEqual(unexpected, [])
                self.assertEqual(errors, [])
