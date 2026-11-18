"""真实浏览器 CI 入口，沿用 _browser 模块、Browser 类或基类和 Playwright 导入标识。"""

import re
import unittest
from pathlib import Path


_BROWSER_TEST = re.compile(
    r"^class (?:\w*Browser\w*\(|\w+\([^)\n]*\b\w*Browser\w*\b)"
    r"|^[ \t]*(?:from|import) playwright\b",
    re.MULTILINE,
)


def main() -> int:
    directory = Path(__file__).resolve().parent
    modules = [
        f"tests.{path.stem}"
        for path in sorted(directory.glob("test_*.py"))
        if path.stem.endswith("_browser") or _BROWSER_TEST.search(path.read_text(encoding="utf-8"))
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    if not suite.countTestCases():
        print("No browser regression tests collected")
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for test, reason in result.skipped:
        print(f"Browser test skipped: {test}: {reason}")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
