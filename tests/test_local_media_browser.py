"""本地媒体页面结构、导航和稳定占位契约。"""
from __future__ import annotations

import re
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class LocalMediaBrowserTests(IsolatedDatabaseTestCase):
    def test_page_has_four_stable_work_areas_and_navigation(self):
        with TestClient(create_app(start_background=False)) as client:
            login = client.get("/login")
            token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login.text).group(1)
            response = client.post(
                "/login", data={"username": "admin", "password": "123456", "csrf_token": token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            page = client.get("/local-media")
        self.assertEqual(page.status_code, 200)
        for marker in (
            'id="lmSourcesSection"', 'id="lmMediaItems"', 'id="lmReviewList"',
            'id="lmTaskList"', 'id="lmSourceModal"', 'id="lmItemContextMenu"',
            'id="lmScrapeModal"', 'id="lmSearchQuery"', 'id="lmExecuteBtn"',
            'id="lmScrapeEpisodeFields"', 'id="lmScrapeSeason"', 'id="lmScrapeEpisode"',
            'id="lmPickLocalRootBtn"', 'id="lmSourceMediaType"', 'id="lmStableSeconds"',
            'id="lmScanMinutes"', 'class="lm-form-grid-3 lm-wide"',
            'js/local-media.js', 'css/local-media.css',
        ):
            self.assertIn(marker, page.text)
        self.assertIn("本地媒体", page.text)
        self.assertIn("正在读取来源配置", page.text)
        self.assertRegex(page.text, re.compile(r'class="[^"]*\blocal-media-page\b[^"]*"'))
        self.assertIn('class="app-modal lm-modal"', page.text)
        self.assertNotIn("下载完成，直接进入媒体库", page.text)
        self.assertNotIn("LOCAL MEDIA PIPELINE", page.text)
        self.assertNotIn("硬链接", page.text)
        self.assertNotIn("复制模式", page.text)
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        self.assertIn("/api/local-media/directories", js)
        self.assertIn("/api/local-media/media-servers", js)
        self.assertIn("/api/local-media/items", js)
        self.assertIn("/api/local-media/items/delete", js)
        self.assertIn("function openScrapeForItem", js)
        self.assertIn("library_id: libraryId, library_name: libraryName", js)
        self.assertIn("lmSourceMode", page.text)
        self.assertIn("row.dataset.libraryRequest", js)
        self.assertIn("allowRoot: !isRootsMode && Boolean(sourceId || networkRoot)", js)
        self.assertIn('id="lmTaskMore"', page.text)
        self.assertIn("taskDisplayLimit = 60", js)
        self.assertIn("function schedulePoll()", js)
        self.assertIn("document.addEventListener('visibilitychange'", js)
        self.assertIn("let refreshQueued = false", js)
        self.assertIn("if (refreshing) return refreshPromise", js)
        self.assertIn("while (refreshQueued)", js)
        self.assertIn("refreshQueued = true", js)
        for contract in (
            "let previewRequestSerial = 0",
            "let searchRequestSerial = 0",
            "let appliedPreviewContext = null",
            "function invalidatePreview()",
            "requestSerial !== previewRequestSerial",
            "inspection?.inspection_id !== context.inspectionId",
            "positionControls.payload(mediaType",
            "appliedPreviewContext = Object.freeze",
            "tmdb_id: confirmedContext.tmdbId",
            "season: confirmedContext.season",
            "episode: confirmedContext.episode",
            "rules_snapshot: confirmedPreview.rules_snapshot",
            "if (!hasLoadedLocalMedia) renderInitialLoadFailure",
            "if (currentManual && window.appAlert)",
        ):
            self.assertIn(contract, js)
        self.assertNotIn("if (refreshing) return;", js)
        self.assertNotIn("setInterval(() => loadAll(false), 10000)", js)
        self.assertNotIn('id="lmManualSource"', page.text)
        self.assertNotIn('id="lmManualPath"', page.text)
        self.assertNotIn('id="lmPickManualPathBtn"', page.text)
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")
        self.assertIn(".lm-media-list", css)
        self.assertIn(".lm-item-context-menu", css)
        self.assertIn(".lm-scrape-workspace", css)
        self.assertIn(".lm-scrape-episode-fields", css)
        self.assertIn(".lm-form-grid-3", css)
        self.assertIn("grid-template-columns: minmax(340px, 35%) minmax(0, 1fr)", css)
        self.assertNotRegex(css, r"\.lm-media-row:hover\s*\{[^}]*transform")
