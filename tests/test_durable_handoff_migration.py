"""历史刷新队列、outbox 与原运行回执的升级恢复。"""
import json
from unittest.mock import patch

from app import database as db
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.modules.scheduler import strm_status
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

    def test_v41_acknowledged_outbox_refresh_gets_terminal_receipt_after_restart(self):
        path = "/media/Film.strm"
        run_result = {"stats": {"generated": 1, "changed_strm_paths": [path]},
                      "media_refresh": {"Jellyfin": "queued"}, "refresh_pending": False}
        run_id = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(run_id, "success", result=json.dumps(run_result))
        confirmed = {"status": "success", "pending": False, "providers": {"Jellyfin": "success"}, "events": {}}
        confirmed_result = {**run_result, "media_refresh_results": confirmed}
        confirmed_id = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(confirmed_id, "success", result=json.dumps(confirmed_result))
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE strm_refresh_outbox DROP COLUMN refresh_intent_json")
            conn.execute("PRAGMA user_version=41")
            # v41 已 ACK outbox，只保留按 provider 合并的统一队列，没有上游身份或运行关联。
            db.kv_set_conn(conn, queue._QUEUE_KEY, json.dumps({
                "version": 1, "recent": {},
                "groups": {"legacy": {"provider": "jellyfin", "status": "queued",
                                      "pending_paths": [path], "inflight_paths": [], "due_at": 0,
                                      "allowed_library_ids": [], "library_binding": {}}},
            }))

        db.init_db()
        worker = MediaRefreshCoordinator()
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)
        with media_refresh_providers("jellyfin"), patch.object(worker, "_client_for") as create_client:
            queue.recover_media_refresh_leases()
            for group in queue.claim_due_media_refreshes(owner=worker._owner, force=True):
                worker._process_group(group)
            worker._reconcile_results()
        create_client.assert_not_called()
        self.assertEqual(queue.media_refresh_queue_status()["paths"], 0)
        original = assert_present(db.get_task_run(run_id))
        result = json.loads(original["result"])
        self.assertEqual(original["status"], "success")
        self.assertEqual(result["stats"], run_result["stats"])
        self.assertFalse(result["refresh_pending"])
        self.assertEqual(result["media_refresh"], {"Jellyfin": "failed"})
        self.assertEqual(strm_status(run_id)["data"]["task"]["media_refresh"], {
            "status": "failed", "pending": False, "providers": {"Jellyfin": "failed"},
        })
        event, = result["media_refresh_results"]["events"].values()
        provider = event["providers"]["Jellyfin"]
        self.assertEqual(provider["previous_status"], "queued")
        self.assertIn("请手动刷新", provider["error"])
        self.assertEqual(json.loads(assert_present(db.get_task_run(confirmed_id))["result"]), confirmed_result)

        db.init_db()
        self.assertEqual(queue.recover_media_refresh_leases(), 0)
        worker._reconcile_results()
        self.assertEqual(assert_present(db.get_task_run(run_id))["result"], original["result"])
