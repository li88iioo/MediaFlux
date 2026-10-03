from __future__ import annotations

import shutil
import unittest
from pathlib import Path


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser dependency
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
APP_SCRIPT = ROOT / "app/static/js/app.js"
ICON_SCRIPT = ROOT / "app/static/js/lucide.min.js"


class LucideLocalRenderingTests(unittest.TestCase):
    def test_dynamic_icon_refreshes_are_scoped_to_changed_regions(self) -> None:
        files = [
            *Path("app/static/js").glob("*.js"),
            *Path("app/templates").glob("*.html"),
        ]
        files = [path for path in files if path.name != "lucide.min.js"]
        unscoped_markers = (
            "lucide.createIcons();",
            "lucide?.createIcons?.();",
            "lucide.createIcons?.();",
        )

        offenders = []
        for path in files:
            source = path.read_text(encoding="utf-8")
            if any(marker in source for marker in unscoped_markers):
                offenders.append(str(path))

        self.assertEqual(offenders, [])


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class LucideRenderingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = next((path for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                           if (path := shutil.which(name))), None)
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest("未找到 Chrome/Chromium")
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("http://mediaflux.test/**", lambda route: route.fulfill(
            content_type="text/html", body='<html><body><div id="outside"><i data-lucide="search"></i></div></body></html>',
        ))
        self.page.goto("http://mediaflux.test/")
        self.page.add_script_tag(content=ICON_SCRIPT.read_text())
        self.page.add_script_tag(content=APP_SCRIPT.read_text())

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_detached_region_and_placeholder_root_render_without_touching_other_icons(self):
        result = self.page.evaluate("""() => {
            const outside = document.querySelector('#outside svg');
            const region = document.createElement('section');
            region.innerHTML = '<i data-lucide="folder"></i><i data-lucide="arrow-up"></i>';
            window.renderLucideIcons(region);
            const placeholder = document.createElement('i');
            placeholder.dataset.lucide = 'circle-check-big';
            region.append(placeholder);
            window.renderLucideIcons(placeholder);
            return {count:region.querySelectorAll('svg').length,
                pending:region.querySelectorAll('i').length,
                outsideUnchanged:document.querySelector('#outside svg') === outside};
        }""")
        self.assertEqual(result, {"count": 3, "pending": 0, "outsideUnchanged": True})

    def test_repeated_region_refresh_is_idempotent_and_does_not_rebuild_the_page(self):
        result = self.page.evaluate("""() => {
            const outside = document.querySelector('#outside svg');
            const region = document.createElement('section');
            region.innerHTML = '<i data-lucide="folder"></i>';
            document.body.append(region);
            window.renderLucideIcons(region);
            const icon = region.querySelector('svg');
            const observer = new MutationObserver(() => {});
            observer.observe(document.body,{childList:true,subtree:true});
            for(let i=0;i<80;i++) window.renderLucideIcons(region);
            const changes = observer.takeRecords().length;
            observer.disconnect();
            return {changes, sameLocal:icon === region.querySelector('svg'),
                sameOutside:outside === document.querySelector('#outside svg')};
        }""")
        self.assertEqual(result, {"changes": 0, "sameLocal": True, "sameOutside": True})

    def test_icon_attributes_and_unknown_placeholders_are_preserved(self):
        result = self.page.evaluate("""() => {
            const region=document.createElement('section');
            region.innerHTML='<i id="custom" data-lucide="loader-circle" class="custom lucide" '
              +'stroke-width="3" aria-hidden="false" aria-label="正在加载" width="32" style="color:red"></i>'
              +'<i id="unknown" data-lucide="no-such-mediaflux-icon"></i>';
            window.renderLucideIcons(region);
            const icon=region.querySelector('#custom');
            return {tag:icon.tagName, width:icon.getAttribute('width'), stroke:icon.getAttribute('stroke-width'),
                hidden:icon.getAttribute('aria-hidden'), label:icon.getAttribute('aria-label'),
                color:icon.style.color, classes:icon.getAttribute('class'),
                paths:icon.children.length, unknown:region.querySelector('#unknown').tagName};
        }""")
        self.assertEqual(result["tag"], "svg")
        self.assertEqual(result["width"], "32")
        self.assertEqual(result["stroke"], "3")
        self.assertEqual(result["hidden"], "false")
        self.assertEqual(result["label"], "正在加载")
        self.assertEqual(result["color"], "red")
        self.assertEqual(result["classes"].split().count("lucide"), 1)
        self.assertIn("custom", result["classes"])
        self.assertGreater(result["paths"], 0)
        self.assertEqual(result["unknown"], "I")

    def test_observer_renders_new_icons_once_and_secret_toggle_keeps_working(self):
        self.page.evaluate("""() => {
            window.originalOutside=document.querySelector('#outside svg');
            const region=document.createElement('section');
            region.id='dynamic';region.innerHTML='<i data-lucide="folder-open"></i>';
            document.body.append(region);
        }""")
        self.page.locator('#dynamic svg').wait_for()
        self.assertTrue(self.page.evaluate("document.querySelector('#outside svg') === window.originalOutside"))
        self.page.evaluate("""() => {
            const input=document.createElement('input');input.type='password';input.value='test';
            input.id='secret';input.className='form-input';document.body.append(input);
        }""")
        self.page.locator('.secret-input-toggle').wait_for()
        self.page.locator('.secret-input-toggle').click()
        self.assertEqual(self.page.locator('#secret').get_attribute('type'), 'text')
        self.assertEqual(self.page.locator('.secret-input-toggle svg').get_attribute('data-lucide'), 'eye-off')
        self.page.locator('.secret-input-toggle').click()
        self.assertEqual(self.page.locator('#secret').get_attribute('type'), 'password')
        self.assertEqual(self.page.locator('.secret-input-toggle svg').get_attribute('data-lucide'), 'eye')
        self.assertTrue(self.page.evaluate("document.querySelector('#outside svg') === window.originalOutside"))


if __name__ == "__main__":
    unittest.main()
