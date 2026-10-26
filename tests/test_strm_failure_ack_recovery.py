"""成功文件的失败账本确认闭环；临时 SQLite / 假云盘，禁止外网。"""
from __future__ import annotations

import tests  # noqa: F401  # 必须在 app 前初始化隔离运行目录。

from contextlib import ExitStack
from dataclasses import replace
import json
import threading
from typing import Any, cast
from unittest.mock import patch

import pytest

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import strm
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient
from tests.test_strm_refresh_closure import _run, _tree


@pytest.fixture(autouse=True)
def isolated_offline_database():
    with isolated_test_database(), ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("network forbidden")))
        yield


def reject_ack():
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER reject_failure_ack BEFORE UPDATE OF status ON strm_failures "
            "WHEN NEW.status='resolved' "
            "BEGIN SELECT RAISE(ABORT, 'isolated ledger ACK unavailable'); END"
        )


def restore_ack_and_restart():
    with db.get_conn() as conn:
        conn.execute("DROP TRIGGER reject_failure_ack")
    db.init_db()
    make_targets_due()


def make_targets_due():
    with db.get_conn() as conn:
        conn.execute("UPDATE strm_change_queue SET next_attempt_at=? WHERE state='queued'", (db.now(),))


def last_status():
    run = db.get_last_task_run("strm_sync")
    assert run is not None
    return run["status"]


def seed_failed_video(root):
    with patch.object(strm, "generate_strm", side_effect=OSError("isolated write failure")):
        failed = _run(root, enabled=False)
    assert failed["strm_partial"] and failed["stats"]["failed"] == 1
    assert len(db.list_strm_failures(status="open")) == 1


@pytest.mark.parametrize("action", ["remove", "upsert"])
@pytest.mark.parametrize("arrival", ["before_ack_retry", "after_ack_retry"])
def test_ack_revalidation_never_overrides_business_change(tmp_path, action, arrival):
    seed_failed_video(tmp_path)
    tree = _tree()
    reject_ack()
    at_ack, submitted = threading.Event(), threading.Event()
    errors = []

    def publish_change():
        try:
            assert at_ack.wait(5)
            if action == "remove":
                tree["d"] = []
                change = {"source_id": "s", "kind": "video", "action": "remove", "file_id": "v"}
            else:
                tree["d"] = [replace(tree["d"][0], name="Latest.mkv", etag="new")]
                change = {"source_id": "s", "kind": "video", "action": "upsert", "file_id": "v",
                          "parent_id": "d", "name": "Latest.mkv", "etag": "new", "size": 100, "rel_dir": "Film"}
            assert STRMScheduler()._persist_change_targets([change])
        except BaseException as exc:
            errors.append(exc)
        finally:
            submitted.set()

    resolve = db.resolve_strm_failures_for_items

    def pause_ack(*args, **kwargs):
        if arrival == "before_ack_retry":
            at_ack.set()
            assert submitted.wait(5)
        return resolve(*args, **kwargs)

    producer = threading.Thread(target=publish_change)
    producer.start()
    try:
        with patch.object(db, "resolve_strm_failures_for_items", side_effect=pause_ack):
            first = _run(tmp_path, enabled=False, tree=tree)
        assert first["stats"]["failure_ledger_failed"] == 1
    finally:
        at_ack.set()
        producer.join(5)
    assert not producer.is_alive() and not errors
    old_path = next(tmp_path.rglob("*.strm"))
    restore_ack_and_restart()
    retry = _run(tmp_path, mode="fast", enabled=False, tree=tree, queue_only=True)
    assert not retry["partial"], retry
    assert db.count_pending_strm_change_targets() == 0
    assert not old_path.exists()
    rows = db.list_strm_index("guangya:s")
    if action == "remove":
        assert rows == []
    else:
        assert len(rows) == 1 and rows[0]["filename"] == "Latest.mkv"
        assert rows[0]["etag"] == "new"


@pytest.mark.parametrize("mode", ["full", "fast"])
def test_ack_sql_failure_is_partial_and_restarts_from_durable_target_without_rewrite(tmp_path, mode):
    seed_failed_video(tmp_path)
    reject_ack()
    result = _run(tmp_path, mode=mode, enabled=False)
    assert result["ok"] and result["strm_partial"], result
    assert last_status() == "partial"
    assert result["source_runtime"][0]["status"] == "partial"
    assert result["stats"]["generated"] == 1
    assert result["stats"]["failed"] == result["stats"]["metadata_failed"] == 0
    assert result["stats"]["failure_ledger_failed"] == 1
    assert not result["fallback_used"], result
    assert len(db.list_strm_failures(status="open")) == 1
    assert db.count_pending_strm_change_targets() == 1
    target = next(tmp_path.rglob("*.strm"))
    before = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino)
    event = STRMScheduler._build_success_event(
        result["stats"], result["media_refresh"], 0.0, "manual", result["sources"], str(tmp_path),
    )
    assert event.title == "⚠️ STRM 同步部分完成"
    assert "失败账本" in str(event.lines)
    restore_ack_and_restart()
    scheduler = STRMScheduler()
    with patch.object(scheduler, "_schedule_persisted_change_queue") as schedule:
        scheduler._recover_change_queue()
    schedule.assert_called_once()
    # 没有调用者传入的变化：仅消费真实持久目标，经 scheduler 原入口恢复。
    with patch.object(strm, "_install_video_candidate", side_effect=AssertionError("must reuse installed file")) as install:
        retry = _run(tmp_path, mode="fast", enabled=False, queue_only=True)
    install.assert_not_called()
    assert retry["ok"] and not retry["partial"], retry
    assert last_status() == "success"
    assert retry["source_runtime"][0]["status"] == "completed"
    assert retry["stats"]["generated"] == retry["stats"]["failed"] == 0
    assert retry["stats"]["skipped"] == 1
    assert db.list_strm_failures(status="open") == []
    assert db.count_pending_strm_change_targets() == 0
    assert (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino) == before


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize("later", ["failure", "retry", "recreated"])
def test_late_ack_cannot_resolve_new_failure_in_same_timestamp(tmp_path, mode, later):
    with patch.object(db, "now", return_value="2026-10-26 00:00:00"):
        seed_failed_video(tmp_path)
        previous = db.list_strm_failures(status="open")[0]
        resolve = db.resolve_strm_failures_for_items

        def newer_failure_then_ack(*args, **kwargs):
            if later == "retry":
                assert db.claim_strm_failures([previous["id"]])
                assert db.release_strm_failure_retry(previous["id"], error="later retry must survive old ACK")
            else:
                if later == "recreated":
                    db.delete_strm_failures(ids=[previous["id"]])
                db.record_strm_failure(
                    source_id="s", source_name="S", file_id="v", parent_id="d",
                    filename="Movie.mkv", action="generate", rel_dir="Film",
                    target_rel_path="Film/Movie.strm", error="later failure must survive old ACK",
                )
            return resolve(*args, **kwargs)

        with patch.object(db, "resolve_strm_failures_for_items", side_effect=newer_failure_then_ack):
            result = _run(tmp_path, mode=mode, enabled=False)
        rows = db.list_strm_failures(status="open")
        assert len(rows) == 1, "旧成功快照错误地确认了后发失败"
        assert (rows[0]["id"] != previous["id"]) == (later == "recreated")
        assert rows[0]["failure_count"] == previous["failure_count"] + int(later == "failure")
        assert rows[0]["retry_count"] == previous["retry_count"] + int(later == "retry")
        assert rows[0]["updated_at"] == previous["updated_at"]
        assert rows[0]["error"] == f"later {'retry' if later == 'retry' else 'failure'} must survive old ACK"
        assert result["strm_partial"] and result["stats"]["failed"] == 0
        assert db.count_pending_strm_change_targets() == 1
    make_targets_due()
    with patch.object(strm, "_install_video_candidate", side_effect=AssertionError("must recheck without rewriting")) as install:
        retry = _run(tmp_path, mode="fast", enabled=False, queue_only=True)
    install.assert_not_called()
    assert retry["ok"] and not retry["partial"]
    assert db.list_strm_failures(status="open") == []
    assert db.count_pending_strm_change_targets() == 0


def test_ack_failure_requeues_claimed_target_and_old_finish_cannot_clear_new_work(tmp_path):
    seed_failed_video(tmp_path)
    db.enqueue_strm_change_targets([{
        "source_id": "s", "kind": "video", "action": "upsert", "file_id": "v",
        "parent_id": "d", "name": "Movie.mkv", "rel_dir": "Film",
    }])
    reject_ack()
    enqueue = db.enqueue_strm_change_targets
    old_claim = {}

    def enqueue_while_claimed(*args, **kwargs):
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM strm_change_queue").fetchone()
        assert row is not None
        before = dict(row)
        assert before["state"] == "running"
        assert before["lease_owner"] and before["lease_generation"] > 0
        old_claim.update(before)
        count = enqueue(*args, **kwargs)
        with db.get_conn() as conn:
            dirty = conn.execute("SELECT * FROM strm_change_queue").fetchone()
        assert dirty is not None and dirty["state"] == "dirty"
        assert json.loads(dirty["pending_changes_json"])[0]["etag"] == "e"
        return count

    with patch.object(db, "enqueue_strm_change_targets", side_effect=enqueue_while_claimed) as requeue:
        result = _run(tmp_path, mode="fast", enabled=False, queue_only=True)
    requeue.assert_called_once()
    assert result["strm_partial"] and not result["fallback_used"], result
    assert result["stats"]["failed"] == 0
    with db.get_conn() as conn:
        pending = conn.execute("SELECT * FROM strm_change_queue").fetchone()
    assert pending is not None and pending["state"] == "queued"
    assert json.loads(pending["pending_changes_json"])[0]["etag"] == "e"
    restore_ack_and_restart()
    newer = db.claim_strm_change_targets(owner="newer-ack-replay")
    assert len(newer) == 1 and newer[0]["lease_generation"] > old_claim["lease_generation"]
    assert db.complete_strm_change_target(
        old_claim["id"], expected_owner=old_claim["lease_owner"],
        expected_lease_generation=old_claim["lease_generation"],
    ) == "stale"
    with db.get_conn() as conn:
        running = conn.execute("SELECT * FROM strm_change_queue").fetchone()
    assert running is not None and running["state"] == "running"
    assert json.loads(running["inflight_changes_json"])[0]["etag"] == "e"
    assert db.release_strm_change_targets(newer, reason="test returns lease for real scheduler") == 1
    with patch.object(strm, "_install_video_candidate", side_effect=AssertionError("correct file must not be rewritten")) as install:
        retry = _run(tmp_path, mode="fast", enabled=False, queue_only=True)
    install.assert_not_called()
    assert retry["ok"] and not retry["partial"], retry
    assert db.count_pending_strm_change_targets() == 0
    assert db.list_strm_failures(status="open") == []


@pytest.mark.parametrize("mode", ["full", "fast"])
def test_no_failure_snapshot_never_issues_batch_update(tmp_path, mode):
    with patch.object(db, "resolve_strm_failures_for_items", side_effect=AssertionError("no ledger UPDATE without a snapshot")) as resolve:
        result = _run(tmp_path, mode=mode, enabled=False)
    resolve.assert_not_called()
    assert result["ok"] and not result["partial"], result
    assert result["stats"]["generated"] == 1
    assert result["stats"]["failure_resolve_batches"] == result["stats"]["failure_ledger_failed"] == 0


def test_ack_replay_uses_current_file_and_does_not_duplicate_source_prefix(tmp_path):
    client = cast(GuangYaClient, _TreeClient(_tree()))

    def sync():
        return strm.sync_strm("s", "http://play.invalid", str(tmp_path), client=client, rel_prefix="Source")

    with patch.object(strm, "generate_strm", side_effect=OSError("isolated write failure")):
        assert sync()["failed"] == 1
    # 旧失败的目录/名字已过期，恢复目标必须来自本轮实际校验成功的对象。
    with db.get_conn() as conn:
        conn.execute("UPDATE strm_failures SET rel_dir='OldSource/Obsolete',filename='Old.mkv'")
    reject_ack()
    result = sync()
    assert result["generated"] == 1 and result["failed"] == 0
    restore_ack_and_restart()
    claimed = db.claim_strm_change_targets(owner="ack-prefix-test")
    assert len(claimed) == 1
    changes = claimed[0]["changes"]
    assert changes[0]["rel_dir"] == "Film"
    assert changes[0]["name"] == "Movie.mkv"
    with patch.object(strm, "_install_video_candidate", side_effect=AssertionError("must not duplicate source prefix")) as install:
        retry = strm.sync_strm_incremental(
            "s", changes, "http://play.invalid", str(tmp_path), client=client, rel_prefix="Source",
        )
    install.assert_not_called()
    assert retry["skipped"] == 1 and not retry["fallback_required"]
    assert db.list_strm_failures(status="open") == []
    assert db.complete_strm_change_target(
        claimed[0]["id"], expected_owner=claimed[0]["lease_owner"],
        expected_lease_generation=claimed[0]["lease_generation"],
    ) == "completed"
    assert list(tmp_path.rglob("*.strm")) == [tmp_path / strm.STRM_SUBDIR / "Source" / "Film" / "Movie.strm"]


@pytest.mark.parametrize("incremental", [False, True])
def test_current_metadata_ack_failure_replays_without_download(tmp_path, incremental):
    file = GuangYaFile("meta", "Movie.nfo", False, 8, "etag", "s")
    client = cast(GuangYaClient, _TreeClient({"s": [file]}))
    target = strm._metadata_target(file, "", str(tmp_path))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"metadata")
    db.upsert_strm_index("guangya-meta:s", file.file_id, file.etag, file.size,
                         file.name, str(target), strm._content_fingerprint(target))
    db.record_strm_failure(source_id="s", source_name="S", file_id=file.file_id,
        parent_id="s", filename=file.name, action="metadata", rel_dir="",
        target_rel_path=target.name, error="old metadata failure")
    reject_ack()
    kwargs: dict[str, Any] = {"client": client, "metadata_exts": {"nfo"}}
    if incremental:
        result = strm.sync_strm_incremental(
            "s", [{"source_id": "s", "file_id": file.file_id, "kind": "metadata",
                   "action": "upsert", "parent_id": "s", "name": file.name, "rel_dir": ""}],
            "http://play.invalid", str(tmp_path), **kwargs,
        )
    else:
        result = strm.sync_strm("s", "http://play.invalid", str(tmp_path), **kwargs)
    assert result["metadata_failed"] == result["metadata_generated"] == 0
    assert result["metadata_skipped"] == result["failure_ledger_failed"] == 1
    restore_ack_and_restart()
    claimed = db.claim_strm_change_targets(owner="ack-metadata-test")
    assert len(claimed) == 1
    assert claimed[0]["changes"][0]["kind"] == "metadata"
    with patch.object(strm, "_install_metadata_candidate", side_effect=AssertionError("must reuse metadata")) as install:
        retry = strm.sync_strm_incremental(
            "s", claimed[0]["changes"], "http://play.invalid", str(tmp_path), **kwargs,
        )
    install.assert_not_called()
    assert retry["metadata_skipped"] == 1 and retry["metadata_queued"] == 0
    assert db.list_strm_failures(status="open") == []
    assert target.read_bytes() == b"metadata"


def test_ack_and_replay_enqueue_failure_keeps_manual_pending_and_truthful_feedback(tmp_path):
    seed_failed_video(tmp_path)
    reject_ack()
    with patch.object(db, "enqueue_strm_change_targets", side_effect=OSError("isolated queue store failure")):
        result = _run(tmp_path, enabled=False)
    assert result["ok"] and result["strm_partial"], result
    assert result["stats"]["generated"] == 1 and result["stats"]["failed"] == 0
    assert last_status() == "partial"
    assert len(db.list_strm_failures(status="open")) == 1
    assert db.count_pending_strm_change_targets() == 0
    assert "手动重试" in str(result["stats"]["error_samples"])
    restore_ack_and_restart()
    with patch.object(strm, "_install_video_candidate", side_effect=AssertionError("must keep successful file")) as install:
        retry = _run(tmp_path, enabled=False)
    install.assert_not_called()
    assert retry["ok"] and not retry["partial"]
    assert db.list_strm_failures(status="open") == []
