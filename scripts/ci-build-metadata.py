#!/usr/bin/env python3
"""Create build provenance metadata from the checked-out source and locked inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from platform_impact import plan_for_paths


HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_JOBS = ["validate_source", "build_openwrt"]


class MetadataError(RuntimeError):
    pass


def canonical_digest(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise MetadataError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def create(root: Path, workflow_sha: str, expected_source_sha: str,
           expected_lock_sha256: str | None, sync_id: str | None,
           base_sha: str | None, upstream_sha: str | None,
           merge_base_sha: str | None, build_reason: str,
           pull_request: int | None, pull_head_sha: str | None,
           pull_base_sha: str | None, pull_merge_sha: str | None,
           output: Path) -> dict:
    source_sha = git(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not HEX40.fullmatch(expected_source_sha) or source_sha != expected_source_sha.lower():
        raise MetadataError("checked-out source SHA differs from the requested SHA")
    if not HEX40.fullmatch(workflow_sha):
        raise MetadataError("workflow SHA must be a full commit SHA")
    lock_path = root / ".github/feed-lock.json"
    if not lock_path.is_file():
        raise MetadataError("source commit has no .github/feed-lock.json")
    lock = json.loads(lock_path.read_text())
    lock_sha = canonical_digest(lock)
    if expected_lock_sha256 and (not HEX64.fullmatch(expected_lock_sha256) or lock_sha != expected_lock_sha256):
        raise MetadataError("feed lock differs from the dispatch input")

    candidate = bool(sync_id)
    identity = {}
    if candidate:
        for field, value in (("base SHA", base_sha), ("upstream SHA", upstream_sha), ("merge-base SHA", merge_base_sha)):
            if not value or not HEX40.fullmatch(value):
                raise MetadataError(f"candidate build is missing a full {field}")
        manifest = json.loads((root / ".github/upstream-candidate.json").read_text())
        expected = {
            "base_sha": base_sha.lower(),
            "upstream_sha": upstream_sha.lower(),
            "merge_base_sha": merge_base_sha.lower(),
            "workflow_sha": workflow_sha.lower(),
            "feed_lock_sha256": lock_sha,
            "sync_id": sync_id,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise MetadataError("candidate manifest does not match this dispatch")
        for label, ancestor in (("base", base_sha), ("upstream", upstream_sha)):
            result = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, source_sha])
            if result.returncode:
                raise MetadataError(f"candidate source does not contain its pinned {label}")
        identity = expected
        pinned_merge = manifest.get("merge_commit_sha")
        if pinned_merge:
            if not HEX40.fullmatch(str(pinned_merge)):
                raise MetadataError("candidate merge commit SHA is not full length")
            parents = git(root, "rev-list", "-n", "1", "--parents", pinned_merge).split()
            if parents != [pinned_merge, base_sha.lower(), upstream_sha.lower()]:
                raise MetadataError("candidate merge commit does not have the exact B/U parents")
            identity["merge_commit_sha"] = pinned_merge
        if not identity.get("merge_commit_sha"):
            for commit in git(root, "rev-list", "--first-parent", source_sha).splitlines():
                parents = git(root, "rev-list", "-n", "1", "--parents", commit).split()
                if parents == [commit, base_sha.lower(), upstream_sha.lower()]:
                    identity["merge_commit_sha"] = commit
                    break
            if not identity.get("merge_commit_sha"):
                raise MetadataError("candidate source lost its pinned two-parent B/U merge")
    feed_revisions = []
    for feed in lock.get("feeds", []):
        feed_path = root / "feeds" / feed["name"]
        revision = git(feed_path, "rev-parse", "HEAD").lower()
        feed_revisions.append({"name": feed["name"], "revision": revision})
    pr_identity = None
    if pull_request:
        for label, value in (("pull request head SHA", pull_head_sha),
                             ("pull request base SHA", pull_base_sha),
                             ("pull request merge SHA", pull_merge_sha)):
            if not value or not HEX40.fullmatch(value):
                raise MetadataError(f"{label} must be a full commit SHA")
        if pull_merge_sha.lower() != source_sha:
            raise MetadataError("pull request merge SHA differs from the checked-out source")
        pr_identity = {
            "number": pull_request,
            "head_sha": pull_head_sha.lower(),
            "base_sha": pull_base_sha.lower(),
            "merge_sha": pull_merge_sha.lower(),
        }

    compare_base = base_sha if candidate else pull_base_sha if pull_request else None
    impact = {"schema_version": 1, "platforms": {}}
    if compare_base:
        changed = git(root, "diff", "--name-only", "--find-renames", compare_base, source_sha).splitlines()
        impact = plan_for_paths(changed)

    metadata = {
        "schema_version": 1,
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow_file": ".github/workflows/openwrt-ci.yml",
        "workflow_sha": workflow_sha.lower(),
        "dispatch_sha": os.environ.get("GITHUB_SHA", "").lower(),
        "source_sha": source_sha,
        "profile": "x86_64-passwall-docker",
        "build_reason": build_reason,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "sync_id": sync_id,
        "candidate": identity,
        "feed_lock_sha256": lock_sha,
        "feed_revisions": feed_revisions,
        "required_jobs": REQUIRED_JOBS,
        "pull_request": pr_identity,
        "platform_impact": impact,
        "validation": {
            "source_sha_match": True,
            "feed_lock_match": True,
            "candidate_identity_match": True if candidate else None,
            "feed_revisions_collected": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--feed-lock-sha256")
    parser.add_argument("--sync-id")
    parser.add_argument("--base-sha")
    parser.add_argument("--upstream-sha")
    parser.add_argument("--merge-base-sha")
    parser.add_argument("--build-reason", default="manual")
    parser.add_argument("--pull-request", type=int)
    parser.add_argument("--pull-head-sha")
    parser.add_argument("--pull-base-sha")
    parser.add_argument("--pull-merge-sha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata = create(
            args.root.resolve(), args.workflow_sha, args.source_sha,
            args.feed_lock_sha256, args.sync_id, args.base_sha,
            args.upstream_sha, args.merge_base_sha, args.build_reason,
            args.pull_request, args.pull_head_sha, args.pull_base_sha,
            args.pull_merge_sha, args.output,
        )
        print(json.dumps(metadata, indent=2, ensure_ascii=False))
    except (MetadataError, OSError, json.JSONDecodeError) as exc:
        print(f"ci-build-metadata: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
