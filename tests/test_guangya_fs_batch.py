"""批量规则与既有光鸭执行器的离线行为回归。"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_fs_change as fs
from tests.test_agent_guangya_batch_files import _arguments, environment  # noqa: F401

_LIMIT = 200 * 1024 * 1024


@pytest.fixture
def batch_env(request, monkeypatch):
    env = request.getfixturevalue("environment")
    client = env[0]
    client.directories["source"] = []

    def delete_empty(file_id, *, expected_etag="", expected_updated_at=0):
        current = client.file_info(file_id)
        if current is None or current.etag != expected_etag or client.list_dir(file_id):
            raise RuntimeError("目录非空或已变化")
        return client.delete([file_id])

    monkeypatch.setattr(client, "delete_empty_directory", delete_empty, raising=False)
    return env


def _dir(client, file_id, parent="source", name=None):
    client.directories[parent].append(GuangYaFile(file_id, name or file_id, True, parent_id=parent, etag=file_id))
    client.directories[file_id] = []


def _file(client, file_id, parent="source", *, name=None, size=_LIMIT):
    client.directories[parent].append(GuangYaFile(file_id, name or file_id + ".mp4", False,
                                                parent_id=parent, size=size, etag=file_id))


def _plan(env, **rule):
    client, context = env
    arguments = _arguments(**rule)
    return fs.build_fs_change_plan(client, owner=context.owner, operations=arguments["operations"], trigger_strm=False)


def _run(client, plan):
    fs.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
    job_id = uuid4().hex
    fs.bind_fs_change_plan_job(plan["plan_id"], owner_digest=plan["owner_digest"], expected_fingerprint=plan["fingerprint"],
                               job_id=job_id, queue_until_epoch=10**12)
    return fs.execute_fs_change_plan({"version": 1, "plan_id": plan["plan_id"], "plan_fingerprint": plan["fingerprint"],
                                      "owner_digest": plan["owner_digest"], "credential_generation": 31, "job_id": job_id},
                                     client_factory=lambda: client)


def test_delete_all_small_types_flatten_and_clean_bottom_up(batch_env):
    client = batch_env[0]
    _dir(client, "pack")
    _dir(client, "leaf", "pack")
    for index, extension in enumerate(("jpg", "srt", "torrent", "mp4")):
        _file(client, f"small-{index}", "leaf", name=f"small.{extension}", size=_LIMIT - 1)
    _file(client, "part-1", "leaf", name="ABC_1.mp4")
    _file(client, "part-2", "pack", name="ABC_2.mp4", size=_LIMIT + 1)
    _file(client, "existing")
    before = deepcopy(client.directories)
    plan = _plan(batch_env, trash_filter={"size_lt_bytes": _LIMIT}, target_path="/source", cleanup_empty_dirs=True)
    assert client.directories == before
    assert plan["stats"]["total"] == 8
    assert all(row["require_empty"] for row in plan["operations"] if row["source"]["is_dir"])
    result = _run(client, plan)
    assert not result["partial"]
    assert result["stats"]["trashed"] == 6
    assert result["stats"]["moved"] == 2
    assert {item.file_id for item in client.list_dir("source")} == {"part-1", "part-2", "existing"}
    assert client.file_info("source") is not None


def test_filters_are_and_within_fields_and_preserve_unmatched_companions(batch_env):
    client = batch_env[0]
    _dir(client, "pack")
    _file(client, "selected", "pack", name="AD sample.MP4", size=50)
    _file(client, "too-small", "pack", name="AD small.mp4", size=9)
    _file(client, "upper-bound", "pack", name="AD large.mp4", size=100)
    _file(client, "poster", "pack", name="AD cover.jpg", size=50)
    _file(client, "not-named", "pack", name="movie.mp4", size=50)
    plan = _plan(batch_env, trash_filter={"size_gte_bytes": 10, "size_lt_bytes": 100, "kinds": ["video"],
                                         "extensions": ["mp4"], "name_contains": ["ad", "广告"]}, cleanup_empty_dirs=True)
    assert [row["source"]["file_id"] for row in plan["operations"]] == ["selected"]
    assert not _run(client, plan)["partial"]
    assert client.file_info("pack") and client.file_info("poster")


def test_keep_both_preserves_parts_versions_and_source_rename_collisions(batch_env):
    client = batch_env[0]
    _dir(client, "a", name="A")
    _dir(client, "b", name="B")
    _file(client, "existing", name="ABC_1.mp4", size=101)
    _file(client, "a1", "a", name="ABC_1.mp4", size=102)
    _file(client, "a2", "a", name="ABC_2.mp4", size=103)
    _file(client, "collision", "a", name="ABC_1 (A).mp4", size=104)
    _file(client, "b1", "b", name="ABC_1.mp4", size=105)
    before = {item.file_id: (item.size, item.etag) for item in [*client.list_dir("source"), *client.list_dir("a"), *client.list_dir("b")] if not item.is_dir}
    plan = _plan(batch_env, target_path="/source", cleanup_empty_dirs=True)
    assert not _run(client, plan)["partial"]
    rows = client.list_dir("source")
    assert len(rows) == len({item.name.casefold() for item in rows}) == 5
    assert {item.file_id: (item.size, item.etag) for item in rows} == before
    assert client.file_info("existing").name == "ABC_1.mp4"
    assert client.file_info("a2").name == "ABC_2.mp4"


def test_target_child_and_ancestors_stay_while_other_empty_directories_are_removed(batch_env):
    client = batch_env[0]
    _dir(client, "archive")
    _dir(client, "target-child", "archive", name="target")
    _dir(client, "other")
    _dir(client, "empty", "other")
    _file(client, "video")
    plan = _plan(batch_env, target_path="/source/archive/target", cleanup_empty_dirs=True)
    assert not _run(client, plan)["partial"]
    assert client.file_info("video").parent_id == "target-child"
    assert client.file_info("archive") and client.file_info("target-child")
    assert client.file_info("other") is None and client.file_info("empty") is None


def test_move_to_account_root_does_not_delete_source_root(batch_env):
    client = batch_env[0]
    _file(client, "video")
    plan = _plan(batch_env, target_path="/", cleanup_empty_dirs=True)
    assert not _run(client, plan)["partial"]
    assert client.file_info("video").parent_id == "0"
    assert client.file_info("source") is not None


def test_cleanup_of_initially_empty_directory_rejects_new_content(batch_env):
    client = batch_env[0]
    _dir(client, "empty")
    plan = _plan(batch_env, cleanup_empty_dirs=True)
    assert plan["operations"][0]["require_empty"] is True
    _file(client, "new", "empty", size=1)
    with pytest.raises(fs.GuangYaFSChangeStale):
        _run(client, plan)
    assert client.file_info("new") and client.file_info("empty")


def test_new_content_during_moves_blocks_only_dependent_cleanup(batch_env, monkeypatch):
    client = batch_env[0]
    _dir(client, "pack")
    _file(client, "video", "pack")
    plan = _plan(batch_env, target_path="/source", cleanup_empty_dirs=True)
    original = client.move

    def moving(ids, parent):
        result = original(ids, parent)
        _file(client, "new", "pack", size=1)
        return result

    monkeypatch.setattr(client, "move", moving)
    result = _run(client, plan)
    assert result["partial"] and result["stats"]["trashed"] == 0
    assert client.file_info("video").parent_id == "source"
    assert client.file_info("new") and client.file_info("pack")


def test_failed_file_move_does_not_remove_parent(batch_env, monkeypatch):
    client = batch_env[0]
    _dir(client, "pack")
    _file(client, "video", "pack")
    plan = _plan(batch_env, target_path="/source", cleanup_empty_dirs=True)

    def rejected(*_args):
        raise GuangYaWriteRejected("move", code="rejected")

    monkeypatch.setattr(client, "move", rejected)
    result = _run(client, plan)
    assert result["partial"] and result["stats"]["trashed"] == 0
    assert client.file_info("video") and client.file_info("pack")


@pytest.mark.parametrize("shape", ["items", "depth", "directories"])
def test_incomplete_scans_never_build_partial_batch_plans(batch_env, shape):
    client = batch_env[0]
    if shape == "items":
        for index in range(2001):
            _file(client, f"f{index}", size=1)
    elif shape == "directories":
        for index in range(500):
            _dir(client, f"d{index}")
    else:
        parent = "source"
        for index in range(13):
            current = f"d{index}"
            _dir(client, current, parent)
            parent = current
        _file(client, "deep", parent, size=1)
    before = deepcopy(client.directories)
    with pytest.raises(fs.GuangYaFSChangeError, match="扫描不完整"):
        _plan(batch_env, trash_filter={"size_lt_bytes": _LIMIT}, cleanup_empty_dirs=True)
    assert client.directories == before


def test_two_thousand_objects_fit_one_plan_and_compact_recovery_source(batch_env):
    client = batch_env[0]
    for index in range(2000):
        _file(client, f"f{index}")
    plan = _plan(batch_env, target_path="/target")
    assert len(plan["operations"]) == 2000
    assert "observation_source" not in plan and "observation_ref" not in plan
    assert fs.load_fs_change_plan(plan["plan_id"], owner="owner")["stats"]["total"] == 2000


def test_more_than_four_hundred_writes_keep_every_execution_receipt(batch_env):
    client = batch_env[0]
    for index in range(405):
        _file(client, f"f{index}")
    plan = _plan(batch_env, target_path="/target")
    result = _run(client, plan)
    assert not result["partial"] and result["stats"]["moved"] == 405
    assert len(result["operation_items"]) == 405
    assert all(row["status"] == "completed" for row in result["operation_items"])
    assert len(client.list_dir("target")) == 405


def test_conflict_error_never_overwrites_or_moves(batch_env):
    client = batch_env[0]
    _dir(client, "pack")
    _file(client, "root-video", name="ABC.mp4")
    _file(client, "child-video", "pack", name="ABC.mp4")
    before = deepcopy(client.directories)
    with pytest.raises(fs.GuangYaFSChangeError, match="同名"):
        _plan(batch_env, target_path="/source", conflict="error")
    assert client.directories == before


def test_keep_both_avoids_companion_name_left_in_source(batch_env):
    client = batch_env[0]
    _dir(client, "pack", name="A")
    _file(client, "root-video", name="ABC_1.mp4")
    _file(client, "moving", "pack", name="ABC_1.mp4")
    _file(client, "left-behind", "pack", name="ABC_1 (A).mp4")
    plan = _plan(batch_env, target_path="/source", move_filter={"name_contains": ["ABC_1.mp4"]}, cleanup_empty_dirs=True)
    assert not _run(client, plan)["partial"]
    assert client.file_info("moving").name == "ABC_1 (A)-2.mp4"
    assert client.file_info("left-behind").parent_id == "pack"
    assert client.file_info("pack") is not None


def test_long_conflicting_name_keeps_part_marker_and_extension(batch_env):
    client = batch_env[0]
    _dir(client, "pack", name="long-release-group")
    name = "作品" * 122 + "_2.mp4"
    _file(client, "root-video", name=name)
    _file(client, "moving", "pack", name=name)
    plan = _plan(batch_env, target_path="/source")
    assert not _run(client, plan)["partial"]
    renamed = client.file_info("moving").name
    assert len(renamed) <= 255
    assert renamed.endswith("_2 (long-release-group).mp4")


def test_failed_directory_read_is_not_treated_as_empty(batch_env, monkeypatch):
    client = batch_env[0]
    _dir(client, "pack")
    original = client.list_dir

    def unreadable(parent_id="0"):
        if parent_id == "pack":
            raise RuntimeError("GuangYaReadRejected")
        return original(parent_id)

    monkeypatch.setattr(client, "list_dir", unreadable)
    with pytest.raises(RuntimeError, match="GuangYaReadRejected"):
        _plan(batch_env, cleanup_empty_dirs=True)
    assert client.file_info("pack") is not None


def test_newnsfw_sized_inventory_preserves_all_kept_ids_sizes_and_versions(batch_env):
    client = batch_env[0]
    for index in range(158):
        _dir(client, f"d{index:03}")
    for index in range(11):
        _file(client, f"root-{index}")
    for index in range(270):
        name = f"root-{index % 11}.mp4" if index < 53 else f"keep-{index}.mp4"
        _file(client, f"keep-{index}", f"d{index % 158:03}", name=name, size=_LIMIT + index)
    kept = {item.file_id: (item.size, item.etag) for rows in client.directories.values() for item in rows if not item.is_dir}
    for index in range(630):
        _file(client, f"small-{index}", f"d{index % 158:03}", size=1024)
    plan = _plan(batch_env, trash_filter={"size_lt_bytes": _LIMIT}, target_path="/source", cleanup_empty_dirs=True)
    assert plan["stats"]["total"] == 1058
    assert plan["stats"]["relocate"] == 53
    result = _run(client, plan)
    assert not result["partial"]
    assert result["stats"]["trashed"] == 630 + 158
    final = client.list_dir("source")
    assert len(final) == 281 and not any(item.is_dir for item in final)
    assert len({item.name.casefold() for item in final}) == 281
    assert {item.file_id: (item.size, item.etag) for item in final} == kept
