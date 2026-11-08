from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


class DockerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = Path(".github/workflows/docker.yml").read_text(encoding="utf-8")
        cls.web_smoke = Path("packaging/scripts/web_setup_smoke.py").read_text(encoding="utf-8")

    def test_test_jobs_prepare_private_tmp_before_execution(self) -> None:
        jobs = yaml.safe_load(self.text)["jobs"]
        for name, runner in (("test", "Run tests"), ("browser", "Run real browser regression tests without skips")):
            steps = jobs[name]["steps"]
            index = next(i for i, step in enumerate(steps) if step.get("name") == runner)
            self.assertTrue(steps[index]["run"].startswith("umask 077\n"))
            setup = steps[index - 1]
            self.assertEqual(setup["name"], "Prepare private test temporary directory")
            with tempfile.TemporaryDirectory() as home:
                exported = Path(home) / "github-env"
                subprocess.run(["bash", "-eu", "-c", setup["run"]], check=True,
                               env={**os.environ, "HOME": home, "GITHUB_ENV": str(exported)})
                temporary = Path(exported.read_text().strip().removeprefix("TMPDIR="))
                self.assertEqual(temporary.parent, Path(home))
                self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)

    def test_main_and_pull_request_and_tags_trigger_workflow(self) -> None:
        push_trigger = self.text.split("  push:", 1)[1].split("  pull_request:", 1)[0]
        self.assertIn("branches: [main]", push_trigger)
        self.assertIn("tags: ['v*']", push_trigger)
        self.assertNotIn("workflow_dispatch:", self.text)

    def test_main_push_does_not_cancel_an_in_progress_release_gate(self) -> None:
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            self.text,
        )
        self.assertNotIn("github.ref_type != 'tag'", self.text)

    def test_release_tags_share_one_serial_promotion_gate(self) -> None:
        self.assertIn(
            "group: docker-${{ startsWith(github.ref, 'refs/tags/v') && 'release-promotion' || github.ref }}",
            self.text,
        )
        self.assertNotIn("group: docker-${{ github.ref }}", self.text)
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            self.text,
        )

    def test_multiarch_build_only_runs_for_version_tags(self) -> None:
        build_job = self.text.split("  build:", 1)[1]
        self.assertIn(
            "if: startsWith(github.ref, 'refs/tags/v')",
            build_job,
        )
        self.assertNotIn("if: github.event_name != 'pull_request'", build_job)

    def test_image_is_smoke_tested_before_multiarch_publish(self) -> None:
        self.assertIn("  smoke:", self.text)
        self.assertIn("docker build \\", self.text)
        self.assertIn("--build-arg VERSION_REF=v0.0.0-ci", self.text)
        self.assertIn('--build-arg GIT_SHA="$SOURCE_SHA"', self.text)
        self.assertIn("--tag mediaflux:smoke", self.text)
        self.assertIn("http://127.0.0.1:1258/readyz", self.text)
        self.assertIn("needs: [test, smoke]", self.text)
        self.assertIn("docker logs mediaflux-smoke", self.text)
        self.assertIn("docker stop --timeout 60 mediaflux-smoke", self.text)
        self.assertIn(".State.ExitCode", self.text)
        self.assertIn("docker rm --force mediaflux-smoke", self.text)

    def test_fresh_smokes_share_web_setup_without_preseeded_admin(self) -> None:
        jobs = yaml.safe_load(self.text)["jobs"]
        step = next(step for step in jobs["smoke"]["steps"]
                    if step.get("name") == "Start image and wait for health check")
        self.assertNotIn("env", step)
        first = step["run"]
        shared = "packaging/scripts/web_setup_smoke.py"
        self.assertEqual(self.text.count("python - < " + shared), 2)
        self.assertLess(first.index("/setup"), first.index(shared))
        self.assertLess(first.index(shared), first.index("/readyz"))
        self.assertEqual(first.count("docker run --detach"), 1)
        self.assertIn("python - < " + shared + " >/dev/null 2>&1", first)
        for field in ("MEDIAFLUX_INITIALIZED", "ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD", "--env"):
            self.assertNotIn(field, first)
        upgrade = next(step for step in jobs["smoke"]["steps"]
                       if step.get("name") == "Smoke test persisted v0.1.4 database upgrade")
        self.assertEqual(upgrade["env"]["MEDIAFLUX_INITIALIZED"], "1")
        self.assertIn("sqlite3.connect", upgrade["run"])
        self.assertNotIn(shared, upgrade["run"])

    def test_amd64_smoke_executes_web_flow_and_redacts_noisy_failure(self) -> None:
        from tests.test_bootstrap_release_contract import BootstrapReleaseContractTests

        harness = BootstrapReleaseContractTests()
        for mode, expected in (("success", 0), ("setup", 37), ("login", 41), ("sandbox", 43)):
            with self.subTest(mode=mode):
                # The harness also checks every generated password is absent from stdout/stderr/argv.
                code, events = harness.run_smoke(mode, step_name="Start image and wait for health check")
                self.assertEqual(code, expected)
                starts = [e for e in events if "--detach" in e["args"]]
                forms = [e for e in events if "requests" in e]
                self.assertEqual(len(starts), 1)
                self.assertIn("seccomp=unconfined", starts[0]["args"])
                self.assertIn("apparmor=unconfined", starts[0]["args"])
                self.assertEqual(len(forms), 1)
                self.assertGreaterEqual(len(forms[0]["settings"]["ENV_WEB_PASSWORD"]), 32)
                if mode == "success":
                    self.assertEqual([(r["method"], r["path"]) for r in forms[0]["requests"]], [
                        ("GET", "/setup"), ("POST", "/setup"), ("GET", "/login"),
                        ("POST", "/login"), ("GET", "/readyz"),
                    ])
                elif mode in {"setup", "login"}:
                    self.assertFalse(any("version" in e["args"] or "doctor" in e["args"] for e in events))
                if mode in {"success", "sandbox"}:
                    probes = [e for e in events if "/app/app/agent/kernel/sandbox_child.py" in e["args"]]
                    self.assertEqual(len(probes), 1)
                    self.assertEqual(probes[0]["args"][-1], "--check")

    def test_graceful_shutdown_accepts_uvicorn_sigterm_and_requires_lifespan_exit(
        self,
    ) -> None:
        self.assertGreaterEqual(self.text.count("0|143) ;;"), 3)
        self.assertGreaterEqual(
            self.text.count('grep -Fq "Application shutdown complete"'),
            3,
        )
        self.assertNotIn(
            'test "$(docker inspect --format \'{{.State.ExitCode}}\' mediaflux-smoke)" = "0"',
            self.text,
        )

    def test_smoke_verifies_embedded_docker_build_metadata(self) -> None:
        self.assertIn("mediaflux.py version --json", self.text)
        self.assertIn('.version == "0.0.0-ci"', self.text)
        self.assertIn('.package == "docker"', self.text)
        self.assertIn(".commit == $sha", self.text)
        self.assertIn("/static/js/app.js", self.text)
        self.assertIn("source_app_js_bytes=$(wc -c < app/static/js/app.js)", self.text)
        self.assertIn("image_app_js_bytes=$(docker exec mediaflux-smoke", self.text)
        self.assertIn(
            'test "$image_app_js_bytes" -lt "$source_app_js_bytes"', self.text
        )
        self.assertIn("! command -v node", self.text)

    def test_smoke_upgrades_a_persisted_v014_database_without_losing_data(self) -> None:
        smoke_job = self.text.split("  smoke:", 1)[1].split("  build:", 1)[0]
        self.assertIn("tests/fixtures/database/v0.1.4-schema.sql", smoke_job)
        self.assertIn("docker_upgrade_sentinel", smoke_job)
        self.assertIn("PRAGMA user_version=1", smoke_job)
        self.assertIn("mediaflux-v014-upgrade", smoke_job)
        self.assertIn("strm_refresh_outbox", smoke_job)
        self.assertIn("legacy_strm_outbox is None", smoke_job)
        self.assertIn("PRAGMA integrity_check", smoke_job)
        self.assertIn("PRAGMA foreign_key_check", smoke_job)
        self.assertIn("docker stop --timeout 60 mediaflux-v014-upgrade", smoke_job)

    def test_full_test_job_installs_runtime_dependencies(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", test_job)
        self.assertIn("--require-hashes -r requirements-release-runtime.lock", test_job)
        self.assertIn('python -m pip install "pytest==9.2.0"', test_job)
        self.assertIn('python -m pip install "ruff==0.17.3"', test_job)
        self.assertIn('version("pytest") == "9.2.0"', test_job)
        self.assertIn('version("ruff") == "0.17.3"', test_job)
        self.assertIn(
            "ruff check app packaging tests --select F,E9",
            test_job,
        )
        self.assertLess(
            test_job.index("npm ci"), test_job.index("python -m pytest tests")
        )
        self.assertLess(
            test_job.index("requirements-release-runtime.lock"),
            test_job.index("python -m pytest tests"),
        )
        self.assertLess(
            test_job.index('version("pytest") == "9.2.0"'),
            test_job.index("python -m pytest tests"),
        )
        self.assertLess(
            test_job.index("ruff check app packaging tests --select F,E9"),
            test_job.index("python -m pytest tests"),
        )

    def test_type_check_gate_uses_ci_python_and_blocks_before_tests(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn('python -m pip install "pyrefly==1.4.0"', test_job)
        self.assertIn('"types-PyYAML==6.0.12.20261030" "types-croniter==6.2.4.20261006"', test_job)
        self.assertIn('version("pyrefly") == "1.4.0"', test_job)
        gate = 'pyrefly check --min-severity=warn --python-interpreter-path "$(command -v python)"'
        self.assertIn(gate, test_job)
        self.assertLess(test_job.index(gate), test_job.index("python -m pytest tests"))
        steps = yaml.safe_load(self.text)["jobs"]["test"]["steps"]
        static_gate = next(step for step in steps if step.get("name") == "Run Python static quality gate")
        self.assertFalse(static_gate.get("continue-on-error", False))
        self.assertNotIn("|| true", static_gate["run"])

    def test_regression_browser_cases_are_in_required_browser_job(self) -> None:
        browser_job = self.text.split("  browser:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn('python -m pip install "playwright==1.64.0"', browser_job)
        modules = browser_job.split("modules = (", 1)[1].split(")", 1)[0]
        for module in (
            "tests.test_node_fixes_agent_feedback",
            "tests.test_settings_review_tooltips_browser",
            "tests.test_metadata_settings_alignment_browser",
        ):
            with self.subTest(module=module):
                self.assertIn(f'"{module}",', modules)
        self.assertIn("unittest.defaultTestLoader.loadTestsFromNames(modules)", browser_job)
        self.assertIn("result.wasSuccessful() and not result.skipped", browser_job)

    def test_runtime_lock_is_generated_with_production_python(self) -> None:
        header = "\n".join(
            Path("requirements-release-runtime.lock")
            .read_text(encoding="utf-8")
            .splitlines()[:8]
        )

        self.assertIn("pip-compile with Python 3.13", header)
        self.assertNotIn("pip-compile with Python 3.12", header)

    def test_full_test_job_has_dependency_free_source_syntax_gate(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn("python -m compileall -q app packaging tests", test_job)
        self.assertIn(
            "find app/static/js -type f -name '*.js' ! -name '*.min.js' -print0",
            test_job,
        )
        self.assertIn('node --check "$file"', test_job)
        self.assertLess(
            test_job.index("compileall"), test_job.index("python -m pytest tests")
        )

    def test_expected_readiness_retries_are_quiet_until_terminal_failure(self) -> None:
        smoke_job = self.text.split("  smoke:", 1)[1].split("  build:", 1)[0]
        for port, path in ((1258, "setup"), (1259, "readyz")):
            with self.subTest(port=port):
                self.assertIn(
                    f"curl --fail --silent http://127.0.0.1:{port}/{path} >/dev/null 2>&1",
                    smoke_job,
                )
        self.assertIn("docker logs mediaflux-smoke", smoke_job)
        self.assertIn("docker logs mediaflux-v014-upgrade", smoke_job)

    def test_full_test_output_is_not_truncated_and_node_is_current(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn('PYTHONUNBUFFERED: "1"', test_job)
        self.assertNotIn("| tail", test_job)
        self.assertIn("actions/setup-node@v6", test_job)
        self.assertIn('node-version: "24.22.2"', test_job)
        self.assertIn("npm install --global npm@12.2.1 --ignore-scripts", test_job)

    def test_candidate_manifest_and_arm64_runtime_are_verified_before_promotion(
        self,
    ) -> None:
        manifest = self.text.index("Verify candidate multi-architecture manifest")
        candidate_smoke = self.text.index("Smoke test candidate images")
        promote = self.text.index("Promote verified image tags")
        self.assertLess(manifest, candidate_smoke)
        self.assertLess(candidate_smoke, promote)
        self.assertIn("steps.build.outputs.digest", self.text)
        self.assertIn('index("linux/amd64")', self.text)
        self.assertIn('index("linux/arm64")', self.text)
        self.assertIn(
            'for platform_and_arch in "linux/amd64:x86_64" "linux/arm64:aarch64"',
            self.text,
        )
        self.assertIn('docker run --rm --platform "$platform"', self.text)
        self.assertIn(
            'output="$RUNNER_TEMP/docker-$safe_platform-version.json"', self.text
        )
        self.assertIn(".arch == $arch", self.text)
        self.assertIn("platform_digest=$(jq -r", self.text)
        self.assertIn("$IMAGE_REPOSITORY@$platform_digest", self.text)
        self.assertIn('container="mediaflux-${safe_platform}-${smoke_dir##*.}"', self.text)
        self.assertIn('volume=$(docker volume create)', self.text)
        self.assertIn('trap cleanup EXIT', self.text)
        self.assertIn('runtime_args=(--platform "$platform" --user 0:0', self.text)
        self.assertIn('docker run --detach "${runtime_args[@]}"', self.text)
        self.assertIn("http://127.0.0.1:1258/readyz", self.text)
        self.assertIn('docker pull --quiet --platform "$platform"', self.text)
        self.assertIn("candidate exited before readiness", self.text)
        self.assertIn(">/dev/null 2>&1; then", self.text)
        self.assertIn('docker stop --timeout 60 "$container"', self.text)
        self.assertNotIn("docker stop --time ", self.text)
        candidate_smoke = self.text.split(
            "      - name: Smoke test candidate images", 1
        )[1].split("      - name: Re-verify release tag before promotion", 1)[0]
        self.assertNotIn("$IMAGE_REPOSITORY@$IMAGE_DIGEST", candidate_smoke)
        self.assertIn("http://127.0.0.1:1258/setup", candidate_smoke)
        self.assertIn('docker exec -i "$container" python -', candidate_smoke)
        self.assertIn('response.code == 303 and response.headers["Location"] == "/login"', self.web_smoke)
        self.assertIn('with request("/login",', self.web_smoke)
        self.assertIn('with request("/readyz")', self.web_smoke)
        self.assertEqual(candidate_smoke.count('docker run --detach "${runtime_args[@]}"'), 1)
        for retired in ("mediaflux.py initialize", "--settings-fd", "--expect-env-sha256", "--grant-file"):
            self.assertNotIn(retired, candidate_smoke)
        self.assertNotIn('docker restart', candidate_smoke)


    def test_registry_existence_checks_fail_closed_before_promotion(self) -> None:
        promote = self.text.split("      - name: Promote verified image tags", 1)[1]
        self.assertGreaterEqual(
            promote.count("packaging/scripts/inspect_registry_digest.sh"),
            2,
        )
        self.assertIn("Unable to determine whether immutable tag", promote)
        self.assertIn("Unable to determine whether mutable tag", promote)

        script = Path("packaging/scripts/inspect_registry_digest.sh").resolve()
        reference = "ghcr.io/example/mediaflux:v0.1.9"
        with tempfile.TemporaryDirectory() as directory:
            fake_docker = Path(directory) / "docker"
            fake_docker.write_text(
                """#!/bin/sh
case "$FAKE_REGISTRY_RESULT" in
  exists)
    printf 'Name: %s\nDigest: sha256:abc123\n' "$4"
    exit 0
    ;;
  missing)
    printf 'ERROR: %s: not found\n' "$4" >&2
    exit 1
    ;;
  unauthorized)
    printf 'ERROR: unauthorized: authentication required\n' >&2
    exit 1
    ;;
  credentials-missing)
    printf 'ERROR: %s: credentials file not found\n' "$4" >&2
    exit 1
    ;;
  no-digest)
    printf 'Name: %s\n' "$4"
    exit 0
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            base_env = {
                **os.environ,
                "PATH": f"{directory}:{os.environ.get('PATH', '')}",
                "MEDIAFLUX_REGISTRY_INSPECT_ATTEMPTS": "1",
                "MEDIAFLUX_REGISTRY_INSPECT_DELAY_SECONDS": "0",
            }

            def inspect(result: str):
                return subprocess.run(
                    ["bash", str(script), reference],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={**base_env, "FAKE_REGISTRY_RESULT": result},
                )

            existing = inspect("exists")
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertEqual(existing.stdout.strip(), "sha256:abc123")
            self.assertEqual(inspect("missing").returncode, 3)
            self.assertEqual(inspect("unauthorized").returncode, 1)
            self.assertEqual(inspect("credentials-missing").returncode, 1)
            self.assertEqual(inspect("no-digest").returncode, 1)

    def test_version_tag_is_immutable_and_mutable_tags_cannot_regress(self) -> None:
        promote = self.text.split("      - name: Promote verified image tags", 1)[1]
        self.assertIn('exact="$IMAGE_REPOSITORY:$DOCKER_VERSION"', promote)
        self.assertIn("Immutable version tag", promote)
        self.assertIn('existing_digest" != "$IMAGE_DIGEST', promote)
        self.assertIn('if [[ "$STABLE" == "true" ]]', promote)
        self.assertIn("check_version_promotion.py", promote)
        self.assertIn(
            'for mutable_tag in "$IMAGE_REPOSITORY:$SERIES" "$IMAGE_REPOSITORY:latest"',
            promote,
        )
        self.assertIn('tags+=("$mutable_tag")', promote)
        self.assertIn("Keep $mutable_tag on newer version", promote)
        self.assertIn("imagetools create --tag", promote)
        self.assertIn('test "$promoted_digest" = "$promotion_digest"', promote)
        self.assertIn("Reuse previously verified immutable tag", promote)
        self.assertIn("belongs to a different commit; refusing overwrite", promote)
        self.assertIn("exists but cannot be verified; refusing overwrite", promote)
        self.assertGreaterEqual(promote.count("exit 1"), 2)
        self.assertIn("existing-exact-manifest.json", promote)
        self.assertIn('"$IMAGE_REPOSITORY@$existing_digest" --raw', promote)
        self.assertNotIn('imagetools inspect "$exact" --raw', promote)
        self.assertIn("platform_digest=$(jq -r", promote)
        self.assertIn("$IMAGE_REPOSITORY@$platform_digest", promote)
        self.assertNotIn('"$exact" mediaflux.py version --json', promote)
        self.assertIn(
            '"$IMAGE_REPOSITORY@$mutable_digest" mediaflux.py version --json',
            promote,
        )
        self.assertNotIn('"$mutable_tag" mediaflux.py version --json', promote)
        self.assertIn('"linux/amd64:x86_64"', promote)
        self.assertIn('"linux/arm64:aarch64"', promote)
        self.assertIn('--arg arch "$expected_arch"', promote)
        self.assertIn(".arch == $arch", promote)

    def test_all_workflow_shell_blocks_are_syntactically_valid(self) -> None:
        workflow = yaml.safe_load(self.text)
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                script = step.get("run")
                if not script:
                    continue
                result = subprocess.run(
                    ["bash", "-n"],
                    input=script,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{job_name}/{step.get('name', 'unnamed')}: {result.stderr}",
                )

    def test_release_build_uses_shared_build_info_contract(self) -> None:
        self.assertIn("packaging/scripts/generate_build_info.py", self.text)
        self.assertIn("packaging/scripts/docker_version_tag.py", self.text)
        context = self.text.split("      - name: Prepare build context", 1)[1].split(
            "      - uses: docker/setup-qemu-action@v3", 1
        )[0]
        epoch = 'SOURCE_DATE_EPOCH=$(git show -s --format=%ct "$BUILD_SHA")'
        generator = "python packaging/scripts/generate_build_info.py"
        self.assertLess(context.index(epoch), context.index(generator))
        self.assertIn('--ref "$VERSION_REF" --commit "$BUILD_SHA"', context)
        self.assertIn("--platform linux --arch multi --package docker", context)
        self.assertIn('--build-time "$SOURCE_DATE_EPOCH"', context)
        self.assertNotIn("--package runtime", context)
        self.assertIn("VERSION_REF=${{ steps.context.outputs.version_ref }}", self.text)
        self.assertIn("GIT_SHA=${{ steps.context.outputs.source_sha }}", self.text)
        self.assertIn(
            "SOURCE_DATE_EPOCH=${{ steps.context.outputs.source_date_epoch }}",
            self.text,
        )
        self.assertIn(
            "ARG SOURCE_DATE_EPOCH", Path("Dockerfile").read_text(encoding="utf-8")
        )

    def test_release_draft_is_verified_before_promotion_and_published_last(
        self,
    ) -> None:
        workflow = yaml.safe_load(self.text)
        steps = workflow["jobs"]["build"]["steps"]
        names = [step.get("name", "") for step in steps]
        prepare = names.index("Prepare GitHub Release")
        reverify = names.index("Re-verify release tag before promotion")
        promote = names.index("Promote verified image tags")
        publish = names.index("Publish GitHub Release")
        self.assertLess(names.index("Smoke test candidate images"), prepare)
        self.assertLess(prepare, reverify)
        self.assertLess(reverify, promote)
        self.assertLess(promote, publish)
        self.assertEqual(steps[prepare]["id"], "release")
        draft_condition = (
            "startsWith(github.ref, 'refs/tags/v') && "
            "steps.release.outputs.ready_draft == 'true'"
        )
        self.assertEqual(steps[promote]["if"], draft_condition)
        self.assertEqual(steps[publish]["if"], draft_condition)
        self.assertIn(
            '--draft --verify-tag --target "$EXPECTED_SHA"', steps[prepare]["run"]
        )
        self.assertIn(
            'gh release edit "$VERSION_REF" --draft=false', steps[publish]["run"]
        )
        self.assertNotIn("gh release upload", steps[publish]["run"])
        for stage in (prepare, publish):
            self.assertIn(
                'git merge-base --is-ancestor "$EXPECTED_SHA" origin/main',
                steps[stage]["run"],
            )
            self.assertIn("verify-release-assets.py", steps[stage]["run"])

    def test_existing_release_assets_are_only_replaced_for_same_commit(self) -> None:
        prepare = self.text.split("      - name: Prepare GitHub Release", 1)[1].split(
            "      - name: Re-verify release tag before promotion", 1
        )[0]
        self.assertIn("--json isDraft,body,tagName,targetCommitish,assets", prepare)
        self.assertIn(
            "mediaflux-release:v1 version=$VERSION_REF commit=$EXPECTED_SHA", prepare
        )
        self.assertIn('gh release download "$VERSION_REF"', prepare)
        self.assertIn("--pattern BUILD-INFO.json", prepare)
        self.assertIn("refusing asset overwrite", prepare)
        self.assertIn("has no verifiable BUILD-INFO.json; refusing overwrite", prepare)
        self.assertIn("ready_draft=false", prepare)
        self.assertIn("Release asset SHA-256 mismatch", prepare)

    def test_release_assets_reuse_commit_epoch_for_annotated_tags(self) -> None:
        prepare = self.text.split("      - name: Prepare GitHub Release", 1)[1].split(
            "      - name: Re-verify release tag before promotion", 1
        )[0]
        self.assertIn(
            "SOURCE_DATE_EPOCH: ${{ steps.context.outputs.source_date_epoch }}",
            prepare,
        )
        self.assertNotIn('git show -s --format=%ct "$VERSION_REF"', prepare)


if __name__ == "__main__":
    unittest.main()
