#!/usr/bin/env python3
"""Prepare and report a reviewable upstream merge candidate without pushing refs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from platform_impact import plan_for_paths
from upstream_sync_control import CandidateSelectionError, candidate_branch


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ".github/upstream-state.json"
LOCK_PATH = ".github/feed-lock.json"
MANIFEST_PATH = ".github/upstream-candidate.json"
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


class SyncError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, input_data: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], input=input_data, text=True,
        capture_output=True, env=(os.environ | env) if env else None,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return result


def resolve_commit(repo: Path, value: str, label: str) -> str:
    if not value:
        raise SyncError(f"{label} is required")
    result = git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    if result.returncode:
        raise SyncError(f"{label} does not resolve to a commit: {value}")
    commit = result.stdout.strip().lower()
    if not HEX40.fullmatch(commit):
        raise SyncError(f"{label} did not resolve to a full commit SHA")
    return commit


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def blob_text(repo: Path, revision: str, path: str) -> str:
    result = git(repo, "show", f"{revision}:{path}", check=False)
    if result.returncode:
        raise SyncError(f"required path is missing at {revision[:12]}: {path}")
    return result.stdout


def blob_sha(repo: Path, revision: str, path: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", f"{revision}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def load_json_at(repo: Path, revision: str, path: str) -> dict:
    try:
        return json.loads(blob_text(repo, revision, path))
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON in {path} at {revision[:12]}: {exc}") from exc


def diff_paths(repo: Path, older: str, newer: str) -> list[str]:
    result = git(repo, "diff", "--name-only", "--find-renames", older, newer)
    return [line for line in result.stdout.splitlines() if line]


def diff_summary(repo: Path, older: str, newer: str) -> dict:
    names = diff_paths(repo, older, newer)
    numstat = git(repo, "diff", "--numstat", older, newer).stdout.splitlines()
    additions = deletions = 0
    for line in numstat:
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            additions += int(parts[0])
            deletions += int(parts[1])
    return {"files": len(names), "additions": additions, "deletions": deletions, "paths": names}


def parse_conflicts(output: str) -> list[str]:
    result = []
    for line in output.splitlines():
        match = re.search(r"CONFLICT \([^)]*\): .* in (.+)$", line)
        if match:
            result.append(match.group(1).strip())
    return sorted(set(result))


def merge_tree(repo: Path, base: str, upstream: str) -> tuple[str | None, str, list[str]]:
    result = git(repo, "merge-tree", "--write-tree", base, upstream, check=False)
    lines = result.stdout.splitlines()
    tree = lines[0].strip() if lines and HEX40.fullmatch(lines[0].strip()) else None
    output = result.stdout + result.stderr
    conflicts = parse_conflicts(output)
    if result.returncode not in (0, 1):
        raise SyncError(f"git merge-tree failed: {output.strip()}")
    return tree, output, conflicts


def state_sha(repo: Path, base: str) -> dict:
    state = load_json_at(repo, base, STATE_PATH)
    if state.get("schema_version") != 1:
        raise SyncError("unsupported upstream state schema")
    if state.get("upstream_repository") != "coolsnowwolf/lede" or state.get("upstream_branch") != "master":
        raise SyncError("upstream repository or branch differs from the confirmed state; history review is required")
    for field in ("upstream_sha", "fork_base_sha"):
        value = str(state.get(field, ""))
        if not HEX40.fullmatch(value):
            raise SyncError(f"upstream state field {field} must be a full commit SHA")
        state[field] = value.lower()
    return state


def validate_history(repo: Path, base: str, upstream: str, state: dict) -> tuple[str, dict]:
    if not is_ancestor(repo, state["upstream_sha"], upstream):
        raise SyncError("confirmed upstream SHA is not an ancestor of the fetched upstream; possible history rewrite")
    if not is_ancestor(repo, state["upstream_sha"], base):
        raise SyncError("confirmed upstream SHA is not an ancestor of the fork base; history review is required")
    if not is_ancestor(repo, state["fork_base_sha"], base):
        raise SyncError("confirmed fork base is not an ancestor of the current base; possible base rewrite")
    bases = git(repo, "merge-base", "--all", base, upstream).stdout.splitlines()
    if len(bases) != 1:
        raise SyncError(f"expected exactly one merge-base, found {len(bases)}")
    merge_base = bases[0].lower()
    return merge_base, {
        "recorded_upstream_is_ancestor": True,
        "recorded_fork_base_is_ancestor": True,
        "merge_base_count": 1,
    }


def conflict_report(repo: Path, merge_base: str, base: str, upstream: str,
                    conflicts: list[str], merge_output: str) -> list[dict]:
    reports = []
    for path in conflicts:
        reports.append({
            "path": path,
            "merge_base_blob": blob_sha(repo, merge_base, path),
            "base_blob": blob_sha(repo, base, path),
            "upstream_blob": blob_sha(repo, upstream, path),
        })
    return reports


def write_candidate_files(repo: Path, base: str, upstream: str, merge_base: str,
                          merge_sha: str, tree: str, workflow_sha: str,
                          sync_id: str, feed_lock: dict) -> tuple[str, dict]:
    lock_bytes = (json.dumps(feed_lock, indent=2, ensure_ascii=False) + "\n").encode()
    lock_digest = hashlib.sha256(
        json.dumps(feed_lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    state = {
        "schema_version": 1,
        "upstream_repository": "coolsnowwolf/lede",
        "upstream_branch": "master",
        "upstream_sha": upstream,
        "fork_base_sha": base,
    }
    manifest = {
        "schema_version": 1,
        "base_sha": base,
        "upstream_sha": upstream,
        "merge_base_sha": merge_base,
        "workflow_sha": workflow_sha,
        "feed_lock_sha256": lock_digest,
        "sync_id": sync_id,
        "merge_commit_sha": merge_sha,
    }
    payloads = {
        STATE_PATH: (json.dumps(state, indent=2) + "\n").encode(),
        LOCK_PATH: lock_bytes,
        MANIFEST_PATH: (json.dumps(manifest, indent=2) + "\n").encode(),
    }
    with tempfile.TemporaryDirectory(prefix="lede-index-") as temp:
        index = Path(temp) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        git(repo, "read-tree", tree, env=env)
        for path, content in payloads.items():
            object_id = git(repo, "hash-object", "-w", "--stdin", input_data=content.decode()).stdout.strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"100644,{object_id},{path}", env=env)
        config_tree = git(repo, "write-tree", env=env).stdout.strip()
    base_date = git(repo, "show", "-s", "--format=%cI", base).stdout.strip()
    identity = {
        "GIT_AUTHOR_NAME": "Upstream Sync",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Upstream Sync",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_AUTHOR_DATE": base_date,
        "GIT_COMMITTER_DATE": base_date,
    }
    final_sha = git(
        repo, "commit-tree", config_tree, "-p", merge_sha,
        input_data=f"Prepare upstream candidate {upstream[:12]}\n", env=identity,
    ).stdout.strip()
    return final_sha, manifest


def generate_lock(repo: Path, merge_sha: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="lede-lock-source-") as temporary:
        root = Path(temporary)
        feeds_path = root / "feeds.conf.default"
        feeds_path.write_text(blob_text(repo, merge_sha, "feeds.conf.default"))
        patch_path = root / "patches/dockerd/100-skip-host-nested-binaries.patch"
        patch_path.parent.mkdir(parents=True)
        patch_path.write_bytes(git(repo, "show", f"{merge_sha}:patches/dockerd/100-skip-host-nested-binaries.patch").stdout.encode())
        output_path = root / LOCK_PATH
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/feed-lock.py"), "create", "--root", str(root),
             "--feeds-conf", str(feeds_path), "--output", str(output_path)],
            check=True,
        )
        return json.loads(output_path.read_text())


def verify_existing(repo: Path, candidate: str, base: str, upstream: str,
                    merge_base: str, workflow_sha: str, sync_id: str) -> tuple[str, dict]:
    reject_conflict_markers(repo, candidate)
    manifest = load_json_at(repo, candidate, MANIFEST_PATH)
    expected = {
        "base_sha": base,
        "upstream_sha": upstream,
        "merge_base_sha": merge_base,
        "workflow_sha": workflow_sha,
        "sync_id": sync_id,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise SyncError(f"existing candidate identity mismatch for {key}; preserve it and create a new reviewed branch")
    if not is_ancestor(repo, base, candidate) or not is_ancestor(repo, upstream, candidate):
        raise SyncError("existing candidate no longer contains both pinned B and U")
    pinned_merge = find_pinned_merge(repo, candidate, base, upstream)
    if not pinned_merge:
        raise SyncError("existing candidate does not preserve a two-parent B/U merge commit")
    if manifest.get("merge_commit_sha") and manifest["merge_commit_sha"].lower() != pinned_merge:
        raise SyncError("candidate manifest merge SHA differs from its preserved B/U merge commit")
    lock = load_json_at(repo, candidate, LOCK_PATH)
    canonical = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    lock_digest = hashlib.sha256(canonical.encode()).hexdigest()
    if manifest.get("feed_lock_sha256") != lock_digest:
        raise SyncError("existing candidate feed lock changed; old validation is invalid")
    state = load_json_at(repo, candidate, STATE_PATH)
    if state.get("upstream_sha") != upstream or state.get("fork_base_sha") != base:
        raise SyncError("existing candidate state does not match its pinned B/U identity")
    manifest_commits = git(repo, "log", "--first-parent", "-n", "1", "--format=%H", candidate, "--", MANIFEST_PATH).stdout.splitlines()
    manifest_commit = manifest_commits[0] if manifest_commits else candidate
    manifest["candidate_modified"] = candidate != manifest_commit
    return lock_digest, manifest


def has_pinned_merge(repo: Path, candidate: str, base: str, upstream: str) -> bool:
    for commit in git(repo, "rev-list", "--first-parent", candidate).stdout.splitlines():
        parents = git(repo, "show", "-s", "--format=%P", commit).stdout.split()
        if parents == [base, upstream]:
            return True
    return False


def find_pinned_merge(repo: Path, candidate: str, base: str, upstream: str) -> str | None:
    for commit in git(repo, "rev-list", "--first-parent", candidate).stdout.splitlines():
        parents = git(repo, "show", "-s", "--format=%P", commit).stdout.split()
        if parents == [base, upstream]:
            return commit.lower()
    return None


def reject_conflict_markers(repo: Path, candidate: str) -> None:
    result = git(repo, "grep", "-n", "-E", "^(<<<<<<< |>>>>>>> )", candidate, "--", ".", check=False)
    if result.returncode == 0:
        examples = "; ".join(result.stdout.splitlines()[:5])
        raise SyncError(f"candidate contains unresolved conflict markers: {examples}")
    if result.returncode != 1:
        raise SyncError("could not scan candidate for unresolved conflict markers")


def register_candidate(repo: Path, base_ref: str, upstream_ref: str,
                       workflow_ref: str, sync_id: str, candidate_ref: str,
                       output: Path | None, dry_run: bool, generate_feeds: bool) -> dict:
    base = resolve_commit(repo, base_ref, "base SHA")
    upstream = resolve_commit(repo, upstream_ref, "upstream SHA")
    workflow_sha = resolve_commit(repo, workflow_ref, "workflow SHA")
    candidate = resolve_commit(repo, candidate_ref, "manual candidate")
    state = state_sha(repo, base)
    merge_base, history = validate_history(repo, base, upstream, state)
    reject_conflict_markers(repo, candidate)
    if not is_ancestor(repo, base, candidate) or not is_ancestor(repo, upstream, candidate):
        raise SyncError("manual candidate must contain both pinned B and U")
    pinned_merge = find_pinned_merge(repo, candidate, base, upstream)
    if not pinned_merge:
        raise SyncError("manual candidate must preserve a two-parent merge commit with B and U")

    existing_manifest = blob_sha(repo, candidate, MANIFEST_PATH)
    if existing_manifest:
        lock_digest, manifest = verify_existing(
            repo, candidate, base, upstream, merge_base, workflow_sha, sync_id,
        )
        status = "candidate_reused"
        final = candidate
    else:
        if any(blob_sha(repo, candidate, path) != blob_sha(repo, base, path)
               for path in (STATE_PATH, LOCK_PATH)):
            raise SyncError("manual candidate already changes upstream state or feed lock without a registration manifest")
        lock = generate_lock(repo, candidate) if generate_feeds else load_json_at(repo, base, LOCK_PATH)
        tree = git(repo, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
        final, manifest = write_candidate_files(
            repo, base, upstream, merge_base, pinned_merge, tree,
            workflow_sha, sync_id, lock,
        )
        lock_digest = manifest["feed_lock_sha256"]
        status = "candidate_registered"

    report = {
        "schema_version": 1,
        "status": status,
        "base_sha": base,
        "upstream_sha": upstream,
        "merge_base_sha": merge_base,
        "workflow_sha": workflow_sha,
        "sync_id": sync_id,
        "candidate_sha": final,
        "candidate_branch": candidate_branch(base, upstream, workflow_sha),
        "registered_from_sha": candidate,
        "feed_lock_sha256": lock_digest,
        "manifest": manifest,
        "history": history,
        "dry_run": dry_run,
        "diffs": {
            "merge_base_to_base": diff_summary(repo, merge_base, base),
            "merge_base_to_upstream": diff_summary(repo, merge_base, upstream),
            "base_to_candidate": diff_summary(repo, base, final),
            "upstream_to_candidate": diff_summary(repo, upstream, final),
        },
        "platform_impact": plan_for_paths(diff_paths(repo, base, final)),
    }
    tree, merge_output, conflicts = merge_tree(repo, base, upstream)
    if conflicts:
        report["resolved_previous_conflicts"] = conflict_report(repo, merge_base, base, upstream, conflicts, merge_output)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
    return report


def make_report(repo: Path, base_ref: str, upstream_ref: str, workflow_ref: str,
                sync_id: str, output: Path | None, dry_run: bool,
                existing_candidate_ref: str | None, generate_feeds: bool) -> dict:
    base = resolve_commit(repo, base_ref, "base SHA")
    upstream = resolve_commit(repo, upstream_ref, "upstream SHA")
    workflow_sha = resolve_commit(repo, workflow_ref, "workflow SHA")
    state = state_sha(repo, base)
    merge_base, history = validate_history(repo, base, upstream, state)
    report = {
        "schema_version": 1,
        "status": "pending",
        "base_sha": base,
        "upstream_sha": upstream,
        "merge_base_sha": merge_base,
        "workflow_sha": workflow_sha,
        "sync_id": sync_id,
        "history": history,
        "diffs": {
            "merge_base_to_base": diff_summary(repo, merge_base, base),
            "merge_base_to_upstream": diff_summary(repo, merge_base, upstream),
        },
        "platform_impact": plan_for_paths(diff_paths(repo, base, upstream)),
        "candidate_branch": candidate_branch(base, upstream, workflow_sha),
        "dry_run": dry_run,
    }
    if is_ancestor(repo, upstream, base):
        report["status"] = "no_updates"
        report["message"] = "the pinned upstream SHA is already an ancestor of the fork base"
    else:
        tree, output_text, conflicts = merge_tree(repo, base, upstream)
        if existing_candidate_ref:
            candidate = resolve_commit(repo, existing_candidate_ref, "existing candidate")
            lock_digest, manifest = verify_existing(
                repo, candidate, base, upstream, merge_base, workflow_sha, sync_id,
            )
            report["status"] = "candidate_reused"
            report["candidate_sha"] = candidate
            report["candidate_modified"] = manifest["candidate_modified"]
            report["feed_lock_sha256"] = lock_digest
            report["manifest"] = manifest
            if conflicts:
                report["previous_merge_conflicts"] = conflict_report(repo, merge_base, base, upstream, conflicts, output_text)
            report["diffs"]["base_to_candidate"] = diff_summary(repo, base, candidate)
            report["diffs"]["upstream_to_candidate"] = diff_summary(repo, upstream, candidate)
            report["platform_impact"] = plan_for_paths(diff_paths(repo, base, candidate))
        elif conflicts:
            report["status"] = "needs_resolution"
            report["conflicts"] = conflict_report(repo, merge_base, base, upstream, conflicts, output_text)
            report["merge_tree_output"] = output_text
            report["reproduce"] = f"git checkout --detach {base} && git merge --no-commit {upstream}"
        elif not tree:
            raise SyncError("merge-tree returned no tree without listing a conflict")
        else:
            parent_tree = tree
            base_date = git(repo, "show", "-s", "--format=%cI", base).stdout.strip()
            identity = {
                "GIT_AUTHOR_NAME": "Upstream Sync",
                "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "Upstream Sync",
                "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_AUTHOR_DATE": base_date,
                "GIT_COMMITTER_DATE": base_date,
            }
            merge_sha = git(
                repo, "commit-tree", parent_tree, "-p", base, "-p", upstream,
                input_data=f"Merge upstream {upstream[:12]}\n", env=identity,
            ).stdout.strip()
            if generate_feeds:
                lock = generate_lock(repo, merge_sha)
            else:
                lock = load_json_at(repo, base, LOCK_PATH)
            final_sha, manifest = write_candidate_files(
                repo, base, upstream, merge_base, merge_sha, parent_tree,
                workflow_sha, sync_id, lock,
            )
            report["status"] = "candidate_ready"
            report["candidate_sha"] = final_sha
            report["merge_commit_sha"] = merge_sha
            report["feed_lock_sha256"] = manifest["feed_lock_sha256"]
            report["manifest"] = manifest
            report["diffs"]["base_to_candidate"] = diff_summary(repo, base, final_sha)
            report["diffs"]["upstream_to_candidate"] = diff_summary(repo, upstream, final_sha)
            report["platform_impact"] = plan_for_paths(diff_paths(repo, base, final_sha))
            report["candidate_branch"] = candidate_branch(base, upstream, workflow_sha)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "report", "register"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--repo", type=Path, default=ROOT)
        sub.add_argument("--base", required=True, help="fixed base ref or full commit SHA")
        sub.add_argument("--upstream", required=True, help="fixed upstream ref or full commit SHA")
        sub.add_argument("--workflow-sha", required=True, help="trusted controller commit W")
        sub.add_argument("--sync-id", required=True, help="unique workflow dispatch identifier")
        sub.add_argument("--output", type=Path)
        sub.add_argument(
            "--candidate-ref", required=command == "register",
            help="existing candidate to validate and reuse; required for register",
        )
        sub.add_argument("--dry-run", action="store_true", help="write report and objects only; never update refs")
        sub.add_argument("--no-lock-refresh", action="store_true", help="reuse the base lock when testing")
    args = parser.parse_args()
    try:
        if args.command == "register":
            report = register_candidate(
                args.repo.resolve(), args.base, args.upstream, args.workflow_sha,
                args.sync_id, args.candidate_ref, args.output, args.dry_run,
                not args.no_lock_refresh,
            )
        else:
            report = make_report(
                args.repo.resolve(), args.base, args.upstream, args.workflow_sha,
                args.sync_id, args.output, args.dry_run, args.candidate_ref,
                args.command == "prepare" and not args.no_lock_refresh,
            )
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0 if report["status"] in ("no_updates", "candidate_ready", "candidate_reused", "candidate_registered", "needs_resolution") else 1
    except (SyncError, CandidateSelectionError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        if getattr(args, "output", None):
            message = (f"external command failed with exit status {exc.returncode}"
                       if isinstance(exc, subprocess.CalledProcessError) else str(exc))
            history_problem = any(token in message.lower() for token in (
                "ancestor", "merge-base", "history", "rewrite", "upstream repository",
            ))
            failure = {
                "schema_version": 1,
                "status": "history_review_required" if history_problem else "infra_failed",
                "base_ref": getattr(args, "base", None),
                "upstream_ref": getattr(args, "upstream", None),
                "workflow_sha": getattr(args, "workflow_sha", None),
                "error": message,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(failure, indent=2, ensure_ascii=False) + "\n")
        print(f"upstream-sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
