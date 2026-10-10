"""手动整理名称精简：共享清洗 API 与本地页面真实点击回归。"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import create_app
from app.modules.recognition.cleaner import _comparison_key
from app.modules.scraper import TMDBScraper
from app.routes.tools_api import clean_scrape_query
from tests.support import IsolatedDatabaseTestCase
from tests.test_guangya_directory_scrape_browser import APP_MODAL_SCRIPT, POSITION_SCRIPT
from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENDPOINT = '/api/tools/scrape/clean-query'
MOVIE_RELEASE = '血玫瑰.Bloody.Rose.1988.LDVDRip.HALFCD.2Audios.mkv'


class ManualScrapeCleanApiTests(IsolatedDatabaseTestCase):
    @staticmethod
    def clean(payload):
        return clean_scrape_query(Request({'type': 'http', 'session': {'logged_in': True}}), payload)

    def test_instance_releases_use_shared_cleaning_and_keep_year(self):
        cases = [json.loads(line) for line in (
            ROOT / 'tests/fixtures/release_recognition_cases.jsonl'
        ).read_text('utf-8').splitlines()]
        with patch.object(TMDBScraper, 'search', side_effect=AssertionError('清洗不能查询 TMDB')):
            for case in cases:
                if 'source-instance-20261010' not in case.get('tags', []):
                    continue
                with self.subTest(filename=case['filename']):
                    response = self.clean({'query': case['filename']})
                    self.assertEqual(response.status_code, 200)
                    expected = case['expected']
                    self.assertEqual(
                        _comparison_key(json.loads(response.body)['query']),
                        _comparison_key(f"{expected['title']} {expected['year']}"),
                    )

    def test_official_title_numbers_unknown_subtitle_and_clean_name_are_preserved(self):
        cases = (
            ('1917.2019.1080p.BluRay.mkv', '1917 2019'),
            ('Toy Story 3', 'Toy Story 3'),
            ('Proper Hybrid Unrated IMAX', 'Proper Hybrid Unrated IMAX'),
            ('Movie.SecretChapter.2024.1080p.BluRay.mkv', 'Movie SecretChapter 2024'),
            ('被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生',
             '被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生'),
            ('[Japanese] Story.2003.1080p.WEB-DL.H264.AAC.mkv', 'Japanese Story 2003'),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                response = self.clean({'query': raw})
                self.assertEqual(json.loads(response.body)['query'], expected)

    def test_invalid_names_are_rejected_before_creating_scraper(self):
        with patch('app.routes.tools_api.TMDBScraper') as scraper:
            for query in (None, '', '   ', 12, ['Movie'], 'a' * 1025):
                with self.subTest(query=str(query)[:20]):
                    self.assertEqual(self.clean({'query': query}).status_code, 400)
            scraper.assert_not_called()

    def test_audio_channels_are_cleaned_whole_and_never_become_episode_numbers(self):
        scraper = TMDBScraper()
        self.addCleanup(scraper.close)
        for audio in ('DTS-HD.MA.5.1', 'DTS.HD.MA.6.1', 'DDP5.1', 'TrueHD.7.1'):
            for position in ('', '.S02E01'):
                with self.subTest(audio=audio, position=position):
                    raw = f'Movie{position}.2024.1080p.BluRay.{audio}.mkv'
                    self.assertEqual(json.loads(self.clean({'query': raw}).body)['query'], 'Movie 2024')
                    parsed = scraper.parse_media(raw)
                    self.assertEqual(parsed.media_type, 'tv' if position else 'movie')
                    self.assertEqual(parsed.source_episode, 1 if position else None)

    def test_anonymous_requests_are_rejected(self):
        with self.assertRaises(HTTPException) as error:
            clean_scrape_query(Request({'type': 'http', 'session': {}}), {'query': 'Movie'})
        self.assertEqual(error.exception.status_code, 401)


@unittest.skipIf(sync_playwright is None, '未安装 Playwright')
class LocalManualScrapeCleanBrowserTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        executable = _chromium_executable(cls.playwright)
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest('未找到 Chrome/Chromium')
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True, args=['--no-sandbox'],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.client = TestClient(create_app(start_background=False))
        self.addCleanup(self.client.close)
        login = self.client.get('/login')
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login.text).group(1)
        response = self.client.post('/login', data={
            'username': 'admin', 'password': '123456', 'csrf_token': token,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        html = self.client.get('/local-media').text
        self.csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', html).group(1)
        self.html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
        self.requests = []
        self.errors = []
        self.clean_status = 200
        self.pending_clean = None
        self.hold_clean = False
        self.inspection_id = 0
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 800})
        self.addCleanup(self.page.close)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.route('**/*', self._route)
        self.page.goto('http://testserver/local-media#manual')
        self.page.evaluate("""() => {
            window.renderLucideIcons = () => {};
            window.appAlert = async () => true;
            window.appConfirm = async () => false;
        }""")
        self.page.add_script_tag(content=APP_MODAL_SCRIPT)
        self.page.add_script_tag(content=POSITION_SCRIPT)
        self.page.add_script_tag(path=str(ROOT / 'app/static/js/local-media.js'))
        self._open()

    def _route(self, route):
        path = urlsplit(route.request.url).path
        if path == '/local-media':
            return route.fulfill(status=200, content_type='text/html', body=self.html)
        if not path.startswith('/api/'):
            return route.fulfill(status=404, body='')
        body = route.request.post_data_json if route.request.post_data else None
        self.requests.append((path, body))
        if path == CLEAN_ENDPOINT:
            if self.hold_clean:
                self.pending_clean = route
                return
            return self._finish_clean(route)
        if path == '/api/local-media/sources':
            data = {'sources': [{'id': 1, 'name': '测试来源', 'media_type': 'auto', 'enabled': True}]}
        elif path == '/api/local-media/tasks':
            data = {'tasks': []}
        elif path == '/api/local-media/items':
            data = {'items': [{
                'source_id': 1, 'source_name': '测试来源', 'name': MOVIE_RELEASE,
                'path': '/test/' + MOVIE_RELEASE, 'kind': 'video', 'organize_ready': True,
            }], 'sources': []}
        elif path == '/api/local-media/inspect':
            self.inspection_id += 1
            data = {
                'inspection_id': str(self.inspection_id), 'selected_kind': 'file',
                'suggested_query': MOVIE_RELEASE, 'media_type': 'movie',
                'video_count': 1, 'file_count': 1,
            }
        elif path == '/api/local-media/search':
            data = {'candidates': [{'tmdb_id': '1', 'title': '血玫瑰', 'media_type': 'movie'}]}
        else:
            self.fail(f'精简名称时出现无关业务请求：{path}')
        route.fulfill(status=200, json=data)

    def _finish_clean(self, route):
        if self.clean_status != 200:
            return route.fulfill(status=self.clean_status, json={'error': '测试清洗失败'})
        response = self.client.post(CLEAN_ENDPOINT, json=route.request.post_data_json,
                                    headers={'X-CSRF-Token': self.csrf})
        route.fulfill(status=response.status_code, content_type='application/json', body=response.content)

    def _open(self):
        self.page.locator('[data-open-item-menu]').first.click()
        self.page.locator('[data-item-action="search"]').click()
        self.page.wait_for_function("document.getElementById('lmScrapeStatus').textContent === '请选择候选'")
        self.requests.clear()

    def _click_clean(self):
        self.page.locator('#lmScrapeCleanBtn').click()
        self.page.wait_for_function("!document.getElementById('lmScrapeCleanBtn').disabled")

    def test_clean_updates_input_searches_once_and_reports_effect(self):
        self._click_clean()
        expected = '血玫瑰 Bloody Rose 1988'
        self.assertEqual(self.page.locator('#lmSearchQuery').input_value(), expected)
        self.assertIn('已精简名称', self.page.locator('#lmScrapeStatus').inner_text())
        self.assertEqual([path for path, _ in self.requests], [CLEAN_ENDPOINT, '/api/local-media/search'])
        self.assertEqual(self.requests[-1][1]['query'], expected)
        self.assertTrue(self.page.locator('#lmExecuteBtn').is_disabled())
        self.assertEqual(self.errors, [])

    def test_clean_title_gives_feedback_and_preserves_position_fields(self):
        self.page.locator('#lmMediaType').select_option('tv')
        self.page.locator('#lmScrapeSeason').fill('2')
        self.page.locator('#lmScrapeEpisode').fill('3')
        self.page.locator('#lmSearchQuery').fill('Toy Story 3')
        self._click_clean()
        self.assertEqual(self.page.locator('#lmSearchQuery').input_value(), 'Toy Story 3')
        self.assertIn('名称无需精简', self.page.locator('#lmScrapeStatus').inner_text())
        self.assertEqual(self.page.locator('#lmScrapeSeason').input_value(), '2')
        self.assertEqual(self.page.locator('#lmScrapeEpisode').input_value(), '3')
        self.assertEqual(self.requests[-1][1]['media_type'], 'tv')

    def test_failed_and_empty_cleaning_keep_input_and_do_not_search(self):
        for status, raw in ((503, MOVIE_RELEASE), (200, '[1080p][AAC]')):
            with self.subTest(status=status):
                self.clean_status = status
                self.page.locator('#lmSearchQuery').fill(raw)
                self.requests.clear()
                self._click_clean()
                self.assertEqual(self.page.locator('#lmSearchQuery').input_value(), raw)
                self.assertEqual([path for path, _ in self.requests], [CLEAN_ENDPOINT])
                self.assertRegex(self.page.locator('#lmScrapeStatus').inner_text(), '精简失败|未识别到')

    def test_pending_response_does_not_overwrite_edits_or_reopened_modal(self):
        for reopen in (False, True):
            with self.subTest(reopen=reopen):
                self.hold_clean = True
                self.page.locator('#lmSearchQuery').fill(MOVIE_RELEASE)
                self.page.locator('#lmScrapeCleanBtn').click()
                self.page.wait_for_function("document.getElementById('lmScrapeCleanBtn').disabled")
                if reopen:
                    self.page.locator('#lmScrapeCloseBtn').click()
                    self._open()
                self.page.locator('#lmSearchQuery').fill('用户重新输入的标题')
                self.requests.clear()
                self._finish_clean(self.pending_clean)
                self.page.wait_for_function("!document.getElementById('lmScrapeCleanBtn').disabled")
                self.assertEqual(self.page.locator('#lmSearchQuery').input_value(), '用户重新输入的标题')
                self.assertNotIn('正在精简', self.page.locator('#lmScrapeStatus').inner_text())
                self.assertEqual(self.requests, [])
                self.assertEqual(self.errors, [])
