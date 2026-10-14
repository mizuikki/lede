#!/usr/bin/env python3
"""Reject forbidden packages and missing required packages in build artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tarfile

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised on minimal systems
    raise SystemExit("PyYAML is required to read the fork contract") from exc


ROOT = Path(__file__).resolve().parents[1]


class ArtifactError(RuntimeError):
    pass


def load_policy(path: Path) -> dict:
    try:
        contract = yaml.safe_load(path.read_text())
        policy = contract["fork_features"]["artifact_validation"]
        required = policy["required_packages"]
        forbidden_names = policy["forbidden_package_names"]
        forbidden_prefixes = policy["forbidden_package_prefixes"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ArtifactError(f"cannot read artifact policy from {path}: {exc}") from exc
    if not required or not all(isinstance(name, str) and name for name in required):
        raise ArtifactError("artifact policy must declare required package names")
    if not all(isinstance(name, str) and name for name in forbidden_names + forbidden_prefixes):
        raise ArtifactError("artifact policy contains an invalid forbidden package name")
    return {
        "required_packages": sorted(set(required)),
        "forbidden_package_names": sorted(set(forbidden_names)),
        "forbidden_package_prefixes": sorted(set(forbidden_prefixes)),
    }


def package_names_from_manifest(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        fields = line.strip().split()
        if fields and fields[0] != "#":
            names.add(fields[0])
    return names


def forbidden_package(name: str, policy: dict) -> bool:
    return (name in policy["forbidden_package_names"]
            or any(name.startswith(prefix) for prefix in policy["forbidden_package_prefixes"]))


def forbidden_ipk(path: str, policy: dict) -> bool:
    filename = Path(path).name
    return (any(filename.startswith(name + "_") for name in policy["forbidden_package_names"])
            or any(filename.startswith(prefix) for prefix in policy["forbidden_package_prefixes"]))


def required_ipk_present(path: str, package: str) -> bool:
    return Path(path).name.startswith(package + "_") and Path(path).name.endswith(".ipk")


def validate_inventory(manifests: list[tuple[str, set[str]]], ipks: list[str], policy: dict) -> dict:
    errors = []
    if not manifests:
        errors.append("no firmware package manifests were found")
    for label, packages in manifests:
        blocked = sorted(name for name in packages if forbidden_package(name, policy))
        if blocked:
            errors.append(f"{label} contains forbidden package(s): {', '.join(blocked)}")
        missing = sorted(set(policy["required_packages"]) - packages)
        if missing:
            errors.append(f"{label} is missing required package(s): {', '.join(missing)}")

    blocked_ipks = sorted(path for path in ipks if forbidden_ipk(path, policy))
    if blocked_ipks:
        errors.append("package set contains forbidden IPK(s): " + ", ".join(blocked_ipks))
    missing_ipks = sorted(
        package for package in policy["required_packages"]
        if not any(required_ipk_present(path, package) for path in ipks)
    )
    if missing_ipks:
        errors.append("package set is missing required IPK(s): " + ", ".join(missing_ipks))

    present_by_manifest = {
        label: sorted(set(policy["required_packages"]) & packages)
        for label, packages in manifests
    }
    present_ipks = sorted(
        package for package in policy["required_packages"]
        if any(required_ipk_present(path, package) for path in ipks)
    )
    return {
        "schema_version": 1,
        "success": not errors,
        "required_packages": policy["required_packages"],
        "forbidden_package_names": policy["forbidden_package_names"],
        "forbidden_package_prefixes": policy["forbidden_package_prefixes"],
        "manifest_required_packages": present_by_manifest,
        "ipk_required_packages": present_ipks,
        "manifest_count": len(manifests),
        "ipk_count": len(ipks),
        "errors": errors,
    }


def validate_build_root(build_root: Path, policy: dict) -> dict:
    target_root = build_root / "targets"
    manifests = []
    if target_root.is_dir():
        for path in sorted(target_root.rglob("*.manifest")):
            relative_to_target = path.relative_to(target_root)
            # Package repository indexes also use .manifest, but describe every
            # built IPK rather than the packages installed into firmware.
            if "packages" in relative_to_target.parts[:-1] or path.name.startswith("Packages."):
                continue
            manifests.append((path.relative_to(build_root).as_posix(),
                              package_names_from_manifest(path.read_text(errors="replace"))))
    ipks = [path.relative_to(build_root).as_posix() for path in build_root.rglob("*.ipk")]
    return validate_inventory(manifests, ipks, policy)


def validate_release_assets(assets_root: Path, policy: dict) -> dict:
    manifest_path = assets_root / "package-manifests.txt"
    if not manifest_path.is_file():
        raise ArtifactError(f"release assets are missing {manifest_path.name}")
    manifests = [(manifest_path.name,
                  package_names_from_manifest(manifest_path.read_text(errors="replace")))]

    archives = sorted(assets_root.glob("*-packages-*.tar.gz"))
    if not archives:
        raise ArtifactError("release assets contain no packages archive")
    ipks = []
    for archive in archives:
        try:
            with tarfile.open(archive, mode="r:gz") as bundle:
                for member in bundle.getmembers():
                    if member.isfile() and member.name.endswith(".ipk"):
                        ipks.append(f"{archive.name}:{member.name}")
        except (OSError, tarfile.TarError) as exc:
            raise ArtifactError(f"cannot read package archive {archive.name}: {exc}") from exc
    return validate_inventory(manifests, ipks, policy)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, help="OpenWrt bin/ output directory")
    source.add_argument("--release-assets", type=Path,
                        help="prepared release-assets directory containing manifests and package archive")
    parser.add_argument("--contract", type=Path, default=ROOT / ".github/fork-contract.yml")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()

    try:
        policy = load_policy(args.contract)
        result = (validate_build_root(args.root, policy) if args.root
                  else validate_release_assets(args.release_assets, policy))
    except (ArtifactError, OSError) as exc:
        result = {"schema_version": 1, "success": False, "errors": [str(exc)]}

    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    sys.stdout.write(rendered)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
