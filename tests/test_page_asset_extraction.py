from __future__ import annotations

import unittest
from pathlib import Path


class PageAssetExtractionTests(unittest.TestCase):
    def test_templates_use_route_generated_static_urls_and_one_shared_css_version(self) -> None:
        templates = {
            path.name: path.read_text(encoding="utf-8")
            for path in Path("app/templates").glob("*.html")
        }
        for name, source in templates.items():
            with self.subTest(template=name):
                self.assertNotRegex(source, r'(?:src|href)=["\']/static/')

        rss = templates["rss.html"]
        self.assertIn(
            "{{ url_for('static', path='js') }}/subscriptions.js?v=20260831a",
            rss,
        )
        versions = set()
        marker = "path='css/dashboard-workbench.css') }}?v="
        for name in ("dashboard.html", "global_search.html", "media_recent.html"):
            source = templates[name]
            self.assertIn(marker, source)
            versions.add(source.split(marker, 1)[1].split('"', 1)[0].split("'", 1)[0])
        self.assertEqual(versions, {"20260820a"})

    def test_large_page_scripts_are_cacheable_classic_assets(self) -> None:
        targets = {
            "organize.html": "organize.js",
            "settings.html": "settings.js",
            "guangya_strm.html": "guangya-strm.js",
            "logs.html": "logs.js",
            "downloads.html": "downloads.js",
        }
        for template_name, asset_name in targets.items():
            with self.subTest(template=template_name):
                template = Path("app/templates", template_name).read_text(encoding="utf-8")
                asset = Path("app/static/js", asset_name)
                source = asset.read_text(encoding="utf-8")
                marker = f"path='js/{asset_name}'"

                self.assertIn(marker, template)
                script_tag = template.split(marker, 1)[1].split("</script>", 1)[0]
                self.assertRegex(script_tag, r"\?v=20\d{6}[a-z]")
                self.assertNotIn("defer", script_tag)
                self.assertGreater(len(source), 10_000)
                self.assertNotIn("{{", source)
                self.assertNotIn("{%", source)

    def test_organize_page_css_is_external_and_loaded_before_content(self) -> None:
        template = Path("app/templates/organize.html").read_text(encoding="utf-8")
        css = Path("app/static/css/organize.css").read_text(encoding="utf-8")

        marker = "path='css/organize.css'"
        self.assertIn(marker, template)
        stylesheet_tag = template.split(marker, 1)[1].split(">", 1)[0]
        self.assertRegex(stylesheet_tag, r"\?v=20\d{6}[a-z]")
        self.assertNotIn("<style>", template)
        self.assertIn(".organize-rules-nav-card", css)
        self.assertIn(".recognition-knowledge-dialog", css)

    def test_motion_runtime_is_limited_to_pages_that_use_it(self) -> None:
        template = Path("app/templates/base.html").read_text(encoding="utf-8")
        expected = "['dashboard', 'agent', 'downloads', 'guangya_strm', 'local_media', 'logs', 'rss']"

        self.assertIn(f"active in {expected}", template)
        condition = template.index("{% if active in")
        gsap = template.index("js/vendor/gsap.min.js")
        motion = template.index("js/motion.js")
        condition_end = template.index("{% endif %}", motion)
        app = template.index("js/app.js")
        self.assertLess(condition, gsap)
        self.assertLess(gsap, motion)
        self.assertLess(motion, condition_end)
        self.assertLess(condition_end, app)

    def test_download_tabs_expose_accessible_roles_and_stable_panels(self) -> None:
        template = Path("app/templates/downloads.html").read_text(encoding="utf-8")
        script = Path("app/static/js/downloads.js").read_text(encoding="utf-8")

        self.assertIn('role="tablist"', template)
        self.assertEqual(template.count('role="tab"'), 3)
        self.assertEqual(template.count('role="tabpanel"'), 3)
        self.assertIn("event.key==='ArrowRight'", script)
        self.assertIn("qbDisplaySignature", script)
        self.assertIn("updateQbLiveRows", script)
        self.assertIn('data-qb-progress-fill', script)
        self.assertIn('id="downloadTabStatus"', template)
        self.assertNotIn("transfer:qb.transfer", script)

    def test_recent_media_async_update_replaces_one_stable_result_plane(self) -> None:
        template = Path("app/templates/media_recent.html").read_text(encoding="utf-8")

        self.assertEqual(template.count("data-media-recent-results"), 3)
        self.assertIn("currentResults.replaceWith(nextResults)", template)
        self.assertIn("currentReset.classList.toggle('is-hidden'", template)
        self.assertIn("currentReset.setAttribute('aria-hidden'", template)
        self.assertNotIn("currentShell.replaceWith", template)


if __name__ == "__main__":
    unittest.main()
