"""Jellyfin 联动删除回归：真实临时 STRM/索引/审计，禁止生产网络与任务。"""
from __future__ import annotations

from typing import Any

import hashlib
import json
import re
import socket
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, call, patch

from fastapi.testclient import TestClient

from app import config
from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaFile
from app.main import create_app
from app.modules import jellyfin_delete as deletion
from app.modules import strm
from app.modules.directory_scrape_errors import DirectoryScrapePublicError
from app.modules.media_server_path_mapping import (
    MediaServerPathMappingError,
    parse_media_server_path_mappings,
    reverse_media_server_path_mapping,
)
from app.repositories.strm import list_strm_path_owners, upsert_strm_index
from app.routes import jellyfin_api as routes
from tests.support import IsolatedDatabaseTestCase


def fingerprint(contents: bytes) -> str:
    return "sha256:" + hashlib.sha256(contents).hexdigest()


class ReverseJellyfinPathTests(unittest.TestCase):
    def test_longest_server_prefix_and_windows_unc_normalization(self):
        cases = (
            ("/media/Movies/作品 A.strm", "/media/Movies", "/mf/作品 A.strm"),
            (r"d:\MEDIA\Movies\作品 A.strm", "D:/Media/Movies", "/mf/作品 A.strm"),
            (r"\\NAS\MEDIA\Movies\作品 A.strm", "//nas/media/Movies", "/mf/作品 A.strm"),
            (r"\\SERVER\STRASSE\Movies\作品 A.strm", "//server/Straße/Movies", "/mf/作品 A.strm"),
        )
        for path, server, expected in cases:
            with self.subTest(path=path):
                mappings = parse_media_server_path_mappings(json.dumps([
                    {"local": "/very/long/local/fallback", "server": server.rsplit("/", 1)[0]},
                    {"local": "/mf", "server": server},
                ]))
                local, selected = reverse_media_server_path_mapping(path, reversed(mappings))
                self.assertEqual(local, expected)
                self.assertEqual(selected.server_prefix, server)

    def test_windows_drive_root_is_preserved(self):
        mappings = parse_media_server_path_mappings('[{"local":"D:/","server":"E:/"}]')
        self.assertEqual(reverse_media_server_path_mapping(r"e:\Movies\A.strm", mappings)[0],
                         "D:/Movies/A.strm")

    def test_unmatched_segment_case_and_traversal_are_rejected(self):
        mappings = parse_media_server_path_mappings('[{"local":"/mf","server":"/media/movie"}]')
        for path in ("/media/movies/A.strm", "/Media/movie/A.strm", "relative.strm",
                     "/unknown/A.strm", "/media/movie/../A.strm", "/media/movie/A\x00.strm"):
            with self.subTest(path=path), self.assertRaises(MediaServerPathMappingError):
                reverse_media_server_path_mapping(path, mappings)

    def test_equally_specific_reverse_mappings_are_ambiguous(self):
        for left, right, path in (("/media", "/media", "/media/A.strm"),
                                  ("D:/MEDIA", "d:/media", r"d:\media\A.strm"),
                                  ("//NAS/Media", "//nas/media", r"\\NAS\MEDIA\A.strm")):
            with self.subTest(server=left):
                mappings = parse_media_server_path_mappings(json.dumps([
                    {"local": "/mf/a", "server": left}, {"local": "/mf/b", "server": right},
                ]))
                with self.assertRaises(MediaServerPathMappingError):
                    reverse_media_server_path_mapping(path, mappings)


class JellyfinDeleteTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.values = {}
        original_get = config.get

        def get_value(key, default=""):
            return self.values[key] if key in self.values else original_get(key, default)

        self.enterContext(patch.object(config, "get", side_effect=get_value))
        self.enterContext(patch.object(strm, "get", side_effect=get_value))
        self.provider = Mock(spec=("logged_in", "file_info", "list_dir", "delete"))
        self.factory = self.enterContext(patch.object(deletion, "GuangYaClient", return_value=self.provider))
        self.close = self.enterContext(patch.object(deletion, "close_guangya_client"))
        self.sync = self.enterContext(patch.object(strm, "trigger_cloud_changes", side_effect=AssertionError("Jellyfin 删除不得触发来源扫描")))
        self.addCleanup(self.sync.assert_not_called)
        self.manager = Mock()
        self.enterContext(patch.object(routes, "get_organize_manager", return_value=self.manager))
        network = self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("禁止测试连接网络")))
        self.addCleanup(network.assert_not_called)
        self.reset_case()

    def reset_case(self):
        with db.get_conn() as conn:
            for table in ("organize_delete_audit", "strm_index", "strm_path_cleanup", "strm_metadata_queue", "download_requests"):
                conn.execute(f"DELETE FROM {table}")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="jellyfin-delete-"))).resolve()
        self.media_root = self.root / "strm" / strm.STRM_SUBDIR
        self.media_root.mkdir(parents=True)
        self.values.update({
            "STRM_ROOT": str(self.media_root.parent),
            "JELLYFIN_PATH_MAPPINGS": json.dumps([{"local": str(self.media_root), "server": "/jellyfin"}]),
            "GY_STRM_SOURCE_DIRS": json.dumps([{"id": f"source-{key}", "name": f"Source {key}"} for key in "abc"]),
        })
        self.nodes, self.listings = {}, {}
        self.store_node(GuangYaFile("library", "Library", True, parent_id="0"))
        for key in "abc":
            self.store_node(GuangYaFile(f"source-{key}", f"Source {key}", True, parent_id="library"))
            self.store_node(GuangYaFile(f"season-{key}", "Season 01", True, parent_id=f"source-{key}"))
        self.provider.reset_mock()
        self.provider.logged_in = True
        self.provider.file_info.side_effect = self.nodes.get
        self.provider.list_dir.side_effect = lambda parent: list(self.listings.get(parent, ()))
        self.provider.delete.side_effect = None
        self.provider.delete.return_value = None
        for mock in (self.factory, self.close, self.sync, self.manager):
            mock.reset_mock()
        self.manager.start_operation.return_value = {"ok": True, "task_id": "jellyfin-delete-task"}

    def store_node(self, item):
        self.nodes[item.file_id] = item
        for children in self.listings.values():
            children[:] = [child for child in children if child.file_id != item.file_id]
        self.listings.setdefault(item.parent_id, []).append(item)

    def change_node(self, file_id, **fields):
        self.store_node(replace(self.nodes[file_id], **fields))

    def seed(self, file_id="file-a", source_id="source-a", relative=None):
        if file_id not in self.nodes:
            self.store_node(GuangYaFile(file_id, f"{file_id}.mkv", False, 1024,
                                       f"etag-{file_id}", source_id.replace("source-", "season-")))
        item = self.nodes[file_id]
        path = self.media_root / (relative or f"{source_id}/{file_id}.strm")
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = f"https://stream.invalid/{file_id}\n".encode()
        path.write_bytes(contents)
        upsert_strm_index(f"guangya:{source_id}", file_id, item.etag, item.size,
                          item.name, str(path), fingerprint(contents))
        return path

    def payload(self, *paths, mode="mf", mappings=None) -> dict[str, Any]:
        raw = ["/jellyfin/" + path.relative_to(self.media_root).as_posix() for path in paths]
        result = {"paths": raw, "mapping_mode": mode,
                  "fingerprints": {remote: fingerprint(local.read_bytes()) for remote, local in zip(raw, paths)}}
        if mode == "manual":
            result["mappings"] = mappings if mappings is not None else json.loads(self.values["JELLYFIN_PATH_MAPPINGS"])
        return result

    def audits(self):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM organize_delete_audit ORDER BY id")]

    def assert_no_write(self):
        self.provider.delete.assert_not_called()
        self.sync.assert_not_called()
        self.assertFalse([row for row in self.audits() if row["status"] != "blocked"])

    def assert_rejected_execution(self, payload, expected):
        # 受理后、Writer 入口的本地变化可抛业务冲突；云端预检失败返回结果。
        try:
            result = deletion.execute_delete(payload, expected)
        except DirectoryScrapePublicError:
            pass
        else:
            self.assertFalse(result["ok"])
            self.assertEqual(result["completed"], 0)
            self.assertEqual(result["items"], [])
            self.assertTrue(result["error"])
        self.assert_no_write()

    def web_client(self, *, login=True):
        # 不进入 TestClient lifespan，避免启动任何后台任务或真实 provider。
        client = TestClient(create_app(start_background=False))
        self.addCleanup(client.close)
        if not login:
            return client, {}
        page = client.get("/login")
        token = self.csrf(page.text)
        username, password = config.web_credentials()
        response = client.post("/login", data={"username": username, "password": password,
                                               "csrf_token": token}, follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        page = client.get("/")
        self.assertEqual(page.status_code, 200, page.text)
        return client, {"X-CSRF-Token": self.csrf(page.text)}

    @staticmethod
    def csrf(html):
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if match is None:
            raise AssertionError("真实 Web 页面必须输出会话 CSRF token")
        return match.group(1)

    def test_mf_and_manual_use_the_same_index_and_original_bytes_identity(self):
        path = self.seed(relative="source-a/作品 A.strm")
        expected = deletion.prepare_delete(self.payload(path))
        self.assertEqual(deletion.prepare_delete(self.payload(path, mode="manual")), expected)
        self.assertEqual((expected[0].path, expected[0].source_id, expected[0].file_id),
                         (str(path), "source-a", "file-a"))
        self.assertEqual(expected[0].fingerprint, fingerprint(path.read_bytes()))
        self.factory.assert_not_called()
        self.assert_no_write()
        result = deletion.execute_delete(self.payload(path, mode="manual"), expected)
        self.assertTrue(result["ok"], result)
        self.provider.delete.assert_called_once_with(["file-a"])

    def test_windows_and_unc_requests_use_original_path_fingerprint_keys(self):
        path = self.seed(relative="source-a/作品 A.strm")
        for server, remote in (("D:/Media", r"d:\MEDIA\source-a\作品 A.strm"),
                               ("//nas/media", r"\\NAS\MEDIA\source-a\作品 A.strm")):
            with self.subTest(remote=remote):
                mappings = [{"local": str(self.media_root), "server": server}]
                self.values["JELLYFIN_PATH_MAPPINGS"] = json.dumps(mappings)
                payload: dict[str, Any] = {"paths": [remote], "fingerprints": {remote: fingerprint(path.read_bytes())}}
                expected = deletion.prepare_delete(payload)
                self.assertEqual(expected[0].path, str(path))
                self.assertEqual(deletion.prepare_delete(dict(payload, mapping_mode="manual", mappings=mappings)), expected)
                # slash-normalized 键不是原始 Jellyfin 路径，不能替代请求中的身份凭据。
                payload["fingerprints"] = {remote.replace("\\", "/"): fingerprint(path.read_bytes())}
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
        self.factory.assert_not_called()
        self.assert_no_write()

    def test_missing_or_invalid_client_fingerprints_are_rejected(self):
        path = self.seed()
        for value in (None, {}, [], {"/wrong/path": fingerprint(path.read_bytes())},
                      {self.payload(path)["paths"][0]: "sha256:" + "0" * 64},
                      {self.payload(path)["paths"][0]: "not-a-fingerprint"}):
            with self.subTest(fingerprints=value):
                payload = self.payload(path)
                if value is None:
                    payload.pop("fingerprints")
                else:
                    payload["fingerprints"] = value
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()

    def test_manual_wrong_mapping_cannot_delete_a_same_named_managed_file(self):
        path = self.seed(relative="source-a/Movie.strm")
        wrong = self.seed("file-b", "source-b", "source-b/Movie.strm")
        payload = self.payload(path, mode="manual", mappings=[
            {"local": str(wrong.parent), "server": "/jellyfin/source-a"},
        ])
        with self.assertRaises(DirectoryScrapePublicError):
            deletion.prepare_delete(payload)
        self.assert_no_write()

    def test_invalid_selection_and_mapping_fail_closed(self):
        path = self.seed()
        overrides = ({"paths": []}, {"paths": "file-a"}, {"paths": [None]}, {"paths": [""]},
                     {"paths": ["x" * 4097]}, {"paths": ["x"] * (deletion.MAX_DELETE_PATHS + 1)},
                     {"paths": ["/unmapped/A.strm"]}, {"paths": ["/jellyfin2/A.strm"]},
                     {"paths": ["/jellyfin/../A.strm"]}, {"mapping_mode": "other"},
                     {"mapping_mode": "manual", "mappings": {}},
                     {"mapping_mode": "manual", "mappings": []})
        for override in overrides:
            with self.subTest(override=override):
                payload = dict(self.payload(path), **override)
                if isinstance(payload["paths"], list):
                    payload["fingerprints"] = {
                        remote: fingerprint(path.read_bytes()) for remote in payload["paths"]
                        if isinstance(remote, str)
                    }
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
        self.assert_no_write()

    def test_outside_symlinks_directories_missing_index_and_content_changes_are_rejected(self):
        for case in ("outside", "root-prefix-sibling", "file-symlink", "parent-symlink", "directory",
                     "missing-index", "missing-file", "changed-bytes", "missing-fingerprint", "wrong-source"):
            with self.subTest(case=case):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                if case in ("outside", "root-prefix-sibling"):
                    local = self.root / "outside" if case == "outside" else Path(str(self.media_root) + "-other")
                    local.mkdir()
                    payload.update(mapping_mode="manual", mappings=[{"local": str(local), "server": "/jellyfin"}])
                elif case == "file-symlink":
                    other = self.root / "other.strm"
                    other.write_bytes(path.read_bytes())
                    path.unlink()
                    path.symlink_to(other)
                elif case == "parent-symlink":
                    alias = self.media_root / "alias"
                    alias.symlink_to(path.parent, target_is_directory=True)
                    payload["paths"] = ["/jellyfin/alias/" + path.name]
                    payload["fingerprints"] = {payload["paths"][0]: fingerprint(path.read_bytes())}
                elif case == "directory":
                    path.unlink()
                    path.mkdir()
                elif case == "missing-file":
                    path.unlink()
                elif case == "changed-bytes":
                    path.write_bytes(b"replaced STRM\n")
                else:
                    with db.get_conn() as conn:
                        if case == "missing-index":
                            conn.execute("DELETE FROM strm_index")
                        elif case == "missing-fingerprint":
                            conn.execute("UPDATE strm_index SET content_fingerprint=''")
                        elif case == "wrong-source":
                            conn.execute("UPDATE strm_index SET source='guangya:unconfigured'")
                with self.assertRaises(DirectoryScrapePublicError):
                    deletion.prepare_delete(payload)
                self.assert_no_write()

    def test_shared_cloud_file_requires_all_local_references(self):
        path = self.seed()
        self.seed(source_id="source-b")
        with self.assertRaises(DirectoryScrapePublicError):
            deletion.prepare_delete(self.payload(path))
        self.assert_no_write()

    def test_cloud_changes_in_last_batch_item_prevent_every_write(self):
        for field, value in (("etag", "changed"), ("name", "renamed.mkv"), ("size", 2048),
                             ("is_dir", True), ("parent_id", "season-c")):
            with self.subTest(field=field):
                self.reset_case()
                first, last = self.seed(), self.seed("file-b", "source-b")
                payload = self.payload(first, last)
                expected = deletion.prepare_delete(payload)
                self.change_node("file-b", **{field: value})
                self.assert_rejected_execution(payload, expected)
                self.assertEqual(self.audits(), [])
                self.close.assert_called_once_with(self.provider)

    def test_an_entire_normal_source_ancestor_chain_is_required(self):
        for case in ("trash-file", "trash-source", "trash-library", "missing-parent", "parent-is-file", "cycle"):
            with self.subTest(case=case):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                expected = deletion.prepare_delete(payload)
                if case.startswith("trash-"):
                    file_id = {"trash-file": "file-a", "trash-source": "source-a", "trash-library": "library"}[case]
                    parent = self.nodes[file_id].parent_id
                    self.listings[parent] = [item for item in self.listings[parent] if item.file_id != file_id]
                elif case == "missing-parent":
                    self.nodes.pop("season-a")
                elif case == "parent-is-file":
                    self.change_node("season-a", is_dir=False)
                else:
                    self.change_node("source-a", parent_id="season-a")
                self.assert_rejected_execution(payload, expected)
                self.assertEqual(self.audits(), [])

    def test_api_202_only_accepts_the_writer_callback(self):
        path = self.seed()
        payload = self.payload(path)
        client, headers = self.web_client()
        response = client.post("/api/jellyfin/delete", json=payload, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["task_id"], "jellyfin-delete-task")
        self.manager.start_operation.assert_called_once()
        args, kwargs = self.manager.start_operation.call_args
        self.assertTrue(callable(args[2]))
        self.assertEqual(kwargs, {"queue_if_busy": False, "dedupe_key": deletion.delete_key(deletion.prepare_delete(payload))})
        self.factory.assert_not_called()
        self.assert_no_write()
        operation_result = args[2]()
        assert isinstance(operation_result, dict)
        self.assertTrue(operation_result["ok"])
        self.provider.delete.assert_called_once_with(["file-a"])


    def request_download(self, *, source_id="source-b", status="downloading", organize_status=""):
        request_id, created = download_requests_repo.create_download_request("jellyfin-" + self.root.name, "magnet")
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id, targets="guangya", status="submitted", gy_status=status,
            gy_isolated=1, gy_target_dir=source_id, gy_staging_parent_dir="library",
            gy_staging_name=source_id, gy_staging_cleanup_status="retained",
            organize_started=1, organize_status=organize_status,
        )
        return request_id

    def assert_retired(self, *source_ids):
        self.sync.assert_not_called()
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM strm_path_cleanup").fetchall()
        self.assertEqual({row["source"].split(":", 1)[1] for row in rows}, set(source_ids))
        for row in rows:
            self.assertEqual(list_strm_path_owners(row["strm_path"]), [])

    def test_active_download_and_organize_states_protect_the_whole_batch(self):
        states = [(status, "") for status in
                  ("submitting", "submitted", "downloading", "outcome_unknown", "manual_review")]
        states += [("completed", status) for status in ("pending", "settling", "queued", "starting", "running")]
        for status, organize_status in states:
            with self.subTest(status=status, organize_status=organize_status):
                self.reset_case()
                first, last = self.seed(), self.seed("file-b", "source-b")
                payload = self.payload(first, last)
                expected = deletion.prepare_delete(payload)
                request_id = self.request_download(status=status, organize_status=organize_status)
                result = deletion.execute_delete(payload, expected)
                self.assertFalse(result["ok"])
                self.assertEqual(result["completed"], 0)
                self.assertIn(f"#{request_id}", result["error"])
                self.assert_no_write()
                self.assertEqual([(row["file_id"], row["status"]) for row in self.audits()],
                                 [("file-b", "blocked")])

    def test_unrelated_active_or_finished_download_does_not_block(self):
        for status, source in (("downloading", "source-c"), ("completed", "source-a")):
            with self.subTest(status=status):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                self.request_download(source_id=source, status=status, organize_status="completed")
                self.assertTrue(deletion.execute_delete(payload, deletion.prepare_delete(payload))["ok"])
                self.provider.delete.assert_called_once_with(["file-a"])

    def test_shared_file_all_sources_are_prechecked_and_deleted_only_once(self):
        for overlapping in (True, False):
            with self.subTest(overlapping=overlapping):
                self.reset_case()
                first, second = self.seed(), self.seed(source_id="source-b")
                if overlapping:
                    self.change_node("source-a", parent_id="source-b")
                payload = self.payload(first, second, first)
                expected = deletion.prepare_delete(payload)
                self.assertEqual(len(expected), 2)
                if not overlapping:
                    self.assert_rejected_execution(payload, expected)
                    continue
                result = deletion.execute_delete(payload, expected)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["completed"], 1)
                self.provider.delete.assert_called_once_with(["file-a"])
                self.assert_retired("source-a", "source-b")

    def test_success_retires_exact_index_but_leaves_local_file_for_jellyfin(self):
        path = self.seed()
        original = path.read_bytes()
        payload = self.payload(path)

        def delete(ids):
            self.assertEqual(ids, ["file-a"])
            audit = self.audits()
            self.assertEqual([(row["file_id"], row["status"]) for row in audit], [("file-a", "pending")])
            self.assertEqual(audit[0]["trigger"], "jellyfin_delete")
            self.assertEqual(audit[0]["gcid"], "etag-file-a")

        self.provider.delete.side_effect = delete
        result = deletion.execute_delete(payload, deletion.prepare_delete(payload))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["stats"]["deleted"], 1)
        audit = self.audits()[0]
        self.assertEqual(audit["status"], "success")
        self.assertEqual(result["items"], [{"file_id": "file-a", "audit_id": audit["id"]}])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list_strm_path_owners(str(path)), [])
        self.assertTrue({"season-a", "source-a", "library", "0"}.issubset(
            {item.args[0] for item in self.provider.list_dir.call_args_list}))
        self.assert_retired("source-a")
        self.close.assert_called_once_with(self.provider)

    def test_read_failure_reports_no_write_and_write_failure_is_never_retried(self):
        for failure in ("read", "write"):
            with self.subTest(failure=failure):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                expected = deletion.prepare_delete(payload)
                getattr(self.provider, "file_info" if failure == "read" else "delete").side_effect = TimeoutError("secret-token-should-not-leak")
                result = deletion.execute_delete(payload, expected)
                self.assertFalse(result["ok"])
                self.assertEqual(result["completed"], 0)
                self.assertNotIn("secret-token", json.dumps(result))
                if failure == "read":
                    self.assertIn("未执行云端删除", result["error"])
                    self.assert_no_write()
                    self.assertEqual(self.audits(), [])
                else:
                    self.assertIn("审计", result["error"])
                    self.assertIn("重试", result["error"])
                    self.provider.delete.assert_called_once_with(["file-a"])
                    self.assertEqual(self.audits()[0]["status"], "failed")
                    self.assert_retired()
                    self.assertEqual(len(list_strm_path_owners(str(path))), 1)

    def test_partial_failure_retires_only_confirmed_deletes(self):
        paths = [self.seed(f"file-{key}", f"source-{key}") for key in "abc"]
        payload = self.payload(*reversed(paths))
        self.provider.delete.side_effect = [None, TimeoutError("unknown write outcome"), None]
        result = deletion.execute_delete(payload, deletion.prepare_delete(payload))
        self.assertFalse(result["ok"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["stats"]["deleted"], 1)
        self.assertEqual([item["file_id"] for item in result["items"]], ["file-a"])
        self.assertEqual(self.provider.delete.call_args_list, [call(["file-a"]), call(["file-b"])])
        self.assertEqual([(row["file_id"], row["status"]) for row in self.audits()],
                         [("file-a", "success"), ("file-b", "failed")])
        self.assert_retired("source-a")
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(list_strm_path_owners(str(paths[0])), [])
        self.assertTrue(all(list_strm_path_owners(str(path)) for path in paths[1:]))

    def test_retirement_failure_does_not_rewrite_a_successful_cloud_delete(self):
        path = self.seed()
        payload = self.payload(path)
        with patch.object(deletion, "retire_deleted_strm_files", side_effect=RuntimeError("storage secret")):
            result = deletion.execute_delete(payload, deletion.prepare_delete(payload))
        self.assertFalse(result["ok"])
        self.assertEqual(result["completed"], 1)
        self.assertIn("STRM", result["error"])
        self.provider.delete.assert_called_once_with(["file-a"])
        self.assertEqual(self.audits()[0]["status"], "success")
        self.assertEqual(len(list_strm_path_owners(str(path))), 1)
        self.assertNotIn("storage secret", json.dumps(result))
        self.assertTrue(deletion.STRM_OPERATION_LOCK.acquire(blocking=False))
        deletion.STRM_OPERATION_LOCK.release()

    def test_retirement_is_idempotent_and_never_touches_unselected_files_or_refreshes(self):
        selected, neighbor = self.seed(), self.seed("neighbor", "source-a")
        targets = deletion.prepare_delete(self.payload(selected))
        self.assertTrue(deletion.execute_delete(self.payload(selected), targets)["ok"])
        counts = deletion.retire_deleted_strm_files([asdict(target) for target in targets])
        self.assertEqual(counts, {"strm_indexes_retired": 0, "metadata_jobs_cancelled": 0})
        self.assert_retired("source-a")
        self.assertTrue(selected.is_file())
        self.assertTrue(neighbor.is_file())
        self.assertEqual(len(list_strm_path_owners(str(neighbor))), 1)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strm_refresh_outbox").fetchone()[0], 0)
        # 重复整个请求在预检阶段被拒绝，不重放云端写操作。
        with self.assertRaises(DirectoryScrapePublicError):
            deletion.prepare_delete(self.payload(selected))
        self.provider.delete.assert_called_once_with(["file-a"])

    def test_retirement_conflict_rolls_back_indexes_and_cleanup_credentials(self):
        paths = [self.seed(), self.seed("file-b", "source-b")]
        targets = deletion.prepare_delete(self.payload(*paths))
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_index SET etag='replacement' WHERE file_id='file-b'")
        with self.assertRaisesRegex(ValueError, "身份发生变化"):
            deletion.retire_deleted_strm_files([asdict(target) for target in targets])
        self.assertTrue(all(list_strm_path_owners(str(path)) for path in paths))
        self.assert_retired()

    def test_native_delete_failure_keeps_precise_recovery_credentials(self):
        from app.modules.strm_recovery import recover_pending_paths

        for native_deleted in (True, False):
            with self.subTest(native_deleted=native_deleted):
                self.reset_case()
                selected, neighbor = self.seed(), self.seed("neighbor", "source-a")
                self.assertTrue(deletion.execute_delete(
                    self.payload(selected), deletion.prepare_delete(self.payload(selected)),
                )["ok"])
                if native_deleted:
                    selected.unlink()
                # 后续已有同步可收尾：没有新回调/扫描器，没有扩大到相邻文件。
                stats = {"cleaned": 0, "metadata_cleaned": 0}
                recover_pending_paths(
                    "guangya:source-a", str(self.media_root.parent), stats,
                    valid_ids={"neighbor"}, only_file_ids={"file-a"},
                )
                self.assertFalse(selected.exists())
                self.assertTrue(neighbor.is_file())
                self.assertEqual(stats["cleaned"], int(not native_deleted))
                self.assert_retired()

    def test_native_failure_recovery_does_not_delete_replacement_contents(self):
        from app.modules.strm_recovery import recover_pending_paths

        selected = self.seed()
        self.assertTrue(deletion.execute_delete(
            self.payload(selected), deletion.prepare_delete(self.payload(selected)),
        )["ok"])
        selected.write_bytes(b"new user contents")
        stats = {"cleaned": 0, "metadata_cleaned": 0}
        recover_pending_paths(
            "guangya:source-a", str(self.media_root.parent), stats,
            valid_ids=set(), only_file_ids={"file-a"},
        )
        self.assertEqual(selected.read_bytes(), b"new user contents")
        self.assertEqual(stats["recovery_pending"], 1)
        self.assert_retired("source-a")

    def test_per_item_rechecks_are_linear_in_batch_size(self):
        paths = [self.seed(f"file-{index}") for index in range(6)]
        payload = self.payload(*paths)
        expected = deletion.prepare_delete(payload)
        with patch.object(deletion, "list_strm_path_owners", wraps=deletion.list_strm_path_owners) as owners:
            result = deletion.execute_delete(payload, expected)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["completed"], len(paths))
        self.assertLessEqual(owners.call_count, 4 * len(paths), "每项不得重新扫描整批索引")
        self.assertEqual(self.provider.delete.call_count, len(paths))

    def test_api_requires_web_session_and_session_csrf(self):
        path = self.seed()
        payload = self.payload(path)
        anonymous, _ = self.web_client(login=False)
        response = anonymous.post("/api/jellyfin/delete", json=payload,
                                  headers={"X-CSRF-Token": "fake", "X-Emby-Token": "fake", "Authorization": "Bearer fake"})
        self.assertEqual(response.status_code, 401)
        client, headers = self.web_client()
        for supplied in ({}, {"X-CSRF-Token": "wrong-session-token"}):
            with self.subTest(headers=supplied):
                response = client.post("/api/jellyfin/delete", json=payload, headers=supplied)
                self.assertEqual(response.status_code, 403, response.text)
        self.manager.start_operation.assert_not_called()
        self.assert_no_write()
        self.assertEqual(client.post("/api/jellyfin/delete", json=payload, headers=headers).status_code, 202)

    def test_api_busy_and_invalid_payloads_never_run_the_delete_callback(self):
        path = self.seed()
        payload = self.payload(path)
        client, headers = self.web_client()
        invalid_cases = (({}, 400), (dict(payload, paths=[]), 400),
                         (dict(payload, fingerprints={}), 400),
                         (dict(payload, fingerprints={payload["paths"][0]: "sha256:" + "0" * 64}), 409))
        for invalid, status in invalid_cases:
            with self.subTest(payload=invalid):
                response = client.post("/api/jellyfin/delete", json=invalid, headers=headers)
                self.assertEqual(response.status_code, status, response.text)
                self.manager.start_operation.assert_not_called()
        self.manager.start_operation.return_value = {"ok": False, "error": "Writer busy"}
        response = client.post("/api/jellyfin/delete", json=payload, headers=headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"], "Writer busy")
        self.assertFalse(self.manager.start_operation.call_args.kwargs["queue_if_busy"])
        self.factory.assert_not_called()
        self.assert_no_write()

    def test_writer_callback_rechecks_identity_mapping_and_new_download_occupancy(self):
        client, headers = self.web_client()
        for change in ("bytes", "index", "mapping", "cloud", "download"):
            with self.subTest(change=change):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                response = client.post("/api/jellyfin/delete", json=payload, headers=headers)
                self.assertEqual(response.status_code, 202, response.text)
                callback = self.manager.start_operation.call_args.args[2]
                self.factory.assert_not_called()
                if change == "bytes":
                    path.write_bytes(b"replacement STRM")
                elif change == "index":
                    with db.get_conn() as conn:
                        conn.execute("UPDATE strm_index SET file_id='replacement-file'")
                elif change == "mapping":
                    self.values["JELLYFIN_PATH_MAPPINGS"] = "[]"
                elif change == "cloud":
                    self.change_node("file-a", etag="replacement-etag")
                else:
                    self.request_download(source_id="source-a")
                try:
                    result = callback()
                except DirectoryScrapePublicError:
                    pass
                else:
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["completed"], 0)
                self.assert_no_write()


    def test_local_or_cloud_changes_between_preflight_and_write_are_rechecked(self):
        for change in ("bytes-during-preflight", "cloud-before-write", "trash-before-write"):
            with self.subTest(change=change):
                self.reset_case()
                path = self.seed()
                payload = self.payload(path)
                expected = deletion.prepare_delete(payload)
                reads = 0

                def file_info(file_id, *, change=change, path=path):
                    nonlocal reads
                    if file_id == "file-a":
                        reads += 1
                        if change == "bytes-during-preflight" and reads == 1:
                            path.write_bytes(b"changed while checking cloud")
                        elif change == "cloud-before-write" and reads == 2:
                            self.change_node(file_id, etag="concurrent-etag")
                        elif change == "trash-before-write" and reads == 2:
                            self.listings["season-a"] = []
                    return self.nodes.get(file_id)

                self.provider.file_info.side_effect = file_info
                self.assert_rejected_execution(payload, expected)
                self.assertEqual(self.audits(), [])
                self.assertEqual(reads, 1 if change == "bytes-during-preflight" else 2)

    def test_later_item_changes_stop_remaining_writes_and_keep_partial_receipts(self):
        for change in ("bytes", "symlink", "cloud", "download", "source", "etag", "new_owner"):
            with self.subTest(change=change):
                self.reset_case()
                paths = [self.seed(f"file-{key}", f"source-{key}") for key in "abc"]
                payload = self.payload(*paths)
                expected = deletion.prepare_delete(payload)

                def delete(ids, *, change=change, paths=paths):
                    self.assertEqual(ids, ["file-a"], "不应写入变更对象或后续对象")
                    if change == "bytes":
                        paths[1].write_bytes(b"external replacement")
                    elif change == "symlink":
                        other = self.root / "replacement.strm"
                        other.write_bytes(paths[1].read_bytes())
                        paths[1].unlink()
                        paths[1].symlink_to(other)
                    elif change == "cloud":
                        self.change_node("file-b", etag="changed-after-first-delete")
                    elif change == "download":
                        self.request_download(source_id="source-b")
                    elif change == "new_owner":
                        self.seed("file-b", "source-c", "source-c/new-reference.strm")
                    else:
                        field, value = ("source", "guangya:source-c") if change == "source" else ("etag", "new-index-etag")
                        with db.get_conn() as conn:
                            conn.execute(f"UPDATE strm_index SET {field}=? WHERE file_id='file-b'", (value,))

                self.provider.delete.side_effect = delete
                result = deletion.execute_delete(payload, expected)
                self.assertFalse(result["ok"])
                self.assertEqual(result["completed"], 1)
                self.assertEqual([item["file_id"] for item in result["items"]], ["file-a"])
                self.provider.delete.assert_called_once_with(["file-a"])
                self.assert_retired("source-a")
                statuses = [(row["file_id"], row["status"]) for row in self.audits()]
                self.assertEqual(statuses, [("file-a", "success")] +
                                 ([("file-b", "blocked")] if change == "download" else []))


if __name__ == "__main__":
    unittest.main()
