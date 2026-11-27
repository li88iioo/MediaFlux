"""发行目录收尾的历史凭据，使用真实 SQLite 日志及回收审计。"""

import pytest

from app import database as db
from app.repositories.organize_history import has_settled_release_video
from tests.support import isolated_test_database


@pytest.fixture(autouse=True)
def release_database():
    with isolated_test_database():
        yield


def record_video(
    *, source_dir_id="source", parent_id="release", source="guangya", status="deleted",
    operation_type="delete", role="video", new_path="/library/Example.S01E01.mkv",
    item_status=None, legacy_incomplete=False,
):
    name = "Example.S01E01.mkv"
    current_parent = "library" if status == "success" else parent_id
    log_id = db.add_organize_log(
        source, f"/{parent_id}/{name}", new_path, "incoming", status,
        operation_type=operation_type, source_dir_id=source_dir_id,
        original_parent_id=parent_id, original_name=name, current_parent_id=current_parent,
        current_name=name, target_parent_id="library", legacy_incomplete=legacy_incomplete,
    )
    db.add_organize_log_items(log_id, [{
        "file_id": "incoming", "role": role, "status": item_status or status,
        "original_parent_id": parent_id, "original_name": name,
        "current_parent_id": current_parent, "current_name": name,
    }])
    return log_id


def record_audit(
    log_id: int, *, trigger="duplicate_cleanup", status="success", file_id="incoming",
    parent_id="release", provider="guangya", replacement_file_id="retained",
):
    db.add_organize_delete_audit(
        organize_log_id=log_id, trigger=trigger, status=status, file_id=file_id,
        parent_id=parent_id, provider=provider, replacement_file_id=replacement_file_id,
        reason="同版本保留已归档文件",
    )


@pytest.mark.parametrize("source_dir_id", ["source", "release"])
def test_settled_duplicate_video_counts_for_its_release(source_dir_id):
    log_id = record_video(source_dir_id=source_dir_id)
    record_audit(log_id)

    assert has_settled_release_video("release", "source")
    assert not has_settled_release_video("sibling", "source")


@pytest.mark.parametrize(("log_fields", "audit_fields"), [
    pytest.param({}, None, id="deleted-log-without-audit"),
    pytest.param({}, {"trigger": "manual"}, id="manual-delete"),
    pytest.param({}, {"status": "failed"}, id="failed-recycle"),
    pytest.param({}, {"status": "pending"}, id="unsettled-recycle"),
    pytest.param({}, {"provider": "other"}, id="wrong-audit-provider"),
    pytest.param({}, {"parent_id": "sibling"}, id="wrong-audit-parent"),
    pytest.param({}, {"replacement_file_id": ""}, id="missing-retained-video"),
    pytest.param({}, {"replacement_file_id": "incoming"}, id="same-retained-video"),
    pytest.param({"status": "skipped", "operation_type": "organize"}, {}, id="skipped-video"),
    pytest.param({"operation_type": "organize"}, {}, id="missing-delete-operation"),
    pytest.param({"source_dir_id": "other"}, {}, id="wrong-source"),
    pytest.param({"parent_id": "sibling"}, {"parent_id": "sibling"}, id="wrong-log-parent"),
    pytest.param({"source": "local"}, {}, id="wrong-log-provider"),
    pytest.param({"role": "subtitle"}, {}, id="main-file-is-not-video"),
    pytest.param({"item_status": "skipped"}, {}, id="unsettled-video-item"),
    pytest.param({"legacy_incomplete": True}, {}, id="incomplete-log"),
])
def test_duplicate_evidence_requires_a_settled_video_and_matching_audit(log_fields, audit_fields):
    log_id = record_video(**log_fields)
    if audit_fields is not None:
        record_audit(log_id, **audit_fields)

    assert not has_settled_release_video("release", "source")


def test_companion_recycle_cannot_prove_the_video_was_recycled():
    log_id = record_video()
    db.add_organize_log_items(log_id, [{
        "file_id": "subtitle", "role": "subtitle", "status": "deleted",
        "original_parent_id": "release", "original_name": "Example.S01E01.srt",
    }])
    record_audit(log_id, file_id="subtitle")
    record_audit(log_id, status="failed")

    assert not has_settled_release_video("release", "source")


def test_audit_from_another_log_for_the_same_file_cannot_be_reused():
    record_video()
    record_audit(record_video(source_dir_id="other"))

    assert not has_settled_release_video("release", "source")


def test_archive_evidence_still_requires_its_recorded_destination_path():
    record_video(status="success", operation_type="organize", new_path="")
    assert not has_settled_release_video("release", "source")

    record_video(status="success", operation_type="reorganize")
    assert has_settled_release_video("release", "source")
