from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from dotenv import dotenv_values

from app.runtime_paths import RuntimePaths


class DockerRuntimeContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (cls.ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.compose_text = (cls.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        cls.compose = yaml.safe_load(cls.compose_text)
        cls.dev_compose = yaml.safe_load(
            (cls.ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
        )
        cls.dev_env_example = (
            cls.ROOT / ".env.development.example"
        ).read_text(encoding="utf-8")
        cls.development_env = {
            key: value
            for key, value in dotenv_values(
                cls.ROOT / ".env.development.example"
            ).items()
            if value is not None
        }
        cls.entrypoint = (
            cls.ROOT / "packaging" / "scripts" / "docker-entrypoint.sh"
        ).read_text(encoding="utf-8")
        cls.dockerignore = (cls.ROOT / ".dockerignore").read_text(encoding="utf-8")
        cls.workflow = (
            cls.ROOT / ".github" / "workflows" / "docker.yml"
        ).read_text(encoding="utf-8")

    def test_image_uses_unified_single_worker_runtime_entrypoint(self) -> None:
        self.assertIn("COPY mediaflux.py /app/", self.dockerfile)
        self.assertIn('CMD ["python", "mediaflux.py", "start"]', self.dockerfile)
        self.assertNotIn('CMD ["uvicorn"', self.dockerfile)
        self.assertNotRegex(self.dockerfile, r"--workers(?:=|\s+)[2-9]")
        self.assertIn("# 后台调度器与 Telegram Bot 采用进程内单例", self.dockerfile)

    def test_image_declares_compatible_writable_runtime_paths(self) -> None:
        expected = {
            "MEDIAFLUX_DATA_DIR": "/app/db",
            "MEDIAFLUX_CONFIG_DIR": "/app/db",
            "MEDIAFLUX_CACHE_DIR": "/app/db/cache",
            "MEDIAFLUX_LOG_DIR": "/app/db/logs",
            "MEDIAFLUX_STRM_DIR": "/data/strm",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertRegex(self.dockerfile, rf"(?m)^\s*{key}={value}(?:\s*\\)?$")

        self.assertNotRegex(self.dockerfile, r"(?m)^\s*STRM_ROOT=")
        self.assertNotRegex(self.dockerfile, r"(?m)^\s*WEB_PORT=")
        self.assertIn("/app/db/cache", self.dockerfile)
        self.assertIn("/app/db/logs", self.dockerfile)
        self.assertIn("/data/strm", self.dockerfile)

        paths = RuntimePaths.from_environment(expected, platform_name="Linux", frozen=False)
        self.assertEqual(paths.data_dir, Path("/app/db"))
        self.assertEqual(paths.config_dir, Path("/app/db"))
        self.assertEqual(paths.cache_dir, Path("/app/db/cache"))
        self.assertEqual(paths.log_dir, Path("/app/db/logs"))
        self.assertEqual(paths.strm_dir, Path("/data/strm"))
        self.assertEqual(paths.database_path, Path("/app/db/mediaflux.db"))
        self.assertEqual(paths.env_file, Path("/app/db/user.env"))

    def test_container_strm_runtime_path_is_the_default_business_output_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-docker-strm-default-") as root:
            root_path = Path(root)
            data_dir = root_path / "db"
            strm_dir = root_path / "strm"
            env = {
                **os.environ,
                "MEDIAFLUX_DATA_DIR": str(data_dir),
                "MEDIAFLUX_CONFIG_DIR": str(data_dir),
                "MEDIAFLUX_CACHE_DIR": str(data_dir / "cache"),
                "MEDIAFLUX_LOG_DIR": str(data_dir / "logs"),
                "MEDIAFLUX_STRM_DIR": str(strm_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            env.pop("STRM_ROOT", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from app.config import get; print(get('STRM_ROOT'))",
                ],
                cwd=self.ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(strm_dir))

    def test_healthcheck_uses_fixed_container_port_and_readiness(self) -> None:
        self.assertIn("127.0.0.1:1258/readyz", self.dockerfile)
        self.assertNotIn("/healthz", self.dockerfile)
        self.assertNotIn("flask_port", self.dockerfile)
        self.assertNotIn("healthcheck", self.compose["services"]["mediaflux"])
        self.assertIn("export WEB_PORT=1258", self.entrypoint)
        self.assertLess(
            self.entrypoint.index("export WEB_PORT=1258"),
            self.entrypoint.index('if [ "$(id -u)" -ne 0 ]; then'),
        )

    def test_production_compose_is_single_file_and_first_run_ready(self) -> None:
        service = self.compose["services"]["mediaflux"]
        self.assertEqual(service["stop_grace_period"], "60s")
        self.assertEqual(
            service["image"],
            "ghcr.io/li88iioo/mediaflux:latest",
        )
        self.assertNotIn("build", service)
        self.assertNotIn("env_file", service)
        self.assertNotIn("environment", service)
        self.assertNotIn("ports", service)
        self.assertEqual(service["network_mode"], "host")
        self.assertEqual(
            service["volumes"],
            [
                "./data:/app/db",
                "./strm:/data/strm",
                "./downloads:/media/downloads",
                "./library:/media/library",
            ],
        )

    def test_development_has_one_independent_compose_and_env(self) -> None:
        service = self.dev_compose["services"]["mediaflux"]
        self.assertEqual(service["image"], "${MEDIAFLUX_DEV_IMAGE}")
        self.assertEqual(service["build"]["context"], ".")
        self.assertEqual(service["build"]["dockerfile"], "Dockerfile")
        self.assertEqual(service["env_file"], [".env.development"])
        self.assertNotIn("environment", service)
        self.assertEqual(self.development_env["APP_ENV"], "development")
        self.assertNotIn("MEDIAFLUX_RUN_AS_ROOT", self.development_env)
        self.assertNotIn("MEDIAFLUX_ALLOW_REMOTE_SETUP", self.development_env)
        self.assertNotIn("WEB_PORT", self.development_env)
        self.assertEqual(
            service["ports"],
            ["${MEDIAFLUX_PUBLISH_PORT}:1258", "${MEDIA_PROXY_PORT}:18096"],
        )
        self.assertEqual(
            service["volumes"],
            [
                "${MEDIAFLUX_DATA_HOST_PATH}:/app/db",
                "${STRM_HOST_PATH}:/data/strm",
                "${MEDIA_DOWNLOADS_HOST_PATH}:/media/downloads",
                "${MEDIA_LIBRARY_HOST_PATH}:/media/library",
            ],
        )
        self.assertIn("MEDIAFLUX_DEV_IMAGE=mediaflux:dev", self.dev_env_example)
        self.assertIn("STRM_HOST_PATH=./strm-data", self.dev_env_example)
        self.assertIn("# ---------- 宿主机端口 ----------", self.dev_env_example)
        self.assertIn("# MEDIAFLUX_RUN_AS_ROOT=0", self.dev_env_example)

        referenced = set(re.findall(r"\$\{([A-Z0-9_]+)\}", (self.ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")))
        self.assertEqual(referenced - self.development_env.keys(), set())

    def test_image_entrypoint_defaults_to_nas_compatibility_and_supports_drop_privileges(self) -> None:
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg gosu", self.dockerfile)
        self.assertIn("MEDIAFLUX_FFPROBE=/usr/bin/ffprobe", self.dockerfile)
        self.assertIn("/usr/bin/ffprobe -version >/dev/null 2>&1", self.dockerfile)
        self.assertIn("COPY packaging/scripts/docker-entrypoint.sh", self.dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/mediaflux-entrypoint"]', self.dockerfile)
        self.assertNotIn("USER mediaflux", self.dockerfile)
        self.assertIn('exec gosu "$puid:$pgid" "$@"', self.entrypoint)
        self.assertIn('chown -R "$puid:$pgid" /app/db', self.entrypoint)
        self.assertIn('data_owner_marker="/app/db/.mediaflux-owner-${puid}-${pgid}"', self.entrypoint)
        self.assertIn("MEDIAFLUX_FIX_DATA_PERMISSIONS", self.entrypoint)
        self.assertEqual(self.entrypoint.count('chown -R "$puid:$pgid" /app/db'), 1)
        self.assertIn('chown "$puid:$pgid" /data/strm', self.entrypoint)
        self.assertNotIn("/media/downloads", self.entrypoint)
        self.assertNotIn("/media/library", self.entrypoint)
        self.assertIn("MEDIAFLUX_RUN_AS_ROOT", self.entrypoint)
        root_mode = self.entrypoint.split(
            'if is_enabled "${MEDIAFLUX_RUN_AS_ROOT:-1}"; then', 1
        )[1].split("fi", 1)[0]
        self.assertIn("rm -f /app/db/.mediaflux-owner-*", root_mode)
        self.assertLess(
            root_mode.index("rm -f /app/db/.mediaflux-owner-*"),
            root_mode.index('exec "$@"'),
        )
        self.assertIn("MEDIAFLUX_FIX_STRM_PERMISSIONS", self.entrypoint)
        self.assertIn("rm -rf /var/lib/apt/lists/*", self.dockerfile)

    def test_non_root_permission_contract_is_consistent_in_user_docs(self) -> None:
        deploy = (self.ROOT / "docs" / "部署指南.md").read_text(encoding="utf-8")
        faq = (self.ROOT / "docs" / "常见问题.md").read_text(encoding="utf-8")
        for text in (deploy, faq, self.dev_env_example):
            self.assertIn("MEDIAFLUX_FIX_DATA_PERMISSIONS", text)
            self.assertIn("MEDIAFLUX_FIX_STRM_PERMISSIONS", text)
        self.assertIn("入口脚本**永不递归改权**", deploy)
        self.assertIn("不要同时配置 Compose 的 `user:`", deploy)

    def test_production_deployment_requires_no_env_file(self) -> None:
        self.assertFalse((self.ROOT / ".env.example").exists())
        service = self.compose["services"]["mediaflux"]
        self.assertNotIn("env_file", service)
        self.assertNotIn("environment", service)
        self.assertIn('"0.0.0.0:111:1258"', self.compose_text)
        self.assertIn('"0.0.0.0:222:18096"', self.compose_text)

    def test_build_context_excludes_local_artifacts_and_agent_state(self) -> None:
        ignored = {
            line.strip()
            for line in self.dockerignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for path in (
            "dist",
            "build",
            "tests",
            ".github",
            ".claude",
            ".superpowers",
            ".pytest_cache",
        ):
            with self.subTest(path=path):
                self.assertIn(path, ignored)

    def test_dockerfile_pins_multiarch_base_and_hashed_runtime_lock(self) -> None:
        self.assertIn(
            "FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a",
            self.dockerfile,
        )
        self.assertIn("COPY requirements-release-runtime.lock", self.dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes", self.dockerfile)
        self.assertNotIn("COPY requirements.txt /app/requirements.txt", self.dockerfile)

    def test_dockerfile_embeds_build_metadata_from_build_args(self) -> None:
        for value in (
            "ARG VERSION_REF",
            "ARG GIT_SHA",
            "ARG TARGETARCH",
            "packaging/scripts/generate_build_info.py",
            "--package docker",
            "/app/app/_build_info.json",
            "chown -R root:root /app/app /app/mediaflux.py",
            "chmod -R a-w /app/app /app/mediaflux.py",
            "chmod 0444 /app/app/_build_info.json",
        ):
            self.assertIn(value, self.dockerfile)
        self.assertNotIn("COPY --chown=mediaflux:mediaflux app /app/app", self.dockerfile)

    def test_release_workflow_fails_closed_before_publishing(self) -> None:
        for value in (
            "if: github.event_name != 'pull_request'",
            'git merge-base --is-ancestor "$BUILD_SHA" origin/main',
            'module._changelog_section(',
            'permissions:\n  contents: read',
            "from app import __version__",
            "does not match source version",
            "CHANGELOG.md is missing a dated, non-empty",
            "provenance: mode=max",
            "sbom: true",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_release_workflow_smoke_and_assets_cover_release_contract(self) -> None:
        for value in (
            "http://127.0.0.1:1258/readyz",
            "http://127.0.0.1:1258/healthz",
            "python mediaflux.py doctor --json",
            "BUILD-INFO.json",
            "RELEASE-NOTES.txt",
            "PYTHON-DEPENDENCIES.spdx.json",
            '"linux/amd64:x86_64"',
            '"linux/arm64:aarch64"',
            'git merge-base --is-ancestor "$EXPECTED_SHA" origin/main',
            "SHA256SUMS",
            'gh release upload "$VERSION_REF" --clobber',
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_official_container_defaults_cover_zero_env_first_run(self) -> None:
        self.assertIn("MEDIAFLUX_CONTAINER=1", self.dockerfile)
        self.assertIn('MEDIAFLUX_RUN_AS_ROOT:-1', self.entrypoint)


if __name__ == "__main__":
    unittest.main()
