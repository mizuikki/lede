# Profiles

This directory contains **single-file build profiles** in `diffconfig` format.
Each profile is a minimal seed configuration that can be expanded into a full
OpenWrt/LEDE `.config` via Kconfig and `make defconfig`.

## Apply a profile

Use:

```sh
scripts/apply-profile.sh <profile-name>
```

Example:

```sh
scripts/apply-profile.sh x86_64-passwall-docker
make -j"$(nproc)"
```

## Refresh a profile

After changing `.config` (e.g. via `make menuconfig`), regenerate the profile:

```sh
./scripts/diffconfig.sh > profiles/x86_64-passwall-docker.diffconfig
```

## Feed package changes

The Passwall/Docker profile follows the package names provided by its feeds.
It no longer selects `trojan-plus`, which was removed from the Passwall packages
feed. The selected Xray and Sing-box packages remain in the profile.
