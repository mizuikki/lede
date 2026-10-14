#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/apply-profile.sh <profile-name> [options]

Applies a single-file diffconfig profile from profiles/<name>.diffconfig to
generate a full .config, then runs "make defconfig" to close dependencies.

Options:
  --update-passwall     Update Passwall feeds before applying (default)
  --no-update-passwall  Skip updating feeds
  --install-passwall    Run feeds install for Passwall feeds after update
  --sync-all-feeds      Run scripts/sync-feeds.sh (update+install all feeds) before applying
  --locked-feeds        Install feeds from .github/feed-lock.json before applying
  --force-feeds         Force-update feeds (passes --force to update-passwall.sh)
  --defconfig-only      Apply profile + make defconfig, no feed updates
  -h, --help            Show this help

Examples:
  scripts/apply-profile.sh x86_64-passwall-docker
  scripts/apply-profile.sh x86_64-passwall-docker --no-update-passwall
  scripts/apply-profile.sh x86_64-passwall-docker --force-feeds
  scripts/apply-profile.sh x86_64-passwall-docker --install-passwall
  scripts/apply-profile.sh x86_64-passwall-docker --sync-all-feeds
  scripts/apply-profile.sh x86_64-passwall-docker --locked-feeds
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

profile_name="$1"
shift

TOPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TOPDIR"

profile_path="profiles/${profile_name}.diffconfig"
if [[ ! -f "$profile_path" ]]; then
  echo "Error: profile not found: $profile_path" >&2
  exit 1
fi

update_passwall=1
install_passwall=0
install_passwall_set=0
sync_all_feeds=0
force_feeds=0
locked_feeds=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --update-passwall) update_passwall=1; shift ;;
    --no-update-passwall) update_passwall=0; shift ;;
    --install-passwall) install_passwall=1; install_passwall_set=1; shift ;;
    --sync-all-feeds) sync_all_feeds=1; shift ;;
    --locked-feeds) locked_feeds=1; update_passwall=0; install_passwall=0; shift ;;
    --force-feeds) force_feeds=1; shift ;;
    --defconfig-only) update_passwall=0; install_passwall=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$locked_feeds" -eq 1 && ( "$sync_all_feeds" -eq 1 || "$force_feeds" -eq 1 ) ]]; then
  echo "Error: --locked-feeds cannot be combined with --sync-all-feeds or --force-feeds" >&2
  exit 2
fi

# Auto-enable feeds install on fresh checkouts where package/feeds links are
# missing. Without this, Kconfig may not "see" feed packages and profile
# options can be ignored.
if [[ "$install_passwall_set" -eq 0 && "$locked_feeds" -eq 0 ]]; then
  if [[ ! -d package/feeds/passwall_packages || ! -d package/feeds/passwall_luci ]]; then
    install_passwall=1
  fi
fi

if ! grep -q '^CONFIG_TARGET_' "$profile_path"; then
  echo "Error: profile does not contain any CONFIG_TARGET_ entries: $profile_path" >&2
  echo "Hint: ensure target/subtarget/profile are present to avoid future drift." >&2
  exit 1
fi

if [[ "$locked_feeds" -eq 1 ]]; then
  echo "[0/4] Installing locked feeds and overlays"
  scripts/sync-feeds.sh --locked
elif [[ "$sync_all_feeds" -eq 1 ]]; then
  if [[ ! -x scripts/sync-feeds.sh ]]; then
    echo "Error: scripts/sync-feeds.sh not found or not executable" >&2
    exit 1
  fi
  echo "[0/4] Syncing all feeds (update+install)"
  scripts/sync-feeds.sh
fi

if [[ "$update_passwall" -eq 1 && "$sync_all_feeds" -eq 0 ]]; then
  if [[ ! -x scripts/update-passwall.sh ]]; then
    echo "Error: scripts/update-passwall.sh not found or not executable" >&2
    exit 1
  fi
  echo "[1/4] Updating Passwall feeds"
  update_args=("--no-install")
  if [[ "$install_passwall" -eq 1 ]]; then
    update_args=()
  fi
  if [[ "$force_feeds" -eq 1 ]]; then
    scripts/update-passwall.sh --force "${update_args[@]}"
  else
    scripts/update-passwall.sh "${update_args[@]}"
  fi
else
  echo "[1/4] Skipping Passwall feed update"
  if [[ "$install_passwall" -eq 1 ]]; then
    echo "Note: --install-passwall requires updating feeds; ignoring install step."
  fi
fi

echo "[2/4] Seeding .config from profile"
cp "$profile_path" .config

echo "[3/4] Generating Kconfig metadata (prepare-tmpinfo)"
make prepare-tmpinfo >/dev/null

echo "[4/4] Running make defconfig"
make defconfig >/dev/null

echo ""
echo "Applied profile: $profile_name ($profile_path)"
echo "Passwall feed revisions:"
./scripts/feeds list -s | awk '/^passwall_(packages|luci)[[:space:]]/ {print "  " $1 "  rev=" $3 "  url=" $4}'
