"""Unit tests for watchupdog.html_export."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from watchupdog.html_export import export_html
from watchupdog.models import FullHealthReport, HealthStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(
    status: HealthStatus = HealthStatus.OK,
    alerts: list[str] | None = None,
    url: str = "http://localhost:8189",
    timestamp: str = "2026-01-01 00:00:00 UTC",
) -> FullHealthReport:
    report = FullHealthReport(
        comfyui_url=url,
        timestamp=timestamp,
    )
    report.overall_status = status
    report.alerts = alerts or []
    return report


def _make_system_stats(vram_used_mb: int = 4096, vram_total_mb: int = 24576) -> types.SimpleNamespace:
    """
    Build a minimal system_stats object matching the fields accessed by html_export:
      s.devices — list with .vram_used_mb, .vram_total_mb, .name
      s.ram_used_mb, s.ram_total_mb
      s.python_version
    """
    device = types.SimpleNamespace(
        name="NVIDIA GeForce RTX 4090",
        vram_used_mb=vram_used_mb,
        vram_total_mb=vram_total_mb,
    )
    return types.SimpleNamespace(
        devices=[device],
        ram_used_mb=8192,
        ram_total_mb=32768,
        python_version="3.11.9",
    )


# ---------------------------------------------------------------------------
# 1. export_html creates a file
# ---------------------------------------------------------------------------

def test_export_html_creates_file(tmp_path):
    report = _make_report()
    out = tmp_path / "report.html"
    result = export_html(report, out)
    assert out.exists()
    assert result == out


# ---------------------------------------------------------------------------
# 2. Output is valid HTML
# ---------------------------------------------------------------------------

def test_export_html_valid_html_structure(tmp_path):
    report = _make_report()
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "<html" in content
    assert "</html>" in content


# ---------------------------------------------------------------------------
# 3. Status embedded in output
# ---------------------------------------------------------------------------

def test_export_html_critical_status_in_output(tmp_path):
    report = _make_report(status=HealthStatus.CRITICAL)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "CRITICAL" in content


def test_export_html_warn_status_in_output(tmp_path):
    report = _make_report(status=HealthStatus.WARN)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "WARN" in content


def test_export_html_ok_status_in_output(tmp_path):
    report = _make_report(status=HealthStatus.OK)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "OK" in content


# ---------------------------------------------------------------------------
# 4. ComfyUI URL appears in output
# ---------------------------------------------------------------------------

def test_export_html_url_in_output(tmp_path):
    url = "http://comfyui.example.com:8188"
    report = _make_report(url=url)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert url in content


# ---------------------------------------------------------------------------
# 5. Alerts appear in output
# ---------------------------------------------------------------------------

def test_export_html_alerts_in_output(tmp_path):
    report = _make_report(
        status=HealthStatus.CRITICAL,
        alerts=["VRAM usage is critical", "Queue is overloaded"],
    )
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "VRAM usage is critical" in content
    assert "Queue is overloaded" in content


def test_export_html_multiple_alerts_all_present(tmp_path):
    alerts = [f"Alert number {i}" for i in range(5)]
    report = _make_report(status=HealthStatus.WARN, alerts=alerts)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    for alert in alerts:
        assert alert in content


# ---------------------------------------------------------------------------
# 6. No alerts → no alerts section content
# ---------------------------------------------------------------------------

def test_export_html_no_alerts_no_alerts_section(tmp_path):
    report = _make_report(status=HealthStatus.OK, alerts=[])
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    # The alerts div class only appears when there are alerts
    assert 'class="alerts"' not in content


# ---------------------------------------------------------------------------
# 7. UTF-8, no raw Python objects
# ---------------------------------------------------------------------------

def test_export_html_is_utf8(tmp_path):
    report = _make_report()
    out = tmp_path / "report.html"
    export_html(report, out)
    # Must be readable as UTF-8 without errors
    content = out.read_bytes().decode("utf-8")
    assert len(content) > 0


def test_export_html_no_raw_python_class_repr(tmp_path):
    report = _make_report()
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert "<class" not in content
    assert "object at 0x" not in content


def test_export_html_special_chars_escaped(tmp_path):
    report = _make_report(
        url="http://localhost:8189",
        alerts=["<script>alert('xss')</script>"],
        status=HealthStatus.WARN,
    )
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    # Raw script tag must not appear unescaped
    assert "<script>alert" not in content


# ---------------------------------------------------------------------------
# 8. system_stats VRAM info appears in output
# ---------------------------------------------------------------------------

def test_export_html_system_stats_vram_in_output(tmp_path):
    report = _make_report(status=HealthStatus.OK)
    report.system_stats = _make_system_stats(vram_used_mb=8192, vram_total_mb=24576)

    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")

    # VRAM section heading
    assert "VRAM" in content
    # GPU name
    assert "RTX 4090" in content


def test_export_html_system_stats_device_name_in_output(tmp_path):
    report = _make_report(status=HealthStatus.OK)
    report.system_stats = _make_system_stats()

    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")

    assert "NVIDIA GeForce RTX 4090" in content


def test_export_html_system_stats_python_version_in_output(tmp_path):
    report = _make_report(status=HealthStatus.OK)
    report.system_stats = _make_system_stats()

    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")

    assert "3.11.9" in content


def test_export_html_no_system_stats_shows_unavailable(tmp_path):
    report = _make_report(status=HealthStatus.OK)
    report.system_stats = None

    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")

    assert "unavailable" in content.lower() or "offline" in content.lower()


# ---------------------------------------------------------------------------
# 9. timestamp appears in output
# ---------------------------------------------------------------------------

def test_export_html_timestamp_in_output(tmp_path):
    ts = "2026-03-15 12:34:56 UTC"
    report = _make_report(timestamp=ts)
    out = tmp_path / "report.html"
    export_html(report, out)
    content = out.read_text(encoding="utf-8")
    assert ts in content


# ---------------------------------------------------------------------------
# 10. Returns the path object
# ---------------------------------------------------------------------------

def test_export_html_returns_path(tmp_path):
    report = _make_report()
    out = tmp_path / "result.html"
    returned = export_html(report, out)
    assert isinstance(returned, Path)
    assert returned == out
