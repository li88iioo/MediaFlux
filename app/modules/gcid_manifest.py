"""MediaFlux GCID 清单导出、校验与 v2 导入标准。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from app.clients.guangya import GuangYaClient

FORMAT_NAME = "mediaflux.gcid-manifest"
FORMAT_VERSION = 2
MAX_MANIFEST_FILES = 10_000
MAX_PATH_LENGTH = 1024
MAX_GCID_LENGTH = 256
_ROOT_KEYS_V2 = {
    "format", "version", "generated_at", "source", "file_count",
    "total_size", "files", "integrity",
}
_SOURCE_KEYS_V2 = {"provider", "directory_id", "directory_name"}
_FILE_KEYS_V2 = {"path", "size", "gcid"}
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:/")
_GCID_RE = re.compile(r"^[A-Za-z0-9_:-]+$")


class ManifestValidationError(ValueError):
    """GCID 清单格式或完整性无效。"""


@dataclass(frozen=True, slots=True)
class GCIDManifestFile:
    path: str
    size: int
    gcid: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "gcid": self.gcid}


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


def _canonical_payload(manifest: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in manifest.items() if key != "integrity"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(manifest)).hexdigest()


def _safe_component(name: str) -> str:
    value = str(name or "").replace("\\", "_").replace("/", "_").strip()
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
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"files[{index}] size 必须是非负整数")
    if value < 0:
        raise ManifestValidationError(f"files[{index}] size 不能为负数")
    return value


def _normalize_gcid(value: Any, index: int) -> str:
    gcid = str(value or "").strip()
    if not gcid:
        raise ManifestValidationError(f"files[{index}] GCID 不能为空")
    if len(gcid) > MAX_GCID_LENGTH or not _GCID_RE.fullmatch(gcid):
        raise ManifestValidationError(f"files[{index}] GCID 格式无效")
    return gcid


def normalize_manifest_v2(payload: Any) -> GCIDManifest:
    """校验并规范化唯一可导入的 MediaFlux v2 清单。"""
    if not isinstance(payload, dict):
        raise ManifestValidationError("清单根节点必须是 JSON 对象")
    if payload.get("format") != FORMAT_NAME:
        raise ManifestValidationError("不是 MediaFlux GCID 清单")
    if payload.get("version") != FORMAT_VERSION:
        raise ManifestValidationError("GCID 导入仅接受 MediaFlux v2 清单")
    unknown = set(payload) - _ROOT_KEYS_V2
    missing = _ROOT_KEYS_V2 - set(payload)
    if unknown or missing:
        raise ManifestValidationError("v2 清单字段不完整或包含非标准字段")

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
        if not isinstance(item, dict) or set(item) != _FILE_KEYS_V2:
            raise ManifestValidationError(f"files[{index}] 必须使用 path/size/gcid 标准字段")
        path = _normalize_path(item.get("path"), index)
        path_key = path.casefold()
        if path_key in seen:
            raise ManifestValidationError(f"存在重复路径: {path}")
        seen.add(path_key)
        normalized_files.append(GCIDManifestFile(
            path=path,
            size=_normalize_size(item.get("size"), index),
            gcid=_normalize_gcid(item.get("gcid"), index),
        ))

    if isinstance(payload.get("file_count"), bool) or payload.get("file_count") != len(normalized_files):
        raise ManifestValidationError("file_count 与 files 数量不一致")
    total_size = sum(item.size for item in normalized_files)
    if isinstance(payload.get("total_size"), bool) or payload.get("total_size") != total_size:
        raise ManifestValidationError("total_size 与文件大小合计不一致")

    normalized_files.sort(key=lambda item: item.path.casefold())
    provisional = GCIDManifest(
        generated_at=generated_at,
        source_provider=provider,
        source_directory_id=directory_id,
        source_directory_name=directory_name,
        files=tuple(normalized_files),
        digest="",
    )
    canonical = provisional.to_dict()
    canonical.pop("integrity", None)
    actual = _digest(canonical)
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}:
        raise ManifestValidationError("缺少 SHA-256 完整性信息")
    expected = str(integrity.get("digest") or "").strip().lower()
    if integrity.get("algorithm") != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ManifestValidationError("SHA-256 完整性信息无效")
    if expected != actual:
        raise ManifestValidationError("清单完整性校验失败，文件可能已被修改")
    return GCIDManifest(
        generated_at=generated_at,
        source_provider=provider,
        source_directory_id=directory_id,
        source_directory_name=directory_name,
        files=tuple(normalized_files),
        digest=actual,
    )


def export_manifest(
    client: GuangYaClient,
    source_dir_id: str,
    source_name: str = "",
    max_files: int = MAX_MANIFEST_FILES,
) -> dict[str, Any]:
    """递归导出可直接进入 v2 预览的标准清单，不修改云端。"""
    source_id = str(source_dir_id or "").strip()
    if not source_id:
        raise ValueError("缺少源目录 ID")
    limit = max(1, min(int(max_files or MAX_MANIFEST_FILES), MAX_MANIFEST_FILES))
    entries: list[dict[str, Any]] = []
    visited_dirs: set[str] = set()

    def walk(dir_id: str, rel_dir: PurePosixPath) -> None:
        if dir_id in visited_dirs:
            raise RuntimeError(f"检测到目录循环: {dir_id}")
        visited_dirs.add(dir_id)
        try:
            for item in client.list_dir(dir_id):
                name = _safe_component(item.name)
                relative = rel_dir / name
                if item.is_dir:
                    walk(item.file_id, relative)
                    continue
                if len(entries) >= limit:
                    raise ValueError(f"文件数量超过安全上限 {limit}")
                gcid = str(item.etag or "").strip()
                if not gcid:
                    detail = client.file_info(item.file_id)
                    gcid = str(detail.etag if detail else "").strip()
                if not gcid:
                    raise ValueError(f"文件缺少 GCID，无法生成 v2 清单: {relative.as_posix()}")
                entries.append({
                    "path": relative.as_posix(),
                    "size": max(0, int(item.size or 0)),
                    "gcid": gcid,
                })
        finally:
            visited_dirs.discard(dir_id)

    walk(source_id, PurePosixPath())
    entries.sort(key=lambda item: item["path"].casefold())
    payload: dict[str, Any] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "provider": "guangya",
            "directory_id": source_id,
            **({"directory_name": _safe_component(source_name)} if source_name else {}),
        },
        "file_count": len(entries),
        "total_size": sum(item["size"] for item in entries),
        "files": entries,
    }
    payload["integrity"] = {"algorithm": "sha256", "digest": _digest(payload)}
    return payload


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
