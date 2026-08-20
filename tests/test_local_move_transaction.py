"""同盘与跨盘移动事务测试。"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.modules.local_move_transaction import LocalMoveError, LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter
from tests.support import IsolatedDatabaseTestCase


@dataclass(frozen=True)
class Plan:
    source: object
    target: Path
    role: str = "video"
    action: str = "move"
    expected_target_identity: tuple[int, int, int, int] | None = None


class LocalMoveTransactionTests(IsolatedDatabaseTestCase):
    def _task(self, source_root: Path) -> int:
        source_id = db.create_local_media_source(
            name=f"source-{uuid.uuid4().hex}", qb_profile="", qb_path_prefix="",
            local_root=str(source_root), owner="admin",
        )
        return db.create_local_media_task(
            source_id, "", str(source_root), owner="admin", trigger="manual"
        )

    def test_same_filesystem_uses_atomic_no_replace_publish_and_records_steps(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"movie-data")
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            task_id = self._task(source_root)
            target = target_root / "Movies" / "Movie.mkv"
            result = LocalMoveTransaction(
                [source_root], [target_root], task_id=task_id, operation_token="same-fs"
            ).execute([Plan(snapshot, target)])
            self.assertEqual(result.status, "completed")
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"movie-data")
            steps = db.list_local_media_operation_steps(task_id, owner="admin")
            self.assertEqual((steps[0]["action"], steps[0]["status"]), ("rename", "completed"))

    def test_replace_action_commits_new_file_and_removes_temporary_backup(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"new-version")
            target = target_root / "Movie.mkv"
            target.write_bytes(b"old-version")
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            result = LocalMoveTransaction(
                [source_root], [target_root], operation_token="replace-version"
            ).execute([Plan(snapshot, target, action="replace")])
            self.assertEqual(result.status, "completed")
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"new-version")
            self.assertEqual(list(target_root.glob(".*.mediaflux-replaced-*")), [])

    def test_later_failure_restores_replaced_target_and_all_sources(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            first, second = source_root / "A.mkv", source_root / "B.mkv"
            first.write_bytes(b"new-a"); second.write_bytes(b"new-b")
            old_target = target_root / "A.mkv"
            old_target.write_bytes(b"old-a")
            adapter = LocalFilesystemAdapter(source_root)
            plans = [
                Plan(adapter.snapshot(first), old_target, action="replace"),
                Plan(adapter.snapshot(second), target_root / "B.mkv"),
            ]
            with patch.object(
                LocalMoveTransaction,
                "_publish_no_replace",
                side_effect=OSError("injected later failure"),
            ):
                with self.assertRaisesRegex(LocalMoveError, "injected later failure"):
                    LocalMoveTransaction(
                        [source_root], [target_root], operation_token="replace-rollback"
                    ).execute(plans)
            self.assertEqual(first.read_bytes(), b"new-a")
            self.assertEqual(second.read_bytes(), b"new-b")
            self.assertEqual(old_target.read_bytes(), b"old-a")
            self.assertFalse((target_root / "B.mkv").exists())
            self.assertEqual(list(target_root.glob(".*.mediaflux-replaced-*")), [])

    def test_cross_filesystem_copy_verify_commit_removes_source_and_partial(self):
        with tempfile.TemporaryDirectory() as source_raw, tempfile.TemporaryDirectory() as target_raw:
            source_root, target_root = Path(source_raw), Path(target_raw)
            source = source_root / "Movie.mkv"
            source.write_bytes(b"x" * 100_000)
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            target = target_root / "Movie.mkv"
            with patch.object(LocalFilesystemAdapter, "same_filesystem", return_value=False):
                LocalMoveTransaction([source_root], [target_root], operation_token="cross-fs").execute(
                    [Plan(snapshot, target)]
                )
            self.assertFalse(source.exists())
            self.assertEqual(target.stat().st_size, 100_000)
            self.assertEqual(list(target_root.glob("*.mediaflux-partial*")), [])
            self.assertEqual(list(target_root.glob(".*.mediaflux-partial-*")), [])

    def test_second_move_failure_rolls_back_first_in_reverse_order(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            first, second = source_root / "A.mkv", source_root / "B.mkv"
            first.write_bytes(b"a"); second.write_bytes(b"b")
            adapter = LocalFilesystemAdapter(source_root)
            plans = [
                Plan(adapter.snapshot(first), target_root / "A.mkv"),
                Plan(adapter.snapshot(second), target_root / "B.mkv"),
            ]
            real_publish = LocalMoveTransaction._publish_no_replace
            calls = 0

            def fail_second(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected failure")
                return real_publish(src, dst)

            with patch.object(
                LocalMoveTransaction, "_publish_no_replace", side_effect=fail_second
            ):
                with self.assertRaisesRegex(LocalMoveError, "injected failure"):
                    LocalMoveTransaction([source_root], [target_root]).execute(plans)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertFalse((target_root / "A.mkv").exists())

    def test_changed_snapshot_and_existing_target_abort_without_writes(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"old")
            adapter = LocalFilesystemAdapter(source_root)
            snapshot = adapter.snapshot(source)
            source.write_bytes(b"changed")
            target = target_root / "Movie.mkv"
            with self.assertRaises(LocalMoveError):
                LocalMoveTransaction([source_root], [target_root]).execute([Plan(snapshot, target)])
            self.assertFalse(target.exists())

            snapshot = adapter.snapshot(source)
            target.write_bytes(b"existing")
            with self.assertRaisesRegex(LocalMoveError, "已存在"):
                LocalMoveTransaction([source_root], [target_root]).execute([Plan(snapshot, target)])
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertTrue(source.exists())

    def test_same_filesystem_verification_failure_restores_source_and_audit(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "source"; target_root = root / "target"
            source_root.mkdir(); target_root.mkdir(); source = source_root / "movie.mkv"
            source.write_bytes(b"movie")
            task_id = self._task(source_root)
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            transaction = LocalMoveTransaction(
                [source_root], [target_root], task_id=task_id, owner="admin", operation_token="verify-fail"
            )
            with patch.object(transaction, "_verify_target", side_effect=LocalMoveError("verify failed")):
                with self.assertRaisesRegex(LocalMoveError, "verify failed"):
                    transaction.execute([Plan(snapshot, target_root / source.name)])
            self.assertTrue(source.exists())
            self.assertFalse((target_root / source.name).exists())
            steps = db.list_local_media_operation_steps(task_id, owner="admin")
            self.assertEqual(steps[0]["status"], "rolled_back")

    def test_cross_filesystem_final_verification_failure_keeps_source_and_removes_target(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "source"; target_root = root / "target"
            source_root.mkdir(); target_root.mkdir(); source = source_root / "movie.mkv"
            source.write_bytes(b"movie")
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            transaction = LocalMoveTransaction([source_root], [target_root])
            original = transaction._verify_target
            calls = 0
            def fail_target(fingerprint, target, size):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise LocalMoveError("target verify failed")
                return original(fingerprint, target, size)
            with patch("app.modules.local_move_transaction.LocalFilesystemAdapter.same_filesystem", return_value=False), patch.object(
                transaction, "_verify_target", side_effect=fail_target
            ):
                with self.assertRaisesRegex(LocalMoveError, "target verify failed"):
                    transaction.execute([Plan(snapshot, target_root / source.name)])
            self.assertTrue(source.exists())
            self.assertFalse((target_root / source.name).exists())

    def test_replace_rejects_target_changed_since_preview_without_writes(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"new-version")
            target = target_root / "Movie.mkv"
            target.write_bytes(b"preview-version")
            adapter = LocalFilesystemAdapter(source_root)
            expected = LocalFilesystemAdapter.regular_file_identity(target)
            target.write_bytes(b"external-newer-version")

            with self.assertRaisesRegex(LocalMoveError, "预览后发生变化"):
                LocalMoveTransaction([source_root], [target_root]).execute([
                    Plan(
                        adapter.snapshot(source),
                        target,
                        action="replace",
                        expected_target_identity=expected,
                    )
                ])

            self.assertEqual(source.read_bytes(), b"new-version")
            self.assertEqual(target.read_bytes(), b"external-newer-version")
            self.assertEqual(list(target_root.glob(".*.mediaflux-replaced-*")), [])

    def test_source_swap_during_publish_is_detected_and_wrong_file_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"inspected")
            replacement = source_root / "replacement.mkv"
            replacement.write_bytes(b"external")
            displaced = source_root / "displaced.mkv"
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            transaction = LocalMoveTransaction([source_root], [target_root])
            real_publish = transaction._publish_no_replace

            def swap_then_publish(src, dst):
                src.replace(displaced)
                replacement.replace(src)
                return real_publish(src, dst)

            with patch.object(transaction, "_publish_no_replace", side_effect=swap_then_publish):
                with self.assertRaisesRegex(LocalMoveError, "最终移动前发生变化"):
                    transaction.execute([Plan(snapshot, target_root / source.name)])

            self.assertEqual(source.read_bytes(), b"external")
            self.assertEqual(displaced.read_bytes(), b"inspected")
            self.assertFalse((target_root / source.name).exists())

    def test_external_target_replacement_is_not_deleted_or_rolled_back(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"mediaflux")
            target = target_root / source.name
            displaced = target_root / "published-by-mediaflux.mkv"
            external = target_root / "external.mkv"
            external.write_bytes(b"external")
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            transaction = LocalMoveTransaction([source_root], [target_root])

            def replace_target_then_fail(*_args):
                target.replace(displaced)
                external.replace(target)
                raise LocalMoveError("injected verification failure")

            with patch.object(transaction, "_verify_target", side_effect=replace_target_then_fail):
                with self.assertRaisesRegex(LocalMoveError, "外部修改"):
                    transaction.execute([Plan(snapshot, target)])

            self.assertEqual(target.read_bytes(), b"external")
            self.assertEqual(displaced.read_bytes(), b"mediaflux")
            self.assertFalse(source.exists())

    def test_cross_filesystem_source_replacement_is_preserved_and_target_removed(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"inspected")
            replacement = source_root / "replacement.mkv"
            replacement.write_bytes(b"external")
            displaced = source_root / "displaced.mkv"
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            transaction = LocalMoveTransaction([source_root], [target_root])
            real_retire = transaction._retire_copied_source

            def swap_then_retire(adapter, item):
                source.replace(displaced)
                replacement.replace(source)
                return real_retire(adapter, item)

            with patch.object(
                LocalFilesystemAdapter, "same_filesystem", return_value=False
            ), patch.object(
                transaction, "_retire_copied_source", side_effect=swap_then_retire
            ):
                with self.assertRaisesRegex(LocalMoveError, "发生变化"):
                    transaction.execute([Plan(snapshot, target_root / source.name)])

            self.assertEqual(source.read_bytes(), b"external")
            self.assertEqual(displaced.read_bytes(), b"inspected")
            self.assertFalse((target_root / source.name).exists())

    def test_global_move_lock_rejects_overlapping_transaction(self):
        from app.modules import local_move_transaction as move_module

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root, target_root = root / "source", root / "library"
            source_root.mkdir(); target_root.mkdir()
            source = source_root / "Movie.mkv"
            source.write_bytes(b"movie")
            snapshot = LocalFilesystemAdapter(source_root).snapshot(source)
            self.assertTrue(move_module._LOCAL_MEDIA_MOVE_LOCK.acquire(blocking=False))
            try:
                with self.assertRaisesRegex(LocalMoveError, "正在进行"):
                    LocalMoveTransaction([source_root], [target_root]).execute([
                        Plan(snapshot, target_root / source.name)
                    ])
            finally:
                move_module._LOCAL_MEDIA_MOVE_LOCK.release()
            self.assertTrue(source.exists())
            self.assertFalse((target_root / source.name).exists())

    def test_symbolic_link_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); real_source = root / "real"; target = root / "target"; link = root / "link"
            real_source.mkdir(); target.mkdir()
            try:
                link.symlink_to(real_source, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不支持符号链接测试")
            if not link.is_symlink() and not (hasattr(link, "is_junction") and link.is_junction()):
                self.skipTest("当前环境未创建有效符号链接或连接点")
            with self.assertRaisesRegex(Exception, "符号链接"):
                LocalMoveTransaction([link], [target])


if __name__ == "__main__":
    unittest.main()
