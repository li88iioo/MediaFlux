"""第三方日志降噪与敏感 URL 抑制测试。"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
