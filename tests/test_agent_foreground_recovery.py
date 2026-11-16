"""真实前台执行者退出后恢复后台目标，存活前台不受时钟推进影响。"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime,
)
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests import test_agent_effect_continuation as cases
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.support import isolated_test_database
from tests.test_agent_effect_admission import _offline_runtime, _OfflineClient

OWNER = 'webk:v1:' + 'c' * 64
REQUEST = 'ordinary-foreground-process'


def _lock_files():
    directory = db.resolve_db_path().parent / 'runtime' / 'locks'
    return {path.name for path in directory.rglob('*.lock')}


def _foreground_worker(db_path, binding, termination, ready, release):
    db.configure_database(db_path, test_mode=True)

    class HeldModel:
        async def stream(self, request, *, cancellation):
            ready.set()
            await asyncio.to_thread(release.wait)
            if termination == 'exit':
                os._exit(23)
            yield ModelEvent(ModelEventType.TEXT_DELTA, text='收到')
            yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')

    async def run():
        runtime = build_agent_kernel_runtime(model=HeldModel(), store=SQLiteKernelStore())
        runtime.session.continuation_binding = binding
        events = [event async for event in runtime.session.run(AgentInput(
            owner=OWNER, session_id=cases.SID, request_id=REQUEST, message='你好，请只回复收到',
        ))]
        assert events[-1].payload.get('status') == 'success', events[-1]

    with config.use_runtime_config_snapshot(binding.snapshot):
        asyncio.run(run())


class _ResumeScript(cases.FollowScript):
    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if step == 'inspect':
            message = {'tool_calls': [{'id': 'resumed-inspect', 'type': 'function',
                'function': {'name': 'guangya__inspect_after', 'arguments': '{}'}}]}
        else:
            assert any('admitted.mp4' in row.get('content', '')
                for row in body['messages'] if row['role'] == 'tool')
            message = {'content': '已重新读取，文件实际名称是 admitted.mp4。'}
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'tool_calls' if step == 'inspect' else 'stop'}]})


@pytest.mark.parametrize('termination', ['kill', 'exit', 'finish'])
def test_cold_scheduler_recovers_after_foreground_exit_without_replaying_write(agent_llm_enabled, tmp_path, termination):
    async def run():
        writes = []
        rename = cases._OfflineClient.rename

        def renamed(client, *args, **kwargs):
            writes.append((args, kwargs))
            return rename(client, *args, **kwargs)

        with patch.object(cases, 'OWNER', OWNER), patch.object(
            cases._OfflineClient, 'rename', autospec=True, side_effect=renamed,
        ):
            async with cases._ready_case(tmp_path) as (store, candidate, client, reads, stack, _users):
                assert len(writes) == 1
                binding = _capture_creation_binding()
                # spawn 子进程使用当前部署快照，不能取 tests 为新进程创建的另一个数据目录。
                binding = replace(binding, snapshot=replace(binding.snapshot,
                    file_values=dict(binding.snapshot.file_values), startup_overrides=dict(binding.snapshot.startup_overrides)))
                process_context = multiprocessing.get_context('spawn')
                ready, release = process_context.Event(), process_context.Event()
                baseline_files = _lock_files()
                worker = process_context.Process(target=_foreground_worker, args=(
                    str(db.resolve_db_path()), binding, termination, ready, release,
                ))
                worker.start()
                try:
                    assert await asyncio.to_thread(ready.wait, 15), f'foreground did not start: exit={worker.exitcode}'
                    foreground_files = _lock_files() - baseline_files
                    assert foreground_files
                    before = await store.load(owner=OWNER, session_id=cases.SID)
                    assert before.metadata['active_publication']['status'] == 'running'
                    assert before.metadata['active_publication']['request_id'] == REQUEST
                    assert before.metadata['effect_waits'][candidate['wait_id']]['final_result']['ok']
                    users = [row for row in before.conversation if row['role'] == 'user']
                    script = _ResumeScript(resume=True)

                    def factory(item, current_binding, current_store):
                        return SimpleNamespace(creation_binding=current_binding,
                            session=cases._session(current_store, script, client, reads, stack=stack))

                    def scheduler(advance=0):
                        instance = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(time.time() + advance))
                        instance._continuation_runtime_factory = factory
                        return instance

                    # 存活执行者一直持有发布权；不能靠 claim TTL 或墙钟猜测死亡。
                    for advance in (0, 60, 3600):
                        due = await SQLiteKernelStore(clock=lambda: time.time() + advance).due_continuations()
                        assert len(due) == 1 and due[0]['wait_id'] == candidate['wait_id']
                        assert await asyncio.to_thread(scheduler(advance)._run_due_continuation_once) == 0
                        assert reads == [] and script.requests == []
                        assert await store.load(owner=OWNER, session_id=cases.SID) == before

                    if termination == 'kill':
                        worker.kill()
                    else:
                        release.set()
                    await asyncio.to_thread(worker.join, 15)
                    assert not worker.is_alive()
                    if termination == 'kill':
                        assert worker.exitcode != 0
                    else:
                        assert worker.exitcode == (23 if termination == 'exit' else 0)
                    after = await SQLiteKernelStore().load(owner=OWNER, session_id=cases.SID)
                    assert after.metadata['active_publication']['status'] == ('finished' if termination == 'finish' else 'running')

                    # 两个冷调度者竞争，仍只能恢复一次原目标，不启动新用户回合或重新提交写入。
                    counts = await asyncio.gather(*(asyncio.to_thread(scheduler()._run_due_continuation_once) for _ in range(2)))
                    assert sorted(counts) == [0, 1]
                    assert reads == ['admitted.mp4'] and not script.steps and len(script.requests) == 2
                    assert len(writes) == 1
                    final = await SQLiteKernelStore().load(owner=OWNER, session_id=cases.SID)
                    assert final.metadata['effect_waits'][candidate['wait_id']]['continuation']['state'] == 'terminal'
                    assert final.metadata['active_publication']['status'] == 'finished'
                    assert [row for row in final.conversation if row['role'] == 'user'] == users
                    assert await SQLiteKernelStore().due_continuations() == []
                    assert not foreground_files.intersection(_lock_files())
                    with db.get_conn() as conn:
                        rows = conn.execute('SELECT status FROM organize_operation_jobs').fetchall()
                    assert [row['status'] for row in rows] == ['completed']
                finally:
                    if worker.is_alive():
                        worker.kill()
                    await asyncio.to_thread(worker.join, 10)
    asyncio.run(run())


def test_finished_foreground_turns_do_not_accumulate_execution_lock_files(tmp_path):
    class FinishedModel:
        async def stream(self, request, *, cancellation):
            yield ModelEvent(ModelEventType.TEXT_DELTA, text='收到')
            yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')

    async def run():
        with isolated_test_database(), _offline_runtime(tmp_path, _OfflineClient()):
            runtime = build_agent_kernel_runtime(model=FinishedModel())
            previous = None
            for index in range(8):
                events = [event async for event in runtime.session.run(AgentInput(
                    owner=OWNER, session_id='foreground-cleanup', request_id=f'ordinary-{index}',
                    message='你好，请只回复收到',
                ))]
                assert events[-1].payload.get('status') == 'success'
                current = _lock_files()
                if previous is not None:
                    assert current == previous
                previous = current
    asyncio.run(run())


def test_legacy_lifecycle_execution_uses_portable_lock_filename():
    from app.agent.kernel.session_guard import foreground_execution_active, foreground_execution_guard

    with isolated_test_database():
        lifecycle = "legacy:" + "a" * 64
        baseline = _lock_files()
        with foreground_execution_guard(OWNER, "legacy-foreground", lifecycle, 1) as acquired:
            assert acquired
            assert foreground_execution_active(OWNER, "legacy-foreground", lifecycle, 1)
            names = _lock_files() - baseline
            assert len(names) == 1 and all(":" not in name for name in names)
        assert not foreground_execution_active(OWNER, "legacy-foreground", lifecycle, 1)
        assert _lock_files() == baseline
