"""Durable authorization publication through real product entries."""
from starlette.responses import JSONResponse
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter
from starlette.requests import Request
from app.agent.kernel.model import ModelAdapter
from typing import cast
import json
import hashlib
from contextlib import ExitStack, closing, contextmanager
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from app import config, database as db
from app.agent import feature_gate as gate
from app.agent.kernel import bootstrap
from tests.support import initialized_web_runtime, isolated_test_database
from tests.test_backup import make_paths


@pytest.fixture
def runtime_env(tmp_path):
    paths = make_paths(tmp_path)
    paths.ensure_writable_dirs()
    paths.env_file.write_text("AGENT_LLM_ENABLED=0\nAGENT_LLM_MODEL=old\nAGENT_LLM_API_URL=https://invalid.example/v1\n")
    with isolated_test_database(), patch.object(config, 'PATHS', paths), patch.object(config, 'ENV_FILE', paths.env_file), patch.object(config, '_cache', None), patch.dict(os.environ):
        for key in ('AGENT_LLM_MODEL', 'AGENT_LLM_API_URL'):
            os.environ.pop(key, None)
        db.configure_database(paths.database_path, test_mode=True)
        db.init_db()
        bootstrap.invalidate_agent_kernel_runtime()
        initialize = getattr(gate, 'initialize_runtime_fence', None)
        if initialize:
            initialize()
        yield paths
        bootstrap.invalidate_agent_kernel_runtime()


def read_record():
    with db.get_conn() as conn:
        row = conn.execute('SELECT value FROM settings_kv WHERE key=?', ('agent.runtime_fence.v1',)).fetchone()
    return json.loads(row['value']) if row else None


@contextmanager
def legacy_runtime_fence():
    """用基线序列化规则通过真实安装与发布器创建 v1 状态。"""
    def v1_digest(values, **_kwargs):
        canonical = {key: value for key, value in values.items() if key != "AGENT_STREAM_DISPLAY_ENABLED"}
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    with (
        patch.object(gate, "RUNTIME_FENCE_VERSION", 1),
        patch.object(config, "_runtime_digest", v1_digest),
        patch.object(config, "RUNTIME_DISPLAY_ONLY_KEYS", frozenset({"AGENT_STREAM_DISPLAY_ENABLED"})),
    ):
        yield


@pytest.fixture
def installed_v1():
    with ExitStack() as upgrade:
        upgrade.enter_context(legacy_runtime_fence())
        with initialized_web_runtime():
            config.set_and_save({"DISCOVERY_ENABLED": "1", "DISCOVERY_DOUBAN_ENABLED": "1", "INDEXER_SEARCH_ENABLED": "1"})
            before = bootstrap._capture_creation_binding()
            upgrade.close()
            assert before.fence.v == 1
            yield before


def test_verified_v1_upgrade_survives_display_writes_and_restart(installed_v1):
    from fastapi.testclient import TestClient
    from app.main import create_app

    old = installed_v1
    raw = config.ENV_FILE.read_bytes()
    with TestClient(create_app(start_background=False)) as web:
        assert web.get("/readyz").status_code == 200
    upgraded = bootstrap._capture_creation_binding()
    assert upgraded.fence.v == 2
    assert (upgraded.fence.incarnation, upgraded.fence.epoch) == (old.fence.incarnation, old.fence.epoch)
    assert upgraded.fence.config_digest != old.fence.config_digest
    assert upgraded.legacy_continuation == (old.fence, old.effective_digest)
    assert config.ENV_FILE.read_bytes() == raw
    with db.get_conn() as conn, pytest.raises(gate.RuntimeFenceError):
        gate.require_runtime_fence(conn, old.fence)

    config.set_and_save({"DISCOVERY_ENABLED": "0", "DISCOVERY_DOUBAN_ENABLED": "0", "INDEXER_SEARCH_ENABLED": "0"})
    with patch.dict(config._SYSTEM_ENV_SNAPSHOT, {"DISCOVERY_ENABLED": "1"}):
        with TestClient(create_app(start_background=False)) as web:
            assert web.get("/readyz").status_code == 200
        assert bootstrap._capture_creation_binding() == upgraded

    config.set_and_save({"AGENT_LLM_MODEL": "temporary", "DISCOVERY_ENABLED": "1"})
    config.set_and_save({"AGENT_LLM_MODEL": upgraded.snapshot.file_values.get("AGENT_LLM_MODEL")})
    current = bootstrap._capture_creation_binding()
    assert current.fence.config_digest == upgraded.fence.config_digest
    assert current.fence.epoch == upgraded.fence.epoch + 2
    assert current.legacy_continuation is None


@pytest.mark.parametrize("fault", ["changing", "unrelated_file_change", "v2_digest_under_v1_version"])
def test_startup_rejects_unverified_v1_before_schema_initialization(installed_v1, fault):
    from app.main import create_app

    data = read_record()
    assert data is not None
    if fault == "changing":
        data["phase"] = "changing"
    elif fault == "unrelated_file_change":
        config.write_env_file(config.ENV_FILE, {**config.all_items(), "AGENT_LLM_MODEL": "external"}, replace=True)
    else:
        data["config_digest"] = config.capture_runtime_config_snapshot().config_digest
    with db.get_conn() as conn:
        conn.execute("UPDATE settings_kv SET value=? WHERE key=?", (json.dumps(data), gate.RUNTIME_FENCE_KEY))

    reason = "runtime_fence_changing" if fault == "changing" else "runtime_snapshot_mismatch"
    with patch.object(db, "init_db") as initialize, pytest.raises(gate.RuntimeFenceError, match=reason):
        create_app(start_background=False)
    initialize.assert_not_called()
    assert read_record() == data


def test_legacy_effective_binding_is_revoked_on_observed_deployment_drift(installed_v1):
    assert gate.initialize_runtime_fence() is not None
    assert bootstrap._capture_creation_binding().legacy_continuation is not None
    with patch.dict(config._SYSTEM_ENV_SNAPSHOT, {"AGENT_LLM_MODEL": "other-deployment"}):
        assert gate.initialize_runtime_fence() is not None
        assert bootstrap._capture_creation_binding().legacy_continuation is None
    assert bootstrap._capture_creation_binding().legacy_continuation is None


def test_publication_commits_changing_before_file_and_stable_after(runtime_env):
    original = config.update_env_file
    seen = []
    def observe(*args, **kwargs):
        seen.append(read_record())
        return original(*args, **kwargs)
    with patch.object(config, 'update_env_file', side_effect=observe):
        config.set_and_save({'AGENT_LLM_MODEL': 'new'})
    assert seen and seen[0] and seen[0]['phase'] == 'changing'
    read_record_result = read_record()
    assert read_record_result is not None
    assert read_record_result['phase'] == 'stable'


def _run_cold_runtime(runtime_env, action):
    script = r'''
import importlib, json, os, pathlib, socket, sys
root = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path.cwd().resolve()
def audit(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg') and args[0].family != socket.AF_UNIX:
        raise AssertionError('exec inet denied')
    if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'):
        raise AssertionError('exec DNS denied')
sys.addaudithook(audit)
assert not any(n == 'app' or n.startswith('app.') for n in sys.modules)
import tests
assert pathlib.Path(tests.__file__).resolve() == source / 'tests/__init__.py'
from app import config, database as db
from app.runtime_paths import configure_runtime_paths
from tests.test_backup import make_paths
paths = make_paths(root)
for key, value in {
    'MEDIAFLUX_DATA_DIR': paths.data_dir,
    'MEDIAFLUX_CONFIG_DIR': paths.config_dir,
    'MEDIAFLUX_CACHE_DIR': paths.cache_dir,
    'MEDIAFLUX_LOG_DIR': paths.log_dir,
    'MEDIAFLUX_STRM_DIR': paths.strm_dir,
    'MEDIAFLUX_TEST_DB_PATH': paths.database_path,
}.items(): os.environ[key] = str(value)
configure_runtime_paths(paths)
importlib.reload(config)
assert os.environ['AGENT_LLM_ENABLED'] == '0'
db.configure_database(paths.database_path, test_mode=True)
db.init_db()
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel import bootstrap
assert pathlib.Path(bootstrap.__file__).resolve() == source / 'app/agent/kernel/bootstrap.py'
assert config.ENV_FILE == paths.env_file
initialize_runtime_fence()
''' + action
    environment = dict(
        config._SYSTEM_ENV_SNAPSHOT,
        AGENT_LLM_ENABLED='0',
        PYTHONDONTWRITEBYTECODE='1',
        MEDIAFLUX_DATA_DIR=str(runtime_env.data_dir),
        MEDIAFLUX_CONFIG_DIR=str(runtime_env.config_dir),
        MEDIAFLUX_CACHE_DIR=str(runtime_env.cache_dir),
        MEDIAFLUX_LOG_DIR=str(runtime_env.log_dir),
        MEDIAFLUX_STRM_DIR=str(runtime_env.strm_dir),
        MEDIAFLUX_TEST_DB_PATH=str(runtime_env.database_path),
    )
    return subprocess.run(
        [sys.executable, '-B', '-c', script, str(runtime_env.data_dir.parent)],
        env=environment, text=True, capture_output=True, timeout=20,
    )


def test_real_worker_publication_invalidates_cached_runtime(runtime_env):
    config.set_and_save({'AGENT_LLM_MODEL': 'old'})
    old = bootstrap.get_agent_kernel_runtime()
    action = r'''
from app.agent import feature_gate as gate
config.set_and_save({'AGENT_LLM_MODEL': 'new'})
new = bootstrap.get_agent_kernel_runtime()
with db.get_conn() as conn:
    gate.require_runtime_fence(conn, new.creation_binding.fence)
print(json.dumps({'model': new.session.model.settings.model}), flush=True)
'''
    result = _run_cold_runtime(runtime_env, action)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'model': 'new'}
    new = bootstrap.get_agent_kernel_runtime()
    assert new is not old
    assert isinstance(new.session.model, OpenAICompatibleModelAdapter)
    assert new.session.model.settings.model == 'new'
    # The retained object keeps its original authority and provider identity.
    assert isinstance(old.session.model, OpenAICompatibleModelAdapter)
    assert old.session.model.settings.model == 'old'
    assert old.creation_binding != new.creation_binding
    with db.get_conn() as conn:
        with pytest.raises(gate.RuntimeFenceError):
            assert old.creation_binding is not None
            gate.require_runtime_fence(conn, old.creation_binding.fence)


@pytest.mark.parametrize('reason', ['stale', 'invalid', 'override'])
def test_rejected_publication_preserves_runtime_and_next_save(runtime_env, reason):
    old = bootstrap.get_agent_kernel_runtime()
    before = read_record()
    raw = config.ENV_FILE.read_bytes()
    expected = b'stale=1\n' if reason == 'stale' else raw
    changes = {'AGENT_LLM_MODEL': 'bad\nvalue' if reason == 'invalid' else 'new'}
    overrides = frozenset({'AGENT_LLM_MODEL'}) if reason == 'override' else frozenset()
    with patch.object(config, '_STARTUP_ENV_OVERRIDES', overrides), patch.dict(
        os.environ, {'AGENT_LLM_MODEL': 'deployment'} if reason == 'override' else {},
    ), pytest.raises((config.AtomicPublishError, ValueError)) as rejected:
        config.update_runtime_env_file(config.ENV_FILE, changes, expected=expected)
    if reason == 'override':
        assert str(rejected.value) == '目标配置由运行环境覆盖'
        with patch.object(config, '_STARTUP_ENV_OVERRIDES', overrides), patch.dict(
            os.environ, {'AGENT_LLM_MODEL': 'deployment'},
        ), pytest.raises(config.ExternalConfigOverrideError, match='^目标配置由运行环境覆盖: AGENT_LLM_MODEL$'):
            config.set_and_save(changes)
    assert config.ENV_FILE.read_bytes() == raw
    assert read_record() == before
    gate.initialize_runtime_fence()
    assert bootstrap.get_agent_kernel_runtime() is old
    config.set_and_save({'AGENT_LLM_MODEL': 'next-valid-save'})
    assert config.get('AGENT_LLM_MODEL') == 'next-valid-save'
    after = read_record()
    assert after is not None and after['phase'] == 'stable'


def test_failed_publication_stays_changing_and_restart_cannot_revive(runtime_env):
    old = bootstrap.get_agent_kernel_runtime()
    before = config.ENV_FILE.read_bytes()
    with patch.object(config, '_publish_noreplace', side_effect=OSError('disk failure')), pytest.raises(OSError):
        config.update_runtime_env_file(config.ENV_FILE, {'AGENT_LLM_MODEL': 'new'}, expected=before)
    assert config.ENV_FILE.read_bytes() == before
    read_record_result_2 = read_record()
    assert read_record_result_2 is not None
    assert read_record_result_2['phase'] == 'changing'
    changing = read_record()
    with pytest.raises(gate.RuntimeFenceError, match='runtime_fence_changing'):
        gate.initialize_runtime_fence()
    assert read_record() == changing
    assert config.ENV_FILE.read_bytes() == before
    with pytest.raises(gate.RuntimeFenceError):
        bootstrap.get_agent_kernel_runtime()
    with pytest.raises(gate.RuntimeFenceError):
        config.set_and_save({'AGENT_LLM_MODEL': 'old'})
    with db.get_conn() as conn:
        with pytest.raises(gate.RuntimeFenceError):
            assert old.creation_binding is not None
            gate.require_runtime_fence(conn, old.creation_binding.fence)


def test_aba_display_exception_and_normal_cold_rebuild(runtime_env):
    old = bootstrap.get_agent_kernel_runtime()
    original = old.creation_binding
    config.set_and_save({'AGENT_STREAM_DISPLAY_ENABLED': '0'})
    assert bootstrap.get_agent_kernel_runtime() is old
    config.set_and_save({'AGENT_LLM_MODEL': 'new'})
    config.set_and_save({'AGENT_LLM_MODEL': 'old'})
    current = bootstrap.get_agent_kernel_runtime()
    assert current.creation_binding is not None
    assert original is not None
    assert current.creation_binding.fence.epoch > original.fence.epoch
    assert current.creation_binding.fence.config_digest == original.fence.config_digest
    assert current.creation_binding != original
    stable = gate.initialize_runtime_fence()
    bootstrap.invalidate_agent_kernel_runtime()
    cold = bootstrap.get_agent_kernel_runtime()
    assert cold.creation_binding == current.creation_binding
    assert stable == current.creation_binding.fence


def test_falsey_explicit_model_never_uses_provider(runtime_env):
    class Falsey:
        def __bool__(self):
            return False
    model = Falsey()
    with patch.object(bootstrap, 'OpenAICompatibleModelAdapter', side_effect=AssertionError('fallback forbidden')):
        runtime = bootstrap.build_agent_kernel_runtime(model=cast(ModelAdapter | None, model))
    assert runtime.session.model is model
    assert runtime.creation_binding is None


def test_snapshot_is_fresh_immutable_scoped_and_binds_startup(runtime_env):
    config.set_and_save({'AGENT_LLM_MODEL': 'old'})
    os.environ['AGENT_LLM_MODEL'] = 'stale-app-value'
    snapshot = config.capture_runtime_config_snapshot()
    with pytest.raises(TypeError):
        import operator
        operator.setitem(cast(dict[str, str], snapshot.file_values), 'AGENT_LLM_MODEL', 'tampered')
    from app.agent.kernel.provider_model import ProviderSettings
    with config.use_runtime_config_snapshot(snapshot):
        assert ProviderSettings.from_config().model == 'old'
        assert config.all_items()['AGENT_LLM_MODEL'] == 'old'
    assert config.get('AGENT_LLM_MODEL') == 'stale-app-value'
    with patch.dict(config._SYSTEM_ENV_SNAPSHOT, {'AGENT_LLM_MODEL': 'deployment'}):
        other = config.capture_runtime_config_snapshot()
        assert other.effective_digest != snapshot.effective_digest
        with config.use_runtime_config_snapshot(other):
            assert ProviderSettings.from_config().model == 'deployment'


def test_require_uses_caller_session_connection_and_blocks_competing_writer(runtime_env):
    import asyncio
    import sqlite3
    from app.agent.kernel.persistence import SQLiteKernelStore
    store = SQLiteKernelStore()
    asyncio.run(store.begin_turn(owner='fence-owner', session_id='fence-session', request_id='fence'))
    get_agent_kernel_runtime_result = bootstrap.get_agent_kernel_runtime().creation_binding
    assert get_agent_kernel_runtime_result is not None
    token = get_agent_kernel_runtime_result.fence
    with db.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        with patch.object(db, 'get_conn', side_effect=AssertionError('must use caller connection')):
            gate.require_runtime_fence(conn, token)
            assert conn.execute('SELECT count(*) FROM agent_kernel_sessions').fetchone()[0] == 1
        with db.get_conn() as other:
            other.execute('PRAGMA busy_timeout=1')
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('UPDATE settings_kv SET value=value WHERE key=?', (gate.RUNTIME_FENCE_KEY,))


def test_publication_updates_only_the_writers_scoped_snapshot(runtime_env):
    from contextvars import copy_context
    snapshot = config.capture_runtime_config_snapshot()
    with config.use_runtime_config_snapshot(snapshot):
        reader = copy_context()
        config.set_and_save({'AGENT_LLM_MODEL': 'published'})
        assert config.get('AGENT_LLM_MODEL') == 'published'
        assert reader.run(config.get, 'AGENT_LLM_MODEL') == 'old'
        assert snapshot.file_values['AGENT_LLM_MODEL'] == 'old'
        config.set_and_save({'AGENT_STREAM_DISPLAY_ENABLED': '0'})
        assert config.get('AGENT_STREAM_DISPLAY_ENABLED') == '0'


def test_real_strm_api_enters_fence_before_sqlite_writer(runtime_env):
    from types import SimpleNamespace
    from app.routes import api
    from unittest.mock import MagicMock
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False)))
    from contextlib import contextmanager
    original = db.reconcile_strm_retired_sources_transaction
    before_writer = []
    @contextmanager
    def observe(*args, **kwargs):
        before_writer.append(read_record())
        with original(*args, **kwargs) as ids:
            yield ids
    with patch.object(api, 'require_api_login'), patch('app.services.clear_dashboard_cache'), patch('app.modules.scheduler.get_scheduler', return_value=MagicMock()), patch.object(db, 'reconcile_strm_retired_sources_transaction', side_effect=observe):
        result = api.save_config(cast(Request, request), {'GY_STRM_SOURCE_DIRS': '[{"id":"33","name":"synthetic"}]'})
    assert result == {'success': True}
    assert before_writer[0] and before_writer[0]['phase'] == 'changing'
    assert json.loads(config.read_env_snapshot(config.ENV_FILE)[1]['GY_STRM_SOURCE_DIRS'])[0]['id'] == '33'
    read_record_result_3 = read_record()
    assert read_record_result_3 is not None
    assert read_record_result_3['phase'] == 'stable'


@pytest.mark.parametrize('reason', ['invalid', 'derived_override'])
def test_rejected_strm_publication_preserves_state_and_allows_retry(runtime_env, reason):
    from types import SimpleNamespace
    from app.routes import api

    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))))

    def source_state():
        with db.get_conn() as conn:
            return {
                'snapshot': [dict(row) for row in conn.execute(
                    "SELECT * FROM settings_kv WHERE key='strm.configured_sources.snapshot.v1'",
                )],
                'retired': [dict(row) for row in conn.execute('SELECT * FROM strm_retired_sources ORDER BY source_id')],
            }

    with patch.object(api, 'require_api_login'), patch.object(api, 'reload_runtime', return_value=([], False)):
        assert api.save_config(request, {
            'GY_STRM_SOURCE_DIRS': '[{"id":"11","name":"old"}]',
            'GY_ORGANIZE_SOURCE_DIRS': '[{"id":"11","name":"old"}]',
            'GY_ORGANIZE_NSFW_SOURCE_IDS': '["11"]',
        }) == {'success': True}
        old_runtime = bootstrap.get_agent_kernel_runtime()
        before, before_sources, before_fence = config.ENV_FILE.read_bytes(), source_state(), read_record()
        changes = {'GY_STRM_SOURCE_DIRS': '[{"id":"33","name":"new"}]'}
        rejected = {**changes, 'QB_PASSWORD': 'bad\0value'}
        overrides = config._STARTUP_ENV_OVERRIDES
        environment = {}
        if reason == 'derived_override':
            changes['GY_ORGANIZE_SOURCE_DIRS'] = '[{"id":"22","name":"new"}]'
            rejected = dict(changes)
            overrides = overrides | {'GY_ORGANIZE_NSFW_SOURCE_IDS'}
            environment = {'GY_ORGANIZE_NSFW_SOURCE_IDS': '["11"]'}
        with patch.object(config, '_STARTUP_ENV_OVERRIDES', overrides), patch.dict(os.environ, environment):
            response = api.save_config(request, rejected)
        assert isinstance(response, JSONResponse)
        assert response.status_code == (409 if reason == 'derived_override' else 500)
        if reason == 'derived_override':
            assert json.loads(bytes(response.body)) == {'error': '目标配置由运行环境覆盖: GY_ORGANIZE_NSFW_SOURCE_IDS'}
        assert config.ENV_FILE.read_bytes() == before
        assert source_state() == before_sources
        assert read_record() == before_fence
        gate.initialize_runtime_fence()
        assert bootstrap.get_agent_kernel_runtime() is old_runtime
        assert api.save_config(request, changes) == {'success': True}
        assert json.loads(config.get('GY_STRM_SOURCE_DIRS'))[0]['id'] == '33'
        after = read_record()
        assert after is not None and after['phase'] == 'stable'
        gate.initialize_runtime_fence()
        cold = _run_cold_runtime(runtime_env, "assert json.loads(config.get('GY_STRM_SOURCE_DIRS'))[0]['id'] == '33'\nbootstrap.get_agent_kernel_runtime()\n")
        assert cold.returncode == 0, cold.stderr


def test_strm_database_failure_compensates_but_never_stabilizes(runtime_env):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.routes import api
    import sqlite3
    original = db.reconcile_strm_retired_sources_transaction
    @contextmanager
    def fail_commit(*args, **kwargs):
        with original(*args, **kwargs) as ids:
            yield ids
            raise sqlite3.OperationalError('synthetic DB commit failure')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False)))
    with patch.object(api, 'require_api_login'), patch.object(db, 'reconcile_strm_retired_sources_transaction', side_effect=fail_commit):
        result = api.save_config(cast(Request, request), {'GY_STRM_SOURCE_DIRS': '[{"id":"33","name":"synthetic"}]'})
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    assert config.read_env_snapshot(config.ENV_FILE)[1].get('GY_STRM_SOURCE_DIRS', '') == ''
    read_record_result_4 = read_record()
    assert read_record_result_4 is not None
    assert read_record_result_4['phase'] == 'changing'
    with pytest.raises(gate.RuntimeFenceError):
        bootstrap.get_agent_kernel_runtime()


def test_worker_dies_after_changing_before_file(runtime_env):
    before = config.ENV_FILE.read_bytes()
    action = r'''
from unittest.mock import patch
from app import config
with patch.object(config, 'update_env_file', side_effect=lambda *a, **k: os._exit(27)):
    config.set_and_save({'AGENT_LLM_MODEL': 'never'})
'''
    result = _run_cold_runtime(runtime_env, action)
    assert result.returncode == 27, result.stderr
    read_record_result_5 = read_record()
    assert read_record_result_5 is not None
    assert read_record_result_5['phase'] == 'changing'
    assert config.ENV_FILE.read_bytes() == before
    changing = read_record()
    with pytest.raises(gate.RuntimeFenceError, match='runtime_fence_changing'):
        gate.initialize_runtime_fence()
    assert read_record() == changing
    assert config.ENV_FILE.read_bytes() == before
    with pytest.raises(gate.RuntimeFenceError):
        bootstrap.get_agent_kernel_runtime()


def test_missing_established_database_is_not_silently_authorized(runtime_env):
    import sqlite3
    runtime_env.database_path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        config.set_and_save({'AGENT_LLM_MODEL': 'never'})
    assert config.read_env_snapshot(config.ENV_FILE)[1]['AGENT_LLM_MODEL'] == 'old'


def test_cold_process_reuses_persisted_stable_binding(runtime_env):
    action = r'''
runtime = bootstrap.get_agent_kernel_runtime()
binding = runtime.creation_binding
print(json.dumps({'incarnation': binding.fence.incarnation,
                  'epoch': binding.fence.epoch, 'effective': binding.effective_digest,
                  'model': runtime.session.model.settings.model}), flush=True)
'''
    results = []
    for _ in range(2):
        result = _run_cold_runtime(runtime_env, action)
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
    assert results[0]['model'] == 'old'


def test_restore_rebinds_verified_payload_and_revokes_old_runtime(runtime_env, tmp_path):
    import sqlite3

    from app.env_file import read_env_bytes
    from app.modules import backup

    old_runtime = bootstrap.get_agent_kernel_runtime()
    old = read_record()
    archive = tmp_path / 'synthetic.zip'
    backup.create_backup(runtime_env, output=archive)
    original = backup._transactional_restore_files
    observed = []

    def observe(paths, files):
        with closing(sqlite3.connect(files['database/mediaflux.db'][1])) as conn, conn:
            row = json.loads(conn.execute(
                'SELECT value FROM settings_kv WHERE key=?', (gate.RUNTIME_FENCE_KEY,)
            ).fetchone()[0])
        observed.append((row, read_env_bytes(files['config/user.env'][1].read_bytes())))
        return original(paths, files)

    with patch.object(backup, '_transactional_restore_files', side_effect=observe):
        backup.restore_backup(runtime_env, archive)

    restored, restored_values = observed[0]
    assert restored['phase'] == 'stable'
    assert old is not None
    assert restored['incarnation'] != old['incarnation']
    assert restored['config_digest'] == config._runtime_digest(restored_values)
    assert read_record() == restored
    new_runtime = bootstrap.get_agent_kernel_runtime()
    assert new_runtime is not old_runtime
    with db.get_conn() as conn, pytest.raises(gate.RuntimeFenceError):
        assert old_runtime.creation_binding is not None
        gate.require_runtime_fence(conn, old_runtime.creation_binding.fence)


def test_restore_rejects_changing_backup_without_touching_existing_db_or_env(runtime_env, tmp_path):
    import sqlite3

    from app.modules import backup

    before_env = config.ENV_FILE.read_bytes()
    with db.get_conn() as conn:
        previous_fence = conn.execute(
            'SELECT value,updated_at FROM settings_kv WHERE key=?', (gate.RUNTIME_FENCE_KEY,)
        ).fetchone()

    with patch.object(config, '_publish_noreplace', side_effect=OSError('disk failure')), pytest.raises(OSError):
        config.update_runtime_env_file(
            config.ENV_FILE, {'AGENT_LLM_MODEL': 'unpublished'}, expected=before_env
        )
    read_record_result_6 = read_record()
    assert read_record_result_6 is not None
    assert read_record_result_6['phase'] == 'changing'

    archive = tmp_path / 'changing-fence.zip'
    backup.create_backup(runtime_env, output=archive)
    with backup._verified_backup(archive) as (_manifest, payloads):
        with closing(sqlite3.connect(payloads['database/mediaflux.db'])) as conn, conn:
            archived = json.loads(conn.execute(
                'SELECT value FROM settings_kv WHERE key=?', (gate.RUNTIME_FENCE_KEY,)
            ).fetchone()[0])
    assert archived['phase'] == 'changing'

    # Restore the existing target's prior stable authorization so rejection
    # can prove that restore did not replace either canonical file.
    with db.get_conn() as conn:
        conn.execute(
            'UPDATE settings_kv SET value=?,updated_at=? WHERE key=?',
            (previous_fence['value'], previous_fence['updated_at'], gate.RUNTIME_FENCE_KEY),
        )
    assert config.ENV_FILE.read_bytes() == before_env

    def file_state(path):
        sidecars = (
            path,
            path.with_name(path.name + '-wal'),
            path.with_name(path.name + '-shm'),
        )
        state = [candidate.read_bytes() if candidate.exists() else None for candidate in sidecars]
        return state

    db_state = file_state(runtime_env.database_path)
    env_state = config.ENV_FILE.read_bytes()
    with pytest.raises(backup.BackupError, match='未完成变更状态'):
        backup.restore_backup(runtime_env, archive)
    assert file_state(runtime_env.database_path) == db_state
    assert config.ENV_FILE.read_bytes() == env_state
