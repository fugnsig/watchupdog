"""Tests for config loader."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from watchupdog.config import load_config, DEFAULT_CONFIG


def test_defaults(tmp_path, monkeypatch):
    # Change cwd to a temp dir so no local watchupdog.toml is picked up
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.url == "http://127.0.0.1:8188"
    assert cfg.interval == 5
    assert cfg.thresholds["vram_warn_pct"] == 90
    assert cfg.thresholds["vram_critical_pct"] == 97
    assert cfg.thresholds["ram_warn_pct"] == 85
    assert cfg.thresholds["queue_warn"] == 10
    assert "NunchakuFluxDiTLoader" in cfg.nunchaku_nodes


def test_override_from_file():
    toml_content = b"""
url = "http://192.168.1.50:8189"
interval = 10

[thresholds]
vram_warn_pct = 80
queue_warn = 5
"""
    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
        f.write(toml_content)
        tmp_path = f.name

    cfg = load_config(tmp_path)
    assert cfg.url == "http://192.168.1.50:8189"
    assert cfg.interval == 10
    assert cfg.thresholds["vram_warn_pct"] == 80
    assert cfg.thresholds["queue_warn"] == 5
    # Non-overridden defaults preserved
    assert cfg.thresholds["ram_warn_pct"] == 85

    Path(tmp_path).unlink()


def test_missing_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config("/nonexistent/path/config.toml")
    assert cfg.url == DEFAULT_CONFIG["url"]


def test_invalid_toml_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("NOT VALID TOML @@@@")

    cfg = load_config(str(bad_toml))
    assert cfg.url == DEFAULT_CONFIG["url"]


def test_expected_models_default_empty():
    # expected_models is intentionally empty by default — users populate it in watchupdog.toml
    cfg = load_config()
    assert isinstance(cfg.expected_models, dict)
