"""Environment & dependency health checks -runs without ComfyUI being online."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Run-scoped directory cache
# ---------------------------------------------------------------------------
# _get_comfyui_dirs() is called up to 6× during a single run_env_checks().
# Each call does real filesystem I/O across 60-70 candidate paths.
# We cache the result for the duration of one run and clear it afterward.

_dirs_cache: list[Path] | None = None


def _get_comfyui_dirs_cached() -> list[Path]:
    """Return cached result of _get_comfyui_dirs() for the current run."""
    global _dirs_cache
    if _dirs_cache is None:
        _dirs_cache = _get_comfyui_dirs()
    return _dirs_cache

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

STATUS_OK = "[OK]"
STATUS_WARN = "[WARN]"
STATUS_FAIL = "[FAIL]"
STATUS_INFO = "[INFO]"


@dataclass
class EnvCheckRow:
    group: str
    check: str
    status: str
    detail: str
    fix_cmd: str | None = None  # command to run to fix this issue


@dataclass
class EnvCheckReport:
    rows: list[EnvCheckRow] = field(default_factory=list)
    auto_fixed: list[str] = field(default_factory=list)
    manual_needed: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r.status == STATUS_OK)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.rows if r.status == STATUS_WARN)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if r.status == STATUS_FAIL)


# ---------------------------------------------------------------------------
# Package version requirements
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES: list[tuple[str, str, bool]] = [
    # (import_name, min_version, required)
    ("httpx", "0.27", True),
    ("aiohttp", "3.9", False),
    ("psutil", "5.9", True),
    ("rich", "13.0", True),
    ("pydantic", "2.0", True),
    ("click", "8.1", True),
    ("pynvml", "11.0", False),
    ("fastapi", "0.110", False),
    ("uvicorn", "0.29", False),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _version_ge(installed: str, minimum: str) -> bool:
    """Return True if installed >= minimum (simple numeric comparison)."""
    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.split(r"[.\-]", v) if x.isdigit())

    try:
        return parts(installed) >= parts(minimum)
    except Exception:
        return False


def _get_package_version(import_name: str) -> str | None:
    """Try importlib.metadata first, then __version__, then None."""
    # Map import name to dist name for common mismatches
    dist_name_map = {
        "httpx": "httpx",
        "aiohttp": "aiohttp",
        "psutil": "psutil",
        "rich": "rich",
        "pydantic": "pydantic",
        "click": "click",
        "pynvml": "pynvml",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "tomli": "tomli",
    }
    dist = dist_name_map.get(import_name, import_name)
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        mod = importlib.import_module(import_name)
        return getattr(mod, "__version__", None)
    except ImportError:
        return None


def _run(cmd: list[str], timeout: int = 10, git: bool = False) -> tuple[int, str, str]:
    try:
        env = None
        if git:
            # Prevent git / Git Credential Manager from prompting or opening a browser.
            # -c credential.helper= (passed inline) disables GCM entirely for this call.
            # The env vars below suppress the fallback terminal prompt.
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_ASKPASS"] = "echo"
            env["SSH_ASKPASS"] = "echo"
            env["GCM_INTERACTIVE"] = "never"
            # Inject -c credential.helper= right after the 'git' executable
            if cmd and cmd[0] == "git":
                cmd = [cmd[0], "-c", "credential.helper="] + cmd[1:]
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"{cmd[0]}: command not found"
    except Exception as e:
        return -1, "", str(e)


# ---------------------------------------------------------------------------
# Check groups
# ---------------------------------------------------------------------------

def _check_python_env() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Python Environment"

    # Python version
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ver >= (3, 10):
        rows.append(EnvCheckRow(group, "Python version", STATUS_OK, f"Python {ver_str}"))
    elif ver >= (3, 9):
        rows.append(
            EnvCheckRow(
                group,
                "Python version",
                STATUS_WARN,
                f"Python {ver_str} — works, but 3.10+ recommended for best compatibility",
            )
        )
    else:
        rows.append(
            EnvCheckRow(
                group,
                "Python version",
                STATUS_FAIL,
                f"Python {ver_str} — ComfyUI requires >= 3.9",
                fix_cmd="Install Python 3.10+ from https://python.org",
            )
        )

    # pip availability
    rc, out, _ = _run([sys.executable, "-m", "pip", "--version"])
    if rc == 0:
        pip_ver = out.split()[1] if len(out.split()) > 1 else "?"
        rows.append(EnvCheckRow(group, "pip available", STATUS_OK, f"pip {pip_ver}"))
    else:
        rows.append(
            EnvCheckRow(
                group,
                "pip available",
                STATUS_FAIL,
                "pip not found",
                fix_cmd=f"{sys.executable} -m ensurepip",
            )
        )

    # Virtual environment check — looks at ComfyUI's Python isolation,
    # not the health-monitor tool's own Python (which intentionally runs on
    # system Python so it works without any venv setup).
    _comfyui_root: Path | None = None
    _env_path = os.environ.get("COMFYUI_PATH")
    if _env_path:
        _comfyui_root = Path(_env_path)
    else:
        _comfyui_root = detect_active_comfyui() or (find_all_comfyui_installs() or [None])[0]

    if _comfyui_root and _comfyui_root.is_dir():
        # Presence of any of these means ComfyUI has its own isolated Python
        _venv_indicators = [
            (_comfyui_root / ".venv",             "venv (.venv)"),
            (_comfyui_root / "venv",              "venv (venv/)"),
            (_comfyui_root / "python_embedded",   "embedded Python (portable)"),
            (_comfyui_root / "python_embeded",    "embedded Python (portable)"),
        ]
        _found_env: str | None = None
        for _venv_path, _label in _venv_indicators:
            if _venv_path.exists():
                _found_env = _label
                break

        if _found_env:
            rows.append(EnvCheckRow(group, "Virtual environment", STATUS_OK,
                                    f"ComfyUI uses isolated {_found_env}"))
        else:
            rows.append(EnvCheckRow(group, "Virtual environment", STATUS_WARN,
                                    f"No .venv or embedded Python found inside {_comfyui_root.name} "
                                    f"— ComfyUI may be sharing the system Python"))
    else:
        # Can't determine ComfyUI root — skip rather than false-alarm
        rows.append(EnvCheckRow(group, "Virtual environment", STATUS_INFO,
                                "ComfyUI path unknown — run with --comfyui-path to check isolation"))

    # tomllib / tomli
    if sys.version_info >= (3, 11):
        rows.append(EnvCheckRow(group, "tomllib (TOML)", STATUS_OK, "stdlib (3.11+)"))
    else:
        ver_ = _get_package_version("tomli")
        if ver_ and _version_ge(ver_, "2.0"):
            rows.append(EnvCheckRow(group, "tomli (TOML)", STATUS_OK, f"tomli {ver_}"))
        elif ver_:
            rows.append(
                EnvCheckRow(
                    group,
                    "tomli (TOML)",
                    STATUS_WARN,
                    f"tomli {ver_} installed but >= 2.0 recommended",
                    fix_cmd=f"{sys.executable} -m pip install 'tomli>=2.0'",
                )
            )
        else:
            rows.append(
                EnvCheckRow(
                    group,
                    "tomli (TOML)",
                    STATUS_FAIL,
                    "tomli not installed (needed on Python < 3.11)",
                    fix_cmd=f"{sys.executable} -m pip install 'tomli>=2.0'",
                )
            )

    # Required + optional packages
    _NOT_INSTALLED_DETAIL: dict[str, str] = {
        "aiohttp": "not required — httpx is the active HTTP client",
    }
    for pkg, min_ver, required in REQUIRED_PACKAGES:
        ver_ = _get_package_version(pkg)
        label = f"{pkg} >= {min_ver}" + ("" if required else " (optional)")
        if ver_ is None:
            status = STATUS_FAIL if required else STATUS_WARN
            detail = _NOT_INSTALLED_DETAIL.get(pkg, "Not installed")
            fix = f"{sys.executable} -m pip install '{pkg}>={min_ver}'"
            rows.append(EnvCheckRow(group, label, status, detail, fix_cmd=fix if required else None))
        elif _version_ge(ver_, min_ver):
            rows.append(EnvCheckRow(group, label, STATUS_OK, f"{ver_} installed"))
        else:
            status = STATUS_FAIL if required else STATUS_WARN
            rows.append(
                EnvCheckRow(
                    group,
                    label,
                    status,
                    f"Installed: {ver_}, need >= {min_ver}",
                    fix_cmd=f"{sys.executable} -m pip install --upgrade '{pkg}>={min_ver}'" if required else None,
                )
            )

    return rows


def _check_gpu_cuda() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "GPU & CUDA"

    # nvidia-smi
    rc, out, err = _run(["nvidia-smi"])
    if rc != 0:
        # Check for AMD/ROCm before giving up
        rc_rocm, out_rocm, _ = _run(["rocm-smi"])
        if rc_rocm == 0:
            rows.append(EnvCheckRow(group, "AMD ROCm GPU", STATUS_OK,
                                    "rocm-smi detected — AMD GPU present"))
            for line in out_rocm.strip().splitlines()[:8]:
                if line.strip() and not line.startswith("="):
                    rows.append(EnvCheckRow(group, "ROCm info", STATUS_INFO, line.strip()[:80]))
            return rows

        # macOS — check for Apple Silicon MPS
        if platform.system() == "Darwin":
            try:
                import torch  # type: ignore[import]
                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    rows.append(EnvCheckRow(group, "Apple Silicon MPS", STATUS_OK,
                                            "MPS (Metal) available — Apple GPU detected"))
                else:
                    rows.append(EnvCheckRow(group, "Apple Silicon MPS", STATUS_WARN,
                                            "MPS not available on this Mac"))
            except ImportError:
                rows.append(EnvCheckRow(group, "macOS GPU", STATUS_INFO,
                                        "torch not installed — cannot probe MPS"))
            return rows

        # Linux without NVIDIA — check /proc/driver/nvidia or lspci
        if platform.system() == "Linux":
            rc_lspci, out_lspci, _ = _run(["lspci"])
            if rc_lspci == 0:
                gpu_lines = [l for l in out_lspci.splitlines()
                             if any(x in l.lower() for x in ("vga", "3d", "display", "gpu"))]
                if gpu_lines:
                    for gl in gpu_lines[:4]:
                        rows.append(EnvCheckRow(group, "GPU (lspci)", STATUS_INFO, gl.strip()[:80]))
                    return rows

        # Windows — enumerate via WMI (handles AMD Radeon, Intel Arc, integrated)
        found_gpus = False
        if platform.system() == "Windows":
            rc_wmi, out_wmi, _ = _run([
                "powershell", "-NoProfile", "-Command",
                "Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name"
            ], timeout=5)
            if rc_wmi == 0 and out_wmi.strip():
                for gpu_line in out_wmi.strip().splitlines():
                    gpu_line = gpu_line.strip()
                    if gpu_line:
                        rows.append(EnvCheckRow(group, "GPU (WMI)", STATUS_INFO, gpu_line[:80]))
                        found_gpus = True

        if not found_gpus:
            rows.append(
                EnvCheckRow(
                    group,
                    "GPU",
                    STATUS_WARN,
                    "No GPU detected via nvidia-smi, rocm-smi, or system query — "
                    "CPU-only mode or unsupported GPU vendor",
                )
            )
        return rows

    rows.append(EnvCheckRow(group, "nvidia-smi", STATUS_OK, "nvidia-smi exits 0"))

    # Parse driver + CUDA version from first smi call
    driver_m = re.search(r"Driver Version:\s*([\d.]+)", out)
    cuda_m = re.search(r"CUDA Version:\s*([\d.]+)", out)
    driver_ver = driver_m.group(1) if driver_m else "?"
    cuda_ver = cuda_m.group(1) if cuda_m else "?"

    rows.append(EnvCheckRow(group, "NVIDIA Driver", STATUS_INFO, f"Driver {driver_ver}"))

    if cuda_ver != "?":
        try:
            cuda_major = int(cuda_ver.split(".")[0])
        except (ValueError, IndexError):
            cuda_major = -1
        if cuda_major >= 12:
            rows.append(EnvCheckRow(group, "CUDA version", STATUS_OK, f"CUDA {cuda_ver}"))
        elif cuda_major >= 11:
            rows.append(
                EnvCheckRow(
                    group,
                    "CUDA version",
                    STATUS_WARN,
                    f"CUDA {cuda_ver} ->= 12.0 recommended for Nunchaku",
                )
            )
        else:
            rows.append(
                EnvCheckRow(
                    group,
                    "CUDA version",
                    STATUS_FAIL,
                    f"CUDA {cuda_ver} -Nunchaku requires >= 11.8",
                )
            )
    else:
        rows.append(EnvCheckRow(group, "CUDA version", STATUS_WARN, "Could not parse CUDA version"))

    # Detailed GPU info
    rc2, out2, _ = _run([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.used",
        "--format=csv,noheader,nounits",
    ])
    if rc2 == 0:
        for line in out2.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                idx, name, mem_total, mem_used = parts[0], parts[1], parts[2], parts[3]
                try:
                    total_gb = int(mem_total) / 1024
                    used_gb = int(mem_used) / 1024
                    rows.append(
                        EnvCheckRow(
                            group,
                            f"GPU {idx}",
                            STATUS_INFO,
                            f"{name} -{total_gb:.1f} GB total, {used_gb:.1f} GB used",
                        )
                    )
                except ValueError:
                    rows.append(EnvCheckRow(group, f"GPU {idx}", STATUS_INFO, f"{name}"))

    # nvcc
    rc3, out3, _ = _run(["nvcc", "--version"])
    if rc3 == 0:
        nvcc_m = re.search(r"release ([\d.]+)", out3)
        nvcc_ver = nvcc_m.group(1) if nvcc_m else "?"
        rows.append(EnvCheckRow(group, "nvcc", STATUS_OK, f"nvcc {nvcc_ver}"))
    else:
        rows.append(
            EnvCheckRow(
                group,
                "nvcc",
                STATUS_WARN,
                "nvcc not on PATH (CUDA toolkit may not be installed, but pynvml works without it)",
            )
        )

    # pynvml enumeration
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", FutureWarning)
            import pynvml  # type: ignore[import]

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            sm = _get_sm_version(handle)
            arch = _sm_to_arch(sm)
            from .metrics import _sanitise_vram
            vram_total, _, _ = _sanitise_vram(mem.total, mem.used, mem.free)
            vram_str = (
                f"{vram_total / (1024**3):.1f} GB" if vram_total > 0
                else "VRAM unavailable"
            )
            rows.append(
                EnvCheckRow(
                    group,
                    f"pynvml GPU {i}",
                    STATUS_OK,
                    f"{name} — {vram_str}, {arch} (sm_{sm})",
                )
            )
    except ImportError:
        rows.append(
            EnvCheckRow(
                group,
                "pynvml enumeration",
                STATUS_WARN,
                "pynvml not installed in monitor env (optional — GPU metrics still available via nvidia-smi)",
            )
        )
    except Exception as e:
        rows.append(EnvCheckRow(group, "pynvml enumeration", STATUS_WARN, str(e)[:80]))

    return rows


def _get_sm_version(handle: Any) -> str:
    """Return SM version string like '89' for Ada Lovelace."""
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", FutureWarning)
            import pynvml  # type: ignore[import]

        major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
        return f"{major}{minor}"
    except Exception:
        return "?"


def _sm_to_arch(sm: str) -> str:
    table = {
        "80": "Ampere (A100) - INT4",
        "86": "Ampere (RTX 30xx) - INT4",
        "87": "Ampere (Jetson) - INT4",
        "89": "Ada Lovelace (RTX 40xx) - INT4",
        "90": "Hopper - INT4",
        "100": "Blackwell (RTX 50xx) - FP4",
    }
    return table.get(sm, f"sm_{sm}")


# ---------------------------------------------------------------------------
# Nunchaku package check
# ---------------------------------------------------------------------------

def _check_nunchaku_package() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Nunchaku Package"

    # Is nunchaku importable?
    try:
        import nunchaku as _nunchaku  # type: ignore[import]

        ver = getattr(_nunchaku, "__version__", None)
        if ver is None:
            try:
                ver = importlib.metadata.version("nunchaku")
            except Exception:
                ver = "unknown"
        rows.append(EnvCheckRow(group, "nunchaku importable", STATUS_OK, f"nunchaku {ver}"))

        # Check CUDA match
        cuda_ver: str | None = None
        try:
            import torch  # type: ignore[import]

            cuda_ver = torch.version.cuda
        except ImportError:
            pass

        if cuda_ver:
            rows.append(EnvCheckRow(group, "nunchaku CUDA build", STATUS_INFO, f"torch CUDA {cuda_ver}"))

    except ImportError:
        rows.append(
            EnvCheckRow(
                group,
                "nunchaku importable",
                STATUS_WARN,
                "not importable from monitor env · checked from monitor Python, not ComfyUI venv — "
                "see Core Package section for venv result",
            )
        )

        # Search for .whl files
        search_roots = _get_comfyui_custom_node_dirs()
        whl_found: list[Path] = []
        for root in search_roots:
            for whl in root.rglob("nunchaku*.whl"):
                whl_found.append(whl)

        if whl_found:
            whl_str = ", ".join(str(w) for w in whl_found[:3])
            rows.append(
                EnvCheckRow(
                    group,
                    "nunchaku wheel file",
                    STATUS_INFO,
                    f"Found: {whl_str}",
                    fix_cmd=f"{sys.executable} -m pip install {whl_found[0]}",
                )
            )
        else:
            rows.append(
                EnvCheckRow(
                    group,
                    "nunchaku wheel file",
                    STATUS_WARN,
                    "No nunchaku .whl found. Install via ComfyUI's NunchakuWheelInstaller node or manually.",
                )
            )

    # SM / arch precision hint
    rows.extend(_check_nunchaku_precision_hint())
    return rows


def _check_nunchaku_precision_hint() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Nunchaku Package"
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", FutureWarning)
            import pynvml  # type: ignore[import]

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        sm = _get_sm_version(handle)
        if sm.startswith("10"):  # sm_100 = Blackwell
            rows.append(
                EnvCheckRow(
                    group,
                    "Expected precision",
                    STATUS_INFO,
                    f"sm_{sm} ->FP4 mode (RTX 50 series Blackwell)",
                )
            )
        elif sm in ("80", "86", "87", "89", "90"):
            rows.append(
                EnvCheckRow(
                    group,
                    "Expected precision",
                    STATUS_INFO,
                    f"sm_{sm} ->INT4 mode ({_sm_to_arch(sm)})",
                )
            )
        else:
            rows.append(EnvCheckRow(group, "Expected precision", STATUS_INFO, f"sm_{sm} -unknown precision"))
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# ComfyUI installation check
# ---------------------------------------------------------------------------

def _get_comfyui_dirs() -> list[Path]:
    """
    Return candidate ComfyUI root directories in priority order.
    Covers: env var, common install locations on Windows/Linux/Mac,
    portable/embedded builds, and any folder starting with 'ComfyUI'
    found on the desktop or home directory.
    """
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add(p: Path) -> None:
        p = p.resolve() if p.exists() else p
        if p not in seen:
            seen.add(p)
            candidates.append(p)

    # 1. Explicit override always wins
    env = os.environ.get("COMFYUI_PATH")
    if env:
        add(Path(env))

    # 2. Relative to current working directory
    add(Path.cwd() / "ComfyUI")
    add(Path.cwd())  # running from inside a ComfyUI dir

    home = Path.home()

    # 3. Common user-level locations
    for base in [home, home / "Desktop", home / "Documents", home / "Downloads"]:
        add(base / "ComfyUI")
        # Also catch versioned names like ComfyUI-main, ComfyUI_v2, etc.
        if base.is_dir():
            try:
                for d in base.iterdir():
                    if d.is_dir() and d.name.lower().startswith("comfyui"):
                        add(d)
            except (PermissionError, OSError):
                pass

    # 4. Windows-specific common locations
    for drive in ["C:", "D:", "E:"]:
        for subfolder in ["ai", "SD", "stable-diffusion", "ComfyUI", ""]:
            base = Path(f"{drive}/{subfolder}") if subfolder else Path(f"{drive}/")
            add(base / "ComfyUI")
            if base.is_dir():
                try:
                    for d in base.iterdir():
                        if d.is_dir() and d.name.lower().startswith("comfyui"):
                            add(d)
                except (PermissionError, OSError):
                    pass

    if sys.platform == "win32":
        # ComfyUI Desktop app paths (Windows only — uses APPDATA/LOCALAPPDATA)
        import os as _os
        _appdata = _os.environ.get("APPDATA", "")
        _localappdata = _os.environ.get("LOCALAPPDATA", "")
        for _base in [
            Path(_appdata) / "ComfyUI",
            Path(_localappdata) / "ComfyUI",
            Path(_localappdata) / "Programs" / "ComfyUI",
            Path(_localappdata) / "comfyui-electron",
        ]:
            add(_base)

        # Portable zip layout: some_folder/ComfyUI/main.py (Windows drive letters only)
        for _drive in ["C:", "D:", "E:", "F:"]:
            for _sub in ["ai", "SD", "stable-diffusion", "tools", "apps", ""]:
                _base = Path(f"{_drive}/{_sub}") if _sub else Path(f"{_drive}/")
                try:
                    for _d in sorted(_base.iterdir()):
                        if "portable" in _d.name.lower():
                            add(_d / "ComfyUI")
                except (OSError, PermissionError):
                    # Covers non-existent drives, permission errors, and any
                    # TOCTOU race between existence check and directory listing.
                    pass

    # 5. Linux common locations
    for base in [
        Path("/opt/ComfyUI"),
        Path("/usr/local/ComfyUI"),
        home / ".local" / "share" / "ComfyUI",
        Path("/workspace/ComfyUI"),  # RunPod / Vast.ai
        Path("/content/ComfyUI"),    # Google Colab
        Path("/tmp/ComfyUI"),
        Path("/vol/ComfyUI"),        # RunPod network volumes
    ]:
        add(base)

    # Linux: scan ~/ai, ~/AI, ~/stable-diffusion one level deep
    for _base in [home / "ai", home / "AI", home / "stable-diffusion", home / "sd",
                  Path("/opt/ai"), Path("/workspace"), Path("/vol")]:
        if _base.exists() and _base.is_dir():
            try:
                for _d in sorted(_base.iterdir()):
                    if _d.is_dir() and "comfyui" in _d.name.lower():
                        add(_d)
                    add(_d / "ComfyUI")  # portable layout
            except Exception:
                pass

    # 6. macOS-specific
    for base in [
        home / "Applications" / "ComfyUI",
        home / "Documents" / "ComfyUI",
        home / "Library" / "Application Support" / "ComfyUI",
        Path("/Applications/ComfyUI"),
        Path("/Applications/ComfyUI.app") / "Contents" / "Resources" / "ComfyUI",
    ]:
        add(base)

    return candidates


def _comfyui_score(d: Path) -> int:
    """
    Structural confidence score that d is a ComfyUI root (0–100).
    Based on distinctive directory/file signatures rather than folder name.
    comfy/ is the single strongest indicator — no other AI project uses that name.
    """
    if not d.is_dir():
        return 0
    score = 0
    if (d / "comfy").is_dir():          score += 50   # core package — most unique
    if (d / "custom_nodes").is_dir():   score += 30   # nearly always present
    if (d / "comfy_extras").is_dir():   score += 20
    if (d / "main.py").exists():        score += 15
    if (d / "server.py").exists():      score += 12
    if (d / "web").is_dir():            score += 10
    if (d / "nodes").is_dir():          score += 8
    if (d / "models").is_dir():         score += 8
    if (d / "models" / "checkpoints").is_dir(): score += 8
    if (d / "input").is_dir():          score += 4
    if (d / "output").is_dir():         score += 4
    if (d / "requirements.txt").exists(): score += 3
    return min(score, 100)


def find_all_comfyui_installs() -> list[Path]:
    """
    Return all directories that structurally look like a ComfyUI installation,
    regardless of what they are named.  Uses confidence scoring so renamed or
    custom-location installs are still detected.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    for d in _get_comfyui_dirs():
        if not d.exists():
            continue
        s = _comfyui_score(d)
        # Accept anything that scores ≥ 20 (has at least some ComfyUI structure)
        # The old strict check (main.py or server.py) is kept as a floor.
        if s >= 20 or (d / "main.py").exists() or (d / "server.py").exists():
            resolved = d.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(d)

    # Sort best matches first so callers that take [0] get the strongest hit
    found.sort(key=lambda p: -_comfyui_score(p))
    return found


# Common ports ComfyUI may run on
_COMFYUI_PORTS = (8188, 8189, 7860, 7861, 8080, 8000, 3000, 3001)


def detect_active_comfyui() -> Path | None:
    """
    Return the ComfyUI root that is currently running, or None.
    Checks common ports first, then falls back to scanning Python process cwds
    so it works regardless of what port the user configured.
    """
    try:
        import psutil
    except ImportError:
        return None

    installs = find_all_comfyui_installs()
    installs_resolved = [i.resolve() for i in installs]

    def _match_cwd(cwd: Path) -> Path | None:
        cwd_r = cwd.resolve()
        for i, ir in zip(installs, installs_resolved):
            if cwd_r == ir or cwd_r.is_relative_to(ir):
                return i
        # Direct structural check even if not in known installs list
        if _comfyui_score(cwd) >= 20:
            return cwd
        return None

    # 1. Check all common ComfyUI ports
    try:
        listening: dict[int, int] = {}
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.pid:
                listening[getattr(conn.laddr, "port", 0)] = conn.pid

        for port in _COMFYUI_PORTS:
            pid = listening.get(port)
            if not pid:
                continue
            try:
                cwd = Path(psutil.Process(pid).cwd())
                result = _match_cwd(cwd)
                if result:
                    return result
            except Exception:
                pass
    except Exception:
        pass

    # 2. Port-agnostic fallback: any Python process in a ComfyUI directory
    try:
        for proc in psutil.process_iter(["pid", "name", "cwd", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cwd_str = proc.info.get("cwd")
                if not cwd_str:
                    continue
                result = _match_cwd(Path(cwd_str))
                if result:
                    return result
            except Exception:
                continue
    except Exception:
        pass

    return None


def _get_comfyui_custom_node_dirs() -> list[Path]:
    """Return candidate nunchaku custom_node dirs for .whl searching."""
    dirs: list[Path] = []
    for d in _get_comfyui_dirs_cached():
        cn = d / "custom_nodes" / "ComfyUI-nunchaku"
        dirs.append(cn)
    return dirs


def _check_comfyui_install() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "ComfyUI Installation"

    comfyui_root: Path | None = None
    for candidate in _get_comfyui_dirs_cached():
        if candidate.exists() and (
            (candidate / "main.py").exists() or (candidate / "server.py").exists()
        ):
            comfyui_root = candidate
            break

    if comfyui_root is None:
        rows.append(EnvCheckRow(
            group, "ComfyUI directory", STATUS_FAIL,
            "Not found. Set COMFYUI_PATH env var or place ComfyUI at ~/ComfyUI",
        ))
        return rows

    rows.append(EnvCheckRow(group, "ComfyUI directory", STATUS_OK, str(comfyui_root)))

    # Core directories
    models_dir = comfyui_root / "models"
    custom_nodes_dir = comfyui_root / "custom_nodes"

    if models_dir.exists():
        model_subdirs = [d for d in models_dir.iterdir() if d.is_dir()]
        rows.append(EnvCheckRow(group, "models/", STATUS_OK,
                                f"{len(model_subdirs)} subdirectories"))
    else:
        rows.append(EnvCheckRow(group, "models/", STATUS_FAIL, "Missing"))

    if custom_nodes_dir.exists():
        node_dirs = [d for d in custom_nodes_dir.iterdir()
                     if d.is_dir() and not d.name.startswith(("__", "."))]
        rows.append(EnvCheckRow(group, "custom_nodes/", STATUS_OK,
                                f"{len(node_dirs)} nodes installed"))
    else:
        rows.append(EnvCheckRow(group, "custom_nodes/", STATUS_WARN, "Missing"))

    return rows


def _check_nunchaku_node_registration(comfyui_root: Path) -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "ComfyUI Installation"
    nunchaku_dir = comfyui_root / "custom_nodes" / "ComfyUI-nunchaku"

    if not nunchaku_dir.exists():
        return rows

    # Try reading __init__.py or nodes/__init__.py
    candidates = [
        nunchaku_dir / "__init__.py",
        nunchaku_dir / "nodes" / "__init__.py",
        nunchaku_dir / "nodes.py",
    ]
    source = ""
    for p in candidates:
        if p.exists():
            try:
                source += p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

    expected_nodes = [
        "NunchakuFluxDiTLoader",
        "NunchakuTextEncoderLoader",
        "NunchakuFluxLoraLoader",
    ]
    for node in expected_nodes:
        # Also allow V2 variants
        if node in source or node + "V2" in source:
            rows.append(EnvCheckRow(group, f"Node: {node}", STATUS_OK, "Found in source"))
        elif not source:
            rows.append(EnvCheckRow(group, f"Node: {node}", STATUS_WARN, "Could not read node source"))
        else:
            rows.append(
                EnvCheckRow(
                    group,
                    f"Node: {node}",
                    STATUS_WARN,
                    "Not found in node source -partial install or wrong version?",
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Model files check (dynamic — any build, any model)
# ---------------------------------------------------------------------------

def _check_model_files() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Model Files"

    comfyui_root: Path | None = None
    for candidate in _get_comfyui_dirs_cached():
        if candidate.exists() and (
            (candidate / "main.py").exists() or (candidate / "server.py").exists()
        ):
            comfyui_root = candidate
            break

    if comfyui_root is None:
        rows.append(EnvCheckRow(group, "Model scan", STATUS_WARN, "ComfyUI not found -skipping model scan"))
        return rows

    from .model_scanner import scan_models, MODEL_EXTENSIONS

    scan = scan_models(comfyui_root)

    if not scan.total_files:
        rows.append(EnvCheckRow(group, "Model scan", STATUS_WARN,
                                f"No model files found under {comfyui_root / 'models'}"))
        return rows

    # One summary line
    rows.append(EnvCheckRow(
        group, "Total models",
        STATUS_INFO,
        f"{scan.total_files} files, {scan.total_size_gb:.1f} GB across {len(scan.by_category)} categories",
    ))

    # One row per category: show top 1-2 files + count
    for category in sorted(scan.by_category):
        files = scan.by_category[category]
        top = files[0]
        detail = f"{top.path.name} ({top.size_gb:.2f} GB)"
        if top.quant:
            detail += f" [{top.quant}]"
        if top.family:
            detail += f" - {top.family}"
        if len(files) > 1:
            detail += f"  (+{len(files)-1} more, {sum(f.size_gb for f in files):.1f} GB total)"
        rows.append(EnvCheckRow(group, category, STATUS_OK, detail))

    return rows


def _check_file(rows: list[EnvCheckRow], group: str, path: Path, label: str) -> None:
    try:
        st = path.stat()
    except FileNotFoundError:
        rows.append(EnvCheckRow(group, label, STATUS_FAIL, f"Not found: {path}"))
        return
    except (PermissionError, OSError) as e:
        rows.append(EnvCheckRow(group, label, STATUS_WARN, f"Cannot read: {e}"))
        return
    if st.st_size > 0:
        size_gb = st.st_size / (1024 ** 3)
        rows.append(EnvCheckRow(group, label, STATUS_OK, f"{size_gb:.2f} GB"))
    else:
        rows.append(EnvCheckRow(group, label, STATUS_FAIL, f"File exists but is empty: {path}"))


# ---------------------------------------------------------------------------
# Port & process check
# ---------------------------------------------------------------------------

def _check_port_process(url: str = "http://127.0.0.1:8188") -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Port & Process"

    # Parse host and port from URL
    import re as _re
    host_m = _re.search(r"https?://([^:/]+)", url)
    host = host_m.group(1) if host_m else "127.0.0.1"
    port_m = _re.search(r":(\d+)", url.split("//", 1)[-1])  # skip the // in http://
    port = int(port_m.group(1)) if port_m else 8188

    # Socket check
    listening = False
    try:
        with socket.create_connection((host, port), timeout=1):
            listening = True
    except (ConnectionRefusedError, socket.timeout, OSError):
        listening = False

    if not listening:
        rows.append(
            EnvCheckRow(
                group,
                f"Port {port}",
                STATUS_FAIL,
                f"Nothing listening on {host}:{port} -ComfyUI not running",
            )
        )
        # Check for ComfyUI python process
        try:
            import psutil  # type: ignore

            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "main.py" in cmdline and "comfyui" in cmdline.lower():
                    rows.append(
                        EnvCheckRow(
                            group,
                            "ComfyUI process",
                            STATUS_WARN,
                            f"PID {proc.info['pid']} running but not yet listening on {port}",
                        )
                    )
                    break
        except Exception:
            pass
        return rows

    rows.append(EnvCheckRow(group, f"Port {port}", STATUS_OK, f"{host}:{port} is open"))

    # Try /system_stats
    try:
        import urllib.request

        with urllib.request.urlopen(f"{url}/system_stats", timeout=3) as resp:
            import json

            data = json.loads(resp.read())
            if data:
                rows.append(EnvCheckRow(group, "ComfyUI /system_stats", STATUS_OK, "Returns valid JSON"))
            else:
                rows.append(EnvCheckRow(group, "ComfyUI /system_stats", STATUS_WARN, "Empty response"))
    except Exception as e:
        rows.append(
            EnvCheckRow(
                group,
                "ComfyUI /system_stats",
                STATUS_WARN,
                f"Port open but /system_stats failed: {str(e)[:60]}",
            )
        )

    return rows


# ---------------------------------------------------------------------------
# Disk space check
# ---------------------------------------------------------------------------

def _check_disk_space() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Disk Space"

    try:
        import shutil as _shutil

        # Find the models directory
        models_dir: Path | None = None
        for candidate in _get_comfyui_dirs_cached():
            md = candidate / "models"
            if md.exists():
                models_dir = md
                break

        check_path = str(models_dir) if models_dir else str(Path.home())
        usage = _shutil.disk_usage(check_path)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)

        label = f"Disk free ({check_path[:30]})"
        detail = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total"

        if free_gb < 5:
            rows.append(EnvCheckRow(group, label, STATUS_FAIL, f"CRITICAL: {detail}"))
        elif free_gb < 20:
            rows.append(
                EnvCheckRow(group, label, STATUS_WARN, f"Low: {detail} (FLUX models need ~20 GB+)")
            )
        else:
            rows.append(EnvCheckRow(group, label, STATUS_OK, detail))

    except Exception as e:
        rows.append(EnvCheckRow(group, "Disk space", STATUS_WARN, f"Could not check: {e}"))

    return rows


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------

def _auto_fix(rows: list[EnvCheckRow], report: EnvCheckReport) -> None:
    """
    Attempt safe pip installs for failed required packages.
    Takes a snapshot of the current pip state BEFORE making any changes.
    """
    # Only snapshot if there is actually something to fix
    fixable = [r for r in rows if r.status == STATUS_FAIL and r.fix_cmd and r.fix_cmd.startswith(sys.executable)]
    if fixable:
        from .backup import create_snapshot, purge_old_snapshots
        try:
            snap = create_snapshot(python_exe=sys.executable, note="before auto-fix")
            report.auto_fixed.append(f"[snapshot] pip state saved to {snap}")
            purge_old_snapshots(keep=10)
        except Exception as e:
            report.manual_needed.append(f"[snapshot] Could not save backup: {e}")

    for row in rows:
        if row.status == STATUS_FAIL and row.fix_cmd and row.fix_cmd.startswith(sys.executable):
            print(f"  Auto-fixing: {row.fix_cmd}")
            rc, out, err = _run(row.fix_cmd.split(), timeout=60)
            if rc == 0:
                report.auto_fixed.append(f"{row.check}: {row.fix_cmd}")
                row.status = STATUS_OK
                row.detail += " (auto-fixed)"
            else:
                report.manual_needed.append(f"{row.check}: run manually -{row.fix_cmd}")
        elif row.status in (STATUS_FAIL, STATUS_WARN) and row.fix_cmd:
            report.manual_needed.append(f"  {row.check}: {row.fix_cmd}")


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------

def _check_comfyui_updates() -> list[EnvCheckRow]:
    rows: list[EnvCheckRow] = []
    group = "Updates"

    comfyui_dirs = [
        d for d in _get_comfyui_dirs_cached()
        if d.exists() and ((d / "main.py").exists() or (d / "server.py").exists())
    ]

    if not comfyui_dirs:
        return rows

    comfyui_root = comfyui_dirs[0]

    # ComfyUI itself
    rc, _, _ = _run(["git", "-C", str(comfyui_root), "fetch", "--dry-run"], timeout=15, git=True)
    rc2, behind, _ = _run(
        ["git", "-C", str(comfyui_root), "rev-list", "--count", "HEAD..@{upstream}"],
        timeout=10, git=True
    )
    if rc2 == 0:
        try:
            n = int(behind.strip())
            if n > 0:
                rows.append(EnvCheckRow(group, "ComfyUI updates", STATUS_WARN,
                                        f"{n} commit(s) behind upstream"))
            else:
                rows.append(EnvCheckRow(group, "ComfyUI updates", STATUS_OK,
                                        "Up to date"))
        except ValueError:
            rows.append(EnvCheckRow(group, "ComfyUI updates", STATUS_INFO,
                                    "Could not determine update status"))
    else:
        rows.append(EnvCheckRow(group, "ComfyUI updates", STATUS_INFO,
                                "No upstream configured or git not available"))

    # Custom nodes — fetch all in parallel (was sequential, O(N×10s) → O(10s))
    nodes_dir = comfyui_root / "custom_nodes"
    if nodes_dir.exists():
        try:
            _node_entries = sorted(nodes_dir.iterdir())
        except (PermissionError, OSError):
            _node_entries = []
        git_node_dirs = [
            d for d in _node_entries
            if d.is_dir() and (d / ".git").exists()
        ]

        def _check_node(node_dir: Path) -> str | None:
            """Return 'name (N)' if behind, else None."""
            _run(["git", "-C", str(node_dir), "fetch", "--dry-run"], timeout=10, git=True)
            rc3, behind3, _ = _run(
                ["git", "-C", str(node_dir), "rev-list", "--count", "HEAD..@{upstream}"],
                timeout=8, git=True,
            )
            if rc3 == 0:
                try:
                    n3 = int(behind3.strip())
                    if n3 > 0:
                        return f"{node_dir.name} ({n3})"
                except ValueError:
                    pass
            return None

        outdated: list[str] = []
        if git_node_dirs:
            with ThreadPoolExecutor(max_workers=min(8, len(git_node_dirs))) as _node_pool:
                for result in _node_pool.map(_check_node, git_node_dirs):
                    if result:
                        outdated.append(result)
            outdated.sort()

        if outdated:
            rows.append(EnvCheckRow(group, "Custom node updates", STATUS_INFO,
                                    f"{len(outdated)} node(s) have upstream commits: " +
                                    ", ".join(outdated[:5]) +
                                    (f" (+{len(outdated)-5} more)" if len(outdated) > 5 else "") +
                                    " · consider updating via ComfyUI Manager"))
        else:
            rows.append(EnvCheckRow(group, "Custom node updates", STATUS_OK,
                                    "All nodes up to date (or no upstream configured)"))

    return rows


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_env_checks(fix: bool = False) -> EnvCheckReport:
    """Run all environment checks and return a report."""
    global _dirs_cache
    from .pip_checks import run_pip_checks

    report = EnvCheckReport()
    all_rows: list[EnvCheckRow] = []

    # Pre-warm the directory cache once so every parallel worker shares it.
    _dirs_cache = _get_comfyui_dirs()

    try:
        # All check groups are independent — run them in parallel.
        # Display order is preserved by collecting results in the original sequence.
        _ordered_keys = [
            "pip", "python", "gpu", "nunchaku",
            "install", "models", "disk", "updates",
        ]
        _tasks = {
            "pip":      lambda: run_pip_checks(),
            "python":   _check_python_env,
            "gpu":      _check_gpu_cuda,
            "nunchaku": _check_nunchaku_package,
            "install":  _check_comfyui_install,
            "models":   _check_model_files,
            "disk":     _check_disk_space,
            "updates":  _check_comfyui_updates,
        }
        _results: dict[str, list[EnvCheckRow]] = {}
        with ThreadPoolExecutor(max_workers=len(_tasks)) as _pool:
            _futures = {key: _pool.submit(fn) for key, fn in _tasks.items()}
            for key, future in _futures.items():
                try:
                    _results[key] = future.result()
                except Exception as _exc:
                    _results[key] = [EnvCheckRow(
                        "Error", key, STATUS_WARN,
                        f"Check raised an exception: {_exc}",
                    )]

        for key in _ordered_keys:
            all_rows.extend(_results[key])

    finally:
        _dirs_cache = None  # clear cache so next run starts fresh

    if fix:
        _auto_fix(all_rows, report)
    else:
        for row in all_rows:
            if row.fix_cmd:
                report.manual_needed.append(f"  {row.check}: {row.fix_cmd}")

    report.rows = all_rows
    return report


# ---------------------------------------------------------------------------
# Privilege / UAC probe
# ---------------------------------------------------------------------------

def probe_privilege_limits() -> dict[str, Any]:
    """
    Probe what the current process can and cannot do due to OS permissions.

    Called once at menu startup so results can be surfaced before the user
    hits an operation that fails silently.

    Returns a dict with:
      is_admin         — True if running as Administrator (Windows) or root (Unix)
      net_connections  — True if psutil.net_connections() is accessible;
                         False on Windows without elevation (AccessDenied)
      backup_writable  — True if the backups directory can be written to;
                         False when the app lives in a UAC-protected path

    On Windows without admin:
      • net_connections=False means port-based ComfyUI detection is unavailable
        (falls back to process-cwd scanning, which works if ComfyUI runs as the
        same user; fails if ComfyUI runs elevated and the monitor does not)
      • backup_writable=False means pip-state snapshots cannot be saved
    """
    from .backup import _BACKUP_DIR

    limits: dict[str, Any] = {
        "is_admin": False,
        "net_connections": True,
        "backup_writable": True,
    }

    # ── Admin / root check ────────────────────────────────────────────────
    try:
        if sys.platform == "win32":
            import ctypes
            limits["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        else:
            limits["is_admin"] = (os.getuid() == 0)
    except Exception:
        pass

    # ── psutil network visibility ─────────────────────────────────────────
    # On Windows, the global TCP/UDP table requires SeDebugPrivilege or
    # elevation.  psutil.net_connections() raises AccessDenied without it.
    try:
        import psutil as _ps
        _ps.net_connections(kind="inet")
    except ImportError:
        pass  # psutil not installed — not a privilege issue
    except Exception:
        limits["net_connections"] = False

    # ── Backup directory write access ─────────────────────────────────────
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _probe = _BACKUP_DIR / ".write_probe"
        _probe.write_text("ok", encoding="utf-8")
        _probe.unlink(missing_ok=True)
    except (PermissionError, OSError):
        limits["backup_writable"] = False

    return limits
