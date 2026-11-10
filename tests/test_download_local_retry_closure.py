"""仅重试本地整理：沿原下载请求恢复，不再次提交下载器。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from starlette.requests import Request

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.modules.download_tracker import DownloadTracker
from app.repositories.download_requests import (
    can_retry_download_local_import,
    retry_download_local_import,
)
from app.routes import downloads_api
from tests.support import isolated_test_database


@pytest.fixture
def retry_case(tmp_path):
    with isolated_test_database('local-retry-closure.db'):
        path = tmp_path / 'downloads' / 'Movie.mkv'
        path.parent.mkdir()
        path.write_bytes(b'media')
        rid, _ = download_requests_repo.create_download_request('local-retry', 'magnet', title='Movie')
        download_requests_repo.update_download_request(rid, status='completed', targets='qb', qb_status='completed',
                                   qb_task_id='a' * 40, qb_content_path=str(path),
                                   local_import_status='failed', local_import_attempts=8,
                                   local_import_error='原路径映射错误', local_import_completed_at=db.now())
        yield rid, path


def test_retry_rebinds_original_request_and_projects_completion(retry_case):
    rid, path = retry_case
    assert can_retry_download_local_import(download_requests_repo.get_download_request(rid))
    source = local_media_repo.create_local_media_source(name='fixed', qb_profile='', qb_path_prefix='',
                                         local_root=str(path.parent), owner='admin')
    with patch.object(downloads_api, 'require_api_login'), patch('app.modules.download_tracker.get_download_tracker') as wake:
        response = downloads_api.retry_local_import_issue(rid, Mock())
    assert response.status_code == 200
    assert json.loads(bytes(response.body))['local_import_status'] == 'pending'
    wake.return_value.reload.assert_called_once()
    row = download_requests_repo.get_download_request(rid)
    assert row is not None
    assert row['local_import_attempts'] == 0 and not row['local_import_error']
    assert row is not None
    assert row['notification_delivery_status'] == 'pending'
    assert rid in [r['id'] for r in download_requests_repo.list_active_download_requests(include_local_import=True)]
    scheduler = Mock()
    def enqueue(task, **kwargs):
        task_id, _created = local_media_repo.create_and_link_qb_local_media_task(rid, source, task.hash, task.content_path)
        return task_id
    scheduler.enqueue_completed_torrent.side_effect = enqueue
    tracker = DownloadTracker()
    with patch('app.modules.local_media_scheduler.get_local_media_scheduler', return_value=scheduler), patch.object(tracker, '_notify_completion'):
        # qB已自动删种也能使用持久的已完成路径，不调用下载提交。
        tracker._update_request(row, [], [], qb_available=False, gy_available=False)
    rebound = download_requests_repo.get_download_request(rid)
    assert rebound is not None
    task_id = int(rebound['local_import_target'].split(':')[1])
    local_media_repo.add_local_media_task_item(task_id, str(path), str(path.parent / 'Library.mkv'), role='video')
    local_media_repo.update_local_media_task(task_id, status='completed', completed_at=db.now())
    final = download_requests_repo.get_download_request(rid)
    assert final is not None
    assert final['local_import_status'] == 'completed' and final['qb_status'] == 'completed'
    assert final is not None
    assert not final['local_import_error']
    assert download_requests_repo.count_download_requests_requiring_attention(rid) == 0
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM download_requests').fetchone()[0] == 1


@pytest.mark.parametrize('fields', [
    {'status':'cancelled'}, {'status':'resubmitted'}, {'qb_status':'downloading'},
    {'qb_status':'manual_review'}, {'local_import_status':'pending'},
    {'local_import_status':'completed'}, {'local_import_target':'local-media-task:123'},
])
def test_retry_refuses_active_historical_or_bound_requests(retry_case, fields):
    rid, _path = retry_case
    download_requests_repo.update_download_request(rid, **fields)
    before_row = download_requests_repo.get_download_request(rid)
    assert before_row is not None
    before = dict(before_row)
    assert not can_retry_download_local_import(before)
    assert not retry_download_local_import(rid)
    after_row = download_requests_repo.get_download_request(rid)
    assert after_row is not None
    assert dict(after_row) == before


def test_retry_is_atomic_and_preserves_peer_backend(retry_case):
    rid, _path = retry_case
    download_requests_repo.update_download_request(rid, status='downloading', targets='both', gy_status='downloading', gy_task_id='remote-task')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry_download_local_import, [rid, rid]))
    assert sorted(results) == [False, True]
    row = download_requests_repo.get_download_request(rid)
    assert row is not None
    assert (row['status'],row['gy_status'],row['gy_task_id']) == ('downloading','downloading','remote-task')


def test_api_requires_auth_and_enabled_source(retry_case):
    from fastapi import HTTPException
    rid, _path = retry_case
    with pytest.raises(HTTPException):
        downloads_api.retry_local_import_issue(rid, cast(Request, SimpleNamespace(session={})))
    with patch.object(downloads_api, 'require_api_login'):
        assert downloads_api.retry_local_import_issue(rid, Mock()).status_code == 400
        assert downloads_api.retry_local_import_issue(999999, Mock()).status_code == 404
    get_download_request_result = download_requests_repo.get_download_request(rid)
    assert get_download_request_result is not None
    assert get_download_request_result['local_import_status'] == 'failed'


def test_attention_ui_exposes_only_local_retry_capability(retry_case):
    rid, _path = retry_case
    data = downloads_api._attention_json(download_requests_repo.get_download_request(rid))
    assert data['retry_local_import'] is True
    assert not data['retry_targets']['qb']['enabled']
    script = Path('app/static/js/downloads.js').read_text()
    assert 'r.retry_local_import?' in script
    assert 'data-issue-local-retry' in script
    assert '仅重试本地整理，不会重新下载' in script


def test_retry_ui_posts_only_local_action_and_keeps_busy_guard():
    import shutil
    import subprocess
    if not shutil.which('node'):
        pytest.skip('需要Node执行真实页面函数')
    script = Path('app/static/js/downloads.js').read_text()
    function = 'async function retryLocalImport' + script.split('async function retryLocalImport', 1)[1].split('async function clearIssue', 1)[0]
    harness = '''
const issueActionBusy=new Set();let issueBatchBusy=false;let succeeds=true;
const alerts=[],requests=[];const appConfirm=async()=>true;
const appAlert=value=>alerts.push(value);const syncIssueActionButtons=()=>{};
const syncIssueSelectionControls=()=>{};const loadIssues=async()=>{};
const loadOverview=async()=>{};const downloadIssuePage=1;
const api=async(path,options)=>{requests.push([path,options]);return {ok:succeeds,json:async()=>succeeds?{ok:true}:{error:'配置未修好'}}};
'''+function+'''
await retryLocalImport(12);succeeds=false;await retryLocalImport(12);
issueActionBusy.add(12);await retryLocalImport(12);
console.log(JSON.stringify({alerts,requests}));
'''
    result = subprocess.run(['node','--input-type=module'],input=harness,text=True,capture_output=True,check=True)
    data = json.loads(result.stdout)
    assert len(data['requests']) == 2
    assert all(r[0] == '/api/downloads/issues/12/retry-local-import' for r in data['requests'])
    assert data['alerts'][0]['title'] == '本地整理已重新排队'
    assert data['alerts'][1]['type'] == 'error'


def test_retry_http_endpoint_retains_login_and_csrf_boundaries():
    import re

    from fastapi.testclient import TestClient

    from tests.support import initialized_web_runtime

    with initialized_web_runtime(), patch('app.routes.auth.get_login_wallpaper', return_value=None):
        from app.main import create_app
        with TestClient(create_app()) as client:
            endpoint = '/api/downloads/issues/123/retry-local-import'
            assert client.post(endpoint).status_code == 401
            login = client.get('/login')
            search_result = re.search(r'name="csrf_token" value="([^"]+)"', login.text)
            assert search_result is not None
            token = search_result.group(1)
            from app.config import web_credentials
            username, password = web_credentials()
            response = client.post('/login', data={'username':username,'password':password,'csrf_token':token}, follow_redirects=False)
            assert response.status_code == 302
            assert client.post(endpoint).status_code == 403
