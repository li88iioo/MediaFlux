"""历史刷新 outbox 的事件令牌与原目标迁移。"""
import json

from app import database as db
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories import media_refresh_queue as queue
from app.repositories import strm as strm_repo
from tests.support import IsolatedDatabaseTestCase, assert_present, media_refresh_providers

class DurableHandoffMigrationTests(IsolatedDatabaseTestCase):
    def test_v24_upgrade_preserves_outbox_and_initializes_handoff(self):
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE strm_refresh_outbox DROP COLUMN event_token")
            conn.execute("ALTER TABLE strm_refresh_outbox DROP COLUMN refresh_intent_json")
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
        self.assertTrue(all(entry["refresh_targets"] is None for entry in entries))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(organize_probe_queue)")}
            self.assertEqual(columns["pending_strm_changes_json"][4], "'[]'")
        db.init_db()
        self.assertEqual(strm_repo.list_strm_refresh_entries(), entries)
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(entries), 2)

    def test_v41_unidentified_refresh_keeps_original_run_and_settles_without_rebinding(self):
        run_id = db.add_task_run("strm_sync", "manual")
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE strm_refresh_outbox DROP COLUMN refresh_intent_json")
            conn.execute(
                "INSERT INTO strm_refresh_outbox(path,event_token,allow_emby,created_at,updated_at) VALUES(?,?,0,'old','old')",
                ("/media/legacy.strm", "legacy"),
            )
            conn.execute("PRAGMA user_version=41")
        db.init_db()
        entries = strm_repo.list_strm_refresh_entries()
        self.assertIsNone(entries[0]["refresh_targets"])
        self.assertEqual(entries[0]["event_token"], "legacy")
        # 旧运行可能只登记了 outbox，尚未将刷新移交到统一队列。
        queue.register_media_refresh_handoff(entries, run_id=run_id, completion={"strm_status": "完成"})
        db.finish_task_run(run_id, "success", result="{}")
        with media_refresh_providers("jellyfin"):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)
        self.assertEqual(queue.claim_due_media_refreshes(owner="after-upgrade", force=True), [])
        run = assert_present(db.get_task_run(run_id))
        result = json.loads(run["result"])["media_refresh_results"]
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["pending"])
        self.assertIn("未记录原媒体服务器", result["events"]["legacy"]["providers"]["媒体库"]["error"])
