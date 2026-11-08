"""离线运行目录迁移与旧/新锁隔离回归。"""
from __future__ import annotations

import io
import json
import os
import time
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.cli import main
from app.modules.backup import runtime_lifecycle_guard
from app.modules.process_lock import (
    LIFECYCLE_LOCK_NAME,
    CrossProcessLock,
    lock_path,
)
from app.modules.runtime_layout import migrate_runtime_layout, require_runtime_layout
from app.runtime_paths import RuntimeLayoutError, RuntimePaths


def make_paths(root: Path, *, split: bool = False) -> RuntimePaths:
    return RuntimePaths(
        program_dir=root / "program", data_dir=root,
        config_dir=root / "config" if split else root,
        cache_dir=root / "cache", log_dir=root / "logs",
        strm_dir=root / "strm", trash_dir=root / "trash",
    )


# Fresh -c interpreter: no tests/app import may precede this guard.
_LEGACY_HOLDER = r'''
import sys, os, socket, json, time, tempfile
from pathlib import Path
source, lock, ready, stop, temporary = map(Path, sys.argv[1:])
assert sys.dont_write_bytecode
assert not any(n == 'tests' or n.startswith(('tests.', 'app.')) or n == 'app' for n in sys.modules)
def audit(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg') and args[0].family != socket.AF_UNIX:
        raise AssertionError('holder denies INET')
    if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'):
        raise AssertionError('holder denies DNS')
    if event in ('subprocess.Popen', 'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec'):
        raise AssertionError('holder denies descendants')
sys.addaudithook(audit)
# Synthetic audit events prove the hook without attempting any network syscall.
checks = []
with socket.socket() as sock:
    for event, args in (
        ('socket.connect', (sock, ('203.0.113.10', 443))),
        ('socket.bind', (sock, ('127.0.0.1', 0))),
        ('socket.sendto', (sock, ('203.0.113.10', 53))),
        ('socket.sendmsg', (sock, ('203.0.113.10', 53))),
        ('socket.getaddrinfo', ('unlisted.invalid', 443)),
        ('socket.gethostbyname', ('unlisted.invalid',)),
        ('socket.gethostbyaddr', ('203.0.113.10',)),
        ('socket.getnameinfo', (('203.0.113.10', 443), 0)),
        ('subprocess.Popen', ('forbidden',)),
        ('os.fork', ()),
    ):
        try:
            sys.audit(event, *args)
        except AssertionError:
            checks.append(event)
        else:
            raise AssertionError('missing preimport denial: ' + event)
tempfile.tempdir = str(temporary)
os.environ['TMPDIR'] = str(temporary)
os.environ['AGENT_LLM_ENABLED'] = '0'
sys.path.insert(0, str(source))
import tests
from app import config, database
from app.modules.process_lock import CrossProcessLock
imports = {n: m.__file__ for n, m in sys.modules.items()
           if (n in ('tests', 'app') or n.startswith(('tests.', 'app.')))
           and getattr(m, '__file__', None)}
assert imports and all(Path(p).resolve().is_relative_to(source) for p in imports.values())
assert Path(tests.__file__).resolve() == source / 'tests/__init__.py'
assert config.ENV_FILE.is_relative_to(temporary)
assert database.resolve_db_path().is_relative_to(temporary)
with lock.open('a+b') as handle:
    CrossProcessLock._acquire_file_lock(handle, blocking=False)
    receipt = {'pid': os.getpid(), 'preimport_denied': checks, 'imports': imports,
               'db': str(database.resolve_db_path()), 'config': str(config.ENV_FILE),
               'temporary': str(temporary), 'bytecode_disabled': sys.dont_write_bytecode}
    pending = ready.with_suffix('.pending')
    pending.write_text(json.dumps(receipt))
    pending.replace(ready)
    deadline = time.monotonic() + 15
    while not stop.exists():
        if time.monotonic() >= deadline:
            raise AssertionError('holder stop handshake timed out')
        time.sleep(.01)
'''


def _reap_legacy_holder(process, stop):
    stop.touch()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class RuntimeLayoutTests(unittest.TestCase):
    def test_default_and_explicit_namespace_share_new_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.database.resolve_db_path", return_value=root / "mediaflux.db"):
                default = CrossProcessLock("shared")
                explicit = CrossProcessLock("shared", directory=root)
                self.assertEqual(default.path, root / "runtime/locks/.mediaflux-shared.lock")
                self.assertEqual(default.path, explicit.path)
                self.assertTrue(default.acquire(False))
                try:
                    self.assertFalse(explicit.acquire(False))
                finally:
                    default.release()

    def test_migrates_scoped_locks_and_stale_pid_only_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root, split=True)
            namespaces = [root, root / "db", root / "agent-guangya-fs-change",
                          root / "agent-guangya-rename", root / "agent-guangya-cleanup"]
            for base in namespaces:
                base.mkdir(exist_ok=True)
                (base / ".mediaflux-sample.lock").touch()
                (base / "keep.json").write_text("preserve", encoding="utf-8")
            (root / "mediaflux.pid").write_text("12345")
            (root / "mediaflux.db").write_bytes(b"database unchanged")
            self.assertEqual(migrate_runtime_layout(paths), len(namespaces))
            for base in namespaces:
                self.assertFalse((base / ".mediaflux-sample.lock").exists())
                self.assertTrue((base / "runtime/locks/.mediaflux-sample.lock").is_file())
                self.assertEqual((base / "keep.json").read_text(), "preserve")
            self.assertEqual((root / "mediaflux.db").read_bytes(), b"database unchanged")
            self.assertFalse((root / "mediaflux.pid").exists())
            self.assertFalse(paths.pid_file.exists())
            self.assertTrue((root / LIFECYCLE_LOCK_NAME).exists())
            self.assertFalse((root / "runtime/locks" / LIFECYCLE_LOCK_NAME).exists())
            self.assertEqual(migrate_runtime_layout(paths), 0)
            require_runtime_layout(paths)
            with runtime_lifecycle_guard(paths):
                pass

    def test_running_instance_blocks_migration_without_creating_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root)
            old = root / ".mediaflux-shared.lock"
            old.touch()
            lifecycle = CrossProcessLock("runtime-lifecycle", directory=root)
            self.assertEqual(lifecycle.path, root / LIFECYCLE_LOCK_NAME)
            self.assertTrue(lifecycle.acquire(False))
            try:
                with self.assertRaisesRegex(RuntimeLayoutError, "仍在运行"):
                    migrate_runtime_layout(paths)
                self.assertTrue(old.exists())
                self.assertFalse(paths.runtime_dir.exists())
            finally:
                lifecycle.release()

    def test_busy_legacy_or_new_lock_prevents_any_legacy_removal(self):
        for busy_target in (False, True):
            with self.subTest(busy_target=busy_target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = make_paths(root)
                first = root / ".mediaflux-a.lock"
                second = root / ".mediaflux-z.lock"
                first.touch()
                second.touch()
                busy = lock_path(root, second.name) if busy_target else second
                busy.parent.mkdir(parents=True, exist_ok=True)
                with busy.open("a+b") as handle:
                    CrossProcessLock._acquire_file_lock(handle, blocking=False)
                    with self.assertRaisesRegex(RuntimeLayoutError, "仍被占用"):
                        migrate_runtime_layout(paths)
                    self.assertTrue(first.exists())
                    self.assertTrue(second.exists())
                self.assertEqual(migrate_runtime_layout(paths), 2)

    def test_start_and_direct_lock_refuse_legacy_layout_release_local_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root)
            old = root / ".mediaflux-shared.lock"
            old.touch()
            with (
                self.assertRaisesRegex(RuntimeLayoutError, "runtime-migrate"),
                runtime_lifecycle_guard(paths),
            ):
                self.fail("startup must not proceed")
            lock = CrossProcessLock("shared", directory=root)
            with self.assertRaisesRegex(RuntimeLayoutError, "runtime-migrate"):
                lock.acquire(False)
            self.assertFalse(lock.path.exists())
            migrate_runtime_layout(paths)
            self.assertTrue(lock.acquire(False))
            lock.release()

    def test_partial_migration_retry_keeps_existing_new_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root)
            old = root / ".mediaflux-shared.lock"
            old.touch()
            target = lock_path(root, old.name)
            target.parent.mkdir(parents=True)
            target.touch()
            inode = target.stat().st_ino
            with (
                patch.object(Path, "unlink", side_effect=OSError("disk unavailable")),
                self.assertRaisesRegex(OSError, "disk unavailable"),
            ):
                migrate_runtime_layout(paths)
            with self.assertRaises(RuntimeLayoutError):
                require_runtime_layout(paths)
            self.assertEqual(migrate_runtime_layout(paths), 1)
            self.assertEqual(target.stat().st_ino, inode)

    def test_non_lock_content_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".mediaflux-shared.lock"
            old.write_bytes(b"unexpected content")
            with self.assertRaisesRegex(RuntimeLayoutError, "异常锁"):
                migrate_runtime_layout(make_paths(root))
            self.assertEqual(old.read_bytes(), b"unexpected content")

    def test_cli_reports_migration_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            with patch("app.cli.get_runtime_paths", return_value=paths):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["runtime-migrate"]), 0)
                self.assertIn("迁移完成", output.getvalue())
                with runtime_lifecycle_guard(paths), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(["runtime-migrate"]), 2)
                self.assertIn("先停服", error.getvalue())


    def test_old_process_holding_lifecycle_or_operation_lock_blocks_migration(self):
        for filename in (LIFECYCLE_LOCK_NAME, ".mediaflux-config-snapshot.lock"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = Path(__file__).resolve().parents[1]
                ready, stop = root / 'ready.json', root / 'stop'
                temporary = root / 'child-temp'
                temporary.mkdir()
                env = os.environ.copy()
                for key in ('PYTHONPATH', 'PYTHONHOME'):
                    env.pop(key, None)
                env.update(TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE='1', AGENT_LLM_ENABLED='0')
                with tempfile.TemporaryFile(mode='w+') as output:
                    process = subprocess.Popen(
                        [sys.executable, '-B', '-c', _LEGACY_HOLDER, str(source),
                         str(root / filename), str(ready), str(stop), str(temporary)],
                        cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT,
                    )
                    try:
                        deadline = time.monotonic() + 10
                        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        output.seek(0)
                        self.assertTrue(ready.exists(), output.read())
                        receipt = json.loads(ready.read_text())
                        self.assertEqual(receipt['pid'], process.pid)
                        self.assertTrue(receipt['bytecode_disabled'])
                        self.assertEqual(len(receipt['preimport_denied']), 10)
                        self.assertTrue(all(Path(p).is_relative_to(source) for p in receipt['imports'].values()))
                        with self.assertRaises(RuntimeLayoutError):
                            migrate_runtime_layout(make_paths(root))
                        self.assertTrue((root / filename).exists())
                        self.assertFalse((root / 'runtime/locks' / filename).exists())
                    finally:
                        _reap_legacy_holder(process, stop)
                    output.seek(0)
                    self.assertEqual(process.returncode, 0, output.read())
                migrate_runtime_layout(make_paths(root))
                require_runtime_layout(make_paths(root))

    def test_cli_pid_written_only_to_runtime_and_removed_even_on_server_error(self):
        import app.main

        environment = dict(os.environ)
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                paths = make_paths(Path(directory))
                def serve(*_args, paths=paths, fail=fail, **_kwargs):
                    self.assertEqual(paths.pid_file.read_text(), str(os.getpid()))
                    self.assertFalse((paths.data_dir / "mediaflux.pid").exists())
                    if fail:
                        raise OSError("server failed")

                with (
                    patch.dict(os.environ),
                    patch("app.cli.get_runtime_paths", return_value=paths),
                    patch("app.modules.backup.recover_pending_restore", return_value=False),
                    patch("app.modules.first_run.resolve_bind_host", return_value="127.0.0.1"),
                    patch.object(app.main, "app", SimpleNamespace(state=SimpleNamespace())),
                    patch("app.cli.uvicorn.run", side_effect=serve),
                    redirect_stderr(io.StringIO()),
                ):
                    if fail:
                        with self.assertRaisesRegex(OSError, "server failed"):
                            main(["start", "--port", "1258"])
                    else:
                        self.assertEqual(main(["start", "--port", "1258"]), 0)
                self.assertFalse(paths.pid_file.exists())
                self.assertEqual(dict(os.environ), environment)


    def test_cli_configures_explicit_data_dir_before_importing_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "selected"
            destination.mkdir()
            (destination / ".mediaflux-shared.lock").touch()
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("MEDIAFLUX_")}
            environment["MEDIAFLUX_DATA_DIR"] = str(root / "not-selected")
            script = """
import sys, socket
from pathlib import Path
assert not any(n == 'app' or n.startswith('app.') for n in sys.modules)
def audit(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg') and args[0].family != socket.AF_UNIX:
        raise AssertionError('exec inet denied')
    if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'):
        raise AssertionError('exec DNS denied')
sys.addaudithook(audit)
from app.cli import main
assert 'app.config' not in sys.modules
assert main(['runtime-migrate', '--data-dir', sys.argv[1]]) == 0
from app import config
assert config.PATHS.data_dir == Path(sys.argv[1])
assert config.PATHS.config_dir == Path(sys.argv[1]) / 'config'
"""
            result = subprocess.run([sys.executable, "-B", "-c", script, str(destination)],
                                    env=environment, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / ".mediaflux-shared.lock").exists())
            self.assertTrue((destination / "runtime/locks/.mediaflux-shared.lock").exists())
            self.assertFalse((root / "not-selected").exists())
