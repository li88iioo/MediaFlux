from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.datastructures import URL

from app import web


class _Request:
    def url_for(self, name: str, **params: object) -> URL:
        if name != "static":
            raise AssertionError(name)
        return URL(f"https://example.invalid/static/{params['path']}")


class StaticAssetUrlTests(unittest.TestCase):
    def tearDown(self) -> None:
        web._static_asset_digest.cache_clear()

    def test_static_url_tracks_actual_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            static_root = Path(root).resolve()
            asset = static_root / "js" / "app.js"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"first")
            context = {"request": _Request()}
            with mock.patch.object(web, "STATIC_DIR", static_root):
                first = web.static_url(context, "js/app.js")
                asset.write_bytes(b"second-version")
                second = web.static_url(context, "js/app.js")

        self.assertRegex(first, r"^/static/js/app\.js\?v=[0-9a-f]{16}$")
        self.assertRegex(second, r"^/static/js/app\.js\?v=[0-9a-f]{16}$")
        self.assertNotEqual(first, second)

    def test_static_url_rejects_escape_and_directory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            static_root = Path(root).resolve()
            (static_root / "js").mkdir(parents=True)
            context = {"request": _Request()}
            with mock.patch.object(web, "STATIC_DIR", static_root):
                for path in ("../secret", "/etc/passwd", "js", "js//app.js"):
                    with self.subTest(path=path), self.assertRaises(
                        (ValueError, FileNotFoundError)
                    ):
                        web.static_url(context, path)

    def test_template_global_uses_the_single_static_helper(self) -> None:
        self.assertIs(web.templates.env.globals["static_url"], web.static_url)


if __name__ == "__main__":
    unittest.main()


class FocusStylePolicyTests(unittest.TestCase):
    """全站焦点样式：点击不画绿色高亮框；键盘焦点只由 main.css/core-layout.css 的中性描边提供。"""

    ROOT = Path(__file__).resolve().parents[1] / "app" / "static" / "css"

    def test_no_stylesheet_paints_green_focus_rings(self):
        import re

        rule = re.compile(r"([^{}]*:focus[^{}]*)\{([^{}]*)\}")
        green = re.compile(r"(outline|box-shadow|border(-[a-z]+)?)\s*:[^;]*(var\(--(accent|mf-brand|discovery-amber|dashboard-accent)|#65f0c2)", re.I)
        offenders = []
        for path in sorted(self.ROOT.rglob("*.css")):
            for selector, body in rule.findall(path.read_text()):
                parts = [part for part in re.split(r",(?![^()]*\))", selector) if part.strip()]
                if parts and all(":focus" in part for part in parts) and green.search(body):
                    offenders.append(f"{path.name}: {selector.strip()[:80]}")
        self.assertEqual(offenders, [])

    def test_global_policy_hides_pointer_focus_and_keeps_keyboard_ring(self):
        for name in ("main.css", "core-layout.css"):
            source = (self.ROOT / name).read_text()
            with self.subTest(stylesheet=name):
                self.assertIn(":focus:not(:focus-visible) { outline: none; }", source)
                self.assertIn(":focus-visible { outline: 2px solid color-mix(in srgb, currentColor 40%, transparent);", source)
