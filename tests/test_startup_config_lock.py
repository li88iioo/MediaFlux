"""Standard installed-database startup must preserve a real pre-migration backup."""
import sqlite3
import zipfile
from contextlib import closing, contextmanager
from unittest.mock import patch

import pytest
from app.agent import feature_gate as gate
from app.modules import backup
from tests.test_setup_bootstrap_support import first_run_case


@pytest.mark.parametrize('secret', [True, False], ids=['lifespan', 'pre-secret'])
def test_standard_legacy_startup_backup_and_restart(secret):
    with first_run_case() as case:
        case.paths.ensure_writable_dirs()
        case.paths.env_file.write_text(
            'MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\n'
            'ENV_WEB_PASSWORD=synthetic-password\n'
            + ('WEB_SECRET_KEY=synthetic-secret\n' if secret else '')
        )
        case._reload_runtime_modules()
        path = case.paths.database_path
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE organize_log (id INTEGER PRIMARY KEY, source TEXT NOT NULL, original_path TEXT NOT NULL, new_path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.execute("INSERT INTO organize_log VALUES (1, 'guangya', 'old', 'new', 'success', '2020-01-01')")
            conn.commit()
        before_config = case.paths.env_file.read_bytes()
        assert not case.database._test_mode_enabled()
        assert case.database.resolve_db_path().resolve() == path.resolve()
        observed = []
        real_backup = backup.create_backup
        real_get_conn = case.database.get_conn

        def observe_backup(*args, **kwargs):
            assert kwargs['include_settings'] is True
            assert isinstance(kwargs['source_connection'], sqlite3.Connection)
            return real_backup(*args, **kwargs)

        @contextmanager
        def observe_conn(*args, **kwargs):
            with real_get_conn(*args, **kwargs) as conn:
                conn.set_trace_callback(observed.append)
                yield conn

        with patch.object(backup, 'create_backup', side_effect=observe_backup) as create, patch.object(case.database, 'get_conn', side_effect=observe_conn):
            with case._new_client():
                with case.database.get_conn() as conn:
                    fence = gate.read_runtime_fence(conn)
                    assert fence.phase == 'stable'
                    assert conn.execute('PRAGMA user_version').fetchone()[0] > 0
                    assert tuple(conn.execute('SELECT original_path, new_path FROM organize_log WHERE id=1').fetchone()) == ('old', 'new')
            archives = list(case.paths.backup_dir.glob('*.zip'))
            assert len(archives) == 1
            with zipfile.ZipFile(archives[0]) as z:
                assert z.read('config/user.env') == before_config
                olddb = path.parent / 'archive-check.db'
                olddb.write_bytes(z.read('database/mediaflux.db'))
            with closing(sqlite3.connect(olddb)) as conn:
                assert conn.execute('PRAGMA user_version').fetchone()[0] == 0
                assert tuple(conn.execute('SELECT original_path, new_path FROM organize_log WHERE id=1').fetchone()) == ('old', 'new')
                assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('organize_log',)]
            with case._new_client():
                with case.database.get_conn() as conn:
                    assert gate.read_runtime_fence(conn) == fence
            assert list(case.paths.backup_dir.glob('*.zip')) == archives
            assert create.call_count == 1
            assert len([sql for sql in observed if sql.startswith('INSERT INTO settings_kv') and gate.RUNTIME_FENCE_KEY in sql]) == 1


@pytest.mark.parametrize('secret', [True, False], ids=['lifespan', 'pre-secret'])
def test_migration_backup_failure_cancels_startup_without_fence(secret):
    with first_run_case() as case:
        case.paths.ensure_writable_dirs()
        case.paths.env_file.write_text(
            'MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\n'
            'ENV_WEB_PASSWORD=synthetic-password\n'
            + ('WEB_SECRET_KEY=synthetic-secret\n' if secret else '')
        )
        case._reload_runtime_modules()
        with closing(sqlite3.connect(case.paths.database_path)) as conn:
            conn.execute('CREATE TABLE organize_log (id INTEGER PRIMARY KEY, source TEXT NOT NULL, original_path TEXT NOT NULL, new_path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.commit()
        before_config = case.paths.env_file.read_bytes()
        with patch.object(backup, '_create_backup_unlocked', side_effect=backup.BackupError('synthetic archive failure')):
            with pytest.raises(RuntimeError, match='synthetic archive failure'):
                with case._new_client():
                    pytest.fail('startup admitted without backup')
        with closing(sqlite3.connect(case.paths.database_path)) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 0
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('organize_log',)]
        assert case.paths.env_file.read_bytes() == before_config
        from app.modules.process_lock import CrossProcessLock
        lock = CrossProcessLock('config-snapshot', directory=case.paths.data_dir)
        assert lock.acquire(blocking=False)
        lock.release()
