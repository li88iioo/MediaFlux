from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class StaticAssetBuildTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    STATIC_ROOT = ROOT / "app" / "static"
    PUBLIC_GLOBALS = {
        "js/app.js": (
            "renderLucideIcons",
            "appAlert",
            "appConfirm",
            "openGuangYaDirectoryPicker",
            "window.fetch",
        ),
        "js/motion.js": ("MFAnim",),
        "js/guangya-directory-scrape.js": ("GuangYaDirectoryScrapeUI",),
        "js/subscriptions.js": ("openSubscriptionModal", "closeSubscriptionModal"),
        "js/downloads.js": (
            "switchDlTab",
            "loadOverview",
            "loadLogs",
            "qbAction",
            "qbDelete",
            "resubmitIssue",
            "clearIssue",
        ),
        "js/logs.js": ("switchTab", "loadOrganize", "refreshActive"),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="mediaflux-static-build-")
        cls.output = Path(cls.tempdir.name) / "static"
        completed = subprocess.run(
            [
                "node",
                "packaging/scripts/minify_static.mjs",
                "--input",
                str(cls.STATIC_ROOT),
                "--output",
                str(cls.output),
            ],
            cwd=cls.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "static asset build failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        cls.build_stdout = completed.stdout

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    @staticmethod
    def _files(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_package_manifest_pins_minifier_versions(self) -> None:
        package = json.loads((self.ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["devDependencies"]["terser"], "5.50.0")
        self.assertEqual(package["devDependencies"]["lightningcss"], "1.33.0")
        self.assertTrue((self.ROOT / "package-lock.json").is_file())

    def test_output_preserves_every_static_asset_path(self) -> None:
        self.assertEqual(self._files(self.STATIC_ROOT), self._files(self.output))

    def test_build_rejects_overlapping_input_and_output(self) -> None:
        completed = subprocess.run(
            [
                "node",
                "packaging/scripts/minify_static.mjs",
                "--input",
                str(self.STATIC_ROOT),
                "--output",
                str(self.STATIC_ROOT.parent),
            ],
            cwd=self.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must not overlap", completed.stderr)

    def test_build_refuses_to_remove_an_unowned_output_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-static-unowned-") as root:
            output = Path(root) / "static"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            completed = subprocess.run(
                [
                    "node",
                    "packaging/scripts/minify_static.mjs",
                    "--input",
                    str(self.STATIC_ROOT),
                    "--output",
                    str(output),
                ],
                cwd=self.ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unowned output directory", completed.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_preminified_vendor_assets_are_copied_byte_for_byte(self) -> None:
        for relative in ("js/lucide.min.js", "js/vendor/gsap.min.js"):
            with self.subTest(relative=relative):
                self.assertEqual(
                    self._sha256(self.STATIC_ROOT / relative),
                    self._sha256(self.output / relative),
                )

    def test_first_party_javascript_and_css_are_smaller(self) -> None:
        candidates = [
            relative
            for relative in self._files(self.STATIC_ROOT)
            if relative.endswith((".js", ".css"))
            and not relative.endswith((".min.js", ".min.css"))
        ]
        source_size = sum((self.STATIC_ROOT / relative).stat().st_size for relative in candidates)
        output_size = sum((self.output / relative).stat().st_size for relative in candidates)
        self.assertLess(output_size, source_size)
        self.assertIn("saved", self.build_stdout)

    def test_built_javascript_is_valid_classic_script(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node)
        for javascript in sorted(self.output.rglob("*.js")):
            with self.subTest(javascript=javascript.relative_to(self.output).as_posix()):
                completed = subprocess.run(
                    [node, "--check", str(javascript)],
                    cwd=self.ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_classic_script_contracts_are_preserved(self) -> None:
        for relative, symbols in self.PUBLIC_GLOBALS.items():
            built = (self.output / relative).read_text(encoding="utf-8")
            for symbol in symbols:
                with self.subTest(relative=relative, symbol=symbol):
                    self.assertIn(symbol, built)


class StaticAssetDockerContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_docker_uses_pinned_builder_and_keeps_node_out_of_runtime(self) -> None:
        dockerfile = (self.ROOT / "Dockerfile").read_text(encoding="utf-8")
        builder = (
            "FROM --platform=$BUILDPLATFORM "
            "node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 "
            "AS static-builder"
        )
        runtime = (
            "FROM python:3.13-slim@sha256:"
            "ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a"
        )
        self.assertIn(builder, dockerfile)
        self.assertIn("RUN npm ci --ignore-scripts --no-audit --no-fund", dockerfile)
        self.assertIn("npm run build:static -- --input /src/app/static --output /out", dockerfile)
        self.assertIn("COPY --from=static-builder /out/ /app/app/static/", dockerfile)
        self.assertLess(dockerfile.index(builder), dockerfile.index(runtime))
        self.assertNotIn("apt-get install node", dockerfile)
        self.assertNotIn("COPY --from=static-builder /src", dockerfile)

    def test_docker_context_excludes_local_node_modules(self) -> None:
        ignored = {
            line.strip()
            for line in (self.ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("node_modules", ignored)
        self.assertIn("npm-debug.log*", ignored)
