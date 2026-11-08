"""Measured C -> B order during concurrent real startup and manual backup."""
import sqlite3
import threading
import zipfile
from contextlib import closing
from unittest.mock import patch

import pytest
from app.agent import feature_gate
from app.modules import backup
from app.modules.process_lock import CrossProcessLock
from tests.test_setup_bootstrap_support import first_run_case


@pytest.mark.parametrize('first', ['startup', 'manual'])
def test_startup_and_manual_backup_keep_config_before_backup(first, request):
    with first_run_case() as case:
        # Importing app.main creates its module-level app. Warm the import while
        # this case still has an empty installation; importing after the legacy
        # database exists would migrate it outside the measured concurrency.
        import app.main  # noqa: F401
        case.paths.ensure_writable_dirs()
        case.paths.env_file.write_text('MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=synthetic-password\nWEB_SECRET_KEY=synthetic-secret\n')
        case._reload_runtime_modules()
        with closing(sqlite3.connect(case.paths.database_path)) as conn:
            conn.execute('CREATE TABLE organize_log (id INTEGER PRIMARY KEY, source TEXT NOT NULL, original_path TEXT NOT NULL, new_path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.commit()

        held, attempted, proceed = threading.Event(), threading.Event(), threading.Event()
        active = set()
        events, errors = [], []
        acquire, release = CrossProcessLock.acquire, CrossProcessLock.release
        first_tid = []
        roles = {}
        def take(lock, blocking=True):
            tid = threading.current_thread()
            name = lock._name
            if name == 'config-snapshot' and first_tid and tid != first_tid[0]:
                attempted.set()
            if name == 'backup-restore':
                assert tid in active, 'B requested without C'
            result = acquire(lock, blocking=blocking)
            if result:
                events.append((tid, name, 'acquired'))
                if name == 'config-snapshot':
                    active.add(tid)
                    if not first_tid:
                        first_tid.append(tid)
                        held.set()
                        assert proceed.wait(5)
            return result
        def drop(lock):
            tid = threading.current_thread()
            if lock._name == 'config-snapshot':
                active.remove(tid)
            events.append((tid, lock._name, 'released'))
            return release(lock)
        def run(role):
            roles[threading.current_thread()] = role
            try:
                if role == 'startup':
                    # Admission migrates at construction, not just lifespan.
                    with case._new_client():
                        with case.database.get_conn() as conn:
                            assert feature_gate.read_runtime_fence(conn).phase == 'stable'
                else:
                    backup.create_backup(case.paths, output=case.paths.backup_dir/'manual.zip')
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(role,), daemon=True) for role in (first, 'manual' if first == 'startup' else 'startup')]
        with patch.object(CrossProcessLock, 'acquire', take), patch.object(CrossProcessLock, 'release', drop):
            try:
                threads[0].start()
                assert held.wait(5)
                threads[1].start()
                assert attempted.wait(5)
            finally:
                proceed.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(8)
            assert not any(thread.is_alive() for thread in threads)
        assert not errors, errors
        assert not active
        request.node.user_properties.append(('lock_trace', repr([(roles.get(tid, 'lifespan'), name, action) for tid, name, action in events])))
        # Construction now includes recovery (C -> B), then migration backup.
        taken = [roles[tid] for tid, name, action in events if (name, action) == ('backup-restore', 'acquired')]
        assert taken.count('startup') == 2
        assert taken.count('manual') == 1
        assert len(taken) == 3
        assert len(list(case.paths.backup_dir.glob('*.zip'))) == 2
        for archive in case.paths.backup_dir.glob('*.zip'):
            with zipfile.ZipFile(archive) as z:
                assert z.testzip() is None
                assert z.read('database/mediaflux.db')


def test_real_restore_and_recovery_keep_exclusive_lifecycle_order(tmp_path):
    from tests.test_config_snapshot_ownership import paths_at
    paths = paths_at(tmp_path)
    paths.ensure_writable_dirs()
    with closing(sqlite3.connect(paths.database_path)) as conn:
        conn.execute('CREATE TABLE sample(value TEXT)')
        conn.execute("INSERT INTO sample VALUES ('saved')")
        conn.commit()
    paths.env_file.write_text('SYNTHETIC=1\n')
    archive = backup.create_backup(paths)
    events = []
    acquire, release = CrossProcessLock.acquire, CrossProcessLock.release
    def take(lock, blocking=True):
        result = acquire(lock, blocking=blocking)
        if result:
            events.append(('enter', lock._name))
        return result
    def drop(lock):
        events.append(('exit', lock._name))
        return release(lock)
    expected = [('enter', name) for name in ('runtime-lifecycle', 'config-snapshot', 'backup-restore')]
    expected += [('exit', name) for name in ('backup-restore', 'config-snapshot', 'runtime-lifecycle')]
    with patch.object(CrossProcessLock, 'acquire', take), patch.object(CrossProcessLock, 'release', drop):
        backup.restore_backup(paths, archive)
        assert events == expected
        events.clear()
        assert backup.recover_pending_restore(paths) is False
        assert events == expected
    with closing(sqlite3.connect(paths.database_path)) as conn:
        assert conn.execute('SELECT value FROM sample').fetchall() == [('saved',)]
