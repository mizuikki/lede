"""Regression tests for the Passwall profile's SSR Plus+ exclusion."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "verify_fork_contract", ROOT / "scripts/verify-fork-contract.py",
)
CONTRACT_MODULE = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(CONTRACT_MODULE)
PROFILE_PATH = "profiles/x86_64-passwall-docker.diffconfig"
DISABLED = (
    "CONFIG_PACKAGE_luci-app-ssr-plus",
    "CONFIG_PACKAGE_luci-i18n-ssr-plus-zh-cn",
)


class PasswallSsrContractTests(unittest.TestCase):
    def setUp(self):
        self.profile = (ROOT / PROFILE_PATH).read_text()

    def check_profile(self, config_text: str, profile_text: str | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix="lede-passwall-ssr-contract-") as temporary:
            config = Path(temporary) / ".config"
            config.write_text(config_text)
            checker = CONTRACT_MODULE.Contract(ROOT, config, "source")
            if profile_text is not None:
                original = checker.path_text
                with mock.patch.object(
                    checker, "path_text",
                    side_effect=lambda relative, **kwargs: (
                        profile_text if relative == PROFILE_PATH and not kwargs.get("trusted")
                        else original(relative, **kwargs)
                    ),
                ):
                    checker.check_profile()
            else:
                checker.check_profile()
            return checker.checks["x86_profile"]

    def test_default_selection_is_present_but_profile_explicitly_disables_both_ui_packages(self):
        defaults = (ROOT / "include/target.mk").read_text()
        self.assertIn("luci-app-ssr-plus", defaults)
        for symbol in DISABLED:
            self.assertEqual(
                CONTRACT_MODULE.Contract.parse_config_text(self.profile).get(symbol), "n",
                f"profile must explicitly turn off {symbol}",
            )

    def test_application_or_language_package_reenabled_as_y_or_m_fails(self):
        for symbol in DISABLED:
            off_line = f"# {symbol} is not set"
            for value in ("y", "m"):
                with self.subTest(symbol=symbol, value=value):
                    config = self.profile.replace(off_line, f"{symbol}={value}")
                    result = self.check_profile(config)
                    self.assertEqual(result["result"], "failure", result["detail"])

                    candidate_profile = self.profile.replace(off_line, f"{symbol}={value}")
                    result = self.check_profile(self.profile, candidate_profile)
                    self.assertEqual(result["result"], "failure", result["detail"])

    def test_missing_language_symbol_is_accepted_when_kconfig_drops_its_dependency(self):
        language = "CONFIG_PACKAGE_luci-i18n-ssr-plus-zh-cn"
        generated_config = self.profile.replace(f"# {language} is not set\n", "")
        result = self.check_profile(generated_config)
        self.assertEqual(result["result"], "success", result["detail"])

    def test_missing_passwall_or_required_backend_still_fails(self):
        for symbol in ("CONFIG_PACKAGE_luci-app-passwall", "CONFIG_PACKAGE_xray-core"):
            with self.subTest(symbol=symbol):
                generated_config = self.profile.replace(f"{symbol}=y\n", "")
                result = self.check_profile(generated_config)
                self.assertEqual(result["result"], "failure", result["detail"])
                self.assertIn(symbol, result["detail"])


if __name__ == "__main__":
    unittest.main()
