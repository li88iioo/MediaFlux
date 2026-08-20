"""整理页滚动性能与稳定 DOM 契约。"""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OrganizeScrollPerformanceContractTests(unittest.TestCase):
    def test_organize_page_uses_scoped_low_paint_scroll_styles(self):
        template = ((ROOT / "app/templates/organize.html").read_text(encoding="utf-8") + (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8") + (ROOT / "app/static/css/organize.css").read_text(encoding="utf-8"))
        styles = (ROOT / "app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn("{% block body_class %}organize-page", template)
        self.assertIn("organize-rules-page", template)
        self.assertIn(".organize-page::before { display: none; }", styles)
        self.assertIn(".organize-page .organize-toggle-tile:hover", styles)
        self.assertIn(".organize-page .organize-control-card", styles)

    def test_status_polling_keeps_dom_nodes_stable_when_payload_is_unchanged(self):
        template = ((ROOT / "app/templates/organize.html").read_text(encoding="utf-8") + (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8") + (ROOT / "app/static/css/organize.css").read_text(encoding="utf-8"))

        self.assertIn('id="organizeStatusIcon"', template)
        self.assertIn('id="organizeStatusTitle"', template)
        self.assertIn('id="organizeStatusDetail"', template)
        self.assertIn("let lastStatusRenderKey='';", template)
        self.assertIn("if(renderKey===lastStatusRenderKey)return;", template)
        render_status = template.split("function renderStatus(data){", 1)[1].split("async function loadStatus", 1)[0]
        self.assertNotIn("replaceChildren", render_status)


if __name__ == "__main__":
    unittest.main()
