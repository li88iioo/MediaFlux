"""等待刷新页面：真实 Jinja/Chromium，全部网络在内存边界替换。"""
from __future__ import annotations

import mimetypes
import os
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright
from tests.test_ui_edit_session_recovery_browser import REPLY_GATE

ROOT = Path(__file__).resolve().parents[1]
QUEUE = '/api/media-libraries/refresh-queue'


@unittest.skipIf(sync_playwright is None, '未安装 Playwright')
class MediaLibrariesRefreshBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest('未找到 Chromium')
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        cls.addClassCleanup(cls.browser.close)
        env = Environment(loader=FileSystemLoader(ROOT / 'app/templates'), autoescape=True)
        cls.html = env.get_template('media_libraries.html').render(
            active='media_libraries', app_version='offline-refresh-test',
            discovery_enabled=False, agent_enabled=False, csrf_token=lambda: 'fixture-csrf',
            static_url=lambda value: '/static/' + value, url_for=lambda name: '/' + name.split('.')[-1],
        )

    def test_selected_paths_stale_response_draft_and_mobile_layout(self):
        for width in (360, 1280):
            with self.subTest(width=width), self.browser.new_context(
                viewport={'width': width, 'height': 1000}, service_workers='block', reduced_motion='reduce',
            ) as context:
                old = '/旧路径/' + '很长的目录名称' * 15 + '/<img src=x onerror="window.xss=true">.strm'
                current = '/整理/需要补映射/Season 1/E01.strm'
                waiting = {'paths': [old, current], 'token': 'v1', 'conflict': False}
                errors, requests, unexpected = [], [], []
                overview = {
                    'servers': [{'server_type': 'jellyfin', 'label': 'Jellyfin', 'enabled': True, 'configured': True,
                                 'mapping_key': 'JELLYFIN_PATH_MAPPINGS', 'mappings': [{'local': '/strm', 'server': '//NAS/STRM'}],
                                 'libraries': [{'id': 'tv', 'name': '剧集', 'locations': ['//NAS/STRM']}]}],
                    'local_bindings': [], 'local_sources': [], 'strm': {'output_root': '/strm', 'sources': []},
                }

                def route_request(route):
                    request = route.request
                    url = urlsplit(request.url)
                    path = unquote(url.path)
                    if url.netloc != 'offline.invalid':
                        unexpected.append(request.url)
                        route.abort()
                    elif path == '/media-libraries':
                        route.fulfill(body=self.html, content_type='text/html')
                    elif path.startswith('/static/'):
                        asset = (ROOT / 'app' / path.lstrip('/')).resolve()
                        if asset.is_relative_to(ROOT / 'app/static') and asset.is_file():
                            route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream')
                        else:
                            route.fulfill(status=404, body='')
                    elif path == '/api/config':
                        route.fulfill(json={})
                    elif path == '/api/media-libraries/overview':
                        route.fulfill(json=overview)
                    elif path == QUEUE:
                        items = [] if not waiting['paths'] else [{
                            'group_key': 'group-jellyfin', 'provider': 'jellyfin', 'token': waiting['token'],
                            'path_count': len(waiting['paths']), 'paths': waiting['paths'], 'reason': '路径未匹配媒体库',
                        }]
                        route.fulfill(json={'items': items, 'summary': {'waiting_configuration': len(items), 'waiting_paths': len(waiting['paths'])}})
                    elif path in (QUEUE + '/clear', QUEUE + '/retry'):
                        body = request.post_data_json
                        requests.append((path, body))
                        if waiting['conflict']:
                            waiting.update(conflict=False, token='changed')
                            route.fulfill(status=409, json={'error': '刷新记录已变化'})
                        else:
                            selected = body['records'][0]['paths']
                            waiting['paths'] = [p for p in waiting['paths'] if p not in selected]
                            waiting['token'] += '-next'
                            route.fulfill(json={'success': True, 'processed_groups': 1, 'processed_paths': len(selected)})
                    else:
                        unexpected.append(path)
                        route.fulfill(status=404, json={})

                context.add_init_script(REPLY_GATE)
                context.route('**/*', route_request)
                context.route_web_socket('**/*', lambda socket: socket.close())
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.set_default_timeout(5000)
                page.goto('http://offline.invalid/media-libraries')
                page.wait_for_function('document.querySelector("#mediaLibrariesPage").dataset.loading === "false"')
                page.locator('[data-mapping-server]').fill('//NAS/尚未保存')
                page.locator('.ml-waiting-details summary').click()
                checkboxes = page.locator('[data-select-waiting-path]')
                self.assertEqual(checkboxes.count(), 2)
                checkboxes.nth(0).check()
                page.locator('[data-refresh-waiting]').scroll_into_view_if_needed()
                before = page.locator('[data-refresh-waiting]').bounding_box()
                page.evaluate('window.savedWaitingRow=document.querySelector("[data-waiting-group]")')
                page.evaluate('(path)=>uiHoldReplies.push({path,key:"slow"})', QUEUE)
                page.locator('[data-refresh-waiting]').click()
                page.wait_for_function('uiReplyGates.slow?.ready')
                page.wait_for_timeout(350)
                during = page.locator('[data-refresh-waiting]').bounding_box()
                assert before is not None and during is not None
                self.assertEqual((before['width'], before['height']), (during['width'], during['height']))
                self.assertEqual(checkboxes.count(), 2)
                page.evaluate('uiReplyGates.slow.release()')
                page.wait_for_function('!document.querySelector("[data-refresh-waiting]").disabled')
                self.assertTrue(page.evaluate('savedWaitingRow===document.querySelector("[data-waiting-group]")'))
                self.assertTrue(checkboxes.nth(0).is_checked())
                self.assertEqual(page.locator('[data-mapping-server]').input_value(), '//NAS/尚未保存')
                self.assertFalse(page.evaluate('Boolean(window.xss)'))
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                evidence = os.environ.get('UI_SESSION_EVIDENCE_DIR')
                if evidence:
                    target = Path(evidence)
                    target.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target / f'media-refresh-{width}.png'), full_page=True)
                page.locator('[data-resolve-waiting="clear"]').click()
                page.wait_for_function('document.querySelectorAll("[data-select-waiting-path]").length===1')
                self.assertEqual(requests[-1][1]['records'][0]['paths'], [old])
                self.assertEqual(checkboxes.first.get_attribute('value'), current)
                self.assertEqual(page.locator('[data-mapping-server]').input_value(), '//NAS/尚未保存')
                waiting['conflict'] = True
                checkboxes.first.check()
                page.locator('[data-resolve-waiting="retry"]').click()
                page.wait_for_function('document.querySelector("#mlWaitingNotice").textContent.includes("已变化")')
                self.assertEqual(page.locator('#mlWaitingSelection').inner_text(), '已选 0 组 · 0 条路径')
                self.assertEqual(checkboxes.count(), 1)
                self.assertEqual(len(requests), 2)
                checkboxes.first.check()
                page.locator('[data-resolve-waiting="retry"]').click()
                page.wait_for_function('document.querySelectorAll("[data-waiting-group]").length===0')
                self.assertEqual(requests[-1][1]['records'][0]['paths'], [current])
                self.assertTrue(page.locator('[data-resolve-waiting="retry"]').is_disabled())
                self.assertEqual(errors, [])
                self.assertEqual(unexpected, [])
