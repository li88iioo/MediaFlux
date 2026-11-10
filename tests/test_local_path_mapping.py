"""跨平台路径映射和根目录防逃逸测试。"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.modules.local_path_mapping import (
    PathMapping,
    PathMappingError,
    assert_within,
    is_windows_or_unc_path,
    normalize_qb_path,
    require_container_absolute_path,
    validate_source_target_roots,
)


class LocalPathMappingTests(unittest.TestCase):
    def test_immutable_mapping_reuses_normalized_rule_across_batch(self):
        from dataclasses import asdict
        from app.modules import local_path_mapping as module

        raw = r"D:\Downloads\Straße"
        mapping = PathMapping(raw, Path("/mnt/library"))
        with patch.object(module, "normalize_qb_path", wraps=normalize_qb_path) as normalize:
            for _ in range(100):
                self.assertTrue(mapping.matches(r"d:\downloads\STRASSE\Title\Film.mkv"))
                self.assertEqual(mapping.normalized_prefix, "D:/Downloads/Straße")
            self.assertLessEqual(normalize.call_count, 102)
        self.assertEqual(asdict(mapping), {"qb_prefix": raw, "local_root": Path("/mnt/library")})

    def test_windows_or_unc_path_detection_does_not_reject_container_paths(self):
        self.assertTrue(is_windows_or_unc_path(r"D:\Downloads"))
        self.assertTrue(is_windows_or_unc_path(r"\\NAS\Media"))
        self.assertTrue(is_windows_or_unc_path("//NAS/Media"))
        self.assertFalse(is_windows_or_unc_path("/media/downloads"))
        self.assertFalse(is_windows_or_unc_path("relative/path"))

    def test_container_absolute_path_rejects_legacy_and_relative_inputs(self):
        self.assertEqual(
            require_container_absolute_path("/media/downloads"), Path("/media/downloads")
        )
        for value in (".", "relative/path", r"D:\Downloads", r"\\NAS\Media"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(PathMappingError, "Docker 容器"):
                    require_container_absolute_path(value)

    def test_windows_drive_and_unc_are_case_insensitive(self):
        drive = PathMapping(r"D:\Downloads", Path("/mnt/d"))
        unc = PathMapping(r"\\NAS\Media", Path("/mnt/nas"))
        self.assertEqual(drive.relative_parts(r"d:\DOWNLOADS\TV\A.mkv"), ("TV", "A.mkv"))
        self.assertEqual(unc.relative_parts(r"\\nas\media\Movie\B.mkv"), ("Movie", "B.mkv"))
        self.assertEqual(
            PathMapping(r"D:\Downloads", Path("/mnt/a")).comparison_prefix,
            PathMapping(r"d:/downloads", Path("/mnt/b")).comparison_prefix,
        )

    def test_unicode_casefold_does_not_corrupt_windows_or_unc_suffix(self):
        drive = PathMapping(r"C:\Straße", Path("/mnt/drive"))
        unc = PathMapping(r"\\Straße\MÉDIA", Path("/mnt/unc"))
        self.assertEqual(
            drive.relative_parts(r"c:\STRASSE\Movie.mkv"), ("Movie.mkv",)
        )
        self.assertEqual(
            unc.relative_parts(r"\\STRASSE\média\Show\Episode.mkv"),
            ("Show", "Episode.mkv"),
        )

    def test_prefix_boundary_is_rejected(self):
        mapping = PathMapping("/downloads/1", Path("/mnt/one"))
        self.assertTrue(mapping.matches("/downloads/1/A.mkv"))
        self.assertFalse(mapping.matches("/downloads/10/A.mkv"))
        with self.assertRaisesRegex(PathMappingError, "不匹配"):
            mapping.relative_parts("/downloads/10/A.mkv")

    def test_dotdot_nul_and_unc_root_are_rejected(self):
        for value in ("/downloads/../etc/passwd", "bad\x00path", r"\\server"):
            with self.subTest(value=value):
                with self.assertRaises(PathMappingError):
                    normalize_qb_path(value)

    def test_symlink_escape_and_outside_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw)
            outside = Path(outside_raw)
            link = root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不支持符号链接测试")
            if not link.is_symlink() and not (hasattr(link, "is_junction") and link.is_junction()):
                self.skipTest("当前环境未创建有效符号链接或连接点")
            with self.assertRaises(PathMappingError):
                assert_within(link / "A.mkv", root)
            with self.assertRaises(PathMappingError):
                assert_within(outside / "A.mkv", root)

    def test_source_and_targets_cannot_recursively_contain_each_other(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source = root / "downloads"
            for target in (source, source / "library", root):
                with self.subTest(target=target):
                    with self.assertRaises(PathMappingError):
                        validate_source_target_roots(source, [target])
            validate_source_target_roots(source, [root / "library"])


if __name__ == "__main__":
    unittest.main()
