#!/usr/bin/env python3
"""Prepare all feeds in isolation, then install a verified snapshot with rollback."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("check_build_inputs", ROOT / "scripts/check-build-inputs.py")
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)
lock_api = inputs.feed_lock
Error = lock_api.LockError


def command(root: Path, *args: str) -> None:
    env = os.environ.copy()
    for key in ("OPENWRT_BUILD", "STAGING_DIR", "STAGING_DIR_HOST", "STAGING_DIR_HOSTPKG", "TMP_DIR", "MAKEFLAGS", "MFLAGS"):
        env.pop(key, None)
    env["TOPDIR"] = str(root)
    result = subprocess.run(args, cwd=root, env=env)
    if result.returncode:
        raise Error(f"snapshot preparation failed ({result.returncode}): {' '.join(args)}")


def recognized_overlay_digests(root: Path) -> set[str]:
    digests = set()
    for path in (root / ".github/feed-lock.json", root / ".cache/feed-sync/installed-lock.json"):
        if path.is_file():
            digests.add(lock_api.load_lock(path)["overlays"]["official_perl"]["sha256"])
    # Older sync-official-perl.sh installations predate the receipt. Recognize
    # only the committed, clean content of their official-repository cache.
    cache = root / ".cache/openwrt-packages"
    if (cache / ".git").is_dir():
        origin = lock_api.run(["git", "-C", str(cache), "remote", "get-url", "origin"], check=False)
        if origin.stdout.decode().strip().removesuffix(".git").rstrip("/") == lock_api.PERL_URL:
            _, digest = lock_api.git_tree_content_digest(cache / ".git", "HEAD", "lang/perl")
            digests.add(digest)
    return digests


def check_existing(root: Path) -> None:
    feeds = root / "feeds"
    if not feeds.exists():
        return
    perl = feeds / "packages/lang/perl"
    perl_ok = None
    docker = "utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch"
    source = root / lock_api.DOCKER_PATCH
    for path in sorted(feeds.iterdir()):
        if not path.is_dir() or path.name.endswith(".tmp"):
            continue
        if not (path / ".git").is_dir() or path.is_symlink():
            raise Error(f"refusing to replace unmanaged feed: {path.name}; preserve it separately first")
        if lock_api.run(["git", "-C", str(path), "diff", "--cached", "--quiet"], check=False).returncode:
            raise Error(f"feed {path.name} contains staged user changes; preserve them before synchronization")
        # Feed checkouts created by these helpers have no local commits. If a
        # user has committed changes, replacing the feed must also be refused.
        remote = lock_api.run(["git", "-C", str(path), "branch", "-r", "--contains", "HEAD"]).stdout.strip()
        if not remote:
            raise Error(f"feed {path.name} has a local commit not represented by a remote-tracking ref")
        status = lock_api.run(["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
        records = status.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            code, relative = record[:2], record[3:].decode(errors="surrogateescape")
            if b"R" in code or b"C" in code:
                raise Error(f"feed {path.name} contains a user rename/copy: {relative}")
            managed = False
            if path.name == "packages" and relative.startswith("lang/perl/"):
                if perl_ok is None:
                    perl_ok = perl.is_dir() and lock_api.directory_content_digest(perl) in recognized_overlay_digests(root)
                managed = perl_ok
            elif path.name == "packages" and relative == docker:
                target = path / relative
                managed = target.is_file() and not target.is_symlink() and source.is_file() and target.read_bytes() == source.read_bytes()
            if not managed:
                raise Error(f"feed {path.name} contains an unrecognized modification: {relative}; "
                            "back it up and resolve it before synchronization (no force reset was performed)")


def copy_source(root: Path, stage: Path) -> None:
    files = lock_api.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).stdout
    for raw in files.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source, destination = root / relative, stage / relative
        if not source.exists() and not source.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, destination)


def prepare(root: Path, stage: Path, lock: dict) -> None:
    copy_source(root, stage)
    lock_path = stage / ".github/feed-lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    lock_api.write_locked_config(lock, stage / "feeds.conf")
    for feed in lock["feeds"]:
        destination = stage / "feeds" / feed["name"]
        destination.mkdir(parents=True)
        print(f"Preparing feed {feed['name']} at {feed['revision']}", flush=True)
        command(stage, "git", "init", "--quiet", str(destination))
        command(stage, "git", "-C", str(destination), "remote", "add", "origin", feed["url"])
        # Borrow only objects from an existing checkout; never its working tree
        # or alternates. A backup remains self-contained after promotion.
        existing = root / "feeds" / feed["name"]
        cached = False
        if (existing / ".git").is_dir():
            result = subprocess.run(["git", "-C", str(destination), "fetch", "--quiet", "--depth=1",
                                     str(existing), feed["revision"]], capture_output=True)
            cached = result.returncode == 0
        if not cached:
            try:
                command(stage, "git", "-C", str(destination), "fetch", "--quiet", "--depth=1", "origin", feed["revision"])
            except Error as exc:
                raise Error(f"cannot fetch feed {feed['name']}: {exc}") from exc
        command(stage, "git", "-C", str(destination), "checkout", "--quiet", "--detach", feed["revision"])
        command(stage, "git", "-C", str(destination), "update-ref", "refs/remotes/origin/" + feed["branch"], feed["revision"])
    command(stage, "./scripts/feeds", "update", "-a")
    command(stage, "./scripts/sync-official-perl.sh", "--locked")
    command(stage, "./scripts/stage-dockerd-build-patch.sh", "--locked")
    # Both sources are checked against the feed's PKG_HASH; requirements never
    # come from a mutable go.mod URL or an old build_dir checkout.
    actual_requirements = inputs.collect_requirements(stage, root / "dl")
    if lock.get("go_packages") is not None and lock["go_packages"] != actual_requirements:
        raise Error("proxy source Go requirements differ from the lock")
    lock["go_packages"] = actual_requirements
    inputs.check_go(stage, actual_requirements, compiler=False)
    command(stage, "./scripts/feeds", "install", "-a")
    command(stage, "make", "prepare-tmpinfo")
    errors = lock_api.verify_installed(stage, lock, stage / "feeds.conf.default")
    if errors:
        raise Error("; ".join(errors))
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")


def promote(root: Path, stage: Path, lock: dict, *, refresh: bool, backup: Path) -> None:
    receipt = stage / ".cache/feed-sync/installed-lock.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(lock, indent=2) + "\n")
    paths = [Path("feeds"), Path("package/feeds"), Path(".cache/feed-sync/installed-lock.json")]
    if refresh:
        paths.append(Path(".github/feed-lock.json"))
    completed = []
    try:
        for relative in paths:
            destination, source, saved = root / relative, stage / relative, backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            had_old = destination.exists() or destination.is_symlink()
            if had_old:
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, saved)
            completed.append((relative, had_old))
            os.replace(source, destination)
    except BaseException:
        for relative, had_old in reversed(completed):
            destination = root / relative
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            if had_old:
                os.replace(backup / relative, destination)
        raise
    # Feed scan records contain absolute preparation paths. Invalidate only
    # scan caches, keeping the compiler and Go build caches for incremental use.
    for path in (root / "feeds").glob("*.tmp"):
        for entry in path.iterdir():
            if entry.name in (".packageinfo", ".targetinfo"):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for path in (root / "tmp").glob(".package*"):
        if path.is_file() or path.is_symlink():
            path.unlink()
    shutil.rmtree(root / "tmp/info", ignore_errors=True)


def synchronize(root: Path, *, locked: bool, lock_path: Path) -> dict:
    state = root / ".cache/feed-sync"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "sync.lock").open("a") as mutex:
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error("another feed synchronization is running") from exc
        failed = state / "FAILED"
        failed.write_text("Synchronization started; builds remain blocked until it succeeds.\n")
        try:
            if (root / "feeds.conf").exists():
                raise Error("feeds.conf exists; migrate its configuration to feeds.conf.default before an all-feed transaction")
            check_existing(root)
            lock = lock_api.load_lock(lock_path) if locked else lock_api.create_lock(root, root / "feeds.conf.default")
            # Locked installs must not silently enrich/change the tracked lock.
            original = json.loads(json.dumps(lock))
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=state) as temporary:
                stage = Path(temporary)
                prepare(root, stage, lock)
                if locked and "go_packages" not in original:
                    lock.pop("go_packages", None)
                    (stage / ".github/feed-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
                # Recheck in case somebody modified an installed feed while
                # the isolated snapshot was downloading.
                check_existing(root)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = state / "backups" / stamp
                promote(root, stage, lock, refresh=not locked, backup=backup)
            failed.unlink()
            print(f"Installed all feeds: lock {lock_api.canonical_digest(lock)}", flush=True)
            print(f"Previous feeds and package links retained in .cache/feed-sync/backups/{stamp}", flush=True)
            return lock
        except BaseException as exc:
            failed.write_text(f"Feed synchronization failed: {type(exc).__name__}. Rerun a complete synchronization.\n")
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--lock-file", type=Path)
    args = parser.parse_args()
    try:
        synchronize(args.root.resolve(), locked=args.locked,
                    lock_path=args.lock_file or args.root / ".github/feed-lock.json")
    except (Error, OSError, ValueError) as exc:
        print(f"sync-feeds: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
