"""Nunchaku-specific detection logic."""

from __future__ import annotations

import re
from typing import Any

from .config import DEFAULT_CONFIG
from .models import NunchakuInfo

# Single source of truth lives in config.py DEFAULT_CONFIG["nunchaku_nodes"].
# Build a frozenset here for O(1) membership tests in detect_nunchaku().
# Do NOT add node names here — add them to DEFAULT_CONFIG instead so the
# config system (and user TOML overrides) stay authoritative.
NUNCHAKU_NODES: frozenset[str] = frozenset(DEFAULT_CONFIG["nunchaku_nodes"])

# Pattern: svdq-{precision}_r{rank}-flux.1-dev.safetensors
_SVDQ_RE = re.compile(r"svdq-(fp4|int4)_r(\d+)-flux\.1-dev\.safetensors", re.IGNORECASE)


def detect_nunchaku(object_info: dict[str, Any] | None) -> NunchakuInfo:
    """Scan /object_info response for Nunchaku nodes and capabilities."""
    info = NunchakuInfo()
    if not object_info:
        return info

    found: list[str] = []
    for node_name in object_info:
        if node_name in NUNCHAKU_NODES:
            found.append(node_name)

    info.nodes_found = found
    info.dit_loader_present = "NunchakuFluxDiTLoader" in found
    info.text_encoder_present = "NunchakuTextEncoderLoader" in found
    info.lora_loader_present = "NunchakuFluxLoraLoader" in found
    info.wheel_installer_present = "NunchakuWheelInstaller" in found

    # Try to extract version from node metadata
    for node_name in found:
        node_data = object_info.get(node_name)
        if not isinstance(node_data, dict):
            node_data = {}
        ver = _extract_version(node_data)
        if ver:
            info.version = ver
            break

    # Detect precision from model filenames in node input definitions
    info.precision_mode = _detect_precision(object_info)

    # Detect FB Cache — check if any node has fb_cache or first_block_cache in inputs
    info.fb_cache_enabled = _detect_fb_cache(object_info)

    return info


def _extract_version(node_data: dict[str, Any]) -> str | None:
    """Try to find a version string in node metadata."""
    desc = node_data.get("description", "")
    if isinstance(desc, str):
        m = re.search(r"v?(\d+\.\d+[\.\d]*)", desc)
        if m:
            return m.group(1)
    return None


def _detect_precision(object_info: dict[str, Any]) -> str | None:
    """Search object_info node input choices for SVDQ model filenames to infer precision."""
    for node_data in object_info.values():
        if not isinstance(node_data, dict):
            continue
        input_raw = node_data.get("input")
        if not isinstance(input_raw, dict):
            continue
        for group in input_raw.values():
            if not isinstance(group, dict):
                continue
            for spec in group.values():
                if not isinstance(spec, list) or not spec:
                    continue
                choices = spec[0]
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if isinstance(choice, str):
                        m = _SVDQ_RE.search(choice)
                        if m:
                            return m.group(1).upper()
    return None


def _detect_fb_cache(object_info: dict[str, Any]) -> bool:
    """Return True if any node exposes fb_cache / first_block_cache input."""
    for node_data in object_info.values():
        if not isinstance(node_data, dict):
            continue
        input_raw = node_data.get("input")
        if not isinstance(input_raw, dict):
            continue
        for group in input_raw.values():
            if not isinstance(group, dict):
                continue
            for key in group:
                if "fb_cache" in key.lower() or "first_block_cache" in key.lower():
                    return True
    return False


def check_nunchaku_vram_anomaly(
    nunchaku: NunchakuInfo,
    vram_used_bytes: int,
    vram_total_bytes: int,
    *,
    anomaly_gb: float = 14.0,
    min_card_gb: float = 12.0,
) -> str | None:
    """Return a warning string if VRAM usage looks wrong for Nunchaku.

    Expected: 6–8 GB on a 16 GB card.  If usage exceeds *anomaly_gb* on a
    card with at least *min_card_gb* total VRAM, a non-quantised fallback is
    suspected.

    Both thresholds are configurable via ``[thresholds]`` in comfyui-health.toml
    (``nunchaku_vram_anomaly_gb`` and ``nunchaku_min_card_gb``) and are passed
    in by the caller; the defaults here match DEFAULT_CONFIG.
    """
    if not nunchaku.dit_loader_present:
        return None
    if vram_total_bytes <= 0:
        return None

    vram_used_gb  = vram_used_bytes  / (1024 ** 3)
    vram_total_gb = vram_total_bytes / (1024 ** 3)

    if vram_total_gb >= min_card_gb and vram_used_gb > anomaly_gb:
        return (
            f"Nunchaku loaded but VRAM usage is {vram_used_gb:.1f} GB "
            f"(expected ~6-8 GB). Possible non-quantised fallback."
        )
    return None


