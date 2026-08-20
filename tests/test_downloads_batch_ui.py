"""下载实时任务多选与批量工具栏前端契约。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/downloads.html"
BASE_TEMPLATE = ROOT / "app/templates/base.html"
STYLES = ROOT / "app/static/css/main.css"


class DownloadBatchUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = TEMPLATE.read_text(encoding="utf-8") + (ROOT / "app/static/js/downloads.js").read_text(encoding="utf-8")
        self.base_template = BASE_TEMPLATE.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_task_toolbar_exposes_select_all_and_three_batch_actions(self):
        for contract in (
            'id="qbTaskToolbar"',
            'id="qbSelectAll"',
            'id="qbSelectionLabel"',
            'id="qbBulkResume"',
            'id="qbBulkPause"',
            'id="qbBulkDelete"',
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn('id="qbClearSelection"', self.template)
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkResume"[^>]+disabled.*?>.*?启动', re.S),
        )
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkPause"[^>]+disabled.*?>.*?停止', re.S),
        )
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkDelete"[^>]+disabled.*?>.*?删除', re.S),
        )

    def test_selection_is_hash_based_and_survives_background_refresh(self):
        for contract in (
            "const selectedQbHashes = new Set()",
            "let currentQbTasks = []",
            "function currentQbHashes()",
            "function syncQbSelectionControls()",
            "if(!available.has(hash))selectedQbHashes.delete(hash)",
            "data-qb-select",
            "selectAll.indeterminate",
            "let overviewRefreshQueued = false",
            "overviewRefreshQueued=true",
            "if(loading||qbActionBusy)return overviewPromise",
            "while(overviewRefreshQueued&&!qbActionBusy)",
            "const keepPreviousTasks=qb.error_code==='connection_failed'",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("selectedQbHashes.clear();\n    const taskCount", self.template)
        self.assertIn("document.addEventListener('visibilitychange',syncOverviewPolling)", self.template)
        self.assertIn("if(document.hidden)", self.template)

    def test_background_reads_keep_previous_data_and_ignore_stale_pages(self):
        for contract in (
            "async function readApiResponse(response)",
            "if(!response.ok)throw new Error",
            "let downloadLogRequestSerial = 0",
            "let downloadIssueRequestSerial = 0",
            "if(requestSerial!==downloadLogRequestSerial)return false",
            "if(requestSerial!==downloadIssueRequestSerial)return false",
            "if(!hasLoadedDownloadLogs)",
            "if(!hasLoadedDownloadIssues)",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("currentLogIds=[];document.getElementById('logList')", self.template)
        self.assertNotIn("currentIssueIds=[];document.getElementById('issueList')", self.template)
        self.assertNotIn("then(r=>r.json()).then(d=>", self.template)

    def test_batch_actions_send_arrays_and_delete_keeps_files(self):
        for contract in (
            "async function runQbAction(action, hashes, clearSelection=false)",
            "JSON.stringify({hashes:unique})",
            "qbBulkAction('resume')",
            "qbBulkAction('pause')",
            "qbBulkAction('delete')",
            "只从 qBittorrent 移除任务，不删除已经下载的文件。",
            "selectedQbHashes.clear()",
        ):
            self.assertIn(contract, self.template)

    def test_toolbar_has_stable_geometry_and_mobile_layout(self):
        for contract in (
            ".qb-task-toolbar { min-height: 42px",
            ".qb-task-title-layout",
            ".qb-task-row.is-selected td",
            ".qb-task-toolbar { display: flex; align-items: center; justify-content: space-between; flex-direction: row;",
            ".qb-bulk-actions { display: flex; align-items: center; justify-content: flex-end; gap: 4px;",
        ):
            self.assertIn(contract, self.styles)

    def test_qb_523_states_use_safe_and_stable_task_controls(self):
        for contract in (
            "const qbPausedStates = new Set(['pausedDL','pausedUP','stoppedDL','stoppedUP'])",
            "const qbStoppableStates = new Set(",
            "const qbTransientStates = new Set(['checkingDL','checkingUP','checkingResumeData','moving'])",
            "function qbTaskControl(state)",
            "return {action:'',label:'处理中',icon:'loader-circle',disabled:true}",
            "control.disabled?'disabled aria-disabled=\"true\"'",
            "checkingResumeData:'检查恢复数据'",
            "forcedMetaDL:'强制获取元数据'",
            "width:76px;height:26px",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("const active=['downloading','forcedDL','metaDL'].includes(t.state)", self.template)
        self.assertNotIn("${active", self.template)
        self.assertGreaterEqual(self.template.count('title="${control.label}"'), 2)

    def test_issue_and_log_tabs_expose_stable_batch_selection_toolbars(self):
        for contract in (
            'id="issueBatchToolbar"',
            'id="issueSelectAll"',
            'id="issueSelectionLabel"',
            'id="issueBulkQb"',
            'id="issueBulkGuangya"',
            'id="issueBulkBoth"',
            'id="issueBulkClear"',
            'id="logBatchToolbar"',
            'id="logSelectAll"',
            'id="logSelectionLabel"',
            'id="logBulkClear"',
            'data-issue-select',
            'data-log-select',
            "const selectedIssueIds = new Set()",
            "const selectedLogIds = new Set()",
            "function syncIssueSelectionControls()",
            "function syncLogSelectionControls()",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn('id="issueClearSelection"', self.template)
        self.assertNotIn('id="logClearSelection"', self.template)
        for contract in (
            ".download-batch-toolbar { min-height: 42px",
            ".download-select-layout",
            ".download-batch-row.is-selected td",
            ".download-batch-actions .rss-btn { min-width: 68px; min-height: 30px",
            ".download-batch-toolbar { display: flex; align-items: center; justify-content: space-between; flex-direction: row;",
            ".download-batch-actions { width: auto; display: flex; align-items: center; justify-content: flex-end; gap: 4px;",
        ):
            self.assertIn(contract, self.styles)

    def test_download_logs_use_fixed_columns_and_safe_source_labels(self):
        for contract in (
            'class="dl-table download-log-table"',
            'class="download-log-source-col"',
            'function downloadLogSourceSvg(key)',
            'function downloadLogSourceHtml(source)',
            "label:'qBittorrent',tone:'is-qb'",
            "label:'光鸭',tone:'is-guangya'",
            '<circle cx="12" cy="12" r="10" fill="currentColor"/>',
            'fill="#ffd45c"',
            'class="source-label download-log-source-badge ${item.tone}"',
            'function downloadLogTarget(path)',
            "label:'种子直链',detail:'HTTPS · 已脱敏'",
            "label:'磁力链接',detail:'MAGNET · 已脱敏'",
            'class="download-log-target-cell desktop-only-cell"',
            'class="download-log-mobile-details"',
        ):
            self.assertIn(contract, self.template)
        for contract in (
            ".download-log-table { table-layout: fixed; }",
            ".download-log-table .download-log-title-cell { min-width: 0; max-width: none; }",
            ".download-log-source-badge { display: inline-flex;",
            ".download-log-source-icon svg { width: 24px; height: 24px; display: block; }",
            ".download-log-source-layout .download-row-check { width: 20px; height: 24px; min-height: 24px; display: flex; align-items: center;",
            ".download-log-source-badge.is-qb { color: var(--info); }",
            ".download-log-source-badge.is-guangya { color: var(--accent); }",
            ".download-log-target { display: grid;",
            ".download-log-mobile-details { display: none; }",
        ):
            self.assertIn(contract, self.styles)
        self.assertNotIn('title="${attr(r.path)}"', self.template)
        self.assertNotIn("${esc(r.path||'-')}</td>", self.template)
        self.assertRegex(
            self.template,
            re.compile(
                r'class="download-log-source-cell".*?data-log-select.*?downloadLogSourceHtml\(r\.source\)',
                re.S,
            ),
        )
        self.assertIn('class="status-pill ${st[1]} download-log-mobile-status"', self.template)
        self.assertIn(
            ".dl-tab-btn { min-width: 0; flex: 1 1 0;",
            self.styles,
        )

    def test_main_stylesheet_cache_key_includes_download_batch_toolbar_release(self):
        match = re.search(r"css/main\.css'\) }}\?v=(\d{8}[a-z])", self.base_template)
        self.assertIsNotNone(match, "main.css 应带静态资源缓存版本")
        self.assertGreaterEqual(match.group(1), "20260809f")

    def test_issue_batch_actions_support_qb_guangya_both_and_safe_cleanup(self):
        for contract in (
            "async function resubmitIssuesBatch(target)",
            "resubmitIssuesBatch('qb')",
            "resubmitIssuesBatch('guangya')",
            "resubmitIssuesBatch('both')",
            "'/api/downloads/issues/batch/resubmit'",
            "async function clearIssuesBatch()",
            "'/api/downloads/issues/batch/clear'",
            "不会删除下载任务、实际文件、原请求或下载日志",
            "不支持的记录会跳过",
        ):
            self.assertIn(contract, self.template)

    def test_log_batch_cleanup_is_explicitly_non_destructive_to_tasks_and_files(self):
        for contract in (
            "async function clearLogsBatch()",
            "'/api/downloads/logs/batch/clear'",
            "不会停止 qB/光鸭任务",
            "不会删除已下载文件或下载请求",
            "currentLogIds.forEach",
        ):
            self.assertIn(contract, self.template)
