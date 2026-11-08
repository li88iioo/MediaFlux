"""退役后旧计划读取/恢复/隐私边界；SQL夹具不是可执行写协议。"""

from __future__ import annotations

import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.errors import AgentToolError
from app.modules.process_lock import CrossProcessLock
from app.repositories.agent_provider_plans import (
    _principal,
    get_provider_plan,
    invalidate_provider_plans_for_owner,
    invalidate_provider_plans_for_session,
)


def seed_plan(*, owner="owner-a", session_id="session-a", status="prepared"):
    ref = "PP-" + secrets.token_hex(12).upper()
    owner_hash, session_hash = _principal(owner, session_id)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_provider_plans (plan_id,owner_digest,session_digest,provider,profile_ref,operation,risk,status,arguments_json,target_snapshot_json,context_fingerprint,result_json,summary,error_code,attempts,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ref,
                owner_hash,
                session_hash,
                "media",
                "configured:jellyfin",
                "media.item.refresh",
                "low_write",
                status,
                '{"item_ref":"old-ref"}',
                '{"item_ref":{"id":"movie-1","name":"历史目标"}}',
                "old-fingerprint",
                '{"accepted":true}' if status == "succeeded" else "{}",
                "历史摘要",
                "",
                int(status != "prepared"),
                time.time() + 900,
                db.now(),
                db.now(),
            ),
        )
    return ref


def _create_running_provider_batch(count):
    refs = []
    for index in range(count):
        ref = seed_plan(
            owner="batch-owner", session_id=f"batch-session-{index}", status="running"
        )
        _insert_provider_executing_audit(ref, f"batch-confirmation-{index}")
        refs.append(ref)
    return refs


@pytest.fixture
def isolated_provider_db(tmp_path: Path):
    previous_path = db.DB_PATH
    previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "provider-plans.db", test_mode=True)
    db.init_db()
    try:
        yield
    finally:
        db.configure_database(previous_path, test_mode=previous_test_mode)


def test_provider_owner_reset_treats_missing_plan_table_as_empty(
    tmp_path: Path,
) -> None:
    previous_path = db.DB_PATH
    previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "empty-provider-plans.db", test_mode=True)
    try:
        assert invalidate_provider_plans_for_owner(owner="owner-a") == {
            "scrubbed_running": 0,
            "deleted": 0,
        }
    finally:
        db.configure_database(previous_path, test_mode=previous_test_mode)


def _insert_provider_executing_audit(plan_ref: str, confirmation_id: str) -> None:
    stamp = db.now()
    db.add_agent_action_history(
        owner_digest="a" * 64,
        tool_name="provider.change.execute",
        risk="write",
        status="executing",
        ok=False,
        summary="Provider 原生写计划执行：执行中",
        safe_details={"plan_ref": plan_ref},
        started_at=stamp,
        finished_at=stamp,
        confirmation_id=confirmation_id,
    )


@pytest.mark.parametrize("count", [0, 1, 32])
def test_provider_recovery_batch_preserves_history_and_is_idempotent(
    isolated_provider_db,
    count,
):
    refs = _create_running_provider_batch(count)
    if refs:
        # 同一计划的多个旧审计，以及旧数据中的小写/空白引用都应归并。
        _insert_provider_executing_audit(f" {refs[0].lower()} ", "batch-duplicate")
    malformed = ["broken-json", "[]", "null", '{"plan_ref":"PP-UNRELATED"}']
    for index, payload in enumerate(malformed):
        confirmation = f"batch-unrelated-{index}"
        _insert_provider_executing_audit("PP-UNRELATED", confirmation)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_action_history SET safe_details=? WHERE confirmation_id=?",
                (payload, confirmation),
            )
    with db.get_conn() as conn:
        if refs:
            conn.execute(
                "UPDATE agent_provider_plans SET context_fingerprint='' WHERE plan_id=?",
                (refs[-1],),
            )
        untouched = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM agent_action_history WHERE confirmation_id LIKE 'batch-unrelated-%' ORDER BY id"
            )
        ]
        assert (
            db.recover_interrupted_provider_plans(conn, timestamp="2026-09-12 00:00:00")
            == count
        )
        plans = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM agent_provider_plans ORDER BY plan_id"
            )
        ]
        assert len(plans) == max(0, count - 1)
        assert all(row["status"] == "outcome_unknown" for row in plans)
        history = [
            dict(row)
            for row in conn.execute("SELECT * FROM agent_action_history ORDER BY id")
        ]
        changed = [
            row
            for row in history
            if not row["confirmation_id"].startswith("batch-unrelated-")
        ]
        assert len(changed) == count + bool(refs)
        assert all(
            (
                row["status"],
                row["ok"],
                row["error_code"],
                row["finished_at"],
                row["elapsed_ms"],
            )
            == ("outcome_unknown", 0, "execution_interrupted", "2026-09-12 00:00:00", 0)
            for row in changed
        )
        assert [
            row
            for row in history
            if row["confirmation_id"].startswith("batch-unrelated-")
        ] == untouched
        assert db.recover_interrupted_provider_plans(conn, timestamp="later") == 0
        assert [
            dict(row)
            for row in conn.execute("SELECT * FROM agent_action_history ORDER BY id")
        ] == history
        assert [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM agent_provider_plans ORDER BY plan_id"
            )
        ] == plans


def test_provider_recovery_rolls_back_plans_when_history_update_fails(
    isolated_provider_db,
):
    refs = _create_running_provider_batch(2)
    with db.get_conn() as conn:
        conn.execute("""CREATE TRIGGER reject_recovered_audit BEFORE UPDATE ON agent_action_history
            BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END""")
    with (
        pytest.raises(sqlite3.IntegrityError, match="injected audit failure"),
        db.get_conn() as conn,
    ):
        conn.execute("BEGIN IMMEDIATE")
        db.recover_interrupted_provider_plans(conn, timestamp="2026-09-12 00:00:00")
    with db.get_conn() as conn:
        assert {
            row["plan_id"]
            for row in conn.execute(
                "SELECT * FROM agent_provider_plans WHERE status='running'"
            )
        } == set(refs)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_action_history WHERE status='executing'"
            ).fetchone()[0]
            == 2
        )


def test_provider_recovery_scans_executing_history_once_per_batch(isolated_provider_db):
    _create_running_provider_batch(32)
    queries = []
    with db.get_conn() as conn:
        conn.set_trace_callback(queries.append)
        assert (
            db.recover_interrupted_provider_plans(conn, timestamp="2026-09-12 00:00:00")
            == 32
        )
        conn.set_trace_callback(None)
    scans = [
        query
        for query in queries
        if query.startswith("SELECT id,safe_details FROM agent_action_history")
    ]
    assert len(scans) == 1


def test_old_prepared_retired_terminal_receipts_unchanged_and_owner_scoped(
    isolated_provider_db,
):
    prepared = seed_plan()
    terminal = [
        seed_plan(status=status)
        for status in ("succeeded", "failed", "outcome_unknown", "stale")
    ]
    with db.get_conn() as conn:
        before = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_provider_plans WHERE status<>'prepared' ORDER BY plan_id"
            )
        ]
    with pytest.raises(AgentToolError) as failure:
        get_provider_plan(owner="other", session_id="session-a", plan_ref=prepared)
    assert failure.value.code == "plan_not_found"
    db.init_db()
    row = get_provider_plan(owner="owner-a", session_id="session-a", plan_ref=prepared)
    assert row["status"] == "stale" and row["error_code"] == "provider_retired"
    assert row["arguments"] == {"item_ref": "old-ref"}
    assert row["target_snapshot"]["item_ref"]["id"] == "movie-1"
    with db.get_conn() as conn:
        assert [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_provider_plans WHERE plan_id<>? ORDER BY plan_id",
                (prepared,),
            )
        ] == before
    for ref in terminal:
        assert (
            get_provider_plan(owner="owner-a", session_id="session-a", plan_ref=ref)[
                "plan_ref"
            ]
            == ref
        )


def test_running_old_writer_lease_then_recovery_does_not_replay(isolated_provider_db):
    ref = seed_plan(status="running")
    _insert_provider_executing_audit(ref, "old-confirmation")
    lock = CrossProcessLock(
        "agent-provider-write", directory=db.resolve_db_path().parent
    )
    assert lock.acquire(blocking=False)
    try:
        db.init_db()
        assert (
            get_provider_plan(owner="owner-a", session_id="session-a", plan_ref=ref)[
                "status"
            ]
            == "running"
        )
        with db.get_conn() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM agent_action_history WHERE confirmation_id='old-confirmation'"
                ).fetchone()[0]
                == "executing"
            )
    finally:
        lock.release()
    db.init_db()
    restored = get_provider_plan(owner="owner-a", session_id="session-a", plan_ref=ref)
    assert (
        restored["status"] == "outcome_unknown"
        and restored["error_code"] == "execution_interrupted"
    )
    assert restored["attempts"] == 1 and restored["arguments"] == {
        "item_ref": "old-ref"
    }
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT status FROM agent_action_history WHERE confirmation_id='old-confirmation'"
            ).fetchone()[0]
            == "outcome_unknown"
        )


@pytest.mark.parametrize("scope", ["owner", "session"])
def test_legacy_privacy_scope_scrubs_running_and_keeps_other_principal(
    isolated_provider_db, scope
):
    first = seed_plan()
    running = seed_plan(status="running")
    same_owner = seed_plan(session_id="session-b")
    other = seed_plan(owner="owner-b")
    clear = (
        invalidate_provider_plans_for_owner
        if scope == "owner"
        else invalidate_provider_plans_for_session
    )
    result = clear(
        owner="owner-a", **({"session_id": "session-a"} if scope == "session" else {})
    )
    assert result == {"scrubbed_running": 1, "deleted": 2 if scope == "owner" else 1}
    with db.get_conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM agent_provider_plans WHERE plan_id=?", (first,)
        ).fetchone()
        row = dict(
            conn.execute(
                "SELECT * FROM agent_provider_plans WHERE plan_id=?", (running,)
            ).fetchone()
        )
        assert (
            row["status"] == "running"
            and row["arguments_json"]
            == row["target_snapshot_json"]
            == row["result_json"]
            == "{}"
        )
        assert not row["context_fingerprint"] and not row["operation"]
        assert bool(
            conn.execute(
                "SELECT 1 FROM agent_provider_plans WHERE plan_id=?", (same_owner,)
            ).fetchone()
        ) == (scope == "session")
    assert (
        get_provider_plan(owner="owner-b", session_id="session-a", plan_ref=other)[
            "plan_ref"
        ]
        == other
    )
    db.init_db()
    with db.get_conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM agent_provider_plans WHERE plan_id=?", (running,)
        ).fetchone()


@pytest.mark.parametrize("first", ["purge", "recover"])
def test_legacy_recovery_and_privacy_purge_serialize_without_resurrecting_facts(
    isolated_provider_db, first
):
    from contextlib import contextmanager
    from threading import current_thread

    ref = seed_plan(status="running")
    _insert_provider_executing_audit(ref, "privacy-confirmation")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_action_history SET owner_digest=?",
            (db.media_owner_digest("owner-a"),),
        )
    entered, release, second_started = Event(), Event(), Event()
    original = db.get_conn

    @contextmanager
    def gated():
        with original() as conn:
            yield conn
            if current_thread().name.endswith("_0"):
                entered.set()
                assert release.wait(5)

    def recover():
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            db.recover_interrupted_provider_plans(conn, timestamp=db.now())

    def purge():
        db.purge_agent_subject_data(owner="owner-a")

    actions = {"purge": purge, "recover": recover}

    def second():
        second_started.set()
        actions["recover" if first == "purge" else "purge"]()

    with patch.object(db, "get_conn", gated), ThreadPoolExecutor(2) as pool:
        one = pool.submit(actions[first])
        try:
            assert entered.wait(5)
            two = pool.submit(second)
            assert second_started.wait(5)
            assert not two.done()
        finally:
            release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    with db.get_conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM agent_provider_plans WHERE plan_id=?", (ref,)
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM agent_action_history WHERE confirmation_id='privacy-confirmation'"
        ).fetchone()
