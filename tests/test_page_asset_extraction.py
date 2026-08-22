from __future__ import annotations

import unittest
from pathlib import Path


class PageAssetExtractionTests(unittest.TestCase):
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

        self.assertIn("path='css/organize.css'", template)
        self.assertIn("path='css/organize.css') }}?v=20260821d", template)
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


if __name__ == "__main__":
    unittest.main()
