"""整理后媒体规格后台补全回归测试。"""
from __future__ import annotations

from dataclasses import asdict
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizeRules
from app.modules.organize_probe_worker import OrganizeProbeWorker
from tests.support import IsolatedDatabaseTestCase


class _ProbeCompletionClient:
    def __init__(self, files: list[GuangYaFile]):
        self.files = {item.file_id: item for item in files}
        self.renames: list[tuple[str, str]] = []

    def file_info(self, file_id: str):
        return self.files.get(str(file_id))

    def list_dir(self, parent_id: str):
        return [item for item in self.files.values() if item.parent_id == str(parent_id)]

    def rename(self, file_id: str, new_name: str):
        current = self.files[str(file_id)]
        self.files[str(file_id)] = GuangYaFile(
            current.file_id, new_name, current.is_dir, current.size,
            current.etag, current.parent_id,
        )
        self.renames.append((str(file_id), str(new_name)))
        return True

    def close(self):
        return None


class OrganizeProbeWorkerTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_probe_queue")
            conn.execute("DELETE FROM organize_log_items")
            conn.execute("DELETE FROM organize_log")

    def _create_log(self) -> int:
        log_id = db.add_organize_log(
            "guangya",
            "incoming/筋肉人：完美超人始祖篇 (2024)(tmdb-236000)/Season 1",
            "动漫/筋肉人：完美超人始祖篇 (2024) {tmdb-236000}/Season 1/"
            "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.mp4",
            "video-15",
            "success",
            "236000",
            provider="tmdb",
            external_id="236000",
            source_dir_id="target",
            original_parent_id="incoming-parent",
            original_name="筋肉人：完美超人始祖篇 - S01E15 - Web-DL 0.0 Mbps.mp4",
            current_parent_id="target-parent",
            current_name="筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.mp4",
            target_parent_id="target-parent",
            media_type="tv",
            title="筋肉人：完美超人始祖篇",
            year="2024",
            season=1,
            episode=15,
            legacy_incomplete=False,
        )
        db.add_organize_log_items(log_id, [
            {
                "file_id": "video-15", "role": "video",
                "original_parent_id": "incoming-parent",
                "original_name": "筋肉人：完美超人始祖篇 - S01E15 - Web-DL 0.0 Mbps.mp4",
                "current_parent_id": "target-parent",
                "current_name": "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.mp4",
                "target_parent_id": "target-parent",
                "target_name": "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.mp4",
                "size": 1000, "etag": "video-etag", "status": "success",
            },
            {
                "file_id": "subtitle-15", "role": "subtitle",
                "original_parent_id": "incoming-parent",
                "original_name": "筋肉人：完美超人始祖篇 - S01E15 - Web-DL 0.0 Mbps.chs.srt",
                "current_parent_id": "target-parent",
                "current_name": "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.chs.srt",
                "target_parent_id": "target-parent",
                "target_name": "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.chs.srt",
                "size": 100, "etag": "subtitle-etag", "status": "success",
            },
        ])
        return log_id

    @staticmethod
    def _make_due() -> None:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00'"
            )

    def test_queue_is_persistent_and_recovers_running_job(self):
        log_id = self._create_log()
        job_id = db.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="动漫/示例/Season 1",
            rules=asdict(OrganizeRules(target_dir_id="target")), delay_seconds=130,
        )
        self._make_due()

        claimed = db.claim_due_organize_probe_jobs(owner="test", limit=1)
        self.assertEqual([item["id"] for item in claimed], [job_id])
        self.assertEqual(db.recover_stale_organize_probe_jobs(force=True), 1)
        self.assertEqual(db.count_organize_probe_jobs()["retry_wait"], 1)

    def test_successful_completion_renames_video_companion_and_triggers_incremental_strm(self):
        from app.modules.media_probe import MediaProfile

        log_id = self._create_log()
        db.enqueue_organize_probe_completion(
            log_id,
            source_id="target",
            rel_dir="动漫/筋肉人：完美超人始祖篇 (2024) {tmdb-236000}/Season 1",
            rules=asdict(OrganizeRules(target_dir_id="target", link_strm=True)),
            delay_seconds=130,
        )
        self._make_due()
        client = _ProbeCompletionClient([
            GuangYaFile(
                "video-15", "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.mp4",
                False, 1000, "video-etag", "target-parent",
            ),
            GuangYaFile(
                "subtitle-15", "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.chs.srt",
                False, 100, "subtitle-etag", "target-parent",
            ),
        ])
        worker = OrganizeProbeWorker()
        worker._client = client
        profile = MediaProfile(
            resolution="1080p", video_codec="H.264", fps="23.976fps",
            audio_codec="AAC", audio_channels="2.0", video_bitrate_bps=2_195_515,
        )

        with patch("app.modules.media_probe.probe_media_profile", return_value=profile), patch(
            "app.modules.organize.Organizer._post_organize_link"
        ) as linked:
            self.assertTrue(worker._process_one())

        expected = (
            "筋肉人：完美超人始祖篇.2024.S01E15-WEB-DL.1080p.H.264."
            "2.2Mbps.23.976fps.AAC.2.0.mp4"
        )
        self.assertEqual(client.files["video-15"].name, expected)
        self.assertEqual(
            client.files["subtitle-15"].name,
            expected.rsplit(".", 1)[0] + ".chs.srt",
        )
        row = db.get_organize_log(log_id)
        self.assertEqual(row["current_name"], expected)
        items = [dict(item) for item in db.list_organize_log_items(log_id)]
        self.assertEqual(items[0]["current_name"], expected)
        self.assertEqual(db.count_organize_probe_jobs()["completed"], 1)
        linked.assert_called_once()
        changes = linked.call_args.args[0]["strm_changes"]
        self.assertEqual({item["file_id"] for item in changes}, {"video-15", "subtitle-15"})

    def test_external_rename_cancels_job_without_overwriting(self):
        from app.modules.media_probe import MediaProfile

        log_id = self._create_log()
        db.enqueue_organize_probe_completion(
            log_id, source_id="target", rel_dir="动漫/示例/Season 1",
            rules=asdict(OrganizeRules(target_dir_id="target")), delay_seconds=130,
        )
        self._make_due()
        client = _ProbeCompletionClient([
            GuangYaFile(
                "video-15", "用户手动改名.mp4", False, 1000,
                "video-etag", "target-parent",
            ),
        ])
        worker = OrganizeProbeWorker()
        worker._client = client

        with patch(
            "app.modules.media_probe.probe_media_profile",
            return_value=MediaProfile(resolution="1080p"),
        ):
            self.assertTrue(worker._process_one())

        self.assertEqual(db.count_organize_probe_jobs()["cancelled"], 1)
        self.assertEqual(client.renames, [])
