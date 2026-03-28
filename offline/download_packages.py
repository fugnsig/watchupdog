#!/usr/bin/env python3
"""
watchupdog — Offline Package Downloader
====================================================
Run this script on an internet-connected machine to pre-download all
required Python wheel files into the  offline/packages/  directory.

Once downloaded, copy the entire  watchupdog_monitor/  folder
(including  offline/packages/ ) to the target offline machine.
The install scripts (install-windows.bat, install-linux.sh,
install-macos.command) will automatically detect and use the local
wheels instead of downloading from the internet.

Usage:
    python offline/download_packages.py              # download for current platform
    python offline/download_packages.py --all-platforms  # cross-platform wheels too

Requirements:
    Python 3.10+  and  pip  (no extra packages needed)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import platform
from pathlib import Path

# ── Packages to pre-download ──────────────────────────────────────────────────
# These match the dependencies declared in pyproject.toml.
# Transitive dependencies are resolved and downloaded automatically by pip.
CORE_PACKAGES = [
    "rich>=13.0",
    "click>=8.1",
    "httpx>=0.27",
    "pydantic>=2.0",
    "psutil>=5.9",
    "tomli>=2.0",
]

OPTIONAL_PACKAGES = [
    "pynvml>=11.0",       # NVIDIA GPU metrics
    "fastapi>=0.110",     # Web dashboard server
    "uvicorn>=0.29",      # ASGI server for dashboard
    "websockets>=12.0",   # WebSocket support
    "aiohttp>=3.9",       # Async HTTP
]

# Build / setuptools wheels (needed for pip install -e . on some systems)
BUILD_PACKAGES = [
    "setuptools>=65",
    "wheel",
]


def download(packages: list[str], dest: Path, extra_args: list[str]) -> dict[str, bool]:
    """Download wheels for a list of packages. Returns {pkg: success}."""
    results: dict[str, bool] = {}
    for pkg in packages:
        label = pkg.split(">=")[0].split("==")[0].split("[")[0]
        print(f"  Downloading  {label} ...", end="", flush=True)
        cmd = [
            sys.executable, "-m", "pip", "download",
            pkg,
            "--dest", str(dest),
            "--quiet",
        ] + extra_args
        rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc == 0:
            print("  OK")
            results[pkg] = True
        else:
            # Retry without --quiet to show the error
            print("  FAILED (retrying with verbose output...)")
            subprocess.call([
                sys.executable, "-m", "pip", "download",
                pkg, "--dest", str(dest),
            ])
            results[pkg] = False
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-download watchupdog wheels for offline installation."
    )
    parser.add_argument(
        "--all-platforms",
        action="store_true",
        help=(
            "Also download pure-Python wheels that work on all platforms. "
            "Useful when preparing packages for a different OS."
        ),
    )
    parser.add_argument(
        "--optional",
        action="store_true",
        default=True,
        help="Include optional packages (pynvml, fastapi, etc.)  [default: yes]",
    )
    parser.add_argument(
        "--no-optional",
        dest="optional",
        action="store_false",
        help="Skip optional packages",
    )
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "packages"
    out_dir.mkdir(exist_ok=True)

    print()
    print("=" * 60)
    print("  watchupdog — Offline Package Downloader")
    print("=" * 60)
    print()
    print(f"  Python    : {sys.executable}")
    print(f"  Platform  : {platform.system()} {platform.machine()}")
    print(f"  Output    : {out_dir}")
    print()

    extra: list[str] = []
    if args.all_platforms:
        # Download platform-independent wheels only
        extra = ["--only-binary", ":all:", "--platform", "any"]
        print("  Mode: all-platforms (pure-Python wheels only)")
    else:
        print("  Mode: current platform")
    print()

    all_packages = CORE_PACKAGES + BUILD_PACKAGES
    if args.optional:
        all_packages += OPTIONAL_PACKAGES

    print(f"  Packages to download: {len(all_packages)}")
    print()

    print("─── Core packages ────────────────────────────────────")
    core_results = download(CORE_PACKAGES + BUILD_PACKAGES, out_dir, extra)

    if args.optional:
        print()
        print("─── Optional packages ────────────────────────────────")
        opt_results = download(OPTIONAL_PACKAGES, out_dir, extra)
    else:
        opt_results = {}

    all_results = {**core_results, **opt_results}
    wheels = list(out_dir.glob("*.whl"))

    print()
    print("=" * 60)
    failed = [k for k, v in all_results.items() if not v]
    if failed:
        print(f"  WARNING: {len(failed)} package(s) failed to download:")
        for f in failed:
            print(f"    - {f}")
        print()
        print("  The install scripts may need internet for these packages.")
    else:
        print(f"  All packages downloaded successfully.")
    print()
    print(f"  {len(wheels)} wheel file(s) saved to:")
    print(f"  {out_dir}")
    print()
    print("  Next steps:")
    print("  1. Copy the entire watchupdog_monitor/ folder to")
    print("     the offline machine (USB drive, network share, etc.)")
    print("  2. On the offline machine, run the appropriate installer:")
    print("       Windows : install-windows.bat")
    print("       Linux   : bash install-linux.sh")
    print("       macOS   : open install-macos.command")
    print()


if __name__ == "__main__":
    main()
