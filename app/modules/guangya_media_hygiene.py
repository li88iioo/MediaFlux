"""光鸭媒体名称卫生扫描。

当前实现重点处理带站点污染的高置信媒体标识及其唯一关联伴随文件，
并生成显式名称映射。真正写入仍由 ``guangya_rename`` 的 owner-bound
冻结计划、确认门和持久队列负责。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any

from app import config
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules.guangya_rename import (
    GuangYaRenamePlanError,
    build_explicit_rename_plan,
)
from app.modules.nsfw import NsfwRecognizer, extract_nsfw_identifier, normalize_code
from app.modules.organize import DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS
from app.modules.organize_postprocess import SUBTITLE_EXTS

_MAX_SCANNED_ITEMS = 10_000
_MAX_SCANNED_DIRS = 5_000
_MAX_CANONICAL_STEM = 180
_DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|tv|cc|me|cn|xyz|site|club|info|top|vip|pro|io)"
)
_FORBIDDEN_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_SPACE_RE = re.compile(r"\s+")
_SUBTITLE_SUFFIX_RE = re.compile(
    r"(?i)((?:[._ -](?:zh(?:[-_](?:cn|tw|hans|hant))?|chs|cht|eng|en|cn|"
    r"sc|tc|forced|default|sdh|cc))+)\s*$"
)
_IMAGE_SUFFIX_RE = re.compile(
    r"(?i)(?:[._ -](poster|fanart|cover|thumb|thumbnail|backdrop))\s*$"
)


@dataclass
class _DirectoryNode:
    item: GuangYaFile
    path: str
    parent_path: str
    children: list[str] = field(default_factory=list)
    files: list[GuangYaFile] = field(default_factory=list)


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaRenamePlanError("光鸭路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise GuangYaRenamePlanError("媒体名称清理不能使用光鸭根目录")
    return "/" + "/".join(parts)


def _cached_list_dir(
    client: GuangYaClient,
    cache: dict[str, list[GuangYaFile]],
    parent_id: str,
) -> list[GuangYaFile]:
    key = str(parent_id)
    if key not in cache:
        cache[key] = client.list_dir(key)
    return cache[key]


def _resolve_path(
    client: GuangYaClient,
    path: str,
    cache: dict[str, list[GuangYaFile]],
) -> tuple[GuangYaFile, str]:
    parent_id = "0"
    parent_path = "/"
    components = path.strip("/").split("/")
    for index, component in enumerate(components):
        items = _cached_list_dir(client, cache, parent_id)
        exact = [item for item in items if item.name == component]
        matches = exact or [
            item for item in items if item.name.casefold() == component.casefold()
        ]
        if len(matches) != 1:
            raise GuangYaRenamePlanError(
                f"路径不存在或名称不唯一：/{'/'.join(components[:index + 1])}"
            )
        current = matches[0]
        if index < len(components) - 1 and not current.is_dir:
            raise GuangYaRenamePlanError("光鸭路径的中间组件不是目录")
        parent_path = "/" + "/".join(components[:index]) if index else "/"
        parent_id = str(current.file_id)
    return current, parent_path


def _configured_exts(key: str, defaults: tuple[str, ...]) -> set[str]:
    raw = str(config.get(key, "") or "")
    if not raw.strip():
        return set(defaults)
    values = {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[,，\s]+", raw)
        if re.fullmatch(r"[A-Za-z0-9]{1,10}", item.strip().lstrip("."))
    }
    return values or set(defaults)


def _extension(item: GuangYaFile) -> str:
    declared = str(item.extension or "").strip().lower().lstrip(".")
    if declared:
        return declared
    match = re.search(r"[.。．]([A-Za-z0-9]{1,10})$", item.name)
    return match.group(1).lower() if match else ""


def _stem(item: GuangYaFile) -> str:
    ext = _extension(item)
    if ext:
        match = re.search(rf"[.。．]{re.escape(ext)}$", item.name, re.IGNORECASE)
        if match:
            return item.name[:match.start()]
    return item.name.rsplit(".", 1)[0] if "." in item.name else item.name


def _contains_domain(value: str, configured_domains: str) -> bool:
    if _DOMAIN_RE.search(str(value or "")):
        return True
    lowered = str(value or "").casefold()
    for domain in re.split(r"[,，\s]+", str(configured_domains or "")):
        normalized = domain.strip().casefold().lstrip(".")
        if normalized and normalized in lowered:
            return True
    return False


def _safe_stem(value: object, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _FORBIDDEN_NAME_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip(" .-_'")
    if not text:
        text = normalize_code(fallback)
    if len(text) > _MAX_CANONICAL_STEM:
        text = text[:_MAX_CANONICAL_STEM].rstrip(" .-_")
    if not text:
        raise GuangYaRenamePlanError("无法生成安全的媒体名称")
    return text


def _configured_recognizer(strip_domains: str) -> NsfwRecognizer | None:
    endpoint = str(config.get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT", "") or "").strip()
    if not endpoint:
        return None
    token = str(config.get("GY_ORGANIZE_NSFW_METATUBE_TOKEN", "") or "").strip()
    try:
        timeout = max(
            2,
            min(int(config.get("GY_ORGANIZE_NSFW_TIMEOUT_SECONDS", "8") or 8), 30),
        )
        return NsfwRecognizer(
            endpoint,
            token,
            strip_domains=strip_domains,
            timeout=timeout,
        )
    except (TypeError, ValueError):
        return None


def _canonical_stem(
    code: str,
    *,
    recognizer: NsfwRecognizer | None,
    lookup_name: str,
    cache: dict[str, tuple[str, bool]],
) -> tuple[str, bool]:
    normalized = normalize_code(code)
    cached = cache.get(normalized)
    if cached is not None:
        return cached
    title = ""
    enriched = False
    if recognizer is not None:
        try:
            unique = {
                _safe_stem(candidate.title, normalized)
                for candidate in recognizer.candidates(lookup_name)
                if str(candidate.title or "").strip()
            }
        except Exception:
            unique = set()
        if len(unique) == 1:
            title = next(iter(unique))
            enriched = True
    result = (_safe_stem(title, normalized), enriched)
    cache[normalized] = result
    return result


def _companion_suffix(item: GuangYaFile) -> str:
    stem = _stem(item)
    ext = _extension(item)
    if ext in SUBTITLE_EXTS:
        match = _SUBTITLE_SUFFIX_RE.search(stem)
        return match.group(1).replace(" ", ".") if match else ""
    if ext in {"jpg", "jpeg", "png", "webp"}:
        match = _IMAGE_SUFFIX_RE.search(stem)
        return f".{match.group(1).lower()}" if match else ""
    return ""


def build_media_hygiene_plan(
    client: GuangYaClient,
    *,
    owner: str,
    path: str,
    recursive: bool = True,
    limit: int = 1_000,
    enrich_metadata: bool = True,
) -> dict[str, Any]:
    """扫描光鸭目录并冻结媒体名称卫生计划。

    当前策略聚焦带站点污染的高置信媒体标识，同时保留后续扩展到其他
    媒体类型和清理策略的通用能力边界。
    """
    target_path = _normalize_path(path)
    safe_limit = max(1, min(int(limit), 10_000))
    strip_domains = str(config.get("GY_ORGANIZE_NSFW_STRIP_DOMAINS", "") or "")
    video_exts = _configured_exts("GY_ORGANIZE_VIDEO_EXTS", DEFAULT_ORGANIZE_VIDEO_EXTS)
    metadata_exts = _configured_exts(
        "GY_ORGANIZE_METADATA_EXTS", DEFAULT_ORGANIZE_METADATA_EXTS
    )
    recognizer = _configured_recognizer(strip_domains) if enrich_metadata else None

    cache: dict[str, list[GuangYaFile]] = {}
    target, parent_path = _resolve_path(client, target_path, cache)
    if not target.is_dir:
        raise GuangYaRenamePlanError("媒体名称清理目前只支持精确目录路径")

    nodes: dict[str, _DirectoryNode] = {
        str(target.file_id): _DirectoryNode(target, target_path, parent_path)
    }
    queue = deque([str(target.file_id)])
    scanned_items = 0
    scanned_dirs = 0
    while queue:
        directory_id = queue.popleft()
        node = nodes[directory_id]
        scanned_dirs += 1
        if scanned_dirs > _MAX_SCANNED_DIRS:
            raise GuangYaRenamePlanError("媒体名称清理目录数量超过安全上限")
        items = _cached_list_dir(client, cache, directory_id)
        for child in items:
            scanned_items += 1
            if scanned_items > _MAX_SCANNED_ITEMS:
                raise GuangYaRenamePlanError("媒体名称清理项目数量超过安全上限")
            if child.is_dir:
                if recursive:
                    child_path = node.path.rstrip("/") + "/" + child.name
                    nodes[str(child.file_id)] = _DirectoryNode(
                        child, child_path, node.path
                    )
                    node.children.append(str(child.file_id))
                    queue.append(str(child.file_id))
                continue
            node.files.append(child)

    canonical_cache: dict[str, tuple[str, bool]] = {}
    changes: list[tuple[GuangYaFile, str, str]] = []
    direct_codes: dict[str, set[str]] = defaultdict(set)
    unidentified_video_dirs: set[str] = set()
    video_counts: dict[str, int] = defaultdict(int)
    video_rows: dict[str, list[tuple[GuangYaFile, str, str]]] = defaultdict(list)
    no_change = 0
    unidentified_videos = 0
    metadata_enriched = 0
    video_renames = 0
    companion_renames = 0
    directory_renames = 0

    for directory_id, node in nodes.items():
        for item in node.files:
            if _extension(item) not in video_exts:
                continue
            video_counts[directory_id] += 1
            identifier = extract_nsfw_identifier(item.name, strip_domains)
            if identifier is None:
                unidentified_videos += 1
                unidentified_video_dirs.add(directory_id)
                continue
            code = normalize_code(identifier.code)
            direct_codes[directory_id].add(code)
            canonical, enriched = _canonical_stem(
                code,
                recognizer=recognizer,
                lookup_name=item.name,
                cache=canonical_cache,
            )
            metadata_enriched += int(enriched)
            target_name = f"{canonical}.{_extension(item)}"
            video_rows[directory_id].append((item, code, target_name))
            if _contains_domain(_stem(item), strip_domains):
                if target_name != item.name:
                    changes.append((item, node.path, target_name))
                    video_renames += 1
                else:
                    no_change += 1

    # 只有同目录唯一视频且伴随文件自身也带同一番号时才联动，避免跨作品误配。
    for directory_id, rows in video_rows.items():
        if len(rows) != 1 or video_counts.get(directory_id, 0) != 1:
            continue
        video, code, video_target = rows[0]
        canonical_base = video_target.rsplit(".", 1)[0]
        node = nodes[directory_id]
        for item in node.files:
            ext = _extension(item)
            if item.file_id == video.file_id or ext not in metadata_exts:
                continue
            identifier = extract_nsfw_identifier(item.name, strip_domains)
            if identifier is None or normalize_code(identifier.code) != code:
                continue
            if not _contains_domain(_stem(item), strip_domains):
                continue
            suffix = _companion_suffix(item)
            target_name = f"{canonical_base}{suffix}.{ext}"
            if target_name == item.name:
                no_change += 1
                continue
            changes.append((item, node.path, target_name))
            companion_renames += 1

    # 自底向上汇总作品番号；目录自身必须也带同一番号和域名污染才会改名。
    subtree_codes: dict[str, set[str]] = {}
    subtree_has_unidentified_video: dict[str, bool] = {}
    for directory_id in reversed(list(nodes)):
        node = nodes[directory_id]
        codes = set(direct_codes.get(directory_id, set()))
        has_unidentified_video = directory_id in unidentified_video_dirs
        for child_id in node.children:
            codes.update(subtree_codes.get(child_id, set()))
            has_unidentified_video = bool(
                has_unidentified_video
                or subtree_has_unidentified_video.get(child_id, False)
            )
        subtree_codes[directory_id] = codes
        subtree_has_unidentified_video[directory_id] = has_unidentified_video
        if (
            len(codes) != 1
            or has_unidentified_video
            or not _contains_domain(node.item.name, strip_domains)
        ):
            continue
        identifier = extract_nsfw_identifier(node.item.name, strip_domains)
        code = next(iter(codes))
        if identifier is None or normalize_code(identifier.code) != code:
            continue
        canonical, _enriched = canonical_cache[code]
        if canonical == node.item.name:
            no_change += 1
            continue
        changes.append((node.item, node.parent_path, canonical))
        directory_renames += 1

    if len(changes) > safe_limit:
        raise GuangYaRenamePlanError(
            f"媒体名称清理匹配超过本次上限 {safe_limit} 个，请缩小目录或提高 limit"
        )
    return build_explicit_rename_plan(
        client,
        owner=owner,
        target=target_path,
        changes=changes,
        cache=cache,
        scanned_items=scanned_items,
        scanned_dirs=scanned_dirs,
        no_change=no_change,
        limit=safe_limit,
        extra_stats={
            "identified_video_count": sum(len(rows) for rows in video_rows.values()),
            "unidentified_video_count": unidentified_videos,
            "video_rename_count": video_renames,
            "companion_rename_count": companion_renames,
            "directory_rename_count": directory_renames,
            "metadata_enriched_count": metadata_enriched,
        },
        transform={
            "enrich_metadata": "1" if enrich_metadata else "0",
            "metatube_configured": "1" if recognizer is not None else "0",
        },
    )
