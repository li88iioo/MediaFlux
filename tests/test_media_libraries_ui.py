"""统一媒体库页面的结构、稳定性与响应式契约。"""
from __future__ import annotations

import unittest
from pathlib import Path


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
        self.assertIn("?v=20260829c", self.template)
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

    def test_refresh_preserves_unsaved_edits_and_initial_load_is_stable(self):
        self.assertIn("manual && state.dirty", self.js)
        self.assertIn("存在尚未保存的媒体库映射", self.js)
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
