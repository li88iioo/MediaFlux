"""真实批量纠偏服务/API 回归测试；云端与 SQLite 均隔离在临时资源中。"""
from __future__ import annotations

from tests.support import assert_present

from collections.abc import Iterator

from app.modules.process_lock import CrossProcessLock

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import copy
import json
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
from app.modules.directory_scrape_errors import DirectoryScrapeConflictError
from app.modules.organize import OrganizeRules
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.scraper import MatchResult
from app.repositories import organize_confirmations as confirmation_repo
from tests.support import authenticated_session, isolated_test_database, release_parse_result


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
        record = self.files.get(file_id)
        return self._as_file(file_id, record) if record else None

    def list_dir(self, directory_id: str) -> list[GuangYaFile]:
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
        service = OrganizeCorrectionService(client=cast(GuangYaClient, self.cloud), scraper=cast(TMDBScraper, self.scraper))
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

def harness(tmp_path: Path, archive_rules) -> Iterator[_Harness]:
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
    status: str = "success",
) -> int:
    file_id = log_file_id or f"file-{len(cloud.files) + 1}"
    cloud.add_file(file_id, filename, parent_id=parent_id)
    log_id = db.add_organize_log(
        source="guangya",
        original_path=original_path,
        new_path="",
        file_id=file_id,
        status=status,
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
        "status": status,
    }])
    return log_id


def _pending_group(harness: _Harness, episodes: int) -> tuple[list[int], dict]:
    ids = [_add_log(harness.cloud, f"Series.S01E{episode:02}.mkv", status="manual")
           for episode in range(1, episodes + 1)]
    subtitle_id = "subtitle-1"
    harness.cloud.add_file(subtitle_id, "Series.S01E01.zh.srt")
    subtitle = harness.cloud.files[subtitle_id]
    db.add_organize_log_items(ids[0], [{
        "file_id": subtitle_id, "role": "subtitle", "status": "manual",
        "original_parent_id": subtitle["parent_id"], "current_parent_id": subtitle["parent_id"],
        "original_name": subtitle["name"], "current_name": subtitle["name"],
        "size": subtitle["size"], "etag": subtitle["etag"],
    }])

    def snapshot(file_id: str) -> dict:
        item = harness.cloud.files[file_id]
        return {"file_id": file_id, **{key: item[key] for key in ("name", "parent_id", "size", "etag")}}

    payload = {
        "source_dir_id": "source-config", "source_parent_id": "source",
        "files": [snapshot(assert_present(db.get_organize_log(log_id))["file_id"]) for log_id in ids],
        "companions": [snapshot(subtitle_id)], "_notification_suppressed": True,
    }
    confirmation_repo.create_organize_confirmation(
        token="pending-group", fingerprint="pending-group", chat_id="", source_name="incoming",
        directory_path="/incoming/Series", payload=payload, expires_at="2099-01-01 00:00:00",
    )
    return ids, payload


def _entries(log_ids: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "log_id": log_id,
            "expected_version": int(assert_present(db.get_organize_log(log_id))["version"]),
            "operation_token": f"batch-token-{log_id}",
        }
        for log_id in log_ids
    ]


def _preview(service: OrganizeCorrectionService, entries: list[dict[str, Any]], candidate=None):
    return service.preview_batch(entries, CANDIDATE if candidate is None else candidate)


@pytest.mark.parametrize("stale", [False, True, "file_changed"])
def test_agent_reorganize_frozen_log_position_without_prerename(
    isolated_db, harness: _Harness, archive_rules, monkeypatch, stale,
):
    """真实纠偏预览/文件执行：旧发布集号直接纠正；确认后改动不能被覆盖。"""
    import asyncio
    from types import SimpleNamespace
    from app import sdk
    from app.agent.kernel.persistence import SQLiteKernelStore
    from app.agent.errors import ToolPipelineError
    from app.modules import organize_correction, organize_tasks
    from tests.test_agent_sdk_effects import context_for, executor

    log_id = _add_log(harness.cloud, "Series - 19.mkv", old_tmdb_id="4242",
                      original_path="/动漫/MF-557-Series - 19")
    db.update_organize_log(log_id, status="failed", error="文件集号超出范围")
    callbacks = []
    def start(operation, reference, callback, **kwargs):
        callbacks.append(callback)
        return {"ok": True, "task_id": "log-correction-task"}

    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", harness.service)
    monkeypatch.setattr(organize_tasks, "get_organize_manager", lambda: SimpleNamespace(start_operation=start))
    archive_rules.link_strm = False
    monkeypatch.setattr(OrganizeCorrectionService, "_notify_reorganize_result", lambda *a, **kw: [])
    monkeypatch.setattr(OrganizeCorrectionService, "_finalize_reorganized_source", lambda *a, **kw: [])

    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        with sdk.call_owner(context.owner):
            detail = sdk.activity.organize_detail(log_id)
        assert detail["allowed_actions"]["reorganize"]
        arguments = {
            "log_id": log_id, "tmdb_id": "4242", "media_type": "tv", "season": 2, "episode": 6,
        }
        if stale == "file_changed":
            harness.cloud.rename("file-1", "changed.mkv")
            writes = list(harness.cloud.write_calls)
            with pytest.raises(ToolPipelineError, match="文件名已变化") as error:
                await effects.prepare("activity.reorganize", arguments, context=context)
            assert error.value.code == "precondition_failed"
            assert not callbacks and harness.cloud.write_calls == writes
            return
        proposal = await effects.prepare("activity.reorganize", arguments, context=context)
        plan = proposal.effect_plan
        assert plan is not None
        assert plan.preview["data"]["season"] == 2
        assert plan.preview["data"]["episode"] == 6
        assert "S02E06" in plan.preview["data"]["file_name"]
        assert not harness.cloud.write_calls and not callbacks
        if stale:
            db.update_organize_log(log_id, status="skipped")
            with pytest.raises(ToolPipelineError, match="版本已变化"):
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert not callbacks and not harness.cloud.write_calls
            return
        completed = await effects.execute_confirmed(plan.plan_id, context=context)
        result = completed.outcome.public_content
        assert result["status"] == "accepted"
        assert result["data"]["log_id"] == log_id and len(callbacks) == 1
        assert not harness.cloud.write_calls
        assert callbacks[0]()["success"]
        row = db.get_organize_log(log_id)
        assert row is not None
        assert row["status"] == "success" and (row["season"], row["episode"]) == (2, 6)
        assert row["confirmation_actor"] == "agent"
        assert harness.scraper.confirmed == []
        with db.get_conn() as conn:
            assert conn.execute("SELECT count(*) FROM manual_episode_confirmations WHERE log_id=?", (log_id,)).fetchone()[0] == 0
        assert "S02E06" in row["current_name"]
        assert row["original_name"] == "Series - 19.mkv"
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert len(callbacks) == 1
    asyncio.run(run())


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
        assert row is not None
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
    manager._lock = cast(CrossProcessLock, threading.Lock())
    manager._lock.acquire()
    manager._task = {"id": "batch-files", "operation": "批量纠正", "status": "running"}
    manager._run_operation("batch-files", "批量纠正", "2条日志", lambda: service.run_batch(
        "reorganize", entries, candidate=CANDIDATE, preview_digest=preview["preview_digest"],
    ))
    assert manager.task_status()["status"] == "partial"
    task_result_result = manager.task_result("batch-files")
    assert task_result_result is not None
    receipt = task_result_result["result"]
    # 新任务接管当前槽位后，原批次逐条回执仍可按 ID 取得。
    manager._task = {"id": "later-task", "status": "running"}
    task_result_result_2 = manager.task_result("batch-files")
    assert task_result_result_2 is not None
    assert task_result_result_2["result"] == receipt
    assert harness.cloud.files["file-1"]["path"].is_file()
    assert harness.cloud.files["file-2"]["parent_id"] == "source"
    assert harness.cloud.files["file-2"]["path"].is_file()

    assert receipt["success"] is False
    assert [item["log_id"] for item in receipt["completed"]] == [ids[0]]
    assert [item["log_id"] for item in receipt["failed"]] == [ids[1]]
    get_organize_log_result = db.get_organize_log(ids[0])
    assert get_organize_log_result is not None
    assert get_organize_log_result["tmdb_id"] == CANDIDATE["tmdb_id"]
    writes_after_partial = list(harness.cloud.write_calls)
    with pytest.raises(ValueError, match="预览已失效|重新预览并确认"):
        service.run_batch(
            "reorganize", entries,
            candidate=CANDIDATE,
            preview_digest=preview["preview_digest"],
        )
    assert harness.cloud.write_calls == writes_after_partial


@pytest.fixture
def logs_api_client(
    isolated_db, harness: _Harness, monkeypatch: pytest.MonkeyPatch,
):
    del isolated_db
    from app.routes import logs_api

    api = FastAPI()
    api.add_middleware(SessionMiddleware, secret_key="batch-correction-test-session")
    api.include_router(logs_api.router)

    @api.post("/__test/login")
    def _test_login(request: Request):
        request.session.update(authenticated_session())
        return {"ok": True}

    monkeypatch.setattr(logs_api, "OrganizeCorrectionService", harness.service)
    with TestClient(api) as client:
        yield client


def test_selected_confirmation_poll_skips_pending_log_resolution(
    logs_api_client, harness: _Harness, monkeypatch: pytest.MonkeyPatch,
):
    _pending_group(harness, 1)
    confirmation_repo.create_organize_confirmation(
        token="executed-group", fingerprint="executed-group", chat_id="", source_name="incoming",
        directory_path="/incoming/Done", payload={"files": [{"file_id": "done", "name": "Done.mkv"}]},
        expires_at="2099-01-01 00:00:00",
    )
    confirmation_repo.update_organize_confirmation("executed-group", status="completed")
    executed_log = db.add_organize_log(
        "guangya", "/incoming/Done", "/archive/Done", "done", "success",
        operation_token="recognition-confirm:executed-group",
    )

    def unexpected_resolution(*_args):
        pytest.fail("普通确认状态回读触发了待确认日志归属查询")

    monkeypatch.setattr(confirmation_repo, "_pending_reorganize_log_ids", unexpected_resolution)
    client = logs_api_client
    assert client.post("/__test/login").status_code == 200
    url = "/api/logs/organize/confirmations"
    tokens = ["pending-group", "executed-group"]
    selected = {"confirmation_ids": tokens, "status": "all", "page": 1, "page_size": 20}
    for response in (
        client.post(f"{url}/query", json=selected),
        client.post(f"{url}/query", json={**selected, "include_correction_logs": False}),
        client.get(url, params={"confirmation_ids": ",".join(tokens), "status": "all"}),
    ):
        assert response.status_code == 200, response.text
        assert {item["confirmation_id"]: item["log_ids"] for item in response.json()["items"]} == {
            "pending-group": [], "executed-group": [executed_log],
        }
    assert client.post(f"{url}/query", json={**selected, "include_correction_logs": "false"}).status_code == 400


def test_pending_group_handoff_and_retry_settle_all_eight_logs(
    logs_api_client, harness: _Harness, archive_rules, monkeypatch: pytest.MonkeyPatch,
):
    with monkeypatch.context() as clock:
        clock.setattr(db, "now", lambda: "2026-01-01 00:00:00")
        log_ids, _ = _pending_group(harness, 8)
    archive_rules.clean_empty = archive_rules.link_strm = archive_rules.notify_enabled = False
    client = logs_api_client
    assert client.post("/__test/login").status_code == 200
    query = "/api/logs/organize/confirmations/query"
    selection = {"confirmation_ids": ["pending-group"], "status": "all", "include_correction_logs": True}
    listed, = client.get(query.removesuffix("/query")).json()["items"]
    assert listed["status"] == "pending" and listed["log_ids"] == []
    response = client.post(query, json=selection)
    assert response.status_code == 200, response.text
    item, = response.json()["items"]
    assert item["status"] == "pending"
    assert item["log_ids"] == log_ids
    assert len(item["files"]) == 8
    assert harness.cloud.write_calls == []

    service = harness.service()
    entries = [{**entry, "confirmation_id": "pending-group"} for entry in _entries(item["log_ids"])]
    preview = _preview(service, entries)
    assert preview["can_execute"], preview
    failed_id = log_ids[-1]
    failed_file = assert_present(db.get_organize_log(failed_id))["file_id"]
    harness.cloud.fail_rename_ids.add(failed_file)
    receipt = service.run_batch("reorganize", entries, candidate=CANDIDATE, preview_digest=preview["preview_digest"])
    assert [row["log_id"] for row in receipt["completed"]] == log_ids[:-1]
    assert [row["log_id"] for row in receipt["failed"]] == [failed_id]
    card = assert_present(confirmation_repo.get_organize_confirmation("pending-group"))
    assert card["status"] == "pending" and not card["result_json"]
    item, = client.post(query, json=selection).json()["items"]
    assert item["log_ids"] == []
    assert harness.cloud.files[failed_file]["parent_id"] == "source"

    completed = [dict(assert_present(db.get_organize_log(log_id))) for log_id in log_ids[:-1]]
    writes_after_partial = len(harness.cloud.write_calls)
    harness.cloud.fail_rename_ids.clear()
    retry = service.preview_reorganize(failed_id, **CANDIDATE)
    assert service._execute_reorganize(failed_id, "retry-failed-episode", retry["version"], retry)["success"]
    assert all(call[1] == failed_file for call in harness.cloud.write_calls[writes_after_partial:])
    assert [dict(assert_present(db.get_organize_log(log_id))) for log_id in log_ids[:-1]] == completed
    card = assert_present(confirmation_repo.get_organize_confirmation("pending-group"))
    assert card["status"] == "completed"
    result = json.loads(card["result_json"])
    assert (result["resolution"], result["moved"], result["organize_log_ids"]) == ("reorganized", 8, log_ids)
    assert [assert_present(db.get_organize_log(log_id))["episode"] for log_id in log_ids] == list(range(1, 9))
    for log_id in log_ids:
        members = db.list_organize_log_items(log_id)
        assert len(members) == (2 if log_id == log_ids[0] else 1)
        for member in members:
            assert member["status"] == "success"
            remote = harness.cloud.files[member["file_id"]]
            assert (remote["parent_id"], remote["name"]) == (member["current_parent_id"], member["current_name"])


def test_pending_log_handoff_is_bounded_across_selected_tokens_and_pages(
    isolated_db, harness: _Harness,
):
    log_ids, payload = _pending_group(harness, 1)
    large_group = {**payload, "files": [
        {**payload["files"][0], "file_id": f"large-{index}"} for index in range(50)
    ], "companions": []}
    confirmation_repo.create_organize_confirmation(
        token="large-group", fingerprint="large-group", chat_id="", source_name="incoming",
        directory_path="/incoming/Large", payload=large_group, expires_at="2099-01-01 00:00:00",
    )
    assert confirmation_repo.query_organize_confirmations(
        tokens=["pending-group"], include_correction_logs=True,
    )["log_ids"] == {
        "pending-group": log_ids,
    }
    second_page = confirmation_repo.query_organize_confirmations(
        tokens=["pending-group", "large-group"], page=2, page_size=1, include_correction_logs=True,
    )
    assert second_page["rows"][0]["token"] == "pending-group"
    assert second_page["log_ids"] == {}
    oversized_selection = ["pending-group", *(f"missing-{index}" for index in range(50))]
    assert confirmation_repo.query_organize_confirmations(
        tokens=oversized_selection, include_correction_logs=True,
    )["log_ids"] == {}


@pytest.mark.parametrize("change", ["newer_card", "rescan", "legacy_same_second", "updated_after",
                                     "wrong_source", "missing_companion", "etag_changed"])
def test_pending_group_handoff_rejects_incomplete_or_stale_original_logs(
    isolated_db, harness: _Harness, monkeypatch: pytest.MonkeyPatch, change,
):
    from app.modules.organize_confirmation_batches import list_confirmations

    monkeypatch.setattr(db, "now", lambda: "2026-01-01 00:00:00")
    log_ids, payload = _pending_group(harness, 2)
    last_file = payload["files"][-1]
    if change == "newer_card":
        confirmation_repo.create_organize_confirmation(
            token="newer-card", fingerprint="different-snapshot", chat_id="", source_name="incoming",
            directory_path="/incoming/Series", payload={**payload, "files": [last_file], "companions": []},
            expires_at="2099-01-01 00:00:00",
        )
        confirmation_repo.update_organize_confirmation("newer-card", status="queued")
    elif change == "rescan":
        _add_log(harness.cloud, last_file["name"], log_file_id=last_file["file_id"], status="manual")
    elif change == "legacy_same_second":
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='pending-group'", (json.dumps(payload),))
    elif change == "updated_after":
        monkeypatch.setattr(db, "now", lambda: "2026-01-01 00:00:01")
        db.update_organize_log(log_ids[-1], error="同期日志发生了更新")
    elif change == "wrong_source":
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET source_dir_id='another-source' WHERE id=?", (log_ids[-1],))
    elif change == "missing_companion":
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_log_items WHERE log_id=? AND role='subtitle'", (log_ids[0],))
    else:
        member, = db.list_organize_log_items(log_ids[-1])
        db.update_organize_log_item(member["id"], etag="different-etag")
    item, = list_confirmations(
        confirmation_ids=["pending-group"], status="all", include_correction_logs=True,
    )["items"]
    assert item["status"] == "pending" and item["log_ids"] == []
    assert harness.cloud.write_calls == []


def test_batch_api_requires_auth_accepts_preview_and_rejects_legacy_implicit_request(
    logs_api_client, harness: _Harness,
):
    client = logs_api_client
    entries = _entries([
        _add_log(harness.cloud, "Series.S01E01.mkv"),
        _add_log(harness.cloud, "Series.S01E02.mkv"),
    ])
    url = "/api/logs/organize/batch"
    body = {"action": "reorganize", "entries": entries, "candidate": CANDIDATE}
    unauthorized = client.post(f"{url}/preview", json=body)
    assert unauthorized.status_code in {401, 403}

    assert client.post("/__test/login").status_code == 200
    response = client.post(f"{url}/preview", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["can_execute"] is True
    assert response.json()["preview_digest"]

    legacy = client.post(url, json={"action": "reorganize", "entries": entries})
    assert legacy.status_code == 400, legacy.text
    assert "候选" in legacy.json().get("error", "") or "预览" in legacy.json().get("error", "")

    assert harness.services
    assert all(service._closed for service in harness.services)
    assert harness.cloud.write_calls == []


def test_single_web_correction_requires_episode_before_retry(
    logs_api_client, harness: _Harness, archive_rules, monkeypatch,
):
    from app.clients.tmdb import TMDBClient
    from app.modules import organize_correction, organize_tasks

    archive_rules.clean_empty = archive_rules.link_strm = archive_rules.notify_enabled = False
    monkeypatch.setattr(TMDBClient, "detail", lambda self, tmdb_id, media_type: {
        "id": int(tmdb_id), "name": "Selected Series", "first_air_date": "2024-01-01",
        "seasons": [{"season_number": 2, "episode_count": 8}],
    })
    scraper = TMDBScraper()
    monkeypatch.setattr(harness, "scraper", scraper)
    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", harness.service)
    manager = organize_tasks.OrganizeTaskManager()
    monkeypatch.setattr(organize_tasks, "get_organize_manager", lambda: manager)

    filename = "Series.S02.mkv"
    log_id = _add_log(harness.cloud, filename, status="failed")
    original = harness.cloud.files["file-1"]["path"]
    content = original.read_bytes()
    sibling = harness.cloud.add_file("sibling", "Series.S02E04.mkv")
    client = logs_api_client
    assert client.post("/__test/login").status_code == 200
    url = f"/api/logs/organize/{log_id}/reorganize"
    before = dict(assert_present(db.get_organize_log(log_id)))
    blocked = client.post(f"{url}/preview", json=CANDIDATE)
    assert blocked.status_code == 400, blocked.text
    assert "集号" in blocked.json()["error"]
    stale = client.post(url, json={
        **CANDIDATE, "operation_token": "old-preview", "expected_version": before["version"],
        "preview_digest": "obsolete",
    })
    assert stale.status_code == 400 and "集号" in stale.json()["error"]
    assert dict(assert_present(db.get_organize_log(log_id))) == before
    assert not harness.cloud.write_calls

    candidate = {**CANDIDATE, "episode": 3}
    preview = client.post(f"{url}/preview", json=candidate).json()
    assert (preview["season"], preview["episode"]) == (2, 3)
    accepted = client.post(url, json={
        **candidate, "operation_token": "correct-episode", "expected_version": preview["version"],
        "preview_digest": preview["preview_digest"],
    })
    assert accepted.status_code == 200, accepted.text
    assert manager.shutdown(timeout=5)
    receipt = assert_present(manager.task_result(accepted.json()["task_id"]))
    assert receipt["result"]["success"], receipt
    row = assert_present(db.get_organize_log(log_id))
    assert (row["status"], row["season"], row["episode"]) == ("success", 2, 3)
    target = harness.cloud.directories["archive"] / preview["target_path"] / preview["file_name"]
    assert "S02E03" in target.name and target.name == row["current_name"]
    assert target.read_bytes() == content and not original.exists()
    assert sibling.is_file()


def test_single_web_confirmation_binds_the_preview_through_execution(
    logs_api_client, harness: _Harness, archive_rules, monkeypatch: pytest.MonkeyPatch,
):
    from app.modules import organize_correction, organize_tasks

    manager = organize_tasks.OrganizeTaskManager()
    operations = []

    def accept(operation, reference, callback, **kwargs):
        operations.append(callback)
        return {"ok": True, "task_id": "correction-task"}

    monkeypatch.setattr(manager, "start_operation", accept)
    monkeypatch.setattr(organize_tasks, "get_organize_manager", lambda: manager)
    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", harness.service)
    archive_rules.year_split = False
    archive_rules.clean_empty = archive_rules.link_strm = archive_rules.notify_enabled = False
    with monkeypatch.context() as clock:
        clock.setattr(db, "now", lambda: "2026-01-01 00:00:00")
        [log_id], _ = _pending_group(harness, 1)
    client = logs_api_client
    assert client.post("/__test/login").status_code == 200
    url = f"/api/logs/organize/{log_id}/reorganize"
    old = client.post(f"{url}/preview", json=CANDIDATE).json()

    def submit(preview, token, **changes):
        return client.post(url, json={
            **CANDIDATE, "operation_token": token, "expected_version": preview["version"],
            "preview_digest": preview["preview_digest"], **changes,
        })

    assert submit(old, "missing", preview_digest="").status_code == 409
    archive_rules.year_split = True
    response = submit(old, "stale")
    assert response.status_code == 409 and "重新预览" in response.json()["error"]
    assert not operations and not harness.cloud.write_calls

    fresh = client.post(f"{url}/preview", json=CANDIDATE).json()
    assert fresh["target_path"] != old["target_path"]
    assert submit(fresh, "changed-position", season=2).status_code == 409
    assert submit(fresh, "accepted").status_code == 200 and len(operations) == 1
    archive_rules.year_split = False
    with pytest.raises(ValueError, match="整理规则已变化"):
        operations.pop()()
    assert not harness.cloud.write_calls

    archive_rules.year_split = True
    assert submit(fresh, "reconfirmed").status_code == 200 and len(operations) == 1
    assert operations.pop()()["success"]
    saved = assert_present(db.get_organize_log(log_id))
    assert saved["new_path"] == f"{fresh['target_path']}/{fresh['file_name']}"
    card = assert_present(confirmation_repo.get_organize_confirmation("pending-group"))
    assert card["status"] == "completed"
    assert json.loads(card["result_json"])["organize_log_ids"] == [log_id]


@pytest.mark.parametrize("transition", ["rescan", "queue", "skip"])
def test_old_confirmation_cannot_be_used_after_other_entry_changes_ownership(
    logs_api_client, harness: _Harness, transition,
):
    [log_id], payload = _pending_group(harness, 1)
    client = logs_api_client
    assert client.post("/__test/login").status_code == 200
    url = f"/api/logs/organize/{log_id}"
    selection = {"confirmation_id": "pending-group"}
    assert client.get(url, params=selection).status_code == 200
    body = {**CANDIDATE, **selection}
    response = client.post(f"{url}/reorganize/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["confirmation_id"] == "pending-group"

    latest_id = None
    if transition == "rescan":
        member = payload["files"][0]
        latest_id = _add_log(harness.cloud, member["name"], log_file_id=member["file_id"], status="manual")
        confirmation_repo.create_organize_confirmation(
            token="new-card", fingerprint="pending-group", chat_id="", source_name="incoming",
            directory_path="/incoming/Series", payload=payload, expires_at="2099-01-01 00:00:00",
        )
        assert client.get(f"/api/logs/organize/{latest_id}", params={"confirmation_id": "new-card"}).status_code == 200
    elif transition == "queue":
        confirmation_repo.claim_organize_confirmation("pending-group", chat_id="", selected_index=0)
    else:
        # 观察取消事务已经提交、跳过日志投影尚未更新原日志的区间。
        confirmation_repo.cancel_organize_confirmation("pending-group", chat_id="", resolution="skipped")
    assert client.get(url, params=selection).status_code == 409
    assert client.post(f"{url}/recognition/search", json=body).status_code == 409
    assert client.post(f"{url}/reorganize/preview", json=body).status_code == 409
    response = client.post(f"{url}/reorganize", json={
        **body, "operation_token": "old-card", "expected_version": preview["version"],
        "preview_digest": preview["preview_digest"],
    })
    assert response.status_code == 409, response.text
    with pytest.raises(DirectoryScrapeConflictError, match="待确认组"):
        harness.service()._execute_reorganize(log_id, "stale-worker", preview["version"], preview)
    assert assert_present(db.get_organize_log(log_id))["status"] == "manual"
    assert harness.cloud.write_calls == []
    if latest_id is not None:
        assert assert_present(db.get_organize_log(latest_id))["status"] == "manual"
        assert assert_present(confirmation_repo.get_organize_confirmation("new-card"))["status"] == "pending"


def test_claimed_correction_excludes_rescan_confirm_and_skip(
    isolated_db, harness: _Harness, archive_rules, monkeypatch: pytest.MonkeyPatch,
):
    [log_id], payload = _pending_group(harness, 1)
    archive_rules.clean_empty = archive_rules.link_strm = archive_rules.notify_enabled = False
    original = dict(assert_present(confirmation_repo.get_organize_confirmation("pending-group")))
    service = harness.service()
    preview = service.preview_reorganize(log_id, **CANDIDATE, confirmation_id="pending-group")
    provider_move = harness.cloud.move
    observed = []

    def move(ids, target):
        if not observed:
            observed.append(assert_present(db.get_organize_log(log_id))["status"])
            with pytest.raises(ValueError, match="正在纠正"):
                confirmation_repo.create_organize_confirmation(
                    token="new-card", fingerprint="pending-group", chat_id="", source_name="incoming",
                    directory_path="/incoming/Series", payload=payload, expires_at="2099-01-01 00:00:00",
                )
            with pytest.raises(ValueError, match="正在纠正"):
                confirmation_repo.claim_organize_confirmation("pending-group", chat_id="", selected_index=0)
            with pytest.raises(ValueError, match="正在纠正"):
                confirmation_repo.cancel_organize_confirmation("pending-group", chat_id="", resolution="skipped")
            assert confirmation_repo.get_organize_confirmation("new-card") is None
            assert dict(assert_present(confirmation_repo.get_organize_confirmation("pending-group"))) == original
        provider_move(ids, target)

    monkeypatch.setattr(harness.cloud, "move", move)
    assert service._execute_reorganize(log_id, "active-correction", preview["version"], preview)["success"]
    assert observed == ["reorganizing"]
    assert assert_present(confirmation_repo.get_organize_confirmation("pending-group"))["status"] == "completed"


def test_bound_batch_cannot_convert_only_part_of_a_pending_group(isolated_db, harness: _Harness):
    log_ids, _ = _pending_group(harness, 3)
    entries = [{**entry, "confirmation_id": "pending-group"} for entry in _entries(log_ids)]
    service = harness.service()
    with pytest.raises(DirectoryScrapeConflictError, match="完整媒体组"):
        service.preview_batch(entries[:2], CANDIDATE)
    preview = service.preview_batch(entries, CANDIDATE)
    assert preview["can_execute"], preview
    assert [item["confirmation_id"] for item in preview["items"]] == ["pending-group"] * 3
    assert harness.cloud.write_calls == []
