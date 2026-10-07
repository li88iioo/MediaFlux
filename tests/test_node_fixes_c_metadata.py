"""C04：真实 HTTP parser + Unix socketpair，绝不连接网络服务。"""
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.strm_metadata_worker import STRMMetadataWorker
from tests.support import isolated_test_database


class MetadataTransportFixesTests(unittest.TestCase):
    def test_C04_short_http_objects_keep_old_file_and_retry_then_publish(self):
        class Cloud:
            credentials_current = True
            logged_in = True
            calls = 0
            def file_info(self, fid):
                return GuangYaFile("nfo", "Movie.nfo", False, 100, "new", "parent")
            def get_download_url(self, fid):
                self.calls += 1
                return "http://metadata.invalid/file"

        for framing in ("length", "eof", "chunked"):
            with self.subTest(framing=framing), isolated_test_database(), tempfile.TemporaryDirectory() as root:
                cloud = Cloud()
                file = cloud.file_info("nfo")
                target = strm._metadata_target(file, "Movie", root)
                target.parent.mkdir(parents=True)
                original = b"original metadata"
                target.write_bytes(original)
                db.upsert_strm_index("guangya-meta:source", "nfo", "old", len(original), file.name,
                                     str(target), strm._content_fingerprint(target))
                before = dict(db.list_strm_index("guangya-meta:source")[0])
                db.enqueue_strm_metadata_jobs([strm._metadata_queue_payload(file, "Movie", root,
                    source_id="source", source_name="source", force=True)])
                threads, sent = [], []
                payload = {"body": b"<nf"}
                def connect(*args, payload=payload, framing=framing, sent=sent, threads=threads, **kwargs):
                    client, server = socket.socketpair(socket.AF_UNIX)
                    def serve():
                        body = payload["body"]
                        try:
                            raw = b""
                            while b"\r\n\r\n" not in raw:
                                block = server.recv(4096)
                                if not block:
                                    return
                                raw += block
                            headers = b"Content-Length: " + str(len(body)).encode() + b"\r\n" if framing == "length" else b""
                            content = body
                            if framing == "chunked":
                                headers = b"Transfer-Encoding: chunked\r\n"
                                content = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
                            server.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"Connection: close\r\n\r\n" + content)
                            sent.append(len(body))
                        finally:
                            server.close()
                    thread = threading.Thread(target=serve, daemon=True)
                    thread.start()
                    threads.append(thread)
                    return client
                settings = {"STRM_ROOT": root, "STRM_METADATA_EXTS": "nfo"}
                worker = STRMMetadataWorker()
                worker._client = cloud
                with requests.Session() as session:
                    session.trust_env = False
                    with patch("urllib3.util.connection.create_connection", side_effect=connect), \
                         patch.object(strm, "_get_metadata_session", return_value=session), \
                         patch.object(strm.time, "sleep"), \
                         patch("app.modules.strm_metadata_worker.get_bool", return_value=True), \
                         patch("app.modules.strm_metadata_worker.get", side_effect=lambda k, d="", settings=settings: settings.get(k, d)), \
                         patch.object(worker, "_flush_media_refresh"):
                        self.assertTrue(worker._process_one())
                        with db.get_conn() as conn:
                            job = dict(conn.execute("SELECT * FROM strm_metadata_queue").fetchone())
                        self.assertEqual(job["status"], "retry_wait")
                        self.assertEqual(target.read_bytes(), original)
                        self.assertEqual(dict(db.list_strm_index("guangya-meta:source")[0]), before)
                        self.assertEqual(db.count_strm_refresh_paths(), 0)
                        self.assertEqual(list(target.parent.iterdir()), [target])
                        self.assertEqual(sent, [3, 3])  # 即时重取直链仍短，交给持久重试
                        self.assertGreaterEqual(cloud.calls, 2)
                        with db.get_conn() as conn:
                            conn.execute("UPDATE strm_metadata_queue SET next_attempt_at='2000-01-01 00:00:00'")
                        payload["body"] = b"x" * 100
                        self.assertTrue(worker._process_one())
                        with db.get_conn() as conn:
                            self.assertEqual(conn.execute("SELECT status FROM strm_metadata_queue").fetchone()[0], "completed")
                        self.assertEqual(target.read_bytes(), payload["body"])
                        self.assertEqual(db.count_strm_refresh_paths(), 1)
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())

    def test_C04_unknown_zero_size_remains_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            file = GuangYaFile("nfo", "Movie.nfo", False, 0, "e", "parent")
            from unittest.mock import MagicMock
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {"Content-Length": "3"}
            response.iter_content.return_value = [b"nfo"]
            with patch.object(strm.requests, "get", return_value=response):
                prepared = strm.prepare_metadata_download(file, "", root, download_url="http://metadata.invalid/file")
            self.assertEqual(prepared.temp.read_bytes(), b"nfo")
            self.assertFalse(prepared.target.exists())
