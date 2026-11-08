"""Small same-generation CAS, lifecycle and strict evidence controls."""
from typing import Any
from app.agent.kernel.task_state import TaskPatch
from typing import cast

import asyncio
import json
import multiprocessing
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import database as db
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import (
    SessionBusyError,
    StalePublicationError,
    StateUpdate,
)
from app.agent.kernel.task_state import (
    TaskConflict,
    identity,
    observation_patch,
    proposal,
    reduce_task,
    task_projection,
    validate_proposal,
)
from tests.test_agent_task_first_slice import GOAL, OWNER, declaration


@pytest.fixture
def task_store(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "cas.db", test_mode=True)
    db.init_db()
    yield SQLiteKernelStore(secret_provider=lambda: "task-test-only")
    db.configure_database(previous[0], test_mode=previous[1])


def control(args, task=None):
    return proposal(args, SimpleNamespace(request_id="request", user_source=GOAL, current_task=task))


def command(task, operation, **extra):
    return control({"operation": operation, **extra}, task)


def observe(task, component="tmdb", *, success=True, owner=OWNER):
    fact = {
        "component": component, "status": "not_configured", "enabled": False,
        "probe_mode": "configuration_only", "network_accessed": False,
    } if success else None
    return observation_patch(
        task, owner=owner, session_id="cas", tool="diagnostics.component",
        call_id="call", observations={component: fact},
    )


def observe_batch(task, *, failed=()):
    return observation_patch(
        task, owner=OWNER, session_id="cas", tool="diagnostics.configuration", call_id="batch-call",
        observations={
            component: None if component in failed else {
                "component": component, "status": "ready", "enabled": True,
                "probe_mode": "configuration_only", "network_accessed": False,
            }
            for component in ("tmdb", "qbittorrent")
        },
    )


def legacy_observations(task, *, owner=OWNER):
    # 模拟旧 reducer 已持久化的合法满容量快照，不放宽现行配额。
    task = deepcopy(task)
    for _ in range(32):
        observe_result = observe(task, owner=owner)
        assert observe_result is not None
        task["verified_facts"].append(observe_result.payload["observations"]["tmdb"])
        task["revision"] += 1
    return task


def cas_worker(database, lease, task_patch, marker, ready, start, committed, results):
    db.configure_database(database, test_mode=True)
    store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
    ready.put(marker)
    if not start.wait(15):
        raise TimeoutError("CAS start barrier")

    async def attempt():
        try:
            await store.commit(
                lease,
                conversation=[{"role": "user", "content": marker}],
                task_patch=task_patch,
            )
        except StalePublicationError as exc:
            assert isinstance(exc.__cause__, SessionBusyError)
            assert committed.wait(15)
            await store.commit(
                lease,
                conversation=[{"role": "user", "content": marker}],
                task_patch=task_patch,
            )

    try:
        asyncio.run(attempt())
    except TaskConflict:
        results.put("conflict")
    else:
        committed.set()
        results.put("committed")


def test_sqlite_same_generation_cas_and_capacity_are_atomic(task_store):
    async def run():
        lease, _ = await task_store.begin_turn(
            owner=OWNER, session_id="cas", request_id="request"
        )
        initial = await task_store.commit(lease, task_patch=control(declaration()))
        task = initial.metadata["task"]
        patch = command(task, "checkpoint", evidence_ids=[], blockers=[])
        other = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        context = multiprocessing.get_context("spawn")
        ready, results, start, committed = (
            context.Queue(),
            context.Queue(),
            context.Event(),
            context.Event(),
        )
        workers = [
            context.Process(
                target=cas_worker,
                args=(
                    db.DB_PATH,
                    lease,
                    patch,
                    marker,
                    ready,
                    start,
                    committed,
                    results,
                ),
            )
            for marker in ("winner-a", "winner-b")
        ]
        try:
            for worker in workers:
                worker.start()
            assert {ready.get(timeout=20), ready.get(timeout=20)} == {
                "winner-a",
                "winner-b",
            }
            start.set()
            assert sorted([results.get(timeout=20), results.get(timeout=20)]) == [
                "committed",
                "conflict",
            ]
            for worker in workers:
                worker.join(20)
                assert worker.exitcode == 0
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
        saved = await other.load(owner=OWNER, session_id="cas")
        with pytest.raises(TaskConflict):
            await task_store.commit(
                lease,
                conversation=[{"role": "user", "content": "stale"}],
                task_patch=patch,
            )
        assert (
            await other.load(owner=OWNER, session_id="cas")
        ).to_payload() == saved.to_payload()
        current = command(
            saved.metadata["task"], "checkpoint", evidence_ids=[], blockers=[]
        )
        with pytest.raises(ValueError):
            await task_store.commit(
                lease,
                task_patch=current,
                conversation=[{"role": "user", "content": "overflow"}],
                updates=[StateUpdate("metadata.pressure", "x" * 262144)],
            )
        assert (
            await other.load(owner=OWNER, session_id="cas")
        ).to_payload() == saved.to_payload()
        for update in (
            StateUpdate("metadata.task", {}),
            StateUpdate("metadata.task", None, "delete"),
            StateUpdate("metadata", {}),
        ):
            with pytest.raises(ValueError):
                await task_store.commit(lease, updates=[update])
        with pytest.raises(ValueError):
            await task_store.update_effect_state(
                owner=OWNER,
                session_id="cas",
                change=lambda state: state.metadata.pop("task"),
            )
        await task_store.update_effect_state(
            owner=OWNER,
            session_id="cas",
            change=lambda state: state.metadata.update(other="safe"),
        )
        assert (await other.load(owner=OWNER, session_id="cas")).metadata[
            "task"
        ] == saved.metadata["task"]

    asyncio.run(run())


def test_evidence_scope_lifecycle_and_component_blockers():
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    observed = {
        "component": "tmdb",
        "status": "not_configured",
        "enabled": True,
        "probe_mode": "configuration_only",
        "network_accessed": False,
    }

    def observation(current, component, fact):
        return observation_patch(
            current,
            owner=OWNER,
            session_id="cas",
            tool="diagnostics.component",
            call_id="call",
            observations={component: fact},
        )

    task = apply(task, observation(task, "tmdb", None))
    assert task["status"] == "blocked"
    task = apply(
        task, observation(task, "qbittorrent", {**observed, "component": "qbittorrent"})
    )
    assert task["blockers"] == [
        {"origin": "query", "component": "tmdb", "epoch": 1, "text": "query:tmdb"}
    ]
    task = apply(task, observation(task, "tmdb", observed))
    assert task["blockers"] == [] and task["status"] == "partial"
    evidence = task["verified_facts"][-1]["evidence_id"]
    with pytest.raises(ValueError):
        apply(task, command(task, "checkpoint", evidence_ids=["forged"], blockers=[]))
    for key, value in (("owner", "another"), ("session_id", "another"), ("epoch", 999)):
        corrupt = deepcopy(task)
        corrupt["verified_facts"][-1]["scope"][key] = value
        with pytest.raises(ValueError):
            apply(
                corrupt,
                command(corrupt, "checkpoint", evidence_ids=[evidence], blockers=[]),
            )
    done = apply(
        task,
        command(
            task,
            "checkpoint",
            evidence_ids=[f["evidence_id"] for f in task["verified_facts"]],
            blockers=[],
        ),
    )
    assert done["status"] == "complete"
    revised = declaration()
    revised.update(operation="revise")
    changed = apply(done, control(revised, done))
    assert changed["epoch"] == 2 and changed["verified_facts"] == []
    with pytest.raises(ValueError):
        apply(
            changed,
            command(changed, "checkpoint", evidence_ids=[evidence], blockers=[]),
        )
    cancelled = apply(changed, command(changed, "cancel", source=GOAL))
    with pytest.raises(TaskConflict):
        apply(cancelled, control(declaration()))
    replacement = declaration()
    fresh = apply(cancelled, control(replacement, cancelled))
    assert fresh["task_id"] != cancelled["task_id"] and (
        fresh["epoch"],
        fresh["revision"],
    ) == (1, 1)
    assert fresh["verified_facts"] == [] and fresh["blockers"] == []
    unsupported = declaration()
    unsupported["obligations"][1]["acceptance"] = {
        "kind": "repair",
        "criterion": "修复并联网",
    }
    assert apply(None, control(unsupported))["status"] == "blocked"


def test_strict_proposal_rejects_coercion_and_unbounded_data():
    mutations = [
        lambda a: a.update(status="complete"),
        lambda a: a.update(obligations=[]),
        lambda a: a["obligations"][0].update(required=1),
        lambda a: a["obligations"][0].update(required=False),
        lambda a: a.update(root_goal="x" * 2049),
        lambda a: a.update(constraints=[str(i) for i in range(9)]),
        lambda a: a["obligations"][1].update(id="tmdb"),
        lambda a: a["obligations"][0].update(
            acceptance={"kind": "repair", "criterion": {"nested": True}}
        ),
        lambda a: a.update(expected={"task_id": "id", "epoch": True, "revision": 1}),
        lambda a: a.update(source=""),
    ]
    for mutate in mutations:
        args = declaration()
        mutate(args)
        with pytest.raises((ValueError, TypeError)):
            validate_proposal(args)
    args = declaration()
    args["source"] = "not the current user"
    with pytest.raises(ValueError):
        control(args)


def test_blocker_origins_coexist_and_remain_bounded():
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    def observe(task, fact=None):
        return observation_patch(
            task, owner=OWNER, session_id="cas", tool="diagnostics.component",
            call_id="call", observations={"tmdb": fact},
        )

    task = apply(None, control(declaration()))
    task = apply(task, observe(task))
    query = {"origin": "query", "component": "tmdb", "epoch": 1, "text": "query:tmdb"}
    reported = {"origin": "reported", "text": "query:tmdb"}
    task = apply(task, command(task, "checkpoint", evidence_ids=[], blockers=["query:tmdb"]))
    assert task["blockers"] == [query, reported] and task["status"] == "blocked"
    again = apply(task, observe(task))
    assert again["blockers"] == [query, reported]  # Repeated failure is idempotent.
    cleared = apply(task, command(task, "checkpoint", evidence_ids=[], blockers=[]))
    assert cleared["blockers"] == [query] and cleared["status"] == "blocked"
    observed = {
        "component": "tmdb", "status": "not_configured", "enabled": False,
        "probe_mode": "configuration_only", "network_accessed": False,
    }
    recovered = apply(task, observe(task, observed))
    assert recovered["blockers"] == [reported] and recovered["status"] == "blocked"
    assert len(recovered["verified_facts"]) == 1
    # Clearing is qualified by epoch, never merely matching text/component.
    other_epoch = deepcopy(task)
    other_epoch["blockers"][0]["epoch"] = 2
    assert apply(other_epoch, observe(other_epoch, observed))["blockers"] == other_epoch["blockers"]
    bounded = apply(task, command(task, "checkpoint", evidence_ids=[], blockers=[str(i) for i in range(15)]))
    assert len(bounded["blockers"]) == 16
    before = deepcopy(task)
    with pytest.raises(ValueError):
        apply(task, command(task, "checkpoint", evidence_ids=[], blockers=[str(i) for i in range(16)]))
    assert task == before
    for values in ([query], [reported], ["duplicate", "duplicate"]):
        with pytest.raises(ValueError):
            command(task, "checkpoint", evidence_ids=[], blockers=values)
    revised = declaration()
    revised.update(operation="revise")
    assert apply(task, control(revised, task))["blockers"] == []



def test_unassociated_observations_keep_latest_component_snapshots():
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    task = apply(task, observe(task, "qbittorrent"))
    other = deepcopy(task["verified_facts"][0])
    first = None
    for _ in range(32):
        patch = observe(task)
        assert patch is not None
        first = first or patch.payload["observations"]["tmdb"]["evidence_id"]
        task = apply(task, patch)
        assert patch is not None
        assert task["verified_facts"] == [other, patch.payload["observations"]["tmdb"]]
        assert task["status"] == "partial"
        assert all(o["state"] == "pending" and o["evidence_ids"] == [] for o in task["obligations"])
    task_projection_result = task_projection(task)
    assert task_projection_result is not None
    assert task_projection_result["remaining_obligation_ids"] == ["tmdb", "qbittorrent"]
    before = deepcopy(task)
    with pytest.raises(ValueError, match="invalid evidence scope"):
        apply(task, command(task, "checkpoint", evidence_ids=[first], blockers=[]))
    assert task == before


def test_explicit_checkpoints_replace_evidence_and_preserve_reported_blockers():
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    previous = None
    for _ in range(32):
        patch = observe(task)
        assert patch is not None
        latest = patch.payload["observations"]["tmdb"]["evidence_id"]
        task = apply(task, patch)
        assert task["obligations"][0]["evidence_ids"] == ([previous] if previous else [])
        assert len(task["verified_facts"]) == (2 if previous else 1)
        # 参数顺序故意与登记顺序相反，最新显式证据不能由参数排序决定。
        selected = [latest, previous] if previous else [latest]
        task = apply(task, command(task, "checkpoint", evidence_ids=selected, blockers=["query:tmdb"]))
        assert task["obligations"][0]["evidence_ids"] == [latest]
        assert task["verified_facts"] == [patch.payload["observations"]["tmdb"]]
        assert task["blockers"] == [{"origin": "reported", "text": "query:tmdb"}]
        previous = latest

    accepted = deepcopy(task["verified_facts"][0])
    task = apply(task, observe(task, "qbittorrent", success=False))
    task = apply(task, observe(task, "qbittorrent"))
    assert task["verified_facts"][0] == accepted
    assert task["obligations"][1]["state"] == "pending"
    assert task["blockers"] == [{"origin": "reported", "text": "query:tmdb"}]
    evidence = task["verified_facts"][-1]["evidence_id"]
    task = apply(task, command(task, "checkpoint", evidence_ids=[evidence], blockers=["query:tmdb"]))
    assert all(o["state"] == "verified" for o in task["obligations"])
    assert task["status"] == "blocked"
    task = apply(task, command(task, "checkpoint", evidence_ids=[], blockers=[]))
    assert task["status"] == "complete"
    assert [o["evidence_ids"] for o in task["obligations"]] == [[previous], [evidence]]


@pytest.mark.parametrize("key", ["owner", "session_id", "task_id", "epoch"])
def test_checkpoint_replacement_rejects_invalid_selected_and_existing_scope(key):
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    task = apply(task, observe(task))
    task = apply(task, command(task, "checkpoint", evidence_ids=[task["verified_facts"][0]["evidence_id"]], blockers=[]))
    task = apply(task, observe(task))
    latest = task["verified_facts"][-1]["evidence_id"]
    for index, error in ((0, "invalid linked evidence"), (1, "invalid evidence scope")):
        corrupt = deepcopy(task)
        corrupt["verified_facts"][index]["scope"][key] = 999 if key == "epoch" else "another"
        before = deepcopy(corrupt)
        with pytest.raises(ValueError, match=error):
            apply(corrupt, command(corrupt, "checkpoint", evidence_ids=[latest], blockers=[]))
        assert corrupt == before
    patch = observe(task)
    assert patch is not None
    patch.payload["observations"]["tmdb"]["scope"][key] = 999 if key == "epoch" else "another"
    before = deepcopy(task)
    with pytest.raises(ValueError, match="invalid observation evidence"):
        apply(task, patch)
    assert task == before


def test_snapshot_reclamation_keeps_cas_and_epoch_fences():
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    stale = observe(task)
    task = apply(task, observe(task))
    evidence = task["verified_facts"][0]["evidence_id"]
    before = deepcopy(task)
    with pytest.raises(TaskConflict):
        apply(task, stale)
    assert task == before
    revised = declaration()
    revised.update(operation="revise")
    task = apply(task, control(revised, task))
    before = deepcopy(task)
    with pytest.raises(ValueError, match="invalid evidence scope"):
        apply(task, command(task, "checkpoint", evidence_ids=[evidence], blockers=[]))
    old_epoch = observe(task)
    assert old_epoch is not None
    old_epoch.payload["observations"]["tmdb"]["epoch"] -= 1
    with pytest.raises(ValueError, match="invalid observation evidence"):
        apply(task, old_epoch)
    assert task == before


def test_snapshot_reclamation_does_not_relax_fact_or_byte_capacity():
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    before = deepcopy(task)
    with pytest.raises(ValueError, match="invalid task array"):
        command(task, "checkpoint", evidence_ids=[str(i) for i in range(33)], blockers=[])
    assert task == before
    oversized = declaration()
    oversized.update(root_goal="G" * 2048, constraints=[str(i) + "C" * 511 for i in range(8)])
    oversized["obligations"] = [
        {**deepcopy(oversized["obligations"][0]), "id": str(i), "description": "D" * 512}
        for i in range(16)
    ]
    with pytest.raises(ValueError, match="task capacity exceeded"):
        reduce_task(None, control(oversized), owner=OWNER, session_id="cas")


@pytest.mark.parametrize("linked_history", [False, True])
def test_sqlite_legacy_snapshot_recovers_after_cold_read(task_store, linked_history):
    owner = "review"  # 短 owner 使旧版 32 条合法事实仍满足 16 KiB 配额。

    async def run():
        lease, state = await task_store.begin_turn(owner=owner, session_id="cas", request_id="request")
        state = await task_store.commit(lease, task_patch=control(declaration()))
        task = legacy_observations(state.metadata["task"], owner=owner)
        latest = deepcopy(task["verified_facts"][-1])
        accepted = latest if linked_history else deepcopy(task["verified_facts"][0])
        retired = task["verified_facts"][1]["evidence_id"]
        # 旧关联列表故意逆序；规范化须按事实登记顺序，而不是模型列表顺序。
        existing_ids = [f["evidence_id"] for f in reversed(task["verified_facts"])] if linked_history else [accepted["evidence_id"]]
        task["obligations"][0].update(state="verified", evidence_ids=existing_ids)
        task.update(status="blocked", blockers=[
            {"origin": "query", "component": "qbittorrent", "epoch": task["epoch"], "text": "query:qbittorrent"},
            {"origin": "reported", "text": "query:qbittorrent"},
        ])
        state.metadata["task"] = task
        assert len(json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode()) <= 16 * 1024
        # 通过真实签名仓储写入旧版快照，仅使用 fixture 的临时数据库。
        with db.get_conn() as conn:
            task_store._write_state(conn, state)
        other = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        cold = await other.load(owner=owner, session_id="cas")
        assert cold.metadata["task"] == task and len(task["verified_facts"]) == 32
        bad = observe(task, "qbittorrent", owner=owner)
        assert bad is not None
        bad.payload["observations"]["qbittorrent"]["scope"]["owner"] = "another"
        with pytest.raises(ValueError, match="invalid observation evidence"):
            await other.commit(lease, task_patch=bad)
        assert (await other.load(owner=owner, session_id="cas")).to_payload() == cold.to_payload()
        recovery = observe(task, "qbittorrent", owner=owner)
        state = await other.commit(lease, task_patch=recovery)
        recovered = state.metadata["task"]
        expected = [accepted] if linked_history else [accepted, latest]
        assert recovery is not None
        assert recovered["verified_facts"] == [*expected, recovery.payload["observations"]["qbittorrent"]]
        assert recovered["obligations"][0]["evidence_ids"] == [accepted["evidence_id"]]
        assert recovered["blockers"] == [{"origin": "reported", "text": "query:qbittorrent"}]
        assert recovered["obligations"][1]["state"] == "pending"
        task_projection_result_2 = task_projection(recovered)
        assert task_projection_result_2 is not None
        assert task_projection_result_2["remaining_obligation_ids"] == ["qbittorrent"]
        with pytest.raises(TaskConflict):
            await other.commit(lease, task_patch=command(task, "checkpoint", evidence_ids=[], blockers=[]))
        with pytest.raises(ValueError, match="invalid evidence scope"):
            await other.commit(lease, task_patch=command(recovered, "checkpoint", evidence_ids=[retired], blockers=[]))
        assert (await other.load(owner=owner, session_id="cas")).to_payload() == state.to_payload()
        assert recovery is not None
        evidence = recovery.payload["observations"]["qbittorrent"]["evidence_id"]
        state = await other.commit(lease, task_patch=command(recovered, "checkpoint", evidence_ids=[evidence], blockers=["query:qbittorrent"]))
        assert state.metadata["task"]["status"] == "blocked"
        state = await other.commit(lease, task_patch=command(state.metadata["task"], "checkpoint", evidence_ids=[], blockers=[]))
        assert state.metadata["task"]["status"] == "complete"
        final = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=owner, session_id="cas")
        assert final.to_payload() == state.to_payload()

    asyncio.run(run())



def test_legacy_link_normalization_validates_every_old_reference():
    owner = "review"
    task = reduce_task(None, control(declaration()), owner=owner, session_id="cas")
    task = legacy_observations(task, owner=owner)
    task["obligations"][0].update(
        state="verified", evidence_ids=[f["evidence_id"] for f in reversed(task["verified_facts"])],
    )
    for index in (0, 31):
        corrupt = deepcopy(task)
        corrupt["verified_facts"][index]["scope"]["owner"] = "another"
        before = deepcopy(corrupt)
        with pytest.raises(ValueError, match="invalid linked evidence"):
            reduce_task(corrupt, cast(TaskPatch, observe(corrupt, "qbittorrent", owner=owner)), owner=owner, session_id="cas")
        assert corrupt == before



def test_batch_observation_is_one_revision_and_requires_explicit_checkpoint():
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    before = deepcopy(task)
    patch = observe_batch(task)
    assert patch is not None
    observed = reduce_task(task, patch, owner=OWNER, session_id="cas")
    assert task == before
    assert observed["revision"] == task["revision"] + 1
    assert patch is not None
    assert observed["verified_facts"] == list(patch.payload["observations"].values())
    assert len({f["evidence_id"] for f in observed["verified_facts"]}) == 2
    assert {f["call_id"] for f in observed["verified_facts"]} == {"batch-call"}
    assert observed["status"] == "partial" and observed["blockers"] == []
    assert all(o["state"] == "pending" and not o["evidence_ids"] for o in observed["obligations"])
    evidence = [f["evidence_id"] for f in observed["verified_facts"]]
    completed = reduce_task(observed, command(observed, "checkpoint", evidence_ids=evidence, blockers=[]), owner=OWNER, session_id="cas")
    assert completed["status"] == "complete" and completed["revision"] == observed["revision"] + 1
    assert [o["evidence_ids"] for o in completed["obligations"]] == [[eid] for eid in evidence]


@pytest.mark.parametrize("failed", ["tmdb", "qbittorrent"])
def test_batch_partial_failure_keeps_valid_facts_pending_and_reported_blocker(failed):
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    text = "query:" + failed
    task = apply(task, command(task, "checkpoint", evidence_ids=[], blockers=[text]))
    previous_revision = task["revision"]
    task = apply(task, observe_batch(task, failed=(failed,)))
    assert task["revision"] == previous_revision + 1
    assert len(task["verified_facts"]) == 1 and task["verified_facts"][0]["component"] != failed
    assert all(o["state"] == "pending" and not o["evidence_ids"] for o in task["obligations"])
    query = {"origin": "query", "component": failed, "epoch": task["epoch"], "text": text}
    reported = {"origin": "reported", "text": text}
    assert query in task["blockers"] and reported in task["blockers"]
    task = apply(task, command(task, "checkpoint", evidence_ids=[task["verified_facts"][0]["evidence_id"]], blockers=[text]))
    projection = task_projection(task)
    assert projection is not None
    assert task["status"] == "blocked" and projection["remaining_obligation_ids"] == [failed]
    accepted = [o["evidence_ids"][:] for o in task["obligations"]]
    task = apply(task, observe_batch(task))
    assert task["blockers"] == [reported]
    assert [o["evidence_ids"] for o in task["obligations"]] == accepted
    task_projection_result_3 = task_projection(task)
    assert task_projection_result_3 is not None
    assert task_projection_result_3["remaining_obligation_ids"] == [failed]
    task = apply(task, command(task, "checkpoint", evidence_ids=[f["evidence_id"] for f in task["verified_facts"]], blockers=[text]))
    projection = task_projection(task)
    assert projection is not None
    assert task["status"] == "blocked" and not projection["remaining_obligation_ids"]


@pytest.mark.parametrize("invalid", ["owner", "session_id", "task_id", "epoch", "fact_epoch", "component", "unknown"])
def test_batch_invalid_second_fact_rejects_the_entire_patch(invalid):
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    patch = observe_batch(task)
    assert patch is not None
    facts = patch.payload["observations"]
    fact = facts["qbittorrent"]
    if invalid == "unknown":
        facts["unsupported"] = facts.pop("qbittorrent")
    elif invalid == "component":
        fact["component"] = "tmdb"
    elif invalid == "fact_epoch":
        fact["epoch"] += 1
    else:
        fact["scope"][invalid] = 999 if invalid == "epoch" else "another"
    before = deepcopy(task)
    with pytest.raises(ValueError):
        reduce_task(task, patch, owner=OWNER, session_id="cas")
    assert task == before and not task["verified_facts"]


def test_batch_factory_ignores_unknown_components_without_trusting_their_facts():
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    observations: dict[str, dict[str, Any] | None] = {"unsupported": {
        "component": "unsupported", "status": "ready", "enabled": True,
        "probe_mode": "configuration_only", "network_accessed": False,
    }}
    kwargs = dict(owner=OWNER, session_id="cas", tool="diagnostics.configuration", call_id="batch-call")
    assert observation_patch(task, observations=observations, **kwargs) is None
    unsupported = observations["unsupported"]
    assert unsupported is not None
    observations["tmdb"] = {**unsupported, "component": "tmdb"}
    patch = observation_patch(task, observations=observations, **kwargs)
    assert patch is not None
    assert set(patch.payload["observations"]) == {"tmdb"}
    saved = reduce_task(task, patch, owner=OWNER, session_id="cas")
    assert [f["component"] for f in saved["verified_facts"]] == ["tmdb"]
    assert saved["status"] == "partial"


@pytest.mark.parametrize("bad_row", [
    {"name": "qbittorrent", "status": "connected", "enabled": True},
    {"name": "qbittorrent", "status": "ready", "enabled": 1},
    {"name": "unknown", "status": "ready", "enabled": True},
])
def test_diagnosis_observer_does_not_trust_invalid_or_unknown_component_rows(bad_row):
    from app.agent.kernel.task_state import configuration_observations
    from app.agent.models import ToolResult

    result = ToolResult(ok=False, status="attention", summary="configuration fixture", data={
        "probe_mode": "configuration_only", "network_accessed": False,
        "components": [{"name": "tmdb", "status": "ready", "enabled": True}, bad_row],
    })
    observations = configuration_observations(result.to_dict(), {})
    assert set(observations) == {"tmdb", "qbittorrent"}
    assert observations["tmdb"] is not None
    assert observations["tmdb"]["status"] == "ready" and observations["qbittorrent"] is None
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    patch = observation_patch(task, owner=OWNER, session_id="cas", tool="diagnostics.configuration", call_id="batch-call", observations=observations)
    assert patch is not None
    saved = reduce_task(task, patch, owner=OWNER, session_id="cas")
    assert saved["status"] == "blocked" and len(saved["verified_facts"]) == 1
    assert all(o["state"] == "pending" and not o["evidence_ids"] for o in saved["obligations"])


def test_batch_sqlite_atomicity_cold_read_and_reference_is_not_task_evidence(task_store):
    async def run():
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id="cas", request_id="request")
        initial = await task_store.commit(lease, task_patch=control(declaration()))
        task = initial.metadata["task"]
        bad = observe_batch(task)
        assert bad is not None
        bad.payload["observations"]["qbittorrent"]["scope"]["owner"] = "another"
        with pytest.raises(ValueError, match="invalid observation evidence"):
            await task_store.commit(lease, task_patch=bad)
        other = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        assert (await other.load(owner=OWNER, session_id="cas")).to_payload() == initial.to_payload()
        patch = observe_batch(task)
        committed = await task_store.commit(lease, task_patch=patch)
        cold = await other.load(owner=OWNER, session_id="cas")
        assert cold.to_payload() == committed.to_payload()
        assert cold.metadata["task"]["revision"] == task["revision"] + 1
        assert len(cold.metadata["task"]["verified_facts"]) == 2
        with pytest.raises(TaskConflict):
            await other.commit(lease, task_patch=patch)
        with pytest.raises(ValueError, match="invalid evidence scope"):
            await other.commit(lease, task_patch=command(cold.metadata["task"], "checkpoint", evidence_ids=["ref_diagnosis_result"], blockers=[]))
        assert (await other.load(owner=OWNER, session_id="cas")).to_payload() == cold.to_payload()
        task = cold.metadata["task"]
        await other.commit(lease, task_patch=command(task, "checkpoint", evidence_ids=[f["evidence_id"] for f in task["verified_facts"]], blockers=[]))
        final = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id="cas")
        assert final.metadata["task"]["status"] == "complete"

    asyncio.run(run())



@pytest.mark.parametrize("invalid_header", [{"network_accessed": True}, {"network_accessed": 0}, {"probe_mode": "connectivity"}])
def test_diagnosis_network_claims_are_not_configuration_evidence(invalid_header):
    from app.agent.kernel.task_state import configuration_observations
    from app.agent.models import ToolResult

    result = ToolResult(ok=False, status="attention", summary="configuration fixture", data={
        "probe_mode": "configuration_only", "network_accessed": False,
        "components": [{"name": component, "status": "ready", "enabled": True} for component in ("tmdb", "qbittorrent")],
        **invalid_header,
    })
    assert configuration_observations(result.to_dict(), {}) == {"tmdb": None, "qbittorrent": None}


@pytest.mark.parametrize("terminal", ["complete", "cancelled"])
def test_terminal_task_can_be_replaced_without_model_supplied_versions(terminal):
    def apply(task, patch):
        return reduce_task(task, patch, owner=OWNER, session_id="cas")

    task = apply(None, control(declaration()))
    task = apply(task, observe_batch(task))
    old_evidence = [fact["evidence_id"] for fact in task["verified_facts"]]
    task = apply(task, command(task, "checkpoint", evidence_ids=old_evidence, blockers=[]))
    if terminal == "cancelled":
        task = apply(task, command(task, "cancel", source=GOAL))
    snapshot = deepcopy(task)
    assert observe(task) is None
    assert observe(task, success=False) is None
    assert observe_batch(task, failed=("qbittorrent",)) is None
    assert task == snapshot  # unrelated reads cannot reopen a terminal checklist

    patch = control(declaration(), task)
    assert patch.expected == identity(task) and "expected" not in patch.payload
    fresh = apply(task, patch)
    assert fresh["task_id"] != task["task_id"]
    assert (fresh["epoch"], fresh["revision"], fresh["status"]) == (1, 1, "partial")
    assert fresh["verified_facts"] == fresh["blockers"] == []
    with pytest.raises(ValueError, match="evidence scope"):
        apply(fresh, command(fresh, "checkpoint", evidence_ids=old_evidence, blockers=[]))
    with pytest.raises(TaskConflict):
        apply(fresh, patch)  # the same model observation cannot replace a newer task


@pytest.mark.parametrize("blocked", [False, True])
def test_new_task_cannot_replace_unfinished_work(blocked):
    task = reduce_task(None, control(declaration()), owner=OWNER, session_id="cas")
    if blocked:
        task = reduce_task(task, cast(TaskPatch, observe(task, success=False)), owner=OWNER, session_id="cas")
    with pytest.raises(TaskConflict, match="finish or cancel"):
        reduce_task(task, control(declaration(), task), owner=OWNER, session_id="cas")
