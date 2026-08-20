"""共享 user.env 正式字面量解析契约。"""
from __future__ import annotations

import unittest

from app.env_file import read_env_bytes, read_env_text


class EnvFileParserTests(unittest.TestCase):
    def test_plain_and_marked_literal_assignments_never_interpolate(self) -> None:
        text = (
            "SOURCE=/srv/media\n"
            "VALUE=${SOURCE}/library\n"
            "SECRET='  $dollar # literal  ' # mediaflux-literal\n"
            "OVERRIDE=plain\n"
            "OVERRIDE='literal' # mediaflux-literal\n"
        )
        expected = {
            "SOURCE": "/srv/media",
            "VALUE": "${SOURCE}/library",
            "SECRET": "  $dollar # literal  ",
            "OVERRIDE": "literal",
        }
        self.assertEqual(read_env_text(text), expected)
        self.assertEqual(read_env_bytes(text.encode("utf-8")), expected)

    def test_invalid_line_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 1 行格式无效"):
            read_env_text("export OLD_STYLE=value\n")


if __name__ == "__main__":
    unittest.main()
