from __future__ import annotations
import importlib.util, json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from app.version import BuildInfo

P=Path('packaging/scripts/generate_build_info.py'); S=importlib.util.spec_from_file_location('build_info_generator',P); assert S and S.loader; M=importlib.util.module_from_spec(S); import sys; sys.modules[S.name]=M; S.loader.exec_module(M)

class BuildInfoTests(unittest.TestCase):
    def test_tag_version_controls_artifact_names(self):
        i=M.generate_build_info('v1.2.3','abcdef12','docker','x86_64','docker',build_time='2026-07-29T00:00:00Z')
        self.assertEqual(i.version,'1.2.3'); self.assertEqual(i.artifact_name,'MediaFlux-1.2.3-docker-x86_64'); self.assertFalse(i.prerelease)
        pre=M.generate_build_info('v1.2.3-beta.1','abcdef12','docker','aarch64','docker',build_time='2026-07-29T00:00:00Z')
        self.assertTrue(pre.prerelease); self.assertEqual(pre.artifact_name,'MediaFlux-1.2.3-beta.1-docker-aarch64')
    def test_all_release_artifact_names_share_the_central_contract(self):
        expected = {
            ("docker", "amd64", "docker"): "MediaFlux-1.2.3-docker-x86_64",
            ("docker", "arm64", "docker"): "MediaFlux-1.2.3-docker-aarch64",
            ("linux", "x86_64", "runtime"): "MediaFlux-runtime-1.2.3-linux-x86_64.tar.gz",
            ("linux", "aarch64", "runtime"): "MediaFlux-runtime-1.2.3-linux-aarch64.tar.gz",
            ("linux", "all", "source"): "MediaFlux-1.2.3-source.tar.gz",
        }
        for (platform_name, arch, package), name in expected.items():
            with self.subTest(platform=platform_name, arch=arch, package=package):
                self.assertEqual(M.artifact_name("1.2.3", platform_name, arch, package), name)

    def test_invalid_release_versions_are_rejected(self):
        for ref in ('', 'latest', '1.2', 'v1.0.0-01', 'v1.0.0-alpha.01'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                M.normalize_version(ref)

    def test_prerelease_classification_ignores_hyphens_in_build_metadata(self):
        stable = M.generate_build_info(
            'v1.2.3+build-foo', 'abcdef12', 'docker', 'multi', 'runtime',
            build_time='2026-07-29T00:00:00Z',
        )
        self.assertFalse(stable.prerelease)
        self.assertTrue(M.is_prerelease('v1.2.3-rc.1+build-foo'))

    def test_docker_version_tag_mapping_and_length_boundary(self):
        self.assertEqual(M.docker_version_tag('v1.2.3'), '1.2.3')
        self.assertEqual(M.docker_version_tag('v1.2.3+build.7'), '1.2.3_build.7')
        self.assertEqual(len(M.docker_version_tag('1.2.3+' + 'a' * 122)), 128)
        with self.assertRaises(ValueError):
            M.docker_version_tag('1.2.3+' + 'a' * 123)
    def test_semver_comparison_ignores_build_metadata_and_orders_prereleases(self):
        ordered = (
            '1.0.0-alpha',
            '1.0.0-alpha.1',
            '1.0.0-alpha.beta',
            '1.0.0-beta',
            '1.0.0-beta.2',
            '1.0.0-beta.11',
            '1.0.0-rc.1',
            '1.0.0',
            '1.1.0',
            '2.0.0',
        )
        for older, newer in zip(ordered, ordered[1:]):
            with self.subTest(older=older, newer=newer):
                self.assertLess(M.compare_versions(older, newer), 0)
                self.assertGreater(M.compare_versions(newer, older), 0)
        self.assertEqual(M.compare_versions('v1.2.3+build.1', '1.2.3+build.9'), 0)
        accepted = ('1.0.0-alpha', '1.0.0-beta.2', '1.0.0-rc.1', '1.0.0', '2.0.0')
        for left in accepted:
            for right in accepted:
                with self.subTest(left=left, right=right):
                    self.assertEqual(
                        M.compare_versions(left, right),
                        -M.compare_versions(right, left),
                    )

    def test_runtime_reads_explicit_build_info_and_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'BUILD-INFO.json'; p.write_text(json.dumps({'version':'9.8.7','commit':'abc','build_time':'now','package':'docker'}))
            old_val = os.environ.get('MEDIAFLUX_BUILD_INFO_FILE')
            try:
                os.environ['MEDIAFLUX_BUILD_INFO_FILE'] = str(p)
                info=BuildInfo.current()
            finally:
                if old_val is None:
                    os.environ.pop('MEDIAFLUX_BUILD_INFO_FILE', None)
                else:
                    os.environ['MEDIAFLUX_BUILD_INFO_FILE'] = old_val
            self.assertEqual((info.version,info.package),('9.8.7','docker'))
            self.assertTrue(info.arch)
        old_val = os.environ.get('MEDIAFLUX_BUILD_INFO_FILE')
        try:
            os.environ['MEDIAFLUX_BUILD_INFO_FILE'] = '/missing'
            self.assertTrue(BuildInfo.current().version)
        finally:
            if old_val is None:
                os.environ.pop('MEDIAFLUX_BUILD_INFO_FILE', None)
            else:
                os.environ['MEDIAFLUX_BUILD_INFO_FILE'] = old_val

if __name__=='__main__': unittest.main()
