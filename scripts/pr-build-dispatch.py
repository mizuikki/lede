#!/usr/bin/env python3
"""Dispatch a read-only build for an exact pull request merge commit."""

from __future__ import annotations

import argparse
import json
import os
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


def object_response(result: subprocess.CompletedProcess, label: str) -> dict:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"{label} did not return a JSON object")
    return value


def list_runs(repository: str, workflow_id: int, workflow_sha: str) -> list[dict]:
    path = (f"repos/{repository}/actions/workflows/{workflow_id}/runs"
            f"?event=workflow_dispatch&head_sha={workflow_sha}&per_page=100")
    value = json.loads(gh(["--paginate", "--slurp", path]).stdout)
    pages = value if isinstance(value, list) else [value]
    runs = []
    for page in pages:
        if isinstance(page, dict) and isinstance(page.get("workflow_runs"), list):
            runs.extend(item for item in page["workflow_runs"] if isinstance(item, dict))
    return runs


def reusable_run(repository: str, workflow_id: int, workflow_sha: str,
                 control_ref: str, pull_number: int, merge_sha: str) -> dict | None:
    title_needle = f"pr-{pull_number}".lower()
    source_needle = merge_sha.lower()
    matches = [run for run in list_runs(repository, workflow_id, workflow_sha)
               if run.get("event") == "workflow_dispatch"
               and run.get("workflow_id") == workflow_id
               and run.get("head_branch") == control_ref
               and str(run.get("head_sha", "")).lower() == workflow_sha.lower()
               and title_needle in str(run.get("display_title") or run.get("name") or "").lower()
               and source_needle in str(run.get("display_title") or run.get("name") or "").lower()]
    active = [run for run in matches if run.get("status") in
              ("queued", "in_progress", "waiting", "requested", "pending")]
    if len(active) > 1:
        raise DispatchError("multiple active builds match this exact pull request merge SHA")
    if active:
        return active[0]
    for run in matches:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        run_id, attempt = run.get("id"), run.get("run_attempt")
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            continue
        artifacts = object_response(
            gh([f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"]),
            "pull request artifact lookup",
        ).get("artifacts", [])
        names = {item.get("name") for item in artifacts if not item.get("expired")}
        prefix = f"openwrt-x86_64-passwall-docker-pr-{pull_number}"
        required = {f"{prefix}-validation-{run_id}-{attempt}", f"{prefix}-{run_id}-{attempt}"}
        if required.issubset(names):
            return run
    return None


def set_pending(repository: str, merge_sha: str, pull_number: int) -> None:
    body = {
        "state": "pending",
        "context": "upstream/ready",
        "description": f"Read-only build queued for PR {pull_number}"[:140],
    }
    gh(["--method", "POST", f"repos/{repository}/statuses/{merge_sha}", "--input", "-"],
       input_data=json.dumps(body).encode())


def dispatch(repository: str, workflow_sha: str, pull_number: int,
             expected_head_sha: str, expected_base_sha: str,
             expected_merge_sha: str) -> dict:
    for label, value in (("trusted workflow SHA", workflow_sha), ("PR head SHA", expected_head_sha),
                         ("PR base SHA", expected_base_sha), ("PR merge SHA", expected_merge_sha)):
        if not HEX40.fullmatch(value):
            raise DispatchError(f"{label} must be a full commit SHA")
    live = object_response(gh([f"repos/{repository}/pulls/{pull_number}"]), "pull request lookup")
    if live.get("state") != "open":
        raise DispatchError("pull request is no longer open")
    head = live.get("head", {})
    base = live.get("base", {})
    if str(head.get("sha", "")).lower() != expected_head_sha.lower():
        raise DispatchError("pull request head changed before dispatch")
    if str(base.get("sha", "")).lower() != expected_base_sha.lower():
        raise DispatchError("pull request base changed before dispatch")
    if str(live.get("merge_commit_sha", "")).lower() != expected_merge_sha.lower():
        raise DispatchError("pull request merge SHA changed before dispatch")
    repository_info = object_response(gh([f"repos/{repository}"]), "repository lookup")
    default_branch = repository_info.get("default_branch")
    if base.get("ref") != default_branch:
        raise DispatchError("only pull requests targeting the default branch are built")
    default = object_response(gh([f"repos/{repository}/branches/{default_branch}"]), "default branch lookup")
    if default.get("commit", {}).get("sha", "").lower() != expected_base_sha.lower():
        raise DispatchError("default branch advanced before pull request dispatch")
    if workflow_sha.lower() != expected_base_sha.lower():
        raise DispatchError("control workflow must be pinned to the exact reviewed PR base")

    workflow = object_response(gh([f"repos/{repository}/actions/workflows/openwrt-ci.yml"]), "workflow lookup")
    workflow_id = workflow.get("id")
    if (not isinstance(workflow_id, int) or workflow.get("path") != ".github/workflows/openwrt-ci.yml"
            or workflow.get("state") != "active"):
        raise DispatchError("OpenWrt-CI workflow is missing, inactive, or has an unexpected path")
    control_ref = f"ci/sync-control-{workflow_sha[:12].lower()}"
    ref_path = f"repos/{repository}/git/ref/tags/{control_ref}"
    existing = gh([ref_path], check=False)
    if existing.returncode == 0:
        ref = object_response(existing, "control tag lookup")
        if ref.get("object", {}).get("sha", "").lower() != workflow_sha.lower():
            raise DispatchError("immutable control tag points to a different workflow commit")
    else:
        payload = json.dumps({"ref": f"refs/tags/{control_ref}", "sha": workflow_sha.lower()}).encode()
        ref = object_response(gh(["--method", "POST", f"repos/{repository}/git/refs", "--input", "-"],
                                  input_data=payload), "control tag creation")
        if ref.get("object", {}).get("sha", "").lower() != workflow_sha.lower():
            raise DispatchError("created control tag does not point to the trusted workflow SHA")

    reused = reusable_run(repository, workflow_id, workflow_sha, control_ref,
                          pull_number, expected_merge_sha)
    if reused:
        return {"run_id": reused.get("id"), "run_attempt": reused.get("run_attempt"),
                "workflow_sha": workflow_sha.lower(), "source_sha": expected_merge_sha.lower(),
                "reused": True, "run_url": reused.get("html_url")}

    set_pending(repository, expected_merge_sha, pull_number)
    inputs = {
        "build_reason": "pull-request",
        "source_sha": expected_merge_sha.lower(),
        "sync_id": "",
        "base_sha": "",
        "upstream_sha": "",
        "merge_base_sha": "",
        "feed_lock_sha256": "",
        "pull_request": str(pull_number),
        "pull_head_sha": expected_head_sha.lower(),
        "pull_base_sha": expected_base_sha.lower(),
        "pull_merge_sha": expected_merge_sha.lower(),
    }
    payload = json.dumps({"ref": control_ref, "inputs": inputs}).encode()
    result = gh([
        "--method", "POST", "--header", "Accept: application/vnd.github+json",
        "--header", f"X-GitHub-Api-Version: {API_VERSION}",
        f"repos/{repository}/actions/workflows/openwrt-ci.yml/dispatches", "--input", "-",
    ], input_data=payload)
    response = object_response(result, "pull request workflow dispatch")
    run_id = response.get("workflow_run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise DispatchError("workflow dispatch response did not contain workflow_run_id")
    run = object_response(gh([f"repos/{repository}/actions/runs/{run_id}"]), "dispatched run lookup")
    title = str(run.get("display_title") or run.get("name") or "").lower()
    if run.get("id") != run_id or run.get("workflow_id") != workflow_id:
        raise DispatchError("dispatched run is not associated with OpenWrt-CI")
    if run.get("event") != "workflow_dispatch":
        raise DispatchError("dispatched pull request build has the wrong event")
    if run.get("head_branch") != control_ref or str(run.get("head_sha", "")).lower() != workflow_sha.lower():
        raise DispatchError("dispatched pull request build used the wrong trusted control ref")
    if f"pr-{pull_number}" not in title or expected_merge_sha.lower() not in title:
        raise DispatchError("dispatched run title does not identify the exact PR and merge SHA")
    attempt = run.get("run_attempt")
    if attempt != 1:
        raise DispatchError("new pull request dispatch did not start at run attempt 1")
    return {"run_id": run_id, "run_attempt": attempt, "workflow_sha": workflow_sha.lower(),
            "source_sha": expected_merge_sha.lower(), "reused": False,
            "run_url": run.get("html_url")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--merge-sha", required=True)
    parser.add_argument("--output", default="pr-dispatch-record.json")
    args = parser.parse_args()
    if not args.repository:
        parser.error("--repository or GITHUB_REPOSITORY is required")
    try:
        result = dispatch(args.repository, args.workflow_sha, args.pull_request,
                          args.head_sha, args.base_sha, args.merge_sha)
        rendered = json.dumps(result, indent=2) + "\n"
        with open(args.output, "w", encoding="utf-8") as output:
            output.write(rendered)
        sys.stdout.write(rendered)
        return 0
    except (DispatchError, OSError) as exc:
        print(f"pr-build-dispatch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
