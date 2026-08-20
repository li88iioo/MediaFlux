from pathlib import Path
import re


def test_global_page_motion_keeps_cards_static_and_numbers_animated():
    root = Path(__file__).resolve().parents[1]
    motion = (root / "app/static/js/motion.js").read_text(encoding="utf-8")
    main_css = (root / "app/static/css/main.css").read_text(encoding="utf-8")
    dashboard_css = (root / "app/static/css/dashboard-workbench.css").read_text(encoding="utf-8")
    base = (root / "app/templates/base.html").read_text(encoding="utf-8")

    count_up = motion[motion.index("countUp(target"):motion.index("staggerIn(targets")]
    stagger = motion[motion.index("staggerIn(targets"):motion.index("shake(target")]
    crossfade = motion[motion.index("crossfade(oldEl"):motion.index("window.MFAnim = MFAnim")]

    assert "window.gsap.to(obj" in count_up
    assert "window.gsap.fromTo" not in stagger
    assert "el.style.opacity = '1'" in stagger
    assert "el.style.transform = 'none'" in stagger
    assert "window.gsap.timeline" not in crossfade
    assert "oldEl.hidden = true" in crossfade
    assert "newEl.hidden = false" in crossfade

    assert re.search(r"\.content\s*\{[^}]*animation:\s*none", main_css)
    assert ".dashboard-server-panel.active { animation: none; }" in main_css
    assert ".settings-panel.active { animation: none; }" in main_css
    assert ".organize-config-panel.active { animation: none; }" in main_css
    assert re.search(r"\.discovery-card\s*\{[^}]*animation:\s*none", main_css)
    assert ".dashboard-page .content { width: 100%; padding: 26px 28px 42px; animation: none; }" in dashboard_css
    assert "motion.js') }}?v=20260820a" in base
