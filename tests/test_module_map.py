"""docs/模块地图.md 必须与 app/ 源码保持同步。"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "模块地图.md").read_text(encoding="utf-8")
INDEX = DOC.split("## 附录：全量模块索引", 1)[1]
INDEX_ROW = re.compile(r"^\| `(app/[^`]+\.py)` \| (D\d\d) \|", re.MULTILINE)


def _source_modules() -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    }


def test_index_lists_every_module_exactly_once():
    listed = Counter(path for path, _ in INDEX_ROW.findall(INDEX))
    duplicated = sorted(path for path, count in listed.items() if count > 1)
    actual = _source_modules()
    assert duplicated == []
    assert sorted(actual - set(listed)) == [], "新增模块未登记到模块地图索引"
    assert sorted(set(listed) - actual) == [], "索引中存在已删除或移动的模块"


def test_index_domains_have_cards():
    cards = set(re.findall(r"^## (D\d\d) ", DOC, re.MULTILINE))
    used = {domain for _, domain in INDEX_ROW.findall(INDEX)}
    assert used <= cards
    assert cards <= used, "存在没有任何模块的领域卡片"


def test_referenced_paths_exist():
    paths = set(re.findall(r"`((?:app|plugins)/[\w./-]*[\w/])`", DOC))
    missing = sorted(path for path in paths if not (ROOT / path).exists())
    assert missing == []


def test_test_globs_match_files():
    globs = set(re.findall(r"tests/test_[\w*]+\.py", DOC))
    empty = sorted(pattern for pattern in globs if not list(ROOT.glob(pattern)))
    assert globs
    assert empty == []
