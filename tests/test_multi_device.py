"""
Multi-device resilience tests: multiple GPUs, CPU-only, heterogeneous setups.

Verifies:
- check_vram_health reports status for all GPUs independently
- OK message covers all devices when multi-GPU, not just the first
- Dashboard labels use device names (not index numbers) for multi-GPU
- RAM check works regardless of multi-socket / NUMA topology
- Mixed-VRAM setups (one GPU with valid data, one with missing data)
"""

from __future__ import annotations

import pytest

from watchupdog.checks import check_vram_health, check_ram_health, _parse_system_stats
from watchupdog.dashboard import render_system_panel
from watchupdog.models import HealthStatus, SystemStats, DeviceInfo


_GiB = 1024 ** 3

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stats(devices: list[dict], ram_total: int = 0, ram_used: int = 0) -> dict:
    return {"devices": devices, "ram_total": ram_total, "ram_used": ram_used,
            "cpu_utilization": 10.0}


def _cuda(name: str, index: int, total_gib: float, free_gib: float) -> dict:
    return {
        "name": name, "type": "cuda", "index": index,
        "vram_total": int(total_gib * _GiB),
        "vram_free":  int(free_gib  * _GiB),
    }


# ---------------------------------------------------------------------------
# check_vram_health — multi-GPU
# ---------------------------------------------------------------------------

def test_two_gpus_both_ok():
    """Both GPUs within thresholds → OK, message mentions both."""
    stats = _stats([
        _cuda("RTX 4090", 0, 24, 10),   # ~58% used
        _cuda("RTX 3090", 1, 24, 12),   # ~50% used
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.OK
    assert "RTX 4090" in check.message
    assert "RTX 3090" in check.message


def test_two_gpus_first_ok_second_warn():
    """GPU 0 OK, GPU 1 above warn threshold → WARN, both names in message."""
    stats = _stats([
        _cuda("RTX 4090", 0, 24, 10),              # ~58% used → OK
        _cuda("RTX 3090", 1, 24, int(24 * 0.07)),  # ~93% used → WARN
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.WARN
    assert "RTX 3090" in check.message


def test_two_gpus_first_warn_second_critical():
    """Worst status escalates to CRITICAL even when one GPU is only WARN."""
    stats = _stats([
        _cuda("RTX 4090", 0, 24, int(24 * 0.08)),  # ~92% used → WARN
        _cuda("RTX 3090", 1, 24, int(24 * 0.01)),  # ~99% used → CRITICAL
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.CRITICAL
    assert "RTX 3090" in check.message
    assert "RTX 4090" in check.message


def test_two_gpus_first_critical_second_ok():
    """CRITICAL escalates even if the second GPU is fine."""
    stats = _stats([
        _cuda("GPU A", 0, 24, int(24 * 0.01)),  # ~99% used → CRITICAL
        _cuda("GPU B", 1, 24, 12),              # ~50% used → OK
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.CRITICAL


def test_four_gpus_all_ok_message_covers_all():
    """4-GPU system all healthy — message names every device."""
    gpus = [_cuda(f"GPU {i}", i, 16, 8) for i in range(4)]
    check, _ = check_vram_health(_stats(gpus), warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.OK
    for i in range(4):
        assert f"GPU {i}" in check.message


def test_two_gpus_one_missing_vram_data():
    """One GPU has valid VRAM data, second has vram_total=0 (missing field).

    The GPU with valid data is still checked. The unavailable one is noted in
    the message but does not produce a spurious CRITICAL/WARN.
    """
    stats = _stats([
        _cuda("RTX 4090", 0, 24, 12),         # 50% used → OK
        {"name": "Tesla T4", "type": "cuda", "index": 1},  # no vram fields
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.OK
    assert "RTX 4090" in check.message
    assert "Tesla T4" in check.message
    assert "unavailable" in check.message


def test_single_gpu_ok_message_format():
    """Single GPU still uses the short 'VRAM OK: X / Y GB' format."""
    stats = _stats([_cuda("RTX 4090", 0, 24, 12)])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert check.status == HealthStatus.OK
    assert "VRAM OK:" in check.message
    assert "RTX 4090" not in check.message  # short form: no device name prefix


def test_cpu_only_no_gpu():
    """No non-CPU devices → CPU-only mode, no VRAM check."""
    stats = _stats([{"name": "cpu", "type": "cpu", "index": 0,
                     "vram_total": 0, "vram_free": 0}])
    check, _ = check_vram_health(stats)
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


def test_empty_devices_list():
    stats = _stats([])
    check, _ = check_vram_health(stats)
    assert check.status == HealthStatus.OK
    assert "cpu" in check.message.lower()


# ---------------------------------------------------------------------------
# check_vram_health — WARN/CRIT details dict has all GPUs
# ---------------------------------------------------------------------------

def test_vram_details_contains_all_gpus():
    """details dict should have an entry for every GPU that was checked."""
    stats = _stats([
        _cuda("RTX 4090", 0, 24, int(24 * 0.08)),  # WARN
        _cuda("RTX 3090", 1, 24, 12),              # OK
    ])
    check, _ = check_vram_health(stats, warn_pct=90, critical_pct=97)
    assert "RTX 4090" in check.details
    assert "RTX 3090" in check.details


# ---------------------------------------------------------------------------
# check_ram_health — multi-socket / NUMA (OS reports aggregate)
# ---------------------------------------------------------------------------

def test_ram_check_uses_api_aggregate():
    """RAM data comes from the API as a single OS-level total.

    Multi-socket systems expose all memory as one aggregate — there's nothing
    NUMA-specific to test. Verify the check works for large (server) RAM sizes.
    """
    stats = _stats([], ram_total=512 * _GiB, ram_used=200 * _GiB)
    check = check_ram_health(stats, warn_pct=85)
    assert check.status == HealthStatus.OK
    assert "200.0" in check.message or "512.0" in check.message


def test_ram_warn_on_high_usage():
    stats = _stats([], ram_total=128 * _GiB, ram_used=int(128 * _GiB * 0.90))
    check = check_ram_health(stats, warn_pct=85)
    assert check.status == HealthStatus.WARN


# ---------------------------------------------------------------------------
# dashboard render_system_panel — multi-GPU label correctness
# ---------------------------------------------------------------------------

def _system_stats_from_devices(devices: list[dict]) -> SystemStats:
    parsed = _parse_system_stats({"devices": devices, "cpu_utilization": 5.0,
                                  "ram_total": 32 * _GiB, "ram_used": 8 * _GiB})
    return parsed


def _panel_text(stats: SystemStats) -> str:
    """Render the system panel to a plain string for assertion."""
    from rich.console import Console
    from io import StringIO
    buf = StringIO()
    c = Console(file=buf, highlight=False, no_color=True, width=120)
    c.print(render_system_panel(stats))
    return buf.getvalue()


def test_dashboard_single_gpu_label_is_vram():
    stats = _system_stats_from_devices([_cuda("RTX 4090", 0, 24, 12)])
    text = _panel_text(stats)
    assert "VRAM" in text
    # Should NOT embed device name in the VRAM label for single-GPU
    # (device name may appear elsewhere in the table but "VRAM" should be the label)


def test_dashboard_two_different_gpus_show_device_names():
    """Heterogeneous 2-GPU: labels should be device names, not 'VRAM 0' / 'VRAM 1'."""
    stats = _system_stats_from_devices([
        _cuda("RTX 4090", 0, 24, 12),
        _cuda("RTX 3090", 1, 24, 10),
    ])
    text = _panel_text(stats)
    assert "RTX 4090" in text
    assert "RTX 3090" in text
    assert "VRAM 0" not in text
    assert "VRAM 1" not in text


def test_dashboard_two_identical_gpus_disambiguated():
    """Two GPUs with the same name get an index suffix to distinguish them."""
    stats = _system_stats_from_devices([
        _cuda("RTX 3090", 0, 24, 12),
        _cuda("RTX 3090", 1, 24, 10),
    ])
    text = _panel_text(stats)
    # Both should appear with an index distinguisher, not ambiguous duplicates
    assert "RTX 3090" in text
    assert "[0]" in text
    assert "[1]" in text


def test_dashboard_four_gpus_all_shown():
    """All 4 GPUs should have their VRAM row rendered."""
    gpus = [_cuda(f"A100 #{i}", i, 80, 40) for i in range(4)]
    stats = _system_stats_from_devices(gpus)
    text = _panel_text(stats)
    for i in range(4):
        assert f"A100 #{i}" in text


def test_dashboard_cpu_only_no_vram_rows():
    """CPU-only device doesn't generate a VRAM row."""
    stats = _system_stats_from_devices([
        {"name": "cpu", "type": "cpu", "index": 0, "vram_total": 0, "vram_free": 0}
    ])
    text = _panel_text(stats)
    assert "VRAM" not in text


def test_dashboard_gpu_with_zero_vram_shows_name():
    """GPU with missing vram_total shows device name (not a VRAM bar)."""
    stats = _system_stats_from_devices([
        {"name": "Tesla T4", "type": "cuda", "index": 0}
    ])
    text = _panel_text(stats)
    assert "Tesla T4" in text
