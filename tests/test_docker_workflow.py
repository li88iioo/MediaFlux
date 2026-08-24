from __future__ import annotations

import subprocess
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
        candidate_smoke = self.text.split("      - name: Smoke test candidate images", 1)[1].split(
            "      - name: Re-verify release tag before promotion", 1
        )[0]
        self.assertNotIn('$IMAGE_REPOSITORY@$IMAGE_DIGEST', candidate_smoke)

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
        self.assertIn("existing-exact-manifest.json", promote)
        self.assertIn('platform_digest=$(jq -r', promote)
        self.assertIn('$IMAGE_REPOSITORY@$platform_digest', promote)
        self.assertNotIn('"$exact" mediaflux.py version --json', promote)
        self.assertIn('"linux/amd64:x86_64"', promote)
        self.assertIn('"linux/arm64:aarch64"', promote)
        self.assertIn('--arg arch "$expected_arch"', promote)
        self.assertIn(".arch == $arch", promote)

    def test_prepare_build_context_shell_is_syntactically_valid(self) -> None:
        workflow = yaml.safe_load(self.text)
        prepare_script = next(
            step["run"]
            for step in workflow["jobs"]["build"]["steps"]
            if step.get("name") == "Prepare build context"
        )
        result = subprocess.run(
            ["bash", "-n"],
            input=prepare_script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_build_uses_shared_build_info_contract(self) -> None:
        self.assertIn("packaging/scripts/generate_build_info.py", self.text)
        self.assertIn("packaging/scripts/docker_version_tag.py", self.text)
        self.assertIn('--ref "$VERSION_REF" --commit "$BUILD_SHA"', self.text)
        self.assertIn("VERSION_REF=${{ steps.context.outputs.version_ref }}", self.text)
        self.assertIn("GIT_SHA=${{ steps.context.outputs.source_sha }}", self.text)
        self.assertIn("SOURCE_DATE_EPOCH=${{ steps.context.outputs.source_date_epoch }}", self.text)
        self.assertIn("ARG SOURCE_DATE_EPOCH", Path("Dockerfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
