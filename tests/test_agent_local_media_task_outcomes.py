"""整理通知的批次、文件事实和任务序号不能被历史 RSS / 完成状态替代。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db, sdk
from app.repositories import local_media as local_media_repo
from app.modules.local_media_tasks import local_tasks, local_task
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.models import ToolContext, ToolResult
from app.modules.local_media_outcomes import local_media_task_outcome
from tests.support import IsolatedDatabaseTestCase


def _item(action: str, name: str = "本集.mkv") -> dict[str, str]:
    return {
        "role": "video",
        "source_path": f"/private/downloads/{name}",
        "action": action,
    }


@pytest.mark.parametrize(
    ("status", "actions", "expected", "archived", "skipped"),
    [
        ("completed", ["skip"], "conflict_skipped", 0, 1),
        ("completed", ["move"], "archived", 1, 0),
        ("completed", ["replace", "skip"], "partial", 1, 1),
        ("completed", [], "unknown", 0, 0),
        ("planned", ["move", "skip"], "pending", 0, 0),
        ("requires_manual", ["move"], "pending", 0, 0),
        ("failed", ["move"], "failed", 0, 0),
        ("completed", ["unrecognized"], "unknown", 0, 0),
    ],
)
def test_file_outcome_is_not_task_status(status, actions, expected, archived, skipped):
    result = local_media_task_outcome(
        SimpleNamespace(status=status), [_item(action) for action in actions]
    )
    assert result["file_outcome"] == expected
    assert result["archived_video_count"] == archived
    assert result["skipped_video_count"] == skipped
    assert "/private/" not in repr(result)


def test_completed_preview_only_does_not_claim_archival():
    task = SimpleNamespace(status="completed", warning="仅预览模式：未移动文件")
    result = local_media_task_outcome(task, [_item("move")])
    assert result["file_outcome"] == "preview_only"
    assert result["archived_video_count"] == 0


def test_task_outcome_only_retains_basename_and_bounded_names():
    task = SimpleNamespace(status="completed", content_path=r"C:\private\episode.mkv")
    result = local_media_task_outcome(
        task, [_item("move", f"episode-{i}.mkv") for i in range(25)]
    )
    assert result["original_filename"] == "episode.mkv"
    assert len(result["file_names"]) == 20
    assert result["file_names_truncated"]
    assert result["archived_video_count"] == 25


class LocalMediaTaskOutcomeTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_operation_steps")
            conn.execute("DELETE FROM local_media_task_items")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
        self.source_id = local_media_repo.create_local_media_source(
            name="本地来源",
            qb_profile="qb",
            qb_path_prefix="/downloads",
            local_root="/private/downloads",
            owner="admin",
        )


    def _task(
        self,
        *,
        status="completed",
        name="本集.mkv",
        action="skip",
        updated_at="2026-09-12 14:22:00",
    ):
        task_id = local_media_repo.create_local_media_task(
            self.source_id, "", f"/private/downloads/{name}", owner="admin"
        )
        local_media_repo.update_local_media_task(
            task_id,
            owner="admin",
            status=status,
            title="本片",
            error="PRIVATE-ERROR",
            completed_at=updated_at if status == "completed" else "",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_tasks SET updated_at=? WHERE id=?",
                (updated_at, task_id),
            )
        if action:
            local_media_repo.add_local_media_task_item(
                task_id,
                f"/private/downloads/{name}",
                f"/private/library/{name}",
                role="video",
                action=action,
                owner="admin",
            )
        return task_id

    def _list(self, **arguments):
        result = local_tasks(**arguments)
        return ToolResult(True, result["status"], "本地任务事实", data=result)

    def _scan(self, task_ids, outcomes=None):
        return {
            "id": 900,
            "scan_ref": "LM900",
            "owner": "admin",
            "task_ids": task_ids,
            "started_at": "2026-09-12 14:21:00",
            "finished_at": "2026-09-12 14:22:00",
            "summary": {"task_outcomes": outcomes or []},
        }

    def test_completed_receipt_preserves_real_file_outcome_in_public_and_model_data(self):
        cases = (
            (["move"], "archived", 1, 0, 0, True),
            (["skip"], "conflict_skipped", 0, 1, 0, True),
            (["move", "skip"], "partial", 1, 1, 0, True),
            ([], "unknown", 0, 0, 0, False),
            (["move", "unrecognized"], "partial", 1, 0, 1, False),
            (["move"], "preview_only", 0, 0, 1, True),
        )
        for actions, outcome, archived, skipped, unknown, ok in cases:
            with self.subTest(outcome=outcome, unknown=unknown):
                task_id = self._task(action=actions[0] if actions else "")
                for index, action in enumerate(actions[1:], 1):
                    local_media_repo.add_local_media_task_item(task_id,
                        f"/private/downloads/本集-{index}.mkv", f"/private/library/本集-{index}.mkv",
                        role="video", action=action, owner="admin")
                if outcome == "preview_only":
                    local_media_repo.update_local_media_task(task_id, warning="仅预览模式：未移动文件", owner="admin")
                accepted = ToolResult(True, "accepted", "任务已重新排队",
                    data={"task_id": task_id}, model_data={"task_id": task_id}, effect_metadata={
                        "completion": {"kind": "local_media_task", "task_id": task_id,
                            "operation": "retry"}})
                result = asyncio.run(wait_for_effect_completion(accepted,
                    tool="local.retry", context=ToolContext(), timeout_seconds=0))
                expected = {"file_outcome": outcome, "video_count": len(actions),
                    "archived_video_count": archived, "skipped_video_count": skipped,
                    "unknown_video_count": unknown}
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.ok, ok)
                for key, value in expected.items():
                    self.assertEqual(result.data[key], value)
                    self.assertEqual(result.data["background_job"][key], value)
                    self.assertEqual(result.to_model_dict()["data"][key], value)
                self.assertIn(f"归档 {archived} 个视频", result.summary)
                if outcome == "preview_only":
                    self.assertIn("预览已完成", result.summary)
                    self.assertIn("未移动文件", result.summary)
                else:
                    self.assertIn(f"冲突跳过 {skipped} 个视频", result.summary)
                if not ok:
                    self.assertTrue(result.error)
                self.assertEqual(result.data["task_id"], task_id)

    def test_list_exposes_safe_original_name_and_conflict_result(self):
        self._task()
        result = self._list(scope="history")
        item = result.data["items"][0]
        assert item["original_filename"] == "本集.mkv"
        assert item["status"] == "completed"
        assert item["file_outcome"] == "conflict_skipped"
        assert item["archived_video_count"] == 0
        assert item["skipped_video_count"] == 1
        assert item["updated_at"] == "2026-09-12 14:22:00"
        assert item["completed_at"] == "2026-09-12 14:22:00"
        assert item["error"] == "PRIVATE-ERROR"
        assert item["content_path"] == "/private/downloads/本集.mkv"
        assert item["task_id"] == item["id"]

    def test_latest_scan_uses_only_its_members(self):
        old = self._task(name="昨天入库.mkv", action="move")
        current = self._task(name="本次冲突.mkv")
        waiting = self._task(status="requires_manual", name="本次待确认.mkv", action="")
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            return_value=self._scan([current, waiting]),
        ):
            result = self._list(scope="latest_scan")
        assert len(result.data["items"]) == 2
        assert result.data["scan_ref"] == "LM900"
        assert {item["original_filename"] for item in result.data["items"]} == {
            "本次冲突.mkv",
            "本次待确认.mkv",
        }
        assert result.data["items"][0]["task_id"] == current
        assert old != current

    def test_missing_scan_does_not_fall_back_to_history(self):
        self._task(name="历史入库.mkv", action="move")
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            side_effect=LookupError,
        ):
            result = self._list(scope="latest_scan")
        assert result.status == "not_recorded"
        assert result.data["items"] == []
        assert result.data["scan_recorded"] is False

    def test_deleted_scan_member_is_not_replaced_with_unrelated_history(self):
        task_id = self._task()
        self._task(name="其他历史.mkv", action="move")
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            return_value=self._scan([task_id, 999999]),
        ):
            result = self._list(scan_ref="LM900")
        assert result.data["total"] == 1
        assert result.data["missing_task_count"] == 1
        assert result.data["missing_tasks_excluded"] is True

    def test_scan_preserves_notification_outcome_after_task_retry(self):
        task_id = self._task(status="requires_manual", action="")
        snapshot = {
            "task_id": task_id,
            "status": "completed",
            "original_filename": "当时冲突.mkv",
            "file_names": ["当时冲突.mkv"],
            "file_outcome": "conflict_skipped",
            "video_count": 1,
            "archived_video_count": 0,
            "skipped_video_count": 1,
            "completed_at": "2026-09-12 14:22:00",
        }
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            return_value=self._scan([task_id], [snapshot]),
        ):
            result = self._list(scope="skipped", scan_ref="LM900")
        item = result.data["items"][0]
        assert item["status"] == "completed"
        assert item["current_status"] == "requires_manual"
        assert item["file_outcome"] == "conflict_skipped"
        assert item["outcome_basis"] == "scan_snapshot"
        assert item["can_inspect"]

    def test_reported_waiting_task_remains_waiting_fact_after_current_failure(self):
        task_id = self._task(status="failed", action="")
        snapshot = {
            "task_id": task_id,
            "status": "requires_manual",
            "original_filename": "当时待确认.mkv",
            "file_outcome": "pending",
            "video_count": 0,
            "archived_video_count": 0,
            "skipped_video_count": 0,
            "updated_at": "2026-09-12 14:22:00",
        }
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            return_value=self._scan([task_id], [snapshot]),
        ):
            result = self._list(scope="latest_scan")
        item = result.data["items"][0]
        assert item["status"] == "requires_manual"
        assert item["current_status"] == "failed"
        assert item["reason_code"] == "manual_match_required"
        assert item["outcome_basis"] == "scan_snapshot"
        assert item["can_inspect"] is False
        assert item["can_retry"] is True

    def test_status_filter_and_updated_sort_precede_limit(self):
        self._task(
            name="老ID最近更新.mkv", updated_at="2026-09-12 14:22:00", action="move"
        )
        for index in range(105):
            self._task(
                name=f"新ID旧时间-{index}.mkv",
                updated_at="2026-09-11 14:22:00",
                status="planned",
            )
        result = self._list(scope="history", limit=1)
        assert result.data["items"][0]["original_filename"] == "老ID最近更新.mkv"
        all_result = self._list(scope="all", limit=1)
        assert all_result.data["items"][0]["original_filename"] == "老ID最近更新.mkv"

    def test_skipped_scope_uses_video_actions_not_rss_or_task_status(self):
        self._task(name="确实归档.mkv", action="move")
        self._task(name="冲突跳过.mkv", action="skip")
        self._task(name="尚未执行.mkv", status="planned", action="skip")
        result = self._list(scope="skipped")
        assert [item["original_filename"] for item in result.data["items"]] == [
            "冲突跳过.mkv"
        ]

    def test_scan_pagination_reaches_all_25_tasks_and_keeps_current_action_refs(self):
        task_ids = [
            self._task(
                name=f"批次-{index}.mkv",
                status="requires_manual" if index >= 20 else "completed",
            )
            for index in range(25)
        ]
        snapshots = [
            {
                "task_id": task_id,
                "status": "completed",
                "original_filename": f"批次-{index}.mkv",
                "file_outcome": "conflict_skipped",
                "video_count": 1,
                "skipped_video_count": 1,
            }
            for index, task_id in enumerate(task_ids)
        ]
        with patch(
            "app.modules.local_media_scan_runs.resolve_local_media_scan",
            return_value=self._scan([*task_ids, 999999], snapshots),
        ):
            first = self._list(scope="skipped", scan_ref="LM900", limit=20)
            second = self._list(
                scope="skipped",
                scan_ref="LM900",
                limit=20,
                offset=first.data["next_offset"],
            )
        assert first.data["total"] == 25
        assert first.data["returned"] == 20
        assert first.data["has_more"]
        assert first.data["next_offset"] == 20
        assert second.data["total"] == 25
        assert second.data["returned"] == 5
        assert second.data["missing_task_count"] == 1
        assert second.data["has_more"] is False
        assert second.data["next_offset"] is None
        names = {
            item["original_filename"]
            for item in [*first.data["items"], *second.data["items"]]
        }
        assert len(names) == 25
        item = second.data["items"][0]
        assert item["task_id"] == task_ids[20]
        assert item["status"] == "completed"
        assert item["current_status"] == "requires_manual"
        assert item["file_outcome"] == "conflict_skipped"
        assert item["can_inspect"]
        selected = local_task(item["task_id"])["task"]
        assert selected["id"] == task_ids[20] and selected["status"] == "requires_manual"

    def test_history_pagination_uses_limit_plus_one_without_claiming_global_total(self):
        for index in range(25):
            self._task(name=f"历史分页-{index}.mkv")
        first = self._list(scope="history", limit=20)
        second = self._list(scope="history", limit=20, offset=first.data["next_offset"])
        assert first.data["total"] == 25
        assert first.data["returned"] == 20
        assert first.data["has_more"]
        assert "matched_total" not in first.data
        assert second.data["total"] == 25
        assert second.data["returned"] == 5
        assert second.data["has_more"] is False
        assert second.data["next_offset"] is None
        assert (
            len(
                {
                    item["original_filename"]
                    for item in [*first.data["items"], *second.data["items"]]
                }
            )
            == 25
        )

    def test_offset_validator_rejects_invalid_offsets(self):
        for value in (True, -1, "20"):
            with pytest.raises(ValueError):
                local_tasks(**sdk.prepare_sdk_method("local.tasks", {"offset": value}, owner="test"))

    def test_scan_ref_validator_rejects_paths(self):
        with pytest.raises(ValueError):
            local_tasks(scan_ref="/private/LM900")
