"""Web Writer 删除下载占用目录项的独立回归：真实隔离库，零 provider 写入。"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaFile
from app.modules import download_staging_cleanup as cleanup
from app.modules.directory_scrape_errors import (
    DirectoryScrapeConflictError,
    public_error_message,
)
from app.routes import guangya_api as routes
from tests.support import isolated_test_database

ACTIVE_DOWNLOAD = ("submitting", "submitted", "downloading", "outcome_unknown", "manual_review")
ACTIVE_ORGANIZE = ("pending", "settling", "queued", "starting", "running")
PROTECTED_ITEMS = ("stage", "nested", "deep", "video", "source", "workspace")


class _DeleteCase:
    def __init__(self):
        # 包含多级祖先、staging 子目录/文件，以及同父目录的无关兄弟项。
        self.nodes = {
            file_id: GuangYaFile(
                file_id, file_id, is_dir, 0 if is_dir else 128,
                f"etag-{file_id}", parent_id, updated_at=77,
            )
            for file_id, parent_id, is_dir in (
                ("workspace", "0", True),
                ("source", "workspace", True),
                ("stage", "source", True),
                ("nested", "stage", True),
                ("deep", "nested", True),
                ("video", "deep", False),
                ("ordinary-dir", "source", True),
                ("ordinary-file", "source", False),
                ("root-file", "0", False),
            )
        }
        self.provider = Mock(logged_in=True)
        self.provider.file_info.side_effect = self.nodes.get
        self.manager = Mock()
        self.manager.start_operation.return_value = {"ok": True, "task_id": "delete-task"}
        self.sequence = 0

    def request(self, **fields):
        self.sequence += 1
        request_id, created = download_requests_repo.create_download_request(f"active-delete-{self.sequence}", "magnet")
        assert created
        state = {
            "targets": "guangya", "status": "submitted", "gy_status": "downloading", "gy_isolated": 1,
            "gy_target_dir": "stage", "gy_staging_parent_dir": "source", "gy_staging_name": "stage",
            "gy_staging_cleanup_status": "retained", "organize_started": 0, "organize_status": "",
        }
        state.update(fields)
        download_requests_repo.update_download_request(request_id, **state)
        return request_id

    def confirmation(self, status, *, source_id="stage"):
        self.sequence += 1
        token = f"active-delete-confirmation-{self.sequence}"
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id="test", source_name=source_id,
            directory_path="/", payload={"source_dir_id": source_id},
            expires_at="2099-01-01 00:00:00",
        )
        db.update_organize_confirmation(token, status=status)
        return token

    def schedule(self, file_id="stage"):
        item = self.nodes[file_id]
        response = routes.delete_item(Mock(), data={
            "file_id": file_id, "expected_name": item.name,
            "expected_parent_id": item.parent_id, "expected_is_dir": item.is_dir,
            "expected_etag": item.etag, "expected_updated_at": item.updated_at,
        })
        assert response.status_code == 202
        assert json.loads(bytes(response.body))["task_id"] == "delete-task"
        call = self.manager.start_operation.call_args
        assert call.args[:2] == ("删除光鸭目录项", item.name)
        assert call.kwargs == {
            "queue_if_busy": False, "dedupe_key": f"guangya-direct-delete:{file_id}",
        }
        self.provider.file_info.assert_not_called()
        self.provider.delete.assert_not_called()
        return call.args[2]

    @staticmethod
    def audit():
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM organize_delete_audit WHERE trigger='web_direct_delete' ORDER BY id"
            ).fetchall()
        assert len(rows) == 1
        return dict(rows[0])

    def blocked(self, callback, file_id="stage", *, request_id=None, secret=""):
        with pytest.raises(DirectoryScrapeConflictError) as raised:
            callback()
        assert raised.value.status_code == 409
        message = public_error_message(raised.value)
        assert message
        if request_id is not None:
            assert f"#{request_id}" in message
        self.provider.delete.assert_not_called()
        self.provider.delete_empty_directory.assert_not_called()
        self.provider.list_dir.assert_not_called()
        audit = self.audit()
        assert audit["status"] == "blocked"
        assert audit["file_id"] == file_id
        assert audit["file_name"] == self.nodes[file_id].name
        assert audit["parent_id"] == self.nodes[file_id].parent_id
        assert audit["reason"] == message
        assert "未调用" in audit["provider_result"]
        if secret:
            assert secret not in message
            assert secret not in json.dumps(audit, ensure_ascii=False)

    def allowed(self, callback, file_id="stage"):
        result = callback()
        assert result["ok"] is True
        assert result["file_id"] == file_id
        assert result["is_dir"] is self.nodes[file_id].is_dir
        assert result["stats"] == {"deleted": 1}
        self.provider.delete.assert_called_once_with([file_id])
        self.provider.list_dir.assert_not_called()
        audit = self.audit()
        assert audit["id"] == result["audit_id"]
        assert audit["status"] == "success"
        assert audit["file_id"] == file_id


@pytest.fixture
def delete_case():
    with isolated_test_database("guangya-active-download-delete.db"):
        case = _DeleteCase()
        with (
            patch.object(routes, "require_api_login"),
            patch.object(routes, "GuangYaClient", return_value=case.provider),
            patch("app.modules.organize_tasks.get_organize_manager", return_value=case.manager),
        ):
            yield case


@pytest.mark.parametrize("gy_status", ACTIVE_DOWNLOAD)
@pytest.mark.parametrize("file_id", PROTECTED_ITEMS)
def test_active_download_protects_staging_contents_and_all_ancestors(delete_case, gy_status, file_id):
    request_id = delete_case.request(gy_status=gy_status)
    delete_case.blocked(delete_case.schedule(file_id), file_id, request_id=request_id)


@pytest.mark.parametrize("gy_status", ACTIVE_DOWNLOAD)
def test_active_download_is_protected_even_with_completed_cleanup_marker(delete_case, gy_status):
    request_id = delete_case.request(gy_status=gy_status, gy_staging_cleanup_status="completed")
    delete_case.blocked(delete_case.schedule(), request_id=request_id)


@pytest.mark.parametrize("organize_status", ACTIVE_ORGANIZE)
@pytest.mark.parametrize("organize_started", (-1, 0, 1))
def test_completed_download_with_active_organize_is_protected(delete_case, organize_status, organize_started):
    request_id = delete_case.request(
        status="completed", gy_status="completed",
        organize_status=organize_status, organize_started=organize_started,
    )
    delete_case.blocked(delete_case.schedule(), request_id=request_id)


@pytest.mark.parametrize("organize_status", ("", None))
@pytest.mark.parametrize("organize_started", (0, 1))
def test_completed_download_waiting_for_organize_is_protected(delete_case, organize_status, organize_started):
    request_id = delete_case.request(
        status="completed", gy_status="completed",
        organize_status=organize_status, organize_started=organize_started,
    )
    delete_case.blocked(delete_case.schedule(), request_id=request_id)


@pytest.mark.parametrize("confirmation_status", ("queued", "running"))
@pytest.mark.parametrize("organize_started", (-1, 0, 1))
def test_active_confirmation_protects_even_with_terminal_organize_and_negative_marker(
    delete_case, confirmation_status, organize_started,
):
    request_id = delete_case.request(
        status="completed", gy_status="completed",
        organize_status="completed", organize_started=organize_started,
    )
    delete_case.confirmation(confirmation_status)
    delete_case.blocked(delete_case.schedule("source"), "source", request_id=request_id)


@pytest.mark.parametrize("file_id", ("ordinary-dir", "ordinary-file", "root-file"))
def test_active_download_does_not_block_unrelated_items(delete_case, file_id):
    delete_case.request()
    delete_case.allowed(delete_case.schedule(file_id), file_id)


@pytest.mark.parametrize("file_id", ("stage", "video", "source"))
@pytest.mark.parametrize("organize_status", ("failed", "completed", "skipped", "stopped"))
def test_completed_download_with_terminal_organize_allows_residual_delete(delete_case, file_id, organize_status):
    delete_case.request(
        status="completed", gy_status="completed", organize_started=1,
        organize_status=organize_status, gy_staging_cleanup_status="failed",
    )
    delete_case.allowed(delete_case.schedule(file_id), file_id)


@pytest.mark.parametrize("file_id", ("stage", "video", "source"))
def test_failed_download_allows_residual_delete(delete_case, file_id):
    delete_case.request(status="failed", gy_status="failed", organize_status="pending")
    delete_case.allowed(delete_case.schedule(file_id), file_id)


@pytest.mark.parametrize("organize_status", ("", None))
def test_empty_organize_with_negative_marker_allows_residual_delete(delete_case, organize_status):
    delete_case.request(
        status="completed", gy_status="completed", organize_status=organize_status, organize_started=-1,
    )
    delete_case.allowed(delete_case.schedule())


@pytest.mark.parametrize("confirmation_status", ("pending", "completed", "cancelled", "expired", "failed"))
def test_nonactive_confirmation_does_not_reinstate_terminal_download_protection(delete_case, confirmation_status):
    delete_case.request(status="completed", gy_status="completed", organize_status="completed")
    delete_case.confirmation(confirmation_status)
    delete_case.allowed(delete_case.schedule())


def test_active_confirmation_for_other_staging_does_not_block(delete_case):
    delete_case.request(status="completed", gy_status="completed", organize_status="completed")
    delete_case.confirmation("running", source_id="ordinary-dir")
    delete_case.allowed(delete_case.schedule())


def test_checks_every_active_request_not_only_the_first(delete_case):
    delete_case.request(gy_target_dir="ordinary-dir", gy_staging_name="ordinary-dir")
    request_id = delete_case.request()
    delete_case.blocked(delete_case.schedule("video"), "video", request_id=request_id)


@pytest.mark.parametrize("file_id", ("ordinary-dir", "ordinary-file"))
@pytest.mark.parametrize("bad_parent", ("", "missing", "ordinary-file", "ordinary-dir"))
def test_empty_missing_nondirectory_and_cyclic_parent_chains_block(delete_case, file_id, bad_parent):
    delete_case.request()
    # 普通兄弟项原本可删除；无法确认其父链时必须保守阻断。
    delete_case.nodes[file_id].parent_id = bad_parent
    delete_case.nodes["ordinary-dir"].parent_id = bad_parent
    delete_case.blocked(delete_case.schedule(file_id), file_id)


@pytest.mark.parametrize("bad_parent", ("", "missing", "stage", "video"))
def test_unverifiable_staging_parent_chain_blocks_ancestor_scope_check(delete_case, bad_parent):
    delete_case.request()
    delete_case.nodes["stage"].parent_id = bad_parent
    delete_case.blocked(delete_case.schedule("ordinary-dir"), "ordinary-dir")


@pytest.mark.parametrize("failed_id", ("source", "stage"))
def test_parent_lookup_failure_is_safe_and_audited(delete_case, failed_id):
    delete_case.request()
    secret = "opaque-provider-read-secret"

    def lookup(file_id):
        if file_id == failed_id:
            raise RuntimeError(secret)
        return delete_case.nodes.get(file_id)

    delete_case.provider.file_info.side_effect = lookup
    delete_case.blocked(delete_case.schedule("ordinary-dir"), "ordinary-dir", secret=secret)


def test_active_request_read_failure_is_safe_and_audited(delete_case):
    secret = "opaque-staging-query-secret"
    callback = delete_case.schedule()
    with patch.object(cleanup, "list_staging_cleanup_requests", side_effect=RuntimeError(secret)) as read:
        delete_case.blocked(callback, secret=secret)
    read.assert_called_once_with(active_only=True)


def test_download_created_after_scheduling_is_protected(delete_case):
    callback = delete_case.schedule()
    request_id = delete_case.request()
    delete_case.blocked(callback, request_id=request_id)


def test_download_reactivated_after_scheduling_is_protected(delete_case):
    request_id = delete_case.request(status="completed", gy_status="completed", organize_status="completed")
    callback = delete_case.schedule()
    download_requests_repo.update_download_request(request_id, gy_status="downloading")
    delete_case.blocked(callback, request_id=request_id)


def test_download_finished_after_scheduling_is_allowed(delete_case):
    request_id = delete_case.request()
    callback = delete_case.schedule()
    download_requests_repo.update_download_request(request_id, status="completed", gy_status="completed", organize_status="completed")
    delete_case.allowed(callback)


@pytest.mark.parametrize("confirmation_status", ("queued", "running"))
def test_confirmation_created_after_scheduling_is_protected(delete_case, confirmation_status):
    request_id = delete_case.request(status="completed", gy_status="completed", organize_status="completed")
    callback = delete_case.schedule()
    delete_case.confirmation(confirmation_status)
    delete_case.blocked(callback, request_id=request_id)


def test_confirmation_finished_after_scheduling_is_allowed(delete_case):
    delete_case.request(status="completed", gy_status="completed", organize_status="completed")
    token = delete_case.confirmation("running")
    callback = delete_case.schedule()
    db.update_organize_confirmation(token, status="completed")
    delete_case.allowed(callback)


def test_active_query_filters_before_limit_and_preserves_default_cleanup_query(delete_case):
    for status in ("completed", "failed"):
        delete_case.request(gy_status=status, organize_status="completed")
    active_id = delete_case.request(gy_staging_cleanup_status="completed")
    rows = cleanup.list_staging_cleanup_requests(active_only=True, limit=1)
    assert [row["id"] for row in rows] == [active_id]
    assert active_id not in {row["id"] for row in cleanup.list_staging_cleanup_requests()}
    with db.get_conn() as conn:
        scoped = cleanup.list_staging_cleanup_requests(source_id="stage", active_only=True, conn=conn)
        assert [row["id"] for row in scoped] == [active_id]
        assert cleanup.list_staging_cleanup_requests(source_id="ordinary-dir", active_only=True, conn=conn) == []


def test_active_query_refuses_to_silently_truncate_protected_requests(delete_case):
    delete_case.request()
    delete_case.request(gy_target_dir="ordinary-dir")
    with pytest.raises(RuntimeError):
        cleanup.list_staging_cleanup_requests(active_only=True, limit=1)


def test_confirmation_queued_during_parent_lookup_invalidates_initial_active_set(delete_case):
    active_a = delete_case.request(gy_target_dir="ordinary-dir", gy_staging_name="ordinary-dir")
    completed_b = delete_case.request(
        status="completed", gy_status="completed", organize_status="completed",
    )
    token_b = delete_case.confirmation("pending", source_id="stage")
    assert {row["id"] for row in cleanup.list_staging_cleanup_requests(active_only=True)} == {active_a}

    def lookup(file_id):
        # 候选 deep 的快照已读取；走到 B 内部的 nested 时，guard 已取得仅含 A 的集合。
        if file_id == "nested":
            get_organize_confirmation_result = db.get_organize_confirmation(token_b)
            assert get_organize_confirmation_result is not None
            assert get_organize_confirmation_result["status"] == "pending"
            db.update_organize_confirmation(token_b, status="queued")
        return delete_case.nodes.get(file_id)

    delete_case.provider.file_info.side_effect = lookup
    callback = delete_case.schedule("deep")
    with patch.object(
        cleanup, "list_staging_cleanup_requests", wraps=cleanup.list_staging_cleanup_requests,
    ) as read:
        delete_case.blocked(callback, "deep")
    # 一次初始查询、一次最终复核，不重复父链扫描。
    assert read.call_count == 2
    assert all(call.kwargs == {"active_only": True} for call in read.call_args_list)
    assert sum(call.args == ("nested",) for call in delete_case.provider.file_info.call_args_list) == 1
    get_organize_confirmation_result_2 = db.get_organize_confirmation(token_b)
    assert get_organize_confirmation_result_2 is not None
    assert get_organize_confirmation_result_2["status"] == "queued"
    assert {row["id"] for row in cleanup.list_staging_cleanup_requests(active_only=True)} == {active_a, completed_b}
    assert "重新确认" in delete_case.audit()["reason"]
