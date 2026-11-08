"""公共巡检投影保留安全缺集接力信息，并可读取历史持久快照。"""
from __future__ import annotations

import json

from app.modules.library_patrol import project_library_patrol_status, serialize_patrol_projection, validate_persisted_patrol_projection


def _finding(title="示例剧", tmdb_id="12345", *, status="updates_available", truncated=False):
    return {"title": title, "tmdb_id": tmdb_id, "status": status, "missing_sample_truncated": truncated,
            "missing_sample": [{"season": 2, "episode": 4}, {"season": 2, "episode": 3}, {"season": 3, "episode": 1}],
            "sources": [{"path": "/private"}], "token": "must-not-leak"}


def _row(raw):
    return {"status": "pending", "outcome": "updates_available", "projection_json": raw,
            "cycle_as_of": "", "cycle_accumulator_json": "{}", "last_finished_at": "2026-08-01 12:00:00", "next_run_at": ""}


def test_public_patrol_result_preserves_safe_resource_followup_without_session_store():
    raw, projection = serialize_patrol_projection({"ok": False, "status": "inconclusive", "data": {
        "as_of": "2026-08-01", "checked_series_count": 3, "missing_episode_count": 3, "findings_truncated": False,
        "findings": [_finding(), _finding("不可靠", "999", status="inconclusive"), _finding("已截断", "998", truncated=True)],
        "sources": [{"server_name": "private", "path": "/secret/media"}],
    }})
    row = _row(raw)
    row["outcome"] = "inconclusive"
    shown = project_library_patrol_status(row, enabled=True)
    assert shown["status"] == "inconclusive"
    assert shown["data"]["findings"] == [
        {"title": "示例剧", "tmdb_id": "12345", "status": "updates_available", "missing_count": 2,
         "missing_sample": [{"season": 2, "episode": 3}, {"season": 2, "episode": 4}], "missing_sample_truncated": False},
        {"title": "示例剧", "tmdb_id": "12345", "status": "updates_available", "missing_count": 1,
         "missing_sample": [{"season": 3, "episode": 1}], "missing_sample_truncated": False},
    ]
    assert any("找资源" in suggestion for suggestion in shown["suggestions"])
    for forbidden in ("private", "secret", "token"):
        assert forbidden not in repr(shown) and forbidden not in raw
    assert validate_persisted_patrol_projection(json.loads(raw)) == projection


def test_historical_projection_remains_readable_and_tampered_projection_is_not_relayed():
    historical = {"as_of": "2026-08-01", "patrol_status": "updates_available", "findings_truncated": False,
                  "checked_series_count": 8, "updates_available_count": 1, "missing_episode_count": 1,
                  "inconclusive_count": 0, "unmapped_series_count": 0, "options": [
                      {"position": 1, "title": "历史剧集", "tmdb_id": "12345", "season": 2, "missing_count": 1, "episode_sample": [3]}]}
    shown = project_library_patrol_status(_row(json.dumps(historical)), enabled=False)
    assert shown["ok"] and shown["data"]["findings"][0]["tmdb_id"] == "12345"
    assert shown["data"]["missing_episode_count"] == 1
    historical["options"][0]["path"] = "/secret/media"
    rejected = project_library_patrol_status(_row(json.dumps(historical)), enabled=False)
    assert rejected["status"] == "inconclusive" and rejected["data"]["findings"] == []
    assert "secret" not in repr(rejected)
