#!/usr/bin/env bash
# ============================================================
#  watchupdog — Linux launcher
#  Drop this file anywhere next to the monitor folder.
#  Compatible with: Ubuntu, Debian, Arch, Fedora, RHEL,
#                   Pop!_OS, Mint, and most other distros.
#
#  First time on a fresh machine?
#    Run the setup script first:   bash install-linux.sh
#    It installs Python, Git, and all required packages.
#
#  To run this launcher directly (two options):
#    Option A:  bash watchupdog-linux.sh
#    Option B:  chmod +x watchupdog-linux.sh
#               ./watchupdog-linux.sh
#  (The install script sets +x automatically.)
# ============================================================

# DO NOT use set -e here; health checks may return non-zero
# and that should not kill the menu.

MONITOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=""
COMFYUI_PATH=""
# Leave URL empty — Python will auto-detect the live instance.
# Only set this if the TOML config specifies a URL or the user enters a port below.
COMFYUI_URL=""

# Detect architecture for informational display
ARCH=$(uname -m 2>/dev/null || echo "unknown")

# Tell Python to use UTF-8 for all I/O
export PYTHONUTF8=1

# ── 1. Find Python ───────────────────────────────────────────
_try_python() {
    local py="$1"
    if command -v "$py" &>/dev/null; then
        local ver
        ver=$("$py" -c "import sys; print(sys.version_info >= (3,9))" 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON=$(command -v "$py")
            return 0
        fi
    fi
    return 1
}

# Active virtual environment or conda env — use it immediately
if [ -n "$VIRTUAL_ENV" ] && [ -f "$VIRTUAL_ENV/bin/python3" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python3"
elif [ -n "$CONDA_PREFIX" ] && [ -f "$CONDA_PREFIX/bin/python3" ]; then
    PYTHON="$CONDA_PREFIX/bin/python3"
elif [ -n "$CONDA_EXE" ]; then
    _CONDA_BASE="$(dirname "$(dirname "$CONDA_EXE")")"
    [ -f "$_CONDA_BASE/bin/python3" ] && PYTHON="$_CONDA_BASE/bin/python3"
fi

# PATH candidates — try newest first
if [ -z "$PYTHON" ]; then
for candidate in python3.16 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python py; do
    _try_python "$candidate" && break
done
fi

# Conda / Mamba / Miniforge (common Linux locations)
if [ -z "$PYTHON" ]; then
    for conda_base in \
        "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" \
        "$HOME/miniforge3" "$HOME/.conda" \
        "/opt/conda" "/opt/miniconda3" "/opt/anaconda3" \
        "/usr/local/conda" "/usr/local/miniconda3"; do
        for py_path in \
            "$conda_base/bin/python3" \
            "$conda_base/envs/comfyui/bin/python3" \
            "$conda_base/envs/comfyui3/bin/python3"; do
            if [ -f "$py_path" ]; then
                PYTHON="$py_path"
                break 2
            fi
        done
    done
fi

# Pyenv
if [ -z "$PYTHON" ] && [ -f "$HOME/.pyenv/shims/python3" ]; then
    PYTHON="$HOME/.pyenv/shims/python3"
fi

if [ -z "$PYTHON" ]; then
    echo "[FAIL] Python 3.9+ not found."
    echo "       Install with: sudo apt install python3  OR  sudo dnf install python3"
    echo "       Or download from: https://python.org"
    read -rp "Press Enter to exit..."
    exit 1
fi
echo "[OK] Python: $PYTHON  (arch: $ARCH)"

# Warn on 32-bit — PyTorch and ComfyUI require 64-bit
_PYBITS=$("$PYTHON" -c "import struct; print(struct.calcsize('P') * 8)" 2>/dev/null)
if [ "$_PYBITS" = "32" ]; then
    echo ""
    echo "[WARN] 32-bit Python detected. PyTorch and ComfyUI require 64-bit Python."
    echo "       Install 64-bit Python: sudo apt install python3  (or via python.org)"
    echo ""
fi

# ── 2. pip available? ────────────────────────────────────────
"$PYTHON" -m pip --version &>/dev/null || {
    echo "[INFO] pip not found — running ensurepip..."
    "$PYTHON" -m ensurepip --upgrade 2>/dev/null || {
        echo "[FAIL] pip unavailable. Install with: sudo apt install python3-pip"
        read -rp "Press Enter to exit..."
        exit 1
    }
}

# ── 3. Core deps ─────────────────────────────────────────────
"$PYTHON" -c "import rich, click, httpx, pydantic" 2>/dev/null || {
    echo "[INFO] Installing core packages..."
    "$PYTHON" -m pip install rich click "httpx>=0.27" "pydantic>=2.0" psutil tomli --quiet 2>/dev/null \
    || "$PYTHON" -m pip install rich click "httpx>=0.27" "pydantic>=2.0" psutil tomli --user --quiet 2>/dev/null
}

# ── 4. Install watchupdog ─────────────────────────────────
"$PYTHON" -c "import watchupdog" 2>/dev/null || {
    echo "[INFO] Installing watchupdog..."
    "$PYTHON" -m pip install -e "$MONITOR_DIR" --quiet 2>/dev/null \
    || "$PYTHON" -m pip install -e "$MONITOR_DIR" --user --quiet 2>/dev/null
}
"$PYTHON" -c "import watchupdog" 2>/dev/null || {
    echo "[FAIL] Could not install watchupdog. Check your internet connection."
    read -rp "Press Enter to exit..."
    exit 1
}

# ── 5. Find ComfyUI ───────────────────────────────────────────
COMFYUI_PATH=$("$PYTHON" "$MONITOR_DIR/find_comfyui.py" 2>/dev/tty || true)
if [ -z "$COMFYUI_PATH" ]; then
    echo ""
    echo "[WARN] ComfyUI not found automatically."
    echo "       Tip: folder must contain main.py or server.py"
    echo ""
    read -rp "Enter full path to your ComfyUI folder (or Enter to skip): " COMFYUI_PATH
    echo ""
    read -rp "Enter ComfyUI port if different from 8188 (or Enter to skip): " _PORTINPUT
    [ -n "$_PORTINPUT" ] && COMFYUI_URL="http://127.0.0.1:$_PORTINPUT"
fi

# ── 6. Read URL from config ───────────────────────────────────
TOML="$MONITOR_DIR/watchupdog.toml"
if [ -f "$TOML" ]; then
    _CFG_URL=$("$PYTHON" -c "
import re, pathlib
p = pathlib.Path('$TOML')
t = p.read_text() if p.exists() else ''
m = re.search(r'url\s*=\s*\"([^\"]+)\"', t)
print(m.group(1) if m else '')
" 2>/dev/null || true)
    [ -n "$_CFG_URL" ] && COMFYUI_URL="$_CFG_URL"
fi

# ── Launch interactive menu ───────────────────────────────────
_LAUNCHER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
while true; do
    "$PYTHON" -m watchupdog.interactive_menu \
        --url "$COMFYUI_URL" \
        --monitor-dir "$MONITOR_DIR" \
        --comfyui-path "$COMFYUI_PATH" \
        --launcher "$_LAUNCHER"
    echo ""
    read -rp "  R = Relaunch   X = Exit: " _choice
    case "${_choice,,}" in
        r) continue ;;
        *) break ;;
    esac
done
