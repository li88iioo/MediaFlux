"""真实本机 Chromium/Jinja：接力按钮与普通提示草稿共用唯一 query 入口。"""
from tests.test_agent_ux_shell_browser import AgentShellBrowserCase


class WorkspaceHandoffBrowserTests(AgentShellBrowserCase):
    def handoff_shell(self, viewport, *, stale=False):
        page, unexpected, errors = self.make_shell(viewport, history=False)
        page.evaluate("""stale => {
            window.__kernelConfig.nextActions = {snapshot_status: 'attention', actions: [{
                id: 'review_downloads', title: '检查下载队列', prompt: '诊断下载队列', description: '仅查看当前队列',
            }]};
            window.__kernelConfig.queryDelayMs = 60;
            window.__kernelConfig.queryEvents = [
                {type: 'turn.started', payload: {channel: 'web', generation: 1}},
                {type: 'tool.started', payload: {tool: 'downloads.diagnose', call_id: 'read-1', effect: 'read'}},
                {type: 'tool.completed', payload: {tool: 'downloads.diagnose', call_id: 'read-1', result: {ok: true, summary: '检查完成'}}},
                {type: 'turn.completed', payload: {status: 'success', answer: '当前队列检查完成。'}},
            ];
            window.__handoffBodies = [];
            const original = window.fetch;
            window.fetch = async (url, options = {}) => {
                if (new URL(String(url), location.href).pathname === '/api/agent/query') {
                    window.__handoffBodies.push(JSON.parse(options.body));
                    if (stale) return new Response(JSON.stringify({error: '该工作区行动已失效，请刷新下一步后重试'}), {
                        status: 409, headers: {'Content-Type': 'application/json'},
                    });
                }
                return original(url, options);
            };
        }""", stale)
        page.wait_for_function("document.querySelector('#agentStartActions').getAttribute('aria-busy') === 'false'")
        page.evaluate('async () => { await document.fonts.ready; }')
        self.assertEqual(page.locator('[data-agent-action-key="review_downloads"]').count(), 1)
        return page, unexpected, errors

    def test_direct_read_sends_key_once_preserves_draft_and_keeps_controls_stable(self):
        for width, height in ((320, 667), (390, 844), (768, 900), (1280, 800)):
            with self.subTest(width=width):
                page, unexpected, errors = self.handoff_shell({'width': width, 'height': height})
                page.locator('#agentPrompt').fill('保留这份草稿')
                button = page.locator('[data-agent-action-key="review_downloads"]')
                text_content = button.get_attribute('aria-label')
                assert text_content is not None
                self.assertIn('只读', text_content)
                draft = page.get_by_role('button', name='填入草稿：检查下载队列', exact=True)
                for control in (button, draft):
                    bounds = control.bounding_box()
                    assert bounds is not None
                    self.assertGreaterEqual(bounds['height'], 44)
                page.mouse.move(0, 0)
                composer = page.locator('#agentComposer').bounding_box()
                assert composer is not None
                draft.click()
                after = page.locator('#agentComposer').bounding_box()
                assert after is not None
                self.assertLessEqual(abs(composer['y'] - after['y']), .5)
                self.assertEqual(page.locator('#agentPrompt').input_value(), '保留这份草稿')
                page.locator('#agentPrompt').fill('')
                draft.click()
                self.assertEqual(page.locator('#agentPrompt').input_value(), '诊断下载队列')
                self.assertEqual(page.evaluate('window.__handoffBodies.length'), 0)
                page.locator('#agentPrompt').fill('保留这份草稿')
                button.focus()
                page.keyboard.press('Enter')
                button.evaluate("node => node.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
                page.wait_for_function('window.__handoffBodies.length === 1')
                body, = page.evaluate('window.__handoffBodies')
                self.assertEqual(body['action_key'], 'review_downloads')
                self.assertEqual(set(body), {'action_key', 'session_id', 'request_id', 'stream'})
                page.get_by_text('当前队列检查完成。', exact=False).first.wait_for()
                self.assertEqual(page.locator('#agentPrompt').input_value(), '保留这份草稿')
                self.assertEqual(page.locator('.agent-message-user').count(), 1)
                self.assertEqual(page.locator('.agent-message-assistant').count(), 1)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertEqual(unexpected, [])
                self.assertEqual(errors, [])

    def test_draft_entry_remains_a_normal_message_not_a_handoff(self):
        page, unexpected, errors = self.handoff_shell({'width': 1280, 'height': 800})
        page.get_by_role('button', name='填入草稿：检查下载队列', exact=True).click()
        page.locator('#agentSend').click()
        page.get_by_text('当前队列检查完成。', exact=False).first.wait_for()
        body, = page.evaluate('window.__handoffBodies')
        self.assertEqual(body['message'], '诊断下载队列')
        self.assertNotIn('action_key', body)
        self.assertEqual(page.locator('#agentPrompt').input_value(), '')
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])

    def test_stale_action_does_not_fall_back_to_prompt_or_duplicate_bubbles(self):
        page, unexpected, errors = self.handoff_shell({'width': 390, 'height': 844}, stale=True)
        page.locator('#agentPrompt').fill('未发送草稿')
        page.locator('[data-agent-action-key="review_downloads"]').click()
        page.get_by_text('该工作区行动已失效，请刷新下一步后重试', exact=False).first.wait_for()
        page.wait_for_function("document.querySelector('#agentStartActions').getAttribute('aria-busy') === 'false'")
        self.assertEqual(page.evaluate('window.__handoffBodies.length'), 1)
        self.assertEqual(page.locator('#agentPrompt').input_value(), '未发送草稿')
        self.assertEqual(page.locator('.agent-message-user').count(), 1)
        self.assertEqual(page.locator('.agent-message-assistant').count(), 1)
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])
