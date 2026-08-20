"""识别共享值对象的兼容与导入边界测试。"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


class RecognitionModelTests(unittest.TestCase):
    def test_scraper_reexports_the_same_shared_model_objects(self):
        from app.modules import scraper
        from app.modules.recognition.models import (
            ReleaseParseEvidence,
            ReleaseParseToken,
        )

        self.assertIs(scraper.ReleaseParseToken, ReleaseParseToken)
        self.assertIs(scraper.ReleaseParseEvidence, ReleaseParseEvidence)
        self.assertEqual(ReleaseParseToken.__module__, "app.modules.scraper")
        self.assertEqual(ReleaseParseEvidence.__module__, "app.modules.scraper")

    def test_token_and_evidence_defaults_and_serialization_remain_stable(self):
        from app.modules.recognition.models import (
            ReleaseParseEvidence,
            ReleaseParseToken,
        )

        token = ReleaseParseToken(kind="episode", value="12")
        evidence = ReleaseParseEvidence(
            kind="folder_title",
            source="parent_path",
            value={"title": "示例剧"},
            confidence=0.987654,
        )

        self.assertEqual(
            token.to_dict(),
            {"kind": "episode", "value": "12", "source": "filename"},
        )
        self.assertEqual(
            evidence.to_dict(),
            {
                "kind": "folder_title",
                "source": "parent_path",
                "value": {"title": "示例剧"},
                "confidence": 0.9877,
            },
        )
        json.dumps([token.to_dict(), evidence.to_dict()], ensure_ascii=False)
        with self.assertRaises(FrozenInstanceError):
            token.value = "13"

    def test_models_import_first_does_not_load_scraper_or_create_a_cycle(self):
        script = textwrap.dedent(
            """
            import sys
            from app.modules.recognition import models

            assert "app.modules.scraper" not in sys.modules
            import app.modules.scraper as scraper
            assert scraper.ReleaseParseToken is models.ReleaseParseToken
            assert scraper.ReleaseParseEvidence is models.ReleaseParseEvidence
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
