from __future__ import annotations

import hashlib
import json
import unittest
from base64 import b64encode
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, PropertyMock, patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules import gcid_import
from app.modules.gcid_import import GCIDImportPreviewStore, PreviewBindingError
from app.modules.gcid_manifest import (
    FORMAT_NAME,
    FORMAT_VERSION,
    GCIDManifest,
    ManifestValidationError,
    normalize_manifest_v2,
    validate_manifest,
)
from app.modules.guangya_gcid import GCIDImportItemResult
from tests.support import IsolatedDatabaseTestCase


def _digest(payload: dict) -> str:
    canonical = {key: value for key, value in payload.items() if key != "integrity"}
    raw = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest(files: list[dict] | None = None, **overrides) -> dict:
    entries = files if files is not None else [
        {"path": "Movies/A.mkv", "size": 10, "gcid": "gcid-a"},
        {"path": "Movies/B.mkv", "size": 20, "gcid": "gcid-b"},
    ]
    payload = {
        "format": FORMAT_NAME,
        "version": 2,
        "generated_at": "2026-07-25T00:00:00+00:00",
        "source": {"provider": "guangya", "directory_name": "测试"},
        "file_count": len(entries),
        "total_size": sum(int(item.get("size", 0)) for item in entries),
        "files": entries,
    }
    payload.update(overrides)
    payload["integrity"] = {"algorithm": "sha256", "digest": _digest(payload)}
    return payload


class GCIDManifestV2Tests(unittest.TestCase):
    def test_canonical_sha256_normalizes_to_immutable_domain_objects(self):
        payload = _manifest()
        manifest = normalize_manifest_v2(payload)

        assert isinstance(manifest, GCIDManifest)
        self.assertEqual(FORMAT_VERSION, 2)
        self.assertEqual(manifest.file_count, 2)
        self.assertEqual(manifest.total_size, 30)
        self.assertEqual(manifest.files[0].path, "Movies/A.mkv")
        self.assertIsNone(manifest.files[0].md5)
        self.assertEqual(manifest.to_dict(), payload)
        with self.assertRaises(FrozenInstanceError):
            manifest.generated_at = "changed"  # type: ignore[misc]

    def test_optional_md5_is_covered_by_canonical_digest(self):
        files = _manifest()["files"]
        files[0]["md5"] = "abcd" * 8
        payload = _manifest(files)

        manifest = normalize_manifest_v2(payload)
        self.assertEqual(manifest.files[0].md5, "abcd" * 8)
        self.assertIsNone(manifest.files[1].md5)
        self.assertEqual(manifest.to_dict(), payload)

        files[0]["md5"] = "1234" * 8
        with self.assertRaisesRegex(ManifestValidationError, "完整性校验失败"):
            normalize_manifest_v2(payload)

    def test_present_md5_must_be_lowercase_32_digit_hex(self):
        for md5 in (None, "", 10**31, "a" * 31, "A" * 32, "g" * 32, "a" * 32 + "\n"):
            with self.subTest(md5=md5), self.assertRaisesRegex(ManifestValidationError, "md5"):
                normalize_manifest_v2(_manifest([
                    {"path": "A.mkv", "size": 1, "gcid": "gcid-a", "md5": md5},
                ]))

    def test_duplicate_normalized_paths_are_rejected(self):
        payload = _manifest([
            {"path": "Movies\\A.mkv", "size": 10, "gcid": "gcid-a"},
            {"path": "Movies/A.mkv", "size": 10, "gcid": "gcid-b"},
        ])
        with self.assertRaisesRegex(ManifestValidationError, "重复路径"):
            normalize_manifest_v2(payload)

    def test_parent_and_absolute_paths_are_rejected(self):
        for unsafe in ("../A.mkv", "Movies/../A.mkv", "/root/A.mkv", "C:/A.mkv"):
            with self.subTest(path=unsafe):
                with self.assertRaisesRegex(ManifestValidationError, "不安全路径"):
                    normalize_manifest_v2(_manifest([
                        {"path": unsafe, "size": 1, "gcid": "gcid-a"},
                    ]))

    def test_empty_gcid_is_rejected_for_import_v2(self):
        with self.assertRaisesRegex(ManifestValidationError, "GCID 不能为空"):
            normalize_manifest_v2(_manifest([
                {"path": "A.mkv", "size": 1, "gcid": ""},
            ]))

    def test_count_and_total_size_mismatch_are_rejected(self):
        with self.assertRaisesRegex(ManifestValidationError, "file_count"):
            normalize_manifest_v2(_manifest(file_count=99))
        with self.assertRaisesRegex(ManifestValidationError, "total_size"):
            normalize_manifest_v2(_manifest(total_size=99))

    def test_import_limit_is_ten_thousand_files(self):
        files = [
            {"path": f"A/{index}.mkv", "size": 1, "gcid": f"gcid-{index}"}
            for index in range(10_001)
        ]
        with self.assertRaisesRegex(ManifestValidationError, "10000"):
            normalize_manifest_v2(_manifest(files))

    def test_v1_and_tgto_formats_are_rejected_for_import(self):
        v1 = _manifest(version=1)
        tgto = {"version": 2, "files": [{"name": "A.mkv", "gcid": "a"}]}
        for payload in (v1, tgto):
            with self.subTest(payload=payload):
                with self.assertRaises(ManifestValidationError):
                    normalize_manifest_v2(payload)
                with self.assertRaises(ManifestValidationError):
                    validate_manifest(payload)

    def test_export_defaults_to_v2_and_validation_remains_available(self):
        listed = {
            "fileId": "a", "fileName": "A.mkv", "resType": 1, "fileSize": 10,
            "gcid": "gcid-a", "md5": "ABCD" * 8,
        }
        needs_detail = {
            "fileId": "b", "fileName": "B.mkv", "resType": 1, "fileSize": 20, "gcid": "gcid-b",
        }
        detail_md5 = "1234" * 8
        sdk = Mock()
        sdk.fs_files.return_value = {
            "msg": "success", "data": {"total": 2, "fileList": [listed, needs_detail]},
        }
        file_info = {
            **needs_detail, "fileMd5": b64encode(bytes.fromhex(detail_md5)).decode("ascii"),
        }
        sdk.fs_detail.return_value = {"msg": "success", "data": {"fileInfo": file_info}}
        client = GuangYaClient.__new__(GuangYaClient)

        with patch.object(GuangYaClient, "raw", new_callable=PropertyMock, return_value=sdk):
            payload = client.generate_gcid_json("root", "测试")

        sdk.fs_detail.assert_called_once_with("b")
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["files"], [
            {"path": "A.mkv", "size": 10, "gcid": "gcid-a", "md5": "abcd" * 8},
            {"path": "B.mkv", "size": 20, "gcid": "gcid-b", "md5": detail_md5},
        ])
        self.assertTrue(validate_manifest(payload)["valid"])
        self.assertTrue(validate_manifest(payload)["import_ready"])


class GCIDImportPreviewStoreTests(unittest.TestCase):
    def test_preview_is_bounded_to_owner_target_digest_and_ttl(self):
        now = [1000.0]
        store = GCIDImportPreviewStore(
            ttl_seconds=1800, max_entries=2, clock=lambda: now[0]
        )
        manifest = normalize_manifest_v2(_manifest())
        preview_id = store.create(manifest, target_dir_id="target-1", owner_id="user-1")

        snapshot = store.consume(
            preview_id,
            target_dir_id="target-1",
            owner_id="user-1",
            manifest_digest=manifest.digest,
        )
        self.assertEqual(snapshot.manifest_digest, manifest.digest)
        self.assertEqual(snapshot.target_dir_id, "target-1")
        with self.assertRaises(PreviewBindingError):
            store.consume(preview_id, target_dir_id="target-2", owner_id="user-1")
        with self.assertRaises(PreviewBindingError):
            store.consume(preview_id, target_dir_id="target-1", owner_id="user-2")
        now[0] += 1801
        with self.assertRaises(PreviewBindingError):
            store.consume(preview_id, target_dir_id="target-1", owner_id="user-1")

    def test_preview_capacity_evicts_oldest_snapshot(self):
        store = GCIDImportPreviewStore(ttl_seconds=1800, max_entries=2)
        manifest = normalize_manifest_v2(_manifest())
        first = store.create(manifest, target_dir_id="1", owner_id="u")
        store.create(manifest, target_dir_id="2", owner_id="u")
        store.create(manifest, target_dir_id="3", owner_id="u")
        with self.assertRaises(PreviewBindingError):
            store.consume(first, target_dir_id="1", owner_id="u")


class GCIDImportDatabaseTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM gcid_import_items")
            conn.execute("DELETE FROM gcid_import_tasks")

    def test_schema_persists_normalized_task_metadata_and_item_outcomes(self):
        manifest = normalize_manifest_v2(_manifest())
        task_id = db.create_gcid_import_task(
            operation_token="op-1",
            manifest_digest=manifest.digest,
            target_dir_id="target-1",
            file_count=manifest.file_count,
            total_size=manifest.total_size,
        )
        db.replace_gcid_import_items(task_id, [
            {
                "path": item.path,
                "size": item.size,
                "gcid": item.gcid,
                "status": "previewed",
            }
            for item in manifest.files
        ])

        task = db.get_gcid_import_task(task_id)
        items = db.list_gcid_import_items(task_id)
        assert task is not None
        self.assertEqual(task["status"], "previewed")
        self.assertEqual(task["manifest_digest"], manifest.digest)
        self.assertEqual([row["path"] for row in items], ["Movies/A.mkv", "Movies/B.mkv"])
        with db.get_conn() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(gcid_import_tasks)")
            }
        self.assertNotIn("raw_manifest", columns)
        self.assertNotIn("token", columns)

    def test_task_status_is_bounded_to_domain_states(self):
        task_id = db.create_gcid_import_task(
            operation_token="op-2",
            manifest_digest="a" * 64,
            target_dir_id="target-1",
            file_count=0,
            total_size=0,
        )
        for status in ("running", "success", "partial_success", "failed"):
            db.update_gcid_import_task(task_id, status=status)
            get_gcid_import_task_result = db.get_gcid_import_task(task_id)
            assert get_gcid_import_task_result is not None
            self.assertEqual(get_gcid_import_task_result["status"], status)
        with self.assertRaises(ValueError):
            db.update_gcid_import_task(task_id, status="unknown")

    def test_import_history_counts_committed_results_during_run_and_retry(self):
        account_key = "a" * 64
        files = [
            {"path": f"{name}.mkv", "size": 1, "gcid": f"gcid-{name}"}
            for name in "ABCD"
        ]
        files[2]["md5"] = "b" * 32
        outcomes = iter([
            GCIDImportItemResult(True),
            GCIDImportItemResult(False, error_code="not_matched"),
            GCIDImportItemResult(False, error_code="not_matched"),
            GCIDImportItemResult(False, error_code="rate_limited"),
            GCIDImportItemResult(True),
            GCIDImportItemResult(False, error_code="not_matched"),
            GCIDImportItemResult(True),
        ])
        visible = []
        stored = []

        def import_file(**_kwargs):
            task, = gcid_import.list_tasks()
            visible.append((task["status"], task["success_count"], task["failed_count"], task["can_retry"]))
            row = db.get_gcid_import_task(task["id"])
            assert row is not None
            stored.append((row["success_count"], row["failed_count"]))
            return next(outcomes)

        writer = Mock()
        writer.import_file.side_effect = import_file
        self.addCleanup(gcid_import.reset_runtime_state)
        with (
            patch.object(gcid_import, "_preview_account_key", return_value=account_key),
            patch.object(gcid_import, "_open_importer", side_effect=lambda: nullcontext((writer, account_key))),
            patch.object(gcid_import.notifier, "notify_gcid_import_started"),
            patch.object(gcid_import.notifier, "notify_gcid_import_finished"),
        ):
            preview = gcid_import.create_preview(
                _manifest(files), target_dir_id="target", owner_id="owner",
            )
            result, _ = gcid_import.run_preview(
                preview_id=preview["preview_id"], target_dir_id="target", owner_id="owner",
                manifest_digest=preview["manifest_digest"], operation_token="progress-run",
            )
            self.assertEqual((result["status"], result["success_count"], result["failed_count"]), (
                "partial_success", 1, 3,
            ))
            retried, _ = gcid_import.retry_task(result["id"], operation_token="progress-retry")

        self.assertEqual(visible, [
            ("running", 0, 0, False),
            ("running", 1, 0, False),
            ("running", 1, 1, False),
            ("running", 1, 2, False),
            ("running", 1, 0, False),
            ("running", 2, 0, False),
            ("running", 2, 1, False),
        ])
        self.assertEqual(stored, [(0, 0)] * 4 + [(1, 3)] * 3)
        self.assertEqual((retried["status"], retried["success_count"], retried["failed_count"]), (
            "partial_success", 3, 1,
        ))
        self.assertEqual({sample["path"]: sample["error"] for sample in result["failed_samples"]}, {
            "B.mkv": "清单缺少 MD5，光鸭仅按 GCID 未命中",
            "C.mkv": "光鸭未命中该文件，无法秒传",
            "D.mkv": "光鸭请求过于频繁，请稍后重试",
        })

    def test_import_history_keeps_previously_saved_failure_message(self):
        saved_error = "光鸭未命中该文件，无法秒传"
        task_id = db.create_gcid_import_task(
            operation_token="historical-miss", manifest_digest="a" * 64,
            target_dir_id="target", file_count=1, total_size=1,
            items=[{"path": "Old.mkv", "size": 1, "gcid": "old", "status": "failed", "error": saved_error}],
        )
        db.update_gcid_import_task(task_id, status="failed", failed_count=1)

        task, = gcid_import.list_tasks()
        self.assertEqual(task["failed_samples"][0]["error"], saved_error)
        self.assertEqual(db.list_gcid_import_items(task_id)[0]["error"], saved_error)


if __name__ == "__main__":
    unittest.main()
