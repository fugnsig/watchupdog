watchupdog — Offline Installation Guide
=====================================================

This folder contains everything needed to install the ComfyUI Health
Monitor on a machine with no internet access (air-gapped, restricted
networks, corporate environments, etc.).


OVERVIEW
--------
The install scripts (install-windows.bat / install-linux.sh /
install-macos.command) automatically check for this folder.
If  offline/packages/  contains .whl files, they are used instead
of downloading from the internet.


STEP 1 — Prepare offline packages (on an internet-connected machine)
----------------------------------------------------------------------
Run the downloader script from the project root:

    Windows:   python offline\download_packages.py
    Linux/Mac: python3 offline/download_packages.py

This downloads all required Python wheel files into offline/packages/.
The script uses your current platform by default.  To download wheels
for a DIFFERENT platform (e.g., preparing on Windows for a Linux box),
pass --all-platforms to get the portable pure-Python wheels:

    python offline/download_packages.py --all-platforms

Options:
    --all-platforms    Download platform-independent wheels (cross-OS)
    --no-optional      Skip pynvml, fastapi, etc.  (saves ~20 MB)


STEP 2 — Transfer to the offline machine
-----------------------------------------
Copy the ENTIRE watchupdog_monitor/ folder to the target machine
using a USB drive, network share, or any other method.

The folder structure should look like:

    watchupdog_monitor/
        install-windows.bat
        install-linux.sh
        install-macos.command
        watchupdog-windows.bat      ← main launcher (Windows)
        watchupdog-linux.sh         ← main launcher (Linux)
        watchupdog-macos.command    ← main launcher (macOS)
        offline/
            README.txt                  ← this file
            download_packages.py        ← downloader script
            packages/
                rich-13.x.x-py3-...whl
                click-8.x.x-py3-...whl
                httpx-0.xx.x-py3-...whl
                ...and more .whl files...
        watchupdog/
            ...source code...


STEP 3 — Run the installer on the offline machine
---------------------------------------------------
NOTE: Python itself must already be installed, OR you must install it
manually before running the offline installer.  The wheel cache covers
Python packages only — not Python itself or Git.

    If Python is NOT installed on the offline machine:
      - Windows: Download python-3.13.x-amd64.exe from python.org on
        another machine, copy it over, run it.  Tick "Add to PATH".
      - Linux:   sudo apt install python3 python3-pip
                 sudo dnf install python3 python3-pip
      - macOS:   Install Xcode CLT first, then Homebrew, then:
                 brew install python3

    Once Python is available:
      - Windows: Double-click  install-windows.bat
      - Linux:   bash install-linux.sh
      - macOS:   Open  install-macos.command  in Finder (right-click → Open)


NOTES
-----
  • The offline/packages/ directory is intentionally excluded from git
    (it is listed in .gitignore).  Re-run download_packages.py whenever
    the project's dependencies are updated.

  • PyTorch, CUDA, and ComfyUI itself are NOT included here — they are
    several GB in size.  Install them separately:

    PyTorch with CUDA:
      https://pytorch.org/get-started/locally/
      pip install torch torchvision torchaudio \
          --index-url https://download.pytorch.org/whl/cu124

    ComfyUI (via the monitor's built-in installer):
      Run the main launcher (watchupdog-windows.bat etc.)
      and choose option  I  from the menu.

  • If a package fails to download or install, the install scripts
    will fall back to internet for that specific package.


PACKAGE LIST (core dependencies)
----------------------------------
  rich>=13.0          Terminal UI rendering
  click>=8.1          CLI framework
  httpx>=0.27         HTTP client for ComfyUI API calls
  pydantic>=2.0       Data validation
  psutil>=5.9         CPU/RAM metrics
  tomli>=2.0          TOML config file parsing  (Python < 3.11)
  setuptools          Build tools (for editable install)
  wheel               Build tools (for editable install)

Optional (downloaded by default, skip with --no-optional):
  pynvml>=11.0        NVIDIA GPU VRAM metrics
  fastapi>=0.110      Web dashboard server
  uvicorn>=0.29       ASGI server
  websockets>=12.0    WebSocket client
  aiohttp>=3.9        Async HTTP
