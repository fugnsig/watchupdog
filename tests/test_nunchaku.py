"""Unit tests for nunchaku detection logic."""

from __future__ import annotations

import pytest

from watchupdog.nunchaku import (
    check_nunchaku_vram_anomaly,
    detect_nunchaku,
    get_precision_from_system_stats,
)

from .fixtures import (
    SAMPLE_OBJECT_INFO_NO_NUNCHAKU,
    SAMPLE_OBJECT_INFO_WHEEL_ONLY,
    SAMPLE_OBJECT_INFO_WITH_NUNCHAKU,
)

GB = 1024**3


def test_detect_all_nodes():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.dit_loader_present
    assert info.text_encoder_present
    assert info.lora_loader_present
    assert not info.wheel_installer_present
    assert len(info.nodes_found) == 3


def test_detect_no_nodes():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_NO_NUNCHAKU)
    assert not info.dit_loader_present
    assert info.nodes_found == []


def test_detect_wheel_only():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WHEEL_ONLY)
    assert info.wheel_installer_present
    assert not info.dit_loader_present


def test_detect_none():
    info = detect_nunchaku(None)
    assert not info.dit_loader_present
    assert info.nodes_found == []


def test_precision_int4():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.precision_mode == "INT4"


def test_precision_fp4():
    obj_info = {
        "NunchakuFluxDiTLoader": {
            "input": {
                "required": {
                    "model": [["svdq-fp4_r32-flux.1-dev.safetensors"]],
                }
            },
            "description": "",
            "output": [],
        }
    }
    info = detect_nunchaku(obj_info)
    assert info.precision_mode == "FP4"


def test_precision_from_system_stats():
    # SVDQ pattern may appear in device name on some ComfyUI builds
    system_stats = {
        "devices": [{"name": "svdq-fp4_r32-flux.1-dev.safetensors", "type": "cuda"}],
    }
    prec = get_precision_from_system_stats(system_stats)
    assert prec == "FP4"


def test_precision_from_system_stats_no_match():
    system_stats = {
        "devices": [{"name": "RTX 5090", "type": "cuda"}],
    }
    prec = get_precision_from_system_stats(system_stats)
    assert prec is None


def test_fb_cache_detected():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.fb_cache_enabled


def test_fb_cache_not_detected():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_NO_NUNCHAKU)
    assert not info.fb_cache_enabled


def test_version_extracted():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.version == "0.3.2"


def test_vram_anomaly_flagged():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    # 15 GB used on a 16 GB card with Nunchaku loaded → anomaly
    msg = check_nunchaku_vram_anomaly(info, 15 * GB, 16 * GB)
    assert msg is not None
    assert "non-quantised" in msg.lower() or "fallback" in msg.lower()


def test_vram_anomaly_ok():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    # 7 GB used on a 16 GB card → within expected range
    msg = check_nunchaku_vram_anomaly(info, 7 * GB, 16 * GB)
    assert msg is None


def test_vram_anomaly_no_nunchaku():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_NO_NUNCHAKU)
    msg = check_nunchaku_vram_anomaly(info, 15 * GB, 16 * GB)
    assert msg is None


def test_vram_anomaly_small_card():
    # 8 GB card — don't flag it (nunchaku expected to use more on smaller cards)
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    msg = check_nunchaku_vram_anomaly(info, 7 * GB, 8 * GB)
    assert msg is None
