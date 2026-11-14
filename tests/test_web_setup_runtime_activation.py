"""Web 首装运行时门控与停机租约回归；只使用临时存储和进程内请求。"""
import base64
import json
import re
import threading
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_setup_bootstrap import cold_run

_USERNAME = 'runtime-review-admin'
_PASSWORD = 'runtime-review-password'


def _csrf_token(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f'CSRF token missing from HTTP {response.status_code}'
    return match.group(1)


def _setup_fields(browser):
    page = browser.get('/setup')
    assert page.status_code == 200
    return {
        'username': _USERNAME,
        'password': _PASSWORD,
        'password_confirm': _PASSWORD,
        'csrf_token': _csrf_token(page),
    }


def _session_signer(application):
    from itsdangerous import TimestampSigner

    secret = next(m.kwargs['secret_key'] for m in application.user_middleware
                  if m.cls.__name__ == 'SessionMiddleware')
    return TimestampSigner(secret)


def _session_data(application, browser):
    cookie = browser.cookies.get('session')
    if not cookie:
        return {}
    payload = _session_signer(application).unsign(cookie)
    return json.loads(base64.b64decode(payload))


def _restore_lock_available(paths):
    from app.modules import backup

    try:
        with backup._exclusive_runtime_lifecycle_guard(paths):
            return True
    except backup.BackupError:
        return False


def _failed_activation_action():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.modules import first_run

    application = create_app()
    with (
        patch('app.modules.recognition_knowledge.ensure_seed_knowledge',
              side_effect=OSError('injected runtime seed failure')),
        TestClient(application, raise_server_exceptions=False) as browser,
    ):
        fields = _setup_fields(browser)
        setup = browser.post('/setup', data=fields, follow_redirects=False)
        assert setup.status_code == 503
        assert not first_run.needs_initialization(), 'DB/env installation must have committed'
        assert not application.state.ready
        assert browser.get('/readyz').status_code == 503

        # 不先 GET /login：原回归由有效的 setup 会话直接 POST 绕过激活。
        login = browser.post('/login', data=fields, follow_redirects=False)
        assert login.status_code == 503, f'failed activation admitted login: HTTP {login.status_code}'
        assert not _session_data(application, browser).get('logged_in'), 'failed activation retained an authenticated session'
        assert not application.state.ready
        assert browser.get('/api/config').status_code == 503
        assert browser.get('/settings', follow_redirects=False).status_code == 503
    return 'verified'


def test_failed_setup_activation_cannot_create_a_logged_in_session(tmp_path):
    assert cold_run(tmp_path, _failed_activation_action) == {'result': 'verified'}


def _fresh_cookie_action():
    from fastapi.testclient import TestClient

    from app import config
    from app.main import create_app
    from app.modules import first_run

    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as browser:
        _setup_fields(browser)
        assert first_run.needs_initialization()
        assert not config.PATHS.database_path.exists()
        assert not config.PATHS.env_file.exists()

        # 首装尚未提交；使用测试 app 的真实密钥签出旧登录态，不调用 setup POST。
        payload = base64.b64encode(json.dumps({
            'logged_in': True, 'csrf_token': 'previous-session-csrf',
        }).encode())
        cookie = _session_signer(application).sign(payload).decode()
        browser.cookies.clear()
        browser.cookies.set('session', cookie)
        assert _session_data(application, browser)['logged_in'] is True

        api = browser.get('/api/config')
        page = browser.get('/settings', follow_redirects=False)
        assert api.status_code == 503, f'old cookie bypassed fresh API gate: HTTP {api.status_code}'
        assert page.status_code == 302
        assert page.headers['location'] == '/setup'
        assert browser.get('/runtime-activation-test-missing', follow_redirects=False).status_code == 404
        assert first_run.needs_initialization()
        assert not application.state.ready
        assert not config.PATHS.database_path.exists(), 'fresh GET created a database'
        assert not config.PATHS.env_file.exists(), 'fresh GET published user.env'
    return 'verified'


def test_fresh_setup_rejects_authenticated_cookie_without_creating_storage(tmp_path):
    assert cold_run(tmp_path, _fresh_cookie_action) == {'result': 'verified'}


def _before_activation_action():
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app()
    with (
        patch('app.modules.recognition_knowledge.ensure_seed_knowledge',
              side_effect=OSError('injected runtime seed failure')),
        TestClient(application, raise_server_exceptions=False) as browser,
    ):
        setup = browser.post('/setup', data=_setup_fields(browser), follow_redirects=False)
        assert setup.status_code == 503
        assert not application.state.ready

        # 用本安装的真实签名密钥模拟浏览器带回旧的已认证 cookie。
        payload = base64.b64encode(json.dumps({
            'logged_in': True, 'csrf_token': 'previous-session-csrf',
        }).encode())
        cookie = _session_signer(application).sign(payload).decode()
        browser.cookies.clear()
        browser.cookies.set('session', cookie)
        assert _session_data(application, browser)['logged_in'] is True

        api = browser.get('/api/config')
        page = browser.get('/settings', follow_redirects=False)
        assert api.status_code == 503, f'old cookie admitted business API: HTTP {api.status_code}'
        assert page.status_code == 503, f'old cookie admitted business page: HTTP {page.status_code}'
        assert browser.get('/healthz').status_code == 200
        assert browser.get('/readyz').status_code == 503
        assert browser.get('/static/js/app.js').status_code == 200
        assert browser.get('/setup', follow_redirects=False).status_code in (302, 303)
    return 'verified'


def test_authenticated_cookie_cannot_enter_business_before_runtime_activation(tmp_path):
    assert cold_run(tmp_path, _before_activation_action) == {'result': 'verified'}


def _login_activation_action():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.modules import first_run

    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as browser:
        fields = _setup_fields(browser)
        activate = application.state.activate_runtime
        with patch('app.modules.recognition_knowledge.ensure_seed_knowledge',
                   side_effect=OSError('injected runtime seed failure')):
            setup = browser.post('/setup', data=fields, follow_redirects=False)
        assert setup.status_code == 503
        assert not first_run.needs_initialization()
        assert not application.state.ready

        # 只解除故障，不替换 app、客户端、安装状态，也不重放 setup。
        login = browser.post('/login', data=fields, follow_redirects=False)
        assert login.status_code == 302
        assert login.headers['location'] == '/'
        assert application.state.activate_runtime is activate
        assert application.state.ready, 'POST /login did not activate the committed installation'
        assert browser.get('/readyz').status_code == 200
        assert browser.get('/api/config').status_code == 200
    return 'verified'


def test_login_post_activates_committed_installation_without_restarting(tmp_path):
    assert cold_run(tmp_path, _login_activation_action) == {'result': 'verified'}


def _lifecycle_lock_action(existing, stop_completes):
    from fastapi.testclient import TestClient

    from app import config, main
    from app.modules import first_run
    from app.modules.media_proxy import get_media_proxy_manager

    if existing:
        first_run.initialize_admin(_USERNAME, _PASSWORD)

    release = threading.Event()
    started = threading.Event()
    events = []

    def wait_for_release():
        started.set()
        release.wait()

    # 是真实存活线程，不把 stop 的 False 简化为没有写入者的纯 mock。
    worker = threading.Thread(target=wait_for_release, daemon=True)

    def start_services():
        events.append('start')
        worker.start()
        assert started.wait(2), 'fake worker failed to start'

    def stop_services():
        events.append('stop')
        if stop_completes:
            release.set()
        worker.join(timeout=2 if stop_completes else .01)
        return not worker.is_alive()

    application = main.create_app(start_background=True)
    manager = get_media_proxy_manager()
    try:
        # 只替换生产者/探测器；安装、运行时激活和实际生命周期锁均走生产逻辑。
        with patch.object(main, 'start_background_services', start_services), \
             patch.object(main, 'stop_background_services', stop_services), \
             patch('app.modules.media_proxy.start_signed_media_probe_runtime', return_value=None), \
             patch('app.modules.media_proxy.shutdown_signed_media_probe_runtime', return_value=True), \
             patch.object(manager, 'start', AsyncMock()), \
             patch.object(manager, 'stop', AsyncMock()):
            with TestClient(application) as browser:
                if not existing:
                    setup = browser.post('/setup', data=_setup_fields(browser), follow_redirects=False)
                    assert setup.status_code == 303
                assert application.state.ready
                assert browser.get('/readyz').status_code == 200
                assert worker.is_alive()
                assert not _restore_lock_available(config.PATHS), 'running service lost its lifecycle lock'

            assert events == ['start', 'stop'], events
            assert worker.is_alive() is (not stop_completes), 'worker liveness does not match shutdown outcome'
            lock_available = _restore_lock_available(config.PATHS)
            assert lock_available is stop_completes, (
                f'existing={existing}, stop_completes={stop_completes}, '
                f'worker_alive={worker.is_alive()}, restore_lock_available={lock_available}'
            )
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(timeout=2)
        assert not worker.is_alive(), 'fake worker was not cleaned up'
    return 'verified'


@pytest.mark.parametrize('existing,stop_completes', [
    (False, False), (True, False), (False, True),
], ids=['web-install-stop-timeout', 'existing-install-stop-timeout', 'web-install-clean-stop'])
def test_web_installation_retains_lifecycle_lock_until_writers_stop(tmp_path, existing, stop_completes):
    assert cold_run(tmp_path, _lifecycle_lock_action, existing, stop_completes) == {'result': 'verified'}
