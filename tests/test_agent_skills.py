"""Skills 的真实 SQLite 修订、发布事务及 native owner 隐私边界。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.references import ReferenceError
from app.agent.kernel.state_types import StalePublicationError, StateUpdate
from app.repositories import agent_skills as skills
from tests.support import IsolatedDatabaseTestCase, isolated_test_database
from tests.test_agent_code_sandbox import make_context


def test_sdk_skill_calls_use_host_identity_and_restore_prior_context():
    from app import sdk

    with isolated_test_database():
        with sdk.call_owner("owner-a"):
            first = sdk.skills.save("research", "可复用检索", "# 完整方法\n")['item']
            with sdk.call_owner("owner-b"):
                assert sdk.skills.list()["items"] == []
                assert sdk.skills.get("research")["item"] is None
            assert sdk.skills.get("research")["item"] == first
            second = sdk.skills.save("research", "修订检索", "# 修改后的方法\n", first["revision"])["item"]
            restored = sdk.skills.restore("research", first["revision"], second["revision"])["item"]
            assert restored["content"] == first["content"]
            assert len(sdk.skills.list("research")["items"]) == 3
        try:
            sdk.skills.list()
        except ValueError as exc:
            assert "身份" in str(exc)
        else:
            raise AssertionError("SDK must not inherit an earlier caller identity")


def test_user_memory_contains_bounded_skill_index_not_skill_body():
    from app.modules.media_preferences import owner_memory

    with isolated_test_database():
        for index in range(21):
            skills.save_skill(owner="skill-owner", name=f"method-{index}",
                              description="检索方法" * 100, content="BODY_MUST_STAY_ON_DEMAND", expected_revision=0)
        value = owner_memory("skill-owner")
        assert "BODY_MUST_STAY_ON_DEMAND" not in value
        memory = json.loads(value)
        assert memory["preferences"] == {} and memory["notes"] == []
        index = memory["skills"]
        assert index["total"] == 21 and len(index["items"]) == 20
        assert all(len(item["description"]) == 160 and item["description_truncated"] for item in index["items"])
        assert index["list_method"] == "skills.list" and index["read_method"] == "skills.get"
        assert owner_memory("other-owner") == ""


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区容器验收")
def test_native_sdk_skills_survive_workspace_reset_and_keep_owner_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def execute(owner, session, code):
        store = SQLiteKernelStore()
        context = await make_context(store, owner=owner, session=session)
        catalog = ToolCatalog()
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
        assert result.effect_plan is None
        data = json.loads(result.outcome.full_model_content or result.outcome.model_content)["data"]
        assert data["exit_code"] == 0, data
        return json.loads(data["stdout"])

    async def exercise():
        first = await execute("skills-a", "first", '''
import json
from mf import skills
print(json.dumps(skills.save("research", "已验证检索方法", "# 完整方法\\n")['item']))
''')
        await SQLiteKernelStore().reset_session(owner="skills-a", session_id="first")
        # 新解释器、新工作区、新 KernelStore 仍从同一身份的持久技能库读取。
        loaded = await execute("skills-a", "second", '''
import json
from mf.skills import get, list
print(json.dumps({"item": get("research")['item'], "index": list()['items']}))
''')
        assert loaded["item"] == first
        assert loaded["index"][0]["revision"] == first["revision"]
        assert "content" not in loaded["index"][0]
        assert await execute("skills-b", "second", '''
import json
from mf import skills
print(json.dumps(skills.list()['items']))
''') == []
        restored = await execute("skills-a", "second", f'''
import json
from mf import skills
updated = skills.save("research", "更新", "# 第二版", {first["revision"]})['item']
print(json.dumps(skills.restore("research", {first["revision"]}, updated['revision'])['item']))
''')
        assert restored["content"] == first["content"]
        assert restored["restored_from"] == first["revision"]
        assert restored["revision"] > first["revision"]

    with isolated_test_database():
        asyncio.run(exercise())


class AgentSkillsTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        self.owner = "web:" + self._testMethodName
        self.other = "tg:" + self._testMethodName

    def save(self, *, owner=None, name="research", expected_revision=0,
             content="# 已验证方法\n\n保留完整正文。\n", **kwargs):
        return skills.save_skill(owner=owner or self.owner, name=name,
                                 description="已验证的资源检索方法", content=content,
                                 expected_revision=expected_revision, **kwargs)

    def publication_check(self, store, lease):
        def check(conn):
            self.assertTrue(conn.in_transaction)
            store.require_current_in_transaction(lease, conn)
        return check

    def test_revisions_restore_and_owner_isolation_across_sessions(self):
        first = self.save()
        other = self.save(owner=self.other, content="# 另一用户\n")
        second = self.save(expected_revision=first["revision"], content="# 修订方法\n")
        restored = skills.restore_skill(owner=self.owner, name="research",
                                        revision=first["revision"], expected_revision=second["revision"])
        self.assertGreater(restored["revision"], second["revision"])
        self.assertEqual(restored["restored_from"], first["revision"])
        self.assertEqual(restored["content"], first["content"])
        self.assertEqual(skills.get_skill(owner=self.owner, name="research"), restored)
        self.assertEqual(skills.get_skill(owner=self.owner, name="research",
                                         revision=second["revision"]), second)
        self.assertIsNone(skills.get_skill(owner=self.other, name="research", revision=first["revision"]))
        self.assertIsNone(skills.get_skill(owner=self.owner, name="other", revision=first["revision"]))
        with self.assertRaises(LookupError):
            skills.restore_skill(owner=self.other, name="research", revision=first["revision"],
                                 expected_revision=other["revision"])
        self.assertEqual(skills.get_skill(owner=self.other, name="research"), other)
        history = skills.list_skills(owner=self.owner, name="research")
        self.assertEqual([row["revision"] for row in history],
                         [restored["revision"], second["revision"], first["revision"]])
        self.assertEqual(skills.list_skills(owner=self.owner), [history[0]])
        self.assertTrue(all("content" not in row and "owner_digest" not in row for row in history))

        async def sessions():
            store = SQLiteKernelStore()
            a, _ = await store.begin_turn(owner=self.owner, session_id="a", request_id="one")
            b, _ = await store.begin_turn(owner=self.owner, session_id="b", request_id="one")
            foreign, _ = await store.begin_turn(owner=self.other, session_id="a", request_id="one")
            await store.reset_session(owner=self.owner, session_id="a")
            await store.delete_session(owner=self.owner, session_id="a")
            cold = SQLiteKernelStore()
            self.assertFalse(await cold.is_current(a))
            self.assertTrue(await cold.is_current(b))
            self.assertTrue(await cold.is_current(foreign))
            self.assertEqual(skills.get_skill(owner=self.owner, name="research"), restored)
            self.assertEqual(skills.get_skill(owner=self.other, name="research"), other)

        asyncio.run(sessions())

    def test_concurrent_create_and_save_restore_use_one_cas(self):
        def race(*operations):
            barrier = threading.Barrier(len(operations))

            def run(operation):
                barrier.wait(timeout=5)
                try:
                    return operation()
                except skills.SkillRevisionConflict as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=len(operations)) as executor:
                outcomes = list(executor.map(run, operations))
            winners = [item for item in outcomes if isinstance(item, dict)]
            conflicts = [item for item in outcomes if isinstance(item, skills.SkillRevisionConflict)]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].current_revision, winners[0]["revision"])
            return winners[0]

        first = race(lambda: self.save(content="first"), lambda: self.save(content="second"))
        second = self.save(expected_revision=first["revision"], content="revised")
        winner = race(
            lambda: self.save(expected_revision=second["revision"], content="concurrent save"),
            lambda: skills.restore_skill(owner=self.owner, name="research", revision=first["revision"],
                                         expected_revision=second["revision"]),
        )
        self.assertEqual(skills.get_skill(owner=self.owner, name="research"), winner)
        self.assertEqual(len(skills.list_skills(owner=self.owner, name="research")), 3)

    def test_current_schema_addition_is_idempotent_and_keeps_existing_data(self):
        with isolated_test_database():
            store = SQLiteKernelStore()
            lease, _ = asyncio.run(store.begin_turn(owner=self.owner, session_id="old", request_id="one"))
            asyncio.run(store.commit(lease, updates=(StateUpdate("summary", "existing state"),)))
            with db.get_conn() as conn:
                conn.execute("DROP TABLE agent_skill_revisions")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            db.init_db()
            first = self.save()
            db.init_db()
            self.assertEqual(skills.get_skill(owner=self.owner, name="research"), first)
            self.assertEqual(asyncio.run(SQLiteKernelStore().load(
                owner=self.owner, session_id="old")).summary, "existing state")
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)

    def test_publication_guard_uses_write_transaction_and_rolls_back_its_changes(self):
        store = SQLiteKernelStore()
        lease, _ = asyncio.run(store.begin_turn(owner=self.owner, session_id="a", request_id="one"))
        with db.get_conn() as conn, self.assertRaisesRegex(ValueError, "active SQLite transaction"):
            store.require_current_in_transaction(lease, conn)
        first = self.save(publication_check=self.publication_check(store, lease))

        def fail(conn):
            self.assertTrue(conn.in_transaction)
            store.update_effect_state_in_transaction(
                conn, owner=self.owner, session_id="a",
                change=lambda state: setattr(state, "summary", "must roll back"),
            )
            raise StalePublicationError("host rejected")

        for operation in (
            lambda: self.save(expected_revision=first["revision"], publication_check=fail),
            lambda: skills.restore_skill(owner=self.owner, name="research", revision=first["revision"],
                                         expected_revision=first["revision"], publication_check=fail),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(StalePublicationError, "host rejected"):
                operation()
            self.assertEqual(skills.get_skill(owner=self.owner, name="research"), first)
            self.assertEqual(asyncio.run(store.load(owner=self.owner, session_id="a")).summary, "")

    def test_continuation_publication_reuses_claim_and_runtime_fence_checks(self):
        from app.agent.feature_gate import RuntimeFenceError, begin_runtime_change, read_runtime_fence
        from tests.test_agent_read_wait_lifecycle import parked_runtime

        async def exercise():
            timer = [100]
            runtime, candidate = await parked_runtime(timer, session_id=self._testMethodName)
            timer[0] = 110
            store = runtime.store
            lease, _ = await store.claim_continuation(candidate, runtime.session.continuation_binding)
            self.assertTrue(await store.is_current(lease))
            first = self.save(owner=lease.owner, publication_check=self.publication_check(store, lease))
            revoked = replace(lease, continuation_claim_id="other-claim")
            self.assertFalse(await store.is_current(revoked))
            with self.assertRaises(StalePublicationError):
                self.save(owner=lease.owner, expected_revision=first["revision"],
                          publication_check=self.publication_check(store, revoked))
            with db.get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                begin_runtime_change(conn, read_runtime_fence(conn))
            self.assertFalse(await store.is_current(lease))
            with self.assertRaises(RuntimeFenceError):
                skills.restore_skill(owner=lease.owner, name="research", revision=first["revision"],
                                     expected_revision=first["revision"],
                                     publication_check=self.publication_check(store, lease))
            self.assertEqual(skills.get_skill(owner=lease.owner, name="research"), first)

        with isolated_test_database():
            asyncio.run(exercise())

    def test_purge_removes_native_partitions_and_skills_without_reusing_generations(self):
        async def exercise():
            store = SQLiteKernelStore()
            scopes = ((self.owner, "a"), (self.owner, "b"), (self.other, "a"))
            leases, refs = {}, {}
            for owner, session_id in scopes:
                lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id="one")
                leases[owner, session_id] = lease
                await store.commit(lease, updates=(StateUpdate("summary", session_id),))
                refs[owner, session_id] = await store.put_for_turn(
                    lease, owner=owner, session_id=session_id, kind="private", value={"session": session_id})
                await store.append_for_turn(AgentEvent(
                    type=AgentEventType.TOOL_COMPLETED, session_id=session_id, turn_id=lease.turn_id,
                    request_id=lease.request_id, sequence=1, payload={"private": session_id}), lease)
            # 先前已删除的会话只剩 epoch；owner purge 必须保留它。
            retired, _ = await store.begin_turn(owner=self.owner, session_id="retired", request_id="old")
            await store.delete_session(owner=self.owner, session_id="retired")
            first = self.save()
            other = self.save(owner=self.other)
            with db.get_conn() as conn:
                epochs_before = [tuple(row) for row in conn.execute(
                    "SELECT session_digest,generation FROM agent_kernel_session_epochs "
                    "WHERE owner_digest=? ORDER BY session_digest", (db.native_agent_owner_digest(self.owner),))]

            deleted = db.purge_agent_subject_data(owner=self.owner)
            self.assertEqual({key: deleted[key] for key in ("kernel_sessions", "kernel_refs", "kernel_events", "skills")},
                             {"kernel_sessions": 2, "kernel_refs": 2, "kernel_events": 2, "skills": 1})
            with db.get_conn() as conn:
                self.assertEqual([tuple(row) for row in conn.execute(
                    "SELECT session_digest,generation FROM agent_kernel_session_epochs "
                    "WHERE owner_digest=? ORDER BY session_digest", (db.native_agent_owner_digest(self.owner),))],
                    epochs_before)
            cold = SQLiteKernelStore()
            for session_id in ("a", "b"):
                lease = leases[self.owner, session_id]
                self.assertFalse(await cold.is_current(lease))
                self.assertIsNone(cold.load_existing_session(owner=self.owner, session_id=session_id))
                self.assertEqual(await cold.list_events(owner=self.owner, session_id=session_id), [])
                with self.assertRaises(ReferenceError):
                    await cold.resolve(refs[self.owner, session_id].ref, owner=self.owner, session_id=session_id)
                with self.assertRaises(StalePublicationError):
                    await cold.commit(lease, updates=(StateUpdate("summary", "late"),))
            self.assertEqual(await cold.resolve(refs[self.other, "a"].ref, owner=self.other, session_id="a"),
                             {"session": "a"})
            self.assertTrue(await cold.is_current(leases[self.other, "a"]))
            self.assertEqual(len(await cold.list_events(owner=self.other, session_id="a")), 1)
            self.assertEqual(skills.get_skill(owner=self.other, name="research"), other)
            self.assertEqual(skills.list_skills(owner=self.owner), [])

            # CAS 本身不授权新建；旧回合的首次创建也必须被宿主 publication guard 拒绝。
            with self.assertRaises(StalePublicationError):
                self.save(name="late-new-skill", publication_check=self.publication_check(cold, leases[self.owner, "a"]))
            for old in (leases[self.owner, "a"], retired):
                new, _ = await cold.begin_turn(owner=self.owner, session_id=old.session_id, request_id="new")
                self.assertGreater(new.generation, old.generation)
                self.assertFalse(await cold.is_current(old))
                with self.assertRaises(StalePublicationError):
                    self.save(publication_check=self.publication_check(cold, old))
            replacement = self.save()
            self.assertGreater(replacement["revision"], first["revision"])
            with self.assertRaises(skills.SkillRevisionConflict):
                self.save(expected_revision=first["revision"])

        asyncio.run(exercise())

    def test_purge_failure_rolls_back_native_data_epochs_and_all_skill_revisions(self):
        store = SQLiteKernelStore()
        lease, _ = asyncio.run(store.begin_turn(owner=self.owner, session_id="a", request_id="one"))
        asyncio.run(store.put_for_turn(lease, owner=self.owner, session_id="a", kind="private", value="private"))
        asyncio.run(store.append_for_turn(AgentEvent(
            type=AgentEventType.TOOL_COMPLETED, session_id="a", turn_id=lease.turn_id,
            request_id=lease.request_id, sequence=1), lease))
        first = self.save()
        self.save(expected_revision=first["revision"], content="revised")
        tables = ("agent_kernel_sessions", "agent_kernel_refs", "agent_kernel_events",
                  "agent_kernel_session_epochs", "agent_skill_revisions")

        def snapshot():
            with db.get_conn() as conn:
                return {table: [tuple(row) for row in conn.execute(
                    f"SELECT * FROM {table} WHERE owner_digest=? ORDER BY rowid",
                    (db.native_agent_owner_digest(self.owner),))] for table in tables}

        before = snapshot()
        original = db.get_conn

        @contextmanager
        def fail_purge():
            with original() as conn:
                conn.execute("CREATE TEMP TRIGGER fail_skill_purge AFTER DELETE ON agent_skill_revisions "
                             "BEGIN SELECT RAISE(ABORT,'purge rollback probe'); END")
                yield conn

        with patch.object(db, "get_conn", fail_purge), self.assertRaisesRegex(sqlite3.IntegrityError, "purge rollback probe"):
            db.purge_agent_subject_data(owner=self.owner)
        self.assertEqual(snapshot(), before)
        self.assertTrue(asyncio.run(store.is_current(lease)))
        self.assertEqual(len(skills.list_skills(owner=self.owner, name="research")), 2)

    def test_input_rejection_does_not_publish_a_revision(self):
        for override in ({"name": "../escape"}, {"name": ""}, {"expected_revision": True},
                         {"expected_revision": -1}, {"content": ""}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.save(**override)
        with self.assertRaises(ValueError):
            skills.save_skill(owner=self.owner, name="research", description="", content="body", expected_revision=0)
        self.assertEqual(skills.list_skills(owner=self.owner), [])
