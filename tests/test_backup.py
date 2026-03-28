"""Unit tests for watchupdog.backup."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watchupdog.backup import (
    _classify_packages,
    create_snapshot,
    diff_snapshots,
    list_snapshots,
    restore_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_snapshot(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal_snapshot(timestamp: str, note: str = "", packages: list[str] | None = None) -> dict:
    return {
        "timestamp": timestamp,
        "note": note,
        "restorable": {
            "pypi": packages or [],
            "local_wheels": [],
            "editable": [],
        },
        "key_packages": {},
        "custom_nodes": [],
        "models": {},
        "packages": packages or [],
        "package_count": len(packages or []),
    }


# ---------------------------------------------------------------------------
# diff_snapshots — package changes
# ---------------------------------------------------------------------------

def test_diff_snapshots_added_removed_changed():
    snap_a = _minimal_snapshot("20260101T000000Z", packages=["numpy==1.24.0", "requests==2.28.0"])
    snap_b = _minimal_snapshot("20260102T000000Z", packages=["numpy==1.25.0", "httpx==0.27.0"])
    result = diff_snapshots(snap_a, snap_b)

    pkgs = result["packages"]
    added_names = [p["name"] for p in pkgs["added"]]
    removed_names = [p["name"] for p in pkgs["removed"]]
    changed_names = [p["name"] for p in pkgs["changed"]]

    assert "httpx" in added_names
    assert "requests" in removed_names
    assert "numpy" in changed_names

    numpy_change = next(p for p in pkgs["changed"] if p["name"] == "numpy")
    assert numpy_change["from"] == "1.24.0"
    assert numpy_change["to"] == "1.25.0"


def test_diff_snapshots_summary_string():
    snap_a = _minimal_snapshot("20260101T000000Z", packages=["numpy==1.24.0", "requests==2.28.0"])
    snap_b = _minimal_snapshot("20260102T000000Z", packages=["numpy==1.25.0", "httpx==0.27.0"])
    result = diff_snapshots(snap_a, snap_b)
    summary = result["packages"]["summary"]
    assert "+1 added" in summary
    assert "-1 removed" in summary
    assert "~1 changed" in summary


def test_diff_snapshots_no_changes():
    pkgs = ["torch==2.3.0", "numpy==1.26.0"]
    snap_a = _minimal_snapshot("20260101T000000Z", packages=pkgs)
    snap_b = _minimal_snapshot("20260102T000000Z", packages=pkgs)
    result = diff_snapshots(snap_a, snap_b)

    assert result["packages"]["added"] == []
    assert result["packages"]["removed"] == []
    assert result["packages"]["changed"] == []


# ---------------------------------------------------------------------------
# diff_snapshots — key_packages changes
# ---------------------------------------------------------------------------

def test_diff_snapshots_key_packages_changes():
    snap_a = {**_minimal_snapshot("20260101T000000Z"), "key_packages": {"torch": "2.2.0", "numpy": "1.24.0"}}
    snap_b = {**_minimal_snapshot("20260102T000000Z"), "key_packages": {"torch": "2.3.0", "numpy": "1.24.0"}}
    result = diff_snapshots(snap_a, snap_b)

    kp = result["key_packages"]
    assert any(c["key"] == "torch" for c in kp)
    torch_change = next(c for c in kp if c["key"] == "torch")
    assert torch_change["from"] == "2.2.0"
    assert torch_change["to"] == "2.3.0"

    # numpy unchanged — must not appear
    assert not any(c["key"] == "numpy" for c in kp)


def test_diff_snapshots_key_packages_absent_value():
    snap_a = {**_minimal_snapshot("20260101T000000Z"), "key_packages": {"torch": "2.2.0"}}
    snap_b = {**_minimal_snapshot("20260102T000000Z"), "key_packages": {"torch": "2.2.0", "xformers": "0.0.26"}}
    result = diff_snapshots(snap_a, snap_b)
    kp = result["key_packages"]
    xformers_change = next(c for c in kp if c["key"] == "xformers")
    assert xformers_change["from"] == "(absent)"
    assert xformers_change["to"] == "0.0.26"


# ---------------------------------------------------------------------------
# diff_snapshots — custom_nodes git hash changes
# ---------------------------------------------------------------------------

def test_diff_snapshots_custom_node_hash_changes():
    snap_a = {
        **_minimal_snapshot("20260101T000000Z"),
        "custom_nodes": [
            {"name": "ComfyUI-Manager", "git_hash": "aabbccdd1111"},
            {"name": "ComfyUI-Impact", "git_hash": "deadbeef1234"},
        ],
    }
    snap_b = {
        **_minimal_snapshot("20260102T000000Z"),
        "custom_nodes": [
            {"name": "ComfyUI-Manager", "git_hash": "aabbccdd9999"},
            {"name": "ComfyUI-Impact", "git_hash": "deadbeef1234"},
        ],
    }
    result = diff_snapshots(snap_a, snap_b)
    node_changes = result["custom_nodes"]
    assert len(node_changes) == 1
    assert node_changes[0]["name"] == "ComfyUI-Manager"
    assert node_changes[0]["from"] == "aabbccdd11"  # truncated to 10 chars
    assert node_changes[0]["to"] == "aabbccdd99"


def test_diff_snapshots_custom_node_added():
    snap_a = {**_minimal_snapshot("20260101T000000Z"), "custom_nodes": []}
    snap_b = {
        **_minimal_snapshot("20260102T000000Z"),
        "custom_nodes": [{"name": "new-node", "git_hash": "cafecafe1234"}],
    }
    result = diff_snapshots(snap_a, snap_b)
    node_changes = result["custom_nodes"]
    assert len(node_changes) == 1
    assert node_changes[0]["name"] == "new-node"
    assert node_changes[0]["from"] == "(absent)"


# ---------------------------------------------------------------------------
# diff_snapshots — error handling
# ---------------------------------------------------------------------------

def test_diff_snapshots_raises_when_no_snapshots_on_disk(tmp_path, monkeypatch):
    """When both args are None and no snapshots exist on disk, raises ValueError."""
    monkeypatch.setattr("watchupdog.backup.list_snapshots", lambda: [])
    with pytest.raises(ValueError, match="Need at least"):
        diff_snapshots(None, None)


def test_diff_snapshots_raises_when_only_one_snapshot(monkeypatch):
    single = _minimal_snapshot("20260101T000000Z")
    monkeypatch.setattr("watchupdog.backup.list_snapshots", lambda: [single])
    with pytest.raises(ValueError, match="Need at least"):
        diff_snapshots(None, None)


# ---------------------------------------------------------------------------
# _classify_packages
# ---------------------------------------------------------------------------

def test_classify_packages_pypi():
    lines = ["numpy==1.24.0", "requests==2.28.0", "torch==2.3.0"]
    pypi, local, editable = _classify_packages(lines)
    assert pypi == lines
    assert local == []
    assert editable == []


def test_classify_packages_editable():
    lines = ["-e git+https://github.com/user/repo.git@main#egg=mypackage"]
    pypi, local, editable = _classify_packages(lines)
    assert editable == lines
    assert pypi == []
    assert local == []


def test_classify_packages_local_file_url():
    lines = ["mywheel @ file:///home/user/wheels/mypackage-1.0-py3-none-any.whl"]
    pypi, local, editable = _classify_packages(lines)
    assert local == lines
    assert pypi == []
    assert editable == []


def test_classify_packages_local_windows_path():
    lines = [r"mywheel @ C:\Users\user\wheels\mypackage-1.0.whl"]
    pypi, local, editable = _classify_packages(lines)
    assert local == lines
    assert pypi == []


def test_classify_packages_mixed():
    lines = [
        "numpy==1.24.0",
        "-e /home/user/myrepo",
        "localwhl @ file:///tmp/foo-1.0.whl",
        "requests==2.28.0",
    ]
    pypi, local, editable = _classify_packages(lines)
    assert len(pypi) == 2
    assert len(local) == 1
    assert len(editable) == 1


# ---------------------------------------------------------------------------
# create_snapshot
# ---------------------------------------------------------------------------

def test_create_snapshot_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("watchupdog.backup._BACKUP_DIR", tmp_path)
    monkeypatch.setattr(
        "watchupdog.backup._freeze",
        lambda exe: ["numpy==1.24.0", "requests==2.28.0"],
    )
    monkeypatch.setattr(
        "watchupdog.backup._collect_hardware",
        lambda: {"gpu_name": "RTX 4090"},
    )
    monkeypatch.setattr(
        "watchupdog.backup._collect_environment",
        lambda exe: {"python_exe": exe, "python_version": "Python 3.11.0", "pip_version": "23.0", "os": "Linux", "machine": "x86_64"},
    )
    monkeypatch.setattr(
        "watchupdog.backup._collect_key_packages",
        lambda exe: {"torch": "2.3.0"},
    )

    result_path = create_snapshot(python_exe=sys.executable, note="test snapshot")

    assert result_path.exists()
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert "timestamp" in data
    assert "restorable" in data
    assert "key_packages" in data
    assert "environment" in data
    assert "hardware" in data
    assert "packages" in data
    assert data["note"] == "test snapshot"


def test_create_snapshot_classifies_packages(tmp_path, monkeypatch):
    monkeypatch.setattr("watchupdog.backup._BACKUP_DIR", tmp_path)
    freeze_output = [
        "numpy==1.24.0",
        "-e /home/user/repo",
        "localwhl @ file:///tmp/foo-1.0.whl",
    ]
    monkeypatch.setattr("watchupdog.backup._freeze", lambda exe: freeze_output)
    monkeypatch.setattr("watchupdog.backup._collect_hardware", lambda: {})
    monkeypatch.setattr("watchupdog.backup._collect_environment", lambda exe: {})
    monkeypatch.setattr("watchupdog.backup._collect_key_packages", lambda exe: {})

    result_path = create_snapshot(python_exe=sys.executable)
    data = json.loads(result_path.read_text(encoding="utf-8"))

    restorable = data["restorable"]
    assert "numpy==1.24.0" in restorable["pypi"]
    assert len(restorable["editable"]) == 1
    assert len(restorable["local_wheels"]) == 1


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------

def test_list_snapshots_returns_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr("watchupdog.backup._BACKUP_DIR", tmp_path)

    for ts in ["20260101T000000Z", "20260103T000000Z", "20260102T000000Z"]:
        snap = _minimal_snapshot(ts)
        (tmp_path / f"pip_state_{ts}.json").write_text(json.dumps(snap), encoding="utf-8")

    snaps = list_snapshots()
    timestamps = [s["timestamp"] for s in snaps]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_snapshots_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("watchupdog.backup._BACKUP_DIR", tmp_path)
    assert list_snapshots() == []


# ---------------------------------------------------------------------------
# restore_snapshot — dry run
# ---------------------------------------------------------------------------

def test_restore_snapshot_dry_run_returns_true(tmp_path):
    snap = _minimal_snapshot(
        "20260101T000000Z",
        packages=["numpy==1.24.0", "requests==2.28.0", "torch==2.3.0"],
    )
    snap_file = tmp_path / "pip_state_20260101T000000Z.json"
    snap_file.write_text(json.dumps(snap), encoding="utf-8")

    success, messages = restore_snapshot(snapshot_path=snap_file, dry_run=True)

    assert success is True
    assert any("Dry run" in m for m in messages)


def test_restore_snapshot_dry_run_message_count(tmp_path):
    packages = [f"pkg{i}==1.{i}.0" for i in range(15)]
    snap = _minimal_snapshot("20260101T000000Z", packages=packages)
    snap_file = tmp_path / "pip_state_20260101T000000Z.json"
    snap_file.write_text(json.dumps(snap), encoding="utf-8")

    success, messages = restore_snapshot(snapshot_path=snap_file, dry_run=True)

    assert success is True
    # Should have base messages + up to 10 "would install" lines + "... and N more"
    assert any("would install" in m for m in messages)
    assert any("more" in m for m in messages)


def test_restore_snapshot_no_snapshots():
    with patch("watchupdog.backup.list_snapshots", return_value=[]):
        success, messages = restore_snapshot()
    assert success is False
    assert any("No snapshots" in m for m in messages)


def test_restore_snapshot_invalid_path(tmp_path):
    bad_path = tmp_path / "nonexistent.json"
    success, messages = restore_snapshot(snapshot_path=bad_path)
    assert success is False
    assert any("Could not read" in m for m in messages)


def test_restore_snapshot_skips_editable(tmp_path):
    snap = {
        "timestamp": "20260101T000000Z",
        "note": "",
        "restorable": {
            "pypi": ["numpy==1.24.0"],
            "local_wheels": [],
            "editable": ["-e /home/user/myrepo"],
        },
        "packages": ["numpy==1.24.0"],
        "package_count": 1,
    }
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snap), encoding="utf-8")

    success, messages = restore_snapshot(snapshot_path=snap_file, dry_run=True)
    assert success is True
    assert any("[SKIP] editable" in m for m in messages)
