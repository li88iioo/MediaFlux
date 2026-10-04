"""真实批量纠偏服务/API 回归测试；云端与 SQLite 均隔离在临时资源中。"""
from __future__ import annotations

import copy
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.scraper import MatchResult
from tests.support import isolated_test_database, release_parse_result


CANDIDATE = {
    "tmdb_id": "4242",
    "media_type": "tv",
    "title": "Selected Series",
    "year": "2024",
}


class _OfflineScraper:
    """只替代候选确认与解析，不替代纠偏服务的预览或写入链路。"""

    def __init__(self) -> None:
        self.closed = False
        self.confirmed: list[tuple[str, str]] = []

    def match_from_tmdb(self, tmdb_id: str, media_type: str) -> MatchResult:
        return MatchResult(
            tmdb_id=tmdb_id,
            title="Selected Series",
            year="2024",
            media_type=media_type,
            confidence=1.0,
            locked=True,
            provider="tmdb",
            external_id=tmdb_id,
        )

    def confirm(
        self,
        raw_name: str,
        tmdb_id: str,
        title: str,
        year: str,
        media_type: str,
        **kwargs: Any,
    ) -> MatchResult:
        del raw_name, kwargs
        self.confirmed.append((tmdb_id, media_type))
        return MatchResult(
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            media_type=media_type,
            confidence=1.0,
            locked=True,
            status="confirmed",
            provider="tmdb",
            external_id=tmdb_id,
        )

    def get_detail(
        self, tmdb_id: str, media_type: str, force_refresh: bool = False,
    ) -> dict[str, Any]:
        del force_refresh
        title, year = "Selected Series", "2024"
        return {
            "id": tmdb_id,
            "name": title,
            "title": title,
            "original_name": title,
            "original_title": title,
            "first_air_date": f"{year}-01-01" if media_type == "tv" else "",
            "release_date": f"{year}-01-01" if media_type == "movie" else "",
            "genres": [],
            "origin_country": ["US"],
            "original_language": "en",
        }

    def parse_media(self, filename: str, parent_path: str = "", match: Any = None):
        found = re.search(r"[sS](\d{1,3})[eE](\d{1,3})", filename)
        fields: dict[str, Any] = {
            "type": getattr(match, "media_type", "tv"),
            "title": getattr(match, "title", ""),
        }
        if found:
            fields.update(season=int(found.group(1)), episode=int(found.group(2)))
        return release_parse_result(fields, filename=filename, parent_path=parent_path)

    def close(self) -> None:
        self.closed = True


class _MemoryCloud:
    """临时目录 backing 的光鸭协议替身，保留真实读校验和 move/rename 行为。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.logged_in = True
        self.directories: dict[str, Path] = {}
        self.directory_parents: dict[str, str] = {}
        self.directory_names: dict[str, str] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.write_calls: list[tuple[Any, ...]] = []
        self.list_calls: list[str] = []
        self.fail_rename_ids: set[str] = set()
        self.closed = False

    def add_directory(self, directory_id: str, *, parent_id: str = "", name: str | None = None) -> None:
        if directory_id in self.directories:
            return
        path = self.root / directory_id
        path.mkdir(parents=True, exist_ok=True)
        self.directories[directory_id] = path
        self.directory_parents[directory_id] = parent_id
        self.directory_names[directory_id] = name or directory_id

    def add_file(
        self,
        file_id: str,
        name: str,
        *,
        parent_id: str = "source",
        size: int = 4,
        etag: str | None = None,
    ) -> Path:
        self.add_directory(parent_id)
        path = self.directories[parent_id] / name
        path.write_bytes((file_id.encode("utf-8") or b"file")[:1] * size)
        self.files[file_id] = {
            "name": name,
            "parent_id": parent_id,
            "size": size,
            "etag": etag or f"etag-{file_id}",
            "path": path,
        }
        return path

    def _as_file(self, file_id: str, record: dict[str, Any] | None = None) -> GuangYaFile:
        item = record or self.files[file_id]
        return GuangYaFile(
            file_id,
            item["name"],
            False,
            item["size"],
            item["etag"],
            item["parent_id"],
        )

    def file_info(self, file_id: str) -> GuangYaFile | None:
        record = self.files.get(str(file_id))
        return self._as_file(str(file_id), record) if record else None

    def list_dir(self, directory_id: str) -> list[GuangYaFile]:
        directory_id = str(directory_id)
        self.list_calls.append(directory_id)
        directories = [
            GuangYaFile(
                child_id,
                self.directory_names[child_id],
                True,
                0,
                "",
                directory_id,
            )
            for child_id, parent_id in self.directory_parents.items()
            if parent_id == directory_id
        ]
        files = [
            self._as_file(file_id, record)
            for file_id, record in self.files.items()
            if record["parent_id"] == directory_id
        ]
        return [*directories, *files]

    def create_dir(self, name: str, parent_id: str) -> str:
        directory_id = f"dir-{len(self.directories) + 1}"
        parent = self.directories[parent_id]
        path = parent / name
        path.mkdir(parents=False, exist_ok=False)
        self.directories[directory_id] = path
        self.directory_parents[directory_id] = parent_id
        self.directory_names[directory_id] = name
        self.write_calls.append(("mkdir", directory_id, parent_id, name))
        return directory_id

    def move(self, file_ids: list[str], target_parent_id: str) -> None:
        target_dir = self.directories[target_parent_id]
        for file_id in file_ids:
            record = self.files[file_id]
            old_path = Path(record["path"])
            new_path = target_dir / old_path.name
            shutil.move(str(old_path), new_path)
            record["path"] = new_path
            record["parent_id"] = target_parent_id
            self.write_calls.append(("move", file_id, target_parent_id))

    def rename(self, file_id: str, name: str) -> None:
        if file_id in self.fail_rename_ids:
            raise OSError(f"test rename failure: {file_id}")
        record = self.files[file_id]
        old_path = Path(record["path"])
        new_path = old_path.with_name(name)
        old_path.rename(new_path)
        record["path"] = new_path
        record["name"] = name
        self.write_calls.append(("rename", file_id, name))

    def external_rename(self, file_id: str, name: str) -> None:
        record = self.files[file_id]
        old_path = Path(record["path"])
        new_path = old_path.with_name(name)
        old_path.rename(new_path)
        record["path"] = new_path
        record["name"] = name

    def close(self) -> None:
        self.closed = True


class _Harness:
    def __init__(self, root: Path) -> None:
        self.cloud = _MemoryCloud(root / "cloud")
        self.cloud.add_directory("archive")
        self.scraper = _OfflineScraper()
        self.services: list[OrganizeCorrectionService] = []

    def service(self) -> OrganizeCorrectionService:
        service = OrganizeCorrectionService(client=self.cloud, scraper=self.scraper)
        self.services.append(service)
        return service

    def close(self) -> None:
        for service in self.services:
            service.close()
        self.scraper.close()
        self.cloud.close()


@pytest.fixture

def isolated_db():
    with isolated_test_database("batch-correction.db"):
        yield


@pytest.fixture

def archive_rules(monkeypatch: pytest.MonkeyPatch):
    # 从真实规则对象派生，仅将隔离云盘根固定为 archive；不改进程/仓库配置。
    rules = copy.deepcopy(OrganizeRules.from_config())
    rules.target_dir_id = "archive"

    def isolated_rules(cls):
        del cls
        return copy.deepcopy(rules)

    monkeypatch.setattr(OrganizeRules, "from_config", classmethod(isolated_rules))
    return rules


@pytest.fixture

def harness(tmp_path: Path, archive_rules) -> _Harness:
    del archive_rules
    value = _Harness(tmp_path)
    try:
        yield value
    finally:
        value.close()


def _add_log(
    cloud: _MemoryCloud,
    filename: str,
    *,
    old_tmdb_id: str = "",
    old_media_type: str = "tv",
    old_title: str = "Misidentified title",
    parent_id: str = "source",
    original_path: str = "/incoming/Series",
    release_parse: dict[str, Any] | None = None,
    log_file_id: str | None = None,
) -> int:
    file_id = log_file_id or f"file-{len(cloud.files) + 1}"
    cloud.add_file(file_id, filename, parent_id=parent_id)
    log_id = db.add_organize_log(
        source="guangya",
        original_path=original_path,
        new_path="",
        file_id=file_id,
        status="success",
        tmdb_id=old_tmdb_id,
        operation_type="organize",
        source_dir_id="source-config",
        original_parent_id=parent_id,
        original_name=filename,
        current_parent_id=parent_id,
        current_name=filename,
        media_type=old_media_type,
        title=old_title,
        year="1900" if old_tmdb_id else "",
        season=None,
        episode=None,
        release_parse=release_parse,
    )
    record = cloud.files[file_id]
    db.add_organize_log_items(log_id, [{
        "file_id": file_id,
        "role": "video",
        "original_parent_id": parent_id,
        "original_name": filename,
        "current_parent_id": parent_id,
        "current_name": filename,
        "size": record["size"],
        "etag": record["etag"],
        "status": "success",
    }])
    return log_id


def _entries(log_ids: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "log_id": log_id,
            "expected_version": int(db.get_organize_log(log_id)["version"]),
            "operation_token": f"batch-token-{log_id}",
        }
        for log_id in log_ids
    ]


def _preview(service: OrganizeCorrectionService, entries: list[dict[str, Any]], candidate=None):
    return service.preview_batch(entries, CANDIDATE if candidate is None else candidate)


def test_preview_uses_explicit_candidate_keeps_episode_positions_and_does_not_write(
    isolated_db, harness: _Harness,
):
    del isolated_db
    cloud = harness.cloud
    ids = [
        _add_log(cloud, "Series.S01E01.mkv", old_tmdb_id="9999"),
        _add_log(cloud, "Series.S01E02.mkv", old_tmdb_id="" , old_media_type=""),
        _add_log(
            cloud,
            "Series.S04E99.mkv",
            old_tmdb_id="1111",
            release_parse={"manual_position": {
                "version": 1,
                "source": "manual_correction",
                "season": 2,
                "episode": 7,
            }},
        ),
    ]
    service = harness.service()
    entries = _entries(ids)

    preview = _preview(service, entries)

    assert preview["can_execute"] is True
    assert preview["errors"] == []
    assert [item["match"]["tmdb_id"] for item in preview["items"]] == ["4242"] * 3
    assert [(item["season"], item["episode"]) for item in preview["items"]] == [
        (1, 1), (1, 2), (2, 7),
    ]
    assert [item["manual_position"] for item in preview["items"]][-1] == {
        "version": 1,
        "source": "manual_correction",
        "season": 2,
        "episode": 7,
    }
    assert preview["preview_digest"]
    assert cloud.list_calls  # 预览通过真实 list_dir 检查源文件与云端目标。
    assert cloud.write_calls == []
    assert all(Path(cloud.files[f"file-{index}"]["path"]).exists() for index in range(1, 4))


def test_preview_rejects_unified_episode_and_legacy_implicit_batch(isolated_db, harness: _Harness):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01E01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ]
    service = harness.service()
    entries = _entries(ids)

    with pytest.raises(ValueError, match="不能统一覆盖集号|保留每条记录的集号"):
        service.preview_batch(entries, {**CANDIDATE, "episode": 8})
    with pytest.raises(ValueError, match="选择正确作品并预览"):
        service.run_batch("reorganize", entries)
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch("reorganize", entries, candidate=CANDIDATE, preview_digest="")


def test_preview_rejects_different_original_titles_even_when_old_id_matches(
    isolated_db, harness: _Harness,
):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Alpha.S01E01.mkv", old_tmdb_id="7777"),
        _add_log(harness.cloud, "Beta.S01E02.mkv", old_tmdb_id="7777"),
    ]

    preview = _preview(harness.service(), _entries(ids))

    assert preview["can_execute"] is False
    assert {error["log_id"] for error in preview["errors"]} == set(ids)
    assert all("不同作品" in error["error"] for error in preview["errors"])


def test_preview_rejects_source_year_mismatch_for_same_title(isolated_db, harness: _Harness):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Reboot.2001.S01E01.mkv", old_tmdb_id="7777"),
        _add_log(harness.cloud, "Reboot.2020.S01E02.mkv", old_tmdb_id="7777"),
    ]

    preview = _preview(harness.service(), _entries(ids))

    assert preview["can_execute"] is False
    assert {error["log_id"] for error in preview["errors"]} == set(ids)
    assert all("不同作品" in error["error"] for error in preview["errors"])


@pytest.mark.parametrize("collision", ["same_file", "same_destination"])
def test_preview_rejects_same_physical_file_or_duplicate_destination(
    collision: str, isolated_db, harness: _Harness,
):
    del isolated_db
    if collision == "same_file":
        shared_id = "shared-physical-file"
        first = _add_log(harness.cloud, "Series.S01E01.mkv", log_file_id=shared_id)
        second = _add_log(harness.cloud, "Series.S01E01.mkv", log_file_id=shared_id)
    else:
        first = _add_log(
            harness.cloud, "Series.S01E01.mkv", parent_id="source-a",
        )
        second = _add_log(
            harness.cloud, "Series.S01E01.mkv", parent_id="source-b",
        )
    preview = _preview(harness.service(), _entries([first, second]))

    assert preview["can_execute"] is False
    assert {error["log_id"] for error in preview["errors"]} == {first, second}
    assert all("同一文件或同一目标" in error["error"] for error in preview["errors"])


def test_preview_blocks_tv_without_episode_number(isolated_db, harness: _Harness):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ]

    preview = _preview(harness.service(), _entries(ids))

    assert preview["can_execute"] is False
    missing = next(item for item in preview["items"] if item["log_id"] == ids[0])
    assert "集号" in missing["error"]


def test_preview_reads_existing_cloud_destination_and_detects_source_rename(
    isolated_db, harness: _Harness,
):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01E01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ]
    service = harness.service()
    entries = _entries(ids)
    first_preview = _preview(service, entries)
    assert first_preview["can_execute"] is True

    # 先独立验证预览后云端源文件名变化使 digest 失效。
    clean_file_id = f"file-{len(harness.cloud.files) + 1}"
    clean_ids = [
        _add_log(harness.cloud, "Other.S01E01.mkv", parent_id="source-other"),
        _add_log(harness.cloud, "Other.S01E02.mkv", parent_id="source-other"),
    ]
    clean_entries = _entries(clean_ids)
    clean_preview = _preview(service, clean_entries)
    assert clean_preview["can_execute"] is True
    writes_before = list(harness.cloud.write_calls)
    harness.cloud.external_rename(clean_file_id, "Other.externally-renamed.mkv")
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", clean_entries,
            candidate=CANDIDATE,
            preview_digest=clean_preview["preview_digest"],
        )
    assert harness.cloud.write_calls == writes_before

    # 已存在的同名云端目标也必须在预览阶段拦截。
    path_parts = [part for part in first_preview["items"][0]["target_path"].split("/") if part]
    parent_id = "archive"
    for part in path_parts:
        match = next((row for row in harness.cloud.list_dir(parent_id) if row.is_dir and row.name == part), None)
        if match is None:
            parent_id = harness.cloud.create_dir(part, parent_id)
        else:
            parent_id = match.file_id
    harness.cloud.add_file(
        "preexisting-target",
        first_preview["items"][0]["items"][0]["to_name"],
        parent_id=parent_id,
    )
    target_preview = _preview(service, entries)
    assert target_preview["can_execute"] is False
    assert any("目标" in error["error"] or "已存在" in error["error"] for error in target_preview["errors"])


def test_preview_digest_is_invalidated_by_version_or_rules_change(isolated_db, harness: _Harness, archive_rules):
    del isolated_db
    first = _add_log(harness.cloud, "Series.S01E01.mkv")
    second = _add_log(harness.cloud, "Series.S01E02.mkv")
    service = harness.service()
    entries = _entries([first, second])
    preview = _preview(service, entries)
    assert preview["can_execute"] is True

    with db.get_conn() as conn:
        conn.execute("UPDATE organize_log SET version=version+1 WHERE id=?", (first,))
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", entries,
            candidate=CANDIDATE,
            preview_digest=preview["preview_digest"],
        )

    # 重新生成版本有效的预览，再只改变本地规则快照。
    refreshed_entries = _entries([first, second])
    refreshed = _preview(service, refreshed_entries)
    assert refreshed["can_execute"] is True
    writes_before = list(harness.cloud.write_calls)
    archive_rules.target_dir_id = "archive-v2"
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", refreshed_entries,
            candidate=CANDIDATE,
            preview_digest=refreshed["preview_digest"],
        )
    assert harness.cloud.write_calls == writes_before


def test_batch_execute_corrects_identity_returns_receipt_and_replay_is_idempotently_blocked(
    isolated_db, harness: _Harness,
):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01E01.mkv", old_tmdb_id="9999"),
        _add_log(harness.cloud, "Series.S01E02.mkv", old_tmdb_id=""),
    ]
    service = harness.service()
    entries = _entries(ids)
    preview = _preview(service, entries)
    assert preview["can_execute"] is True

    receipt = service.run_batch(
        "reorganize", entries,
        candidate=CANDIDATE,
        preview_digest=preview["preview_digest"],
    )

    assert receipt["success"] is True
    assert receipt["requested"] == 2
    assert [item["log_id"] for item in receipt["completed"]] == ids
    assert receipt["failed"] == []
    for log_id in ids:
        row = db.get_organize_log(log_id)
        assert row["status"] == "success"
        assert row["tmdb_id"] == CANDIDATE["tmdb_id"]
        assert row["title"] == CANDIDATE["title"]
    writes_after_first_execution = list(harness.cloud.write_calls)
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", entries,
            candidate=CANDIDATE,
            preview_digest=preview["preview_digest"],
        )
    assert harness.cloud.write_calls == writes_after_first_execution


def test_batch_receipt_reports_partial_failure_without_replaying_completed_item(
    isolated_db, harness: _Harness,
):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01E01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ]
    service = harness.service()
    entries = _entries(ids)
    preview = _preview(service, entries)
    assert preview["can_execute"] is True
    harness.cloud.fail_rename_ids.add("file-2")

    from app.modules.organize_tasks import OrganizeTaskManager
    manager = OrganizeTaskManager()
    manager._lock = threading.Lock()
    manager._lock.acquire()
    manager._task = {"id": "batch-files", "operation": "批量纠正", "status": "running"}
    manager._run_operation("batch-files", "批量纠正", "2条日志", lambda: service.run_batch(
        "reorganize", entries, candidate=CANDIDATE, preview_digest=preview["preview_digest"],
    ))
    assert manager.task_status()["status"] == "partial"
    receipt = manager.task_result("batch-files")["result"]
    # 新任务接管当前槽位后，原批次逐条回执仍可按 ID 取得。
    manager._task = {"id": "later-task", "status": "running"}
    assert manager.task_result("batch-files")["result"] == receipt
    assert harness.cloud.files["file-1"]["path"].is_file()
    assert harness.cloud.files["file-2"]["parent_id"] == "source"
    assert harness.cloud.files["file-2"]["path"].is_file()

    assert receipt["success"] is False
    assert [item["log_id"] for item in receipt["completed"]] == [ids[0]]
    assert [item["log_id"] for item in receipt["failed"]] == [ids[1]]
    assert db.get_organize_log(ids[0])["tmdb_id"] == CANDIDATE["tmdb_id"]
    writes_after_partial = list(harness.cloud.write_calls)
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", entries,
            candidate=CANDIDATE,
            preview_digest=preview["preview_digest"],
        )
    assert harness.cloud.write_calls == writes_after_partial


def test_batch_api_requires_auth_accepts_preview_and_rejects_legacy_implicit_request(
    isolated_db, harness: _Harness, monkeypatch: pytest.MonkeyPatch,
):
    del isolated_db
    ids = [
        _add_log(harness.cloud, "Series.S01E01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ]
    entries = _entries(ids)
    from app.routes import logs_api

    api = FastAPI()
    api.add_middleware(SessionMiddleware, secret_key="batch-correction-test-session")
    api.include_router(logs_api.router)

    @api.post("/__test/login")
    def _test_login(request: Request):
        request.session["logged_in"] = True
        return {"ok": True}

    created: list[OrganizeCorrectionService] = []

    def service_factory():
        service = OrganizeCorrectionService(client=harness.cloud, scraper=harness.scraper)
        created.append(service)
        return service

    monkeypatch.setattr(logs_api, "OrganizeCorrectionService", service_factory)
    with TestClient(api) as client:
        unauthorized = client.post(
            "/api/logs/organize/batch/preview",
            json={"action": "reorganize", "entries": entries, "candidate": CANDIDATE},
        )
        assert unauthorized.status_code in {401, 403}

        assert client.post("/__test/login").status_code == 200
        response = client.post(
            "/api/logs/organize/batch/preview",
            json={"action": "reorganize", "entries": entries, "candidate": CANDIDATE},
        )
        assert response.status_code == 200, response.text
        assert response.json()["can_execute"] is True
        assert response.json()["preview_digest"]

        legacy = client.post(
            "/api/logs/organize/batch",
            json={"action": "reorganize", "entries": entries},
        )
        assert legacy.status_code == 400, legacy.text
        assert "候选" in legacy.json().get("error", "") or "预览" in legacy.json().get("error", "")

    assert created
    assert all(service._closed for service in created)
    assert harness.cloud.write_calls == []
