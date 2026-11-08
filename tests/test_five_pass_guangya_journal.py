"""光鸭统一变更操作日志追加实现，历史 JSONL 字节协议保持不变。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.modules import guangya_fs_change

_MODULES = (guangya_fs_change,)


class GuangYaJournalProtocolTests(unittest.TestCase):
    def test_all_operations_preserve_existing_history_and_same_jsonl_bytes(self):
        stamp = "2026-09-07T18:00:00+08:00"
        prior = b'{"action":"historical","version":1}\n'
        events = [{"action": "intent", "name": "目录\n伴随字幕", "count": 2},
                  {"action": "final", "at": "preserved-event-time", "ok": True}]
        expected = prior + "".join(
            json.dumps({"at": stamp, **event}, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ).encode()
        with tempfile.TemporaryDirectory() as root:
            for index, module in enumerate(_MODULES):
                with self.subTest(module=module.__name__):
                    path = Path(root) / f"{index}.jsonl"
                    path.write_bytes(prior)
                    with patch.object(module, "_journal_path", return_value=path), patch.object(module, "_now_iso", return_value=stamp):
                        for event in events:
                            module._append_journal("a" * 32, event)
                    self.assertEqual(path.read_bytes(), expected)

    def test_fsync_failure_remains_visible_and_next_append_can_recover(self):
        with tempfile.TemporaryDirectory() as root:
            for index, module in enumerate(_MODULES):
                with self.subTest(module=module.__name__):
                    path = Path(root) / f"{index}.jsonl"
                    with patch.object(module, "_journal_path", return_value=path):
                        with patch("os.fsync", side_effect=OSError("synthetic disk failure")):
                            with self.assertRaises(OSError):
                                module._append_journal("b" * 32, {"action": "uncertain"})
                        module._append_journal("b" * 32, {"action": "recovered"})
                    self.assertEqual([json.loads(line)["action"] for line in path.read_text().splitlines()], ["uncertain", "recovered"])
