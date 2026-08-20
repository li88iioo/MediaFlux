from __future__ import annotations

import json
import re
import tempfile
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
    # Starlette 0.52.x 尚未导出 StarletteDeprecationWarning；
    # 新版该类型仍继承 UserWarning，配合窄消息匹配可跨版本过滤。
    category=UserWarning,
)
from fastapi.testclient import TestClient

from app import database as db
from tests.support import IsolatedDatabaseTestCase


def _insert_index(source: str, path: Path) -> int:
    db.upsert_strm_index(
        source,
        f"file-{uuid.uuid4().hex}",
        "etag",
        1,
        path.name,
        str(path),
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM strm_index WHERE source=? AND strm_path=?",
            (source, str(path)),
        ).fetchone()
    return int(row["id"])


def _config_values(strm_root: Path, configured_source_ids: list[str] | None = None):
    configured = [
        {"id": source_id, "name": f"真实源 {index + 1}"}
        for index, source_id in enumerate(configured_source_ids or [])
    ]
    values = {
        "GY_STRM_SOURCE_DIRS": json.dumps(configured),
        "STRM_ROOT": str(strm_root),
    }
    return patch(
        "app.config.get",
        side_effect=lambda key, default="": values.get(key, default),
    )


def _csrf_token(response) -> str:
    match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', response.text)
    if not match:
        raise AssertionError("页面未输出 CSRF Token")
    return match.group(1)


@contextmanager
def _api_client(strm_root: Path, *, login: bool = True):
    from app.main import create_app

    values = {
        "APP_ENV": "development",
        "MEDIAFLUX_INITIALIZED": "1",
        "WEB_SECRET_KEY": "strm-index-diagnostic-test-secret",
        "ENV_WEB_PASSPORT": "diagnostic-user",
        "ENV_WEB_PASSWORD": "diagnostic-password",
        "GY_STRM_SOURCE_DIRS": "[]",
        "STRM_ROOT": str(strm_root),
    }
    with patch(
        "app.config.get",
        side_effect=lambda key, default="": values.get(key, default),
    ), TestClient(create_app(), raise_server_exceptions=False) as client:
        csrf = ""
        if login:
            login_page = client.get("/login")
            login_response = client.post(
                "/login",
                data={
                    "csrf_token": _csrf_token(login_page),
                    "username": values["ENV_WEB_PASSPORT"],
                    "password": values["ENV_WEB_PASSWORD"],
                },
                follow_redirects=False,
            )
            if login_response.status_code != 302:
                raise AssertionError(login_response.text)
            csrf = _csrf_token(client.get("/guangya/strm"))
        yield client, csrf


class StrmIndexDiagnosticClassifierTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")

    def test_only_missing_unconfigured_tmp_uuid_source_is_confirmed_test_artifact(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-diagnostic-") as root:
            temp_root = Path(root)
            configured_source_id = str(uuid.uuid4())
            missing_test_source_id = str(uuid.uuid4())
            existing_uuid_source_id = str(uuid.uuid4())

            real_row_id = _insert_index(
                f"guangya:{configured_source_id}",
                temp_root / "configured-missing.strm",
            )
            confirmed_row_id = _insert_index(
                f"guangya:{missing_test_source_id}",
                temp_root / "isolated-missing.strm",
            )
            existing_path = temp_root / "existing.strm"
            existing_path.write_text("http://example.invalid/play", encoding="utf-8")
            existing_row_id = _insert_index(
                f"guangya-meta:{existing_uuid_source_id}",
                existing_path,
            )
            non_uuid_row_id = _insert_index(
                "guangya:manual-source",
                temp_root / "manual-missing.strm",
            )

            configured_sources = json.dumps([
                {"id": configured_source_id, "name": "真实配置源"},
            ])
            values = {
                "GY_STRM_SOURCE_DIRS": configured_sources,
            }
            with patch(
                "app.config.get",
                side_effect=lambda key, default="": values.get(key, default),
            ):
                result = db.list_strm_index_diagnostics(str(temp_root))

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["missing"], 3)
        self.assertEqual(result["real_source"], 1)
        self.assertEqual(result["configured_source_count"], 1)
        self.assertEqual(result["confirmed_test_artifact"], 1)
        self.assertEqual(result["confirmed_test_artifact_ids"], [confirmed_row_id])
        self.assertNotIn(real_row_id, result["confirmed_test_artifact_ids"])
        self.assertNotIn(existing_row_id, result["confirmed_test_artifact_ids"])
        self.assertNotIn(non_uuid_row_id, result["confirmed_test_artifact_ids"])
        self.assertEqual(result["video"]["total"], 3)
        self.assertEqual(result["video"]["existing"], 0)
        self.assertEqual(result["video"]["missing"], 3)
        self.assertEqual(result["metadata"]["total"], 1)
        self.assertEqual(result["metadata"]["existing"], 1)
        self.assertEqual(result["metadata"]["missing"], 0)
        self.assertEqual(result["other"]["total"], 0)

    def test_classification_by_kind_video_metadata_other(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-kind-") as root:
            temp_root = Path(root)
            v_exist = temp_root / "v_exist.strm"
            v_exist.write_text("http://example/v1", encoding="utf-8")
            _insert_index("guangya:dir1", v_exist)
            _insert_index("guangya:dir1", temp_root / "v_miss.strm")

            m_exist = temp_root / "m_exist.nfo"
            m_exist.write_text("<movie/>", encoding="utf-8")
            _insert_index("guangya-meta:dir1", m_exist)
            _insert_index("guangya-meta:dir1", temp_root / "m_miss.nfo")

            o_miss = temp_root / "o_miss.strm"
            _insert_index("custom_other:dir1", o_miss)

            with _config_values(temp_root, ["dir1"]):
                result = db.list_strm_index_diagnostics(str(temp_root))

            self.assertEqual(result["total"], 5)
            self.assertEqual(result["video"]["total"], 2)
            self.assertEqual(result["video"]["existing"], 1)
            self.assertEqual(result["video"]["missing"], 1)
            self.assertEqual(result["metadata"]["total"], 2)
            self.assertEqual(result["metadata"]["existing"], 1)
            self.assertEqual(result["metadata"]["missing"], 1)
            self.assertEqual(result["other"]["total"], 1)
            self.assertEqual(result["other"]["missing"], 1)
            self.assertEqual(result["configured_source_count"], 1)
            self.assertEqual(result["real_source"], 4)



class StrmIndexCleanupTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")

    def test_cleanup_deletes_a_confirmed_test_index_without_touching_filesystem(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-cleanup-") as root:
            temp_root = Path(root)
            row_id = _insert_index(
                f"guangya:{uuid.uuid4()}", temp_root / "missing.strm"
            )
            with _config_values(temp_root), patch.object(
                Path, "unlink", side_effect=AssertionError("不应删除文件")
            ):
                deleted = db.delete_confirmed_test_strm_indexes([row_id])

            self.assertEqual(deleted, 1)
            with db.get_conn() as conn:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS count FROM strm_index WHERE id=?", (row_id,)
                ).fetchone()["count"]
            self.assertEqual(remaining, 0)

    def test_cleanup_rejects_mixed_ids_and_rolls_back_every_deletion(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-cleanup-") as root:
            temp_root = Path(root)
            confirmed_id = _insert_index(
                f"guangya:{uuid.uuid4()}", temp_root / "confirmed-missing.strm"
            )
            unsafe_id = _insert_index(
                "guangya:manual-source", temp_root / "manual-missing.strm"
            )
            with _config_values(temp_root):
                with self.assertRaisesRegex(ValueError, "确认测试索引"):
                    db.delete_confirmed_test_strm_indexes([confirmed_id, unsafe_id])

            with db.get_conn() as conn:
                remaining_ids = {
                    int(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM strm_index WHERE id IN (?,?)",
                        (confirmed_id, unsafe_id),
                    )
                }
            self.assertEqual(remaining_ids, {confirmed_id, unsafe_id})

    def test_cleanup_reclassifies_each_row_when_missing_file_now_exists(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-cleanup-") as root:
            temp_root = Path(root)
            target = temp_root / "appeared.strm"
            row_id = _insert_index(f"guangya:{uuid.uuid4()}", target)
            with _config_values(temp_root):
                diagnostics = db.list_strm_index_diagnostics(str(temp_root))
                self.assertEqual(diagnostics["confirmed_test_artifact_ids"], [row_id])
                target.write_text("do-not-delete", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "确认测试索引"):
                    db.delete_confirmed_test_strm_indexes([row_id])
                self.assertTrue(target.exists())

            with db.get_conn() as conn:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS count FROM strm_index WHERE id=?", (row_id,)
                ).fetchone()["count"]
            self.assertEqual(remaining, 1)


class StrmIndexDiagnosticApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")

    def test_diagnostics_requires_login(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-api-") as root:
            with _api_client(Path(root), login=False) as (client, _):
                response = client.get("/api/strm/index-diagnostics")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "unauthorized")

    def test_cleanup_requires_existing_csrf_protection(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-api-") as root:
            with _api_client(Path(root)) as (client, _):
                response = client.post(
                    "/api/strm/index-diagnostics/cleanup",
                    json={"confirm": "CLEAN TEST INDEX", "ids": [1]},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "CSRF token invalid")

    def test_diagnostics_returns_only_safe_counts_and_confirmed_ids(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-api-") as root:
            temp_root = Path(root)
            secret = "signed-secret-credential"
            row_id = _insert_index(
                f"guangya:{uuid.uuid4()}-{secret}",
                temp_root / f"missing-{secret}.strm",
            )
            with _api_client(temp_root) as (client, _):
                response = client.get("/api/strm/index-diagnostics")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["confirmed_test_artifact_ids"], [row_id])
        self.assertNotIn(secret, response.text)
        self.assertNotIn(str(temp_root), response.text)
        self.assertEqual(
            set(response.json()),
            {
                "total", "existing", "missing", "real_source",
                "confirmed_test_artifact", "confirmed_test_artifact_ids",
                "configured_source_count", "video", "metadata", "other",
                "metadata_queue",
            },
        )

    def test_cleanup_rejects_invalid_confirmation_and_id_shapes(self):
        invalid_payloads = [
            {},
            {"confirm": "clean test index", "ids": [1]},
            {"confirm": "CLEAN TEST INDEX", "ids": []},
            {"confirm": "CLEAN TEST INDEX", "ids": "1"},
            {"confirm": "CLEAN TEST INDEX", "ids": [True]},
            {"confirm": "CLEAN TEST INDEX", "ids": ["1"]},
            {"confirm": "CLEAN TEST INDEX", "ids": list(range(1, 102))},
        ]
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-api-") as root:
            with _api_client(Path(root)) as (client, csrf):
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.post(
                            "/api/strm/index-diagnostics/cleanup",
                            headers={"X-CSRF-Token": csrf},
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertEqual(set(response.json()), {"error"})

    def test_cleanup_deletes_confirmed_ids_and_returns_refreshed_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-api-") as root:
            temp_root = Path(root)
            row_id = _insert_index(
                f"guangya:{uuid.uuid4()}", temp_root / "missing.strm"
            )
            with _api_client(temp_root) as (client, csrf):
                response = client.post(
                    "/api/strm/index-diagnostics/cleanup",
                    headers={"X-CSRF-Token": csrf},
                    json={"confirm": "CLEAN TEST INDEX", "ids": [row_id]},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertEqual(response.json()["diagnostics"]["total"], 0)
        self.assertEqual(response.json()["diagnostics"]["confirmed_test_artifact_ids"], [])


class StrmIndexDiagnosticUiTests(IsolatedDatabaseTestCase):
    def test_page_reserves_a_compact_diagnostic_result_panel(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-ui-") as root:
            with _api_client(Path(root)) as (client, _):
                response = client.get("/guangya/strm")

        self.assertEqual(response.status_code, 200, response.text)
        html = response.text
        self.assertIn('id="strmIndexDiagnosticCard"', html)
        self.assertIn('id="strmIndexDiagnosticResult"', html)
        self.assertIn('min-height: 96px', html)
        self.assertIn('class="strm-index-diagnostic-cleanup-slot"', html)
        self.assertIn('id="cleanupStrmTestIndexesBtn"', html)
        self.assertIn(
            'class="jump-btn danger strm-index-diagnostic-cleanup is-unavailable"', html
        )
        self.assertRegex(
            html, r'id="cleanupStrmTestIndexesBtn"[^>]*aria-hidden="true"'
        )
        self.assertIn('STRM 已落盘', html)
        self.assertIn('STRM 文件缺失', html)
        self.assertNotIn('本地视频文件已落盘', html)
        self.assertNotIn("云端文件匹配正常", html)
        self.assertNotIn("云端对应文件已缺失", html)
        self.assertIn('data-strm-diagnostic="video.total"', html)
        self.assertIn('data-strm-diagnostic="metadata.total"', html)
        self.assertIn('data-strm-diagnostic="configured_source_count"', html)

    def test_page_requires_explicit_typed_cleanup_and_keeps_diagnostic_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="mediaflux-strm-ui-") as root:
            with _api_client(Path(root)) as (client, _):
                response = client.get("/guangya/strm")

        html = response.text + Path("app/static/js/guangya-strm.js").read_text(encoding="utf-8")
        self.assertIn("let diagnosticSnapshot=null", html)
        self.assertIn("verifyText:'CLEAN TEST INDEX'", html)
        self.assertIn("confirm:'CLEAN TEST INDEX'", html)
        self.assertIn("'/api/strm/index-diagnostics/cleanup'", html)
        self.assertIn("diagnosticSnapshot.confirmed_test_artifact_ids", html)
        self.assertIn("当前没有可清理的测试索引", html)



if __name__ == "__main__":
    import unittest

    unittest.main()
