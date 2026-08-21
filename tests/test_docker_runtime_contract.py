from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from app.runtime_paths import RuntimePaths


class DockerRuntimeContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (cls.ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.compose_text = (cls.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        cls.compose = yaml.safe_load(cls.compose_text)
        cls.env_example = (cls.ROOT / ".env.example").read_text(encoding="utf-8")
        cls.dockerignore = (cls.ROOT / ".dockerignore").read_text(encoding="utf-8")

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

        healthcheck = self.compose["services"]["mediaflux"]["healthcheck"]["test"]
        self.assertEqual(healthcheck[:2], ["CMD", "python"])
        self.assertIn("127.0.0.1:1258/readyz", healthcheck[-1])
        self.assertNotIn("/healthz", healthcheck[-1])
        self.assertNotIn("flask_port", healthcheck[-1])

    def test_compose_preserves_existing_port_and_volume_contracts(self) -> None:
        service = self.compose["services"]["mediaflux"]
        self.assertEqual(service["stop_grace_period"], "60s")
        self.assertEqual(
            service["image"],
            "${MEDIAFLUX_IMAGE:-ghcr.io/li88iioo/mediaflux:latest}",
        )
        self.assertEqual(
            service["ports"],
            ["${MEDIAFLUX_PUBLISH_HOST:-127.0.0.1}:${WEB_PORT:-1258}:1258"],
        )
        self.assertEqual(
            service["volumes"],
            [
                "./db:/app/db",
                "${STRM_HOST_PATH:-./strm-data}:/data/strm",
                "${MEDIA_DOWNLOADS_HOST_PATH:-./media-downloads}:/media/downloads",
                "${MEDIA_LIBRARY_HOST_PATH:-./media-library}:/media/library",
            ],
        )
        self.assertNotIn("env_file", service)
        environment = service["environment"]
        self.assertEqual(
            set(environment),
            {
                "APP_ENV", "WEB_HOST", "WEB_DEBUG", "WEB_SECRET_KEY",
                "MEDIAFLUX_INITIALIZED",
                "WEB_PORT",
                "ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD",
                "SESSION_COOKIE_SECURE", "LOCAL_MEDIA_BROWSE_ROOTS",
            },
        )
        self.assertEqual(environment["APP_ENV"], "production")
        self.assertEqual(environment["WEB_HOST"], "0.0.0.0")
        self.assertEqual(environment["WEB_PORT"], "1258")
        self.assertNotIn("STRM_ROOT", environment)
        self.assertEqual(
            environment["LOCAL_MEDIA_BROWSE_ROOTS"],
            "${LOCAL_MEDIA_BROWSE_ROOTS:-/media/downloads,/media/library}",
        )
        self.assertEqual(
            environment["WEB_SECRET_KEY"],
            "${WEB_SECRET_KEY:?set WEB_SECRET_KEY in .env}",
        )
        self.assertEqual(
            environment["ENV_WEB_PASSPORT"],
            "${ENV_WEB_PASSPORT:?set ENV_WEB_PASSPORT in .env}",
        )
        self.assertEqual(
            environment["ENV_WEB_PASSWORD"],
            "${ENV_WEB_PASSWORD:?set ENV_WEB_PASSWORD in .env}",
        )
        self.assertEqual(
            environment["SESSION_COOKIE_SECURE"],
            "${SESSION_COOKIE_SECURE:-0}",
        )

    def test_image_bundles_ffprobe_before_switching_to_non_root_user(self) -> None:
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg", self.dockerfile)
        self.assertIn("MEDIAFLUX_FFPROBE=/usr/bin/ffprobe", self.dockerfile)
        self.assertIn("/usr/bin/ffprobe -version >/dev/null 2>&1", self.dockerfile)
        self.assertLess(
            self.dockerfile.index("apt-get install -y --no-install-recommends ffmpeg"),
            self.dockerfile.index("USER mediaflux"),
        )
        self.assertIn("rm -rf /var/lib/apt/lists/*", self.dockerfile)

    def test_compose_keeps_non_root_security_contract(self) -> None:
        service = self.compose["services"]["mediaflux"]
        self.assertIn("USER mediaflux", self.dockerfile)
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_env_example_uses_safe_placeholders_and_explains_port_scope(self) -> None:
        self.assertRegex(self.env_example, r"(?m)^WEB_SECRET_KEY=\s*$")
        self.assertRegex(self.env_example, r"(?m)^ENV_WEB_PASSPORT=\s*$")
        self.assertRegex(self.env_example, r"(?m)^ENV_WEB_PASSWORD=\s*$")
        self.assertIn("留空时 Compose 会拒绝启动", self.env_example)
        self.assertIn("secrets.token_urlsafe", self.env_example)
        self.assertNotRegex(self.env_example, r"(?m)^ENV_WEB_PASSPORT=admin\s*$")
        self.assertNotRegex(self.env_example, r"(?m)^ENV_WEB_PASSWORD=123456\s*$")
        self.assertIn("MEDIAFLUX_PUBLISH_HOST=127.0.0.1", self.env_example)
        self.assertIn("局域网访问时显式改为 0.0.0.0", self.env_example)
        self.assertIn("WEB_PORT 只控制宿主机发布端口", self.env_example)
        self.assertNotIn("MEDIAFLUX_CONTAINER_PORT", self.env_example)
        self.assertIn("经 HTTPS 反向代理访问时必须设为 1", self.env_example)
        self.assertIn("MEDIAFLUX_IMAGE=ghcr.io/li88iioo/mediaflux:latest", self.env_example)
        self.assertIn("不要再从 Web 设置页修改", self.env_example)
        self.assertIn("WEB_HOST=0.0.0.0", self.env_example)
        self.assertIn("MEDIAFLUX_DATA_DIR=/app/db", self.env_example)
        self.assertIn("MEDIAFLUX_CONFIG_DIR=/app/db", self.env_example)
        self.assertIn("MEDIAFLUX_CACHE_DIR=/app/db/cache", self.env_example)
        self.assertIn("MEDIAFLUX_LOG_DIR=/app/db/logs", self.env_example)
        self.assertIn("MEDIAFLUX_STRM_DIR=/data/strm", self.env_example)
        self.assertIn("MEDIA_DOWNLOADS_HOST_PATH=./media-downloads", self.env_example)
        self.assertIn("MEDIA_LIBRARY_HOST_PATH=./media-library", self.env_example)
        self.assertIn(
            "LOCAL_MEDIA_BROWSE_ROOTS=/media/downloads,/media/library",
            self.env_example,
        )
        self.assertIn("业务配置请在 Web「设置」页维护", self.env_example)
        self.assertIn("./db/user.env", self.env_example)
        for application_key in (
            "AGENT_ENABLED",
            "TG_BOT_TOKEN",
            "TMDB_API_KEY",
            "QB_URL",
            "MEDIA_MOVIE_TEMPLATE",
            "AGENT_LLM_API_KEY",
            "TAVILY_API_KEY",
        ):
            self.assertNotRegex(
                self.env_example, rf"(?m)^{application_key}=",
            )
        self.assertNotIn("${showTitle}", self.env_example)


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

    def test_bare_container_cli_fails_closed_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-docker-contract-") as root:
            root_path = Path(root)
            data_dir = root_path / "db"
            env = {
                **os.environ,
                "APP_ENV": "production",
                "MEDIAFLUX_CONTAINER": "1",
                "MEDIAFLUX_DATA_DIR": str(data_dir),
                "MEDIAFLUX_CONFIG_DIR": str(data_dir),
                "MEDIAFLUX_CACHE_DIR": str(data_dir / "cache"),
                "MEDIAFLUX_LOG_DIR": str(data_dir / "logs"),
                "MEDIAFLUX_STRM_DIR": str(root_path / "strm"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for key in ("ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD", "WEB_SECRET_KEY"):
                env.pop(key, None)

            completed = subprocess.run(
                [sys.executable, "mediaflux.py", "start"],
                cwd=self.ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("MediaFlux container refused to start", completed.stderr)
        self.assertIn("ENV_WEB_PASSPORT", completed.stderr)
        self.assertIn("ENV_WEB_PASSWORD", completed.stderr)
        self.assertIn("WEB_SECRET_KEY", completed.stderr)


if __name__ == "__main__":
    unittest.main()
