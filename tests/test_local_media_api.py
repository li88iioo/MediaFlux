"""本地媒体 API 鉴权、CSRF、来源配置和薄路由测试。"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class LocalMediaAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
        self._path_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._path_tmp.cleanup)
        self.local_root = Path(self._path_tmp.name) / "downloads"
        self.movie_target = Path(self._path_tmp.name) / "media" / "Movies"
        self.default_target = Path(self._path_tmp.name) / "media"
        self.local_root.mkdir(parents=True)
        self.movie_target.mkdir(parents=True)
        self.client = TestClient(create_app(start_background=False))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login", data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return self._token(self.client.get("/settings").text)

    def test_get_requires_login_and_post_requires_csrf(self):
        self.assertEqual(self.client.get("/api/local-media/sources").status_code, 401)
        self.assertEqual(self.client.get("/api/local-media/items").status_code, 401)
        self.login()
        response = self.client.post("/api/local-media/sources", json={})
        self.assertEqual(response.status_code, 403)
        deleted = self.client.post("/api/local-media/items/delete", json={})
        self.assertEqual(deleted.status_code, 403)

    def test_source_crud_and_category_targets(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        payload = {
            "name": "qB 下载目录", "qb_profile": "configured:qb",
            "qb_path_prefix": "/downloads", "local_root": str(self.local_root),
            # 旧客户端提交的 SMB 字段应被忽略，不再写入或通过 API 暴露。
            "smb_user": "nasadmin", "smb_pass": "secret123",
            "enabled": True, "stable_seconds": 60, "scan_enabled": True,
            "scan_interval_minutes": 10, "media_type": "movie", "mode": "move",
            "targets": [{
                "category": "movie", "path": str(self.movie_target),
                "provider": "jellyfin", "library_id": "movies", "library_name": "电影",
                "server_path": "//NAS/Video/Movies",
            }],
        }
        with patch("app.routes.local_media_api.get_local_media_scheduler") as scheduler:
            created = self.client.post("/api/local-media/sources", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        source_id = created.json()["id"]
        self.assertEqual(created.json()["targets"][0]["category"], "movie")
        self.assertEqual(created.json()["media_type"], "movie")
        self.assertEqual(created.json()["targets"][0]["provider"], "jellyfin")
        self.assertEqual(created.json()["targets"][0]["library_id"], "movies")
        self.assertEqual(created.json()["targets"][0]["library_name"], "电影")
        self.assertEqual(created.json()["targets"][0]["server_path"], "//NAS/Video/Movies")
        self.assertNotIn("smb_user", created.json())
        self.assertNotIn("has_smb_pass", created.json())
        self.assertNotIn("secret123", created.text)
        scheduler.return_value.reload.assert_called_once()
        stored_source = db.get_local_media_source(source_id, owner="admin")
        self.assertEqual((stored_source.smb_user, stored_source.smb_pass), ("", ""))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_sources SET smb_user=?,smb_pass=? WHERE id=?",
                ("legacy-user", "legacy-pass", source_id),
            )

        preserved = self.client.put(
            f"/api/local-media/sources/{source_id}",
            json={"name": "只改名称"}, headers=headers,
        )
        self.assertEqual(preserved.status_code, 200, preserved.text)
        self.assertEqual(
            preserved.json()["targets"][0]["server_path"], "//NAS/Video/Movies",
        )

        payload["name"] = "主下载目录"
        payload["targets"] = [{
            "category": "default", "path": str(self.default_target),
            "provider": "emby", "library_id": "library-1", "library_name": "媒体库",
            "server_path": "D:/Media",
        }]
        updated = self.client.put(
            f"/api/local-media/sources/{source_id}", json=payload, headers=headers
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "主下载目录")
        self.assertEqual([item["category"] for item in updated.json()["targets"]], ["default"])
        self.assertEqual(updated.json()["targets"][0]["server_path"], "D:/Media")
        self.assertNotIn("smb_user", updated.json())
        self.assertNotIn("has_smb_pass", updated.json())
        stored_source = db.get_local_media_source(source_id, owner="admin")
        self.assertEqual((stored_source.smb_user, stored_source.smb_pass), ("", ""))

        deleted = self.client.delete(f"/api/local-media/sources/{source_id}", headers=headers)
        self.assertEqual(deleted.json(), {"deleted": True})

    def test_source_paths_require_container_paths_but_qb_prefix_may_be_windows(self):
        token = self.login()
        headers = {"X-CSRF-Token": token}
        base_payload = {
            "name": "跨平台 qB 来源",
            "qb_path_prefix": r"W:\Downloads",
            "targets": [{"category": "movie", "path": str(self.movie_target)}],
        }
        for invalid_root in (r"W:\Downloads", r"\\NAS\Downloads"):
            with self.subTest(local_root=invalid_root):
                response = self.client.post(
                    "/api/local-media/sources",
                    json={**base_payload, "local_root": invalid_root},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("Docker 容器内路径", response.json()["error"])

        with patch("app.routes.local_media_api.get_local_media_scheduler"):
            created = self.client.post(
                "/api/local-media/sources",
                json={**base_payload, "local_root": str(self.local_root)},
                headers=headers,
            )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["qb_path_prefix"], r"W:\Downloads")


    def test_local_directory_browser_is_authenticated_and_source_scoped(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); child = root / "Movies"; child.mkdir(); (root / "file.txt").write_text("x")
            self.assertEqual(self.client.get("/api/local-media/directories").status_code, 401)
            self.login()
            with patch("app.modules.local_directory_browser.get", return_value=str(root)):
                listing = self.client.get("/api/local-media/directories", params={"path": "__roots__"})
                nested = self.client.get("/api/local-media/directories", params={"path": str(root)})
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(listing.json()["directories"][0]["path"], str(root))
            self.assertIs(listing.json()["directories"][0]["is_dir"], True)
            self.assertEqual([item["name"] for item in nested.json()["directories"]], ["Movies"])
            self.assertIs(nested.json()["directories"][0]["is_dir"], True)

            with patch("app.modules.local_directory_browser.get", return_value=""), patch(
                "app.modules.local_directory_browser._platform_roots", return_value=[root]
            ):
                fallback = self.client.get(
                    "/api/local-media/directories", params={"path": "__roots__"}
                )
            self.assertEqual(fallback.status_code, 200, fallback.text)
            self.assertEqual(fallback.json()["directories"], [
                {"id": str(root), "name": root.name, "path": str(root), "is_dir": True}
            ])

            source_id = db.create_local_media_source(
                name="scoped", qb_profile="", qb_path_prefix="", local_root=str(root), owner="admin"
            )
            escaped = self.client.get(
                "/api/local-media/directories", params={"source_id": source_id, "path": str(root.parent)}
            )
            self.assertEqual(escaped.status_code, 400)

    def test_browse_root_configuration_ignores_relative_windows_and_unc_paths(self):
        from app.modules.local_directory_browser import _configured_roots

        with tempfile.TemporaryDirectory() as root_raw, patch(
            "app.modules.local_directory_browser.get",
            return_value=f"app,{root_raw},C:\\Media,\\\\NAS\\Media",
        ):
            self.assertEqual(_configured_roots(), [Path(root_raw).resolve()])

    def test_root_container_default_browser_exposes_mounts_not_container_root(self):
        from app.modules.local_directory_browser import _configured_roots

        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
            tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as mountinfo,
        ):
            first = Path(first_raw).resolve()
            second = Path(second_raw).resolve()
            mountinfo.write(
                "20 1 0:1 / / rw - overlay overlay rw\n"
                f"21 20 0:2 / {first} rw - ext4 /dev/a rw\n"
                f"22 20 0:3 / {second} rw - ext4 /dev/b rw\n"
                "23 20 0:4 / /proc rw - proc proc rw\n"
                "24 20 0:5 / /tmp rw - tmpfs tmpfs rw\n"
            )
            mountinfo.flush()
            with patch.dict("os.environ", {"MEDIAFLUX_CONTAINER": "1"}), patch(
                "app.modules.local_directory_browser.get", return_value=""
            ), patch(
                "app.modules.local_directory_browser._MOUNTINFO_PATH", Path(mountinfo.name)
            ):
                roots = _configured_roots()

        self.assertEqual(
            roots,
            sorted([first, second], key=lambda item: str(item).casefold()),
        )
        self.assertNotIn(Path("/"), roots)

    def test_legacy_source_edit_requires_container_path_migration(self):
        token = self.login()
        headers = {"X-CSRF-Token": token}
        source_id = db.create_local_media_source(
            name="旧 UNC 来源", qb_profile="", qb_path_prefix=r"\\NAS\Downloads",
            local_root=r"\\NAS\Downloads", owner="admin",
        )
        response = self.client.put(
            f"/api/local-media/sources/{source_id}",
            json={"name": "旧 UNC 来源"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Docker 容器内路径", response.json()["error"])

    def test_items_and_inspect_reject_relative_paths_even_when_source_is_cwd(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        source_id = db.create_local_media_source(
            name="cwd", qb_profile="", qb_path_prefix="", local_root=str(Path.cwd()),
            owner="admin",
        )

        items = self.client.get(
            "/api/local-media/items", params={"source_id": source_id, "path": "."},
        )
        inspected = self.client.post(
            "/api/local-media/inspect",
            json={"source_id": source_id, "path": "."},
            headers=headers,
        )

        for response in (items, inspected):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Docker 容器内绝对路径", response.json()["error"])

    def test_items_and_inspect_explain_legacy_source_migration(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        for legacy_root in (r"D:\Downloads", r"\\NAS\Downloads"):
            with self.subTest(legacy_root=legacy_root):
                source_id = db.create_local_media_source(
                    name="legacy", qb_profile="", qb_path_prefix=legacy_root,
                    local_root=legacy_root, owner="admin",
                )
                items = self.client.get(
                    "/api/local-media/items", params={"source_id": source_id},
                )
                inspected = self.client.post(
                    "/api/local-media/inspect",
                    json={"source_id": source_id},
                    headers=headers,
                )
                for response in (items, inspected):
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("Windows/UNC", response.json()["error"])
                    self.assertIn("Docker 容器路径", response.json()["error"])
                db.delete_local_media_source(source_id, owner="admin")

    def test_legacy_unc_directory_browser_request_has_docker_mount_guidance(self):
        self.login()
        responses = [
            self.client.get(
                "/api/local-media/directories", params={"path": r"\\NAS\Media"},
            ),
            self.client.get(
                "/api/local-media/directories",
                params={"path": "__roots__", "network_root": r"\\NAS\Media"},
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Windows/UNC", response.json()["error"])
            self.assertIn("容器路径", response.json()["error"])

    def test_media_server_library_endpoints_only_expose_enabled_profiles(self):
        self.login()
        profile = SimpleNamespace(
            server_type="jellyfin", label="Jellyfin", url="http://jellyfin",
            credential="secret", enabled=True, configured=True,
        )
        client = Mock()
        client.list_virtual_folders.return_value = [{
            "id": "movies", "name": "电影", "locations": ["/media/Movies"],
            "collection_type": "movies",
        }]
        with patch("app.routes.local_media_api.list_configured_profiles", return_value=[profile]), patch(
            "app.clients.jellyfin.JellyfinClient", return_value=client
        ):
            servers = self.client.get("/api/local-media/media-servers")
            libraries = self.client.get("/api/local-media/media-servers/jellyfin/libraries")
        self.assertEqual(servers.json(), {"servers": [{"provider": "jellyfin", "label": "Jellyfin"}]})
        self.assertEqual(libraries.json()["libraries"][0]["id"], "movies")
        self.assertNotIn("secret", servers.text + libraries.text)

    def test_invalid_path_and_category_are_rejected_without_db_write(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        response = self.client.post(
            "/api/local-media/sources",
            json={
                "name": "bad", "local_root": "/tmp/bad\u0000path",
                "targets": [{"category": "music", "path": "/tmp/music"}],
            }, headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.list_local_media_sources(owner="admin"), [])

        response = self.client.post(
            "/api/local-media/sources",
            json={
                "name": "bad category", "local_root": "/tmp/source",
                "targets": [{"category": "music", "path": "/tmp/music"}],
            }, headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.list_local_media_sources(owner="admin"), [])

    def test_source_paths_reject_overlap_and_symlink_roots_without_db_write(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        nested_target = self.local_root / "Movies"
        nested_target.mkdir()
        payload = {
            "name": "overlap", "local_root": str(self.local_root),
            "targets": [{"category": "movie", "path": str(nested_target)}],
        }
        overlap = self.client.post("/api/local-media/sources", json=payload, headers=headers)
        self.assertEqual(overlap.status_code, 400, overlap.text)

        symlink_root = Path(self._path_tmp.name) / "downloads-link"
        created_symlink = False
        try:
            symlink_root.symlink_to(self.local_root, target_is_directory=True)
            created_symlink = symlink_root.is_symlink() or getattr(symlink_root, "is_junction", lambda: False)()
        except OSError:
            pass
        if created_symlink:
            payload["name"] = "symlink"
            payload["local_root"] = str(symlink_root)
            payload["targets"] = [{"category": "movie", "path": str(self.movie_target)}]
            symlink = self.client.post("/api/local-media/sources", json=payload, headers=headers)
            self.assertEqual(symlink.status_code, 400, symlink.text)
        else:
            with patch("app.routes.local_media_api._validate_source_paths", side_effect=ValueError("允许根目录不能是符号链接")):
                payload["name"] = "symlink"
                payload["local_root"] = str(symlink_root)
                payload["targets"] = [{"category": "movie", "path": str(self.movie_target)}]
                symlink = self.client.post("/api/local-media/sources", json=payload, headers=headers)
                self.assertEqual(symlink.status_code, 400, symlink.text)
        self.assertEqual(db.list_local_media_sources(owner="admin"), [])


    def test_media_items_list_configured_source_entries_and_soft_delete(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        movie_dir = self.local_root / "Movie.2026"
        movie_dir.mkdir()
        (movie_dir / "Movie.2026.mkv").write_bytes(b"movie")
        loose_video = self.local_root / "Loose.Movie.2026.mkv"
        loose_video.write_bytes(b"loose")
        (self.local_root / "readme.txt").write_text("not media")
        non_media_dir = self.local_root / "Documents"
        non_media_dir.mkdir()
        (non_media_dir / "archive.zip").write_bytes(b"archive")
        trash = self.local_root / ".mediaflux-trash"
        trash.mkdir()
        (trash / "ignored").mkdir()
        for ignored_name in ("@eaDir", "temp", "TMP", "#recycle"):
            ignored = self.local_root / ignored_name
            ignored.mkdir()
            (ignored / "Ignored.mkv").write_bytes(b"ignored")
        source_id = db.create_local_media_source(
            name="本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        db.upsert_local_library_target(
            source_id, "default", str(self.default_target), owner="admin",
        )

        listed = self.client.get("/api/local-media/items")
        self.assertEqual(listed.status_code, 200, listed.text)
        items = listed.json()["items"]
        self.assertEqual([item["name"] for item in items], ["Loose.Movie.2026.mkv", "Movie.2026"])
        self.assertTrue(all(item["organize_ready"] for item in items))
        self.assertEqual({item["kind"] for item in items}, {"directory", "video"})
        self.assertNotIn("readme.txt", listed.text)
        self.assertNotIn("Documents", listed.text)
        self.assertNotIn("archive.zip", listed.text)
        self.assertNotIn(".mediaflux-trash", listed.text)
        for ignored_name in ("@eaDir", "temp", "TMP", "#recycle"):
            self.assertNotIn(ignored_name, listed.text)

        target = next(item for item in items if item["name"] == "Movie.2026")
        deleted = self.client.post(
            "/api/local-media/items/delete",
            json={"source_id": source_id, "path": target["path"], "identity": target["identity"]},
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["recoverable"])
        self.assertFalse(movie_dir.exists())
        self.assertEqual(len(list(trash.iterdir())), 2)
        refreshed_names = [
            item["name"] for item in self.client.get("/api/local-media/items").json()["items"]
        ]
        self.assertEqual(refreshed_names, ["Loose.Movie.2026.mkv"])

    def test_media_items_can_browse_nested_directories_with_safe_navigation_metadata(self):
        self.login()
        show = self.local_root / "Show"
        season = show / "Season 01"
        ignored = show / "@eaDir"
        season.mkdir(parents=True)
        ignored.mkdir()
        episode = season / "Show.S01E01.mkv"
        episode.write_bytes(b"episode")
        (show / "Show.Special.mkv").write_bytes(b"special")
        (ignored / "Ignored.mkv").write_bytes(b"ignored")
        source_id = db.create_local_media_source(
            name="本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        db.upsert_local_library_target(
            source_id, "default", str(self.default_target), owner="admin",
        )

        root = self.client.get("/api/local-media/items", params={"source_id": source_id})
        self.assertEqual(root.status_code, 200, root.text)
        root_data = root.json()
        self.assertEqual(root_data["browse"]["current_path"], str(self.local_root))
        self.assertEqual(root_data["browse"]["parent_path"], "")
        self.assertEqual(root_data["browse"]["breadcrumbs"], [
            {"name": "本地下载", "path": str(self.local_root)},
        ])
        root_show = next(item for item in root_data["items"] if item["name"] == "Show")
        self.assertTrue(root_show["deletable"])
        self.assertEqual(root_show["relative_path"], "Show")

        nested = self.client.get(
            "/api/local-media/items",
            params={"source_id": source_id, "path": str(show)},
        )
        self.assertEqual(nested.status_code, 200, nested.text)
        nested_data = nested.json()
        self.assertEqual(
            [item["name"] for item in nested_data["items"]],
            ["Season 01", "Show.Special.mkv"],
        )
        self.assertTrue(all(not item["deletable"] for item in nested_data["items"]))
        self.assertNotIn("@eaDir", nested.text)
        self.assertEqual(nested_data["browse"]["parent_path"], str(self.local_root))
        self.assertEqual(
            [crumb["name"] for crumb in nested_data["browse"]["breadcrumbs"]],
            ["本地下载", "Show"],
        )

        season_result = self.client.get(
            "/api/local-media/items",
            params={"source_id": source_id, "path": str(season)},
        )
        self.assertEqual(season_result.status_code, 200, season_result.text)
        season_item = season_result.json()["items"][0]
        self.assertEqual(season_item["path"], str(episode))
        self.assertEqual(season_item["relative_path"], "Show/Season 01/Show.S01E01.mkv")
        self.assertFalse(season_item["deletable"])

        missing_source = self.client.get(
            "/api/local-media/items", params={"path": str(show)},
        )
        self.assertEqual(missing_source.status_code, 400, missing_source.text)
        ignored_path = self.client.get(
            "/api/local-media/items",
            params={"source_id": source_id, "path": str(ignored)},
        )
        self.assertEqual(ignored_path.status_code, 400, ignored_path.text)
        outside = self.client.get(
            "/api/local-media/items",
            params={"source_id": source_id, "path": str(self.default_target)},
        )
        self.assertEqual(outside.status_code, 400, outside.text)

    def test_media_item_delete_rejects_changed_snapshot_and_nested_path(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        show = self.local_root / "Show"
        show.mkdir()
        episode = show / "S01E01.mkv"
        episode.write_bytes(b"episode")
        source_id = db.create_local_media_source(
            name="本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        item = self.client.get("/api/local-media/items").json()["items"][0]
        changed = dict(item["identity"]); changed["mtime_ns"] += 1
        rejected = self.client.post(
            "/api/local-media/items/delete",
            json={"source_id": source_id, "path": item["path"], "identity": changed},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertTrue(show.exists())
        nested = self.client.post(
            "/api/local-media/items/delete",
            json={"source_id": source_id, "path": str(episode), "identity": item["identity"]},
            headers=headers,
        )
        self.assertEqual(nested.status_code, 400, nested.text)
        self.assertTrue(episode.exists())

    def test_scan_existing_media_starts_scheduler_and_enqueues_all_sources(self):
        csrf = self.login()
        scheduler = Mock()
        scheduler.status.return_value = {"running": False}
        scheduler.enqueue_manual_scan_candidates.return_value = {
            "ok": True,
            "source_count": 2,
            "scanned_sources": 1,
            "candidate_count": 3,
            "queued_count": 3,
            "task_ids": [10, 11, 12],
            "sources": [],
        }
        with patch(
            "app.routes.local_media_api.get_local_media_scheduler",
            return_value=scheduler,
        ):
            response = self.client.post(
                "/api/local-media/scan",
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["queued_count"], 3)
        scheduler.start.assert_called_once_with()
        scheduler.enqueue_manual_scan_candidates.assert_called_once_with(silent=True)

    def test_requires_manual_directory_task_displays_the_specific_failed_episode(self):
        self.login()
        source_id = db.create_local_media_source(
            name="本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        show = self.local_root / "Show (2026)"
        show.mkdir()
        task_id = db.create_local_media_task(
            source_id, "", str(show), owner="admin", trigger="scan",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="requires_manual",
            error="剧集文件缺少集数，不能自动归档: Show.S01.mkv",
        )

        response = self.client.get("/api/local-media/tasks?status=requires_manual")

        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["tasks"][0]
        self.assertEqual(task["content_name"], "Show (2026)")
        self.assertEqual(task["display_name"], "Show.S01.mkv")

    def test_requires_manual_task_inspection_delegates_to_service(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        service = Mock()
        service.inspect_task.return_value = {"inspection_id": "inspect-task-1", "video_count": 2}
        with patch("app.routes.local_media_api.get_local_media_service", return_value=service):
            response = self.client.post("/api/local-media/tasks/88/inspect", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["inspection_id"], "inspect-task-1")
        service.inspect_task.assert_called_once_with("admin", 88)

    def test_task_detail_infers_missing_historical_recognition_from_target_path(self):
        self.login()
        source_id = db.create_local_media_source(
            name="历史本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", str(self.local_root / "Show.S02E08.mp4"),
            owner="admin", trigger="scan",
        )
        target = (
            self.default_target / "动漫" /
            "骸骨骑士大人异世界冒险中 (2022) {tmdb-1235283}" / "Season 2" /
            "骸骨骑士大人异世界冒险中.2022.S02E08-WEB-DL.1080p.mp4"
        )
        db.add_local_media_task_item(
            task_id, str(self.local_root / "Show.S02E08.mp4"), str(target),
            role="video", size=507, owner="admin",
        )
        db.update_local_media_task(task_id, owner="admin", status="completed")
        scraper = Mock()
        scraper.parse_media.return_value = SimpleNamespace(
            title="骸骨骑士大人异世界冒险中", year="2022", media_type="tv",
            effective_season=2, effective_episode=8,
        )
        with patch(
            "app.routes.local_media_api.get_local_media_service",
            return_value=SimpleNamespace(scraper=scraper),
        ):
            response = self.client.get(f"/api/local-media/tasks/{task_id}")
        self.assertEqual(response.status_code, 200, response.text)
        recognition = response.json()["recognition"]
        self.assertEqual(recognition["source"], "history_inferred")
        self.assertEqual(recognition["media"][0]["tmdb_id"], "1235283")
        self.assertEqual(recognition["media"][0]["seasons"][0]["episodes"], [8])
        scraper.parse_media.assert_called_once()

    def test_task_detail_and_selected_log_clear_are_owner_scoped_and_safe(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        source_id = db.create_local_media_source(
            name="本地下载", qb_profile="", qb_path_prefix="",
            local_root=str(self.local_root), owner="admin",
        )
        completed_id = db.create_local_media_task(
            source_id, "", str(self.local_root / "Movie.2026.mkv"),
            owner="admin", trigger="manual",
        )
        db.add_local_media_task_item(
            completed_id, str(self.local_root / "Movie.2026.mkv"),
            str(self.movie_target / "Movie (2026).mkv"), role="video", size=123, owner="admin",
        )
        task = db.get_local_media_task(completed_id, owner="admin")
        db.add_local_media_operation_step(
            completed_id, task.operation_token, 0, "move",
            str(self.local_root / "Movie.2026.mkv"), str(self.movie_target / "Movie (2026).mkv"),
            owner="admin",
        )
        db.update_local_media_task(
            completed_id, owner="admin", status="completed", title="Movie", year="2026",
        )
        busy_id = db.create_local_media_task(
            source_id, "", str(self.local_root / "Busy.mkv"), owner="admin", trigger="scan",
        )

        detail = self.client.get(f"/api/local-media/tasks/{completed_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["task"]["content_path"], str(self.local_root / "Movie.2026.mkv"))
        self.assertEqual(detail.json()["task"]["title"], "Movie")
        self.assertEqual(detail.json()["recognition"]["source"], "manual_selection")
        self.assertEqual(detail.json()["recognition"]["media"][0]["title"], "Movie")
        self.assertTrue(detail.json()["task"]["clearable"])
        self.assertEqual(detail.json()["items"][0]["role"], "video")
        self.assertEqual(detail.json()["steps"][0]["action"], "move")

        rejected = self.client.request(
            "DELETE", "/api/local-media/tasks",
            json={"confirm": "", "ids": [completed_id]}, headers=headers,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        cleared = self.client.request(
            "DELETE", "/api/local-media/tasks",
            json={"confirm": "CLEAR", "ids": [completed_id, busy_id, 999999]},
            headers=headers,
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(
            cleared.json(),
            {"requested": 3, "deleted": 1, "skipped_busy": 1, "missing": 1},
        )
        self.assertIsNone(db.get_local_media_task(completed_id, owner="admin"))
        self.assertIsNotNone(db.get_local_media_task(busy_id, owner="admin"))
        self.assertEqual(self.client.get(f"/api/local-media/tasks/{completed_id}").status_code, 404)

    def test_inspect_preview_execute_delegate_to_service_and_hide_private_plans(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        source_id = db.create_local_media_source(
            name="source", qb_profile="", qb_path_prefix="", local_root="/tmp/source", owner="admin"
        )
        service = Mock()
        service.inspect_source.return_value = {"inspection_id": "inspect-1", "video_count": 1}
        service.preview.return_value = {
            "status": "planned", "plans": [{"target_name": "Movie.mkv"}],
            "_move_plans": [object()], "_cleanup_candidates": [object()],
        }
        service.create_manual_task.return_value = 88
        service.execute_task.return_value = {"status": "completed", "task_id": 88}
        with patch("app.routes.local_media_api.get_local_media_service", return_value=service), patch(
            "app.routes.local_media_api.db.claim_local_media_task", return_value=True
        ):
            inspected = self.client.post(
                "/api/local-media/inspect", json={"source_id": source_id}, headers=headers
            )
            previewed = self.client.post(
                "/api/local-media/preview",
                json={"inspection_id": "inspect-1", "tmdb_id": "1", "media_type": "tv",
                      "season": 2, "episode": 7},
                headers=headers,
            )
            executed = self.client.post(
                "/api/local-media/execute",
                json={"inspection_id": "inspect-1", "tmdb_id": "1", "media_type": "tv",
                      "season": 2, "episode": 7},
                headers=headers,
            )
        self.assertEqual(inspected.status_code, 200)
        self.assertNotIn("_move_plans", previewed.json())
        preview_call = service.preview.call_args
        self.assertEqual(preview_call.kwargs["season_override"], 2)
        self.assertEqual(preview_call.kwargs["episode_override"], 7)
        create_call = service.create_manual_task.call_args
        self.assertEqual(create_call.kwargs["season_override"], 2)
        self.assertEqual(create_call.kwargs["episode_override"], 7)
        self.assertEqual(executed.json()["status"], "completed")

    def test_preview_rejects_invalid_position_override_before_service_call(self):
        csrf = self.login(); headers = {"X-CSRF-Token": csrf}
        service = Mock()
        with patch("app.routes.local_media_api.get_local_media_service", return_value=service):
            response = self.client.post(
                "/api/local-media/preview",
                json={"inspection_id": "inspect-1", "media_type": "tv", "season": True},
                headers=headers,
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("季数必须是整数", response.json()["error"])
        service.preview.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
