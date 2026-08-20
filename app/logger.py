"""日志系统。

- 控制台 + 滚动文件（db/logs/app.log），按天轮转
- 毫秒级格式，与原项目 MsFormatter 对齐
- 统一入口 get_logger(name)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from logging.handlers import TimedRotatingFileHandler

from app.config import PATHS
from app.private_files import protect_private_stream
from app.sensitive_data import redact_sensitive_text

LOG_DIR = PATHS.log_dir
LOG_DIR.mkdir(parents=True, exist_ok=True)

_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False

def normalize_telebot_polling_error(value: object) -> str | None:
    """把 TeleBot 长轮询网络异常收敛为不含 URL、Token 和堆栈的单行摘要。"""
    message = str(value or "")
    lowered = message.casefold()
    prefixes = (
        "infinity polling exception:",
        "threaded polling exception:",
        "polling exception:",
    )
    if not any(lowered.startswith(prefix) for prefix in prefixes):
        return None
    if any(key in lowered for key in ("connecttimeout", "connect timeout", "connecttimedouterror")):
        return "Telegram Bot 连接超时（ConnectTimeout），将在后台自动重试"
    if any(key in lowered for key in ("readtimeout", "read timed out", "readtimeouterror")):
        return "Telegram Bot 读取超时（ReadTimeout），将在后台自动重试"
    if any(key in lowered for key in ("sslerror", "certificate_verify_failed", "ssl:", "tls")):
        return "Telegram Bot TLS/SSL 连接异常（SSL），将在后台自动重试"
    if any(key in lowered for key in (
        "connectionerror", "newconnectionerror", "max retries exceeded",
        "connection reset", "connection refused", "connection aborted",
        "remote end closed connection", "remotedisconnected",
        "network is unreachable", "name or service not known",
        "temporary failure in name resolution", "proxyerror",
    )):
        return "Telegram Bot 网络连接异常（ConnectionError），将在后台自动重试"
    return None


def configure_telebot_logging() -> None:
    """让 TeleBot 统一走 MediaFlux 根日志处理器，避免重复输出原始堆栈。"""
    vendor_logger = logging.getLogger("TeleBot")
    # pyTelegramBotAPI 会自行挂载 stderr handler。若保留它，异常会先以原始
    # 堆栈输出一次，再经根 logger 脱敏输出一次，既重复也可能泄露 Bot URL。
    vendor_logger.handlers.clear()
    vendor_logger.propagate = True
    vendor_logger.setLevel(logging.INFO)


class _WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """兼容 Windows 轮转，并在每次打开日志时收紧 POSIX 权限。"""

    def clear(self) -> None:
        """在持有 handler 锁时截断当前流，避免 Windows 文件句柄竞态。"""
        self.acquire()
        try:
            if self.stream is None:
                self.stream = self._open()
            self.stream.seek(0)
            self.stream.truncate(0)
            self.stream.flush()
        finally:
            self.release()

    def _open(self):
        stream = super()._open()
        if not protect_private_stream(stream):
            # 日志权限失败不能递归写日志；保持业务可用，由外层健康检查发现。
            pass
        return stream

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            self.rolloverAt = self.computeRollover(int(time.time()))


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        raw_message = record.getMessage()
        if record.name.lower() == "telebot":
            if raw_message.startswith("Exception traceback:"):
                return False
            normalized = normalize_telebot_polling_error(raw_message)
            if normalized is not None:
                record.msg = normalized
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
                return True
        if record.name.lower() == "asyncio":
            # 过滤 Windows ProactorEventLoop 客户端主动断开连接（WinError 10054 / _call_connection_lost）无害噪音
            if (
                "_call_connection_lost" in raw_message
                or "10054" in raw_message
                or "远程主机强迫关闭了一个现有的连接" in raw_message
                or "connectionreseterror" in raw_message.lower()
            ):
                return False
        record.msg = redact_sensitive_text(raw_message)
        record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    """对最终日志文本脱敏，覆盖异常堆栈和 stack_info。"""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            record.exc_text = redact_sensitive_text(self.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_sensitive_text(record.stack_info)
        return redact_sensitive_text(super().format(record))


def _setup_root() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 清掉重复 handler
    root.handlers.clear()

    console = logging.StreamHandler()
    console.addFilter(_RedactFilter())
    console.setFormatter(_RedactingFormatter(_FORMAT, _DATEFMT))
    root.addHandler(console)

    disable_file_logging = str(
        os.environ.get("MEDIAFLUX_DISABLE_FILE_LOGGING", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not disable_file_logging:
        file_handler = _WindowsSafeTimedRotatingFileHandler(
            LOG_DIR / "app.log", when="midnight", backupCount=14, encoding="utf-8"
        )
        file_handler.addFilter(_RedactFilter())
        file_handler.setFormatter(_RedactingFormatter(_FORMAT, _DATEFMT))
        root.addHandler(file_handler)

    # 降低第三方库噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    # httpx/httpcore 的 INFO request-line 会包含 Provider URL；在第三方 logger
    # 产生记录前抑制，避免依赖事后脱敏。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    configure_telebot_logging()

    _configured = True


def clear_runtime_log(path: Path | None = None) -> None:
    """可靠清空当前运行日志，优先复用仍被 logging 持有的文件句柄。"""
    _setup_root()
    target = Path(path or (LOG_DIR / "app.log")).resolve()
    matched_handler = False
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, _WindowsSafeTimedRotatingFileHandler):
            continue
        try:
            handler_path = Path(handler.baseFilename).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if handler_path != target:
            continue
        handler.clear()
        matched_handler = True
    if matched_handler:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as stream:
        stream.seek(0)
        stream.truncate(0)
        stream.flush()
        if not protect_private_stream(stream):
            raise PermissionError(f"无法收紧日志文件权限: {target}")


def get_logger(name: str) -> logging.Logger:
    _setup_root()
    return logging.getLogger(name)
