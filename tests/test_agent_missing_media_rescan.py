"""补库重搜、真实受理回执与入库核验在同一持久生命周期内收敛。"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import agent_download_verification as agent_download_verification_repo
from app.repositories.agent_download_verification import (
    SQLiteMissingMediaWorkflowRepository,
)
from app.modules.agent_download_verification_scheduler import DownloadLibraryVerificationScheduler
from tests.test_agent_missing_media_workflows import query_workflows
from tests.support import isolated_test_database
from tests.test_agent_missing_media_workflows import _VERIFICATION, _search_result


@pytest.fixture
def frozen_workflow():
    with isolated_test_database():
        repository = SQLiteMissingMediaWorkflowRepository()
        workflow_id = repository.capture_search(owner="owner", **_search_result())
        reference = repository.select_candidate(
            owner="owner", verification=_VERIFICATION,
            candidate_title="The.Show.S02E03.1080p", target="qb",
        )
        assert workflow_id is not None and reference is not None
        request_id, _ = download_requests_repo.create_download_request(
            "rescan-followup", "magnet", title="The.Show.S02E03", origin="agent",
        )
        yield SimpleNamespace(repository=repository, workflow_id=workflow_id,
                              reference=asdict(reference), request_id=request_id)


@pytest.mark.parametrize("rescan_at", ["confirmation", "submitted", "verification_pending"])
@pytest.mark.parametrize("rescans", [1, 3])
def test_rescan_preserves_actual_submission_until_real_scheduler_completion(frozen_workflow, rescan_at, rescans):
    case = frozen_workflow
    if rescan_at != "confirmation":
        assert case.repository.attach_submission(
            workflow_ref=case.reference, request_id=case.request_id,
            verification_enqueued=rescan_at == "verification_pending",
        )
    for _ in range(rescans):
        case.repository.capture_search(owner="owner", **_search_result(candidate=False))
    cold = SQLiteMissingMediaWorkflowRepository()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_missing_media_workflow_items WHERE item_id=?",
                           (case.reference["item_id"],)).fetchone()
    assert row is not None
    if rescan_at == "confirmation":
        # 旧候选失效仅修订一次；真实写入的回执可以稍晚于其他会话的只读搜索。
        assert row["state"] == "stale"
        assert row["revision"] == case.reference["revision"] + 1
        assert not cold.release_confirmation(owner="owner", workflow_ref=case.reference)
        assert cold.attach_submission(workflow_ref=case.reference, request_id=case.request_id, verification_enqueued=True)
    else:
        assert row["state"] == rescan_at
        assert row["download_request_id"] == case.request_id
    download_requests_repo.update_download_request(case.request_id, targets="qb", status="completed", qb_status="completed")
    assert agent_download_verification_repo.enqueue_agent_download_verification(case.request_id, targets=[{**_VERIFICATION}], owner='owner') == "queued"
    job = agent_download_verification_repo.claim_due_agent_download_verification(current_time="2099-01-01 00:00:00")
    assert job is not None
    job["targets"] = [{**target, "result": "visible"} for target in job["targets"]]
    DownloadLibraryVerificationScheduler()._finish(job, status="visible", result="visible", attempts=1, current=db.now())
    verification = agent_download_verification_repo.get_agent_download_verification(case.request_id)
    assert verification is not None and verification["status"] == "visible"
    old = next(row for row in cold.list_for_owner(owner="owner") if row["workflow_id"] == case.workflow_id)
    assert old["item_state"] == old["state"] == "visible"
    assert old["download_request_id"] == case.request_id


@pytest.mark.parametrize("accepted_state", ["verification_pending", "attention", "visible"])
@pytest.mark.parametrize("accepted_episode", [3, 4])
def test_rescan_refreshes_mixed_season_without_losing_accepted_or_terminal_items(accepted_state, accepted_episode):
    with isolated_test_database():
        repository = SQLiteMissingMediaWorkflowRepository()
        result = _search_result()
        result["episodes"] = [
            {"episode": episode, "has_candidate": True}
            for episode in (3, 4)
        ]
        result["verification"].pop("episode")
        original = repository.capture_search(owner="owner", **result)
        selected = repository.select_candidate(owner="owner", verification={**_VERIFICATION, "episode": accepted_episode},
                                               candidate_title=f"The.Show.S02E{accepted_episode:02d}.1080p", target="qb")
        assert selected is not None
        request, _ = download_requests_repo.create_download_request("mixed-season", "magnet", title=f"The.Show.S02E{accepted_episode:02d}", origin="agent")
        assert repository.attach_submission(workflow_ref=asdict(selected), request_id=request, verification_enqueued=True)
        if accepted_state != "verification_pending":
            assert repository.finish_verification(request_id=request, status=accepted_state,
                                                  result="visible" if accepted_state == "visible" else "missing", targets=[{**_VERIFICATION, "episode": accepted_episode, "result": 'visible' if accepted_state == 'visible' else 'missing'}])
        repository.capture_search(owner="owner", **_search_result(candidate=False))
        old = {row["episode"]: row for row in repository.list_for_owner(owner="owner") if row["workflow_id"] == original}
        assert old[accepted_episode]["item_state"] == accepted_state
        assert old[accepted_episode]["download_request_id"] == request
        assert old[7 - accepted_episode]["item_state"] == "stale"
        parent_state = "stale" if accepted_state == "visible" else accepted_state
        assert {row["state"] for row in old.values()} == {parent_state}
        public = query_workflows(repository, owner="owner", limit=10)
        public_old = next(flow for flow in public["workflows"] if len(flow["items"]) == 2)
        assert public_old["state"] == parent_state
        assert public_old["state_label"] != "已入库并可见"
        assert public["attention"] == (1 if accepted_state == "verification_pending" else 2)
        if accepted_state == "verification_pending":
            cold = SQLiteMissingMediaWorkflowRepository()
            assert cold.finish_verification(request_id=request, status="visible", result="visible", targets=[{**_VERIFICATION, "episode": accepted_episode, "result": 'visible'}])
            public = query_workflows(cold, owner="owner", limit=10)
            public_old = next(flow for flow in public["workflows"] if len(flow["items"]) == 2)
            assert public_old["state"] == "stale"
            assert public_old["state_label"] == "流程已过期，需要重新检查"
            assert public["attention"] == 2


def test_season_is_publicly_visible_only_when_every_episode_is_visible():
    with isolated_test_database():
        repository = SQLiteMissingMediaWorkflowRepository()
        result = _search_result()
        result["verification"].pop("episode")
        result["episodes"] = [{"episode": episode, "has_candidate": True} for episode in (3, 4)]
        repository.capture_search(owner="owner", **result)
        for episode in (3, 4):
            reference = repository.select_candidate(owner="owner", verification={**_VERIFICATION, "episode": episode},
                                                    candidate_title=f"The.Show.S02E{episode:02d}", target="qb")
            assert reference is not None
            request, _ = download_requests_repo.create_download_request(f"all-visible-{episode}", "magnet", origin="agent")
            assert repository.attach_submission(workflow_ref=asdict(reference), request_id=request, verification_enqueued=True)
            assert repository.finish_verification(request_id=request, status="visible", result="visible", targets=[{**_VERIFICATION, "episode": episode, "result": 'visible'}])
        public = query_workflows(repository, owner="owner", limit=10)
        assert public["status"] == "healthy"
        assert public["attention"] == 0
        assert len(public["workflows"]) == 1
        assert public["workflows"][0]["state"] == "visible"
        assert public["workflows"][0]["state_label"] == "已入库并可见"
        assert all(item["state"] == "visible" for item in public["workflows"][0]["items"])


@pytest.mark.parametrize("terminal,result", [("visible", "visible"), ("attention", "missing")])
@pytest.mark.parametrize("rescan", [False, True])
def test_attach_absorbs_real_scheduler_terminal_that_arrives_first(frozen_workflow, terminal, result, rescan):
    case = frozen_workflow
    if rescan:
        case.repository.capture_search(owner="owner", **_search_result())
    download_requests_repo.update_download_request(case.request_id, targets="qb", status="completed", qb_status="completed")
    assert agent_download_verification_repo.enqueue_agent_download_verification(case.request_id, targets=[{**_VERIFICATION}], owner='owner') == "queued"
    job = agent_download_verification_repo.claim_due_agent_download_verification(current_time="2099-01-01 00:00:00")
    assert job is not None
    job["targets"] = [{**target, "result": result} for target in job["targets"]]
    DownloadLibraryVerificationScheduler()._finish(job, status=terminal, result=result, attempts=1, current=db.now())
    cold = SQLiteMissingMediaWorkflowRepository()
    assert cold.attach_submission(workflow_ref=case.reference, request_id=case.request_id, verification_enqueued=True)
    old = next(row for row in cold.list_for_owner(owner="owner") if row["workflow_id"] == case.workflow_id)
    assert old["state"] == old["item_state"] == terminal
    assert old["last_error_code"] == ("" if terminal == "visible" else "verification_attention")


@pytest.mark.parametrize("rescans", [1, 3])
def test_cancel_and_reselect_rejects_the_old_reference_after_rescan(frozen_workflow, rescans):
    case = frozen_workflow
    assert case.repository.release_confirmation(owner="owner", workflow_ref=case.reference)
    newer = case.repository.select_candidate(owner="owner", verification=_VERIFICATION,
                                             candidate_title="The.Show.S02E03.REPACK", target="guangya")
    assert newer is not None and newer.revision > case.reference["revision"] + 1
    for _ in range(rescans):
        case.repository.capture_search(owner="owner", **_search_result())
    cold = SQLiteMissingMediaWorkflowRepository()
    assert not cold.attach_submission(workflow_ref=case.reference, request_id=case.request_id, verification_enqueued=True)
    assert cold.attach_submission(workflow_ref=asdict(newer), request_id=case.request_id, verification_enqueued=True)


@pytest.mark.parametrize("candidate", [False, True])
def test_stale_unselected_search_item_is_not_a_frozen_confirmation(candidate):
    with isolated_test_database():
        repository = SQLiteMissingMediaWorkflowRepository()
        original = repository.capture_search(owner="owner", **_search_result(candidate=candidate))
        with db.get_conn() as conn:
            item = conn.execute("SELECT item_id,revision FROM agent_missing_media_workflow_items WHERE workflow_id=?", (original,)).fetchone()
        assert item is not None
        reference = {"workflow_id": original, "item_id": item["item_id"], "revision": item["revision"]}
        repository.capture_search(owner="owner", **_search_result())
        request, _ = download_requests_repo.create_download_request("unfrozen", "magnet", title="The.Show.S02E03", origin="agent")
        assert not repository.attach_submission(workflow_ref=reference, request_id=request, verification_enqueued=True)


def test_actual_submission_cannot_be_rebound_by_duplicate_or_stale_reference(frozen_workflow):
    case = frozen_workflow
    case.repository.capture_search(owner="owner", **_search_result())
    assert case.repository.attach_submission(workflow_ref=case.reference, request_id=case.request_id, verification_enqueued=True)
    other, _ = download_requests_repo.create_download_request("other-submission", "magnet", title="The.Show.S02E03.Other", origin="agent")
    for request in (case.request_id, other):
        assert not case.repository.attach_submission(workflow_ref=case.reference, request_id=request, verification_enqueued=True)
    old = next(row for row in case.repository.list_for_owner(owner="owner") if row["workflow_id"] == case.workflow_id)
    assert old["download_request_id"] == case.request_id
    assert old["item_state"] == "verification_pending"


@pytest.mark.parametrize("verification_enqueued", [False, True])
def test_accepted_task_survives_search_capacity_pressure_and_cold_completion(frozen_workflow, verification_enqueued):
    case = frozen_workflow
    assert case.repository.attach_submission(workflow_ref=case.reference, request_id=case.request_id,
                                             verification_enqueued=verification_enqueued)
    for _ in range(52):
        case.repository.capture_search(owner="owner", **_search_result())
    with db.get_conn() as conn:
        item = conn.execute("SELECT state,download_request_id FROM agent_missing_media_workflow_items WHERE item_id=?",
                            (case.reference["item_id"],)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM agent_missing_media_workflows").fetchone()
    assert item is not None and item["download_request_id"] == case.request_id
    assert item["state"] == ("verification_pending" if verification_enqueued else "submitted")
    assert count is not None and count[0] == 50
    cold = SQLiteMissingMediaWorkflowRepository()
    assert cold.finish_verification(request_id=case.request_id, status="visible", result="visible", targets=[{**_VERIFICATION, "result": 'visible'}])
    with db.get_conn() as conn:
        final = conn.execute(
            "SELECT w.state,i.state AS item_state FROM agent_missing_media_workflows w "
            "JOIN agent_missing_media_workflow_items i ON i.workflow_id=w.workflow_id WHERE i.item_id=?",
            (case.reference["item_id"],),
        ).fetchone()
    assert final is not None and final["state"] == final["item_state"] == "visible"
