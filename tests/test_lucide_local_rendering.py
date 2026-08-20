from __future__ import annotations

import unittest
from pathlib import Path


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

    def test_shared_renderer_accepts_a_local_root(self) -> None:
        source = Path("app/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("function renderIcons(root)", source)
        self.assertIn("root: root || document", source)
        self.assertIn("window.renderLucideIcons = renderIcons", source)


if __name__ == "__main__":
    unittest.main()
