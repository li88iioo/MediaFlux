"""本轮媒体链路业务验收：真实临时文件/SQLite/安装器/outbox，provider 离线。"""

from __future__ import annotations

import tests  # noqa: F401 -- 应用导入前隔离 DB/config/log/cache
import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from app import database as db
from app.clients.guangya import GuangYaFile
from app.clients.jellyfin import JellyfinClient
from app.modules import media_probe, strm, strm_metadata_worker as metadata
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.modules import media_refresh_coordinator as refresh
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult, extract_recognition_context
from app.repositories import media_refresh_queue as queue
from tests.support import isolated_test_database, release_parse_result


@pytest.fixture(autouse=True)
def no_network():
    with (
        patch(
            "socket.socket.connect",
            side_effect=AssertionError("real network forbidden"),
        ),
        patch(
            "socket.create_connection",
            side_effect=AssertionError("real network forbidden"),
        ),
    ):
        yield


class Catalog:
    supports_parent_path = True

    def match(self, filename, parent_path="", *, media_type_hint=""):
        return MatchResult(
            tmdb_id="88",
            title="Audit Series",
            year="2026",
            media_type="tv",
            confidence=1.0,
            status="matched",
        )

    def match_from_tmdb(self, tmdb_id, media_type):
        return replace(self.match(""), tmdb_id=tmdb_id, media_type=media_type)

    def parse_media(self, filename, parent_path="", match=None):
        context = extract_recognition_context(filename, parent_path)
        return release_parse_result(
            dict(
                season=context.season,
                episode=context.episode,
                title="Audit Series",
                year="2026",
                type="tv",
            ),
            filename=filename,
            parent_path=parent_path,
        )

    def get_detail(self, tmdb_id, media_type, *, force_refresh=False):
        return {
            "genres": [{"id": 18}],
            "origin_country": ["US"],
            "first_air_date": "2026-01-01",
            "number_of_seasons": 1,
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }

    def get_tv_season_detail(self, tmdb_id, season):
        return {
            "episodes": [
                {"episode_number": n, "name": f"Episode {n}"} for n in range(1, 13)
            ]
        }

    def search_candidates(self, *args, **kwargs):
        return []


class Response:
    def __init__(self, payload=None, body=b""):
        self.payload, self.body = payload, body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), 7):
            yield self.body[start : start + 7]


class MediaEndpoint:
    """替换 HTTP transport 而非媒体刷新规划，保留真实客户端作用域/ID解析。"""

    def __init__(self, root):
        self.root = str(root)
        self.posts = []
        self.fail = False

    def get(self, url, **kwargs):
        path = urlsplit(url).path
        if path == "/Library/VirtualFolders":
            return Response(
                [
                    {
                        "ItemId": "audit-library",
                        "Name": "审查库",
                        "Locations": [self.root],
                        "CollectionType": "tvshows",
                    }
                ]
            )
        if path == "/Items":
            return Response({"Items": [], "TotalRecordCount": 0})
        raise AssertionError(f"unexpected media GET {path}")

    def post(self, url, **kwargs):
        path = urlsplit(url).path
        assert path == "/Items/audit-library/Refresh", "不得触发全库 fallback"
        if self.fail:
            raise OSError("media endpoint offline")
        self.posts.append(path)
        return Response()

    def close(self):
        pass

    def client(self, provider):
        assert provider == "jellyfin"
        client = JellyfinClient("http://media.invalid", "offline-test-key")
        client._session.close()
        client._session = self
        return client


def drain_refresh(endpoint, *, owner="audit-consumer", now_epoch=None):
    worker = MediaRefreshCoordinator()
    worker._owner = owner
    claimed = queue.claim_due_media_refreshes(
        owner=owner, force=True, now_epoch=now_epoch
    )
    with patch.object(worker, "_client_for", side_effect=endpoint.client):
        for group in claimed:
            worker._process_group(group)
    return worker, claimed


class CloudFiles:
    """离线对象读取器，元数据 body 来自真实临时文件，保留分页批量校验。"""

    def __init__(self, root, count=4):
        self.files = {}
        self.paths = {}
        self.info_calls = 0
        self.list_calls = 0
        self.download_calls = 0
        root.mkdir()
        for n in range(1, count + 1):
            for extension in ("mkv", "nfo"):
                file_id = f"{extension}-{n}"
                filename = f"Audit.Series.S01E{n:02d}.{extension}"
                path = root / filename
                path.write_bytes(
                    f"<episode>{n}</episode>".encode()
                    if extension == "nfo"
                    else b"video-fixture" * n
                )
                self.files[file_id] = GuangYaFile(
                    file_id,
                    filename,
                    False,
                    path.stat().st_size,
                    f"etag-{file_id}",
                    "season",
                )
                self.paths[file_id] = path

    def file_info(self, file_id):
        self.info_calls += 1
        file = self.files.get(file_id)
        return replace(file) if file else None

    def iter_dir(self, parent, **kwargs):
        self.list_calls += 1
        return iter(
            replace(file) for file in self.files.values() if file.parent_id == parent
        )

    def get_download_url(self, file_id, **kwargs):
        return f"http://metadata.invalid/{file_id}"

    def download(self, url, **kwargs):
        self.download_calls += 1
        return Response(body=self.paths[urlsplit(url).path.lstrip("/")].read_bytes())

    def changes(self):
        return [
            dict(
                source_id="audit",
                kind="video" if file.name.endswith("mkv") else "metadata",
                action="upsert",
                file_id=file.file_id,
                name=file.name,
                etag=file.etag,
                size=file.size,
                parent_id=file.parent_id,
                rel_dir="Audit Series/Season 01",
            )
            for file in self.files.values()
        ]


@contextmanager
def cloud_runtime(cloud, root):
    with (
        patch.object(metadata, "get_bool", return_value=True),
        patch.object(
            metadata,
            "get",
            side_effect=lambda k, d="": str(root) if k == "STRM_ROOT" else d,
        ),
        patch.object(strm.requests, "get", side_effect=cloud.download),
        patch.object(refresh, "_configured_provider_names", return_value=("jellyfin",)),
    ):
        yield


def generate_batch(cloud, root, should_stop=None):
    return strm.sync_strm_incremental(
        "audit",
        cloud.changes(),
        "http://play.invalid",
        str(root),
        client=cloud,
        metadata_exts={"nfo"},
        should_stop=should_stop,
    )


def test_normal_local_recognition_move_probe_and_refresh(tmp_path):
    with isolated_test_database():
        source, target = tmp_path / "incoming", tmp_path / "library"
        source.mkdir()
        target.mkdir()
        contents = {}
        for n in (1, 2):
            for ext in ("mkv", "srt"):
                name = f"Audit.Series.S01E{n:02d}.{ext}"
                contents[name] = f"{ext}-episode-{n}".encode()
                (source / name).write_bytes(contents[name])
        source_id = db.create_local_media_source(
            "audit", "", "", str(source), media_type="tv"
        )
        db.upsert_local_library_target(
            source_id,
            "tv",
            str(target),
            provider="jellyfin",
            library_id="audit-library",
            library_name="审查库",
        )
        service = LocalMediaService(scraper=Catalog())
        ffprobe_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                        }
                    ]
                }
            ),
            "",
        )
        try:
            inspection = service.inspect_source("admin", source_id, source)
            rules = service._serialize_rules_snapshot(
                OrganizeRules(small_file_mb=0, clean_empty=False)
            )
            task_id = service.create_manual_task(
                "admin",
                inspection["inspection_id"],
                tmdb_id="88",
                media_type="tv",
                rules_snapshot=rules,
            )
            with patch.object(
                media_probe, "_run_ffprobe", return_value=ffprobe_result
            ) as probe:
                result = service.execute_task("admin", task_id)
            assert result["status"] == "completed", result
            moved = [Path(p) for p in result["moved"]]
            assert len(moved) == 4
            assert sorted(p.read_bytes() for p in moved) == sorted(contents.values())
            assert all("Audit Series" in str(p) for p in moved)
            assert all("1080p" in p.name for p in moved if p.suffix == ".mkv")
            assert list(source.iterdir()) == []
            assert probe.call_count == 2
            assert len(db.list_local_media_operation_steps(task_id)) == 4
            assert db.get_local_media_task(task_id).status == "completed"
            assert queue.media_refresh_queue_status()["paths"] == 1
            endpoint = MediaEndpoint(target)
            worker, claimed = drain_refresh(endpoint)
            assert worker._completed_session == 1
            assert len(claimed) == len(endpoint.posts) == 1
            assert queue.media_refresh_queue_status()["paths"] == 0
            with pytest.raises(LocalMediaServiceError, match="已完成"):
                service.execute_task("admin", task_id)
            assert sorted(p.read_bytes() for p in moved) == sorted(contents.values())
        finally:
            service.close()


def test_batch_cloud_strm_metadata_outbox_and_media_service(tmp_path):
    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud")
        root = tmp_path / "strm"
        root.mkdir()
        worker = metadata.STRMMetadataWorker()
        worker._client = cloud
        # 保留真实批量 flush 门槛，以便观察完成事务产生的真实 outbox。
        worker._last_refresh_at = metadata.time.monotonic()
        with cloud_runtime(cloud, root):
            result = generate_batch(cloud, root)
            assert result["generated"] == 4 and result["metadata_queued"] == 4, result
            assert cloud.list_calls == 1 and cloud.info_calls == 0
            for _ in range(4):
                assert worker._process_one()
            assert db.count_strm_refresh_paths() == 4
            assert len(list(root.rglob("*.strm"))) == 4
            assert len(list(root.rglob("*.nfo"))) == 4
            for path in root.rglob("*.nfo"):
                assert (
                    path.read_bytes() == (tmp_path / "cloud" / path.name).read_bytes()
                )
            assert all(
                dict(row)["status"] == "completed"
                for row in db.list_strm_metadata_queue(status="all")
            )
            assert len(db.list_strm_index("guangya:audit")) == 4
            assert len(db.list_strm_index("guangya-meta:audit")) == 4
            # 正常视频同步的收尾将已变化路径也交给同一持久 outbox。
            db.enqueue_strm_refresh_paths(result["changed_strm_paths"])
            assert db.count_strm_refresh_paths() == 8
            worker._flush_media_refresh(force=True)
            assert db.count_strm_refresh_paths() == 0
            assert queue.media_refresh_queue_status()["paths"] == 8
            endpoint = MediaEndpoint(root)
            coordinator, claimed = drain_refresh(endpoint)
            assert len(claimed) == 1 and coordinator._completed_session == 1
            assert endpoint.posts == ["/Items/audit-library/Refresh"]
            assert queue.media_refresh_queue_status()["paths"] == 0
            repeated = generate_batch(cloud, root)
            assert repeated["generated"] == repeated["metadata_queued"] == 0
            assert repeated["skipped"] == repeated["metadata_skipped"] == 4
            assert worker._process_one() is False
            assert not list(root.rglob("*.part"))


def test_batch_metadata_path_migration_keeps_both_refresh_intents(tmp_path):
    """后台元数据迁移已删除旧副本时，新旧目录都必须进入持久刷新交接。"""
    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud", count=1)
        root = tmp_path / "strm"
        root.mkdir()
        worker = metadata.STRMMetadataWorker()
        worker._client = cloud
        worker._last_refresh_at = metadata.time.monotonic()
        file = cloud.files["nfo-1"]
        with cloud_runtime(cloud, root):
            for directory in ("Old Series/Season 01", "New Series/Season 01"):
                db.enqueue_strm_metadata_jobs(
                    [
                        strm._metadata_queue_payload(
                            file,
                            directory,
                            str(root),
                            source_id="audit",
                            source_name="审查",
                            force=True,
                        )
                    ]
                )
                assert worker._process_one()
                if directory.startswith("Old"):
                    old = Path(db.list_strm_index("guangya-meta:audit")[0]["strm_path"])
                    assert old.is_file()
                    db.acknowledge_strm_refresh_paths(db.list_strm_refresh_entries())
            new = Path(db.list_strm_index("guangya-meta:audit")[0]["strm_path"])
            assert not old.exists()
            assert new.read_bytes() == cloud.paths["nfo-1"].read_bytes()
            assert {entry["path"] for entry in db.list_strm_refresh_entries()} == {
                str(old),
                str(new),
            }


def test_interrupt_metadata_ack_crash_reuses_verified_installation(tmp_path):
    """模拟真实安装完成、持久 ACK 之前进程退出；重启只补 ACK/outbox，不重复下载。"""
    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud", count=1)
        root = tmp_path / "strm"
        root.mkdir()
        file = cloud.files["nfo-1"]
        db.enqueue_strm_metadata_jobs(
            [
                strm._metadata_queue_payload(
                    file,
                    "Audit Series",
                    str(root),
                    source_id="audit",
                    source_name="审查",
                )
            ]
        )
        first = metadata.STRMMetadataWorker()
        first._client = cloud
        with cloud_runtime(cloud, root):
            with patch.object(
                db,
                "complete_strm_metadata_job",
                side_effect=SystemExit("crash before ACK"),
            ):
                with pytest.raises(SystemExit):
                    first._process_one()
            path = Path(db.list_strm_index("guangya-meta:audit")[0]["strm_path"])
            installed = path.stat()
            assert path.read_bytes() == cloud.paths["nfo-1"].read_bytes()
            assert db.list_strm_metadata_queue(status="all")[0]["status"] == "running"
            assert db.count_strm_refresh_paths() == 0
            assert db.recover_stale_strm_metadata_jobs(force=True) == 1
            restarted = metadata.STRMMetadataWorker()
            restarted._client = cloud
            restarted._last_refresh_at = metadata.time.monotonic()
            assert restarted._process_one()
            assert path.stat().st_ino == installed.st_ino
            assert path.stat().st_mtime_ns == installed.st_mtime_ns
            assert db.list_strm_metadata_queue(status="all")[0]["status"] == "completed"
            assert db.count_strm_refresh_paths() == 1
            assert cloud.download_calls == 1, "已落盘且指纹确认的元数据恢复不应再次下载"
            assert restarted._process_one() is False


def test_interrupt_cancel_restart_finishes_only_uncommitted_files(tmp_path):
    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud")
        root = tmp_path / "strm"
        root.mkdir()
        with cloud_runtime(cloud, root):
            stopped = generate_batch(
                cloud, root, should_stop=lambda: bool(list(root.rglob("*.strm")))
            )
            assert stopped["stopped"] is True and stopped["clean_skipped"] is True
            assert stopped["generated"] == 1
            first = next(root.rglob("*.strm"))
            original = (
                first.read_bytes(),
                first.stat().st_ino,
                first.stat().st_mtime_ns,
            )
            resumed = generate_batch(cloud, root)
            assert resumed["generated"] == 3 and resumed["metadata_queued"] == 4
            assert (
                first.read_bytes(),
                first.stat().st_ino,
                first.stat().st_mtime_ns,
            ) == original
            assert len(list(root.rglob("*.strm"))) == 4
            assert len(db.list_strm_index("guangya:audit")) == 4


def test_interrupt_refresh_outbox_ack_and_media_failure_replay(tmp_path):
    with isolated_test_database():
        endpoint = MediaEndpoint(tmp_path)
        paths = [str(tmp_path / f"Series {n}" / "episode.strm") for n in range(3)]
        for path in paths:
            p = Path(path)
            p.parent.mkdir()
            p.write_text("http://play.invalid/fixture")
        db.enqueue_strm_refresh_paths(paths)
        worker = metadata.STRMMetadataWorker()
        with patch.object(
            refresh, "_configured_provider_names", return_value=("jellyfin",)
        ):
            with patch.object(
                db,
                "acknowledge_strm_refresh_paths",
                side_effect=OSError("ACK unavailable"),
            ):
                worker._flush_media_refresh(force=True)
            assert db.count_strm_refresh_paths() == 3
            assert queue.media_refresh_queue_status()["paths"] == 3
            restarted = metadata.STRMMetadataWorker()
            restarted._flush_media_refresh(force=True)
            assert db.count_strm_refresh_paths() == 0
            assert queue.media_refresh_queue_status()["paths"] == 3
        claimed = queue.claim_due_media_refreshes(owner="dead-consumer", force=True)
        assert len(claimed) == 1
        assert queue.recover_media_refresh_leases() == 1
        endpoint.fail = True
        failed, _ = drain_refresh(endpoint, owner="restart")
        assert failed._failed_session == 1
        assert queue.media_refresh_queue_status()["retry_wait"] == 1
        assert queue.media_refresh_queue_status()["paths"] == 3
        assert all(Path(p).is_file() for p in paths)
        endpoint.fail = False
        complete, _ = drain_refresh(endpoint, owner="restart-2")
        assert complete._completed_session == 1
        assert len(endpoint.posts) == 1
        assert queue.media_refresh_queue_status()["paths"] == 0


def test_history_strm_index_backfill_and_old_path_recovery(tmp_path):
    from app.modules.strm_recovery import recover_pending_paths

    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud", count=1)
        root = tmp_path / "strm"
        root.mkdir()
        video = [change for change in cloud.changes() if change["kind"] == "video"]
        first = strm.sync_strm_incremental(
            "audit", video, "http://play.invalid", str(root), client=cloud
        )
        assert first["generated"] == 1
        old = Path(db.list_strm_index("guangya:audit")[0]["strm_path"])
        snapshot = (old.read_bytes(), old.stat().st_ino, old.stat().st_mtime_ns)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_index SET content_fingerprint='' WHERE source='guangya:audit'"
            )
        restored = strm.sync_strm_incremental(
            "audit", video, "http://play.invalid", str(root), client=cloud
        )
        row = dict(db.list_strm_index("guangya:audit")[0])
        assert restored["skipped"] == 1 and restored["generated"] == 0
        assert row["content_fingerprint"].startswith("sha256:")
        assert (old.read_bytes(), old.stat().st_ino, old.stat().st_mtime_ns) == snapshot
        new = old.parent.parent / "New Season" / old.name
        new.parent.mkdir()
        new.write_bytes(old.read_bytes())
        db.upsert_strm_index(
            row["source"],
            row["file_id"],
            row["etag"],
            row["size"],
            row["filename"],
            str(new),
            row["content_fingerprint"],
        )
        assert len(db.list_strm_path_cleanup("guangya:audit")) == 1
        stats = dict(
            cleaned=0,
            metadata_cleaned=0,
            changes=[],
            changed_strm_paths=[],
            omitted_count=0,
        )
        recover_pending_paths(
            "guangya:audit",
            str(root),
            stats,
            valid_ids={row["file_id"]},
            on_refresh_paths=db.enqueue_strm_refresh_paths,
        )
        assert not old.exists() and new.read_bytes() == snapshot[0]
        assert stats["cleaned"] == 1
        assert db.list_strm_path_cleanup("guangya:audit") == []
        assert db.count_strm_refresh_paths() == 1
        recover_pending_paths(
            "guangya:audit",
            str(root),
            stats,
            valid_ids={row["file_id"]},
            on_refresh_paths=db.enqueue_strm_refresh_paths,
        )
        assert stats["cleaned"] == 1


def test_interrupt_reused_metadata_is_revalidated_under_commit_lock(tmp_path):
    with isolated_test_database():
        cloud = CloudFiles(tmp_path / "cloud", count=1)
        root = tmp_path / "strm"
        root.mkdir()
        job = strm._metadata_queue_payload(
            cloud.files["nfo-1"],
            "Series",
            str(root),
            source_id="audit",
            source_name="审查",
        )
        with cloud_runtime(cloud, root):
            prepared = strm.prepare_strm_metadata_job(job, str(root), client=cloud)
            installed = strm.commit_strm_metadata_job(job, prepared, str(root))
            reused = strm.prepare_strm_metadata_job(job, str(root), client=cloud)
            assert reused["prepared"] is None
            target = Path(installed["path"])
            target.write_bytes(b"externally changed")
            with pytest.raises(RuntimeError, match="发生变化"):
                strm.commit_strm_metadata_job(job, reused, str(root))
            assert target.read_bytes() == b"externally changed"
            assert cloud.download_calls == 1


class WritableCloud(CloudFiles):
    """provider move/rename 在临时目录实际执行，云盘控制面保持内存 DTO。"""

    def __init__(self, root, count=2):
        super().__init__(root, count=count)
        target = root.parent / "cloud-library"
        target.mkdir()
        self.directories = {"season": root, "target": target}
        self.files.update(
            {
                key: GuangYaFile(key, path.name, True, parent_id="0")
                for key, path in self.directories.items()
            }
        )
        self.writes = []

    def list_dir(self, parent):
        return list(self.iter_dir(parent))

    def create_dir(self, name, parent):
        key = f"directory-{len(self.files)}"
        target = self.directories[parent] / name
        target.mkdir()
        self.directories[key] = target
        self.files[key] = GuangYaFile(key, name, True, parent_id=parent)
        return key

    def move(self, file_ids, parent):
        for file_id in file_ids:
            file = self.files[file_id]
            self.paths[file_id] = self.paths[file_id].rename(
                self.directories[parent] / file.name
            )
            file.parent_id = parent
            self.writes.append(("move", file_id, parent))
        return True

    def rename(self, file_id, name):
        file = self.files[file_id]
        self.paths[file_id] = self.paths[file_id].rename(
            self.paths[file_id].with_name(name)
        )
        file.name = name
        self.writes.append(("rename", file_id, name))
        return True


def test_normal_cloud_organize_write_to_strm_and_refresh(tmp_path):
    from app.modules.organize import Organizer

    with isolated_test_database():
        cloud = WritableCloud(tmp_path / "incoming")
        original_bodies = {
            file_id: path.read_bytes() for file_id, path in cloud.paths.items()
        }
        root = tmp_path / "strm"
        root.mkdir()
        organizer = Organizer(client=cloud, scraper=Catalog())
        rules = OrganizeRules(
            target_dir_id="target",
            small_file_mb=0,
            clean_empty=False,
            notify_enabled=False,
            library_notify=False,
        )
        ffprobe_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                        }
                    ]
                }
            ),
            "",
        )
        with (
            cloud_runtime(cloud, root),
            patch.object(media_probe, "_run_ffprobe", return_value=ffprobe_result),
        ):
            plans, stats = organizer.organize(
                "season",
                rules,
                dry_run=False,
                post_actions=False,
                operation_token="deep-audit-cloud",
            )
            assert stats["moved"] == 2 and stats["failed"] == 0, stats
            assert all(plan.action == "move" for plan in plans)
            assert len(cloud.writes) == 8, cloud.writes
            assert all(
                path.read_bytes() == original_bodies[key]
                for key, path in cloud.paths.items()
            )
            assert all(cloud.files[key].parent_id != "season" for key in cloud.paths)
            assert len(stats["strm_changes"]) == 4
            result = strm.sync_strm_incremental(
                "target",
                stats["strm_changes"],
                "http://play.invalid",
                str(root),
                client=cloud,
                metadata_exts={"nfo"},
            )
            assert result["generated"] == 2 and result["metadata_queued"] == 2, result
            worker = metadata.STRMMetadataWorker()
            worker._client = cloud
            worker._last_refresh_at = metadata.time.monotonic()
            assert worker._process_one() and worker._process_one()
            db.enqueue_strm_refresh_paths(result["changed_strm_paths"])
            assert db.count_strm_refresh_paths() == 4
            worker._flush_media_refresh(force=True)
            endpoint = MediaEndpoint(root)
            completed, _ = drain_refresh(endpoint)
            assert completed._completed_session == 1 and len(endpoint.posts) == 1
            assert queue.media_refresh_queue_status()["paths"] == 0
