"""通用光鸭文件变更持久队列迁移与结果投影测试。"""

from __future__ import annotations

import sqlite3
import unittest

from app import database
from app.repositories.organize_operation_jobs import sanitize_organize_operation_result


class GuangYaFSChangeQueueTests(unittest.TestCase):
    def test_v17_migration_preserves_existing_jobs_and_accepts_generic_change_kind(
        self,
    ):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE organize_operation_jobs (
                job_id TEXT PRIMARY KEY,
                job_kind TEXT NOT NULL CHECK(job_kind IN (
                    'agent_directory_scrape','agent_guangya_cleanup',
                    'agent_guangya_rename','directory_scrape'
                )),
                owner_digest TEXT NOT NULL,
                operation TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                payload_auth TEXT NOT NULL DEFAULT '',
                dedupe_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending','running','completed','partial','failed',
                    'cancelled','manual_review'
                )),
                lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                expires_at REAL NOT NULL DEFAULT 0,
                purged_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            INSERT INTO organize_operation_jobs(
                job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at
            ) VALUES (
                'old','agent_guangya_rename','owner','旧改名','dedupe','2026-08-31','2026-08-31'
            );
            """
        )

        database._migrate_agent_guangya_fs_change_jobs_v17(connection)

        preserved = connection.execute(
            "SELECT job_kind,operation FROM organize_operation_jobs WHERE job_id='old'"
        ).fetchone()
        self.assertEqual(
            dict(preserved),
            {
                "job_kind": "agent_guangya_rename",
                "operation": "旧改名",
            },
        )
        connection.execute(
            """
            INSERT INTO organize_operation_jobs(
                job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at
            ) VALUES (
                'new','agent_guangya_fs_change','owner','通用变更','dedupe-new',
                '2026-08-31','2026-08-31'
            )
            """
        )
        self.assertEqual(
            connection.execute(
                "SELECT job_kind FROM organize_operation_jobs WHERE job_id='new'"
            ).fetchone()[0],
            "agent_guangya_fs_change",
        )
        connection.close()

    def test_public_result_projection_keeps_new_aggregate_counts_only(self):
        result = sanitize_organize_operation_result(
            {
                "stats": {
                    "total": 4,
                    "moved": 1,
                    "trashed": 1,
                    "created": 1,
                    "renamed": 1,
                    "file_id": 999,
                }
            }
        )
        self.assertEqual(
            result["stats"],
            {
                "total": 4,
                "moved": 1,
                "trashed": 1,
                "created": 1,
                "renamed": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
