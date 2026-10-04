"""无覆盖文件发布与有界 RSS 积压的真实进程/SQLite/ZIP 合同。"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import backup
from app.modules.local_move_transaction import LocalMoveError, LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter
from app.modules.rss import RSSEngine
from tests.support import isolated_test_database
from tests.test_local_move_transaction import Plan
from tests.test_ten_pass_process_restore_audit import runtime_paths
from tests.support import seed_rss_entry_state


class LocalPublishAndRSSRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.path = self.enterContext(isolated_test_database("mediaflux.db"))

    def child(self, script, expected_exit):
        # tests 先设置隔离路径，子进程同样拒绝真实外部连接。
        bootstrap = """
import tests
import socket, sys
from app import database as db
def deny(*args, **kwargs):
    raise AssertionError('External connections forbidden')
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
db.configure_database(sys.argv[1], test_mode=True)
db.init_db()
"""
        result = subprocess.run(
            [sys.executable, "-c", bootstrap + script, str(self.path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, expected_exit, result.stderr)

    def test_exit_between_link_and_unlink_preserves_aliases_and_manual_gate(self):
        self.child(
            r"""
import os, json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app.modules.local_move_transaction import LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter
from tests.test_local_move_transaction import Plan
root = Path(sys.argv[1]).parent
incoming, library = root / "incoming", root / "library"
incoming.mkdir(); library.mkdir()
source, target = incoming / "Movie.mkv", library / "Movie.mkv"
source.write_bytes(b"process-only-copy")
sid = db.create_local_media_source(name="interrupted-link", qb_profile="", qb_path_prefix="", local_root=str(incoming), owner="admin")
task = db.create_local_media_task(sid, "", str(source), trigger="manual")
assert db.claim_local_media_task(task, owner="admin")
db.update_local_media_task(task, status="moving")
(root / "local-manifest.json").write_text(json.dumps({"task":task, "source":str(source), "target":str(target)}))
real_link = os.link
def link_then_exit(*args, **kwargs):
    real_link(*args, **kwargs)
    os._exit(31)
plan = Plan(LocalFilesystemAdapter(incoming).snapshot(source), target)
with patch("ctypes.CDLL", return_value=SimpleNamespace(renameat2=None)), patch("os.link", side_effect=link_then_exit):
    LocalMoveTransaction([incoming], [library], task_id=task, operation_token="interrupted-link").execute([plan])
""",
            31,
        )
        manifest = json.loads((self.path.parent / "local-manifest.json").read_text())
        source, target = Path(manifest["source"]), Path(manifest["target"])
        self.assertTrue(source.samefile(target))
        self.assertEqual(target.read_bytes(), b"process-only-copy")
        db.init_db()
        task = db.get_local_media_task(manifest["task"], owner="admin")
        self.assertEqual(task.status, "requires_manual")
        self.assertFalse(db.reset_local_media_task(task.id, owner="admin"))
        steps = db.list_local_media_operation_steps(task.id, owner="admin")
        self.assertEqual([row["status"] for row in steps], ["failed"])
        db.init_db()
        self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
        self.assertTrue(source.samefile(target))
        self.assertEqual(source.read_bytes(), b"process-only-copy")

    def test_fallback_batch_rolls_back_once_then_retry_moves_all_files(self):
        incoming, library = self.path.parent / "incoming", self.path.parent / "library"
        incoming.mkdir()
        library.mkdir()
        for name in ("A.mkv", "B.mkv"):
            (incoming / name).write_bytes(name.encode())
        plans = [
            Plan(
                LocalFilesystemAdapter(incoming).snapshot(incoming / name),
                library / name,
            )
            for name in ("A.mkv", "B.mkv")
        ]
        real_unlink = os.unlink
        failures = []

        def deny_second_once(path, **kwargs):
            if Path(path).name == "B.mkv" and not failures:
                failures.append(True)
                raise PermissionError(errno.EACCES, "second source busy")
            return real_unlink(path, **kwargs)

        with patch("ctypes.CDLL", return_value=SimpleNamespace(renameat2=None)):
            with (
                patch("os.unlink", side_effect=deny_second_once),
                self.assertRaises(LocalMoveError) as caught,
            ):
                LocalMoveTransaction([incoming], [library]).execute(plans)
            self.assertEqual(caught.exception.rollback_errors, [])
            self.assertEqual(
                sorted(p.name for p in incoming.iterdir()), ["A.mkv", "B.mkv"]
            )
            self.assertEqual(list(library.iterdir()), [])
            result = LocalMoveTransaction([incoming], [library]).execute(plans)
        self.assertEqual(result.status, "completed")
        self.assertEqual(list(incoming.iterdir()), [])
        for name in ("A.mkv", "B.mkv"):
            self.assertEqual((library / name).read_bytes(), name.encode())

    def rss_round(self, sid):
        engine = RSSEngine()
        submitted = []

        def complete(entry, **kwargs):
            self.assertEqual(entry["status"], "pending")
            submitted.append(int(entry["id"]))
            seed_rss_entry_state(int(entry["id"]), "downloaded")
            return {"ok": True, "method": "qBittorrent"}

        with (
            patch.object(
                engine, "refresh", return_value={"total": 0, "new": 0, "skipped": 0}
            ),
            patch.object(engine, "_download_entry", side_effect=complete),
        ):
            result = engine.auto_download(sid)
        self.assertEqual(len(submitted), 100)
        return result, submitted

    def test_rss_exit_and_reopen_preserve_full_backlog_without_duplicate_submissions(
        self,
    ):
        self.child(
            r"""
import os, json
from pathlib import Path
from unittest.mock import patch
from app.modules.rss import RSSEngine
sid = db.add_rss_subscription("Process backlog", "https://synthetic.invalid/feed")
db.update_rss_subscription(sid, {"action":"download"})
for n in range(451):
    db.add_rss_entry_with_media(sid, f"Episode {n}", f"process:{n}")
submitted = []
def complete(entry, **kwargs):
    submitted.append(int(entry["id"]))
    with db.get_conn() as conn:
        conn.execute("UPDATE rss_entries SET status='downloaded',processed=1,processed_at=? WHERE id=?", (db.now(), int(entry["id"])))
    return {"ok":True, "method":"qBittorrent"}
engine = RSSEngine()
with patch.object(engine, "refresh", return_value={"total":0,"new":0,"skipped":0}), patch.object(engine, "_download_entry", side_effect=complete):
    result = engine.auto_download(sid)
assert result["deferred"] == 351
(Path(sys.argv[1]).parent / "rss-manifest.json").write_text(json.dumps({"sid":sid, "submitted":submitted}))
os._exit(32)
""",
            32,
        )
        manifest = json.loads((self.path.parent / "rss-manifest.json").read_text())
        db.init_db()
        result, submitted = self.rss_round(manifest["sid"])
        self.assertEqual(result["deferred"], 251)
        self.assertTrue(set(submitted).isdisjoint(manifest["submitted"]))
        result, next_submitted = self.rss_round(manifest["sid"])
        self.assertEqual(result["deferred"], 151)
        self.assertTrue(
            set(next_submitted).isdisjoint(submitted + manifest["submitted"])
        )

    def test_rss_backup_roundtrip_preserves_count_and_bounded_submission(self):
        sid = db.add_rss_subscription(
            "Restored backlog", "https://synthetic.invalid/feed"
        )
        db.update_rss_subscription(sid, {"action": "download"})
        for number in range(451):
            db.add_rss_entry_with_media(sid, f"Episode {number}", f"backup:{number}")
        paths = runtime_paths(self.path)
        archive = backup.create_backup(paths)
        db.delete_rss_subscription(sid)
        backup.restore_backup(paths, archive)
        db.configure_database(self.path, test_mode=True)
        db.init_db()
        result, _submitted = self.rss_round(sid)
        self.assertEqual(result["deferred"], 351)
        self.assertFalse(backup.recover_pending_restore(paths))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
