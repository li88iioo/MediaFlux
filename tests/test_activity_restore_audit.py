"""真实备份恢复同时保持活动选择身份、旧时间格式和全量成员业务结论。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app import sdk
from app.modules import backup
from app.repositories.activity import timeline_snapshot
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database


class ActivityRestoreAuditTests(unittest.TestCase):
    @staticmethod
    def _paths(database_path):
        root = database_path.parent
        paths = RuntimePaths(
            root / "program",
            root,
            root,
            root / "cache",
            root / "logs",
            root / "strm",
            root / "trash",
        )
        paths.ensure_writable_dirs()
        paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
        return paths

    def _seed(self):
        request, _ = db.create_download_request(
            "history-download", "magnet", title="history-download"
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET created_at='2026-01-01 00:00:00',updated_at='' WHERE id=?",
                (request,),
            )
            conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) "
                "VALUES('guangya','/fixture/old','/fixture/new','history-organize','success','2026-01-07 00:00:00',NULL)"
            )
            source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) "
                "VALUES('history','/fixture','2026-01-01 00:00:00','2026-01-01 00:00:00')"
            ).lastrowid
            task_id = conn.execute(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture','history-old','history-old','completed','2026-01-01 00:00:00','2026-01-09 00:00:00')",
                (source,),
            ).lastrowid
            assert task_id is not None
            self.task = task_id
            conn.executemany(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture',?,?,'completed','2026-01-02 00:00:00','2026-01-02 00:00:00')",
                [(source, f"history-{i}", f"history-{i}") for i in range(30)],
            )
            conn.executemany(
                "INSERT INTO local_media_task_items(task_id,source_path,role,status,created_at,updated_at) "
                "VALUES(?,?,'subtitle',?,'2026-01-01 00:00:00','2026-01-01 00:00:00')",
                [
                    (
                        self.task,
                        f"/fixture/member-{i}",
                        "verified" if i < 149 else "failed",
                    )
                    for i in range(150)
                ],
            )
        self.target = {"kind": "local_media", "id": self.task}

    def _business(self):
        search = sdk.activity.search(query="history", limit=2)
        timeline = timeline_snapshot(self.target["kind"], self.target["id"])
        with db.get_conn() as conn:
            raw = {
                table: [
                    dict(row)
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")
                ]
                for table in (
                    "download_requests",
                    "organize_log",
                    "local_media_tasks",
                    "local_media_task_items",
                )
            }
        return {
            "raw": raw,
            "search": search,
            "selection": {"items": [{"kind": row["kind"], "id": row["id"]} for row in search["items"]]},
            "status": timeline["status"],
            "attention": timeline["data"]["needs_attention"],
            "members": next(
                stage
                for stage in timeline["data"]["stages"]
                if stage["stage"] == "成员处理"
            ),
        }

    def _change(self):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_task_items SET status='verified' WHERE task_id=?",
                (self.task,),
            )
            conn.execute(
                "UPDATE local_media_tasks SET updated_at='2026-01-01 00:00:00' WHERE id=?",
                (self.task,),
            )

    def test_restore_preserves_original_selection_ids_timestamps_and_hidden_failure(
        self,
    ):
        with isolated_test_database("mediaflux.db") as path:
            paths = self._paths(path)
            self._seed()
            expected = self._business()
            self.assertEqual(expected["selection"]["items"][0], self.target)
            self.assertEqual(expected["selection"]["items"][1]["kind"], "organize")
            self.assertEqual(expected["status"], "attention")
            archive = backup.create_backup(paths)
            self._change()
            self.assertNotEqual(self._business(), expected)
            backup.restore_backup(paths, archive)
            db.configure_database(path, test_mode=True)
            db.init_db()
            self.assertEqual(self._business(), expected)
            self.assertFalse(backup.recover_pending_restore(paths))

    def test_interrupted_restore_recovers_live_business_state_only_once(self):
        with isolated_test_database("mediaflux.db") as path:
            paths = self._paths(path)
            self._seed()
            archive = backup.create_backup(paths)
            self._change()
            expected = self._business()
            self.assertEqual(expected["status"], "completed")
            replace = backup.os.replace
            interrupted = False

            def replace_then_interrupt(source, destination):
                nonlocal interrupted
                result = replace(source, destination)
                if (
                    not interrupted
                    and Path(destination) == path
                    and Path(source).name.endswith(".tmp")
                ):
                    interrupted = True
                    raise KeyboardInterrupt("fixture after database replacement")
                return result

            with (
                patch.object(backup.os, "replace", replace_then_interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                backup.restore_backup(paths, archive)
            self.assertTrue(interrupted)
            self.assertTrue(backup.recover_pending_restore(paths))
            db.configure_database(path, test_mode=True)
            db.init_db()
            self.assertEqual(self._business(), expected)
            self.assertFalse(backup.recover_pending_restore(paths))
            self.assertEqual(self._business(), expected)
