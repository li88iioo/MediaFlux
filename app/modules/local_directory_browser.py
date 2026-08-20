"""受限的本地目录浏览器，仅暴露目录名称与规范化路径。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from app.config import get
from app.logger import get_logger
from app.modules.local_path_mapping import PathMappingError, assert_within

logger = get_logger(__name__)

VIRTUAL_ROOT = "__roots__"


def _platform_roots() -> list[Path]:
    """返回当前系统可浏览的文件系统入口，供未配置白名单时使用。"""
    if os.name == "nt":
        roots: list[Path] = []
        seen_letters: set[str] = set()

        # 1. 常规本地物理磁盘
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            try:
                if drive.is_dir():
                    roots.append(drive)
                    seen_letters.add(letter.upper())
            except Exception as exc:
                logger.debug("枚举物理磁盘 %s 异常: %s", letter, exc)

        # 2. Windows 映射网络驱动器（如 X: -> \\Nas\视频，W: -> \\Nas\固态）
        from app.modules.windows_smb import get_windows_mapped_network_drives, ensure_smb_connection
        try:
            mapped = get_windows_mapped_network_drives()
        except Exception as exc:
            logger.warning("获取 Windows 映射网络驱动器列表失败: %s", exc)
            mapped = {}

        for drive_key, remote_unc in mapped.items():
            letter = drive_key.rstrip(":").upper()
            if letter in seen_letters:
                continue
            try:
                # 尝试建立/校验与对应 UNC 共享的连接（若已存凭据会自动注入）
                ok, err = ensure_smb_connection(remote_unc)
                if not ok:
                    logger.info("网络映射盘 %s (%s) 暂需身份验证或不可用: %s", letter, remote_unc, err)
            except Exception as exc:
                logger.warning("校验网络映射盘 %s (%s) 连接异常: %s", letter, remote_unc, exc)

            # 在 Windows 环境下，映射网络驱动器均作为合法根入口暴露
            try:
                roots.append(Path(f"{letter}:\\"))
                seen_letters.add(letter)
            except Exception as exc:
                logger.warning("添加网络映射盘入口 %s 失败: %s", letter, exc)

        return roots
    return [Path("/")]


def _configured_roots() -> list[Path]:
    raw = str(get("LOCAL_MEDIA_BROWSE_ROOTS", "") or "").strip()
    if raw:
        parts = [item.strip() for item in re.split(r"[\n,;]+", raw) if item.strip()]
        roots = [Path(item).expanduser() for item in parts]
    else:
        roots = _platform_roots()

    from app.modules.windows_smb import is_windows, resolve_drive_or_unc_path

    normalized: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = assert_within(root, root)
        except PathMappingError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        if is_windows():
            match_drive = re.match(r"^[a-zA-Z]:[\\/]*$", str(resolved))
            if match_drive:
                seen.add(key)
                normalized.append(resolved)
                continue
        real_check_path = Path(resolve_drive_or_unc_path(str(resolved))) if is_windows() else resolved
        if not real_check_path.is_dir():
            continue
        seen.add(key)
        normalized.append(resolved)
    return normalized


def assert_browse_root_allowed(path: Path) -> Path:
    """显式配置浏览白名单时，确认临时根目录没有绕过白名单。"""
    raw = str(get("LOCAL_MEDIA_BROWSE_ROOTS", "") or "").strip()
    candidate = Path(path).expanduser()
    if not raw:
        return assert_within(candidate, candidate)
    return _allowed_root(candidate, _configured_roots())


def _display_name(path: Path) -> str:
    path_str = str(path).strip().replace("/", "\\")
    if not path_str:
        return ""
    if os.name == "nt":
        try:
            from app.modules.windows_smb import get_windows_mapped_network_drives
            mapped = get_windows_mapped_network_drives()
            norm_path = path_str.rstrip("\\").casefold()
            for d_letter, unc in mapped.items():
                norm_d = d_letter.replace("/", "\\").rstrip("\\").casefold()
                norm_unc = unc.replace("/", "\\").rstrip("\\").casefold()
                if norm_path in (norm_d, norm_unc):
                    return f"{d_letter} ({unc})"
        except Exception as exc:
            logger.debug("获取映射盘显示名异常 %s: %s", path_str, exc)
    name = path.name
    return name or str(path)


def _allowed_root(path: Path, roots: list[Path]) -> Path:
    for root in roots:
        try:
            return assert_within(path, root)
        except PathMappingError:
            continue
    raise PathMappingError("路径超出允许浏览范围")


def browse_local_directories(
    path: str = "",
    *,
    allowed_root: Path | None = None,
    source_id: int = 0,
    owner: str = "admin",
) -> dict:
    """列出一级子目录；指定 allowed_root 时禁止离开该来源根目录。"""
    if source_id:
        from app import database as db
        source = db.get_local_media_source(source_id, owner=owner)
        if source:
            from app.modules.windows_smb import ensure_smb_connection, parse_unc_share_root
            if parse_unc_share_root(source.local_root):
                try:
                    ensure_smb_connection(source.local_root, getattr(source, "smb_user", ""), getattr(source, "smb_pass", ""))
                except Exception as exc:
                    logger.warning("来源目录 SMB 挂载异常 %s: %s", source.local_root, exc)
            if allowed_root is None:
                allowed_root = Path(source.local_root)

    roots = [assert_within(allowed_root, allowed_root)] if allowed_root is not None else _configured_roots()
    if not roots:
        raise PathMappingError("没有可浏览的本地目录")

    requested = str(path or "").strip()
    if os.name == "nt" and re.match(r"^[a-zA-Z]:$", requested):
        requested = requested + "\\"

    if not requested or requested == VIRTUAL_ROOT:
        if allowed_root is not None:
            current = roots[0]
        else:
            return {
                "current": VIRTUAL_ROOT,
                "parent": "",
                "directories": [
                    {"id": str(root), "name": _display_name(root), "path": str(root)}
                    for root in roots
                ],
            }
    else:
        from app.modules.windows_smb import is_windows, resolve_drive_or_unc_path, ensure_smb_connection
        candidate = Path(requested).expanduser()
        is_abs = candidate.is_absolute()
        if not is_abs and (is_windows() or requested.startswith("\\\\") or requested.startswith("//")):
            is_abs = bool(re.match(r"^([a-zA-Z]:|[\\/]{2}[^\\/]+[\\/][^\\/]+)", requested))
        if not is_abs:
            raise PathMappingError("目录路径必须是绝对路径")
        current = _allowed_root(candidate, roots)

    real_current = Path(resolve_drive_or_unc_path(str(current))) if is_windows() else current
    if is_windows():
        try:
            ensure_smb_connection(str(real_current))
        except Exception as exc:
            logger.warning("浏览前确保 SMB 连接异常 %s: %s", real_current, exc)

    try:
        path_exists = real_current.exists()
    except OSError as exc:
        from app.modules.windows_smb import explain_windows_network_error
        raise ValueError(explain_windows_network_error(exc, str(current))) from exc

    if not path_exists:
        raise FileNotFoundError("目录不存在")

    try:
        path_is_dir = real_current.is_dir()
    except OSError as exc:
        from app.modules.windows_smb import explain_windows_network_error
        raise ValueError(explain_windows_network_error(exc, str(current))) from exc

    if not path_is_dir:
        raise ValueError("所选路径不是目录")
    if real_current.is_symlink():
        raise PathMappingError("不允许浏览符号链接目录")

    directories: list[dict[str, str]] = []
    try:
        children = sorted(real_current.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        from app.modules.windows_smb import explain_windows_network_error
        raise ValueError(explain_windows_network_error(exc, str(current))) from exc

    current_prefix_str = str(current).rstrip("\\/ ")
    is_root_drive = bool(re.match(r"^[a-zA-Z]:[\\/]*$", current_prefix_str))

    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if is_windows() and is_root_drive:
                display_child_str = f"{current_prefix_str}\\{child.name}"
            elif is_windows():
                display_child_str = f"{current_prefix_str}\\{child.name}"
            else:
                display_child_str = f"{current_prefix_str}/{child.name}"
            child_candidate = Path(display_child_str)
            safe_child = _allowed_root(child_candidate, roots)
        except (OSError, PathMappingError):
            continue
        directories.append({"id": str(safe_child), "name": child.name, "path": str(safe_child)})

    root = next(root for root in roots if current == root or root in current.parents)
    parent = ""
    if current != root:
        parent = str(current.parent)
    elif allowed_root is None:
        parent = VIRTUAL_ROOT
    return {"current": str(current), "parent": parent, "directories": directories}
