"""缺集补库工作流的持久化、安全隔离与终态闭环测试。"""
from __future__ import annotations

import json

from app import database as db
from app.agent.missing_media_workflows import (
    SQLiteMissingMediaWorkflowRepository,
    list_missing_workflows,
    workflow_followup_context,
    workflow_ref_from_context,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.recent_download_submissions import (
    enqueue_recent_download_library_verification,
    parse_recent_download_verification_context,
)
from tests.support import IsolatedDatabaseTestCase


_VERIFICATION = {
    "title": "The Show",
    "tmdb_id": "12345",
    "season": 2,
    "episode": 3,
    "as_of": "2026-08-03",
}


def _search_result(*, candidate: bool = True) -> ToolResult:
    selected = {
        "result_id": "resource-result-secret",
        "title": "The.Show.S02E03.1080p",
        "site_id": "nyaa",
        "magnet": "magnet:?xt=urn:btih:must-not-persist",
        "url": "https://secret.invalid/resource",
        "path": "/private/must-not-persist",
    } if candidate else None
    return ToolResult(
        True,
        "success",
        "searched",
        data={
            "verification": {
                **_VERIFICATION,
                "verified_missing": True,
                "sources": [{"path": "/secret/library"}],
            },
            "search": {
                "items": [{"private_url": "https://secret.invalid/item"}],
                "recommendation": {"selected": selected, "alternatives": []},
            },
        },
    )


def _submission_result(request_id: int) -> ToolResult:
    return ToolResult(
        True,
        "accepted",
        "submitted",
        data={
            "request_id": request_id,
            "target": "qb",
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "created": True,
            "duplicate": False,
            "magnet": "magnet:?xt=urn:btih:must-not-persist",
            "path": "/private/must-not-persist",
        },
    )


class MissingMediaWorkflowTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_missing_media_workflow_items")
            conn.execute("DELETE FROM agent_missing_media_workflows")
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM download_requests")
        self.repository = SQLiteMissingMediaWorkflowRepository(
            secret_provider=lambda: "workflow-test-secret"
        )

    def test_search_projection_is_owner_scoped_and_persists_no_resource_secret(self):
        workflow_id = self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_episode_resources",
            result=_search_result(),
        )
        self.assertTrue(workflow_id)
        self.assertEqual(len(self.repository.list_for_owner(owner="session-a")), 1)
        self.assertEqual(self.repository.list_for_owner(owner="session-b"), ())

        with db.get_conn() as conn:
            workflow = dict(conn.execute(
                "SELECT * FROM agent_missing_media_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone())
            item = dict(conn.execute(
                "SELECT * FROM agent_missing_media_workflow_items WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone())
        serialized = json.dumps({"workflow": workflow, "item": item}, ensure_ascii=False)
        for secret in (
            "resource-result-secret",
            "magnet:",
            "secret.invalid",
            "/private/",
            "/secret/library",
            "session-a",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(workflow["state"], "selection_required")
        self.assertEqual(item["candidate_title"], "")
        self.assertIsNone(item["download_request_id"])

    def test_selection_submission_restart_and_visible_verification_form_closed_loop(self):
        self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_episode_resources",
            result=_search_result(),
        )
        ref = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.1080p",
            target="qb",
        )
        self.assertIsNotNone(ref)
        self.assertIsNone(self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="must-not-reuse",
            target="qb",
        ))
        context = workflow_followup_context({**_VERIFICATION, "library_name": "动漫库"}, ref)
        parsed = parse_recent_download_verification_context(context)
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.season, parsed.episode), (2, 3))
        self.assertEqual(parsed.library_name, "动漫库")
        self.assertEqual(workflow_ref_from_context(context), {
            "workflow_id": ref.workflow_id,
            "item_id": ref.item_id,
            "revision": ref.revision,
        })

        request_id, created = db.create_download_request(
            "workflow-closed-loop",
            "magnet",
            title="The Show S02E03",
            origin="agent:session-a",
        )
        self.assertTrue(created)
        db.update_download_request(
            request_id,
            targets="qb",
            status="downloading",
            qb_status="downloading",
        )
        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id),
            context,
        ))

        restarted = SQLiteMissingMediaWorkflowRepository(
            secret_provider=lambda: "workflow-test-secret"
        )
        row = restarted.list_for_owner(owner="session-a")[0]
        self.assertEqual(row["state"], "verification_pending")
        self.assertEqual(row["item_state"], "verification_pending")
        self.assertEqual(row["download_request_id"], request_id)
        self.assertTrue(restarted.finish_verification(
            request_id=request_id,
            status="visible",
            result="visible",
        ))
        final_row = restarted.list_for_owner(owner="session-a")[0]
        self.assertEqual(final_row["state"], "visible")
        self.assertEqual(final_row["item_state"], "visible")
        self.assertFalse(restarted.finish_verification(
            request_id=request_id,
            status="visible",
            result="visible",
        ))

    def test_cancel_or_expiry_releases_confirmation_for_reselection(self):
        self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_episode_resources",
            result=_search_result(),
        )
        first = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.1080p",
            target="qb",
        )
        self.assertIsNotNone(first)
        assert first is not None
        ref = {
            "workflow_id": first.workflow_id,
            "item_id": first.item_id,
            "revision": first.revision,
        }
        self.assertTrue(self.repository.release_confirmation(
            owner="session-a", workflow_ref=ref
        ))
        second = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.2160p",
            target="guangya",
        )
        self.assertIsNotNone(second)
        assert second is not None
        active = ({
            "workflow_id": second.workflow_id,
            "item_id": second.item_id,
            "revision": second.revision,
        },)
        self.assertEqual(self.repository.reconcile_confirmations(
            owner="session-a", active_refs=active
        ), 0)
        self.assertEqual(self.repository.reconcile_confirmations(
            owner="session-a", active_refs=()
        ), 1)
        third = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.REPACK",
            target="both",
        )
        self.assertIsNotNone(third)

    def test_new_search_stales_previous_active_workflow_and_public_view_is_safe(self):
        first_id = self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_episode_resources",
            result=_search_result(),
        )
        second_id = self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_episode_resources",
            result=_search_result(candidate=False),
        )
        self.assertNotEqual(first_id, second_id)
        rows = self.repository.list_for_owner(owner="session-a")
        states = {str(row["workflow_id"]): str(row["state"]) for row in rows}
        self.assertEqual(states[first_id], "stale")
        self.assertEqual(states[second_id], "search_ready")

        public = list_missing_workflows(
            {"limit": 10},
            ToolContext(owner="session-a"),
            repository=self.repository,
        )
        self.assertTrue(public.ok)
        self.assertEqual(public.status, "attention")
        self.assertEqual(public.data["total"], 2)
        self.assertGreaterEqual(public.data["attention"], 1)
        serialized = json.dumps(public.data, ensure_ascii=False)
        for secret in (
            first_id,
            second_id,
            "workflow_id",
            "item_id",
            "tmdb_id",
            "resource-result-secret",
            "magnet:",
            "secret.invalid",
            "/private/",
        ):
            self.assertNotIn(secret, serialized)
        self.assertTrue(any("重新搜索" in suggestion for suggestion in public.suggestions))

    def test_listing_limit_keeps_every_episode_of_selected_workflow(self):
        result = ToolResult(
            True,
            "success",
            "searched season",
            data={
                "verification": {
                    "title": "Long Season",
                    "tmdb_id": "98765",
                    "season": 1,
                    "as_of": "2026-08-03",
                    "verified_missing": True,
                },
                "episodes": [
                    {
                        "episode": episode,
                        "search": {
                            "recommendation": {
                                "selected": {"title": f"Long.Season.E{episode:02d}"},
                                "alternatives": [],
                            }
                        },
                    }
                    for episode in range(1, 13)
                ],
            },
        )
        self.repository.capture_search(
            owner="session-a",
            tool_name="library.search_missing_season_resources",
            result=result,
        )
        rows = self.repository.list_for_owner(owner="session-a", limit=1)
        self.assertEqual(len(rows), 12)
        public = list_missing_workflows(
            {"limit": 1},
            ToolContext(owner="session-a"),
            repository=self.repository,
        )
        self.assertEqual(len(public.data["workflows"]), 1)
        self.assertEqual(len(public.data["workflows"][0]["items"]), 12)
