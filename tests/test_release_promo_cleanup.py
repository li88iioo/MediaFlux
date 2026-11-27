"""已归档发行目录的推广残留收尾；使用临时 SQLite 与内存云盘。"""

from dataclasses import replace
from typing import cast

import pytest

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_confirmations import _cleanup_confirmed_source
from app.modules.scraper import TMDBScraper
from tests.support import PagedDirectoryTestMixin, isolated_test_database


PROMO_STEM = "【更多无水印高品质资源请访问 www.Butailing.com】" * 2
PROMO_FILES = {"DOC": 296080, "MKV": 636976, "MP4": 296080}


class ReleaseDrive(PagedDirectoryTestMixin):
    supports_guarded_empty_directory_delete = True

    def __init__(self):
        self.live: dict[str, GuangYaFile] = {}
        self.recycled: list[str] = []
        for directory in ("source", "archive", "other"):
            self.add(directory, "0", directory, is_dir=True)

    def add(self, file_id, parent_id, name, size=0, *, is_dir=False):
        self.live[file_id] = GuangYaFile(
            file_id, name, is_dir, size, f"etag-{file_id}", parent_id, updated_at=100,
        )

    def release(self, directory):
        self.add(directory, "source", directory, is_dir=True)
        for extension, size in PROMO_FILES.items():
            self.add(f"{directory}-{extension}", directory, f"{PROMO_STEM}.{extension}", size)

    def list_dir(self, directory_id):
        return [replace(item) for item in self.live.values() if item.parent_id == directory_id]

    def file_info(self, file_id):
        return replace(self.live[file_id]) if file_id in self.live else None

    def _recycle(self, file_id):
        item = self.live.pop(file_id)
        self.live[item.parent_id].updated_at += 1
        self.recycled.append(file_id)

    def delete(self, file_ids):
        for file_id in file_ids:
            assert not self.live[file_id].is_dir, "目录必须走带空目录条件的回收"
            self._recycle(file_id)
        return True

    def delete_empty_directory(self, file_id, *, expected_etag="", expected_updated_at=0):
        directory = self.live[file_id]
        assert directory.is_dir and not self.list_dir(file_id)
        assert expected_etag or expected_updated_at
        assert not expected_etag or expected_etag == directory.etag
        assert not expected_updated_at or expected_updated_at == directory.updated_at
        self._recycle(file_id)
        return True


@pytest.fixture
def drive():
    with isolated_test_database():
        yield ReleaseDrive()


@pytest.fixture
def organizer(drive):
    # 所有待扫描视频都低于小文件阈值，无需调用识别服务。
    instance = Organizer(client=cast(GuangYaClient, drive), scraper=cast(TMDBScraper, object()))
    try:
        yield instance
    finally:
        instance.close()


def record_episode(drive, directory, episode, *, source="source", status="success", operation_token=""):
    """按整理成功主链路的形状写入主日志、video 明细及归档位置。"""
    file_id = f"episode-{episode}"
    name = f"示例剧.S01E{episode:02d}.mkv"
    target = "archive" if status == "success" else directory
    if status == "success":
        drive.add(file_id, target, name, 800_000_000)
    with db.get_conn() as conn:
        log_id = db.add_organize_log(
            "guangya", f"/{directory}/{name}", f"/archive/{name}" if status == "success" else "",
            file_id, status, "123", source_dir_id=source, original_parent_id=directory,
            original_name=name, current_parent_id=target, current_name=name, target_parent_id="archive",
            media_type="tv", title="示例剧", season=1, episode=episode, operation_token=operation_token, _conn=conn,
        )
        db.add_organize_log_items(log_id, [{
            "file_id": file_id, "role": "video", "original_parent_id": directory,
            "original_name": name, "current_parent_id": target, "current_name": name,
            "target_parent_id": "archive", "target_name": name, "status": status,
            "size": 800_000_000, "etag": f"etag-{file_id}",
        }], _conn=conn)
    if operation_token:
        db.mark_organize_logs_confirmation_actor(operation_token, "human")


def clean(organizer, directories, *, cancel_check=None):
    return organizer._clean_empty_dirs_report(
        [(directory, 1) for directory in directories], protected_source_ids={"source", "archive"},
        release_source_id="source", cancel_check=cancel_check,
    )


def test_five_release_directories_recycle_all_fifteen_files_then_empty_directories(drive, organizer):
    directories = [f"release-{n}" for n in range(5)]
    for directory in directories:
        drive.release(directory)
    for episode in range(1, 9):
        record_episode(drive, directories[(episode - 1) % 5], episode)

    expected = set(directories) | {
        f"{directory}-{ext}" for directory in directories for ext in PROMO_FILES
    }
    report = clean(organizer, directories)

    assert report["cleaned"] == 5
    assert report["promo_files_recycled"] == 15
    assert report["delete_failures"] == 0
    assert sorted(drive.recycled) == sorted(expected)
    assert {item.file_id for item in drive.list_dir("archive")} == {f"episode-{n}" for n in range(1, 9)}
    audits = db.list_organize_delete_audits()
    assert all(row["status"] == "success" for row in audits)
    assert sorted(row["file_id"] for row in audits) == sorted(expected)


def test_generic_empty_cleanup_and_disabled_organize_preserve_old_residue_until_enabled_rerun(drive, organizer):
    drive.release("old")
    record_episode(drive, "old", 1)
    drive.add("ordinary-empty", "source", "空目录", is_dir=True)
    assert organizer.clean_empty_dirs("source") == 1
    assert drive.recycled == ["ordinary-empty"]

    rules = OrganizeRules(target_dir_id="archive", clean_empty=False, notify_enabled=False, link_strm=False)
    _, stats = organizer.organize("source", rules, dry_run=False, post_actions=False)
    assert stats["failed"] == stats["moved"] == 0
    assert drive.recycled == ["ordinary-empty"]

    # 新任务只有旧日志能证明归档；当前扫描中已无可整理的正片。
    rules.clean_empty = True
    for expected_count in (3, 0):
        _, stats = organizer.organize("source", rules, dry_run=False, post_actions=False)
        assert stats["failed"] == stats["moved"] == 0
        assert stats.get("promo_files_recycled", 0) == expected_count
        assert sorted(drive.recycled) == sorted(["ordinary-empty", "old", "old-DOC", "old-MKV", "old-MP4"])
        assert [item.file_id for item in drive.list_dir("archive")] == ["episode-1"]


def test_large_release_ads_require_exact_content_and_preserve_mixed_directories(drive, organizer, monkeypatch):
    size = 15_089_802
    gcid = "A" * 40
    monkeypatch.setattr("app.modules.organize_scan._RELEASE_PROMO_MP4_SIGNATURES", {
        "b8f961d6f6844c4b3d2fafdaa7e3f0f5dd4f118ac41aa18ca4f2030d39dffa30",
    })
    for episode, directory in enumerate(("settled", "mixed", "unproven"), start=1):
        drive.add(directory, "source", directory, is_dir=True)
        drive.add(directory + "-clip", directory, "同一短片的另一发行名.MP4", size)
        drive.live[directory + "-clip"].etag = gcid.lower()
        if directory != "unproven":
            record_episode(drive, directory, episode)
    drive.add("different-content", "mixed", "社 區 最 新 情 報.mp4", size)
    drive.live["different-content"].etag = "B" * 40
    drive.add("wrong-size", "mixed", "同名.mp4", size - 1)
    drive.live["wrong-size"].etag = gcid
    drive.add("no-fingerprint", "mixed", "社区情报.mp4", size)
    drive.live["no-fingerprint"].etag = ""

    rules = OrganizeRules(target_dir_id="archive", clean_empty=True, small_file_mb=80, link_strm=False)
    _, stats = organizer.organize("source", rules, dry_run=False, post_actions=False)
    assert stats.get("promo_files_recycled", 0) == 1 and stats["empty_dirs_cleaned"] == 1
    assert drive.recycled == ["settled-clip", "settled"]
    assert stats["skipped"] == 3
    assert any("缺少正片入库或同版本回收记录" in reason for reason in stats["empty_dir_cleanup_reasons"])
    assert len(drive.list_dir("mixed")) == 4 and len(drive.list_dir("unproven")) == 1
    _, again = organizer.organize("source", rules, dry_run=False, post_actions=False)
    assert again.get("promo_files_recycled", 0) == again["empty_dirs_cleaned"] == 0
    assert drive.recycled == ["settled-clip", "settled"]


@pytest.mark.parametrize("clean_empty", [True, False], ids=["last-confirmation", "duplicate-cleanup-disabled"])
def test_confirmed_release_with_permanent_source_and_release_scoped_history(drive, organizer, monkeypatch, clean_empty):
    monkeypatch.setenv("GY_ORGANIZE_SOURCE_DIRS", '[{"id":"source","name":"永久整理源"}]')
    drive.release("release")
    promo_ids = {item.file_id for item in drive.list_dir("release")}
    payload = {"source_dir_id": "source", "source_parent_id": "release"}
    rules = OrganizeRules(target_dir_id="archive", clean_empty=True)

    drive.add("episode-8", "release", "示例剧.S01E08.mkv", 800_000_000)
    record_episode(drive, "release", 7, source="release", operation_token="recognition-confirm:e07")
    first = {"moved": 1}
    _cleanup_confirmed_source(payload, organizer, rules, first)
    assert {item.file_id for item in drive.list_dir("release")} == promo_ids | {"episode-8"}
    assert first["source_dir_cleanup_not_empty"] == 1
    assert drive.recycled == []

    record_episode(drive, "release", 8, source="release", operation_token="recognition-confirm:e08")
    if clean_empty:
        stats = {"moved": 1}
    else:
        # 同一盘面已有两集成功记录，另一次同版回收只授权清理其原空父链。
        stats = {"moved": 0, "duplicate_recycled": 1, "duplicate_cleanup_parent_ids": ["release"]}
    _cleanup_confirmed_source(payload, organizer, replace(rules, clean_empty=clean_empty), stats)

    assert {item.file_id for item in drive.list_dir("archive")} == {"episode-7", "episode-8"}
    assert "source" in drive.live
    if clean_empty:
        assert stats["promo_files_recycled"] == 3
        assert stats["empty_dirs_cleaned"] == 1
        assert sorted(drive.recycled[:-1]) == sorted(promo_ids)
        assert drive.recycled[-1] == "release"
    else:
        assert stats["source_dir_cleanup_not_empty"] == 1
        assert {item.file_id for item in drive.list_dir("release")} == promo_ids
        assert drive.recycled == []


@pytest.mark.parametrize("unknown", ["采访花絮.MKV", "www.Butailing.com.S01E01.MKV", f"{PROMO_STEM}.zip", "字幕.srt"])
def test_unknown_file_preserves_every_member_of_an_archived_release(drive, organizer, unknown):
    drive.release("mixed")
    record_episode(drive, "mixed", 1)
    drive.add("unknown", "mixed", unknown, PROMO_FILES["MKV"])
    before = set(drive.live)
    clean(organizer, ["mixed"])
    assert set(drive.live) == before
    assert drive.recycled == []


@pytest.mark.parametrize("change", [{"name": "剧组采访.MKV"}, {"etag": "new-content-etag"}], ids=["renamed", "etag-changed"])
def test_promo_file_changed_before_recycle_is_preserved_and_audited_as_blocked(drive, organizer, monkeypatch, change):
    drive.add("release", "source", "发布目录", is_dir=True)
    drive.add("clip", "release", f"{PROMO_STEM}.MKV", PROMO_FILES["MKV"])
    record_episode(drive, "release", 8)
    changed = replace(drive.live["clip"], **change)
    file_info = drive.file_info

    def read_current_file(file_id):
        if file_id == "clip":
            drive.live[file_id] = changed
        return file_info(file_id)

    monkeypatch.setattr(drive, "file_info", read_current_file)
    report = clean(organizer, ["release"])

    assert report["cleaned"] == 0
    assert drive.list_dir("release") == [changed]
    assert drive.recycled == []
    audits = db.list_organize_delete_audits()
    assert [(row["file_id"], row["status"]) for row in audits] == [("clip", "blocked")]


def test_archive_evidence_must_match_both_parent_and_current_source_tree(drive, organizer):
    for directory in ("unarchived", "failed", "wrong-source", "moved", "sibling"):
        drive.release(directory)
    record_episode(drive, "sibling", 1)
    record_episode(drive, "failed", 2, status="failed")
    record_episode(drive, "wrong-source", 3, source="other")
    drive.add("wrapper", "source", "外层目录", is_dir=True)
    drive.live["moved"].parent_id = "wrapper"
    record_episode(drive, "moved", 4)
    # 已扫描的目录通过上级移出了原来源；其自身的 ID、名字及直接父级仍稳定。
    drive.live["wrapper"].parent_id = "other"
    before = set(drive.live)
    clean(organizer, ["unarchived", "failed", "wrong-source", "moved"])
    assert set(drive.live) == before
    assert drive.recycled == []


@pytest.mark.parametrize("interrupt", ["cancel", "provider_failure"])
def test_partial_recycle_resumes_from_live_members_without_replaying_success(drive, organizer, monkeypatch, interrupt):
    drive.release("partial")
    record_episode(drive, "partial", 1)
    members = {item.file_id for item in drive.list_dir("partial")}
    with monkeypatch.context() as scoped:
        if interrupt == "provider_failure":
            delete_files = drive.delete

            def fail_after_first(file_ids):
                for file_id in file_ids:
                    if drive.recycled:
                        raise RuntimeError("模拟云盘拒绝本次回收")
                    delete_files([file_id])
                return True

            scoped.setattr(drive, "delete", fail_after_first)
        report = clean(
            organizer, ["partial"],
            cancel_check=(lambda: bool(drive.recycled)) if interrupt == "cancel" else None,
        )

    assert report["stopped" if interrupt == "cancel" else "delete_failures"]
    assert report["promo_files_recycled"] == 1
    assert len(drive.recycled) == 1
    assert drive.recycled[0] in members
    assert {item.file_id for item in drive.list_dir("partial")} == members - set(drive.recycled)
    report = clean(organizer, ["partial"])
    assert report["cleaned"] == 1 and report["delete_failures"] == 0
    assert report["promo_files_recycled"] == 2
    assert sorted(drive.recycled) == sorted(members | {"partial"})
    assert set(drive.live) == {"source", "archive", "other", "episode-1"}
