from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from fastapi.testclient import TestClient

from app import config, database as db
from app.clients.base import DashboardData
from app.clients.guangya import GuangYaFile
from app.clients.qbittorrent import TorrentAddResult, TorrentTask
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.main import create_app
from app.modules.gcid_manifest import (
    ManifestValidationError,
    export_manifest,
    validate_manifest,
)
from app.modules.download_dispatcher import (
    create_request,
    dispatch_request,
    normalize_download_url,
    parse_torrent_metadata,
    request_key,
    request_keys,
    torrent_download_input,
)
from app.modules.download_tracker import DownloadTracker
from app.modules.local_media_scheduler import (
    LocalMediaProbeRetryable,
    LocalMediaSourceMigrationRequired,
)
from app.modules.media_proxy import (
    _parse_range,
    _websocket_upstream_url,
    local_file_response,
    resolve_local_binding,
    rewrite_playback_info,
    validate_listen_host,
    validate_upstream_url,
)
from app.modules.naming import build_context, render_template, validate_template
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_execution import execute_organize_plans
from app.modules.organize_postprocess import companion_target_name
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.runtime_log import (
    RuntimeLogChunk,
    RuntimeLogEvent,
    clear_logs,
    log_generation,
    log_identity,
    read_from_offset,
    read_last_lines,
    read_stream_chunk,
)
from app.modules.rss import RSSEngine
from app.modules.scraper import (
    MatchResult,
    TMDBScraper,
    _explicit_tmdb_id_from_path,
    extract_recognition_context,
    parse_release_position,
)
from app.modules.strm import STRM_SUBDIR, sync_strm
from tests.support import (
    InitializedWebTestCase,
    IsolatedDatabaseTestCase,
    release_parse_fields,
    release_parse_result,
)


def _parse_fields(parser, filename: str, parent_path: str = "") -> dict[str, object]:
    return release_parse_fields(parser.parse_media(filename, parent_path))


class GCIDManifestTests(unittest.TestCase):
    def setUp(self):
        file_type = GuangYaFile
        self.tree = {
            "root": [file_type("dir", "剧集", True)],
            "dir": [
                file_type("b", "B.mkv", False, 20, "gcid-b", "dir"),
                file_type("a", "A.mkv", False, 10, "gcid-a", "dir"),
            ],
        }
        self.client = type("Client", (), {
            "list_dir": lambda inner, file_id: self.tree[file_id],
            "file_info": lambda inner, file_id: None,
        })()

    def test_recursive_export_and_integrity(self):
        manifest = export_manifest(self.client, "root", "测试")
        self.assertEqual([item["path"] for item in manifest["files"]], ["剧集/A.mkv", "剧集/B.mkv"])
        self.assertEqual(validate_manifest(manifest)["file_count"], 2)

    def test_tampering_is_rejected(self):
        manifest = export_manifest(self.client, "root")
        manifest["files"][0]["size"] = 99
        with self.assertRaises(ManifestValidationError):
            validate_manifest(manifest)


class ScraperAndOrganizerTests(IsolatedDatabaseTestCase):
    def test_strict_pinyin_matching(self):
        scraper = TMDBScraper()
        self.assertEqual(scraper._pinyin_similarity("Xiao Fang", "小芳"), 1.0)
        self.assertEqual(scraper._pinyin_similarity("Xi", "西"), 0.0)
        candidates = [{"id": 1, "name": "小芳", "original_name": "小芳", "first_air_date": "2026-01-01"}]
        result = scraper._pick_best("Xiao Fang", "2026", "tv", candidates)
        self.assertEqual((result.tmdb_id, result.confidence), ("1", 1.0))
        self.assertEqual((result.provider, result.external_id), ("tmdb", "1"))
        self.assertEqual(
            [(item.provider, item.external_id) for item in result.candidates],
            [("tmdb", "1")],
        )

    def test_optional_categories(self):
        details = {
            "concert": {
                "genres": [{"id": 10402}],
                "origin_country": ["US"],
                "name": "The Eras Tour Concert",
            },
            "kids": {
                "genres": [{"id": 10762}],
                "origin_country": ["CN"],
                "name": "小小科学家",
            },
        }
        scraper = type("Scraper", (), {
            "get_detail": lambda inner, tmdb_id, media_type: details[tmdb_id],
        })()
        organizer = Organizer(client=object(), scraper=scraper)

        concert = MatchResult(
            tmdb_id="concert", title="The Eras Tour Concert", year="2023", media_type="movie"
        )
        self.assertEqual(organizer.classify(concert, OrganizeRules(add_concert=True))[0], "演唱会")
        self.assertEqual(organizer.classify(concert, OrganizeRules(add_concert=False))[0], "电影")

        kids = MatchResult(tmdb_id="kids", title="小小科学家", year="2026", media_type="tv")
        self.assertEqual(organizer.classify(kids, OrganizeRules(add_kids=True))[0], "儿童节目")
        self.assertEqual(organizer.classify(kids, OrganizeRules(add_kids=False))[0], "剧集")

    def test_legacy_templates_and_scope_are_ignored_by_fixed_naming_contract(self):
        organizer = Organizer(client=object(), scraper=object())
        match = MatchResult(tmdb_id="88", title="Show: Name", year="2026", media_type="tv")
        file = GuangYaFile("v1", "Show.Name.S01E02.1080p.WEB-DL.H265.mkv", False)
        rules = OrganizeRules(
            media_info_enabled=False,
            rename_enabled=False,
            tv_template="LEGACY-${showTitle}.${ext}",
            show_dir_template="LEGACY-${showTitle}",
            naming_scope="local",
        )
        self.assertEqual(
            organizer.build_new_name(match, file, {"season": 1, "episode": 2}, rules),
            "Show_ Name.2026.S01E02-WEB-DL.1080p.H.265.mkv",
        )
        self.assertEqual(
            organizer.build_show_dir(match, rules),
            "Show_ Name (2026) {tmdb-88}",
        )

    def test_naming_template_validation_rejects_unknown_or_malformed_fields(self):
        context = build_context(title="Movie", year="2026", tmdb_id="1", ext="mkv")
        self.assertEqual(render_template("${showTitle}.${ext}", context), "Movie.mkv")
        with self.assertRaises(ValueError):
            validate_template("${unknownField}.${ext}")
        with self.assertRaises(ValueError):
            validate_template("${showTitle")

    def test_companion_metadata_matches_video_basename(self):
        organizer = Organizer(client=object(), scraper=object())
        plan = OrganizePlan(
            file_id="v1", original_name="Show.S01E01.mkv", original_path="Show",
        )
        candidates = [
            GuangYaFile("s1", "Show.S01E01.zh.ass", False),
            GuangYaFile("n1", "Show.S01E01.nfo", False),
            GuangYaFile("s2", "Show.S01E02.zh.ass", False),
        ]
        matched = organizer._companions_for_plan(plan, candidates)
        self.assertEqual([item.file_id for item in matched], ["s1", "n1"])

    def test_shorter_video_does_not_steal_extended_metadata(self):
        organizer = Organizer(client=object(), scraper=object())
        short_plan = OrganizePlan(
            file_id="v1", original_name="Show.S01E01.mkv", original_path="Show",
        )
        extended_plan = OrganizePlan(
            file_id="v2", original_name="Show.S01E01.Extended.mkv", original_path="Show",
        )
        candidates = [
            GuangYaFile("n1", "Show.S01E01.Extended.nfo", False),
            GuangYaFile("p1", "Show.S01E01-poster.jpg", False),
        ]

        self.assertEqual(
            [item.file_id for item in organizer._companions_for_plan(short_plan, candidates)],
            ["p1"],
        )
        self.assertEqual(
            [item.file_id for item in organizer._companions_for_plan(extended_plan, candidates)],
            ["n1"],
        )

    def test_unmatched_execution_persists_correctable_media_group(self):
        companion = GuangYaFile("sub", "Unknown.S01E01.zh.ass", False, 2, "e2", "source-id")
        plan = OrganizePlan(
            file_id="video", original_name="Unknown.S01E01.mkv", original_path="source",
            original_parent_id="source-id", size=100,
            match=MatchResult(media_type="tv", need_confirm=True, error="TMDB 无结果"),
            action="skip", note="需人工确认",
        )
        organizer = Organizer(client=object(), scraper=object())
        stats = {"stopped": 0}
        with patch("app.modules.organize.add_organize_log", return_value=42) as add_log, patch(
            "app.modules.organize.add_organize_log_items"
        ) as add_items:
            execute_organize_plans(organizer,
                [plan], OrganizeRules(), stats, {"source": [companion]}, None,
                source_dir_id="configured-source",
            )
        self.assertEqual(add_log.call_args.args[4], "manual")
        self.assertEqual(add_log.call_args.kwargs["source_dir_id"], "configured-source")
        saved = add_items.call_args.args[1]
        self.assertEqual([(item["role"], item["file_id"]) for item in saved], [
            ("video", "video"), ("subtitle", "sub")
        ])
        self.assertTrue(all(item["status"] == "manual" for item in saved))

    def test_success_audit_resolves_previous_manual_record(self):
        pending_id = db.add_organize_log(
            "guangya", "source", "", "video", "manual", "",
            original_parent_id="source-id", original_name="Unknown.S01E01.mkv",
            current_parent_id="source-id", current_name="Unknown.S01E01.mkv",
            media_type="tv", title="Unknown", error="需要人工确认",
            legacy_incomplete=False,
        )
        db.add_organize_log_items(pending_id, [{
            "file_id": "video", "role": "video",
            "original_parent_id": "source-id", "original_name": "Unknown.S01E01.mkv",
            "current_parent_id": "source-id", "current_name": "Unknown.S01E01.mkv",
            "status": "manual", "error": "需要人工确认",
        }])

        success_id = Organizer._write_organize_audit(
            ("guangya", "source", "剧集/Unknown/Season 1/Unknown.S01E01.mkv", "video", "success", "1"),
            {
                "original_parent_id": "source-id",
                "original_name": "Unknown.S01E01.mkv",
                "current_parent_id": "target-id",
                "current_name": "Unknown.S01E01.mkv",
                "target_parent_id": "target-id",
                "media_type": "tv",
                "title": "Unknown",
                "legacy_incomplete": False,
            },
            [{
                "file_id": "video", "role": "video",
                "original_parent_id": "source-id", "original_name": "Unknown.S01E01.mkv",
                "current_parent_id": "target-id", "current_name": "Unknown.S01E01.mkv",
                "target_parent_id": "target-id", "target_name": "Unknown.S01E01.mkv",
                "status": "success",
            }],
        )

        self.assertGreater(success_id, pending_id)
        self.assertEqual(db.get_organize_log(pending_id)["status"], "confirmed")
        self.assertEqual(db.list_organize_log_items(pending_id)[0]["status"], "confirmed")
        timeline = db.list_organize_timeline(origin="guangya", limit=10)
        self.assertEqual([row["id"] for row in timeline], [success_id])

    def test_safe_replacement_keeps_existing_until_incoming_succeeds(self):
        operations = []
        existing = GuangYaFile("old-file", "Movie (2026)(tmdb-1).mkv", False, 10)

        class FakeRaw:
            def fs_delete(self, file_ids):
                operations.append(("delete", tuple(file_ids)))

        class FakeClient:
            raw = FakeRaw()

            def list_dir(self, dir_id):
                return [existing]

            def rename(self, file_id, name):
                operations.append(("rename", file_id, name))

            def move(self, file_ids, target_id):
                operations.append(("move", tuple(file_ids), target_id))
                if file_ids == ["new-file"]:
                    raise RuntimeError("simulated move failure")

        match = MatchResult(tmdb_id="1", title="Movie", year="2026", media_type="movie")
        plan = OrganizePlan(
            file_id="new-file", original_name="Movie.Remux.mkv",
            original_path="source", original_parent_id="source-id", size=100,
            match=match, new_name=existing.name, target_path="电影",
        )
        organizer = Organizer(client=FakeClient(), scraper=object())
        organizer._ensure_dir_chain = lambda root, path, *_args: "target-id"
        stats = {"moved": 0, "renamed": 0, "metadata_moved": 0,
                 "stopped": 0, "skipped": 0, "conflict": 0, "failed": 0}
        with patch("app.modules.organize.add_organize_log"), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            execute_organize_plans(organizer,
                [plan], OrganizeRules(target_dir_id="target", conflict_strategy=2),
                stats, {}, None,
            )
        self.assertEqual(stats["failed"], 1)
        self.assertNotIn(("delete", ("old-file",)), operations)
        self.assertEqual(operations[-1], ("rename", "old-file", existing.name))


class MediaProxyTests(InitializedWebTestCase):
    def test_validation_rejects_ssrf_and_path_escape(self):
        self.assertEqual(validate_upstream_url("http://127.0.0.1:8096/"), "http://127.0.0.1:8096")
        with self.assertRaises(ValueError):
            validate_upstream_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            validate_upstream_url("http://user:pass@127.0.0.1:8096")
        with self.assertRaises(ValueError):
            validate_listen_host("192.168.88.10")
        with tempfile.TemporaryDirectory() as root:
            resolved = resolve_local_binding(root, "Movies/video.mkv")
            self.assertEqual(resolved, (Path(root) / "Movies/video.mkv").resolve())
            with self.assertRaisesRegex(ValueError, "越界"):
                resolve_local_binding(root, "../secret.mkv")
            with self.assertRaisesRegex(ValueError, "相对路径"):
                resolve_local_binding(root, str(Path(root).resolve() / "video.mkv"))

    def test_range_parser_supports_standard_and_suffix_ranges(self):
        self.assertEqual(_parse_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(_parse_range("bytes=7-", 10), (7, 9))
        self.assertEqual(_parse_range("bytes=-3", 10), (7, 9))
        with self.assertRaises(ValueError):
            _parse_range("bytes=10-12", 10)
        with self.assertRaises(ValueError):
            _parse_range("bytes=1-2,4-5", 10)

    def test_local_file_response_range_and_head(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "video.bin"
            path.write_bytes(b"0123456789")
            ranged = SimpleNamespace(method="GET", headers={"range": "bytes=2-5"})
            response = local_file_response(ranged, path)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            body = asyncio.run(self._stream_body(response))
            self.assertEqual(body, b"2345")
            head = SimpleNamespace(method="HEAD", headers={})
            head_response = local_file_response(head, path)
            self.assertEqual(head_response.status_code, 200)
            self.assertEqual(head_response.headers["content-length"], "10")

    @staticmethod
    async def _stream_body(response) -> bytes:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    def test_websocket_target_is_derived_only_from_fixed_upstream(self):
        websocket = SimpleNamespace(url=SimpleNamespace(query="api_key=client-token"))
        self.assertEqual(
            _websocket_upstream_url(
                "https://media.example.test/base",
                websocket,
                "socket",
            ),
            "wss://media.example.test/base/socket?api_key=client-token",
        )

    def test_playback_info_rewrite_forces_bound_source_to_direct_play(self):
        payload = {
            "MediaSources": [{
                "Id": "source-1",
                "SupportsDirectPlay": False,
                "SupportsDirectStream": False,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/item/master.m3u8",
            }]
        }
        binding = {
            "id": 1,
            "source_type": "guangya",
            "guangya_file_id": "file-1",
        }
        with patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=binding):
            rewritten, changed = rewrite_playback_info(payload, 1, "item-1")
        self.assertTrue(changed)
        source = rewritten["MediaSources"][0]
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsDirectStream"])
        self.assertFalse(source["SupportsTranscoding"])
        self.assertEqual(source["Path"], "/Videos/item-1/stream")
        self.assertNotIn("TranscodingUrl", source)

    def test_database_crud_and_binding_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "proxy.db"
            media_root = Path(root) / "media"
            media_root.mkdir()
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                instance_id = db.add_media_proxy_instance(
                    name="Test Jellyfin",
                    server_type="jellyfin",
                    upstream_url="http://127.0.0.1:8096",
                    api_key="secret",
                    listen_host="127.0.0.1",
                    listen_port=18096,
                    local_root=str(media_root),
                    enabled=1,
                )
                self.assertEqual(db.get_media_proxy_instance(instance_id)["name"], "Test Jellyfin")
                binding_id = db.add_media_proxy_binding(
                    instance_id=instance_id,
                    media_item_id="item-1",
                    media_source_id="source-1",
                    source_type="local",
                    local_relative_path="Movies/video.mkv",
                )
                binding = db.get_media_proxy_binding(instance_id, "item-1", "source-1")
                self.assertEqual(int(binding["id"]), binding_id)
                self.assertTrue(db.delete_media_proxy_instance(instance_id))
                self.assertEqual(db.list_media_proxy_bindings(instance_id), [])

    def test_management_api_masks_secret_and_rejects_absolute_binding(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "proxy-api.db"
            app = create_app()
            with patch("app.database.DB_PATH", test_db):
                with TestClient(app, raise_server_exceptions=False) as client:
                    login_page = client.get("/login")
                    token = SecurityTests._csrf_token(login_page)
                    from app.config import web_credentials
                    username, password = web_credentials()
                    login = client.post(
                        "/login",
                        data={"csrf_token": token, "username": username, "password": password},
                        follow_redirects=False,
                    )
                    self.assertEqual(login.status_code, 302)
                    settings = client.get("/settings")
                    headers = {"X-CSRF-Token": SecurityTests._csrf_token(settings)}
                    created = client.post(
                        "/api/media-proxy",
                        headers=headers,
                        json={
                            "name": "API Proxy",
                            "server_type": "jellyfin",
                            "upstream_url": "http://127.0.0.1:8096",
                            "api_key": "secret-key",
                            "listen_host": "127.0.0.1",
                            "listen_port": 18097,
                            "local_root": root,
                            "enabled": False,
                        },
                    )
                    self.assertEqual(created.status_code, 201, created.text)
                    instance = created.json()["instance"]
                    self.assertEqual(instance["api_key"], "********")
                    rejected = client.post(
                        f"/api/media-proxy/{instance['id']}/bindings",
                        headers=headers,
                        json={
                            "media_item_id": "item",
                            "source_type": "local",
                            "local_relative_path": str(Path(root) / "absolute.mkv"),
                        },
                    )
                    self.assertEqual(rejected.status_code, 400)


class DatabaseMigrationTests(unittest.TestCase):
    def test_stale_rss_submission_is_recovered_as_unknown_and_not_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "stale-rss.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                sub_id = db.add_rss_subscription("Legacy", "https://example.com/rss")
                entry_id = db.add_rss_entry(
                    sub_id,
                    "Interrupted submit",
                    "interrupted-guid",
                    payload='{"torrent_url":"magnet:?xt=legacy"}',
                )
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE rss_entries SET status='submitting',"
                        "submitted_at=datetime('now','localtime','-16 minutes') WHERE id=?",
                        (entry_id,),
                    )
                db.init_db()
                row = db.get_rss_entry(entry_id)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], "submission_outcome_unknown")
                self.assertEqual(row["failure_retryable"], 0)
                self.assertIsNotNone(row["failed_at"])
class DownloadRequestLocalMediaTests(unittest.TestCase):
    @staticmethod
    def _task(content_path: str, save_path: str) -> TorrentTask:
        return TorrentTask(
            hash="hash", name="Movie.2026", progress=1.0, state="uploading",
            save_path=save_path, content_path=content_path, size=100,
            downloaded=100, dlspeed=0, upspeed=0, eta=0, ratio=0,
            category="", added_on=0,
        )

    def test_completed_request_is_linked_to_new_local_media_task(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request("local-key", "magnet")
                db.update_download_request(request_id, qb_status="completed", status="completed")
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.return_value = 77
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler
                ):
                    tracker._start_local_import({"id": request_id}, self._task("/downloads/Movie.mkv", "/downloads"))
                row = db.get_download_request(request_id)
                self.assertEqual(row["local_import_status"], "pending")
                self.assertEqual(row["local_import_target"], "local-media-task:77")
                self.assertEqual(db.update_download_request_for_local_media_task(77, "completed"), 1)
                self.assertFalse(db.link_download_request_to_local_media_task(
                    request_id, 77, "/downloads/Movie.mkv"
                ))
                self.assertEqual(db.get_download_request(request_id)["local_import_status"], "completed")
                self.assertEqual(db.list_active_download_requests(include_local_import=True), [])

    def test_retryable_local_media_probe_recovers_without_becoming_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request("retryable-key", "magnet")
                db.update_download_request(request_id, qb_status="completed", status="completed")
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.side_effect = [
                    LocalMediaProbeRetryable("扫描路径不存在: Movie.mkv"),
                    77,
                ]
                task = self._task("/downloads/Movie.mkv", "/downloads")
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler",
                    return_value=scheduler,
                ):
                    tracker._start_local_import(db.get_download_request(request_id), task)
                    waiting = db.get_download_request(request_id)
                    self.assertEqual(waiting["local_import_status"], "pending")
                    self.assertEqual(waiting["local_import_attempts"], 1)
                    self.assertIn("扫描路径不存在", waiting["local_import_error"])
                    self.assertTrue(db.list_active_download_requests(include_local_import=True))

                    tracker._start_local_import(waiting, task)

                recovered = db.get_download_request(request_id)
                self.assertEqual(recovered["local_import_status"], "pending")
                self.assertEqual(recovered["local_import_target"], "local-media-task:77")
                self.assertEqual(recovered["local_import_error"], "")

    def test_retryable_local_media_probe_becomes_visible_failure_after_limit(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request("retry-limit-key", "magnet")
                db.update_download_request(request_id, qb_status="completed", status="completed")
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.side_effect = LocalMediaProbeRetryable(
                    "目录暂时不可完整读取: Movie.2026"
                )
                task = self._task("/downloads/Movie.2026", "/downloads")
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler",
                    return_value=scheduler,
                ):
                    for _ in range(8):
                        tracker._start_local_import(db.get_download_request(request_id), task)

                failed = db.get_download_request(request_id)
                self.assertEqual(failed["local_import_status"], "failed")
                self.assertEqual(failed["local_import_attempts"], 8)
                self.assertIn("暂时不可完整读取", failed["local_import_error"])
                self.assertEqual(db.list_active_download_requests(include_local_import=True), [])

    def test_legacy_local_media_source_is_visible_configuration_failure(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request("legacy-source-key", "magnet")
                db.update_download_request(request_id, qb_status="completed", status="completed")
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.side_effect = (
                    LocalMediaSourceMigrationRequired(
                        "媒体来源仍使用已停用的 Windows/UNC 路径；"
                        "请改为 Docker 容器路径"
                    )
                )
                task = self._task(r"D:\Downloads\Movie.mkv", r"D:\Downloads")
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler",
                    return_value=scheduler,
                ):
                    tracker._start_local_import(db.get_download_request(request_id), task)

                row = db.get_download_request(request_id)
                self.assertEqual(row["local_import_status"], "failed")
                self.assertIn("Windows/UNC", row["local_import_error"])
                self.assertIn("Docker 容器路径", row["local_import_error"])
                self.assertEqual(db.list_active_download_requests(include_local_import=True), [])

    def test_legacy_source_failure_does_not_overwrite_terminal_import_state(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.side_effect = (
                    LocalMediaSourceMigrationRequired("请改为 Docker 容器内绝对路径")
                )
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler",
                    return_value=scheduler,
                ):
                    for index, terminal_status in enumerate(("completed", "skipped"), start=1):
                        request_id, _ = db.create_download_request(
                            f"terminal-legacy-{index}", "magnet"
                        )
                        db.update_download_request(
                            request_id,
                            local_import_status="pending",
                            local_import_error="pending",
                        )
                        stale_row = db.get_download_request(request_id)
                        db.update_download_request(
                            request_id,
                            local_import_status=terminal_status,
                            local_import_error="terminal",
                            local_import_completed_at="2026-08-22 00:00:00",
                        )

                        tracker._start_local_import(
                            stale_row,
                            self._task(r"D:\Downloads\Movie.mkv", r"D:\Downloads"),
                        )

                        current = db.get_download_request(request_id)
                        self.assertEqual(current["local_import_status"], terminal_status)
                        self.assertEqual(current["local_import_error"], "terminal")
                        self.assertEqual(
                            current["local_import_completed_at"], "2026-08-22 00:00:00"
                        )

    def test_unmatched_completed_request_is_marked_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request("unmatched-key", "magnet")
                db.update_download_request(request_id, qb_status="completed", status="completed")
                tracker = DownloadTracker()
                scheduler = Mock()
                scheduler.enqueue_completed_torrent.return_value = None
                with patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler
                ):
                    tracker._start_local_import({"id": request_id}, self._task("/downloads/Movie.mkv", "/downloads"))
                row = db.get_download_request(request_id)
                self.assertEqual(row["local_import_status"], "skipped")
                self.assertIn("未命中", row["local_import_error"])
                self.assertEqual(db.list_active_download_requests(include_local_import=True), [])


class OrganizeCorrectionTests(unittest.TestCase):
    def test_batch_validation_rejects_movies_before_cloud_write(self):
        service = OrganizeCorrectionService(client=Mock(), scraper=object())
        details = {
            1: {"id": 1, "media_type": "tv", "tmdb_id": "11",
                "allowed_actions": {"reorganize": True}},
            2: {"id": 2, "media_type": "movie", "tmdb_id": "22",
                "allowed_actions": {"reorganize": True}},
        }
        with patch.object(service, "detail", side_effect=lambda log_id: details[log_id]):
            with self.assertRaisesRegex(ValueError, "包含电影"):
                service.validate_batch([1, 2], "reorganize")
        service.client.assert_not_called()

    def test_batch_validation_requires_unique_multiple_tv_logs(self):
        service = OrganizeCorrectionService(client=object(), scraper=object())
        with self.assertRaisesRegex(ValueError, "至少选择两条"):
            service.validate_batch([1], "revert")
        with self.assertRaisesRegex(ValueError, "重复 ID"):
            service.validate_batch([1, 1], "delete")

    def test_batch_reorganize_uses_each_logs_snapshot_and_collects_failures(self):
        service = OrganizeCorrectionService(client=object(), scraper=object())
        details = {
            1: {"id": 1, "media_type": "tv", "tmdb_id": "101",
                "allowed_actions": {"reorganize": True}},
            2: {"id": 2, "media_type": "tv", "tmdb_id": "202",
                "allowed_actions": {"reorganize": True}},
        }
        entries = [
            {"log_id": 1, "expected_version": 3, "operation_token": "a"},
            {"log_id": 2, "expected_version": 7, "operation_token": "b"},
        ]
        with patch.object(service, "detail", side_effect=lambda log_id: details[log_id]), patch.object(
            service, "reorganize", side_effect=[
                {"success": True, "warnings": []}, RuntimeError("simulated batch failure")
            ],
        ) as reorganize:
            result = service.run_batch("reorganize", entries)
        self.assertFalse(result["success"])
        self.assertEqual([item["log_id"] for item in result["completed"]], [1])
        self.assertEqual(result["failed"], [{"log_id": 2, "error": "simulated batch failure"}])
        self.assertEqual(reorganize.call_args_list[0].args, (1, "a", 3, "101", "tv"))
        self.assertEqual(reorganize.call_args_list[1].args, (2, "b", 7, "202", "tv"))

    def test_partial_failure_is_frozen_for_manual_reconciliation(self):
        service = OrganizeCorrectionService(client=object(), scraper=object())
        row = {
            "id": 5, "status": "partial_failed", "legacy_incomplete": 0,
            "operation_token": "", "version": 2,
        }
        items = [{
            "file_id": "video", "role": "video",
            "original_parent_id": "source", "original_name": "Movie.mkv",
            "current_parent_id": "target", "current_name": "电影.mkv",
        }]
        with patch("app.database.get_organize_log", return_value=row), patch(
            "app.database.list_organize_log_items", return_value=items
        ), patch("app.database.list_organize_operation_steps", return_value=[]):
            detail = service.detail(5)
        self.assertTrue(detail["allowed_actions"]["search"])
        self.assertTrue(detail["allowed_actions"]["preview"])
        for action in ("reorganize", "return_to_source", "revert", "delete"):
            self.assertFalse(detail["allowed_actions"][action])
        self.assertIn("冻结自动写操作", detail["safety_notice"])
        self.assertIn("人工核对", detail["safety_notice"])

    def test_companion_target_name_preserves_language_and_metadata_suffix(self):
        self.assertEqual(
            companion_target_name(
                "Show.S01E01.mkv", "节目.2026.S01E01.mkv", "Show.S01E01.zh.forced.ass"
            ),
            "节目.2026.S01E01.zh.forced.ass",
        )
        self.assertEqual(
            companion_target_name(
                "Movie.mkv", "电影 (2026)(tmdb-1).mkv", "Movie-poster.jpg"
            ),
            "电影 (2026)(tmdb-1)-poster.jpg",
        )

    def test_legacy_log_is_read_only_and_cannot_be_claimed(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source/folder", "电影/Movie.mkv", "f1", "success", "1"
                )
                service = OrganizeCorrectionService(client=object(), scraper=object())
                detail = service.detail(log_id)
                self.assertTrue(detail["legacy_incomplete"])
                self.assertIn("禁止猜测式回退", detail["safety_notice"])
                self.assertFalse(detail["allowed_actions"]["return_to_source"])
                self.assertFalse(db.claim_organize_log_operation(
                    log_id, "token", "returning", ("success",), detail["version"]
                ))

    def test_complete_log_detail_and_atomic_claim(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source", "剧集/节目/Show.S01E01.mkv", "video", "success", "88",
                    original_parent_id="source-id", original_name="Show.S01E01.mkv",
                    current_parent_id="target-id", current_name="Show.2026.S01E01.mkv",
                    target_parent_id="target-id", media_type="tv", title="节目", year="2026",
                    season=1, episode=1, legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [
                    {"file_id": "video", "role": "video", "original_parent_id": "source-id",
                     "original_name": "Show.S01E01.mkv", "current_parent_id": "target-id",
                     "current_name": "Show.2026.S01E01.mkv", "size": 100},
                    {"file_id": "sub", "role": "subtitle", "original_parent_id": "source-id",
                     "original_name": "Show.S01E01.zh.ass", "current_parent_id": "target-id",
                     "current_name": "Show.2026.S01E01.zh.ass", "size": 2},
                ])
                detail = OrganizeCorrectionService(client=object(), scraper=object()).detail(log_id)
                self.assertFalse(detail["legacy_incomplete"])
                self.assertIsNone(detail["release_parse"])
                self.assertEqual(len(detail["items"]), 2)
                self.assertTrue(detail["allowed_actions"]["reorganize"])
                self.assertTrue(db.claim_organize_log_operation(
                    log_id, "token-a", "reorganizing", ("success",), detail["version"]
                ))
                self.assertFalse(db.claim_organize_log_operation(
                    log_id, "token-b", "reorganizing", ("success",), detail["version"]
                ))

    def test_organize_detail_decodes_release_parse_diagnostic(self):
        diagnostic = {
            "title": "节目", "year": "2026", "media_type": "tv",
            "source_position": {"season": 1, "episode": 13},
            "effective_position": {"season": 2, "episode": 1},
            "evidence": [{
                "kind": "episode", "source": "release_context",
                "value": 13, "confidence": 1.0,
            }],
        }
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source", "动漫/节目/Season 02/节目.S02E01.mkv",
                    "video", "success", "88",
                    original_parent_id="source-id", original_name="Show.E13.mkv",
                    current_parent_id="target-id", current_name="节目.S02E01.mkv",
                    target_parent_id="target-id", media_type="tv", title="节目",
                    year="2026", season=2, episode=1, release_parse=diagnostic,
                    legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [{
                    "file_id": "video", "role": "video",
                    "original_parent_id": "source-id", "original_name": "Show.E13.mkv",
                    "current_parent_id": "target-id", "current_name": "节目.S02E01.mkv",
                }])
                detail = OrganizeCorrectionService(
                    client=object(), scraper=object()
                ).detail(log_id)
        self.assertEqual(detail["release_parse"], diagnostic)
        self.assertNotIn("release_parse_json", detail)

    def test_reorganize_preview_is_read_only_and_renames_media_group(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source", "old/Show.S01E01.mkv", "video", "success", "1",
                    original_parent_id="source-id", original_name="Show.S01E01.mkv",
                    current_parent_id="old-id", current_name="Show.S01E01.mkv",
                    target_parent_id="old-id", media_type="tv", title="旧节目", year="2025",
                    season=1, episode=1, legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [
                    {"file_id": "video", "role": "video", "original_parent_id": "source-id",
                     "original_name": "Show.S01E01.mkv", "current_parent_id": "old-id",
                     "current_name": "Show.S01E01.mkv", "size": 100},
                    {"file_id": "sub", "role": "subtitle", "original_parent_id": "source-id",
                     "original_name": "Show.S01E01.zh.ass", "current_parent_id": "old-id",
                     "current_name": "Show.S01E01.zh.ass", "size": 2},
                ])

                class FakeScraper:
                    parse_media = lambda inner, name, parent_path="", match=None: release_parse_result(
                        {"season": 1, "episode": 1, "type": "tv"},
                        filename=name, parent_path=parent_path,
                    )
                    get_detail = lambda inner, tmdb_id, media_type: {"genres": [], "origin_country": ["CN"]}
                    match_from_tmdb = lambda inner, tmdb_id, media_type: MatchResult(
                        tmdb_id=tmdb_id, title="正确节目", year="2026", media_type="tv", confidence=1.0
                    )

                client = Mock()
                service = OrganizeCorrectionService(client=client, scraper=FakeScraper())
                with patch("app.modules.organize_correction.OrganizeRules.from_config", return_value=OrganizeRules(
                    target_dir_id="root", region_split=False, year_split=False, link_strm=False
                )):
                    preview = service.preview_reorganize(log_id, "99", "tv")
                self.assertFalse(preview["cloud_write"])
                self.assertEqual(
                    preview["target_path"],
                    "剧集/正确节目 (2026) {tmdb-99}/Season 1",
                )
                self.assertEqual(preview["items"][1]["to_name"], "正确节目.2026.S01E01.zh.ass")
                client.assert_not_called()

    def test_reorganize_preview_defaults_bare_episode_to_first_season(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                name = (
                    "[Nekomoe kissaten&LoliHouse] Maou Gakuin no Futekigousha "
                    "- 13 [WebRip 1080P HEVC-10bit AAC ASSx2].mkv"
                )
                log_id = db.add_organize_log(
                    "guangya", "Maou Gakuin no Futekigousha", "", "video",
                    "skipped", "97617",
                    original_parent_id="source-id", original_name=name,
                    current_parent_id="source-id", current_name=name,
                    media_type="tv", title="魔王学院的不适任者", year="2020",
                    season=None, episode=13, legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [{
                    "file_id": "video", "role": "video",
                    "original_parent_id": "source-id", "original_name": name,
                    "current_parent_id": "source-id", "current_name": name,
                    "size": 100,
                }])

                class FakeScraper:
                    def parse_media(self, filename, parent_path="", match=None):
                        return release_parse_result(
                            {"season": None, "episode": 13, "type": "tv"},
                            filename=filename, parent_path=parent_path,
                        )

                    def get_detail(self, _tmdb_id, _media_type):
                        return {"genres": [{"id": 16}], "origin_country": ["JP"]}

                    def match_from_tmdb(self, tmdb_id, media_type):
                        return MatchResult(
                            tmdb_id=tmdb_id, title="魔王学院的不适任者",
                            year="2020", media_type=media_type, confidence=1.0,
                        )

                service = OrganizeCorrectionService(client=Mock(), scraper=FakeScraper())
                with patch(
                    "app.modules.organize_correction.OrganizeRules.from_config",
                    return_value=OrganizeRules(
                        target_dir_id="root", region_split=False,
                        year_split=False, link_strm=False,
                    ),
                ):
                    preview = service.preview_reorganize(log_id, "97617", "tv")
                    corrected = service.preview_reorganize(
                        log_id, "97617", "tv", season=2, episode=4
                    )

                self.assertEqual(
                    preview["target_path"],
                    "动漫/魔王学院的不适任者 (2020) {tmdb-97617}/Season 1",
                )
                self.assertIn("S01E13", preview["file_name"])

                self.assertEqual(
                    corrected["target_path"],
                    "动漫/魔王学院的不适任者 (2020) {tmdb-97617}/Season 2",
                )
                self.assertIn("S02E04", corrected["file_name"])
                self.assertEqual((corrected["season"], corrected["episode"]), (2, 4))

    def test_scraper_tmdb_marker_supports_generated_parenthesis_format(self):
        scraper = TMDBScraper()

        def detail(tmdb_id, media_type):
            return {
                "id": int(tmdb_id),
                "name": "Show" if media_type == "tv" else "Movie",
                "title": "Movie",
                "first_air_date": "2026-01-01",
                "release_date": "2026-01-01",
            }

        with patch.object(scraper, "get_detail", side_effect=detail):
            parsed = _parse_fields(scraper, "Movie (2026)(tmdb-123).mkv")
            result = scraper.match("Movie (2026)(tmdb-123).mkv")
            inherited = scraper.match(
                "Show.S02E13.mkv",
                "1/我独自升级 tmdb127532/第二季",
                media_type_hint="tv",
            )
            inherited_plain_episode = scraper.match(
                "13.mp4",
                "1/我独自升级 tmdb127532/第二季",
            )
            file_wins = scraper.match(
                "Show.S03E16.{tmdb-456}.mkv",
                "1/旧目录 tmdb127532",
                media_type_hint="tv",
            )
        self.assertEqual(parsed["tmdb_id"], "123")
        episode = _parse_fields(scraper, "Show.S03E16.{tmdb-456}.mkv")
        self.assertEqual(
            (episode["tmdb_id"], episode["type"], episode["season"], episode["episode"]),
            ("456", "tv", 3, 16),
        )
        for marker in (
            "tmdb789",
            "tmdb 789",
            "tmdb+789",
            "tdmb+789",
            "tmdb-789",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(
                    _parse_fields(scraper, f"{marker}.mkv")["tmdb_id"],
                    "789",
                )
        for rejected in (
            "Movie-tmdb789-1080p.mkv",
            "Movie-tdmb+789-1080p.mkv",
            "tdmb789.mkv",
            "tmdb１２３.mkv",
            "tmdb12345678901.mkv",
            "word{tmdb-789}word.mkv",
            "word(tmdb-789)word.mkv",
            "word{tmdb-789}.mkv",
            "{tmdb-789}word.mkv",
        ):
            with self.subTest(rejected=rejected):
                self.assertEqual(
                    _parse_fields(scraper, rejected).get("tmdb_id", ""),
                    "",
                )
        self.assertEqual(
            _explicit_tmdb_id_from_path(
                "1/Show.2026 {tmdb-789}", nearest_first=True
            ),
            "789",
        )
        self.assertEqual(
            _explicit_tmdb_id_from_path(
                "1/作品{tmdb-789}.mkv", nearest_first=True
            ),
            "789",
        )
        self.assertEqual(inherited.tmdb_id, "127532")
        self.assertEqual(
            (inherited_plain_episode.tmdb_id, inherited_plain_episode.media_type),
            ("127532", "tv"),
        )
        self.assertEqual(file_wins.tmdb_id, "456")
        self.assertEqual(
            _explicit_tmdb_id_from_path(
                "1/Show {tmdb-111} {tmdb-222}", nearest_first=True
            ),
            "",
        )
        self.assertEqual(
            _explicit_tmdb_id_from_path(
                "1/Show tmdb+111 tdmb+222", nearest_first=True
            ),
            "",
        )
        self.assertEqual((result.title, result.year, result.media_type), ("Movie", "2026", "movie"))

    def test_tmdb_marker_suffix_preserves_episode_semantics(self):
        scraper = TMDBScraper()
        for marker in ("tmdb223911", "tmdb 223911", "tdmb+223911"):
            with self.subTest(marker=marker):
                parsed = _parse_fields(scraper,
                    f"[Shridhuu] Renegade Immortal - 153 {marker}.mkv"
                )
                self.assertEqual(
                    (
                        parsed["tmdb_id"], parsed["type"],
                        parsed["season"], parsed["episode"],
                    ),
                    ("223911", "tv", None, 153),
                )
                self.assertEqual(
                    parse_release_position(
                        f"[Shridhuu] Renegade Immortal - 153 {marker}.mkv"
                    ),
                    {"season": None, "episode": 153, "episode_end": None},
                )
                context = extract_recognition_context(
                    f"[Shridhuu] Renegade Immortal - 153 {marker}.mkv",
                    "",
                )
                self.assertEqual((context.season, context.episode), (None, 153))
                self.assertEqual(context.normalized_title, "Renegade Immortal")
                self.assertNotIn("tmdb", context.normalized_title.lower())
                self.assertNotIn("tdmb", context.normalized_title.lower())

        parent_context = extract_recognition_context(
            "E01.mkv",
            "1/Example Show tmdb123",
        )
        self.assertEqual(parent_context.episode, 1)
        self.assertEqual(parent_context.folder_title, "Example Show")
        self.assertNotIn("tmdb", parent_context.normalized_title.lower())

        season_episode = _parse_fields(scraper,
            "Show S02E06 tmdb278043.mkv"
        )
        self.assertEqual(
            (
                season_episode["tmdb_id"], season_episode["type"],
                season_episode["season"], season_episode["episode"],
            ),
            ("278043", "tv", 2, 6),
        )
        movie = _parse_fields(scraper, "Movie 2026 tmdb123.mkv")
        self.assertEqual(
            (movie["tmdb_id"], movie["type"], movie["season"], movie["episode"]),
            ("123", "movie", None, None),
        )

    def test_scraper_tmdb_marker_rejects_missing_or_mismatched_detail_identity(self):
        scraper = TMDBScraper()
        for detail, expected_fragment in (
            ({"title": "Movie", "release_date": "2026-01-01"}, "缺少 ID"),
            ({"id": 456, "title": "Movie", "release_date": "2026-01-01"}, "响应 456"),
        ):
            with self.subTest(detail=detail):
                with patch.object(scraper, "get_detail", return_value=detail):
                    result = scraper.match("Movie.2026.tmdb123.mkv")

                self.assertEqual(result.status, "request_error")
                self.assertTrue(result.need_confirm)
                self.assertEqual(result.matched_by, "tmdb_id")
                self.assertIn(expected_fragment, result.error)

    def test_scraper_tmdb_marker_conflict_fails_closed_without_parent_fallback(self):
        scraper = TMDBScraper()
        with patch.object(scraper, "get_detail") as get_detail:
            result = scraper.match(
                "Show tmdb111 tmdb222 S01E03.mkv",
                "1/Parent tmdb999",
                media_type_hint="tv",
            )

        get_detail.assert_not_called()
        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.matched_by, "tmdb_marker_conflict")
        self.assertIn("多个不同 TMDB 标记", result.error)

    def test_return_to_source_rejects_remote_snapshot_drift(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source", "movie/Movie.mkv", "video", "success", "1",
                    original_parent_id="source-id", original_name="Movie.mkv",
                    current_parent_id="target-id", current_name="电影.mkv",
                    target_parent_id="target-id", media_type="movie", title="电影", year="2026",
                    legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [{
                    "file_id": "video", "role": "video", "original_parent_id": "source-id",
                    "original_name": "Movie.mkv", "current_parent_id": "target-id",
                    "current_name": "电影.mkv", "size": 100,
                }])
                client = Mock()
                client.file_info.return_value = GuangYaFile(
                    "video", "外部改名.mkv", False, 100, "", "target-id"
                )
                service = OrganizeCorrectionService(client=client, scraper=object())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "文件名已被外部修改"):
                    service.return_to_source(log_id, "return-token", version)
                client.move.assert_not_called()
                client.rename.assert_not_called()
                self.assertEqual(db.get_organize_log(log_id)["status"], "success")

    @staticmethod
    def _make_correction_log(test_db: Path, *, with_subtitle: bool = True) -> int:
        with patch("app.database.DB_PATH", test_db):
            db.init_db()
            log_id = db.add_organize_log(
                "guangya", "source", "movie/Movie.mkv", "video", "success", "1",
                original_parent_id="source-id", original_name="Movie.mkv",
                current_parent_id="target-id", current_name="电影.mkv",
                target_parent_id="target-id", media_type="movie", title="电影", year="2026",
                legacy_incomplete=False,
            )
            items = [{
                "file_id": "video", "role": "video", "original_parent_id": "source-id",
                "original_name": "Movie.mkv", "current_parent_id": "target-id",
                "current_name": "电影.mkv", "size": 100, "etag": "video-etag",
            }]
            if with_subtitle:
                items.append({
                    "file_id": "sub", "role": "subtitle", "original_parent_id": "source-id",
                    "original_name": "Movie.zh.ass", "current_parent_id": "target-id",
                    "current_name": "电影.zh.ass", "size": 2, "etag": "sub-etag",
                })
            db.add_organize_log_items(log_id, items)
            return log_id

    def test_return_to_source_cleans_exact_empty_season_and_media_directories(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya",
                    "1/Original.Show.S01E01.mkv",
                    (
                        "剧集/1 (2006) {tmdb-294418}/Season 1/"
                        "1.2006.S01E01.mkv"
                    ),
                    "video",
                    "success",
                    "294418",
                    original_parent_id="source-id",
                    original_name="Original.Show.S01E01.mkv",
                    current_parent_id="season-id",
                    current_name="1.2006.S01E01.mkv",
                    target_parent_id="season-id",
                    media_type="tv",
                    title="1",
                    year="2006",
                    season=1,
                    episode=1,
                    legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [{
                    "file_id": "video",
                    "role": "video",
                    "original_parent_id": "source-id",
                    "original_name": "Original.Show.S01E01.mkv",
                    "current_parent_id": "season-id",
                    "current_name": "1.2006.S01E01.mkv",
                    "target_parent_id": "season-id",
                    "target_name": "1.2006.S01E01.mkv",
                    "size": 100,
                    "etag": "video-etag",
                }])

            remote = {
                "category-id": GuangYaFile(
                    "category-id", "剧集", True, etag="category-etag",
                    parent_id="target-root", updated_at=10,
                ),
                "show-id": GuangYaFile(
                    "show-id", "1 (2006) {tmdb-294418}", True,
                    etag="show-etag", parent_id="category-id", updated_at=20,
                ),
                "season-id": GuangYaFile(
                    "season-id", "Season 1", True, etag="season-etag",
                    parent_id="show-id", updated_at=30,
                ),
                "video": GuangYaFile(
                    "video", "1.2006.S01E01.mkv", False, 100,
                    "video-etag", "season-id",
                ),
            }
            deleted: list[str] = []

            class FakeClient:
                supports_atomic_empty_directory_delete = False
                supports_guarded_empty_directory_delete = True

                def file_info(self, file_id):
                    return remote.get(file_id)

                def list_dir(self, parent_id):
                    return [
                        item for item in remote.values()
                        if item.parent_id == parent_id
                    ]

                def rename(self, file_id, name):
                    remote[file_id].name = name

                def move(self, file_ids, parent_id):
                    remote[file_ids[0]].parent_id = parent_id

                def delete_empty_directory(
                    self, file_id, *, expected_etag="", expected_updated_at=0,
                ):
                    directory = remote[file_id]
                    self_outer.assertTrue(directory.is_dir)
                    self_outer.assertEqual(directory.etag, expected_etag)
                    self_outer.assertEqual(directory.updated_at, expected_updated_at)
                    self_outer.assertEqual(self.list_dir(file_id), [])
                    deleted.append(file_id)
                    del remote[file_id]
                    return True

            self_outer = self
            rules = OrganizeRules(target_dir_id="target-root", link_strm=False)
            with patch("app.database.DB_PATH", test_db), patch(
                "app.modules.organize_correction.OrganizeRules.from_config",
                return_value=rules,
            ), patch(
                "app.modules.organize_correction.config.get", return_value="",
            ):
                service = OrganizeCorrectionService(
                    client=FakeClient(), scraper=object()
                )
                version = service.detail(log_id)["version"]
                result = service.return_to_source(
                    log_id, "return-clean-dirs", version
                )

                self.assertTrue(result["success"])
                self.assertEqual(result["empty_dirs_cleaned"], 2)
                self.assertEqual(result["empty_dir_cleanup_status"], "cleaned")
                self.assertEqual(result["warnings"], [])
                self.assertEqual(deleted, ["season-id", "show-id"])
                self.assertIn("category-id", remote)
                self.assertEqual(remote["video"].parent_id, "source-id")
                self.assertEqual(
                    remote["video"].name, "Original.Show.S01E01.mkv"
                )
                audits = db.list_organize_delete_audits(log_id, limit=10)
                ordered_audits = list(reversed(audits))
                self.assertEqual(
                    [row["trigger"] for row in ordered_audits],
                    [
                        "return_to_source_empty_dir_cleanup",
                        "return_to_source_empty_dir_cleanup",
                    ],
                )
                self.assertEqual(
                    [row["gcid"] for row in ordered_audits],
                    ["season-etag", "show-etag"],
                )

    def test_return_cleanup_accepts_guarded_recycle_bin_fallback(self):
        directory = GuangYaFile(
            "season-id", "Season 1", True, etag="season-etag",
            parent_id="show-id", updated_at=30,
        )
        client = SimpleNamespace(
            supports_atomic_empty_directory_delete=False,
            supports_guarded_empty_directory_delete=True,
            list_dir=Mock(return_value=[]),
            file_info=Mock(return_value=directory),
            delete_empty_directory=Mock(return_value=True),
        )
        service = OrganizeCorrectionService(client=client, scraper=object())

        def run_delete(*_args, **kwargs):
            return kwargs["delete_operation"]()

        with patch(
            "app.modules.organize_correction.execute_recycle_bin_delete",
            side_effect=run_delete,
        ) as audited_delete:
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1, directories=[directory], protected=set(),
                target_root_id="show-id",
            )

        self.assertEqual(cleaned, 1)
        self.assertEqual(warnings, [])
        client.delete_empty_directory.assert_called_once_with(
            "season-id", expected_etag="season-etag", expected_updated_at=30,
        )
        self.assertIn(
            "双重版本与空目录复核后移入回收站",
            audited_delete.call_args.kwargs["reason"],
        )

    def test_return_cleanup_orders_all_seasons_before_media_root(self):
        remote = {
            "show-id": GuangYaFile(
                "show-id", "Show (2026) {tmdb-1}", True, etag="show-etag",
                parent_id="category-id", updated_at=20,
            ),
            "season-1": GuangYaFile(
                "season-1", "Season 1", True, etag="s1-etag",
                parent_id="show-id", updated_at=30,
            ),
            "season-2": GuangYaFile(
                "season-2", "Season 2", True, etag="s2-etag",
                parent_id="show-id", updated_at=40,
            ),
        }
        deleted: list[str] = []

        class FakeClient:
            supports_atomic_empty_directory_delete = False
            supports_guarded_empty_directory_delete = True

            def list_dir(self, parent_id):
                return [item for item in remote.values() if item.parent_id == parent_id]

            def file_info(self, file_id):
                return remote.get(file_id)

            def delete_empty_directory(
                self, file_id, *, expected_etag="", expected_updated_at=0,
            ):
                self_outer.assertEqual(self.list_dir(file_id), [])
                deleted.append(file_id)
                del remote[file_id]
                return True

        def run_delete(*_args, **kwargs):
            return kwargs["delete_operation"]()

        self_outer = self
        service = OrganizeCorrectionService(client=FakeClient(), scraper=object())
        directories = [remote["season-1"], remote["show-id"], remote["season-2"]]
        with patch(
            "app.modules.organize_correction.execute_recycle_bin_delete",
            side_effect=run_delete,
        ):
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1, directories=directories, protected=set(),
                target_root_id="category-id",
            )

        self.assertEqual(cleaned, 3)
        self.assertEqual(warnings, [])
        self.assertEqual(deleted, ["season-1", "season-2", "show-id"])

    def test_return_cleanup_keeps_nonempty_directory(self):
        directory = GuangYaFile(
            "season-id", "Season 1", True, etag="season-etag",
            parent_id="show-id", updated_at=30,
        )
        child = GuangYaFile("other", "keep.mkv", False, parent_id="season-id")
        client = SimpleNamespace(
            supports_atomic_empty_directory_delete=False,
            supports_guarded_empty_directory_delete=True,
            list_dir=Mock(return_value=[child]),
            file_info=Mock(return_value=directory),
            delete_empty_directory=Mock(),
        )
        service = OrganizeCorrectionService(client=client, scraper=object())
        with patch(
            "app.modules.organize_correction.record_blocked_delete"
        ) as blocked:
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1, directories=[directory], protected=set(),
                target_root_id="show-id",
            )

        self.assertEqual(cleaned, 0)
        self.assertIn("仍含其他内容", warnings[0])
        client.file_info.assert_not_called()
        client.delete_empty_directory.assert_not_called()
        self.assertEqual(blocked.call_count, 1)
        self.assertEqual(blocked.call_args.kwargs["reason"], "目录仍含其他内容，已保留")

    def test_return_cleanup_audit_failure_is_nonfatal_after_file_return(self):
        client = SimpleNamespace(
            supports_atomic_empty_directory_delete=False,
            supports_guarded_empty_directory_delete=False,
            delete_empty_directory=Mock(),
        )
        service = OrganizeCorrectionService(client=client, scraper=object())
        directory = GuangYaFile(
            "show-id", "Show (2026) {tmdb-1}", True, etag="show-etag",
            parent_id="category-id", updated_at=20,
        )
        with patch(
            "app.modules.organize_correction.record_blocked_delete",
            side_effect=RuntimeError("audit unavailable"),
        ):
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1, directories=[directory], protected=set(),
                target_root_id="target-root",
            )

        self.assertEqual(cleaned, 0)
        self.assertTrue(any("不支持安全的空目录回收站清理" in item for item in warnings))
        self.assertTrue(any("审计写入失败" in item for item in warnings))
        client.delete_empty_directory.assert_not_called()

    def test_return_cleanup_requires_explicit_safe_capability(self):
        directory = GuangYaFile(
            "season-id", "Season 1", True, etag="season-etag",
            parent_id="show-id", updated_at=30,
        )
        client = SimpleNamespace(delete_empty_directory=Mock())
        service = OrganizeCorrectionService(client=client, scraper=object())
        with patch(
            "app.modules.organize_correction.record_blocked_delete"
        ) as blocked:
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1, directories=[directory], protected=set(),
                target_root_id="target-root",
            )

        self.assertEqual(cleaned, 0)
        self.assertIn("不支持安全的空目录回收站清理", warnings[0])
        client.delete_empty_directory.assert_not_called()
        blocked.assert_called_once()

    def test_return_cleanup_rejects_source_subtree_when_target_is_cloud_root(self):
        remote = {
            "season-id": GuangYaFile(
                "season-id", "Season 1", True, etag="season-etag",
                parent_id="show-id", updated_at=30,
            ),
            "show-id": GuangYaFile(
                "show-id", "Show (2026) {tmdb-1}", True, etag="show-etag",
                parent_id="source-root", updated_at=20,
            ),
            "source-root": GuangYaFile(
                "source-root", "整理来源", True, etag="source-etag",
                parent_id="0", updated_at=10,
            ),
        }
        service = OrganizeCorrectionService(
            client=SimpleNamespace(file_info=lambda file_id: remote.get(file_id)),
            scraper=object(),
        )
        item = SimpleNamespace(
            original_parent_id="different-source", current_parent_id="season-id",
        )
        rules = OrganizeRules(target_dir_id="0", link_strm=False)
        with patch(
            "app.modules.organize_correction.config.get",
            return_value='[{"id":"source-root","name":"整理来源"}]',
        ):
            directories, _protected, warnings = (
                service._capture_return_cleanup_directories(
                    detail={
                        "new_path": (
                            "Show (2026) {tmdb-1}/Season 1/Show.S01E01.mkv"
                        ),
                        "media_type": "tv",
                        "tmdb_id": "1",
                        "season": 1,
                    },
                    items=[item],
                    rules=rules,
                )
            )

        self.assertEqual(directories, [])
        self.assertIn("不属于当前整理目标根", warnings[0])

    def test_return_cleanup_rechecks_target_ancestry_before_delete(self):
        snapshot = GuangYaFile(
            "season-id", "Season 1", True, etag="season-etag",
            parent_id="show-id", updated_at=30,
        )
        remote = {
            "season-id": GuangYaFile(
                "season-id", "Season 1", True, etag="season-etag",
                parent_id="show-id", updated_at=30,
            ),
            "show-id": GuangYaFile(
                "show-id", "Show (2026) {tmdb-1}", True, etag="show-etag",
                parent_id="foreign-category", updated_at=20,
            ),
            "foreign-category": GuangYaFile(
                "foreign-category", "剧集", True, etag="category-etag",
                parent_id="0", updated_at=10,
            ),
        }
        client = SimpleNamespace(
            supports_atomic_empty_directory_delete=False,
            supports_guarded_empty_directory_delete=True,
            list_dir=Mock(return_value=[]),
            file_info=Mock(side_effect=lambda file_id: remote.get(file_id)),
            delete_empty_directory=Mock(),
        )
        service = OrganizeCorrectionService(client=client, scraper=object())
        with patch(
            "app.modules.organize_correction.execute_recycle_bin_delete"
        ) as audited_delete:
            cleaned, warnings = service._cleanup_return_target_directories(
                log_id=1,
                directories=[snapshot],
                protected=set(),
                target_root_id="target-root",
            )

        self.assertEqual(cleaned, 0)
        self.assertIn("未能清理", warnings[0])
        client.delete_empty_directory.assert_not_called()
        audited_delete.assert_not_called()

    def test_return_cleanup_rejects_matching_identity_outside_target_root(self):
        remote = {
            "season-id": GuangYaFile(
                "season-id", "Season 1", True, etag="season-etag",
                parent_id="show-id", updated_at=30,
            ),
            "show-id": GuangYaFile(
                "show-id", "Show (2026) {tmdb-1}", True, etag="show-etag",
                parent_id="foreign-category", updated_at=20,
            ),
            "foreign-category": GuangYaFile(
                "foreign-category", "剧集", True, etag="category-etag",
                parent_id="foreign-root", updated_at=10,
            ),
            "foreign-root": GuangYaFile(
                "foreign-root", "Other", True, etag="root-etag",
                parent_id="0", updated_at=5,
            ),
        }
        service = OrganizeCorrectionService(
            client=SimpleNamespace(file_info=lambda file_id: remote.get(file_id)),
            scraper=object(),
        )
        item = SimpleNamespace(
            original_parent_id="source-id", current_parent_id="season-id",
        )
        rules = OrganizeRules(target_dir_id="target-root", link_strm=False)
        with patch(
            "app.modules.organize_correction.config.get", return_value="",
        ):
            directories, _protected, warnings = (
                service._capture_return_cleanup_directories(
                    detail={
                        "new_path": (
                            "剧集/Show (2026) {tmdb-1}/Season 1/"
                            "Show.S01E01.mkv"
                        ),
                        "media_type": "tv",
                        "tmdb_id": "1",
                        "season": 1,
                    },
                    items=[item],
                    rules=rules,
                )
            )

        self.assertEqual(directories, [])
        self.assertIn("不属于当前整理目标根", warnings[0])

    def test_return_cleanup_accepts_metatube_identity_under_target_root(self):
        remote = {
            "season-id": GuangYaFile(
                "season-id", "Season 1", True, etag="season-etag",
                parent_id="show-id", updated_at=30,
            ),
            "show-id": GuangYaFile(
                "show-id", "Show (2026) {metatube-series_1}", True,
                etag="show-etag", parent_id="category-id", updated_at=20,
            ),
            "category-id": GuangYaFile(
                "category-id", "剧集", True, etag="category-etag",
                parent_id="target-root", updated_at=10,
            ),
        }
        service = OrganizeCorrectionService(
            client=SimpleNamespace(file_info=lambda file_id: remote.get(file_id)),
            scraper=object(),
        )
        item = SimpleNamespace(
            original_parent_id="source-id", current_parent_id="season-id",
        )
        rules = OrganizeRules(target_dir_id="target-root", link_strm=False)
        with patch(
            "app.modules.organize_correction.config.get", return_value="",
        ):
            directories, _protected, warnings = (
                service._capture_return_cleanup_directories(
                    detail={
                        "new_path": (
                            "剧集/Show (2026) {metatube-series_1}/Season 1/"
                            "Show.S01E01.mkv"
                        ),
                        "media_type": "tv",
                        "provider": "metatube",
                        "external_id": "series_1",
                        "season": 1,
                    },
                    items=[item],
                    rules=rules,
                )
            )

        self.assertEqual(
            [directory.file_id for directory in directories],
            ["season-id", "show-id"],
        )
        self.assertEqual(warnings, [])

    def test_return_cleanup_rejects_unproven_historical_directory_shape(self):
        class FakeClient:
            def file_info(self, file_id):
                return GuangYaFile(
                    file_id, "剧集", True, etag="category-etag",
                    parent_id="target-root", updated_at=10,
                )

        service = OrganizeCorrectionService(
            client=FakeClient(), scraper=object()
        )
        item = SimpleNamespace(
            original_parent_id="source-id", current_parent_id="category-id"
        )
        rules = OrganizeRules(target_dir_id="target-root", link_strm=False)
        with patch(
            "app.modules.organize_correction.config.get", return_value="",
        ):
            directories, _protected, warnings = (
                service._capture_return_cleanup_directories(
                    detail={
                        "new_path": "剧集/file.mkv",
                        "media_type": "movie",
                        "tmdb_id": "1",
                    },
                    items=[item],
                    rules=rules,
                )
            )

        self.assertEqual(directories, [])
        self.assertIn("缺少匹配的媒体身份标识", warnings[0])

    def test_return_to_source_rolls_back_current_file_when_move_fails(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db, with_subtitle=False)
            remote = {"video": GuangYaFile(
                "video", "电影.mkv", False, 100, "video-etag", "target-id"
            )}

            class FakeClient:
                def file_info(self, file_id):
                    return remote.get(file_id)

                def list_dir(self, parent_id):
                    return [item for item in remote.values() if item.parent_id == parent_id]

                def rename(self, file_id, name):
                    remote[file_id].name = name

                def move(self, file_ids, parent_id):
                    if parent_id == "source-id":
                        raise RuntimeError("simulated move failure")
                    remote[file_ids[0]].parent_id = parent_id

            with patch("app.database.DB_PATH", test_db):
                service = OrganizeCorrectionService(client=FakeClient(), scraper=object())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "simulated move failure"):
                    service.return_to_source(log_id, "return-fail", version)
                self.assertEqual((remote["video"].parent_id, remote["video"].name),
                                 ("target-id", "电影.mkv"))
                self.assertEqual(db.get_organize_log(log_id)["status"], "failed")
                steps = db.list_organize_operation_steps(log_id)
                self.assertEqual(steps[0]["status"], "failed")

    def test_return_to_source_rolls_back_completed_members(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db)
            remote = {
                "video": GuangYaFile("video", "电影.mkv", False, 100, "video-etag", "target-id"),
                "sub": GuangYaFile("sub", "电影.zh.ass", False, 2, "sub-etag", "target-id"),
            }

            class FakeClient:
                def file_info(self, file_id):
                    return remote.get(file_id)

                def list_dir(self, parent_id):
                    return [item for item in remote.values() if item.parent_id == parent_id]

                def rename(self, file_id, name):
                    remote[file_id].name = name

                def move(self, file_ids, parent_id):
                    file_id = file_ids[0]
                    if file_id == "sub" and parent_id == "source-id":
                        raise RuntimeError("subtitle move failure")
                    remote[file_id].parent_id = parent_id

            with patch("app.database.DB_PATH", test_db):
                service = OrganizeCorrectionService(client=FakeClient(), scraper=object())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "subtitle move failure"):
                    service.return_to_source(log_id, "return-batch-fail", version)
                self.assertEqual((remote["video"].parent_id, remote["video"].name),
                                 ("target-id", "电影.mkv"))
                self.assertEqual((remote["sub"].parent_id, remote["sub"].name),
                                 ("target-id", "电影.zh.ass"))
                steps = db.list_organize_operation_steps(log_id)
                statuses = {row["file_id"]: row["status"] for row in steps}
                self.assertEqual(statuses, {"video": "rolled_back", "sub": "failed"})

    def test_reorganize_persists_contextual_learning_after_success(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db, with_subtitle=False)
            remote = {"video": GuangYaFile(
                "video", "电影.mkv", False, 100, "video-etag", "target-id"
            )}
            confirm_calls = []

            class FakeScraper:
                parse_media = lambda inner, name, parent_path="", match=None: release_parse_result(
                    {"season": None, "episode": None, "type": "movie"},
                    filename=name, parent_path=parent_path,
                )
                get_detail = lambda inner, tmdb_id, media_type: {
                    "genres": [], "origin_country": ["CN"]
                }
                match_from_tmdb = lambda inner, tmdb_id, media_type: MatchResult(
                    tmdb_id=tmdb_id, title="新电影", year="2027",
                    media_type="movie", confidence=1.0,
                )

                def confirm(self, *args, **kwargs):
                    confirm_calls.append((args, kwargs))

            class FakeClient:
                def file_info(self, file_id):
                    return remote.get(file_id)

                def list_dir(self, parent_id):
                    if parent_id == "root":
                        return [GuangYaFile("movie-root", "电影", True, parent_id="root")]
                    if parent_id == "movie-root":
                        return [GuangYaFile(
                            "movie-target", "新电影 (2027) {tmdb-99}", True,
                            parent_id="movie-root",
                        )]
                    return [item for item in remote.values() if item.parent_id == parent_id]

                def create_dir(self, name, parent_id):
                    raise AssertionError("测试目录已存在")

                def move(self, file_ids, parent_id):
                    remote[file_ids[0]].parent_id = parent_id

                def rename(self, file_id, name):
                    remote[file_id].name = name

            with patch("app.database.DB_PATH", test_db), patch(
                "app.modules.organize_correction.OrganizeRules.from_config",
                return_value=OrganizeRules(
                    target_dir_id="root", region_split=False, year_split=False,
                    link_strm=False,
                ),
            ), patch.object(
                OrganizeCorrectionService, "_notify_reorganize_result", return_value=[]
            ):
                service = OrganizeCorrectionService(
                    client=FakeClient(), scraper=FakeScraper()
                )
                version = service.detail(log_id)["version"]
                result = service.reorganize(
                    log_id, "reorganize-success", version, "99", "movie"
                )
                updated_log = dict(db.get_organize_log(log_id))

            self.assertTrue(result["success"])
            self.assertEqual(
                (updated_log["provider"], updated_log["external_id"]),
                ("tmdb", "99"),
            )
            self.assertEqual(len(confirm_calls), 1)
            args, kwargs = confirm_calls[0]
            self.assertEqual(args[:5], (
                "Movie.mkv", "99", "新电影", "2027", "movie"
            ))
            self.assertEqual(kwargs["parent_path"], "source")
            self.assertEqual(kwargs["rejected_tmdb_ids"], ["1"])

    def test_reorganize_rolls_back_current_file_when_rename_fails(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db, with_subtitle=False)
            remote = {"video": GuangYaFile(
                "video", "电影.mkv", False, 100, "video-etag", "target-id"
            )}

            class FakeScraper:
                parse_media = lambda inner, name, parent_path="", match=None: release_parse_result(
                    {"season": None, "episode": None, "type": "movie"},
                    filename=name, parent_path=parent_path,
                )
                get_detail = lambda inner, tmdb_id, media_type: {"genres": [], "origin_country": ["CN"]}
                match_from_tmdb = lambda inner, tmdb_id, media_type: MatchResult(
                    tmdb_id=tmdb_id, title="新电影", year="2027", media_type="movie",
                    confidence=1.0,
                )
                confirm = lambda *args, **kwargs: None

            class FakeClient:
                def file_info(self, file_id):
                    return remote.get(file_id)

                def list_dir(self, parent_id):
                    if parent_id == "root":
                        return [GuangYaFile("new-target", "电影", True, parent_id="root")]
                    if parent_id == "new-target":
                        return [GuangYaFile(
                            "movie-target", "新电影 (2027) {tmdb-99}", True,
                            parent_id="new-target",
                        )]
                    return [item for item in remote.values() if item.parent_id == parent_id]

                def create_dir(self, name, parent_id):
                    raise AssertionError("测试目录已存在")

                def move(self, file_ids, parent_id):
                    remote[file_ids[0]].parent_id = parent_id

                def rename(self, file_id, name):
                    if name.startswith("新电影"):
                        raise RuntimeError("simulated rename failure")
                    remote[file_id].name = name

            with patch("app.database.DB_PATH", test_db), patch(
                "app.modules.organize_correction.OrganizeRules.from_config",
                return_value=OrganizeRules(
                    target_dir_id="root", region_split=False, year_split=False,
                    link_strm=False,
                ),
            ):
                service = OrganizeCorrectionService(client=FakeClient(), scraper=FakeScraper())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "simulated rename failure"):
                    service.reorganize(log_id, "reorganize-fail", version, "99", "movie")
                self.assertEqual((remote["video"].parent_id, remote["video"].name),
                                 ("target-id", "电影.mkv"))
                self.assertEqual(db.get_organize_log(log_id)["status"], "failed")
                self.assertEqual(db.list_organize_operation_steps(log_id)[0]["status"], "failed")

    def test_delete_snapshot_drift_does_not_claim_log(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db, with_subtitle=False)
            client = Mock()
            client.file_info.return_value = GuangYaFile(
                "video", "外部改名.mkv", False, 100, "video-etag", "target-id"
            )
            with patch("app.database.DB_PATH", test_db):
                service = OrganizeCorrectionService(client=client, scraper=object())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "文件名已被外部修改"):
                    service.delete_group(log_id, "delete-token", version, "DELETE")
                self.assertEqual(db.get_organize_log(log_id)["status"], "success")
                client.delete.assert_not_called()

    def test_revert_latest_rolls_back_completed_members(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db)
            with patch("app.database.DB_PATH", test_db):
                db.add_organize_operation_step(
                    log_id, "previous", 1, "move_rename", file_id="video",
                    from_parent_id="source-id", from_name="Movie.mkv",
                    to_parent_id="target-id", to_name="电影.mkv", status="success",
                )
                db.add_organize_operation_step(
                    log_id, "previous", 2, "move_rename", file_id="sub",
                    from_parent_id="source-id", from_name="Movie.zh.ass",
                    to_parent_id="target-id", to_name="电影.zh.ass", status="success",
                )
                remote = {
                    "video": GuangYaFile("video", "电影.mkv", False, 100, "video-etag", "target-id"),
                    "sub": GuangYaFile("sub", "电影.zh.ass", False, 2, "sub-etag", "target-id"),
                }

                class FakeClient:
                    def file_info(self, file_id):
                        return remote.get(file_id)

                    def list_dir(self, parent_id):
                        return [item for item in remote.values() if item.parent_id == parent_id]

                    def rename(self, file_id, name):
                        remote[file_id].name = name

                    def move(self, file_ids, parent_id):
                        file_id = file_ids[0]
                        if file_id == "sub" and parent_id == "source-id":
                            raise RuntimeError("revert subtitle failure")
                        remote[file_id].parent_id = parent_id

                service = OrganizeCorrectionService(client=FakeClient(), scraper=object())
                version = service.detail(log_id)["version"]
                with self.assertRaisesRegex(RuntimeError, "revert subtitle failure"):
                    service.revert_latest(log_id, "revert-fail", version)
                self.assertEqual((remote["video"].parent_id, remote["video"].name),
                                 ("target-id", "电影.mkv"))
                self.assertEqual((remote["sub"].parent_id, remote["sub"].name),
                                 ("target-id", "电影.zh.ass"))
                self.assertEqual(db.get_organize_log(log_id)["status"], "revert_failed")

    def test_init_db_recovers_interrupted_operation_state(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            log_id = self._make_correction_log(test_db, with_subtitle=False)
            with patch("app.database.DB_PATH", test_db):
                version = db.get_organize_log(log_id)["version"]
                self.assertTrue(db.claim_organize_log_operation(
                    log_id, "busy-token", "returning", ("success",), version
                ))
                step_id = db.add_organize_operation_step(
                    log_id, "busy-token", 1, "return_to_source",
                    file_id="video", status="running",
                )
                db.init_db()
                self.assertEqual(db.get_organize_log(log_id)["status"], "interrupted")
                step = next(row for row in db.list_organize_operation_steps(log_id)
                            if row["id"] == step_id)
                self.assertEqual(step["status"], "interrupted")

    def test_incomplete_member_snapshot_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                log_id = db.add_organize_log(
                    "guangya", "source", "movie/Movie.mkv", "video", "success", "1",
                    original_parent_id="source-id", original_name="Movie.mkv",
                    current_parent_id="target-id", current_name="电影.mkv",
                    legacy_incomplete=False,
                )
                db.add_organize_log_items(log_id, [{
                    "file_id": "video", "role": "video",
                    "original_parent_id": "source-id", "original_name": "Movie.mkv",
                    "current_parent_id": "", "current_name": "电影.mkv",
                }])
                detail = OrganizeCorrectionService(client=object(), scraper=object()).detail(log_id)
                self.assertFalse(detail["allowed_actions"]["reorganize"])
                self.assertIn("不完整成员快照", detail["safety_notice"])


class MediaRefreshTests(unittest.TestCase):
    def test_emby_refresh_library_uses_post(self):
        client = EmbyClient("http://emby.local", "token")
        response = type("Response", (), {"raise_for_status": lambda inner: None})()
        client._session.post = Mock(return_value=response)
        self.assertTrue(client.refresh_library("library-id"))
        self.assertIn("/Items/library-id/Refresh", client._session.post.call_args.args[0])

    def test_jellyfin_refresh_library_uses_post(self):
        client = JellyfinClient("http://jellyfin.local", "token")
        response = type("Response", (), {"raise_for_status": lambda inner: None})()
        client._session.post = Mock(return_value=response)
        self.assertTrue(client.refresh_library("library-id"))
        self.assertIn("/Items/library-id/Refresh", client._session.post.call_args.args[0])
        call = client._session.post.call_args
        self.assertEqual(call.kwargs["params"], {"metadataRefreshMode": "FullRefresh"})
        self.assertIn("MediaBrowser Token", call.kwargs["headers"]["Authorization"])

    def test_emby_refresh_for_path_matches_virtual_folder(self):
        client = EmbyClient("http://emby.local", "token")
        client.product_kind = "emby"
        def request(path, params=None):
            if path == "/Library/VirtualFolders/Query":
                return {
                    "Items": [{
                        "ItemId": "lib-1", "Name": "STRM",
                        "Locations": ["D:/Media/STRM"],
                    }],
                    "TotalRecordCount": 1,
                }
            if path == "/Items":
                return {"Items": [], "TotalRecordCount": 0}
            raise AssertionError(path)

        client._request = request
        client.refresh_library = Mock(return_value=True)
        client.refresh_all = Mock(return_value=True)
        self.assertTrue(client.refresh_for_path("D:/Media/STRM"))
        client.refresh_library.assert_called_once_with("lib-1")
        client.refresh_all.assert_not_called()


class STRMIndexTests(IsolatedDatabaseTestCase):
    def test_index_cleanup_without_download_url_requests(self):
        source_id = f"test-{uuid.uuid4().hex}"
        source_key = f"guangya:{source_id}"
        tree = {
            source_id: [
                GuangYaFile("keep", "Keep.mkv", False, 10, "e1", source_id),
                GuangYaFile("gone", "Gone.mkv", False, 10, "e2", source_id),
            ]
        }
        client = type("Client", (), {"list_dir": lambda inner, file_id: tree[file_id]})()
        try:
            with tempfile.TemporaryDirectory() as root:
                first = sync_strm(source_id, "http://example", root, client=client)
                self.assertEqual(first["generated"], 2)
                tree[source_id] = tree[source_id][:1]
                second = sync_strm(source_id, "http://example", root, client=client)
                self.assertEqual(second["cleaned"], 1)
                self.assertFalse((Path(root) / STRM_SUBDIR / "Gone.strm").exists())
        finally:
            db.delete_strm_index_ids(source_key, ["keep", "gone"])


class JellyfinDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = JellyfinClient("http://jellyfin.local", "token")
        self.client._user_id = lambda: "user-id"

    def test_libraries_use_user_views_and_work_level_counts(self):
        responses = {
            "/Users/user-id/Views": {
                "Items": [
                    {
                        "Id": "a" * 32,
                        "Name": "电视剧",
                        "CollectionType": "tvshows",
                        "ImageTags": {"Primary": "library-tag"},
                    },
                    {"Id": "b" * 32, "Name": "Playlists", "CollectionType": "playlists"},
                ]
            },
        }

        def request(path, params=None):
            if path == "/Users/user-id/Items":
                self.assertEqual(params["IncludeItemTypes"], "Series")
                return {"TotalRecordCount": 236}
            return responses[path]

        self.client._request = request
        libraries = self.client._libraries()
        self.assertEqual([(item.name, item.count) for item in libraries], [("电视剧", 236)])
        self.assertEqual(
            libraries[0].primary_image,
            f"/media-image/jellyfin/{'a' * 32}?tag=library-tag",
        )
        self.assertEqual(
            libraries[0].web_url,
            f"http://jellyfin.local/web/index.html#/tv?topParentId={'a' * 32}&collectionType=tvshows",
        )

    def test_library_web_urls_follow_jellyfin_12_collection_routes(self):
        item_id = "c" * 32
        cases = {
            "tvshows": f"#/tv?topParentId={item_id}&collectionType=tvshows",
            "movies": f"#/movies?topParentId={item_id}&collectionType=movies",
            "music": f"#/music?topParentId={item_id}&collectionType=music",
            "books": f"#/books?topParentId={item_id}&collectionType=books",
            "musicvideos": f"#/musicvideos?topParentId={item_id}&collectionType=musicvideos",
            "homevideos": f"#/homevideos?topParentId={item_id}",
            "mixed": f"#/mixed?topParentId={item_id}&collectionType=mixed",
        }
        for collection_type, suffix in cases.items():
            with self.subTest(collection_type=collection_type):
                self.assertEqual(
                    self.client._library_web_url(item_id, collection_type),
                    f"http://jellyfin.local/web/index.html{suffix}",
                )
        self.assertEqual(
            self.client._library_web_url(item_id, "unknown"),
            f"http://jellyfin.local/web/index.html#!/details?id={item_id}",
        )

    def test_episode_uses_series_poster_and_display_metadata(self):
        item = self.client._media_item(
            {
                "Id": "1" * 32,
                "Name": "序章",
                "Type": "Episode",
                "SeriesId": "2" * 32,
                "SeriesPrimaryImageTag": "series-tag",
                "SeriesName": "测试剧集",
                "ParentIndexNumber": 1,
                "IndexNumber": 3,
                "UserData": {"PlayedPercentage": 42.5, "LastPlayedDate": "2026-07-24"},
            },
            played=True,
        )
        self.assertEqual(item.display_name, "测试剧集")
        self.assertEqual(item.episode_label, "第 3 集")
        self.assertEqual(
            item.primary_image,
            f"/media-image/jellyfin/{'2' * 32}?tag=series-tag",
        )
        self.assertEqual(item.progress, 42.5)
        self.assertIn("#!/details?id=" + "1" * 32, item.web_url)


class DashboardCacheTests(unittest.TestCase):
    def setUp(self):
        from app.services import clear_dashboard_cache

        clear_dashboard_cache()

    def tearDown(self):
        from app.services import clear_dashboard_cache

        clear_dashboard_cache()

    def test_dashboard_cache_reuses_recent_result(self):
        from app import services

        board = DashboardData(server_name="Test", online=True)
        with patch("app.services._dashboard_config_key", return_value=("test",)), patch(
            "app.services._fetch_dashboards", return_value=[board]
        ) as fetch:
            self.assertIs(services.build_dashboards()[0], board)
            self.assertIs(services.build_dashboards()[0], board)
        fetch.assert_called_once_with()

    def test_dashboard_page_cache_reader_reuses_hot_result(self):
        from app import services

        board = DashboardData(server_name="Cached", server_type="jellyfin", online=True)
        with patch("app.services._dashboard_config_key", return_value=("test",)), patch(
            "app.services._fetch_dashboards", return_value=[board]
        ):
            services.build_dashboards()
            boards, is_cached = services.get_cached_dashboards_or_stubs()

        self.assertTrue(is_cached)
        self.assertEqual(boards, [board])

    def test_dashboard_page_cache_reader_returns_offline_configuration_stubs(self):
        from app import services

        enabled = {"JELLYFIN_ENABLED": True, "EMBY_ENABLED": True}
        values = {
            "JELLYFIN_URL": "http://jellyfin.local:8096",
            "EMBY_URL": "http://emby.local:8096",
        }
        with patch("app.services._dashboard_config_key", return_value=("test",)), patch(
            "app.services.get_bool", side_effect=lambda key, default=False: enabled.get(key, default)
        ), patch(
            "app.services.get", side_effect=lambda key, default="": values.get(key, default)
        ):
            boards, is_cached = services.get_cached_dashboards_or_stubs()

        self.assertFalse(is_cached)
        self.assertEqual([board.server_type for board in boards], ["jellyfin", "emby"])
        self.assertTrue(all(not board.online for board in boards))
        self.assertEqual(boards[0].web_url, "http://jellyfin.local:8096")
        self.assertEqual(boards[1].web_url, "http://emby.local:8096")

    def test_force_refresh_replaces_cached_dashboard_result(self):
        from app import services

        old = DashboardData(server_name="Old", online=True)
        fresh = DashboardData(server_name="Fresh", online=True)
        with patch("app.services._dashboard_config_key", return_value=("test",)), patch(
            "app.services._fetch_dashboards", side_effect=[[old], [fresh]]
        ) as fetch:
            self.assertIs(services.build_dashboards()[0], old)
            self.assertIs(services.build_dashboards(force=True)[0], fresh)
            self.assertIs(services.build_dashboards()[0], fresh)

        self.assertEqual(fetch.call_count, 2)

    def test_dashboard_cache_prevents_concurrent_refresh_stampede(self):
        from app import services

        calls = 0
        calls_lock = threading.Lock()

        def fetch():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return [DashboardData(server_name="Test", online=True)]

        results = []
        with patch("app.services._dashboard_config_key", return_value=("test",)), patch(
            "app.services._fetch_dashboards", side_effect=fetch
        ):
            threads = [threading.Thread(target=lambda: results.append(services.build_dashboards())) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 6)
    def test_dashboard_jobs_attach_stable_server_metadata(self):
        from app import services

        emby_board = DashboardData(server_name="Home Emby", online=True)
        jellyfin_board = DashboardData(server_name="Home Jellyfin", online=True)
        values = {
            "EMBY_ENABLED": True,
            "JELLYFIN_ENABLED": True,
        }
        strings = {
            "EMBY_URL": "http://emby.local",
            "EMBY_TOKEN": "emby-token",
            "JELLYFIN_URL": "http://jellyfin.local",
            "JELLYFIN_API_KEY": "jellyfin-key",
        }
        with patch("app.services.get_bool", side_effect=lambda key: values.get(key, False)), patch(
            "app.services.get", side_effect=lambda key: strings.get(key, "")
        ), patch("app.services.EmbyClient") as emby, patch("app.services.JellyfinClient") as jellyfin:
            emby.return_value.get_dashboard.return_value = emby_board
            jellyfin.return_value.get_dashboard.return_value = jellyfin_board
            boards = [job() for job in services._dashboard_jobs()]
        self.assertEqual([(b.server_type, b.web_url) for b in boards], [
            ("emby", "http://emby.local"),
            ("jellyfin", "http://jellyfin.local"),
        ])


class TelegramBotTests(unittest.TestCase):
    class FakeBot:
        def __init__(self):
            self.message_handlers = []
            self.callback_handlers = []
            self.replies = []
            self.commands = []

        def message_handler(self, **filters):
            def decorate(handler):
                self.message_handlers.append((filters, handler))
                return handler
            return decorate

        def callback_query_handler(self, **filters):
            def decorate(handler):
                self.callback_handlers.append((filters, handler))
                return handler
            return decorate

        def reply_to(self, message, text, **kwargs):
            self.replies.append((message, text, kwargs))

        def set_my_commands(self, commands):
            self.commands = commands

    @staticmethod
    def _telebot_types():
        class Markup:
            def __init__(self, row_width=2):
                self.row_width = row_width
                self.buttons = []

            def add(self, *buttons):
                self.buttons.extend(buttons)

        return SimpleNamespace(types=SimpleNamespace(
            InlineKeyboardMarkup=Markup,
            InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
                text=text, callback_data=callback_data,
            ),
            BotCommand=lambda command, description: SimpleNamespace(
                command=command, description=description,
            ),
        ))

    def test_handlers_register_commands_without_emoji_and_reject_unauthorized_chat(self):
        from app.bot import handlers

        bot = self.FakeBot()
        values = {"TG_CHAT_ID": "100"}
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.bot.handlers.get_bool", return_value=True
        ):
            handlers._register_commands(bot, self._telebot_types())
            start = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["start"]
            )
            agent = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["agent"]
            )
            agent_reset = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["agent_reset"]
            )
            start(SimpleNamespace(chat=SimpleNamespace(id=200)))
            start(SimpleNamespace(chat=SimpleNamespace(id=100)))
            command_message = SimpleNamespace(
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=200),
            )
            with patch("app.bot.agent_adapter.handle_agent_guide") as guide, patch(
                "app.bot.agent_adapter.handle_agent_reset"
            ) as reset:
                agent(command_message)
                agent_reset(command_message)
            guide.assert_called_once_with(bot, command_message, ANY)
            reset.assert_called_once_with(bot, command_message)
        self.assertEqual(bot.replies[0][1], "未授权会话")
        self.assertIn("<b>MediaFlux Bot</b>", bot.replies[1][1])
        self.assertNotRegex(bot.replies[1][1], r"[🎬🎞️⬇️⏳🔄❌📭📡✅⏸]")
        self.assertEqual({item.command for item in bot.commands}, {
            "start", "help", "status", "sync_gy", "organize", "media_search", "rss", "rss_refresh", "rss_dl",
            "agent", "agent_reset",
        })

    def test_disabled_agent_keeps_control_entry_and_classic_commands(self):
        from app.bot import handlers

        bot = self.FakeBot()
        values = {"TG_CHAT_ID": "100"}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.handlers.get_bool", return_value=False):
            handlers._register_commands(bot, self._telebot_types())
            start = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["start"]
            )
            rss_refresh = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_refresh"]
            )
            start(SimpleNamespace(chat=SimpleNamespace(id=100)))
            with patch("app.bot.handlers.db.list_rss_subscriptions", return_value=[]):
                rss_refresh(SimpleNamespace(
                    chat=SimpleNamespace(id=100),
                    from_user=SimpleNamespace(id=9),
                    text="/rss_refresh",
                ))

        commands = {item.command for item in bot.commands}
        self.assertIn("agent", commands)
        self.assertNotIn("agent_reset", commands)
        self.assertTrue({
            "start", "help", "status", "sync_gy", "organize", "media_search",
            "rss", "rss_refresh", "rss_dl",
        }.issubset(commands))
        help_text = bot.replies[-2][1]
        self.assertIn("<b>Media Agent</b>", help_text)
        self.assertIn("/agent — 查看状态并开启或关闭 Agent", help_text)
        self.assertNotIn("/agent_reset", help_text)
        self.assertIn("/rss_refresh ID — 刷新订阅", help_text)
        self.assertIn("/status — 查看整理、同步与待处理状态", help_text)
        self.assertIn("暂无 RSS 订阅", bot.replies[-1][1])


    def test_status_command_reports_runtime_state_and_escapes_current_source(self):
        from app.bot import handlers

        bot = self.FakeBot()
        values = {"TG_CHAT_ID": "100"}
        manager = SimpleNamespace(task_status=lambda: {
            "status": "running", "current_source": "下载/<测试>",
        })
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager,
        ), patch(
            "app.bot.handlers.db.count_download_requests_requiring_attention", return_value=2,
        ), patch.object(handlers, "_sync_running", True):
            handlers._register_commands(bot, self._telebot_types())
            status = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["status"]
            )
            status(SimpleNamespace(chat=SimpleNamespace(id=100)))

        rendered = bot.replies[-1][1]
        self.assertIn("MediaFlux 运行状态", rendered)
        self.assertIn("整理：整理中", rendered)
        self.assertIn("STRM 同步：运行中", rendered)
        self.assertIn("下载待处理：2 项", rendered)
        self.assertIn("下载/&lt;测试&gt;", rendered)
        self.assertNotIn("下载/<测试>", rendered)

    def test_download_dispatch_exception_returns_safe_feedback(self):
        from app.bot import handlers

        bot = Mock()
        secret = "https://secret.invalid/task?token=SECRET"
        with patch(
            "app.modules.download_dispatcher.dispatch_request",
            side_effect=RuntimeError(secret),
        ):
            handlers._dispatch_download_callback(bot, 100, 7, 9, "qb")

        text = bot.edit_message_text.call_args.args[0]
        self.assertIn("下载提交异常", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("SECRET", text)
        self.assertIsNone(bot.edit_message_text.call_args.kwargs["reply_markup"])

    def test_invalid_download_inputs_return_feedback_without_creating_request(self):
        from app.bot import handlers

        bot = self.FakeBot()
        bot.send_chat_action = lambda *_args, **_kwargs: None
        bot.get_file = lambda _file_id: SimpleNamespace(file_path="bad.torrent")
        bot.download_file = lambda _path: b""
        values = {"TG_CHAT_ID": "100"}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.modules.download_dispatcher.create_request") as create_request:
            handlers._register_commands(bot, self._telebot_types())
            link_handler = [
                handler for filters, handler in bot.message_handlers
                if filters.get("content_types") == ["text"] and filters.get("func") is not None
            ][-1]
            document_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("content_types") == ["document"]
            )
            link_handler(SimpleNamespace(
                text="magnet:?dn=missing-hash",
                chat=SimpleNamespace(id=100),
                message_id=3,
            ))
            document_handler(SimpleNamespace(
                chat=SimpleNamespace(id=100),
                message_id=4,
                document=SimpleNamespace(
                    file_name="bad.torrent", mime_type="application/x-bittorrent", file_id="f1",
                ),
            ))

        create_request.assert_not_called()
        replies = [item[1] for item in bot.replies]
        self.assertTrue(any(text.startswith("下载链接无效：") and "BTIH" in text for text in replies))
        self.assertTrue(any(text == "种子文件无效：种子文件为空" for text in replies))

    def test_handlers_register_agent_patrol_callback_and_delegate_after_chat_auth(self):
        from app.bot import handlers

        bot = self.FakeBot()
        telebot = self._telebot_types()
        values = {"TG_CHAT_ID": "100"}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            handlers._register_commands(bot, telebot)
            patrol_filters, patrol_handler = next(
                (filters, handler)
                for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="agp:summary"))
            )
            self.assertTrue(patrol_filters["func"](SimpleNamespace(data="agp:resources")))
            self.assertFalse(patrol_filters["func"](SimpleNamespace(data="aga:token")))
            call = SimpleNamespace(
                id="callback",
                data="agp:summary",
                from_user=SimpleNamespace(id=200),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            )
            with patch(
                "app.bot.agent_adapter.handle_agent_patrol_callback"
            ) as callback:
                patrol_handler(call)
        callback.assert_called_once_with(bot, call, telebot)

    def test_organize_command_uses_web_multi_source_config(self):
        from app.bot import handlers

        raw = '[{"id":"11","name":"源一"},{"id":"22","name":"源二"},{"id":"11","name":"重复"}]'
        values = {
            "GY_ORGANIZE_SOURCE_DIRS": raw,
        }
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)):
            self.assertEqual(handlers._configured_organize_sources(), [
                {"id": "11", "name": "源一"},
                {"id": "22", "name": "源二"},
            ])

    def test_download_follow_up_matches_enabled_capabilities(self):
        from app.bot import handlers

        values = {"GY_ORGANIZE_TARGET_DIR": "target"}
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.bot.handlers.db.list_local_media_sources", return_value=[]
        ):
            text = handlers._download_follow_up_text(["guangya", "qb"])
        self.assertIn("启动整理", text)
        self.assertIn("没有启用的本地媒体来源", text)
        self.assertNotIn("完成后自动衔接整理与入库", text)

    def test_config_save_restarts_polling_only_for_background_app(self):
        from app.routes import api

        foreground_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))
        )
        background_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=True))
        )
        payload = {"TG_BOT_TOKEN": "new-token", "TG_CHAT_ID": "100"}
        with patch("app.routes.api.require_api_login"), patch(
            "app.routes.api.config.set_and_save"
        ), patch("app.notifier.reset") as reset, patch("app.bot.restart_bot") as restart, patch(
            "app.services.clear_dashboard_cache"
        ), patch("app.modules.scheduler.get_scheduler"):
            self.assertEqual(api.save_config(foreground_request, payload), {"success": True})
            reset.assert_called_once()
            restart.assert_not_called()
            reset.reset_mock()
            self.assertEqual(api.save_config(background_request, payload), {"success": True})
            restart.assert_called_once()
            reset.assert_not_called()

            restart.reset_mock(return_value=True)
            restart.return_value = False
            result = api.save_config(background_request, payload)
            self.assertTrue(result["success"])
            self.assertIn("Telegram Bot 配置已保存", result["warnings"][0])

            restart.reset_mock(side_effect=True)
            restart.side_effect = RuntimeError("restart failed")
            result = api.save_config(background_request, payload)
            self.assertTrue(result["success"])
            self.assertIn("运行中实例热更新失败", result["warnings"][0])

    def test_notifier_reuses_single_bot_and_escapes_event_content(self):
        from app import notifier

        bot = Mock()
        notifier.reset()
        values = {"TG_BOT_TOKEN": "token", "TG_CHAT_ID": "100"}
        with patch.dict(
            os.environ, {"MEDIAFLUX_TEST_ALLOW_TELEGRAM": "1"}, clear=False
        ), patch(
            "app.notifier.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch(
            "telebot.TeleBot", return_value=bot,
        ) as constructor:
            self.assertIs(notifier.get_bot(), bot)
            self.assertIs(notifier.get_bot(), bot)
            message = notifier.format_event("任务", "<unsafe>")
            self.assertTrue(notifier.send(message))
        constructor.assert_called_once_with("token", parse_mode="HTML")
        self.assertIn("&lt;unsafe&gt;", message)
        bot.send_message.assert_called_once_with("100", message)
        notifier.reset()


class DownloadRequestConcurrencyTests(unittest.TestCase):
    @staticmethod
    def _hybrid_torrent_and_btmh():
        raw_info = (
            b"d12:meta versioni2e4:name6:hybrid6:pieces20:"
            + (b"a" * 20)
            + b"e"
        )
        torrent = torrent_download_input(
            "hybrid.torrent",
            b"d4:info" + raw_info + b"e",
        )
        v2_hash = hashlib.sha256(raw_info).hexdigest()
        btmh = normalize_download_url(f"magnet:?xt=urn:btmh:1220{v2_hash}")
        return torrent, btmh

    def test_atomic_create_deduplicates_concurrent_requests(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                results = []
                errors = []
                lock = threading.Lock()

                def create_one(index: int) -> None:
                    try:
                        result = db.create_download_request(
                            "same-request-key", "magnet", title="Demo",
                            source_value="magnet:?xt=urn:btih:concurrent",
                            chat_id="chat", message_id=str(index),
                        )
                        with lock:
                            results.append(result)
                    except Exception as exc:
                        with lock:
                            errors.append(exc)

                threads = [threading.Thread(target=create_one, args=(index,)) for index in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                self.assertEqual(len(results), 8)
                self.assertEqual(sum(1 for _request_id, created in results if created), 1)
                self.assertEqual(len({request_id for request_id, _created in results}), 1)
                with db.get_conn() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]
                self.assertEqual(count, 1)

    def test_hybrid_torrent_alias_persists_for_later_btmh_only_magnet(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                torrent, btmh = self._hybrid_torrent_and_btmh()

                first = create_request(torrent, "chat", "1")
                duplicate = create_request(btmh, "chat", "2")

                self.assertTrue(first["created"])
                self.assertFalse(duplicate["created"])
                self.assertEqual(duplicate["id"], first["id"])
                with db.get_conn() as conn:
                    owner_ids = {
                        int(row["request_id"])
                        for row in conn.execute(
                            "SELECT request_id FROM download_request_keys "
                            "WHERE request_key IN (?,?)",
                            (request_keys(torrent)[0], request_keys(btmh)[0]),
                        ).fetchall()
                    }
                self.assertEqual(owner_ids, {first["id"]})

    def test_claim_missing_target_reopens_attention_and_preserves_terminal_history(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                request_id, _ = db.create_download_request(
                    "supplement-key", "magnet", title="Demo",
                    source_value="magnet:?xt=urn:btih:supplement",
                )
                self.assertTrue(db.claim_download_request(request_id, "both"))
                db.update_download_request(
                    request_id, status="submitted", qb_status="submitted", gy_status="failed",
                    attention_cleared_at=db.now(), attention_clear_note="已确认",
                )
                self.assertEqual(db.claim_download_request_targets(request_id, "guangya"), ("guangya",))
                row = db.get_download_request(request_id)
                self.assertEqual(row["qb_status"], "submitted")
                self.assertEqual(row["gy_status"], "submitting")
                self.assertFalse(row["attention_cleared_at"])

                db.update_download_request(
                    request_id, status="completed", qb_status="completed", gy_status="failed",
                    completed_at=db.now(),
                )
                self.assertEqual(db.claim_download_request_targets(request_id, "guangya"), ())
                terminal = db.get_download_request(request_id)
                self.assertEqual(terminal["status"], "completed")
                self.assertEqual(terminal["qb_status"], "completed")

    def test_terminal_download_can_create_a_new_attempt_without_relaxing_running_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                first_id, first_created = db.create_download_request(
                    "retry-key", "magnet", title="Demo",
                    source_value="magnet:?xt=urn:btih:retry",
                )
                self.assertTrue(first_created)
                duplicate_id, duplicate_created = db.create_download_request(
                    "retry-key", "magnet", title="Demo",
                    source_value="magnet:?xt=urn:btih:retry",
                )
                self.assertEqual(duplicate_id, first_id)
                self.assertFalse(duplicate_created)

                db.update_download_request(
                    first_id, status="completed", gy_status="completed",
                    completed_at=db.now(),
                )
                retry_id, retry_created = db.create_download_request(
                    "retry-key", "magnet", title="Demo",
                    source_value="magnet:?xt=urn:btih:retry",
                )
                self.assertTrue(retry_created)
                self.assertNotEqual(retry_id, first_id)
                current = db.get_download_request(retry_id)
                self.assertEqual(current["request_key"], "retry-key")
                self.assertEqual(current["status"], "pending")
                old = db.get_download_request(first_id)
                self.assertIn(":history:", old["request_key"])

                db.update_download_request(
                    retry_id, status="cancelled", completed_at=db.now()
                )
                after_cancel_id, after_cancel_created = db.create_download_request(
                    "retry-key", "magnet", title="Demo",
                    source_value="magnet:?xt=urn:btih:retry",
                )
                self.assertTrue(after_cancel_created)
                self.assertNotEqual(after_cancel_id, retry_id)
                self.assertIn(
                    ":history:", db.get_download_request(retry_id)["request_key"]
                )

    def test_canonical_torrent_identity_deduplicates_equivalent_magnet_and_can_retry(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                torrent = torrent_download_input(
                    "demo.torrent", b"d4:infod4:name4:Testee"
                )
                initial = create_request(torrent, "chat", "1")
                self.assertTrue(initial["created"])

                equivalent = normalize_download_url(torrent.source_value)
                duplicate = create_request(equivalent, "chat", "2")
                self.assertFalse(duplicate["created"])
                self.assertEqual(duplicate["id"], initial["id"])

                db.update_download_request(
                    initial["id"], status="completed", qb_status="completed",
                    completed_at=db.now(),
                )
                retry = create_request(equivalent, "chat", "3")
                self.assertTrue(retry["created"])
                self.assertNotEqual(retry["id"], initial["id"])
                self.assertEqual(
                    db.get_download_request(retry["id"])["request_key"],
                    request_keys(equivalent)[0],
                )
                self.assertIn(
                    ":history:", db.get_download_request(initial["id"])["request_key"]
                )

    def test_pending_download_owner_requires_persisted_chat_and_user(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "downloads.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                owned_id, _ = db.create_download_request(
                    "owned-key", "magnet", chat_id="-100", user_id="9"
                )
                self.assertEqual(
                    db.bind_pending_download_request_owner(
                        owned_id, chat_id="-100", user_id="9"
                    )["user_id"],
                    "9",
                )
                self.assertIsNone(
                    db.bind_pending_download_request_owner(
                        owned_id, chat_id="-100", user_id="10"
                    )
                )

                incomplete_id, _ = db.create_download_request(
                    "incomplete-owner-key", "magnet", chat_id="-100"
                )
                self.assertIsNone(
                    db.bind_pending_download_request_owner(
                        incomplete_id, chat_id="-100", user_id="10"
                    )
                )
                db.update_download_request(
                    owned_id, status="cancelled", completed_at=db.now()
                )
                self.assertIsNone(
                    db.bind_pending_download_request_owner(
                        owned_id, chat_id="-100", user_id="9"
                    )
                )


class SecurityTests(InitializedWebTestCase):
    def test_blank_web_credentials_fail_closed(self):
        from app import config

        with patch("app.config.get", side_effect=lambda key, default="": ""):
            self.assertEqual(config.web_credentials(), ("", ""))

    def setUp(self):
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf_token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _authenticated(self):
        from app.config import web_credentials

        login_page = self.client.get("/login")
        token = self._csrf_token(login_page)
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        settings = self.client.get("/settings")
        return {"X-CSRF-Token": self._csrf_token(settings)}

    def test_health_headers_and_csrf(self):
        favicon = self.client.get("/favicon.ico")
        self.assertEqual(favicon.status_code, 200)
        self.assertIn(favicon.headers["content-type"], {"image/svg+xml", "image/x-icon"})
        self.assertIn(b"<svg", favicon.content)
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self._authenticated()
        self.assertEqual(self.client.post("/api/config", json={}).status_code, 403)

    def test_unauthenticated_dynamic_html_is_no_store_and_uses_versioned_shared_js(self):
        login = self.client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.headers["cache-control"], "no-store")
        self.assertIn("/static/js/lucide.min.js?v=20260816a", login.text)
        self.assertIn("/static/js/app.js?v=20260824a", login.text)

    def test_authenticated_html_is_no_store_without_affecting_static_assets(self):
        self._authenticated()

        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertTrue(settings.headers["content-type"].startswith("text/html"))
        self.assertEqual(settings.headers["cache-control"], "no-store")
        self.assertIn("/static/js/lucide.min.js?v=20260816a", settings.text)
        self.assertRegex(settings.text, r"/static/js/app\.js\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        static = self.client.get("/static/js/app.js")
        self.assertEqual(static.status_code, 200)
        self.assertNotEqual(static.headers.get("cache-control"), "no-store")

    def test_api_no_store_policy_remains_unchanged(self):
        headers = self._authenticated()
        with patch("app.routes.api.build_dashboards", return_value=[]):
            response = self.client.get("/api/dashboard", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.headers["content-type"].startswith("application/json"))

    def test_dashboard_api_force_refresh_bypasses_service_cache(self):
        headers = self._authenticated()
        with patch("app.routes.api.build_dashboards", return_value=[]) as dashboards:
            response = self.client.get("/api/dashboard?refresh=1", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        dashboards.assert_called_once_with(force=True)

    def test_dashboard_api_exposes_playable_total_and_media_composition(self):
        headers = self._authenticated()
        board = DashboardData(
            server_name="AIO",
            server_type="jellyfin",
            online=True,
            total_items=16,
            movie_count=3,
            series_count=8,
            episode_count=13,
        )
        with patch("app.routes.api.build_dashboards", return_value=[board]):
            response = self.client.get("/api/dashboard", headers=headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()[0]
        self.assertEqual(payload["total_items"], 16)
        self.assertEqual(payload["movie_count"], 3)
        self.assertEqual(payload["series_count"], 8)
        self.assertEqual(payload["episode_count"], 13)

    def test_download_overview_reports_unconfigured_qb_without_creating_client(self):
        headers = self._authenticated()
        with patch(
            "app.routes.downloads_api.config.get",
            side_effect=lambda key, default="": "" if key == "QB_URL" else default,
        ), patch("app.routes.downloads_api.QBittorrentClient") as qb_client:
            response = self.client.get("/api/downloads/overview", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["qb"], {
            "configured": False,
            "online": False,
            "tasks": [],
            "transfer": None,
            "error_code": "not_configured",
            "error": "未连接到 qBittorrent",
        })
        self.assertNotIn("guangya", response.json())
        qb_client.assert_not_called()

    def test_download_overview_hides_configured_qb_connection_exception(self):
        headers = self._authenticated()
        client = Mock()
        client.list_torrents.side_effect = RuntimeError("requests secret connection detail")
        with patch(
            "app.routes.downloads_api.config.get",
            side_effect=lambda key, default="": "http://qb.local:8080" if key == "QB_URL" else default,
        ), patch("app.routes.downloads_api.QBittorrentClient", return_value=client):
            response = self.client.get("/api/downloads/overview", headers=headers)
        self.assertEqual(response.status_code, 200)
        qb = response.json()["qb"]
        self.assertTrue(qb["configured"])
        self.assertFalse(qb["online"])
        self.assertEqual(qb["error_code"], "connection_failed")
        self.assertEqual(qb["error"], "连接失败，请检查地址、认证信息和网络")
        self.assertNotIn("requests secret", response.text)

    def test_stale_login_csrf_returns_recoverable_form(self):
        initial = self.client.get("/login")
        old_token = self._csrf_token(initial)
        response = self.client.post(
            "/login",
            data={
                "csrf_token": "stale-token",
                "username": "admin",
                "password": "ignored",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("登录会话已刷新", response.text)
        self.assertNotIn("CSRF token invalid", response.text)
        self.assertNotEqual(self._csrf_token(response), old_token)

    def test_organize_log_api_protects_legacy_records(self):
        headers = self._authenticated()
        legacy = {
            "id": 9, "source": "guangya", "original_path": "source/folder",
            "new_path": "movie/file.mkv", "file_id": "f1", "status": "success",
            "tmdb_id": "1", "operation_type": "organize", "source_dir_id": "",
            "original_parent_id": "", "original_name": "", "current_parent_id": "",
            "current_name": "", "target_parent_id": "", "media_type": "movie",
            "title": "", "year": "", "season": None, "episode": None, "error": "",
            "parent_log_id": None, "operation_token": "", "version": 1,
            "legacy_incomplete": 1, "created_at": "2026-07-25 00:00:00",
            "updated_at": "2026-07-25 00:00:00",
        }
        with patch("app.database.get_organize_log", return_value=legacy), patch(
            "app.database.list_organize_log_items", return_value=[]
        ), patch("app.database.list_organize_operation_steps", return_value=[]):
            detail = self.client.get("/api/logs/organize/9", headers=headers)
            reverted = self.client.post(
                "/api/logs/organize/9/revert",
                json={"operation_token": "t", "expected_version": 1}, headers=headers,
            )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("禁止猜测式回退", detail.json()["safety_notice"])
        self.assertFalse(detail.json()["allowed_actions"]["revert"])
        self.assertEqual(reverted.status_code, 400)
        self.assertIn("没有可安全回退", reverted.json()["error"])

    def test_runtime_log_endpoint_requires_login_and_returns_fixed_source(self):
        self.assertEqual(self.client.get('/api/logs/runtime').status_code, 401)
        self._authenticated()
        snapshot = RuntimeLogChunk(
            events=(RuntimeLogEvent('safe line', 456, 'event-checkpoint'),),
            offset=456,
            stream_id='abc:def',
            checkpoint='chunk-checkpoint',
            generation=7,
        )
        with patch('app.routes.logs_api.read_stream_chunk', return_value=snapshot), patch(
            'app.routes.logs_api.log_snapshot', return_value=(123, 456)
        ):
            response = self.client.get('/api/logs/runtime?lines=9999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['source'], 'app.log')
        self.assertEqual(response.json()['offset'], 456)
        self.assertEqual(response.json()['lines'], ['safe line'])
        self.assertEqual(response.json()['stream_id'], 'abc:def')
        self.assertEqual(response.json()['checkpoint'], 'chunk-checkpoint')
        self.assertEqual(response.json()['generation'], 7)

    def test_runtime_log_clear_endpoint_truncates_persistent_file_and_is_idempotent(self):
        headers = self._authenticated()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("old-one\nold-two\n", encoding="utf-8")
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = self.client.delete("/api/logs/runtime", headers=headers)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(path.read_bytes(), b"")
                self.assertEqual(first.json()["offset"], 0)
                self.assertEqual(first.json()["checkpoint"], "")
                first_generation = first.json()["generation"]
                second = self.client.delete("/api/logs/runtime", headers=headers)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(path.read_bytes(), b"")
                self.assertGreater(second.json()["generation"], first_generation)

    def test_runtime_log_clear_keeps_existing_handler_writable(self):
        from app.logger import _WindowsSafeTimedRotatingFileHandler, clear_runtime_log

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            handler = _WindowsSafeTimedRotatingFileHandler(
                path, when="midnight", backupCount=1, encoding="utf-8"
            )
            logger = logging.getLogger("runtime-clear-handler-test")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                logger.info("old")
                self.assertIn("old", path.read_text(encoding="utf-8"))
                clear_runtime_log(path)
                self.assertEqual(path.read_bytes(), b"")
                logger.info("new")
                handler.flush()
                rendered = path.read_text(encoding="utf-8")
            finally:
                logger.handlers = []
                root.removeHandler(handler)
                handler.close()
        self.assertIn("new", rendered)
        self.assertNotIn("old", rendered)

    def test_runtime_log_clear_baseline_does_not_skip_write_after_truncate(self):
        from app.modules import runtime_log

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("old\n", encoding="utf-8")

            def truncate_then_write(target):
                with Path(target).open("r+b") as handle:
                    handle.seek(0)
                    handle.truncate(0)
                with Path(target).open("a", encoding="utf-8") as handle:
                    handle.write("written-in-old-sampling-window\n")

            with patch("app.modules.runtime_log.APP_LOG", path), patch.object(
                runtime_log, "clear_runtime_log", side_effect=truncate_then_write
            ):
                generation, offset, stream_id, checkpoint = clear_logs()
                chunk = read_stream_chunk(
                    offset,
                    expected_stream_id=stream_id,
                    expected_checkpoint=checkpoint,
                    expected_generation=generation,
                )

        self.assertEqual(offset, 0)
        self.assertEqual(checkpoint, "")
        self.assertEqual(
            [event.line for event in chunk.events],
            ["written-in-old-sampling-window"],
        )

    def test_runtime_log_generation_excludes_pre_clear_cursor_and_shows_new_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("old\n", encoding="utf-8")
            with patch("app.modules.runtime_log.APP_LOG", path):
                before = read_stream_chunk(0)
                old_generation = log_generation()
                generation, offset, stream_id, checkpoint = clear_logs()
                self.assertGreater(generation, old_generation)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("new\n")
                after = read_stream_chunk(
                    before.offset,
                    expected_stream_id=before.stream_id,
                    expected_checkpoint=before.checkpoint,
                    expected_generation=old_generation,
                )
                baseline = read_stream_chunk(
                    offset,
                    expected_stream_id=stream_id,
                    expected_checkpoint=checkpoint,
                    expected_generation=generation,
                )
        self.assertEqual(after.reset_reason, "cleared")
        self.assertEqual([event.line for event in after.events], ["new"])
        self.assertEqual([event.line for event in baseline.events], ["new"])
        self.assertNotIn("old", "\n".join(event.line for event in after.events))

    def test_runtime_log_stream_uses_reset_start_and_per_line_offsets(self):
        from app.routes import logs_api

        class Request:
            def __init__(self):
                self.checks = 0

            async def is_disconnected(self):
                self.checks += 1
                return self.checks > 1

        chunk = RuntimeLogChunk(
            events=(
                RuntimeLogEvent("first", 6, "checkpoint-6"),
                RuntimeLogEvent("second", 13, "checkpoint-13"),
            ),
            offset=13,
            stream_id="abc:def",
            reset_reason="rotated",
            reset_offset=0,
            checkpoint="checkpoint-13",
            reset_checkpoint="",
            generation=0,
        )
        with patch("app.routes.logs_api.require_api_login"), patch(
            "app.routes.logs_api.read_stream_chunk", return_value=chunk
        ) as reader:
            response = asyncio.run(
                logs_api.runtime_log_stream(
                    Request(), offset=99, stream_id="old:id", checkpoint="old-checkpoint",
                    generation=0,
                )
            )

            async def first_events():
                events = []
                async for item in response.body_iterator:
                    events.append(item)
                    if len(events) == 3:
                        break
                return events

            events = asyncio.run(first_events())
        self.assertIn('"offset": 0', events[0])
        self.assertIn('"stream_id": "abc:def"', events[0])
        self.assertIn("event: reset", events[0])
        self.assertIn('"offset": 6', events[1])
        self.assertIn('"offset": 13', events[2])
        self.assertIn('"checkpoint": "checkpoint-6"', events[1])
        self.assertIn('"checkpoint": "checkpoint-13"', events[2])
        self.assertIn("event: log", events[1])
        self.assertIn("event: log", events[2])
        reader.assert_called_once_with(
            99, expected_stream_id="old:id", expected_checkpoint="old-checkpoint",
            expected_generation=0,
        )

    def test_runtime_log_stream_reports_truncated_line_once_as_reset_notice(self):
        from app.routes import logs_api

        class Request:
            def __init__(self):
                self.checks = 0

            async def is_disconnected(self):
                self.checks += 1
                return self.checks > 1

        notice = "--- 单条日志超过显示上限，内容已截断 ---"
        chunk = RuntimeLogChunk(
            events=(RuntimeLogEvent(notice, 4096, "checkpoint-4096"),),
            offset=4096,
            stream_id="abc:def",
            reset_reason="line_truncated",
            reset_offset=4096,
            checkpoint="checkpoint-4096",
            reset_checkpoint="checkpoint-4096",
        )
        with patch("app.routes.logs_api.require_api_login"), patch(
            "app.routes.logs_api.read_stream_chunk", return_value=chunk
        ):
            response = asyncio.run(
                logs_api.runtime_log_stream(
                    Request(), offset=0, stream_id="", checkpoint="", generation=0
                )
            )

            async def first_event():
                async for item in response.body_iterator:
                    return item
                return ""

            event = asyncio.run(first_event())

        self.assertIn("event: reset", event)
        self.assertIn('"reason": "line_truncated"', event)
        self.assertIn(f'"notice": "{notice}"', event)
        self.assertNotIn("event: log", event)

    def test_runtime_log_tail_reads_fixed_file_and_detects_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text(
                "one\nGET https://api.telegram.org/bot123456:runtimeSecret/getMe\nthree\n",
                encoding="utf-8",
            )
            with patch("app.modules.runtime_log.APP_LOG", path):
                self.assertEqual(
                    read_last_lines(2),
                    ["GET https://api.telegram.org/bot123456:********/getMe", "three"],
                )
                lines, offset, rotated = read_from_offset(0)
                self.assertEqual(
                    lines,
                    ["one", "GET https://api.telegram.org/bot123456:********/getMe", "three"],
                )
                self.assertFalse(rotated)
                path.write_text("new\n", encoding="utf-8")
                next_lines, _, rotated = read_from_offset(offset)
                self.assertTrue(rotated)
                self.assertEqual(next_lines, ["new"])

    def test_runtime_log_checkpoint_detects_copytruncate_after_fast_regrow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            original = "".join(
                f"2026-08-01 00:00:{index % 60:02d}.000 | INFO    | app.worker | old-{index:04d}\n"
                for index in range(200)
            )
            path.write_text(original, encoding="utf-8")
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = read_stream_chunk(0, max_bytes=4 * 1024 * 1024)
                original_inode = path.stat().st_ino
                replacement = "".join(
                    f"2026-08-01 01:00:{index % 60:02d}.000 | INFO    | app.worker | new-{index:04d}\n"
                    for index in range(300)
                )
                path.write_text(replacement, encoding="utf-8")
                self.assertEqual(path.stat().st_ino, original_inode)
                self.assertGreater(path.stat().st_size, first.offset)
                second = read_stream_chunk(
                    first.offset,
                    expected_stream_id=first.stream_id,
                    expected_checkpoint=first.checkpoint,
                    max_bytes=4 * 1024 * 1024,
                )

        self.assertEqual(second.reset_reason, "rotated")
        self.assertEqual(second.reset_offset, 0)
        self.assertTrue(second.events)
        self.assertIn("new-0000", second.events[0].line)

    def test_runtime_log_checkpoint_samples_head_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            shared_tail = b"z" * 127 + b"\n"
            original = b"a" * 256 + shared_tail
            replacement = b"b" * 256 + shared_tail
            path.write_bytes(original)
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = read_stream_chunk(0, max_bytes=4096)
                original_inode = path.stat().st_ino
                path.write_bytes(replacement)
                self.assertEqual(path.stat().st_ino, original_inode)
                second = read_stream_chunk(
                    first.offset,
                    expected_stream_id=first.stream_id,
                    expected_checkpoint=first.checkpoint,
                    max_bytes=4096,
                )

        self.assertEqual(second.reset_reason, "rotated")
        self.assertEqual(second.reset_offset, 0)

    def test_runtime_log_checkpoint_allows_normal_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text(
                "2026-08-01 00:00:00.000 | INFO    | app.worker | first\n",
                encoding="utf-8",
            )
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = read_stream_chunk(0)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "2026-08-01 00:00:01.000 | INFO    | app.worker | second\n"
                    )
                second = read_stream_chunk(
                    first.offset,
                    expected_stream_id=first.stream_id,
                    expected_checkpoint=first.checkpoint,
                )

        self.assertEqual(second.reset_reason, "")
        self.assertEqual([event.line.rsplit(" | ", 1)[-1] for event in second.events], ["second"])
        self.assertNotEqual(second.checkpoint, first.checkpoint)

    def test_runtime_log_dense_short_lines_are_bounded_by_event_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            physical_line = (
                "2026-08-01 00:00:00.000 | INFO    | app.worker | dense\n"
            ).encode("utf-8")
            path.write_bytes(physical_line * 10000)
            expected_size = path.stat().st_size
            with patch("app.modules.runtime_log.APP_LOG", path):
                chunk = read_stream_chunk(
                    0, max_bytes=64 * 1024, max_events=64
                )

        self.assertEqual(len(chunk.events), 64)
        self.assertEqual(chunk.reset_reason, "tail_rebase")
        self.assertEqual(chunk.offset, expected_size)
        self.assertEqual(chunk.reset_offset, expected_size - len(physical_line) * 64)
        self.assertTrue(chunk.checkpoint)

    def test_runtime_log_stream_rebases_to_a_bounded_tail_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text(
                "".join(
                    f"2026-08-01 00:00:{index % 60:02d}.000 | INFO    | app.worker | line-{index:05d}-payload\n"
                    for index in range(1000)
                ),
                encoding="utf-8",
            )
            expected_size = path.stat().st_size
            with patch("app.modules.runtime_log.APP_LOG", path):
                lines, offset, reset = read_from_offset(0, max_bytes=4096)

        self.assertTrue(reset)
        self.assertEqual(offset, expected_size)
        self.assertNotIn("line-00000-payload", lines)
        self.assertTrue(lines[-1].endswith("line-00999-payload"))
        self.assertLess(sum(len(line) + 1 for line in lines), 4096)

    def test_runtime_log_stream_reports_tail_rebase_without_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text(
                "".join(
                    f"2026-08-01 00:00:{index % 60:02d}.000 | INFO    | app.worker | item-{index:05d}\n"
                    for index in range(1000)
                ),
                encoding="utf-8",
            )
            with patch("app.modules.runtime_log.APP_LOG", path):
                chunk = read_stream_chunk(0, max_bytes=4096)

        self.assertEqual(chunk.reset_reason, "tail_rebase")
        self.assertGreater(chunk.reset_offset, 0)
        self.assertTrue(chunk.events)

    def test_runtime_log_stream_detects_same_size_file_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("old-a\nold-b\n", encoding="utf-8")
            with patch("app.modules.runtime_log.APP_LOG", path):
                original_stream_id = log_identity()
                offset = path.stat().st_size
                replacement = Path(tmp) / "replacement.log"
                replacement.write_text("new-a\nnew-b\n", encoding="utf-8")
                replacement.replace(path)
                chunk = read_stream_chunk(offset, expected_stream_id=original_stream_id)

        self.assertEqual(chunk.reset_reason, "rotated")
        self.assertEqual(chunk.reset_offset, 0)
        self.assertEqual([item.line for item in chunk.events], ["new-a", "new-b"])
        self.assertNotEqual(chunk.stream_id, original_stream_id)

    def test_runtime_log_tail_rebase_keeps_long_telebot_traceback_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            marker = "2026-08-01 00:00:00.000 | ERROR   | TeleBot | Exception traceback:"
            continuation = "".join(
                f'  File "secret-{index:05d}.py", line 1\n' for index in range(4000)
            )
            recovered = "2026-08-01 00:00:01.000 | INFO    | app.worker | recovered"
            path.write_text(f"{marker}\n{continuation}{recovered}\n", encoding="utf-8")
            with patch("app.modules.runtime_log.APP_LOG", path):
                chunk = read_stream_chunk(0, max_bytes=4096)

        lines = [item.line for item in chunk.events]
        self.assertEqual(chunk.reset_reason, "tail_rebase")
        self.assertEqual(lines, [recovered])
        self.assertNotIn("secret-", "\n".join(lines))

    def test_runtime_log_stream_marks_and_advances_past_an_oversized_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_bytes(b"x" * 10000)
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = read_stream_chunk(0, max_bytes=4096)
                with path.open("ab") as handle:
                    handle.write(b" completed\n")
                second = read_stream_chunk(
                    first.offset,
                    expected_stream_id=first.stream_id,
                    expected_checkpoint=first.checkpoint,
                    max_bytes=4096,
                )

        self.assertEqual(first.reset_reason, "line_truncated")
        self.assertEqual(
            [item.line for item in first.events],
            ["--- 单条日志超过显示上限，内容已截断 ---"],
        )
        self.assertGreater(first.offset, 0)
        self.assertEqual(second.events, ())
        self.assertEqual(second.reset_reason, "")

    def test_runtime_log_stream_silently_discards_remaining_oversized_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_bytes(b"x" * 4096)
            with patch("app.modules.runtime_log.APP_LOG", path):
                first = read_stream_chunk(0, max_bytes=4096)
                with path.open("ab") as handle:
                    handle.write(b"y" * 4096)
                second = read_stream_chunk(
                    first.offset,
                    expected_stream_id=first.stream_id,
                    expected_checkpoint=first.checkpoint,
                    max_bytes=4096,
                )
                with path.open("ab") as handle:
                    handle.write(b"\n")
                    handle.write(
                        b"2026-08-01 00:00:01.000 | INFO    | app.worker | visible\n"
                    )
                third = read_stream_chunk(
                    second.offset,
                    expected_stream_id=second.stream_id,
                    expected_checkpoint=second.checkpoint,
                    max_bytes=4096,
                )

        self.assertEqual(first.reset_reason, "line_truncated")
        self.assertTrue(first.checkpoint.startswith("discard:"))
        self.assertEqual(second.reset_reason, "")
        self.assertEqual(second.events, ())
        self.assertTrue(second.checkpoint.startswith("discard:"))
        self.assertEqual(third.reset_reason, "")
        self.assertFalse(third.checkpoint.startswith("discard:"))
        self.assertEqual(
            [event.line.rsplit(" | ", 1)[-1] for event in third.events],
            ["visible"],
        )

    def test_runtime_log_folds_historical_telebot_traceback_and_normalizes_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            before = "2026-08-01 00:00:00.000 | INFO    | app.worker | before"
            summary = (
                "2026-08-01 00:00:01.000 | ERROR   | TeleBot | Infinity polling exception: "
                "ConnectTimeoutError https://api.telegram.org/bot123456:historySecret/getMe"
            )
            marker = "2026-08-01 00:00:01.001 | ERROR   | TeleBot | Exception traceback:"
            continuation = [
                "Traceback (most recent call last):",
                '  File "connectionpool.py", line 1, in urlopen',
                "requests.exceptions.ConnectTimeout: timed out",
                "",
            ]
            after = "2026-08-01 00:00:02.000 | INFO    | app.worker | after"
            raw_lines = [before, summary, marker, *continuation, after]
            path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
            expected_summary = (
                "2026-08-01 00:00:01.000 | ERROR   | TeleBot | "
                "Telegram Bot 连接超时（ConnectTimeout），将在后台自动重试"
            )
            with patch("app.modules.runtime_log.APP_LOG", path):
                self.assertEqual(read_last_lines(10), [before, expected_summary, after])
                lines, _, rotated = read_from_offset(0)
                self.assertFalse(rotated)
                self.assertEqual(lines, [before, expected_summary, after])

                raw = path.read_bytes()
                middle = raw.index(b"connectionpool.py") + 6
                lines, _, reset = read_from_offset(middle)
                self.assertTrue(reset)
                self.assertEqual(lines, [after])

    def test_runtime_log_incremental_read_keeps_traceback_suppressed_until_next_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            before = "2026-08-01 00:00:00.000 | INFO    | app.worker | before"
            marker = "2026-08-01 00:00:01.000 | ERROR   | TeleBot | Exception traceback:"
            path.write_text(
                "\n".join([before, marker, "Traceback (most recent call last):"]) + "\n",
                encoding="utf-8",
            )
            with patch("app.modules.runtime_log.APP_LOG", path):
                first, offset, _ = read_from_offset(0)
                self.assertEqual(first, [before])
                with path.open("a", encoding="utf-8") as handle:
                    handle.write('  File "requests.py", line 1\n')
                    handle.write("requests.exceptions.ConnectTimeout: timed out\n")
                    handle.write("2026-08-01 00:00:02.000 | INFO    | app.worker | recovered\n")
                second, _, rotated = read_from_offset(offset)
                self.assertFalse(rotated)
                self.assertEqual(
                    second,
                    ["2026-08-01 00:00:02.000 | INFO    | app.worker | recovered"],
                )

    def test_runtime_log_does_not_advance_past_incomplete_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_bytes(b"2026-08-01 00:00:00.000 | INFO    | app.worker | partial")
            with patch("app.modules.runtime_log.APP_LOG", path):
                first, offset, _ = read_from_offset(0)
                self.assertEqual(first, [])
                self.assertEqual(offset, 0)
                with path.open("ab") as handle:
                    handle.write(b" complete\n")
                second, new_offset, _ = read_from_offset(offset)
                self.assertEqual(
                    second,
                    ["2026-08-01 00:00:00.000 | INFO    | app.worker | partial complete"],
                )
                self.assertEqual(new_offset, path.stat().st_size)

    def test_media_image_proxy_access_and_input_boundary(self):
        self.assertEqual(
            self.client.get("/media-image/jellyfin/" + "a" * 32).status_code,
            401,
        )
        self._authenticated()
        self.assertEqual(
            self.client.get("/media-image/jellyfin/not-an-id").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/media-image/unknown/" + "a" * 32).status_code,
            404,
        )

    def test_naming_config_rejects_unknown_template_variables(self):
        headers = self._authenticated()
        response = self.client.post(
            "/api/config",
            json={"MEDIA_MOVIE_TEMPLATE": "${notAllowed}.${ext}"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("包含不允许的配置项", response.json()["error"])
        self.assertIn("MEDIA_MOVIE_TEMPLATE", response.json()["error"])

    def test_telegram_test_message_endpoint(self):
        headers = self._authenticated()
        sent = SimpleNamespace(message_id=108)
        bot = Mock()
        bot.send_message.return_value = sent
        with patch("telebot.TeleBot", return_value=bot):
            response = self.client.post(
                "/api/telegram/test",
                json={"token": "123456:test-token", "chat_id": "10001"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message_id"], 108)
        self.assertNotIn("test-token", response.text)
        bot.send_message.assert_called_once()

    def test_telegram_test_message_uses_saved_token_after_config_save(self):
        headers = self._authenticated()
        saved_token = "123456:saved-test-token"
        sent = SimpleNamespace(message_id=109)
        bot = Mock()
        bot.send_message.return_value = sent
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": (
                saved_token if key == "TG_BOT_TOKEN" else default
            ),
        ), patch("telebot.TeleBot", return_value=bot) as bot_factory:
            response = self.client.post(
                "/api/telegram/test",
                json={"token": "", "chat_id": "10001"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message_id"], 109)
        self.assertNotIn(saved_token, response.text)
        bot_factory.assert_called_once_with(
            saved_token, parse_mode="HTML", threaded=False
        )
        bot.send_message.assert_called_once()

    def test_media_connection_endpoint_returns_server_identity(self):
        headers = self._authenticated()
        response = Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"ServerName": "Living Room", "Version": "12.0.1"}
        with patch("app.routes.api.requests.get", return_value=response) as request_get:
            result = self.client.post(
                "/api/media/test",
                json={
                    "server_type": "jellyfin",
                    "url": "http://jellyfin.local:8096",
                    "token": "test-token",
                },
                headers=headers,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["server_name"], "Living Room")
        self.assertEqual(result.json()["product"], "Jellyfin")
        self.assertEqual(result.json()["version"], "12.0.1")
        self.assertGreaterEqual(result.json()["latency_ms"], 1)
        self.assertIn("/System/Info", request_get.call_args.args[0])
        self.assertIsNone(request_get.call_args.kwargs["params"])
        self.assertIn("MediaBrowser Token", request_get.call_args.kwargs["headers"]["Authorization"])
        self.assertNotIn("test-token", result.text)

    def test_legacy_media_connection_identifies_jellyfin_10_and_uses_compatible_auth(self):
        headers = self._authenticated()
        response = Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ServerName": "Legacy Room",
            "ProductName": "Jellyfin Server",
            "Version": "10.11.11",
        }
        with patch("app.routes.api.requests.get", return_value=response) as request_get:
            result = self.client.post(
                "/api/media/test",
                json={
                    "server_type": "emby",
                    "url": "http://legacy.local:8096",
                    "token": "legacy-token",
                },
                headers=headers,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["product"], "Jellyfin")
        self.assertEqual(result.json()["version"], "10.11.11")
        call = request_get.call_args
        self.assertEqual(call.kwargs["headers"]["X-Emby-Token"], "legacy-token")
        self.assertIn("MediaBrowser Token", call.kwargs["headers"]["Authorization"])
        self.assertIsNone(call.kwargs["params"])
        self.assertNotIn("legacy-token", result.text)

    def test_media_connection_endpoint_validates_inputs(self):
        headers = self._authenticated()
        invalid_type = self.client.post(
            "/api/media/test",
            json={"server_type": "plex", "url": "http://plex.local", "token": "x"},
            headers=headers,
        )
        invalid_url = self.client.post(
            "/api/media/test",
            json={"server_type": "emby", "url": "file:///tmp/test", "token": "x"},
            headers=headers,
        )
        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(invalid_url.status_code, 400)

    def test_proxy_batch_uses_only_fixed_targets_and_injects_configured_services(self):
        headers = self._authenticated()
        values = {
            "PROXY_URL": "",
            "JELLYFIN_URL": "http://jellyfin.local:8096",
            "EMBY_URL": "http://emby.local:8096",
            "QB_URL": "http://qb.local:8080",
            "QB_API_KEY": "qb-secret",
        }
        response = Mock(status_code=200)
        with patch("app.routes.tools_api.config.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.routes.tools_api.requests.get", return_value=response
        ) as request_get:
            result = self.client.post(
                "/api/tools/proxy/test", json={"use_proxy": False}, headers=headers
            )
        self.assertEqual(result.status_code, 200)
        payload = result.json()
        keys = {item["key"] for item in payload["results"]}
        self.assertTrue({"jellyfin", "emby", "qb", "guangya_web", "guangya_api"}.issubset(keys))
        self.assertEqual(payload["summary"]["total"], 14)
        called_urls = {call.args[0] for call in request_get.call_args_list}
        self.assertIn("http://jellyfin.local:8096/System/Info/Public", called_urls)
        self.assertIn("http://emby.local:8096/System/Info/Public", called_urls)
        self.assertIn("http://qb.local:8080/api/v2/app/version", called_urls)

        rejected = self.client.post(
            "/api/tools/proxy/test",
            json={"use_proxy": False, "url": "http://127.0.0.1/private"},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("不接受自定义目标", rejected.json()["error"])

    def test_clear_organize_logs_keeps_busy_records_and_deletes_audit_children(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "organize.db"
            with patch("app.database.DB_PATH", test_db):
                db.init_db()
                removable = db.add_organize_log(
                    "guangya", "source", "target", "file-a", "success", "1"
                )
                busy = db.add_organize_log(
                    "guangya", "source", "target", "file-b", "reorganizing", "2"
                )
                db.add_organize_log_items(removable, [{
                    "file_id": "file-a", "role": "video", "original_parent_id": "source",
                    "original_name": "A.mkv", "current_parent_id": "target", "current_name": "A.mkv",
                }])
                db.add_organize_operation_step(
                    removable, "token", 1, "rename", file_id="file-a"
                )
                source_id = db.create_local_media_source(
                    "本地测试", "", "", "/downloads", owner="admin"
                )
                removable_local = db.create_local_media_task(
                    source_id, "", "/downloads/finished.mkv", owner="admin", trigger="manual"
                )
                busy_local = db.create_local_media_task(
                    source_id, "", "/downloads/running.mkv", owner="admin", trigger="manual"
                )
                db.update_local_media_task(removable_local, owner="admin", status="completed")
                db.update_local_media_task(busy_local, owner="admin", status="moving")
                db.add_local_media_task_item(
                    removable_local, "/downloads/finished.mkv", "/media/finished.mkv",
                    role="video", owner="admin",
                )
                local_token = db.get_local_media_task(removable_local, owner="admin").operation_token
                db.add_local_media_operation_step(
                    removable_local, local_token, 1, "move",
                    "/downloads/finished.mkv", "/media/finished.mkv", owner="admin",
                )

                result = db.clear_organize_logs()
                self.assertEqual(result, {
                    "deleted": 2,
                    "skipped_busy": 2,
                    "deleted_guangya": 1,
                    "deleted_local": 1,
                    "skipped_busy_guangya": 1,
                    "skipped_busy_local": 1,
                })
                self.assertIsNone(db.get_organize_log(removable))
                self.assertIsNotNone(db.get_organize_log(busy))
                self.assertEqual(db.list_organize_log_items(removable), [])
                self.assertEqual(db.list_organize_operation_steps(removable), [])
                self.assertIsNone(db.get_local_media_task(removable_local, owner="admin"))
                self.assertIsNotNone(db.get_local_media_task(busy_local, owner="admin"))
                self.assertEqual(db.list_local_media_task_items(removable_local, owner="admin"), [])
                with db.get_conn() as conn:
                    local_steps = conn.execute(
                        "SELECT COUNT(*) FROM local_media_operation_steps WHERE task_id=?",
                        (removable_local,),
                    ).fetchone()[0]
                self.assertEqual(local_steps, 0)

    def test_clear_organize_logs_api_requires_explicit_keyword(self):
        headers = self._authenticated()
        denied = self.client.request(
            "DELETE", "/api/logs/organize", json={"confirm": "NO"}, headers=headers
        )
        self.assertEqual(denied.status_code, 400)
        with patch("app.routes.logs_api.db.clear_organize_logs", return_value={"deleted": 3, "skipped_busy": 1}):
            allowed = self.client.request(
                "DELETE", "/api/logs/organize", json={"confirm": "CLEAR"}, headers=headers
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["deleted"], 3)
        self.assertIn("未删除任何云端或本地媒体文件", allowed.json()["message"])

    def test_dashboard_and_settings_render_new_configuration_experience(self):
        self._authenticated()
        with patch("app.routes.pages.get_cached_dashboards_or_stubs", return_value=([], True)):
            dashboard = self.client.get("/")
        settings = self.client.get("/settings")
        organize = self.client.get("/organize")
        organize_rules = self.client.get("/organize-rules")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("去配置", dashboard.text)
        self.assertNotIn('class="dashboard-command"', dashboard.text)
        self.assertNotIn('class="workspace-heading"', dashboard.text)
        self.assertIn('id="mediaConfigModal"', dashboard.text)
        self.assertIn('data-save-media="jellyfin"', dashboard.text)
        self.assertIn('data-test-media="jellyfin"', dashboard.text)
        self.assertIn('data-test-media="emby"', dashboard.text)
        self.assertIn('data-key="JELLYFIN_USER_ID"', dashboard.text)
        self.assertIn('data-key="EMBY_USER_ID"', dashboard.text)
        self.assertIn("不回退管理员历史", dashboard.text)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(organize.status_code, 200)
        self.assertEqual(organize_rules.status_code, 200)
        self.assertNotIn('data-settings-target="strm"', settings.text)
        self.assertNotIn('data-settings-target="offline"', settings.text)
        self.assertIn('id="testTelegramBtn"', settings.text)
        self.assertIn('id="proxyTestBtn"', settings.text)
        self.assertIn('class="network-config-grid"', settings.text)
        self.assertIn('class="network-test-head"', settings.text)
        self.assertNotIn('id="proxyTarget"', settings.text)
        self.assertNotIn('id="proxyCustomUrl"', settings.text)
        self.assertIn('id="openLocksBtn"', settings.text)
        self.assertIn('id="tmdbLocksModal"', settings.text)
        self.assertNotIn('data-key="MEDIA_MOVIE_TEMPLATE"', settings.text)
        self.assertNotIn('data-key="MEDIA_MOVIE_DIR_TEMPLATE"', organize_rules.text)
        self.assertNotIn('data-key="MEDIA_MOVIE_TEMPLATE"', organize_rules.text)
        self.assertNotIn('data-key="MEDIA_TV_TEMPLATE"', organize_rules.text)
        self.assertNotIn('data-key="MEDIA_NAMING_SCOPE"', organize_rules.text)
        self.assertIn('data-nav-cluster="guangya"', settings.text)
        self.assertIn('>登录<', settings.text)
        self.assertIn('>离线转存<', settings.text)
        self.assertIn('>更多<', settings.text)
        self.assertNotIn('>分享转存<', settings.text)
        self.assertNotIn('>GCID 清单<', settings.text)
        self.assertIn('>STRM 同步<', settings.text)
        self.assertIn('>媒体反代<', settings.text)
        css = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(encoding="utf-8")
        self.assertIn(".media-config-modal { position: fixed; inset: 0;", css)
        self.assertIn(".media-config-modal[hidden] { display: none; }", css)
        self.assertIn(".media-config-body { min-height: 0; flex: 1 1 auto;", css)
        self.assertIn(".app-message-modal { z-index: 440; }", css)
        root = Path(__file__).resolve().parents[1] / "app"
        base_template = (root / "templates" / "base.html").read_text(encoding="utf-8")
        app_script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="appMessageModal"', base_template)
        self.assertIn("brand_wordmark('sidebar')", base_template)
        self.assertNotRegex(base_template, re.compile(r"tggodrive|tgtodrive", re.IGNORECASE))
        self.assertNotIn('<span>实用工具</span>', base_template)
        self.assertIn("window.appAlert = function", app_script)
        native_dialog_pattern = re.compile(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(")
        for path in [*(root / "templates").glob("*.html"), *(root / "static" / "js").glob("*.js")]:
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(native_dialog_pattern.search(source), f"原生浏览器操作框残留: {path}")
        tools = self.client.get('/tools')
        gcid = self.client.get('/guangya/more?view=gcid')
        logs = self.client.get('/logs')
        logs_source = logs.text + (root / 'static' / 'js' / 'logs.js').read_text(encoding='utf-8')
        self.assertNotIn('>代理连通性测试<', tools.text)
        self.assertIn('实用工具功能已归位', tools.text)
        self.assertIn('id="gcidSourceValue"', gcid.text)
        self.assertNotIn('id="tabScrape"', logs_source)
        self.assertNotIn('openScrapeBtn', logs_source)
        self.assertIn('id="clearOrganizeLogsBtn"', logs_source)
        self.assertIn('>清理记录</button>', logs_source)
        self.assertIn('光鸭与本地整理记录', logs_source)
        self.assertNotIn('清除光鸭记录', logs_source)
        self.assertNotIn('id="tabDownloads"', logs_source)
        self.assertNotIn('id="downloadPanel"', logs_source)
        self.assertNotIn('id="downloadList"', logs_source)
        self.assertIn('id="organizePagination"', logs_source)
        self.assertIn('class="logs-filterbar"', logs_source)
        self.assertIn('class="logs-batchbar"', logs_source)
        self.assertIn("ORGANIZE_LOG_PAGE_SIZE = 20", logs_source)
        self.assertIn('id="tabRuntime"', logs_source)
        self.assertIn('id="runtimeLogView"', logs_source)
        self.assertIn('id="scrapeFilename"', logs_source)
        self.assertIn('id="organizeDetailModal"', logs_source)
        self.assertIn('id="organizeTmdbCandidates"', logs_source)
        self.assertIn('id="organizeReorganizeBtn"', logs_source)
        self.assertIn('id="organizeReturnBtn"', logs_source)
        self.assertIn('id="organizeRevertBtn"', logs_source)
        self.assertIn('id="organizeDeleteBtn"', logs_source)
        self.assertIn('id="organizeBatchRenameBtn"', logs_source)
        self.assertIn("waitOrganizeTask(data.task_id,null,{reopen:false})", logs_source)
        self.assertIn("runtimeOffset=payload.offset??runtimeOffset", logs_source)
        self.assertIn("runtimeStreamId=data.stream_id||''", logs_source)
        self.assertIn("runtimeCheckpoint=data.checkpoint||''", logs_source)
        self.assertIn("runtimeGeneration=data.generation||0", logs_source)
        self.assertIn("generation:String(runtimeGeneration)", logs_source)
        self.assertIn("checkpoint:runtimeCheckpoint", logs_source)
        self.assertIn("async function clearRuntimeLogs()", logs_source)
        self.assertIn("api('/api/logs/runtime',{method:'DELETE'})", logs_source)
        self.assertIn("const requestSerial=++runtimeRequestSerial", logs_source)
        self.assertIn("source.addEventListener('cursor'", logs_source)
        self.assertIn("payload.reason==='tail_rebase'", logs_source)
        self.assertIn("let runtimeRequestSerial = 0", logs_source)
        self.assertIn("requestSerial!==runtimeRequestSerial||activeTab!=='runtime'", logs_source)
        self.assertIn("runtimeReconnectTimer=setTimeout(()=>connectRuntimeStream(requestSerial),1500)", logs_source)
        self.assertIn("++runtimeRequestSerial", logs_source)
        self.assertIn("let overviewRequestSerial = 0", logs_source)
        self.assertIn("if(requestSerial!==overviewRequestSerial)return false", logs_source)
        self.assertIn("if(!response.ok)throw new Error(data.error||'概览读取失败')", logs_source)
        self.assertIn("正在重试", logs_source)
        self.assertIn("runtimeReconnectTimer=setTimeout", logs_source)
        self.assertIn('id="lockList"', settings.text)
        self.assertIn('data-save-settings', settings.text)
        self.assertNotIn('id="saveSettingsBtn"', settings.text)
        self.assertNotIn('data-key="JELLYFIN_API_KEY"', settings.text)
        self.assertNotIn('data-key="EMBY_TOKEN"', settings.text)
        self.assertNotIn('class="media-settings-callout"', settings.text)

    def test_dashboard_compacts_media_controls_into_workbench_topbar(self):
        self._authenticated()
        board = DashboardData(
            server_name="VM-AIO",
            server_type="jellyfin",
            web_url="http://jellyfin.local:8096/",
            online=True,
        )
        with patch("app.routes.pages.get_cached_dashboards_or_stubs", return_value=([board], True)):
            dashboard = self.client.get("/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('<body class="dashboard-page">', dashboard.text)
        self.assertNotIn('class="workspace-heading"', dashboard.text)
        self.assertNotIn('class="dashboard-command"', dashboard.text)
        self.assertNotIn('data-theme-location="workspace"', dashboard.text)
        self.assertIn('id="toggleSidebar"', dashboard.text)
        self.assertNotIn('class="server-head-actions"', dashboard.text)
        self.assertIn('class="dashboard-topbar"', dashboard.text)
        self.assertIn('class="dashboard-global-search"', dashboard.text)
        self.assertIn('class="dashboard-connection-picker"', dashboard.text)
        self.assertIn('data-open-media-config="jellyfin"', dashboard.text)
        self.assertIn('href="http://jellyfin.local:8096/"', dashboard.text)
        self.assertIn("VM-AIO", dashboard.text)

        css = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(encoding="utf-8")
        self.assertIn(".toggle-slider::before { content: \"\"; position: absolute; top: 50%;", css)
        self.assertIn("transform: translate(20px,-50%);", css)
        self.assertIn(
            ".media-node-summary > div strong, .media-node-summary > div span { display: block; }",
            css,
        )
        self.assertNotIn(".media-node-summary strong, .media-node-summary span", css)
        self.assertIn(
            "@media (min-width: 901px) {\n    .dashboard-page .workspace-bar { display: none; }",
            css,
        )
        self.assertIn(".dashboard-page .workspace-actions { display: none; }", css)

    def test_settings_navigation_is_single_level_responsive_tab_bar(self):
        self._authenticated()
        settings = self.client.get("/settings")
        root = Path(__file__).resolve().parents[1] / "app" / "static" / "css"
        main_css = (root / "main.css").read_text(encoding="utf-8")
        settings_css = (root / "settings-agent.css").read_text(encoding="utf-8")

        self.assertIn('<nav class="settings-index"', settings.text)
        self.assertNotIn('<aside class="settings-index"', settings.text)
        self.assertNotIn("CONFIG SECTIONS", settings.text)
        self.assertIn("settings-agent.css", settings.text)
        self.assertIn(".settings-page .settings-workspace {", settings_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", settings_css)
        self.assertIn("min-height: 52px;", settings_css)
        self.assertIn("gap: 6px;", settings_css)
        self.assertIn("padding: 5px;", settings_css)
        self.assertIn("border: 1px solid var(--border-soft);", settings_css)
        self.assertIn("border-radius: var(--radius-md, 12px);", settings_css)
        self.assertIn("box-shadow: none;", settings_css)
        self.assertIn("height: 40px;", settings_css)
        self.assertIn("padding: 0 18px;", settings_css)
        self.assertIn("border-radius: 8px;", settings_css)
        self.assertIn(".settings-tab:hover { color: var(--text-primary); background: var(--bg-card-hover); }", settings_css)
        self.assertIn(".settings-tab.active { color: var(--accent); background: var(--accent-soft); }", settings_css)
        self.assertIn(".settings-tab:focus-visible", settings_css)
        self.assertIn("@media (max-width: 760px)", settings_css)
        self.assertIn("height: 38px; padding: 0 14px; gap: 6px;", settings_css)
        self.assertNotIn("repeat(7,minmax(0,1fr))", main_css)
        self.assertNotIn(".settings-tab::before { content:", main_css)
        self.assertNotIn(".settings-tab.active { color: var(--accent); background: transparent; border-bottom-color: var(--accent);", main_css)

    def test_theme_bootstrap_and_controls_are_shared_by_app_and_login(self):
        login = self.client.get("/login")
        self._authenticated()
        settings = self.client.get("/settings")
        self.assertEqual(login.status_code, 200)
        self.assertEqual(settings.status_code, 200)
        for html in (login.text, settings.text):
            self.assertIn("mediaflux.theme.mode", html)
            self.assertIn("prefers-color-scheme: dark", html)
            self.assertIn("data-theme-toggle", html)
            self.assertIn('class="brand-wordmark', html)
            self.assertLess(html.index("mediaflux.theme.mode"), html.index('rel="stylesheet"'))
        self.assertIn("css/core-layout.css", login.text)
        self.assertIn("css/main.css", settings.text)
        self.assertIn('data-theme-location="workspace"', settings.text)
        self.assertIn('data-theme-location="login"', login.text)
        self.assertIn("js/app.js", login.text)

    def test_brand_assets_and_favicon_are_wired_into_app_and_login(self):
        root = Path(__file__).resolve().parents[1] / "app"
        login = self.client.get("/login")
        self._authenticated()
        settings = self.client.get("/settings")
        for name in ("img/mediaflux-logo.svg", "img/mediaflux-mark.svg", "favicon.svg"):
            asset = root / "static" / name
            self.assertTrue(asset.is_file(), name)
            source = asset.read_text(encoding="utf-8")
            self.assertIn("<svg", source)
            self.assertNotIn("linearGradient", source)
        for html in (login.text, settings.text):
            self.assertIn('rel="icon" type="image/svg+xml"', html)
            self.assertIn('/static/favicon.svg', html)
        for html in (login.text, settings.text):
            self.assertIn('class="brand-mark-svg', html)
            self.assertIn("brand-mark-upper", html)
            self.assertIn("brand-mark-lower", html)
        self.assertRegex(login.text, r"css/core-layout\.css\?v=2026081[0-9][a-z]")
        self.assertRegex(settings.text, r"css/main\.css\?v=202608(?:1[0-9]|2[0-9])[a-z]")
        self.assertIn("brand-wordmark-media", login.text)
        self.assertIn("brand-wordmark-media", settings.text)

    def test_download_page_has_qb_configuration_empty_states(self):
        self._authenticated()
        page = self.client.get("/downloads")
        downloads_source = page.text + (Path(__file__).resolve().parents[1] / "app/static/js/downloads.js").read_text(encoding="utf-8")
        self.assertIn('class="qb-empty-state"', downloads_source)
        self.assertIn("未连接到 qBittorrent", downloads_source)
        self.assertIn("连接失败，请检查地址、认证信息和网络", downloads_source)
        self.assertIn('href="/settings#downloads"', downloads_source)
        self.assertIn("前往 qB 配置", downloads_source)
        self.assertIn("qb.error_code==='not_configured'", downloads_source)

    def test_scrape_input_keeps_filename_field_flexible_and_action_compact(self):
        css = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".scrape-input .form-input { min-width: 0; flex: 1 1 auto; }", css)
        self.assertIn(".scrape-input .btn { width: auto; min-width: 112px; flex: 0 0 auto; }", css)
        self.assertIn(".scrape-input .btn { width: 100%; max-width: 100% !important; }", css)

    def test_operational_pages_share_compact_workspace_header_and_fixed_scrollbar(self):
        self._authenticated()
        pages = {
            "/logs": "日志记录",
            "/local-media": "本地媒体",
            "/organize-rules": "整理规则",
            "/downloads": "下载管理",
            "/rss": "订阅",
            "/guangya": "光鸭云盘",
            "/guangya/offline": "光鸭离线转存",
            "/guangya/more": "更多",
            "/guangya/more": "更多",
            "/organize": "光鸭整理",
            "/guangya/strm": "光鸭 STRM 同步",
            "/guangya/media-proxy": "Emby / Jellyfin 媒体反代",
            "/guangya/more?view=gcid": "更多",
        }
        for path, title in pages.items():
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn("compact-workspace-page", page.text)
                self.assertIn(f'<h1 class="page-title">{title}</h1>', page.text)

        root = Path(__file__).resolve().parents[1]
        css = (root / "app" / "static" / "css" / "main.css").read_text(encoding="utf-8")
        local_media_css = (root / "app" / "static" / "css" / "local-media.css").read_text(encoding="utf-8")
        organize_template = (root / "app" / "templates" / "organize.html").read_text(encoding="utf-8")
        self.assertIn("overflow-x: hidden; overflow-y: scroll; }", css)
        self.assertNotIn("overflow-y: scroll; scrollbar-gutter: stable", css)
        self.assertIn(".compact-workspace-page::after { display: none; }", css)
        self.assertIn(".compact-workspace-page .content { animation: none; }", css)
        self.assertIn(".compact-workspace-page .workspace-bar", css)
        self.assertIn("min-height: 64px", css)
        self.assertIn("padding: 0 24px", css)
        self.assertIn(".compact-workspace-page .workspace-kicker::after", css)
        self.assertNotIn(".local-media-page .workspace-bar", local_media_css)
        local_media_template = (root / "app" / "templates" / "local_media.html").read_text(encoding="utf-8")
        self.assertRegex(local_media_template, r"local-media\.css'\) \}\}\?v=202608(?:1[8-9]|2[0-9])[a-z]")
        self.assertNotIn("organize-rules-workspace-heading", organize_template)
        self.assertNotIn("updateOrganizeRulesHeader", organize_template)

    def test_agent_page_disables_shell_animation_and_keeps_history_rail_responsive(self):
        self._authenticated()
        page = self.client.get("/agent")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="agent-page-body"', page.text)
        self.assertIn('aria-controls="agentHistoryRail"', page.text)
        self.assertIn('aria-expanded="false"', page.text)
        self.assertIn('<dialog class="agent-rail agent-history-drawer"', page.text)
        self.assertIn('data-agent-history-close', page.text)
        self.assertNotIn('onclick="document.querySelector', page.text)

        root = Path(__file__).resolve().parents[1] / "app" / "static"
        css = (root / "css" / "agent.css").read_text(encoding="utf-8")
        script = (root / "js" / "agent.js").read_text(encoding="utf-8")
        self.assertIn("body.agent-page-body::after { display: none; }", css)
        self.assertIn("max-width: 100%; animation: none;", css)
        self.assertIn(".agent-history-drawer::backdrop", css)
        self.assertIn(".agent-history-drawer[open] { display: flex;", css)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn(
            ".agent-submit-slot { width: 44px; height: 44px; min-width: 44px; flex: 0 0 44px",
            css,
        )
        self.assertIn("max-width: 44px; display: inline-flex", css)
        self.assertNotIn("show-rail", css)
        self.assertIn("historyRail.showModal()", script)
        self.assertIn("historyRail.close()", script)
        self.assertIn("typeof historyRail.close === 'function'", script)
        self.assertIn("historyRail.removeAttribute('open')", script)
        self.assertIn("if (!capabilityNode) return;", script)

    def test_sidebar_bootstrap_and_collapsed_guangya_flyout_contract(self):
        self._authenticated()
        page = self.client.get("/guangya")
        settings = self.client.get("/settings")
        root = Path(__file__).resolve().parents[1] / "app"
        script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "css" / "main.css").read_text(encoding="utf-8")
        self.assertIn("mediaflux.sidebar.collapsed", page.text)
        self.assertIn("document.documentElement.dataset.sidebar", page.text)
        self.assertLess(page.text.index("mediaflux.sidebar.collapsed"), page.text.index("css/main.css"))
        self.assertIn('class="nav-cluster-flyout"', page.text)
        self.assertIn('aria-controls="guangyaSubmenu guangyaFlyout"', page.text)
        self.assertIn('id="guangyaSubmenu"', page.text)
        self.assertIn('class="nav-cluster open" data-nav-cluster="guangya" data-nav-default-open="true"', settings.text)
        self.assertIn("mediaflux.nav.guangya.open", settings.text)
        for href in (
            "/guangya", "/guangya/offline", "/guangya/more",
            "/guangya/strm", "/guangya/media-proxy", "/organize",
        ):
            self.assertIn(f'href="{href}"', page.text)
        self.assertIn('class="nav-flyout-item active"', page.text)
        self.assertIn("document.documentElement.dataset.sidebar", script)
        self.assertIn("closeGuangyaFlyout", script)
        self.assertIn("function closeExpandedNavClusters()", script)
        self.assertIn("closeExpandedNavClusters();", script)
        self.assertIn("cluster.classList.contains('open')", script)
        self.assertIn("saveNavClusterPreference(cluster, open)", script)
        self.assertIn("readNavClusterPreference(cluster)", script)
        self.assertIn("event.key === 'Escape'", script)
        self.assertIn("!cluster.contains(event.target)", script)
        self.assertIn("window.innerWidth <= 900", script)
        self.assertIn(':root[data-sidebar="collapsed"]', css)
        self.assertIn(".nav-cluster-flyout", css)
        self.assertIn("max-height: calc(100vh - 24px)", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn("scrollbar-width: none", css)
        self.assertIn(".nav::-webkit-scrollbar { width: 0; height: 0; display: none; }", css)
        self.assertIn("overscroll-behavior: contain", css)
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("--sidebar-w: 160px;", css)
        self.assertIn("--sidebar-collapsed-w: 72px;", css)
        self.assertIn(
            'padding-inline: 6px; scrollbar-gutter: auto;',
            css,
        )
        self.assertIn("transform: translateX(calc(-100% - 2px)); box-shadow: none;", css)
        self.assertIn(".sidebar.open { transform: translateX(0); box-shadow: var(--mobile-sidebar-shadow); }", css)

    def test_theme_controller_supports_auto_light_dark_and_system_changes(self):
        script = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("mediaflux.theme.mode", script)
        self.assertIn("const THEME_MODES = ['auto', 'light', 'dark']", script)
        self.assertIn("mediaflux:themechange", script)
        self.assertIn("prefers-color-scheme: dark", script)
        self.assertIn("addEventListener('change'", script)
        self.assertIn("addListener", script)
        self.assertIn("monitor-cog", script)
        self.assertIn("sun", script)
        self.assertIn("moon", script)
        self.assertIn("window.mediaFluxTheme", script)

    def test_light_theme_tokens_and_stable_toggle_contract(self):
        css = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css"
        ).read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="light"]', css)
        for token in (
            "--bg-base:", "--bg-sidebar:", "--bg-card:", "--bg-input:",
            "--border:", "--text-primary:", "--text-secondary:", "--accent:",
            "--on-accent:", "--grid-line:", "--sidebar-surface:",
        ):
            self.assertIn(token, css)
        self.assertIn(".theme-toggle {", css)
        self.assertIn("width: 36px", css)
        self.assertIn("height: 36px", css)
        self.assertIn(".sr-only {", css)
        self.assertIn("color-scheme: light", css)
        rss = (
            Path(__file__).resolve().parents[1] / "app" / "templates" / "rss.html"
        ).read_text(encoding="utf-8")
        self.assertIn('class="rss-sub-modal ', rss)
        self.assertIn(".rss-sub-modal {", css)
        self.assertNotIn("background:rgba(0,0,0,.6)", rss)
        self.assertNotIn("background:rgba(255,255,255,.05)", rss)

    def test_mediaflux_wordmark_and_login_composition_contract(self):
        login = self.client.get("/login")
        self._authenticated()
        settings = self.client.get("/settings")
        css = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css"
        ).read_text(encoding="utf-8")
        login_css = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "core-layout.css"
        ).read_text(encoding="utf-8")
        for html in (login.text, settings.text):
            self.assertIn("brand-wordmark-media", html)
            self.assertIn("brand-wordmark-cut", html)
            self.assertIn("brand-wordmark-flux", html)
        self.assertIn('class="login-card-container"', login.text)
        self.assertIn('class="login-title"', login.text)
        self.assertNotIn("<strong>MEDIA<br>AUTOMATION</strong>", login.text)
        for source in (css, login_css):
            self.assertIn(".brand-wordmark {", source)
            self.assertIn(".brand-wordmark-flux {", source)
        self.assertIn(".login-card-container {", login_css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", login_css)
        self.assertIn("pointer-events: none", login_css)
        self.assertIn("const motionDisabled = reducedMotion || saveData;", login.text)
        self.assertIn("const particleCount = Math.min(42", login.text)
        self.assertNotIn("const particleCount = 75", login.text)
        self.assertRegex(login.text, r"css/core-layout\.css\?v=2026081[0-9][a-z]")
        self.assertNotIn("@import url(", css)
        self.assertNotIn("@import url(", login_css)

    def test_latest_guangya_share_and_organize_ui_contracts(self):
        self._authenticated()
        guangya = self.client.get("/guangya")
        share = self.client.get("/guangya/more")
        organize = self.client.get("/organize")
        organize_rules = self.client.get("/organize-rules")
        self.assertNotIn("sensitive-phone-sentinel", guangya.text)
        self.assertIn('id="gyCapabilityText"', guangya.text)
        self.assertIn('data-key="GY_SHARE_TARGET_DIR"', share.text)
        self.assertIn('id="saveShareTargetBtn"', share.text)
        self.assertIn('id="organizeSourceDirs"', organize.text)
        self.assertIn('id="runOrganizeBtn"', organize.text)
        self.assertIn('id="stopOrganizeBtn"', organize.text)
        self.assertIn('id="cleanEmptyBtn"', organize.text)
        self.assertIn('value="10"', organize_rules.text)
        self.assertIn('id="r_strm_notify"', organize_rules.text)
        self.assertIn('id="collapseSidebar"', organize.text)
        self.assertIn('class="organize-flow-tabs"', organize_rules.text)
        self.assertNotIn('data-tab-target="source"', organize.text)
        self.assertIn('data-tab-panel="delivery"', organize_rules.text)
        self.assertNotIn('class="organize-tool-note"', organize.text)
        offline = self.client.get("/guangya/offline")
        strm = self.client.get("/guangya/strm")
        self.assertIn('class="offline-settings-grid"', offline.text)
        self.assertIn('class="settings-section offline-settings-section"', offline.text)
        self.assertIn('class="strm-settings-grid"', strm.text)
        self.assertIn('class="card settings-section strm-settings-section strm-source-section"', strm.text)
        stylesheet = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".standalone-settings { width: 100%; max-width: none; margin-inline: 0; }", stylesheet)
        self.assertIn(".strm-settings-grid { display: grid; grid-template-columns: minmax(0,1.2fr) minmax(0,0.8fr);", stylesheet)
        self.assertIn(".strm-source-section .form-row { display: grid; grid-template-columns: 170px minmax(0, 1fr); justify-content: start;", stylesheet)
        self.assertIn(".settings-section.strm-settings-section { min-width: 0; height: auto; margin: 0; padding: 20px;", stylesheet)
        self.assertIn(".settings-section.offline-settings-section { min-width: 0; height: 100%; margin: 0; padding: 22px;", stylesheet)
        self.assertIn('class="strm-section-head"', strm.text)
        self.assertIn('class="strm-runtime-actions"', strm.text)
        self.assertIn("@media (max-width: 1450px)", stylesheet)
        self.assertIn(".offline-settings-grid { grid-template-columns: 1fr; }", stylesheet)
        self.assertNotIn(".standalone-settings { width: min(1100px,100%); }", stylesheet)
        self.assertIn(".logs-summary { display: grid; grid-template-columns: repeat(4,minmax(0,1fr));", stylesheet)
        self.assertNotIn(".content { width: 100%; max-width: 1680px;", stylesheet)
        self.assertIn(':root[data-sidebar="collapsed"] .poster-grid', stylesheet)
        self.assertIn(':root[data-sidebar="collapsed"] .media-panel-added .poster-grid', stylesheet)

    def test_feature_pages_drop_command_banners_and_offline_outer_card(self):
        self._authenticated()
        offline = self.client.get("/guangya/offline")
        strm = self.client.get("/guangya/strm")
        proxy = self.client.get("/guangya/media-proxy")

        for response in (offline, strm, proxy):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('class="card card-pad feature-command', response.text)

        self.assertNotIn("GUANGYA OFFLINE RULES", offline.text)
        self.assertNotIn("GUANGYA STREAM INDEX", strm.text)
        self.assertNotIn("MEDIA REVERSE PROXY", proxy.text)
        self.assertIn(
            '<section class="standalone-settings offline-settings-shell" id="offlineForm">',
            offline.text,
        )
        self.assertNotIn(
            '<section class="card card-pad standalone-settings offline-settings-shell"',
            offline.text,
        )
        self.assertIn('class="offline-settings-grid"', offline.text)
        self.assertNotIn('id="offlineSelectionPanel"', offline.text)
        self.assertNotIn('id="offlinePreviewBtn"', offline.text)

        stylesheet = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".feature-command", stylesheet)
        self.assertNotIn(".proxy-command", stylesheet)

    def test_guangya_configuration_pages_share_persistent_settings_savebar(self):
        self._authenticated()
        offline = self.client.get("/guangya/offline")
        organize = self.client.get("/organize")
        strm = self.client.get("/guangya/strm")
        rules = self.client.get("/organize-rules")
        stylesheet = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )

        for response in (offline, organize, strm, rules):
            self.assertEqual(response.status_code, 200)
        for response in (offline, organize, strm, rules):
            self.assertIn("persistent-savebar-page", response.text)
            self.assertIn("workspace-savebar", response.text)
        self.assertIn('class="settings-savebar workspace-savebar"', offline.text)
        self.assertIn('class="organize-savebar settings-savebar workspace-savebar"', organize.text)
        self.assertIn('class="organize-savebar settings-savebar workspace-savebar"', rules.text)
        self.assertIn('class="strm-settings-savebar settings-savebar workspace-savebar"', strm.text)
        self.assertIn('class="btn btn-primary" id="saveOfflineBtn"', offline.text)
        self.assertIn('class="btn btn-primary" id="saveOrganizeConfigBtn"', organize.text)
        self.assertIn('class="btn btn-primary" id="saveStrmBtn"', strm.text)
        self.assertIn('class="btn btn-primary" id="saveOrganizeConfigBtn"', rules.text)
        self.assertIn(
            ".strm-runtime-card { display: grid; gap: 12px; margin-top: 16px; padding: 14px; background: var(--control-tint); border: 1px solid var(--border-soft);",
            stylesheet,
        )
        self.assertIn(".settings-savebar.workspace-savebar {", stylesheet)
        self.assertIn("position: fixed;", stylesheet)
        self.assertIn('left: var(--sidebar-w);', stylesheet)
        self.assertIn(':root[data-sidebar="collapsed"] .settings-savebar.workspace-savebar', stylesheet)
        self.assertIn(".persistent-savebar-page .content {", stylesheet)

    def test_global_confirm_and_alert_match_refined_control_surface(self):
        root = Path(__file__).resolve().parents[1] / "app"
        stylesheet = (root / "static" / "css" / "main.css").read_text(encoding="utf-8")
        script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
        template = (root / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn('class="card app-confirm-dialog" role="alertdialog"', template)
        self.assertIn('class="card app-message-dialog" role="alertdialog"', template)
        self.assertIn(".app-confirm-dialog,\n.app-message-dialog {", stylesheet)
        self.assertIn("width: min(460px, 100%);", stylesheet)
        self.assertIn("grid-template-columns: 48px minmax(0, 1fr);", stylesheet)
        self.assertIn("padding: 24px 24px 0;", stylesheet)
        self.assertIn("column-gap: 16px;", stylesheet)
        self.assertIn("--modal-radius: 18px;", stylesheet)
        self.assertIn("border-radius: var(--modal-radius);", stylesheet)
        self.assertIn("border-radius: var(--modal-action-radius);", stylesheet)
        self.assertIn("background: var(--footer-surface);", stylesheet)
        self.assertIn(".app-confirm-modal.is-danger .app-confirm-copy .server-kicker", stylesheet)
        self.assertIn(".app-message-modal.is-success .app-message-copy .server-kicker", stylesheet)
        self.assertIn(".app-confirm-actions button:focus-visible", stylesheet)
        self.assertIn("confirmModal.classList.toggle('is-danger', danger)", script)
        self.assertIn("confirmSubmit.classList.toggle('btn-danger', danger)", script)
        self.assertIn("messageModal.classList.add(`is-${type}`)", script)

    def test_rss_batch_mark_and_download_contracts(self):
        headers = self._authenticated()
        with patch("app.database.update_rss_entries_processed", return_value=2) as update:
            marked = self.client.post(
                "/api/rss/entries/mark",
                json={"entry_ids": [11, 12, 11], "processed": True},
                headers=headers,
            )
        self.assertEqual(marked.status_code, 200)
        self.assertEqual(marked.json()["updated"], 2)
        update.assert_called_once_with([11, 12], True)

        result = {
            "total": 2,
            "succeeded": [{"id": 11, "method": "qb"}],
            "failed": [{"id": 12, "error": "failed"}],
            "success_count": 1,
            "failure_count": 1,
        }
        with patch("app.modules.rss.RSSEngine.download_many", return_value=result) as download_many:
            downloaded = self.client.post(
                "/api/rss/entries/batch-download",
                json={"entry_ids": [11, 12, 11]},
                headers=headers,
            )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.json()["result"]["failure_count"], 1)
        download_many.assert_called_once_with([11, 12])

        with patch("app.database.count_download_logs", return_value=0) as count_logs, patch(
            "app.database.list_download_logs", return_value=[]
        ) as logs:
            searched = self.client.get("/api/downloads/logs?keyword=demo", headers=headers)
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.json()["page_size"], 20)
        self.assertEqual(searched.json()["items"], [])
        count_logs.assert_called_once_with(source=None, status=None, keyword="demo")
        logs.assert_called_once_with(
            source=None, status=None, keyword="demo", limit=20, offset=0
        )

        rss_page = self.client.get("/rss")
        self.assertIn('id="entryCheckAll"', rss_page.text)
        self.assertIn('id="batchDownloadBtn"', rss_page.text)
        self.assertIn('id="f_interval"', rss_page.text)
        self.assertIn('id="pickRssTargetBtn"', rss_page.text)
        self.assertIn('id="filterKeyword"', rss_page.text)
        self.assertIn('class="rss-entry-filters" aria-label="订阅条目筛选"', rss_page.text)

        downloads_page = self.client.get("/downloads")
        downloads_source = downloads_page.text + (Path(__file__).resolve().parents[1] / "app/static/js/downloads.js").read_text(encoding="utf-8")
        self.assertIn('id="downloadLogPagination"', downloads_page.text)
        self.assertIn('class="download-log-search"', downloads_page.text)
        self.assertIn('class="download-log-search-btn"', downloads_page.text)
        search_wrapper = downloads_page.text.index('class="download-log-search"')
        search_input = downloads_page.text.index('id="logKeyword"', search_wrapper)
        search_button = downloads_page.text.index('class="download-log-search-btn"', search_input)
        search_wrapper_end = downloads_page.text.index("</div>", search_button)
        self.assertLess(search_button, search_wrapper_end)
        self.assertIn("DOWNLOAD_LOG_PAGE_SIZE = 20", downloads_source)
        self.assertNotIn("renderLogs(d.logs||[])", downloads_source)

        stylesheet = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "main.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".rss-entry-head { display: grid; grid-template-columns: auto minmax(0,1fr);", stylesheet)
        self.assertIn(".rss-entry-filters { min-width: 0; display: grid; grid-template-columns: minmax(280px,1fr) 150px 150px auto;", stylesheet)
        self.assertIn(".download-log-search-btn { position: absolute;", stylesheet)

    def test_log_and_rss_retention_pagination_contracts(self):
        headers = self._authenticated()
        with patch("app.database.count_organize_logs", return_value=41) as count_logs, patch(
            "app.database.list_organize_logs", return_value=[]
        ) as list_logs:
            response = self.client.get(
                "/api/logs/organize?page=2&page_size=20&status=success&q=demo",
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 41)
        self.assertEqual(response.json()["pages"], 3)
        count_logs.assert_called_once_with(status="success", keyword="demo")
        list_logs.assert_called_once_with(
            status="success", keyword="demo", limit=20, offset=20
        )

        with patch("app.database.list_rss_entries", return_value=[]) as entries:
            searched = self.client.get("/api/rss/entries?q=episode", headers=headers)
        self.assertEqual(searched.status_code, 200)
        entries.assert_called_once_with(
            sub_id=None, status=None, keyword="episode", limit=300
        )

        from app.modules.rss_scheduler import RSSScheduler
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.db.purge_processed_rss_entries", return_value=4) as purge:
            self.assertEqual(scheduler._run_cleanup_if_due(), 4)
            self.assertEqual(scheduler._run_cleanup_if_due(), 0)
        purge.assert_called_once_with(retention_days=7)

    def test_rss_processed_retention_only_removes_old_processed_rows(self):
        original = db.DB_PATH
        with tempfile.TemporaryDirectory() as root:
            db.DB_PATH = Path(root) / "rss-retention.db"
            try:
                db.init_db()
                sub_id = db.add_rss_subscription("Demo", "https://example.com/rss")
                old_processed = db.add_rss_entry(sub_id, "Old processed", "old")
                recent_processed = db.add_rss_entry(sub_id, "Recent processed", "recent")
                old_pending = db.add_rss_entry(sub_id, "Old pending", "pending")
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE rss_entries SET status='downloaded',processed=1,processed_at='2026-07-01 00:00:00' WHERE id=?",
                        (old_processed,),
                    )
                    conn.execute(
                        "UPDATE rss_entries SET status='skipped',processed=1,processed_at=datetime('now') WHERE id=?",
                        (recent_processed,),
                    )
                    conn.execute(
                        "UPDATE rss_entries SET created_at='2026-07-01 00:00:00' WHERE id=?",
                        (old_pending,),
                    )
                self.assertEqual(db.purge_processed_rss_entries(7), 1)
                self.assertIsNone(db.get_rss_entry(old_processed))
                self.assertIsNotNone(db.get_rss_entry(recent_processed))
                self.assertIsNotNone(db.get_rss_entry(old_pending))
            finally:
                db.DB_PATH = original

    def test_rss_download_claim_prevents_duplicate_submission(self):
        entry = {
            "id": 7,
            "rss_item_id": 3,
            "title": "Episode 01",
            "status": "pending",
            "processed": 0,
            "payload": '{"torrent_url":"magnet:?xt=urn:btih:test"}',
            "download_method": "qb",
            "qb_save_path": "/downloads/anime",
            "gy_target_dir": "",
            "gy_target_dir_name": "",
        }
        engine = RSSEngine()
        with patch("app.database.get_rss_entry", return_value=entry), patch(
            "app.database.claim_rss_qb_download",
            side_effect=[
                {"status": "claimed", "lease_token": "lease-1"},
                {"status": "unavailable", "lease_token": ""},
            ],
        ) as claim, patch(
            "app.database.finalize_rss_qb_download", return_value=True
        ) as finalize, patch.object(
            engine, "_push_qb_detailed", return_value=TorrentAddResult(True)
        ) as push, patch(
            "app.database.update_rss_entry_status"
        ), patch("app.database.add_download_log"):
            first = engine.download(7)
            second = engine.download(7)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(claim.call_count, 2)
        finalize.assert_called_once()
        push.assert_called_once_with("magnet:?xt=urn:btih:test", save_path="/downloads/anime")

    def test_telegram_download_request_dedup_and_torrent_metadata(self):
        item = normalize_download_url("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Demo")
        self.assertEqual(item.title, "Demo")
        self.assertEqual(request_key(item), request_key(normalize_download_url(
            "magnet:?dn=Other&xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567"
        )))
        base32_hash = base64.b32encode(
            bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        ).decode("ascii")
        self.assertEqual(
            request_key(item),
            request_key(normalize_download_url(f"magnet:?xt=urn:btih:{base32_hash}")),
        )
        torrent = torrent_download_input("demo.torrent", b"d4:infod4:name4:Testee")
        self.assertEqual(torrent.title, "Test")
        self.assertTrue(torrent.source_value.startswith("magnet:?xt=urn:btih:"))
        self.assertEqual(
            request_key(torrent), request_key(normalize_download_url(torrent.source_value))
        )
        btmh = normalize_download_url(
            "magnet:?xt=urn:btmh:1220" + "12" * 32
        )
        colliding_prefix_btih = normalize_download_url(
            "magnet:?xt=urn:btih:" + "12" * 20
        )
        self.assertNotEqual(request_key(btmh), request_key(colliding_prefix_btih))

        with patch("app.database.create_download_request", side_effect=[(9, True), (9, False)]), patch(
            "app.database.get_download_request", return_value={"status": "pending"}
        ):
            first = create_request(item, "chat", "1")
            second = create_request(item, "chat", "2")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])

    def test_download_dispatcher_partial_success_and_claim(self):
        row = {
            "id": 31, "title": "Demo", "source_value": "magnet:?xt=urn:btih:abc",
            "kind": "magnet", "torrent_data": None,
        }
        with patch("app.database.get_download_request", return_value=row), patch(
            "app.database.claim_download_request", side_effect=[True, False]
        ), patch("app.modules.download_dispatcher._submit_qb", return_value={
            "ok": True, "task_id": "abc", "error": ""
        }), patch("app.modules.download_dispatcher._submit_guangya", return_value={
            "ok": False, "task_id": "", "error": "光鸭未登录", "decision": {}
        }), patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.database.add_download_log"
        ) as add_log:
            first = dispatch_request(31, "both")
            second = dispatch_request(31, "both")
        self.assertTrue(first["ok"])
        self.assertEqual(first["succeeded"], ["qb"])
        self.assertEqual(first["failed"], ["guangya"])
        self.assertFalse(second["ok"])
        self.assertTrue(second["duplicate"])
        update.assert_called_once()
        self.assertEqual(add_log.call_count, 2)

    def test_download_dispatcher_contains_backend_exception(self):
        row = {
            "id": 32, "title": "Demo", "source_value": "magnet:?xt=urn:btih:def",
            "kind": "magnet", "torrent_data": None,
        }
        with patch("app.database.get_download_request", return_value=row), patch(
            "app.database.claim_download_request", return_value=True
        ), patch("app.modules.download_dispatcher._submit_qb", side_effect=RuntimeError("qB unavailable")), patch(
            "app.modules.download_dispatcher._submit_guangya", return_value={
                "ok": True, "task_id": "", "error": "",
                "decision": {"target_dir_id": "source", "target_dir_name": "下载目录"},
            }
        ), patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.database.add_download_log"
        ) as add_log:
            result = dispatch_request(32, "both")
        self.assertTrue(result["ok"])
        self.assertEqual(result["succeeded"], ["guangya"])
        self.assertEqual(result["failed"], ["qb"])
        self.assertIn("qB unavailable", result["error"])
        self.assertEqual(update.call_args.kwargs["status"], "submitted")
        self.assertEqual(update.call_args.kwargs["qb_status"], "failed")
        self.assertEqual(add_log.call_count, 2)

    def test_download_tracker_marks_qb_error_state_failed(self):
        tracker = DownloadTracker()
        row = {
            "id": 41, "title": "Broken", "status": "submitted",
            "qb_status": "submitted", "gy_status": "", "qb_task_id": "hash-1",
            "gy_task_id": "", "source_value": "magnet:?xt=urn:btih:hash-1",
            "organize_started": 0, "chat_id": "chat",
        }
        task = SimpleNamespace(hash="hash-1", name="Broken", progress=0.2, state="error")
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.database.update_download_request"
        ), patch.object(
            tracker, "_update_backend_log"
        ) as update_log, patch(
            "app.database.claim_download_request_notification",
            return_value={"token": "notice-41", "attempts": 0},
        ), patch(
            "app.database.finalize_download_request_notification",
            return_value=True,
        ), patch("app.modules.download_tracker.send") as notify:
            tracker._update_request(row, [task], [])
        self.assertEqual(update.call_args.kwargs["qb_status"], "failed")
        self.assertEqual(update.call_args.kwargs["status"], "failed")
        update_log.assert_called_once_with(41, "qb", "failed", 0.2, "hash-1")
        notify.assert_called_once()

    def test_download_tracker_starts_organize_after_guangya_completion(self):
        tracker = DownloadTracker()
        row = {
            "id": 42, "title": "Ready", "status": "downloading",
            "qb_status": "", "gy_status": "downloading", "qb_task_id": "",
            "gy_task_id": "gy-1", "source_value": "magnet:?xt=urn:btih:ready",
            "organize_started": 0, "chat_id": "chat", "gy_target_dir": "source",
            "gy_target_name": "下载目录",
        }
        task = {"id": "gy-1", "name": "Ready", "progress": 1.0, "status": "completed"}
        rules = OrganizeRules(target_dir_id="target")
        manager = Mock()
        manager.start.return_value = {"ok": True, "task_id": "organize-1", "run_id": 17}
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.database.update_download_request"
        ), patch.object(
            tracker, "_update_backend_log"
        ), patch(
            "app.database.claim_download_request_organize", return_value=True
        ), patch("app.modules.download_tracker.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "target",
        }.get(key, default)), patch(
            "app.modules.download_tracker.OrganizeRules.from_config", return_value=rules
        ), patch("app.modules.download_tracker.get_organize_manager", return_value=manager), patch(
            "app.database.claim_download_request_notification",
            return_value={"token": "notice-42", "attempts": 0},
        ), patch(
            "app.database.finalize_download_request_notification",
            return_value=True,
        ), patch(
            "app.modules.download_tracker.send"
        ):
            tracker._update_request(row, [], [task])
        manager.start.assert_called_once_with(
            [{"id": "source", "name": "下载目录"}], rules,
            trigger_type="download", download_request_ids=[42],
        )
        self.assertTrue(any(call.kwargs.get("status") == "completed" for call in update.call_args_list))

    def test_download_tracker_queues_completed_download_when_organizer_is_busy(self):
        tracker = DownloadTracker()
        row = {
            "id": 43, "title": "Queued", "chat_id": "chat",
            "gy_target_dir": "isolated-source", "gy_target_name": "任务隔离目录",
            "organize_status": "",
        }
        manager = Mock()
        manager.start.return_value = {"ok": False, "error": "网盘整理任务正在运行"}
        rules = OrganizeRules(target_dir_id="target")
        with patch("app.modules.download_tracker.get", side_effect=lambda key, default="": {
            "GY_ORGANIZE_TARGET_DIR": "target",
        }.get(key, default)), patch(
            "app.modules.download_tracker.OrganizeRules.from_config", return_value=rules
        ), patch(
            "app.modules.download_tracker.get_organize_manager", return_value=manager
        ), patch(
            "app.database.claim_download_request_organize", return_value=True
        ), patch("app.database.update_download_request") as update, patch(
            "app.modules.download_tracker.send"
        ) as notify:
            tracker._start_organize(row)

        update.assert_called_once_with(
            43, organize_started=0, organize_status="queued", organize_error="",
            strm_status="pending", strm_error="",
        )
        notify.assert_called_once()

    def test_guangya_loads_refresh_token_and_keeps_directory_id_as_string(self):
        from app.clients.guangya import GuangYaClient

        raw = Mock()
        raw.token = "access"
        raw.refresh_token_value = "refresh"
        raw.device_id = "device-1"
        raw.token_expires_at = None
        raw.cloud_create_task.return_value = {"msg": "success"}
        raw_class = Mock(return_value=raw)
        with tempfile.TemporaryDirectory() as root:
            token_file = Path(root) / "token.json"
            token_file.write_text(
                '{"access_token":"access","refresh_token":"refresh","device_id":"device-1","expires_at":1893456000}',
                encoding="utf-8",
            )
            with patch("app.clients.guangya._load_raw", return_value=raw_class):
                client = GuangYaClient(token_file=token_file)
                client.add_offline_task("magnet:?xt=urn:btih:test", "1927445875113771071")
        raw_class.assert_called_once_with(
            access_token="access", refresh_token="refresh", device_id="device-1"
        )
        raw.cloud_create_task.assert_called_once_with(
            url="magnet:?xt=urn:btih:test", parent_id="1927445875113771071"
        )

    def test_guangya_refresh_persists_rotated_tokens_and_device(self):
        from app.clients.guangya import GuangYaClient

        class Raw:
            def __init__(self, **kwargs):
                self.token = kwargs.get("access_token") or ""
                self.refresh_token_value = kwargs.get("refresh_token") or ""
                self.device_id = kwargs.get("device_id") or "generated"
                self.token_expires_at = 1
                self._client = SimpleNamespace(headers={})

            def refresh_token(self, _refresh_token=None):
                self.token = "new-access"
                self.refresh_token_value = "new-refresh"
                self.token_expires_at = time.time() + 3600
                return {"access_token": self.token}

        with tempfile.TemporaryDirectory() as root:
            token_file = Path(root) / "token.json"
            token_file.write_text(
                '{"access_token":"old","refresh_token":"refresh","device_id":"stable-device","expires_at":1}',
                encoding="utf-8",
            )
            with patch("app.clients.guangya._load_raw", return_value=Raw):
                client = GuangYaClient(token_file=token_file)
                client.raw
            saved = __import__("json").loads(token_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["device_id"], "stable-device")
        self.assertGreater(float(saved["expires_at"]), time.time())

    def test_guangya_share_pagination_deduplicates_and_hides_token(self):
        from app.clients.guangya import GuangYaClient

        raw_class = Mock()
        raw_class.share_access_token.return_value = {"data": {"accessToken": "secret-token"}}
        raw_class.share_files_list.side_effect = [
            {"data": {"list": [
                {"fileId": "a", "fileName": "A", "resType": 1, "fileSize": 1},
                {"fileId": "b", "fileName": "B", "resType": 1, "fileSize": 2},
            ]}},
            {"data": {"list": [
                {"fileId": "b", "fileName": "B", "resType": 1, "fileSize": 2},
                {"fileId": "c", "fileName": "C", "resType": 2, "fileSize": 0},
            ]}},
        ]
        with patch("app.clients.guangya._load_raw", return_value=raw_class):
            result = GuangYaClient().list_share_files(
                "https://www.guangyapan.com/s/demo", page_size=2, max_pages=2
            )
        self.assertEqual([item["id"] for item in result["files"]], ["a", "b", "c"])
        self.assertNotIn("access_token", result)
        self.assertNotIn("_access_token", result)
        self.assertEqual(raw_class.share_files_list.call_count, 2)

    def test_config_endpoint_accepts_share_and_organize_subset(self):
        headers = self._authenticated()
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={
                    "GY_SHARE_TARGET_DIR": "share-target",
                    "GY_SHARE_TARGET_DIR_NAME": "转存目录",
                    "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"source-1","name":"源一"}]',
                    "GY_ORGANIZE_SMALL_FILE_MB": "10",
                    "GY_ORGANIZE_STRM_DETAIL_NOTIFY": "1",
                },
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        save.assert_called_once()

    def test_config_endpoint_resets_notifier_after_telegram_change(self):
        headers = self._authenticated()
        with patch("app.routes.api.config.set_and_save"), patch("app.notifier.reset") as reset:
            response = self.client.post(
                "/api/config", json={"TG_CHAT_ID": "10002"}, headers=headers
            )
        self.assertEqual(response.status_code, 200)
        reset.assert_called_once_with()

    def test_config_endpoint_accepts_media_server_subset(self):
        headers = self._authenticated()
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={
                    "JELLYFIN_ENABLED": "1",
                    "JELLYFIN_URL": "http://jellyfin.local",
                    "JELLYFIN_API_KEY": "********",
                    "JELLYFIN_USER_ID": "user-guid-1",
                },
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with({
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://jellyfin.local",
            "JELLYFIN_USER_ID": "user-guid-1",
        })

    def test_config_endpoint_rejects_unsafe_media_user_id(self):
        headers = self._authenticated()
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={"EMBY_USER_ID": "../admin?token=secret"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("共享用户 ID 无效", response.json()["error"])
        save.assert_not_called()

    def test_config_endpoint_maps_concurrent_save_to_single_line_conflict(self):
        headers = self._authenticated()
        with patch(
            "app.routes.api.config.set_and_save",
            side_effect=config.ConcurrentConfigUpdateError("secret conflict detail"),
        ), self.assertLogs("app.routes.api", level="WARNING") as captured:
            response = self.client.post(
                "/api/config",
                json={"TG_CHAT_ID": "87654321"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"error": "配置已被其他操作修改，请刷新页面后重试"},
        )
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(captured.records[0].exc_info)
        self.assertNotIn("secret conflict detail", "\n".join(captured.output))

    def test_config_endpoint_maps_mount_permission_error_to_safe_503(self):
        headers = self._authenticated()
        failure = PermissionError(errno.EPERM, "SECRET-TOKEN operation not permitted")
        with patch(
            "app.routes.api.config.set_and_save",
            side_effect=failure,
        ), self.assertLogs("app.routes.api", level="ERROR") as captured:
            response = self.client.post(
                "/api/config",
                json={"TG_CHAT_ID": "87654322"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"error": "配置暂时无法保存，请检查数据目录权限或存储状态后重试"},
        )
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(captured.records[0].exc_info)
        rendered = "\n".join(captured.output)
        self.assertIn("reason=permission_denied", rendered)
        self.assertIn("errno=1", rendered)
        self.assertNotIn("SECRET-TOKEN", rendered)

    def test_config_endpoint_exposes_runtime_strm_default_without_persisting_it(self):
        headers = self._authenticated()
        with patch("app.routes.api.config.all_items", return_value={}), patch(
            "app.routes.api.config.has_external_override", return_value=False
        ), patch("app.routes.api.config.get", return_value="/data/strm") as get_value:
            response = self.client.get("/api/config", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["STRM_ROOT"], "/data/strm")
        get_value.assert_called_once_with("STRM_ROOT", "")

    def test_sensitive_config_is_masked_and_mask_is_not_saved(self):
        headers = self._authenticated()
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        if response.json().get("TMDB_API_KEY"):
            self.assertEqual(response.json()["TMDB_API_KEY"], "********")
        with patch("app.routes.api.config.set_and_save") as save:
            response = self.client.post(
                "/api/config",
                json={"TMDB_API_KEY": "********"},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        # 掩码表示保留旧值；无真实变更时不执行空写盘。
        save.assert_not_called()


def __getattr__(name: str):
    """兼容探索实施计划中的显式 TMDBScraper 回归入口。"""
    if name == "TMDBScraperTests":
        from tests.test_discovery_providers import TMDBScraperCompatibilityTests
        return TMDBScraperCompatibilityTests
    raise AttributeError(name)


if __name__ == "__main__":
    unittest.main()
