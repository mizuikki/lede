#!/usr/bin/env bash
set -euo pipefail

TOPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOPDIR"

source_patch="$TOPDIR/patches/dockerd/100-skip-host-nested-binaries.patch"
package_dir="$TOPDIR/feeds/packages/utils/dockerd"
target_patch="$package_dir/patches/999-openwrt-skip-host-nested-binaries.patch"
locked=0
lock_file="$TOPDIR/.github/feed-lock.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --locked) locked=1; shift ;;
    --lock-file) lock_file="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: scripts/stage-dockerd-build-patch.sh [--locked] [--lock-file PATH]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$source_patch" ]]; then
  echo "Error: dockerd compatibility patch is missing: $source_patch" >&2
  exit 1
fi

if [[ ! -f "$package_dir/Makefile" ]]; then
  echo "Error: dockerd package is missing. Run ./scripts/feeds update packages first." >&2
  exit 1
fi

if ! grep -qx 'PKG_NAME:=dockerd' "$package_dir/Makefile"; then
  echo "Error: unexpected dockerd package Makefile: $package_dir/Makefile" >&2
  exit 1
fi

if [[ "$locked" -eq 1 ]]; then
  expected="$(python3 - "$lock_file" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(lock.get("overlays", {}).get("dockerd_patch", {}).get("sha256", ""))
PY
)"
  actual="$(sha256sum "$source_patch" | awk '{print $1}')"
  if [[ ! "$expected" =~ ^[0-9a-f]{64}$ || "$actual" != "$expected" ]]; then
    echo "Error: dockerd patch digest differs from the feed lock" >&2
    exit 1
  fi
fi

if [[ -f "$target_patch" ]] && cmp -s "$source_patch" "$target_patch"; then
  echo "Dockerd compatibility patch already staged"
  exit 0
fi

install -Dm0644 "$source_patch" "$target_patch"
echo "Staged dockerd compatibility patch"
