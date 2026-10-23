"""无需 SDK 或网络的插件发布回归；所有假构建材料均在隔离目录内。"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "MediaFlux.Jellyfin" / "scripts"
TEMP = ROOT / ".superpowers" / "tmp" / "plugin013-packaging"
SPEC = importlib.util.spec_from_file_location("jellyfin_manifest", SCRIPTS / "manifest.py")
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


class JellyfinPluginPackagingTests(unittest.TestCase):
    remote: dict[str, Any]

    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.plugin = self.repo / "plugins" / "MediaFlux.Jellyfin"
        (self.plugin / "scripts").mkdir(parents=True)
        for name in ("package.sh", "verify.sh", "manifest.py"):
            shutil.copyfile(SCRIPTS / name, self.plugin / "scripts" / name)
        self.project = self.plugin / "MediaFlux.Jellyfin.csproj"
        self.project.write_text(
            '<Project><PropertyGroup><Version>0.1.3</Version>'
            '<TargetFramework>net10.0</TargetFramework><AssemblyName>MediaFlux.Jellyfin</AssemblyName>'
            '</PropertyGroup><ItemGroup><PackageReference Include="Jellyfin.Controller" Version="12.2.0"/>'
            '<PackageReference Include="Jellyfin.Model" Version="12.2.0"/></ItemGroup></Project>',
            encoding="utf-8",
        )
        self.meta: dict[str, Any] = {
            "guid": "9037f29f-2ebc-4b8a-952f-960e24402bb5", "name": "MediaFlux",
            "version": "0.1.3.0", "targetAbi": "12.2.0.0", "owner": "MediaFlux",
            "category": "General", "overview": "说明", "description": "插件说明",
            "timestamp": "2026-10-24T00:00:00Z", "assemblies": ["MediaFlux.Jellyfin.dll"], "imagePath": "logo.png",
        }
        self.write_meta()
        (self.plugin / "Plugin.cs").write_text(self.meta["guid"], encoding="utf-8")
        self.dll = self.plugin / "bin" / "Release" / "net10.0" / "MediaFlux.Jellyfin.dll"
        self.dll.parent.mkdir(parents=True)
        self.dll.write_bytes(b"fixture plugin assembly")
        for name in ("Jellyfin.Api.dll", "MediaBrowser.Controller.dll", "MediaFlux.Jellyfin.Tests.dll"):
            (self.dll.parent / name).write_bytes(b"must not be packaged")
        (self.plugin / "Resources").mkdir()
        (self.plugin / "Resources" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "jellyfin-plugin.md").write_text("用户说明", encoding="utf-8")
        (self.repo / "LICENSE").write_text("MIT fixture license", encoding="utf-8")
        self.previous = self.repo / "previous.json"
        self.previous.write_text("[]", encoding="utf-8")
        self.output = self.repo / "manifest.json"
        self.log = self.repo / "commands.jsonl"
        self.bin = self.repo / "tools"
        self.bin.mkdir()
        self.executable("dotnet", '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["COMMAND_LOG"]).open("a") as log:
    log.write(json.dumps(["dotnet", *args]) + "\\n")
if args == ["--version"]:
    print(os.environ.get("FAKE_SDK", "10.0.402"))
''')
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                    "DOTNET_BIN": str(self.bin / "dotnet"), "COMMAND_LOG": str(self.log),
                    "JELLYFIN_VERIFY_CACHE": str(self.repo / "official"),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def write_meta(self):
        (self.plugin / "meta.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def executable(self, name, code):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + code, encoding="utf-8")
        path.chmod(0o755)

    def package(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return manifest.package(self.plugin)

    def update(self, archive=None):
        manifest.update(self.plugin, self.previous, archive or self.package(), self.output)
        return json.loads(self.output.read_text(encoding="utf-8"))

    def history(self, version="0.1.2.0"):
        item: dict[str, Any] = {key: self.meta[key] for key in manifest.FIELDS}
        item["versions"] = [{"version": version, "targetAbi": "12.2.0.0", "checksum": "a" * 32,
                             "sourceUrl": manifest.DOWNLOAD + "/older/plugin.zip",
                             "timestamp": "2026-10-14T00:00:00Z", "changelog": "历史说明"}]
        item["imageUrl"] = "https://example.invalid/newer.png"
        return [item]

    def test_package_is_reproducible_and_excludes_host_and_tests(self):
        archive = self.package()
        original = archive.read_bytes()
        os.utime(self.dll, (1800000000, 1800000000))
        self.assertEqual(original, self.package().read_bytes())
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), {"MediaFlux.Jellyfin.dll", "meta.json", "logo.png", "README.md", "LICENSE"})
            self.assertIsNone(zipped.testzip())
            self.assertEqual(zipped.read("README.md").decode(), "用户说明")
        self.assertEqual(archive.with_suffix(".zip.sha256").read_text(),
                         f"{hashlib.sha256(original).hexdigest()}  {archive.name}\n")
        self.assertEqual((archive.parent / "logo.png").read_bytes(),
                         (self.plugin / "Resources" / "logo.png").read_bytes())

    def test_metadata_mismatch_fails_before_writing_artifacts(self):
        for key, value in (("version", "0.1.2.0"), ("targetAbi", "12.3.0.0"),
                           ("guid", "00000000-0000-0000-0000-000000000000"),
                           ("assemblies", ["MediaFlux.Jellyfin.dll", "Jellyfin.Api.dll"])):
            with self.subTest(key=key):
                saved = self.meta[key]
                self.meta[key] = value
                self.write_meta()
                with self.assertRaises(ValueError):
                    self.package()
                self.meta[key] = saved
        self.assertFalse((self.plugin / "artifacts").exists())

    def test_production_baseline_is_fixed_at_12_2(self):
        self.project.write_text(self.project.read_text().replace("12.2.0", "12.3.0"))
        with self.assertRaisesRegex(ValueError, "Production baseline"):
            self.package()

    def test_package_shell_builds_before_packaging_and_requires_pinned_sdk(self):
        script = self.plugin / "scripts" / "package.sh"
        result = subprocess.run(["bash", str(script)], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(commands[1], ["dotnet", "build", str(self.project), "-c", "Release", "--nologo"])
        result = subprocess.run(["bash", str(script)], env={**self.env, "FAKE_SDK": "10.0.401"},
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("10.0.402", result.stderr)

    def test_verify_downloads_official_sources_and_selects_only_test_dependency(self):
        self.executable("curl", '''import io, json, os, sys, tarfile
from pathlib import Path
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))
version = url.rsplit("/v", 1)[1]
with Path(os.environ["COMMAND_LOG"]).open("a") as log:
    log.write(json.dumps(["curl", url]) + "\\n")
with tarfile.open(args[args.index("-o") + 1], "w:gz") as archive:
    entry = tarfile.TarInfo(f"jellyfin-{version}/Jellyfin.Api/Jellyfin.Api.csproj")
    data = b"<Project/>"
    entry.size = len(data)
    archive.addfile(entry, io.BytesIO(data))
''')
        script = self.plugin / "scripts" / "verify.sh"
        production = self.project.read_bytes()
        for version in ("12.2", "12.3"):
            with self.subTest(version=version):
                result = subprocess.run(["bash", str(script), version], env=self.env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = [json.loads(line) for line in self.log.read_text().splitlines()]
                self.assertIn(["curl", f"https://codeload.github.com/jellyfin/jellyfin/tar.gz/refs/tags/v{version}"], commands)
                self.assertEqual(commands[-2][1], "build")
                self.assertIn(f"jellyfin-{version}/Jellyfin.Api/Jellyfin.Api.csproj", commands[-2][2])
                self.assertIn(f"-p:JellyfinVersion={version}.0", commands[-1])
                self.assertIn(f"-p:JellyfinApiDir={self.repo}/official/jellyfin-{version}/Jellyfin.Api/bin/Release/net10.0", commands[-1])
        self.assertEqual(production, self.project.read_bytes())

    def test_verify_rejects_unsupported_versions_before_using_sdk_or_network(self):
        for args in (("12.4",), ("12.2", "extra")):
            result = subprocess.run(["bash", str(self.plugin / "scripts" / "verify.sh"), *args],
                                    env=self.env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.log.exists())

    def test_manifest_uses_jellyfin_md5_and_dedicated_release_urls(self):
        archive = self.package()
        entry = self.update(archive)[0]
        release = entry["versions"][0]
        self.assertEqual(release["checksum"], hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest())
        self.assertEqual(release["targetAbi"], "12.2.0.0")
        self.assertEqual(release["sourceUrl"], f"{manifest.DOWNLOAD}/jellyfin-plugin-v0.1.3/{archive.name}")
        self.assertEqual(entry["imageUrl"], f"{manifest.DOWNLOAD}/jellyfin-plugin-v0.1.3/logo.png")
        self.assertNotIn("imagePath", entry)
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(json.loads(zipped.read("meta.json"))["imagePath"], "logo.png")

    def test_manifest_preserves_history_and_repeat_is_identical(self):
        history = self.history()
        self.previous.write_text(json.dumps(history), encoding="utf-8")
        result = self.update()
        self.assertEqual(result[0]["versions"][1], history[0]["versions"][0])
        content = self.output.read_bytes()
        self.previous.write_bytes(content)
        self.update()
        self.assertEqual(self.output.read_bytes(), content)

    def test_out_of_order_release_preserves_newer_metadata(self):
        history = self.history("0.1.10.0")
        history[0]["description"] = "新版说明"
        self.previous.write_text(json.dumps(history), encoding="utf-8")
        result = self.update()[0]
        self.assertEqual([v["version"] for v in result["versions"]], ["0.1.10.0", "0.1.3.0"])
        self.assertEqual(result["description"], "新版说明")
        self.assertEqual(result["imageUrl"], history[0]["imageUrl"])

    def test_changed_contents_at_same_version_leave_manifest_untouched(self):
        self.update()
        content = self.output.read_bytes()
        self.previous.write_bytes(content)
        self.dll.write_bytes(b"different assembly at the same version")
        with self.assertRaisesRegex(ValueError, "existing plugin version"):
            self.update()
        self.assertEqual(self.output.read_bytes(), content)
        self.assertEqual(self.previous.read_bytes(), content)

    def test_unknown_manifest_and_duplicate_versions_are_rejected(self):
        duplicate = self.history()
        duplicate[0]["versions"] *= 2
        foreign = self.history()
        foreign[0]["guid"] = "00000000-0000-0000-0000-000000000000"
        for value in ({}, [None], foreign, duplicate, [{"guid": self.meta["guid"]}]):
            with self.subTest(value=value):
                self.previous.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    self.update()
                self.assertFalse(self.output.exists())

    def test_archive_with_host_dll_is_rejected_before_manifest_write(self):
        archive = self.package()
        with zipfile.ZipFile(archive, "a") as zipped:
            zipped.writestr("Jellyfin.Api.dll", b"host")
        with self.assertRaisesRegex(ValueError, "Invalid plugin ZIP"):
            self.update(archive)
        self.assertFalse(self.output.exists())

    def remote_release(self, tag, assets=None):
        if not hasattr(self, "remote"):
            self.remote = {"releases": {}, "tags": ["jellyfin-plugin-v0.1.3"]}
        self.remote["releases"][tag] = {"draft": False}
        if tag not in self.remote["tags"]:
            self.remote["tags"].append(tag)
        directory = self.repo / "remote" / tag
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in (assets or {}).items():
            (directory / name).write_bytes(data)

    def publish(self):
        if not (self.plugin / "artifacts").exists():
            self.package()
        if not hasattr(self, "remote"):
            self.remote = {"releases": {}, "tags": ["jellyfin-plugin-v0.1.3"]}
        state = self.repo / "remote.json"
        state.write_text(json.dumps(self.remote), encoding="utf-8")
        self.executable("gh", '''import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text())
root = state_path.parent / "remote"
with Path(os.environ["COMMAND_LOG"]).open("a") as log:
    log.write(json.dumps(["gh", *args]) + "\\n")
if args[0] == "api":
    if state.get("fail_api"):
        raise SystemExit(1)
    if "/releases?" in args[-1]:
        print(json.dumps([[{"tag_name": tag, **info} for tag, info in state["releases"].items()]]))
    else:
        print(json.dumps([[{"name": tag} for tag in state["tags"]]]))
    raise SystemExit(0)
command, tag = args[1:3]
directory = root / tag
if command == "create":
    if tag in state["releases"] or "--latest=false" not in args:
        raise SystemExit(1)
    state["releases"][tag] = {"draft": "--draft" in args}
    state["tags"].append(tag)
    directory.mkdir(parents=True, exist_ok=True)
    if Path(args[3]).is_file():
        shutil.copyfile(args[3], directory / Path(args[3]).name)
elif command == "view":
    print(json.dumps({"isDraft": state["releases"][tag]["draft"],
                      "assets": [{"name": path.name} for path in directory.iterdir()]}))
elif command == "download":
    if state.get("fail_download") == tag:
        raise SystemExit(1)
    name = args[args.index("--pattern") + 1]
    destination = (Path(args[args.index("--output") + 1]) if "--output" in args
                   else Path(args[args.index("--dir") + 1]) / name)
    data = (directory / name).read_bytes()
    if destination.parent.name == "published" and state.get("corrupt_download"):
        data += b"corrupt"
    if destination.parent.name == "current" and state.get("manifest_race"):
        data += b"changed"
    destination.write_bytes(data)
elif command == "upload":
    source = Path(args[3])
    if (directory / source.name).exists() and "--clobber" not in args:
        raise SystemExit(1)
    shutil.copyfile(source, directory / source.name)
elif command == "edit":
    state["releases"][tag]["draft"] = False
else:
    raise SystemExit(1)
state_path.write_text(json.dumps(state))
''')
        runner = Path(tempfile.mkdtemp(dir=self.repo))
        (runner / "plugin.json").write_text(json.dumps(manifest.metadata(self.plugin)), encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "jellyfin-plugin.yml").read_text(encoding="utf-8")
        script = textwrap.dedent(workflow.rsplit("        run: |\n", 1)[1])
        result = subprocess.run(["bash", "-c", script], cwd=self.repo,
                                env={**self.env, "FAKE_GH_STATE": str(state), "RUNNER_TEMP": str(runner),
                                     "GH_REPO": "li88iioo/MediaFlux", "GITHUB_SHA": "a" * 40},
                                capture_output=True, text=True, timeout=30)
        self.remote = json.loads(state.read_text())
        return result

    def mutations(self):
        return [args for args in (json.loads(line) for line in self.log.read_text().splitlines())
                if args[:2] == ["gh", "release"] and args[2] in ("upload", "create", "edit")]

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_initializes_repository_and_retries_without_overwriting_assets(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.remote["releases"]), {"jellyfin-plugin-v0.1.3", "manifest"})
        self.assertEqual(self.mutations()[-1][2:4], ["create", "manifest"])
        mutations = self.mutations()
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(), mutations)

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_preserves_history_when_publishing_next_version(self):
        previous = json.dumps(self.history()).encode()
        self.remote_release("manifest", {"manifest.json": previous})
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads((self.repo / "remote" / "manifest" / "manifest.json").read_bytes())
        self.assertEqual([v["version"] for v in current[0]["versions"]], ["0.1.3.0", "0.1.2.0"])
        self.assertEqual(self.mutations()[-1][2:4], ["upload", "manifest"])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_rejects_conflicting_published_zip_without_uploading(self):
        archive = self.package()
        self.remote_release("jellyfin-plugin-v0.1.3", {archive.name: b"different contents"})
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])
        self.assertNotIn("manifest", self.remote["releases"])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_blocks_unknown_manifest_tag(self):
        self.remote = {"releases": {}, "tags": ["manifest", "jellyfin-plugin-v0.1.3"]}
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown manifest tag", result.stderr)
        self.assertEqual(self.mutations(), [])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_does_not_treat_api_failure_as_empty_repository(self):
        self.remote = {"releases": {}, "tags": [], "fail_api": True}
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_does_not_replace_unreadable_manifest(self):
        self.remote_release("manifest")
        self.remote["fail_download"] = "manifest"
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_rejects_corrupt_download_before_creating_manifest(self):
        self.remote = {"releases": {}, "tags": [], "corrupt_download": True}
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(args[2:4] == ["upload", "jellyfin-plugin-v0.1.3"] for args in self.mutations()))
        self.assertNotIn("manifest", self.remote["releases"])

    @unittest.skipUnless(shutil.which("jq"), "发布链集成测试需要 jq")
    def test_workflow_rechecks_manifest_before_overwriting(self):
        previous = json.dumps(self.history()).encode()
        self.remote_release("manifest", {"manifest.json": previous})
        self.remote["manifest_race"] = True
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(args[2:4] == ["upload", "jellyfin-plugin-v0.1.3"] for args in self.mutations()))
        self.assertEqual((self.repo / "remote" / "manifest" / "manifest.json").read_bytes(), previous)
        self.assertFalse(any(args[2:4] == ["upload", "manifest"] for args in self.mutations()))


if __name__ == "__main__":
    unittest.main()
