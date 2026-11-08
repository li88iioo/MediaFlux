"""Agent 持久队列准入：真实 SQLite、签名计划、Kernel 和跨进程回执。

仅 Provider 使用离线夹具；不 mock 入队结果、会话仓储或 worker dispatch。
暂停 dispatcher 是为了精确控制提交/验证/执行边界，不替代任何业务成功判定。
可单独运行：python -B -m unittest tests.test_agent_effect_admission -v
"""
from __future__ import annotations
from app.clients.guangya import GuangYaClient
from typing import cast

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from app.modules.process_lock import CrossProcessLock
from app.modules.scraper import TMDBScraper
import unittest
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import database as db
from app.agent import completion_observers as observers
from app.agent import effect_completion as effects
from app.modules import directory_scrape as scrape_service
from app.agent.errors import ToolPipelineError
from app.agent.kernel import completion_receipts as receipts
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import (
    CancellationToken,
    StalePublicationError,
    StateUpdate,
)
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolContext, ToolResult
from app.modules import guangya_fs_change as fs
from app.modules import organize_tasks
from app.modules.directory_scrape import DirectoryScrapeService, DirectoryScrapeStore
from app.modules.organize_rules import OrganizeRules
from app.repositories import organize_operation_jobs as jobs
from tests.support import initialized_web_runtime
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
from tests.support import _DirectoryScrapeTMDB

_OWNER = "effect-admission-owner"
_SESSION = "effect-admission-session"
_FS_TOOL = "cloud.change"
_DOMAINS = {
    "fs": (_FS_TOOL, "agent_guangya_fs_change"),
    "scrape": ("cloud.scrape", "agent_directory_scrape"),
}


class _OfflineClient(FakeGatewayClient):
    """补上清理预览所需的 guarded-empty Provider 能力，仍然只操作内存树。"""

    supports_guarded_empty_directory_delete = True

    def delete_empty_directory(self, file_id, **_guards):
        if self.list_dir(file_id):
            raise AssertionError("非空目录不能按空目录删除")
        return self.delete([file_id])


def _deny_network(*_args, **_kwargs):
    raise AssertionError("effect admission 回归禁止外联")


class _DomainActionStack(ExitStack):
    pass


@contextmanager
def _offline_runtime(root: Path, client: FakeGatewayClient):
    with _DomainActionStack() as stack:
        for method in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, method, _deny_network))
        stack.enter_context(patch.object(socket, "create_connection", _deny_network))
        stack.enter_context(patch.object(socket, "getaddrinfo", _deny_network))
        for module, attribute, folder in (
            (fs, "_directory", "fs-plans"),
        ):
            stack.enter_context(patch.object(module, attribute, lambda folder=folder: root / folder))
        stack.enter_context(patch("app.agent.kernel.ports.sdk_effects.GuangYaClient", lambda: client))
        stack.enter_context(patch.object(
            fs, "execute_fs_change_plan",
            partial(fs.execute_fs_change_plan, client_factory=lambda: cast(GuangYaClient, client)),
        ))
        yield stack


@contextmanager
def _paused_manager():
    manager = organize_tasks.OrganizeTaskManager()
    # 只控制调度：真正的 start_durable_operation / repo 事务照常运行。
    worker_lock = threading.Lock()
    manager._lock = cast(CrossProcessLock, worker_lock)
    manager._lock.acquire()
    try:
        with patch.object(manager, "_ensure_operation_dispatcher", lambda: None), patch.object(
            organize_tasks, "get_organize_manager", lambda: manager,
        ):
            yield manager
    finally:
        manager._lock.release()


def _read_state(store, *, owner=_OWNER, session_id=_SESSION, conn=None):
    if conn is not None:
        return store._load_row(conn, owner, session_id)
    with db.get_conn() as reader:
        return store._load_row(reader, owner, session_id)


def _seed(state, plan_id):
    record = state.metadata["effect_waits"][plan_id]
    return json.loads(receipts._completion_cipher().decrypt(record["sealed"].encode()))


def _confirmed_binding(scope):
    return {
        "generation": scope.lease.generation,
        "turn_id": scope.lease.turn_id,
        "plan_id": scope.plan_id,
    }


async def _confirmed_scope(store, *, owner=_OWNER, session_id=_SESSION, plan_id=None):
    lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id="admit-request")
    scope = receipts.EffectCompletionScope(
        store=store, lease=lease, plan_id=plan_id or uuid4().hex, channel="web",
    )
    from app.agent.kernel.state_types import _ConfirmationClaim
    await store.commit(lease, updates=(StateUpdate("pending_effect_plan_id", scope.plan_id),))
    await store.commit(lease, updates=(_ConfirmationClaim("", _confirmed_binding(scope), request_id=lease.request_id),))
    return scope


def _signed_fs_plan(client, *, owner=_OWNER, new_name="admitted.mp4"):
    plan = fs.build_fs_change_plan(client, owner=owner, trigger_strm=False,
        operations=[{"op": "rename", "file_id": "rename", "new_name": new_name}])
    return fs.confirm_fs_change_plan(plan["plan_id"], owner=owner, expected_fingerprint=plan["fingerprint"])


def _enqueue_arguments(plan, *, owner=_OWNER, dedupe_key=None):
    return {
        "job_kind": "agent_guangya_fs_change", "owner": owner,
        "operation": "光鸭文件变更", "reference": "已确认冻结计划",
        "payload": {
            "version": 1, "plan_id": plan["plan_id"], "plan_fingerprint": plan["fingerprint"],
            "owner_digest": plan["owner_digest"], "credential_generation": plan["credential_generation"],
        },
        "dedupe_key": dedupe_key or f"effect-admission:{plan['plan_id']}",
    }


def _admission(scope, *, tool=_FS_TOOL):
    return partial(effects.admit_organize_operation, tool=tool, scope=scope)


def _spec(domain="fs", *, verifier=None):
    from app.agent.kernel.ports.sdk_effects import cloud_change_tool
    assert domain == "fs"
    tool = cloud_change_tool()
    if verifier is not None:
        tool = replace(tool, verify=lambda arguments, result, context: verifier(arguments, result))
    return tool


def _domain_arguments(domain, client, context, stack, *, new_name="admitted.mp4"):
    if domain == "fs":
        return {"operations": [{"op": "rename", "file_id": "rename", "new_name": new_name}], "trigger_strm": False}
    # 真正的 DirectoryScrapeService/preview；只有 TMDB 数据和云盘来自离线 Provider。
    client.directories["source"] = [replace(
        client.file_info("rename"), name="Iron.Man.2008.1080p.mkv", extension="mkv", size=2 * 1024**3,
    )]
    rules = OrganizeRules(
        target_dir_id="target", small_file_mb=0, region_split=False, year_split=False,
        clean_empty=False, link_strm=False, notify_enabled=False,
    )
    service = DirectoryScrapeService(
        client=client, scraper=cast(TMDBScraper, _DirectoryScrapeTMDB()), store=DirectoryScrapeStore(), rules_loader=lambda: rules,
    )
    stack.enter_context(patch.object(scrape_service, "get_directory_scrape_service", lambda: service))
    stack.callback(service.close)
    return {"file_id": "source", "media_type": "movie", "tmdb_id": "1726"}


async def _kernel_plan(domain, store, client, stack, *, owner=_OWNER, session_id=_SESSION,
                       wait=False, verifier=None, request_id="confirm-request", new_name="admitted.mp4",
                       effect_store=None):
    lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id=request_id)
    arguments = _domain_arguments(domain, client, ToolContext(
        owner=owner, session_id=session_id, request_id=request_id,
    ), stack, new_name=new_name)
    events = []

    async def progress(event):
        events.append(deepcopy(event))

    context = ToolCallContext(
        owner=owner, session_id=session_id, request_id=lease.request_id, turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=progress,
        wait_for_completion=wait, channel="web",
    )
    if domain == "scrape":
        from app.agent.kernel.ports.sdk_effects import directory_scrape_tool
        tool = directory_scrape_tool()
        if verifier is not None:
            original_verify = tool.verify
            assert original_verify is not None
            async def verify(arguments, value, context):
                checked = verifier(arguments, value)
                return await original_verify(arguments, checked, context)
            tool = replace(tool, verify=verify)
    else:
        tool = _spec(domain, verifier=verifier)
    catalog = ToolCatalog([tool])
    pipeline = ToolPipeline(catalog=catalog, state_store=store, effect_store=effect_store)
    prepared = await pipeline.execute(_DOMAINS[domain][0], arguments, context=context)
    assert prepared.effect_plan is not None
    return pipeline, context, prepared.effect_plan, events


def _run_real_worker(row):
    manager = organize_tasks.OrganizeTaskManager()
    worker_lock = threading.Lock()
    manager._lock = cast(CrossProcessLock, worker_lock)
    manager._lock.acquire()
    manager._worker = cast(threading.Thread, object())
    manager._task = {
        "id": row["job_id"], "status": "running", "durable": True,
        "owner_digest": row["owner_digest"], "stats": {},
    }
    # 原 worker 的签名验证、lease/cancel guard、领域 dispatch、逐项写后核验全部执行。
    manager._run_durable_operation(dict(row))
    assert manager._worker is None
    assert not worker_lock.locked()
    return manager


class AgentEffectAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db_path = self.stack.enter_context(initialized_web_runtime()).database_path
        self.root = self.db_path.parent
        self.client = _OfflineClient()
        self.runtime = self.stack.enter_context(_offline_runtime(self.root, self.client))
        self.manager = self.stack.enter_context(_paused_manager())
        self.store = SQLiteKernelStore()

    def _job_count(self):
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0]

    def _assert_unbound(self, plan, *, owner=_OWNER):
        stored = fs.load_fs_change_plan(plan["plan_id"], owner=owner, expected_fingerprint=plan["fingerprint"])
        self.assertEqual(stored["status"], "confirmed")
        self.assertFalse(stored.get("job_id"))
        self.assertFalse(stored.get("queue_until_epoch"))

    def _assert_seed(self, state, plan_id, row, *, owner=_OWNER, session_id=_SESSION,
                     tool=_FS_TOOL, lease=None):
        self.assertIn(plan_id, state.metadata.get("effect_waits", {}))
        record = state.metadata["effect_waits"][plan_id]
        seed = _seed(state, plan_id)
        reference = jobs.organize_operation_public_ref(row["job_id"])
        self.assertEqual(seed["owner"], owner)
        self.assertEqual(seed["session_id"], session_id)
        self.assertEqual(seed["plan_id"], plan_id)
        self.assertEqual(seed["tool"], tool)
        self.assertEqual(seed["channel"], "web")
        self.assertEqual(seed["tracker"], {"kind": "guangya_operation", "value": {"operation_ref": reference}})
        self.assertEqual(seed["result"]["data"]["operation_ref"], reference)
        if lease is not None:
            self.assertEqual((seed["turn_id"], seed["request_id"]), (lease.turn_id, lease.request_id))
        self.assertEqual(record["state"], "pending")
        self.assertIs(record["delivered"], False)
        self.assertGreater(record["expires_at"], record["created_at"])
        self.assertEqual(state.metadata["effect_next_poll_at"], record["next_poll_at"])
        return seed

    async def test_enqueue_commits_job_and_seed_in_one_sqlite_transaction_before_fs_bind(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        order, statements = [], []
        real_bind = jobs._bind_fs_change_plan_to_job

        @contextmanager
        def traced_connection():
            with db.get_conn() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        def admit(conn, row, replayed):
            order.append("admission")
            assert isinstance(conn, sqlite3.Connection)
            assert isinstance(row, sqlite3.Row)
            self.assertTrue(conn.in_transaction)
            self.assertIs(replayed, False)
            self.assertEqual(conn.execute(
                "SELECT job_id FROM organize_operation_jobs WHERE job_id=?", (row["job_id"],),
            ).fetchone()[0], row["job_id"])
            self._assert_unbound(plan)
            _admission(scope)(conn, row, replayed)
            self._assert_seed(_read_state(self.store, conn=conn), scope.plan_id, row, lease=scope.lease)
            # 另一条真实连接在 commit 前既看不到 job，也看不到 seed。
            with sqlite3.connect(str(self.db_path)) as observer:
                observer.row_factory = sqlite3.Row
                self.assertEqual(observer.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0], 0)
                self.assertFalse(_read_state(self.store, conn=observer).metadata.get("effect_waits"))

        def bind(row):
            order.append("fs-bind")
            return real_bind(row)

        with patch.object(jobs, "get_conn", traced_connection), patch.object(jobs, "_bind_fs_change_plan_to_job", bind):
            row, replayed = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=admit)
        self.assertIs(replayed, False)
        self.assertEqual(order, ["admission", "fs-bind"])
        self.assertEqual(sum(sql.upper().startswith("BEGIN") for sql in statements), 1)
        self.assertEqual(sum(sql.upper() == "COMMIT" for sql in statements), 1)
        self.assertNotIn("ROLLBACK", statements)
        self.assertEqual(self._job_count(), 1)
        self.assertTrue(jobs.verify_organize_operation_payload(row))
        self._assert_seed(_read_state(self.store), scope.plan_id, row, lease=scope.lease)
        bound = fs.load_fs_change_plan(plan["plan_id"], owner=_OWNER, expected_fingerprint=plan["fingerprint"])
        self.assertEqual((bound["status"], bound["job_id"]), ("queued", row["job_id"]))

    async def test_callback_exception_after_state_write_rolls_back_job_seed_and_never_binds_fs(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        before = _read_state(self.store)
        failure = RuntimeError("seed persistence fault after real state UPDATE")

        def fail(conn, row, replayed):
            _admission(scope)(conn, row, replayed)
            self._assert_seed(_read_state(self.store, conn=conn), scope.plan_id, row)
            raise failure

        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError) as raised:
                jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=fail)
        self.assertIs(raised.exception.__cause__, failure)
        bind.assert_not_called()
        self.assertEqual(self._job_count(), 0)
        self.assertEqual(_read_state(self.store), before)
        self._assert_unbound(plan)

    async def test_seed_sealing_failure_rolls_back_and_manager_propagates_admission_error(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        before = _read_state(self.store)
        failure = ValueError("cannot seal effect seed")
        arguments = _enqueue_arguments(plan)
        with patch.object(receipts, "_completion_cipher", side_effect=failure), patch.object(
            jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job,
        ) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError) as raised:
                self.manager.start_durable_operation(
                    arguments["operation"], arguments["reference"],
                    job_kind=arguments["job_kind"], owner=arguments["owner"],
                    payload=arguments["payload"], dedupe_key=arguments["dedupe_key"],
                    on_admitted=_admission(scope),
                )
        self.assertIs(raised.exception.__cause__, failure)
        bind.assert_not_called()
        self.assertEqual(self._job_count(), 0)
        self.assertEqual(_read_state(self.store), before)
        self._assert_unbound(plan)

    async def test_actual_state_capacity_failure_rolls_back_job_seed_and_fs_binding(self):
        self.store = SQLiteKernelStore(max_state_bytes=16 * 1024)
        scope = await _confirmed_scope(self.store)
        with db.get_conn() as conn:
            size = conn.execute("SELECT length(CAST(state_json AS BLOB)) FROM agent_kernel_sessions").fetchone()[0]
        padding = "x" * (self.store.max_state_bytes - size - 128)
        await self.store.update_effect_state(owner=_OWNER, session_id=_SESSION,
                                            change=lambda state: state.metadata.update(admission_padding=padding))
        before = _read_state(self.store)
        self.assertFalse(before.execution_capacity)
        plan = _signed_fs_plan(self.client)
        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError) as raised:
                jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        assert isinstance(raised.exception.__cause__, ValueError)
        self.assertIn("持久化上限", str(raised.exception.__cause__))
        bind.assert_not_called()
        self.assertEqual(self._job_count(), 0)
        self.assertEqual(_read_state(self.store), before)
        self._assert_unbound(plan)

    async def _check_kernel_domains(self, *, wait):
        for domain, (tool, kind, *_handlers) in _DOMAINS.items():
            with self.subTest(domain=domain, wait=wait):
                # 每类独立 Provider/owner，避免跨领域计划和队列容量干扰。
                client = _OfflineClient()
                owner, session_id = f"{_OWNER}-{domain}", f"{_SESSION}-{domain}"
                with _offline_runtime(self.root / domain, client) as runtime:
                    observations = []
                    verified = []
                    real_enqueue = organize_tasks.enqueue_organize_operation_job
                    plan_id = None

                    def observe_commit(**kwargs):
                        self.assertTrue(callable(kwargs.get("on_admitted")))
                        row, replayed = real_enqueue(**kwargs)
                        self.assertFalse(verified, "commit 观测必须在 verify 之前")
                        self.assertFalse(replayed)
                        self.assertEqual(row["job_kind"], kind)
                        state = _read_state(self.store, owner=owner, session_id=session_id)
                        self._assert_seed(state, plan_id, row, owner=owner, session_id=session_id,
                                          tool=tool, lease=context.lease)
                        observations.append(dict(row))
                        return row, replayed

                    def verifier(_arguments, result):
                        self.assertEqual(len(observations), 1)
                        verified.append(result.data["operation_ref"])
                        return result

                    pipeline, context, plan, events = await _kernel_plan(
                        domain, self.store, client, runtime, owner=owner, session_id=session_id,
                        wait=wait, verifier=verifier,
                    )
                    plan_id = plan.plan_id
                    # 仍调用真实 waiter，只把前台等待期限置零；不伪造终态。
                    real_waiter = effects.wait_for_effect_completion
                    with patch.object(effects, "wait_for_effect_completion", partial(real_waiter, timeout_seconds=0)), patch.object(organize_tasks, "enqueue_organize_operation_job", observe_commit):
                        result = await pipeline.execute_confirmed(plan_id, context=context)
                    self.assertEqual(len(observations), 1)
                    row = jobs.get_organize_operation_job(observations[0]["job_id"])
                    assert row is not None
                    self.assertEqual(row["status"], "pending")
                    self.assertTrue(jobs.verify_organize_operation_payload(row))
                    self.assertEqual(verified, [jobs.organize_operation_public_ref(row["job_id"])])
                    self.assertTrue(result.outcome.public_content["ok"])
                    self.assertNotEqual(result.outcome.public_content["status"], "completed")
                    self._assert_seed(_read_state(self.store, owner=owner, session_id=session_id),
                                      plan_id, row, owner=owner, session_id=session_id, tool=tool)
                    self.assertEqual(any(event.get("phase") == "background_job" for event in events), wait and domain == "scrape")

    async def test_active_kernel_domains_register_at_commit_without_foreground_wait(self):
        await self._check_kernel_domains(wait=False)

    async def test_active_kernel_domains_have_seed_before_verifier_and_real_waiter(self):
        await self._check_kernel_domains(wait=True)

    async def test_memory_kernel_rejects_durable_admission_and_cannot_fallback_to_accepted(self):
        memory = InMemorySessionStateStore()
        pipeline, context, plan, _ = await _kernel_plan("fs", memory, self.client, self.runtime)
        frozen_id = plan.metadata["fs_change_plan_id"]
        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(ToolPipelineError) as raised:
                await pipeline.execute_confirmed(plan.plan_id, context=context)
        causes = []
        exc = raised.exception
        while exc is not None:
            causes.append(exc)
            exc = exc.__cause__
        self.assertTrue(any(isinstance(exc, jobs.OrganizeOperationAdmissionError) for exc in causes), causes)
        self.assertTrue(any(type(exc) is RuntimeError for exc in causes), causes)
        bind.assert_not_called()
        self.assertEqual(self._job_count(), 0)
        self.assertFalse((await memory.load(owner=_OWNER, session_id=_SESSION)).metadata.get("effect_waits"))
        stored = fs.load_fs_change_plan(frozen_id, owner=_OWNER)
        self.assertEqual(stored["status"], "confirmed")
        self.assertFalse(stored.get("job_id"))

    async def test_non_agent_public_service_can_enqueue_without_effect_scope(self):
        plan = _signed_fs_plan(self.client)
        self.assertEqual(self._job_count(), 0)
        result = fs.submit_fs_change_plan(plan["plan_id"], owner=_OWNER, expected_fingerprint=plan["fingerprint"])
        self.assertEqual(self._job_count(), 1)
        row = jobs.get_organize_operation_job(result["task_id"])
        assert row is not None
        self.assertEqual(row["status"], "pending")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_kernel_sessions").fetchone()[0], 0)


    async def test_stale_generation_after_new_turn_rolls_back_and_keeps_new_pending(self):
        scope = await _confirmed_scope(self.store)
        newer, _ = await self.store.begin_turn(owner=_OWNER, session_id=_SESSION, request_id="new-turn")
        await self.store.commit(newer, updates=(StateUpdate("pending_effect_plan_id", "new-pending"),))
        before = _read_state(self.store)
        plan = _signed_fs_plan(self.client)
        with self.assertRaises(jobs.OrganizeOperationAdmissionError) as raised:
            jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        assert isinstance(raised.exception.__cause__, StalePublicationError)
        self.assertEqual(self._job_count(), 0)
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(before.pending_effect_plan_id, "new-pending")
        self._assert_unbound(plan)

    async def test_confirmed_publication_must_match_exact_generation_turn_and_plan(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        invalid_scopes = (
            replace(scope, lease=replace(scope.lease, generation=scope.lease.generation + 1)),
            replace(scope, lease=replace(scope.lease, turn_id="another-confirm-turn")),
            replace(scope, plan_id="another-confirm-plan"),
        )
        for invalid in invalid_scopes:
            with self.subTest(scope=invalid):
                before = _read_state(self.store)
                with self.assertRaises(jobs.OrganizeOperationAdmissionError) as raised:
                    jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(invalid))
                assert isinstance(raised.exception.__cause__, StalePublicationError)
                self.assertEqual(self._job_count(), 0)
                self.assertEqual(_read_state(self.store), before)
                self._assert_unbound(plan)

    async def test_missing_confirmed_publication_does_not_admit_on_generation_match(self):
        lease, _ = await self.store.begin_turn(owner=_OWNER, session_id=_SESSION, request_id="ordinary-read")
        scope = receipts.EffectCompletionScope(self.store, lease, uuid4().hex, "web")
        plan = _signed_fs_plan(self.client)
        before = _read_state(self.store)
        with self.assertRaises(jobs.OrganizeOperationAdmissionError):
            jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        self.assertEqual(self._job_count(), 0)
        self.assertEqual(_read_state(self.store), before)
        self._assert_unbound(plan)

    async def test_purged_session_is_not_resurrected_by_late_admission(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        self.assertTrue(await self.store.delete_session(owner=_OWNER, session_id=_SESSION))
        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError):
                jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        bind.assert_not_called()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_kernel_sessions").fetchone()[0], 0)
        self.assertEqual(self._job_count(), 0)
        self._assert_unbound(plan)
        new_scope = await _confirmed_scope(self.store, plan_id=scope.plan_id)
        self.assertGreater(new_scope.lease.generation, scope.lease.generation)
        before = _read_state(self.store)
        with self.assertRaises(jobs.OrganizeOperationAdmissionError):
            jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(self._job_count(), 0)

    async def test_same_scope_same_dedupe_replays_exact_job_and_one_seed(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        arguments = _enqueue_arguments(plan)
        first, replayed = jobs.enqueue_organize_operation_job(**arguments, on_admitted=_admission(scope))
        self.assertFalse(replayed)
        before = _read_state(self.store).metadata["effect_waits"][scope.plan_id]
        seen = []

        def admit(conn, row, replayed):
            seen.append((row["job_id"], replayed, conn.in_transaction))
            _admission(scope)(conn, row, replayed)

        second, replayed = jobs.enqueue_organize_operation_job(**arguments, job_id=uuid4().hex, on_admitted=admit)
        self.assertTrue(replayed)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(seen, [(first["job_id"], True, True)])
        self.assertEqual(self._job_count(), 1)
        state = _read_state(self.store)
        self.assertEqual(list(state.metadata["effect_waits"]), [scope.plan_id])
        self._assert_seed(state, scope.plan_id, first, lease=scope.lease)
        after = state.metadata["effect_waits"][scope.plan_id]
        self.assertEqual({key: value for key, value in before.items() if key != "sealed"},
                         {key: value for key, value in after.items() if key != "sealed"})

    async def test_same_confirmation_plan_cannot_point_at_second_dedupe_job(self):
        scope = await _confirmed_scope(self.store)
        original = _signed_fs_plan(self.client)
        first, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(original), on_admitted=_admission(scope))
        other_plan = _signed_fs_plan(self.client, new_name="other.mp4")
        before = _read_state(self.store)
        with self.assertRaises(jobs.OrganizeOperationAdmissionError):
            jobs.enqueue_organize_operation_job(**_enqueue_arguments(other_plan), on_admitted=_admission(scope))
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(_read_state(self.store), before)
        get_organize_operation_job_result_3 = jobs.get_organize_operation_job(first["job_id"])
        assert get_organize_operation_job_result_3 is not None
        self.assertEqual(get_organize_operation_job_result_3["status"], "pending")
        self._assert_unbound(other_plan)

    async def test_same_dedupe_cannot_be_claimed_by_different_confirmed_plan(self):
        original_scope = await _confirmed_scope(self.store)
        original = _signed_fs_plan(self.client)
        first, _ = jobs.enqueue_organize_operation_job(
            **_enqueue_arguments(original, dedupe_key="shared-dedupe"), on_admitted=_admission(original_scope),
        )
        new_scope = await _confirmed_scope(self.store)
        new_plan = _signed_fs_plan(self.client, new_name="other.mp4")
        before = _read_state(self.store)
        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError):
                jobs.enqueue_organize_operation_job(
                    **_enqueue_arguments(new_plan, dedupe_key="shared-dedupe"), on_admitted=_admission(new_scope),
                )
        bind.assert_not_called()
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(_seed(before, original_scope.plan_id)["tracker"]["value"]["operation_ref"],
                         jobs.organize_operation_public_ref(first["job_id"]))
        self._assert_unbound(new_plan)

    async def test_same_owner_other_session_cannot_claim_existing_dedupe(self):
        original_scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        first, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(original_scope))
        other = await _confirmed_scope(self.store, session_id="another-session", plan_id=original_scope.plan_id)
        old_before = _read_state(self.store)
        other_before = _read_state(self.store, session_id="another-session")
        with self.assertRaises(jobs.OrganizeOperationAdmissionError):
            jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(other))
        self.assertEqual(self._job_count(), 1)
        get_organize_operation_job_result_4 = jobs.get_organize_operation_job(first["job_id"])
        assert get_organize_operation_job_result_4 is not None
        self.assertEqual(get_organize_operation_job_result_4["status"], "pending")
        self.assertEqual(_read_state(self.store), old_before)
        self.assertEqual(_read_state(self.store, session_id="another-session"), other_before)

    async def test_foreign_owner_row_is_rejected_even_with_matching_confirmation_binding(self):
        plan = _signed_fs_plan(self.client)
        foreign_row, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan))
        scope = await _confirmed_scope(self.store, owner="other-owner")
        before = _read_state(self.store, owner="other-owner")
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(StalePublicationError):
                _admission(scope)(conn, foreign_row, False)
        self.assertEqual(_read_state(self.store, owner="other-owner"), before)
        self.assertEqual(self._job_count(), 1)

    async def test_legacy_job_without_seed_is_not_claimed_or_registered_on_replay(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        legacy, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan))
        before = _read_state(self.store)
        with patch.object(jobs, "_bind_fs_change_plan_to_job", wraps=jobs._bind_fs_change_plan_to_job) as bind:
            with self.assertRaises(jobs.OrganizeOperationAdmissionError):
                jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        bind.assert_not_called()
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(self._job_count(), 1)
        get_organize_operation_job_result_5 = jobs.get_organize_operation_job(legacy["job_id"])
        assert get_organize_operation_job_result_5 is not None
        self.assertEqual(get_organize_operation_job_result_5["status"], "pending")

    async def test_replay_seed_identity_cannot_change_request_channel_or_tool(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        cases = (
            (replace(scope, lease=replace(scope.lease, request_id="other-request")), _FS_TOOL),
            (replace(scope, channel="telegram"), _FS_TOOL),
            (scope, "other.write"),
        )
        for altered, tool in cases:
            with self.subTest(channel=altered.channel, request=altered.lease.request_id, tool=tool):
                before = _read_state(self.store)
                with self.assertRaises(jobs.OrganizeOperationAdmissionError):
                    jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(altered, tool=tool))
                self.assertEqual(self._job_count(), 1)
                self.assertEqual(_read_state(self.store), before)

    async def test_guangya_operation_verify_refuses_to_backfill_missing_seed(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        legacy, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan))
        reference = jobs.organize_operation_public_ref(legacy["job_id"])
        result = ToolResult(True, "accepted", "已提交", data={"operation_ref": reference})
        tracker = observers._completion_tracker(result)
        assert tracker is not None
        self.assertEqual(tracker.kind, "guangya_operation")
        before = _read_state(self.store)
        with self.assertRaises(StalePublicationError):
            await effects._register_effect_wait(result, tracker, _FS_TOOL, scope)
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(self._job_count(), 1)

    async def test_guangya_operation_verify_consumes_existing_identity_without_resetting_record(self):
        scope = await _confirmed_scope(self.store)
        plan = _signed_fs_plan(self.client)
        row, _ = jobs.enqueue_organize_operation_job(**_enqueue_arguments(plan), on_admitted=_admission(scope))
        before = _read_state(self.store).metadata["effect_waits"][scope.plan_id]
        result = ToolResult(True, "accepted", "领域细节", data={
            "operation_ref": jobs.organize_operation_public_ref(row["job_id"]), "rename_count": 1,
        })
        tracker = observers._completion_tracker(result)
        assert tracker is not None
        await effects._register_effect_wait(result, tracker, _FS_TOOL, scope)
        state = _read_state(self.store)
        after = state.metadata["effect_waits"][scope.plan_id]
        self.assertEqual({key: value for key, value in before.items() if key != "sealed"},
                         {key: value for key, value in after.items() if key != "sealed"})
        self.assertEqual(_seed(state, scope.plan_id)["result"]["data"]["rename_count"], 1)
        mismatch = ToolResult(True, "accepted", "别的 job", data={"operation_ref": jobs.organize_operation_public_ref(uuid4().hex)})
        before_state = _read_state(self.store)
        with self.assertRaises(StalePublicationError):
            tracker = observers._completion_tracker(mismatch)
            assert tracker is not None
            await effects._register_effect_wait(mismatch, tracker, _FS_TOOL, scope)
        self.assertEqual(_read_state(self.store), before_state)


    async def test_non_queue_trackers_keep_existing_registration_behavior(self):
        for kind in ("guangya_task", "guangya_organize_task", "local_media_task", "local_media_scan", "agent_job", "strm_run", "library_patrol"):
            with self.subTest(kind=kind):
                scope = await _confirmed_scope(self.store)
                tracker = observers._CompletionTracker(kind, {"task_id": f"offline-{kind}"})
                result = ToolResult(True, "accepted", "非同队列后台任务", data={"accepted": True})
                await effects._register_effect_wait(result, tracker, "offline.background", scope)
                state = _read_state(self.store)
                self.assertEqual(_seed(state, scope.plan_id)["tracker"], {"kind": kind, "value": tracker.value})
                self.assertEqual(state.metadata["effect_waits"][scope.plan_id]["state"], "pending")

    async def test_sqlite_effect_update_requires_existing_transaction_and_does_not_begin_or_commit(self):
        scope = await _confirmed_scope(self.store)
        before = _read_state(self.store)
        with db.get_conn() as conn:
            self.assertFalse(conn.in_transaction)
            with self.assertRaisesRegex(ValueError, "active SQLite transaction"):
                self.store.update_effect_state_in_transaction(
                    conn, owner=_OWNER, session_id=_SESSION, change=lambda state: state.metadata.update(marker=True),
                )
            self.assertFalse(conn.in_transaction)
            conn.execute("BEGIN IMMEDIATE")
            statements = []
            conn.set_trace_callback(statements.append)
            self.store.update_effect_state_in_transaction(
                conn, owner=_OWNER, session_id=_SESSION, change=lambda state: state.metadata.update(marker=True),
            )
            self.assertTrue(conn.in_transaction)
            self.assertFalse(any(sql.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK")) for sql in statements))
            self.assertTrue(_read_state(self.store, conn=conn).metadata["marker"])
            conn.rollback()
        self.assertEqual(_read_state(self.store), before)
        self.assertEqual(before.metadata["confirmed_publication"], _confirmed_binding(scope))

    async def test_real_process_exit_before_commit_leaves_no_job_or_wait(self):
        root = self.root / "precommit-process"
        root.mkdir()
        command = [sys.executable, "-B", "-m", "tests.test_agent_effect_admission", "--admission-child"]
        try:
            for mode, expected in (("before_commit", 79), ("inspect_rollback", 0)):
                result = await asyncio.to_thread(
                    subprocess.run, command + [mode, str(root)],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            report = json.loads((root / "rollback.json").read_text())
            self.assertEqual(report, {"jobs": 0, "waits": 0, "eligible_jobs": 0, "orphan_plan_status": "queued"})
        finally:
            import tests
            for path in root.glob("*-test-runtime.json"):
                temporary = Path(json.loads(path.read_text()))
                if (temporary != tests._TEST_ROOT and temporary.parent == Path(tempfile.gettempdir())
                        and temporary.name.startswith("mediaflux-test-runtime-")):
                    shutil.rmtree(temporary, ignore_errors=True)

    async def test_real_process_exit_after_enqueue_commit_recovers_one_receipt_without_overwriting_new_pending(self):
        root = self.root / "subprocess"
        root.mkdir()
        command = [sys.executable, "-B", "-m", "tests.test_agent_effect_admission", "--admission-child"]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        repository = Path(__file__).resolve().parents[1]
        try:
            crashed = await asyncio.to_thread(
                subprocess.run, command + ["admit", str(root)], cwd=repository, env=environment,
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(crashed.returncode, 77, crashed.stdout + crashed.stderr)
            boundary = json.loads((root / "crash.json").read_text())
            self.assertEqual(boundary["boundary"], "enqueue_committed_before_handler_return_and_verify")
            self.assertEqual(boundary["seed_count"], 1)
            self.assertFalse((root / "verified.json").exists())
            completed = await asyncio.to_thread(
                subprocess.run, command + ["complete", str(root)], cwd=repository, env=environment,
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(completed.returncode, 78, completed.stdout + completed.stderr)
            terminal = json.loads((root / "completed.json").read_text())
            self.assertTrue(terminal["rename_verified"])
            self.assertEqual(terminal["receipt_count"], 0)
            resumed = await asyncio.to_thread(
                subprocess.run, command + ["finish", str(root)], cwd=repository, env=environment,
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["seed_count_before_resume"], 1)
            self.assertEqual(result["queue_status"], "completed")
            self.assertTrue(result["rename_verified"])
            self.assertEqual(result["receipt_publish_count"], 1)
            self.assertEqual(result["repeat_publish_count"], 0)
            self.assertEqual(result["receipt_count"], 1)
            self.assertTrue(result["new_pending_preserved"])
            self.assertTrue(result["new_conversation_preserved"])
            self.assertNotEqual(result["old_plan_id"], result["pending_plan_id"])
            self.assertEqual(result["provider"], "explicit offline fixtures; socket access denied")
        finally:
            # os._exit 不运行 tests 包的 atexit；只回收子进程自己生成的临时运行目录。
            import tests
            for path in root.glob("*-test-runtime.json"):
                temporary = Path(json.loads(path.read_text()))
                if (temporary != tests._TEST_ROOT and temporary.parent == Path(tempfile.gettempdir())
                        and temporary.name.startswith("mediaflux-test-runtime-")):
                    shutil.rmtree(temporary, ignore_errors=True)


async def _child_process(mode: str, root: Path):
    import tests

    db.configure_database(root / "state.db", test_mode=True)
    db.init_db()
    from app.agent.feature_gate import initialize_runtime_fence
    initialize_runtime_fence()
    (root / f"{mode}-test-runtime.json").write_text(json.dumps(str(tests._TEST_ROOT)))
    store, client = SQLiteKernelStore(), _OfflineClient()
    with _offline_runtime(root, client) as runtime:
        if mode == "inspect_rollback":
            boundary = json.loads((root / "precommit.json").read_text())
            state = await store.load(owner=_OWNER, session_id=_SESSION)
            with db.get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0]
            bound = fs._read(boundary["fs_plan_id"])
            report = {"jobs": count, "waits": len(state.metadata.get("effect_waits", {})),
                      "eligible_jobs": len(jobs.list_pending_organize_operation_jobs()),
                      "orphan_plan_status": bound["status"]}
            (root / "rollback.json").write_text(json.dumps(report))
            return
        if mode in {"admit", "before_commit"}:
            def verifier(_arguments, result):
                (root / "verified.json").write_text(json.dumps({"verified": True}))
                return result

            pipeline, context, plan, _ = await _kernel_plan("fs", store, client, runtime, wait=True, verifier=verifier)
            real_enqueue = organize_tasks.enqueue_organize_operation_job

            def crash_after_commit(**kwargs):
                row, replayed = real_enqueue(**kwargs)
                assert not replayed
                state = _read_state(store)
                seed = _seed(state, plan.plan_id)
                assert seed["tracker"]["value"]["operation_ref"] == jobs.organize_operation_public_ref(row["job_id"])
                (root / "crash.json").write_text(json.dumps({
                    "job_id": row["job_id"], "plan_id": plan.plan_id,
                    "boundary": "enqueue_committed_before_handler_return_and_verify",
                    "seed_count": len(state.metadata.get("effect_waits", {})),
                }))
                # 真正的进程退出，不抛异常、不执行 handler 返回、verify 或清理。
                os._exit(77)

            if mode == "before_commit":
                real_bind = jobs._bind_fs_change_plan_to_job
                def crash_before_commit(row):
                    real_bind(row)
                    payload = json.loads(row["payload_json"])
                    (root / "precommit.json").write_text(json.dumps({"fs_plan_id": payload["plan_id"]}))
                    os._exit(79)  # FS文件已绑定，但SQLite事务仍未提交；不得补入队。
                runtime.enter_context(patch.object(jobs, "_bind_fs_change_plan_to_job", crash_before_commit))
            manager = organize_tasks.OrganizeTaskManager()
            with patch.object(organize_tasks, "get_organize_manager", lambda: manager), patch.object(
                organize_tasks, "enqueue_organize_operation_job", crash_after_commit,
            ):
                await pipeline.execute_confirmed(plan.plan_id, context=context)
            raise AssertionError("未到达真实 enqueue commit 崩溃边界")

        boundary = json.loads((root / "crash.json").read_text())
        row = jobs.get_organize_operation_job(boundary["job_id"])
        if mode == "finish":
            terminal = json.loads((root / "completed.json").read_text())
            assert row is not None
            assert row["status"] == "completed" and terminal["job_id"] == row["job_id"]
            assert terminal["rename_verified"] and terminal["receipt_count"] == 0
            before = protected = await store.load(owner=_OWNER, session_id=_SESSION)
            done, manager = row, organize_tasks.OrganizeTaskManager()
            pending_plan_id = protected.pending_effect_plan_id
            # 这次冷恢复仅允许读取持久结果，任何重复 Provider 写入都使测试失败。
            runtime.enter_context(patch.object(client, "rename", side_effect=AssertionError("must not replay completed write")))
        else:
            assert row is not None
            assert row["status"] == "pending"
            assert jobs.verify_organize_operation_payload(row)
            before = await store.load(owner=_OWNER, session_id=_SESSION)
            old_seed = _seed(before, boundary["plan_id"])
            assert old_seed["tracker"]["value"]["operation_ref"] == jobs.organize_operation_public_ref(row["job_id"])
            # 先通过真实 Kernel 建新 pending，再让旧队列完成，验证晚到结果绝不能覆盖它。
            _pipeline, newer, pending, _ = await _kernel_plan(
                "fs", store, client, runtime, request_id="new-user-turn", new_name="next-pending.mp4",
            )
            new_state = await store.load(owner=_OWNER, session_id=_SESSION)
            await store.commit(newer.lease, conversation=[*new_state.conversation, {"role": "user", "content": "新请求仍等待确认"}],
                               updates=(StateUpdate("summary", "new-turn-summary"),))
            protected = await store.load(owner=_OWNER, session_id=_SESSION)
            assert protected.pending_effect_plan_id == pending.plan_id
            assert protected.generation > before.generation
            claimed = jobs.claim_organize_operation_job(row["job_id"])
            assert claimed is not None
            manager = _run_real_worker(claimed)
            done = jobs.get_organize_operation_job(row["job_id"])
            assert done is not None
            assert done["status"] == "completed", dict(done)
            file_info_result_6 = client.file_info("rename")
            assert file_info_result_6 is not None
            assert file_info_result_6.name == "admitted.mp4"
            if mode == "complete":
                assert done is not None
                (root / "completed.json").write_text(json.dumps({
                    "job_id": done["job_id"], "rename_verified": True,
                    "receipt_count": sum(bool(item.get("completion_receipt_id")) for item in protected.conversation),
                }))
                os._exit(78)  # 队列真实终态已提交；后台补发尚未读取该终态。
            pending_plan_id = pending.plan_id

        def due(state):
            record = state.metadata["effect_waits"][boundary["plan_id"]]
            record["next_poll_at"] = 0
            state.metadata["effect_next_poll_at"] = 0

        await store.update_effect_state(owner=_OWNER, session_id=_SESSION, change=due)
        with patch.object(organize_tasks, "get_organize_manager", lambda: manager):
            counts = await asyncio.gather(effects.poll_effect_receipts(store), effects.poll_effect_receipts(SQLiteKernelStore()))
            assert sum(counts) == 1, counts
            # 再次制造 due 也不应重复发布已投递回执；再新建仓储模拟另一个重启。
            await store.update_effect_state(owner=_OWNER, session_id=_SESSION, change=due)
            repeats = await effects.poll_effect_receipts(SQLiteKernelStore())
            assert repeats == 0
        final = await SQLiteKernelStore().load(owner=_OWNER, session_id=_SESSION)
        receipts = [item for item in final.conversation if item.get("completion_receipt_id") == boundary["plan_id"]]
        assert len(receipts) == 1, final.conversation
        assert receipts[0]["effect_plan_id"] == boundary["plan_id"]
        assert receipts[0]["tool_name"] == _FS_TOOL
        assert json.loads(receipts[0]["content"].split("\n", 1)[1])["status"] == "completed"
        assert final.pending_effect_plan_id == protected.pending_effect_plan_id
        assert final.generation == protected.generation
        assert final.summary == protected.summary
        assert final.conversation[:-1] == protected.conversation
        for key in ("pending_effect_binding", "pending_effect_goal", "confirmed_publication", "active_publication", "task", "read_wait"):
            assert final.metadata.get(key) == protected.metadata.get(key), key
        assert final.metadata["effect_waits"][boundary["plan_id"]]["delivered"] is True
        assert done is not None
        report = {
            "seed_count_before_resume": len(before.metadata["effect_waits"]),
            "queue_status": done["status"], "rename_verified": terminal["rename_verified"],
            "receipt_publish_count": sum(counts), "repeat_publish_count": repeats, "receipt_count": len(receipts),
            "new_pending_preserved": final.pending_effect_plan_id == pending_plan_id,
            "new_conversation_preserved": final.conversation[:-1] == protected.conversation,
            "old_plan_id": boundary["plan_id"], "pending_plan_id": pending_plan_id,
            "provider": "explicit offline fixtures; socket access denied",
        }
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False))
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--admission-child":
        asyncio.run(_child_process(sys.argv[2], Path(sys.argv[3])))
    else:
        unittest.main()
