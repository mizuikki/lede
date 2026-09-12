# Scripts

This directory contains helper scripts intended to make feed syncing and
configuration/profile-based builds more repeatable.

The repository also auto-synchronizes `feeds/packages/lang/perl` from the
official `openwrt/packages` `openwrt-25.12` branch. Feed sync flows refresh
that tree automatically so Perl does not depend on manual local compatibility
patches.

Feed sync also stages a local `dockerd` source patch. It prevents Moby's
release helper from copying incomplete host-side runtime binaries when an x86
target is built on an x86 GitHub Actions runner; OpenWrt packages those runtime
components separately.

## `update-passwall.sh`

Updates the Passwall feeds and optionally installs all packages from them.

### What it does

- Runs `./scripts/feeds update passwall_packages passwall_luci` (use `--force` to pass `-f`)
- Optionally runs:
  - `./scripts/feeds install -a -p passwall_packages`
  - `./scripts/feeds install -a -p passwall_luci`
- Optionally runs `make defconfig`
- Prints the current revisions for both feeds

### Usage

```sh
scripts/update-passwall.sh [--force] [--no-install] [--defconfig]
```

### Examples

Update to the latest feed state and install packages:

```sh
scripts/update-passwall.sh
```

Force-update feeds (discard local changes under `feeds/passwall_*` if needed) and run `make defconfig`:

```sh
scripts/update-passwall.sh --force --defconfig
```

Update only (skip install):

```sh
scripts/update-passwall.sh --no-install
```

## `sync-feeds.sh`

Synchronizes feeds using `./scripts/feeds update` and `./scripts/feeds install`.

### What it does

- By default: `./scripts/feeds update -a` and `./scripts/feeds install -a`
- When feed names are provided: updates those feeds and installs packages from those feeds
- Refreshes `feeds/packages/lang/perl` from official OpenWrt before installing
- Rebuilds the `packages` feed index after the Perl tree is refreshed

### Usage

```sh
scripts/sync-feeds.sh [--force] [--update-only|--install-only] [--no-sync-official-perl] [feedname...]
```

### Examples

Update and install all feeds:

```sh
scripts/sync-feeds.sh
```

Update only:

```sh
scripts/sync-feeds.sh --update-only
```

Sync a subset of feeds:

```sh
scripts/sync-feeds.sh packages luci
```

## `sync-official-perl.sh`

Synchronizes `feeds/packages/lang/perl` from the official `openwrt/packages`
repository branch `openwrt-25.12`.

### What it does

- Maintains a cache under `.cache/openwrt-packages`
- Copies the upstream `lang/perl` directory into `feeds/packages/lang/perl`
- Prints the exact upstream revision used for traceability

### Usage

```sh
scripts/sync-official-perl.sh
```

## `apply-profile.sh`

Applies a `diffconfig` profile from `profiles/` to generate a full `.config`,
then runs `make defconfig`.

### What it does

- Optionally syncs all feeds first (`--sync-all-feeds`)
- Optionally updates Passwall feeds first (default)
- Generates `.config` from `profiles/<name>.diffconfig`
- Runs `make defconfig`
- Prints Passwall feed revisions for traceability

### Usage

```sh
scripts/apply-profile.sh <profile-name> [options]
```

### Examples

Apply profile and update Passwall feeds first:

```sh
scripts/apply-profile.sh x86_64-passwall-docker
```

Apply profile after syncing all feeds (update + install):

```sh
scripts/apply-profile.sh x86_64-passwall-docker --sync-all-feeds
```

## `ci-source-info.sh`

Prints `source_sha`, `short_sha`, and `publish_release` for the checked-out
commit. It requires `EXPECTED_SOURCE_SHA` (a full commit SHA), `DEFAULT_BRANCH`,
and `GITHUB_REF`. A mismatched checkout or missing default-branch reference is
an error. Publication requires a dispatch on the default branch and a source
SHA equal to `origin/<default-branch>`.

`OpenWrt-CI` reads this helper from the workflow commit, so it can also build
older source commits. It refreshes the default branch and calls the helper
again before publishing. Candidate builds are downloadable Actions artifacts.

See [upstream sync and candidate build design](../doc/upstream-sync.md) for the
workflow contract, validation command, and the proposed long-term sync flow.
