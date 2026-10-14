#!/usr/bin/env python3
"""Create and verify immutable OpenWrt feed and overlay locks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = Path(".github/feed-lock.json")
PERL_URL = "https://github.com/openwrt/packages"
PERL_BRANCH = "openwrt-25.12"
PERL_PATH = "lang/perl"
PERL_DESTINATION = "feeds/packages/lang/perl"
DOCKER_PATCH = "patches/dockerd/100-skip-host-nested-binaries.patch"
DOCKER_STAGED = "feeds/packages/utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class LockError(RuntimeError):
    pass


def run(args: list[str], *, cwd: Path | None = None, check: bool = True,
        input_data: bytes | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, input=input_data, capture_output=True)
    if check and result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise LockError(f"command failed ({result.returncode}): {args!r}: {message}")
    return result


def canonical_digest(lock: dict) -> str:
    payload = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_feeds(path: Path) -> list[dict]:
    feeds: list[dict] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        try:
            words = shlex.split(content)
        except ValueError as exc:
            raise LockError(f"invalid feed syntax at {path}:{line_number}: {exc}") from exc
        if len(words) < 3 or not words[0].startswith("src-"):
            raise LockError(f"unsupported feed syntax at {path}:{line_number}: {content}")
        feed_type, name, source = words[0][4:], words[1], words[2]
        if feed_type != "git":
            raise LockError(f"enabled non-git feed is not lockable: {name} ({feed_type})")
        if name in seen:
            raise LockError(f"duplicate feed name: {name}")
        seen.add(name)
        source, _, pinned = source.partition("^")
        url, separator, branch = source.partition(";")
        branch = branch if separator else None
        if pinned:
            if not HEX40.fullmatch(pinned.lower()):
                raise LockError(f"feed {name} has a non-full revision in {path}")
        if not url.startswith(("https://", "http://", "ssh://", "git://")):
            raise LockError(f"feed {name} has an unsupported URL")
        feeds.append({"name": name, "url": url, "branch": branch, "configured_revision": pinned or None})
    if not feeds:
        raise LockError(f"no enabled Git feeds found in {path}")
    return feeds


def resolve_ref(url: str, branch: str | None) -> tuple[str, str]:
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    result = run(["git", "ls-remote", "--symref", url, ref])
    revision = None
    resolved_ref = None
    for line in result.stdout.decode().splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            continue
        value, name = fields
        if value.startswith("ref: ") and name == "HEAD":
            resolved_ref = value[5:]
        elif name == ref:
            revision = value.lower()
    if not branch and resolved_ref:
        resolved_ref = resolved_ref
    elif branch:
        resolved_ref = ref
    if not revision or not HEX40.fullmatch(revision):
        raise LockError(f"could not resolve {url} {ref}")
    branch_name = resolved_ref.removeprefix("refs/heads/") if resolved_ref else "HEAD"
    return revision, branch_name


def git_tree_content_digest(git_dir: Path, revision: str, tree_path: str) -> tuple[str, str]:
    tree = run(["git", f"--git-dir={git_dir}", "rev-parse", f"{revision}:{tree_path}"]).stdout.decode().strip()
    records = run(["git", f"--git-dir={git_dir}", "ls-tree", "-rz", "-r", revision, "--", tree_path]).stdout
    digest = hashlib.sha256()
    for record in records.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = meta.decode().split()
        if kind != "blob":
            continue
        relative = raw_path.decode(errors="surrogateescape")
        prefix = tree_path.rstrip("/") + "/"
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
        content = run(["git", f"--git-dir={git_dir}", "cat-file", "blob", object_id]).stdout
        digest.update(mode.encode() + b"\0" + relative.encode(errors="surrogateescape") + b"\0")
        digest.update(str(len(content)).encode() + b"\0" + content + b"\0")
    return tree, digest.hexdigest()


def directory_content_digest(directory: Path) -> str:
    if not directory.is_dir():
        raise LockError(f"overlay directory is missing: {directory}")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        relative = path.relative_to(directory).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            mode = "120000"
            content = os.readlink(path).encode()
        elif path.is_file():
            mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            content = path.read_bytes()
        else:
            raise LockError(f"unsupported file in overlay: {path}")
        digest.update(mode.encode() + b"\0" + relative.encode(errors="surrogateescape") + b"\0")
        digest.update(str(len(content)).encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def create_lock(root: Path, feeds_config: Path) -> dict:
    feeds = []
    for order, feed in enumerate(parse_feeds(feeds_config), 1):
        configured_branch = feed["branch"]
        if feed["configured_revision"]:
            revision = feed["configured_revision"]
            branch = feed["branch"]
        else:
            revision, resolved_branch = resolve_ref(feed["url"], feed["branch"])
            branch = feed["branch"] or resolved_branch
        feeds.append({
            "name": feed["name"],
            "url": feed["url"],
            "branch": branch,
            "configured_branch": configured_branch,
            "revision": revision,
            "order": order,
        })

    perl_revision, _ = resolve_ref(PERL_URL, PERL_BRANCH)
    with tempfile.TemporaryDirectory(prefix="lede-perl-lock-") as temporary:
        git_dir = Path(temporary) / "perl.git"
        run(["git", "init", "--bare", "--quiet", str(git_dir)])
        run(["git", f"--git-dir={git_dir}", "fetch", "--quiet", "--depth=1", PERL_URL, perl_revision])
        perl_tree, perl_digest = git_tree_content_digest(git_dir, "FETCH_HEAD", PERL_PATH)

    docker_file = root / DOCKER_PATCH
    if not docker_file.is_file():
        raise LockError(f"dockerd overlay is missing: {docker_file}")
    return {
        "schema_version": 1,
        "feeds": feeds,
        "overlays": {
            "official_perl": {
                "repository": PERL_URL,
                "branch": PERL_BRANCH,
                "revision": perl_revision,
                "path": PERL_DESTINATION,
                "tree": perl_tree,
                "sha256": perl_digest,
            },
            "dockerd_patch": {
                "path": DOCKER_PATCH,
                "staged_path": DOCKER_STAGED,
                "sha256": hashlib.sha256(docker_file.read_bytes()).hexdigest(),
            },
        },
    }


def load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read lock file {path}: {exc}") from exc
    if lock.get("schema_version") != 1 or not isinstance(lock.get("feeds"), list):
        raise LockError(f"unsupported feed lock schema: {path}")
    names = set()
    orders = []
    for feed in lock["feeds"]:
        if not HEX40.fullmatch(str(feed.get("revision", "")).lower()):
            raise LockError(f"feed {feed.get('name')} does not contain a full commit SHA")
        if feed.get("name") in names:
            raise LockError(f"duplicate feed in lock: {feed.get('name')}")
        names.add(feed.get("name"))
        orders.append(feed.get("order"))
    if orders != list(range(1, len(orders) + 1)):
        raise LockError("feed lock order must be contiguous and preserve feeds.conf.default")
    perl = lock.get("overlays", {}).get("official_perl", {})
    patch = lock.get("overlays", {}).get("dockerd_patch", {})
    for name, value in (("Perl", perl.get("revision")), ("Perl tree", perl.get("tree"))):
        if not HEX40.fullmatch(str(value or "").lower()):
            raise LockError(f"{name} lock value is not a full SHA")
    for value in (perl.get("sha256"), patch.get("sha256")):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value or "")):
            raise LockError("overlay lock must contain full SHA-256 digests")
    return lock


def write_locked_config(lock: dict, path: Path) -> None:
    lines = ["# Generated from .github/feed-lock.json; do not edit."]
    for feed in lock["feeds"]:
        lines.append(f"src-git {feed['name']} {feed['url']}^{feed['revision']}")
    path.write_text("\n".join(lines) + "\n")


def verify_installed(root: Path, lock: dict, feeds_config: Path) -> list[str]:
    errors: list[str] = []
    configured = parse_feeds(feeds_config)
    expected = [(f["name"], f["url"], f.get("configured_branch")) for f in lock["feeds"]]
    actual = [(f["name"], f["url"], f["branch"]) for f in configured]
    if actual != expected:
        errors.append("enabled feed names, URLs, branches, or order differ from the lock")
    for feed in lock["feeds"]:
        path = root / "feeds" / feed["name"]
        if not (path / ".git").exists():
            errors.append(f"feed is not installed: {feed['name']}")
            continue
        revision = run(["git", "-C", str(path), "rev-parse", "HEAD"], check=False)
        if revision.returncode or revision.stdout.decode().strip().lower() != feed["revision"].lower():
            errors.append(f"feed revision drift: {feed['name']}")

    allowed_dirty = {
        "lang/perl/",
        "utils/dockerd/patches/999-openwrt-skip-host-nested-binaries.patch",
    }
    for feed in lock["feeds"]:
        path = root / "feeds" / feed["name"]
        if not (path / ".git").exists():
            continue
        status = run([
            "git", "-C", str(path), "status", "--porcelain=v1", "-z",
            "--untracked-files=all",
        ]).stdout
        records = status.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                errors.append(f"could not parse {feed['name']} feed status record")
                continue
            code = record[:2].decode("ascii", errors="replace")
            changed_paths = [record[3:].decode(errors="surrogateescape")]
            if "R" in code or "C" in code:
                if index >= len(records):
                    errors.append(f"could not parse rename in {feed['name']} feed status")
                else:
                    changed_paths.append(records[index].decode(errors="surrogateescape"))
                    index += 1
            for relative in changed_paths:
                allowed_overlay = (
                    feed["name"] == "packages"
                    and any(relative == item or relative.startswith(item) for item in allowed_dirty)
                )
                if not allowed_overlay:
                    errors.append(f"unexpected feed working tree modification: {feed['name']}:{relative}")

    perl = lock["overlays"]["official_perl"]
    perl_path = root / perl["path"]
    if not perl_path.is_dir():
        errors.append("official Perl overlay is missing")
    elif directory_content_digest(perl_path) != perl["sha256"]:
        errors.append("official Perl overlay digest drift")

    patch = lock["overlays"]["dockerd_patch"]
    source = root / patch["path"]
    staged = root / patch["staged_path"]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != patch["sha256"]:
        errors.append("dockerd source patch digest drift")
    if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != patch["sha256"]:
        errors.append("dockerd staged patch is missing or differs from the lock")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="resolve enabled feed refs and overlay digests")
    create.add_argument("--root", type=Path, default=ROOT)
    create.add_argument("--feeds-conf", type=Path, default=None)
    create.add_argument("--output", type=Path, default=ROOT / LOCK_PATH)
    create.add_argument("--print-digest", action="store_true")
    materialize = subparsers.add_parser("materialize", help="write pinned feeds.conf")
    materialize.add_argument("--lock", type=Path, default=ROOT / LOCK_PATH)
    materialize.add_argument("--output", type=Path, default=ROOT / "feeds.conf")
    verify = subparsers.add_parser("verify", help="check installed feed revisions and overlays")
    verify.add_argument("--root", type=Path, default=ROOT)
    verify.add_argument("--lock", type=Path, default=ROOT / LOCK_PATH)
    verify.add_argument("--feeds-conf", type=Path, default=None)
    digest = subparsers.add_parser("digest", help="print canonical lock digest")
    digest.add_argument("--lock", type=Path, default=ROOT / LOCK_PATH)
    overlay_digest = subparsers.add_parser("overlay-digest", help="hash an extracted overlay tree")
    overlay_digest.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            config = args.feeds_conf or args.root / "feeds.conf.default"
            lock = create_lock(args.root, config)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n")
            print(canonical_digest(lock) if args.print_digest else args.output)
        elif args.command == "materialize":
            write_locked_config(load_lock(args.lock), args.output)
            print(args.output)
        elif args.command == "verify":
            lock = load_lock(args.lock)
            config = args.feeds_conf or args.root / "feeds.conf"
            errors = verify_installed(args.root, lock, config)
            if errors:
                print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
                return 1
            print(f"feeds and overlays match lock {canonical_digest(lock)}")
        elif args.command == "digest":
            print(canonical_digest(load_lock(args.lock)))
        elif args.command == "overlay-digest":
            print(directory_content_digest(args.path))
    except LockError as exc:
        print(f"feed-lock: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
