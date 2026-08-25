from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _css(name: str) -> str:
    return (ROOT / "app/static/css" / name).read_text(encoding="utf-8")


def test_shared_mobile_modals_use_dynamic_viewport_and_single_scroll_owner():
    css = _css("main.css")

    assert ".media-config-modal" in css and "overflow: hidden" in css
    assert re.search(r"\.media-config-dialog \{[^}]*100dvh[^}]*safe-area-inset-bottom", css)
    assert re.search(r"\.organize-detail-dialog \{[^}]*100dvh[^}]*safe-area-inset-bottom", css)
    assert re.search(r"\.organize-detail-body \{[^}]*min-height: 0[^}]*flex: 1 1 auto[^}]*overflow-y: auto", css)
    assert re.search(r"\.organize-detail-footer \{[^}]*flex: 0 0 auto", css)
    assert ".organize-detail-actions { display: grid; grid-template-columns: repeat(2,minmax(0,1fr));" in css
    assert "#organizeReorganizeBtn { grid-column: 1 / -1; }" in css
    assert re.search(r"\.settings-dir-dialog \{[^}]*100dvh[^}]*safe-area-inset-bottom", css)
    assert ".rss-sub-modal { display: none;" in css
    rss_overlay = css.split(".rss-sub-modal {", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in rss_overlay
    assert "overflow-y: auto" not in rss_overlay


def test_organize_rule_and_knowledge_modals_keep_actions_inside_visible_viewport():
    css = _css("organize.css")

    assert "recognition-knowledge-dialog" in css
    assert "100dvh" in css
    assert "env(safe-area-inset-bottom,0px)" in css
    assert ".recognition-knowledge-ledger>.tmdb-regex-toolbar,.recognition-knowledge-search{flex:0 0 auto}" in css
    assert ".recognition-knowledge-actions{display:flex;flex:0 0 auto" in css
    assert ".recognition-knowledge-ledger>.tmdb-regex-toolbar{align-items:center;flex-direction:row" in css
    assert ".recognition-knowledge-ledger>.tmdb-regex-toolbar .jump-btn{width:auto" in css
    assert ".recognition-knowledge-actions{align-items:stretch;flex-direction:column" in css
    assert ".tmdb-regex-editor-actions{position:sticky;bottom:0;z-index:2}" in css
    assert ".tmdb-regex-editor-scroll{flex:none;max-height:none;overflow:visible}" in css


def test_model_picker_and_already_repaired_get_workspace_use_safe_dynamic_viewport():
    settings_css = _css("settings-agent.css")
    main_css = _css("main.css")

    assert re.search(r"\.agent-model-picker-dialog \{[^}]*100dvh[^}]*safe-area-inset-bottom", settings_css)
    assert re.search(r"@media \(max-width: 560px\)[\s\S]*?\.agent-model-picker-dialog \{[^}]*100dvh", settings_css)
    assert re.search(r"@media \(max-width: 560px\)[\s\S]*?\.discovery-dialog \{[^}]*100dvh", main_css)
    assert re.search(r"\.discovery-resource-bulk \{[^}]*safe-area-inset-bottom", main_css)


def test_named_mobile_modal_markup_still_uses_the_repaired_dialog_shells():
    logs = (ROOT / "app/templates/logs.html").read_text(encoding="utf-8")
    organize = (ROOT / "app/templates/organize.html").read_text(encoding="utf-8")
    rss = (ROOT / "app/templates/rss.html").read_text(encoding="utf-8")

    assert 'id="organizeDetailModal"' in logs and "organize-detail-dialog" in logs
    for modal_id in ("preprocessRulesModal", "recognitionKnowledgeModal", "tmdbRegexRulesModal"):
        assert f'id="{modal_id}" class="media-config-modal"' in organize
    assert 'id="mediaSubModal" class="rss-sub-modal subscription-modal"' in rss
