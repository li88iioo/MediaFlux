from __future__ import annotations

import unittest
from pathlib import Path


class TestDirectoryLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.tests_dir = cls.root / "tests"

    def test_root_contains_no_test_modules(self):
        self.assertEqual(
            [path.name for path in sorted(self.root.glob("test_*.py"))],
            [],
        )

    def test_support_and_package_markers_exist(self):
        self.assertTrue((self.tests_dir / "__init__.py").is_file())
        self.assertTrue((self.tests_dir / "support.py").is_file())

    def test_standard_discovery_loads_the_packaged_suite(self):
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(self.tests_dir),
            pattern="test_*.py",
            top_level_dir=str(self.root),
        )
        self.assertGreaterEqual(suite.countTestCases(), 700)
