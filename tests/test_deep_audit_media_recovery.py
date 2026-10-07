"""本轮媒体任务恢复审查：真实队列、离线 provider 边界。"""

from __future__ import annotations

import tests  # noqa: F401 -- 必须先隔离运行目录，再导入应用
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.modules import strm_metadata_worker as metadata
from tests.support import isolated_test_database


@pytest.fixture(autouse=True)
def no_network():
    with (
        patch(
            "socket.socket.connect",
            side_effect=AssertionError("real network forbidden"),
        ),
        patch(
            "socket.create_connection",
            side_effect=AssertionError("real network forbidden"),
        ),
    ):
        yield


def metadata_job(**updates):
    job = dict(
        source_id="audit",
        source_name="审查",
        file_id="meta",
        parent_id="parent",
        filename="Film.nfo",
        etag="v1",
        size=24,
        rel_dir="Film",
        target_rel_path="整理/Film/Film.nfo",
    )
    job.update(updates)
    return job


@pytest.mark.parametrize("interruption", ["revision", "cancel", "lease"])
def test_superseded_metadata_failure_does_not_poison_current_work(
    tmp_path, interruption
):
    """准备阶段的旧请求失败，只能影响它拥有的 revision/lease，不能污染新任务。"""
    with isolated_test_database():
        db.enqueue_strm_metadata_jobs([metadata_job()])
        worker = metadata.STRMMetadataWorker()
        worker._client = SimpleNamespace(credentials_current=True, logged_in=True)

        def interrupted_download(*args, **kwargs):
            if interruption == "revision":
                db.enqueue_strm_metadata_jobs(
                    [metadata_job(etag="v2", filename="Renamed.nfo")]
                )
            elif interruption == "cancel":
                db.cancel_strm_metadata_job("audit", "meta", reason="source removed")
            else:
                db.recover_stale_strm_metadata_jobs(force=True, owner=worker._owner)
                db.claim_due_strm_metadata_jobs(owner="successor")
            raise OSError("old download disconnected")

        with (
            patch.object(metadata, "get_bool", return_value=True),
            patch.object(
                metadata,
                "get",
                side_effect=lambda k, d="": str(tmp_path) if k == "STRM_ROOT" else d,
            ),
            patch.object(
                metadata, "prepare_strm_metadata_job", side_effect=interrupted_download
            ),
        ):
            assert worker._process_one() is True

        row = dict(db.list_strm_metadata_queue(status="all")[0])
        expected = {"revision": "queued", "cancel": "cancelled", "lease": "running"}
        assert row["status"] == expected[interruption]
        assert row["attempts"] == 0
        assert db.list_strm_failures(status="all") == [], (
            "旧租约失败不得重新生成可写重试入口"
        )
        assert worker._failed_session == 0
        assert worker._consecutive_failures == 0


def test_current_metadata_failure_remains_retryable(tmp_path):
    with isolated_test_database():
        db.enqueue_strm_metadata_jobs([metadata_job()])
        worker = metadata.STRMMetadataWorker()
        worker._client = SimpleNamespace(credentials_current=True, logged_in=True)
        with (
            patch.object(metadata, "get_bool", return_value=True),
            patch.object(
                metadata,
                "get",
                side_effect=lambda k, d="": str(tmp_path) if k == "STRM_ROOT" else d,
            ),
            patch.object(
                metadata, "prepare_strm_metadata_job", side_effect=OSError("offline")
            ),
        ):
            assert worker._process_one() is True
        row = dict(db.list_strm_metadata_queue(status="all")[0])
        assert row["status"] == "retry_wait"
        assert row["attempts"] == 1
        assert len(db.list_strm_failures(status="all")) == 1
        assert worker._failed_session == 1
