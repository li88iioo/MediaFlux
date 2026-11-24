"""MediaFlux GCID 清单导出、校验与 v2 导入标准。"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from app.clients.guangya import GuangYaClient, GuangYaFile, normalize_provider_md5

FORMAT_NAME = "mediaflux.gcid-manifest"
FORMAT_VERSION = 2
MAX_TARGET_DIR_ID_LENGTH = 256
MAX_MANIFEST_FILES = 10_000
MAX_PATH_LENGTH = 1024
MAX_GCID_LENGTH = 256
# 每条路径最多占 6 * 1024 个 JSON 字节。64 MiB 容纳一万条记录、
# GCID/MD5、旧版两空格缩进及预览请求的外层字段。
MAX_GCID_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_ROOT_KEYS_V2 = {
    "format", "version", "generated_at", "source", "file_count",
    "total_size", "files", "integrity",
}
_SOURCE_KEYS_V2 = {"provider", "directory_id", "directory_name"}
_FILE_REQUIRED_KEYS_V2 = {"path", "size", "gcid"}
_FILE_KEYS_V2 = _FILE_REQUIRED_KEYS_V2 | {"md5"}
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:/")
_GCID_RE = re.compile(r"^[A-Za-z0-9_:-]+$")
_MD5_RE = re.compile(r"[0-9a-f]{32}")


class ManifestValidationError(ValueError):
    """GCID 清单格式或完整性无效。"""


@dataclass(frozen=True, slots=True)
class GCIDManifestFile:
    path: str
    size: int
    gcid: str
    md5: str | None = None

    def to_dict(self) -> dict[str, Any]:
        entry = {"path": self.path, "size": self.size, "gcid": self.gcid}
        if self.md5 is not None:
            entry["md5"] = self.md5
        return entry


@dataclass(frozen=True, slots=True)
class GCIDManifest:
    generated_at: str
    source_provider: str
    source_directory_id: str
    source_directory_name: str
    files: tuple[GCIDManifestFile, ...]
    digest: str

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        source: dict[str, str] = {"provider": self.source_provider}
        if self.source_directory_id:
            source["directory_id"] = self.source_directory_id
        if self.source_directory_name:
            source["directory_name"] = self.source_directory_name
        payload: dict[str, Any] = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "generated_at": self.generated_at,
            "source": source,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "files": [item.to_dict() for item in self.files],
        }
        payload["integrity"] = {"algorithm": "sha256", "digest": self.digest}
        return payload


def iter_manifest_bytes(manifest: dict[str, Any]) -> Iterator[bytes]:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for part in encoder.iterencode(manifest):
        yield part.encode("utf-8")


def _digest(manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for part in iter_manifest_bytes({key: value for key, value in manifest.items() if key != "integrity"}):
        digest.update(part)
    return digest.hexdigest()


def _safe_component(name: str) -> str:
    value = (name or "").replace("\\", "_").replace("/", "_").strip()
    return value or "未命名"


def _normalize_path(value: Any, index: int) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if (
        not raw
        or len(raw) > MAX_PATH_LENGTH
        or "\x00" in raw
        or raw.startswith("/")
        or _DRIVE_PATH_RE.match(raw)
    ):
        raise ManifestValidationError(f"files[{index}] 包含不安全路径")
    parts = raw.split("/")
    if ".." in parts:
        raise ManifestValidationError(f"files[{index}] 包含不安全路径")
    normalized_parts = [part for part in parts if part not in ("", ".")]
    if not normalized_parts:
        raise ManifestValidationError(f"files[{index}] 包含不安全路径")
    normalized = PurePosixPath(*normalized_parts).as_posix()
    if len(normalized) > MAX_PATH_LENGTH:
        raise ManifestValidationError(f"files[{index}] 路径过长")
    return normalized


def _normalize_size(value: Any, index: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SQLITE_INTEGER:
        raise ManifestValidationError(f"files[{index}] size 必须是可记录的非负整数")
    return value


def _normalize_gcid(value: Any, index: int) -> str:
    gcid = str(value or "").strip()
    if not gcid:
        raise ManifestValidationError(f"files[{index}] GCID 不能为空")
    if len(gcid) > MAX_GCID_LENGTH or not _GCID_RE.fullmatch(gcid):
        raise ManifestValidationError(f"files[{index}] GCID 格式无效")
    return gcid


def _build_manifest(payload: dict[str, Any]) -> GCIDManifest:
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at or len(generated_at) > 128:
        raise ManifestValidationError("generated_at 无效")
    source = payload.get("source")
    if not isinstance(source, dict) or set(source) - _SOURCE_KEYS_V2:
        raise ManifestValidationError("source 必须使用 MediaFlux v2 标准字段")
    provider = str(source.get("provider") or "").strip().lower()
    if provider != "guangya":
        raise ManifestValidationError("v2 清单 provider 必须是 guangya")
    directory_id = str(source.get("directory_id") or "").strip()
    directory_name = str(source.get("directory_name") or "").strip()
    if len(directory_id) > 256 or len(directory_name) > 512:
        raise ManifestValidationError("source 字段过长")

    files = payload.get("files")
    if not isinstance(files, list):
        raise ManifestValidationError("files 必须是数组")
    if len(files) > MAX_MANIFEST_FILES:
        raise ManifestValidationError(f"文件数量超过上限 {MAX_MANIFEST_FILES}")

    normalized_files: list[GCIDManifestFile] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not _FILE_REQUIRED_KEYS_V2 <= item.keys() <= _FILE_KEYS_V2:
            raise ManifestValidationError(f"files[{index}] 必须使用 path/size/gcid 标准字段，可附带 md5")
        md5 = item.get("md5")
        if "md5" in item and (not isinstance(md5, str) or not _MD5_RE.fullmatch(md5)):
            raise ManifestValidationError(f"files[{index}] md5 必须是 32 位小写十六进制")
        path = _normalize_path(item.get("path"), index)
        path_key = path.casefold()
        if path_key in seen:
            raise ManifestValidationError(f"存在重复路径: {path}")
        seen.add(path_key)
        normalized_files.append(GCIDManifestFile(
            path=path,
            size=_normalize_size(item.get("size"), index),
            gcid=_normalize_gcid(item.get("gcid"), index),
            md5=md5,
        ))

    # 路径虽不重复，文件仍不能同时成为另一个文件的父目录。
    for path in seen:
        parent = path.rpartition("/")[0]
        while parent:
            if parent in seen:
                raise ManifestValidationError(f"文件与目录路径冲突: {parent}")
            parent = parent.rpartition("/")[0]

    if isinstance(payload.get("file_count"), bool) or payload.get("file_count") != len(normalized_files):
        raise ManifestValidationError("file_count 与 files 数量不一致")
    total_size = sum(item.size for item in normalized_files)
    if isinstance(payload.get("total_size"), bool) or payload.get("total_size") != total_size:
        raise ManifestValidationError("total_size 与文件大小合计不一致")
    if total_size > _MAX_SQLITE_INTEGER:
        raise ManifestValidationError("total_size 超过可记录的上限")

    normalized_files.sort(key=lambda item: item.path.casefold())
    manifest = GCIDManifest(
        generated_at=generated_at,
        source_provider=provider,
        source_directory_id=directory_id,
        source_directory_name=directory_name,
        files=tuple(normalized_files),
        digest="",
    )
    return replace(manifest, digest=_digest(manifest.to_dict()))


def normalize_manifest_v2(payload: Any) -> GCIDManifest:
    """校验并规范化唯一可导入的 MediaFlux v2 清单。"""
    if not isinstance(payload, dict):
        raise ManifestValidationError("清单根节点必须是 JSON 对象")
    if payload.get("format") != FORMAT_NAME:
        raise ManifestValidationError("不是 MediaFlux GCID 清单")
    if payload.get("version") != FORMAT_VERSION:
        raise ManifestValidationError("GCID 导入仅接受 MediaFlux v2 清单")
    if payload.keys() != _ROOT_KEYS_V2:
        raise ManifestValidationError("v2 清单字段不完整或包含非标准字段")

    manifest = _build_manifest(payload)
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}:
        raise ManifestValidationError("缺少 SHA-256 完整性信息")
    expected = str(integrity.get("digest") or "").strip().lower()
    if integrity.get("algorithm") != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ManifestValidationError("SHA-256 完整性信息无效")
    if expected != manifest.digest:
        raise ManifestValidationError("清单完整性校验失败，文件可能已被修改")
    return manifest


def _export_file(client: GuangYaClient, item: GuangYaFile, path: str) -> GCIDManifestFile:
    size = max(0, (item.size or 0))
    gcid = (item.etag or "").strip()
    md5 = normalize_provider_md5(item.md5)
    if gcid and md5:
        return GCIDManifestFile(path, size, gcid, md5)

    detail = client.file_info(item.file_id) if item.file_id else None
    if detail is None:
        raise ValueError(f"无法读取文件详情，无法生成 v2 清单: {path}")
    detail_gcid = (detail.etag or "").strip()
    detail_md5 = normalize_provider_md5(detail.md5)
    if (
        (detail.file_id, detail.is_dir, detail.size) != (item.file_id, False, size)
        or (gcid and detail_gcid and gcid != detail_gcid)
        or (md5 and detail_md5 and md5 != detail_md5)
    ):
        raise ValueError(f"文件列表与详情的身份不一致，无法生成 v2 清单: {path}")
    if not detail_gcid:
        raise ValueError(f"文件详情缺少 GCID，无法生成 v2 清单: {path}")
    return GCIDManifestFile(path, size, detail_gcid, md5 or detail_md5)


def export_manifest(
    client: GuangYaClient,
    source_dir_id: str,
    source_name: str = "",
    max_files: int = MAX_MANIFEST_FILES,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """递归导出 v2 清单，开始扫描目录或完成文件条目时报告累计计数。"""
    source_id = (source_dir_id or "").strip()
    if not source_id:
        raise ValueError("缺少源目录 ID")
    limit = max(1, min((max_files or MAX_MANIFEST_FILES), MAX_MANIFEST_FILES))
    entries: list[dict[str, Any]] = []
    visited_dirs: set[str] = set()
    directory_count = 0

    def walk(dir_id: str, rel_dir: PurePosixPath) -> None:
        nonlocal directory_count
        if dir_id in visited_dirs:
            raise RuntimeError(f"检测到目录循环: {dir_id}")
        visited_dirs.add(dir_id)
        try:
            directory_count += 1
            if on_progress is not None:
                on_progress(directory_count, len(entries))
            for item in client.list_dir(dir_id):
                name = _safe_component(item.name)
                relative = rel_dir / name
                if item.is_dir:
                    walk(item.file_id, relative)
                    continue
                if len(entries) >= limit:
                    raise ValueError(f"文件数量超过安全上限 {limit}")
                entries.append(_export_file(client, item, relative.as_posix()).to_dict())
                if on_progress is not None:
                    on_progress(directory_count, len(entries))
        finally:
            visited_dirs.discard(dir_id)

    walk(source_id, PurePosixPath())
    return _build_manifest({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "provider": "guangya",
            "directory_id": source_id,
            **({"directory_name": _safe_component(source_name)} if source_name else {}),
        },
        "file_count": len(entries),
        "total_size": sum(item["size"] for item in entries),
        "files": entries,
    }).to_dict()


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """校验正式版 MediaFlux v2 GCID 清单。"""
    normalized = normalize_manifest_v2(manifest)
    return {
        "valid": True,
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "file_count": normalized.file_count,
        "total_size": normalized.total_size,
        "missing_gcid_count": 0,
        "import_ready": True,
        "import_blocker": "",
        "integrity": normalized.digest,
    }
