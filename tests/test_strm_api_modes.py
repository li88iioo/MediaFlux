from __future__ import annotations

import json
from dataclasses import replace
from time import monotonic, sleep
from typing import cast
from unittest.mock import Mock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.main import create_app
from app.modules import scheduler, strm
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories import strm as strm_repo
from app.repositories.media_refresh_queue import claim_due_media_refreshes
from app.routes import strm_api
from tests.support import initialized_web_runtime, media_refresh_providers
from tests.test_config_empty_values_api import login
from tests.test_strm_hardening import _TreeClient


def _call_with_scheduler(handler):
    scheduler = Mock()
    scheduler.validate_config.return_value = ""
    scheduler.trigger.return_value = {"ok": True}
    with patch.object(strm_api, "require_api_login"), patch.object(
        strm_api, "get_scheduler", return_value=scheduler
    ):
        response = handler(Mock())
    return scheduler, response


def test_only_explicit_fast_and_full_run_routes_are_registered():
    paths = {
        route.path
        for route in strm_api.router.routes
        if isinstance(route, APIRoute) and route.methods is not None and "POST" in route.methods
    }
    assert "/api/strm/run" not in paths
    assert "/api/strm/run/fast" in paths
    assert "/api/strm/run/full" in paths


def test_fast_run_endpoint_only_processes_trusted_change_queue():
    scheduler, response = _call_with_scheduler(strm_api.run_fast_now)
    scheduler.trigger.assert_called_once_with("manual", sync_mode="fast")
    assert response.status_code == 202


def test_explicit_full_run_endpoint_requests_complete_calibration():
    scheduler, response = _call_with_scheduler(strm_api.run_full_now)
    scheduler.trigger.assert_called_once_with(
        "manual", sync_mode="full", force_full=True
    )
    assert response.status_code == 202


@pytest.mark.parametrize("edited_old_path", [False, True], ids=["missing-old-path", "edited-old-path"])
def test_failure_retry_replays_both_paths_of_a_moved_video(tmp_path, edited_old_path):
    base_url = "http://127.0.0.1:1258"
    source = "source"
    source_key = f"guangya:{source}"
    old_file = GuangYaFile("movie", "Movie.mkv", False, 100, "original", "old")
    moved_file = replace(old_file, parent_id="new", etag="moved")
    cloud = _TreeClient({
        source: [
            GuangYaFile("old", "Old", True, parent_id=source),
            GuangYaFile("new", "New", True, parent_id=source),
        ],
        "old": [old_file],
        "new": [],
    })
    local_source = tmp_path / strm.STRM_SUBDIR / "Audit"
    old_path, new_path = (local_source / directory / "Movie.strm" for directory in ("Old", "New"))

    with (
        patch("socket.socket.connect", side_effect=AssertionError("无法联网")) as connect,
        patch("socket.socket.connect_ex", side_effect=AssertionError("无法联网")) as connect_ex,
        initialized_web_runtime(),
        media_refresh_providers("jellyfin"),
        TestClient(create_app(start_background=False)) as web,
        patch.object(strm, "GuangYaClient", return_value=cloud),
    ):
        headers = login(web)
        saved = web.post("/api/config", headers=headers, json={
            "STRM_ROOT": str(tmp_path), "GY_STRM_BASE_URL": base_url,
            "GY_STRM_SOURCE_DIRS": json.dumps([{"id": source, "name": "Audit"}]),
            "STRM_SKIP_THRESHOLD_MB": "0",
        })
        assert saved.status_code == 200, saved.json()
        sync_options = {"client": cast(GuangYaClient, cloud), "rel_prefix": "Audit", "clean_invalid": False}
        assert strm.sync_strm(source, base_url, str(tmp_path), **sync_options)["generated"] == 1
        old_payload = old_path.read_bytes()
        cloud.tree.update(old=[], new=[moved_file])
        new_path.parent.mkdir(parents=True)
        new_path.write_text("external pointer", encoding="utf-8")
        assert strm.sync_strm(source, base_url, str(tmp_path), **sync_options)["failed"] == 1
        failure_id = db.list_strm_failures(status="open")[0]["id"]
        new_path.unlink()

        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER audit_outbox_unavailable BEFORE INSERT ON strm_refresh_outbox "
                "BEGIN SELECT RAISE(ABORT, 'audit outbox unavailable'); END"
            )
        first = web.post("/api/strm/failures/retry", headers=headers, json={"ids": [failure_id]})
        assert first.status_code == 200, first.json()
        first_result = first.json()
        assert (first_result["resolved"], first_result["failed"], first_result["remaining"]) == (0, 1, 1)
        assert not old_path.exists() and new_path.is_file()
        assert [row["strm_path"] for row in strm_repo.list_strm_path_cleanup(source_key)] == [str(old_path)]
        assert strm_repo.list_strm_refresh_entries() == []
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER audit_outbox_unavailable")

        if edited_old_path:
            edited_payload = b"locally edited pointer"
            old_path.write_bytes(edited_payload)
            blocked = web.post("/api/strm/failures/retry", headers=headers, json={"ids": [failure_id]})
            assert blocked.status_code == 200, blocked.json()
            blocked_result = blocked.json()
            assert (blocked_result["resolved"], blocked_result["failed"], blocked_result["remaining"]) == (0, 1, 1)
            assert old_path.read_bytes() == edited_payload
            assert strm_repo.list_strm_path_cleanup(source_key)
            assert strm_repo.list_strm_refresh_entries() == []
            old_path.write_bytes(old_payload)

        replay = web.post("/api/strm/failures/retry", headers=headers, json={"all": True, "action": "generate"})
        assert replay.status_code == 200, replay.json()
        replay_result = replay.json()
        assert (replay_result["resolved"], replay_result["failed"], replay_result["remaining"]) == (1, 0, 0)
        assert [row["strm_path"] for row in strm_repo.list_strm_index(source_key)] == [str(new_path)]
        assert new_path.read_text(encoding="utf-8") == strm.build_play_url(
            base_url, moved_file.file_id, moved_file.etag, moved_file.size, moved_file.name,
        )
        assert not old_path.exists()
        expected_paths = {str(old_path), str(new_path)}
        assert {entry["path"] for entry in strm_repo.list_strm_refresh_entries()} == expected_paths
        assert strm_repo.list_strm_path_cleanup(source_key) == []

        STRMMetadataWorker()._flush_media_refresh(force=True)
        [refresh] = claim_due_media_refreshes(owner="retry-audit", force=True)
        assert refresh["provider"] == "jellyfin"
        assert set(refresh["paths"]) == expected_paths
        assert strm_repo.list_strm_refresh_entries() == []
        connect.assert_not_called()
        connect_ex.assert_not_called()


def test_full_run_reconciles_registered_old_root_and_keeps_current_deletion_independent(tmp_path):
    old, current = tmp_path / "A", tmp_path / "B"
    cloud = _TreeClient({"source": []})
    sync = scheduler.sync_strm

    with (
        initialized_web_runtime(),
        TestClient(create_app(start_background=False)) as web,
        patch.object(scheduler, "sync_strm", side_effect=lambda **kwargs: sync(
            client=cast(GuangYaClient, cloud), **kwargs,
        )),
    ):
        headers = login(web)

        def save(**updates):
            response = web.post("/api/config", json=updates, headers=headers)
            assert response.status_code == 200, response.json()

        def run_full(*names):
            cloud.tree["source"] = [
                GuangYaFile(name, f"{name}.mkv", False, 100, "etag", "source")
                for name in names
            ]
            assert web.post("/api/strm/run/full", headers=headers).status_code == 202
            deadline = monotonic() + 5
            while monotonic() < deadline:
                runs = web.get("/api/strm/runs").json()
                if runs and runs[0]["status"] != "running" and not scheduler.get_scheduler().status()["running"]:
                    return runs[0]
                sleep(0.01)
            raise AssertionError("STRM full run did not finish")

        save(
            STRM_ROOT=str(old), GY_STRM_BASE_URL="http://127.0.0.1:1258",
            GY_STRM_SOURCE_DIRS=json.dumps([{"id": "source", "name": "Audit"}]),
            STRM_SKIP_THRESHOLD_MB="0",
            STRM_NOTIFY_ENABLED="0",
        )
        old_directory = old / strm.STRM_SUBDIR / "Audit"
        assert run_full("Deleted")["status"] == "success"
        modified = old_directory / "Modified.strm"
        save(STRM_ROOT=str(current))
        # B has no output directory. The older A record is still eligible.
        result = run_full()
        assert result["status"] == "success", result
        assert not (old_directory / "Deleted.strm").exists()
        assert not (current / strm.STRM_SUBDIR).exists()

        save(STRM_ROOT=str(old))
        assert run_full("Modified")["status"] == "success"
        modified.write_bytes(b"external change")
        save(STRM_ROOT=str(current))
        assert run_full("Current")["status"] == "partial"
        result = run_full()
        assert result["status"] == "partial" and result["result"]["stats"]["error_samples"]
        assert modified.read_bytes() == b"external change"
        assert not (current / strm.STRM_SUBDIR / "Audit" / "Current.strm").exists()
        assert [row["file_id"] for row in strm_repo.list_strm_index("guangya:source")] == ["Modified"]
