from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from app.cli import main
from app.modules.update_check import UpdateCheckError, check_for_updates
from app.version import BuildInfo


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self.payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def build(version: str = "1.2.3", package: str = "docker") -> BuildInfo:
    return BuildInfo("MediaFlux", version, "abc", "now", "3.12", "Linux", package)


class UpdateCheckTests(unittest.TestCase):
    def test_selects_newer_stable_release_and_matching_asset(self) -> None:
        payload = [
            {
                "tag_name": "v1.3.0-beta.1", "draft": False, "prerelease": True,
                "html_url": "https://github.com/li88iioo/MediaFlux/releases/tag/v1.3.0-beta.1",
                "assets": [],
            },
            {
                "tag_name": "v1.2.4", "draft": False, "prerelease": False,
                "published_at": "2026-08-09T00:00:00Z",
                "html_url": "https://github.com/li88iioo/MediaFlux/releases/tag/v1.2.4",
                "assets": [{
                    "name": "MediaFlux-1.2.4-source.tar.gz",
                    "browser_download_url": "https://github.com/li88iioo/MediaFlux/releases/download/v1.2.4/MediaFlux-1.2.4-source.tar.gz",
                }],
            },
        ]
        result = check_for_updates(
            build=build(), machine="AMD64", opener=lambda *_a, **_k: FakeResponse(payload)
        )
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "1.2.4")
        self.assertEqual(result.recommended_asset_name, "MediaFlux-1.2.4-source.tar.gz")

    def test_prerelease_is_opt_in_and_stable_beats_older_prerelease(self) -> None:
        payload = [
            {"tag_name": "v2.0.0-beta.1", "draft": False, "prerelease": True, "assets": []},
            {"tag_name": "v1.9.9", "draft": False, "prerelease": False, "assets": []},
        ]
        stable = check_for_updates(build=build("1.9.8"), opener=lambda *_a, **_k: FakeResponse(payload))
        preview = check_for_updates(
            build=build("1.9.8"), include_prerelease=True,
            opener=lambda *_a, **_k: FakeResponse(payload),
        )
        self.assertEqual(stable.latest_version, "1.9.9")
        self.assertEqual(preview.latest_version, "2.0.0-beta.1")

    def test_stable_release_sorts_after_same_core_prerelease(self) -> None:
        payload = [
            {"tag_name": "v2.0.0-beta.2", "draft": False, "prerelease": True, "assets": []},
            {"tag_name": "v2.0.0", "draft": False, "prerelease": False, "assets": []},
        ]
        result = check_for_updates(
            build=build("2.0.0-beta.1"), include_prerelease=True,
            opener=lambda *_a, **_k: FakeResponse(payload),
        )
        self.assertEqual(result.latest_version, "2.0.0")
        self.assertTrue(result.update_available)

    def test_rejects_invalid_or_oversized_response(self) -> None:
        with self.assertRaises(UpdateCheckError):
            check_for_updates(build=build(), opener=lambda *_a, **_k: FakeResponse({"bad": True}))
        response = FakeResponse([])
        response.headers["Content-Length"] = str(1024 * 1024 + 1)
        with self.assertRaises(UpdateCheckError):
            check_for_updates(build=build(), opener=lambda *_a, **_k: response)

    def test_cli_json_output(self) -> None:
        result = type("Result", (), {"as_dict": lambda self: {
            "current_version": "1.2.3", "latest_version": "1.2.4", "update_available": True
        }})()
        output = io.StringIO()
        with patch("app.modules.update_check.check_for_updates", return_value=result), patch("sys.stdout", output):
            self.assertEqual(main(["update", "check", "--json"]), 0)
        self.assertTrue(json.loads(output.getvalue())["update_available"])


if __name__ == "__main__":
    unittest.main()
