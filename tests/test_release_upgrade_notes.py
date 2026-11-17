"""已发布说明保持自洽；开发迁移不得回写历史，正式tag仍须匹配当前schema。"""

from __future__ import annotations

import os
import re
from pathlib import Path
import unittest
from unittest.mock import patch

from app import __version__
from app.database import SCHEMA_VERSION
from app.database_migrations import _SCHEMA_MIGRATIONS
from tests.test_release_metadata import B


class ReleaseUpgradeNotesTests(unittest.TestCase):
    def setUp(self):
        self.changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        self.section = B._changelog_section(self.changelog, __version__)
        self.assertTrue(self.section, "当前源码版本缺少有效的候选发布说明")

    def upgrade_span(self):
        span = re.search(r"\bschema\s*(\d+)\s*→\s*(\d+)", self.section)
        assert (span) is not None, "升级说明必须声明完整 schema 跨度"
        return int(span.group(1)), int(span.group(2))

    def assert_supported_upgrade(self, previous, target):
        if os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
            self.assertEqual(target, SCHEMA_VERSION, "正式tag发布说明必须覆盖当前schema")
        else:
            # 普通修复可以增加迁移，但项目规则禁止改写已发布CHANGELOG。
            self.assertLessEqual(target, SCHEMA_VERSION)
            for version in range(target, SCHEMA_VERSION):
                self.assertIn(version, _SCHEMA_MIGRATIONS, "开发schema缺少可达升级路径")
        self.assertLessEqual(previous, target)

    def test_release_upgrade_target_is_supported_by_current_code(self):
        self.assert_supported_upgrade(*self.upgrade_span())

    def test_formal_tag_still_rejects_schema_drift(self):
        previous, target = self.upgrade_span()
        with patch.dict(os.environ, {"GITHUB_REF": f"refs/tags/v{__version__}"}), patch(
            f"{__name__}.SCHEMA_VERSION", target + 1,
        ):
            with self.assertRaisesRegex(AssertionError, "正式tag发布说明必须覆盖当前schema"):
                self.assert_supported_upgrade(previous, target)

    def test_rollback_warning_names_the_actual_new_schema(self):
        previous, target = self.upgrade_span()
        if previous < target:
            self.assertRegex(
                self.section,
                rf"schema{previous}\s*程序[^\n]*schema{target}\s*数据库",
            )
        else:
            self.assertRegex(self.section, r"不新增[^。\n]*迁移|无[^。\n]*迁移")
        self.assertIn("离线恢复", self.section)
        self.assertIn("停止服务", self.section)

    def test_compare_links_start_from_previous_formal_version(self):
        versions = re.findall(
            r"^## \[([^\]]+)\] - \d{4}-\d{2}-\d{2}$", self.changelog, re.MULTILINE
        )
        position = versions.index(__version__)
        previous = versions[position + 1]
        links = dict(
            re.findall(r"^\[([^\]]+)\]:\s*(\S+)$", self.changelog, re.MULTILINE)
        )
        self.assertTrue(links["Unreleased"].endswith(f"/compare/v{__version__}...HEAD"))
        self.assertTrue(
            links[__version__].endswith(f"/compare/v{previous}...v{__version__}")
        )
