"""Established storage must be recognizable before startup can migrate it."""
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from app.agent import feature_gate as gate
from tests.test_setup_bootstrap_support import first_run_case


@pytest.mark.parametrize('agent_enabled', ['0', '1'])
def test_startup_rejects_configuration_mismatch_before_migration(agent_enabled):
    from app.main import _initialize_runtime_storage
    from tests.test_setup_bootstrap_support import initialize_paths

    with first_run_case() as case:
        initialize_paths(case.paths, {'AGENT_ENABLED': agent_enabled})
        case._reload_runtime_modules()
        with case.database.get_conn() as conn:
            conn.execute('PRAGMA user_version=0')
        # A different file must not cause migrations or recovery writes
        # before the existing installation/configuration binding is checked.
        with case.paths.env_file.open('a') as stream:
            stream.write('\nWEB_HOST=127.0.0.2\n')
        before_db = case.paths.database_path.read_bytes()
        before_config = case.paths.env_file.read_bytes()
        with patch.object(case.database, 'init_db', wraps=case.database.init_db) as init:
            with pytest.raises(gate.RuntimeFenceError, match='^runtime_snapshot_mismatch$'):
                _initialize_runtime_storage()
            init.assert_not_called()
        assert case.paths.database_path.read_bytes() == before_db
        assert case.paths.env_file.read_bytes() == before_config


@pytest.mark.parametrize('secret', [True, False], ids=['lifespan', 'pre-secret'])
@pytest.mark.parametrize('configuration', ['file', 'environment'])
@pytest.mark.parametrize('schema', ['zero-byte', 'empty-sqlite', 'unrelated', 'settings-only', 'name-only', 'legacy'])
def test_startup_recognizes_legacy_storage_before_migration(secret, configuration, schema):
    with first_run_case() as case:
        case.paths.ensure_writable_dirs()
        values = {'MEDIAFLUX_INITIALIZED': '1', 'ENV_WEB_PASSPORT': 'admin',
                  'ENV_WEB_PASSWORD': 'synthetic-password'}
        if secret:
            values['WEB_SECRET_KEY'] = 'synthetic-secret'
        if configuration == 'file':
            case.paths.env_file.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
        else:
            os.environ.update(values)
        case._reload_runtime_modules()
        assert not case.first_run.needs_initialization()
        path = case.paths.database_path
        if schema not in ('zero-byte', 'empty-sqlite'):
            # Isolated migration mode omits backup I/O, not startup/schema
            # or fence checks. Empty DB regressions use the ordinary path.
            path = path.with_name('isolated-migration.db')
            case.database.configure_database(path, test_mode=True)
        path.touch()
        with closing(sqlite3.connect(path)) as conn:
            if schema == 'empty-sqlite':
                conn.execute('VACUUM')
            elif schema == 'unrelated':
                conn.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')
            elif schema == 'settings-only':
                conn.execute('CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)')
            elif schema == 'name-only':
                conn.execute('CREATE TABLE organize_log (id INTEGER PRIMARY KEY)')
            elif schema == 'legacy':
                # Unversioned early application schema, without settings_kv,
                # newer organize columns, or any of the later feature tables.
                conn.execute('CREATE TABLE organize_log (id INTEGER PRIMARY KEY, source TEXT NOT NULL, original_path TEXT NOT NULL, new_path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)')
                conn.execute("INSERT INTO organize_log VALUES (1, 'guangya', 'old', 'new', 'success', '2020-01-01')")
            conn.commit()
        if schema == 'zero-byte':
            assert path.stat().st_size == 0
        else:
            assert path.stat().st_size > 0
        before_db = path.read_bytes()
        before_config = case.paths.env_file.read_bytes() if case.paths.env_file.exists() else None
        original = case.database.init_db
        with patch.object(case.database, 'init_db', wraps=original) as init:
            if schema == 'legacy':
                with case._new_client():
                    with case.database.get_conn() as conn:
                        fence = gate.read_runtime_fence(conn)
                        assert fence.phase == 'stable'
                        assert tuple(conn.execute('SELECT original_path, new_path FROM organize_log WHERE id=1').fetchone()) == ('old', 'new')
                with case._new_client():
                    with case.database.get_conn() as conn:
                        assert gate.read_runtime_fence(conn) == fence
                assert init.call_count >= 2
                if configuration == 'environment':
                    saved = case.config.read_env_snapshot(case.paths.env_file)[1]
                    assert not (saved.keys() & values.keys())  # 不落盘部署凭据。
                    # 首次迁移后可通过同一配置发布链修改设置。
                    case.config.set_and_save({'WEB_HOST': '127.0.0.1'})
                    assert case.config.get('WEB_HOST') == '127.0.0.1'
                    # 已建立 fence 后，环境变量不能掩盖文件丢失。
                    case.paths.env_file.unlink()
                    case._reload_runtime_modules()
                    case.database.configure_database(path, test_mode=True)
                    with pytest.raises(gate.RuntimeFenceError, match='runtime_configuration_unavailable'):
                        case._new_client()
                    assert not case.paths.env_file.exists()
            else:
                with pytest.raises(gate.RuntimeFenceError, match='^runtime_database_schema_unrecognized$'):
                    with case._new_client():
                        pass
                assert init.call_count == 0
                assert path.read_bytes() == before_db
                assert (case.paths.env_file.read_bytes() if case.paths.env_file.exists() else None) == before_config
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                    has_kv = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings_kv'").fetchone()
                    if has_kv:
                        assert conn.execute('SELECT 1 FROM settings_kv WHERE key=?', (gate.RUNTIME_FENCE_KEY,)).fetchone() is None
                assert path.read_bytes() == before_db
