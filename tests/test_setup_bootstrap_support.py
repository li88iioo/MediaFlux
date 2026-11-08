"""真实单一安装夹具；不注入 schema、fence 或测试安装分支。"""
import os
import secrets
import unittest
from contextlib import contextmanager


@contextmanager
def first_run_case():
    """复用安装夹具时保留 unittest 的完整生命周期，包括初始化失败时的清理。"""
    from tests.test_first_run import FirstRunTests

    case = FirstRunTests()
    try:
        case.setUp()
        try:
            yield case
        finally:
            case.tearDown()
    finally:
        assert case.doCleanups(), "安装夹具的注册清理失败"


def initialize_paths(paths, settings=None):
    """可预配业务配置，但所有写入均由唯一安装事务完成。"""
    from app import config
    from app.modules.setup_bootstrap import initialize_installation

    expected, values = config.read_env_snapshot_no_recovery(paths.env_file)
    values.setdefault('ENV_WEB_PASSPORT', 'operator')
    values.setdefault('ENV_WEB_PASSWORD', 'test-bootstrap-password')
    values.setdefault('WEB_SECRET_KEY', os.environ.get('WEB_SECRET_KEY') or secrets.token_urlsafe(48))
    values.update(settings or {})
    values['MEDIAFLUX_INITIALIZED'] = '1'
    return initialize_installation(paths, values, expected_env=expected)


def initialize_web_paths(paths, username, password, inspect_page=None):
    """通过普通应用真实完成表单安装，同一客户端立即登录，不替换服务器。"""
    import re
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(), base_url='http://127.0.0.1:1258') as client:
        page = client.get('/setup')
        assert page.status_code == 200
        assert 'name="setup_code"' not in page.text
        assert not paths.env_file.exists()
        assert not paths.database_path.exists()
        assert not (paths.config_dir / '.web-secret-key').exists()
        search_result = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert search_result is not None
        token = search_result.group(1)
        assert token in client.get('/setup').text
        if inspect_page:
            inspect_page(page, client)
        page = client.get('/setup')
        search_result_2 = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert search_result_2 is not None
        token = search_result_2.group(1)
        response = client.post('/setup', data={
            'username': username, 'password': password,
            'password_confirm': password, 'csrf_token': token,
        }, headers={'Origin': str(client.base_url).rstrip('/')}, follow_redirects=False)
        assert response.status_code in (302, 303), response.text
        assert response.headers['location'] == '/login'
        assert not client.cookies.get('session')
        page = client.get('/login')
        assert page.status_code == 200
        search_result_3 = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert search_result_3 is not None
        token = search_result_3.group(1)
        response = client.post('/login', data={
            'username': username, 'password': password, 'csrf_token': token,
        }, follow_redirects=False)
        assert response.status_code == 302, response.text
        assert response.headers['location'] == '/'
        assert client.get('/readyz').status_code == 200


class AgentShellFixtureLifecycleTests(unittest.TestCase):
    def test_failed_page_fixture_cannot_restore_stale_runtime_after_traceback_gc(self):
        import gc
        import weakref
        from types import TracebackType
        from unittest.mock import patch

        from app import config, database as db, runtime_paths
        from app.modules import first_run
        from tests import test_agent_page as pages, test_agent_ux_shell_browser as shell

        def snapshot():
            return (os.environ.get("MEDIAFLUX_TEST_MODE"), os.environ.get("MEDIAFLUX_TEST_DB_PATH"),
                    config.ENV_FILE, config.PATHS, runtime_paths._configured_paths,
                    db.DB_PATH, db.resolve_db_path(), db._configured_test_mode, db._test_mode_enabled())

        original_setup, original_teardown = pages.AgentPageTests.setUp, pages.AgentPageTests.tearDown
        for phase in ("setup", "login", "teardown"):
            with (self.subTest(phase=phase), patch.dict(os.environ), patch.dict(config.__dict__),
                  patch.dict(db.__dict__), patch.dict(first_run.__dict__),
                  patch.object(runtime_paths, "_configured_paths", runtime_paths._configured_paths)):
                before = snapshot()

                class ShellFixture(shell.AgentShellBrowserCase):
                    tearDown_exceptions: list[tuple[type[BaseException], BaseException, TracebackType]]

                case_ref: weakref.ReferenceType[pages.AgentPageTests] | None = None

                def fail(case, failure):
                    nonlocal case_ref
                    case_ref = weakref.ref(case)
                    raise RuntimeError(f"offline shell {failure} failure")

                def setup_failure(case):
                    original_setup(case)
                    fail(case, "setup")

                def login_failure(case):
                    fail(case, "login")

                def teardown_failure(case):
                    original_teardown(case)
                    fail(case, "teardown")

                retained: RuntimeError | None = None
                with (patch("socket.socket.connect", side_effect=AssertionError("禁止真实网络")) as connect,
                      patch("socket.socket.connect_ex", side_effect=AssertionError("禁止真实网络")) as connect_ex,
                      patch.object(pages.AgentPageTests, "setUp", setup_failure if phase == "setup" else original_setup),
                      patch.object(pages.AgentPageTests, "_login", login_failure),
                      patch.object(pages.AgentPageTests, "tearDown", teardown_failure if phase == "teardown" else original_teardown),
                      patch.object(shell.browser_support.AgentKernelBrowserTests, "setUpClass") as browser_start):
                    try:
                        try:
                            ShellFixture.setUpClass()
                        except RuntimeError as error:
                            retained = error  # 像 pytest 一样保留 traceback，使 case 延后回收。
                        self.assertIsNotNone(retained)
                        assert retained is not None and case_ref is not None
                        self.assertEqual(str(retained), f"offline shell {phase} failure")
                        if phase == "teardown":
                            self.assertIsInstance(retained.__context__, RuntimeError)
                            self.assertEqual(str(retained.__context__), "offline shell login failure")
                        ShellFixture.doClassCleanups()
                        self.assertEqual(ShellFixture.tearDown_exceptions, [])
                        self.assertEqual(snapshot(), before)
                        self.assertIsNotNone(case_ref())
                        retained = None
                        gc.collect()
                        self.assertIsNone(case_ref())
                        self.assertEqual(snapshot(), before)
                        browser_start.assert_not_called()
                        connect.assert_not_called()
                        connect_ex.assert_not_called()
                    finally:
                        ShellFixture.doClassCleanups()
                        retained = None
                        gc.collect()
