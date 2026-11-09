"""旧来源约束的真实SQLite升级：实体、CAS修订、下载关联及原子回滚。"""

from __future__ import annotations

import sqlite3

import pytest

from app import database as db
from app import database_migrations, database_schema


@pytest.fixture
def legacy(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 还原 v39 的旧来源约束及尚未增加分享重试计数的真实下载表。
    schema = database_schema._SCHEMA.replace(
        "CHECK(source_tool = 'resources.search_missing')",
        "CHECK(source_tool IN ('library.search_missing_episode_resources','library.search_missing_season_resources'))",
    )
    conn.executescript(schema)
    conn.execute("ALTER TABLE download_requests DROP COLUMN share_retry_count")
    conn.executemany(
        "INSERT INTO download_requests(id,request_key,kind,status,gy_status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'old-created','old-updated')",
        [
            (17, "legacy-magnet", "magnet", "pending", ""),
            (18, "legacy-share-first", "guangya_share", "failed", "failed"),
            (19, "legacy-share-retried", "guangya_share", "failed", "failed"),
        ],
    )
    conn.executemany(
        "INSERT INTO download_log(source,request_id,status,created_at) "
        "VALUES(?,?,'failed','old-created')",
        [("qb", 17), ("guangya_share", 18), ("guangya_share", 19), ("guangya_share", 19)],
    )
    for number, source in enumerate(
        (
            "library.search_missing_episode_resources",
            "library.search_missing_season_resources",
        ),
        1,
    ):
        conn.execute(
            "INSERT INTO agent_missing_media_workflows VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"workflow-{number}",
                f"owner-{number}",
                source,
                "Show",
                "12345",
                1,
                "2026-08-01",
                "verification_pending",
                9,
                "old-created",
                "old-updated",
            ),
        )
        conn.execute(
            "INSERT INTO agent_missing_media_workflow_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                number,
                f"item-{number}",
                f"workflow-{number}",
                "Show",
                "12345",
                1,
                number,
                "2026-08-01",
                "verification_pending",
                "Show.Pack",
                "qb",
                17,
                "",
                7,
                "old-created",
                "old-updated",
            ),
        )
    conn.execute(
        "UPDATE sqlite_sequence SET seq=1000 WHERE name='agent_missing_media_workflow_items'"
    )
    conn.execute("PRAGMA user_version=39")
    conn.commit()
    monkeypatch.setattr(db, "_create_pre_migration_backup", lambda *a, **k: None)
    yield conn
    conn.close()


def test_v39_upgrade_preserves_all_entities_and_foreign_keys(legacy):
    assert "share_retry_count" not in {
        row[1] for row in legacy.execute("PRAGMA table_info(download_requests)")
    }
    requests = [dict(r) for r in legacy.execute("SELECT * FROM download_requests ORDER BY id")]
    logs = [tuple(r) for r in legacy.execute("SELECT * FROM download_log ORDER BY id")]
    parents = [
        dict(r)
        for r in legacy.execute(
            "SELECT * FROM agent_missing_media_workflows ORDER BY workflow_id"
        )
    ]
    members = [
        dict(r)
        for r in legacy.execute(
            "SELECT * FROM agent_missing_media_workflow_items ORDER BY id"
        )
    ]
    assert db._prepare_schema_migration(legacy, database_existed=True) == 39
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert [dict(r) for r in legacy.execute("SELECT * FROM download_requests ORDER BY id")] == [
        {**r, "share_retry_count": {17: 0, 18: 0, 19: 1}[r["id"]]} for r in requests
    ]
    assert [tuple(r) for r in legacy.execute("SELECT * FROM download_log ORDER BY id")] == logs
    assert [
        dict(r)
        for r in legacy.execute(
            "SELECT * FROM agent_missing_media_workflows ORDER BY workflow_id"
        )
    ] == [{**r, "source_tool": "resources.search_missing"} for r in parents]
    assert [
        dict(r)
        for r in legacy.execute(
            "SELECT * FROM agent_missing_media_workflow_items ORDER BY id"
        )
    ] == members
    assert (
        legacy.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='agent_missing_media_workflow_items'"
        ).fetchone()[0]
        == 1000
    )
    assert legacy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not legacy.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "UPDATE agent_missing_media_workflows SET source_tool='library.search_missing_episode_resources'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "UPDATE agent_missing_media_workflow_items SET workflow_id='missing-parent'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "UPDATE agent_missing_media_workflow_items SET download_request_id=999"
        )
    legacy.execute(
        "DELETE FROM agent_missing_media_workflows WHERE workflow_id='workflow-1'"
    )
    assert (
        legacy.execute(
            "SELECT COUNT(*) FROM agent_missing_media_workflow_items"
        ).fetchone()[0]
        == 1
    )
    database_migrations._migrate_missing_resource_source_v40(legacy)
    assert (
        legacy.execute("SELECT COUNT(*) FROM agent_missing_media_workflows").fetchone()[
            0
        ]
        == 1
    )


@pytest.mark.parametrize("migration_version", [39, 40])
def test_upgrade_chain_rolls_back_parent_child_source_and_version(
    legacy, monkeypatch, migration_version
):
    before = {
        name: [tuple(r) for r in legacy.execute(f"SELECT * FROM {name}")]
        for name in (
            "download_requests",
            "download_log",
            "agent_missing_media_workflows",
            "agent_missing_media_workflow_items",
            "sqlite_sequence",
        )
    }
    request_columns = list(legacy.execute("PRAGMA table_info(download_requests)"))
    migration = database_migrations._SCHEMA_MIGRATIONS[migration_version]

    def fail(conn):
        migration(conn)
        raise RuntimeError("interrupted upgrade")

    monkeypatch.setitem(database_migrations._SCHEMA_MIGRATIONS, migration_version, fail)
    with pytest.raises(RuntimeError, match="interrupted upgrade"):
        db._prepare_schema_migration(legacy, database_existed=True)
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 39
    assert list(legacy.execute("PRAGMA table_info(download_requests)")) == request_columns
    for name, rows in before.items():
        assert [tuple(r) for r in legacy.execute(f"SELECT * FROM {name}")] == rows
    assert not legacy.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%_v40'"
    ).fetchall()
    assert not legacy.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("version", [0, 39])
def test_real_file_database_startup_migrates_old_rows_and_keeps_submission(
    legacy, version
):
    from tests.support import isolated_test_database

    parent = "agent_missing_media_workflows"
    child = "agent_missing_media_workflow_items"
    parents = [
        tuple(r) for r in legacy.execute(f"SELECT * FROM {parent} ORDER BY workflow_id")
    ]
    members = [tuple(r) for r in legacy.execute(f"SELECT * FROM {child} ORDER BY id")]
    with isolated_test_database():
        request_id, _ = db.create_download_request(
            "source-migration-real-db", "magnet", origin="agent"
        )
        members = [(*r[:11], request_id, *r[12:]) for r in members]
        with db.get_conn() as conn:
            conn.execute(f"DROP TABLE {child}")
            conn.execute(f"DROP TABLE {parent}")
            for name in (parent, child):
                conn.execute(
                    legacy.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (name,),
                    ).fetchone()[0]
                )
            conn.executemany(
                f"INSERT INTO {parent} VALUES({','.join('?' for _ in parents[0])})",
                parents,
            )
            conn.executemany(
                f"INSERT INTO {child} VALUES({','.join('?' for _ in members[0])})",
                members,
            )
            conn.execute(f"PRAGMA user_version={version}")
        db.init_db()
        with db.get_conn() as conn:
            assert (
                conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            )
            current = [
                tuple(r)
                for r in conn.execute(f"SELECT * FROM {parent} ORDER BY workflow_id")
            ]
            assert current == [
                (*r[:2], "resources.search_missing", *r[3:]) for r in parents
            ]
            assert [
                tuple(r) for r in conn.execute(f"SELECT * FROM {child} ORDER BY id")
            ] == members
            assert (
                conn.execute(
                    "SELECT id FROM download_requests WHERE id=?", (request_id,)
                ).fetchone()[0]
                == request_id
            )
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
