"""停服后一次性收纳运行文件；不迁移数据库、配置或领域计划。"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from app.modules.process_lock import (
    LIFECYCLE_LOCK_NAME,
    CrossProcessLock,
    lock_path,
)
from app.runtime_paths import RuntimeLayoutError, RuntimePaths


def _legacy_locks(paths: RuntimePaths) -> list[Path]:
    # 不递归扫描数据目录，避免触及日志、备份和用户文件。
    namespaces = {
        paths.data_dir,
        paths.database_path.parent,
        paths.token_dir,
        *(paths.data_dir / name for name in (
            "agent-guangya-cleanup", "agent-guangya-fs-change", "agent-guangya-rename",
        )),
    }
    return sorted({
        file
        for directory in namespaces
        for file in directory.glob(".mediaflux-*.lock")
        if file.name != LIFECYCLE_LOCK_NAME
    })


def require_runtime_layout(paths: RuntimePaths) -> None:
    """启动前明确拒绝旧布局，禁止一边运行一边更换锁身份。"""
    if _legacy_locks(paths) or (paths.data_dir / "mediaflux.pid").exists():
        raise RuntimeLayoutError(
            "检测到旧运行文件布局；请先停止所有 MediaFlux 服务及离线工具，"
            "使用相同数据目录配置运行 python mediaflux.py runtime-migrate 后再启动。"
            "迁移期间不要运行旧版本。"
        )


def migrate_runtime_layout(paths: RuntimePaths) -> int:
    """离线迁移，可重复执行；保留唯一根目录生命周期锁跨版本互斥。

    锁文件没有业务数据，预检持有所有旧锁后，在各自 namespace 的
    runtime/locks 建立新锁，再移除停用的旧锁。绝不替换已存在的新锁
    inode；中断后重跑只处理剩余项。旧版本服务/工具须全程保持停止。
    """
    from app.modules.backup import BackupError, _exclusive_runtime_lifecycle_guard

    try:
        with _exclusive_runtime_lifecycle_guard(paths), ExitStack() as held:
            locks = _legacy_locks(paths)
            opened = []
            # 先核查所有旧/新锁；任意占用或文件异常时，不删除任何旧文件。
            for old in locks:
                if old.is_symlink() or not old.is_file() or old.stat().st_size > 1:
                    raise RuntimeLayoutError(f"拒绝迁移非空或异常锁文件：{old.name}")
                target = lock_path(old.parent, old.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                for file in (old, target):
                    if file.is_symlink():
                        raise RuntimeLayoutError(f"拒绝迁移符号链接锁：{file.name}")
                    handle = held.enter_context(file.open("a+b"))
                    try:
                        CrossProcessLock._acquire_file_lock(handle, blocking=False)
                    except OSError as exc:
                        raise RuntimeLayoutError(
                            f"运行锁仍被占用或不可访问：{file.name}；请停止所有服务和离线工具后重试"
                        ) from exc
                    if file == old:
                        opened.append((old, handle))
            for old, handle in opened:
                # Windows 不允许删除打开的文件；生命周期锁和对应新锁仍被持有。
                handle.close()
                old.unlink()
            # 停服后的 PID 已失效，不复制为看似仍在运行的记录；启动写入新路径。
            (paths.data_dir / "mediaflux.pid").unlink(missing_ok=True)
            return len(locks)
    except BackupError as exc:
        raise RuntimeLayoutError("实例仍在运行，请先停服再迁移运行目录") from exc
