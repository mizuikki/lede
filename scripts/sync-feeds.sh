#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/sync-feeds.sh [options] [feedname...]

Synchronize OpenWrt/LEDE feeds using ./scripts/feeds.

By default, this script updates AND installs all feeds (-a).
If one or more feed names are provided, only those feeds are synced.
After feed update, this script refreshes feeds/packages/lang/perl from the
official OpenWrt packages repository.

Options:
  --update-only     Only run "feeds update" (no install)
  --install-only    Only run "feeds install" (no update)
  -f, --force       Force "feeds update" (passes -f)
  --no-sync-official-perl
                    Skip automatic synchronization of official perl
  --locked           Install exact feed and overlay revisions from .github/feed-lock.json
  --lock-file PATH   Use another feed lock file with --locked
  -h, --help        Show this help

Examples:
  scripts/sync-feeds.sh
  scripts/sync-feeds.sh --update-only
  scripts/sync-feeds.sh packages luci
  scripts/sync-feeds.sh --force passwall_packages passwall_luci
EOF
}

TOPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOPDIR"

if [[ ! -x ./scripts/feeds ]]; then
  echo "Error: ./scripts/feeds not found or not executable (TOPDIR=$TOPDIR)" >&2
  exit 1
fi

if [[ ! -x ./scripts/sync-official-perl.sh ]]; then
  echo "Error: ./scripts/sync-official-perl.sh not found or not executable (TOPDIR=$TOPDIR)" >&2
  exit 1
fi

do_update=1
do_install=1
force=0
sync_perl=1
locked=0
lock_file="$TOPDIR/.github/feed-lock.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --update-only) do_install=0; shift ;;
    --install-only) do_update=0; shift ;;
    -f|--force) force=1; shift ;;
    --no-sync-official-perl) sync_perl=0; shift ;;
    --locked) locked=1; shift ;;
    --lock-file) lock_file="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

feeds=("$@")

if [[ "$locked" -eq 1 ]]; then
  if [[ "${#feeds[@]}" -gt 0 || "$do_update" -ne 1 || "$do_install" -ne 1 || "$force" -ne 0 || "$sync_perl" -ne 1 ]]; then
    echo "Error: --locked must be used alone; it always installs all locked feeds and overlays." >&2
    exit 2
  fi
  if [[ ! -f "$lock_file" ]]; then
    echo "Error: feed lock not found: $lock_file" >&2
    exit 1
  fi
  if [[ -e "$TOPDIR/feeds.conf" ]]; then
    echo "Error: feeds.conf already exists; refusing to replace a local feed configuration." >&2
    exit 1
  fi
  cleanup_locked_config() {
    rm -f "$TOPDIR/feeds.conf"
  }
  trap cleanup_locked_config EXIT

  echo "[1/4] Materializing pinned feed configuration"
  python3 scripts/feed-lock.py materialize --lock "$lock_file" --output feeds.conf
  echo "[2/4] Installing pinned Git feeds"
  ./scripts/feeds update -a
  echo "[3/4] Applying locked official Perl and dockerd overlays"
  ./scripts/sync-official-perl.sh --locked --lock-file "$lock_file"
  ./scripts/stage-dockerd-build-patch.sh --locked --lock-file "$lock_file"
  echo "[4/4] Installing feed packages and verifying all inputs"
  ./scripts/feeds install -a
  python3 scripts/feed-lock.py verify --root "$TOPDIR" --lock "$lock_file" --feeds-conf "$TOPDIR/feeds.conf.default"
  exit 0
fi

update_args=()
install_args=()
if [[ "${#feeds[@]}" -eq 0 ]]; then
  update_args+=("-a")
  install_args+=("-a")
else
  update_args+=("${feeds[@]}")
  install_args+=("-a")
  for f in "${feeds[@]}"; do
    install_args+=("-p" "$f")
  done
fi

if [[ "$sync_perl" -eq 1 && "$do_update" -eq 1 && "${#feeds[@]}" -gt 0 ]]; then
  has_packages=0
  for f in "${feeds[@]}"; do
    if [[ "$f" == "packages" ]]; then
      has_packages=1
      break
    fi
  done
  if [[ "$has_packages" -eq 0 ]]; then
    echo "Note: adding 'packages' to feed update so official perl sync can refresh it"
    update_args+=("packages")
  fi
fi

if [[ "$do_update" -eq 1 ]]; then
  echo "[1/4] Updating feeds"
  if [[ "$force" -eq 1 ]]; then
    ./scripts/feeds update -f "${update_args[@]}"
  else
    ./scripts/feeds update "${update_args[@]}"
  fi
else
  echo "[1/4] Skipping feeds update (--install-only)"
fi

if [[ "$sync_perl" -eq 1 ]]; then
  if [[ "$do_update" -eq 0 && ! -e "$TOPDIR/feeds/packages/.git" ]]; then
    echo "Error: feeds/packages is missing. Run scripts/sync-feeds.sh packages or ./scripts/feeds update packages first." >&2
    exit 1
  fi
  echo "[2/4] Synchronizing official perl"
  ./scripts/sync-official-perl.sh
else
  echo "[2/4] Skipping official perl synchronization (--no-sync-official-perl)"
fi

if [[ -d "$TOPDIR/feeds/packages" ]]; then
  if [[ ! -x ./scripts/stage-dockerd-build-patch.sh ]]; then
    echo "Error: scripts/stage-dockerd-build-patch.sh not found or not executable (TOPDIR=$TOPDIR)" >&2
    exit 1
  fi
  echo "[3/4] Staging dockerd build compatibility patch"
  ./scripts/stage-dockerd-build-patch.sh
else
  echo "[3/4] Skipping dockerd patch (packages feed is unavailable)"
fi

if [[ "$do_install" -eq 1 ]]; then
  echo "[4/4] Installing feed packages"
  ./scripts/feeds install "${install_args[@]}"
else
  echo "[4/4] Skipping feeds install (--update-only)"
fi
