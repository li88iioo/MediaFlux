"""不可信 Torrent 文件清单解析契约。"""
from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from app.modules.download_dispatcher import (
    BencodeError,
    TorrentManifest,
    TorrentManifestFile,
    parse_torrent_manifest,
    parse_torrent_metadata,
)


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            _bencode(key) + _bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


def _torrent(info: dict) -> bytes:
    return _bencode({b"announce": b"https://tracker.invalid/announce", b"info": info})


class TorrentManifestTests(unittest.TestCase):
    def test_parses_v1_single_file_without_exposing_tracker(self):
        manifest = parse_torrent_manifest(_torrent({
            b"length": 1_024,
            b"name": "测试.S01E01.mkv".encode(),
            b"piece length": 16_384,
            b"pieces": b"x" * 20,
        }))

        self.assertEqual(manifest, TorrentManifest(
            name="测试.S01E01.mkv",
            version="v1",
            files=(TorrentManifestFile(("测试.S01E01.mkv",), 1_024),),
        ))
        self.assertNotIn("tracker", repr(manifest).lower())

    def test_parses_v1_multi_file_utf8_paths(self):
        manifest = parse_torrent_manifest(_torrent({
            b"files": [
                {b"length": 100, b"path": [b"Season 01", b"Show.S01E01.mkv"]},
                {b"length": 10, b"path.utf-8": ["字幕".encode(), b"Show.S01E01.ass"]},
            ],
            b"name": b"Release Pack",
            b"piece length": 16_384,
            b"pieces": b"x" * 20,
        }))

        self.assertEqual(manifest.version, "v1")
        self.assertEqual(
            [item.relative_path for item in manifest.files],
            ["Season 01/Show.S01E01.mkv", "字幕/Show.S01E01.ass"],
        )

    def test_parses_v2_file_tree(self):
        manifest = parse_torrent_manifest(_torrent({
            b"file tree": {
                b"Season 02": {
                    b"Show.S02E01.mkv": {b"": {b"length": 2_048}},
                    b"Show.S02E02.mkv": {b"": {b"length": 4_096}},
                },
            },
            b"meta version": 2,
            b"name": b"Show Season 2",
            b"piece length": 16_384,
        }))

        self.assertEqual(manifest.version, "v2")
        self.assertEqual(
            [item.relative_path for item in manifest.files],
            ["Season 02/Show.S02E01.mkv", "Season 02/Show.S02E02.mkv"],
        )

    def test_hybrid_prefers_v2_tree_without_duplicate_v1_entries(self):
        manifest = parse_torrent_manifest(_torrent({
            b"file tree": {b"Show.S01E01.mkv": {b"": {b"length": 2_048}}},
            b"files": [{b"length": 2_048, b"path": [b"Show.S01E01.mkv"]}],
            b"meta version": 2,
            b"name": b"Hybrid Show",
            b"piece length": 16_384,
            b"pieces": b"x" * 20,
        }))

        self.assertEqual(manifest.version, "hybrid")
        self.assertEqual(len(manifest.files), 1)
        self.assertEqual(manifest.files[0].relative_path, "Show.S01E01.mkv")

    def test_rejects_path_traversal_and_embedded_separators(self):
        for unsafe in (b"..", b"folder/file.mkv", b"folder\\file.mkv"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(BencodeError):
                    parse_torrent_manifest(_torrent({
                        b"files": [{b"length": 1, b"path": [unsafe]}],
                        b"name": b"Unsafe",
                        b"piece length": 16_384,
                        b"pieces": b"x" * 20,
                    }))

    def test_rejects_case_insensitive_duplicate_paths(self):
        with self.assertRaisesRegex(BencodeError, "重复路径"):
            parse_torrent_manifest(_torrent({
                b"files": [
                    {b"length": 1, b"path": [b"Show.S01E01.mkv"]},
                    {b"length": 2, b"path": [b"show.s01e01.MKV"]},
                ],
                b"name": b"Duplicate",
                b"piece length": 16_384,
                b"pieces": b"x" * 20,
            }))

    def test_rejects_invalid_utf8_and_duplicate_dictionary_keys(self):
        with self.assertRaisesRegex(BencodeError, "UTF-8"):
            parse_torrent_manifest(_torrent({
                b"length": 1,
                b"name": b"\xff",
                b"piece length": 16_384,
                b"pieces": b"x" * 20,
            }))

        duplicate_info = b"d4:name3:one4:name3:two6:lengthi1ee"
        with self.assertRaisesRegex(BencodeError, "重复字典键"):
            parse_torrent_manifest(b"d4:info" + duplicate_info + b"e")

    def test_rejects_malformed_or_empty_manifests(self):
        invalid_payloads = (
            b"",
            b"de",
            _torrent({b"name": b"missing-files"}),
            _torrent({b"file tree": {}, b"meta version": 2, b"name": b"empty-v2"}),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(BencodeError):
                    parse_torrent_manifest(payload)


if __name__ == "__main__":
    unittest.main()


class UnifiedTorrentDecoderTests(unittest.TestCase):
    def test_metadata_and_manifest_reject_the_same_malformed_encoding(self):
        malformed = (
            b"d4:infod6:lengthi01e4:name8:Demo.mkvee",
            b"d4:infod6:lengthi" + b"9" * 5000 + b"e4:name8:Demo.mkvee",
            b"d4:infod6:lengthi-0e4:name8:Demo.mkvee",
            b"d4:infod6:lengthi1e4:name8:Demo.mkv4:name8:Demo.mkvee",
            b"d4:infod6:lengthi1e4:name08:Demo.mkvee",
            b"d4:infod6:lengthi1e4:name8:Demo.mkv4:junk" + b"l" * 70 + b"e" * 70 + b"ee",
        )
        for data in malformed:
            for parser in (parse_torrent_metadata, parse_torrent_manifest):
                with self.subTest(parser=parser.__name__, data=data[:60]):
                    with self.assertRaises(BencodeError):
                        parser(data)

    def test_metadata_hash_uses_original_info_bytes_not_reencoded_dictionary(self):
        # 字典顺序刻意不同于编码器排序；Tracker 身份必须保留原始 info 字节。
        raw_info = b"d4:name8:Demo.mkv6:lengthi1ee"
        data = b"d4:info" + raw_info + b"e"
        name, torrent_id = parse_torrent_metadata(data)
        self.assertEqual(name, "Demo.mkv")
        self.assertEqual(torrent_id, hashlib.sha1(raw_info).hexdigest())
        self.assertEqual(parse_torrent_manifest(data).name, name)

    def test_both_entrypoints_enforce_the_input_size_bound(self):
        data = _torrent({b"name": b"Demo.mkv", b"length": 1})
        with patch("app.modules.download_dispatcher._TORRENT_MAX_BYTES", 8):
            for parser in (parse_torrent_metadata, parse_torrent_manifest):
                with self.subTest(parser=parser.__name__):
                    with self.assertRaisesRegex(BencodeError, "限制"):
                        parser(data)

    def test_both_entrypoints_enforce_structural_budget(self):
        data = _torrent({b"name": b"Demo.mkv", b"length": 1})
        with patch("app.modules.download_dispatcher._BENCODE_MAX_VALUES", 3):
            for parser in (parse_torrent_metadata, parse_torrent_manifest):
                with self.subTest(parser=parser.__name__):
                    with self.assertRaisesRegex(BencodeError, "结构过大"):
                        parser(data)
