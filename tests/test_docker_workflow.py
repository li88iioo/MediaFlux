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

    def test_image_is_smoke_tested_before_multiarch_publish(self) -> None:
        self.assertIn("  smoke:", self.text)
        self.assertIn("docker build \\", self.text)
        self.assertIn("--build-arg VERSION_REF=v0.0.0-ci", self.text)
        self.assertIn('--build-arg GIT_SHA="$SOURCE_SHA"', self.text)
        self.assertIn("--tag mediaflux:smoke", self.text)
        self.assertIn("http://127.0.0.1:1258/readyz", self.text)
        self.assertIn("needs: [test, smoke]", self.text)
        self.assertIn("docker logs mediaflux-smoke", self.text)
        self.assertIn("docker stop --time 60 mediaflux-smoke", self.text)
        self.assertIn(".State.ExitCode", self.text)
        self.assertIn("docker rm --force mediaflux-smoke", self.text)

    def test_smoke_verifies_embedded_docker_build_metadata(self) -> None:
        self.assertIn("mediaflux.py version --json", self.text)
        self.assertIn('.version == "0.0.0-ci"', self.text)
        self.assertIn('.package == "docker"', self.text)
        self.assertIn(".commit == $sha", self.text)
        self.assertIn("/static/js/app.js", self.text)
        self.assertIn("source_app_js_bytes=$(wc -c < app/static/js/app.js)", self.text)
        self.assertIn("image_app_js_bytes=$(docker exec mediaflux-smoke", self.text)
        self.assertIn('test "$image_app_js_bytes" -lt "$source_app_js_bytes"', self.text)
        self.assertIn("! command -v node", self.text)

    def test_smoke_upgrades_a_persisted_v014_database_without_losing_data(self) -> None:
        smoke_job = self.text.split("  smoke:", 1)[1].split("  build:", 1)[0]
        self.assertIn("tests/fixtures/database/v0.1.4-schema.sql", smoke_job)
        self.assertIn("docker_upgrade_sentinel", smoke_job)
        self.assertIn("PRAGMA user_version=1", smoke_job)
        self.assertIn("mediaflux-v014-upgrade", smoke_job)
        self.assertIn("strm_metadata_refresh_outbox", smoke_job)
        self.assertIn("PRAGMA integrity_check", smoke_job)
        self.assertIn("PRAGMA foreign_key_check", smoke_job)
        self.assertIn("docker stop --time 60 mediaflux-v014-upgrade", smoke_job)

    def test_full_test_job_installs_runtime_dependencies(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", test_job)
        self.assertIn("--require-hashes -r requirements-release-runtime.lock", test_job)
        self.assertLess(test_job.index("npm ci"), test_job.index("unittest discover"))
        self.assertLess(
            test_job.index("requirements-release-runtime.lock"),
            test_job.index("unittest discover"),
        )

    def test_full_test_output_is_not_truncated_and_node_is_current(self) -> None:
        test_job = self.text.split("  test:", 1)[1].split("  smoke:", 1)[0]
        self.assertIn('PYTHONUNBUFFERED: "1"', test_job)
        self.assertNotIn("| tail", test_job)
        self.assertIn("actions/setup-node@v6", test_job)
        self.assertIn('node-version: "24"', test_job)

    def test_candidate_manifest_and_arm64_runtime_are_verified_before_promotion(self) -> None:
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
        self.assertIn('output="$RUNNER_TEMP/docker-$safe_platform-version.json"', self.text)
        self.assertIn('.arch == $arch', self.text)
        self.assertIn('platform_digest=$(jq -r', self.text)
        self.assertIn('$IMAGE_REPOSITORY@$platform_digest', self.text)
        self.assertIn('container="mediaflux-${safe_platform}-startup"', self.text)
        self.assertIn('docker run --detach --platform "$platform"', self.text)
        self.assertIn("http://127.0.0.1:1258/readyz", self.text)
        self.assertIn('docker stop --time 60 "$container"', self.text)
        candidate_smoke = self.text.split("      - name: Smoke test candidate images", 1)[1].split(
            "      - name: Re-verify release tag before promotion", 1
        )[0]
        self.assertNotIn('$IMAGE_REPOSITORY@$IMAGE_DIGEST', candidate_smoke)

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
        self.assertIn('for mutable_tag in "$IMAGE_REPOSITORY:$SERIES" "$IMAGE_REPOSITORY:latest"', promote)
        self.assertIn('tags+=("$mutable_tag")', promote)
        self.assertIn("Keep $mutable_tag on newer version", promote)
        self.assertIn("imagetools create --tag", promote)
        self.assertIn('test "$promoted_digest" = "$promotion_digest"', promote)
        self.assertIn("Reuse previously verified immutable tag", promote)
        self.assertIn("belongs to a different commit; refusing overwrite", promote)
        self.assertIn("exists but cannot be verified; refusing overwrite", promote)
        self.assertGreaterEqual(promote.count("exit 1"), 2)
        self.assertIn("existing-exact-manifest.json", promote)
        self.assertIn('platform_digest=$(jq -r', promote)
        self.assertIn('$IMAGE_REPOSITORY@$platform_digest', promote)
        self.assertNotIn('"$exact" mediaflux.py version --json', promote)
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
        self.assertIn('--ref "$VERSION_REF" --commit "$BUILD_SHA"', self.text)
        self.assertIn("VERSION_REF=${{ steps.context.outputs.version_ref }}", self.text)
        self.assertIn("GIT_SHA=${{ steps.context.outputs.source_sha }}", self.text)
        self.assertIn("SOURCE_DATE_EPOCH=${{ steps.context.outputs.source_date_epoch }}", self.text)
        self.assertIn("ARG SOURCE_DATE_EPOCH", Path("Dockerfile").read_text(encoding="utf-8"))

    def test_existing_release_assets_are_only_replaced_for_same_commit(self) -> None:
        publish = self.text.split("      - name: Publish GitHub Release", 1)[1]
        self.assertIn('gh release download "$VERSION_REF"', publish)
        self.assertIn("--pattern BUILD-INFO.json", publish)
        self.assertIn("EXISTING_RELEASE_SHA=$(jq -r '.commit // empty'", publish)
        self.assertIn('"$EXISTING_RELEASE_SHA" != "$EXPECTED_SHA"', publish)
        self.assertIn("refusing asset overwrite", publish)
        self.assertIn("has no verifiable BUILD-INFO.json; refusing overwrite", publish)

    def test_release_assets_reuse_commit_epoch_for_annotated_tags(self) -> None:
        publish = self.text.split("      - name: Publish GitHub Release", 1)[1]
        self.assertIn(
            "SOURCE_DATE_EPOCH: ${{ steps.context.outputs.source_date_epoch }}",
            publish,
        )
        self.assertNotIn('git show -s --format=%ct "$VERSION_REF"', publish)


if __name__ == "__main__":
    unittest.main()
