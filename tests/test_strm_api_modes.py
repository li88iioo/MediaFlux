from __future__ import annotations

from unittest.mock import Mock, patch

from app.routes import strm_api


def _call_with_scheduler(handler):
    scheduler = Mock()
    scheduler.validate_config.return_value = ""
    scheduler.trigger.return_value = {"ok": True}
    with patch.object(strm_api, "require_api_login"), patch.object(
        strm_api, "get_scheduler", return_value=scheduler
    ):
        response = handler(Mock())
    return scheduler, response


def test_legacy_run_endpoint_preserves_full_sync_semantics():
    scheduler, response = _call_with_scheduler(strm_api.run_now)
    scheduler.trigger.assert_called_once_with(
        "manual", sync_mode="full", force_full=True
    )
    assert response.status_code == 202


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
