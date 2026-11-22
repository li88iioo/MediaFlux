"""GCID 任务与明细的事务、全量读取和真实 SQLite 升级。"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import TypedDict

import pytest

from app import database as db, runtime_paths
from app.modules.backup import restore_backup
from tests.support import isolated_test_database


class _TaskArgs(TypedDict):
    operation_token: str
    manifest_digest: str
    target_dir_id: str
    file_count: int
    total_size: int


@pytest.fixture
def task_args() -> Iterator[_TaskArgs]:
    with isolated_test_database():
        yield {
            "operation_token": "manifest-import",
            "manifest_digest": "a" * 64,
            "target_dir_id": "target",
            "file_count": 2,
            "total_size": 8,
        }


def test_create_persists_items_and_replay_preserves_results(task_args: _TaskArgs):
    account_key = "account-a"
    items = [
        {"path": "first.mkv", "size": 3, "gcid": "first", "md5": "b" * 32},
        {"path": "second.mkv", "size": 5, "gcid": "second"},
    ]
    task_id = db.create_gcid_import_task(**task_args, target_account_key=account_key, items=items)
    task = db.get_gcid_import_task(task_id)
    assert task is not None and task["target_account_key"] == account_key
    rows = db.list_gcid_import_items(task_id)
    assert [(row["path"], row["md5"], row["status"]) for row in rows] == [
        ("first.mkv", "b" * 32, "previewed"), ("second.mkv", "", "previewed"),
    ]

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE gcid_import_items SET status='success',remote_file_id='saved' WHERE id=?",
            (rows[0]["id"],),
        )
    original = [dict(row) for row in db.list_gcid_import_items(task_id)]
    assert db.create_gcid_import_task(
        **task_args, target_account_key=account_key, items=[{**items[0], "md5": "c" * 32}],
    ) == task_id
    assert db.create_gcid_import_task(**task_args, target_account_key=account_key, items=[{}]) == task_id
    with pytest.raises(ValueError, match="operation_token 已绑定"):
        db.create_gcid_import_task(**task_args, target_account_key="account-b", items=items)
    assert [dict(row) for row in db.list_gcid_import_items(task_id)] == original


def test_create_rolls_back_task_and_items_if_second_insert_fails(task_args: _TaskArgs):
    items = [
        {"path": "first.mkv", "size": 3, "gcid": "first"},
        {"path": "second.mkv", "size": 5, "gcid": "second"},
    ]
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER fail_second_item BEFORE INSERT ON gcid_import_items "
            "WHEN NEW.path='second.mkv' "
            "BEGIN SELECT RAISE(ABORT, 'injected item failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected item failure"):
        db.create_gcid_import_task(**task_args, items=items)
    assert db.list_gcid_import_tasks() == []
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gcid_import_items").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_second_item")

    task_id = db.create_gcid_import_task(**task_args, items=items)
    assert [row["path"] for row in db.list_gcid_import_items(task_id)] == [
        "first.mkv", "second.mkv",
    ]


def test_replace_and_list_persist_md5_and_keep_more_than_a_thousand_items(task_args: _TaskArgs):
    count = 1001
    task_args["file_count"] = count
    task_args["total_size"] = count
    task_id = db.create_gcid_import_task(**task_args)
    task = db.get_gcid_import_task(task_id)
    assert task is not None and task["target_account_key"] == ""
    items = [
        {"path": f"{number}.mkv", "size": 1, "gcid": f"gcid-{number}", "status": "failed"}
        for number in range(count)
    ]
    items[-1]["md5"] = "c" * 32
    db.replace_gcid_import_items(task_id, items)
    rows = db.list_gcid_import_items(task_id)
    assert [row["path"] for row in rows] == [item["path"] for item in items]
    assert rows[0]["md5"] == ""
    assert rows[-1]["md5"] == "c" * 32
    assert db.list_gcid_import_items(task_id, "failed") == rows
    assert db.list_gcid_import_items(task_id, "failed", limit=1) == rows[:1]


def _paths(root):
    root.mkdir()
    return runtime_paths.RuntimePaths(
        program_dir=root / "program", data_dir=root, config_dir=root,
        cache_dir=root / "cache", log_dir=root / "logs", strm_dir=root / "strm",
        trash_dir=root / "trash",
    )


@pytest.mark.parametrize("legacy_version", [45, 44, 0])
def test_legacy_gcid_rows_survive_startup_backup_and_restore(tmp_path, monkeypatch, legacy_version):
    source = _paths(tmp_path / "source")
    with closing(sqlite3.connect(source.database_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(db._SCHEMA)
        conn.execute("DROP TABLE gcid_cleared_operations")
        if legacy_version < 45:
            conn.execute("ALTER TABLE gcid_import_tasks DROP COLUMN target_account_key")
            conn.execute("ALTER TABLE gcid_import_items DROP COLUMN md5")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO gcid_import_tasks(id,operation_token,manifest_digest,target_dir_id,"
            "status,file_count,total_size,success_count,created_at,updated_at) "
            "VALUES(7,'legacy',?,'target','success',1,9,1,'created','finished')",
            ("a" * 64,),
        )
        conn.execute(
            "INSERT INTO gcid_import_items(id,task_id,path,size,gcid,status,remote_file_id,created_at,updated_at) "
            "VALUES(11,7,'legacy.mkv',9,'legacy-gcid','success','remote','created','finished')"
        )
        conn.execute(f"PRAGMA user_version={legacy_version}")
        legacy_task = dict(conn.execute("SELECT * FROM gcid_import_tasks").fetchone())
        legacy_item = dict(conn.execute("SELECT * FROM gcid_import_items").fetchone())

    monkeypatch.setattr(db, "_configured_test_mode", False)
    monkeypatch.setenv("MEDIAFLUX_TEST_MODE", "0")

    def upgrade_and_check(paths):
        monkeypatch.setattr(db, "DB_PATH", paths.database_path)
        monkeypatch.setattr(runtime_paths, "_configured_paths", paths)
        db.init_db()
        with db.get_conn() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM gcid_cleared_operations").fetchone()[0] == 0
            for table, name in (("gcid_import_tasks", "target_account_key"), ("gcid_import_items", "md5")):
                column = next(row for row in conn.execute(f"PRAGMA table_info({table})") if row["name"] == name)
                assert (column["type"], column["notnull"], column["dflt_value"]) == ("TEXT", 1, "''")
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        task = db.get_gcid_import_task(7)
        assert task is not None and dict(task) == {**legacy_task, "target_account_key": ""}
        assert [dict(row) for row in db.list_gcid_import_items(7)] == [{**legacy_item, "md5": ""}]

    upgrade_and_check(source)
    archive, = source.backup_dir.glob("*.zip")
    restored = _paths(tmp_path / "restored")
    manifest = restore_backup(restored, archive)
    assert manifest.payload["reason"] == f"pre-migration-{legacy_version}-to-{db.SCHEMA_VERSION}"
    assert manifest.payload["database_schema_version"] == legacy_version
    with closing(sqlite3.connect(restored.database_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == legacy_version
    upgrade_and_check(restored)
