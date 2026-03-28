"""Unit tests for health check functions."""

from __future__ import annotations

import pytest

from watchupdog.checks import (
    check_connectivity,
    check_error_rate,
    check_model_files,
    check_nunchaku_nodes,
    check_queue_health,
    check_ram_health,
    check_stale_jobs,
    check_vram_health,
)
from watchupdog.models import HealthStatus
from watchupdog.nunchaku import detect_nunchaku

from .fixtures import (
    SAMPLE_HISTORY_ALL_OK,
    SAMPLE_HISTORY_WITH_ERRORS,
    SAMPLE_OBJECT_INFO_NO_NUNCHAKU,
    SAMPLE_OBJECT_INFO_WHEEL_ONLY,
    SAMPLE_OBJECT_INFO_WITH_NUNCHAKU,
    SAMPLE_QUEUE_BUSY,
    SAMPLE_QUEUE_EMPTY,
    SAMPLE_SYSTEM_STATS,
    SAMPLE_SYSTEM_STATS_HIGH_VRAM,
)

# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

def test_connectivity_ok():
    result = check_connectivity(SAMPLE_SYSTEM_STATS)
    assert result.status == HealthStatus.OK


def test_connectivity_fail():
    result = check_connectivity(None)
    assert result.status == HealthStatus.CRITICAL


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def test_queue_empty():
    check, stats = check_queue_health(SAMPLE_QUEUE_EMPTY, warn_threshold=10)
    assert check.status == HealthStatus.OK
    assert stats.pending_count == 0
    assert stats.running_count == 0


def test_queue_over_threshold():
    check, stats = check_queue_health(SAMPLE_QUEUE_BUSY, warn_threshold=10)
    assert check.status == HealthStatus.WARN
    assert stats.pending_count == 13


def test_queue_under_threshold():
    check, stats = check_queue_health(SAMPLE_QUEUE_BUSY, warn_threshold=20)
    assert check.status == HealthStatus.OK


def test_queue_none():
    check, stats = check_queue_health(None)
    assert check.status == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# VRAM
# ---------------------------------------------------------------------------

def test_vram_ok():
    check, _ = check_vram_health(SAMPLE_SYSTEM_STATS, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.OK


def test_vram_critical():
    check, _ = check_vram_health(SAMPLE_SYSTEM_STATS_HIGH_VRAM, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.CRITICAL


def test_vram_warn():
    # Create a stat at ~92%
    stats = {
        "devices": [
            {
                "name": "RTX 4090",
                "type": "cuda",
                "index": 0,
                "vram_total": 24 * 1024**3,
                "vram_free": int(24 * 1024**3 * 0.08),  # 8% free = 92% used
                "torch_vram_total": 0,
                "torch_vram_free": 0,
            }
        ]
    }
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.WARN


def test_vram_none():
    check, _ = check_vram_health(None)
    assert check.status == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------

def test_ram_ok():
    check = check_ram_health(SAMPLE_SYSTEM_STATS, warn_pct=85)
    # 8 GB used / 32 GB total = 25% → OK
    assert check.status == HealthStatus.OK


def test_ram_warn():
    stats = {
        "ram_total": 16 * 1024**3,
        "ram_used": int(16 * 1024**3 * 0.90),  # 90%
        "devices": [],
    }
    check = check_ram_health(stats, warn_pct=85)
    assert check.status == HealthStatus.WARN


# ---------------------------------------------------------------------------
# Nunchaku detection
# ---------------------------------------------------------------------------

def test_nunchaku_all_nodes():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.dit_loader_present
    assert info.text_encoder_present
    assert info.lora_loader_present
    assert not info.wheel_installer_present


def test_nunchaku_precision_from_object_info():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    # The fixture includes "svdq-int4_r32-flux.1-dev.safetensors"
    assert info.precision_mode == "INT4"


def test_nunchaku_version_from_description():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.version == "0.3.2"


def test_nunchaku_fb_cache_detected():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    assert info.fb_cache_enabled


def test_nunchaku_no_nodes():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_NO_NUNCHAKU)
    assert not info.dit_loader_present
    assert not info.nodes_found


def test_nunchaku_none():
    info = detect_nunchaku(None)
    assert not info.dit_loader_present


# ---------------------------------------------------------------------------
# Nunchaku node check
# ---------------------------------------------------------------------------

def test_nunchaku_check_ok():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU)
    check = check_nunchaku_nodes(info)
    assert check.status == HealthStatus.OK


def test_nunchaku_check_no_nodes():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_NO_NUNCHAKU)
    check = check_nunchaku_nodes(info)
    assert check.status == HealthStatus.WARN


def test_nunchaku_check_wheel_only():
    info = detect_nunchaku(SAMPLE_OBJECT_INFO_WHEEL_ONLY)
    check = check_nunchaku_nodes(info)
    assert check.status == HealthStatus.WARN
    assert "wheel" in check.message.lower()


# ---------------------------------------------------------------------------
# Error rate
# ---------------------------------------------------------------------------

def test_error_rate_zero():
    check, stats = check_error_rate(SAMPLE_HISTORY_ALL_OK)
    assert check.status == HealthStatus.OK
    assert stats.error_rate_pct == 0.0
    assert stats.failed_jobs == 0


def test_error_rate_with_failures():
    check, stats = check_error_rate(SAMPLE_HISTORY_WITH_ERRORS)
    assert stats.failed_jobs == 2
    assert stats.total_jobs == 10
    assert stats.error_rate_pct == 20.0


def test_error_rate_none():
    check, stats = check_error_rate(None)
    assert check.status == HealthStatus.UNKNOWN


def test_error_rate_empty():
    check, stats = check_error_rate({})
    assert check.status == HealthStatus.OK
    assert stats.total_jobs == 0


# ---------------------------------------------------------------------------
# Stale jobs
# ---------------------------------------------------------------------------

def test_stale_jobs_no_running():
    check = check_stale_jobs(SAMPLE_QUEUE_EMPTY)
    assert check.status == HealthStatus.OK


def test_stale_jobs_with_running():
    check = check_stale_jobs(SAMPLE_QUEUE_BUSY, stale_minutes=5)
    # We don't have timestamps in queue, so it should report running but not error
    assert check.status == HealthStatus.OK
    assert "running" in check.message.lower()


def test_stale_jobs_none():
    check = check_stale_jobs(None)
    assert check.status == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Model files
# ---------------------------------------------------------------------------

def test_model_files_found():
    check = check_model_files(
        SAMPLE_OBJECT_INFO_WITH_NUNCHAKU,
        expected_models={
            "flux": ["svdq-int4_r32-flux.1-dev.safetensors"],
            "clip": ["clip_l.safetensors"],
        },
    )
    assert check.status == HealthStatus.OK


def test_model_files_not_found():
    check = check_model_files(
        SAMPLE_OBJECT_INFO_NO_NUNCHAKU,
        expected_models={
            "flux": ["svdq-int4_r32-flux.1-dev.safetensors"],
        },
    )
    assert check.status == HealthStatus.WARN


def test_model_files_none_object_info():
    check = check_model_files(None)
    assert check.status == HealthStatus.UNKNOWN


def test_model_files_no_expected():
    check = check_model_files(SAMPLE_OBJECT_INFO_WITH_NUNCHAKU, expected_models=None)
    assert check.status == HealthStatus.OK
