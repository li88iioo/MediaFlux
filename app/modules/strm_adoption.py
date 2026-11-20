"""用本轮云端候选核对已有 STRM 目录的索引归属。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, unquote, urlsplit

from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.playgy_signing import decode_playgy_path_token
from app.modules.runtime_diagnostics import (
    _open_existing_ancestor,
    _regular_open_flags,
    _uses_dir_fd,
    _validate_string_components,
)
from app.modules.strm_recovery import MAX_POINTER_BYTES, _pointer


_PLAYGY_PATH = re.compile(r"/playgy/([^/]+)/([^/]+)/[0-9]+/.+\Z")
_SIGNATURE = re.compile(r"[0-9a-fA-F]{64}")


def _playgy_file_id(payload: bytes) -> str:
    if len(payload) > MAX_POINTER_BYTES:
        raise ValueError("本地 STRM 指针超过大小上限")
    try:
        value = payload.decode("utf-8-sig").strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username is not None or "\\" in url.netloc
            or any(char.isspace() for char in url.netloc)
        ):
            raise ValueError
        _ = url.port  # urlsplit 在访问 port 时才校验其格式。
        route = _PLAYGY_PATH.search(url.path)
        if route is None or any(part in {".", ".."} for part in url.path.split("/")):
            raise ValueError

        # 旧实例的签名密钥可能已经轮换，早期指针也可能没有签名。
        # 这里只识别链接结构，归属由本轮候选的文件 ID 和生成路径证明。
        query = parse_qs(url.query, keep_blank_values=True)
        version, signature, encoding = query.get("v"), query.get("sig"), query.get("enc")
        if version is not None or signature is not None or encoding is not None:
            if (
                version != ["1"] or not signature or len(signature) != 1
                or not _SIGNATURE.fullmatch(signature[0])
            ):
                raise ValueError
        file_id = unquote(route[1], errors="strict")
        if encoding is not None:
            if encoding != ["b64"] or not decode_playgy_path_token(unquote(route[2], errors="strict")):
                raise ValueError
            file_id = decode_playgy_path_token(file_id)
        if not file_id or file_id in {".", ".."}:
            raise ValueError
        return file_id
    except ValueError:
        raise ValueError("本地 STRM 不是可核验的 MediaFlux 播放指针") from None


@contextmanager
def _open_directory(path: Path) -> Generator[int | Path, None, None]:
    if _uses_dir_fd():
        descriptor, _, missing, _ = _open_existing_ancestor(path)
        if descriptor is not None:
            try:
                if not missing:
                    yield descriptor
                    return
            finally:
                os.close(descriptor)
    else:
        info, _, _ = _validate_string_components(path)
        if info is not None and stat.S_ISDIR(info.st_mode):
            yield path
            return
    raise ValueError("STRM 来源目录不存在、无法读取或包含符号链接")


def _read_pointer(path: Path, directory: int | Path) -> bytes:
    if isinstance(directory, int):
        # _pointer 按路径重新打开文件。使用已经核对的目录句柄，避免打开时跟随链接。
        fd = os.open(path.name, _regular_open_flags(writable=False), dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("本地 STRM 指针必须是普通文件")
            payload = stream.read(MAX_POINTER_BYTES + 1)
            after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    else:
        before, _, _ = _validate_string_components(path)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise ValueError("本地 STRM 指针必须是普通文件且不能是符号链接")
        payload = _pointer(path)
        after = path.lstat()
    if (
        not os.path.samestat(before, after)
        or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("本地 STRM 指针在核对期间发生变化")
    return payload


def _read_directory(
    directory: Path, max_dirs: int, max_entries: int, check: Callable[[], None],
) -> tuple[dict[Path, tuple[str, str]], set[Path]]:
    pending = [directory]
    directories = {directory}
    entries = 0
    pointers: dict[Path, tuple[str, str]] = {}
    file_ids: set[str] = set()
    while pending:
        check()
        current = pending.pop()
        with _open_directory(current) as handle, os.scandir(handle) as children:
            for entry in children:
                check()
                entries += 1
                if entries > max_entries:
                    raise ValueError("STRM 来源目录超过核对条目上限")
                path = current / entry.name
                if entry.is_symlink() or entry.is_junction():
                    raise ValueError("STRM 来源目录包含符号链接或目录联接")
                if entry.is_dir(follow_symlinks=False):
                    if len(directories) >= max_dirs:
                        raise ValueError("STRM 来源目录超过核对目录数上限")
                    directories.add(path)
                    pending.append(path)
                elif entry.name.lower().endswith(".strm"):
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError("本地 STRM 指针必须是普通文件")
                    payload = _read_pointer(path, handle)
                    file_id = _playgy_file_id(payload)
                    if file_id in file_ids:
                        raise ValueError("本地 STRM 指针包含重复的文件 ID")
                    file_ids.add(file_id)
                    pointers[path] = (file_id, f"sha256:{hashlib.sha256(payload).hexdigest()}")
    if not pointers:
        raise ValueError("STRM 来源目录没有可核对的 STRM 指针")
    return pointers, directories


def verify_strm_adoption(
    root: Path,
    prefix: str,
    candidates: Iterable[tuple[GuangYaFile, Path]],
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """返回当前来源可批量登记的索引行，指纹对应已有指针的原始字节。

    candidates 来自本轮完整扫描，目标为生成器投影到沿用前缀的绝对路径。
    本地每个 STRM 都必须与候选的生成路径和文件 ID 一致，远端新增文件可留待生成。
    任何失败或取消都抛出安全的 ValueError，调用方可复查 should_stop。
    """
    max_dirs, max_entries, max_candidates, seconds = strm._scan_limits()
    deadline = time.monotonic() + seconds

    def check() -> None:
        if should_stop is not None and should_stop():
            raise ValueError("STRM 目录核对已取消")
        if time.monotonic() >= deadline:
            raise ValueError("STRM 目录核对超过总时限")

    check()
    if (
        prefix.strip() in {"", ".", ".."} or PureWindowsPath(prefix).drive
        or any(char in prefix for char in ("/", "\\", "\x00"))
    ):
        raise ValueError("STRM 来源前缀必须是单个目录名")
    try:
        root = root.expanduser().absolute()
        if ".." in root.parts:
            raise ValueError("STRM 根目录不能包含路径跳转")
        component = strm.source_directory_component(prefix)
        directory = root / strm.STRM_SUBDIR / component
        pointers, directories = _read_directory(directory, max_dirs, max_entries, check)
        check()

        targets: set[Path] = set()
        file_ids: set[str] = set()
        rows: list[dict] = []
        for count, (file, target) in enumerate(candidates, 1):
            check()
            if count > max_candidates:
                raise ValueError("STRM 目录核对超过视频候选上限")
            if file.is_dir or not file.file_id or not file.name:
                raise ValueError("STRM 视频候选缺少有效的文件身份")
            if file.file_id in file_ids:
                raise ValueError("STRM 视频候选包含重复的文件 ID")
            file_ids.add(file.file_id)
            if ".." in target.parts or not target.is_relative_to(directory):
                raise ValueError("STRM 视频候选的生成路径超出来源目录")
            if target in targets or target in directories:
                raise ValueError("STRM 视频候选的生成路径存在冲突")
            targets.add(target)
            pointer = pointers.get(target)
            if pointer is None:
                continue
            file_id, fingerprint = pointer
            if file_id != file.file_id:
                raise ValueError("STRM 指针的文件 ID 与该生成路径的候选不一致")
            rows.append({
                "file_id": file.file_id,
                "filename": file.name,
                "size": file.size,
                "etag": file.etag,
                "strm_path": str(target),
                "content_fingerprint": fingerprint,
            })
        check()
        if len(rows) != len(pointers):
            raise ValueError("本地 STRM 存在无法匹配当前来源的指针路径")
        return rows
    except (OSError, RuntimeError, UnicodeError):
        raise ValueError("无法安全读取 STRM 来源目录或指针") from None
