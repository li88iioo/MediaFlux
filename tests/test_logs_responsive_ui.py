import json
import subprocess
from pathlib import Path


def test_logs_template_markup_contract():
    template_path = Path("app/templates/logs.html")
    assert template_path.is_file(), "logs.html 模板文件必须存在"
    content = template_path.read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8")

    # 验证整理日志中筛选栏结构与搜索行容器
    assert "logs-filterbar" in content
    assert 'id="orgOrigin"' in content
    assert 'id="orgStatus"' in content
    assert "logs-search-row" in content
    assert 'id="orgKeyword"' in content
    assert "logs-search-btn" in content

    # 验证批量操作栏三按钮契约
    assert 'id="organizeBatchRenameBtn"' in content
    assert 'id="organizeBatchRevertBtn"' in content
    assert 'id="organizeBatchDeleteBtn"' in content

    # 验证项目实时日志两行排布结构契约
    assert "runtime-log-filterbar" in content
    assert 'id="runtimeLevel"' in content
    assert "runtime-searchbox" in content
    assert 'id="runtimeFilter"' in content
    assert "runtime-log-actions" in content
    assert "runtime-log-meta-row" in content
    assert 'id="runtimeAutoScroll"' in content
    assert 'id="runtimeState"' in content
    assert "runtime-btn-group" in content
    assert 'id="runtimePauseBtn"' in content
    assert 'id="runtimeClearBtn"' in content

    # 整理详情去除重复提示与正文危险区，回收站操作统一收入口底栏。
    template = template_path.read_text(encoding="utf-8")
    assert "organize-correction-hint" not in template
    assert "organizeDangerZone" not in content
    footer = template[template.index('<footer class="organize-detail-footer">'):template.index("</footer>", template.index('<footer class="organize-detail-footer">'))]
    assert footer.index('id="organizeDeleteBtn"') < footer.index('id="organizeReturnBtn"')
    assert "deleteButton.hidden=!data.allowed_actions.delete" in content

    # 首次异步加载完成前分页器不可见，避免在短占位行下方闪入内容区。
    assert 'id="organizePagination" aria-label="整理日志分页" aria-busy="true" aria-hidden="true"' in template
    assert 'id="organizePrev" disabled' in template
    assert 'id="organizeNext" disabled' in template
    assert "function revealOrganizePagination()" in content
    assert "pagination.classList.remove('is-initializing')" in content

    # 新详情卡片保留解析、流向、规格标签，并对白名单外角色降级。
    assert 'id="organizeReleaseParseSection" hidden' in template
    assert 'class="organize-flow-hero"' in content
    assert 'class="organize-item organize-item-card"' in content
    assert "['video','subtitle','nfo','image','metadata'].includes(rawRole)" in content
    assert 'class="organize-item-role role-${role}"' in content
    assert "fn.includes('DV')" not in content
    assert "(?:^|[^A-Z0-9])" in content
    assert "HDR10\\\\+|HDR10PLUS" in content
    assert "parent_directory:'父目录'" in content
    assert "release_context:'发布信息'" in content
    assert "explicit_marker:'显式标记'" in content


def test_logs_responsive_css_contract():
    css_path = Path("app/static/css/main.css")
    assert css_path.is_file(), "main.css 样式表文件必须存在"
    css = css_path.read_text(encoding="utf-8")

    # 基础样式中需包含 search-row 定义
    assert ".logs-search-row" in css
    assert ".table-pagination.is-initializing { visibility: hidden; pointer-events: none; }" in css

    # 移动端/小屏响应式中需包含整理日志的分行规则：来源/状态、搜索行、批量改名/回退两列、回收站通栏
    assert ".logs-filterbar:not(.runtime-log-filterbar)" in css
    assert "#orgOrigin" in css
    assert "#orgStatus" in css
    assert "#organizeBatchRenameBtn" in css
    assert "#organizeBatchRevertBtn" in css
    assert "#organizeBatchDeleteBtn" in css
    assert "grid-column: 1 / -1" in css
    assert ".organize-detail-actions .jump-btn, .organize-detail-actions .btn { width: auto; min-height: 38px; height: 38px; font-size: 12px; font-weight: 600; border-radius: 7px; }" in css
    assert ".organize-detail-actions .jump-btn, .organize-detail-actions .btn { width: 100%; min-width: 0; min-height: 42px; height: 42px; white-space: nowrap; }" in css
    assert "#organizeDeleteBtn, #organizeReorganizeBtn { grid-column: 1 / -1; }" in css
    assert ".organize-flow-hero { grid-template-columns: 1fr; gap: 8px; }" in css
    assert ".organize-flow-connector svg { transform: rotate(90deg); }" in css
    assert ".organize-meta-chip { min-width: 0; white-space: normal; overflow-wrap: anywhere; }" in css

    # 实时日志移动端需包含级别+搜索框并排、控制条整行两端并排
    assert ".runtime-log-filterbar" in css
    assert ".runtime-level-select" in css
    assert ".runtime-searchbox" in css
    assert ".runtime-log-actions" in css
    assert ".runtime-log-meta-row" in css
    assert ".runtime-btn-group" in css


def test_logs_number_only_motion_contract():
    template = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
    css = Path("app/static/css/main.css").read_text(encoding="utf-8")

    assert 'id="refreshLogsBtn" aria-busy="false"' in template
    assert "window.MFAnim.countUp" in template
    assert "window.MFAnim.staggerIn" not in template
    assert "window.MFAnim.crossfade" not in template
    assert "Promise.all([loadOverview(), loadOrganize()])" in template
    assert "lockElementHeight(body.closest('.table-wrap'))" in template
    assert "requestAnimationFrame(() => requestAnimationFrame" in template
    assert "classList.add('is-refreshing')" in template

    assert "#refreshLogsBtn.is-refreshing svg" in css
    assert "@keyframes logs-refresh-spin" in css
    assert "#refreshLogsBtn.is-refreshing svg { animation: none; }" in css


def test_logs_media_tags_use_release_token_boundaries():
    source = Path("app/static/js/logs.js").read_text(encoding="utf-8")
    start = source.index("function _extractMediaTags(filename){")
    end = source.index("\nfunction _formatReleasePosition", start)
    function_source = source[start:end]
    samples = [
        "Adventure.Time.DVDRip.HDRip.4Kids.mkv",
        "Show_2160P_HEVC_DV_WEB-DL_Atmos.mkv",
        "Show.1080p.H264.HDR10+.WEB-DL.AAC.mkv",
    ]
    script = (
        function_source
        + "\nconsole.log(JSON.stringify("
        + json.dumps(samples, ensure_ascii=False)
        + ".map(_extractMediaTags)));"
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == [
        [],
        ["4K UHD", "HEVC", "Dolby Vision", "WEB-DL"],
        ["1080p", "AVC", "HDR10+", "WEB-DL"],
    ]
