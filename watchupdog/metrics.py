"""System metrics: CPU, RAM, VRAM via psutil + pynvml (or nvidia-smi fallback)."""

from __future__ import annotations

import subprocess
from typing import Any

# Sanity cap for VRAM values.  No shipping hardware exceeds ~80 GB per device;
# NVML returns NVML_VALUE_NOT_AVAILABLE (0xFFFFFFFF or 0xFFFFFFFFFFFFFFFF) when
# a field is unavailable instead of raising an exception.  Any value above this
# threshold is treated as "not available" and clamped to 0.
_VRAM_SANE_MAX_BYTES: int = 1 << 43   # 8 TiB — comfortably above any real GPU

try:
    import psutil

    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import pynvml  # type: ignore[import]
    pynvml.nvmlInit()
    _PYNVML_OK = True
except Exception:
    _PYNVML_OK = False


def get_cpu_percent() -> float | None:
    if not _PSUTIL_OK:
        return None
    try:
        return psutil.cpu_percent(interval=0.1)
    except Exception:
        return None


def get_ram_info() -> dict[str, int] | None:
    """Returns total/used/free in bytes, or None."""
    if not _PSUTIL_OK:
        return None
    try:
        mem = psutil.virtual_memory()
        return {"total": mem.total, "used": mem.used, "free": mem.available}
    except Exception:
        return None


def _sanitise_vram(total: int, used: int, free: int) -> tuple[int, int, int]:
    """
    Clamp raw NVML memory values to a coherent (total, used, free) triple.

    NVML can return garbage in several ways when it initialises but then
    encounters a driver inconsistency:

    • NVML_VALUE_NOT_AVAILABLE (0xFFFFFFFF / 0xFFFFFFFFFFFFFFFF) — sentinel for
      "field unavailable"; treat the whole device as having no readable VRAM.
    • free > total — valid when a cudaFree() lands between the two internal
      driver reads; clamp free to total so downstream arithmetic stays non-negative.
    • total <= 0 — nonsensical; treat as unavailable.
    • used or free < 0 — can't happen with unsigned NVML types, but JSON sources
      could supply negatives; clamp to 0.

    Returns (total, used, free) where 0 <= used <= total and 0 <= free <= total.
    If total is unusable the triple (0, 0, 0) is returned and the caller should
    skip the device.
    """
    # Sentinel check before any other arithmetic
    if total <= 0 or total >= _VRAM_SANE_MAX_BYTES:
        return 0, 0, 0
    used  = max(0, min(int(used),  total))
    free  = max(0, min(int(free),  total))
    return int(total), used, free


def get_vram_info_pynvml() -> list[dict[str, Any]]:
    """Return per-GPU VRAM info via pynvml."""
    if not _PYNVML_OK:
        return []
    try:
        count = pynvml.nvmlDeviceGetCount()
        devices = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            total, used, free = _sanitise_vram(mem.total, mem.used, mem.free)
            if total == 0:
                continue   # NVML reported unusable values — skip device
            devices.append(
                {
                    "index": i,
                    "name": name,
                    "vram_total": total,
                    "vram_used": used,
                    "vram_free": free,
                }
            )
        return devices
    except Exception:
        return []


def get_vram_info_nvidia_smi() -> list[dict[str, Any]]:
    """Fallback: parse nvidia-smi for VRAM info."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        devices = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    devices.append(
                        {
                            "index": int(parts[0]),
                            "name": parts[1],
                            "vram_total": int(parts[2]) * 1024 * 1024,
                            "vram_used": int(parts[3]) * 1024 * 1024,
                            "vram_free": int(parts[4]) * 1024 * 1024,
                        }
                    )
                except ValueError:
                    pass
        return devices
    except Exception:
        return []


def get_vram_info() -> list[dict[str, Any]]:
    """Return VRAM info, preferring pynvml over nvidia-smi."""
    info = get_vram_info_pynvml()
    if info:
        return info
    return get_vram_info_nvidia_smi()
