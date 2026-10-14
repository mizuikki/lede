#!/usr/bin/env python3
"""Check locked feed inputs and the OpenWrt Go compiler before building."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("feed_lock", ROOT / "scripts/feed-lock.py")
feed_lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed_lock)
Error = feed_lock.LockError
PACKAGES = {
    "xray-core": "feeds/passwall_packages/xray-core/Makefile",
    "sing-box": "feeds/passwall_packages/sing-box/Makefile",
}


def version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        raise Error(f"unsupported Go version: {value}")
    parts = tuple(int(n) for n in value.split("."))
    return parts + (0,) * (3 - len(parts))


def assignments(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise Error(f"package Makefile is missing: {path}")
    return dict(re.findall(r"^(\w+)\s*:?=\s*(.*?)\s*$", path.read_text(), re.M))


def archive_requirement(makefile: Path, downloads: Path, *, fetch: bool) -> dict | None:
    values = assignments(makefile)
    name, release, digest = (values.get(k, "") for k in ("PKG_NAME", "PKG_VERSION", "PKG_HASH"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[0-9][A-Za-z0-9_.-]*", release):
        raise Error(f"unsupported proxy source version: {makefile}")
    filename = f"{name}-{release}.tar.gz"
    if values.get("PKG_SOURCE") != "$(PKG_NAME)-$(PKG_VERSION).tar.gz" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Error(f"unsupported proxy source archive/hash: {makefile}")
    archive = downloads / filename
    if not archive.is_file() and not fetch:
        return None
    if not archive.is_file():
        url = values.get("PKG_SOURCE_URL", "").replace("$(PKG_VERSION)", release).rstrip("?")
        if not url.startswith("https://codeload.github.com/"):
            raise Error(f"unsupported proxy source URL: {makefile}")
        downloads.mkdir(parents=True, exist_ok=True)
        print(f"Checking source requirement: {name} {release}", flush=True)
        with tempfile.NamedTemporaryFile(dir=downloads, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                output.close()
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise Error(f"source archive hash mismatch: {filename}")
                os.replace(temporary, archive)
            finally:
                temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise Error(f"source archive hash mismatch: {filename}; preserve it and fetch a verified copy")
    try:
        with tarfile.open(archive) as source:
            members = [m for m in source.getmembers() if m.isfile() and m.name.rstrip("/").split("/")[1:] == ["go.mod"]]
            if len(members) != 1 or members[0].size > 1024 * 1024:
                raise Error(f"missing or ambiguous root go.mod: {filename}")
            mod = source.extractfile(members[0]).read()
    except (OSError, tarfile.TarError) as exc:
        raise Error(f"cannot read Go source archive: {filename}: {exc}") from exc
    match = re.search(rb"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", mod, re.M)
    if not match:
        raise Error(f"missing Go directive: {filename}")
    return {"version": release, "source_sha256": digest,
            "go_mod_sha256": hashlib.sha256(mod).hexdigest(), "go_version": match[1].decode()}


def collect_requirements(root: Path, downloads: Path) -> dict:
    requirements = {}
    for package, relative in PACKAGES.items():
        if (root / relative).is_file():
            requirements[package] = archive_requirement(root / relative, downloads, fetch=True)
    return requirements


def check_go(root: Path, requirements: dict, *, compiler: bool, config: Path | None = None) -> None:
    selected = set(requirements)
    if config is not None and config.is_file():
        selected = {p for p in requirements if re.search(
            rf"^CONFIG_PACKAGE_{re.escape(p)}=(?:y|m)$", config.read_text(), re.M)}
    if not selected:
        return
    values = assignments(root / "feeds/packages/lang/golang/golang/Makefile")
    available = values.get("GO_VERSION_MAJOR_MINOR", "")
    patch = values.get("GO_VERSION_PATCH", "")
    if patch:
        available += "." + patch
    for package in sorted(selected):
        requirement = requirements[package]
        actual = assignments(root / PACKAGES[package])
        if actual.get("PKG_VERSION") != requirement["version"] or actual.get("PKG_HASH") != requirement["source_sha256"]:
            raise Error(f"{package} source differs from its recorded Go requirement; synchronize all feeds again")
        if version(available) < version(requirement["go_version"]):
            raise Error(f"{package} {requirement['version']} requires Go >= {requirement['go_version']}; "
                        f"packages feed provides Go {available}. Synchronize all feeds together.")
    binary = root / "staging_dir/hostpkg/lib/go-cross/bin/go"
    if compiler and binary.exists():
        result = subprocess.run([str(binary), "version"], capture_output=True, text=True)
        match = re.search(r"\bgo(\d+\.\d+(?:\.\d+)?)\b", result.stdout)
        if result.returncode or not match:
            raise Error("cannot identify the OpenWrt Go compiler; rebuild golang/host")
        if version(match[1]) != version(available):
            raise Error(f"OpenWrt Go compiler is {match[1]}, but the feed selects {available}. Run "
                        "make package/feeds/packages/golang/host/clean && "
                        'make -j"$(nproc)" package/feeds/packages/golang/host/compile V=s first.')


def check_build(root: Path, goal: str, config: Path | None = None) -> None:
    failed = root / ".cache/feed-sync/FAILED"
    if failed.exists():
        raise Error("feed synchronization is incomplete or failed; rerun scripts/sync-feeds.sh "
                    "or scripts/sync-feeds.sh --locked successfully before building")
    receipt = root / ".cache/feed-sync/installed-lock.json"
    lock_path = root / ".github/feed-lock.json"
    lock = feed_lock.load_lock(lock_path) if lock_path.is_file() else {}
    if receipt.is_file():
        installed = feed_lock.load_lock(receipt)
        if feed_lock.canonical_digest(installed) != feed_lock.canonical_digest(lock):
            raise Error("installed feeds differ from the committed lock; install --locked-feeds again")
        config_path = root / "feeds.conf"
        errors = feed_lock.verify_installed(root, lock, config_path if config_path.exists() else root / "feeds.conf.default")
        if errors:
            raise Error("; ".join(errors))
    requirements = dict(lock.get("go_packages", {}))
    if not requirements:
        for package, relative in PACKAGES.items():
            if (root / relative).is_file():
                requirement = archive_requirement(root / relative, root / "dl", fetch=False)
                if requirement:
                    requirements[package] = requirement
    check_compiler = goal == "world" or any(p in goal and goal.endswith("/compile") for p in PACKAGES)
    check_go(root, requirements, compiler=check_compiler, config=config or root / ".config")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--goal", default="world")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        check_build(args.root, args.goal, args.config)
    except (Error, OSError, ValueError) as exc:
        print(f"build inputs: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
