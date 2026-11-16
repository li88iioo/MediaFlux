"""Real config-lock ownership, contention and publication-scope controls."""
import multiprocessing
import os
import threading
from dataclasses import replace
from unittest.mock import patch

import pytest
from app import config
from app.agent.feature_gate import RuntimeFenceError
from app.modules import backup
from app.modules.process_lock import CrossProcessLock
from app.runtime_paths import RuntimePaths
from tests.test_setup_bootstrap import cold_run


def paths_at(root):
    return RuntimePaths(program_dir=root/'program', data_dir=root/'data', config_dir=root/'config', cache_dir=root/'cache', log_dir=root/'logs', strm_dir=root/'strm', trash_dir=root/'trash')


def _contender(paths, ready, start, attempted, entered):
    real = CrossProcessLock.acquire
    def acquire(lock, blocking=True):
        # Observe an actual rejected kernel-lock acquisition before blocking.
        assert not real(lock, blocking=False)
        attempted.set()
        return real(lock, blocking=blocking)
    ready.set()
    assert start.wait(5)
    with patch.object(CrossProcessLock, 'acquire', acquire):
        with backup.config_snapshot_guard(paths):
            entered.set()


@pytest.mark.parametrize('kind', ['thread', 'spawn'])
def test_other_owner_contends_until_outer_exit(tmp_path, kind):
    paths = paths_at(tmp_path)
    ctx = multiprocessing.get_context('spawn') if kind == 'spawn' else threading
    ready, start, attempted, entered = [ctx.Event() for _ in range(4)]
    errors = []
    def thread_target():
        try:
            _contender(paths, ready, start, attempted, entered)
        except BaseException as exc:
            errors.append(exc)
    worker = multiprocessing.get_context('spawn').Process(target=_contender, args=(paths, ready, start, attempted, entered)) if kind == 'spawn' else threading.Thread(target=thread_target, daemon=True)
    try:
        with backup.config_snapshot_guard(paths):
            worker.start()
            assert ready.wait(5)
            start.set()
            assert attempted.wait(5)
            assert not entered.is_set()
        assert entered.wait(5)
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
        if kind == 'spawn':
            assert not isinstance(worker, threading.Thread)
            assert worker.exitcode == 0
    finally:
        worker.join(1)
        if kind == 'spawn' and worker.is_alive():
            assert not isinstance(worker, threading.Thread)
            worker.kill()
            worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize('outer', ['runtime', 'direct'])
def test_same_path_mixed_nesting_and_baseexception_release(tmp_path, outer):
    paths = paths_at(tmp_path)
    alias = replace(paths, data_dir=paths.data_dir/'..'/'data')
    counts = []
    acquire, release = CrossProcessLock.acquire, CrossProcessLock.release
    def take(lock, blocking=True):
        counts.append('acquire')
        return acquire(lock, blocking=blocking)
    def drop(lock):
        counts.append('release')
        return release(lock)
    with patch.object(config, 'PATHS', paths), patch.object(CrossProcessLock, 'acquire', take), patch.object(CrossProcessLock, 'release', drop):
        with pytest.raises(KeyboardInterrupt):
            guard = config.runtime_config_read_guard() if outer == 'runtime' else backup.config_snapshot_guard(paths)
            with guard:
                with config.runtime_config_read_guard(), backup.config_snapshot_guard(alias):
                    with pytest.raises(ValueError):
                        with backup.config_snapshot_guard(paths):
                            raise ValueError('inner')
                    assert counts == ['acquire']
                assert counts == ['acquire']
                raise KeyboardInterrupt
        assert counts == ['acquire', 'release']
        with backup.config_snapshot_guard(paths):
            pass
        assert counts == ['acquire', 'release', 'acquire', 'release']


def test_failed_acquire_leaves_no_owner(tmp_path):
    paths = paths_at(tmp_path)
    for failure in (False, KeyboardInterrupt()):
        def reject(*args, **kwargs):
            if isinstance(failure, BaseException):
                raise failure
            return failure
        with patch.object(CrossProcessLock, 'acquire', reject):
            with pytest.raises((backup.BackupError, KeyboardInterrupt)):
                with backup.config_snapshot_guard(paths):
                    pytest.fail('unacquired owner admitted')
        with backup.config_snapshot_guard(paths):
            lock = CrossProcessLock('config-snapshot', directory=paths.data_dir)
            assert not lock.acquire(blocking=False)


def _holder(paths, held, release):
    with backup.config_snapshot_guard(paths):
        held.set()
        assert release.wait(8)


def test_different_path_never_borrows_runtime_owner(tmp_path):
    a, b = paths_at(tmp_path/'a'), paths_at(tmp_path/'b')
    ctx = multiprocessing.get_context('spawn')
    held, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_holder, args=(b, held, release))
    process.start()
    attempted = threading.Event()
    entered = threading.Event()
    errors = []
    original = CrossProcessLock.acquire
    def acquire(lock, blocking=True):
        if lock.path.resolve() == CrossProcessLock('config-snapshot', directory=b.data_dir).path.resolve():
            assert not original(lock, blocking=False)
            attempted.set()
        return original(lock, blocking=blocking)
    def nested():
        try:
            with patch.object(config, 'PATHS', a), config.runtime_config_read_guard():
                with patch.object(config, 'PATHS', b), config.runtime_config_read_guard():
                    entered.set()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=nested, daemon=True)
    try:
        assert held.wait(5)
        with patch.object(CrossProcessLock, 'acquire', acquire):
            worker.start()
            assert attempted.wait(5)
            assert not entered.is_set()
            release.set()
            assert entered.wait(5)
            worker.join(5)
        process.join(5)
        assert not errors and not worker.is_alive() and process.exitcode == 0
    finally:
        release.set()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(5)
        if worker.ident is not None:
            worker.join(5)
        assert not worker.is_alive()


def test_publication_scope_restores_across_paths_and_keeps_failure(tmp_path):
    a, b = paths_at(tmp_path/'a'), paths_at(tmp_path/'b')
    display = {'AGENT_STREAM_DISPLAY_ENABLED': '1'}
    with patch.object(config, 'PATHS', a):
        with pytest.raises(RuntimeFenceError, match='runtime_publication_failed'):
            with config.runtime_config_publication(display):
                outer = config._runtime_guard.scope
                with patch.object(config, 'PATHS', b), config.runtime_config_publication(display):
                    assert config._runtime_guard.scope is not outer
                    with patch.object(config, 'PATHS', a), config.runtime_config_read_guard():
                        assert config._runtime_guard.scope is outer
                        with pytest.raises(ValueError):
                            with config.runtime_config_publication(display):
                                raise ValueError('swallowed')
                assert config._runtime_guard.scope is outer
                with pytest.raises(RuntimeFenceError, match='runtime_publication_scope_changed'):
                    with config.runtime_config_publication({'AGENT_LLM_ENABLED': '1'}):
                        pass
        with config.runtime_config_publication(display):
            pass


def _fork_owner_scenario(tmp_path, unwind_first):
    from app.modules import backup
    from app.modules.process_lock import CrossProcessLock

    assert threading.enumerate() == [threading.main_thread()]
    paths = paths_at(tmp_path)
    ctx = multiprocessing.get_context('fork')
    attempted, entered = ctx.Event(), ctx.Event()
    guard = backup.config_snapshot_guard(paths)
    # 保留真实父锁的引用到子进程 os._exit：仅测 owner/LOCK_UN，不让
    # 继承上下文展开时的文件析构混入此场景。未替换任何实际锁操作。
    retained = []
    real_acquire = CrossProcessLock.acquire
    def retain(lock, blocking=True):
        retained.append(lock)
        return real_acquire(lock, blocking=blocking)
    with patch.object(CrossProcessLock, 'acquire', retain):
        guard.__enter__()
    pid = os.fork()
    if pid == 0:
        try:
            if unwind_first:
                guard.__exit__(None, None, None)
            real = CrossProcessLock.acquire
            def acquire(lock, blocking=True):
                assert not real(lock, blocking=False)
                attempted.set()
                return real(lock, blocking=blocking)
            with patch.object(CrossProcessLock, 'acquire', acquire):
                with backup.config_snapshot_guard(paths):
                    entered.set()
            if not unwind_first:
                guard.__exit__(None, None, None)
            os._exit(0)
        except BaseException:
            os._exit(1)
    try:
        assert attempted.wait(5)
        assert not entered.is_set()
    finally:
        guard.__exit__(None, None, None)
        if not entered.wait(5):
            os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert entered.is_set()
    return True


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork identity control')
@pytest.mark.parametrize('unwind_first', [True, False])
def test_fork_child_cannot_reuse_or_unlock_parent_owner(tmp_path, unwind_first):
    assert cold_run(tmp_path, _fork_owner_scenario, tmp_path, unwind_first) == {'result': True}
