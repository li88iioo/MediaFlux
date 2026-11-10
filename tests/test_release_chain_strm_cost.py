"""历史 STRM 校准只对候选旧副本验证新文件，不重复读全部已索引文件。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.repositories import strm as strm_repo
from app.clients.guangya import GuangYaFile
from app.modules import strm, strm_recovery
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database


def test_no_orphans_does_not_reread_all_indexed_pointers():
    with isolated_test_database(), tempfile.TemporaryDirectory() as root:
        for i in range(30):
            file = GuangYaFile(str(i), f"E{i}.mkv", False, 100, "e")
            path = strm.generate_strm(file, "Series", "http://play.invalid", root)
            strm_repo.upsert_strm_index("guangya:s", str(i), "e", 100, file.name, str(path), strm._content_fingerprint(path))
        stats = STRMScheduler._empty_stats()
        with patch.object(strm_recovery, "_pointer", wraps=strm_recovery._pointer) as read:
            strm_recovery.reconcile_historical_strm(root, "http://play.invalid", [{"id": "s"}], stats)
        assert stats["cleaned"] == 0
        assert not stats["clean_skipped"]
        assert read.call_count == 0, read.call_count


def test_orphan_needs_a_verified_real_replacement_even_with_matching_index():
    for changed in (False, True):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            file = GuangYaFile("video", "E1.mkv", False, 100, "e")
            path = strm.generate_strm(file, "New", "http://play.invalid", root)
            strm_repo.upsert_strm_index("guangya:s", "video", "e", 100, file.name, str(path), strm._content_fingerprint(path))
            orphan = Path(root) / strm.STRM_SUBDIR / "Old.strm"
            orphan.write_bytes(path.read_bytes())
            if changed:
                path.write_text("manual change", encoding="utf-8")
            else:
                path.unlink()
            stats = STRMScheduler._empty_stats()
            strm_recovery.reconcile_historical_strm(root, "http://play.invalid", [{"id": "s"}], stats)
            assert orphan.is_file()
            assert stats["clean_skipped"]
