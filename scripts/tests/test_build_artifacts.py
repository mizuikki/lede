"""Tests for firmware manifests and uploaded IPK package inventory checks."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_build_artifacts", ROOT / "scripts/check-build-artifacts.py",
)
CHECKER = importlib.util.module_from_spec(CHECKER_SPEC)
CHECKER_SPEC.loader.exec_module(CHECKER)
POLICY = yaml.safe_load((ROOT / ".github/fork-contract.yml").read_text())[
    "fork_features"]["artifact_validation"]


class BuildArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lede-artifact-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.required = POLICY["required_packages"]

    def make_build_tree(self, extra_manifest=(), extra_ipks=()):
        build = self.root / "bin"
        manifest = build / "targets/x86/64/openwrt-test.manifest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("".join(f"{name} - 1\n" for name in (*self.required, *extra_manifest)))
        package_index = build / "targets/x86/64/packages/Packages.manifest"
        package_index.parent.mkdir(parents=True, exist_ok=True)
        package_index.write_text("Package: luci-app-ssr-plus\n")
        packages = build / "packages/x86_64/test"
        packages.mkdir(parents=True, exist_ok=True)
        for name in (*self.required, *extra_ipks):
            (packages / f"{name}_1_all.ipk").touch()
        return build

    def make_release_assets(self, extra_manifest=(), extra_ipks=(), label="release-assets"):
        assets = self.root / label
        assets.mkdir()
        (assets / "package-manifests.txt").write_text(
            "".join(f"{name} - 1\n" for name in (*self.required, *extra_manifest))
        )
        archive = assets / "openwrt-test-packages-20261014.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name in (*self.required, *extra_ipks):
                member = tarfile.TarInfo(f"packages/x86_64/test/{name}_1_all.ipk")
                member.size = 0
                bundle.addfile(member, io.BytesIO(b""))
        return assets

    def test_normal_build_manifest_and_ipk_collection_pass(self):
        build = self.make_build_tree()
        result = CHECKER.validate_build_root(build, CHECKER.load_policy(ROOT / ".github/fork-contract.yml"))
        self.assertTrue(result["success"], result["errors"])

    def test_firmware_manifest_with_disabled_application_or_language_package_fails(self):
        for name in ("luci-app-ssr-plus", "luci-i18n-ssr-plus-ja"):
            with self.subTest(package=name):
                build = self.make_build_tree(extra_manifest=(name,))
                result = CHECKER.validate_build_root(build, CHECKER.load_policy(ROOT / ".github/fork-contract.yml"))
                self.assertFalse(result["success"])
                self.assertTrue(any("openwrt-test.manifest" in error and name in error
                                    for error in result["errors"]))

    def test_package_ipk_set_with_disabled_application_or_language_package_fails(self):
        for name in ("luci-app-ssr-plus", "luci-i18n-ssr-plus-ja"):
            with self.subTest(package=name):
                build = self.make_build_tree(extra_ipks=(name,))
                result = CHECKER.validate_build_root(build, CHECKER.load_policy(ROOT / ".github/fork-contract.yml"))
                self.assertFalse(result["success"])
                self.assertTrue(any("forbidden IPK" in error and name in error
                                    for error in result["errors"]))

    def test_missing_passwall_or_required_backend_fails_manifest_and_ipk_validation(self):
        for package in ("luci-app-passwall", "xray-core"):
            with self.subTest(package=package):
                build = self.make_build_tree()
                manifest = next((build / "targets").rglob("*.manifest"))
                manifest.write_text("\n".join(
                    line for line in manifest.read_text().splitlines()
                    if not line.startswith(package + " - ")
                ) + "\n")
                for path in build.rglob(f"{package}_*.ipk"):
                    path.unlink()
                result = CHECKER.validate_build_root(build, CHECKER.load_policy(ROOT / ".github/fork-contract.yml"))
                self.assertFalse(result["success"])
                self.assertTrue(any(package in error for error in result["errors"]))

    def test_normal_release_assets_pass_and_disabled_manifest_or_ipk_fails(self):
        policy = CHECKER.load_policy(ROOT / ".github/fork-contract.yml")
        result = CHECKER.validate_release_assets(self.make_release_assets(), policy)
        self.assertTrue(result["success"], result["errors"])

        assets_with_forbidden_manifest = self.make_release_assets(
            extra_manifest=("luci-app-ssr-plus",), label="release-manifest-forbidden",
        )
        result = CHECKER.validate_release_assets(assets_with_forbidden_manifest, policy)
        self.assertFalse(result["success"])

        assets_with_forbidden_ipk = self.make_release_assets(
            extra_ipks=("luci-i18n-ssr-plus-ja",), label="release-ipk-forbidden",
        )
        result = CHECKER.validate_release_assets(assets_with_forbidden_ipk, policy)
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
