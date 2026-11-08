"""名称转换和历史队列迁移；所有写入由公共fs-change负责。"""

from __future__ import annotations
import sqlite3
import unittest
from copy import deepcopy
from app import database_migrations
from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_rename


class FakeGuangYaClient:
    def __init__(self, *, reject_ids: set[str] | None = None):
        self.logged_in = True
        self.credential_generation = 7
        self.reject_ids = set(reject_ids or ())
        self.closed = False
        self.directories = {
            "0": [GuangYaFile("d1", "整理", True, parent_id="0")],
            "d1": [GuangYaFile("d2", "动漫", True, parent_id="d1")],
            "d2": [
                GuangYaFile(
                    "f1",
                    "师兄太稳健.S01E08.2160p.15.1Mbps.60fps.mp4",
                    False,
                    size=100,
                    etag="etag-1",
                    parent_id="d2",
                    extension="mp4",
                ),
                GuangYaFile(
                    "f2",
                    "钢之炼金术师 FA - S01E04. 1080p 2.4 Mbps. h265.mkv",
                    False,
                    size=200,
                    etag="etag-2",
                    parent_id="d2",
                    extension="mkv",
                ),
                GuangYaFile(
                    "f3",
                    "已经正常.S01E01.1080p.mp4",
                    False,
                    size=300,
                    etag="etag-3",
                    parent_id="d2",
                    extension="mp4",
                ),
            ],
        }

    def list_dir(self, parent_id="0"):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def rename(self, file_id, new_name):
        if str(file_id) in self.reject_ids:
            raise GuangYaWriteRejected(
                "rename", code="166", message="provider rejected"
            )
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    item.name = str(new_name)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True


class RenamePureTests(unittest.TestCase):
    def test_remove_bitrate_handles_compact_and_spaced_without_damaging_codec(self):
        cases = {
            "Title.H.265.15.1Mbps.60fps.mp4": "Title.H.265.60fps.mp4",
            "Title.1080p 2.4 Mbps. h265.mkv": "Title.1080p. h265.mkv",
            "Title - S01E04 - 0.0 Mbps.mkv": "Title - S01E04.mkv",
            "Title.H.264.20Mbps.30fps.mp4": "Title.H.264.30fps.mp4",
            "Movie 1.5 GB.mp4": "Movie 1.5 GB.mp4",
        }
        for before, after in cases.items():
            with self.subTest(before=before):
                self.assertEqual(guangya_rename.remove_legacy_bitrate(before), after)

    def test_v9_queue_migration_preserves_rows_and_allows_both_guangya_kinds(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE organize_operation_jobs (job_id TEXT PRIMARY KEY,job_kind TEXT NOT NULL CHECK(job_kind IN ('agent_directory_scrape','directory_scrape')),owner_digest TEXT NOT NULL,operation TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL DEFAULT '{}',payload_auth TEXT NOT NULL DEFAULT '',dedupe_digest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',lease_generation INTEGER NOT NULL DEFAULT 0,result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',cancel_requested INTEGER NOT NULL DEFAULT 0,expires_at REAL NOT NULL DEFAULT 0,purged_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT)"
        )
        conn.execute(
            "INSERT INTO organize_operation_jobs(job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("a" * 32, "directory_scrape", "owner", "刮削", "dedupe", "c", "u"),
        )
        database_migrations._migrate_agent_guangya_operation_jobs_v10(conn)
        row = conn.execute(
            "SELECT job_kind,operation FROM organize_operation_jobs WHERE job_id=?",
            ("a" * 32,),
        ).fetchone()
        self.assertEqual(
            (row["job_kind"], row["operation"]), ("directory_scrape", "刮削")
        )
        for job_id, job_kind, operation, dedupe in (
            ("b" * 32, "agent_guangya_rename", "改名", "dedupe2"),
            ("c" * 32, "agent_guangya_cleanup", "清理", "dedupe3"),
        ):
            conn.execute(
                "INSERT INTO organize_operation_jobs(job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, job_kind, "owner", operation, dedupe, "c", "u"),
            )
        self.assertEqual(
            {
                row["job_kind"]
                for row in conn.execute(
                    "SELECT job_kind FROM organize_operation_jobs WHERE job_id IN (?,?)",
                    ("b" * 32, "c" * 32),
                )
            },
            {"agent_guangya_rename", "agent_guangya_cleanup"},
        )
        conn.close()
