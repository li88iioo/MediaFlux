from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from shutil import copytree
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import playgy_signing, strm, strm_adoption
from app.modules.strm_adoption import verify_strm_adoption
from app.modules.strm_recovery import MAX_POINTER_BYTES
from app.repositories import strm as strm_repo
from tests.support import PagedDirectoryTestMixin, isolated_test_database


def _video(file_id: str = "video", name: str = "影片.mkv", parent_id: str = "source") -> GuangYaFile:
    return GuangYaFile(file_id, name, False, 1024, "current-etag", parent_id)


def _old_url(file: GuangYaFile) -> bytes:
    # 最早的 MF 生成器直接拼接路径，文件名保留空格和 Unicode。
    return f"http://old-mf.invalid:1258/playgy/{file.file_id}/old-etag/512/{file.name}".encode()


def _put(root: Path, file: GuangYaFile, rel_dir: str, payload: bytes) -> Path:
    path = strm._strm_target(file, rel_dir, str(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_copied_library_verifies_historical_playgy_and_keeps_companions(tmp_path, monkeypatch):
    source, root = tmp_path / "export", tmp_path / "library"
    prefix = "剧集:精选"
    component = strm.safe_path_component(prefix)
    season = str(Path(component, strm.safe_path_component("第一季:特别篇")))
    unsigned = _video("legacy", "电影 一.mkv")
    signed = _video("legacy-signed", "电影 二.mkv")
    escaped = _video("百分号%?", "剧集 #1 {metatube-id}.mkv", "season")
    encoded = _video("云端/文件", "剧集 #2.mkv", "season")
    monkeypatch.setattr(playgy_signing, "get_web_secret", lambda: "old-installation-key")
    old_signature = playgy_signing.sign_playgy(signed.file_id, "old-etag", 512)
    base = "https://archive-mf.invalid:1258/reverse/proxy"
    copies = [
        (unsigned, component, b"\xef\xbb\xbf" + _old_url(unsigned) + b"\r\n"),
        (signed, component, _old_url(signed) + f"?v=1&sig={old_signature}".encode()),
        (escaped, season, strm.build_play_url(base, escaped.file_id, "older", 512, escaped.name).encode()),
        (encoded, season, strm.build_play_url(base, encoded.file_id, "old/etag", 512, encoded.name).encode()),
    ]
    for file, rel_dir, payload in copies:
        _put(source, file, rel_dir, payload)
    sidecars = {"电影 一.zh.srt": b"subtitle", "poster.jpg": b"poster", "movie.nfo": b"metadata"}
    for name, payload in sidecars.items():
        (source / strm.STRM_SUBDIR / component / name).write_bytes(payload)
    copytree(source, root)
    candidates = [(file, strm._strm_target(file, rel_dir, str(root))) for file, rel_dir, _ in copies]
    newcomer = _video("new", "刚加入.mkv")
    new_candidate = (newcomer, strm._strm_target(newcomer, component, str(root)))

    def unexpected_access(*_args, **_kwargs):
        pytest.fail("只读核对访问了网络、签名密钥、数据库或重复投影了路径")

    monkeypatch.setattr(playgy_signing, "get_web_secret", unexpected_access)
    monkeypatch.setattr("socket.getaddrinfo", unexpected_access)
    monkeypatch.setattr("socket.socket.connect", unexpected_access)
    monkeypatch.setattr("app.database.get_conn", unexpected_access)
    monkeypatch.setattr(strm, "_strm_target", unexpected_access)
    rows = verify_strm_adoption(root, prefix, iter([*candidates, new_candidate]))

    assert rows == [
        {
            "file_id": file.file_id, "filename": file.name, "size": file.size, "etag": file.etag,
            "strm_path": str(target),
            "content_fingerprint": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        }
        for (file, target), (_, _, payload) in zip(candidates, copies, strict=True)
    ]
    for row, (_, _, payload) in zip(rows, copies, strict=True):
        assert Path(row["strm_path"]).read_bytes() == payload
    for name, payload in sidecars.items():
        assert (root / strm.STRM_SUBDIR / component / name).read_bytes() == payload


def test_verified_index_allows_rewriting_an_old_pointer(tmp_path):
    video = _video()
    old_url = _old_url(video)
    path = _put(tmp_path, video, "影片", old_url)
    base = "http://new-mf.invalid:1258"
    source_key = "guangya:source"

    class Cloud(PagedDirectoryTestMixin):
        def list_dir(self, directory_id: str, /) -> list[GuangYaFile]:
            assert directory_id == "source"
            return [video]

    def sync() -> dict:
        return strm.sync_strm(
            "source", base, str(tmp_path), client=cast(GuangYaClient, Cloud()),
            rel_prefix="影片", clean_invalid=False, clean_empty_dirs=False,
        )

    with isolated_test_database():
        assert sync()["failed"] == 1
        assert path.read_bytes() == old_url
        rows = verify_strm_adoption(tmp_path, "影片", [(video, path)])
        assert strm_repo.list_strm_index(source_key) == []
        strm_repo.upsert_strm_index_batch(source_key, rows)
        assert sync()["generated"] == 1
        fresh = strm.build_play_url(base, video.file_id, video.etag, video.size, video.name).encode()
        assert path.read_bytes() == fresh
        assert strm_repo.list_strm_index(source_key)[0]["content_fingerprint"] == (
            f"sha256:{hashlib.sha256(fresh).hexdigest()}"
        )


@pytest.mark.parametrize(("case", "error"), [
    ("mixed_id", "文件 ID.*不一致"),
    ("moved_path", "无法匹配"),
    ("removed_remote", "无法匹配"),
    ("repeated_local", "重复的文件 ID"),
])
def test_rejects_unproven_pointers_in_an_otherwise_matching_library(tmp_path, case, error):
    first = _video("first", "第一部.mkv")
    other = _video("other", "第二部.mkv", "correct-dir")
    correct_dir = str(Path("影片", "正确分类"))
    kept = _put(tmp_path, first, "影片", _old_url(first))
    rel_dir, payload = correct_dir, _old_url(other)
    if case == "mixed_id":
        payload = _old_url(_video("unrelated", other.name, "other-source"))
    elif case == "moved_path":
        rel_dir = str(Path("影片", "错放分类"))
    elif case == "repeated_local":
        payload = _old_url(first)
    unproven = _put(tmp_path, other, rel_dir, payload)
    candidates = [(first, kept)]
    if case != "removed_remote":
        target = strm._strm_target(other, correct_dir, str(tmp_path)) if rel_dir != correct_dir else unproven
        candidates.append((other, target))

    with pytest.raises(ValueError, match=error):
        verify_strm_adoption(tmp_path, "影片", candidates)
    assert kept.read_bytes() == _old_url(first)
    assert unproven.read_bytes() == payload


@pytest.mark.parametrize("blocker", ["candidate", "directory"])
def test_rejects_generated_path_collisions(tmp_path, blocker):
    first = _video("first", "第一部.mkv")
    other = _video("other", "第二部.mkv")
    path = _put(tmp_path, first, "影片", _old_url(first))
    other_target = strm._strm_target(other, "影片", str(tmp_path))
    candidates = [(first, path), (other, other_target)]
    if blocker == "candidate":
        third = _video("third", "第二部.mp4")
        candidates.append((third, strm._strm_target(third, "影片", str(tmp_path))))
    else:
        other_target.mkdir()

    with pytest.raises(ValueError, match="生成路径存在冲突"):
        verify_strm_adoption(tmp_path, "影片", candidates)


@pytest.mark.parametrize("kind", ["outside", "relative", "parent_jump"])
def test_rejects_candidate_targets_outside_source(tmp_path, kind):
    first = _video()
    path = _put(tmp_path, first, "影片", _old_url(first))
    other = _video("other", "第二部.mkv")
    target = strm._strm_target(other, "另一来源", str(tmp_path))
    if kind == "relative":
        target = target.relative_to(tmp_path)
    elif kind == "parent_jump":
        target = path.parent / ".." / target.parent.name / target.name

    with pytest.raises(ValueError, match="超出来源目录"):
        verify_strm_adoption(tmp_path, "影片", [(first, path), (other, target)])


@pytest.mark.parametrize("location", ["root", "output", "prefix", "nested", "pointer"])
def test_rejects_symlinks_without_adopting_targets(tmp_path, location):
    root = tmp_path / "library"
    file = _video()
    rel_dir = str(Path("影片", "分类"))
    path = _put(root, file, rel_dir, _old_url(file))
    link = {
        "root": root, "output": root / strm.STRM_SUBDIR,
        "prefix": path.parent.parent, "nested": path.parent, "pointer": path,
    }[location]
    moved = tmp_path / "actual"
    original = moved / path.relative_to(link)
    link.rename(moved)
    link.symlink_to(moved, target_is_directory=location != "pointer")

    with pytest.raises(ValueError, match="符号链接"):
        verify_strm_adoption(root, "影片", [(file, path)])
    assert original.read_bytes() == _old_url(file)


@pytest.mark.parametrize("prefix", ["", "..", "/absolute", "a/../b", "a\\..\\b", "C:relative"])
def test_rejects_prefix_path_components(tmp_path, prefix):
    with pytest.raises(ValueError, match="单个目录名"):
        verify_strm_adoption(tmp_path, prefix, [])


def test_requires_an_existing_directory_with_pointers(tmp_path):
    with pytest.raises(ValueError, match="目录不存在"):
        verify_strm_adoption(tmp_path, "影片", [])
    directory = tmp_path / strm.STRM_SUBDIR / "影片"
    directory.mkdir(parents=True)
    (directory / "poster.jpg").write_bytes(b"poster")
    with pytest.raises(ValueError, match="没有可核对"):
        verify_strm_adoption(tmp_path, "影片", [])


@pytest.mark.parametrize("url", [
    "https://private.invalid/player?secret=do-not-expose&next=/playgy/video/e/1/movie.mkv",
    "https://private.invalid/playgy/video/e/nonnumeric/movie.mkv?secret=do-not-expose",
    "https://private.invalid/playgy/video/e/1/movie.mkv?enc=b64&v=1&sig=" + "a" * 64,
    "https://private.invalid/playgy/video/e/1/movie.mkv\nhttps://private.invalid/do-not-expose",
    "file:///playgy/video/e/1/movie.mkv?secret=do-not-expose",
    "",
])
def test_rejects_non_playgy_pointers_without_exposing_input(tmp_path, url):
    file = _video()
    path = _put(tmp_path, file, "影片", url.encode())
    with pytest.raises(ValueError, match="MediaFlux 播放指针") as error:
        verify_strm_adoption(tmp_path, "影片", [(file, path)])
    assert "private.invalid" not in str(error.value)
    assert "do-not-expose" not in str(error.value)


def test_rejects_oversized_and_special_pointers(tmp_path):
    file = _video()
    path = _put(tmp_path, file, "影片", b"x" * (MAX_POINTER_BYTES + 1))
    with pytest.raises(ValueError, match="大小上限"):
        verify_strm_adoption(tmp_path, "影片", [(file, path)])
    if hasattr(os, "mkfifo"):
        path.unlink()
        os.mkfifo(path)
        with pytest.raises(ValueError, match="必须是普通文件"):
            verify_strm_adoption(tmp_path, "影片", [(file, path)])


@pytest.mark.parametrize(("setting", "error"), [
    ("STRM_SCAN_MAX_DIRECTORIES", "目录数上限"),
    ("STRM_SCAN_MAX_ENTRIES", "条目上限"),
    ("STRM_SCAN_MAX_CANDIDATES", "候选上限"),
])
def test_honors_strm_scan_budgets(tmp_path, monkeypatch, setting, error):
    file = _video()
    rel_dir = str(Path("影片", "分类"))
    path = _put(tmp_path, file, rel_dir, _old_url(file))
    newcomer = _video("new", "另一个.mkv")
    new_target = strm._strm_target(newcomer, rel_dir, str(tmp_path))
    monkeypatch.setenv(setting, "1")
    with pytest.raises(ValueError, match=error):
        verify_strm_adoption(tmp_path, "影片", [(file, path), (newcomer, new_target)])


def test_times_out_during_local_walk(tmp_path, monkeypatch):
    file = _video()
    path = _put(tmp_path, file, "影片", _old_url(file))
    ticks = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setenv("STRM_SCAN_DEADLINE_SECONDS", "1")
    monkeypatch.setattr(strm_adoption, "time", SimpleNamespace(monotonic=lambda: next(ticks, 2.0)))
    with pytest.raises(ValueError, match="总时限"):
        verify_strm_adoption(tmp_path, "影片", [(file, path)])


@pytest.mark.parametrize("stage", ["walk", "candidates"])
def test_cancellation_during_verification_returns_no_partial_rows(tmp_path, monkeypatch, stage):
    video = _video()
    path = _put(tmp_path, video, "影片", _old_url(video))
    newcomer = _video("new", "另一个.mkv")
    new_target = strm._strm_target(newcomer, "影片", str(tmp_path))
    stopped = Event()
    read_pointer = strm_adoption._read_pointer

    def read_and_stop(path: Path, directory: int | Path) -> bytes:
        result = read_pointer(path, directory)
        stopped.set()
        return result

    def candidates() -> Iterator[tuple[GuangYaFile, Path]]:
        assert not stopped.is_set()
        yield video, path
        stopped.set()
        yield newcomer, new_target

    if stage == "walk":
        monkeypatch.setattr(strm_adoption, "_read_pointer", read_and_stop)
    with pytest.raises(ValueError, match="已取消"):
        verify_strm_adoption(tmp_path, "影片", candidates(), should_stop=stopped.is_set)
    assert stopped.is_set()
    assert path.read_bytes() == _old_url(video)
