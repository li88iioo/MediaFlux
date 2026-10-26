"""STRM 路径迁移的进程中断恢复与历史副本校准；只使用临时数据库/假云盘。"""

from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories.strm import StrmRefreshPolicy
from tests.support import isolated_test_database
from tests.support import PagedDirectoryTestMixin

BASE_URL = "http://play.invalid"


class MoveCloud(PagedDirectoryTestMixin):
    def __init__(self, location="Old", source="source", fail_source=""):
        self.location = location
        self.source = source
        self.fail_source = fail_source

    def list_dir(self, fid):
        if fid == self.fail_source:
            raise RuntimeError("isolated scan failure")
        if fid in {"source", "other"}:
            return (
                [GuangYaFile("series", self.location, True, parent_id=fid)]
                if fid == self.source
                else []
            )
        return [self.file_info("video")]

    def file_info(self, fid):
        return GuangYaFile(fid, "Ultraman.S01E01.mkv", False, 100, "etag", "series")


def sync(root, location="Old", source="source", **kwargs):
    return strm.sync_strm(
        source, BASE_URL, str(root), client=cast(GuangYaClient, MoveCloud(location, source)), **kwargs
    )


def crash_move(db_path, root, stage="after_index"):
    code = """
import os, sys
import tests
from pathlib import Path
from unittest.mock import patch
from app import database as db
from app.modules import strm
from tests.test_strm_move_recovery import sync

db.configure_database(Path(sys.argv[1]), test_mode=True)
real_delete = strm._delete_owned_file
def crash(*args, **kwargs):
    if sys.argv[3] == "after_delete":
        real_delete(*args, **kwargs)
    os._exit(73)
target = "app.modules.strm.db.upsert_strm_index" if sys.argv[3] == "before_index" else "app.modules.strm._delete_owned_file"
with (
    patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
    patch("socket.socket.connect_ex", side_effect=AssertionError("network forbidden")),
    patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
    patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")),
    patch(target, side_effect=crash),
):
    sync(sys.argv[2], "New")
os._exit(74)
"""
    # os._exit 不执行 tests 的 atexit；子进程的所有临时 runtime 由父进程回收。
    with tempfile.TemporaryDirectory(prefix="strm-crash-runtime-") as runtime_root:
        result = subprocess.run(
            [sys.executable, "-c", code, str(db_path), str(root), stage],
            env={**os.environ, "TMPDIR": runtime_root, "TEMP": runtime_root, "TMP": runtime_root},
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)


class StrmFirstInstallRecoveryTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.socket.connect", "socket.socket.connect_ex",
                     "socket.create_connection", "socket.getaddrinfo"):
            self.enterContext(patch(name, side_effect=AssertionError("network forbidden")))
        self.database_path = self.enterContext(isolated_test_database())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = MoveCloud().file_info("video")
        self.target = strm._strm_target(self.file, "New", str(self.root))
        self.payload = strm.build_play_url(
            BASE_URL, self.file.file_id, self.file.etag, self.file.size, self.file.name,
        ).encode("utf-8")
        self.policy = StrmRefreshPolicy(
            media_root=str(self.root / strm.STRM_SUBDIR), allow_emby=False,
        )

    def _sync(self, incremental=False):
        if not incremental:
            return sync(self.root, "New", refresh_policy=self.policy)
        change = {
            "source_id": "source", "kind": "video", "action": "upsert",
            "file_id": self.file.file_id, "rel_dir": "New", "name": self.file.name,
            "etag": self.file.etag, "size": self.file.size, "parent_id": self.file.parent_id,
        }
        return strm.sync_strm_incremental(
            "source", [change], BASE_URL, str(self.root),
            client=cast(GuangYaClient, MoveCloud("New")), refresh_policy=self.policy,
        )

    def _crash_and_recover(self, *, incremental=False):
        # 真正结束子进程：文件已原子替换，但首条索引及 outbox 尚未提交。
        crash_move(self.database_path, self.root, "before_index")
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(db.list_strm_index("guangya:source"), [])
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        db.init_db()
        stats = self._sync(incremental)
        self.assertEqual(stats["failed"], 0, stats)
        self.assertEqual(stats["generated"], 1, stats)
        self.assertFalse(stats["clean_skipped"], stats)
        rows = db.list_strm_index("guangya:source")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strm_path"], str(self.target))
        self.assertEqual(rows[0]["content_fingerprint"], strm._content_fingerprint(self.target))
        entries = db.list_strm_refresh_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], str(self.target.parent))
        self.assertFalse(entries[0]["allow_emby"])
        db.init_db()
        again = self._sync(incremental)
        self.assertEqual(again["generated"], 0, again)
        self.assertEqual(db.list_strm_refresh_entries(), entries)
        with patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as handoff:
            STRMMetadataWorker()._flush_media_refresh(force=True)
        handoff.assert_called_once_with(
            [str(self.target.parent)], immediate=True, allow_emby=False,
        )
        self.assertEqual(db.count_strm_refresh_paths(), 0)

    def test_first_install_process_exit_recovers_through_full_scan(self):
        self._crash_and_recover()

    def test_first_install_process_exit_recovers_through_incremental(self):
        self._crash_and_recover(incremental=True)

    def test_unindexed_different_content_is_not_claimed(self):
        self.target.parent.mkdir(parents=True)
        for payload in (b"http://foreign.invalid/video", self.payload + b"\n",
                        b"x" + self.payload[1:]):
            with self.subTest(payload=payload):
                self.target.write_bytes(payload)
                stats = self._sync()
                self.assertEqual(stats["failed"], 1, stats)
                self.assertEqual(self.target.read_bytes(), payload)
                self.assertEqual(db.list_strm_index("guangya:source"), [])
                self.assertEqual(db.count_strm_refresh_paths(), 0)

    def test_exact_content_owned_by_another_namespace_is_not_claimed(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.payload)
        for source in ("guangya:other", "guangya-meta:source"):
            with self.subTest(source=source):
                db.upsert_strm_index(
                    source, self.file.file_id, self.file.etag, self.file.size,
                    self.file.name, str(self.target), strm._content_fingerprint(self.target),
                )
                owners = db.list_strm_path_owners(str(self.target))
                stats = self._sync()
                self.assertEqual(stats["failed"], 1, stats)
                self.assertEqual(self.target.read_bytes(), self.payload)
                self.assertEqual(db.list_strm_path_owners(str(self.target)), owners)
                self.assertEqual(db.list_strm_index("guangya:source"), [])
                self.assertEqual(db.count_strm_refresh_paths(), 0)
                db.delete_strm_index_ids(source, [self.file.file_id])

    def test_content_changed_after_recovery_proof_is_not_overwritten(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.payload)
        read_state = strm._read_strm_state

        def changed_after_read(path, expected_url):
            result = read_state(path, expected_url)
            self.target.write_bytes(b"external edit during install")
            return result

        with patch.object(strm, "_read_strm_state", side_effect=changed_after_read) as proof:
            stats = self._sync()
        proof.assert_called_once()
        self.assertEqual(stats["failed"], 1, stats)
        self.assertEqual(self.target.read_bytes(), b"external edit during install")
        self.assertEqual(db.list_strm_index("guangya:source"), [])
        self.assertEqual(db.count_strm_refresh_paths(), 0)

    def test_recovery_index_failure_preserves_file_and_can_retry(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.payload)
        # 在真实索引事务内令 outbox 写入失败，必须一起回滚，不能吞掉刷新意图。
        with patch.object(StrmRefreshPolicy, "record", side_effect=sqlite3.OperationalError("isolated failure")) as commit:
            stats = self._sync()
        commit.assert_called_once()
        self.assertEqual(stats["failed"], 1, stats)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(db.list_strm_index("guangya:source"), [])
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        failures = db.list_strm_failures(status="open")
        self.assertEqual(len(failures), 1)
        result = strm.retry_strm_failures(
            [int(failures[0]["id"])], "test",
            client=cast(GuangYaClient, MoveCloud("New")),
            runtime_config={
                "sources": strm.plan_strm_sources([{"id": "source", "name": "Source"}]),
                "strm_root": str(self.root), "base_url": BASE_URL,
                "media_server_refresh": True, "allow_emby": False,
            },
        )
        self.assertEqual(result["resolved"], 1, result)
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(len(db.list_strm_index("guangya:source")), 1)
        self.assertEqual(db.count_strm_refresh_paths(), 1)
        self.assertFalse(db.list_strm_refresh_entries()[0]["allow_emby"])
        self.assertFalse(db.list_strm_failures(status="open"))


class StrmMoveRecoveryTests(unittest.TestCase):
    def test_scoped_recovery_does_not_read_unrelated_receipts_or_index_rows(self):
        from app.modules.strm_recovery import recover_pending_paths

        measurements = []
        for unrelated in (400, 4000):
            with self.subTest(unrelated=unrelated), isolated_test_database(), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sync(root, "New")
                current = next(root.rglob("*.strm"))
                old = current.parent.parent / "Old" / "Copy.strm"
                old.parent.mkdir()
                old.write_bytes(current.read_bytes())
                fingerprint = strm._content_fingerprint(old)
                with db.get_conn() as conn:
                    conn.executemany(
                        "INSERT INTO strm_index(source,file_id,strm_path,content_fingerprint,created_at) VALUES(?,?,?,?,?)",
                        [("guangya:source", f"unrelated-{i}", str(root / f"unused-{i}"), fingerprint, db.now()) for i in range(unrelated)],
                    )
                db.enqueue_strm_path_cleanup([
                    {"source": "guangya:source", "file_id": f"unrelated-{i}",
                     "strm_path": str(root / f"unused-old-{i}"), "content_fingerprint": fingerprint}
                    for i in range(unrelated)
                ] + [{"source": "guangya:source", "file_id": "video", "strm_path": str(old),
                      "content_fingerprint": fingerprint}])
                real_get_conn = db.get_conn
                cold_steps, operation_steps, statements = [], [], []

                @contextmanager
                def measured(
                    real_get_conn=real_get_conn, cold_steps=cold_steps,
                    operation_steps=operation_steps, statements=statements,
                ):
                    steps = [0]

                    def count():
                        steps[0] += 1
                        return 0

                    with real_get_conn() as conn:
                        conn.set_progress_handler(count, 1)
                        # 单独记录新连接首次 prepare 的 schema 初始化；LIMIT 0
                        # 不访问凭据行，不 ANALYZE，也不改变被测查询的执行计划。
                        conn.execute("SELECT 1 FROM strm_path_cleanup LIMIT 0").fetchall()
                        cold_steps.append(steps[0])
                        steps[0] = 0
                        start = len(statements)
                        conn.set_trace_callback(lambda sql: statements.append((steps[0], sql)))
                        yield conn
                    operation_steps.append(steps[0])
                    trace = statements[start:]
                    statements[start:] = [
                        ((trace[i + 1][0] if i + 1 < len(trace) else steps[0]) - before, sql)
                        for i, (before, sql) in enumerate(trace)
                    ]

                stats = STRMScheduler._empty_stats()
                refreshed = []
                with patch.object(db, "get_conn", measured):
                    recover_pending_paths(
                        "guangya:source", str(root), stats, valid_ids={"video"},
                        only_file_ids={"video"}, on_refresh_paths=refreshed.extend,
                    )
                self.assertFalse(old.exists())
                self.assertFalse(stats["clean_skipped"], stats)
                self.assertIn(str(old.parent), refreshed)
                with db.get_conn() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM strm_path_cleanup").fetchone()[0], unrelated)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM strm_index").fetchone()[0], unrelated + 1)
                    plans = [
                        [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
                        for _, sql in statements if sql.startswith("SELECT")
                    ]
                measured_steps = sum(operation_steps)
                print("SCOPED_RECOVERY_SQL", {"unrelated": unrelated, "cold_steps": cold_steps,
                      "operation_steps": operation_steps, "statements": statements, "plans": plans})
                self.assertLess(measured_steps, 4000, statements)
                measurements.append(measured_steps)
        self.assertLessEqual(abs(measurements[1] - measurements[0]), 100, measurements)

    def test_scoped_receipt_query_keeps_global_id_paging_and_empty_scope(self):
        with isolated_test_database():
            db.enqueue_strm_path_cleanup([
                {"source": "guangya:source", "file_id": ("a", "b", "unrelated")[i % 3],
                 "strm_path": f"/unused/{i}", "content_fingerprint": "sha256:unused"}
                for i in range(756)
            ])
            with db.get_conn() as conn:
                conn.executemany(
                    "INSERT INTO strm_index(source,file_id,strm_path,created_at) VALUES(?,?,?,?)",
                    [("guangya:source", fid, f"/unused/{fid}", db.now()) for fid in ("a", "b", "unrelated")],
                )
            real_get_conn = db.get_conn

            @contextmanager
            def limited():
                with real_get_conn() as conn:
                    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
                    yield conn

            scope = {"a", "b"} | {f"not-present-{i}" for i in range(2000)}
            with patch.object(db, "get_conn", limited):
                self.assertEqual(db.list_strm_path_cleanup("guangya:source", only_file_ids=set()), [])
                self.assertEqual(db.list_strm_index("guangya:source", only_file_ids=set()), [])
                self.assertEqual(
                    {row["file_id"] for row in db.list_strm_index("guangya:source", only_file_ids=scope)},
                    {"a", "b"},
                )
                rows = []
                while page := db.list_strm_path_cleanup(
                    "guangya:source", only_file_ids=scope,
                    after_id=int(rows[-1]["id"]) if rows else 0,
                ):
                    rows.extend(page)
            self.assertEqual(len(rows), 504)
            self.assertEqual({row["file_id"] for row in rows}, {"a", "b"})
            self.assertEqual([row["id"] for row in rows], sorted({row["id"] for row in rows}))

    def test_process_exit_at_each_install_boundary_converges(self):
        for stage in ("before_index", "after_index", "after_delete"):
            with (
                self.subTest(stage=stage),
                isolated_test_database() as database_path,
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                sync(root)
                old = next(root.rglob("*.strm"))
                crash_move(database_path, root, stage)
                stats = sync(root, "New")
                self.assertEqual(stats["failed"], 0, stats)
                self.assertFalse(old.exists(), stats)
                self.assertEqual(len(list(root.rglob("*.strm"))), 1)
                self.assertIn(str(old), stats["changed_strm_paths"], stats)
                again = sync(root, "New")
                self.assertEqual(again["generated"], 0)
                self.assertEqual(again["cleaned"], 0)
                self.assertFalse(again["clean_skipped"], again)

    def test_full_sync_removes_historical_unindexed_exact_copy(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            db.delete_strm_index_ids("guangya:source", ["video"])
            stats = sync(root, "New")
            self.assertFalse(old.exists())
            self.assertEqual(stats["cleaned"], 1)
            self.assertIn(str(old), stats["changed_strm_paths"])
            self.assertEqual(len(list(Path(root).rglob("*.strm"))), 1)

    def test_historical_cleanup_preserves_foreign_changed_and_non_strm_files(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root, "New")
            target = next(Path(root).rglob("*.strm"))
            old_dir = target.parent.parent / "Old"
            old_dir.mkdir()
            contents = {
                "foreign.strm": b"http://other.invalid/play/video",
                "edited.strm": target.read_bytes() + b"# manual edit",
                "episode.nfo": b"<movie/>",
                "poster.jpg": b"jpeg",
            }
            for name, payload in contents.items():
                (old_dir / name).write_bytes(payload)
            stats = sync(root, "New")
            for name, payload in contents.items():
                self.assertEqual((old_dir / name).read_bytes(), payload)
            self.assertEqual(stats["cleaned"], 0)
            self.assertTrue(stats["clean_skipped"], stats)
            self.assertTrue(stats["error_samples"], stats)

    def test_incomplete_scan_does_not_clean_historical_copy(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            db.delete_strm_index_ids("guangya:source", ["video"])
            cloud = MoveCloud("New")
            original = cloud.list_dir

            def entries(fid):
                if fid == "source":
                    return original(fid) + [GuangYaFile("broken", "Broken", True)]
                if fid == "broken":
                    raise RuntimeError("isolated incomplete listing")
                return original(fid)

            cloud.list_dir = entries
            stats = strm.sync_strm("source", BASE_URL, root, client=cast(GuangYaClient, cloud))
            self.assertTrue(old.is_file())
            self.assertTrue(stats["clean_skipped"])
            self.assertEqual(stats["generated"], 0)

    def test_interrupted_old_file_user_edit_is_preserved_and_reported(self):
        with (
            isolated_test_database() as database_path,
            tempfile.TemporaryDirectory() as root,
        ):
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            crash_move(database_path, root)
            old.write_text("user changed this pointer", encoding="utf-8")
            stats = sync(root, "New")
            self.assertEqual(old.read_text(), "user changed this pointer")
            self.assertTrue(stats["clean_skipped"], stats)
            self.assertTrue(stats["error_samples"], stats)

    def test_symlink_and_file_outside_strm_root_are_preserved(self):
        with (
            isolated_test_database(),
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as external,
        ):
            sync(root, "New")
            target = next(Path(root).rglob("*.strm"))
            outside = Path(external) / "other.strm"
            outside.write_bytes(target.read_bytes())
            link_dir = target.parent.parent / "Linked"
            link_dir.symlink_to(external, target_is_directory=True)
            link = target.parent.parent / "old.strm"
            link.symlink_to(outside)
            stats = sync(root, "New")
            self.assertTrue(link.is_symlink())
            self.assertTrue(outside.is_file())
            self.assertEqual(stats["cleaned"], 0)

    def test_another_sources_indexed_copy_is_not_an_orphan(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root, "New")
            target = next(Path(root).rglob("*.strm"))
            other = target.parent.parent / "Other.strm"
            other.write_bytes(target.read_bytes())
            db.upsert_strm_index(
                "guangya:other",
                "video",
                "etag",
                100,
                "Ultraman.S01E01.mkv",
                str(other),
                strm._content_fingerprint(other),
            )
            stats = sync(root, "New")
            self.assertTrue(other.is_file())
            self.assertEqual(stats["cleaned"], 0)

    def test_scheduler_full_round_sweeps_cross_source_orphans_once(self):
        for scoped in (False, True):
            with (
                self.subTest(scoped=scoped),
                isolated_test_database(),
                tempfile.TemporaryDirectory() as root,
            ):
                sources = [
                    {"id": "source", "name": "A", "rel_prefix": "A"},
                    {"id": "other", "name": "B", "rel_prefix": "B"},
                ]
                sync(root, rel_prefix="A")
                old = next(Path(root).rglob("*.strm"))
                db.delete_strm_index_ids("guangya:source", ["video"])
                selected = sources[1:] if scoped else sources
                scheduler = STRMScheduler()
                scheduler._source_runtime = [
                    {**s, "status": "pending", "completed": 0, "total": 0}
                    for s in selected
                ]

                def execute(**kwargs):
                    return strm.sync_strm(client=cast(GuangYaClient, MoveCloud("New", "other")), **kwargs)

                with (
                    patch(
                        "app.modules.scheduler.configured_strm_source_plans",
                        return_value=(sources, ""),
                    ),
                    patch("app.modules.scheduler.sync_strm", side_effect=execute),
                ):
                    stats, _, stopped = scheduler._run_full_sources(
                        selected,
                        base_url=BASE_URL,
                        strm_root=root,
                        exts={"mkv"},
                        metadata_exts=set(),
                        threshold=0,
                        active_ids_complete=not scoped,
                    )
                self.assertFalse(stopped)
                self.assertEqual(old.exists(), scoped, stats)
                self.assertEqual(stats["cleaned"], 0 if scoped else 1)

    def test_incremental_recovery_is_scoped_to_confirmed_ids(self):
        with (
            isolated_test_database() as database_path,
            tempfile.TemporaryDirectory() as root,
        ):
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            crash_move(database_path, root)
            change = {
                "source_id": "source",
                "kind": "video",
                "action": "upsert",
                "file_id": "video",
                "parent_id": "series",
                "rel_dir": "New",
                "name": "Ultraman.S01E01.mkv",
                "etag": "etag",
                "size": 100,
            }
            stats = strm.sync_strm_incremental(
                "source", [change], BASE_URL, root, client=cast(GuangYaClient, MoveCloud("New"))
            )
            self.assertFalse(stats["fallback_required"], stats)
            self.assertFalse(old.exists())
            self.assertEqual(stats["cleaned"], 1)
            self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])

    def test_recovery_keeps_old_copy_when_new_pointer_is_missing(self):
        from app.modules.strm_recovery import recover_pending_paths

        with (
            isolated_test_database() as database_path,
            tempfile.TemporaryDirectory() as root,
        ):
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            crash_move(database_path, root)
            current = Path(db.list_strm_index("guangya:source")[0]["strm_path"])
            current.unlink()
            stats = STRMScheduler._empty_stats()
            recover_pending_paths("guangya:source", root, stats, valid_ids={"video"})
            self.assertTrue(old.exists())
            self.assertTrue(stats["clean_skipped"])
            self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
            restored = sync(root, "New")
            self.assertTrue(current.is_file())
            self.assertFalse(old.exists(), restored)

    def test_cleanup_journal_rejects_symlink_replacement_of_old_path(self):
        with (
            isolated_test_database() as database_path,
            tempfile.TemporaryDirectory() as root,
        ):
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            crash_move(database_path, root)
            current = Path(db.list_strm_index("guangya:source")[0]["strm_path"])
            other = current.parent / "user.data"
            other.write_bytes(old.read_bytes())
            old.unlink()
            old.symlink_to(other)
            stats = sync(root, "New")
            self.assertTrue(other.is_file())
            self.assertTrue(old.is_symlink())
            self.assertTrue(stats["clean_skipped"])
            self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)

    def test_recovery_refresh_handoff_failure_preserves_journal_for_retry(self):
        with (
            isolated_test_database() as database_path,
            tempfile.TemporaryDirectory() as root,
        ):
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            crash_move(database_path, root)

            def unavailable(paths):
                raise OSError("isolated outbox failure")

            stats = sync(root, "New", on_refresh_paths=unavailable)
            self.assertFalse(old.exists())
            self.assertTrue(stats["stopped"])
            self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
            refreshed = []
            stats = sync(
                root, "New", on_refresh_paths=lambda paths: refreshed.extend(paths)
            )
            self.assertIn(str(old.parent), refreshed)
            self.assertFalse(stats["clean_skipped"])
            self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])

    def test_recovery_scan_is_bounded_and_preserves_unvisited_files(self):
        from app.modules.strm_recovery import reconcile_historical_strm

        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root, "New")
            current = next(Path(root).rglob("*.strm"))
            for i in range(5):
                target = current.parent.parent / f"Old{i}" / "Copy.strm"
                target.parent.mkdir()
                target.write_bytes(current.read_bytes())
            stats = STRMScheduler._empty_stats()
            with patch.object(strm, "_scan_limits", return_value=(2, 100, 100, 30)):
                reconcile_historical_strm(root, BASE_URL, [{"id": "source"}], stats)
            self.assertTrue(stats["clean_skipped"])
            self.assertTrue(stats["error_samples"])
            self.assertGreater(len(list(Path(root).rglob("Copy.strm"))), 0)

    def test_recovery_preserves_old_if_new_copy_changes_during_scan(self):
        from app.modules import strm_recovery

        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root, "New")
            current = next(Path(root).rglob("*.strm"))
            old = current.parent.parent / "Old.strm"
            old.write_bytes(current.read_bytes())

            def walk(*args):
                current.write_text("changed by an external writer", encoding="utf-8")
                yield old

            stats = STRMScheduler._empty_stats()
            with patch.object(strm_recovery, "_local_pointers", side_effect=walk):
                strm_recovery.reconcile_historical_strm(
                    root, BASE_URL, [{"id": "source"}], stats
                )
            self.assertTrue(old.is_file())
            self.assertTrue(stats["clean_skipped"])

    def test_recovery_rechecks_old_fingerprint_at_delete_boundary(self):
        from app.modules import strm_recovery

        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root, "New")
            current = next(Path(root).rglob("*.strm"))
            old = current.parent.parent / "Old.strm"
            old.write_bytes(current.read_bytes())
            real_delete = strm._delete_owned_file

            def external_write(path, owners, action):
                path.write_text("external pointer", encoding="utf-8")
                return real_delete(path, owners, action)

            stats = STRMScheduler._empty_stats()
            with patch.object(strm, "_delete_owned_file", side_effect=external_write):
                strm_recovery.reconcile_historical_strm(
                    root, BASE_URL, [{"id": "source"}], stats
                )
            self.assertEqual(old.read_text(), "external pointer")
            self.assertTrue(stats["clean_skipped"])

    def test_stale_file_delete_error_is_reported_and_remains_retryable(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root)
            old = next(Path(root).rglob("*.strm"))
            cloud = MoveCloud("New")
            cloud.file_info = lambda fid: GuangYaFile(
                "replacement", "Ultraman.S01E01.mkv", False, 100, "etag", "series"
            )
            real_delete = strm._delete_owned_file

            def permission_error(path, rows, action):
                if path == old:
                    raise PermissionError("isolated NAS directory permission error")
                return real_delete(path, rows, action)

            with patch.object(strm, "_delete_owned_file", side_effect=permission_error):
                stats = strm.sync_strm("source", BASE_URL, root, client=cast(GuangYaClient, cloud))
            self.assertTrue(old.exists())
            self.assertEqual(stats["generated"], 1)
            self.assertTrue(stats["clean_skipped"], stats)
            self.assertTrue(stats["error_samples"], stats)
            retry = strm.sync_strm("source", BASE_URL, root, client=cast(GuangYaClient, cloud))
            self.assertFalse(old.exists(), retry)
            self.assertFalse(retry["clean_skipped"], retry)

    def test_normal_move_does_not_duplicate_removed_file_details(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            sync(root)
            stats = sync(root, "New")
            removed = [item for item in stats["changes"] if item["action"] == "removed"]
            self.assertEqual(stats["cleaned"], 1)
            self.assertEqual(len(removed), 1, stats)
            self.assertEqual(len(stats["changed_strm_paths"]), 2, stats)


class StrmRetiredPathRecoveryTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.socket.connect", "socket.socket.connect_ex",
                     "socket.create_connection", "socket.getaddrinfo"):
            self.enterContext(patch(name, side_effect=AssertionError("network forbidden")))
        self.database_path = self.enterContext(isolated_test_database())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.policy = StrmRefreshPolicy(
            media_root=str(self.root / strm.STRM_SUBDIR), allow_emby=False,
        )

    def _displaced_paths(self, namespace="guangya:source", suffix=".strm"):
        old = self.root / strm.STRM_SUBDIR / "Old" / f"video{suffix}"
        current = self.root / strm.STRM_SUBDIR / "New" / old.name
        for path in (old, current):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"trusted generated content")
            db.upsert_strm_index(
                namespace, "video", "etag", 100, path.name, str(path),
                strm._content_fingerprint(path),
            )
        db.enqueue_strm_retired_source("source", "S", str(self.root))
        return old, current

    def _retire(self, **kwargs):
        return strm.clean_retired_strm_sources(
            set(), clean_empty_dirs=False, refresh_policy=self.policy, **kwargs,
        )

    def _assert_task_status(self, status):
        run = db.get_last_task_run("strm_sync")
        assert run is not None
        self.assertEqual(run["status"], status)

    def _fail_old_refresh(self, old):
        old_directory = str(old.parent).replace("'", "''")
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER retired_old_refresh_fail BEFORE INSERT ON strm_refresh_outbox "
                f"WHEN NEW.path='{old_directory}' "
                "BEGIN SELECT RAISE(FAIL, 'isolated old refresh failure'); END"
            )

    def test_actual_move_crash_then_source_removal_retires_both_paths(self):
        from tests.test_strm_refresh_closure import _consume, _providers, _run

        sync(self.root)
        old = next(self.root.rglob("*.strm"))
        crash_move(self.database_path, self.root, "after_index")
        db.init_db()
        current = Path(db.list_strm_index("guangya:source")[0]["strm_path"])
        self.assertTrue(old.is_file())
        self.assertTrue(current.is_file())
        self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
        # 无可信凭据的同内容文件不在退役删除范围，禁止扩大成目录扫删。
        untracked = old.parent / "Untracked.strm"
        untracked.write_bytes(old.read_bytes())
        db.enqueue_strm_retired_source("source", "S", str(self.root))
        runtime_root = self.root / "ChangedRuntimeRoot"
        with _providers("jellyfin"):
            result = _run(runtime_root, retire=True, allow_emby=False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["strm_partial"], result)
        self.assertEqual(result["stats"]["cleaned"], 2, result)
        self.assertEqual(result["stats"]["retired_blocked"], 0)
        self._assert_task_status("success")
        self.assertFalse(old.exists())
        self.assertFalse(current.exists())
        self.assertTrue(untracked.is_file())
        self.assertEqual(db.list_strm_index("guangya:source"), [])
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
        _consume(providers=("jellyfin",), expected_paths={str(old.parent), str(current.parent)})
        db.init_db()
        again = self._retire()
        self.assertEqual(again["sources"], 0)
        self.assertTrue(untracked.is_file())

    def test_retired_metadata_receipt_is_consumed_even_without_current_index(self):
        old, current = self._displaced_paths("guangya-meta:source", ".nfo")
        current.unlink()
        db.delete_strm_index_ids("guangya-meta:source", ["video"])
        result = self._retire()
        self.assertEqual(result["cleaned"], 1, result)
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["removed_paths"], [str(old)])
        self.assertFalse(old.exists())
        self.assertEqual(db.list_strm_path_cleanup("guangya-meta:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
        self.assertEqual(
            {(entry["path"], entry["allow_emby"]) for entry in db.list_strm_refresh_entries()},
            {(str(old.parent), False)},
        )

    def test_retired_old_path_reused_by_active_index_is_not_deleted(self):
        old, current = self._displaced_paths()
        db.upsert_strm_index(
            "guangya:active", "other", "etag", 100, old.name, str(old),
            strm._content_fingerprint(old),
        )
        result = strm.clean_retired_strm_sources(
            {"active"}, clean_empty_dirs=False, refresh_policy=self.policy,
        )
        self.assertEqual(result["cleaned"], 1)
        self.assertEqual(result["blocked"], 0)
        self.assertTrue(old.is_file())
        self.assertFalse(current.exists())
        self.assertEqual(len(db.list_strm_index("guangya:active")), 1)
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
        self.assertNotIn(str(old), result["removed_paths"])

    def test_modified_old_path_keeps_retirement_partial_until_restored(self):
        from tests.test_strm_refresh_closure import _providers, _run

        old, current = self._displaced_paths()
        expected = old.read_bytes()
        old.write_bytes(b"external modification")
        with _providers("jellyfin"):
            result = _run(self.root, retire=True, allow_emby=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["strm_partial"], result)
        self._assert_task_status("partial")
        self.assertEqual(result["stats"]["retired_blocked"], 1)
        self.assertTrue(result["stats"]["error_samples"])
        self.assertEqual(old.read_bytes(), b"external modification")
        self.assertFalse(current.exists())
        self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
        self.assertEqual(len(db.list_strm_retired_sources()), 1)
        old.write_bytes(expected)
        with _providers("jellyfin"):
            retry = _run(self.root, retire=True, allow_emby=False)
        self.assertFalse(retry["strm_partial"], retry)
        self._assert_task_status("success")
        self.assertFalse(old.exists())
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])

    def test_old_path_refresh_sql_failure_keeps_receipt_and_retirement_for_retry(self):
        old, current = self._displaced_paths()
        self._fail_old_refresh(old)
        result = self._retire()
        self.assertTrue(result["stopped"], result)
        self.assertGreater(result["blocked"], 0)
        self.assertTrue(result["errors"])
        self.assertFalse(old.exists())
        self.assertFalse(current.exists())
        self.assertEqual(db.list_strm_index("guangya:source"), [])
        self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
        self.assertEqual(len(db.list_strm_retired_sources()), 1)
        self.assertNotIn(str(old.parent), {entry["path"] for entry in db.list_strm_refresh_entries()})
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER retired_old_refresh_fail")
        db.init_db()
        retry = self._retire()
        self.assertFalse(retry["stopped"], retry)
        self.assertEqual(retry["blocked"], 0)
        self.assertIn(str(old), retry["removed_paths"])
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
        self.assertEqual(
            {(entry["path"], entry["allow_emby"]) for entry in db.list_strm_refresh_entries()},
            {(str(old.parent), False), (str(current.parent), False)},
        )

    def test_scheduler_reports_refresh_sql_failure_then_recovers_retired_receipt(self):
        from tests.test_strm_refresh_closure import _consume, _providers, _run

        old, current = self._displaced_paths()
        self._fail_old_refresh(old)
        with _providers("jellyfin"):
            result = _run(self.root, retire=True, allow_emby=False)
        self.assertFalse(result["ok"], result)
        self._assert_task_status("failed")
        self.assertFalse(old.exists())
        self.assertFalse(current.exists())
        self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 1)
        self.assertEqual(len(db.list_strm_retired_sources()), 1)
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER retired_old_refresh_fail")
        db.init_db()
        with _providers("jellyfin"):
            retry = _run(self.root, retire=True, allow_emby=False)
        self.assertTrue(retry["ok"], retry)
        self.assertFalse(retry["strm_partial"], retry)
        self._assert_task_status("success")
        self.assertIn(str(old), retry["stats"]["changed_strm_paths"])
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
        _consume(providers=("jellyfin",), expected_paths={str(old.parent), str(current.parent)})

    def test_cancellation_after_old_delete_keeps_unacked_receipts_and_remaining_file(self):
        old, current = self._displaced_paths()
        remaining = old.parent / "Remaining.strm"
        remaining.write_bytes(old.read_bytes())
        db.enqueue_strm_path_cleanup([{
            "source": "guangya:source", "file_id": "remaining", "strm_path": str(remaining),
            "content_fingerprint": strm._content_fingerprint(remaining),
        }])
        stopped = False
        real_delete = strm._delete_owned_file

        def delete_then_stop(path, rows, action):
            nonlocal stopped
            deleted = real_delete(path, rows, action)
            if path == old:
                stopped = True
            return deleted

        with patch.object(strm, "_delete_owned_file", side_effect=delete_then_stop):
            result = self._retire(should_stop=lambda: stopped)
        self.assertTrue(result["stopped"], result)
        self.assertFalse(old.exists())
        self.assertFalse(current.exists())
        self.assertTrue(remaining.is_file())
        self.assertEqual(len(db.list_strm_path_cleanup("guangya:source")), 2)
        self.assertEqual(len(db.list_strm_retired_sources()), 1)
        retry = self._retire()
        self.assertFalse(retry["stopped"], retry)
        self.assertEqual(retry["blocked"], 0)
        self.assertFalse(remaining.exists())
        self.assertIn(str(old), retry["removed_paths"])
        self.assertEqual(db.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(db.list_strm_retired_sources(), [])
