from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm as strm_module
from tests.support import IsolatedDatabaseTestCase

from app.modules.strm import (
    STRM_SUBDIR,
    generate_strm,
    safe_path_component,
    sync_strm,
)
from app.modules.strm_metadata_worker import STRMMetadataWorker


class _TreeClient:
    def __init__(self, tree):
        self.tree = tree

    def list_dir(self, file_id):
        value = self.tree[file_id]
        if isinstance(value, Exception):
            raise value
        return value

    def file_info(self, file_id):
        expected = str(file_id)
        for entries in self.tree.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if str(entry.file_id) == expected:
                    return entry
        return None

    @staticmethod
    def get_download_url(file_id):
        return f"https://download.invalid/{file_id}"


class _Response:
    def __init__(self, payload: bytes, tracker=None, *, headers=None, chunks=None):
        self.payload = payload
        self.tracker = tracker
        self.headers = headers or {}
        self.chunks = chunks

    def __enter__(self):
        if self.tracker:
            self.tracker.enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.tracker:
            self.tracker.exit()
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        if self.tracker:
            time.sleep(0.04)
        yield from self.chunks if self.chunks is not None else (self.payload,)


def _cleanup_source_indexes(source_id: str) -> None:
    for source_key in (f"guangya:{source_id}", f"guangya-meta:{source_id}"):
        rows = db.list_strm_index(source_key)
        db.delete_strm_index_ids(source_key, [row["file_id"] for row in rows])


def _drain_metadata_jobs(client, root: str, metadata_exts: set[str]) -> int:
    """用正式持久队列 worker 完成本测试已排队的元数据。"""
    worker = STRMMetadataWorker()
    worker._client = client
    processed = 0
    values = {
        "STRM_ROOT": root,
        "STRM_METADATA_EXTS": ",".join(sorted(metadata_exts)),
    }
    with patch(
        "app.modules.strm_metadata_worker.get_bool", return_value=True
    ), patch(
        "app.modules.strm_metadata_worker.get",
        side_effect=lambda key, default="": values.get(key, default),
    ), patch.object(worker, "_flush_media_refresh"):
        while worker._process_one():
            processed += 1
            if processed > 1000:
                raise AssertionError("STRM 元数据测试队列未能收敛")
    return processed


class StrmHardeningTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")
            conn.execute("DELETE FROM strm_metadata_refresh_outbox")
    def test_standard_strm_name_matches_sidecar_basename(self):
        video = GuangYaFile("video", "Show.S01E01.mkv", False, 100, "etag")
        sidecar = GuangYaFile("nfo", "Show.S01E01.nfo", False, 10, "meta")

        with tempfile.TemporaryDirectory() as root:
            strm_path = generate_strm(video, "剧集/Show/Season 1", "http://example", root)
            metadata_path = strm_module._metadata_target(
                sidecar, "剧集/Show/Season 1", root
            )

        self.assertEqual(strm_path.name, "Show.S01E01.strm")
        self.assertEqual(metadata_path.name, "Show.S01E01.nfo")
        self.assertEqual(strm_path.stem, metadata_path.stem)

    def test_metatube_identity_is_hidden_from_local_strm_and_metadata_paths(self):
        tag = "{metatube-javbus-ssis001}"
        video = GuangYaFile(
            "video", f"SSIS-001 (2024) {tag}.mp4", False, 100, "etag"
        )
        sidecar = GuangYaFile(
            "nfo", f"SSIS-001 (2024) {tag}.nfo", False, 10, "meta"
        )
        rel_dir = f"成人内容/SSIS-001 (2024) {tag}"

        with tempfile.TemporaryDirectory() as root:
            strm_path = generate_strm(video, rel_dir, "http://example", root)
            metadata_path = strm_module._metadata_target(sidecar, rel_dir, root)

        self.assertEqual(
            strm_path.relative_to(Path(root)).as_posix(),
            "光鸭云盘/成人内容/SSIS-001 (2024)/SSIS-001 (2024).strm",
        )
        self.assertEqual(
            metadata_path.relative_to(Path(root)).as_posix(),
            "光鸭云盘/成人内容/SSIS-001 (2024)/SSIS-001 (2024).nfo",
        )

    def test_clean_title_identity_is_hidden_from_local_strm_and_metadata_paths(self):
        tag = "{clean_title-ATID-675}"
        video = GuangYaFile("video", f"ATID-675 {tag}.mp4", False, 100, "etag")
        sidecar = GuangYaFile("nfo", f"ATID-675 {tag}.nfo", False, 10, "meta")
        rel_dir = f"成人内容/ATID-675 {tag}"

        with tempfile.TemporaryDirectory() as root:
            strm_path = generate_strm(video, rel_dir, "http://example", root)
            metadata_path = strm_module._metadata_target(sidecar, rel_dir, root)

        self.assertEqual(
            strm_path.relative_to(Path(root)).as_posix(),
            "光鸭云盘/成人内容/ATID-675/ATID-675.strm",
        )
        self.assertEqual(
            metadata_path.relative_to(Path(root)).as_posix(),
            "光鸭云盘/成人内容/ATID-675/ATID-675.nfo",
        )

    def test_full_sync_migrates_indexed_double_suffix_strm(self):
        source_id = f"source-migrate-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        video = GuangYaFile("video", "Movie.mkv", False, 100, "etag", source_id)
        client = _TreeClient({source_id: [video]})
        try:
            with tempfile.TemporaryDirectory() as root:
                old_path = Path(root) / STRM_SUBDIR / "Movie.mkv.strm"
                old_path.parent.mkdir(parents=True)
                payload = strm_module.build_play_url(
                    "http://example", video.file_id, video.etag,
                    video.size, video.name,
                )
                old_path.write_text(payload, encoding="utf-8")
                fingerprint = f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
                db.upsert_strm_index(
                    source_key, video.file_id, video.etag, video.size,
                    video.name, str(old_path), fingerprint,
                )

                result = sync_strm(
                    source_id, "http://example", root, client=client,
                    clean_invalid=False,
                )
                new_path = Path(root) / STRM_SUBDIR / "Movie.strm"
                row = db.list_strm_index(source_key)[0]

                self.assertTrue(new_path.is_file())
                self.assertFalse(old_path.exists())
                self.assertEqual(row["strm_path"], str(new_path))
                self.assertEqual(result["generated"], 1)
                self.assertEqual(result["cleaned"], 1)
        finally:
            _cleanup_source_indexes(source_id)

    def test_long_utf8_names_are_hashed_stably_and_keep_strm_extension(self):
        prefix = "超长中文影视名称" * 40
        first = GuangYaFile("f1", f"{prefix}甲.mkv", False, 100, "e1")
        second = GuangYaFile("f2", f"{prefix}乙.mkv", False, 100, "e2")

        with tempfile.TemporaryDirectory() as root:
            path1 = generate_strm(first, "", "http://example", root)
            again = generate_strm(first, "", "http://example", root)
            path2 = generate_strm(second, "", "http://example", root)

            self.assertEqual(path1, again)
            self.assertNotEqual(path1.name, path2.name)
            self.assertLessEqual(len(path1.name.encode("utf-8")), 255)
            self.assertTrue(path1.name.endswith(".strm"), path1.name)
            self.assertRegex(path1.name, r"~[0-9a-f]{12}\.strm$")
            self.assertFalse(list(path1.parent.glob("*.part")))

    def test_remote_directory_components_cannot_escape_root(self):
        source_id = f"source-{uuid.uuid4().hex}"
        dangerous_dir = GuangYaFile("dir-1", "../危险\\目录", True, parent_id=source_id)
        video = GuangYaFile("video-1", "片名/正片.mkv", False, 100, "etag", "dir-1")
        client = _TreeClient({source_id: [dangerous_dir], "dir-1": [video]})
        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(source_id, "http://example", root, client=client)
                self.assertEqual(result["generated"], 1)
                generated = list((Path(root) / STRM_SUBDIR).rglob("*.strm"))
                self.assertEqual(len(generated), 1)
                self.assertTrue(generated[0].resolve().is_relative_to((Path(root) / STRM_SUBDIR).resolve()))
                self.assertNotIn("..", generated[0].relative_to(Path(root) / STRM_SUBDIR).parts)
                self.assertRegex(generated[0].name, r"^片名_正片~[0-9a-f]{12}\.strm$")
        finally:
            _cleanup_source_indexes(source_id)

    def test_same_target_name_selects_larger_then_stable_file_id_tiebreak(self):
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        files = [
            GuangYaFile("small", "Movie.mkv", False, 100, "small-etag", source_id),
            GuangYaFile("z-large", "Movie.mkv", False, 200, "z-etag", source_id),
            GuangYaFile("a-large", "Movie.mkv", False, 200, "a-etag", source_id),
        ]
        client = _TreeClient({source_id: files})
        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(source_id, "http://example", root, client=client)
                target = Path(root) / STRM_SUBDIR / "Movie.strm"
                self.assertEqual(result["total"], 3)
                self.assertEqual(result["generated"], 1)
                self.assertEqual(result["duplicates_skipped"], 2)
                self.assertIn("/playgy/a-large/", target.read_text(encoding="utf-8"))
                rows = db.list_strm_index(source_key)
                self.assertEqual([row["file_id"] for row in rows], ["a-large"])
        finally:
            _cleanup_source_indexes(source_id)

    def test_candidate_fold_preserves_first_seen_on_exact_tie(self):
        first = GuangYaFile("same", "Movie.mkv", False, 100, "etag", "first")
        second = GuangYaFile("same", "Movie.mkv", False, 100, "etag", "second")
        target = Path("/library/Movie.mkv.strm")
        winners = {}

        strm_module._record_candidate(winners, target, (first, "first-dir"))
        strm_module._record_candidate(winners, target, (second, "second-dir"))

        winner, rel_dir, duplicate_count = winners[str(target)]
        self.assertIs(winner, first)
        self.assertEqual(rel_dir, "first-dir")
        self.assertEqual(duplicate_count, 1)

    def test_generate_stop_keeps_all_scanned_duplicate_statistics(self):
        source_id = f"source-{uuid.uuid4().hex}"
        files = [
            GuangYaFile("a-small", "A.mkv", False, 100, "a1", source_id),
            GuangYaFile("a-large", "A.mkv", False, 200, "a2", source_id),
            GuangYaFile("b-small", "B.mkv", False, 100, "b1", source_id),
            GuangYaFile("b-large", "B.mkv", False, 200, "b2", source_id),
        ]
        client = _TreeClient({source_id: files})
        phase = {"generate": False}

        def on_progress(stage, _completed, _total, _message):
            if stage == "generate":
                phase["generate"] = True

        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(
                    source_id,
                    "http://example",
                    root,
                    client=client,
                    on_progress=on_progress,
                    should_stop=lambda: phase["generate"],
                )
            self.assertTrue(result["stopped"])
            self.assertEqual(result["stop_stage"], "generate")
            self.assertEqual(result["duplicates_skipped"], 2)
            self.assertEqual(result["skipped"], 2)
        finally:
            _cleanup_source_indexes(source_id)

    def test_winner_change_rewrites_shared_path_without_stale_cleanup_deleting_it(self):
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        tree = {
            source_id: [GuangYaFile("old", "Movie.mkv", False, 100, "old", source_id)]
        }
        client = _TreeClient(tree)
        try:
            with tempfile.TemporaryDirectory() as root:
                sync_strm(source_id, "http://example", root, client=client)
                tree[source_id] = [GuangYaFile("new", "Movie.mkv", False, 200, "new", source_id)]
                result = sync_strm(source_id, "http://example", root, client=client)

                target = Path(root) / STRM_SUBDIR / "Movie.strm"
                self.assertTrue(target.exists())
                self.assertIn("/playgy/new/", target.read_text(encoding="utf-8"))
                self.assertEqual(result["cleaned"], 0)
                rows = db.list_strm_index(source_key)
                self.assertEqual([row["file_id"] for row in rows], ["new"])
        finally:
            _cleanup_source_indexes(source_id)

    def test_metadata_download_rejects_declared_and_streamed_oversize_payloads(self):
        file = GuangYaFile("meta", "poster.jpg", False, 4, "etag", "root")
        cases = (
            _Response(b"", headers={"Content-Length": "5"}),
            _Response(b"", chunks=[b"123", b"45"]),
        )
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as root, \
                    patch("app.modules.strm._metadata_file_limit", return_value=4), \
                    patch("app.modules.strm.requests.get", return_value=response):
                with self.assertRaises(ValueError):
                    strm_module.download_metadata(
                        file,
                        "",
                        root,
                        download_url="https://download.invalid/meta",
                    )
                self.assertFalse(list(Path(root).rglob("*.part")))

    def test_metadata_download_rejects_oversize_remote_file_before_request(self):
        file = GuangYaFile("meta", "poster.jpg", False, 5, "etag", "root")
        with tempfile.TemporaryDirectory() as root, \
                patch("app.modules.strm._metadata_file_limit", return_value=4), \
                patch("app.modules.strm.requests.get") as request:
            with self.assertRaises(ValueError):
                strm_module.download_metadata(
                    file,
                    "",
                    root,
                    download_url="https://download.invalid/meta",
                )
        request.assert_not_called()

    def test_metadata_download_enforces_total_deadline(self):
        file = GuangYaFile("meta", "poster.jpg", False, 4, "etag", "root")
        response = _Response(b"", chunks=[b"12", b"34"])
        with tempfile.TemporaryDirectory() as root, \
                patch("app.modules.strm._metadata_file_limit", return_value=4), \
                patch("app.modules.strm._metadata_download_deadline_seconds", return_value=1), \
                patch("app.modules.strm.time.monotonic", side_effect=[0.0, 0.0, 0.5, 1.5]), \
                patch("app.modules.strm.requests.get", return_value=response):
            with self.assertRaises(TimeoutError):
                strm_module.download_metadata(
                    file,
                    "",
                    root,
                    download_url="https://download.invalid/meta",
                )
            self.assertFalse(list(Path(root).rglob("*.part")))

    def test_index_and_cleanup_database_calls_stay_on_coordinator_thread(self):
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        client = _TreeClient({
            source_id: [
                GuangYaFile("video", "Movie.mkv", False, 100, "v", source_id),
                GuangYaFile("meta", "Movie.nfo", False, 4, "m", source_id),
            ]
        })
        client.get_download_url = lambda file_id: "https://download.invalid/meta"
        coordinator = threading.get_ident()
        call_threads = []
        original_upsert = db.upsert_strm_index
        original_list = db.list_strm_index
        original_delete = db.delete_strm_index_ids

        def record(callable_):
            def wrapper(*args, **kwargs):
                call_threads.append(threading.get_ident())
                return callable_(*args, **kwargs)
            return wrapper

        try:
            with tempfile.TemporaryDirectory() as root, \
                    patch("app.modules.strm.requests.get", return_value=_Response(b"nfo")), \
                    patch("app.modules.strm.db.upsert_strm_index", side_effect=record(original_upsert)), \
                    patch("app.modules.strm.db.list_strm_index", side_effect=record(original_list)), \
                    patch("app.modules.strm.db.delete_strm_index_ids", side_effect=record(original_delete)):
                sync_strm(
                    source_id,
                    "http://example",
                    root,
                    client=client,
                    metadata_exts={"nfo"},
                )
            self.assertTrue(call_threads)
            self.assertEqual(set(call_threads), {coordinator})
        finally:
            for cleanup_key in (source_key, f"guangya-meta:{source_id}"):
                rows = original_list(cleanup_key)
                original_delete(cleanup_key, [row["file_id"] for row in rows])

    def test_failed_winner_install_preserves_old_file_and_index_and_skips_cleanup(self):
        """新赢家安装任一步失败，都必须回滚到旧赢家并熔断 stale cleanup。"""
        original_upsert = db.upsert_strm_index
        original_delete = db.delete_strm_index_ids
        original_replace = Path.replace

        for stage in ("generate", "replace", "upsert", "old_index"):
            with self.subTest(stage=stage):
                source_id = f"source-{stage}-{uuid.uuid4().hex}"
                source_key = f"guangya:{source_id}"
                tree = {
                    source_id: [GuangYaFile("old", "Movie.mkv", False, 100, "old-etag", source_id)]
                }
                client = _TreeClient(tree)
                try:
                    with tempfile.TemporaryDirectory() as root:
                        first = sync_strm(source_id, "http://example", root, client=client)
                        self.assertEqual(first["generated"], 1)
                        target = Path(root) / STRM_SUBDIR / "Movie.strm"
                        old_text = target.read_text(encoding="utf-8")
                        tree[source_id] = [
                            GuangYaFile("new", "Movie.mkv", False, 200, "new-etag", source_id)
                        ]

                        patches = []
                        if stage == "generate":
                            patches.append(patch("app.modules.strm.generate_strm", side_effect=OSError("generate failed")))
                        elif stage == "replace":
                            def fail_new_replace(path_obj, destination):
                                if path_obj.name.endswith(".part") and Path(destination) == target:
                                    raise OSError("replace failed")
                                return original_replace(path_obj, destination)
                            patches.append(patch.object(Path, "replace", new=fail_new_replace))
                        elif stage == "upsert":
                            def fail_new_upsert(source, file_id, *args, **kwargs):
                                if file_id == "new":
                                    raise RuntimeError("upsert failed")
                                return original_upsert(source, file_id, *args, **kwargs)
                            patches.append(patch("app.modules.strm.db.upsert_strm_index", side_effect=fail_new_upsert))
                        else:
                            def fail_conflict_transaction(source, file_id, *args, **kwargs):
                                if file_id == "new" and tuple(
                                    kwargs.get("conflicting_file_ids") or ()
                                ) == ("old",):
                                    raise RuntimeError("old index delete failed")
                                return original_upsert(source, file_id, *args, **kwargs)
                            patches.append(patch(
                                "app.modules.strm.db.upsert_strm_index",
                                side_effect=fail_conflict_transaction,
                            ))

                        entered = []
                        try:
                            for current_patch in patches:
                                entered.append(current_patch)
                                current_patch.start()
                            try:
                                result = sync_strm(source_id, "http://example", root, client=client)
                                raised = None
                            except Exception as exc:
                                result = {}
                                raised = exc
                        finally:
                            for current_patch in reversed(entered):
                                current_patch.stop()

                        self.assertIsNone(raised, f"{stage} 不应把事务异常抛出: {raised}")
                        self.assertTrue(result.get("clean_skipped"), stage)
                        self.assertTrue(target.exists(), stage)
                        self.assertEqual(target.read_text(encoding="utf-8"), old_text, stage)
                        rows = db.list_strm_index(source_key)
                        self.assertEqual([row["file_id"] for row in rows], ["old"], stage)
                finally:
                    for cleanup_key in (source_key, f"guangya-meta:{source_id}"):
                        rows = db.list_strm_index(cleanup_key)
                        original_delete(cleanup_key, [row["file_id"] for row in rows])

    def test_old_path_unlink_failure_rolls_back_new_path_and_index(self):
        """同一 file_id 改名时，旧路径删除失败必须完整回滚。"""
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        tree = {
            source_id: [GuangYaFile("same-id", "Old.mkv", False, 100, "old-etag", source_id)]
        }
        client = _TreeClient(tree)
        original_unlink = Path.unlink
        try:
            with tempfile.TemporaryDirectory() as root:
                sync_strm(source_id, "http://example", root, client=client)
                old_path = Path(root) / STRM_SUBDIR / "Old.strm"
                old_text = old_path.read_text(encoding="utf-8")
                new_path = Path(root) / STRM_SUBDIR / "New.strm"
                tree[source_id] = [
                    GuangYaFile("same-id", "New.mkv", False, 120, "new-etag", source_id)
                ]

                def fail_old_unlink(path_obj, *args, **kwargs):
                    if path_obj == old_path:
                        raise OSError("old path unlink failed")
                    return original_unlink(path_obj, *args, **kwargs)

                with patch.object(Path, "unlink", new=fail_old_unlink):
                    result = sync_strm(source_id, "http://example", root, client=client)

                self.assertTrue(result["clean_skipped"])
                self.assertTrue(old_path.exists())
                self.assertEqual(old_path.read_text(encoding="utf-8"), old_text)
                self.assertFalse(new_path.exists())
                rows = db.list_strm_index(source_key)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["file_id"], "same-id")
                self.assertEqual(rows[0]["etag"], "old-etag")
                self.assertEqual(rows[0]["strm_path"], str(old_path))
        finally:
            _cleanup_source_indexes(source_id)

    def test_windows_reserved_and_forbidden_components_are_sanitized_stably(self):
        for raw in ("CON", "con.txt", "AUX.", "bad:name?x*", "trailing. "):
            with self.subTest(raw=raw):
                cleaned = safe_path_component(raw)
                self.assertEqual(cleaned, safe_path_component(raw))
                self.assertFalse(any(char in cleaned for char in '<>:"/\\|?*'))
                self.assertFalse(cleaned.endswith((" ", ".")))
                stem = cleaned.split(".", 1)[0].upper()
                self.assertNotIn(stem, {
                    "CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10)),
                })
        self.assertNotEqual(safe_path_component("CON"), safe_path_component("_CON"))

    def test_full_scan_ignores_remote_directory_cycles(self):
        source_id = f"source-{uuid.uuid4().hex}"
        calls = {}

        class CountingClient(_TreeClient):
            def list_dir(self, file_id):
                calls[file_id] = calls.get(file_id, 0) + 1
                return super().list_dir(file_id)

        client = CountingClient({
            source_id: [GuangYaFile("dir-a", "A", True, parent_id=source_id)],
            "dir-a": [
                GuangYaFile(source_id, "root-loop", True, parent_id="dir-a"),
                GuangYaFile("video-a", "Episode.mkv", False, 10, "etag", "dir-a"),
            ],
        })
        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(
                    source_id, "http://example", root, client=client, clean_invalid=False,
                )
            self.assertEqual(result["generated"], 1)
            self.assertEqual(result["directories"], 2)
            self.assertEqual(calls, {source_id: 1, "dir-a": 1})
        finally:
            _cleanup_source_indexes(source_id)

    def test_full_scan_handles_directory_depth_beyond_python_recursion_limit(self):
        source_id = f"source-{uuid.uuid4().hex}"
        tree = {}
        current = source_id
        depth = 1100
        for index in range(depth):
            child = f"dir-{index}"
            tree[current] = [GuangYaFile(child, "d", True, parent_id=current)]
            current = child
        tree[current] = []
        client = _TreeClient(tree)
        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(
                    source_id, "http://example", root, client=client, clean_invalid=False,
                )
            self.assertEqual(result["directories"], depth + 1)
            self.assertEqual(result["failed"], 0)
        finally:
            _cleanup_source_indexes(source_id)

    def test_retry_lookup_ignores_cycles_per_source(self):
        source_id = f"source-{uuid.uuid4().hex}"
        calls = {}

        class CountingClient(_TreeClient):
            def list_dir(self, file_id):
                calls[file_id] = calls.get(file_id, 0) + 1
                return super().list_dir(file_id)

        client = CountingClient({
            source_id: [GuangYaFile("dir-a", "A", True, parent_id=source_id)],
            "dir-a": [
                GuangYaFile(source_id, "root-loop", True, parent_id="dir-a"),
                GuangYaFile("wanted", "Episode.mkv", False, 10, "etag", "dir-a"),
            ],
        })
        lookup = strm_module._locate_retry_files(
            client, [{"id": source_id, "rel_prefix": ""}], {"wanted"},
        )
        self.assertIn("wanted", lookup.located)
        self.assertFalse(lookup.scan_incomplete)
        self.assertFalse(lookup.stopped)
        self.assertEqual(calls, {source_id: 1, "dir-a": 1})

    def test_retry_lookup_enforces_directory_entry_and_deadline_budgets(self):
        cases = (
            (
                "directories",
                {
                    "source": [GuangYaFile("child", "Child", True, parent_id="source")],
                    "child": [GuangYaFile("wanted", "Episode.mkv", False, 10, "e", "child")],
                },
                (1, 100, 100, 60),
                None,
            ),
            (
                "entries",
                {
                    "source": [
                        GuangYaFile("other", "Other.mkv", False, 10, "o", "source"),
                        GuangYaFile("wanted", "Episode.mkv", False, 10, "e", "source"),
                    ],
                },
                (100, 1, 100, 60),
                None,
            ),
            (
                "deadline",
                {
                    "source": [GuangYaFile("wanted", "Episode.mkv", False, 10, "e", "source")],
                },
                (100, 100, 100, 1),
                (0.0, 2.0),
            ),
        )
        for reason, tree, limits, clock_values in cases:
            with self.subTest(reason=reason), patch(
                "app.modules.strm._scan_limits", return_value=limits
            ):
                client = _TreeClient(tree)
                clock = (
                    patch(
                        "app.modules.strm.time.monotonic",
                        side_effect=lambda values=iter(clock_values or ()): next(values, 2.0),
                    )
                    if clock_values is not None
                    else nullcontext()
                )
                with clock:
                    lookup = strm_module._locate_retry_files(
                        client, [{"id": "source", "rel_prefix": ""}], {"wanted"},
                    )
                self.assertNotIn("wanted", lookup.located)
                self.assertTrue(lookup.scan_incomplete)
                self.assertEqual(lookup.scan_limit_reason, reason)
                self.assertLessEqual(lookup.entries, limits[1])

    def test_retry_lookup_applies_entry_budget_before_advancing_iter_dir(self):
        class PagingClient:
            def __init__(self):
                self.yielded = 0

            def iter_dir(self, _file_id, *, should_stop=None):
                if should_stop and should_stop():
                    return
                for index in range(3):
                    self.yielded += 1
                    yield GuangYaFile(
                        f"other-{index}", f"Other-{index}.mkv", False,
                        10, str(index), "source",
                    )

        client = PagingClient()
        with patch("app.modules.strm._scan_limits", return_value=(100, 1, 100, 60)):
            lookup = strm_module._locate_retry_files(
                client, [{"id": "source", "rel_prefix": ""}], {"wanted"},
            )

        self.assertEqual(client.yielded, 1)
        self.assertEqual(lookup.entries, 1)
        self.assertTrue(lookup.scan_incomplete)
        self.assertEqual(lookup.scan_limit_reason, "entries")

    def test_cleaned_names_always_get_raw_name_hash_to_avoid_component_collisions(self):
        """只要发生清洗，就必须用原名哈希区分清洗前不同的组件。"""
        escaped = safe_path_component("Movie\\Cut.mkv", extra_suffix=".strm")
        literal = safe_path_component("Movie_Cut.mkv", extra_suffix=".strm")
        self.assertNotEqual(escaped, literal)
        self.assertEqual(escaped, safe_path_component("Movie\\Cut.mkv", extra_suffix=".strm"))
        self.assertRegex(escaped, r"^Movie_Cut~[0-9a-f]{12}\.mkv\.strm$")
        self.assertEqual(literal, "Movie_Cut.mkv.strm")

        source_id = f"source-{uuid.uuid4().hex}"
        client = _TreeClient({
            source_id: [
                GuangYaFile("dir-a", "Season\\Cut", True, parent_id=source_id),
                GuangYaFile("dir-b", "Season_Cut", True, parent_id=source_id),
            ],
            "dir-a": [GuangYaFile("video-a", "Episode.mkv", False, 10, "a", "dir-a")],
            "dir-b": [GuangYaFile("video-b", "Episode.mkv", False, 10, "b", "dir-b")],
        })
        try:
            with tempfile.TemporaryDirectory() as root:
                result = sync_strm(source_id, "http://example", root, client=client)
                generated = list((Path(root) / STRM_SUBDIR).rglob("*.strm"))
                self.assertEqual(result["generated"], 2)
                self.assertEqual(len(generated), 2)
                self.assertEqual(len({path.parent.name for path in generated}), 2)
        finally:
            _cleanup_source_indexes(source_id)

    def test_metadata_same_size_new_identity_refreshes_content_and_index_state(self):
        """同名元数据的 file_id/etag 改变时，不能仅凭尺寸相同跳过。"""
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        tree = {
            source_id: [GuangYaFile("meta-old", "poster.jpg", False, 8, "etag-old", source_id)]
        }
        client = _TreeClient(tree)
        client.get_download_url = lambda file_id: f"https://download.invalid/{file_id}"

        def fake_get(url, stream, timeout):
            payload = b"old-data" if url.endswith("meta-old") else b"new-data"
            return _Response(payload)

        try:
            with tempfile.TemporaryDirectory() as root, patch(
                "app.modules.strm.requests.get", side_effect=fake_get
            ):
                first = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(first["metadata_queued"], 1)
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)
                target = Path(root) / STRM_SUBDIR / "poster.jpg"
                self.assertEqual(target.read_bytes(), b"old-data")

                tree[source_id] = [
                    GuangYaFile("meta-new", "poster.jpg", False, 8, "etag-new", source_id)
                ]
                second = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(second["metadata_queued"], 1)
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)

                self.assertEqual(second["metadata_skipped"], 0)
                self.assertEqual(target.read_bytes(), b"new-data")
                rows = db.list_strm_index(source_key)
                self.assertEqual([row["file_id"] for row in rows], ["meta-new"])
                self.assertEqual(rows[0]["etag"], "etag-new")
                self.assertEqual(rows[0]["strm_path"], str(target))
        finally:
            _cleanup_source_indexes(source_id)

    def test_metadata_index_failure_restores_previous_content_and_state(self):
        """元数据文件已下载但索引提交失败时，必须恢复旧文件和旧索引。"""
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        tree = {
            source_id: [GuangYaFile("meta-old", "poster.jpg", False, 8, "etag-old", source_id)]
        }
        client = _TreeClient(tree)
        client.get_download_url = lambda file_id: f"https://download.invalid/{file_id}"
        original_upsert = db.upsert_strm_index

        def fake_get(url, stream, timeout):
            return _Response(b"old-data" if url.endswith("meta-old") else b"new-data")

        try:
            with tempfile.TemporaryDirectory() as root, patch(
                "app.modules.strm.requests.get", side_effect=fake_get
            ):
                sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)
                target = Path(root) / STRM_SUBDIR / "poster.jpg"
                old_text = target.read_bytes()
                tree[source_id] = [
                    GuangYaFile("meta-new", "poster.jpg", False, 8, "etag-new", source_id)
                ]

                def fail_new_upsert(source, file_id, *args, **kwargs):
                    if file_id == "meta-new":
                        raise RuntimeError("metadata upsert failed")
                    return original_upsert(source, file_id, *args, **kwargs)

                with patch(
                    "app.modules.strm.db.upsert_strm_index", side_effect=fail_new_upsert
                ):
                    result = sync_strm(
                        source_id, "http://example", root, client=client,
                        metadata_exts={"jpg"},
                    )
                    self.assertEqual(result["metadata_queued"], 1)
                    self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)

                self.assertTrue(target.exists())
                self.assertEqual(target.read_bytes(), old_text)
                rows = db.list_strm_index(source_key)
                self.assertEqual([row["file_id"] for row in rows], ["meta-old"])
                queue = [
                    dict(row) for row in db.list_strm_metadata_queue()
                    if row["file_id"] == "meta-new"
                ]
                self.assertEqual(queue[0]["status"], "retry_wait")
        finally:
            _cleanup_source_indexes(source_id)

    def test_disabling_metadata_sync_preserves_downloaded_file_and_index(self):
        """关闭 metadata_exts 后不得扫描或清理独立元数据 namespace。"""
        source_id = f"source-{uuid.uuid4().hex}"
        metadata_key = f"guangya-meta:{source_id}"
        tree = {
            source_id: [GuangYaFile("poster", "poster.jpg", False, 4, "etag", source_id)]
        }
        client = _TreeClient(tree)
        client.get_download_url = lambda file_id: f"https://download.invalid/{file_id}"

        try:
            with tempfile.TemporaryDirectory() as root, patch(
                "app.modules.strm.requests.get", return_value=_Response(b"data")
            ):
                enabled = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(enabled["metadata_queued"], 1)
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)
                target = Path(root) / STRM_SUBDIR / "poster.jpg"
                self.assertTrue(target.exists())

                disabled = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts=set(),
                )

                self.assertEqual(disabled["metadata_total"], 0)
                self.assertEqual(disabled["metadata_cleaned"], 0)
                self.assertTrue(target.exists())
                rows = db.list_strm_index(metadata_key)
                self.assertEqual([row["file_id"] for row in rows], ["poster"])
                self.assertEqual(rows[0]["strm_path"], str(target))
        finally:
            _cleanup_source_indexes(source_id)

    def test_complete_enabled_scan_cleans_video_and_metadata_with_split_stats(self):
        """完整成功扫描时分别清理失效 STRM 与元数据并拆分统计。"""
        source_id = f"source-{uuid.uuid4().hex}"
        video_key = f"guangya:{source_id}"
        metadata_key = f"guangya-meta:{source_id}"
        tree = {
            source_id: [
                GuangYaFile("video", "Movie.mkv", False, 100, "video-etag", source_id),
                GuangYaFile("poster", "poster.jpg", False, 4, "meta-etag", source_id),
            ]
        }
        client = _TreeClient(tree)
        client.get_download_url = lambda file_id: f"https://download.invalid/{file_id}"

        try:
            with tempfile.TemporaryDirectory() as root, patch(
                "app.modules.strm.requests.get", return_value=_Response(b"data")
            ):
                sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)
                video_path = Path(root) / STRM_SUBDIR / "Movie.strm"
                metadata_path = Path(root) / STRM_SUBDIR / "poster.jpg"
                tree[source_id] = []

                result = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )

                self.assertEqual(result["cleaned"], 1)
                self.assertEqual(result["metadata_cleaned"], 1)
                self.assertFalse(video_path.exists())
                self.assertFalse(metadata_path.exists())
                self.assertEqual(db.list_strm_index(video_key), [])
                self.assertEqual(db.list_strm_index(metadata_key), [])
        finally:
            _cleanup_source_indexes(source_id)

    def test_remote_scan_failure_preserves_both_namespaces(self):
        """任一远端目录扫描失败时，视频和元数据 namespace 都禁止清理。"""
        source_id = f"source-{uuid.uuid4().hex}"
        video_key = f"guangya:{source_id}"
        metadata_key = f"guangya-meta:{source_id}"
        tree = {
            source_id: [
                GuangYaFile("video", "Movie.mkv", False, 100, "video-etag", source_id),
                GuangYaFile("poster", "poster.jpg", False, 4, "meta-etag", source_id),
            ]
        }
        client = _TreeClient(tree)
        client.get_download_url = lambda file_id: f"https://download.invalid/{file_id}"

        try:
            with tempfile.TemporaryDirectory() as root, patch(
                "app.modules.strm.requests.get", return_value=_Response(b"data")
            ):
                sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )
                self.assertEqual(_drain_metadata_jobs(client, root, {"jpg"}), 1)
                video_path = Path(root) / STRM_SUBDIR / "Movie.strm"
                metadata_path = Path(root) / STRM_SUBDIR / "poster.jpg"
                tree[source_id] = [
                    GuangYaFile("broken-dir", "Broken", True, parent_id=source_id)
                ]
                tree["broken-dir"] = RuntimeError("scan failed")

                result = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts={"jpg"},
                )

                self.assertTrue(result["clean_skipped"])
                self.assertEqual(result["cleaned"], 0)
                self.assertEqual(result["metadata_cleaned"], 0)
                self.assertTrue(video_path.exists())
                self.assertTrue(metadata_path.exists())
                self.assertEqual([row["file_id"] for row in db.list_strm_index(video_key)], ["video"])
                self.assertEqual([row["file_id"] for row in db.list_strm_index(metadata_key)], ["poster"])
        finally:
            _cleanup_source_indexes(source_id)

    def test_legacy_metadata_rows_in_video_namespace_are_preserved_conservatively(self):
        """旧版混合 namespace 中非 STRM 记录不能因迁移猜测被删除。"""
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        client = _TreeClient({source_id: []})

        try:
            with tempfile.TemporaryDirectory() as root:
                legacy_metadata = Path(root) / STRM_SUBDIR / "legacy.jpg"
                stale_strm = Path(root) / STRM_SUBDIR / "stale.mkv.strm"
                legacy_metadata.parent.mkdir(parents=True)
                legacy_metadata.write_bytes(b"legacy")
                stale_strm.write_text("old", encoding="utf-8")
                db.upsert_strm_index(
                    source_key, "legacy-meta", "meta-etag", 6,
                    "legacy.jpg", str(legacy_metadata),
                )
                db.upsert_strm_index(
                    source_key, "stale-video", "video-etag", 3,
                    "stale.mkv", str(stale_strm),
                )

                result = sync_strm(
                    source_id, "http://example", root, client=client,
                    metadata_exts=set(),
                )

                self.assertEqual(result["cleaned"], 0)
                self.assertEqual(result["metadata_cleaned"], 0)
                self.assertTrue(result["clean_skipped"])
                self.assertTrue(legacy_metadata.exists())
                self.assertTrue(stale_strm.exists())
                rows = db.list_strm_index(source_key)
                self.assertEqual(
                    [row["file_id"] for row in rows],
                    ["legacy-meta", "stale-video"],
                )
        finally:
            _cleanup_source_indexes(source_id)

    def test_deep_relative_paths_are_stably_compressed_within_byte_budget(self):
        """完整相对路径超预算时必须稳定压缩目录组件而不是触发 ENAMETOOLONG。"""
        file = GuangYaFile("deep", "Final.Movie.2160p.mkv", False, 100, "etag")
        component = "非常深的中文目录" * 12
        rel_dir = "/".join(f"{index:02d}-{component}" for index in range(36))

        with tempfile.TemporaryDirectory() as root:
            first = generate_strm(file, rel_dir, "http://example", root)
            second = generate_strm(file, rel_dir, "http://example", root)
            relative = first.relative_to(Path(root))
            budget = getattr(strm_module, "MAX_RELATIVE_PATH_BYTES", 3072)

            self.assertEqual(first, second)
            self.assertTrue(first.exists())
            self.assertLessEqual(len(str(relative).encode("utf-8")), budget)
            self.assertTrue(first.name.endswith(".strm"))
            self.assertTrue(any(part.startswith("~path-") for part in relative.parts))

    def test_any_directory_scan_failure_disables_stale_cleanup_for_whole_round(self):
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        stale_id = "stale"
        failing_dir = GuangYaFile("broken-dir", "Broken", True, parent_id=source_id)
        video = GuangYaFile("new", "New.mkv", False, 100, "new", source_id)
        client = _TreeClient({
            source_id: [video, failing_dir],
            "broken-dir": RuntimeError("scan failed"),
        })
        try:
            with tempfile.TemporaryDirectory() as root:
                stale_path = Path(root) / STRM_SUBDIR / "Stale.mkv.strm"
                stale_path.parent.mkdir(parents=True)
                stale_path.write_text("old", encoding="utf-8")
                db.upsert_strm_index(source_key, stale_id, "e", 1, "Stale.mkv", str(stale_path))

                result = sync_strm(source_id, "http://example", root, client=client)

                self.assertTrue(result["clean_skipped"])
                self.assertTrue(result["scan_incomplete"])
                self.assertEqual(result["generated"], 0)
                self.assertFalse((Path(root) / STRM_SUBDIR / "New.strm").exists())
                self.assertTrue(stale_path.exists())
                self.assertEqual([row["file_id"] for row in db.list_strm_index(source_key)], [stale_id])
        finally:
            _cleanup_source_indexes(source_id)

    def test_scan_budget_aborts_before_generation_and_preserves_existing_index(self):
        source_id = f"source-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        client = _TreeClient({
            source_id: [
                GuangYaFile("new-a", "A.mkv", False, 100, "a", source_id),
                GuangYaFile("new-b", "B.mkv", False, 100, "b", source_id),
            ]
        })
        try:
            with tempfile.TemporaryDirectory() as root:
                stale_path = Path(root) / STRM_SUBDIR / "Stale.mkv.strm"
                stale_path.parent.mkdir(parents=True)
                stale_path.write_text("old", encoding="utf-8")
                db.upsert_strm_index(source_key, "stale", "e", 1, "Stale.mkv", str(stale_path))

                with patch("app.modules.strm._scan_limits", return_value=(100, 1, 100, 60)):
                    result = sync_strm(source_id, "http://example", root, client=client)

                self.assertTrue(result["scan_incomplete"])
                self.assertEqual(result["scan_limit_reason"], "entries")
                self.assertEqual(result["generated"], 0)
                self.assertTrue(result["clean_skipped"])
                self.assertTrue(stale_path.exists())
                self.assertFalse((Path(root) / STRM_SUBDIR / "A.strm").exists())
                self.assertEqual(
                    [row["file_id"] for row in db.list_strm_index(source_key)],
                    ["stale"],
                )
        finally:
            _cleanup_source_indexes(source_id)


if __name__ == "__main__":
    unittest.main()
