"""本地媒体终态不得因重复执行被覆写或再次消费同路径的新文件。"""

from typing import cast
from app.modules.scraper import TMDBScraper
from pathlib import Path
from unittest.mock import patch

import pytest
from app.repositories import local_media as local_media_repo
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult
from tests.support import isolated_test_database
from tests.test_local_media_service import FakeScraper


@pytest.mark.parametrize("recreate", [False, True])
def test_completed_task_replay_preserves_result_and_new_source(tmp_path, recreate):
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        source_root = tmp_path / "incoming"
        target_root = tmp_path / "library"
        source_root.mkdir(); target_root.mkdir()
        incoming = source_root / "Movie.2026.mkv"
        incoming.write_bytes(b"first-media")
        source_id = local_media_repo.create_local_media_source(name="S", qb_profile="", qb_path_prefix="", local_root=str(source_root), owner="admin")
        local_media_repo.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
        service = LocalMediaService(scraper=cast(TMDBScraper, FakeScraper(MatchResult(tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0))))
        rules = OrganizeRules(region_split=False, year_split=False, naming_scope="both", conflict_strategy=3, emby_refresh=False, media_probe_enabled=False)
        with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
            inspection = service.inspect_source("admin", source_id, incoming)
            preview = service.preview("admin", inspection["inspection_id"], tmdb_id="1", media_type="movie")
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"],
                preview_digest=preview["preview_digest"],
            )
            assert local_media_repo.claim_local_media_task(task_id, owner="admin")
            first = service.execute_task("admin", task_id)
            assert first["status"] == "completed"
            target = Path(first["moved"][0])
            before = local_media_repo.get_local_media_task(task_id, owner="admin")
            if recreate:
                incoming.write_bytes(b"new-distinct-media")
            try:
                service.execute_task("admin", task_id)
            except LocalMediaServiceError:
                pass  # 显式拒绝与幂等返回均可，但必须保持已完成结果。
            after = local_media_repo.get_local_media_task(task_id, owner="admin")
            assert after is not None
            assert after.status == "completed", after
            assert before is not None
            assert after.version == before.version
            assert target.read_bytes() == b"first-media"
            if recreate:
                assert incoming.read_bytes() == b"new-distinct-media"
        service.close()
