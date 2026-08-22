from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.modules.media_server_path_mapping import (
    MediaServerPathMappingError,
    apply_media_server_path_mapping,
    configured_media_server_refresh_options,
    encode_media_server_path_mappings,
    media_server_path_is_within,
    normalize_media_server_path,
    parse_media_server_path_mappings,
)


class MediaServerPathMappingTests(unittest.TestCase):
    def test_longest_local_prefix_wins_and_preserves_suffix(self):
        mappings = parse_media_server_path_mappings(json.dumps([
            {"local": "/data/strm", "server": r"\\NAS\Media"},
            {"local": "/data/strm/光鸭云盘/电影", "server": r"\\NAS\Media\整理\电影"},
        ], ensure_ascii=False))

        mapped, selected = apply_media_server_path_mapping(
            "/data/strm/光鸭云盘/电影/作品 A",
            mappings,
        )

        self.assertEqual(mapped, "//NAS/Media/整理/电影/作品 A")
        self.assertEqual(selected.local_prefix, "/data/strm/光鸭云盘/电影")

    def test_object_form_is_canonicalized_for_single_line_env_storage(self):
        encoded = encode_media_server_path_mappings(json.dumps({
            "/data/strm/电影": r"\\NAS\Media\电影",
        }, ensure_ascii=False))

        self.assertNotIn("\n", encoded)
        self.assertEqual(json.loads(encoded), [{
            "local": "/data/strm/电影",
            "server": "//NAS/Media/电影",
        }])

    def test_relative_or_root_prefix_is_rejected(self):
        with self.assertRaises(MediaServerPathMappingError):
            parse_media_server_path_mappings('[{"local":"relative","server":"/media"}]')
        with self.assertRaises(MediaServerPathMappingError):
            parse_media_server_path_mappings('[{"local":"/","server":"/media"}]')

    def test_posix_prefix_matching_is_case_sensitive(self):
        mappings = parse_media_server_path_mappings(json.dumps([
            {"local": "/data/Movies", "server": "/media/movies"},
            {"local": "/data/movies", "server": "/media/lower-movies"},
        ]))

        mapped, selected = apply_media_server_path_mapping(
            "/data/movies/Film",
            mappings,
        )

        self.assertEqual(mapped, "/media/lower-movies/Film")
        self.assertEqual(selected.local_prefix, "/data/movies")

    def test_windows_drive_root_mapping_is_preserved(self):
        mappings = parse_media_server_path_mappings(json.dumps([
            {"local": "D:/", "server": "E:/"},
        ]))

        mapped, selected = apply_media_server_path_mapping(
            "d:/Media/Movies/Film", mappings,
        )

        self.assertEqual(mapped, "E:/Media/Movies/Film")
        self.assertEqual(selected.local_prefix, "D:/")
        self.assertEqual(selected.server_prefix, "E:/")

    def test_windows_prefix_matching_is_case_insensitive_and_segment_safe(self):
        mappings = parse_media_server_path_mappings(json.dumps([
            {"local": r"\\Server\Straße", "server": "/media/library"},
        ], ensure_ascii=False))

        mapped, _selected = apply_media_server_path_mapping(
            r"\\SERVER\STRASSE\Season 01",
            mappings,
        )

        self.assertEqual(mapped, "/media/library/Season 01")

    def test_duplicate_windows_prefix_is_rejected_case_insensitively(self):
        with self.assertRaises(MediaServerPathMappingError):
            parse_media_server_path_mappings(json.dumps([
                {"local": r"D:\STRM", "server": "/media/a"},
                {"local": r"d:\strm", "server": "/media/b"},
            ]))

    def test_conflicting_alias_fields_are_rejected(self):
        with self.assertRaises(MediaServerPathMappingError):
            parse_media_server_path_mappings(json.dumps([{
                "local": "/data/a", "source": "/data/b", "server": "/media/a",
            }]))
        with self.assertRaises(MediaServerPathMappingError):
            parse_media_server_path_mappings(json.dumps([{
                "local": "/data/a", "server": "/media/a", "remote": "/media/b",
            }]))

    def test_equivalent_alias_fields_remain_compatible(self):
        mappings = parse_media_server_path_mappings(json.dumps([{
            "local": r"D:\STRM", "source": "d:/strm",
            "server": r"\\NAS\Media", "remote": "//nas/media",
        }]))

        self.assertEqual(len(mappings), 1)

    def test_path_containment_handles_filesystem_roots_and_segment_boundaries(self):
        self.assertTrue(media_server_path_is_within("/media/Film", "/"))
        self.assertFalse(media_server_path_is_within("//NAS/Share/Film", "/"))
        self.assertTrue(media_server_path_is_within("D:/Media/Film", "d:/"))
        self.assertTrue(media_server_path_is_within(
            "//NAS/Share/Film", "//nas/share",
        ))
        self.assertFalse(media_server_path_is_within(
            "/media/movies/Film", "/media/movie",
        ))
        self.assertFalse(media_server_path_is_within(
            "//nas/media2/Film", "//nas/media",
        ))
        self.assertEqual(normalize_media_server_path("D:/"), "D:/")

    def test_invalid_config_forces_global_fallback_off(self):
        with patch(
            "app.modules.media_server_path_mapping.config.get",
            return_value="not-json",
        ), patch(
            "app.modules.media_server_path_mapping.config.get_bool",
            return_value=True,
        ):
            options = configured_media_server_refresh_options("jellyfin")

        self.assertEqual(options["path_mappings"], ())
        self.assertFalse(options["allow_global_refresh_fallback"])

if __name__ == "__main__":
    unittest.main()
