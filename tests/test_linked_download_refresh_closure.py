"""本地刷新结果经过下载通知合并后不能消失或被冒报完成。"""
from unittest.mock import patch

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.modules.local_media_notifications import (
    build_local_media_event,
    notify_local_media_task,
)
from app.modules.telegram_download_lifecycle import build_download_lifecycle_event
from app.modules.telegram_notification_center import NotificationPublishResult
from tests.support import isolated_test_database


@pytest.fixture
def notification_case():
    with isolated_test_database('linked-refresh-closure.db'):
        sid = local_media_repo.create_local_media_source(name='audit', qb_profile='', qb_path_prefix='', local_root='/source', owner='admin')
        tid = local_media_repo.create_local_media_task(sid, '', '/source/Film.mkv', owner='admin', trigger='qb_completed')
        local_media_repo.add_local_media_task_item(tid, '/source/Film.mkv', '/library/Film.mkv', role='video', action='move')
        local_media_repo.update_local_media_task(tid, status='completed', completed_at=db.now())
        rid, _ = download_requests_repo.create_download_request('linked-refresh', 'magnet', title='Film', chat_id='test')
        download_requests_repo.update_download_request(rid, status='completed', targets='qb', qb_status='completed', local_import_status='completed', local_import_target=f'local-media-task:{tid}')
        with patch('app.modules.telegram_download_lifecycle.get_notification_thread_event', return_value=None):
            yield rid, tid, sid


@pytest.mark.parametrize('refresh_status', ['failed','queued','completed','skipped'])
def test_linked_lifecycle_keeps_refresh_result(notification_case, refresh_status):
    rid, tid, sid = notification_case
    warnings = ['媒体库刷新失败：入队失败'] if refresh_status == 'failed' else []
    local_media_repo.update_local_media_task(tid, warning='；'.join(warnings))
    result = {'status':'completed','moved':['/library/Film.mkv'],'warnings':warnings,'media_refresh_status':refresh_status}
    standalone = build_local_media_event(local_media_repo.get_local_media_task(tid), local_media_repo.get_local_media_source(sid), result)
    with patch('app.modules.local_media_notifications.config.get_bool', return_value=True), patch('app.modules.telegram_download_lifecycle.publish_notification_thread', return_value=NotificationPublishResult(True)) as publish:
        assert notify_local_media_task(tid, result)
        linked = publish.call_args.args[1]
    if refresh_status == 'failed':
        refresh_text = dict(linked.fields)['媒体库']
        assert isinstance(refresh_text, str)
        assert '刷新失败' in refresh_text
        refresh_text = dict(standalone.fields)['媒体库刷新']
        assert isinstance(refresh_text, str)
        assert '刷新失败' in refresh_text
        assert linked.state == 'error' and '部分完成' in linked.title
    else:
        assert dict(linked.fields)['媒体库'] == dict(standalone.fields)['媒体库刷新']
        assert '归档完成' in linked.title
        assert '下载与入库完成' not in linked.title
    get_download_request_result = download_requests_repo.get_download_request(rid)
    assert get_download_request_result is not None
    assert get_download_request_result['local_import_status'] == 'completed'


@pytest.mark.parametrize('warning', [
    '媒体库刷新失败：连接不可用', 'Jellyfin 刷新入队失败',
    'jellyfin 服务端路径映射无效，已跳过刷新',
])
def test_refresh_failure_survives_resultless_notification_rebuild(notification_case, warning):
    rid, tid, _sid = notification_case
    local_media_repo.update_local_media_task(tid, warning=warning)
    rebuilt = build_download_lifecycle_event(download_requests_repo.get_download_request(rid))
    assert rebuilt.state == 'error'
    refresh_text = dict(rebuilt.fields)['媒体库']
    assert isinstance(refresh_text, str)
    assert '刷新失败' in refresh_text
    assert '入库完成' not in str(rebuilt.title)


def test_unrelated_warning_does_not_invent_refresh_failure(notification_case):
    rid, tid, _sid = notification_case
    local_media_repo.update_local_media_task(tid, warning='qB任务移除失败')
    event = build_download_lifecycle_event(download_requests_repo.get_download_request(rid), stats={'media_refresh_status':'completed'})
    assert event.state == 'completed'
    refresh_text = dict(event.fields)['媒体库']
    assert isinstance(refresh_text, str)
    assert '刷新完成' in refresh_text


def test_peer_refresh_success_does_not_hide_local_refresh_failure(notification_case):
    rid, tid, _sid = notification_case
    local_media_repo.update_local_media_task(tid, warning='媒体库刷新失败：连接不可用')
    event = build_download_lifecycle_event(download_requests_repo.get_download_request(rid), media_refresh='云端媒体已刷新')
    assert event.state == 'error'
    refresh_text = dict(event.fields)['媒体库']
    assert isinstance(refresh_text, str)
    assert '本地媒体库刷新失败' in refresh_text
