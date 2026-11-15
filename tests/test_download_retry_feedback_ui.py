"""执行真实页面函数，验证无操作回执不冒报成功且跳过原因可见。"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), '需要 Node.js 执行页面函数')
class DownloadRetryFeedbackUITests(unittest.TestCase):
    def run_js(self, source):
        result = subprocess.run(['node', '--input-type=module'], input=source, text=True,
                                capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_rss_mark_zero_is_warning_and_positive_count_is_success(self):
        template = Path('app/templates/rss.html').read_text()
        function = 'async function batchMark' + template.split('async function batchMark', 1)[1].split('async function dlEntry', 1)[0]
        result = self.run_js('''
let updated=0;
const alerts=[];
const batchRequest=async()=>({updated});
const appAlert=async value=>alerts.push(value);
const loadEntries=()=>{};
''' + function + '''
await batchMark(false);
updated=1;
await batchMark(false);
console.log(JSON.stringify(alerts));
''')
        self.assertEqual(result[0]['type'], 'warning')
        self.assertIn('状态未改变', result[0]['message'])
        self.assertEqual(result[1]['type'], 'success')
        self.assertIn('已更新 1', result[1]['message'])

    def test_skipped_retry_displays_backend_reason_without_changing_counts(self):
        script = Path('app/static/js/downloads.js').read_text()
        function = 'async function resubmitIssuesBatch' + script.split('async function resubmitIssuesBatch', 1)[1].split('async function clearIssuesBatch', 1)[0]
        result = self.run_js('''
const selectedIssueIds=new Set([334]);
let issueBatchBusy=false;
const issueActionBusy=new Set();
const alerts=[];
const appConfirm=async()=>true;
const appAlert=value=>alerts.push(value);
const syncIssueSelectionControls=()=>{};
const syncVisibleIssueActions=()=>{};
const loadIssues=async()=>{};
const loadOverview=async()=>{};
const loadLogs=async()=>{};
const downloadIssuePage=1,downloadLogPage=1;
const api=async()=>({ok:true,json:async()=>({succeeded:0,partial:0,failed:0,skipped:1,
    items:[{source_request_id:334,outcome:'skipped',error:'光鸭任务仍在处理'}]})});
''' + function + '''
await resubmitIssuesBatch('guangya');
console.log(JSON.stringify(alerts));
''')
        self.assertEqual(result[0]['type'], 'warning')
        self.assertIn('跳过 1 条', result[0]['message'])
        self.assertIn('请求 #334：光鸭任务仍在处理', result[0]['message'])
