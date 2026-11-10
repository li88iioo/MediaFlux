"""Media Agent 整理记录摘要的聚合、脱敏、路由与 API 契约。"""

from __future__ import annotations
import sqlite3
from unittest.mock import patch

from app import database as db, sdk
from app.repositories import local_media as local_media_repo
from tests.support import IsolatedDatabaseTestCase


class OrganizeAuditUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM organize_log")

    def tearDown(self):
        pass

    @staticmethod
    def _guangya(status: str, *, title: str, stamp: str = "2026-08-10 10:00:00") -> int:
        log_id = db.add_organize_log(
            "guangya",
            "/private/original/SECRET.mkv",
            "/private/new/SECRET.mkv",
            "PRIVATE-FILE-ID",
            status,
            provider="tmdb",
            external_id="PRIVATE-EXTERNAL-ID",
            original_parent_id="PRIVATE-PARENT",
            original_name="PRIVATE-FILE.mkv",
            media_type="movie",
            title=title,
            year="2026",
            error="token=PRIVATE-ERROR https://private.example/error",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_log SET created_at=?,updated_at=? WHERE id=?",
                (stamp, stamp, log_id),
            )
        return log_id

    @staticmethod
    def _local(
        status: str,
        *,
        title: str,
        owner: str = "admin",
        trigger: str = "manual",
        stamp: str = "2026-08-10 11:00:00",
    ) -> int:
        source_id = local_media_repo.create_local_media_source(
            name=f"PRIVATE-SOURCE-{title}",
            qb_profile="qb",
            qb_path_prefix="/private/qb",
            local_root="/private/local",
            owner=owner,
        )
        task_id = local_media_repo.create_local_media_task(
            source_id,
            f"PRIVATE-HASH-{title}",
            f"/private/{title}.mkv",
            owner=owner,
            trigger=trigger,
        )
        local_media_repo.update_local_media_task(
            task_id,
            owner=owner,
            status=status,
            title=title,
            media_type="tv",
            year="2026",
            season=1,
            episode=2,
            tmdb_id="PRIVATE-TMDB-ID",
            error="PRIVATE-ERROR /private/path",
            warning="PRIVATE-WARNING",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_tasks SET created_at=?,updated_at=? WHERE id=? AND owner=?",
                (stamp, stamp, task_id, owner),
            )
        return task_id

    def test_arguments_and_empty_page(self):
        self.assertEqual(sdk.activity.organize_logs()["total"], 0)
        for invalid in ({"unexpected": True}, {"origin": "remote"}, {"status": "bad"},
                        {"limit": True}, {"limit": 0}, {"limit": "10"}, {"offset": -1},
                        {"offset": True}, {"keyword": None}):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                sdk.METHODS["activity.organize_logs"](**invalid)

    def test_database_projection_maps_filters_and_owner_scope(self):
        self._guangya("success", title="云端电影", stamp="2026-08-10 09:00:00")
        self._guangya("partial_failed", title="云端失败", stamp="2026-08-10 10:00:00")
        self._guangya("skipped", title="云端跳过", stamp="2026-08-10 11:00:00")
        self._guangya("reverted", title="云端回退", stamp="2026-08-10 12:00:00")
        self._guangya("moving", title="云端处理中", stamp="2026-08-10 13:00:00")
        self._local("completed", title="本地完成", stamp="2026-08-10 14:00:00")
        self._local("failed", title="本地失败", stamp="2026-08-10 15:00:00")
        self._local("requires_manual", title="本地待确认", stamp="2026-08-10 16:00:00")
        self._local("planned", title="本地处理中", stamp="2026-08-10 17:00:00")
        self._local("failed", title="其他用户", owner="other")
        summary = db.organize_history_page(owner="admin", limit=50)
        self.assertEqual(summary["total"], 9)
        self.assertEqual(summary["by_origin"], {"guangya": 5, "local": 4})
        self.assertEqual(
            summary["by_status"],
            {
                "failed": 2,
                "manual": 1,
                "processing": 2,
                "reverted": 1,
                "skipped": 1,
                "success": 2,
            },
        )
        self.assertEqual(summary["items"][0]["title"], "本地处理中")
        filtered = db.organize_history_page(
            owner="admin", origin="local", status="failed", limit=1
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["by_origin"], {"local": 1})
        self.assertEqual(filtered["by_status"], {"failed": 1})
        self.assertFalse(filtered["has_more"])

    def test_complete_fields_and_redacted_credentials_keep_real_ids(self):
        title = "真实长标题" * 40
        log_id = self._guangya("failed", title=title)
        result = sdk.activity.organize_logs(status="failed")
        row = result["items"][0]
        self.assertEqual((row["id"], row["kind"], row["status"]), (log_id, "organize", "failed"))
        self.assertEqual(row["title"], title)
        self.assertEqual(row["original_path"], "/private/original/SECRET.mkv")
        self.assertEqual(row["provider"], "tmdb")
        self.assertNotIn("PRIVATE-ERROR", row["error"])
        self.assertIn("https://private.example/error", row["error"])
        self.assertEqual(sdk.activity.timeline(row["kind"], row["id"])["status"], "attention")

    def test_pages_keep_tail_total_and_match_web_deleted_status(self):
        ids = [self._guangya("deleted", title=f"已回收{i}") for i in range(63)]
        self._guangya("failed", title="不要混入")
        first = sdk.activity.organize_logs(origin="guangya", status="deleted", limit=60)
        last = sdk.activity.organize_logs(origin="guangya", status="deleted", offset=first["next_offset"])
        self.assertEqual(first["total"], db.count_organize_timeline(origin="guangya", status="deleted"))
        self.assertEqual(first["by_status"], {"deleted": 63})
        self.assertEqual([first["returned"], last["returned"]], [60, 3])
        self.assertEqual({row["id"] for row in first["items"]+last["items"]}, set(ids))
        self.assertFalse(last["has_more"])
        empty = sdk.activity.organize_logs(status="deleted", offset=100)
        self.assertEqual((empty["total"], empty["items"], empty["next_offset"]), (63, [], None))
        issue = sdk.activity.organize_logs(status="issues")
        self.assertEqual(issue["by_status"], {"failed": 1})
        self.assertEqual(sdk.activity.organize_logs(keyword="已回收62")["returned"], 1)

    def test_database_failure_is_not_an_empty_success(self):
        with patch("app.repositories.activity.db.organize_history_page",
                   side_effect=sqlite3.OperationalError("PRIVATE SQL /private/db.sqlite")):
            with self.assertRaisesRegex(RuntimeError, "^暂时无法读取整理记录$"):
                sdk.activity.organize_logs()
