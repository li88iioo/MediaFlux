"""第三方日志降噪与敏感 URL 抑制测试。"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from unittest.mock import patch
from io import StringIO

from app.logger import (
    _RedactFilter,
    _RedactingFormatter,
    _file_logging_disabled,
    get_logger,
    log_throttled,
    reset_log_limiter,
)


class FileLoggingIsolationTests(unittest.TestCase):
    def test_direct_test_execution_disables_file_logging_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            sys, "argv", ["tests/test_logger.py"]
        ):
            self.assertTrue(_file_logging_disabled())

    def test_unittest_discovery_disables_file_logging_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            sys, "argv", ["python", "-m", "unittest", "discover", "-s", "tests"]
        ):
            self.assertTrue(_file_logging_disabled())

    def test_explicit_zero_keeps_file_logging_enabled_for_path_contract_tests(self):
        with patch.dict(
            os.environ,
            {
                "MEDIAFLUX_TESTING": "1",
                "MEDIAFLUX_DISABLE_FILE_LOGGING": "0",
            },
            clear=True,
        ):
            self.assertFalse(_file_logging_disabled())


class ThirdPartyLoggingTests(unittest.TestCase):
    def test_http_client_request_lines_are_suppressed_below_warning(self):
        get_logger(__name__)
        self.assertGreaterEqual(
            logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING
        )
        self.assertGreaterEqual(
            logging.getLogger("httpcore").getEffectiveLevel(), logging.WARNING
        )

        captured: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        root = logging.getLogger()
        handler = CaptureHandler()
        root.addHandler(handler)
        try:
            logging.getLogger("httpx").info(
                'HTTP Request: POST https://private-provider.invalid/v1/chat/completions '
                '"HTTP/1.1 200 OK"'
            )
            logging.getLogger("httpcore").debug(
                "request headers include provider-secret"
            )
        finally:
            root.removeHandler(handler)

        self.assertEqual(captured, [])

    def test_exception_traceback_is_redacted_after_final_formatting(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(_RedactFilter())
        handler.setFormatter(_RedactingFormatter("%(levelname)s | %(message)s"))
        logger = logging.getLogger("redaction-traceback-test")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        try:
            try:
                raise RuntimeError(
                    "provider failed Authorization: Bearer traceback-secret-token"
                )
            except RuntimeError:
                logger.exception("request failed")
        finally:
            logger.handlers = []
            handler.close()

        rendered = stream.getvalue()
        self.assertIn("RuntimeError", rendered)
        self.assertIn("Authorization: Bearer ********", rendered)
        self.assertNotIn("traceback-secret-token", rendered)

    def test_stack_info_is_redacted_before_rendering(self):
        record = logging.LogRecord(
            "stack-redaction-test",
            logging.ERROR,
            __file__,
            1,
            "request failed",
            (),
            None,
        )
        record.stack_info = "Authorization: Bearer abc"
        rendered = _RedactingFormatter("%(message)s").format(record)
        self.assertIn("Authorization: Bearer ********", rendered)
        self.assertNotIn("Bearer abc", rendered)

    def test_asyncio_windows_connection_reset_noise_is_filtered(self):
        redact_filter = _RedactFilter()
        noisy_records = [
            logging.LogRecord(
                "asyncio",
                logging.ERROR,
                __file__,
                1,
                "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
                (),
                None,
            ),
            logging.LogRecord(
                "asyncio",
                logging.ERROR,
                __file__,
                1,
                "ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。",
                (),
                None,
            ),
        ]
        for record in noisy_records:
            self.assertFalse(redact_filter.filter(record))

        normal_asyncio_record = logging.LogRecord(
            "asyncio",
            logging.ERROR,
            __file__,
            1,
            "Real unexpected asyncio task error occurred",
            (),
            None,
        )
        self.assertTrue(redact_filter.filter(normal_asyncio_record))

    def test_throttled_log_emits_first_and_summarizes_suppressed_records(self):
        reset_log_limiter()
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("throttled-log-test")
        logger.handlers = [CaptureHandler()]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            with patch("app.logger.time.monotonic", side_effect=[100.0, 101.0, 401.0]):
                self.assertTrue(log_throttled(logger, logging.WARNING, "offline", "上游离线"))
                self.assertFalse(log_throttled(logger, logging.WARNING, "offline", "上游离线"))
                self.assertTrue(log_throttled(logger, logging.WARNING, "offline", "上游离线"))
        finally:
            logger.handlers = []
            reset_log_limiter()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].getMessage(), "上游离线")
        self.assertEqual(records[1].getMessage(), "上游离线（期间已抑制 1 条重复日志）")

    def test_telebot_network_retries_and_startup_lines_are_rate_limited(self):
        redact_filter = _RedactFilter()
        raw_error = (
            "Infinity polling exception: HTTPSConnectionPool(host='api.telegram.org'): "
            "Max retries exceeded with url: /bot123:secret/getUpdates "
            "(Caused by ConnectionError('network down'))"
        )
        first_error = logging.LogRecord(
            "TeleBot", logging.ERROR, __file__, 1, raw_error, (), None,
        )
        repeated_error = logging.LogRecord(
            "TeleBot", logging.ERROR, __file__, 1, raw_error, (), None,
        )
        first_start = logging.LogRecord(
            "TeleBot", logging.INFO, __file__, 1,
            "Starting your bot with username: [@private_bot]", (), None,
        )
        repeated_start = logging.LogRecord(
            "TeleBot", logging.INFO, __file__, 1,
            "Starting your bot with username: [@private_bot]", (), None,
        )

        self.assertTrue(redact_filter.filter(first_error))
        self.assertEqual(
            first_error.getMessage(),
            "Telegram Bot 网络连接异常（ConnectionError），将在后台自动重试",
        )
        self.assertFalse(redact_filter.filter(repeated_error))
        self.assertTrue(redact_filter.filter(first_start))
        self.assertEqual(first_start.getMessage(), "Telegram Bot 正在建立轮询连接")
        self.assertFalse(redact_filter.filter(repeated_start))

    def test_telebot_getupdates_conflict_is_normalized_and_rate_limited(self):
        redact_filter = _RedactFilter()
        raw_message = (
            "Threaded polling exception: A request to the Telegram API was "
            "unsuccessful. Error code: 409. Description: Conflict: terminated by "
            "other getUpdates request; make sure that only one bot instance is running"
        )

        first = logging.LogRecord(
            "TeleBot", logging.ERROR, __file__, 1, raw_message, (), None,
        )
        repeated = logging.LogRecord(
            "TeleBot", logging.ERROR, __file__, 1, raw_message, (), None,
        )
        retry_wait = logging.LogRecord(
            "TeleBot",
            logging.INFO,
            __file__,
            1,
            "Waiting for 16.0 seconds until retry",
            (),
            None,
        )

        self.assertTrue(redact_filter.filter(first))
        self.assertEqual(
            first.getMessage(),
            "Telegram Bot 轮询冲突（HTTP 409）：检测到另一实例正在使用同一 Bot Token；"
            "请只保留一个 Bot 实例",
        )
        self.assertFalse(redact_filter.filter(repeated))
        self.assertFalse(redact_filter.filter(retry_wait))

    def test_global_error_handler_logs_summary_without_a_second_traceback(self):
        from starlette.requests import Request

        from app import main

        handler = main.app.exception_handlers[Exception]
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/failure",
            "raw_path": b"/api/failure",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        })

        with self.assertLogs("app.main", level="ERROR") as captured:
            response = asyncio.run(handler(request, RuntimeError("SECRET-TOKEN")))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(captured.records[0].exc_info)
        rendered = "\n".join(captured.output)
        self.assertIn("method=POST", rendered)
        self.assertIn("path=/api/failure", rendered)
        self.assertIn("type=RuntimeError", rendered)
        self.assertNotIn("SECRET-TOKEN", rendered)


if __name__ == "__main__":
    unittest.main()
