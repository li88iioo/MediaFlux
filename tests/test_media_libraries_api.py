"""统一媒体库控制中心 API 与页面路由测试。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import config
from app.repositories import local_media as local_media_repo
from app.main import create_app
from app.routes import media_libraries_api
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import IsolatedDatabaseTestCase, isolated_test_database, preserve_web_auth_config


class MediaLibrariesAPITests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        from tests.support import initialized_web_runtime
        self.enterContext(initialized_web_runtime())
        self.client = TestClient(create_app(start_background=False))
        self.client.__enter__()

    def tearDown(self) -> None:
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
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return self._token(self.client.get("/media-libraries").text)

    @staticmethod
    def _profiles() -> list[MediaServerProfile]:
        return [
            MediaServerProfile(
                source="configured:jellyfin",
                server_type="jellyfin",
                label="Jellyfin",
                url="http://jellyfin.local:8096",
                credential="jellyfin-secret",
                enabled=True,
            ),
            MediaServerProfile(
                source="configured:emby",
                server_type="emby",
                label="Emby",
                url="http://emby.local:8096",
                credential="emby-secret",
                enabled=False,
            ),
        ]

    def test_routes_require_login_and_page_renders_after_login(self):
        self.assertEqual(self.client.get("/api/media-libraries/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/media-libraries/strm-directories").status_code, 401)
        self.assertEqual(self.client.get("/api/media-libraries/local-directories").status_code, 401)
        self.assertEqual(self.client.post("/api/media-libraries/mappings", json={}).status_code, 401)
        self.assertEqual(
            self.client.post("/api/media-libraries/path-test", json={}).status_code,
            401,
        )
        self.login()
        page = self.client.get("/media-libraries")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertRegex(page.text, r"/static/css/media-libraries\.css\?v=[0-9a-f]{16}")
        self.assertIn('id="mediaLibrariesPage"', page.text)
        self.assertEqual(
            self.client.post("/api/media-libraries/path-test", json={}).status_code,
            403,
        )

    def test_library_payload_preserves_collection_type_for_local_category_inference(self):
        normalized = media_libraries_api._library_payload({
            "id": "shows",
            "name": "节目",
            "collection_type": "TVShows",
            "locations": ["//NAS/节目"],
        })
        legacy = media_libraries_api._library_payload({
            "id": "movies",
            "name": "电影",
            "CollectionType": "Movies",
            "locations": [],
        })
        self.assertEqual(normalized["collection_type"], "tvshows")
        self.assertEqual(legacy["collection_type"], "movies")

    def test_profile_probe_always_closes_short_lived_client(self):
        profile = self._profiles()[0]
        client = Mock()
        client.list_virtual_folders.return_value = []
        with patch("app.modules.media_server_profiles.MediaServerProfile.create_client", return_value=client):
            provider, libraries, error = media_libraries_api._probe_profile(profile)
        self.assertEqual((provider, libraries, error), ("jellyfin", [], ""))
        client.close.assert_called_once_with()

    def test_overview_projects_profiles_mappings_bindings_and_empty_online_server(self):
        self.login()
        profiles = self._profiles()

        def probe(profile: MediaServerProfile):
            if profile.server_type == "jellyfin":
                return "jellyfin", [], ""
            return "emby", [], "媒体服务器未启用"

        mapping_payloads = {
            "jellyfin": ([{"local": "/data/strm", "server": "//NAS/STRM"}], ""),
            "emby": ([], ""),
        }
        bindings = [{
            "source_id": 1,
            "source_name": "qB 下载",
            "category": "anime",
            "category_label": "动漫",
            "local_path": "/media/library/动漫",
            "provider": "jellyfin",
            "library_id": "anime",
            "library_name": "动漫",
            "server_path": "//NAS/视频/动漫",
        }]
        strm = {
            "root": "/data/strm",
            "output_root": "/data/strm/光鸭云盘",
            "source_error": "",
            "sources": [{"id": "1", "name": "整理", "local_path": "/data/strm/光鸭云盘/整理"}],
            "directories": [
                {"id": "root::0", "name": "全部 STRM", "local_path": "/data/strm/光鸭云盘", "kind": "root", "source_id": ""},
                {"id": "category:1:1", "name": "整理 / 动漫", "local_path": "/data/strm/光鸭云盘/整理/动漫", "kind": "category", "source_id": "1"},
            ],
        }
        with (
            patch("app.routes.media_libraries_api.list_configured_profiles", return_value=profiles),
            patch("app.routes.media_libraries_api._probe_profile", side_effect=probe),
            patch("app.routes.media_libraries_api._mapping_payload", side_effect=lambda provider: mapping_payloads[provider]),
            patch("app.routes.media_libraries_api._local_bindings", return_value=bindings),
            patch("app.routes.media_libraries_api._strm_summary", return_value=strm),
            # 历史全库回退键已不参与刷新，但概览字段与原值保持对外契约。
            patch(
                "app.routes.media_libraries_api.config.get_bool",
                side_effect=lambda key, default=None: key == "JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK",
            ),
        ):
            response = self.client.get("/api/media-libraries/overview")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(
            [(server["fallback_key"], server["allow_global_refresh_fallback"]) for server in payload["servers"]],
            [("JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK", True), ("EMBY_ALLOW_GLOBAL_REFRESH_FALLBACK", False)],
        )
        self.assertEqual(payload["summary"], {
            "configured_servers": 2,
            "online_servers": 1,
            "libraries": 0,
            "path_mappings": 1,
            "local_bindings": 1,
            "total_mappings": 2,
            "local_sources": 0,
            "strm_sources": 1,
        })
        jellyfin = payload["servers"][0]
        self.assertEqual(jellyfin["mappings"][0]["server"], "//NAS/STRM")
        self.assertNotIn("credential", jellyfin)
        self.assertNotIn("jellyfin-secret", response.text)
        self.assertEqual(payload["local_bindings"], bindings)
        self.assertEqual(payload["strm"], strm)

    def test_strm_summary_lists_existing_category_directories(self):
        # 单来源也按选中目录名隔离，不借用应用启动后已发布的其它来源快照。
        self.enterContext(isolated_test_database())
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / media_libraries_api.STRM_SUBDIR
            (output / "整理" / "动漫").mkdir(parents=True)
            (output / "整理" / "电影").mkdir()
            (output / "整理" / ".cache").mkdir()

            def config_value(key: str, default: str = "") -> str:
                values = {
                    "STRM_ROOT": str(root),
                    "GY_STRM_SOURCE_DIRS": '[{"id":"1","name":"整理"}]',
                }
                return values.get(key, default)

            with patch("app.routes.media_libraries_api.config.get", side_effect=config_value):
                payload = media_libraries_api._strm_summary()

        self.assertEqual(payload["output_root"], str(output))
        self.assertEqual(payload["sources"][0]["local_path"], str(output / "整理"))
        names = [item["name"] for item in payload["directories"]]
        self.assertEqual(names, ["全部 STRM", "整理", "整理 / 动漫", "整理 / 电影"])
        self.assertNotIn(".cache", names)
        anime = next(item for item in payload["directories"] if item["name"] == "整理 / 动漫")
        self.assertEqual(anime["local_path"], str(output / "整理" / "动漫"))
        self.assertEqual(anime["kind"], "category")

    def test_strm_summary_uses_selected_directory_name_for_old_empty_prefix(self):
        self.enterContext(isolated_test_database())
        from app import database as db

        db.kv_set(db._STRM_SOURCE_SNAPSHOT_KEY, json.dumps({
            "version": 1, "sources": [{"id": "1", "name": "整理"}],
            "strm_root": "/data/strm", "local_prefixes": {"1": ""},
        }))
        with patch("app.routes.media_libraries_api.config.get", side_effect=lambda key, default="": {
            "STRM_ROOT": "/data/strm", "GY_STRM_SOURCE_DIRS": '[{"id":"1","name":"整理"}]',
        }.get(key, default)):
            payload = media_libraries_api._strm_summary()
        self.assertEqual(payload["source_error"], "")
        self.assertEqual(payload["sources"][0]["local_path"], "/data/strm/光鸭云盘/整理")

    def test_strm_directory_browser_supports_nested_sources_and_blocks_escape(self):
        self.login()
        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            output = root / media_libraries_api.STRM_SUBDIR
            anime = output / "整理" / "动漫"
            nsfw = output / "NSFW"
            anime.mkdir(parents=True)
            nsfw.mkdir()

            with patch(
                "app.routes.media_libraries_api.config.get",
                side_effect=preserve_web_auth_config(lambda key, default="": str(root) if key == "STRM_ROOT" else default),
            ):
                root_response = self.client.get("/api/media-libraries/strm-directories")
                source_response = self.client.get(
                    "/api/media-libraries/strm-directories",
                    params={"path": str(output / "整理")},
                )
                leaf_response = self.client.get(
                    "/api/media-libraries/strm-directories",
                    params={"path": str(anime)},
                )
                escaped_response = self.client.get(
                    "/api/media-libraries/strm-directories",
                    params={"path": outside_dir},
                )

        self.assertEqual(root_response.status_code, 200, root_response.text)
        root_directories = root_response.json()["directories"]
        self.assertEqual([item["name"] for item in root_directories], ["NSFW", "整理"])
        self.assertTrue(all(item["is_dir"] is True for item in root_directories))
        self.assertEqual(source_response.status_code, 200, source_response.text)
        source_directory = source_response.json()["directories"][0]
        self.assertEqual(source_directory["path"], str(anime))
        self.assertIs(source_directory["is_dir"], True)
        self.assertEqual(leaf_response.status_code, 200, leaf_response.text)
        self.assertEqual(leaf_response.json()["current"], str(anime))
        self.assertEqual(leaf_response.json()["directories"], [])
        self.assertEqual(escaped_response.status_code, 400, escaped_response.text)
        self.assertIn("允许", escaped_response.text)

    def test_strm_directory_browser_requires_configured_existing_output_root(self):
        self.login()
        with patch("app.routes.media_libraries_api.config.get", side_effect=preserve_web_auth_config(lambda _key, default="": "")):
            missing_config = self.client.get("/api/media-libraries/strm-directories")
        self.assertEqual(missing_config.status_code, 400, missing_config.text)
        self.assertIn("尚未配置", missing_config.text)

        with TemporaryDirectory() as temp_dir:
            missing_root = Path(temp_dir) / "missing"
            with patch("app.routes.media_libraries_api.config.get", side_effect=preserve_web_auth_config(lambda _key, default="": str(missing_root))):
                missing_output = self.client.get("/api/media-libraries/strm-directories")
        self.assertEqual(missing_output.status_code, 400, missing_output.text)
        self.assertIn("不存在", missing_output.text)


    def test_local_directory_browser_uses_configured_roots_and_blocks_escape(self):
        self.login()
        with TemporaryDirectory() as root_raw, TemporaryDirectory() as outside_raw:
            root = Path(root_raw).resolve()
            child = root / "动漫"
            child.mkdir()
            outside = Path(outside_raw).resolve()
            with patch(
                "app.modules.local_directory_browser._configured_roots",
                return_value=[root],
            ):
                roots = self.client.get(
                    "/api/media-libraries/local-directories", params={"path": "__roots__"}
                )
                nested = self.client.get(
                    "/api/media-libraries/local-directories", params={"path": str(root)}
                )
                escaped = self.client.get(
                    "/api/media-libraries/local-directories", params={"path": str(outside)}
                )
        self.assertEqual(roots.status_code, 200, roots.text)
        self.assertEqual(roots.json()["directories"][0]["id"], str(root))
        self.assertEqual(nested.status_code, 200, nested.text)
        self.assertEqual(nested.json()["directories"][0]["id"], str(child))
        self.assertEqual(escaped.status_code, 400, escaped.text)

    def test_legacy_global_refresh_keys_stay_savable_and_read_back(self):
        """历史全库回退键不再控制刷新，但旧配置提交与原值回读不能变成 400 或丢值。"""
        from app import config

        headers = {"X-CSRF-Token": self.login()}
        legacy = {"JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK": "1", "EMBY_ALLOW_GLOBAL_REFRESH_FALLBACK": "0"}
        response = self.client.post("/api/config", json=legacy, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({key: config.get(key) for key in legacy}, legacy)
        payload = self.client.get("/api/config").json()
        self.assertEqual({key: payload[key] for key in legacy}, legacy)

    def test_unified_save_persists_strm_and_local_mappings(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with TemporaryDirectory() as temp_raw:
            base = Path(temp_raw).resolve()
            source_root = base / "downloads"
            target_root = base / "library" / "动漫"
            source_root.mkdir(parents=True)
            target_root.mkdir(parents=True)
            source_id = local_media_repo.create_local_media_source(
                name="本地下载-保存", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            scheduler = Mock()
            with (
                patch("app.routes.media_libraries_api.config.has_external_override", return_value=False),
                patch("app.routes.media_libraries_api.config.set_and_save") as save_config,
                patch(
                    "app.modules.local_media_scheduler.get_local_media_scheduler",
                    return_value=scheduler,
                ),
            ):
                response = self.client.post(
                    "/api/media-libraries/mappings",
                    headers=headers,
                    json={
                        "strm_mappings": {
                            "jellyfin": [{"local": "/data/strm", "server": "//NAS/STRM"}],
                        },
                        "local_bindings": [{
                            "source_id": source_id,
                            "category": "anime",
                            "local_path": str(target_root),
                            "provider": "jellyfin",
                            "library_id": "anime",
                            "library_name": "动漫",
                            "server_path": "//NAS/媒体/动漫",
                        }],
                    },
                )
        self.assertEqual(response.status_code, 200, response.text)
        targets = local_media_repo.list_local_library_targets(source_id, owner="admin")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].path, str(target_root))
        self.assertEqual(targets[0].server_path, "//NAS/媒体/动漫")
        save_config.assert_called_once_with({
            "JELLYFIN_PATH_MAPPINGS": '[{"local":"/data/strm","server":"//NAS/STRM"}]',
        })
        scheduler.reload.assert_called_once_with()

    def test_unified_save_rejects_local_mapping_without_media_library(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with TemporaryDirectory() as temp_raw:
            base = Path(temp_raw).resolve()
            source_root = base / "downloads-unbound"
            target_root = base / "library-unbound"
            source_root.mkdir()
            target_root.mkdir()
            source_id = local_media_repo.create_local_media_source(
                name="本地下载-未绑定", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            response = self.client.post(
                "/api/media-libraries/mappings",
                headers=headers,
                json={
                    "strm_mappings": {},
                    "local_bindings": [{
                        "source_id": source_id,
                        "category": "default",
                        "local_path": str(target_root),
                        "provider": "",
                        "library_id": "",
                        "library_name": "",
                        "server_path": "",
                    }],
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("必须选择 Jellyfin 或 Emby", response.text)
        self.assertEqual(local_media_repo.list_local_library_targets(source_id, owner="admin"), [])

    def test_unified_save_rolls_back_local_bindings_when_config_save_fails(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with TemporaryDirectory() as temp_raw:
            base = Path(temp_raw).resolve()
            source_root = base / "downloads"
            old_target = base / "library" / "旧"
            new_target = base / "library" / "新"
            source_root.mkdir(parents=True)
            old_target.mkdir(parents=True)
            new_target.mkdir(parents=True)
            source_id = local_media_repo.create_local_media_source(
                name="本地下载-回滚", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            local_media_repo.upsert_local_library_target(
                source_id, "default", str(old_target), owner="admin"
            )
            with (
                patch("app.routes.media_libraries_api.config.has_external_override", return_value=False),
                patch(
                    "app.routes.media_libraries_api.config.set_and_save",
                    side_effect=OSError("write failed"),
                ),
            ):
                response = self.client.post(
                    "/api/media-libraries/mappings",
                    headers=headers,
                    json={
                        "strm_mappings": {"jellyfin": []},
                        "local_bindings": [{
                            "source_id": source_id,
                            "category": "default",
                            "local_path": str(new_target),
                            "provider": "jellyfin",
                            "library_id": "movies",
                            "library_name": "电影",
                            "server_path": "//NAS/媒体/电影",
                        }],
                    },
                )
        self.assertEqual(response.status_code, 503, response.text)
        targets = local_media_repo.list_local_library_targets(source_id, owner="admin")
        self.assertEqual([item.path for item in targets], [str(old_target)])

    def test_unified_save_returns_conflict_and_rolls_back_on_concurrent_config_change(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with TemporaryDirectory() as temp_raw:
            base = Path(temp_raw).resolve()
            source_root = base / "downloads"
            old_target = base / "library" / "旧"
            new_target = base / "library" / "新"
            source_root.mkdir(parents=True)
            old_target.mkdir(parents=True)
            new_target.mkdir(parents=True)
            source_id = local_media_repo.create_local_media_source(
                name="本地下载-并发", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            local_media_repo.upsert_local_library_target(
                source_id, "default", str(old_target), owner="admin"
            )
            with (
                patch("app.routes.media_libraries_api.config.has_external_override", return_value=False),
                patch(
                    "app.routes.media_libraries_api.config.set_and_save",
                    side_effect=config.ConcurrentConfigUpdateError("private detail"),
                ),
            ):
                response = self.client.post(
                    "/api/media-libraries/mappings",
                    headers=headers,
                    json={
                        "strm_mappings": {"jellyfin": []},
                        "local_bindings": [{
                            "source_id": source_id,
                            "category": "default",
                            "local_path": str(new_target),
                            "provider": "jellyfin",
                            "library_id": "movies",
                            "library_name": "电影",
                            "server_path": "//NAS/媒体/电影",
                        }],
                    },
                )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("private detail", response.text)
        targets = local_media_repo.list_local_library_targets(source_id, owner="admin")
        self.assertEqual([item.path for item in targets], [str(old_target)])

    def test_path_test_reports_matched_covered_and_unmatched(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        profile = self._profiles()[0]
        client = Mock()
        client.list_virtual_folders.return_value = [{
            "id": "anime",
            "name": "动漫",
            "locations": ["//NAS/MediaFlux/STRM/光鸭云盘/整理/动漫"],
        }]
        base_payload = {
            "provider": "jellyfin",
            "local_path": "/data/strm",
            "server_path": "//NAS/MediaFlux/STRM",
        }
        with (
            patch("app.routes.media_libraries_api.list_configured_profiles", return_value=[profile]),
            patch("app.modules.media_server_profiles.MediaServerProfile.create_client", return_value=client),
        ):
            matched = self.client.post(
                "/api/media-libraries/path-test",
                headers=headers,
                json={**base_payload, "sample_path": "/data/strm/光鸭云盘/整理/动漫"},
            )
            covered = self.client.post(
                "/api/media-libraries/path-test",
                headers=headers,
                json=base_payload,
            )
            unmatched = self.client.post(
                "/api/media-libraries/path-test",
                headers=headers,
                json={**base_payload, "sample_path": "/data/strm/光鸭云盘/NSFW"},
            )

        self.assertEqual(matched.status_code, 200, matched.text)
        self.assertEqual(matched.json()["status"], "matched")
        self.assertEqual(matched.json()["matches"][0]["mode"], "direct")
        self.assertEqual(covered.json()["status"], "covered")
        self.assertEqual(covered.json()["matches"][0]["mode"], "covered")
        self.assertEqual(unmatched.json()["status"], "unmatched")
        self.assertEqual(client.close.call_count, 3)
        self.assertEqual(unmatched.json()["matches"], [])

    def test_path_test_uses_unique_longest_library_location(self):
        headers = {"X-CSRF-Token": self.login()}
        profile = self._profiles()[0]
        client = Mock()
        cases = (
            ("duplicate", "/media/movies", [
                {"id": "a", "locations": ["/media/movies"]},
                {"id": "b", "locations": ["/media/movies"]},
            ], "ambiguous", {"a", "b"}),
            ("nested", "/media/movies/anime", [
                {"id": "a", "locations": ["/media/movies"]},
                {"id": "b", "locations": ["/media/movies"]},
                {"id": "anime", "locations": ["/media/movies/anime"]},
            ], "matched", {"anime"}),
            ("nested-duplicate", "/media/movies/anime", [
                {"id": "root", "locations": ["/media/movies"]},
                {"id": "a", "locations": ["/media/movies/anime"]},
                {"id": "b", "locations": ["/media/movies/anime"]},
            ], "ambiguous", {"a", "b"}),
            ("unc-duplicate", "//NAS/Media", [
                {"id": "a", "locations": [r"\\NAS\Media"]},
                {"id": "b", "locations": ["//nas/media/"]},
            ], "ambiguous", {"a", "b"}),
            ("same-library-aliases", "D:/STRASSE", [
                {"id": "a", "locations": ["d:/Straße/", r"D:\STRASSE"]},
            ], "matched", {"a"}),
            ("missing-library-id", "/media/movies", [
                {"id": "", "locations": ["/media/movies"]},
            ], "unmatched", set()),
        )
        with (
            patch("app.routes.media_libraries_api.list_configured_profiles", return_value=[profile]),
            patch("app.modules.media_server_profiles.MediaServerProfile.create_client", return_value=client),
        ):
            for label, server_path, folders, status, ids in cases:
                with self.subTest(case=label):
                    client.list_virtual_folders.return_value = folders
                    response = self.client.post("/api/media-libraries/path-test", headers=headers, json={
                        "provider": "jellyfin", "local_path": "/data/strm", "server_path": server_path,
                        "sample_path": "/data/strm/Film.strm",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertEqual(result["status"], status)
                    self.assertEqual({item["id"] for item in result["matches"]}, ids)
                    self.assertTrue(all(item["mode"] == "direct" for item in result["matches"]))
        self.assertEqual(client.close.call_count, len(cases))

    def test_path_test_local_binding_matches_execution_without_binding_strm(self):
        headers = {"X-CSRF-Token": self.login()}
        profile = self._profiles()[0]
        client = Mock()
        folders = [
            {"id": "a", "name": "电影 A", "locations": ["/media/movies"]},
            {"id": "b", "name": "电影 B", "locations": ["/media/movies"]},
        ]
        payload = {"provider": "jellyfin", "local_path": "/data/library", "server_path": "/media/movies",
                   "sample_path": "/data/library/Film.strm", "library_id": "a", "library_name": "电影 A"}
        cases = (
            ("bound-local", {"source_kind": "local"}, folders, 200, "matched", {"a"}),
            ("selected-strm", {"source_kind": "strm"}, folders, 200, "ambiguous", {"a", "b"}),
            ("unspecified-kind", {}, folders, 200, "ambiguous", {"a", "b"}),
            ("stale-id", {"source_kind": "local", "library_id": "missing"}, folders, 400, "绑定已失效", set()),
            ("stale-name", {"source_kind": "local", "library_name": "旧名称"}, folders, 400, "名称与绑定 ID 不一致", set()),
            ("legacy-name", {"source_kind": "local", "library_id": ""}, folders, 200, "matched", {"a"}),
            ("duplicate-name", {"source_kind": "local", "library_id": ""}, [folders[0], {**folders[1], "name": "电影 A"}],
             400, "存在同名媒体库", set()),
            ("selected-other-path", {"source_kind": "local", "server_path": "/elsewhere"}, folders, 200, "unmatched", set()),
            ("filter-before-longest", {"source_kind": "local"}, [{**folders[0], "locations": ["/media"]}, folders[1]],
             200, "matched", {"a"}),
        )
        with (
            patch("app.routes.media_libraries_api.list_configured_profiles", return_value=[profile]),
            patch("app.modules.media_server_profiles.MediaServerProfile.create_client", return_value=client),
        ):
            for label, extra, libraries, http_status, status, ids in cases:
                with self.subTest(case=label):
                    client.list_virtual_folders.return_value = libraries
                    response = self.client.post("/api/media-libraries/path-test", headers=headers, json={**payload, **extra})
                    self.assertEqual(response.status_code, http_status, response.text)
                    if http_status == 400:
                        self.assertIn(status, response.json()["error"])
                        continue
                    result = response.json()
                    self.assertEqual(result["status"], status)
                    self.assertEqual({item["id"] for item in result["matches"]}, ids)
                    self.assertEqual(result["allowed_library_ids"], ["a"] if extra.get("source_kind") == "local" else [])
        self.assertEqual(client.close.call_count, len(cases))

    def test_path_test_validates_source_kind_and_binding_fields_before_upstream(self):
        headers = {"X-CSRF-Token": self.login()}
        payload = {"provider": "jellyfin", "local_path": "/data/library", "server_path": "/media/movies"}
        with patch("app.routes.media_libraries_api.list_configured_profiles") as profiles:
            for extra in (
                {"source_kind": "invalid"}, {"source_kind": []}, {"source_kind": "local"},
                {"source_kind": "local", "library_id": ["a"]},
                {"source_kind": "local", "library_id": "a", "library_name": {}},
            ):
                with self.subTest(extra=extra):
                    response = self.client.post("/api/media-libraries/path-test", headers=headers, json={**payload, **extra})
                    self.assertEqual(response.status_code, 400, response.text)
            profiles.assert_not_called()

    def test_path_test_rejects_invalid_input_and_handles_upstream_failure(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        profile = self._profiles()[0]
        valid = {
            "provider": "jellyfin",
            "local_path": "/data/strm",
            "server_path": "//NAS/STRM",
        }
        with patch("app.routes.media_libraries_api.list_configured_profiles", return_value=[profile]):
            invalid_provider = self.client.post(
                "/api/media-libraries/path-test",
                headers=headers,
                json={**valid, "provider": "plex"},
            )
            invalid_sample = self.client.post(
                "/api/media-libraries/path-test",
                headers=headers,
                json={**valid, "sample_path": ["/data/strm"]},
            )
        self.assertEqual(invalid_provider.status_code, 400, invalid_provider.text)
        self.assertEqual(invalid_sample.status_code, 400, invalid_sample.text)
        self.assertIn("测试路径格式无效", invalid_sample.text)

        broken_client = Mock()
        broken_client.list_virtual_folders.side_effect = ConnectionError("private upstream")
        with (
            patch("app.routes.media_libraries_api.list_configured_profiles", return_value=[profile]),
            patch("app.modules.media_server_profiles.MediaServerProfile.create_client", return_value=broken_client),
        ):
            upstream = self.client.post(
                "/api/media-libraries/path-test", headers=headers, json=valid,
            )
        self.assertEqual(upstream.status_code, 502, upstream.text)
        self.assertNotIn("private upstream", upstream.text)
