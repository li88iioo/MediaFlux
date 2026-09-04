"""MediaFlux 可写运行目录的跨平台解析。"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

_configured_paths: RuntimePaths | None = None


@dataclass(frozen=True)
class RuntimePaths:
    program_dir: Path
    data_dir: Path
    config_dir: Path
    cache_dir: Path
    log_dir: Path
    strm_dir: Path
    trash_dir: Path

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def database_path(self) -> Path:
        """返回数据库路径；共享 data/config 目录时保留源码与 Docker 的平铺布局。"""
        if self.config_dir == self.data_dir:
            return self.data_dir / "mediaflux.db"
        return self.data_dir / "db" / "mediaflux.db"

    @property
    def env_file(self) -> Path:
        return self.config_dir / "user.env"

    @property
    def token_dir(self) -> Path:
        """返回 GuangYa token 目录，保留源码布局中的 ``db/`` 位置。

        开发态偶尔会从临时 staging 目录导入代码，但仍把仓库目录作为
        ``data_dir``、仓库的 ``db/`` 作为 ``config_dir``。此时不能仅凭
        ``program_dir`` 判断布局，否则凭证会错误写到仓库根目录。
        """
        source_layout_dir = self.data_dir / "db"
        if self.config_dir == source_layout_dir:
            return self.config_dir
        if self.data_dir == self.program_dir:
            return source_layout_dir
        return self.data_dir

    @property
    def token_file(self) -> Path:
        return self.token_dir / "guangya_token.json"

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        platform_name: str | None = None,
        frozen: bool | None = None,
    ) -> RuntimePaths:
        """按显式环境变量、运行模式和平台解析目录。"""
        environment = os.environ if env is None else env
        effective_platform = platform_name or platform.system()
        is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen

        if not is_frozen:
            program_dir = Path(__file__).resolve().parent.parent
            defaults = {
                "MEDIAFLUX_DATA_DIR": program_dir,
                "MEDIAFLUX_CONFIG_DIR": program_dir / "db",
                "MEDIAFLUX_CACHE_DIR": program_dir / "db" / "cache",
                "MEDIAFLUX_LOG_DIR": program_dir / "db" / "logs",
                "MEDIAFLUX_STRM_DIR": program_dir / "strm-data",
            }
        else:
            program_dir = Path(__file__).resolve().parent.parent
            defaults = {
                "MEDIAFLUX_DATA_DIR": Path("/app/db"),
                "MEDIAFLUX_CONFIG_DIR": Path("/app/db"),
                "MEDIAFLUX_CACHE_DIR": Path("/app/db/cache"),
                "MEDIAFLUX_LOG_DIR": Path("/app/db/logs"),
                "MEDIAFLUX_STRM_DIR": Path("/data/strm"),
            }

        resolved = {
            name: _environment_path(
                name,
                environment.get(name),
                default,
                effective_platform,
            )
            for name, default in defaults.items()
        }
        trash_dir = (
            resolved["MEDIAFLUX_DATA_DIR"] / "trash"
            if is_frozen or environment.get("MEDIAFLUX_DATA_DIR")
            else program_dir / "db" / "trash"
        )
        return cls(
            program_dir=program_dir,
            data_dir=resolved["MEDIAFLUX_DATA_DIR"],
            config_dir=resolved["MEDIAFLUX_CONFIG_DIR"],
            cache_dir=resolved["MEDIAFLUX_CACHE_DIR"],
            log_dir=resolved["MEDIAFLUX_LOG_DIR"],
            strm_dir=resolved["MEDIAFLUX_STRM_DIR"],
            trash_dir=trash_dir,
        )

    def ensure_writable_dirs(self) -> None:
        """创建运行数据目录，绝不创建或修改程序目录。"""
        writable_dirs = {
            self.data_dir,
            self.database_path.parent,
            self.backup_dir,
            self.config_dir,
            self.cache_dir,
            self.log_dir,
            self.strm_dir,
            self.trash_dir,
        }
        for directory in sorted(writable_dirs, key=lambda path: str(path)):
            if directory != self.program_dir:
                directory.mkdir(parents=True, exist_ok=True)


def protected_runtime_output_target(
    paths: RuntimePaths,
    destination: Path,
) -> Path | None:
    """返回与输出路径重合的权威运行文件，包含符号链接与硬链接别名。"""
    candidate = Path(destination).expanduser()
    protected_files = (
        paths.database_path,
        paths.env_file,
        paths.token_file,
    )

    def normalized(path: Path) -> str:
        return os.path.normcase(
            os.path.realpath(os.path.abspath(os.fspath(path.expanduser())))
        )

    candidate_normalized = normalized(candidate)
    for protected in protected_files:
        if candidate_normalized == normalized(protected):
            return protected
        try:
            if candidate.exists() and protected.exists() and candidate.samefile(protected):
                return protected
        except OSError:
            continue
    return None


def _environment_path(
    name: str,
    value: str | None,
    default: Path,
    platform_name: str,
) -> Path:
    if not value:
        return default
    candidate = Path(value)
    if not _is_absolute(candidate, platform_name):
        raise ValueError(f"{name} 必须是绝对路径")
    return candidate


def _is_absolute(path: Path, platform_name: str) -> bool:
    if platform_name == "Windows":
        return path.is_absolute() or PureWindowsPath(str(path)).is_absolute()
    return path.is_absolute() or PurePosixPath(path.as_posix()).is_absolute()


def get_runtime_paths() -> RuntimePaths:
    """返回显式配置路径；未配置时从当前环境解析。"""
    return _configured_paths or RuntimePaths.from_environment()


def configure_runtime_paths(paths: RuntimePaths | None) -> None:
    """仅供测试和 CLI 启动前覆盖当前进程的运行目录。"""
    global _configured_paths
    _configured_paths = paths
