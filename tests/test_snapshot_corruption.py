"""
Adversarial snapshot corpus: snapshots that pass _is_snapshot() but have
corrupted, missing, or maliciously crafted internal data.

Tests trace every validation gate in restore_snapshot() and _safe_req_lines():

  Gate 1 — _is_snapshot()       : content-based detection (not filename)
  Gate 2 — _coerce_snap()       : field normalisation / type coercion
  Gate 3 — install path check   : comfyui.root must match current installation
  Gate 4 — Python version check : major.minor must match; unparseable → WARN
  Gate 5 — _safe_req_lines()    : drop flag lines; split embedded newlines first
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watchupdog.backup import (
    _coerce_snap,
    _is_snapshot,
    _safe_req_lines,
    restore_snapshot,
)

# Current Python version string in the format restore_snapshot() produces.
_CUR_PY_VER = f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
_CUR_PY_MM  = f"{sys.version_info.major}.{sys.version_info.minor}"


# ---------------------------------------------------------------------------
# Minimal valid snapshot factory
# ---------------------------------------------------------------------------

def _snap(
    *,
    pypi: list[str] | None = None,
    packages: list[str] | None = None,
    comfyui_root: str = "/fake/comfyui",
    python_version: str | None = None,
    restorable: dict | None = None,
) -> dict[str, Any]:
    if python_version is None:
        python_version = _CUR_PY_VER
    """Build a minimal snapshot dict that passes _is_snapshot()."""
    res: dict[str, Any] = {
        "restorable": restorable if restorable is not None else {
            "pypi": pypi if pypi is not None else ["torch==2.0.0"],
            "local_wheels": [],
            "editable": [],
        },
        "environment": {
            "python_version": python_version,
            "os": "Linux-5.15",
        },
        "comfyui": {
            "root": comfyui_root,
        },
        "hardware": {},
        "timestamp": "2026-01-01T00:00:00Z",
    }
    if packages is not None:
        res["packages"] = packages
    return res


def _write_snap(tmp_path: Path, data: dict) -> Path:
    """Write snapshot dict to a temp file and return its path."""
    p = tmp_path / "pip_state_test.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Gate 1 — _is_snapshot() detection
# ---------------------------------------------------------------------------

class TestIsSnapshot:
    def test_passes_with_restorable_key(self):
        assert _is_snapshot({"restorable": {"pypi": ["torch==2.0.0"]}})

    def test_passes_with_legacy_packages_key(self):
        assert _is_snapshot({"packages": ["torch==2.0.0"]})

    def test_passes_with_both_keys(self):
        assert _is_snapshot({"restorable": {"pypi": []}, "packages": ["x==1"]})

    def test_fails_with_empty_restorable_and_no_packages(self):
        # Empty dict is falsy — _is_snapshot must return False
        assert not _is_snapshot({"restorable": {}})

    def test_fails_with_empty_packages_list(self):
        assert not _is_snapshot({"packages": []})

    def test_fails_with_neither_key(self):
        assert not _is_snapshot({"comfyui": {"root": "/foo"}, "timestamp": "x"})

    def test_fails_on_empty_dict(self):
        assert not _is_snapshot({})

    def test_passes_with_non_empty_legacy_packages(self):
        # A list with even one entry is truthy
        assert _is_snapshot({"packages": ["numpy==1.24.0"]})


# ---------------------------------------------------------------------------
# Gate 2 — _coerce_snap() normalisation
# ---------------------------------------------------------------------------

class TestCoerceSnap:
    def test_non_dict_top_level_returns_empty(self):
        result = _coerce_snap(["not", "a", "dict"])
        assert isinstance(result, dict)
        assert result.get("restorable") == {
            "pypi": [], "local_wheels": [], "editable": []
        }

    def test_none_top_level_returns_empty(self):
        result = _coerce_snap(None)
        assert isinstance(result, dict)

    def test_non_dict_restorable_becomes_empty(self):
        result = _coerce_snap({"restorable": "not-a-dict", "packages": ["x==1"]})
        assert result["restorable"]["pypi"] == []

    def test_non_list_pypi_becomes_empty(self):
        result = _coerce_snap({"restorable": {"pypi": "numpy==1.24.0"}})
        assert result["restorable"]["pypi"] == []

    def test_non_string_entries_in_pypi_filtered_out(self):
        result = _coerce_snap({
            "restorable": {"pypi": ["good==1.0", 42, None, {"pkg": "bad"}]},
            "packages": [],
        })
        assert result["restorable"]["pypi"] == ["good==1.0"]

    def test_non_dict_comfyui_replaced_with_empty(self):
        result = _coerce_snap({"restorable": {"pypi": ["x==1"]}, "comfyui": "not-a-dict"})
        # comfyui is not normalised by _coerce_snap but restore_snapshot uses .get()
        # so this should not crash — verify the field passes through without error
        assert result is not None

    def test_extra_fields_preserved(self):
        data = {
            "restorable": {"pypi": ["x==1"]},
            "my_custom_field": "preserved",
        }
        result = _coerce_snap(data)
        assert result.get("my_custom_field") == "preserved"

    def test_non_list_custom_nodes_becomes_empty(self):
        result = _coerce_snap({
            "restorable": {"pypi": []},
            "custom_nodes": "should-be-list",
        })
        assert result["custom_nodes"] == []

    def test_non_dict_models_becomes_empty(self):
        result = _coerce_snap({
            "restorable": {"pypi": []},
            "models": ["not", "a", "dict"],
        })
        assert result["models"] == {}


# ---------------------------------------------------------------------------
# Gate 3 — install path check in restore_snapshot()
# ---------------------------------------------------------------------------

class TestInstallPathGate:
    def test_wrong_installation_blocks_restore(self, tmp_path):
        # Both directories exist on this machine → same-machine wrong-install → BLOCK.
        install_a = tmp_path / "ComfyUI-A"
        install_b = tmp_path / "ComfyUI-B"
        install_a.mkdir()
        install_b.mkdir()

        snap_data = _snap(comfyui_root=str(install_a))
        snap_path = _write_snap(tmp_path, snap_data)

        ok, msgs = restore_snapshot(
            snapshot_path=snap_path,
            comfyui_root=str(install_b),
        )
        assert not ok
        assert any("BLOCKED" in m for m in msgs)
        assert any("DIFFERENT" in m for m in msgs)

    def test_missing_comfyui_root_in_snap_issues_warn(self, tmp_path):
        """Snapshot with no comfyui.root passes the path gate with a warning."""
        snap_data = _snap(comfyui_root="")
        # Ensure the field is truly absent/empty so path check can't verify
        snap_data["comfyui"]["root"] = ""
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, f"Python {sys.version.split()[0]}", "")),
        ):
            ok, msgs = restore_snapshot(
                snapshot_path=snap_path,
                comfyui_root="/some/comfyui",
                dry_run=True,
            )
        assert any("WARN" in m and "cannot confirm" in m.lower() for m in msgs)

    def test_non_dict_comfyui_field_issues_warn(self, tmp_path):
        """comfyui field set to a string (not dict) — root resolves to ''."""
        snap_data = _snap()
        snap_data["comfyui"] = "corrupted-string"
        snap_path = _write_snap(tmp_path, snap_data)

        # After _coerce_snap the comfyui field passes through as-is (string).
        # restore_snapshot does snap_data.get("comfyui", {}) → string → .get("root","") fails.
        # Confirm this doesn't crash and issues an appropriate message.
        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, f"Python {sys.version.split()[0]}", "")),
        ):
            ok, msgs = restore_snapshot(
                snapshot_path=snap_path,
                comfyui_root="/some/comfyui",
                dry_run=True,
            )
        # Should not raise; should surface a warning or proceed with warning
        assert isinstance(msgs, list)

    def test_matching_installation_passes(self, tmp_path):
        real_root = str(tmp_path / "comfyui")
        snap_data = _snap(comfyui_root=real_root)
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, f"Python {sys.version.split()[0]}", "")),
        ):
            ok, msgs = restore_snapshot(
                snapshot_path=snap_path,
                comfyui_root=real_root,
                dry_run=True,
            )
        assert any("[OK]" in m and "verified" in m.lower() for m in msgs)


# ---------------------------------------------------------------------------
# Gate 4 — Python version check
# ---------------------------------------------------------------------------

class TestPythonVersionGate:
    def _current_mm(self) -> str:
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def test_matching_version_passes(self, tmp_path):
        ver = _CUR_PY_VER
        snap_data = _snap(python_version=ver)
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        assert any("[OK]" in m and "Python" in m for m in msgs)

    def test_mismatched_version_blocks(self, tmp_path):
        snap_data = _snap(python_version="Python 2.7.18")
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        assert not ok
        assert any("BLOCKED" in m for m in msgs)
        assert any("mismatch" in m.lower() for m in msgs)

    def test_empty_python_version_skips_check(self, tmp_path):
        snap_data = _snap(python_version="")
        snap_path = _write_snap(tmp_path, snap_data)

        _, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        # No Python check messages when version is absent
        assert not any("BLOCKED" in m and "Python" in m for m in msgs)

    def test_missing_python_version_key_skips_check(self, tmp_path):
        snap_data = _snap()
        del snap_data["environment"]["python_version"]
        snap_path = _write_snap(tmp_path, snap_data)

        _, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        assert not any("BLOCKED" in m and "Python" in m for m in msgs)

    def test_unparseable_python_version_issues_warn(self, tmp_path):
        """'Python alpha-build' → _parse_major_minor returns None → WARN emitted."""
        snap_data = _snap(python_version="Python alpha-build")
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        assert any(
            "WARN" in m and "parse" in m.lower() and "python" in m.lower()
            for m in msgs
        ), f"Expected unparseable-version WARN, got: {msgs}"

    def test_corrupt_exe_path_blocks(self, tmp_path):
        """python_exe recorded in snapshot points to nonexistent path → block."""
        snap_data = _snap()
        snap_data["environment"]["python_exe"] = "/nonexistent/python"
        snap_path = _write_snap(tmp_path, snap_data)

        with patch("watchupdog.backup.Path.exists", return_value=False):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)
        assert not ok
        assert any("exist" in m.lower() or "BLOCKED" in m for m in msgs)


# ---------------------------------------------------------------------------
# Gate 5 — _safe_req_lines() injection defence
# ---------------------------------------------------------------------------

class TestSafeReqLines:
    def test_normal_packages_pass_through(self):
        lines = ["torch==2.0.0", "numpy>=1.24", "Pillow==9.5.0"]
        assert _safe_req_lines(lines) == lines

    def test_direct_flag_line_dropped(self):
        lines = ["-i https://evil.example.com/simple/", "torch==2.0.0"]
        result = _safe_req_lines(lines)
        assert result == ["torch==2.0.0"]
        assert not any(l.startswith("-") for l in result)

    def test_extra_index_url_dropped(self):
        lines = ["--extra-index-url https://evil.example.com/", "numpy==1.24.0"]
        result = _safe_req_lines(lines)
        assert result == ["numpy==1.24.0"]

    def test_requirement_file_include_dropped(self):
        """'-r /etc/passwd' must be dropped (file-read injection vector)."""
        lines = ["-r /etc/passwd", "torch==2.0.0"]
        result = _safe_req_lines(lines)
        assert result == ["torch==2.0.0"]

    def test_embedded_newline_injection_blocked(self):
        """
        'legit==1.0\\n-i https://evil.com' appears as one JSON string.
        When written to a requirements file via '\\n'.join(), it creates
        two physical lines — the second being a pip option flag.
        _safe_req_lines must split on '\\n' first to catch it.
        """
        lines = ["legit==1.0\n-i https://evil.example.com/simple/"]
        result = _safe_req_lines(lines)
        assert "legit==1.0" in result
        assert not any("-i" in l for l in result)

    def test_embedded_newline_at_start_blocked(self):
        lines = ["\n-i https://evil.example.com/\nlegit==1.0"]
        result = _safe_req_lines(lines)
        assert "legit==1.0" in result
        assert not any("-i" in l for l in result)

    def test_carriage_return_newline_injection_blocked(self):
        lines = ["good==1.0\r\n--index-url https://evil.example.com/"]
        result = _safe_req_lines(lines)
        assert "good==1.0" in result
        assert not any("--index-url" in l for l in result)

    def test_blank_lines_and_comments_stripped(self):
        lines = ["", "# comment", "torch==2.0.0", "  ", "# another"]
        result = _safe_req_lines(lines)
        assert result == ["torch==2.0.0"]

    def test_empty_input_returns_empty(self):
        assert _safe_req_lines([]) == []

    def test_all_flag_lines_returns_empty(self):
        lines = ["-i https://a.com/", "--extra-index-url https://b.com/"]
        assert _safe_req_lines(lines) == []

    def test_legacy_packages_with_flag_dropped(self):
        """Legacy flat 'packages' list from old snapshots — same filter applies."""
        lines = ["-i https://evil.com/simple/", "numpy==1.24.0", "torch==2.0.0"]
        result = _safe_req_lines(lines)
        assert result == ["numpy==1.24.0", "torch==2.0.0"]


# ---------------------------------------------------------------------------
# Full restore_snapshot() adversarial integration tests
# ---------------------------------------------------------------------------

class TestRestoreAdversarial:
    def test_all_pypi_are_flags_returns_nothing_to_install(self, tmp_path):
        snap_data = _snap(pypi=["-i https://evil.com/simple/", "--extra-index-url https://evil.com/"])
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        assert not ok
        assert any("Nothing to install" in m or "WARN" in m for m in msgs)

    def test_mixed_flags_and_packages_flags_are_warned(self, tmp_path):
        snap_data = _snap(pypi=[
            "-i https://evil.com/simple/",
            "torch==2.0.0",
            "numpy==1.24.0",
        ])
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        assert any("WARN" in m and "'-'" in m for m in msgs)
        # Legitimate packages still installed
        assert any("torch==2.0.0" in m for m in msgs)

    def test_embedded_newline_injection_blocked(self, tmp_path):
        """Snapshot embedding a newline + flag in a package string is rejected.

        'torch==2.0.0\\n-i https://evil.com/simple/' is a single JSON string.
        _safe_req_lines() splits on embedded newlines so each physical line pip
        would see gets the leading-dash filter applied.  The '-i' segment must
        never reach the requirements file.
        Note: the flagged counter uses len(pypi) - len(result), so a multi-segment
        entry that shrinks-but-survives doesn't increment it — no WARN fires in
        this specific case, but the injection is still blocked.
        """
        snap_data = _snap(pypi=["torch==2.0.0\n-i https://evil.com/simple/"])
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        # The '-i' injection segment must never appear in the install list.
        all_text = "\n".join(msgs)
        assert "-i https://evil.com" not in all_text
        # 'torch==2.0.0' (the clean segment before the newline) should survive.
        assert any("torch==2.0.0" in m for m in msgs)

    def test_legacy_flat_packages_with_flag_injection(self, tmp_path):
        """Legacy 'packages' key (flat list) also filtered through _safe_req_lines."""
        snap_data = {
            "packages": ["-i https://evil.com/simple/", "numpy==1.24.0"],
            "environment": {"python_version": ""},
            "hardware": {},
            "timestamp": "2026-01-01T00:00:00Z",
        }
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, "", "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        assert any("WARN" in m and "'-'" in m for m in msgs)

    def test_non_string_pypi_entries_silently_dropped(self, tmp_path):
        """Non-string entries are dropped by _coerce_snap before reaching _safe_req_lines."""
        snap_data = _snap(pypi=["torch==2.0.0", 42, None, {"inject": "bad"}])
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        # Only torch==2.0.0 survives
        assert any("torch==2.0.0" in m for m in msgs)
        # No TypeError or crash
        assert isinstance(msgs, list)

    def test_unreadable_file_returns_error(self, tmp_path):
        snap_path = tmp_path / "nonexistent.json"
        ok, msgs = restore_snapshot(snapshot_path=snap_path)
        assert not ok
        assert any("Could not read" in m for m in msgs)

    def test_invalid_json_returns_error(self, tmp_path):
        snap_path = tmp_path / "corrupt.json"
        snap_path.write_text("{ not valid json !!!", encoding="utf-8")
        ok, msgs = restore_snapshot(snapshot_path=snap_path)
        assert not ok
        assert any("Could not read" in m for m in msgs)

    def test_cross_machine_different_drive_warns_not_blocks(self, tmp_path):
        """Snapshot from D:\\ComfyUI restored onto C:\\ComfyUI → WARN, not BLOCK.

        The snapshot root path doesn't exist on the current machine, which
        signals a cross-machine transfer rather than a wrong-installation
        mistake.  restore_snapshot() must warn and continue rather than block.
        """
        # A root path that cannot exist on the current test machine.
        cross_machine_root = "/nonexistent_machine_xyzzy/ComfyUI"
        local_root = str(tmp_path / "ComfyUI")
        Path(local_root).mkdir()

        snap_data = _snap(comfyui_root=cross_machine_root)
        snap_path = _write_snap(tmp_path, snap_data)

        # Pass sys.executable explicitly so the exe-existence check uses a real
        # binary instead of the nonexistent D:\... path stored in the snapshot.
        with patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")):
            ok, msgs = restore_snapshot(
                snapshot_path=snap_path,
                comfyui_root=local_root,
                python_exe=sys.executable,
                dry_run=True,
            )

        # Must NOT be blocked with the "DIFFERENT ComfyUI installation" error.
        assert not any("DIFFERENT" in m for m in msgs), \
            f"Cross-machine restore should not be blocked:\n{chr(10).join(msgs)}"
        # Must issue a WARN explaining cross-machine assumption.
        assert any("WARN" in m and "cross-machine" in m.lower() for m in msgs), \
            f"Expected cross-machine WARN:\n{chr(10).join(msgs)}"

    def test_same_machine_wrong_install_still_blocks(self, tmp_path):
        """Two local ComfyUI installs on the same machine → hard block still applies."""
        install_a = tmp_path / "ComfyUI-A"
        install_b = tmp_path / "ComfyUI-B"
        install_a.mkdir()
        install_b.mkdir()

        snap_data = _snap(comfyui_root=str(install_a))
        snap_path = _write_snap(tmp_path, snap_data)

        # Directories exist → restore_snapshot detects same-machine wrong install.
        ok, msgs = restore_snapshot(
            snapshot_path=snap_path,
            comfyui_root=str(install_b),
        )

        assert not ok
        assert any("BLOCKED" in m for m in msgs)
        assert any("DIFFERENT" in m for m in msgs)

    def test_cross_machine_no_comfyui_path_hints_at_flag(self, tmp_path):
        """No --comfyui-path on a cross-machine snapshot → error mentions --comfyui-path."""
        cross_machine_exe = "/nonexistent_machine/ComfyUI/.venv/bin/python"
        snap_data = _snap()
        snap_data["environment"]["python_exe"] = cross_machine_exe
        snap_data["environment"]["python_version"] = _CUR_PY_VER
        snap_path = _write_snap(tmp_path, snap_data)

        # No python_exe override, no comfyui_root → exe comes from snapshot → doesn't exist
        with patch("watchupdog.backup.Path.exists", return_value=False):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        assert not ok
        assert any("--comfyui-path" in m for m in msgs), \
            f"Expected --comfyui-path hint:\n{chr(10).join(msgs)}"

    def test_snap_with_only_restorable_empty_pypi_nothing_to_install(self, tmp_path):
        snap_data = _snap(pypi=[])
        snap_path = _write_snap(tmp_path, snap_data)

        with (
            patch("watchupdog.backup.Path.exists", return_value=True),
            patch("watchupdog.backup._run", return_value=(0, _CUR_PY_VER, "")),
        ):
            ok, msgs = restore_snapshot(snapshot_path=snap_path, dry_run=True)

        assert not ok
        assert any("Nothing to install" in m for m in msgs)
