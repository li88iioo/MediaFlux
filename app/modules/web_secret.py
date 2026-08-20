"""Web session 与签名令牌共用的密钥提供器。"""
from __future__ import annotations

import secrets
import threading

from app import config
from app.runtime_paths import RuntimePaths, get_runtime_paths


class WebSecretUnavailable(RuntimeError):
    """已初始化的生产安装缺少 Web Secret。"""


_lock = threading.RLock()
_process_secrets: dict[RuntimePaths, str] = {}


def configured_web_secret() -> str:
    return config.get("WEB_SECRET_KEY", "").strip()


def _is_production() -> bool:
    return config.get("APP_ENV", "development").strip().lower() == "production"


def _fresh_install() -> bool:
    from app.modules.first_run import needs_initialization

    return needs_initialization()


def get_web_secret() -> str:
    """返回外部/已持久化密钥；fresh install 则生成进程内统一密钥。"""
    configured = configured_web_secret()
    if configured:
        return configured
    if not _fresh_install() and _is_production():
        raise WebSecretUnavailable("生产模式已初始化安装必须配置 WEB_SECRET_KEY")

    paths = get_runtime_paths()
    with _lock:
        return _process_secrets.setdefault(paths, secrets.token_urlsafe(48))
