"""第三方日志降噪与敏感 URL 抑制测试。"""
from __future__ import annotations

import asyncio
import logging
import unittest
from io import StringIO

from app.logger import _RedactFilter, _RedactingFormatter, get_logger


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
