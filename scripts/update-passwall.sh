#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/update-passwall.sh [options]

Update Openwrt-Passwall feeds (passwall_packages + passwall_luci) to the latest
state allowed by your feeds configuration, then (optionally) install all
packages from those feeds.

Options:
  -f, --force       Force update feeds (may discard local changes in feeds/*)
  --no-install      Skip "feeds install" step
  --defconfig       Run "make defconfig" after updating/installing feeds
  -h, --help        Show this help

Examples:
  scripts/update-passwall.sh
  scripts/update-passwall.sh --force --defconfig
  scripts/update-passwall.sh --no-install
EOF
}

force=0
do_install=1
do_defconfig=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--force) force=1; shift ;;
    --no-install) do_install=0; shift ;;
    --defconfig) do_defconfig=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

TOPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOPDIR"

if [[ ! -x ./scripts/feeds ]]; then
  echo "Error: ./scripts/feeds not found or not executable (TOPDIR=$TOPDIR)" >&2
  exit 1
fi

if ! ./scripts/feeds list -n | grep -qx 'passwall_packages'; then
  echo "Error: feed 'passwall_packages' not found in feeds.conf / feeds.conf.default" >&2
  echo "Hint: add a line like:" >&2
  echo "  src-git passwall_packages https://github.com/Openwrt-Passwall/openwrt-passwall-packages.git;main" >&2
  exit 1
fi

if ! ./scripts/feeds list -n | grep -qx 'passwall_luci'; then
  echo "Error: feed 'passwall_luci' not found in feeds.conf / feeds.conf.default" >&2
  echo "Hint: add a line like:" >&2
  echo "  src-git passwall_luci https://github.com/Openwrt-Passwall/openwrt-passwall.git;main" >&2
  exit 1
fi

update_args=()
if [[ "$force" -eq 1 ]]; then
  update_args+=("-f")
fi

mkdir -p .cache/feed-sync
echo "Partial feed update requires a successful full synchronization before building." > .cache/feed-sync/FAILED

echo "[1/3] Updating feeds: passwall_packages passwall_luci"
./scripts/feeds update "${update_args[@]}" passwall_packages passwall_luci

if [[ "$do_install" -eq 1 ]]; then
  echo "[2/3] Installing packages from feeds (may take a while)"
  ./scripts/feeds install -a -p passwall_packages
  ./scripts/feeds install -a -p passwall_luci
else
  echo "[2/3] Skipping feeds install (--no-install)"
fi

if [[ "$do_defconfig" -eq 1 ]]; then
  echo "[3/3] Running make defconfig"
  make defconfig
else
  echo "[3/3] Done"
fi

echo ""
echo "Passwall feed revisions:"
./scripts/feeds list -s | awk '/^passwall_(packages|luci)[[:space:]]/ {print "  " $1 "  rev=" $3 "  url=" $4}'
