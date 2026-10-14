#!/usr/bin/env python3
"""Derive required platform checks from the exact source paths in a candidate."""

from __future__ import annotations

from pathlib import PurePosixPath


PLATFORM_CHECKS = {
    "qualcomm_ipq60xx": ["image_build", "dts_review", "upgrade_review"],
    "airoha_an758x": ["image_build", "upgrade_review", "device_review"],
    "rockchip_rk3568": ["target_build", "sgmii_qsgmii_review"],
}

PLATFORM_PREFIXES = {
    "qualcomm_ipq60xx": (
        "target/linux/qualcommax/",
        "package/firmware/ipq-wifi/",
        "package/boot/uboot-ipq40xx/",
    ),
    "airoha_an758x": (
        "target/linux/airoha/",
        "package/boot/uboot-airoha/",
    ),
    "rockchip_rk3568": (
        "target/linux/rockchip/",
        "package/boot/uboot-rockchip/",
    ),
}

SHARED_PLATFORM_PREFIXES = (
    "target/linux/generic/",
    "package/kernel/",
    "include/kernel.mk",
    "include/target.mk",
    "toolchain/",
)


def plan_for_paths(paths: list[str] | tuple[str, ...]) -> dict:
    """Conservatively list affected platforms and their mandatory evidence checks."""
    normalized = []
    for path in paths:
        value = str(path).replace("\\", "/")
        item = PurePosixPath(value)
        if item.is_absolute() or ".." in item.parts:
            raise ValueError(f"invalid changed path: {path}")
        normalized.append(item.as_posix())

    all_platforms = any(path.startswith(SHARED_PLATFORM_PREFIXES) for path in normalized)
    affected = set(PLATFORM_CHECKS) if all_platforms else set()
    for platform, prefixes in PLATFORM_PREFIXES.items():
        if any(path.startswith(prefix) or path == prefix.rstrip("/") for path in normalized for prefix in prefixes):
            affected.add(platform)

    return {
        "schema_version": 1,
        "platforms": {
            name: list(PLATFORM_CHECKS[name])
            for name in sorted(affected)
        },
    }
