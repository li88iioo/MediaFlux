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
const accepted=await resubmitIssuesBatch('guangya');
console.log(JSON.stringify({alerts,accepted,selected:[...selectedIssueIds]}));
''')
        self.assertEqual(result['alerts'][0]['type'], 'warning')
        self.assertIn('跳过 1 条', result['alerts'][0]['message'])
        self.assertIn('请求 #334：光鸭任务仍在处理', result['alerts'][0]['message'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['selected'], [334])

    def test_batch_retry_only_deselects_items_with_accepted_receipts(self):
        script = Path('app/static/js/downloads.js').read_text()
        function = 'async function resubmitIssuesBatch' + script.split('async function resubmitIssuesBatch', 1)[1].split('async function clearIssuesBatch', 1)[0]
        cases = (
            ({'ok': False, 'error': '凭据已过期'}, [334, 335, 336, 337], False, 'error'),
            ({'ok': False, 'failed': 1, 'items': [
                {'source_request_id': 334, 'outcome': 'failed', 'error': '明确拒绝'},
            ]}, [334, 335, 336, 337], False, 'error'),
            ({'ok': False, 'succeeded': 1, 'partial': 1, 'failed': 1, 'skipped': 1, 'items': [
                {'source_request_id': 334, 'outcome': 'succeeded'},
                {'source_request_id': 335, 'outcome': 'partial', 'error': '另一端失败'},
                {'source_request_id': 336, 'outcome': 'failed', 'error': '结果未知，请先核对，勿重复提交'},
                {'source_request_id': 337, 'outcome': 'skipped', 'duplicate': True, 'error': '已在处理'},
            ]}, [336, 337], True, 'warning'),
            ({'ok': True, 'succeeded': 4, 'items': [
                {'source_request_id': id, 'outcome': 'succeeded'} for id in range(334, 338)
            ]}, [], True, 'success'),
            ({}, [334, 335, 336, 337], False, 'warning'),
            ({'transport_error': True}, [334, 335, 336, 337], False, 'error'),
        )
        for receipt, selected, accepted, tone in cases:
            with self.subTest(receipt=receipt):
                result = self.run_js('''
const selectedIssueIds=new Set([334,335,336,337]);
let issueBatchBusy=false;
const issueActionBusy=new Set();
const alerts=[];
let posts=0;
const appConfirm=async()=>true;
const appAlert=value=>alerts.push(value);
const syncIssueSelectionControls=()=>{};
const syncVisibleIssueActions=()=>{};
const loadOverview=async()=>{};
const receipt=''' + json.dumps(receipt) + ''';
const api=async()=>{posts++;if(receipt.transport_error)throw new Error('connection lost');
    return {ok:true,json:async()=>receipt};};
''' + function + '''
const accepted=await resubmitIssuesBatch('qb');
console.log(JSON.stringify({alerts,accepted,selected:[...selectedIssueIds],posts,busy:issueBatchBusy}));
''')
                self.assertEqual(result['selected'], selected)
                self.assertEqual(result['accepted'], accepted)
                self.assertEqual(result['alerts'][0]['type'], tone)
                self.assertEqual(result['posts'], 1)
                self.assertFalse(result['busy'])
                if receipt.get('error'):
                    self.assertIn(receipt['error'], result['alerts'][0]['message'])
                if receipt.get('transport_error'):
                    self.assertIn('状态未知', result['alerts'][0]['message'])
                    self.assertIn('不要立即重复提交', result['alerts'][0]['message'])
