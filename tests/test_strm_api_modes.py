from __future__ import annotations

import json
from time import monotonic, sleep
from typing import cast
from unittest.mock import Mock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.main import create_app
from app.modules import scheduler, strm
from app.repositories import strm as strm_repo
from app.routes import strm_api
from tests.support import initialized_web_runtime
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
