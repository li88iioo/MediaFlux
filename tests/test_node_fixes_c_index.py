"""C06：逐项提交只读取当前 ID/目标冲突，真实 SQLite 计数与保护回归。"""
import hashlib
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.repositories import strm as strm_repo
from app.clients.guangya import GuangYaFile
from app.modules import strm
from tests.support import isolated_test_database


def prepared(root, fid="new", name="Movie.nfo", rel="Incoming", content=b"new"):
    file = GuangYaFile(fid, name, False, len(content), "e", "parent")
    target = strm._metadata_target(file, rel, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = strm._temporary_path(target)
    temp.write_bytes(content)
    return {"file": file, "rel_dir": rel, "prepared": strm.PreparedMetadataDownload(
        target, temp, "sha256:" + hashlib.sha256(content).hexdigest())}


class MetadataIndexFixesTests(unittest.TestCase):
    def test_C06_commit_queries_do_not_read_entire_source(self):
        for initial in (1000, 10000):
            with self.subTest(initial=initial), isolated_test_database(), tempfile.TemporaryDirectory() as root:
                strm_repo.upsert_strm_index_batch("guangya-meta:source", [
                    {"file_id": f"seed-{i}", "filename": f"Seed{i}.nfo", "size": 3,
                     "etag": "e", "strm_path": str(Path(root) / strm.STRM_SUBDIR / "Existing" / f"Seed{i}.nfo")}
                    for i in range(initial)])
                real_get_conn = db.get_conn
                fetched, queries = [], []
                class Cursor:
                    def __init__(self, cursor, sql, fetched=fetched, queries=queries):
                        self.cursor, self.sql = cursor, sql
                        self.fetched, self.queries = fetched, queries
                    def __getattr__(self, name):
                        return getattr(self.cursor, name)
                    def fetchall(self):
                        rows = self.cursor.fetchall()
                        if "strm_index" in self.sql and self.sql.startswith("SELECT"):
                            self.fetched.append(len(rows))
                            self.queries.append(self.sql)
                        return rows
                class Connection:
                    def __init__(self, conn):
                        self.conn = conn
                    def __getattr__(self, name):
                        return getattr(self.conn, name)
                    def execute(self, sql, *args):
                        return Cursor(self.conn.execute(sql, *args), sql)
                @contextmanager
                def measured(real_get_conn=real_get_conn):
                    with real_get_conn() as conn:
                        yield Connection(conn)
                with patch.object(db, "get_conn", measured), patch.object(strm_repo, "list_strm_index", wraps=strm_repo.list_strm_index) as full:
                    for i in range(10):
                        item = prepared(root, fid=f"new-{i}", name=f"New{i}.nfo")
                        self.assertEqual(strm.commit_strm_metadata_job(
                            {"source_id": "source", "file_id": f"new-{i}"}, item, root)["status"], "completed")
                self.assertEqual(len(queries), 10)
                self.assertEqual(sum(fetched), 0, f"不应读取 {sum(fetched)} 条无关索引")
                full.assert_not_called()
                # 当前 ID 已存在的重放仍只取一项，并清理未使用临时下载。
                fetched.clear()
                with patch.object(db, "get_conn", measured):
                    item = prepared(root, fid="new-0", name="New0.nfo")
                    result = strm.commit_strm_metadata_job({"source_id": "source", "file_id": "new-0"}, item, root)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(sum(fetched), 1)
                self.assertFalse(item["prepared"].temp.exists())

    def test_C06_target_conflict_and_source_boundaries_survive_targeted_query(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            item = prepared(root)
            target = item["prepared"].target
            target.write_bytes(b"old")
            fingerprint = strm._content_fingerprint(target)
            for source, fid in (("source", "owner"), ("source", "owner2"), ("other", "other-owner")):
                strm_repo.upsert_strm_index(f"guangya-meta:{source}", fid, "old", 3, target.name, str(target), fingerprint)
            strm.commit_strm_metadata_job({"source_id": "source", "file_id": "new"}, item, root)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual([r["file_id"] for r in strm_repo.list_strm_index("guangya-meta:source")], ["new"])
            self.assertEqual([r["file_id"] for r in strm_repo.list_strm_index("guangya-meta:other")], ["other-owner"])

    def test_C06_external_write_is_not_overwritten_and_index_rolls_back(self):
        for fail_db in (False, True):
            with self.subTest(fail_db=fail_db), isolated_test_database(), tempfile.TemporaryDirectory() as root:
                item = prepared(root)
                target = item["prepared"].target
                target.write_bytes(b"old")
                strm_repo.upsert_strm_index("guangya-meta:source", "owner", "old", 3, target.name,
                                     str(target), strm._content_fingerprint(target))
                if not fail_db:
                    target.write_bytes(b"external")
                failing_write = patch.object(strm_repo, "upsert_strm_index", side_effect=OSError("db fail")) if fail_db else nullcontext()
                with failing_write, self.assertRaises((RuntimeError, OSError)):
                    strm.commit_strm_metadata_job({"source_id": "source", "file_id": "new"}, item, root)
                self.assertEqual(target.read_bytes(), b"old" if fail_db else b"external")
                self.assertEqual([r["file_id"] for r in strm_repo.list_strm_index("guangya-meta:source")], ["owner"])
