#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/sync-official-perl.sh [--locked] [--lock-file PATH]
       scripts/sync-official-perl.sh [--revision SHA]

Synchronize the official OpenWrt Perl feed overlay into feeds/packages/lang/perl.
Without --locked, the selected upstream branch is refreshed as before.
With --locked, only the revision and digest recorded in the feed lock are used.
EOF
}

TOPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOPDIR"

locked=0
lock_file="$TOPDIR/.github/feed-lock.json"
revision=""
expected_tree=""
expected_digest=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --locked) locked=1; shift ;;
    --lock-file) lock_file="$2"; shift 2 ;;
    --revision) revision="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

upstream_url="https://github.com/openwrt/packages"
upstream_branch="openwrt-25.12"
cache_dir="$TOPDIR/.cache/openwrt-packages"
src_dir="$cache_dir/lang/perl"
dst_dir="$TOPDIR/feeds/packages/lang/perl"

if [[ "$locked" -eq 1 ]]; then
  if [[ ! -f "$lock_file" ]]; then
    echo "Error: feed lock not found: $lock_file" >&2
    exit 1
  fi
  perl_lock="$(python3 - "$lock_file" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
perl = lock.get("overlays", {}).get("official_perl", {})
print("\t".join((perl.get("revision", ""), perl.get("tree", ""), perl.get("sha256", ""), perl.get("repository", ""), perl.get("branch", ""))))
PY
)"
  IFS=$'\t' read -r revision expected_tree expected_digest upstream_url upstream_branch <<< "$perl_lock"
  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ || ! "$expected_tree" =~ ^[0-9a-f]{40}$ || ! "$expected_digest" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Error: invalid official Perl lock values" >&2
    exit 1
  fi
elif [[ -n "$revision" ]]; then
  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Error: revision must be a full commit SHA" >&2
    exit 1
  fi
else
  revision=""
fi

if [[ ! -d "$TOPDIR/feeds/packages/.git" ]]; then
  echo "Error: feeds/packages is missing. Install the pinned packages feed first." >&2
  exit 1
fi
if [[ ! -x "$TOPDIR/scripts/feeds" ]]; then
  echo "Error: ./scripts/feeds is not executable." >&2
  exit 1
fi

mkdir -p "$TOPDIR/.cache"
if [[ ! -d "$cache_dir/.git" ]]; then
  git init --quiet "$cache_dir"
  git -C "$cache_dir" remote add origin "$upstream_url"
else
  git -C "$cache_dir" remote set-url origin "$upstream_url"
fi

if [[ "$locked" -eq 1 || -n "$revision" ]]; then
  git -C "$cache_dir" fetch --quiet --depth 1 origin "$revision"
  git -C "$cache_dir" checkout --quiet --force --detach "$revision"
else
  git -C "$cache_dir" fetch --quiet --depth 1 origin "$upstream_branch"
  git -C "$cache_dir" checkout --quiet --force -B "$upstream_branch" FETCH_HEAD
  revision="$(git -C "$cache_dir" rev-parse HEAD)"
fi
git -C "$cache_dir" clean -fdq

if [[ "$(git -C "$cache_dir" rev-parse HEAD)" != "$revision" ]]; then
  echo "Error: official Perl checkout does not match requested revision" >&2
  exit 1
fi
if [[ ! -f "$src_dir/Makefile" || ! -f "$src_dir/perlver.mk" ]]; then
  echo "Error: upstream Perl source is incomplete at $revision" >&2
  exit 1
fi
if [[ -n "$expected_tree" ]]; then
  actual_tree="$(git -C "$cache_dir" rev-parse "HEAD:${PERL_PATH:-lang/perl}")"
  if [[ "$actual_tree" != "$expected_tree" ]]; then
    echo "Error: official Perl tree differs from the locked tree" >&2
    exit 1
  fi
fi

mkdir -p "$(dirname "$dst_dir")"
mkdir -p "$dst_dir"
rsync -a --delete "$src_dir"/ "$dst_dir"/
if [[ ! -f "$dst_dir/Makefile" || ! -f "$dst_dir/perlver.mk" ]]; then
  echo "Error: synchronized Perl overlay is incomplete" >&2
  exit 1
fi

if [[ -n "$expected_digest" ]]; then
  actual_digest="$(python3 "$TOPDIR/scripts/feed-lock.py" overlay-digest --path "$dst_dir")"
  if [[ "$actual_digest" != "$expected_digest" ]]; then
    echo "Error: synchronized Perl overlay digest differs from the feed lock" >&2
    exit 1
  fi
fi

# Invalidate the package dump cache after replacing Perl, then rebuild it from
# the already locked packages feed working tree without fetching a feed tip.
rm -rf "$TOPDIR/feeds/packages.tmp"
rm -f "$TOPDIR/feeds/packages.index" "$TOPDIR/feeds/packages.targetindex"
./scripts/feeds update -i packages

echo "Synchronized official Perl:"
echo "  source=$upstream_url"
echo "  branch=$upstream_branch"
echo "  revision=$revision"
echo "  destination=feeds/packages/lang/perl"
echo "  locked=$([[ "$locked" -eq 1 || -n "$expected_tree" ]] && echo true || echo false)"
echo "  packages_index=refreshed"
