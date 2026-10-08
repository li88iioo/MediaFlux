"""元数据全量预检按目标路径建索引，不能逐候选遍历全部历史记录。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm
from tests.support import isolated_test_database
from tests.test_strm_metadata_queue import _TreeClient


class StrmMetadataScanIndexTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("mediaflux.db"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def seed_and_scan(self, candidate_count, index_count):
        files = [
            GuangYaFile(f"m{i}", f"Movie{i}.nfo", False, 8, f"e{i}", "source")
            for i in range(candidate_count)
        ]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO strm_index(source,file_id,etag,size,filename,strm_path,content_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        "guangya-meta:source",
                        f"m{i}",
                        f"e{i}",
                        8,
                        f"Movie{i}.nfo",
                        str(self.root / strm.STRM_SUBDIR / f"Movie{i}.nfo"),
                        "sha256:missing",
                        db.now(),
                    )
                    for i in range(index_count)
                ],
            )
        reads = []
        original = db.list_strm_index

        class ObservedRow(dict):
            def __getitem__(self, key):
                if key == "strm_path":
                    reads.append(1)
                return super().__getitem__(key)

        def observed(source):
            rows = original(source)
            return (
                [ObservedRow(row) for row in rows]
                if source == "guangya-meta:source"
                else rows
            )

        with patch.object(db, "list_strm_index", side_effect=observed):
            stats = strm.sync_strm(
                "source",
                "http://media.invalid",
                str(self.root),
                client=_TreeClient({"source": files}),
                metadata_exts={"nfo"},
                clean_invalid=False,
                clean_empty_dirs=False,
            )
        return stats, len(reads)

    def test_two_hundred_candidates_do_not_rescan_thousand_history_paths(self):
        stats, reads = self.seed_and_scan(200, 1000)
        self.assertEqual(stats["metadata_queued"], 200)
        self.assertEqual(stats["metadata_failed"], 0)
        self.assertEqual(db.count_strm_metadata_jobs()["queued"], 200)
        self.assertEqual(len(db.list_strm_index("guangya-meta:source")), 1000)
        self.assertEqual(list(self.root.rglob("*.nfo")), [])
        # 建图最多读取每个历史路径一次，另有每候选的旧路径与当前状态两次读取。
        self.assertLessEqual(reads, 1000 + 2 * 200)

    def test_shared_target_keeps_all_owner_fingerprints_during_preflight(self):
        target = self.root / strm.STRM_SUBDIR / "Movie.nfo"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"metadata")
        fingerprint = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        db.upsert_strm_index(
            "guangya-meta:source",
            "valid-owner",
            "old",
            8,
            target.name,
            str(target),
            fingerprint,
        )
        db.upsert_strm_index(
            "guangya-meta:source",
            "bad-owner",
            "old",
            8,
            target.name,
            str(target),
            "sha256:old-missing-copy",
        )
        remote = GuangYaFile("valid-owner", target.name, False, 8, "new", "source")
        original = db.list_strm_index

        def valid_first(source):
            return sorted(
                original(source), key=lambda row: str(row["file_id"]) != "valid-owner"
            )

        with patch.object(db, "list_strm_index", side_effect=valid_first):
            stats = strm.sync_strm(
                "source",
                "http://media.invalid",
                str(self.root),
                client=_TreeClient({"source": [remote]}),
                metadata_exts={"nfo"},
                clean_invalid=False,
                clean_empty_dirs=False,
            )
        self.assertEqual(stats["metadata_queued"], 1)
        self.assertEqual(stats["metadata_failed"], 0)
        self.assertEqual(target.read_bytes(), b"metadata")
        self.assertEqual(len(db.list_strm_index("guangya-meta:source")), 2)

    def test_unindexed_subtitle_is_queued_then_adopted_only_after_cloud_bytes_match(self):
        target = self.root / strm.STRM_SUBDIR / "Movie.ass"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"subtitle")
        stamp = target.stat().st_mtime_ns
        remote = GuangYaFile("legacy", target.name, False, 8, "cloud", "source")
        client = _TreeClient({"source": [remote]})
        stats = strm.sync_strm("source", "http://media.invalid", str(self.root), client=client,
                               metadata_exts={"ass"}, clean_invalid=False)
        self.assertEqual(stats["metadata_queued"], 1)
        self.assertEqual(stats["metadata_failed"], 0)
        self.assertEqual(db.list_strm_index("guangya-meta:source"), [])
        job = dict(db.list_strm_metadata_queue()[0])
        temp = strm._temporary_path(target)
        temp.write_bytes(b"subtitle")
        prepared = {"file": remote, "prepared": strm.PreparedMetadataDownload(
            target, temp, strm._content_fingerprint(temp)), "rel_dir": ""}
        with patch.object(Path, "replace", side_effect=AssertionError("相同文件不得覆盖")):
            result = strm.commit_strm_metadata_job(job, prepared, str(self.root))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertFalse(temp.exists())
        row = db.list_strm_index("guangya-meta:source")[0]
        self.assertTrue(strm._metadata_state_matches(row, remote, target))
        again = strm.sync_strm("source", "http://media.invalid", str(self.root), client=client,
                               metadata_exts={"ass"}, clean_invalid=False)
        self.assertEqual(again["metadata_queued"], 0)
        self.assertEqual(again["metadata_skipped"], 1)

    def test_adoption_preserves_different_local_bytes_and_cleans_prepared_file(self):
        target = self.root / strm.STRM_SUBDIR / "Movie.ass"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"my edits")
        remote = GuangYaFile("legacy", target.name, False, 8, "cloud", "source")
        temp = strm._temporary_path(target)
        temp.write_bytes(b"subtitle")
        with self.assertRaises(strm._STRMOwnershipError):
            strm._install_metadata_candidate(remote, "", target, str(self.root), "guangya-meta:source", [], "",
                prepared=strm.PreparedMetadataDownload(target, temp, strm._content_fingerprint(temp)))
        self.assertEqual(target.read_bytes(), b"my edits")
        self.assertFalse(temp.exists())
        self.assertEqual(db.list_strm_index("guangya-meta:source"), [])

    def test_adoption_index_failure_never_deletes_existing_subtitle(self):
        target = self.root / strm.STRM_SUBDIR / "Movie.ass"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"subtitle")
        remote = GuangYaFile("legacy", target.name, False, 8, "cloud", "source")
        temp = strm._temporary_path(target)
        temp.write_bytes(b"subtitle")
        with patch.object(db, "upsert_strm_index", side_effect=RuntimeError("disk busy")), self.assertRaisesRegex(RuntimeError, "disk busy"):
            strm._install_metadata_candidate(remote, "", target, str(self.root), "guangya-meta:source", [], "",
                prepared=strm.PreparedMetadataDownload(target, temp, strm._content_fingerprint(temp)))
        self.assertEqual(target.read_bytes(), b"subtitle")
        self.assertFalse(temp.exists())
        self.assertEqual(db.list_strm_index("guangya-meta:source"), [])

    def test_real_http_truncated_body_is_retried_without_publishing_partial_content(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        payload = b"subtitle from HTTP"
        attempts = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                attempts.append(1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload[:3] if len(attempts) == 1 else payload)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            remote = GuangYaFile("m", "Movie.ass", False, len(payload), "v1", "source")
            try:
                prepared = strm.prepare_metadata_download(remote, "", str(self.root),
                    download_url=f"http://127.0.0.1:{server.server_port}/subtitle")
                self.assertEqual(prepared.temp.read_bytes(), payload)
                self.assertFalse(prepared.target.exists())
                self.assertEqual(len(attempts), 2)
                prepared.temp.unlink()
            finally:
                server.shutdown()
                thread.join(timeout=3)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
