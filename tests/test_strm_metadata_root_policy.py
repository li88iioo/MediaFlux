"""元数据消费者与 full 扫描跨输出根切换时共用冻结策略；禁止外网。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import strm as strm_repo
from app.repositories import strm_request_ownership as strm_request_ownership_repo
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import strm
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories.strm import StrmRefreshPolicy
from app.repositories.strm_request_ownership import refresh_work_key
from tests.support import isolated_test_database
from tests.test_strm_metadata_queue import _Response, _TreeClient


@pytest.fixture
def runtime(tmp_path):
    with isolated_test_database(), ExitStack() as stack:
        for name in ("socket.socket.connect", "socket.socket.connect_ex",
                     "socket.create_connection", "socket.getaddrinfo"):
            stack.enter_context(patch(name, side_effect=AssertionError("network forbidden")))
        remote = GuangYaFile("meta", "Movie.nfo", False, 8, "m1", "film")
        client = cast(GuangYaClient, _TreeClient({
            "source": [GuangYaFile("film", "Film", True, parent_id="source")],
            "film": [remote],
        }))
        worker = STRMMetadataWorker()
        worker._client = client
        settings = {"STRM_ROOT": str(tmp_path / "A"), "STRM_METADATA_EXTS": "nfo"}
        stack.enter_context(patch("app.modules.strm_metadata_worker.get_bool", return_value=True))
        stack.enter_context(patch("app.modules.strm_metadata_worker.get", side_effect=lambda k, d="": settings.get(k, d)))
        stack.enter_context(patch("app.modules.strm.requests.get", return_value=_Response()))
        handoff = stack.enter_context(patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "pending"},
        ))
        request, _ = download_requests_repo.create_download_request("metadata-root-policy", "magnet")
        owners = strm_request_ownership_repo.current_strm_request_owners([request])
        yield tmp_path, remote, client, worker, settings, handoff, owners


def scan(client, root, policy):
    return strm.sync_strm(
        "source", "http://media.invalid", str(root), client=client,
        metadata_exts={"nfo"}, refresh_policy=policy,
    )


def target(root: Path) -> Path:
    return root / strm.STRM_SUBDIR / "Film" / "Movie.nfo"


def refresh_work():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM strm_request_work WHERE kind='refresh'")]


@pytest.mark.parametrize("enabled,allow_emby", [(False, False), (True, False), (True, True)])
def test_old_consumer_finishes_frozen_root_then_full_migrates_latest_policy(runtime, enabled, allow_emby):
    base, _, client, worker, settings, handoff, owners = runtime
    old_root, new_root = base / "A", base / "B"
    policy = StrmRefreshPolicy(
        media_root=str(old_root / strm.STRM_SUBDIR), enabled=enabled,
        allow_emby=allow_emby, request_owners=owners,
    )
    assert scan(client, old_root, policy)["metadata_queued"] == 1
    queued = dict(strm_repo.list_strm_metadata_queue()[0])
    settings["STRM_ROOT"] = str(new_root)
    assert worker._process_one()
    assert target(old_root).is_file(), "旧任务必须在冻结旧根落盘，禁用刷新也不能丢掉根"
    assert target(old_root).read_bytes() == b"metadata"
    assert not target(new_root).exists()
    completed = dict(strm_repo.list_strm_metadata_queue()[0])
    assert completed["status"] == "completed"
    assert completed["refresh_policy_json"] == queued["refresh_policy_json"]
    old_events = strm_repo.list_strm_refresh_entries()
    assert {e["path"] for e in old_events} == ({str(target(old_root).parent)} if enabled else set())
    assert all(e["allow_emby"] == allow_emby for e in old_events)

    # 新配置只由下一次真实 full 扫描形成新快照；不能因消费者提前写了新根而跳过刷新。
    latest_policy = replace(policy, media_root=str(new_root / strm.STRM_SUBDIR))
    stats = scan(client, new_root, latest_policy)
    assert stats["metadata_queued"] == 1, stats
    latest = dict(strm_repo.list_strm_metadata_queue()[0])
    assert latest["revision"] == queued["revision"] + 1
    assert json.loads(latest["refresh_policy_json"])["request_owners"] == owners
    assert strm_repo.list_strm_refresh_entries() == old_events
    assert worker._process_one()
    assert target(new_root).read_bytes() == b"metadata"
    assert strm_repo.list_strm_index("guangya-meta:source")[0]["strm_path"] == str(target(new_root))
    entries = strm_repo.list_strm_refresh_entries()
    paths = {str(target(root).parent) for root in (old_root, new_root)} if enabled else set()
    assert {e["path"] for e in entries} == paths
    assert all(e["allow_emby"] == allow_emby for e in entries)
    work = refresh_work()
    assert {w["work_key"] for w in work} == {refresh_work_key(path, allow_emby) for path in paths}
    for row in work:
        assert {key: row[key] for key in owners[0]} == owners[0]
    # 随后的已同步 full 不重置事件令牌、扩大 provider 或丢弃请求归属。
    assert scan(client, new_root, latest_policy)["metadata_skipped"] == 1
    assert strm_repo.list_strm_refresh_entries() == entries
    handoff.reset_mock()
    handoff.return_value = {"Jellyfin": "queued"}
    worker._flush_media_refresh(force=True)
    if enabled:
        handoff.assert_called_once_with(sorted(paths), immediate=True, allow_emby=allow_emby)
    else:
        handoff.assert_not_called()
    assert strm_repo.list_strm_refresh_entries() == []
    assert refresh_work() == []


def test_legacy_empty_media_root_uses_current_root_and_keeps_owner_scope(runtime):
    base, remote, client, worker, settings, _, owners = runtime
    payload = strm._metadata_queue_payload(
        remote, "Film", str(base / "A"), source_id="source", source_name="source",
        refresh_policy=StrmRefreshPolicy(allow_emby=False, request_owners=owners),
    )
    assert payload["refresh_policy"]["media_root"] == ""
    strm_repo.enqueue_strm_metadata_jobs([payload])
    settings["STRM_ROOT"] = str(base / "B")
    assert worker._process_one()
    assert not target(base / "A").exists()
    assert target(base / "B").read_bytes() == b"metadata"
    assert dict(strm_repo.list_strm_metadata_queue()[0])["status"] == "completed"
    entries = strm_repo.list_strm_refresh_entries()
    assert [(e["path"], e["allow_emby"]) for e in entries] == [(str(target(base / "B")), False)]
    assert refresh_work()[0]["request_id"] == owners[0]["request_id"]
    policy = StrmRefreshPolicy(media_root=str(base / "B" / strm.STRM_SUBDIR))
    assert scan(client, base / "B", policy)["metadata_skipped"] == 1
    assert strm_repo.list_strm_refresh_entries() == entries


def test_new_revision_during_download_fences_old_root_and_freezes_successor(runtime):
    base, _, client, worker, settings, _, owners = runtime
    old_policy = StrmRefreshPolicy(
        media_root=str(base / "A" / strm.STRM_SUBDIR), enabled=False,
        allow_emby=False, request_owners=owners,
    )
    assert scan(client, base / "A", old_policy)["metadata_queued"] == 1
    new_policy = replace(old_policy, media_root=str(base / "B" / strm.STRM_SUBDIR), enabled=True, allow_emby=True)
    prepare = strm.prepare_strm_metadata_job

    def supersede_after_download(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        settings["STRM_ROOT"] = str(base / "B")
        assert scan(client, base / "B", new_policy)["metadata_queued"] == 1
        return prepared

    with patch("app.modules.strm_metadata_worker.prepare_strm_metadata_job", side_effect=supersede_after_download):
        assert worker._process_one()
    row = dict(strm_repo.list_strm_metadata_queue()[0])
    assert row["status"] == "queued" and row["revision"] == 2
    assert not list(base.rglob("*.nfo"))
    assert not list(base.rglob("*.part"))
    assert strm_repo.list_strm_refresh_entries() == []
    settings["STRM_ROOT"] = str(base / "C")
    assert worker._process_one()
    assert target(base / "B").is_file(), "新 revision 同样必须使用自己的冻结根，不随再次改配置漂移"
    assert not target(base / "C").exists()
    entries = strm_repo.list_strm_refresh_entries()
    assert [(e["path"], e["allow_emby"]) for e in entries] == [(str(target(base / "B").parent), True)]
    assert refresh_work()[0]["request_id"] == owners[0]["request_id"]
    current_policy = replace(new_policy, media_root=str(base / "C" / strm.STRM_SUBDIR))
    assert scan(client, base / "C", current_policy)["metadata_queued"] == 1
    assert strm_repo.list_strm_refresh_entries() == entries
    assert worker._process_one()
    assert target(base / "C").read_bytes() == b"metadata"
    paths = {str(target(base / root).parent) for root in ("B", "C")}
    assert {entry["path"] for entry in strm_repo.list_strm_refresh_entries()} == paths
    assert {row["work_key"] for row in refresh_work()} == {refresh_work_key(path, True) for path in paths}


def test_mismatched_refresh_root_cannot_ack_completed_and_silently_drop_outbox(runtime):
    base, _, client, _, _, _, owners = runtime
    policy = StrmRefreshPolicy(
        media_root=str(base / "A" / strm.STRM_SUBDIR),
        allow_emby=False, request_owners=owners,
    )
    scan(client, base / "A", policy)
    claimed = strm_repo.claim_due_strm_metadata_jobs(owner="root-policy-test")[0]
    options = {
        "expected_owner": claimed["lease_owner"],
        "expected_lease_generation": claimed["lease_generation"],
        "expected_revision": claimed["revision"],
    }
    with pytest.raises(ValueError, match="刷新"):
        strm_repo.settle_strm_metadata_job(claimed["id"], refresh_paths=[str(target(base / "B"))], **options)
    assert dict(strm_repo.list_strm_metadata_queue()[0])["status"] == "running"
    assert strm_repo.list_strm_refresh_entries() == []
    assert strm_repo.settle_strm_metadata_job(claimed["id"], refresh_paths=[str(target(base / "A"))], **options) == "completed"
    assert strm_repo.list_strm_refresh_entries()[0]["allow_emby"] is False
    assert refresh_work()[0]["request_id"] == owners[0]["request_id"]
