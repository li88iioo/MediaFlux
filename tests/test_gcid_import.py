from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.gcid_import import GCIDImportPreviewStore, PreviewBindingError
from app.modules.gcid_manifest import (
    FORMAT_NAME,
    FORMAT_VERSION,
    GCIDManifest,
    ManifestValidationError,
    export_manifest,
    normalize_manifest_v2,
    validate_manifest,
)
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
        manifest = normalize_manifest_v2(_manifest())

        self.assertIsInstance(manifest, GCIDManifest)
        self.assertEqual(FORMAT_VERSION, 2)
        self.assertEqual(manifest.file_count, 2)
        self.assertEqual(manifest.total_size, 30)
        self.assertEqual(manifest.files[0].path, "Movies/A.mkv")
        self.assertEqual(manifest.digest, _digest(manifest.to_dict()))
        with self.assertRaises(FrozenInstanceError):
            manifest.generated_at = "changed"  # type: ignore[misc]

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
        tree = {
            "root": [GuangYaFile("a", "A.mkv", False, 10, "gcid-a", "root")],
        }
        client = type("Client", (), {
            "list_dir": lambda inner, file_id: tree[file_id],
            "file_info": lambda inner, file_id: None,
        })()

        payload = export_manifest(client, "root", "测试")

        self.assertEqual(payload["version"], 2)
        self.assertEqual(set(payload["files"][0]), {"path", "size", "gcid"})
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
            self.assertEqual(db.get_gcid_import_task(task_id)["status"], status)
        with self.assertRaises(ValueError):
            db.update_gcid_import_task(task_id, status="unknown")


if __name__ == "__main__":
    unittest.main()
