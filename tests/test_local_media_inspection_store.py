from __future__ import annotations

import time
import unittest
from pathlib import Path

from app.modules.local_media_service import (
    LocalMediaServiceError,
    _Inspection,
    _InspectionStore,
)
from app.modules.local_storage import LocalFileSnapshot


def _record(
    path: str,
    *,
    owner: str = "admin",
    source_id: int = 1,
    snapshots: int = 1,
    created_at: float | None = None,
) -> _Inspection:
    selected = Path(path)
    values = [
        LocalFileSnapshot(
            path=selected / f"item-{index}.mkv",
            relative_path=f"item-{index}.mkv",
            size=1,
            mtime_ns=index + 1,
            device=1,
            inode=index + 1,
            role="video",
        )
        for index in range(snapshots)
    ]
    return _Inspection(
        owner=owner,
        source_id=source_id,
        root=Path("/library"),
        selected_path=selected,
        snapshots=values,
        digest=f"digest-{path}-{snapshots}",
        created_at=time.time() if created_at is None else created_at,
    )


class InspectionStoreTests(unittest.TestCase):
    def test_same_owner_source_and_path_replaces_previous_snapshot(self):
        store = _InspectionStore(max_records=10, max_snapshot_entries=10)
        first = store.put(_record("/library/movie"))
        second = store.put(_record("/library/movie", snapshots=2))

        with self.assertRaisesRegex(LocalMediaServiceError, "不存在或已过期"):
            store.get("admin", first)
        self.assertEqual(len(store.get("admin", second).snapshots), 2)
        self.assertEqual(len(store._records), 1)
        self.assertEqual(store._snapshot_entries, 2)

    def test_lru_enforces_record_and_total_snapshot_bounds(self):
        store = _InspectionStore(max_records=3, max_snapshot_entries=4)
        first = store.put(_record("/library/a", snapshots=2))
        second = store.put(_record("/library/b", snapshots=2))
        # 访问 first，使 second 成为 LRU；第三条写入后按总条目数淘汰 second。
        store.get("admin", first)
        third = store.put(_record("/library/c", snapshots=2))

        with self.assertRaisesRegex(LocalMediaServiceError, "不存在或已过期"):
            store.get("admin", second)
        self.assertIsNotNone(store.get("admin", first))
        self.assertIsNotNone(store.get("admin", third))
        self.assertLessEqual(len(store._records), 3)
        self.assertLessEqual(store._snapshot_entries, 4)

    def test_consume_removes_record_and_updates_snapshot_budget(self):
        store = _InspectionStore(max_records=10, max_snapshot_entries=10)
        inspection_id = store.put(_record("/library/movie", snapshots=3))

        consumed = store.consume("admin", inspection_id)

        self.assertEqual(len(consumed.snapshots), 3)
        self.assertEqual(store._snapshot_entries, 0)
        with self.assertRaisesRegex(LocalMediaServiceError, "不存在或已过期"):
            store.get("admin", inspection_id)

    def test_expired_records_are_removed_from_all_indexes(self):
        store = _InspectionStore(
            ttl_seconds=1, max_records=10, max_snapshot_entries=10,
        )
        expired = store.put(
            _record("/library/expired", created_at=time.time() - 10)
        )
        current = store.put(_record("/library/current"))

        with self.assertRaisesRegex(LocalMediaServiceError, "不存在或已过期"):
            store.get("admin", expired)
        self.assertIsNotNone(store.get("admin", current))
        self.assertEqual(len(store._scope_ids), 1)


if __name__ == "__main__":
    unittest.main()
