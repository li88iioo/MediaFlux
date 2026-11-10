"""业务前像与步骤同存，回退在一个事务内恢复身份/位置/成员目标。"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest
from app import database as db
from app import database_migrations
from app.repositories import organize_history
from tests.support import isolated_test_database


def group():
    lid = db.add_organize_log("guangya", "incoming", "First/E1.mkv", "video", "success", "1",
        source_dir_id="source", original_parent_id="incoming", original_name="Original.mkv",
        current_parent_id="old-season", current_name="First.S01E01.mkv", target_parent_id="old-season",
        media_type="tv", title="First", year="2025", season=1, episode=1, legacy_incomplete=False)
    db.add_organize_log_items(lid, [{"file_id": "video", "role": "video", "original_parent_id": "incoming",
        "original_name": "Original.mkv", "current_parent_id": "old-season", "current_name": "First.S01E01.mkv",
        "target_parent_id": "old-season", "target_name": "First.S01E01.mkv", "size": 100, "etag": "e", "status": "success"}])
    return lid


def test_v26_upgrade_preserves_legacy_steps_without_inventing_before_state():
    with isolated_test_database():
        lid = group()
        step = organize_history.add_organize_operation_step(lid, "old", 1, "move_rename", file_id="video", status="success")
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE organize_operation_steps DROP COLUMN state_before_json")
            conn.execute("PRAGMA user_version=26")
        db.init_db()
        old = organize_history.list_organize_operation_steps(lid)[0]
        assert old["id"] == step and old["state_before_json"] == ""
        with db.get_conn() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v27_migration_rolls_back_ddl_and_version():
    with isolated_test_database():
        group()
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE organize_operation_steps DROP COLUMN state_before_json")
            conn.execute("PRAGMA user_version=26")
        migrate = database_migrations._SCHEMA_MIGRATIONS[26]
        def fail(conn):
            migrate(conn)
            raise RuntimeError("isolated migration failure")
        with patch.dict(database_migrations._SCHEMA_MIGRATIONS, {26: fail}), pytest.raises(RuntimeError):
            db.init_db()
        with db.get_conn() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 26
            assert "state_before_json" not in {r[1] for r in conn.execute("PRAGMA table_info(organize_operation_steps)")}
        db.init_db()


def test_snapshot_persists_with_step_and_restores_all_business_fields():
    with isolated_test_database():
        lid = group()
        db.update_organize_log(lid, release_parse_json=json.dumps({"manual_position": {"season": 1, "episode": 1}}))
        before = organize_history.capture_organize_business_snapshot(lid)
        organize_history.add_organize_operation_step(lid, "change", 1, "move_rename", file_id="video", status="success", state_before=before)
        persisted = json.loads(organize_history.list_organize_operation_steps(lid)[0]["state_before_json"])
        assert persisted == before
        db.update_organize_log(lid, tmdb_id="2", title="Second", year="2026", season=2, episode=3,
                              new_path="Second/E3.mkv", target_parent_id="new-season")
        item = db.list_organize_log_items(lid)[0]
        db.update_organize_log_item(item["id"], target_parent_id="new-season", target_name="Second.S02E03.mkv")
        assert organize_history.restore_organize_business_snapshot(lid, persisted, status="reverted", operation_type="revert",
                                                    current_parent_id="old-season", current_name="First.S01E01.mkv", error="")
        actual = organize_history.capture_organize_business_snapshot(lid)
        assert actual == before
        get_organize_log_result = db.get_organize_log(lid)
        assert get_organize_log_result is not None
        assert get_organize_log_result["status"] == "reverted"


def test_member_write_failure_rolls_back_log_identity_and_target_updates():
    with isolated_test_database():
        lid = group()
        before = organize_history.capture_organize_business_snapshot(lid)
        db.update_organize_log(lid, title="Second", tmdb_id="2")
        current = organize_history.capture_organize_business_snapshot(lid)
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER fail_member_restore BEFORE UPDATE ON organize_log_items BEGIN SELECT RAISE(ABORT,'isolated'); END")
        with pytest.raises(sqlite3.IntegrityError):
            organize_history.restore_organize_business_snapshot(lid, before, status="reverted")
        assert organize_history.capture_organize_business_snapshot(lid) == current


def test_snapshot_cannot_restore_changed_members_or_incomplete_identity():
    with isolated_test_database():
        lid = group()
        before = organize_history.capture_organize_business_snapshot(lid)
        incomplete = {**before, "log": {"title": "wrong"}}
        with pytest.raises(ValueError):
            organize_history.restore_organize_business_snapshot(lid, incomplete)
        forged = {**before, "items": [{**before["items"][0], "file_id": "different"}]}
        with pytest.raises(ValueError):
            organize_history.restore_organize_business_snapshot(lid, forged)
        assert organize_history.capture_organize_business_snapshot(lid) == before


def test_business_step_query_does_not_truncate_at_display_limit():
    with isolated_test_database():
        lid = group()
        before = organize_history.capture_organize_business_snapshot(lid)
        organize_history.add_organize_operation_step(lid, "previous", 1, "move_rename", file_id="video", status="success")
        for i in range(305):
            organize_history.add_organize_operation_step(lid, "latest", i + 1, "move_rename", file_id=f"file-{i}", status="success",
                                          state_before=before if i == 0 else None)
        assert len(organize_history.list_organize_operation_steps(lid)) == 300
        actual = organize_history.list_latest_reversible_organize_steps(lid)
        assert len(actual) == 305
        assert {row["operation_token"] for row in actual} == {"latest"}
        assert sum(bool(row["state_before_json"]) for row in actual) == 1


def test_probe_recovery_query_ignores_display_window_and_other_jobs():
    with isolated_test_database():
        lid = group()
        expected = [organize_history.add_organize_operation_step(lid, "probe:7:first", 1, "probe_rename",
                    file_id="video", status="running")]
        expected.append(organize_history.add_organize_operation_step(lid, "probe:7:second", 1, "probe_rename",
                        file_id="video", status="interrupted"))
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO organize_operation_steps(log_id,operation_token,step_index,action,status) VALUES(?,?,?,?,?)",
                [(lid, "newer-events", i, "other", "success") for i in range(1010)],
            )
        organize_history.add_organize_operation_step(lid, "probe:70:first", 1, "probe_rename", status="running")
        organize_history.add_organize_operation_step(lid, "probe:7:third", 1, "probe_rename", status="success")
        organize_history.add_organize_operation_step(lid, "probe:7:fourth", 1, "move_rename", status="running")
        assert not {r['id'] for r in organize_history.list_organize_operation_steps(lid, limit=1000)}.intersection(expected)
        actual = organize_history.list_pending_organize_probe_steps(lid, 7)
        assert [row['id'] for row in actual] == expected[::-1]
        assert organize_history.list_pending_organize_probe_steps(lid + 1, 7) == []
