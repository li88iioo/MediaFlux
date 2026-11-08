"""The actual setup lifecycle must not leave a stale empty-config fence."""
from unittest.mock import patch

import pytest

from tests.test_setup_bootstrap_support import first_run_case


def test_initialized_legacy_database_can_publish_missing_session_secret():
    with first_run_case() as case:
        case.paths.ensure_writable_dirs()
        case.paths.env_file.write_text(
            'MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=synthetic-strong-password\n'
        )
        case._reload_runtime_modules()
        case.database.init_db()
        from app.main import create_app
        create_app(start_background=False)
        assert case.config.read_env_snapshot(case.paths.env_file)[1].get('WEB_SECRET_KEY')


def test_setup_then_normal_publication():
    with first_run_case() as case:
        with case._new_client() as client:
            page = case._setup_page(client)
            response = client.post('/setup', data=case._fields(page), follow_redirects=False)
            assert response.status_code == 303
            assert response.headers['location'] == '/login'
            case._login(client)
            case.config.set_and_save({'AGENT_LLM_MODEL': 'synthetic-new-model'})
            assert case.config.get('AGENT_LLM_MODEL') == 'synthetic-new-model'
            from app.agent.feature_gate import read_runtime_fence
            with case.database.get_conn() as conn:
                fence = read_runtime_fence(conn)
                assert fence.phase == 'stable'
                assert fence.config_digest == case.config.capture_runtime_config_snapshot().config_digest
            assert client.get('/readyz').status_code == 200


@pytest.mark.parametrize("phase", ["setup", "body", "teardown"])
def test_installation_fixture_restores_runtime_even_when_a_phase_fails(phase):
    from app import config, database, runtime_paths
    from tests.test_first_run import FirstRunTests

    def snapshot():
        return (config.ENV_FILE, config.PATHS, runtime_paths._configured_paths,
                database.resolve_db_path(), database._test_mode_enabled())

    before = snapshot()
    method = "setUp" if phase == "setup" else "tearDown"
    original = getattr(FirstRunTests, method)

    def fail(case):
        original(case)
        raise RuntimeError("installation fixture interrupted")

    with patch.object(FirstRunTests, method, fail if phase != "body" else original):
        with pytest.raises(RuntimeError, match="installation fixture interrupted"):
            with first_run_case():
                if phase == "body":
                    raise RuntimeError("installation fixture interrupted")
    assert snapshot() == before
