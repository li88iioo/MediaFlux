"""缺集补库工作流的持久化、安全隔离与终态闭环测试。"""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import agent_download_verification as agent_download_verification_repo
from app.agent.domains.downloads.recent_submissions import (
    RecentDownloadSubmissionStore,
    parse_recent_download_verification_context,
)
from app.agent.domains.library.missing_workflow_runtime import (
    MissingMediaWorkflowRuntime,
)
from app.repositories.agent_download_verification import (
    SQLiteMissingMediaWorkflowRepository,
    workflow_followup_context,
    workflow_ref_from_context,
)
from app.agent.models import ToolResult
from tests.support import IsolatedDatabaseTestCase

_VERIFICATION = {
    "title": "The Show",
    "tmdb_id": "12345",
    "season": 2,
    "episode": 3,
    "as_of": "2026-08-03",
}


def _search_result(*, candidate: bool = True) -> dict:
    return {"verification": {**_VERIFICATION, "verified_missing": True, "sources": [{"path":"/secret/library"}]},
            "episodes": [{"episode": _VERIFICATION["episode"], "has_candidate": candidate}]}


def query_workflows(repository, *, owner, limit=10):
    from app.modules import missing_resources
    with patch.object(missing_resources, "SQLiteMissingMediaWorkflowRepository", return_value=repository):
        return missing_resources.missing_workflows(owner=owner, limit=limit)


def _submission_result(request_id: int) -> ToolResult:
    return ToolResult(
        True,
        "accepted",
        "submitted",
        data={"items": [{
            "result_id": "resource-result-secret", "request_id": request_id,
            "target": "qb",
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "created": True,
            "duplicate": False,
            "magnet": "magnet:?xt=urn:btih:must-not-persist",
            "path": "/private/must-not-persist",
        }]},
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

    def test_kernel_runtime_closes_search_selection_submission_verification_chain(self):
        recent = RecentDownloadSubmissionStore()

        runtime = MissingMediaWorkflowRuntime(
            repository=self.repository,
            recent_download_store=recent,
        )
        self.assertTrue(
            runtime.repository.capture_search(owner="session-a", **_search_result())
        )
        followups = runtime.stage_candidates(
            owner="session-a",
            candidates=[
                {
                    "result_id": "resource-result-secret", "verification": _VERIFICATION,
                    "candidate_title": "The.Show.S02E03.1080p",
                    "target": "qb",
                }
            ],
        )
        self.assertEqual(len(followups), 1)
        self.assertEqual(
            self.repository.list_for_owner(owner="session-a")[0]["item_state"],
            "confirmation_required",
        )

        request_id, created = download_requests_repo.create_download_request(
            "kernel-runtime-closed-loop",
            "magnet", verification_targets=[_VERIFICATION], verification_owner="session-a",
            title="The Show S02E03",
            origin="agent:session-a",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(request_id, targets="qb", status="submitted", qb_status="submitted")
        submission = _submission_result(request_id)
        submission.data["source_type"] = "resource_candidates"
        runtime.complete_submission(
            owner="session-a",
            tool_name="resources.download",
            result=submission,
            followups=followups,
        )

        row = self.repository.list_for_owner(owner="session-a")[0]
        self.assertEqual(row["item_state"], "verification_pending")
        self.assertEqual(row["download_request_id"], request_id)
        job = agent_download_verification_repo.get_agent_download_verification(request_id)
        assert job is not None
        self.assertEqual(job["owner"], "session-a")
        captured = recent.get(owner="session-a")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].request_id, request_id)
        assert captured[0].verification is not None
        self.assertEqual(captured[0].verification.episode, 3)

    def test_native_pack_joins_every_frozen_target_only_to_matching_owner_job(self):
        for mode in ("all_targets", "missing_second", "foreign_owner"):
            with self.subTest(mode=mode):
                owner = "pack-" + mode
                runtime = MissingMediaWorkflowRuntime(repository=self.repository)
                proofs = [{**_VERIFICATION, "episode": episode} for episode in (3, 4)]
                searched = _search_result()
                searched["episodes"] = [{"episode": episode, "has_candidate": True} for episode in (3, 4)]
                runtime.repository.capture_search(owner=owner, **searched)
                followups = runtime.stage_candidates(owner=owner, candidates=[{
                    "result_id": "resource-result-secret", "verification": proof,
                    "candidate_title": "The.Show.S02.Pack", "target": "qb",
                } for proof in proofs])
                request_id, _ = download_requests_repo.create_download_request("native-pack-"+mode, "magnet", origin="agent",
                    verification_targets=proofs[:1] if mode == "missing_second" else proofs,
                    verification_owner="foreign" if mode == "foreign_owner" else owner)
                download_requests_repo.update_download_request(request_id, targets="qb", status="submitted", qb_status="submitted")
                result = _submission_result(request_id)
                before = deepcopy(result.to_dict())
                runtime.complete_submission(owner=owner, tool_name="resources.download", result=result, followups=followups)
                rows = {row["episode"]: row for row in self.repository.list_for_owner(owner=owner)}
                for episode in (3, 4):
                    attached = mode == "all_targets" or (mode == "missing_second" and episode == 3)
                    self.assertEqual(rows[episode]["item_state"], "verification_pending" if attached else "selection_required")
                    if attached:
                        self.assertEqual(rows[episode]["download_request_id"], request_id)
                self.assertEqual(result.to_dict(), before)
                request = download_requests_repo.get_download_request(request_id)
                assert request is not None
                self.assertEqual(request["status"], "submitted")

    def test_kernel_runtime_releases_cancelled_confirmation_for_reselection(self):
        runtime = MissingMediaWorkflowRuntime(repository=self.repository)
        runtime.repository.capture_search(owner="session-a", **_search_result())
        followups = runtime.stage_candidates(
            owner="session-a",
            candidates=[
                {
                    "result_id": "resource-result-secret", "verification": _VERIFICATION,
                    "candidate_title": "The.Show.S02E03.1080p",
                    "target": "qb",
                }
            ],
        )
        runtime.release_followups(owner="session-a", followups=followups)

        row = self.repository.list_for_owner(owner="session-a")[0]
        self.assertEqual(row["item_state"], "selection_required")
        assert (self.repository.select_candidate(
                owner="session-a",
                verification=_VERIFICATION,
                candidate_title="The.Show.S02E03.2160p",
                target="guangya",
            )) is not None

    def test_registration_failure_preserves_accepted_receipt_and_recent_request(self):
        recent = RecentDownloadSubmissionStore()
        runtime = MissingMediaWorkflowRuntime(repository=self.repository, recent_download_store=recent)
        runtime.repository.capture_search(owner="session-a", **_search_result())
        followups = runtime.stage_candidates(owner="session-a", candidates=[{
            "result_id": "resource-result-secret", "verification": _VERIFICATION, "candidate_title": "The.Show.S02E03", "target": "qb",
        }])
        request_id, _ = download_requests_repo.create_download_request("registration-error", "magnet", origin="agent")
        download_requests_repo.update_download_request(request_id, targets="qb", status="submitted", qb_status="submitted")
        result = _submission_result(request_id)
        result.data["source_type"] = "resource_candidates"
        original = deepcopy(result.to_dict())
        with patch.object(agent_download_verification_repo, "get_agent_download_verification", side_effect=RuntimeError("storage unavailable")):
            runtime.complete_submission(owner="session-a", tool_name="resources.download", result=result, followups=followups)
        self.assertEqual(result.to_dict(), original)
        self.assertEqual(recent.get(owner="session-a")[0].request_id, request_id)
        self.assertIsNone(agent_download_verification_repo.get_agent_download_verification(request_id))
        self.assertNotEqual(self.repository.list_for_owner(owner="session-a")[0]["item_state"], "verification_pending")
        request = download_requests_repo.get_download_request(request_id)
        assert request is not None
        self.assertEqual(request["status"], "submitted")

    def test_search_projection_is_owner_scoped_and_persists_no_resource_secret(self):
        workflow_id = self.repository.capture_search(owner="session-a", **_search_result())
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
        self.repository.capture_search(owner="session-a", **_search_result())
        ref = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.1080p",
            target="qb",
        )
        assert (ref) is not None
        self.assertIsNone(self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="must-not-reuse",
            target="qb",
        ))
        context = workflow_followup_context({**_VERIFICATION, "library_name": "动漫库"}, ref)
        parsed = parse_recent_download_verification_context(context)
        assert (parsed) is not None
        self.assertEqual((parsed.season, parsed.episode), (2, 3))
        self.assertEqual(parsed.library_name, "动漫库")
        self.assertEqual(workflow_ref_from_context(context), {
            "workflow_id": ref.workflow_id,
            "item_id": ref.item_id,
            "revision": ref.revision,
        })

        request_id, created = download_requests_repo.create_download_request(
            "workflow-closed-loop",
            "magnet", verification_targets=[{**_VERIFICATION, "library_name": "动漫库"}], verification_owner="session-a",
            title="The Show S02E03",
            origin="agent:session-a",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="downloading",
            qb_status="downloading",
        )
        submission = _submission_result(request_id)
        submission.data["source_type"] = "resource_candidates"
        assert context is not None
        context["result_id"] = "resource-result-secret"
        MissingMediaWorkflowRuntime(repository=self.repository).complete_submission(
            owner="session-a", tool_name="resources.download", result=submission, followups=[context],
        )

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
            targets=[{**_VERIFICATION, "result": "visible"}],
        ))
        final_row = restarted.list_for_owner(owner="session-a")[0]
        self.assertEqual(final_row["state"], "visible")
        self.assertEqual(final_row["item_state"], "visible")
        self.assertFalse(restarted.finish_verification(
            request_id=request_id,
            status="visible",
            result="visible",
            targets=[{**_VERIFICATION, "result": "visible"}],
        ))

    def test_pack_verification_projects_each_episode_including_late_attachment(self):
        targets = [{**_VERIFICATION, "episode": episode} for episode in (3, 4)]
        refs = []
        for target in targets:
            search = _search_result()
            search["verification"].update(target)
            search["episodes"] = [{"episode": target["episode"], "has_candidate": True}]
            self.repository.capture_search(owner="session-a", **search)
            ref = self.repository.select_candidate(owner="session-a", verification=target, candidate_title="The.Show.S02.Pack", target="qb")
            assert ref is not None
            refs.append({"workflow_id": ref.workflow_id, "item_id": ref.item_id, "revision": ref.revision})
        request, _ = download_requests_repo.create_download_request("two-episode-pack", "magnet", origin="agent", verification_targets=targets, verification_owner="session-a")
        self.assertTrue(self.repository.attach_submission(workflow_ref=refs[0], request_id=request, verification_enqueued=True))
        job = agent_download_verification_repo.claim_due_agent_download_verification()
        assert job is not None
        results = [{**target, "result": "visible" if target["episode"] == 3 else "missing"} for target in job["targets"]]
        self.assertTrue(agent_download_verification_repo.update_agent_download_verification(
            request, status="attention", result="missing", attempts=3, next_check_at=db.now(),
            expected_lease_generation=job["lease_generation"], targets=results,
        ))
        cold = SQLiteMissingMediaWorkflowRepository(secret_provider=lambda: "workflow-test-secret")
        self.assertTrue(cold.finish_verification(request_id=request, status="attention", result="missing", targets=results))
        self.assertTrue(cold.attach_submission(workflow_ref=refs[1], request_id=request, verification_enqueued=True))
        states = {row["episode"]: row["item_state"] for row in cold.list_for_owner(owner="session-a")}
        self.assertEqual(states, {3: "visible", 4: "attention"})
        self.assertFalse(cold.finish_verification(request_id=request, status="attention", result="missing", targets=results))

    def test_cancel_or_expiry_releases_confirmation_for_reselection(self):
        self.repository.capture_search(owner="session-a", **_search_result())
        first = self.repository.select_candidate(
            owner="session-a",
            verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.1080p",
            target="qb",
        )
        assert (first) is not None
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
        assert (second) is not None
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
        assert (third) is not None

    def test_new_search_stales_previous_active_workflow_and_public_view_is_safe(self):
        first_id = self.repository.capture_search(owner="session-a", **_search_result())
        second_id = self.repository.capture_search(owner="session-a", **_search_result(candidate=False))
        self.assertNotEqual(first_id, second_id)
        rows = self.repository.list_for_owner(owner="session-a")
        states = {str(row["workflow_id"]): str(row["state"]) for row in rows}
        assert first_id is not None and second_id is not None
        self.assertEqual(states[first_id], "stale")
        self.assertEqual(states[second_id], "search_ready")

        public = query_workflows(self.repository, owner="session-a", limit=10)
        self.assertTrue(public["ok"])
        self.assertEqual(public["status"], "attention")
        self.assertEqual(public["total"], 2)
        serialized = json.dumps(public, ensure_ascii=False)
        for secret in (
            "resource-result-secret",
            "magnet:",
            "secret.invalid",
            "/private/",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual({w["workflow_id"] for w in public["workflows"]}, {first_id,second_id})

    def test_listing_limit_keeps_every_episode_of_selected_workflow(self):
        result = {"verification": {"title":"Long Season", "tmdb_id":"98765", "season":1,
                                   "as_of":"2026-08-03", "verified_missing":True},
                  "episodes": [{"episode": e, "has_candidate": True} for e in range(1,13)]}
        self.repository.capture_search(owner="session-a", **result)
        rows = self.repository.list_for_owner(owner="session-a", limit=1)
        self.assertEqual(len(rows), 12)
        public = query_workflows(self.repository, owner="session-a", limit=1)
        self.assertEqual(len(public["workflows"]), 1)
        self.assertEqual(len(public["workflows"][0]["items"]), 12)
