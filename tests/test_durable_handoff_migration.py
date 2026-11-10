"""v24 -> v25 保留交接队列并补齐事件令牌；重复初始化不改变事件身份。"""
from app import database as db
from app.repositories import strm as strm_repo
from tests.support import IsolatedDatabaseTestCase


class DurableHandoffMigrationTests(IsolatedDatabaseTestCase):
    def test_v24_upgrade_preserves_outbox_and_initializes_handoff(self):
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE strm_refresh_outbox DROP COLUMN event_token")
            conn.execute("ALTER TABLE organize_probe_queue DROP COLUMN pending_strm_changes_json")
            conn.executemany(
                "INSERT INTO strm_refresh_outbox(path,allow_emby,created_at,updated_at) VALUES(?,?,?,?)",
                [("/media/film", 0, "old", "old"), ("/media/film", 1, "old", "old")],
            )
            conn.execute("PRAGMA user_version=24")
        db.init_db()
        entries = strm_repo.list_strm_refresh_entries()
        self.assertEqual(len(entries), 2)
        tokens = {entry["event_token"] for entry in entries}
        self.assertEqual(len(tokens), 2)
        self.assertTrue(all(len(token) == 32 for token in tokens))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(organize_probe_queue)")}
            self.assertEqual(columns["pending_strm_changes_json"][4], "'[]'")
        db.init_db()
        self.assertEqual(strm_repo.list_strm_refresh_entries(), entries)
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(entries), 2)
