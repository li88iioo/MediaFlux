"""统一媒体库页面的结构、稳定性与响应式契约。"""
from __future__ import annotations

import mimetypes
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

from tests import test_agent_kernel_browser as browser_support

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Route


class _PathTestFixture(TypedDict):
    response: dict[str, object]
    calls: list[dict[str, str]]
    unexpected: list[str]
    errors: list[str]
    pending: list[Route]
    hold: bool


class MediaLibrariesUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.base = (root / "app/templates/base.html").read_text(encoding="utf-8")
        cls.template = (root / "app/templates/media_libraries.html").read_text(encoding="utf-8")
        cls.css = (root / "app/static/css/media-libraries.css").read_text(encoding="utf-8")
        cls.js = (root / "app/static/js/media-libraries.js").read_text(encoding="utf-8")

    def test_sidebar_places_media_library_immediately_before_settings(self):
        nav = self.base[self.base.index('<nav class="nav">'):self.base.index('</nav>')]
        media = nav.index("url_for('pages.media_libraries')")
        settings = nav.index("url_for('pages.settings')")
        between = nav[media:settings]
        self.assertGreater(settings, media)
        self.assertNotIn('class="nav-item', between.split('</a>', 1)[-1])
        self.assertEqual(nav.count("url_for('pages.media_libraries')"), 1)
        self.assertIn("active=='media_libraries'", nav)
        self.assertIn('<span>媒体库</span>', nav)

    def test_template_loads_dedicated_assets_and_stable_placeholders(self):
        self.assertIn("{% block page_title %}媒体库{% endblock %}", self.template)
        self.assertIn("css/media-libraries.css", self.template)
        self.assertIn("js/media-libraries.js", self.template)
        self.assertIn("static_url('css/media-libraries.css')", self.template)
        self.assertIn("static_url('js/media-libraries.js')", self.template)
        self.assertNotRegex(self.template, r"\?v=20\d{6}[a-z]")
        self.assertIn('id="mediaLibrariesPage"', self.template)
        self.assertIn('data-loading="true"', self.template)
        for element_id in ("mlSummary", "mlServerCount", "mlLibraryCount", "mlMappingCount", "mlBindingCount"):
            self.assertIn(f'id="{element_id}"', self.template)
        self.assertIn("min-height: 196px", self.css)

    def test_page_is_single_unified_mapping_workbench_without_provider_tabs(self):
        self.assertIn("媒体库与路径映射", self.template)
        self.assertIn("STRM 输出目录和本地归档目录", self.template)
        self.assertIn("添加映射", self.template)
        self.assertIn("保存映射", self.template)
        self.assertNotIn('role="tab"', self.template)
        self.assertNotIn('role="tabpanel"', self.template)
        self.assertNotIn("mlMappingTabJellyfin", self.template)
        self.assertNotIn("mlMappingTabEmby", self.template)
        self.assertNotIn("data-server-form", self.template)

    def test_mapping_rows_switch_between_strm_and_local_sources(self):
        self.assertIn("function configuredServers()", self.js)
        self.assertIn("server.enabled && server.configured", self.js)
        self.assertIn("function libraryChoices()", self.js)
        self.assertIn("state.overview?.local_bindings", self.js)
        self.assertIn("state.overview?.local_sources", self.js)
        self.assertIn("kind: 'strm'", self.js)
        self.assertIn("kind: 'local'", self.js)
        self.assertIn("data-mapping-library", self.js)
        self.assertIn("data-toggle-mapping-source", self.js)
        self.assertNotIn("data-mapping-local-scope", self.js)
        self.assertIn("function inferLocalCategory(choice)", self.js)
        self.assertIn("collectionType:", self.js)
        self.assertIn("function allLocalSourceIds()", self.js)
        self.assertIn("sourceIds:", self.js)
        self.assertIn("file-symlink", self.js)
        self.assertIn("hard-drive", self.js)
        self.assertNotIn("不绑定媒体服务器", self.js)
        self.assertIn("data-mapping-local-path", self.js)
        self.assertIn("data-pick-directory", self.js)
        self.assertIn("openGuangYaDirectoryPicker", self.js)
        self.assertIn("/api/media-libraries/strm-directories", self.js)
        self.assertIn("/api/media-libraries/local-directories", self.js)
        self.assertIn("/api/media-libraries/mappings", self.js)
        self.assertIn(".ml-directory-control", self.css)
        self.assertIn(".ml-directory-path[readonly]", self.css)
        self.assertIn(".ml-source-toggle", self.css)
        self.assertIn(".ml-source-control", self.css)
        self.assertNotIn(".ml-source-control.is-local", self.css)
        self.assertNotIn(".ml-local-scope-select", self.css)
        self.assertNotIn("data-mapping-tab", self.js)
        self.assertNotIn("data-add-strm-mapping", self.js)

    def test_initial_load_has_stable_placeholders(self):
        self.assertIn("state.overview ? Promise.resolve() : sleep(320)", self.js)
        self.assertIn("setInitialLoading(true)", self.js)
        self.assertIn("min-height: 300px", self.css)

    def test_mobile_mapping_grid_uses_named_non_overlapping_areas(self):
        responsive = self.css[self.css.index("@media (max-width: 1180px)"):]
        for area in ('"library source"', '"directory server"', '"actions actions"', '"result result"'):
            self.assertIn(area, responsive)
        mobile = self.css[self.css.index("@media (max-width: 760px)"):]
        for area in ('"library"', '"source"', '"directory"', '"server"', '"actions"', '"result"'):
            self.assertIn(area, mobile)
        self.assertIn("width: 100%;", mobile)
        self.assertIn("min-width: 0;", mobile)
        self.assertIn(".ml-field-label", self.css)

    def test_runtime_uses_unified_overview_save_and_path_test_endpoints(self):
        self.assertIn("api('/api/media-libraries/overview')", self.js)
        self.assertIn("api('/api/media-libraries/path-test'", self.js)
        self.assertIn("api('/api/media-libraries/mappings'", self.js)
        self.assertIn("strm_mappings: strmMappings", self.js)
        self.assertIn("local_bindings: localBindings", self.js)
        self.assertIn(".flatMap((row) => availableLocalSourceIds(row.sourceIds)", self.js)
        self.assertIn("requestAnimationFrame", self.js)
        self.assertNotIn("@keyframes", self.css)


@unittest.skipIf(browser_support.sync_playwright is None, "系统环境未安装 Playwright")
class MediaLibrariesPathTestBrowserTests(unittest.TestCase):
    browser: Browser

    @classmethod
    def setUpClass(cls) -> None:
        browser_support.AgentKernelBrowserTests.setUpClass.__func__(cls)
        env = Environment(loader=FileSystemLoader(browser_support.ROOT / "app/templates"), autoescape=True)
        cls.html = env.get_template("media_libraries.html").render(
            active="media_libraries", app_version="path-test", discovery_enabled=False,
            agent_enabled=False, csrf_token=lambda: "fake", static_url=lambda value: "/static/" + value,
            url_for=lambda name: "/" + name.split(".")[-1],
        )

    def make_page(self, width: int, source_kind: Literal["strm", "local"] = "strm") -> tuple[Page, _PathTestFixture]:
        context = self.browser.new_context(
            viewport={"width": width, "height": 900}, reduced_motion="reduce", service_workers="block",
        )
        self.addCleanup(context.close)
        fixture: _PathTestFixture = {"response": {}, "calls": [], "unexpected": [], "errors": [], "pending": [], "hold": False}
        libraries = [
            {"id": "a", "name": "电影 A", "locations": ["/media/movies"]},
            {"id": "b", "name": "电影 B", "locations": ["/media/movies"]},
        ]
        server: dict[str, object] = {
            "server_type": "jellyfin", "label": "Jellyfin", "enabled": True,
            "configured": True, "mapping_key": "JELLYFIN_PATH_MAPPINGS", "libraries": libraries,
            "mappings": [{"local": "/data/strm", "server": "/media/movies"}],
        }
        overview: dict[str, object] = {
            "servers": [server],
            "summary": {"configured_servers": 1, "online_servers": 1, "libraries": 2,
                        "total_mappings": 1, "path_mappings": 1, "local_bindings": 0, "local_sources": 0},
            "strm": {"output_root": "/data/strm", "sources": []},
            "local_sources": [{"id": 1, "name": "本地", "local_root": "/data/downloads", "enabled": True}],
            "local_bindings": [],
        }
        if source_kind == "local":
            server["mappings"] = []
            overview["local_bindings"] = [{
                "source_id": 1, "category": "movie", "local_path": "/data/strm", "server_path": "/media/movies",
                "provider": "jellyfin", "library_id": "a", "library_name": "电影 A",
            }]

        def route(request: Route) -> None:
            url = urlsplit(request.request.url)
            path = unquote(url.path)
            if url.netloc == "media-libraries.test":
                if path == "/media-libraries":
                    request.fulfill(body=self.html, content_type="text/html")
                    return
                if path.startswith("/static/"):
                    asset = (browser_support.ROOT / "app" / path.lstrip("/")).resolve()
                    if asset.is_relative_to(browser_support.ROOT / "app/static") and asset.is_file():
                        request.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
                        return
                if path == "/api/config":
                    request.fulfill(json={})
                    return
                if path == "/api/media-libraries/overview":
                    request.fulfill(json=overview)
                    return
                if path == "/api/media-libraries/path-test":
                    payload = request.request.post_data_json
                    assert isinstance(payload, dict)
                    assert all(isinstance(key, str) and isinstance(value, str) for key, value in payload.items())
                    fixture["calls"].append(payload)
                    if fixture["hold"]:
                        fixture["pending"].append(request)
                    else:
                        request.fulfill(json=fixture["response"])
                    return
            fixture["unexpected"].append(request.request.url)
            request.abort()

        context.route("**/*", route)
        context.route_web_socket("**/*", lambda websocket: websocket.close())
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: fixture["errors"].append(str(error)))
        page.goto("http://media-libraries.test/media-libraries")
        page.locator('[data-mapping-library]').select_option("jellyfin:a")
        page.evaluate("() => document.fonts.ready")
        return page, fixture

    def test_path_test_requires_unique_selected_match_on_desktop_and_mobile(self):
        cases: tuple[tuple[str, list[str], str, str], ...] = (
            ("ambiguous", ["a", "b"], "is-warning", "自动刷新将跳过"),
            ("matched", ["a"], "is-success", "映射有效"),
            ("matched", ["b"], "is-warning", "不是当前选择"),
            ("covered", ["a"], "is-warning", "上层目录"),
            ("unmatched", [], "is-warning", "未命中"),
        )
        for width in (1440, 390):
            page, fixture = self.make_page(width)
            button = page.locator("[data-test-mapping]")
            result = page.locator("[data-mapping-result]")
            fixture["hold"] = True
            geometry = """() => {
                const button = document.querySelector('[data-test-mapping]').getBoundingClientRect();
                return [button.width, button.height,
                    document.querySelector('[data-mapping-row]').getBoundingClientRect().height];
            }"""
            initial = page.evaluate(geometry)
            for status, ids, expected_class, text in cases:
                with self.subTest(width=width, status=status, ids=ids):
                    fixture["response"] = {"status": status, "mapped_path": "/media/movies", "matches": [
                        {"id": item, "name": f"电影 {item.upper()}", "mode": "covered" if status == "covered" else "direct"}
                        for item in ids
                    ]}
                    button.click()
                    page.wait_for_function("() => document.querySelector('[data-test-mapping]').disabled")
                    self.assertIn("正在核对", result.inner_text())
                    self.assertEqual(page.evaluate(geometry), initial)
                    fixture["pending"].pop().fulfill(json=fixture["response"])
                    page.wait_for_function("text => document.querySelector('[data-mapping-result]').textContent.includes(text)", arg=text)
                    result_class = result.get_attribute("class")
                    assert result_class is not None
                    self.assertIn(expected_class, result_class)
                    self.assertEqual(result.get_attribute("role"), "status")
                    self.assertFalse(button.is_disabled())
                    self.assertEqual(page.evaluate(geometry), initial)
                    self.assertEqual(result.get_attribute("title"), "电影 A、电影 B" if status == "ambiguous" else None)
                    if expected_class != "is-success":
                        self.assertNotIn("映射有效", result.inner_text())
            self.assertEqual(len(fixture["calls"]), len(cases))
            self.assertTrue(all(call == {
                "provider": "jellyfin", "local_path": "/data/strm",
                "source_kind": "strm", "library_id": "a", "library_name": "电影 A",
                "server_path": "/media/movies", "sample_path": "/data/strm",
            } for call in fixture["calls"]))
            self.assertEqual(fixture["errors"], [])
            self.assertEqual(fixture["unexpected"], [])
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_local_path_test_sends_binding_and_shows_valid_for_duplicate_root(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                page, fixture = self.make_page(width, "local")
                fixture["response"] = {
                    "status": "matched", "source_kind": "local", "allowed_library_ids": ["a"],
                    "mapped_path": "/media/movies", "matches": [{"id": "a", "name": "电影 A", "mode": "direct"}],
                }
                page.locator("[data-test-mapping]").click()
                page.wait_for_function("document.querySelector('[data-mapping-result]').textContent.includes('映射有效')")
                self.assertEqual(fixture["calls"], [{
                    "provider": "jellyfin", "source_kind": "local", "library_id": "a", "library_name": "电影 A",
                    "local_path": "/data/strm", "server_path": "/media/movies", "sample_path": "/data/strm",
                }])
                result = page.locator("[data-mapping-result]")
                result_class = result.get_attribute("class")
                assert result_class is not None
                self.assertIn("is-success", result_class)
                self.assertNotIn("跳过", result.inner_text())
                self.assertEqual(fixture["errors"], [])
                self.assertEqual(fixture["unexpected"], [])
