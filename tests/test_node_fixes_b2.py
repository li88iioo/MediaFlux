"""B2：后台规格补全沿用前台的分片身份与统一命名。"""

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import unittest
from dataclasses import replace
from unittest.mock import patch

from app import database as db
from app.repositories import organize_probe as organize_probe_repo
from app.clients.guangya import GuangYaFile
from app.modules.media_probe import MediaProfile
from app.modules.nsfw import extract_nsfw_part_index
from app.modules.organize import (
    OrganizePlan,
    Organizer,
    OrganizeRules,
    organize_rules_snapshot,
)
from app.modules.organize_probe_worker import OrganizeProbeWorker
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_organize_probe_worker import _ProbeCompletionClient


class ProbePreservesMultipart(IsolatedDatabaseTestCase):
    def test_background_probe_preserves_initially_planned_part_marker(self):
        for provider, original_name, part in (
            (provider, name, part)
            for provider in ("metatube", "clean_title")
            for name, part in (
                ("ABC-123.CD1.mp4", 1),
                ("ABC-123.part2.mp4", 2),
                ("ABC-123.mp4", None),
            )
        ):
            with self.subTest(provider=provider, original_name=original_name):
                rules = OrganizeRules(target_dir_id="target", link_strm=False)
                match = MatchResult(
                    title="ABC-123",
                    media_type="movie",
                    confidence=1.0,
                    status="matched",
                    provider=provider,
                    external_id="ABC-123",
                )
                original = GuangYaFile(
                    f"video-{provider}-{part}",
                    original_name,
                    False,
                    1000,
                    "etag",
                    "incoming",
                )
                initial = OrganizePlan(
                    file_id=original.file_id,
                    original_name=original_name,
                    original_path="incoming",
                    original_parent_id="incoming",
                    match=match,
                    action="move",
                )
                organizer = Organizer(client=cast(GuangYaClient, object()), scraper=cast(TMDBScraper, object()))
                organizer._apply_media_profile_to_move_plan(
                    initial,
                    original,
                    rules,
                    match,
                    {"season": None, "episode": None, "part": part},
                    None,
                )
                self.assertEqual(extract_nsfw_part_index(initial.new_name), part)
                current = GuangYaFile(
                    original.file_id,
                    initial.new_name,
                    False,
                    1000,
                    "etag",
                    "target-parent-" + provider,
                )
                subtitle = GuangYaFile(
                    current.file_id + "-subtitle",
                    current.name.rsplit(".", 1)[0] + ".en.srt",
                    False,
                    10,
                    "subtitle-etag",
                    current.parent_id,
                )
                client = _ProbeCompletionClient([current, subtitle])
                log_id = db.add_organize_log(
                    "guangya",
                    "incoming",
                    "library/" + initial.new_name,
                    original.file_id,
                    "success",
                    "",
                    provider=provider,
                    external_id="ABC-123",
                    source_dir_id="incoming",
                    original_parent_id="incoming",
                    original_name=original_name,
                    current_parent_id=current.parent_id,
                    current_name=current.name,
                    target_parent_id=current.parent_id,
                    media_type="movie",
                    title="ABC-123",
                    year="",
                    legacy_incomplete=False,
                )
                db.add_organize_log_items(
                    log_id,
                    [
                        {
                            "file_id": current.file_id,
                            "role": "video",
                            "original_parent_id": "incoming",
                            "original_name": original_name,
                            "current_parent_id": current.parent_id,
                            "current_name": current.name,
                            "target_parent_id": current.parent_id,
                            "target_name": current.name,
                            "size": 1000,
                            "etag": "etag",
                            "status": "success",
                        },
                        {
                            "file_id": subtitle.file_id,
                            "role": "subtitle",
                            "original_parent_id": "incoming",
                            "original_name": original_name.rsplit(".", 1)[0]
                            + ".en.srt",
                            "current_parent_id": subtitle.parent_id,
                            "current_name": subtitle.name,
                            "target_parent_id": subtitle.parent_id,
                            "target_name": subtitle.name,
                            "size": 10,
                            "etag": subtitle.etag,
                            "status": "success",
                        },
                    ],
                )
                organize_probe_repo.enqueue_organize_probe_completion(
                    log_id,
                    source_id="target",
                    rel_dir="library",
                    rules=organize_rules_snapshot(rules),
                    delay_seconds=130,
                )
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00'"
                    )
                worker = OrganizeProbeWorker()
                worker._client = cast(GuangYaClient, client)
                profile = MediaProfile(resolution="1080p", video_codec="H264")
                with patch(
                    "app.modules.media_probe.probe_media_profile", return_value=profile
                ):
                    self.assertTrue(worker._process_one())
                file_info_result = client.file_info(current.file_id)
                assert file_info_result is not None
                after = file_info_result.name
                job = None
                with db.get_conn() as conn:
                    job = dict(
                        conn.execute(
                            "SELECT * FROM organize_probe_queue WHERE organize_log_id=?",
                            (log_id,),
                        ).fetchone()
                    )
                print(
                    {
                        "provider": provider,
                        "initial_name": initial.new_name,
                        "after_probe": after,
                        "queue_status": job["status"],
                    }
                )
                self.assertEqual(extract_nsfw_part_index(after), part)
                expected = replace(initial)
                organizer._apply_media_profile_to_move_plan(
                    expected,
                    current,
                    rules,
                    match,
                    {"season": None, "episode": None, "part": part},
                    profile,
                )
                self.assertEqual(after, expected.new_name)
                self.assertEqual(expected.multipart_index, part)
                file_info_result_2 = client.file_info(subtitle.file_id)
                assert file_info_result_2 is not None
                self.assertEqual(
                    file_info_result_2.name,
                    after.rsplit(".", 1)[0] + ".en.srt",
                )
                self.assertEqual(job["status"], "completed")
                get_organize_log_result = db.get_organize_log(log_id)
                assert get_organize_log_result is not None
                self.assertEqual(get_organize_log_result["current_name"], after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
