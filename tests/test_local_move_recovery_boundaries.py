"""移动已发布与回滚占用之间的真实文件系统异常边界。"""
from __future__ import annotations

from typing import cast
from app.modules.scraper import TMDBScraper

import errno
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.repositories import local_media as local_media_repo
from app.modules.local_media_service import LocalMediaService
from app.modules.local_move_transaction import LocalMoveError, LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper
from tests.test_local_move_transaction import Plan


class LocalMoveRecoveryBoundariesTests(IsolatedDatabaseTestCase):
    def test_identity_read_after_publication_restores_new_and_old_media(self):
        for action in ('move', 'replace'):
            for persistent in (False, True):
                with self.subTest(action=action, persistent=persistent), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    src, dst = root / 'source', root / 'library'
                    src.mkdir(); dst.mkdir()
                    source, target = src / 'Movie.mkv', dst / 'Movie.mkv'
                    source.write_bytes(b'only-new-copy')
                    identity = source.stat()
                    if action == 'replace':
                        target.write_bytes(b'old-library-copy')
                    plan = Plan(LocalFilesystemAdapter(src).snapshot(source), target, action=action)
                    real_lstat = Path.lstat
                    faults = []

                    def fail_published_lstat(path, *args, **kwargs):
                        info = real_lstat(path, *args, **kwargs)
                        if ((persistent or not faults) and not source.exists()
                                and str(path).startswith('/proc/self/fd/')
                                and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)):
                            faults.append(True)
                            raise OSError(errno.EIO, 'injected post-publication identity read')
                        return info

                    with patch.object(Path, 'lstat', new=fail_published_lstat):
                        with self.assertRaises(LocalMoveError) as caught:
                            LocalMoveTransaction([src], [dst]).execute([plan])
                    self.assertTrue(faults)
                    if persistent:
                        self.assertTrue(caught.exception.rollback_errors)
                        self.assertEqual(target.read_bytes(), b'only-new-copy')
                    else:
                        self.assertEqual(caught.exception.rollback_errors, [])
                        self.assertEqual(source.read_bytes(), b'only-new-copy')
                        if action == 'replace':
                            self.assertEqual(target.read_bytes(), b'old-library-copy')
                        else:
                            self.assertFalse(target.exists())
                        self.assertEqual(list(dst.glob('.*.mediaflux-replaced-*')), [])

    def test_outer_service_records_rollback_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as raw:
            src, dst = Path(raw) / 'source', Path(raw) / 'library'
            src.mkdir(); dst.mkdir()
            source = src / 'Movie.2026.mkv'
            source.write_bytes(b'only-new-copy')
            identity = source.stat()
            source_id = local_media_repo.create_local_media_source(name='recovery-boundary', qb_profile='',
                qb_path_prefix='', local_root=str(src), owner='admin')
            local_media_repo.upsert_local_library_target(source_id, 'movie', str(dst), owner='admin')
            service = LocalMediaService(scraper=cast(TMDBScraper, FakeScraper(MatchResult(
                tmdb_id='1', title='Movie', year='2026', media_type='movie', confidence=1.0))))
            real_lstat = Path.lstat
            faults = []

            def fail_published_lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if (not faults and not source.exists() and str(path).startswith('/proc/self/fd/')
                        and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)):
                    faults.append(True)
                    raise OSError(errno.EIO, 'injected one-shot identity failure')
                return info

            try:
                with patch('app.modules.local_media_service.probe_local_media_profile', return_value=None), \
                     patch.object(Path, 'lstat', new=fail_published_lstat):
                    inspection = service.inspect_source('admin', source_id, source)
                    preview = service.preview('admin', inspection['inspection_id'], '1', 'movie')
                    task_id = service.create_manual_task(
                        'admin', inspection['inspection_id'], preview_digest=preview['preview_digest'],
                    )
                    self.assertTrue(local_media_repo.claim_local_media_task(task_id, owner='admin'))
                    with self.assertRaises(LocalMoveError) as caught:
                        service.execute_task('admin', task_id)
            finally:
                service.close()
            self.assertEqual(faults, [True])
            self.assertEqual(caught.exception.rollback_errors, [])
            self.assertEqual(source.read_bytes(), b'only-new-copy')
            get_local_media_task_result = local_media_repo.get_local_media_task(task_id, owner='admin')
            assert get_local_media_task_result is not None
            self.assertEqual(get_local_media_task_result.status, 'failed')
            self.assertEqual([r['status'] for r in local_media_repo.list_local_media_operation_steps(
                task_id, owner='admin')], ['rolled_back'])

    def test_rollback_never_overwrites_source_created_after_precheck(self):
        for same_fs in (True, False):
            with self.subTest(same_fs=same_fs), tempfile.TemporaryDirectory() as raw:
                src, dst = Path(raw) / 'source', Path(raw) / 'library'
                src.mkdir(); dst.mkdir()
                source, second = src / 'A.mkv', src / 'B.mkv'
                source.write_bytes(b'original'); second.write_bytes(b'second')
                target = dst / 'A.mkv'
                (dst / 'B.mkv').write_bytes(b'occupied')
                adapter = LocalFilesystemAdapter(src)
                plans = [Plan(adapter.snapshot(p), dst / p.name) for p in (source, second)]
                txn = LocalMoveTransaction([src], [dst])
                injected = []

                def filesystem_probe(*args):
                    if txn._moved and not source.exists():
                        source.write_bytes(b'new-independent-download')
                        injected.append(True)
                    return same_fs

                with patch.object(LocalFilesystemAdapter, 'same_filesystem', side_effect=filesystem_probe):
                    with self.assertRaises(LocalMoveError) as caught:
                        txn.execute(plans)
                self.assertEqual(injected, [True])
                self.assertEqual(source.read_bytes(), b'new-independent-download')
                self.assertEqual(target.read_bytes(), b'original')
                self.assertTrue(caught.exception.rollback_errors)
                self.assertEqual(list(src.glob('.*.mediaflux-rollback-*')), [])

    def test_cross_filesystem_identity_failure_keeps_source_and_restores_old_target(self):
        for action in ('move', 'replace'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as raw:
                src, dst = Path(raw) / 'source', Path(raw) / 'library'
                src.mkdir(); dst.mkdir()
                source, target = src / 'Movie.mkv', dst / 'Movie.mkv'
                source.write_bytes(b'original-copy-source')
                if action == 'replace':
                    target.write_bytes(b'old-library-copy')
                plan = Plan(LocalFilesystemAdapter(src).snapshot(source), target, action=action)
                txn = LocalMoveTransaction([src], [dst])
                real_require = txn._require_identity
                faults = []

                def fail_target_read(path, expected, *, label):
                    if label == '事务目标' and not faults:
                        self.assertTrue(source.exists())
                        self.assertEqual(target.read_bytes(), b'original-copy-source')
                        faults.append(True)
                        raise OSError(errno.EIO, 'injected copy-publication identity failure')
                    return real_require(path, expected, label=label)

                with patch.object(LocalFilesystemAdapter, 'same_filesystem', return_value=False), \
                     patch.object(txn, '_require_identity', side_effect=fail_target_read):
                    with self.assertRaises(LocalMoveError) as caught:
                        txn.execute([plan])
                self.assertEqual(faults, [True])
                self.assertEqual(caught.exception.rollback_errors, [])
                self.assertEqual(source.read_bytes(), b'original-copy-source')
                if action == 'replace':
                    self.assertEqual(target.read_bytes(), b'old-library-copy')
                else:
                    self.assertFalse(target.exists())
                self.assertEqual(list(dst.glob('.*.mediaflux-*')), [])
