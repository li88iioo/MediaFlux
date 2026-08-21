from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.directory_scrape_errors import DirectoryScrapeStateError
from app.modules.organize import (
    OrganizeContext, OrganizePlan, OrganizePlanningResult, OrganizeRules, Organizer,
)
from app.modules.organize_execution import execute_organize_plans
from app.modules.scraper import MatchResult
from app.modules.strm import STRM_SUBDIR, sync_strm
from app.modules.strm_notifications import append_change, relative_change
from app.routes import proxy as proxy_routes
from app.routes.proxy import play_gy
from tests.support import IsolatedDatabaseTestCase


class _TreeClient:
    def __init__(self, tree: dict[str, list[GuangYaFile]]) -> None:
        self.tree = tree

    def list_dir(self, file_id: str) -> list[GuangYaFile]:
        return self.tree.get(file_id, [])


class _MetadataClient(_TreeClient):
    def get_download_url(self, file_id: str) -> str:
        return f"https://storage.invalid/{file_id}?signature=secret"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield self.payload


class OrganizeRobustnessTests(IsolatedDatabaseTestCase):
    def test_archive_target_inside_source_is_rejected(self) -> None:
        source = GuangYaFile("source", "来源", True, parent_id="0")
        child = GuangYaFile("child", "归档", True, parent_id="source")
        client = type(
            "Client",
            (),
            {"file_info": lambda _self, file_id: {"source": source, "child": child}.get(file_id)},
        )()
        organizer = Organizer(client=client, scraper=object())

        with self.assertRaisesRegex(DirectoryScrapeStateError, "归档目标位于来源目录内"):
            organizer._validate_target_outside_source("source", "child")

    def test_companion_is_rolled_back_when_rename_fails_after_move(self) -> None:
        operations: list[tuple] = []
        companion = GuangYaFile("sub", "Movie.nfo", False, 10, "sub-etag", "source-id")

        class Client:
            def list_dir(self, _file_id: str):
                return []

            def move(self, file_ids: list[str], target_id: str):
                operations.append(("move", tuple(file_ids), target_id))
                return True

            def rename(self, file_id: str, name: str):
                operations.append(("rename", file_id, name))
                if file_id == "sub":
                    raise RuntimeError("companion rename failed")
                return True

        plan = OrganizePlan(
            file_id="video",
            original_name="Movie.mkv",
            original_path="source",
            original_parent_id="source-id",
            size=100,
            etag="video-etag",
            match=MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie"
            ),
            main_category="电影",
            new_name="Movie.2026.mkv",
            target_path="电影/Movie (2026) {tmdb-1}",
        )
        stats = {
            "moved": 0,
            "renamed": 0,
            "rename_failed": 0,
            "metadata_moved": 0,
            "stopped": 0,
            "skipped": 0,
            "conflict": 0,
            "failed": 0,
            "subtitle_moved": 0,
            "subtitle_skipped": 0,
            "subtitle_reasons": [],
            "skip_reasons": [],
        }
        organizer = Organizer(client=Client(), scraper=object())

        with patch.object(organizer, "_ensure_dir_chain", return_value="target-id"), patch(
            "app.modules.organize.add_organize_log", return_value=1
        ), patch("app.modules.organize.add_organize_log_items"):
            execute_organize_plans(organizer,
                [plan],
                OrganizeRules(target_dir_id="archive", rename_enabled=True),
                stats,
                {"source": [companion]},
                None,
                source_dir_id="source-id",
            )

        self.assertIn(("move", ("sub",), "target-id"), operations)
        self.assertIn(("move", ("sub",), "source-id"), operations)
        self.assertIn(("move", ("video",), "source-id"), operations)
        self.assertEqual(stats["renamed"], 0)
        self.assertEqual(stats["failed"], 1)

    def test_successful_move_emits_trusted_strm_change_manifest(self) -> None:
        class Client:
            @staticmethod
            def file_info(_file_id: str):
                return GuangYaFile(
                    "video", "Movie.mkv", False, 100, "video-etag", "source-id"
                )

            @staticmethod
            def list_dir(_file_id: str):
                return []

            @staticmethod
            def move(_file_ids: list[str], _target_id: str):
                return True

            @staticmethod
            def rename(_file_id: str, _name: str):
                return True

        plan = OrganizePlan(
            file_id="video", original_name="Movie.mkv", original_path="incoming",
            original_parent_id="source-id", size=100, etag="video-etag",
            match=MatchResult(tmdb_id="1", title="Movie", year="2026", media_type="movie"),
            main_category="电影", new_name="Movie.2026.mkv",
            target_path="电影/Movie (2026) {tmdb-1}",
        )
        stats = {
            "moved": 0, "renamed": 0, "rename_failed": 0,
            "metadata_moved": 0, "stopped": 0, "skipped": 0,
            "conflict": 0, "failed": 0, "subtitle_moved": 0,
            "subtitle_skipped": 0, "subtitle_reasons": [], "skip_reasons": [],
            "scan_errors": [], "strm_changes": [], "strm_force_full": False,
        }
        organizer = Organizer(client=Client(), scraper=object())
        with patch.object(organizer, "_ensure_dir_chain", return_value="target-id"), patch.object(
            organizer, "_resolve_variant_conflict", return_value=(None, "none", "")
        ), patch.object(organizer, "_write_organize_audit", return_value=1):
            execute_organize_plans(organizer,
                [plan],
                OrganizeRules(target_dir_id="archive", rename_enabled=True),
                stats, {}, None, source_dir_id="source-id",
            )

        self.assertEqual(stats["moved"], 1)
        self.assertFalse(stats["strm_force_full"])
        self.assertEqual(stats["strm_changes"], [{
            "source_id": "archive", "kind": "video", "action": "upsert",
            "file_id": "video", "rel_dir": "电影/Movie (2026) {tmdb-1}",
            "name": "Movie.2026.mkv", "etag": "video-etag", "size": 100,
            "parent_id": "target-id",
        }])

    def test_post_organize_link_forwards_manifest_to_scheduler(self) -> None:
        stats = {
            "moved": 1,
            "strm_changes": [{
                "source_id": "archive", "kind": "video", "action": "upsert",
                "file_id": "video",
            }],
            "strm_force_full": False,
        }
        rules = OrganizeRules(
            link_strm=True, notify_enabled=True, strm_detail_notify=True,
            emby_refresh=True,
        )
        scheduler = unittest.mock.Mock()
        scheduler.trigger.return_value = {"ok": True}
        values = {"GY_STRM_BASE_URL": "http://media.invalid", "STRM_ROOT": "/tmp/strm"}
        with patch(
            "app.modules.organize.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            Organizer._post_organize_link(stats, rules, download_request_ids=[7])

        scheduler.trigger.assert_called_once_with(
            "organize", notify_override=True, detail_notify_override=True,
            emby_refresh_override=True, download_request_ids=[7],
            organize_changes=stats["strm_changes"], force_full=False,
        )
        self.assertTrue(stats["strm"]["ok"])

    def test_partial_organize_does_not_trigger_strm_link(self) -> None:
        stats = {"moved": 1, "failed": 1, "scan_errors": []}
        rules = OrganizeRules(link_strm=True)
        with patch.object(Organizer, "_post_organize_link") as post_link, patch.object(
            Organizer, "_notify_result"
        ):
            Organizer.trigger_post_actions(stats, rules)

        post_link.assert_not_called()
        self.assertTrue(stats["strm"]["skipped"])

    def test_audit_failure_does_not_trigger_strm_link(self) -> None:
        stats = {"moved": 1, "failed": 0, "scan_errors": [], "audit_failures": 1}
        rules = OrganizeRules(link_strm=True)
        with patch.object(Organizer, "_post_organize_link") as post_link, patch.object(
            Organizer, "_notify_result"
        ):
            Organizer.trigger_post_actions(stats, rules)

        post_link.assert_not_called()
        self.assertTrue(stats["strm"]["skipped"])

    def test_stale_remote_snapshot_fails_before_creating_target_or_moving(self) -> None:
        operations: list[tuple] = []

        class Client:
            def file_info(self, _file_id: str):
                return GuangYaFile(
                    "video", "Movie-renamed.mkv", False, 100, "etag-new", "source-id"
                )

            def list_dir(self, _file_id: str):
                operations.append(("list", _file_id))
                return []

            def move(self, file_ids: list[str], target_id: str):
                operations.append(("move", tuple(file_ids), target_id))
                return True

            def rename(self, file_id: str, name: str):
                operations.append(("rename", file_id, name))
                return True

        plan = OrganizePlan(
            file_id="video", original_name="Movie.mkv", original_path="source",
            original_parent_id="source-id", size=100, etag="etag-old",
            match=MatchResult(tmdb_id="1", title="Movie", year="2026", media_type="movie"),
            main_category="电影", new_name="Movie.2026.mkv",
            target_path="电影/Movie (2026) {tmdb-1}",
        )
        stats = {
            "moved": 0, "renamed": 0, "rename_failed": 0,
            "metadata_moved": 0, "stopped": 0, "skipped": 0,
            "conflict": 0, "failed": 0, "subtitle_moved": 0,
            "subtitle_skipped": 0, "subtitle_reasons": [], "skip_reasons": [],
        }
        organizer = Organizer(client=Client(), scraper=object())

        with patch.object(organizer, "_ensure_dir_chain") as ensure_target:
            execute_organize_plans(organizer,
                [plan], OrganizeRules(target_dir_id="archive"), stats, {}, None,
                source_dir_id="source-id",
            )

        ensure_target.assert_not_called()
        self.assertEqual(operations, [])
        self.assertEqual(stats["failed"], 1)
        latest = dict(db.list_organize_logs(limit=1)[0])
        self.assertEqual(latest["status"], "failed")
        self.assertIn("预览后发生变化", latest["error"])

    def test_remote_restore_replays_original_name_when_state_lookup_fails(self) -> None:
        operations: list[tuple] = []

        class Client:
            def file_info(self, _file_id: str):
                raise TimeoutError("lookup timeout")

            def rename(self, file_id: str, name: str):
                operations.append(("rename", file_id, name))
                return True

            def move(self, file_ids: list[str], target_id: str):
                operations.append(("move", tuple(file_ids), target_id))
                return True

        item = GuangYaFile("video", "Movie.mkv", False, 100, "etag", "source")
        Organizer(client=Client(), scraper=object())._restore_remote_file(
            item, "source", "Movie.mkv"
        )

        self.assertIn(("rename", "video", "Movie.mkv"), operations)
        self.assertIn(("move", ("video",), "source"), operations)


class StrmRobustnessTests(IsolatedDatabaseTestCase):
    def _cleanup(self, source_id: str) -> None:
        for key in (f"guangya:{source_id}", f"guangya-meta:{source_id}"):
            rows = db.list_strm_index(key)
            db.delete_strm_index_ids(key, [row["file_id"] for row in rows])

    def test_full_sync_rejects_preexisting_symlink_escape(self) -> None:
        source_id = f"symlink-{uuid.uuid4().hex}"
        file = GuangYaFile("video", "Movie.mkv", False, 100, "etag", source_id)
        try:
            with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
                output = Path(root) / STRM_SUBDIR
                try:
                    output.symlink_to(Path(outside), target_is_directory=True)
                    if not output.is_symlink() and not os.path.islink(output):
                        self.skipTest("symlink unavailable on this platform")
                except OSError as exc:
                    self.skipTest(f"symlink unavailable: {exc}")
                with self.assertRaisesRegex(ValueError, "超出配置根目录"):
                    sync_strm(
                        source_id,
                        "http://media.invalid",
                        root,
                        client=_TreeClient({source_id: [file]}),
                        clean_invalid=False,
                    )
                self.assertEqual(list(Path(outside).iterdir()), [])
        finally:
            self._cleanup(source_id)

    def test_full_sync_never_deletes_historical_index_path_outside_current_root(self) -> None:
        source_id = f"outside-index-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        file = GuangYaFile("video", "Renamed.mkv", False, 200, "new-etag", source_id)
        try:
            with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
                outside_path = Path(outside) / "Movie.mkv.strm"
                outside_path.write_text("keep", encoding="utf-8")
                db.upsert_strm_index(
                    source_key,
                    file.file_id,
                    "old-etag",
                    100,
                    "Movie.mkv",
                    str(outside_path),
                )

                result = sync_strm(
                    source_id,
                    "http://media.invalid",
                    root,
                    client=_TreeClient({source_id: [file]}),
                    clean_invalid=False,
                )

                self.assertEqual(result["generated"], 1)
                self.assertTrue(outside_path.is_file())
                row = db.list_strm_index(source_key)[0]
                self.assertEqual(
                    Path(row["strm_path"]),
                    Path(root) / STRM_SUBDIR / "Renamed.strm",
                )
        finally:
            self._cleanup(source_id)

    def test_full_metadata_sync_removes_old_path_after_remote_rename(self) -> None:
        source_id = f"metadata-rename-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        file = GuangYaFile("nfo", "Renamed.nfo", False, 7, "new-etag", source_id)
        try:
            with tempfile.TemporaryDirectory() as root:
                old_path = Path(root) / STRM_SUBDIR / "Old.nfo"
                old_path.parent.mkdir(parents=True)
                old_path.write_bytes(b"old")
                db.upsert_strm_index(
                    source_key,
                    file.file_id,
                    "old-etag",
                    3,
                    "Old.nfo",
                    str(old_path),
                    f"sha256:{hashlib.sha256(b'old').hexdigest()}",
                )
                with patch("app.modules.strm.requests.get", return_value=_Response(b"newdata")):
                    result = sync_strm(
                        source_id,
                        "http://media.invalid",
                        root,
                        client=_MetadataClient({source_id: [file]}),
                        metadata_exts={"nfo"},
                        clean_invalid=False,
                    )

                new_path = Path(root) / STRM_SUBDIR / "Renamed.nfo"
                self.assertEqual(result["metadata_generated"], 1)
                self.assertEqual(result["metadata_cleaned"], 1)
                self.assertFalse(old_path.exists())
                self.assertEqual(new_path.read_bytes(), b"newdata")
                self.assertEqual(Path(db.list_strm_index(source_key)[0]["strm_path"]), new_path)
        finally:
            self._cleanup(source_id)

    def test_notification_change_redacts_signed_url_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "Movie.strm"
            change = relative_change(
                "failed",
                target,
                root,
                error="download https://storage.invalid/file?signature=secret token=hidden",
            )
            stats: dict = {}
            append_change(stats, change)

        serialized = json.dumps(stats, ensure_ascii=False)
        self.assertNotIn("storage.invalid", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertIn("URL 已隐藏", serialized)

    def test_metadata_cross_rename_fails_closed_without_overwriting_files(self) -> None:
        source_id = f"metadata-swap-{uuid.uuid4().hex}"
        source_key = f"guangya-meta:{source_id}"
        first = GuangYaFile("first", "B.nfo", False, 5, "etag-b", source_id)
        second = GuangYaFile("second", "A.nfo", False, 5, "etag-a", source_id)
        try:
            with tempfile.TemporaryDirectory() as root:
                output = Path(root) / STRM_SUBDIR
                output.mkdir(parents=True)
                path_a = output / "A.nfo"
                path_b = output / "B.nfo"
                path_a.write_bytes(b"old-a")
                path_b.write_bytes(b"old-b")
                db.upsert_strm_index(source_key, first.file_id, "old-a", 5, "A.nfo", str(path_a))
                db.upsert_strm_index(source_key, second.file_id, "old-b", 5, "B.nfo", str(path_b))

                with patch("app.modules.strm.requests.get") as request_get:
                    result = sync_strm(
                        source_id,
                        "http://media.invalid",
                        root,
                        client=_MetadataClient({source_id: [first, second]}),
                        metadata_exts={"nfo"},
                        clean_invalid=False,
                    )

                self.assertEqual(result["metadata_generated"], 0)
                self.assertEqual(result["metadata_failed"], 2)
                self.assertEqual(path_a.read_bytes(), b"old-a")
                self.assertEqual(path_b.read_bytes(), b"old-b")
                request_get.assert_not_called()
        finally:
            self._cleanup(source_id)

    def test_notification_labels_also_redact_secret_query_values(self) -> None:
        stats: dict = {}
        append_change(stats, {
            "action": "failed",
            "directory": "Anime?token=directory-secret",
            "filename": "Movie?signature=file-secret.strm",
        })

        serialized = json.dumps(stats, ensure_ascii=False)
        self.assertNotIn("directory-secret", serialized)
        self.assertNotIn("file-secret", serialized)
        self.assertIn("********", serialized)


class PlayGyRouteRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy_routes._playgy_signed_urls.clear()

    @staticmethod
    def _signed_args(file_id: str = "file", etag: str = "etag", size: str = "1") -> dict:
        from app.modules.playgy_signing import sign_playgy

        return {"v": "1", "sig": sign_playgy(file_id, etag, size)}

    def test_redirect_disables_cache_and_referrer(self) -> None:
        client = type(
            "Client",
            (),
            {
                "logged_in": True,
                "get_download_url": lambda _self, _file_id, **_kwargs: "https://storage.invalid/file?signature=secret",
            },
        )()
        with patch("app.routes.proxy.GuangYaClient", return_value=client):
            response = play_gy("file", "etag", "1", "Movie.mkv", **self._signed_args())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_redirect_short_caches_signed_url_and_bounds_provider_call(self) -> None:
        calls: list[dict] = []

        class Client:
            logged_in = True

            def get_download_url(self, _file_id: str, **kwargs) -> str:
                calls.append(kwargs)
                return "https://storage.invalid/file?signature=secret"

        with patch("app.routes.proxy.GuangYaClient", return_value=Client()):
            first = play_gy("cached", "etag", "1", "Movie.mkv", **self._signed_args("cached"))
            second = play_gy("cached", "etag", "1", "Movie.mkv", **self._signed_args("cached"))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["timeout"], proxy_routes.PLAYGY_SIGNED_URL_TIMEOUT_SECONDS
        )
        self.assertTrue(calls[0]["raise_timeout"])

    def test_cache_scope_changes_with_provider_token_and_bound_user_agent(self) -> None:
        calls: list[tuple[str, str]] = []

        class Raw:
            download_url_user_agent_bound = True

            def __init__(self, token: str):
                self.token = token

        class Client:
            logged_in = True

            def __init__(self, token: str):
                self.raw = Raw(token)

            def get_download_url(self, file_id: str, **_kwargs) -> str:
                calls.append((self.raw.token, file_id))
                return f"https://storage.invalid/{self.raw.token}/{file_id}"

        first_request = SimpleNamespace(headers={"user-agent": "Player/A"})
        second_request = SimpleNamespace(headers={"user-agent": "Player/B"})
        with patch(
            "app.routes.proxy.GuangYaClient",
            side_effect=[Client("token-a"), Client("token-b"), Client("token-b")],
        ):
            first = play_gy(
                "scoped", "etag", "1", "Movie.mkv",
                request=first_request, **self._signed_args("scoped"),
            )
            second = play_gy(
                "scoped", "etag", "1", "Movie.mkv",
                request=first_request, **self._signed_args("scoped"),
            )
            third = play_gy(
                "scoped", "etag", "1", "Movie.mkv",
                request=second_request, **self._signed_args("scoped"),
            )

        self.assertEqual(first.headers["location"], "https://storage.invalid/token-a/scoped")
        self.assertEqual(second.headers["location"], "https://storage.invalid/token-b/scoped")
        self.assertEqual(third.headers["location"], "https://storage.invalid/token-b/scoped")
        self.assertEqual(calls, [
            ("token-a", "scoped"),
            ("token-b", "scoped"),
            ("token-b", "scoped"),
        ])

    def test_provider_timeout_returns_retryable_gateway_timeout(self) -> None:
        class Client:
            logged_in = True

            def get_download_url(self, _file_id: str, **_kwargs) -> str:
                raise TimeoutError("signed provider secret")

        with patch("app.routes.proxy.GuangYaClient", return_value=Client()):
            response = play_gy("slow", "etag", "1", "Movie.mkv", **self._signed_args("slow"))

        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body), {"error": "光鸭播放地址获取超时"})
        self.assertNotIn("secret", response.body.decode("utf-8"))

    def test_missing_direct_url_does_not_fall_back_to_local_filename_search(self) -> None:
        client = type(
            "Client",
            (),
            {"logged_in": True, "get_download_url": lambda _self, _file_id, **_kwargs: ""},
        )()
        with patch("app.routes.proxy.GuangYaClient", return_value=client):
            response = play_gy("file", "etag", "1", "Movie.mkv", **self._signed_args())

        self.assertEqual(response.status_code, 404)

    def test_exception_response_does_not_expose_provider_details(self) -> None:
        class Client:
            logged_in = True

            def get_download_url(self, _file_id: str, **_kwargs) -> str:
                raise RuntimeError("https://storage.invalid/file?signature=secret")

        with patch("app.routes.proxy.GuangYaClient", return_value=Client()):
            response = play_gy("file", "etag", "1", "Movie.mkv", **self._signed_args())

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload, {"error": "光鸭播放地址获取失败"})
        self.assertNotIn("storage.invalid", response.body.decode("utf-8"))

    def test_invalid_signature_is_rejected_before_provider_call(self) -> None:
        with patch("app.routes.proxy.GuangYaClient") as client:
            response = play_gy("file", "etag", "1", "Movie.mkv", v="1", sig="0" * 64)

        self.assertEqual(response.status_code, 403)
        client.assert_not_called()


class OrganizeTaskManagerRobustnessTests(IsolatedDatabaseTestCase):
    def test_worker_start_failure_releases_operation_lock(self) -> None:
        from app.modules.organize_tasks import OrganizeTaskManager

        manager = OrganizeTaskManager()
        with patch("app.modules.organize_tasks.Organizer._validate_target_outside_source"), patch(
            "app.modules.organize_tasks.threading.Thread.start",
            side_effect=RuntimeError("thread unavailable"),
        ):
            result = manager.start(
                [{"id": "source", "name": "来源"}],
                OrganizeRules(target_dir_id="archive"),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(manager.task_status()["status"], "failed")
        self.assertTrue(manager._lock.acquire(blocking=False))
        manager._lock.release()

class StrmSchedulerRobustnessTests(IsolatedDatabaseTestCase):
    def test_stop_waits_for_run_blocking_caller_thread(self) -> None:
        from app.modules.scheduler import STRMScheduler

        started = threading.Event()
        release = threading.Event()

        class BlockingScheduler(STRMScheduler):
            def _execute_locked(self, trigger_type: str) -> dict:
                started.set()
                release.wait(timeout=2)
                try:
                    return {"ok": True, "trigger_type": trigger_type}
                finally:
                    with self._state_lock:
                        if self._worker is threading.current_thread():
                            self._worker = None
                        self._running = False
                        self._current_trigger = ""
                        self._run_options = {}
                    self._run_lock.release()

        scheduler = BlockingScheduler()
        result: dict = {}
        caller = threading.Thread(
            target=lambda: result.update(scheduler.run_blocking("telegram"))
        )
        caller.start()
        self.assertTrue(started.wait(timeout=1))
        stopper = threading.Thread(target=lambda: scheduler.stop(timeout=2))
        stopper.start()
        self.assertTrue(stopper.is_alive())
        release.set()
        caller.join(timeout=2)
        stopper.join(timeout=2)
        self.assertFalse(caller.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(result["ok"])

    def test_stop_rejects_new_strm_workers(self) -> None:
        from app.modules.scheduler import STRMScheduler

        scheduler = STRMScheduler()
        scheduler.stop(timeout=0)
        result = scheduler.trigger("manual")
        self.assertFalse(result["ok"])
        self.assertIn("正在停止", result["error"])

    def test_organize_trigger_is_coalesced_instead_of_dropped_while_busy(self) -> None:
        from app.modules.scheduler import STRMScheduler

        scheduler = STRMScheduler()
        started: list[tuple[str, dict]] = []

        def fake_start(trigger_type: str, options: dict) -> dict:
            started.append((trigger_type, dict(options)))
            scheduler._run_lock.release()
            return {"ok": True, "message": "started"}

        self.assertTrue(scheduler._run_lock.acquire(blocking=False))
        with patch.object(scheduler, "_start_locked_worker", side_effect=fake_start):
            result = scheduler.trigger(
                "organize",
                notify_override=True,
                detail_notify_override=True,
                emby_refresh_override=True,
                download_request_ids=[1],
                organize_changes=[{
                    "source_id": "archive", "kind": "video",
                    "file_id": "same", "action": "upsert",
                }],
            )
            self.assertTrue(result["ok"])
            self.assertTrue(result["queued"])
            updated = scheduler.trigger(
                "organize",
                notify_override=False,
                detail_notify_override=False,
                emby_refresh_override=False,
                download_request_ids=[2, 1],
                organize_changes=[{
                    "source_id": "archive", "kind": "video",
                    "file_id": "same", "action": "remove",
                }],
            )
            self.assertTrue(updated["ok"])
            self.assertTrue(updated["queued"])
            pending_status = scheduler.status()
            self.assertTrue(pending_status["pending_organize"])
            self.assertEqual(pending_status["pending_organize_changes"], 1)
            self.assertEqual(pending_status["pending_organize_requests"], 2)
            self.assertEqual(pending_status["pending_organize_chats"], 0)
            scheduler._run_lock.release()
            if scheduler._pending_thread:
                scheduler._pending_thread.join(timeout=2)

        self.assertEqual(started[0][0], "organize")
        self.assertFalse(started[0][1]["notify_override"])
        self.assertFalse(started[0][1]["detail_notify_override"])
        self.assertFalse(started[0][1]["emby_refresh_override"])
        self.assertEqual(started[0][1]["download_request_ids"], [1, 2])
        self.assertEqual(started[0][1]["organize_changes"], [{
            "source_id": "archive", "kind": "video",
            "file_id": "same", "action": "remove",
        }])
        final_status = scheduler.status()
        self.assertFalse(final_status["pending_organize"])
        self.assertEqual(final_status["pending_organize_changes"], 0)
        self.assertEqual(final_status["pending_organize_requests"], 0)
        self.assertEqual(final_status["pending_organize_chats"], 0)

    def test_per_file_failures_persist_partial_task_state(self) -> None:
        from app.modules import scheduler as scheduler_module

        scheduler = scheduler_module.STRMScheduler()
        partial_stats = {
            "total": 1,
            "generated": 0,
            "skipped": 0,
            "failed": 1,
            "metadata_total": 0,
            "metadata_generated": 0,
            "metadata_skipped": 0,
            "metadata_failed": 0,
            "metadata_cleaned": 0,
            "cleaned": 0,
            "clean_skipped": True,
            "empty_dirs_cleaned": 0,
            "directories": 1,
            "scan_elapsed_seconds": 0.1,
            "metadata_elapsed_seconds": 0.0,
            "error_samples": ["生成失败"],
            "changes": [],
            "omitted_count": 0,
        }
        sources = [{
            "id": "source-partial",
            "name": "来源",
            "rel_prefix": "",
            "source_key": "guangya:source-partial",
            "metadata_source_key": "guangya-meta:source-partial",
        }]
        values = {
            "GY_STRM_BASE_URL": "http://media.invalid",
            "STRM_ROOT": "/tmp/strm-partial",
        }
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=sources
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm", return_value=partial_stats
        ), patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking("manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["source_runtime"][0]["status"], "partial")
        self.assertEqual(db.get_last_task_run("strm_sync")["status"], "partial")


class OrganizeTraversalBudgetTests(IsolatedDatabaseTestCase):
    class RecordingTreeClient:
        def __init__(self, tree):
            self.tree = tree
            self.calls = []

        def list_dir(self, file_id: str):
            self.calls.append(str(file_id))
            return self.tree.get(str(file_id), [])

    @staticmethod
    def _dir(file_id: str, name: str, parent_id: str) -> GuangYaFile:
        return GuangYaFile(file_id, name, True, parent_id=parent_id)

    def test_scan_groups_root_and_top_level_directories_without_splitting_nested_paths(self) -> None:
        mib = 1024 * 1024
        root_video = GuangYaFile("root-video", "Root.S01E01.mkv", False, 100 * mib, parent_id="root")
        group_a = self._dir("group-a", "作品 A", "root")
        group_b = self._dir("group-b", "作品 B", "root")
        nested = self._dir("nested-a", "Season 01", "group-a")
        a_video = GuangYaFile("a-video", "A.S01E01.mkv", False, 100 * mib, parent_id="nested-a")
        b_video = GuangYaFile("b-video", "B.S01E01.mkv", False, 100 * mib, parent_id="group-b")
        client = self.RecordingTreeClient({
            "root": [group_a, group_b, root_video],
            "group-a": [nested],
            "nested-a": [a_video],
            "group-b": [b_video],
        })
        organizer = Organizer(client=client, scraper=object())
        stats = organizer._initial_stats()

        result = organizer._scan_source(
            OrganizeContext(source_dir_id="root", source_name="待整理"),
            OrganizeRules(),
            stats,
        )

        groups = {
            item.file.file_id: (item.source_group_id, item.source_group_path)
            for item in result.scanned_videos
        }
        self.assertEqual(groups["root-video"], ("root", "__root__"))
        self.assertEqual(groups["a-video"], ("group-a", "作品 A"))
        self.assertEqual(groups["b-video"], ("group-b", "作品 B"))

    def test_negative_max_files_keeps_legacy_fail_closed_semantics(self) -> None:
        video = GuangYaFile("video", "Episode.01.mkv", False, 1024, parent_id="root")
        client = self.RecordingTreeClient({"root": [video]})
        organizer = Organizer(client=client, scraper=object())

        with patch("app.modules.organize.execute_organize_plans") as execute:
            plans, stats = organizer.organize(
                "root", OrganizeRules(), dry_run=False, max_files=-1,
                post_actions=False,
            )

        self.assertEqual(plans, [])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(client.calls, [])
        execute.assert_called_once()
        self.assertIs(execute.call_args.args[0], organizer)
        self.assertEqual(execute.call_args.args[1], [])

    def test_positive_max_files_marks_preview_incomplete(self) -> None:
        videos = [
            GuangYaFile(
                f"video-{index}", f"Episode.{index:02d}.mkv", False,
                1024 * 1024 * 1024, parent_id="root",
            )
            for index in range(1, 4)
        ]
        client = self.RecordingTreeClient({"root": videos})
        scraper = type("Scraper", (), {
            "match": lambda _self, _name, _parent="", **_kwargs: MatchResult(
                tmdb_id="1", title="Example", year="2026", media_type="tv",
                confidence=1.0, status="matched", matched_by="test",
            )
        })()
        organizer = Organizer(client=client, scraper=scraper)

        with patch.object(
            organizer, "_build_plans",
            return_value=OrganizePlanningResult([], {}),
        ):
            _plans, stats = organizer.organize(
                "root", OrganizeRules(), dry_run=True, max_files=1,
            )

        self.assertEqual(stats["total"], 1)
        self.assertFalse(stats["scan_complete"])
        self.assertEqual(stats["scan_limited"], 1)
        self.assertEqual(stats["scan_limit_kind"], "max_files")

    def test_positive_max_files_blocks_cloud_writes(self) -> None:
        videos = [
            GuangYaFile(
                f"video-{index}", f"Episode.{index:02d}.mkv", False,
                1024 * 1024 * 1024, parent_id="root",
            )
            for index in range(1, 3)
        ]
        client = self.RecordingTreeClient({"root": videos})
        scraper = type("Scraper", (), {
            "match": lambda _self, _name, _parent="", **_kwargs: MatchResult(
                tmdb_id="1", title="Example", year="2026", media_type="tv",
                confidence=1.0, status="matched", matched_by="test",
            )
        })()
        organizer = Organizer(client=client, scraper=scraper)

        with patch("app.modules.organize.execute_organize_plans") as execute, self.assertRaisesRegex(
            RuntimeError, "文件数量上限"
        ):
            organizer.organize(
                "root", OrganizeRules(), dry_run=False, max_files=1, post_actions=False,
            )

        execute.assert_not_called()

    def test_cancelled_scan_does_not_read_root_directory(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        video = GuangYaFile("video", "Episode.01.mkv", False, 1024, parent_id="root")
        client = self.RecordingTreeClient({"root": [video]})
        organizer = Organizer(client=client, scraper=object())

        plans, stats = organizer.organize(
            "root", OrganizeRules(), dry_run=True, cancel_event=cancel_event,
        )

        self.assertEqual(plans, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(stats["stopped"], 1)
        self.assertEqual(stats["scan_dirs"], 0)
        self.assertEqual(stats["scan_list_dir_calls"], 0)

    def test_organize_depth_budget_marks_preview_incomplete(self) -> None:
        root = self._dir("d1", "一层", "root")
        second = self._dir("d2", "二层", "d1")
        client = self.RecordingTreeClient({"root": [root], "d1": [second], "d2": []})
        organizer = Organizer(
            client=client, scraper=object(), traversal_limits=(1, 10, 100)
        )

        plans, stats = organizer.organize("root", OrganizeRules(), dry_run=True)

        self.assertEqual(plans, [])
        self.assertEqual(client.calls, ["root", "d1"])
        self.assertFalse(stats["scan_complete"])
        self.assertEqual(stats["scan_limited"], 1)
        self.assertEqual(stats["scan_limit_kind"], "depth")
        self.assertEqual(stats["scan_dirs"], 2)
        self.assertTrue(any("安全上限" in item for item in stats["scan_errors"]))

    def test_organize_deduplicates_directory_ids_and_terminates_cycle(self) -> None:
        duplicate_a = self._dir("dup", "入口 A", "root")
        duplicate_b = self._dir("dup", "入口 B", "root")
        cycle = self._dir("root", "回到根目录", "dup")
        client = self.RecordingTreeClient({
            "root": [duplicate_a, duplicate_b],
            "dup": [cycle],
        })
        organizer = Organizer(
            client=client, scraper=object(), traversal_limits=(8, 10, 100)
        )

        _plans, stats = organizer.organize("root", OrganizeRules(), dry_run=True)

        self.assertEqual(client.calls, ["root", "dup"])
        self.assertTrue(stats["scan_complete"])
        self.assertEqual(stats["scan_limited"], 0)
        self.assertEqual(stats["scan_dirs"], 2)
        self.assertEqual(stats["scan_duplicate_dirs"], 2)

    def test_organize_entry_budget_fails_closed_before_cloud_writes(self) -> None:
        files = [
            GuangYaFile(f"f{index}", f"note-{index}.txt", False, 1, parent_id="root")
            for index in range(3)
        ]
        client = self.RecordingTreeClient({"root": files})
        organizer = Organizer(
            client=client, scraper=object(), traversal_limits=(8, 10, 2)
        )

        with patch("app.modules.organize.execute_organize_plans") as execute, self.assertRaisesRegex(
            RuntimeError, "扫描超过安全上限"
        ):
            organizer.organize(
                "root", OrganizeRules(), dry_run=False, require_complete_scan=True
            )

        execute.assert_not_called()

    def test_clean_empty_budget_aborts_without_deleting_directories(self) -> None:
        child = self._dir("child", "子目录", "root")
        grandchild = self._dir("grandchild", "孙目录", "child")
        client = self.RecordingTreeClient({
            "root": [child],
            "child": [grandchild],
            "grandchild": [],
        })
        organizer = Organizer(
            client=client, scraper=object(), traversal_limits=(1, 10, 100)
        )

        with patch(
            "app.modules.organize.execute_recycle_bin_delete"
        ) as recycle, self.assertRaisesRegex(RuntimeError, "扫描超过安全上限"):
            organizer.clean_empty_dirs("root")

        recycle.assert_not_called()
        self.assertEqual(client.calls, ["root", "child"])
