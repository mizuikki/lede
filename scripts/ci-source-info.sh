#!/usr/bin/env bash
set -euo pipefail

# Print GitHub Actions outputs for the checked-out source. The workflow refreshes
# origin's default branch before using publish_release to authorize publication.
: "${DEFAULT_BRANCH:?DEFAULT_BRANCH is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${EXPECTED_SOURCE_SHA:?EXPECTED_SOURCE_SHA is required}"

if [[ ! "$EXPECTED_SOURCE_SHA" =~ ^[[:xdigit:]]{40}$ ]]; then
  echo "Error: source_sha must be a full 40-character commit SHA." >&2
  exit 1
fi

source_sha="$(git rev-parse --verify 'HEAD^{commit}')"
if [[ "$source_sha" != "${EXPECTED_SOURCE_SHA,,}" ]]; then
  echo "Error: checkout does not match the requested source SHA." >&2
  exit 1
fi

default_sha="$(git rev-parse --verify "refs/remotes/origin/${DEFAULT_BRANCH}^{commit}")"
publish_release=false
if [[ "$GITHUB_REF" == "refs/heads/${DEFAULT_BRANCH}" && "$source_sha" == "$default_sha" ]]; then
  publish_release=true
fi

printf 'source_sha=%s\n' "$source_sha"
printf 'short_sha=%s\n' "${source_sha:0:12}"
printf 'publish_release=%s\n' "$publish_release"
printf 'Build source: %s; publish release: %s\n' "$source_sha" "$publish_release" >&2
