"""
Core package & compatibility checks.

All package inspection runs INSIDE the ComfyUI venv Python via subprocess,
so results reflect what ComfyUI actually sees — not the system Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .env_checks import (
    STATUS_FAIL,
    STATUS_INFO,
    STATUS_OK,
    STATUS_WARN,
    EnvCheckRow,
    _get_comfyui_dirs,
    _run,
    _version_ge,
    detect_active_comfyui,
    find_all_comfyui_installs,
)

# ---------------------------------------------------------------------------
# ComfyUI venv discovery
# ---------------------------------------------------------------------------

# Canonical list of relative paths from a ComfyUI root to its Python interpreter.
# Used by venv_python_for_root() and every caller that discovers venv Python.
# Add new layouts here — do not duplicate this list elsewhere.
_VENV_PYTHON_CANDIDATES: list[str] = [
    ".venv/Scripts/python.exe",     # Windows venv (pip-created)
    ".venv/bin/python",             # Unix venv
    "python_embedded/python.exe",   # ComfyUI portable (correct spelling)
    "python_embeded/python.exe",    # ComfyUI portable (legacy typo — kept for compat)
    "venv/Scripts/python.exe",      # Windows venv (no dot)
    "venv/bin/python",              # Unix venv (no dot)
]


def venv_python_for_root(root: Path) -> Path | None:
    """Return the first existing Python interpreter inside *root*, or None.

    Checks all known venv/portable layouts in priority order.  This is the
    single source of truth for ComfyUI venv discovery — do not duplicate the
    candidate list in other modules.
    """
    for rel in _VENV_PYTHON_CANDIDATES:
        py = root / rel
        if py.exists():
            return py
    return None


def _detect_conda_python(root: Path) -> tuple[Path, str] | None:
    """Look for a conda env that plausibly belongs to this ComfyUI install.

    Returns (python_exe, label) or None.  Checks in order:
      1. CONDA_PREFIX env var — the currently activated conda environment.
      2. Environments named 'comfyui*' in standard conda base directories.

    Does not shell out to ``conda`` (slow, may not be on PATH).
    """
    # 1. Active conda env (user already activated it)
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        p = Path(conda_prefix)
        for rel in ("bin/python3", "bin/python", "python.exe"):
            py = p / rel
            if py.exists():
                return py, f"conda env '{p.name}' (CONDA_PREFIX)"

    # 2. Scan common conda base directories for 'comfyui*' named envs
    home = Path.home()
    conda_bases: list[Path] = [
        home / "miniconda3",
        home / "anaconda3",
        home / "miniforge3",
        home / "mambaforge",
        home / "micromamba",
        Path("/opt/conda"),
        Path("/opt/miniconda3"),
        Path("/opt/anaconda3"),
        # macOS Homebrew conda installs (Intel and Apple Silicon)
        Path("/opt/homebrew/Caskroom/miniforge/base"),
        Path("/opt/homebrew/Caskroom/miniconda/base"),
        Path("/opt/homebrew/Caskroom/anaconda/base"),
        Path("/usr/local/Caskroom/miniforge/base"),
        Path("/usr/local/Caskroom/miniconda/base"),
        Path("/usr/local/Caskroom/anaconda/base"),
    ]
    for base in conda_bases:
        envs_dir = base / "envs"
        if not envs_dir.is_dir():
            continue
        try:
            for env_dir in sorted(envs_dir.iterdir()):
                if "comfyui" in env_dir.name.lower():
                    for rel in ("bin/python3", "bin/python", "python.exe"):
                        py = env_dir / rel
                        if py.exists():
                            return py, f"conda env '{env_dir.name}'"
        except PermissionError:
            continue

    return None


def detect_python_for_root(
    root: Path,
    python_cfg: dict | None = None,
) -> tuple[Path, str]:
    """Resolve the Python interpreter for *root* using a priority chain.

    Returns ``(python_exe, strategy_label)``.  The label describes which
    strategy succeeded and is included in status rows so the user always
    knows which Python is being used for pip checks.

    Priority (unless overridden by python_cfg):
      1. Config-specified venv_path when strategy != 'auto'
      2. Standard venv candidates inside root (.venv, python_embeded, …)
      3. Active / named conda environment
      4. System Python — last resort, emits a WARN-worthy label

    When strategy is 'auto', steps 1 and 2 are merged: venv candidates are
    tried first, then conda, then system Python.
    """
    cfg = python_cfg or {}
    strategy: str = cfg.get("strategy", "auto") or "auto"
    venv_path_hint: str = cfg.get("venv_path", "") or ""

    # --- Config-driven override (strategy != auto) ---
    if strategy != "auto" and venv_path_hint:
        hint = Path(venv_path_hint)
        # Absolute path (e.g. conda env outside root)
        if not hint.is_absolute():
            hint = root / hint
        # Is it a directory? treat as venv root and probe for python inside it.
        if hint.is_dir():
            for suffix in (
                "Scripts/python.exe",
                "bin/python3",
                "bin/python",
                "python.exe",
            ):
                py = hint / suffix
                if py.exists():
                    return py, f"{strategy} ({venv_path_hint})"
        # Is it already a direct path to a python executable?
        if hint.is_file():
            return hint, f"{strategy} ({venv_path_hint})"
        # Path specified but not found — fall through to auto detection and
        # the caller will see a WARN label.

    # --- Standard venv / embedded candidates ---
    py = venv_python_for_root(root)
    if py:
        rel = py.relative_to(root).as_posix()
        if "python_embed" in rel:
            label = f"embedded Python ({rel})"
        else:
            label = f"venv ({rel})"
        return py, label

    # --- Conda ---
    conda_result = _detect_conda_python(root)
    if conda_result:
        return conda_result

    # --- System Python fallback ---
    return (
        Path(sys.executable),
        "system Python — no venv or conda env found; pip checks may reflect the wrong environment",
    )


def find_comfyui_python() -> tuple[Path | None, Path | None]:
    """Return (python_exe, comfyui_root) for the ComfyUI environment.

    Scans all known ComfyUI install locations using structural confidence
    scoring so renamed / custom-path installs work too.
    """
    from .env_checks import _comfyui_score
    # Collect candidates sorted by confidence score (highest first)
    candidates_with_score = []
    for root in _get_comfyui_dirs():
        if not root.exists():
            continue
        s = _comfyui_score(root)
        if s >= 20 or (root / "main.py").exists() or (root / "server.py").exists():
            candidates_with_score.append((s, root))
    candidates_with_score.sort(key=lambda x: -x[0])

    for _score_val, root in candidates_with_score:
        py = venv_python_for_root(root)
        if py:
            return py, root

    return None, None


# ---------------------------------------------------------------------------
# Probe script — runs inside the venv Python, returns JSON
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = r"""
import sys, json, re, importlib.metadata as meta, importlib, os
from pathlib import Path

out = {}

def get_ver(name):
    try:
        return meta.version(name)
    except Exception:
        pass
    try:
        m = importlib.import_module(name)
        return getattr(m, "__version__", None)
    except Exception:
        return None

# --- Python ---
out["python"] = sys.version

# --- torch ---
try:
    import torch
    out["torch_version"] = torch.__version__
    out["torch_cuda_version"] = torch.version.cuda          # e.g. "13.0"
    out["torch_cuda_available"] = torch.cuda.is_available()
    out["rocm_available"] = hasattr(torch.version, "hip") and torch.version.hip is not None
    if not out["torch_cuda_available"] and out["rocm_available"]:
        out["torch_cuda_available"] = True  # ROCm reports as cuda-compatible
        out["rocm_mode"] = True
    if torch.cuda.is_available():
        out["torch_cuda_device_count"] = torch.cuda.device_count()
        out["torch_cuda_device_name"] = torch.cuda.get_device_name(0)
    # Apple Silicon MPS
    try:
        out["mps_available"] = hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    except Exception:
        out["mps_available"] = False
except ImportError:
    out["torch_version"] = None

# --- nunchaku ---
try:
    import nunchaku
    # Resolve site-packages dirs without an unbounded rglob over the whole prefix.
    import site as _site
    _sp_dirs: list[Path] = []
    try:
        _sp_dirs = [Path(p) for p in _site.getsitepackages()]
    except AttributeError:
        # getsitepackages() is absent in virtualenvs on some Python builds;
        # fall back to the user site-packages and the stdlib location.
        try:
            _sp_dirs = [Path(_site.getusersitepackages())]
        except Exception:
            pass
    # Flatten all nunchaku dist-info entries found in site-packages (no rglob).
    _nunchaku_dist: list[Path] = []
    for _sp in _sp_dirs:
        _nunchaku_dist.extend(_sp.glob("nunchaku*.dist-info"))
    try:
        out["nunchaku_version"] = meta.version("nunchaku")
    except Exception:
        # Parse version from dist-info directory name as a fallback.
        for p in _nunchaku_dist:
            if p.name.startswith("nunchaku-"):
                parts = p.name.split("-")
                if len(parts) >= 2:
                    out["nunchaku_version"] = parts[1]
                    break
    out["nunchaku_dist_names"] = [p.name for p in _nunchaku_dist]
    out["nunchaku_importable"] = True
except ImportError:
    out["nunchaku_importable"] = False
    out["nunchaku_version"] = None

# --- xformers ---
out["xformers_version"] = get_ver("xformers")

# --- numpy ---
out["numpy_version"] = get_ver("numpy")

# --- safetensors ---
out["safetensors_version"] = get_ver("safetensors")

# --- transformers ---
out["transformers_version"] = get_ver("transformers")

# --- diffusers ---
out["diffusers_version"] = get_ver("diffusers")

# --- accelerate ---
out["accelerate_version"] = get_ver("accelerate")

# --- requests + urllib3 conflict ---
out["requests_version"] = get_ver("requests")
out["urllib3_version"] = get_ver("urllib3")
out["charset_normalizer_version"] = get_ver("charset-normalizer")

# --- pynvml / nvidia-ml-py ---
try:
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore", FutureWarning)
        import pynvml
    try:
        out["pynvml_version"] = meta.version("nvidia-ml-py")
        out["pynvml_pkg"] = "nvidia-ml-py"
    except Exception:
        out["pynvml_version"] = meta.version("pynvml")
        out["pynvml_pkg"] = "pynvml"
except ImportError:
    out["pynvml_version"] = None

# --- pip conflicts (pip check) ---
import subprocess, sys
r = subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    capture_output=True, encoding="utf-8", errors="replace"
)
out["pip_check_rc"] = r.returncode
out["pip_check_out"] = (r.stdout + r.stderr).strip()[:2000]

print(json.dumps(out))
"""


def probe_venv(python_exe: Path) -> dict[str, Any] | None:
    """Run the probe script in the given Python and return parsed JSON."""
    rc, out, err = _run([str(python_exe), "-c", _PROBE_SCRIPT], timeout=90)
    if rc != 0:
        return None
    # Find the JSON line (ignore FutureWarning lines etc.)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Parse nunchaku wheel / dist-info name for build metadata
# ---------------------------------------------------------------------------

# nunchaku-1.3.0.dev20260202+cu13.0torch2.10.dist-info
# nunchaku-1.3.0.dev20260202+cu13.0torch2.10-cp310-cp310-win_amd64.whl
_NUNCHAKU_BUILD_RE = re.compile(
    r"nunchaku[^+]*\+cu([\d.]+)torch([\d.]+)",
    re.IGNORECASE,
)


def parse_nunchaku_build(name: str) -> dict[str, str] | None:
    """Extract {'cuda': '13.0', 'torch': '2.10'} from a nunchaku dist name or whl filename."""
    m = _NUNCHAKU_BUILD_RE.search(name)
    if m:
        return {"cuda": m.group(1), "torch": m.group(2)}
    return None


def _cuda_major_minor(ver: str | None) -> tuple[int, int] | None:
    """Parse '13.0' or '13' into (13, 0)."""
    if not ver:
        return None
    parts = ver.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return major, minor
    except ValueError:
        return None


def _torch_major_minor(ver: str | None) -> tuple[int, int] | None:
    """Parse '2.10.0+cu130' → (2, 10)."""
    if not ver:
        return None
    # strip local version tag
    base = ver.split("+")[0]
    parts = base.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Requirements.txt parser
# ---------------------------------------------------------------------------

def parse_requirements(req_path: Path) -> list[tuple[str, str | None]]:
    """Return list of (package_name, min_version_or_None) from requirements.txt."""
    results = []
    if not req_path.exists():
        return results
    try:
        content = req_path.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, OSError):
        return results
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Parse: package>=version, package==version, package~=version, package
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([><=~!]{1,2})\s*([\d.]+)", line)
        if m:
            results.append((m.group(1), m.group(3)))
        else:
            m2 = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if m2:
                results.append((m2.group(1), None))
    return results


# ---------------------------------------------------------------------------
# Main check function
# ---------------------------------------------------------------------------

def run_pip_checks(
    comfyui_root: Path | None = None,
    python_cfg: dict | None = None,
) -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Core Package Compatibility"

    # 1. Find ComfyUI root — prefer explicit arg, then active process, then first found
    all_installs = find_all_comfyui_installs()
    active_root = detect_active_comfyui()

    if comfyui_root:
        root = comfyui_root
        if len(all_installs) > 1:
            others = [str(i) for i in all_installs if i.resolve() != root.resolve()]
            rows.append(EnvCheckRow(group, "Multiple ComfyUI installs", STATUS_INFO,
                                    f"{len(all_installs)} found. Using specified: {root}. "
                                    f"Others: {', '.join(others[:3])}"))
    elif active_root:
        root = active_root
        if len(all_installs) > 1:
            others = [str(i) for i in all_installs if i.resolve() != root.resolve()]
            rows.append(EnvCheckRow(group, "Multiple ComfyUI installs", STATUS_WARN,
                                    f"{len(all_installs)} found. Active: {root}. "
                                    f"Others: {', '.join(others[:3])}"))
        else:
            rows.append(EnvCheckRow(group, "ComfyUI install", STATUS_OK,
                                    f"Active: {root}"))
    elif all_installs:
        root = all_installs[0]
        if len(all_installs) > 1:
            others = [str(i) for i in all_installs[1:]]
            rows.append(EnvCheckRow(group, "Multiple ComfyUI installs", STATUS_WARN,
                                    f"{len(all_installs)} found, none currently running. "
                                    f"Defaulting to: {root}. Others: {', '.join(others[:3])}"))
        else:
            rows.append(EnvCheckRow(group, "ComfyUI install", STATUS_INFO,
                                    f"Found (not running): {root}"))
    else:
        rows.append(EnvCheckRow(group, "ComfyUI install", STATUS_FAIL,
                                "No ComfyUI installation found. Use --comfyui-path to specify."))
        return rows

    python_exe, py_label = detect_python_for_root(root, python_cfg)

    _is_system_fallback = py_label.startswith("system Python")
    _is_conda = py_label.startswith("conda")

    # 2. Probe the venv BEFORE emitting the Python row so the status reflects
    #    whether the binary can actually execute — a corrupt or incomplete
    #    python_embeded passes .exists() but fails here.
    probe = probe_venv(python_exe)
    if probe is None:
        # Binary found but unusable: either corrupt, incomplete, or wrong arch.
        _py_status = STATUS_WARN if _is_system_fallback else STATUS_WARN
        rows.append(EnvCheckRow(group, "ComfyUI Python", _py_status,
                                f"{python_exe}  [{py_label}]  — binary not executable"))
        rows.append(EnvCheckRow(group, "Venv probe", STATUS_FAIL,
                                "Failed to run probe script — Python binary may be "
                                "corrupt, incomplete, or wrong architecture"))
        return rows

    if _is_system_fallback:
        rows.append(EnvCheckRow(group, "ComfyUI Python", STATUS_WARN,
                                f"{python_exe}  [{py_label}]"))
    else:
        rows.append(EnvCheckRow(group, "ComfyUI Python", STATUS_OK,
                                f"{python_exe}  [{py_label}]"))

    _py_row_label = "Python (conda)" if _is_conda else ("Python (system)" if _is_system_fallback else "Python (venv)")
    rows.append(EnvCheckRow(group, _py_row_label, STATUS_OK,
                            probe.get("python", "?").split()[0]))

    # 3. torch
    torch_ver = probe.get("torch_version")
    torch_cuda = probe.get("torch_cuda_version")    # e.g. "13.0"
    cuda_avail = probe.get("torch_cuda_available", False)
    rocm_mode = probe.get("rocm_mode", False)

    if not torch_ver:
        rows.append(EnvCheckRow(group, "torch", STATUS_FAIL,
                                "torch not installed in ComfyUI venv",
                                fix_cmd=f"{python_exe} -m pip install torch --index-url https://download.pytorch.org/whl/cu124"))
        return rows  # nothing else is meaningful without torch

    torch_color = STATUS_OK
    torch_detail = f"{torch_ver}"
    if rocm_mode:
        torch_detail += "  (ROCm/AMD build)"
    elif torch_cuda:
        torch_detail += f"  (CUDA build: {torch_cuda})"
    if not cuda_avail and not rocm_mode:
        # Check for MPS (Apple Silicon) or CPU-only intentional modes
        mps_available = probe.get("mps_available", False)
        if mps_available:
            torch_detail += "  (Apple MPS)"
        else:
            torch_color = STATUS_WARN
            torch_detail += "  — no GPU acceleration (CPU-only or unsupported GPU)"
    rows.append(EnvCheckRow(group, "torch", torch_color, torch_detail))

    # 4. GPU availability
    if cuda_avail:
        dev = probe.get("torch_cuda_device_name", "?")
        if rocm_mode:
            rows.append(EnvCheckRow(group, "torch GPU", STATUS_OK, f"ROCm/AMD — {dev}"))
        else:
            rows.append(EnvCheckRow(group, "torch GPU", STATUS_OK, f"CUDA — {dev}"))
    else:
        mps_available = probe.get("mps_available", False)
        if mps_available:
            rows.append(EnvCheckRow(group, "torch GPU", STATUS_OK, "Apple MPS (Metal) available"))
        else:
            rows.append(EnvCheckRow(group, "torch GPU", STATUS_WARN,
                                    "No GPU acceleration detected — CPU-only mode. "
                                    "ComfyUI will work but generation will be very slow."))

    # 5. Nunchaku build compatibility
    nunchaku_ver = probe.get("nunchaku_version")
    nunchaku_ok = probe.get("nunchaku_importable", False)
    dist_names: list[str] = probe.get("nunchaku_dist_names", [])

    nunchaku_build: dict[str, str] | None = None
    for name in dist_names:
        nunchaku_build = parse_nunchaku_build(name)
        if nunchaku_build:
            break

    # Also check the .whl file sitting in the ComfyUI root
    whl_build: dict[str, str] | None = None
    if root:
        for whl in root.glob("nunchaku*.whl"):
            whl_build = parse_nunchaku_build(whl.name)
            if whl_build:
                break

    if not nunchaku_ok:
        pass   # not installed — don't mention it at all
    else:
        build_str = ""
        if nunchaku_build:
            build_str = f"  (built for cu{nunchaku_build['cuda']}, torch{nunchaku_build['torch']})"
        ver_str = nunchaku_ver or "version unknown"
        rows.append(EnvCheckRow(group, "nunchaku importable", STATUS_OK, f"{ver_str}{build_str}"))

    # 6. nunchaku <-> torch CUDA compatibility
    if nunchaku_ok and nunchaku_build and torch_cuda:
        n_cuda = _cuda_major_minor(nunchaku_build["cuda"])
        t_cuda = _cuda_major_minor(torch_cuda)

        if n_cuda and t_cuda:
            if n_cuda[0] == t_cuda[0]:  # major must match exactly
                rows.append(EnvCheckRow(group, "nunchaku CUDA == torch CUDA", STATUS_OK,
                                        f"cu{nunchaku_build['cuda']} == cu{torch_cuda}"))
            else:
                rows.append(EnvCheckRow(group, "nunchaku CUDA == torch CUDA", STATUS_FAIL,
                                        f"MISMATCH: nunchaku built for cu{nunchaku_build['cuda']}, "
                                        f"torch built for cu{torch_cuda}. "
                                        f"Install a nunchaku wheel matching your torch CUDA."))

    # 7. nunchaku <-> torch version compatibility
    if nunchaku_ok and nunchaku_build and torch_ver:
        n_torch = _torch_major_minor(nunchaku_build["torch"])
        t_torch = _torch_major_minor(torch_ver)

        if n_torch and t_torch:
            if n_torch == t_torch:
                rows.append(EnvCheckRow(group, "nunchaku torch == installed torch", STATUS_OK,
                                        f"torch{nunchaku_build['torch']} == {torch_ver.split('+')[0]}"))
            else:
                rows.append(EnvCheckRow(group, "nunchaku torch == installed torch", STATUS_FAIL,
                                        f"MISMATCH: nunchaku built for torch{nunchaku_build['torch']}, "
                                        f"installed torch is {torch_ver.split('+')[0]}. "
                                        f"Get matching nunchaku wheel from https://github.com/mit-han-lab/nunchaku/releases"))

    # 8. torch CUDA build <= system CUDA driver
    # (driver must be >= CUDA toolkit version used to build torch)
    rc, smi_out, _ = _run(["nvidia-smi"])
    if rc == 0 and torch_cuda:
        driver_cuda_m = re.search(r"CUDA Version:\s*([\d.]+)", smi_out)
        if driver_cuda_m:
            driver_cuda = driver_cuda_m.group(1)
            d = _cuda_major_minor(driver_cuda)
            t = _cuda_major_minor(torch_cuda)
            if d and t:
                if d >= t:
                    rows.append(EnvCheckRow(group, "CUDA driver >= torch CUDA build", STATUS_OK,
                                            f"driver {driver_cuda} >= torch cu{torch_cuda}"))
                else:
                    rows.append(EnvCheckRow(group, "CUDA driver >= torch CUDA build", STATUS_FAIL,
                                            f"Driver CUDA {driver_cuda} < torch build cu{torch_cuda}. "
                                            f"Update NVIDIA driver."))

    # 9. xformers <-> torch
    xf_ver = probe.get("xformers_version")
    if xf_ver:
        t_mm = _torch_major_minor(torch_ver)
        # xformers 0.0.28+ requires torch 2.x
        if t_mm and t_mm[0] >= 2:
            rows.append(EnvCheckRow(group, "xformers", STATUS_OK,
                                    f"{xf_ver}  (torch {torch_ver.split('+')[0]})"))
        else:
            rows.append(EnvCheckRow(group, "xformers", STATUS_WARN,
                                    f"{xf_ver} may be incompatible with torch {torch_ver}"))
    else:
        rows.append(EnvCheckRow(group, "xformers", STATUS_INFO,
                                "Not installed (optional - improves VRAM efficiency on older GPUs)"))

    # 10. numpy
    np_ver = probe.get("numpy_version")
    if np_ver:
        if _version_ge(np_ver, "2.0"):
            rows.append(EnvCheckRow(group, "numpy", STATUS_OK,
                                    f"{np_ver}  (2.x - check custom nodes for compatibility)"))
        elif _version_ge(np_ver, "1.25"):
            rows.append(EnvCheckRow(group, "numpy", STATUS_OK, f"{np_ver}"))
        else:
            rows.append(EnvCheckRow(group, "numpy", STATUS_WARN,
                                    f"{np_ver} - ComfyUI requires >= 1.25.0",
                                    fix_cmd=f"{python_exe} -m pip install 'numpy>=1.25'"))
    else:
        rows.append(EnvCheckRow(group, "numpy", STATUS_FAIL, "Not installed",
                                fix_cmd=f"{python_exe} -m pip install 'numpy>=1.25'"))

    # 11. Key deps: safetensors, transformers, diffusers, accelerate
    key_deps = [
        ("safetensors", "safetensors_version", "0.4.2"),
        ("transformers", "transformers_version", "4.50.3"),
        ("diffusers", "diffusers_version", "0.26.0"),
        ("accelerate", "accelerate_version", "0.26.0"),
    ]
    for label, key, min_ver in key_deps:
        ver = probe.get(key)
        if not ver:
            rows.append(EnvCheckRow(group, label, STATUS_WARN, "Not installed",
                                    fix_cmd=f"{python_exe} -m pip install '{label}>={min_ver}'"))
        elif _version_ge(ver, min_ver):
            rows.append(EnvCheckRow(group, label, STATUS_OK, ver))
        else:
            rows.append(EnvCheckRow(group, label, STATUS_WARN,
                                    f"{ver} - recommend >= {min_ver}",
                                    fix_cmd=f"{python_exe} -m pip install --upgrade '{label}>={min_ver}'"))

    # 12. requests / urllib3 conflict
    req_ver = probe.get("requests_version")
    url3_ver = probe.get("urllib3_version")
    if req_ver and url3_ver:
        # requests 2.x ships its own urllib3; if system urllib3 is 2.x and requests < 2.32,
        # there's a chardet/charset-normalizer conflict warning
        if _version_ge(url3_ver, "2.0") and not _version_ge(req_ver, "2.32"):
            rows.append(EnvCheckRow(group, "requests/urllib3 conflict", STATUS_WARN,
                                    f"requests {req_ver} + urllib3 {url3_ver} - version mismatch (harmless but noisy)",
                                    fix_cmd=f"{python_exe} -m pip install --upgrade requests"))
        else:
            rows.append(EnvCheckRow(group, "requests/urllib3", STATUS_OK,
                                    f"requests {req_ver}, urllib3 {url3_ver}"))

    # 13. pip check (detect broken/conflicting deps)
    pip_rc = probe.get("pip_check_rc", -1)
    pip_out = probe.get("pip_check_out", "").strip()
    if pip_rc == 0:
        rows.append(EnvCheckRow(group, "pip check (no conflicts)", STATUS_OK,
                                "No dependency conflicts detected"))
    else:
        conflict_lines = [l for l in pip_out.splitlines() if l.strip()]

        # Extract the source package from each conflict line.
        # pip check format: "<pkg> <ver> has requirement ..."
        import re as _re
        _src_pkgs: set[str] = set()
        for _line in conflict_lines:
            _m = _re.match(r"^([\w\-\.]+)\s", _line)
            if _m:
                _src_pkgs.add(_m.group(1).lower())

        # Core packages whose conflicts indicate a real problem.
        _CORE = {
            "torch", "torchvision", "torchaudio", "numpy", "transformers",
            "diffusers", "accelerate", "safetensors", "pydantic", "requests",
            "urllib3", "pillow", "opencv-python", "opencv-python-headless",
            "xformers", "nunchaku", "comfyui",
        }
        _non_core_only = bool(_src_pkgs) and _src_pkgs.isdisjoint(_CORE)

        summary = "; ".join(conflict_lines[:3]) + (
            f" (+{len(conflict_lines) - 3} more)" if len(conflict_lines) > 3 else ""
        )

        if _non_core_only:
            # All conflicts come from non-core packages — almost certainly stale
            # custom node dependencies with outdated version pins.
            _src_str = ", ".join(sorted(_src_pkgs))
            rows.append(EnvCheckRow(
                group, "pip check (conflicts found)", STATUS_INFO,
                f"conflicts from {_src_str} — likely stale custom node "
                f"dependencies with outdated pins, not affecting core functionality; "
                f"{summary}",
            ))
        else:
            rows.append(EnvCheckRow(group, "pip check (conflicts found)", STATUS_WARN, summary))

    # 14. requirements.txt coverage
    if root:
        req_path = root / "requirements.txt"
        reqs = parse_requirements(req_path)
        missing: list[str] = []
        outdated: list[str] = []
        if reqs:
            # Run a batch version check in the venv
            pkg_names = [name for name, _ in reqs]
            check_script = (
                "import importlib.metadata as m, json, sys\n"
                f"pkgs = {pkg_names!r}\n"
                "out = {}\n"
                "for p in pkgs:\n"
                "    try: out[p] = m.version(p)\n"
                "    except Exception: out[p] = None\n"
                "print(json.dumps(out))"
            )
            rc2, out2, _ = _run([str(python_exe), "-c", check_script], timeout=15)
            installed_map: dict[str, str | None] = {}
            for line in out2.splitlines():
                if line.strip().startswith("{"):
                    try:
                        installed_map = json.loads(line)
                    except Exception:
                        pass
                    break

            for pkg, min_ver in reqs:
                inst = installed_map.get(pkg)
                if inst is None:
                    missing.append(pkg)
                elif min_ver and not _version_ge(inst, min_ver):
                    outdated.append(f"{pkg} {inst} (need >={min_ver})")

            if not missing and not outdated:
                rows.append(EnvCheckRow(group, f"requirements.txt ({len(reqs)} pkgs)", STATUS_OK,
                                        "All satisfied"))
            else:
                if missing:
                    m_str = ", ".join(missing[:5]) + (f" (+{len(missing)-5} more)" if len(missing) > 5 else "")
                    rows.append(EnvCheckRow(group, "requirements.txt missing", STATUS_FAIL, m_str,
                                            fix_cmd=f"{python_exe} -m pip install -r {req_path}"))
                if outdated:
                    o_str = ", ".join(outdated[:3])
                    rows.append(EnvCheckRow(group, "requirements.txt outdated", STATUS_WARN, o_str,
                                            fix_cmd=f"{python_exe} -m pip install --upgrade -r {req_path}"))

    return rows
