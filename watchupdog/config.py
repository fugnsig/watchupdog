"""TOML config loader with sensible defaults."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG: dict[str, Any] = {
    "url": "http://127.0.0.1:8188",
    "interval": 5,
    "timeout": 5,
    "max_backups": 10,
    "thresholds": {
        "queue_warn": 10,
        "vram_warn_pct": 90,
        "vram_critical_pct": 97,
        "ram_warn_pct": 85,
        "disk_warn_gb": 20.0,
        "disk_critical_gb": 5.0,
        "disk_warn_pct": 90.0,
        "disk_critical_pct": 95.0,
        "stale_job_minutes": 5,
        "history_jobs": 50,
        # Nunchaku VRAM anomaly: warn if a card with >= nunchaku_min_card_gb
        # total VRAM is using more than nunchaku_vram_anomaly_gb, which suggests
        # Nunchaku's quantised weights did not load (non-quantised fallback).
        "nunchaku_vram_anomaly_gb": 14.0,
        "nunchaku_min_card_gb": 12.0,
    },
    # expected_models is intentionally empty — the tool discovers what is
    # actually present rather than checking for specific filenames.
    # Add entries here only if you want to enforce specific files are loaded.
    # Example:
    #   "flux": ["svdq-int4_r32-flux.1-dev.safetensors"]
    "expected_models": {},
    "nunchaku_nodes": [
        "NunchakuFluxDiTLoader",
        "NunchakuTextEncoderLoader",
        "NunchakuFluxLoraLoader",
        "NunchakuWheelInstaller",
    ],
    # Set to false to skip Nunchaku-specific checks (for non-Nunchaku installs)
    "nunchaku_checks": True,
    # Python / venv resolution strategy for pip checks.
    # strategy: auto | venv | embedded | conda | system
    #   auto     — try venv candidates, then conda, then system Python (default)
    #   venv     — look for a standard venv at venv_path inside the ComfyUI root
    #   embedded — look for embedded Python at venv_path inside the ComfyUI root
    #   conda    — use the conda env at venv_path (absolute) or CONDA_PREFIX
    #   system   — use system Python (last resort, not recommended)
    # venv_path: relative (or absolute for conda) path override.
    #   Ignored when strategy = "auto".
    #   Examples: ".venv", "python_embeded", "/home/user/miniconda3/envs/comfyui"
    "python": {
        "strategy": "auto",
        "venv_path": "",
    },
    "webhooks": {
        "discord_url": "",
        "ntfy_url": "",
        "on_warn": False,
        "min_interval_seconds": 300,
    },
}


# Per-threshold validation rules: (coerce_type, min_value, max_value)
# Values outside the range are clamped; unparseable strings fall back to default.
_THRESHOLD_SPECS: dict[str, tuple[type, float, float]] = {
    "queue_warn":               (int,   1,       10_000),
    "vram_warn_pct":            (float, 0.0,     100.0),
    "vram_critical_pct":        (float, 0.0,     100.0),
    "ram_warn_pct":             (float, 0.0,     100.0),
    "disk_warn_gb":             (float, 0.0,     1_000_000.0),
    "disk_critical_gb":         (float, 0.0,     1_000_000.0),
    "disk_warn_pct":            (float, 0.0,     100.0),
    "disk_critical_pct":        (float, 0.0,     100.0),
    "stale_job_minutes":        (float, 0.1,     10_000.0),
    "history_jobs":             (int,   1,       100_000),
    "nunchaku_vram_anomaly_gb": (float, 0.0,     1_000.0),
    "nunchaku_min_card_gb":     (float, 0.0,     1_000.0),
}


def _sanitize_thresholds(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *raw* with each known threshold coerced and clamped.

    - Non-numeric strings → replaced with the default value for that key.
    - Negative values or values above 100 for percentages → clamped to [min, max].
    - Unknown keys are passed through unchanged.
    - Missing keys are filled from DEFAULT_CONFIG defaults.
    """
    defaults = DEFAULT_CONFIG["thresholds"]
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if key in _THRESHOLD_SPECS:
            t, lo, hi = _THRESHOLD_SPECS[key]
            try:
                coerced: float = t(val)
            except (TypeError, ValueError):
                coerced = t(defaults[key])
            out[key] = max(lo, min(hi, coerced))
        else:
            out[key] = val
    # Fill in any threshold keys absent from the user's config
    for key, default_val in defaults.items():
        if key not in out:
            out[key] = default_val
    return out


class Config:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def url(self) -> str:
        val = self._data.get("url", DEFAULT_CONFIG["url"])
        if not isinstance(val, str) or not val.strip():
            return str(DEFAULT_CONFIG["url"])
        return val

    @property
    def interval(self) -> int:
        try:
            return int(self._data.get("interval", DEFAULT_CONFIG["interval"]))
        except (TypeError, ValueError):
            return int(DEFAULT_CONFIG["interval"])

    @property
    def timeout(self) -> float:
        try:
            return float(self._data.get("timeout", DEFAULT_CONFIG["timeout"]))
        except (TypeError, ValueError):
            return float(DEFAULT_CONFIG["timeout"])

    @property
    def thresholds(self) -> dict[str, Any]:
        val = self._data.get("thresholds", {})
        raw = val if isinstance(val, dict) else {}
        return _sanitize_thresholds(raw)

    @property
    def expected_models(self) -> dict[str, list[str]]:
        val = self._data.get("expected_models", DEFAULT_CONFIG["expected_models"])
        return val if isinstance(val, dict) else {}

    @property
    def nunchaku_nodes(self) -> list[str]:
        val = self._data.get("nunchaku_nodes", DEFAULT_CONFIG["nunchaku_nodes"])
        return val if isinstance(val, list) else list(DEFAULT_CONFIG["nunchaku_nodes"])

    @property
    def nunchaku_checks(self) -> bool:
        return bool(self._data.get("nunchaku_checks", True))

    @property
    def max_backups(self) -> int:
        try:
            return int(self._data.get("max_backups", 10))
        except (TypeError, ValueError):
            return 10

    @property
    def webhooks(self) -> dict[str, Any]:
        return self._data.get("webhooks", {})

    @property
    def python_cfg(self) -> dict[str, Any]:
        val = self._data.get("python", {})
        return val if isinstance(val, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict):
            if isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                # TOML value is a scalar but the default expects a dict (e.g.
                # the user wrote `thresholds = "disabled"` instead of a table).
                # Silently keep the default so every downstream dict-subscript
                # remains safe.  A warning is issued so the user knows their
                # config key was ignored.
                import warnings
                warnings.warn(
                    f"Config key {k!r} expects a table but got "
                    f"{type(v).__name__!r} — keeping default value.",
                    stacklevel=3,
                )
        else:
            result[k] = v
    return result


def load_config(config_path: Path | str | None = None) -> Config:
    """Load config from file, merging with defaults. Falls back gracefully."""
    data = copy.deepcopy(DEFAULT_CONFIG)

    if tomllib is None:
        return Config(data)

    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.append(Path("watchupdog.toml"))
    xdg = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    candidates.append(Path(xdg) / "watchupdog" / "config.toml")

    for path in candidates:
        if path.exists():
            try:
                with open(path, "rb") as f:
                    file_data = tomllib.load(f)
                data = _deep_merge(data, file_data)
                break
            except Exception as e:
                import warnings
                warnings.warn(f"Failed to load config from {path}: {e}", stacklevel=2)
                pass

    return Config(data)
