"""Media Agent 整理记录摘要的聚合、脱敏、路由与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.organize_audit_actions import (
    audit_organize_logs,
    organize_audit_arguments,
)
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
        source_id = db.create_local_media_source(
            name=f"PRIVATE-SOURCE-{title}",
            qb_profile="qb",
            qb_path_prefix="/private/qb",
            local_root="/private/local",
            owner=owner,
        )
        task_id = db.create_local_media_task(
            source_id,
            f"PRIVATE-HASH-{title}",
            f"/private/{title}.mkv",
            owner=owner,
            trigger=trigger,
        )
        db.update_local_media_task(
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

    def test_arguments_are_strict_and_normalized(self):
        self.assertEqual(
            organize_audit_arguments({}),
            {"origin": "all", "status": "all", "limit": 10},
        )
        self.assertEqual(
            organize_audit_arguments(
                {"origin": " GUANGYA ", "status": " Failed ", "limit": 50}
            ),
            {"origin": "guangya", "status": "failed", "limit": 50},
        )
        for invalid in (
            [],
            {"unexpected": True},
            {"origin": "remote"},
            {"status": "deleted"},
            {"limit": True},
            {"limit": 0},
            {"limit": 51},
            {"limit": "10"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                organize_audit_arguments(invalid)

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
        summary = db.get_agent_organize_audit(owner="admin", limit=50)
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
        self.assertEqual(summary["records"][0]["title"], "本地处理中")
        filtered = db.get_agent_organize_audit(
            owner="admin", origin="local", status="failed", limit=1
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["by_origin"], {"local": 1})
        self.assertEqual(filtered["by_status"], {"failed": 1})
        self.assertFalse(filtered["truncated"])

    def test_result_is_fixed_shape_and_redacts_sensitive_fields(self):
        self._guangya(
            "failed",
            title="token=TOP-SECRET https://private.example /private/video.mkv",
        )
        result = audit_organize_logs({"origin": "all", "status": "all", "limit": 10})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["counts"]["by_status"]["failed"], 1)
        self.assertEqual(result.data["records"][0]["title"], "未命名条目")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "TOP-SECRET",
            "/private",
            "private.example",
            "PRIVATE-FILE-ID",
            "PRIVATE-EXTERNAL-ID",
            "PRIVATE-TMDB-ID",
            "token=",
        ):
            self.assertNotIn(secret, serialized)
        serialized_data = json.dumps(result.data, ensure_ascii=False)
        for forbidden_key in (
            "id",
            "raw_status",
            "source_label",
            "trigger",
            "path",
            "error",
            "warning",
            "tmdb_id",
            "provider",
            "external_id",
            "version",
        ):
            self.assertNotIn(f'"{forbidden_key}"', serialized_data)

    def test_unavailable_result_is_stable_and_does_not_leak_exception(self):
        with patch(
            "app.agent.organize_audit_actions.db.get_agent_organize_audit",
            side_effect=RuntimeError("PRIVATE SQL /private/db.sqlite"),
        ):
            result = audit_organize_logs(
                {"origin": "all", "status": "all", "limit": 10}
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE SQL", serialized)
        self.assertNotIn("/private", serialized)
