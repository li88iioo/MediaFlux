"""Positive safety controls promoted from the independent SPEC probes."""
from app.agent.kernel.model import ModelAdapter
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter
from typing import cast
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from app import config, database as db
from app.agent import feature_gate
from app.agent.kernel import bootstrap
from tests import test_agent_generation_profiles as profiles
from tests.test_setup_bootstrap_support import first_run_case

local_config = profiles.local_config


def test_injected_bool_exception_is_propagated_once(local_config):
    failure = ValueError('synthetic truth evaluation')
    class Model:
        calls = 0
        def __bool__(self):
            self.calls += 1
            raise failure
    model = Model()
    with patch.object(bootstrap, 'OpenAICompatibleModelAdapter', side_effect=AssertionError('default forbidden')), patch.object(bootstrap.ProviderSettings, 'from_config', side_effect=AssertionError('provider read forbidden')):
        with pytest.raises(ValueError) as caught:
            bootstrap.build_agent_kernel_runtime(model=cast(ModelAdapter | None, model))
    assert caught.value is failure
    assert model.calls == 1


@pytest.mark.parametrize('failed', [False, True])
def test_direct_constructor_rechecks_after_real_publication(local_config, failed):
    original = config.get_many
    def publish():
        if failed:
            with patch.object(config, 'update_env_file', side_effect=OSError('synthetic failure')):
                with pytest.raises(OSError):
                    config.set_and_save({'AGENT_LLM_MODEL': 'changed'})
        else:
            config.set_and_save({'AGENT_LLM_MODEL': 'changed'})
    with ThreadPoolExecutor(max_workers=1) as pool:
        def read_then_publish(keys, *args, **kwargs):
            values = original(keys, *args, **kwargs)
            if 'AGENT_LLM_MODEL' in keys:
                pool.submit(publish).result(timeout=10)
            return values
        with patch.object(config, 'get_many', side_effect=read_then_publish):
            with pytest.raises(feature_gate.RuntimeFenceError, match='runtime_changed_during_creation'):
                bootstrap.build_agent_kernel_runtime()
    assert bootstrap._runtime is None
    if not failed:
        rebuilt = bootstrap.build_agent_kernel_runtime()
        assert isinstance(rebuilt.session.model, OpenAICompatibleModelAdapter)
        assert rebuilt.session.model.settings.model == 'changed'


def test_publisher_does_not_recreate_missing_database(local_config):
    path = db.resolve_db_path()
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        config.set_and_save({'AGENT_LLM_MODEL': 'never'})
    assert not path.exists()


@pytest.mark.parametrize('state', ['missing-db', 'corrupt-db', 'missing-config', 'corrupt-config', 'stable', 'changing'])
def test_actual_lifespan_storage_authorization(state):
    with first_run_case() as case:
        case.paths.ensure_writable_dirs()
        case.paths.env_file.write_text('MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=synthetic-password\nWEB_SECRET_KEY=synthetic-secret\n')
        case._reload_runtime_modules()
        before = None
        if state == 'corrupt-db':
            case.paths.database_path.write_bytes(b'not sqlite')
        elif state != 'missing-db':
            case.database.init_db()
            before = feature_gate.initialize_runtime_fence()
            if state == 'changing':
                with db.get_conn() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    assert before is not None
                    before = feature_gate.begin_runtime_change(conn, before)
            if state == 'missing-config':
                case.paths.env_file.unlink()
            elif state == 'corrupt-config':
                case.paths.env_file.write_bytes(b'\xff')
            case._reload_runtime_modules()
        env_before = case.paths.env_file.read_bytes() if case.paths.env_file.exists() else None
        if state == 'missing-db':
            # 已有管理员配置却丢失DB，不能作为可提交的全新安装开放。
            with pytest.raises(RuntimeError):
                case._new_client()
            assert not case.paths.database_path.exists()
        elif state == 'stable':
            with case._new_client():
                with db.get_conn() as conn:
                    assert feature_gate.read_runtime_fence(conn) == before
        else:
            with pytest.raises((RuntimeError, sqlite3.DatabaseError, UnicodeError, ValueError)):
                with case._new_client():
                    pass
            if state == 'changing':
                with db.get_conn() as conn:
                    assert feature_gate.read_runtime_fence(conn) == before
            before = None
        if state == 'corrupt-db':
                assert case.paths.database_path.read_bytes() == b'not sqlite'
        assert (case.paths.env_file.read_bytes() if case.paths.env_file.exists() else None) == env_before
