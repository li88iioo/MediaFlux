"""首次运行初始化状态与管理员凭据的安全落盘。"""
from __future__ import annotations

import ipaddress
import os
import threading
from dataclasses import dataclass

from app import config
from app.runtime_paths import RuntimePaths, get_runtime_paths


class InitializationError(RuntimeError):
    """首次初始化不能继续时抛出。"""

    def __init__(self, message: str, *, initialized_by_competitor: bool = False) -> None:
        super().__init__(message)
        self.initialized_by_competitor = initialized_by_competitor


class UnsafeFirstRunBindingError(RuntimeError):
    """首次初始化试图监听非回环地址。"""


@dataclass(frozen=True)
class _StartupState:
    paths: RuntimePaths
    needs_initialization: bool
    env_bytes: bytes | None
    config_error: str = ""


_state_lock = threading.RLock()
_startup_state: _StartupState | None = None
_LINE_SEPARATORS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _manual_recovery_message(reason: str) -> str:
    return (
        f"无法安全读取 user.env（{reason}）。"
        "请先备份并移走 user.env，确认文件权限与内容后再重新初始化。"
    )


def _read_env_snapshot(paths: RuntimePaths) -> tuple[bytes | None, dict[str, str], str]:
    try:
        payload, values = config.read_env_snapshot(paths.env_file)
        return payload, values, ""
    except config.CorruptConfigFileError:
        return None, {}, _manual_recovery_message("内容不是有效 UTF-8")
    except config.UnsafeConfigFileError as exc:
        return None, {}, _manual_recovery_message(str(exc))
    except (OSError, ValueError) as exc:
        return None, {}, _manual_recovery_message(f"读取失败: {exc}")


def _effective_value(key: str, file_values: dict[str, str]) -> str:
    external = os.getenv(key)
    if external:
        return external
    return file_values.get(key, "")


def _capture_startup_state(paths: RuntimePaths | None = None) -> _StartupState:
    runtime_paths = paths or get_runtime_paths()
    env_bytes, file_values, config_error = _read_env_snapshot(runtime_paths)
    username = _effective_value("ENV_WEB_PASSPORT", file_values)
    password = _effective_value("ENV_WEB_PASSWORD", file_values)
    marker = _effective_value("MEDIAFLUX_INITIALIZED", file_values)
    # 正式版只接受由首启流程或部署清单显式写入的初始化标记。
    initialized = marker == "1" and bool(username and password) and not config_error
    return _StartupState(
        paths=runtime_paths,
        needs_initialization=not initialized,
        env_bytes=env_bytes,
        config_error=config_error,
    )


def _startup_snapshot() -> _StartupState:
    global _startup_state
    with _state_lock:
        current_paths = get_runtime_paths()
        if _startup_state is None or _startup_state.paths != current_paths:
            _startup_state = _capture_startup_state(current_paths)
        return _startup_state


def _reset_startup_state_for_tests() -> None:
    """私有测试钩子：按当前 RuntimePaths 重新抓取生命周期前的状态。"""
    global _startup_state
    with _state_lock:
        _startup_state = _capture_startup_state()


def refresh_startup_state_after_restore() -> None:
    """恢复事务替换配置后，重新冻结首启状态。"""
    global _startup_state
    with _state_lock:
        _startup_state = _capture_startup_state()


def needs_initialization() -> bool:
    """返回进程启动前是否不存在任何可识别的既有安装状态。"""
    return _startup_snapshot().needs_initialization


def initialization_error() -> str:
    """返回首启页可展示的安全恢复提示。"""
    return _startup_snapshot().config_error


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _remote_setup_allowed() -> bool:
    enabled = {"1", "true", "yes", "on"}
    explicit = os.getenv("MEDIAFLUX_ALLOW_REMOTE_SETUP", "").strip().lower()
    if explicit:
        return explicit in enabled
    return os.getenv("MEDIAFLUX_CONTAINER", "").strip().lower() in enabled


def resolve_bind_host(requested: str | None, *, initialized_default: str = "0.0.0.0") -> str:
    """源码首启仅监听回环；官方容器默认开放一次性 Web 初始化。"""
    if needs_initialization():
        if requested and not _is_loopback_host(requested) and not _remote_setup_allowed():
            raise UnsafeFirstRunBindingError(
                "首次初始化禁止监听非回环地址；请先在本机完成初始化，"
                "或显式设置 MEDIAFLUX_ALLOW_REMOTE_SETUP=1"
            )
        if requested:
            return requested
        return initialized_default if _remote_setup_allowed() else "127.0.0.1"
    if requested:
        return requested
    return config.get("WEB_HOST", initialized_default) or initialized_default


def _contains_disallowed_separator(value: str) -> bool:
    return "\0" in value or any(character in _LINE_SEPARATORS for character in value)


def _validate_credentials(username: str, password: str) -> str:
    if _contains_disallowed_separator(username):
        raise ValueError("管理员用户名不能包含换行或空字符")
    normalized_username = username.strip()
    if not normalized_username:
        raise ValueError("管理员用户名不能为空")
    if len(normalized_username) > 128:
        raise ValueError("管理员用户名不能超过 128 个字符")
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    if len(password) > 512:
        raise ValueError("密码不能超过 512 个字符")
    if _contains_disallowed_separator(password):
        raise ValueError("密码不能包含换行或空字符")
    return normalized_username


def initialize_admin(username: str, password: str) -> None:
    """创建或安全修复管理员配置，保留恢复文件中的无关设置。"""
    normalized_username = _validate_credentials(username, password)

    from app.modules.web_secret import get_web_secret

    global _startup_state
    with _state_lock:
        state = _startup_snapshot()
        if not state.needs_initialization:
            raise InitializationError(
                "MediaFlux 已初始化，不能覆盖现有管理员凭据",
                initialized_by_competitor=True,
            )
        if state.config_error:
            raise InitializationError(state.config_error)

        updates = {
            "ENV_WEB_PASSPORT": normalized_username,
            "ENV_WEB_PASSWORD": password,
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": get_web_secret(),
        }
        try:
            from app.modules.backup import config_snapshot_guard

            with config_snapshot_guard(state.paths):
                result = config.update_env_file(
                    state.paths.env_file,
                    updates,
                    expected=state.env_bytes,
                )
        except (OSError, UnicodeError, config.AtomicPublishError) as exc:
            # 任意存储/发布异常后都重新捕获真实状态，避免 setup/login 生命周期卡死。
            _startup_state = _capture_startup_state(state.paths)
            initialized_by_competitor = not _startup_state.needs_initialization
            if initialized_by_competitor:
                message = "MediaFlux 已由其他进程完成初始化"
            elif _startup_state.config_error:
                message = _startup_state.config_error
            elif isinstance(exc, (FileExistsError, config.ConcurrentConfigUpdateError)):
                message = "配置已发生变化，请刷新页面后重试"
            else:
                message = f"无法安全保存初始化配置：{exc}"
            raise InitializationError(
                message,
                initialized_by_competitor=initialized_by_competitor,
            ) from exc

        config._apply_runtime_values(result, path=state.paths.env_file)
        _startup_state = _StartupState(
            paths=state.paths,
            needs_initialization=False,
            env_bytes=result.payload,
            config_error="",
        )


# auth/first_run 在应用 lifespan 前导入时立即冻结启动前状态。
_startup_snapshot()
