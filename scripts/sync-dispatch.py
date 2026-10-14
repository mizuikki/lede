#!/usr/bin/env python3
"""Create an immutable workflow-control tag and dispatch an exact build run."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


API_VERSION = "2026-03-10"
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


class DispatchError(RuntimeError):
    pass


def gh(args: list[str], *, input_data: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["gh", "api", *args], input=input_data, capture_output=True)
    if check and result.returncode:
        raise DispatchError(result.stderr.decode(errors="replace").strip() or "GitHub API request failed")
    return result


def parse_json(result: subprocess.CompletedProcess, label: str) -> dict:
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DispatchError(f"{label} returned no valid JSON") from exc
    if not isinstance(data, dict):
        raise DispatchError(f"{label} returned an invalid JSON object")
    return data


def dispatch_record(repository: str, workflow_file: str, workflow_sha: str,
                    source_sha: str, base_sha: str, upstream_sha: str,
                    merge_base_sha: str, feed_lock_sha256: str,
                    sync_id: str, tag: str, workflow_id: int,
                    run: dict, *, reused: bool, reuse_reason: str) -> dict:
    return {
        "schema_version": 1,
        "repository": repository,
        "workflow_id": workflow_id,
        "workflow_file": workflow_file,
        "workflow_sha": workflow_sha.lower(),
        "control_ref": f"refs/tags/{tag}",
        "sync_id": sync_id,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "source_sha": source_sha.lower(),
        "base_sha": base_sha.lower(),
        "upstream_sha": upstream_sha.lower(),
        "merge_base_sha": merge_base_sha.lower(),
        "feed_lock_sha256": feed_lock_sha256,
        "run_url": run.get("html_url"),
        "reused": reused,
        "reuse_reason": reuse_reason,
    }


def list_workflow_runs(repository: str, workflow_id: int, workflow_sha: str) -> list[dict]:
    path = (f"repos/{repository}/actions/workflows/{workflow_id}/runs"
            f"?event=workflow_dispatch&head_sha={workflow_sha}&per_page=100")
    result = gh(["--paginate", "--slurp", path])
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError("workflow run lookup returned invalid JSON") from exc
    if isinstance(pages, dict):
        pages = [pages]
    if not isinstance(pages, list):
        raise DispatchError("workflow run lookup returned an invalid result")
    runs = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs", []), list):
            raise DispatchError("workflow run lookup returned an invalid page")
        runs.extend(page.get("workflow_runs", []))
    return runs


def reusable_run(repository: str, workflow_id: int, workflow_sha: str,
                 source_sha: str, sync_id: str, tag: str,
                 retry_completed: bool = False) -> tuple[dict | None, str]:
    matches = []
    for run in list_workflow_runs(repository, workflow_id, workflow_sha):
        title = str(run.get("display_title") or run.get("name") or "").lower()
        if (run.get("workflow_id") == workflow_id
                and run.get("event") == "workflow_dispatch"
                and run.get("head_branch") == tag
                and str(run.get("head_sha", "")).lower() == workflow_sha.lower()
                and sync_id.lower() in title and source_sha.lower() in title):
            matches.append(run)

    active = [run for run in matches if run.get("status") in
              ("queued", "in_progress", "waiting", "requested", "pending")]
    if len(active) > 1:
        raise DispatchError("multiple active workflow runs match the exact candidate identity")
    if active:
        run = active[0]
        if not isinstance(run.get("id"), int) or not isinstance(run.get("run_attempt"), int):
            raise DispatchError("active workflow run has no exact run ID and attempt")
        return run, "exact run is still active"

    successful = [run for run in matches
                  if run.get("status") == "completed" and run.get("conclusion") == "success"]
    for run in successful:
        run_id = run.get("id")
        attempt = run.get("run_attempt")
        run_url = run.get("html_url")
        if not isinstance(run_id, int) or not isinstance(attempt, int) or not run_url:
            continue
        status_result = gh([f"repos/{repository}/commits/{source_sha}/statuses?per_page=100"])
        try:
            statuses = json.loads(status_result.stdout)
        except json.JSONDecodeError as exc:
            raise DispatchError("candidate status lookup returned invalid JSON") from exc
        if not isinstance(statuses, list):
            raise DispatchError("candidate status lookup returned an invalid result")
        ready = any(status.get("context") == "upstream/ready"
                    and status.get("state") == "success"
                    and status.get("target_url") == run_url
                    for status in statuses if isinstance(status, dict))
        if not ready:
            continue
        artifacts = parse_json(
            gh([f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"]),
            "candidate artifact lookup",
        ).get("artifacts", [])
        artifact_prefix = f"openwrt-x86_64-passwall-docker-{sync_id}"
        expected_names = {
            f"{artifact_prefix}-validation-{run_id}-{attempt}",
            f"{artifact_prefix}-{run_id}-{attempt}",
        }
        available = {item.get("name") for item in artifacts if not item.get("expired")}
        if expected_names.issubset(available):
            return run, "exact successful status and unexpired artifact already exist"

    if matches and not retry_completed:
        raise DispatchError(
            "an exact candidate build already completed but has no reusable ready result; "
            "wait for aggregation or request an explicit retry"
        )
    return None, "no matching workflow run exists"


def set_pending_status(repository: str, source_sha: str, sync_id: str) -> None:
    payload = {
        "state": "pending",
        "context": "upstream/ready",
        "description": f"Build queued for {sync_id}"[:140],
    }
    gh([
        "--method", "POST", f"repos/{repository}/statuses/{source_sha}", "--input", "-",
    ], input_data=json.dumps(payload).encode())


def dispatch(repository: str, workflow_file: str, workflow_sha: str, source_sha: str,
              base_sha: str, upstream_sha: str, merge_base_sha: str,
              feed_lock_sha256: str, sync_id: str,
              retry_existing: bool = False) -> dict:
    for label, value in (("workflow SHA", workflow_sha), ("source SHA", source_sha),
                         ("base SHA", base_sha), ("upstream SHA", upstream_sha),
                         ("merge-base SHA", merge_base_sha)):
        if not HEX40.fullmatch(value):
            raise DispatchError(f"{label} must be a full 40-character SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", feed_lock_sha256):
        raise DispatchError("feed lock digest must be a full SHA-256")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", sync_id):
        raise DispatchError("sync_id contains unsupported characters")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+\.yml", workflow_file):
        raise DispatchError("workflow file name is invalid")

    workflow = parse_json(gh([f"repos/{repository}/actions/workflows/{workflow_file}"]), "workflow lookup")
    workflow_id = workflow.get("id")
    expected_path = f".github/workflows/{workflow_file}"
    if not isinstance(workflow_id, int) or workflow.get("path") != expected_path or workflow.get("state") != "active":
        raise DispatchError("the requested workflow is missing, inactive, or has an unexpected path")

    tag = f"ci/sync-control-{workflow_sha[:12].lower()}"
    ref_path = f"repos/{repository}/git/ref/tags/{tag}"
    existing_result = gh([ref_path], check=False)
    if existing_result.returncode == 0:
        existing = parse_json(existing_result, "control tag lookup")
        if existing.get("object", {}).get("sha", "").lower() != workflow_sha.lower():
            raise DispatchError("immutable control tag exists but points to a different workflow commit")
    else:
        payload = json.dumps({"ref": f"refs/tags/{tag}", "sha": workflow_sha}).encode()
        created = gh(["--method", "POST", f"repos/{repository}/git/refs", "--input", "-"], input_data=payload)
        ref = parse_json(created, "control tag creation")
        if ref.get("object", {}).get("sha", "").lower() != workflow_sha.lower():
            raise DispatchError("created control tag does not point to the requested workflow commit")

    existing_run, reuse_reason = reusable_run(
        repository, workflow_id, workflow_sha, source_sha, sync_id, tag,
        retry_completed=retry_existing,
    )
    if existing_run:
        return dispatch_record(
            repository, workflow_file, workflow_sha, source_sha, base_sha,
            upstream_sha, merge_base_sha, feed_lock_sha256, sync_id, tag,
            workflow_id, existing_run, reused=True, reuse_reason=reuse_reason,
        )

    # Clear any old success before dispatching a retry or a distinct candidate
    # build for the same source commit. The aggregator will replace pending only
    # after checking this exact run, attempt, and both unexpired artifacts.
    set_pending_status(repository, source_sha, sync_id)

    payload = {
        "ref": tag,
        "inputs": {
            "build_reason": "upstream-sync",
            "source_sha": source_sha.lower(),
            "sync_id": sync_id,
            "base_sha": base_sha.lower(),
            "upstream_sha": upstream_sha.lower(),
            "merge_base_sha": merge_base_sha.lower(),
            "feed_lock_sha256": feed_lock_sha256,
        },
    }
    api_version_headers = [
        "--header", "Accept: application/vnd.github+json",
        "--header", f"X-GitHub-Api-Version: {API_VERSION}",
    ]
    result = gh([
        "--method", "POST", *api_version_headers,
        f"repos/{repository}/actions/workflows/{workflow_file}/dispatches",
        "--input", "-",
    ], input_data=json.dumps(payload).encode())
    response = parse_json(result, "workflow dispatch")
    run_id = response.get("workflow_run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise DispatchError("workflow dispatch response did not contain workflow_run_id")

    run = parse_json(gh([f"repos/{repository}/actions/runs/{run_id}"]), "dispatched run lookup")
    if run.get("id") != run_id or run.get("workflow_id") != workflow_id:
        raise DispatchError("dispatched run ID is not associated with the requested workflow")
    if run.get("event") != "workflow_dispatch":
        raise DispatchError("dispatched run has the wrong event")
    if run.get("head_branch") != tag or run.get("head_sha", "").lower() != workflow_sha.lower():
        raise DispatchError("dispatched run ref or workflow SHA does not match the pinned control tag")
    title = str(run.get("display_title") or run.get("name") or "")
    if sync_id.lower() not in title.lower() or source_sha.lower() not in title.lower():
        raise DispatchError("dispatched run title does not include the unique sync_id and source SHA")
    attempt = run.get("run_attempt")
    if attempt != 1:
        raise DispatchError("new dispatch did not start at run attempt 1")
    return dispatch_record(
        repository, workflow_file, workflow_sha, source_sha, base_sha,
        upstream_sha, merge_base_sha, feed_lock_sha256, sync_id, tag,
        workflow_id, run, reused=False, reuse_reason="new exact dispatch created",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--workflow-file", default="openwrt-ci.yml")
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--merge-base-sha", required=True)
    parser.add_argument("--feed-lock-sha256", required=True)
    parser.add_argument("--sync-id", required=True)
    parser.add_argument("--retry-existing", action="store_true",
                        help="dispatch a new run after a prior exact attempt has completed")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.repository:
        parser.error("--repository or GITHUB_REPOSITORY is required")
    try:
        record = dispatch(
            args.repository, args.workflow_file, args.workflow_sha, args.source_sha,
            args.base_sha, args.upstream_sha, args.merge_base_sha,
            args.feed_lock_sha256, args.sync_id, args.retry_existing,
        )
        rendered = json.dumps(record, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        sys.stdout.write(rendered)
    except DispatchError as exc:
        print(f"sync-dispatch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
