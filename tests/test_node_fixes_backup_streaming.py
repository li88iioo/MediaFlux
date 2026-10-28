"""备份、校验、恢复统一使用有界流，保持既有归档与事务协议。"""
from __future__ import annotations

import sqlite3
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.modules import backup as backup_module
from app.modules.backup import BackupError, create_backup, restore_backup, verify_backup
from tests.test_backup import make_paths


class BackupStreamingTests(unittest.TestCase):
    def make_database(self, root, mib=2):
        paths = make_paths(root)
        paths.ensure_writable_dirs()
        with sqlite3.connect(paths.database_path) as conn:
            conn.execute('CREATE TABLE fixture(value BLOB)')
            conn.executemany('INSERT INTO fixture VALUES(zeroblob(1048576))', [()] * mib)
        paths.env_file.write_text('WEB_PORT=1258\n')
        return paths

    def test_create_never_materializes_database_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw))
            original = Path.read_bytes

            def bounded(path):
                self.assertNotEqual(path.suffix, '.db', 'SQLite snapshot must stay on disk')
                return original(path)

            with patch.object(Path, 'read_bytes', new=bounded):
                archive = create_backup(paths)
            self.assertEqual(verify_backup(archive).as_dict()['database_schema_version'], 0)

    def test_verify_and_restore_do_not_read_whole_archive_payloads(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw))
            archive = create_backup(paths)
            original = zipfile.ZipFile.read

            def metadata_only(archive_obj, name, *args, **kwargs):
                self.assertEqual(name, 'manifest.json', 'payloads must use bounded streams')
                return original(archive_obj, name, *args, **kwargs)

            with sqlite3.connect(paths.database_path) as conn:
                conn.execute('DELETE FROM fixture')
            paths.env_file.write_text('WEB_PORT=9000\n')
            with patch.object(zipfile.ZipFile, 'read', new=metadata_only):
                verify_backup(archive)
                restore_backup(paths, archive)
            with sqlite3.connect(paths.database_path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 2)
            self.assertEqual(paths.env_file.read_text(), 'WEB_PORT=1258\n')

    def test_backup_round_trip_in_temporary_path_with_uri_delimiters(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw), mib=1)
            with sqlite3.connect(paths.database_path) as conn:
                conn.execute('PRAGMA user_version=1')
            staging = Path(raw) / '备份 #100%? 暂存'
            staging.mkdir()
            with patch.object(tempfile, 'tempdir', str(staging)):
                archive = create_backup(paths)
                self.assertEqual(verify_backup(archive).as_dict()['database_schema_version'], 1)
                with sqlite3.connect(paths.database_path) as conn:
                    conn.execute('DELETE FROM fixture')
                paths.env_file.write_text('WEB_PORT=9000\n')
                restore_backup(paths, archive)
            with sqlite3.connect(paths.database_path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 1)
            self.assertEqual(paths.env_file.read_text(), 'WEB_PORT=1258\n')
            self.assertEqual(list(staging.iterdir()), [])
            self.assertFalse((Path(raw) / '备份 ').exists())

    def test_large_backup_and_verification_have_bounded_python_allocations(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw), mib=24)
            archive = None
            for operation in ('create', 'verify'):
                with self.subTest(operation=operation):
                    tracemalloc.start()
                    try:
                        if operation == 'create':
                            archive = create_backup(paths)
                        else:
                            assert archive is not None
                            verify_backup(archive)
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    self.assertLess(peak, 8 * 1024 * 1024, 'whole-database memory allocation regressed')

    def test_archive_write_failure_preserves_existing_backup_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw))
            archive = create_backup(paths)
            original = archive.read_bytes()
            with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
                with self.assertRaises(BackupError):
                    create_backup(paths, output=archive)
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(list(archive.parent.glob(f'.{archive.name}.*.tmp')), [])
            verify_backup(archive)

    def test_partial_restore_staging_failure_preserves_live_data_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = self.make_database(Path(raw))
            archive = create_backup(paths)
            with sqlite3.connect(paths.database_path) as conn:
                conn.execute('DELETE FROM fixture')
            paths.env_file.write_text('WEB_PORT=9000\n')
            real_copy = backup_module._copy_stream
            faults = []

            def fail_staging(source, target):
                if '.restore.' in str(target.name):
                    target.write(b'partial database')
                    faults.append(True)
                    raise OSError('disk full while staging restore')
                return real_copy(source, target)

            with patch.object(backup_module, '_copy_stream', side_effect=fail_staging):
                with self.assertRaises(BackupError):
                    restore_backup(paths, archive)
            self.assertEqual(faults, [True])
            with sqlite3.connect(paths.database_path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM fixture').fetchone()[0], 0)
            self.assertEqual(paths.env_file.read_text(), 'WEB_PORT=9000\n')
            self.assertEqual(list(Path(raw).rglob('*.restore.*')), [])
            self.assertFalse((paths.data_dir / '.mediaflux-restore.journal.json').exists())
