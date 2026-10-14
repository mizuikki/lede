#!/usr/bin/env python3
"""Validate an exact build attempt and publish the upstream/ready status."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import quote, urlparse
import zipfile

from platform_impact import plan_for_paths
from upstream_sync_control import (
    CandidateSelectionError,
    candidate_branch_from_sync_id,
    candidate_control_ref,
    candidate_sync_id_from_branch,
    validate_candidate_manifest,
)


REQUIRED_JOBS = ("validate_source", "build_openwrt")
CONTEXT = "upstream/ready"
SYNC_ID_RE = re.compile(r"upstream-[0-9a-f]{12}-[0-9a-f]{12}-w[0-9a-f]{12}")
PR_TITLE_RE = re.compile(r"\bpr-(\d+)\b", re.IGNORECASE)
EVIDENCE_RE = re.compile(r"<!--\s*upstream-sync-evidence:(\{.*?\})\s*-->", re.S)
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ResultError(RuntimeError):
    pass


def canonical_digest(data: dict) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def summarize_jobs(jobs: list[dict]) -> dict[str, str]:
    summaries: dict[str, str] = {}
    for job in jobs:
        name = job.get("name") or job.get("display_title")
        if name:
            summaries[str(name)] = str(job.get("conclusion") or job.get("status") or "missing")
    return summaries


def pull_request_numbers(run: dict) -> set[int]:
    refs = run.get("pull_requests") or []
    return {item["number"] for item in refs
            if isinstance(item, dict) and isinstance(item.get("number"), int)}


def evidence_result(impact_plan: dict | None, comments: list[dict] | None,
                    review_states: dict[str, object] | None, pull_request_author: str | None,
                    candidate_sha: str | None,
                    reviewed_head_sha: str | None = None) -> tuple[list[str], list[str], list[dict]]:
    """Return missing and failed evidence checks from approved non-author comments."""
    if impact_plan is None:
        return [], ["platform impact plan is unavailable"], []
    platforms = impact_plan.get("platforms")
    if not isinstance(platforms, dict):
        return [], ["platform impact plan is invalid"], []
    required = {(str(platform), str(check))
                for platform, checks in platforms.items()
                if isinstance(checks, list)
                for check in checks}
    states = {str(user).lower(): state for user, state in (review_states or {}).items()}
    latest: dict[tuple[str, str, str], tuple[int, dict]] = {}
    failures: list[str] = []
    for order, comment in enumerate(comments or []):
        user = (comment.get("user") or {}).get("login")
        if not user or str(user).lower() == str(pull_request_author or "").lower():
            continue
        reviewer = states.get(str(user).lower())
        if isinstance(reviewer, dict):
            if str(reviewer.get("state", "")).upper() != "APPROVED":
                continue
            if (reviewed_head_sha and str(reviewer.get("commit_id", "")).lower()
                    != reviewed_head_sha.lower()):
                continue
        elif str(reviewer or "").upper() != "APPROVED" or reviewed_head_sha:
            continue
        for match in EVIDENCE_RE.finditer(str(comment.get("body") or "")):
            try:
                item = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            platform, check = str(item.get("platform", "")), str(item.get("check", ""))
            key = (platform, check)
            if key not in required:
                continue
            sha = str(item.get("candidate_sha", "")).lower()
            if not HEX40.fullmatch(sha) or sha != str(candidate_sha or "").lower():
                continue
            result = str(item.get("result", "")).lower()
            url = str(item.get("evidence_url", ""))
            digest = str(item.get("sha256", "")).lower()
            parsed = urlparse(url)
            if result not in ("pass", "fail"):
                failures.append(f"{platform}/{check} evidence has an invalid result")
                continue
            if parsed.scheme != "https" or not parsed.hostname or not HEX64.fullmatch(digest):
                failures.append(f"{platform}/{check} evidence must include an HTTPS URL and SHA-256")
                continue
            storage_key = (str(user).lower(), platform, check)
            latest[storage_key] = (order, {"result": result, "evidence_url": url,
                                           "sha256": digest, "reviewer": str(user)})

    accepted: dict[tuple[str, str], list[dict]] = {}
    for (_, platform, check), (_, item) in latest.items():
        if item["result"] == "fail":
            failures.append(f"{platform}/{check} was reported failed")
        else:
            accepted.setdefault((platform, check), []).append(item)
    missing = [f"{platform}/{check}" for platform, check in sorted(required)
               if (platform, check) not in accepted]
    return missing, failures, [item for values in accepted.values() for item in values]


def is_superseded_run(run: dict, newer_runs: list[dict]) -> bool:
    """Return true when a later dispatch/build covers the same logical candidate or PR."""
    run_id = run.get("id")
    workflow_id = run.get("workflow_id")
    event = run.get("event")
    if not isinstance(run_id, int) or not isinstance(workflow_id, int):
        return False

    if event == "workflow_dispatch":
        title = str(run.get("display_title") or run.get("name") or "").lower()
        sync_match = SYNC_ID_RE.search(title)
        source_shas = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", title))
        pr_match = PR_TITLE_RE.search(title)
        if not source_shas:
            return False
        source_sha = next(iter(source_shas))
        for candidate in newer_runs:
            other_title = str(candidate.get("display_title") or candidate.get("name") or "").lower()
            other_sync = SYNC_ID_RE.search(other_title)
            other_pr = PR_TITLE_RE.search(other_title)
            other_shas = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", other_title))
            same_identity = (
                (sync_match and other_sync and other_sync.group(0) == sync_match.group(0))
                or (pr_match and other_pr and other_pr.group(1) == pr_match.group(1))
            )
            if (same_identity and candidate.get("id", 0) > run_id
                    and candidate.get("workflow_id") == workflow_id
                    and candidate.get("event") == event
                    and candidate.get("head_branch") == run.get("head_branch")
                    and str(candidate.get("head_sha", "")).lower() == str(run.get("head_sha", "")).lower()
                    and source_sha in other_shas):
                return True
        return False

    if event == "pull_request":
        branch = run.get("head_branch")
        head_repository = (run.get("head_repository") or {}).get("full_name")
        numbers = pull_request_numbers(run)
        for candidate in newer_runs:
            if not (candidate.get("id", 0) > run_id
                    and candidate.get("workflow_id") == workflow_id
                    and candidate.get("event") == event
                    and candidate.get("head_branch") == branch
                    and (candidate.get("head_repository") or {}).get("full_name") == head_repository):
                continue
            other_numbers = pull_request_numbers(candidate)
            if not numbers or not other_numbers or numbers & other_numbers:
                return True
    return False


def result_run_list(repository: str, workflow_id: int, event: str) -> list[dict]:
    pages = gh_json(
        f"repos/{repository}/actions/workflows/{workflow_id}/runs?event={quote(event, safe='')}&per_page=100",
        "--paginate", "--slurp",
    )
    if isinstance(pages, dict):
        pages = [pages]
    if not isinstance(pages, list):
        raise ResultError("workflow run list returned an invalid result")
    runs = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs", []), list):
            raise ResultError("workflow run list returned an invalid page")
        runs.extend(page.get("workflow_runs", []))
    return runs


def evaluate(event: dict, run: dict, jobs: list[dict], metadata: dict | None,
             lock: dict | None, candidate_manifest: dict | None,
             pull_request: dict | None, current_default_sha: str | None,
             artifact_present: bool, expected_workflow_id: int,
             default_branch: str, expected_repository: str | None = None,
             impact_plan: dict | None = None,
             workflow_source_matches_base: bool | None = None,
             merge_parents_match: bool | None = None,
             candidate_merge_chain_matches: bool | None = None,
             evidence_comments: list[dict] | None = None,
             review_states: dict[str, object] | None = None) -> dict:
    workflow_run = event.get("workflow_run", event)
    run_id = int(workflow_run.get("id", -1))
    attempt = int(workflow_run.get("run_attempt", 0))
    errors: list[str] = []
    missing_evidence: list[str] = []
    accepted_evidence: list[dict] = []
    target_sha = None
    mode = None
    sync_id = None

    current_attempt = int(run.get("run_attempt", 0) or 0)
    if run.get("id") == run_id and 0 < attempt < current_attempt:
        return {
            "schema_version": 1,
            "success": True,
            "state": "neutral",
            "context": CONTEXT,
            "target_sha": None,
            "mode": "stale_attempt_ignored",
            "sync_id": None,
            "run_id": run_id,
            "run_attempt": attempt,
            "required_jobs": summarize_jobs(jobs),
            "errors": [],
        }

    if (run.get("event") == "pull_request" and (
            str(run.get("head_branch", "")).startswith("sync/upstream-")
            or (pull_request is not None
                and str(pull_request.get("head", {}).get("ref", "")).startswith("sync/upstream-")))):
        return {
            "schema_version": 1,
            "success": True,
            "state": "neutral",
            "context": CONTEXT,
            "target_sha": None,
            "mode": "candidate_pull_request_preview_ignored",
            "sync_id": None,
            "run_id": run_id,
            "run_attempt": attempt,
            "required_jobs": summarize_jobs(jobs),
            "errors": [],
        }

    if run.get("event") == "workflow_dispatch" and metadata is not None:
        if not metadata.get("sync_id") and not metadata.get("pull_request"):
            return {
                "schema_version": 1,
                "success": True,
                "state": "neutral",
                "context": CONTEXT,
                "target_sha": None,
                "mode": "manual_build_ignored",
                "sync_id": None,
                "run_id": run_id,
                "run_attempt": attempt,
                "required_jobs": summarize_jobs(jobs),
                "errors": [],
            }

    if run.get("id") != run_id or run.get("run_attempt") != attempt:
        errors.append("run ID or attempt does not match the workflow_run event")
    if workflow_run.get("head_sha") and str(workflow_run.get("head_sha")).lower() != str(run.get("head_sha", "")).lower():
        errors.append("workflow_run event SHA differs from the exact API run")
    if run.get("workflow_id") != expected_workflow_id:
        errors.append("run belongs to a different workflow")
    run_path = str(run.get("path", ""))
    if run_path and run_path.split("@", 1)[0] != ".github/workflows/openwrt-ci.yml":
        errors.append("run path differs from the reviewed OpenWrt-CI workflow file")
    if run.get("status") != "completed":
        errors.append("workflow run has not completed")
    if run.get("conclusion") != "success":
        errors.append(f"workflow conclusion is {run.get('conclusion') or 'missing'}")

    job_results = summarize_jobs(jobs)
    for required in REQUIRED_JOBS:
        if job_results.get(required) != "success":
            errors.append(f"required job {required} concluded {job_results.get(required, 'missing')}")

    if not artifact_present or metadata is None:
        errors.append("build artifact is missing or expired")
    if metadata is not None:
        if metadata.get("workflow_file") != ".github/workflows/openwrt-ci.yml":
            errors.append("artifact came from an unexpected workflow file")
        if metadata.get("profile") != "x86_64-passwall-docker":
            errors.append("artifact profile differs from the required build profile")
        if expected_repository and metadata.get("repository") != expected_repository:
            errors.append("artifact repository differs from the workflow repository")
        if str(metadata.get("run_id")) != str(run_id):
            errors.append("artifact run ID does not match this workflow run")
        if str(metadata.get("run_attempt")) != str(attempt):
            errors.append("artifact run attempt does not match this workflow attempt")
        if metadata.get("event") != run.get("event"):
            errors.append("artifact event differs from this workflow run")
        if metadata.get("workflow_sha", "").lower() != str(run.get("head_sha", "")).lower() and run.get("event") == "workflow_dispatch":
            errors.append("artifact workflow SHA differs from the dispatched workflow commit")
        if metadata.get("required_jobs") != list(REQUIRED_JOBS):
            errors.append("artifact required-job declaration differs from controller policy")
        if metadata.get("validation", {}).get("source_sha_match") is not True:
            errors.append("artifact source SHA validation did not pass")
        if metadata.get("validation", {}).get("feed_lock_match") is not True:
            errors.append("artifact feed lock validation did not pass")
        if not lock or metadata.get("feed_lock_sha256") != canonical_digest(lock):
            errors.append("artifact feed lock is missing or its digest differs")
        if metadata.get("validation", {}).get("feed_revisions_collected") is not True:
            errors.append("artifact does not confirm collection of actual feed revisions")
        expected_revisions = [
            {"name": feed.get("name"), "revision": str(feed.get("revision", "")).lower()}
            for feed in (lock or {}).get("feeds", [])
        ]
        actual_revisions = metadata.get("feed_revisions")
        if actual_revisions != expected_revisions:
            errors.append("actual feed revisions do not match the pinned feed lock")

    run_event = run.get("event")
    title = str(run.get("display_title") or run.get("name") or "")
    title_sync = SYNC_ID_RE.search(title)
    title_pr = PR_TITLE_RE.search(title)
    if run_event == "workflow_dispatch" and (
            (metadata is not None and metadata.get("sync_id")) or title_sync):
        mode = "candidate"
        sync_id = metadata.get("sync_id") if metadata else title_sync.group(0)
        candidate = (metadata or {}).get("candidate") or {}
        target_sha = (metadata or {}).get("source_sha") or (pull_request or {}).get("head", {}).get("sha")
        if metadata is None:
            errors.append("candidate build metadata is missing or expired")
        else:
            try:
                expected_ref = candidate_control_ref(str(metadata.get("workflow_sha", "")))
            except CandidateSelectionError:
                expected_ref = ""
            if run.get("head_branch") != expected_ref:
                errors.append("run used an unexpected control ref")
            if str(run.get("head_sha", "")).lower() != str(metadata.get("workflow_sha", "")).lower():
                errors.append("run head SHA differs from the pinned workflow SHA")
            if str(metadata.get("dispatch_sha", "")).lower() != str(run.get("head_sha", "")).lower():
                errors.append("artifact dispatch SHA differs from the workflow run SHA")
            if metadata.get("validation", {}).get("candidate_identity_match") is not True:
                errors.append("candidate identity validation did not pass")
            if (not sync_id or sync_id.lower() not in title.lower()
                    or str(metadata.get("source_sha", "")).lower() not in title.lower()):
                errors.append("workflow run title does not identify this sync attempt and source SHA")
            if candidate_manifest is None:
                errors.append("current candidate manifest is unavailable")
            else:
                for key in ("base_sha", "upstream_sha", "merge_base_sha", "workflow_sha", "merge_commit_sha", "feed_lock_sha256", "sync_id"):
                    if candidate.get(key) != candidate_manifest.get(key):
                        errors.append(f"candidate {key} differs from the artifact")
                if candidate.get("sync_id") != sync_id:
                    errors.append("candidate sync_id differs from the workflow run")
                if metadata.get("feed_lock_sha256") != candidate_manifest.get("feed_lock_sha256"):
                    errors.append("candidate feed lock differs from the artifact lock")
        if pull_request is None:
            errors.append("open candidate pull request is unavailable")
        else:
            head = pull_request.get("head", {})
            base = pull_request.get("base", {})
            if head.get("sha", "").lower() != str(metadata.get("source_sha", "")).lower():
                errors.append("candidate HEAD changed after this build was dispatched")
            if base.get("ref") != default_branch:
                errors.append("candidate pull request targets a non-default branch")
            if base.get("sha", "").lower() != str(candidate.get("base_sha", "")).lower():
                errors.append("candidate pull request base differs from the pinned base SHA")
            if current_default_sha and current_default_sha.lower() != str(candidate.get("base_sha", "")).lower():
                errors.append("default branch advanced after candidate validation")
        if metadata is not None and metadata.get("platform_impact") != impact_plan:
            errors.append("artifact platform impact plan differs from the trusted path analysis")
        if candidate_merge_chain_matches is not True:
            errors.append("candidate no longer contains the exact two-parent B/U merge")
        missing_evidence, evidence_failures, accepted_evidence = evidence_result(
            impact_plan, evidence_comments, review_states,
            (pull_request or {}).get("user", {}).get("login"), target_sha,
            (pull_request or {}).get("head", {}).get("sha"),
        )
        errors.extend(evidence_failures)
    elif run_event == "workflow_dispatch" and (
            (metadata is not None and metadata.get("pull_request")) or title_pr):
        mode = "pull_request"
        if pull_request is None:
            errors.append("pull request metadata is unavailable")
        elif metadata is None:
            target_sha = pull_request.get("merge_commit_sha")
            errors.append("pull request build metadata is missing or expired")
        else:
            head = pull_request.get("head", {})
            base = pull_request.get("base", {})
            pr_meta = metadata.get("pull_request") or {}
            merge_sha = str(pull_request.get("merge_commit_sha") or "").lower()
            target_sha = merge_sha or run.get("head_sha")
            number = pull_request.get("number")
            workflow_sha = str(base.get("sha", "")).lower()
            try:
                expected_ref = candidate_control_ref(workflow_sha)
            except CandidateSelectionError:
                expected_ref = ""
            if str(head.get("ref", "")).startswith("sync/upstream-"):
                errors.append("upstream candidates require their fixed candidate dispatch")
            if pr_meta.get("number") != number:
                errors.append("artifact pull request number differs")
            if str(pr_meta.get("head_sha", "")).lower() != str(head.get("sha", "")).lower():
                errors.append("pull request HEAD changed after the build")
            if str(pr_meta.get("base_sha", "")).lower() != workflow_sha:
                errors.append("artifact trusted workflow SHA differs from the exact PR base SHA")
            if str(pr_meta.get("merge_sha", "")).lower() != merge_sha:
                errors.append("artifact pull request merge SHA differs from the current merge ref")
            if metadata.get("workflow_sha", "").lower() != workflow_sha:
                errors.append("artifact did not use the reviewed PR base as its control workflow")
            if str(metadata.get("source_sha", "")).lower() != merge_sha:
                errors.append("pull request build source differs from the current merge ref")
            if str(metadata.get("dispatch_sha", "")).lower() != workflow_sha:
                errors.append("pull request dispatch did not use its exact trusted base commit")
            if run.get("head_branch") != expected_ref or str(run.get("head_sha", "")).lower() != workflow_sha:
                errors.append("pull request build used an unexpected workflow control ref")
            title = str(run.get("display_title") or run.get("name") or "").lower()
            if f"pr-{number}" not in title or merge_sha not in title:
                errors.append("workflow run title does not identify the exact pull request merge SHA")
            run_pr = pull_request_numbers(run)
            if run_pr and run_pr != {number}:
                errors.append("workflow run is associated with a different pull request")
            if not HEX40.fullmatch(merge_sha):
                errors.append("pull request merge SHA is unavailable or abbreviated")
            if current_default_sha and current_default_sha.lower() != workflow_sha:
                errors.append("pull request base advanced after validation")
            if workflow_source_matches_base is not True:
                errors.append("pull request changed or removed the trusted OpenWrt-CI workflow")
            if merge_parents_match is not True:
                errors.append("pull request merge commit parents do not match the exact base and head")
            if metadata.get("platform_impact") != impact_plan:
                errors.append("artifact platform impact plan differs from the trusted path analysis")
            if base.get("ref") != default_branch:
                errors.append("upstream/ready only aggregates pull requests targeting the default branch")
            missing_evidence, evidence_failures, accepted_evidence = evidence_result(
                impact_plan, evidence_comments, review_states,
                pull_request.get("user", {}).get("login"), target_sha,
                head.get("sha"),
            )
            errors.extend(evidence_failures)
    elif run_event == "pull_request":
        mode = "pull_request_preview_ignored"
        return {
            "schema_version": 1, "success": True, "state": "neutral",
            "context": CONTEXT, "target_sha": None, "mode": mode,
            "sync_id": None, "run_id": run_id, "run_attempt": attempt,
            "required_jobs": job_results, "errors": [],
        }
    elif run_event == "workflow_dispatch":
        mode = "manual_build_ignored"
        return {
            "schema_version": 1, "success": True, "state": "neutral",
            "context": CONTEXT, "target_sha": None, "mode": mode,
            "sync_id": None, "run_id": run_id, "run_attempt": attempt,
            "required_jobs": job_results, "errors": [],
        }
    else:
        errors.append(f"unsupported workflow event: {run_event or 'missing'}")

    if target_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", str(target_sha)):
        errors.append("status target is not a full commit SHA")
    pending_errors = [f"platform evidence pending: {item}" for item in missing_evidence]
    state = "failure" if errors else "pending" if pending_errors else "success"
    return {
        "schema_version": 1,
        "success": state == "success",
        "state": state,
        "context": CONTEXT,
        "target_sha": target_sha,
        "mode": mode,
        "sync_id": sync_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "required_jobs": job_results,
        "errors": errors + pending_errors,
        "platform_impact": impact_plan,
        "accepted_platform_evidence": accepted_evidence,
    }


def gh_json(path: str, *flags: str) -> object:
    result = subprocess.run(["gh", "api", *flags, path], capture_output=True)
    if result.returncode:
        raise ResultError(result.stderr.decode(errors="replace").strip() or f"GitHub API GET failed: {path}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ResultError(f"GitHub API returned invalid JSON for {path}") from exc


def post_status(repository: str, target_sha: str, state: str,
                description: str, target_url: str | None = None) -> None:
    if not HEX40.fullmatch(str(target_sha)):
        raise ResultError("status target is not a full commit SHA")
    body = {"state": state, "context": CONTEXT, "description": description[:140]}
    if target_url:
        body["target_url"] = target_url
    subprocess.run(
        ["gh", "api", "--method", "POST", f"repos/{repository}/statuses/{target_sha}", "--input", "-"],
        input=json.dumps(body).encode(), check=True,
    )


def read_validation_artifact(payload: bytes) -> tuple[dict, dict]:
    expected = {"sync-validation.json", "feed-lock.json"}
    found: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for item in archive.infolist():
            relative = PurePosixPath(item.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in item.filename:
                raise ResultError("artifact contains a path outside its archive root")
            mode = item.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ResultError("artifact contains a symbolic link")
            name = relative.name
            if item.is_dir() or name not in expected:
                continue
            if name in found:
                raise ResultError(f"artifact contains multiple {name} files")
            if item.file_size > 5 * 1024 * 1024:
                raise ResultError(f"artifact {name} exceeds the validation metadata size limit")
            found[name] = archive.read(item)
    missing = expected - found.keys()
    if missing:
        raise ResultError("validation artifact is missing " + ", ".join(sorted(missing)))
    try:
        metadata = json.loads(found["sync-validation.json"])
        lock = json.loads(found["feed-lock.json"])
    except json.JSONDecodeError as exc:
        raise ResultError("validation artifact contains invalid JSON") from exc
    if not isinstance(metadata, dict) or not isinstance(lock, dict):
        raise ResultError("validation artifact JSON values must be objects")
    return metadata, lock


def flatten_api_pages(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        raise ResultError("GitHub API list returned an invalid result")
    items: list[dict] = []
    for page in value:
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        elif isinstance(page, dict):
            items.append(page)
        else:
            raise ResultError("GitHub API list returned an invalid page")
    return items


def compare_paths(repository: str, base_sha: str, head_sha: str) -> list[str]:
    comparison = gh_json(f"repos/{repository}/compare/{base_sha}...{head_sha}")
    files = comparison.get("files") if isinstance(comparison, dict) else None
    if not isinstance(files, list):
        raise ResultError("GitHub compare API did not return a complete changed-file list")
    if len(files) >= 300:
        raise ResultError("GitHub compare API file list may be truncated; platform plan is unresolved")
    paths = [item.get("filename") for item in files if isinstance(item, dict)]
    if len(paths) != len(files) or any(not isinstance(path, str) for path in paths):
        raise ResultError("GitHub compare API returned an invalid changed-file list")
    return paths


def content_blob_sha(repository: str, path: str, revision: str) -> str:
    content = gh_json(f"repos/{repository}/contents/{path}?ref={quote(revision, safe='')}")
    if not isinstance(content, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(content.get("sha", ""))):
        raise ResultError(f"GitHub contents API did not return the {path} blob SHA")
    return content["sha"]


def same_workflow_file(repository: str, base_sha: str, merge_sha: str) -> bool:
    path = ".github/workflows/openwrt-ci.yml"
    try:
        return content_blob_sha(repository, path, base_sha) == content_blob_sha(repository, path, merge_sha)
    except ResultError:
        return False


def exact_merge_parents(repository: str, merge_sha: str, base_sha: str, head_sha: str) -> bool:
    try:
        commit = gh_json(f"repos/{repository}/git/commits/{merge_sha}")
    except ResultError:
        return False
    parents = commit.get("parents", []) if isinstance(commit, dict) else []
    return [parent.get("sha", "").lower() for parent in parents if isinstance(parent, dict)] == [
        base_sha.lower(), head_sha.lower(),
    ]


def candidate_merge_chain(repository: str, candidate_sha: str,
                          base_sha: str, upstream_sha: str,
                          merge_commit_sha: str | None) -> bool:
    if merge_commit_sha and HEX40.fullmatch(merge_commit_sha.lower()):
        try:
            merge = gh_json(f"repos/{repository}/git/commits/{merge_commit_sha}")
            comparison = gh_json(
                f"repos/{repository}/compare/{merge_commit_sha}...{candidate_sha}"
            )
        except ResultError:
            return False
        parents = merge.get("parents", []) if isinstance(merge, dict) else []
        parent_shas = [parent.get("sha", "").lower() for parent in parents if isinstance(parent, dict)]
        return (parent_shas == [base_sha.lower(), upstream_sha.lower()]
                and comparison.get("merge_base_commit", {}).get("sha", "").lower() == merge_commit_sha.lower()
                and comparison.get("status") in ("ahead", "identical"))
    return False


def review_evidence(repository: str, number: int) -> tuple[list[dict], dict[str, dict]]:
    reviews = flatten_api_pages(gh_json(
        f"repos/{repository}/pulls/{number}/reviews?per_page=100", "--paginate", "--slurp",
    ))
    comments = flatten_api_pages(gh_json(
        f"repos/{repository}/issues/{number}/comments?per_page=100", "--paginate", "--slurp",
    ))
    ordered = sorted(reviews, key=lambda item: (str(item.get("submitted_at") or ""), int(item.get("id", 0) or 0)))
    states: dict[str, dict] = {}
    for review in ordered:
        user = (review.get("user") or {}).get("login")
        if user:
            states[str(user).lower()] = {
                "state": str(review.get("state") or "").upper(),
                "commit_id": str(review.get("commit_id") or "").lower(),
            }
    comments.sort(key=lambda item: (str(item.get("created_at") or ""), int(item.get("id", 0) or 0)))
    return comments, states


def run_title_source_sha(title: str) -> str | None:
    values = re.findall(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", title.lower())
    return values[-1] if values else None


def exact_candidate_pull_request(pulls: list[dict], repository: str,
                                 branch: str, source_sha: str) -> dict:
    """Select one same-repository PR whose branch and complete HEAD match the run."""
    if not HEX40.fullmatch(str(source_sha).lower()):
        raise ResultError("candidate run title does not contain a full source SHA")
    matches = [item for item in pulls
               if item.get("head", {}).get("ref") == branch
               and str(((item.get("head", {}).get("repo") or {}).get("full_name")) or "").lower()
               == repository.lower()]
    if len(matches) != 1:
        raise ResultError("the exact same-repository candidate pull request is unavailable or ambiguous")
    pull = matches[0]
    head_sha = str(pull.get("head", {}).get("sha", "")).lower()
    if not HEX40.fullmatch(head_sha) or head_sha != source_sha.lower():
        raise ResultError("candidate pull request HEAD is not the full SHA named by this build")
    return pull


def validate_candidate_pull_identity(pr: dict, repository: str, branch: str,
                                     manifest: dict, default_branch: str | None = None) -> None:
    try:
        validate_candidate_manifest(branch, manifest)
    except CandidateSelectionError as exc:
        raise ResultError(str(exc)) from exc
    head = pr.get("head", {})
    base = pr.get("base", {})
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo is None or str(head_repo).lower() != repository.lower():
        raise ResultError("candidate pull request is not from this repository")
    if (head.get("ref") != branch
            or str(base.get("sha", "")).lower() != manifest["base_sha"]):
        raise ResultError("candidate pull request branch or base differs from its fixed B/U/W identity")
    if default_branch and base.get("ref") != default_branch:
        raise ResultError("candidate pull request does not target the current default branch")


def exact_candidate_run(run: dict, workflow_id: int, sync_id: str,
                        source_sha: str, workflow_sha: str) -> bool:
    title = str(run.get("display_title") or run.get("name") or "")
    return (
        run.get("event") == "workflow_dispatch"
        and run.get("workflow_id") == workflow_id
        and run.get("head_branch") == candidate_control_ref(workflow_sha)
        and str(run.get("head_sha", "")).lower() == workflow_sha.lower()
        and title == f"OpenWrt {sync_id} {source_sha}"
    )


def run_gh() -> dict:
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    workflow = gh_json(f"repos/{repository}/actions/workflows/openwrt-ci.yml")
    workflow_id = workflow["id"]

    trigger = os.environ.get("GITHUB_EVENT_NAME", "workflow_run")
    pr = None
    manifest = None
    if trigger == "workflow_run":
        workflow_run = event.get("workflow_run", {})
        run_id = int(workflow_run["id"])
        attempt = int(workflow_run["run_attempt"])
        run = gh_json(f"repos/{repository}/actions/runs/{run_id}")
        if run.get("event") == "pull_request":
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "legacy_pull_request_preview_ignored", "errors": []}
        title = str(run.get("display_title") or run.get("name") or "")
        sync_match = SYNC_ID_RE.search(title)
        pr_match = PR_TITLE_RE.search(title)
        if sync_match:
            sync_id = sync_match.group(0)
            try:
                branch = candidate_branch_from_sync_id(sync_id)
            except CandidateSelectionError as exc:
                raise ResultError(str(exc)) from exc
            source_sha = run_title_source_sha(title)
            if title != f"OpenWrt {sync_id} {source_sha}":
                raise ResultError("candidate run title does not match the controller run-name format")
            owner = repository.split("/", 1)[0]
            if "/" not in repository:
                raise ResultError("repository must use owner/repository form")
            pulls = flatten_api_pages(gh_json(
                f"repos/{repository}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}&per_page=100",
                "--paginate", "--slurp",
            ))
            pr = exact_candidate_pull_request(pulls, repository, branch, source_sha or "")
            content = gh_json(f"repos/{repository}/contents/.github/upstream-candidate.json?ref={quote(branch, safe='')}")
            manifest = json.loads(base64.b64decode(content["content"]).decode())
            validate_candidate_pull_identity(pr, repository, branch, manifest)
        elif pr_match:
            pr = gh_json(f"repos/{repository}/pulls/{int(pr_match.group(1))}")
        else:
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "manual_build_ignored", "errors": []}
    elif trigger == "issue_comment":
        issue = event.get("issue", {})
        if not issue.get("pull_request") or not issue.get("number"):
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "non_pr_comment_ignored", "errors": []}
        pr = gh_json(f"repos/{repository}/pulls/{int(issue['number'])}")
        branch = pr.get("head", {}).get("ref", "")
        is_candidate = branch.startswith("sync/upstream-")
        if pr.get("state") != "open":
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "closed_pull_request_ignored", "errors": []}
        if is_candidate and pr.get("head", {}).get("repo", {}).get("full_name") != repository:
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "foreign_candidate_ignored", "errors": []}
        default_branch = event.get("repository", {}).get("default_branch")
        if not default_branch:
            default_branch = gh_json(f"repos/{repository}").get("default_branch")
        if not is_candidate and (pr.get("base", {}).get("ref") != default_branch or pr.get("draft")):
            return {"schema_version": 1, "success": True, "state": "neutral",
                    "context": CONTEXT, "target_sha": None,
                    "mode": "ineligible_pull_request_ignored", "errors": []}
        source_sha = str(pr.get("head", {}).get("sha", "")).lower() if is_candidate else str(pr.get("merge_commit_sha", "")).lower()
        if not HEX40.fullmatch(source_sha):
            raise ResultError("current pull request status target is unavailable")
        if is_candidate:
            if str((pr.get("head", {}).get("repo") or {}).get("full_name", "")).lower() != repository.lower():
                return {"schema_version": 1, "success": True, "state": "neutral",
                        "context": CONTEXT, "target_sha": None,
                        "mode": "foreign_candidate_ignored", "errors": []}
            try:
                candidate_sync_id_from_branch(branch)
            except CandidateSelectionError as exc:
                raise ResultError(str(exc)) from exc
        post_status(repository, source_sha, "pending",
                    "Revalidating the exact pull request build and platform evidence",
                    pr.get("html_url"))
        runs = result_run_list(repository, workflow_id, "workflow_dispatch")
        if is_candidate:
            content = gh_json(f"repos/{repository}/contents/.github/upstream-candidate.json?ref={quote(branch, safe='')}")
            manifest = json.loads(base64.b64decode(content["content"]).decode())
            sync_id = str(manifest.get("sync_id", ""))
            workflow_sha = str(manifest.get("workflow_sha", "")).lower()
            validate_candidate_pull_identity(pr, repository, branch, manifest, default_branch)
            if source_sha != str(pr.get("head", {}).get("sha", "")).lower():
                raise ResultError("candidate source SHA changed during comment aggregation")
            matches = [item for item in runs if exact_candidate_run(
                item, workflow_id, sync_id, source_sha, workflow_sha,
            )]
        else:
            workflow_sha = str(pr.get("base", {}).get("sha", "")).lower()
            matches = [item for item in runs
                       if f"pr-{pr['number']}" in str(item.get("display_title") or "").lower()
                       and source_sha in str(item.get("display_title") or "").lower()
                       and str(item.get("head_sha", "")).lower() == workflow_sha]
        if not matches:
            raise ResultError("no exact build run exists for this pull request head/base/merge SHA")
        selected = max(matches, key=lambda item: int(item.get("id", 0)))
        run_id = int(selected["id"])
        run = gh_json(f"repos/{repository}/actions/runs/{run_id}")
        attempt = int(run.get("run_attempt", 0))
        event = {"workflow_run": {"id": run_id, "run_attempt": attempt,
                                  "head_sha": run.get("head_sha")},
                 "repository": {"default_branch": default_branch}}
    else:
        raise ResultError(f"unsupported result trigger: {trigger}")

    jobs_response = gh_json(
        f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        "--paginate", "--slurp",
    )
    if isinstance(jobs_response, list):
        jobs = [job for page in jobs_response
                for job in (page if isinstance(page, list) else page.get("jobs", []))]
    else:
        jobs = jobs_response.get("jobs", [])

    default_branch = event.get("repository", {}).get("default_branch")
    if not default_branch:
        repo_info = gh_json(f"repos/{repository}")
        default_branch = repo_info["default_branch"]
    default_info = gh_json(f"repos/{repository}/branches/{default_branch}")
    current_default_sha = default_info["commit"]["sha"]

    artifact_list = gh_json(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
    artifacts = artifact_list.get("artifacts", [])
    metadata = None
    lock = None
    artifact_present = False
    sync_id = None
    expected_name = None
    expected_build_name = None

    title = str(run.get("display_title") or run.get("name") or "")
    sync_match = SYNC_ID_RE.search(title)
    pr_match = PR_TITLE_RE.search(title)
    if run.get("event") == "workflow_dispatch" and sync_match:
        sync_id = sync_match.group(0)
        identity = sync_id
        expected_name = f"openwrt-x86_64-passwall-docker-{identity}-validation-{run_id}-{attempt}"
        expected_build_name = f"openwrt-x86_64-passwall-docker-{identity}-{run_id}-{attempt}"
        if pr is None:
            try:
                branch = candidate_branch_from_sync_id(sync_id)
            except CandidateSelectionError as exc:
                raise ResultError(str(exc)) from exc
            source_sha = run_title_source_sha(title)
            if title != f"OpenWrt {sync_id} {source_sha}":
                raise ResultError("candidate run title does not match the controller run-name format")
            owner = repository.split("/", 1)[0]
            if "/" not in repository:
                raise ResultError("repository must use owner/repository form")
            pulls = flatten_api_pages(gh_json(
                f"repos/{repository}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}&per_page=100",
                "--paginate", "--slurp",
            ))
            pr = exact_candidate_pull_request(pulls, repository, branch, source_sha or "")
            content = gh_json(f"repos/{repository}/contents/.github/upstream-candidate.json?ref={quote(branch, safe='')}")
            manifest = json.loads(base64.b64decode(content["content"]).decode())
            validate_candidate_pull_identity(pr, repository, branch, manifest)
    elif run.get("event") == "workflow_dispatch" and pr_match:
        if pr is None:
            pr = gh_json(f"repos/{repository}/pulls/{int(pr_match.group(1))}")
        identity = f"pr-{pr['number']}"
        expected_name = f"openwrt-x86_64-passwall-docker-{identity}-validation-{run_id}-{attempt}"
        expected_build_name = f"openwrt-x86_64-passwall-docker-{identity}-{run_id}-{attempt}"

    artifact = next((item for item in artifacts if item.get("name") == expected_name), None) if expected_name else None
    build_artifact = next((item for item in artifacts if item.get("name") == expected_build_name), None) if expected_build_name else None
    if (artifact and not artifact.get("expired")
            and build_artifact and not build_artifact.get("expired")):
        artifact_present = True
        binary = subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"], capture_output=True)
        if binary.returncode:
            artifact_present = False
        else:
            metadata, lock = read_validation_artifact(binary.stdout)

    impact_plan = None
    workflow_match = None
    merge_match = None
    comments: list[dict] = []
    review_states: dict[str, object] = {}
    if pr is not None and run.get("event") == "workflow_dispatch":
        base_sha = (manifest or {}).get("base_sha") or pr.get("base", {}).get("sha")
        head_sha = pr.get("head", {}).get("sha")
        is_candidate_pr = str(pr.get("head", {}).get("ref", "")).startswith("sync/upstream-")
        merge_sha = head_sha if is_candidate_pr else pr.get("merge_commit_sha")
        if isinstance(base_sha, str) and isinstance(merge_sha, str) and merge_sha:
            impact_plan = plan_for_paths(compare_paths(repository, base_sha, merge_sha))
            if not is_candidate_pr:
                workflow_match = same_workflow_file(repository, base_sha, merge_sha)
                merge_match = exact_merge_parents(repository, merge_sha, base_sha, head_sha)
            else:
                merge_match = candidate_merge_chain(
                    repository, pr.get("head", {}).get("sha", ""),
                    (manifest or {}).get("base_sha", ""),
                    (manifest or {}).get("upstream_sha", ""),
                    (manifest or {}).get("merge_commit_sha"),
                )
                workflow_match = True
        comments, review_states = review_evidence(repository, int(pr["number"]))

    result = evaluate(
        event, run, jobs, metadata, lock, manifest, pr, current_default_sha,
        artifact_present, workflow_id, default_branch, repository,
        impact_plan, workflow_match,
        merge_match if pr and not str(pr.get("head", {}).get("ref", "")).startswith("sync/upstream-") else None,
        merge_match if pr and str(pr.get("head", {}).get("ref", "")).startswith("sync/upstream-") else None,
        comments, review_states,
    )
    if result["target_sha"]:
        matching_runs = result_run_list(repository, workflow_id, run.get("event", ""))
        if is_superseded_run(run, matching_runs):
            result.update({
                "success": True,
                "state": "neutral",
                "target_sha": None,
                "mode": "superseded_run_ignored",
                "errors": [],
            })
    result["target_url"] = run.get("html_url")
    if result["target_sha"]:
        description = "; ".join(result["errors"]) if result["errors"] else "All required source, build, lock, and platform checks passed"
        post_status(repository, result["target_sha"], result["state"], description, result["target_url"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evaluate", action="store_true", help="read the provided JSON files without calling GitHub")
    parser.add_argument("--event-json", type=Path)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--jobs-json", type=Path)
    parser.add_argument("--metadata-json", type=Path)
    parser.add_argument("--lock-json", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    parser.add_argument("--pull-request-json", type=Path)
    parser.add_argument("--impact-plan-json", type=Path)
    parser.add_argument("--evidence-json", type=Path)
    parser.add_argument("--workflow-source-matches-base", action="store_true")
    parser.add_argument("--merge-parents-match", action="store_true")
    parser.add_argument("--candidate-merge-chain-matches", action="store_true")
    parser.add_argument("--current-default-sha")
    parser.add_argument("--artifact-present", action="store_true")
    parser.add_argument("--workflow-id", type=int, default=1)
    parser.add_argument("--default-branch", default="master")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    try:
        if args.evaluate:
            event = json.loads(args.event_json.read_text())
            run = json.loads(args.run_json.read_text())
            jobs_data = json.loads(args.jobs_json.read_text())
            jobs = jobs_data.get("jobs", jobs_data) if isinstance(jobs_data, dict) else jobs_data
            read = lambda path: json.loads(path.read_text()) if path else None
            result = evaluate(
                event, run, jobs, read(args.metadata_json), read(args.lock_json),
                read(args.manifest_json), read(args.pull_request_json),
                args.current_default_sha, args.artifact_present,
                args.workflow_id, args.default_branch, args.repository,
                read(args.impact_plan_json),
                args.workflow_source_matches_base, args.merge_parents_match,
                args.candidate_merge_chain_matches,
                (read(args.evidence_json) or {}).get("comments", []),
                (read(args.evidence_json) or {}).get("review_states", {}),
            )
        else:
            result = run_gh()
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        sys.stdout.write(rendered)
        return 0 if result["success"] else 1
    except (ResultError, OSError, KeyError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"sync-result: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
