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
    assert "#organizeDeleteBtn, #organizeReorganizeBtn { grid-column: 1 / -1; }" in css
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
    assert ".recognition-knowledge-dialog{width:min(1200px,100%);height:auto}" in css
    assert ".recognition-knowledge-body{grid-template-columns:minmax(440px,1.16fr) minmax(340px,.84fr);gap:16px;min-height:560px;padding:16px}" in css
    assert ".recognition-knowledge-toolbar{display:grid;grid-template-columns:minmax(112px,.72fr) minmax(180px,1.28fr) 136px auto" in css
    assert ".rules-workbench-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "#newPreprocessRuleBtn,#newRecognitionKnowledgeBtn,#newTmdbRegexRuleBtn{display:none}" in css
    assert ".rules-workbench-body{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr);min-height:0;padding:12px;overflow:hidden}" in css
    assert ".rules-workbench-body>[data-rules-panel][hidden]{display:none!important}" in css
    assert ".tmdb-regex-dialog{height:calc(100dvh - 16px" in css
    assert ".rules-workbench-body>.tmdb-regex-ledger .tmdb-regex-table-frame{flex:1 1 auto;height:auto;min-height:0}" in css
    assert ".preprocess-toolbar-actions{display:flex;flex:0 0 auto;flex-direction:row" in css
    assert "#tmdbRegexLedgerPanel .tmdb-regex-toolbar{align-items:center;flex-direction:row" in css
    assert ".recognition-knowledge-toolbar{display:grid;grid-template-columns:minmax(0,1fr) 112px" in css
    assert ".recognition-knowledge-toolbar-copy{display:none}" in css
    assert ".rules-workbench-body>.tmdb-regex-editor .tmdb-regex-editor-scroll{flex:1 1 auto;min-height:0;max-height:none;overflow:auto}" in css
    assert ".rules-workbench-body>.tmdb-regex-editor .tmdb-regex-editor-actions{position:static;flex:0 0 auto}" in css
    assert ".recognition-knowledge-actions{padding-bottom:calc(10px + env(safe-area-inset-bottom,0px))}" in css


def test_rule_management_modals_expose_mobile_ledger_editor_tabs_and_single_row_knowledge_controls():
    template = (ROOT / "app/templates/organize.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/organize.js").read_text(encoding="utf-8")

    assert template.count('class="rules-workbench-tabs"') == 3
    assert template.count('data-rules-tab="ledger"') == 3
    assert template.count('data-rules-tab="editor"') == 3
    assert template.count('data-rules-panel="ledger"') == 3
    assert template.count('data-rules-panel="editor"') == 3
    for prefix in ("preprocess", "recognitionKnowledge", "tmdbRegex"):
        assert f'id="{prefix}LedgerTab"' in template
        assert f'id="{prefix}EditorTab"' in template
        assert f'id="{prefix}LedgerPanel"' in template
        assert f'id="{prefix}EditorPanel"' in template

    toolbar_start = template.index('class="tmdb-regex-toolbar recognition-knowledge-toolbar"')
    toolbar_end = template.index('id="recognitionKnowledgeTableBody"', toolbar_start)
    toolbar = template[toolbar_start:toolbar_end]
    assert toolbar.index('id="recognitionKnowledgeSearch"') < toolbar.index('id="recognitionKnowledgeFilter"') < toolbar.index('id="newRecognitionKnowledgeBtn"')

    assert "function createResponsiveRulesWorkbench(modalElement)" in script
    assert "panel.hidden=mobile&&!selected" in script
    assert "preprocessRulesWorkbench.activate('ledger',{resetScroll:true})" in script
    assert "recognitionKnowledgeWorkbench.activate('ledger',{resetScroll:true})" in script
    assert "regexRulesWorkbench.activate('ledger',{resetScroll:true})" in script
    assert "openPreprocessRuleEditor(rule)" in script
    assert "openRecognitionKnowledgeEditor(item={})" in script
    assert "openRegexRuleEditor(rule)" in script
    assert "getElementById('preprocessEditorTab').addEventListener('click',()=>openPreprocessRuleEditor())" in script
    assert "getElementById('recognitionKnowledgeEditorTab').addEventListener('click',()=>openRecognitionKnowledgeEditor())" in script
    assert "getElementById('tmdbRegexEditorTab').addEventListener('click',()=>openRegexRuleEditor())" in script


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
