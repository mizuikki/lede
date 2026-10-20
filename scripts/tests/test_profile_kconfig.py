"""Integration regression against OpenWrt's real Kconfig expansion.

Set OPENWRT_KCONFIG_ROOT to an initialized OpenWrt checkout with locked feeds
installed to include this test in unittest discovery.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = Path(os.environ["OPENWRT_KCONFIG_ROOT"]).resolve() if os.environ.get(
    "OPENWRT_KCONFIG_ROOT") else None


@unittest.skipUnless(BUILD_ROOT, "set OPENWRT_KCONFIG_ROOT for real Kconfig integration")
class ProfileKconfigTests(unittest.TestCase):
    def test_profile_preserves_docker_storage_and_passwall_backends(self):
        build_root = BUILD_ROOT
        profile_path = build_root / "profiles/x86_64-passwall-docker.diffconfig"
        profile_path.write_text((ROOT / "profiles/x86_64-passwall-docker.diffconfig").read_text())
        (build_root / ".config").write_text(profile_path.read_text())
        for command in (["make", "prepare-tmpinfo"], ["make", "defconfig"]):
            result = subprocess.run(command, cwd=build_root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        values = {}
        for line in (build_root / ".config").read_text(errors="replace").splitlines():
            if line.startswith("# ") and line.endswith(" is not set"):
                values[line[2:-11]] = "n"
            elif line.startswith("CONFIG_") and "=" in line:
                name, value = line.split("=", 1)
                values[name] = value

        self.assertEqual(values.get("CONFIG_TARGET_ROOTFS_EXT4FS"), "y")
        self.assertEqual(values.get("CONFIG_TARGET_ROOTFS_SQUASHFS"), "n")
        self.assertEqual(values.get("CONFIG_TARGET_ROOTFS_PARTSIZE"), "4096")
        self.assertEqual(values.get("CONFIG_DEFAULT_luci-app-ssr-plus"), "y")
        self.assertNotIn(values.get("CONFIG_PACKAGE_luci-app-ssr-plus"), ("y", "m"))
        self.assertNotIn(values.get("CONFIG_PACKAGE_luci-i18n-ssr-plus-zh-cn"), ("y", "m"))
        for package in (
            "luci-app-passwall", "luci-i18n-passwall-zh-cn", "shadowsocksr-libev-ssr-local",
            "sing-box", "xray-core",
        ):
            self.assertEqual(values.get(f"CONFIG_PACKAGE_{package}"), "y", package)


if __name__ == "__main__":
    unittest.main()
