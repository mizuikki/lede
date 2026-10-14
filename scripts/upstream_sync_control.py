#!/usr/bin/env python3
"""Helpers for selecting a single immutable open upstream candidate."""

from __future__ import annotations

import re


class CandidateSelectionError(RuntimeError):
    pass


HEX40 = re.compile(r"^[0-9a-f]{40}$")
SYNC_ID = re.compile(r"^upstream-([0-9a-f]{12})-([0-9a-f]{12})-w([0-9a-f]{12})$")


def candidate_sync_id(base_sha: str, upstream_sha: str, workflow_sha: str) -> str:
    values = [str(value).lower() for value in (base_sha, upstream_sha, workflow_sha)]
    if any(not HEX40.fullmatch(value) for value in values):
        raise CandidateSelectionError("candidate B/U/W identity must use full commit SHAs")
    base, upstream, workflow = values
    return f"upstream-{base[:12]}-{upstream[:12]}-w{workflow[:12]}"


def candidate_branch(base_sha: str, upstream_sha: str, workflow_sha: str) -> str:
    return f"sync/{candidate_sync_id(base_sha, upstream_sha, workflow_sha)}"


def candidate_branch_from_sync_id(sync_id: str) -> str:
    if not SYNC_ID.fullmatch(str(sync_id)):
        raise CandidateSelectionError("candidate run does not contain the complete sync identity")
    return f"sync/{sync_id}"


def candidate_sync_id_from_branch(branch: str) -> str:
    prefix = "sync/"
    if not str(branch).startswith(prefix):
        raise CandidateSelectionError("candidate branch does not use the sync namespace")
    sync_id = str(branch)[len(prefix):]
    if candidate_branch_from_sync_id(sync_id) != branch:
        raise CandidateSelectionError("candidate branch does not use the complete sync identity")
    return sync_id


def candidate_control_ref(workflow_sha: str) -> str:
    value = str(workflow_sha).lower()
    if not HEX40.fullmatch(value):
        raise CandidateSelectionError("candidate workflow identity must be a full commit SHA")
    return f"ci/sync-control-{value[:12]}"


def select_open_candidate(pulls: list[dict], default_branch: str,
                          repository: str | None = None) -> dict | None:
    candidates = []
    for pull in pulls:
        if not str(pull.get("headRefName", "")).startswith("sync/upstream-"):
            continue
        head_repository = pull.get("headRepository")
        if repository and (not isinstance(head_repository, dict)
                           or str(head_repository.get("nameWithOwner") or "").lower()
                           != repository.lower()):
            continue
        candidates.append(pull)
    if len(candidates) > 1:
        branches = ", ".join(sorted(str(item.get("headRefName")) for item in candidates))
        raise CandidateSelectionError(f"multiple open upstream candidates: {branches}")
    if candidates and candidates[0].get("baseRefName") != default_branch:
        raise CandidateSelectionError("open upstream candidate does not target the current default branch")
    return candidates[0] if candidates else None


def validate_candidate_manifest(branch: str, manifest: dict) -> None:
    values = {key: str(manifest.get(key, "")).lower() for key in
              ("base_sha", "upstream_sha", "workflow_sha")}
    if any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in values.values()):
        raise CandidateSelectionError("open candidate manifest is missing a full B/U/W identity")
    expected_id = candidate_sync_id(values["base_sha"], values["upstream_sha"], values["workflow_sha"])
    expected_branch = candidate_branch(values["base_sha"], values["upstream_sha"], values["workflow_sha"])
    if manifest.get("sync_id") != expected_id or branch != expected_branch:
        raise CandidateSelectionError("open candidate branch and full B/U/W manifest identity differ")


def candidate_expired(manifest: dict, current_base_sha: str) -> bool:
    return str(manifest.get("base_sha", "")).lower() != current_base_sha.lower()
