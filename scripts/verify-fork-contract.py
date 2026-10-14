#!/usr/bin/env python3
"""Check that a source tree or build configuration preserves fork behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised on minimal systems
    raise SystemExit("PyYAML is required to read .github/fork-contract.yml") from exc


ROOT = Path(__file__).resolve().parents[1]
CONFIG_RE = re.compile(r"^(?:# )?([A-Za-z0-9_-]+)(?:=(.*)| is not set)$")


class Contract:
    def __init__(self, root: Path, config_path: Path | None, mode: str,
                 source_ref: str | None = None, trusted_ref: str | None = None):
        self.root = root.resolve()
        self.config_path = config_path
        self.mode = mode
        self.source_ref = source_ref
        self.trusted_ref = trusted_ref or source_ref
        self.checks: dict[str, dict] = {}
        self.errors: list[str] = []
        contract_path = self.root / ".github/fork-contract.yml"
        contract_text = self.git_text(self.trusted_ref, ".github/fork-contract.yml") if self.trusted_ref else contract_path.read_text()
        self.contract = yaml.safe_load(contract_text)
        if self.contract.get("schema_version") != 1:
            raise ValueError(f"unsupported contract schema in {contract_path}")
        self.features = self.contract["fork_features"]

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = {"result": "success" if ok else "failure", "detail": detail}
        if not ok:
            self.errors.append(f"{name}: {detail}")

    def git_text(self, revision: str, relative: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{revision}:{relative}"],
            capture_output=True,
        )
        if result.returncode:
            raise FileNotFoundError(f"{relative} at {revision[:12]}")
        return result.stdout.decode(errors="replace")

    def source_path_exists(self, relative: str) -> bool:
        if self.source_ref:
            result = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "-e", f"{self.source_ref}:{relative}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
        return (self.root / relative).exists()

    def path_text(self, relative: str, *, trusted: bool = False) -> str:
        revision = self.trusted_ref if trusted else self.source_ref
        if revision:
            return self.git_text(revision, relative)
        path = self.root / relative
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path.read_text(errors="replace")

    def check_passwall(self) -> None:
        spec = self.features["passwall_provider"]
        lines = [line.split("#", 1)[0].strip() for line in self.path_text(spec["feeds_file"]).splitlines()]
        active = [line for line in lines if line.startswith("src-git ")]
        names = [line.split()[1] for line in active]
        order_ok = all(name in names for name in ("passwall_packages", "passwall_luci", "packages", "helloworld"))
        if order_ok:
            order_ok = max(names.index("passwall_packages"), names.index("passwall_luci")) < min(
                names.index("packages"), names.index("helloworld")
            )
        luci = next((line for line in active if line.split()[1] == "luci"), "")
        branch_ok = luci.endswith(";" + spec["luci_branch"])
        self.record("passwall_feed_order", order_ok, "active provider order=" + ",".join(names))
        self.record("luci_feed_branch", branch_ok, f"expected luci tracking branch {spec['luci_branch']}")
        if self.mode == "build":
            provider_errors = []
            for package, provider in spec["packages_provider"].items():
                link = self.root / "package/feeds" / provider / package
                target = link.resolve(strict=False)
                expected = (self.root / "feeds" / provider / package).resolve(strict=False)
                if not link.is_symlink() or target != expected or not target.exists():
                    provider_errors.append(f"{package} is not installed from {provider}")
            self.record("installed_passwall_providers", not provider_errors, "; ".join(provider_errors))

    @staticmethod
    def parse_config_text(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in text.splitlines():
            match = CONFIG_RE.match(line)
            if not match:
                continue
            key, value = match.groups()
            result[key] = value if value is not None else "n"
        return result

    @staticmethod
    def parse_config(path: Path) -> dict[str, str]:
        return Contract.parse_config_text(path.read_text(errors="replace"))

    def check_profile(self) -> None:
        spec = self.features["x86_profile"]
        profile = self.parse_config_text(self.path_text(spec["path"]))
        missing_or_changed = []
        required_failures = []
        required_groups = spec.get("required_selections", {})
        for group, selections in required_groups.items():
            for name, value in selections.items():
                if profile.get(name) != value:
                    required_failures.append(
                        f"profile {group} requires {name}={value}, got {profile.get(name, '<missing>')}"
                    )
        forbidden = [name for name in spec["forbidden_symbols"] if profile.get(name) in ("y", "m")]
        if self.config_path:
            actual = self.parse_config(self.config_path)
            for name, value in profile.items():
                if actual.get(name) != value:
                    missing_or_changed.append(f"{name}: expected {value}, got {actual.get(name, '<missing>')}")
            for group, selections in required_groups.items():
                for name, value in selections.items():
                    if actual.get(name) != value:
                        required_failures.append(
                            f".config {group} requires {name}={value}, got {actual.get(name, '<missing>')}"
                        )
            forbidden.extend(name for name in spec["forbidden_symbols"] if actual.get(name) in ("y", "m"))
        required_target = required_groups.get("target", {})
        profile_has_target = all(profile.get(name) == value for name, value in required_target.items())
        ok = not forbidden and profile_has_target and not required_failures and not missing_or_changed
        detail = "all required profile and diffconfig selections are preserved" if self.config_path and not missing_or_changed and not required_failures else ""
        if missing_or_changed:
            detail = "; ".join(missing_or_changed)
        if required_failures:
            detail = (detail + "; " if detail else "") + "; ".join(required_failures)
        if forbidden:
            detail = (detail + "; " if detail else "") + "forbidden selections: " + ", ".join(forbidden)
        if not profile_has_target:
            detail = (detail + "; " if detail else "") + "profile has no enabled target selection"
        self.record("x86_profile", ok, detail or "profile selections are present")

    def check_network_defaults(self) -> None:
        spec = self.features["network_defaults"]
        failures = []
        for relative in spec["lan_config_files"]:
            text = self.path_text(relative)
            if f'lan) ipad=${{ipaddr:-"{spec["lan_address"]}"}}' not in text:
                failures.append(f"{relative} does not default LAN to {spec['lan_address']}")
        dhcp = self.path_text(spec["dhcp_file"])
        if not re.search(r"option\s+start\s+" + re.escape(spec["dhcp_start"]) + r"\s*(?:\n|$)", dhcp):
            failures.append(f"DHCP start is not {spec['dhcp_start']}")
        if not re.search(r"option\s+limit\s+" + re.escape(spec["dhcp_limit"]) + r"\s*(?:\n|$)", dhcp):
            failures.append(f"DHCP limit is not {spec['dhcp_limit']}")
        self.record("network_defaults", not failures, "; ".join(failures) or "LAN address and DHCP pool match")

    def check_perl_and_docker(self) -> None:
        perl = self.features["perl_override"]
        perl_text = self.path_text(perl["helper"])
        perl_ok = "--locked" in perl_text and "--revision" in perl_text and perl["destination"] in perl_text
        self.record("official_perl_locking", perl_ok, "official Perl helper has a pinned revision mode")

        docker = self.features["dockerd_overlay"]
        source = self.root / docker["source_patch"]
        source_ok = bool(self.path_text(docker["source_patch"]))
        makefile_ok = True
        if self.mode == "build":
            package_makefile = self.root / docker["package_makefile"]
            makefile_ok = package_makefile.is_file() and f"PKG_NAME:={docker['package_name']}" in package_makefile.read_text(errors="replace")
        lock_ok = False
        try:
            lock = json.loads(self.path_text(".github/feed-lock.json"))
            patch_lock = lock.get("overlays", {}).get("dockerd_patch", {})
            patch_bytes = self.path_text(docker["source_patch"]).encode()
            lock_ok = hashlib.sha256(patch_bytes).hexdigest() == patch_lock.get("sha256")
        except (FileNotFoundError, json.JSONDecodeError):
            lock_ok = False
        ok = source_ok and makefile_ok and lock_ok
        self.record("dockerd_overlay_lock", ok, "source patch, package name, and SHA-256 lock agree" if ok else "dockerd patch, package, or lock is missing/drifted")
        if self.mode == "build":
            staged = self.root / docker["staged_patch"]
            staged_ok = source_ok and staged.is_file() and staged.read_bytes() == source.read_bytes()
            self.record("dockerd_staged_patch", staged_ok, "staged feed patch equals the locked source patch")

    def check_device_contract(self) -> None:
        failures = []
        for feature in ("airoha_history", "qualcomm_history", "an7583_config", "rockchip_backports"):
            spec = self.features[feature]
            for relative in spec.get("required_files", []) + spec.get("other_required_files", []):
                if not self.source_path_exists(relative):
                    failures.append(f"{feature}: missing {relative}")
            for relative in spec.get("paths", []):
                if not self.source_path_exists(relative):
                    failures.append(f"{feature}: missing {relative}")
            for relative, required in spec.get("required_tokens", {}).items():
                text = self.path_text(relative)
                for token in required:
                    if token not in text:
                        failures.append(f"{feature}: {relative} lost {token}")
            for relative, required in spec.get("required_patch_tokens", {}).items():
                text = self.path_text(relative)
                for token in required:
                    if token not in text:
                        failures.append(f"{feature}: {relative} lost {token}")

        q = self.features["qualcomm_history"]
        image = self.path_text(q["image_file"])
        match = re.search(r"define Device/EmmcImage\s*(.*?)\s*endef", image, re.S)
        if not match:
            failures.append("qualcomm_history: Device/EmmcImage template is missing")
        else:
            template = match.group(1)
            for token in q["emmc_template"]:
                if token not in template:
                    failures.append(f"qualcomm_history: EmmcImage lost {token}")
            for token in q["forbidden_emmc_images"]:
                if token in template:
                    failures.append(f"qualcomm_history: EmmcImage declares unsupported {token}")
        for device in q["required_devices"]:
            if f"TARGET_DEVICES += {device}" not in image:
                failures.append(f"qualcomm_history: missing target device {device}")
        if "<<<<<<<" in image or ">>>>>>>" in image:
            failures.append("qualcomm_history: unresolved conflict markers in image definitions")

        an_config = self.path_text(self.features["an7583_config"]["path"])
        for key, value in self.features["an7583_config"]["required"].items():
            if f"{key}={value}" not in an_config:
                failures.append(f"an7583_config: {key}={value} is missing")

        self.record("device_history", not failures, "; ".join(failures) or "Airoha, Qualcomm, and Rockchip inventory checks pass")

    def check_provenance(self) -> None:
        source = self.path_text(self.features["source_provenance"]["helper"], trusted=True)
        trusted_workflow = self.path_text(self.features["source_provenance"]["candidate_workflow"], trusted=True)
        candidate_workflow = self.path_text(self.features["source_provenance"]["candidate_workflow"])
        candidate_controller = self.path_text(".github/workflows/sync-upstream.yml")
        try:
            workflow = yaml.load(candidate_workflow, Loader=yaml.BaseLoader)
            triggers = workflow.get("on", {})
            permissions = workflow.get("permissions", {})
            jobs = workflow.get("jobs", {})
            build = jobs.get("build_openwrt", {})
            publisher = jobs.get("publish_release", {})
            publisher_if = str(publisher.get("if", ""))
            build_steps = build.get("steps", [])
            build_upload = any(str(step.get("uses", "")).startswith("actions/upload-artifact@")
                               for step in build_steps)
            non_publisher_jobs = [job for name, job in jobs.items() if name != "publish_release"]
            non_publisher_writes = any(
                value == "write"
                for job in non_publisher_jobs
                for value in job.get("permissions", {}).values()
            )
            non_publisher_commands = "\n".join(
                step.get("run", "")
                for job in non_publisher_jobs
                for step in job.get("steps", [])
            )
            checkout_credentials = [
                step.get("with", {}).get("persist-credentials")
                for job in jobs.values()
                for step in job.get("steps", [])
                if str(step.get("uses", "")).startswith("actions/checkout@")
            ]
            controller = yaml.load(candidate_controller, Loader=yaml.BaseLoader)
            controller_triggers = controller.get("on", {})
            controller_job = controller.get("jobs", {}).get("prepare_candidate", {})
            controller_if = str(controller_job.get("if", ""))
            controller_commands = "\n".join(
                step.get("run", "")
                for job in controller.get("jobs", {}).values()
                for step in job.get("steps", [])
            )
            push_commands = [line for line in controller_commands.splitlines()
                             if re.search(r"\bgit\s+push\b", line)]
        except (AttributeError, TypeError, yaml.YAMLError):
            workflow = {}
            triggers = permissions = jobs = {}
            publisher_if = ""
            build_upload = False
            non_publisher_writes = True
            non_publisher_commands = "gh release git push"
            checkout_credentials = []
            build = publisher = {}
            controller_triggers = controller_job = {}
            controller_if = ""
            controller_commands = "--force git push master"
            push_commands = ["git push master --force"]
        checks = [
            "EXPECTED_SOURCE_SHA" in source,
            "git rev-parse --verify 'HEAD^{commit}'" in source,
            "workflow_dispatch:" in trusted_workflow,
            "persist-credentials: false" in trusted_workflow,
            "upload-artifact" in trusted_workflow,
            "workflow_dispatch" in triggers and "pull_request" in triggers,
            "pull_request_target" not in triggers,
            permissions.get("contents") == "read",
            not any(value == "write" for value in permissions.values()),
            not non_publisher_writes,
            all(value == "false" for value in checkout_credentials),
            build_upload,
            "gh release" not in non_publisher_commands and "git push" not in non_publisher_commands,
            "github.event_name == 'workflow_dispatch'" in publisher_if,
            "inputs.sync_id == ''" in publisher_if,
            "github.ref == format(" in publisher_if,
            publisher.get("permissions", {}).get("contents") == "write",
            "workflow_dispatch" in controller_triggers,
            "github.ref == format(" in controller_if,
            bool(push_commands),
            all("CANDIDATE_BRANCH" in command and "DEFAULT_BRANCH" not in command
                and "master" not in command and "--force" not in command
                for command in push_commands),
            "--force" not in controller_commands,
        ]
        self.record("source_provenance", all(checks), "candidate workflow retains source checks, artifact-only build, and isolated default-branch publisher")

    def run(self) -> dict:
        self.check_passwall()
        self.check_profile()
        self.check_network_defaults()
        self.check_perl_and_docker()
        self.check_device_contract()
        self.check_provenance()
        return {
            "schema_version": 1,
            "mode": self.mode,
            "success": not self.errors,
            "checks": self.checks,
            "errors": self.errors,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("source", "build"), default="source")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-ref", help="read source files from this Git commit without checking them out")
    parser.add_argument("--trusted-ref", help="read workflow/provenance code from this trusted commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        checker = Contract(args.root, args.config, args.mode, args.source_ref, args.trusted_ref)
        result = checker.run()
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        result = {
            "schema_version": 1,
            "mode": args.mode,
            "success": False,
            "checks": {},
            "errors": [str(exc)],
        }
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    sys.stdout.write(rendered)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
