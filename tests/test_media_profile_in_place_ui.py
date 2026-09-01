from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 可选浏览器依赖
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_SCRIPT = ROOT / "app/static/js/discovery.js"
SUBSCRIPTIONS_SCRIPT = ROOT / "app/static/js/subscriptions.js"
GLOBAL_SEARCH_TEMPLATE = ROOT / "app/templates/global_search.html"
RSS_TEMPLATE = ROOT / "app/templates/rss.html"
PROFILE_HOST = ROOT / "app/templates/_media_profile_host.html"
PROFILE_DIALOG = ROOT / "app/templates/_media_profile_dialog.html"
MAIN_STYLES = ROOT / "app/static/css/main.css"


class MediaProfileInPlaceUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.discovery = DISCOVERY_SCRIPT.read_text(encoding="utf-8")
        self.subscriptions = SUBSCRIPTIONS_SCRIPT.read_text(encoding="utf-8")
        self.global_search = GLOBAL_SEARCH_TEMPLATE.read_text(encoding="utf-8")
        self.rss = RSS_TEMPLATE.read_text(encoding="utf-8")
        self.host = PROFILE_HOST.read_text(encoding="utf-8")
        self.dialog = PROFILE_DIALOG.read_text(encoding="utf-8")
        self.styles = MAIN_STYLES.read_text(encoding="utf-8")

    def test_search_and_subscription_pages_mount_the_shared_profile_host(self):
        for template in (self.global_search, self.rss):
            self.assertIn('{% include "_media_profile_host.html" %}', template)
            self.assertIn("static_url('js/discovery.js')", template)
        self.assertIn('data-discovery-profile-host="true"', self.host)
        self.assertIn('{% include "_media_profile_dialog.html" %}', self.host)
        self.assertIn('id="discovery-detail-dialog"', self.dialog)

    def test_profile_links_keep_navigation_fallback_but_open_in_place_for_plain_clicks(self):
        self.assertIn("data-media-profile-link", self.global_search)
        self.assertGreaterEqual(self.subscriptions.count("dataset.mediaProfileLink = ''"), 4)
        for contract in (
            "const profileOnly = root.dataset.discoveryProfileHost === 'true'",
            "function detailIdentityFromURL(value)",
            "a[data-media-profile-link]",
            "event.preventDefault()",
            "void openDetail(identity, link)",
            "if (profileOnly) return;",
            "if (!profileOnly) loadActive();",
        ):
            self.assertIn(contract, self.discovery)
        self.assertRegex(
            self.discovery,
            re.compile(
                r"event\.defaultPrevented.*?event\.button !== 0.*?event\.metaKey.*?event\.ctrlKey.*?event\.shiftKey.*?event\.altKey",
                re.S,
            ),
        )
        self.assertIn("link.target === '_blank'", self.discovery)
        self.assertIn("link.hasAttribute('download')", self.discovery)

    @unittest.skipIf(sync_playwright is None, "未安装 Playwright")
    def test_plain_click_opens_and_closes_profile_without_changing_current_page(self):
        browser_path = next(
            (
                path
                for path in (
                    shutil.which("google-chrome"),
                    shutil.which("google-chrome-stable"),
                    shutil.which("chromium"),
                    shutil.which("chromium-browser"),
                )
                if path
            ),
            None,
        )
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        script = self.discovery
        dialog = self.dialog
        styles = self.styles
        html = f"""
            <!doctype html>
            <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>{styles}</style></head>
            <body>
                <a id="profileLink" data-media-profile-link href="/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134&return_query=test">查看媒体档案</a>
                <div data-discovery-root data-discovery-profile-host="true" data-resource-results-enabled="false">
                    <div hidden>
                        <div id="discovery-source-tabs"></div><div id="discovery-filter-region"></div>
                        <form id="discovery-search-form"><input id="discovery-search-query"><button id="discovery-search-submit"><span></span></button></form>
                        <span id="discovery-provider-status"></span><div id="discovery-sections"></div><div id="discovery-grid"></div><div id="discovery-stage"></div>
                        <button id="discovery-refresh"></button><div id="discovery-load-more-row"><button id="discovery-load-more"><span></span></button></div><div id="discovery-page-sentinel"></div>
                    </div>
                    <div id="discovery-live"></div>
                    {dialog}
                </div>
                <script>
                    window.renderLucideIcons = () => {{}};
                    window.fetch = async () => ({{
                        ok: true,
                        status: 200,
                        json: async () => ({{detail: {{provider: 'tmdb', media_type: 'movie', external_id: '693134', tmdb_id: 693134, title: '沙丘2', year: '2024', overview: '测试简介'}}}}),
                    }});
                </script>
                <script>{script}</script>
            </body></html>
        """

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 390, "height": 640})
            page.route(
                "http://mediaflux.test/search*",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            page.route(
                "http://mediaflux.test/rss*",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            page.goto("http://mediaflux.test/search?q=test", wait_until="domcontentloaded")
            before = page.url
            page.locator("#profileLink").click()
            page.locator("#discovery-detail-dialog[open]").wait_for()
            page.get_by_text("沙丘2", exact=True).wait_for()
            self.assertEqual(page.url, before)
            bounds = page.locator("#discovery-detail-dialog").evaluate(
                "node => { const rect = node.getBoundingClientRect(); return {top: rect.top, bottom: rect.bottom, viewport: innerHeight}; }"
            )
            self.assertGreaterEqual(bounds["top"], -1)
            self.assertLessEqual(bounds["bottom"], bounds["viewport"] + 1)
            page.locator("[data-discovery-dialog-close]").click()
            page.wait_for_function("!document.querySelector('#discovery-detail-dialog').open")
            self.assertEqual(page.url, before)
            self.assertTrue(page.locator("#profileLink").evaluate("node => node === document.activeElement"))

            page.goto("http://mediaflux.test/rss#media", wait_until="domcontentloaded")
            page.locator("#profileLink").evaluate(
                "node => node.href = '/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134&return_to=/rss%23media'"
            )
            rss_before = page.url
            page.locator("#profileLink").click()
            page.locator("#discovery-detail-dialog[open]").wait_for()
            page.get_by_text("沙丘2", exact=True).wait_for()
            self.assertEqual(page.url, rss_before)
            page.locator("[data-discovery-dialog-close]").click()
            page.wait_for_function("!document.querySelector('#discovery-detail-dialog').open")
            self.assertEqual(page.url, rss_before)
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()


if __name__ == "__main__":
    unittest.main()
