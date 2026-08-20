from __future__ import annotations

import unittest

from app.modules.download_dispatcher import normalize_download_url


class DownloadInputValidationTests(unittest.TestCase):
    def test_validates_magnet_ed2k_and_http_structure(self):
        valid_hash = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(
            normalize_download_url(f"magnet:?xt=urn:btih:{valid_hash}").kind,
            "magnet",
        )
        self.assertEqual(
            normalize_download_url(
                "ed2k://|file|demo.mkv|1024|0123456789abcdef0123456789abcdef|/"
            ).kind,
            "ed2k",
        )
        self.assertEqual(normalize_download_url("https://example.com/demo.torrent").kind, "http")

    def test_rejects_protocol_prefix_without_download_identity(self):
        with self.assertRaisesRegex(ValueError, "BTIH"):
            normalize_download_url("magnet:?dn=missing-hash")
        with self.assertRaisesRegex(ValueError, "ED2K"):
            normalize_download_url("ed2k://broken")
        with self.assertRaisesRegex(ValueError, "有效域名"):
            normalize_download_url("https:///missing-host")
