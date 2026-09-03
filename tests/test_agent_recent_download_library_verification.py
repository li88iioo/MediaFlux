"""最近缺集下载完成后的媒体库核验续接测试。"""

from __future__ import annotations

import unittest

from app.agent.models import Evidence, ToolResult
from app.agent.recent_download_submissions import (
    RecentDownloadSubmission,
    RecentDownloadVerification,
    build_recent_download_library_verification,
)

_VERIFICATION_CONTEXT = {
    "title": "The Show",
    "tmdb_id": "12345",
    "season": 2,
    "episode": 3,
    "as_of": "2026-08-03",
    "library_name": "动漫库",
}


def _submission_result(request_id: int | None) -> ToolResult:
    data = {
        "result_id": "safe-result-00000001",
        "created": True,
        "target": "qb",
        "status": "submitted",
        "succeeded": ["qb"],
        "failed": [],
        "duplicate": False,
    }
    if request_id is not None:
        data["request_id"] = request_id
    return ToolResult(True, "accepted", "submitted", data=data)


def _record(*, verification: bool = True) -> RecentDownloadSubmission:
    return RecentDownloadSubmission(
        request_id=1,
        target="qb",
        dispatch_status="submitted",
        succeeded=("qb",),
        failed=(),
        created=True,
        duplicate=False,
        result_status="accepted",
        captured_at="2026-08-03T12:00:00+08:00",
        verification=RecentDownloadVerification(**_VERIFICATION_CONTEXT)
        if verification
        else None,
    )


class RecentDownloadLibraryVerificationProjectionTests(unittest.TestCase):
    def test_missing_context_fails_closed(self):
        result = build_recent_download_library_verification(
            _record(verification=False),
            ToolResult(True, "up_to_date", "audit"),
            position=1,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "precondition_failed")
        self.assertEqual(result.data, {})

    def test_up_to_date_marks_target_visible(self):
        evidence = Evidence("Jellyfin", "已检查剧集库存", "2026-08-03T12:00:00+08:00")
        result = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "up_to_date",
                "audit",
                data={"missing_count": 0, "private_path": "/secret"},
                evidence=[evidence],
            ),
            position=1,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["verification"], "visible")
        self.assertEqual(result.data["missing_count"], 0)
        self.assertNotIn("private_path", result.data)
        self.assertEqual(result.evidence, [evidence])

    def test_up_to_date_with_exact_target_requires_local_visibility(self):
        inconclusive = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "up_to_date",
                "audit",
                data={
                    "missing_count": 0,
                    "target_aired": False,
                    "target_local": False,
                    "target_missing": False,
                },
            ),
            position=1,
        )
        self.assertFalse(inconclusive.ok)
        self.assertEqual(inconclusive.status, "inconclusive")
        self.assertEqual(inconclusive.data["verification"], "inconclusive")
        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "up_to_date",
                "audit",
                data={
                    "missing_count": 0,
                    "target_aired": True,
                    "target_local": True,
                    "target_missing": False,
                },
            ),
            position=1,
        )
        self.assertTrue(visible.ok)
        self.assertEqual(visible.status, "up_to_date")
        self.assertEqual(visible.data["verification"], "visible")

    def test_missing_complete_sample_and_truncated_sample_are_distinguished(self):
        missing = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "updates_available",
                "audit",
                data={
                    "missing_count": 2,
                    "missing_sample": [{"season": 2, "episode": 3}],
                    "missing_sample_truncated": False,
                },
            ),
            position=1,
        )
        self.assertEqual(missing.status, "updates_available")
        self.assertEqual(missing.data["verification"], "missing")
        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "updates_available",
                "audit",
                data={
                    "missing_count": 1,
                    "missing_sample": [{"season": 2, "episode": 4}],
                    "missing_sample_truncated": False,
                },
            ),
            position=1,
        )
        self.assertEqual(visible.status, "up_to_date")
        self.assertEqual(visible.data["verification"], "visible")
        inconclusive = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "updates_available",
                "audit",
                data={
                    "missing_count": 20,
                    "missing_sample": [{"season": 2, "episode": 4}],
                    "missing_sample_truncated": True,
                },
            ),
            position=1,
        )
        self.assertFalse(inconclusive.ok)
        self.assertEqual(inconclusive.status, "inconclusive")
        self.assertEqual(inconclusive.data["verification"], "inconclusive")

    def test_exact_target_projection_wins_when_missing_sample_is_truncated(self):
        missing = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "updates_available",
                "audit",
                data={
                    "missing_count": 180,
                    "missing_sample": [{"season": 2, "episode": 1}],
                    "missing_sample_truncated": True,
                    "target_aired": True,
                    "target_local": False,
                    "target_missing": True,
                },
            ),
            position=1,
        )
        self.assertEqual(missing.data["verification"], "missing")
        self.assertEqual(missing.data["library_name"], "动漫库")
        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "updates_available",
                "audit",
                data={
                    "missing_count": 179,
                    "missing_sample": [{"season": 2, "episode": 1}],
                    "missing_sample_truncated": True,
                    "target_aired": True,
                    "target_local": True,
                    "target_missing": False,
                },
            ),
            position=1,
        )
        self.assertEqual(visible.data["verification"], "visible")
        self.assertEqual(visible.status, "up_to_date")
